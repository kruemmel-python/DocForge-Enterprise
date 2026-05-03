from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from smql_embedding_adapter.adapter import EmbeddingAdapter
from smql_embedding_adapter.config import (
    AdapterConfig,
    LMStudioConfig,
    MyceliaConfig,
    Settings as SMQLSettings,
)

from .config import Settings
from .models import CodeShard, RetrievedContext


@dataclass(slots=True)
class SemanticIndex:
    adapter: EmbeddingAdapter = field(init=False)

    """MyceliaDB/SMQL-backed semantic code index.

    The adapter always keeps a local mmap sidecar vault. If MyceliaDB is enabled
    and reachable, records are also sent to the gateway. Retrieval policy is
    controlled by settings.mycelia.search_backend.
    """

    settings: Settings
    collection: str

    def __post_init__(self) -> None:
        smql_settings = SMQLSettings(
            adapter=AdapterConfig(
                vault_path=self.settings.mycelia.vault_path,
                default_collection=self.collection,
                default_dimension=self.settings.mycelia.default_dimension,
                store_text_default=self.settings.mycelia.store_text,
                search_backend=self.settings.mycelia.search_backend,
                sealed_mode=self.settings.mycelia.sealed_mode,
            ),
            lmstudio=LMStudioConfig(
                base_url=self.settings.lmstudio.base_url,
                embedding_model=self.settings.lmstudio.embedding_model,
                chat_model=self.settings.lmstudio.chat_model,
                timeout_seconds=self.settings.lmstudio.embedding_timeout_seconds,
                enabled=not self.settings.pipeline.dry_run,
            ),
            mycelia=MyceliaConfig(
                base_url=self.settings.mycelia.base_url,
                token=self.settings.mycelia.token,
                timeout_seconds=self.settings.lmstudio.gateway_timeout_seconds,
                enabled=self.settings.mycelia.enabled and not self.settings.pipeline.dry_run,
            ),
        )
        self.adapter = EmbeddingAdapter(smql_settings)

    @staticmethod
    def embedding_text(shard: CodeShard) -> str:
        symbol_line = ", ".join(shard.symbols)
        return f"""KIND: {shard.kind}
FILE: {shard.file_path}
LANGUAGE: {shard.language}
SPAN: {shard.char_start}-{shard.char_end}
SYMBOLS: {symbol_line}

CONTENT:
{shard.content}
""".strip()

    def ingest(self, shards: Iterable[CodeShard], *, batch_size: int = 32) -> list[dict[str, Any]]:
        shard_list = list(shards)
        results: list[dict[str, Any]] = []
        actual_batch_size = max(1, min(int(batch_size), self.settings.pipeline.max_embedding_batch_size))

        for i in range(0, len(shard_list), actual_batch_size):
            batch = shard_list[i:i + actual_batch_size]
            texts = [self.embedding_text(shard) for shard in batch]
            ids = [shard.id for shard in batch]
            metadata: list[Mapping[str, Any]] = [
                {
                    "kind": shard.kind,
                    "file_path": shard.file_path,
                    "language": shard.language,
                    "start": shard.char_start,
                    "end": shard.char_end,
                    "sha256": shard.sha256,
                    "symbols": list(shard.symbols),
                    "source": shard.file_path,
                }
                for shard in batch
            ]
            try:
                result = self.adapter.ingest_texts(
                    texts,
                    ids=ids,
                    metadata=metadata,
                    collection=self.collection,
                    store_text=self.settings.mycelia.store_text,
                )
                results.append(
                    {
                        "collection": result.collection,
                        "count": result.count,
                        "merkle_head": result.merkle_head,
                        "mycelia_status": result.mycelia_status,
                        "status": "ok",
                        "batch_start": i,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - embedding failure should not kill documentation
                results.append(
                    {
                        "collection": self.collection,
                        "count": 0,
                        "merkle_head": "",
                        "mycelia_status": "failed",
                        "status": "error",
                        "batch_start": i,
                        "error": str(exc),
                    }
                )
                if not self.settings.pipeline.continue_on_timeout:
                    raise
        return results

    def query(self, query: str, *, limit: int | None = None) -> tuple[list[RetrievedContext], dict[str, Any]]:
        actual_limit = self.settings.pipeline.retrieval_limit if limit is None else limit
        result = self.adapter.query_text(query, collection=self.collection, limit=actual_limit)
        contexts: list[RetrievedContext] = []
        for item in result.results:
            record = item.record
            contexts.append(
                RetrievedContext(
                    id=record.id,
                    score=float(item.score),
                    file_path=str(record.metadata.get("file_path", "")),
                    text=record.text or record.text_preview,
                    metadata=dict(record.metadata),
                )
            )
        meta = {
            "collection": result.collection,
            "count": result.count,
            "merkle_head": result.merkle_head,
            "mycelia_status": result.mycelia_status,
            "retrieval_backend": result.retrieval_backend,
            "mycelia_native": result.mycelia_native,
            "sealed_attestation": result.sealed_attestation,
        }
        return contexts, meta
