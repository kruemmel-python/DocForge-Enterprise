from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import SecuritySettings


VENDOR_DIRS = {
    ".git", ".idea", ".vscode", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", ".venv", "venv", "env", "node_modules",
    "dist", "build", "target", ".gradle", ".next", ".cache", "coverage",
}

SECRET_DIRS = {"keys", "secrets", "certs", "private", ".ssh"}

BLOCKED_SUFFIXES = {
    ".pyc", ".pyo", ".pyd", ".dll", ".so", ".dylib", ".exe", ".obj", ".o",
    ".class", ".jar", ".war", ".ear", ".db", ".sqlite", ".sqlite3", ".pem",
    ".key", ".p12", ".pfx", ".crt", ".der", ".token", ".keystore", ".jks",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".7z", ".rar", ".tar", ".mp4", ".mp3", ".wav",
}

SECRET_NAME_PATTERNS = [
    re.compile(r"(^|[._-])(secret|password|passwd|private|credential|token|apikey|api_key)([._-]|$)", re.I),
    re.compile(r"\.env($|\.)", re.I),
]

SECRET_VALUE_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-+/=]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-._~+/]+=*"),
]


@dataclass(frozen=True, slots=True)
class FileDecision:
    allowed: bool
    reason: str = ""


def is_probably_binary(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return True
    if not data:
        return False
    sample = data[:4096]
    non_text = sum(byte < 9 or (13 < byte < 32) for byte in sample)
    return non_text / max(len(sample), 1) > 0.30


def should_skip_path(path: Path, settings: SecuritySettings) -> FileDecision:
    parts = set(path.parts)
    lower_parts = {part.lower() for part in path.parts}
    name = path.name.lower()

    if settings.block_vendor_dirs and lower_parts.intersection(VENDOR_DIRS):
        return FileDecision(True, "vendor/cache/build directory")

    if settings.block_secret_files and lower_parts.intersection(SECRET_DIRS):
        return FileDecision(True, "secret directory")

    if path.suffix.lower() in BLOCKED_SUFFIXES:
        return FileDecision(True, f"blocked suffix {path.suffix.lower()}")

    if settings.block_secret_files:
        for pattern in SECRET_NAME_PATTERNS:
            if pattern.search(name):
                return FileDecision(True, "secret-like file name")

    return FileDecision(False, "")


def redact_secrets(text: str) -> tuple[str, int]:
    count = 0
    redacted = text
    for pattern in SECRET_VALUE_PATTERNS:
        redacted, n = pattern.subn("[REDACTED_SECRET]", redacted)
        count += n
    return redacted, count
