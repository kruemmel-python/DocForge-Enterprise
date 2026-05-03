from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
ArtifactKind = Literal["code","config","documentation","test","security","data","unknown"]
@dataclass(frozen=True, slots=True)
class ProjectFile:
    path: Path; relative_path: str; language: str; kind: ArtifactKind; content: str; sha256: str; size_bytes: int
@dataclass(frozen=True, slots=True)
class CodeShard:
    id: str; file_path: str; language: str; kind: ArtifactKind; content: str; char_start: int; char_end: int; sha256: str; ordinal: int; symbols: tuple[str,...]=()
@dataclass(slots=True)
class AnalysisRecord:
    id: str; stage: str; source_id: str; payload: dict[str,Any]; status: str="ok"; error: str=""
@dataclass(slots=True)
class RetrievedContext:
    id: str; score: float; file_path: str; text: str; metadata: dict[str,Any]=field(default_factory=dict)
@dataclass(slots=True)
class PipelineStats:
    files_seen:int=0; files_indexed:int=0; files_skipped:int=0; shards_created:int=0; shards_analyzed:int=0
    retrieval_events:int=0; llm_failures:int=0; json_repairs:int=0; timeouts:int=0; adaptive_shard_retries:int=0
    checkpoint_writes:int=0; embedding_failures:int=0; estimated_llm_chat_calls:int=0; estimated_embedding_calls:int=0
    actual_llm_chat_calls:int=0; actual_embedding_calls:int=0
    claims_total:int=0; claims_supported:int=0; claims_unsupported:int=0; evidence_coverage_percent:float=0.0
