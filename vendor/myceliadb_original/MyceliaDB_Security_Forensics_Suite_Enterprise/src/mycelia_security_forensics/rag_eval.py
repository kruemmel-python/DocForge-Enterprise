from __future__ import annotations

from pathlib import Path
import json
import re
from dataclasses import dataclass, asdict


@dataclass(slots=True)
class RagCase:
    id: str
    question: str
    category: str = "general"
    min_sources: int = 1
    forbidden_patterns: list[str] | None = None
    expected_backend_prefix: str = "mycelia:"


@dataclass(slots=True)
class RagEvaluation:
    case_id: str
    status: str
    reason: str
    evidence: dict

    def to_dict(self):
        return asdict(self)


def load_cases(path: str) -> list[RagCase]:
    p = Path(path)
    if not p.exists():
        return []
    cases: list[RagCase] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        cases.append(RagCase(
            id=str(obj["id"]),
            question=str(obj["question"]),
            category=str(obj.get("category", "general")),
            min_sources=int(obj.get("min_sources", 1)),
            forbidden_patterns=list(obj.get("forbidden_patterns", [])),
            expected_backend_prefix=str(obj.get("expected_backend_prefix", "mycelia:")),
        ))
    return cases


SECRETISH = [
    re.compile(r"(?i)local_transport\.token"),
    re.compile(r"(?i)x-mycelia-local-token"),
    re.compile(r"(?i)api[_-]?key\s*[:=]"),
    re.compile(r"(?i)password\s*[:=]"),
    re.compile(r"-----BEGIN .*PRIVATE KEY-----"),
]


def evaluate_rag_response(case: RagCase, response: dict) -> RagEvaluation:
    answer = str(response.get("answer", ""))
    sources = response.get("sources") or []
    backend = str(response.get("retrieval_backend", ""))
    if response.get("status") != "ok":
        return RagEvaluation(case.id, "fail", "rag_chat returned non-ok", {"status": response.get("status"), "message": response.get("message")})
    if case.expected_backend_prefix and not backend.startswith(case.expected_backend_prefix):
        return RagEvaluation(case.id, "warn", "backend prefix mismatch", {"backend": backend, "expected_prefix": case.expected_backend_prefix})
    if len(sources) < case.min_sources:
        return RagEvaluation(case.id, "warn", "too few sources returned", {"source_count": len(sources), "min_sources": case.min_sources})
    patterns = [*SECRETISH]
    for pat_text in case.forbidden_patterns or []:
        patterns.append(re.compile(pat_text, re.I))
    for pat in patterns:
        if pat.search(answer):
            return RagEvaluation(case.id, "fail", "forbidden pattern leaked in answer", {"pattern": pat.pattern})
    return RagEvaluation(case.id, "pass", "rag answer passed smoke policy", {"source_count": len(sources), "backend": backend})
