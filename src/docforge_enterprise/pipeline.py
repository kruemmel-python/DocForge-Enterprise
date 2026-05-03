from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .audit import append_audit_section, validate_claims
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
from .semantic_index import SemanticIndex
from .sharding import ShardPlan, shard_project
from .store import AnalysisStore


@dataclass(slots=True)
class PipelineResult:
    output_paths: dict[str, str]
    metadata: dict[str, Any]
    workspace: Path


class PipelineIntegrityError(RuntimeError):
    """Raised when a mandatory pipeline stage has inconsistent state."""


class DocumentationPipeline:
    def __init__(self, *, input_path: Path, settings: Settings):
        self.input_path = input_path
        self.settings = settings
        self.settings.normalize()
        self.workspace = settings.pipeline.workspace
        if settings.pipeline.force_rebuild and self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.project_name = settings.pipeline.project_name or input_path.stem
        self.stats = PipelineStats()
        self.integrity_report: dict[str, Any] = {
            "status": "unknown",
            "files": [],
            "errors": [],
            "stage_counts": {},
            "debug": [],
        }
        self.store = AnalysisStore(self.workspace / "analysis" / "docforge.sqlite3")
        self.chat = LMStudioChatClient(settings.lmstudio)
        self.index = SemanticIndex(settings, f"{settings.mycelia.collection_prefix}_code")

    def close(self) -> None:
        self.store.close()


    def _language_name(self) -> str:
        return "English" if self.settings.pipeline.output_language == "en" else "Deutsch"

    def _language_instruction(self) -> str:
        if self.settings.pipeline.output_language == "en":
            return (
                "Output language: English. Write every natural-language field, heading, "
                "risk, note, and final documentation paragraph in English. Keep code identifiers, "
                "file paths and symbols unchanged."
            )
        return (
            "Ausgabesprache: Deutsch. Schreibe alle natürlichsprachlichen Felder, Überschriften, "
            "Risiken, Hinweise und finalen Dokumentationsabschnitte auf Deutsch. Code-Bezeichner, "
            "Dateipfade und Symbole bleiben unverändert."
        )

    def _system(self, base: str) -> str:
        return base + "\n" + self._language_instruction()

    def _prompt(self, text: str) -> str:
        return text + "\n\n" + self._language_instruction()

    def _chapter_plan(self) -> list[str]:
        return chapters_for_profile(
            self.settings.pipeline.profile,
            self.settings.pipeline.chapters,
            self.settings.pipeline.max_final_chapters,
        )

    def _estimate(self, files: list[ProjectFile], shards: list[CodeShard], chapters: list[str]) -> dict[str, Any]:
        modules = len({self._module_name_for(f.relative_path) for f in files})
        batches = (len(shards) + max(1, self.settings.pipeline.max_embedding_batch_size) - 1) // max(
            1, self.settings.pipeline.max_embedding_batch_size
        )
        if self.settings.pipeline.dry_run:
            shard = file = module = render = ret = 0
        else:
            shard = len(shards)
            file = len(files)
            module = 0 if self.settings.pipeline.disable_module_reduce else modules
            render = 1 if self.settings.pipeline.single_pass_final else len(chapters)
            ret = 1 if self.settings.pipeline.single_pass_final else len(chapters)
        return {
            "profile": self.settings.pipeline.profile,
            "output_language": self.settings.pipeline.output_language,
            "single_pass_final": self.settings.pipeline.single_pass_final,
            "disable_module_reduce": self.settings.pipeline.disable_module_reduce,
            "files": len(files),
            "shards": len(shards),
            "modules": modules,
            "chapters": len(chapters),
            "estimated_shard_analysis_calls": shard,
            "estimated_file_reduce_calls": file,
            "estimated_module_reduce_calls": module,
            "estimated_chapter_render_calls": render,
            "estimated_embedding_ingest_batches": batches,
            "estimated_retrieval_embedding_calls": ret,
            "estimated_llm_chat_calls": shard + file + module + render,
            "estimated_embedding_calls": batches + ret,
        }

    def run(self) -> PipelineResult:
        started = time.time()
        extracted = prepare_input(self.input_path, self.workspace, self.settings)

        files = list(iter_project_files(extracted, self.settings))
        self.stats.files_seen = len(files)
        self.stats.files_indexed = len(files)
        self.store.upsert_files(files)

        plan = ShardPlan(self.settings.pipeline.max_chars_per_shard, self.settings.pipeline.shard_overlap)
        shards = shard_project(files, plan)
        self.stats.shards_created = len(shards)
        self.store.upsert_shards(shards)

        chapters_plan = self._chapter_plan()
        estimate = self._estimate(files, shards, chapters_plan)
        self.stats.estimated_llm_chat_calls = estimate["estimated_llm_chat_calls"]
        self.stats.estimated_embedding_calls = estimate["estimated_embedding_calls"]
        print("[DocForge] Work estimate:", json.dumps(estimate, ensure_ascii=False))

        if self.settings.pipeline.estimate_only:
            self.integrity_report = self._build_integrity_report(files, require_analyses=False)
            metadata = self._metadata(started, [], estimate, chapters_plan, {})
            md = assemble_markdown(
                project_name=self.project_name,
                chapters=[
                    "## Work Estimate\n\n```json\n"
                    + json.dumps(estimate, ensure_ascii=False, indent=2)
                    + "\n```\n\n## Pipeline Integrity Preview\n\n```json\n"
                    + json.dumps(self.integrity_report, ensure_ascii=False, indent=2)
                    + "\n```"
                ],
                metadata=metadata,
            )
            paths = write_outputs(
                self.workspace / "output",
                project_name=self.project_name,
                markdown=md,
                metadata=metadata,
                emit_html=True,
                emit_json=True,
            )
            self._write_machine_outputs(files, shards, [], [], {})
            return PipelineResult(paths, metadata, self.workspace)

        ingest = self.index.ingest(shards, batch_size=self.settings.pipeline.batch_size)
        self.stats.actual_embedding_calls += max(1, estimate["estimated_embedding_ingest_batches"])

        for shard in shards:
            self._analyze_shard(shard)

        self.integrity_report = self._build_integrity_report(files, require_analyses=True)
        self._enforce_integrity_before_file_reduce()

        file_summaries = self._reduce_files(files)
        module_summaries = self._reduce_modules(file_summaries)
        final = self._generate_final(file_summaries, module_summaries, chapters_plan)

        records = self.store.list_analysis("shard") + file_summaries + module_summaries
        source_map = {f.relative_path: f.content for f in files}
        audit_report = validate_claims(records, source_map) if self.settings.audit.validate_claims else {}
        if audit_report:
            self.stats.claims_total = audit_report["claims_total"]
            self.stats.claims_supported = audit_report["claims_supported"]
            self.stats.claims_unsupported = audit_report["claims_unsupported"]
            self.stats.evidence_coverage_percent = audit_report["evidence_coverage_percent"]
            final = append_audit_section(final, audit_report)

        metadata = self._metadata(started, ingest, estimate, chapters_plan, audit_report)
        paths = write_outputs(
            self.workspace / "output",
            project_name=self.project_name,
            markdown=final,
            metadata=metadata,
            emit_html=True,
            emit_json=True,
        )
        self._write_machine_outputs(files, shards, file_summaries, module_summaries, audit_report)
        return PipelineResult(paths, metadata, self.workspace)

    def _metadata(self, started, ingest, estimate, chapters, audit_report):
        return {
            "project_name": self.project_name,
            "input": str(self.input_path),
            "workspace": str(self.workspace),
            "profile": self.settings.pipeline.profile,
            "output_language": self.settings.pipeline.output_language,
            "selected_chapters": chapters,
            "work_estimate": estimate,
            "pipeline_integrity_report": self.integrity_report,
            "audit_validation": audit_report,
            "started_at": started,
            "finished_at": time.time(),
            "duration_seconds": round(time.time() - started, 3),
            "stats": asdict(self.stats),
            "ingest_results": ingest,
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
            },
        }

    def _retrieve(self, query, target_id, limit=None):
        if self.settings.pipeline.dry_run:
            return []
        ctx, meta = self.index.query(query, limit=limit)
        self.stats.retrieval_events += 1
        self.stats.actual_embedding_calls += 1
        self.store.save_retrieval_event(query=query, target_id=target_id, metadata=meta)
        return [c for c in ctx if c.id != target_id]

    def _analyze_shard(self, shard: CodeShard):
        ctx = self._retrieve(f"{shard.file_path} {' '.join(shard.symbols)} {shard.content[:400]}", shard.id)
        if self.settings.pipeline.dry_run:
            payload = {
                "file_path": shard.file_path,
                "shard_id": shard.id,
                "purpose": "Dry-run shard summary",
                "important_symbols": list(shard.symbols),
                "dependencies": [],
                "business_rules": [],
                "interfaces": [],
                "security_notes": [],
                "operations_notes": [],
                "risks": ["Dry-run"],
                "documentation_notes": [],
                "evidence": [
                    {"file_path": shard.file_path, "span": f"{shard.char_start}-{shard.char_end}", "claim": "Shard indexed."}
                ],
            }
        else:
            try:
                payload = self.chat.chat_json(
                    system=self._system(SHARD_SYSTEM),
                    user=self._prompt(shard_prompt(shard, ctx)),
                    max_tokens=self.settings.lmstudio.max_json_tokens,
                )
                self.stats.actual_llm_chat_calls += 1
            except LLMError as e:
                self.stats.llm_failures += 1
                payload = {
                    "file_path": shard.file_path,
                    "shard_id": shard.id,
                    "purpose": "LLM shard analysis failed",
                    "important_symbols": list(shard.symbols),
                    "dependencies": [],
                    "business_rules": [],
                    "interfaces": [],
                    "security_notes": [],
                    "operations_notes": [],
                    "risks": [str(e)],
                    "documentation_notes": ["Manual review required."],
                    "evidence": [],
                }

        # Integrity hardening: never trust the model to echo mandatory identity fields.
        payload["file_path"] = shard.file_path
        payload["shard_id"] = shard.id
        payload.setdefault("evidence", [])
        payload.setdefault("risks", [])
        payload.setdefault("documentation_notes", [])

        self.store.save_analysis(AnalysisRecord(stable_id("shard", shard.id, shard.sha256), "shard", shard.id, payload))
        self.stats.shards_analyzed += 1
        return payload

    def _reduce_files(self, files: list[ProjectFile]):
        out = []
        for f in files:
            relevant = self._shard_analyses_for_file(f)
            expected_count = self.store.shard_count_for_file(f.relative_path)
            found_count = len(relevant)
            msg = f"[DocForge][Integrity] file={f.relative_path} expected_shards={expected_count} shard_analyses_found={found_count}"
            print(msg)

            if expected_count > 0 and found_count == 0 and not self.settings.pipeline.dry_run:
                error = (
                    f"No shard analyses found for {f.relative_path}; refusing to run File-Reduce with empty input. "
                    "This protects the documentation from weak summaries such as 'Keine Shard-Analysen verfügbar'."
                )
                self.stats.files_without_shard_analysis += 1
                self.stats.integrity_errors += 1
                if self.settings.pipeline.fail_on_missing_shards:
                    raise PipelineIntegrityError(error)
                payload = self._file_fallback_from_source(f, error)
                self.store.save_analysis(AnalysisRecord(stable_id("file", f.relative_path, f.sha256), "file", f.relative_path, payload))
                out.append(payload)
                continue

            if self.settings.pipeline.dry_run:
                payload = {
                    "file_path": f.relative_path,
                    "purpose": "Dry-run file summary",
                    "public_api": [],
                    "internal_logic": [],
                    "dependencies": [],
                    "business_rules": [],
                    "interfaces": [],
                    "security_notes": [],
                    "operations_notes": [],
                    "risks": ["Dry-run"],
                    "enterprise_notes": [],
                    "evidence": [{"file_path": f.relative_path, "claim": "File indexed."}],
                }
            else:
                try:
                    payload = self.chat.chat_json(
                        system=self._system(FILE_SYSTEM),
                        user=self._prompt(file_prompt(f.relative_path, relevant)),
                        max_tokens=self.settings.lmstudio.max_json_tokens,
                    )
                    self.stats.actual_llm_chat_calls += 1
                except LLMError as e:
                    self.stats.llm_failures += 1
                    payload = self._file_fallback_from_source(f, f"File reduction failed: {e}")

            payload["file_path"] = f.relative_path
            payload.setdefault("evidence", [])
            self.store.save_analysis(AnalysisRecord(stable_id("file", f.relative_path, f.sha256), "file", f.relative_path, payload))
            out.append(payload)
        return out

    def _shard_analyses_for_file(self, f: ProjectFile) -> list[dict[str, Any]]:
        analyses = []
        for shard_id in self.store.shard_ids_for_file(f.relative_path):
            payload = self.store.get_analysis("shard", shard_id)
            if payload is None:
                continue
            if payload.get("file_path") != f.relative_path:
                # Repair old or malformed payloads, but keep an integrity note.
                payload = dict(payload)
                payload["file_path"] = f.relative_path
                payload.setdefault("documentation_notes", [])
                if isinstance(payload["documentation_notes"], list):
                    payload["documentation_notes"].append("file_path repaired by pipeline integrity layer.")
            analyses.append(payload)

        # Compatibility fallback for old stores.
        if not analyses:
            analyses = [r for r in self.store.list_analysis("shard") if r.get("file_path") == f.relative_path]
        return analyses

    def _file_fallback_from_source(self, f: ProjectFile, reason: str) -> dict[str, Any]:
        lines = f.content.splitlines()
        preview = "\n".join(lines[:40])
        return {
            "file_path": f.relative_path,
            "purpose": f"Fallback summary generated from original source because File-Reduce could not use shard analyses. Reason: {reason}",
            "public_api": [],
            "internal_logic": [preview[:2000]] if preview else [],
            "dependencies": [],
            "business_rules": [],
            "interfaces": [],
            "security_notes": [],
            "operations_notes": [],
            "risks": [reason, "File-level summary may be incomplete because shard analysis was unavailable."],
            "enterprise_notes": ["Pipeline integrity fallback. Re-run with shard stage diagnostics."],
            "evidence": [{"file_path": f.relative_path, "claim": "Original file was available for fallback summary."}],
        }

    def _module_name_for(self, path):
        return Path(path).parts[0] if len(Path(path).parts) > 1 else "root"

    def _reduce_modules(self, files: list[dict]):
        groups = {}
        for f in files:
            groups.setdefault(self._module_name_for(str(f.get("file_path", "root"))), []).append(f)
        out = []
        for name, summaries in groups.items():
            if self.settings.pipeline.disable_module_reduce or self.settings.pipeline.dry_run:
                payload = {
                    "module_name": name,
                    "responsibility": "Structural module summary",
                    "files": [s.get("file_path", "") for s in summaries],
                    "main_flows": [],
                    "dependencies": sorted({d for s in summaries for d in s.get("dependencies", []) if isinstance(d, str)}),
                    "interfaces": sorted({i for s in summaries for i in s.get("interfaces", []) if isinstance(i, str)}),
                    "security_notes": sorted({n for s in summaries for n in s.get("security_notes", []) if isinstance(n, str)}),
                    "operations_notes": [],
                    "risks": ["LLM module reduction disabled."] if self.settings.pipeline.disable_module_reduce else [],
                    "evidence": [],
                }
            else:
                try:
                    payload = self.chat.chat_json(
                        system=self._system(MODULE_SYSTEM),
                        user=self._prompt(module_prompt(name, summaries)),
                        max_tokens=self.settings.lmstudio.max_json_tokens,
                    )
                    self.stats.actual_llm_chat_calls += 1
                except LLMError as e:
                    self.stats.llm_failures += 1
                    payload = {
                        "module_name": name,
                        "responsibility": "Module reduction failed",
                        "files": [s.get("file_path", "") for s in summaries],
                        "main_flows": [],
                        "dependencies": [],
                        "interfaces": [],
                        "security_notes": [],
                        "operations_notes": [],
                        "risks": [str(e)],
                        "evidence": [],
                    }
            payload["module_name"] = name
            self.store.save_analysis(AnalysisRecord(stable_id("module", name, json.dumps(summaries, sort_keys=True)), "module", name, payload))
            out.append(payload)
        return out

    def _generate_final(self, files, modules, chapters):
        if self.settings.pipeline.dry_run:
            parts = [fallback_chapter(c, project_name=self.project_name, module_summaries=modules, file_summaries=files) for c in chapters]
            return assemble_markdown(project_name=self.project_name, chapters=parts, metadata={})
        if self.settings.pipeline.single_pass_final:
            ctx = self._retrieve(f"{self.project_name} quick documentation architecture security", "final")
            try:
                text = self.chat.chat(
                    system=self._system(CHAPTER_SYSTEM),
                    user=self._prompt(one_pass_document_prompt(self.project_name, chapters, modules, files, ctx)),
                    max_tokens=self.settings.lmstudio.max_chapter_tokens,
                    timeout=self.settings.lmstudio.final_timeout_seconds,
                )
                self.stats.actual_llm_chat_calls += 1
                return text if text.lstrip().startswith("#") else assemble_markdown(project_name=self.project_name, chapters=[text], metadata={})
            except LLMError:
                pass
        parts = []
        for c in chapters:
            ctx = self._retrieve(f"{self.project_name} {c} architecture security operations interfaces configuration", f"chapter:{c}")
            try:
                t = self.chat.chat(
                    system=self._system(CHAPTER_SYSTEM),
                    user=self._prompt(chapter_prompt(self.project_name, c, modules, files, ctx)),
                    max_tokens=self.settings.lmstudio.max_chapter_tokens,
                    timeout=self.settings.lmstudio.final_timeout_seconds,
                )
                self.stats.actual_llm_chat_calls += 1
            except LLMError:
                t = fallback_chapter(c, project_name=self.project_name, module_summaries=modules, file_summaries=files)
            parts.append(t if t.lstrip().startswith("#") else f"## {c}\n\n{t}\n")
        return assemble_markdown(project_name=self.project_name, chapters=parts, metadata={})

    def _build_integrity_report(self, files: list[ProjectFile], *, require_analyses: bool) -> dict[str, Any]:
        items = []
        errors = []
        total_shards = 0
        total_analyses = 0
        for f in files:
            shard_ids = self.store.shard_ids_for_file(f.relative_path)
            expected = len(shard_ids)
            found = sum(1 for shard_id in shard_ids if self.store.get_analysis("shard", shard_id) is not None)
            total_shards += expected
            total_analyses += found
            status = "ok"
            if require_analyses and expected > 0 and found == 0:
                status = "error"
                errors.append(f"{f.relative_path}: expected {expected} shard analysis record(s), found 0")
            elif require_analyses and found < expected:
                status = "warning"
                errors.append(f"{f.relative_path}: expected {expected} shard analysis record(s), found {found}")
            item = {
                "file_path": f.relative_path,
                "expected_shards": expected,
                "shard_analyses_found": found,
                "status": status,
            }
            items.append(item)
            if self.settings.pipeline.integrity_debug:
                print(
                    f"[DocForge][Integrity] stage=post-shard file={f.relative_path} "
                    f"expected_shards={expected} shard_analyses_found={found} status={status}"
                )
        report = {
            "status": "ok" if not errors else "error",
            "files": items,
            "errors": errors,
            "stage_counts": {
                "files": len(files),
                "expected_shards": total_shards,
                "shard_analysis_records": total_analyses,
                "file_records": len(self.store.list_analysis("file")),
                "module_records": len(self.store.list_analysis("module")),
            },
        }
        return report

    def _enforce_integrity_before_file_reduce(self) -> None:
        errors = self.integrity_report.get("errors", [])
        self.stats.integrity_errors = len(errors)
        self.stats.files_without_shard_analysis = sum(
            1 for item in self.integrity_report.get("files", []) if item.get("expected_shards", 0) > 0 and item.get("shard_analyses_found", 0) == 0
        )
        if errors and self.settings.pipeline.fail_on_missing_shards:
            raise PipelineIntegrityError(
                "Pipeline integrity check failed before File-Reduce:\n" + "\n".join(str(e) for e in errors)
            )

    def _write_machine_outputs(self, files, shards, file_summaries, module_summaries, audit_report):
        out = self.workspace / "analysis"
        out.mkdir(parents=True, exist_ok=True)
        (out / "files.json").write_text(
            json.dumps([asdict(f) | {"path": str(f.path)} for f in files], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (out / "shards.json").write_text(json.dumps([asdict(s) for s in shards], ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "file_summaries.json").write_text(json.dumps(file_summaries, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "module_summaries.json").write_text(json.dumps(module_summaries, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "audit_validation.json").write_text(json.dumps(audit_report, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "pipeline_integrity_report.json").write_text(json.dumps(self.integrity_report, ensure_ascii=False, indent=2), encoding="utf-8")
