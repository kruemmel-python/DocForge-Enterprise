"""Mmap-backed vector store."""

from __future__ import annotations

import heapq
import json
import mmap
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .merkle import MerkleLedger, sha256_text, stable_json
from .types import EmbeddingRecord, SearchResult
from .vector_math import (
    coerce_float32_vector,
    cosine_similarity_memoryview,
    l2_norm,
    sha256_vector,
    vector_to_le_bytes,
)


class MMapVectorStore:
    """Append-only float32 vector store with a latest-record active view.

    Vector bytes stay append-only in ``vectors.f32``. The JSONL index is an event
    log: normal record lines activate or replace an id, while ``{"op":"delete"}``
    lines tombstone older ids. Search only sees the active view. This preserves
    forensic history without returning duplicates after repeated ingests.
    """

    MANIFEST_VERSION = "SMQL_ADAPTER_COLLECTION_V1"

    def __init__(self, root: str | Path, collection: str = "default", dimension: int | None = None) -> None:
        if not collection or "/" in collection or "\\" in collection:
            raise ValueError("collection must be a simple non-empty name")
        self.root = Path(root)
        self.collection = collection
        self.path = self.root / collection
        self.path.mkdir(parents=True, exist_ok=True)
        self.vectors_path = self.path / "vectors.f32"
        self.index_path = self.path / "index.jsonl"
        self.manifest_path = self.path / "manifest.json"
        self.ledger = MerkleLedger(self.path / "ledger.jsonl")
        self.records: list[EmbeddingRecord] = []
        self._dimension = dimension
        self._load_or_init()

    @property
    def dimension(self) -> int | None:
        return self._dimension

    @property
    def count(self) -> int:
        return len(self.records)

    def _load_or_init(self) -> None:
        if self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            self._dimension = int(manifest["dimension"])
        elif self._dimension is not None:
            self._write_manifest()
        if self.index_path.exists():
            with self.index_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    op = str(raw.get("op", "record"))
                    if op in {"delete", "tombstone"}:
                        self._drop_active_id(str(raw.get("id", "")))
                        continue
                    record = EmbeddingRecord(
                        id=str(raw["id"]),
                        collection=str(raw.get("collection", self.collection)),
                        offset=int(raw["offset"]),
                        dimension=int(raw["dimension"]),
                        norm=float(raw["norm"]),
                        vector_sha256=str(raw["vector_sha256"]),
                        payload_sha256=str(raw["payload_sha256"]),
                        created_at=float(raw["created_at"]),
                        pheromone=float(raw.get("pheromone", 1.0)),
                        metadata=dict(raw.get("metadata", {})),
                        text_preview=str(raw.get("text_preview", "")),
                        text=raw.get("text"),
                        mycelia_signature=str(raw.get("mycelia_signature", "")),
                    )
                    # Backward-compatible recovery for v0.1/v0.1.1 vaults: if
                    # an id appears multiple times without a tombstone, the last
                    # record becomes active and older rows remain historical bytes.
                    self._drop_active_id(record.id)
                    self.records.append(record)

    def _drop_active_id(self, id_: str) -> bool:
        if not id_:
            return False
        before = len(self.records)
        self.records = [record for record in self.records if record.id != id_]
        return len(self.records) != before

    def _write_manifest(self) -> None:
        if self._dimension is None:
            return
        self.manifest_path.write_text(
            json.dumps(
                {
                    "version": self.MANIFEST_VERSION,
                    "collection": self.collection,
                    "dimension": self._dimension,
                    "float_format": "float32-le",
                    "records": len(self.records),
                    "active_records": len(self.records),
                    "updated_at": time.time(),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _append_tombstone(self, id_: str, *, reason: str = "replace") -> None:
        event = {
            "op": "delete",
            "id": id_,
            "collection": self.collection,
            "reason": reason,
            "created_at": time.time(),
        }
        with self.index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        self.ledger.append("tombstone", event)

    def append(
        self,
        *,
        id: str,
        vector: Sequence[float],
        metadata: Mapping[str, Any] | None = None,
        text: str | None = None,
        store_text: bool = False,
        pheromone: float = 1.0,
        mycelia_signature: str = "",
        replace_existing: bool = True,
    ) -> EmbeddingRecord:
        vec = coerce_float32_vector(vector)
        if self._dimension is None:
            self._dimension = len(vec)
            self._write_manifest()
        if len(vec) != self._dimension:
            raise ValueError(
                "dimension mismatch: "
                f"collection '{self.collection}' was created with {self._dimension} dimensions, "
                f"but the current embedding provider returned {len(vec)} dimensions. "
                "Use a new collection name, reset the collection, or keep using the same "
                "embedding model/dimension for both ingest and query."
            )

        if replace_existing and self._drop_active_id(id):
            self._append_tombstone(id, reason="replace")

        raw = vector_to_le_bytes(vec)
        self.vectors_path.parent.mkdir(parents=True, exist_ok=True)
        offset = self.vectors_path.stat().st_size if self.vectors_path.exists() else 0
        with self.vectors_path.open("ab") as f:
            f.write(raw)

        meta = dict(metadata or {})
        payload = {
            "id": id,
            "metadata": meta,
            "text_sha256": sha256_text(text or ""),
            "text_length": len(text or ""),
        }
        text_preview = (text or "")[:240].replace("\n", " ")
        record = EmbeddingRecord(
            id=id,
            collection=self.collection,
            offset=offset,
            dimension=len(vec),
            norm=l2_norm(vec),
            vector_sha256=sha256_vector(vec),
            payload_sha256=sha256_text(stable_json(payload)),
            created_at=time.time(),
            pheromone=max(0.0, min(1.0, float(pheromone))),
            metadata=meta,
            text_preview=text_preview,
            text=text if store_text else None,
            mycelia_signature=mycelia_signature,
        )
        with self.index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")
        self.records.append(record)
        self._write_manifest()
        self.ledger.append(
            "ingest",
            {
                "id": record.id,
                "collection": record.collection,
                "offset": record.offset,
                "dimension": record.dimension,
                "vector_sha256": record.vector_sha256,
                "payload_sha256": record.payload_sha256,
                "replace_existing": replace_existing,
            },
        )
        return record

    def append_batch(
        self,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
        *,
        metadata: Sequence[Mapping[str, Any]] | None = None,
        texts: Sequence[str] | None = None,
        store_text: bool = False,
        replace_existing: bool = True,
    ) -> list[EmbeddingRecord]:
        if len(ids) != len(vectors):
            raise ValueError("ids and vectors length mismatch")
        if metadata is not None and len(metadata) != len(ids):
            raise ValueError("metadata length mismatch")
        if texts is not None and len(texts) != len(ids):
            raise ValueError("texts length mismatch")
        records: list[EmbeddingRecord] = []
        for i, (id_, vector) in enumerate(zip(ids, vectors, strict=True)):
            records.append(
                self.append(
                    id=id_,
                    vector=vector,
                    metadata=metadata[i] if metadata is not None else {},
                    text=texts[i] if texts is not None else None,
                    store_text=store_text,
                    replace_existing=replace_existing,
                )
            )
        return records


    def get_record(self, id_: str) -> EmbeddingRecord | None:
        """Return the active record for an id, if present."""
        for record in self.records:
            if record.id == id_:
                return record
        return None

    def append_search_event(self, *, query_sha256: str, limit: int, result_ids: Sequence[str], backend: str) -> None:
        """Append a search event when retrieval was delegated outside this store."""
        self.ledger.append(
            "search",
            {
                "collection": self.collection,
                "limit": int(limit),
                "query_sha256": query_sha256,
                "result_ids": list(result_ids),
                "backend": backend,
            },
        )

    def search(self, query: Sequence[float], *, limit: int = 10, min_score: float = -1.0) -> list[SearchResult]:
        if self._dimension is None or not self.records:
            return []
        if len(query) != self._dimension:
            raise ValueError(
                "dimension mismatch: "
                f"collection '{self.collection}' contains {self._dimension}-dimension vectors, "
                f"but the query embedding has {len(query)} dimensions. "
                "Query with the same embedding model used for ingest, use a matching collection, "
                "or reset/rebuild the collection."
            )
        limit = max(1, min(1000, int(limit)))
        query_vec = coerce_float32_vector(query)
        query_norm = l2_norm(query_vec)
        if not self.vectors_path.exists():
            return []
        row_bytes = self._dimension * 4
        heap: list[tuple[float, int, SearchResult]] = []
        with self.vectors_path.open("rb") as f:
            if f.seek(0, os.SEEK_END) == 0:
                return []
            size = f.tell()
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                for idx, record in enumerate(self.records):
                    if record.offset + row_bytes > size:
                        continue
                    row = memoryview(mm)[record.offset : record.offset + row_bytes].cast("f")
                    try:
                        cosine = cosine_similarity_memoryview(query_vec, query_norm, row, record.norm)
                    finally:
                        row.release()
                    score = cosine * record.pheromone
                    if score < min_score:
                        continue
                    result = SearchResult(
                        id=record.id,
                        score=score,
                        cosine=cosine,
                        pheromone=record.pheromone,
                        record=record,
                    )
                    if len(heap) < limit:
                        heapq.heappush(heap, (score, idx, result))
                    elif score > heap[0][0]:
                        heapq.heapreplace(heap, (score, idx, result))
        results = [item[2] for item in sorted(heap, key=lambda x: x[0], reverse=True)]
        self.ledger.append(
            "search",
            {
                "collection": self.collection,
                "limit": limit,
                "query_sha256": sha256_vector(query_vec),
                "result_ids": [r.id for r in results],
            },
        )
        return results

    def reset(self) -> None:
        for p in (self.vectors_path, self.index_path, self.manifest_path, self.path / "ledger.jsonl"):
            if p.exists():
                p.unlink()
        self.records.clear()
        self._dimension = None
        self.ledger = MerkleLedger(self.path / "ledger.jsonl")
        self.ledger.append("reset", {"collection": self.collection})
