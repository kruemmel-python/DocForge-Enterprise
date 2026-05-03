from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any
import time


class Status(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"
    SKIP = "skip"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


def _safe_json_value(value: Any) -> Any:
    """Convert evidence into JSON primitives without using obj.__dict__.

    This fixes slots=True dataclasses such as SecretHit and makes reports
    resilient against Path, Enum, set, tuple and unknown objects.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {f.name: _safe_json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(_safe_json_value(k)): _safe_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(slots=True)
class Finding:
    check_id: str
    title: str
    status: Status
    severity: Severity = Severity.INFO
    summary: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    recommendation: str = ""
    category: str = "general"
    started_at: float = field(default_factory=time.time)
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "title": self.title,
            "status": self.status.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "evidence": _safe_json_value(self.evidence),
            "recommendation": self.recommendation,
            "category": self.category,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class SuiteReport:
    suite: str
    version: str
    target: dict[str, Any]
    started_at: str
    duration_ms: float
    findings: list[Finding]
    summary: dict[str, Any]
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "version": self.version,
            "target": _safe_json_value(self.target),
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "summary": _safe_json_value(self.summary),
            "artifacts": _safe_json_value(self.artifacts),
            "findings": [f.to_dict() for f in self.findings],
        }
