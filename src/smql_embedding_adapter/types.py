"""Shared data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


Vector = list[float]


@dataclass(slots=True, frozen=True)
class EmbeddingInput:
    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class EmbeddingRecord:
    id: str
    collection: str
    offset: int
    dimension: int
    norm: float
    vector_sha256: str
    payload_sha256: str
    created_at: float
    pheromone: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    text_preview: str = ""
    text: str | None = None
    mycelia_signature: str = ""


@dataclass(slots=True, frozen=True)
class SearchResult:
    id: str
    score: float
    cosine: float
    pheromone: float
    record: EmbeddingRecord


@dataclass(slots=True, frozen=True)
class IngestResult:
    collection: str
    count: int
    ids: list[str]
    merkle_head: str
    mycelia_status: str = "not-configured"


@dataclass(slots=True, frozen=True)
class QueryResult:
    collection: str
    count: int
    results: list[SearchResult]
    merkle_head: str
    mycelia_status: str = "not-configured"
    retrieval_backend: str = "sidecar"
    mycelia_native: dict[str, Any] = field(default_factory=dict)
    sealed_attestation: dict[str, Any] = field(default_factory=dict)
