"""Append-only Merkle ledger for adapter provenance."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


ZERO_HASH = "0" * 64


def stable_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True, frozen=True)
class LedgerEvent:
    index: int
    ts: float
    op: str
    payload_hash: str
    previous_hash: str
    event_hash: str
    payload: dict[str, Any]


class MerkleLedger:
    """JSONL ledger where each event hash commits to the previous event."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._head = ZERO_HASH
        self._count = 0
        if self.path.exists():
            self._load_head()

    @property
    def head(self) -> str:
        return self._head

    @property
    def count(self) -> int:
        return self._count

    def _load_head(self) -> None:
        last: dict[str, Any] | None = None
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)
                    self._count += 1
        if last:
            self._head = str(last.get("event_hash", ZERO_HASH))

    def append(self, op: str, payload: Mapping[str, Any]) -> LedgerEvent:
        event_payload = dict(payload)
        payload_hash = sha256_text(stable_json(event_payload))
        base = {
            "index": self._count,
            "ts": time.time(),
            "op": op,
            "payload_hash": payload_hash,
            "previous_hash": self._head,
            "payload": event_payload,
        }
        event_hash = sha256_text(stable_json(base))
        event = LedgerEvent(
            index=self._count,
            ts=float(base["ts"]),
            op=op,
            payload_hash=payload_hash,
            previous_hash=self._head,
            event_hash=event_hash,
            payload=event_payload,
        )
        line = stable_json(
            {
                "index": event.index,
                "ts": event.ts,
                "op": event.op,
                "payload_hash": event.payload_hash,
                "previous_hash": event.previous_hash,
                "event_hash": event.event_hash,
                "payload": event.payload,
            }
        )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._head = event_hash
        self._count += 1
        return event

    def verify(self) -> tuple[bool, str]:
        previous = ZERO_HASH
        count = 0
        if not self.path.exists():
            return True, ZERO_HASH
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                raw = json.loads(line)
                expected_payload = sha256_text(stable_json(raw["payload"]))
                if expected_payload != raw["payload_hash"]:
                    return False, f"payload hash mismatch at {count}"
                base = {
                    "index": raw["index"],
                    "ts": raw["ts"],
                    "op": raw["op"],
                    "payload_hash": raw["payload_hash"],
                    "previous_hash": raw["previous_hash"],
                    "payload": raw["payload"],
                }
                expected_event = sha256_text(stable_json(base))
                if raw["previous_hash"] != previous:
                    return False, f"previous hash mismatch at {count}"
                if raw["event_hash"] != expected_event:
                    return False, f"event hash mismatch at {count}"
                previous = raw["event_hash"]
                count += 1
        return True, previous
