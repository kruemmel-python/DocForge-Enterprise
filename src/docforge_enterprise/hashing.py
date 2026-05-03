from __future__ import annotations
import hashlib, json
from typing import Any
def sha256_text(text: str) -> str: return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
def sha256_bytes(data: bytes) -> str: return hashlib.sha256(data).hexdigest()
def stable_json(data: Any) -> str: return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def stable_id(*parts: object) -> str: return sha256_text("\n".join(map(str, parts)))[:32]
