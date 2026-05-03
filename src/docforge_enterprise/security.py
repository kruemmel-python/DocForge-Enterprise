from __future__ import annotations
import re
from pathlib import Path
from dataclasses import dataclass
from .config import SecuritySettings
VENDOR={".git",".venv","venv","node_modules","__pycache__","dist","build",".pytest_cache"}
BLOCKED={".pem",".key",".token",".db",".sqlite",".sqlite3",".pyc",".exe",".dll",".so",".zip",".png",".jpg",".pdf"}
SECRET_PATTERNS=[re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-+/=]{16,}")]
@dataclass(frozen=True, slots=True)
class FileDecision: allowed:bool; reason:str=""
def is_probably_binary(data:bytes)->bool: return b"\x00" in data[:4096]
def should_skip_path(path:Path, settings:SecuritySettings)->FileDecision:
    lower={p.lower() for p in path.parts}
    if settings.block_vendor_dirs and lower.intersection(VENDOR): return FileDecision(True,"vendor/cache")
    if path.suffix.lower() in BLOCKED: return FileDecision(True,"blocked suffix")
    if settings.block_secret_files and re.search(r"(?i)(secret|password|credential|token|\.env)", path.name): return FileDecision(True,"secret-like name")
    return FileDecision(False,"")
def redact_secrets(text:str)->tuple[str,int]:
    c=0
    for pat in SECRET_PATTERNS:
        text,n=pat.subn("[REDACTED_SECRET]",text); c+=n
    return text,c
