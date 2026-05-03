from __future__ import annotations

from pathlib import Path
import json
from dataclasses import dataclass, asdict
from typing import Any


@dataclass(slots=True)
class RehydrationAudit:
    exists: bool
    path: str
    bytes: int = 0
    events_total: int = 0
    events_failed: int = 0
    latest_counts: dict[str, int] | None = None
    sample_event_keys: list[str] | None = None

    def to_dict(self):
        return asdict(self)


def audit_jsonl(path: str) -> RehydrationAudit:
    p = Path(path)
    if not p.exists():
        return RehydrationAudit(False, str(p))
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    failed = 0
    keys: set[str] = set()
    total = 0
    try:
        size = p.stat().st_size
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    obj = json.loads(line)
                    keys.update(obj.keys())
                    collection = str(obj.get("collection") or obj.get("namespace") or obj.get("col") or "unknown")
                    vid = str(obj.get("id") or obj.get("vector_id") or obj.get("key") or f"event-{total}")
                    deleted = bool(obj.get("deleted") or obj.get("tombstone"))
                    if deleted:
                        latest.pop((collection, vid), None)
                    else:
                        latest[(collection, vid)] = obj
                except Exception:
                    failed += 1
        counts: dict[str, int] = {}
        for collection, _vid in latest.keys():
            counts[collection] = counts.get(collection, 0) + 1
        return RehydrationAudit(True, str(p), size, total, failed, counts, sorted(keys)[:40])
    except Exception:
        return RehydrationAudit(True, str(p), 0, total, failed + 1, {}, [])
