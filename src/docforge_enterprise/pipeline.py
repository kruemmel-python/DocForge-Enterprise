from __future__ import annotations

import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .extractor import iter_project_files, prepare_input
from .hashing import stable_id
from .lmstudio import LLMError, LMStudioChatClient
from .models import AnalysisRecord, CodeShard, PipelineStats, ProjectFile, RetrievedContext
from .prompts import (
    CHAPTER_SYSTEM,
    FILE_SYSTEM,
    MODULE_SYSTEM,
    SHARD_SYSTEM,
    chapter_prompt,
    file_prompt,
    module_prompt,
    one_pass_document_prompt,
    shard_prompt,
)
from .renderer import assemble_markdown, chapters_for_profile, fallback_chapter, write_outputs
from .resilience import is_timeout_exception
from .semantic_index import SemanticIndex
from .sharding import ShardPlan, shard_project
from .store import AnalysisStore


@dataclass(slots=True)
class PipelineResult:
    output_paths: dict[str, str]
    metadata: dict[str, Any]
    workspace: Path


class DocumentationPipeline:
    def __init__(self, *, input_path: Path, settings: Settings) -> None:
        self.input_path = input_path
        self.settings = settings
        self.settings.normalize()
        self.workspace = settings.pipeline.workspace
        if settings.pipeline.force_rebuild and self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.project_name = settings.pipeline.project_name or input_path.stem
        self.stats = PipelineStats()
        self.store = AnalysisStore(self.workspace / "analysis" / "docforge.sqlite3")
        self.chat = LMStudioChatClient(settings.lmstudio)
        self.index = SemanticIndex(
            settings=settings,
            collection=f"{settings.mycelia.collection_prefix}_code",
        )

    def run(self) -> PipelineResult:
        started = time.time()

        extracted = prepare_input(self.input_path, self.workspace, self.settings)

        files = list(iter_project_files(extracted, self.settings))
        self.stats.files_seen = len(files)
        self.stats.files_indexed = len(files)
        self.store.upsert_files(files)

        plan = ShardPlan(
            max_chars=self.settings.pipeline.max_chars_per_shard,
            overlap=self.settings.pipeline.shard_overlap,
        )
        shards = shard_project(files, plan)
        self.stats.shards_created = len(shards)
        self.store.upsert_shards(shards)

        selected_chapters = self._chapter_plan()
        estimate = self._estimate_work(files=files, shards=shards, chapter_count=len(selected_chapters))
        self.stats.estimated_llm_chat_calls = int(estimate["estimated_llm_chat_calls"])
        self.stats.estimated_embedding_calls = int(estimate["estimated_embedding_calls"])
        if self.settings.pipeline.explain_llm_calls:
            print("[DocForge] Work estimate:")
            for key, value in estimate.items():
                print(f"[DocForge]   {key}: {value}")

        if self.settings.pipeline.estimate_only:
            ingest_results = []
            file_summaries: list[dict[str, Any]] = []
            module_summaries: list[dict[str, Any]] = []
            chapters = [
                "## Work Estimate\n\n"
                "This run was executed with `--estimate-only`; no LM Studio calls were performed.\n\n"
                "```json\n"
                + json.dumps(estimate, ensure_ascii=False, indent=2)
                + "\n```\n"
            ]
            metadata = self._metadata(started, ingest_results, estimate=estimate)
            markdown = assemble_markdown(project_name=self.project_name, chapters=chapters, metadata=metadata)
            paths = write_outputs(
                self.workspace / "output",
                project_name=self.project_name,
                markdown=markdown,
                metadata=metadata,
                emit_html=self.settings.pipeline.emit_html,
                emit_json=self.settings.pipeline.emit_json,
            )
            self._write_machine_outputs(files, shards, file_summaries, module_summaries)
            return PipelineResult(output_paths=paths, metadata=metadata, workspace=self.workspace)

        ingest_results = self.index.ingest(shards, batch_size=self.settings.pipeline.batch_size)
        self.stats.embedding_failures = sum(1 for item in ingest_results if item.get("status") == "error")
        self.stats.actual_embedding_calls += max(1, (len(shards) + max(1, self.settings.pipeline.max_embedding_batch_size) - 1) // max(1, self.settings.pipeline.max_embedding_batch_size))

        self._checkpoint("indexed", {"files": len(files), "shards": len(shards), "embedding_failures": self.stats.embedding_failures, "estimate": estimate})
        self._analyze_shards(shards)

        file_summaries = self._reduce_files(files)
        module_summaries = self._reduce_modules(file_summaries)
        chapters = self._generate_chapters(file_summaries, module_summaries, selected_chapters)

        metadata = self._metadata(started, ingest_results, estimate=estimate, selected_chapters=selected_chapters)

        markdown = assemble_markdown(project_name=self.project_name, chapters=chapters, metadata=metadata)
        paths = write_outputs(
            self.workspace / "output",
            project_name=self.project_name,
            markdown=markdown,
            metadata=metadata,
            emit_html=self.settings.pipeline.emit_html,
            emit_json=self.settings.pipeline.emit_json,
        )

        self._write_machine_outputs(files, shards, file_summaries, module_summaries)
        return PipelineResult(output_paths=paths, metadata=metadata, workspace=self.workspace)


    def _chapter_plan(self) -> list[str]:
        return chapters_for_profile(
            self.settings.pipeline.profile,
            self.settings.pipeline.chapters,
            self.settings.pipeline.max_final_chapters,
        )

    def _estimate_work(self, *, files: list[ProjectFile], shards: list[CodeShard], chapter_count: int) -> dict[str, Any]:
        module_count = len({self._module_name_for(f.relative_path) for f in files})
        embedding_batches = max(
            1 if shards else 0,
            (len(shards) + max(1, self.settings.pipeline.max_embedding_batch_size) - 1)
            // max(1, self.settings.pipeline.max_embedding_batch_size),
        )
        retrieval_embedding_calls = 0 if self.settings.pipeline.dry_run else len(shards)
        if self.settings.pipeline.single_pass_final:
            retrieval_embedding_calls += 0 if self.settings.pipeline.dry_run else 1
            chapter_calls = 0 if self.settings.pipeline.dry_run else 1
        else:
            retrieval_embedding_calls += 0 if self.settings.pipeline.dry_run else chapter_count
            chapter_calls = 0 if self.settings.pipeline.dry_run else chapter_count

        shard_calls = 0 if self.settings.pipeline.dry_run else len(shards)
        file_calls = 0 if self.settings.pipeline.dry_run else len(files)
        module_calls = 0 if (self.settings.pipeline.dry_run or self.settings.pipeline.disable_module_reduce) else module_count
        total_chat = shard_calls + file_calls + module_calls + chapter_calls
        total_embeddings = embedding_batches + retrieval_embedding_calls

        return {
            "profile": self.settings.pipeline.profile,
            "single_pass_final": self.settings.pipeline.single_pass_final,
            "disable_module_reduce": self.settings.pipeline.disable_module_reduce,
            "files": len(files),
            "shards": len(shards),
            "modules": module_count,
            "chapters": chapter_count,
            "estimated_shard_analysis_calls": shard_calls,
            "estimated_file_reduce_calls": file_calls,
            "estimated_module_reduce_calls": module_calls,
            "estimated_chapter_render_calls": chapter_calls,
            "estimated_embedding_ingest_batches": embedding_batches,
            "estimated_retrieval_embedding_calls": retrieval_embedding_calls,
            "estimated_llm_chat_calls": total_chat,
            "estimated_embedding_calls": total_embeddings,
        }

    def _metadata(
        self,
        started: float,
        ingest_results: list[dict[str, Any]],
        *,
        estimate: dict[str, Any],
        selected_chapters: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "input": str(self.input_path),
            "workspace": str(self.workspace),
            "started_at": started,
            "finished_at": time.time(),
            "duration_seconds": round(time.time() - started, 3),
            "profile": self.settings.pipeline.profile,
            "selected_chapters": selected_chapters or self._chapter_plan(),
            "work_estimate": estimate,
            "stats": asdict(self.stats),
            "ingest_results": ingest_results,
            "lmstudio": {
                "base_url": self.settings.lmstudio.base_url,
                "chat_model": self.settings.lmstudio.chat_model,
                "embedding_model": self.settings.lmstudio.embedding_model,
            },
            "mycelia": {
                "enabled": self.settings.mycelia.enabled,
                "base_url": self.settings.mycelia.base_url,
                "vault_path": str(self.settings.mycelia.vault_path),
                "collection_prefix": self.settings.mycelia.collection_prefix,
                "search_backend": self.settings.mycelia.search_backend,
                "sealed_mode": self.settings.mycelia.sealed_mode,
            },
        }

    def close(self) -> None:
        self.store.close()


    def _analyze_shards(self, shards: list[CodeShard]) -> None:
        workers = min(max(1, int(self.settings.pipeline.analysis_workers)), max(1, int(self.settings.pipeline.max_analysis_workers)))
        if workers == 1 or self.settings.pipeline.dry_run:
            for shard in shards:
                self._analyze_shard(shard)
            self.stats.json_repairs = self.chat.json_repairs
            return

        # Bounded parallelism: shard analysis is I/O-heavy (HTTP to LM Studio).
        # Keep worker count modest because one local model is usually GPU/CPU bound.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="docforge-shard") as executor:
            futures = {executor.submit(self._analyze_shard, shard): shard for shard in shards}
            for future in as_completed(futures):
                shard = futures[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - isolate one shard failure from the run
                    self.stats.llm_failures += 1
                    if is_timeout_exception(exc):
                        self.stats.timeouts += 1
                    payload = {
                        "file_path": shard.file_path,
                        "shard_id": shard.id,
                        "purpose": "Shard worker failed.",
                        "important_symbols": list(shard.symbols),
                        "dependencies": [],
                        "business_rules": [],
                        "interfaces": [],
                        "security_notes": [],
                        "operations_notes": [],
                        "risks": [str(exc)],
                        "documentation_notes": ["This shard requires manual review."],
                        "evidence": [],
                    }
                    self.store.save_analysis(
                        AnalysisRecord(
                            id=stable_id("shard-worker-failure", shard.id, shard.sha256),
                            stage="shard",
                            source_id=shard.id,
                            payload=payload,
                            status="ok",
                            error=str(exc),
                        )
                    )

        self.stats.json_repairs = self.chat.json_repairs


    def _checkpoint(self, stage: str, payload: dict[str, Any]) -> None:
        self.store.save_checkpoint(stage, payload | {"updated_at": time.time()})
        self.stats.checkpoint_writes += 1

    def _chat_json_with_adaptive_timeout(self, shard: CodeShard, contexts: list[RetrievedContext]) -> dict[str, Any]:
        """Analyze one shard. On timeout, optionally retry with a smaller prompt.

        This does not physically rewrite the shard plan. It shrinks the prompt surface:
        shorter code slice, fewer retrieved neighbors, lower output tokens. That is enough
        to rescue many local 7B/14B LM Studio runs without losing the whole pipeline.
        """
        try:
            return self.chat.chat_json(
                system=SHARD_SYSTEM,
                user=shard_prompt(shard, contexts),
                max_tokens=self.settings.lmstudio.max_json_tokens,
                timeout=self.settings.lmstudio.chat_timeout_seconds,
                label="lmstudio.shard_analysis",
            )
        except LLMError as exc:
            if not self.settings.pipeline.adaptive_shard_on_timeout or not is_timeout_exception(exc):
                raise

            self.stats.adaptive_shard_retries += 1
            reduced_content = shard.content[: max(self.settings.pipeline.min_chars_per_shard, 300)]
            reduced = CodeShard(
                id=shard.id,
                file_path=shard.file_path,
                language=shard.language,
                kind=shard.kind,
                content=reduced_content,
                char_start=shard.char_start,
                char_end=shard.char_start + len(reduced_content),
                sha256=shard.sha256,
                ordinal=shard.ordinal,
                symbols=shard.symbols,
            )
            return self.chat.chat_json(
                system=SHARD_SYSTEM,
                user=shard_prompt(reduced, contexts[:2]),
                max_tokens=max(700, self.settings.lmstudio.max_json_tokens // 2),
                timeout=self.settings.lmstudio.chat_timeout_seconds,
                label="lmstudio.shard_analysis.adaptive",
            )

    def _retrieve(self, query: str, target_id: str, limit: int | None = None) -> list[RetrievedContext]:
        if self.settings.pipeline.dry_run:
            return []
        try:
            contexts, meta = self.index.query(query, limit=limit)
            self.stats.retrieval_events += 1
            self.stats.actual_embedding_calls += 1
            self.store.save_retrieval_event(query=query, target_id=target_id, metadata=meta)
            return [ctx for ctx in contexts if ctx.id != target_id]
        except Exception as exc:
            self.store.save_retrieval_event(
                query=query,
                target_id=target_id,
                metadata={"status": "error", "error": str(exc)},
            )
            return []

    def _analyze_shard(self, shard: CodeShard) -> dict[str, Any]:
        cached = self.store.get_analysis("shard", shard.id)
        if cached is not None and not self.settings.pipeline.force_rebuild:
            return cached

        query = f"{shard.file_path} {shard.language} {' '.join(shard.symbols)} {shard.content[:500]}"
        contexts = self._retrieve(query, shard.id, self.settings.pipeline.retrieval_limit)

        if self.settings.pipeline.dry_run:
            payload = {
                "file_path": shard.file_path,
                "shard_id": shard.id,
                "purpose": f"Dry-run summary for {shard.file_path}",
                "important_symbols": list(shard.symbols),
                "dependencies": [],
                "business_rules": [],
                "interfaces": [],
                "security_notes": [],
                "operations_notes": [],
                "risks": ["Dry-run mode: no LLM analysis performed."],
                "documentation_notes": ["Use without --dry-run for full LM Studio analysis."],
                "evidence": [
                    {
                        "file_path": shard.file_path,
                        "span": f"{shard.char_start}-{shard.char_end}",
                        "claim": "Shard exists and was indexed.",
                    }
                ],
            }
        else:
            try:
                payload = self._chat_json_with_adaptive_timeout(shard, contexts)
                self.stats.actual_llm_chat_calls += 1
            except LLMError as exc:
                self.stats.llm_failures += 1
                if is_timeout_exception(exc):
                    self.stats.timeouts += 1
                payload = {
                    "file_path": shard.file_path,
                    "shard_id": shard.id,
                    "purpose": "LLM analysis failed.",
                    "important_symbols": list(shard.symbols),
                    "dependencies": [],
                    "business_rules": [],
                    "interfaces": [],
                    "security_notes": [],
                    "operations_notes": [],
                    "risks": [str(exc)],
                    "documentation_notes": ["This shard requires manual review."],
                    "evidence": [],
                }

        record = AnalysisRecord(
            id=stable_id("shard", shard.id, shard.sha256),
            stage="shard",
            source_id=shard.id,
            payload=payload,
            status="ok",
        )
        self.store.save_analysis(record)
        self.stats.shards_analyzed += 1
        if self.stats.shards_analyzed % self.settings.pipeline.checkpoint_every == 0:
            self._checkpoint("shard_analysis", {"shards_analyzed": self.stats.shards_analyzed})
        return payload

    def _reduce_files(self, files: list[ProjectFile]) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []

        for project_file in files:
            cached = self.store.get_analysis("file", project_file.relative_path)
            if cached is not None and not self.settings.pipeline.force_rebuild:
                summaries.append(cached)
                continue

            shard_ids = self.store.shard_ids_for_file(project_file.relative_path)
            shard_analyses = [
                self.store.get_analysis("shard", shard_id) for shard_id in shard_ids
            ]
            shard_analyses = [item for item in shard_analyses if item is not None]

            if self.settings.pipeline.dry_run:
                payload = {
                    "file_path": project_file.relative_path,
                    "purpose": f"Dry-run file summary for {project_file.relative_path}",
                    "public_api": [],
                    "internal_logic": [],
                    "dependencies": [],
                    "business_rules": [],
                    "interfaces": [],
                    "security_notes": [],
                    "operations_notes": [],
                    "risks": ["Dry-run mode."],
                    "enterprise_notes": [],
                    "evidence": [{"file_path": project_file.relative_path, "claim": "File was indexed."}],
                }
            else:
                try:
                    payload = self.chat.chat_json(
                        system=FILE_SYSTEM,
                        user=file_prompt(project_file.relative_path, shard_analyses),
                        max_tokens=self.settings.lmstudio.max_json_tokens,
                        timeout=self.settings.lmstudio.chat_timeout_seconds,
                        label="lmstudio.file_reduce",
                    )
                    self.stats.actual_llm_chat_calls += 1
                except LLMError as exc:
                    self.stats.llm_failures += 1
                    if is_timeout_exception(exc):
                        self.stats.timeouts += 1
                    payload = {
                        "file_path": project_file.relative_path,
                        "purpose": "File reduction failed.",
                        "public_api": [],
                        "internal_logic": [],
                        "dependencies": [],
                        "business_rules": [],
                        "interfaces": [],
                        "security_notes": [],
                        "operations_notes": [],
                        "risks": [str(exc)],
                        "enterprise_notes": ["Manual review required."],
                        "evidence": [],
                    }

            self.store.save_analysis(
                AnalysisRecord(
                    id=stable_id("file", project_file.relative_path, project_file.sha256),
                    stage="file",
                    source_id=project_file.relative_path,
                    payload=payload,
                )
            )
            summaries.append(payload)

        return summaries

    @staticmethod
    def _module_name_for(file_path: str) -> str:
        parts = Path(file_path).parts
        if len(parts) >= 2:
            return parts[0]
        return "root"

    def _reduce_modules(self, file_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for summary in file_summaries:
            path = str(summary.get("file_path", "root"))
            groups.setdefault(self._module_name_for(path), []).append(summary)

        if self.settings.pipeline.disable_module_reduce:
            module_summaries = []
            for module_name, summaries in groups.items():
                module_summaries.append(
                    {
                        "module_name": module_name,
                        "responsibility": "Structural module summary generated without LLM module reduction.",
                        "files": [str(item.get("file_path", "")) for item in summaries],
                        "main_flows": [],
                        "dependencies": sorted({dep for item in summaries for dep in item.get("dependencies", []) if isinstance(dep, str)}),
                        "interfaces": sorted({itf for item in summaries for itf in item.get("interfaces", []) if isinstance(itf, str)}),
                        "security_notes": sorted({note for item in summaries for note in item.get("security_notes", []) if isinstance(note, str)}),
                        "operations_notes": [],
                        "risks": ["Module LLM reduction disabled by selected profile/settings."],
                        "evidence": [],
                    }
                )
            return module_summaries

        module_summaries: list[dict[str, Any]] = []
        for module_name, summaries in groups.items():
            cached = self.store.get_analysis("module", module_name)
            if cached is not None and not self.settings.pipeline.force_rebuild:
                module_summaries.append(cached)
                continue

            if self.settings.pipeline.dry_run:
                payload = {
                    "module_name": module_name,
                    "responsibility": f"Dry-run module summary for {module_name}",
                    "files": [str(item.get("file_path", "")) for item in summaries],
                    "main_flows": [],
                    "dependencies": [],
                    "interfaces": [],
                    "security_notes": [],
                    "operations_notes": [],
                    "risks": ["Dry-run mode."],
                    "evidence": [],
                }
            else:
                try:
                    payload = self.chat.chat_json(
                        system=MODULE_SYSTEM,
                        user=module_prompt(module_name, summaries),
                        max_tokens=self.settings.lmstudio.max_json_tokens,
                        timeout=self.settings.lmstudio.chat_timeout_seconds,
                        label="lmstudio.module_reduce",
                    )
                    self.stats.actual_llm_chat_calls += 1
                except LLMError as exc:
                    self.stats.llm_failures += 1
                    if is_timeout_exception(exc):
                        self.stats.timeouts += 1
                    payload = {
                        "module_name": module_name,
                        "responsibility": "Module reduction failed.",
                        "files": [str(item.get("file_path", "")) for item in summaries],
                        "main_flows": [],
                        "dependencies": [],
                        "interfaces": [],
                        "security_notes": [],
                        "operations_notes": [],
                        "risks": [str(exc)],
                        "evidence": [],
                    }

            self.store.save_analysis(
                AnalysisRecord(
                    id=stable_id("module", module_name, json.dumps(summaries, sort_keys=True)),
                    stage="module",
                    source_id=module_name,
                    payload=payload,
                )
            )
            module_summaries.append(payload)

        return module_summaries

    def _generate_chapters(
        self,
        file_summaries: list[dict[str, Any]],
        module_summaries: list[dict[str, Any]],
        selected_chapters: list[str],
    ) -> list[str]:
        chapters: list[str] = []

        if self.settings.pipeline.dry_run:
            return [
                fallback_chapter(
                    title,
                    project_name=self.project_name,
                    module_summaries=module_summaries,
                    file_summaries=file_summaries,
                )
                for title in selected_chapters
            ]

        if self.settings.pipeline.single_pass_final:
            contexts = self._retrieve(
                f"{self.project_name} quick documentation architecture security operations interfaces configuration",
                target_id="document:single-pass",
                limit=self.settings.pipeline.retrieval_limit,
            )
            try:
                text = self.chat.chat(
                    system=CHAPTER_SYSTEM,
                    user=one_pass_document_prompt(
                        self.project_name,
                        selected_chapters,
                        module_summaries[: self.settings.pipeline.chapter_context_module_limit],
                        file_summaries[: self.settings.pipeline.chapter_context_file_limit],
                        contexts,
                    ),
                    temperature=0.2,
                    max_tokens=self.settings.lmstudio.max_chapter_tokens,
                    timeout=self.settings.lmstudio.final_timeout_seconds,
                    label="lmstudio.single_pass_final",
                )
                self.stats.actual_llm_chat_calls += 1
                return [text if text.lstrip().startswith("#") else f"## Dokumentation\n\n{text.strip()}\n"]
            except LLMError as exc:
                self.stats.llm_failures += 1
                if is_timeout_exception(exc):
                    self.stats.timeouts += 1
                return [
                    fallback_chapter(
                        title,
                        project_name=self.project_name,
                        module_summaries=module_summaries,
                        file_summaries=file_summaries,
                    )
                    for title in selected_chapters
                ]

        for title in selected_chapters:
            if self.settings.pipeline.explain_llm_calls:
                print(f"[DocForge] Rendering chapter: {title}")
            contexts = self._retrieve(
                f"{self.project_name} {title} architecture security operations interfaces configuration",
                target_id=f"chapter:{title}",
                limit=self.settings.pipeline.retrieval_limit,
            )
            try:
                text = self.chat.chat(
                    system=CHAPTER_SYSTEM,
                    user=chapter_prompt(
                        self.project_name,
                        title,
                        module_summaries[: self.settings.pipeline.chapter_context_module_limit],
                        file_summaries[: self.settings.pipeline.chapter_context_file_limit],
                        contexts,
                    ),
                    temperature=0.2,
                    max_tokens=self.settings.lmstudio.max_chapter_tokens,
                    timeout=self.settings.lmstudio.final_timeout_seconds,
                    label="lmstudio.chapter_render",
                )
                self.stats.actual_llm_chat_calls += 1
            except LLMError as exc:
                text = fallback_chapter(
                    title,
                    project_name=self.project_name,
                    module_summaries=module_summaries,
                    file_summaries=file_summaries,
                )
                self.stats.llm_failures += 1
                if is_timeout_exception(exc):
                    self.stats.timeouts += 1

            if not text.lstrip().startswith("#"):
                text = f"## {title}\n\n{text.strip()}\n"
            chapters.append(text)

        return chapters

    def _write_machine_outputs(
        self,
        files: list[ProjectFile],
        shards: list[CodeShard],
        file_summaries: list[dict[str, Any]],
        module_summaries: list[dict[str, Any]],
    ) -> None:
        out = self.workspace / "analysis"
        out.mkdir(parents=True, exist_ok=True)
        (out / "files.json").write_text(
            json.dumps([asdict(f) | {"path": str(f.path)} for f in files], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out / "shards.json").write_text(
            json.dumps([asdict(s) for s in shards], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out / "file_summaries.json").write_text(
            json.dumps(file_summaries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out / "module_summaries.json").write_text(
            json.dumps(module_summaries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
