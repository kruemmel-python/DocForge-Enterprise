from __future__ import annotations

from pathlib import Path
import re
import hashlib
import math
from dataclasses import dataclass


@dataclass(slots=True)
class SecretHit:
    path: str
    line: int
    pattern: str
    preview: str


DEFAULT_IGNORE_DIRS: set[str] = {
    ".venv",
    "venv",
    "env",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "site-packages",
    "node_modules",
    "build",
    "dist",
    "reports",
    "snapshots",
    "state",
    ".smql_adapter",
}

DIRECT_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("jwt-token", re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("private-key-marker", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
]

ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|api_key|apikey|token)\b\s*[:=]\s*(?P<value>['\"]?[^'\"\s,#;]+['\"]?)"
)

_CODE_VALUE_PREFIXES = (
    "args.",
    "cfg.",
    "settings.",
    "self.",
    "os.",
    "path(",
    "pathlib.",
    "none",
    "true",
    "false",
    "null",
    "token",
    "password",
    "username",
    "getattr(",
    "str(",
    "bytes(",
    "open(",
    "request",
    "headers",
    "literal_secret",
    "<redacted>",
)


def _safe_preview(line: str) -> str:
    line = line.strip()
    if len(line) > 180:
        line = line[:180] + "…"
    # Redact common values after separators, keeping the assignment shape.
    return re.sub(r"([:=]\s*['\"]?)[^'\"\s]{6,}", r"\1<redacted>", line)


def _is_ignored_path(path: Path, ignore_dirs: set[str]) -> bool:
    parts = {p.lower() for p in path.parts}
    return bool(parts.intersection({d.lower() for d in ignore_dirs}))


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {ch: s.count(ch) for ch in set(s)}
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_like_literal_secret_value(raw: str) -> bool:
    value = raw.strip().strip("'\"")
    if len(value) < 16:
        return False
    low = value.lower()

    # Common source-code expressions and placeholders are not secrets.
    if any(low.startswith(prefix) for prefix in _CODE_VALUE_PREFIXES):
        return False
    if low in {"your-token-here", "change-me", "example-token", "example-secret", "dummy-token"}:
        return False
    if value.startswith(("$", "%", "{", "[", "(")):
        return False

    # Source expressions are not disk-resident secret literals.  The scanner is
    # intentionally conservative here: a call such as
    # ``token = _read_token_file(candidate)`` is a retrieval path, not leaked
    # material.  Without this guard, code that correctly loads tokens from the
    # approved token file is reported as a high-severity leak.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\.]*\([^\n]*\)", value):
        return False
    if re.search(r"[()\[\]{}]", value):
        return False

    if "." in value and not re.fullmatch(r"[A-Za-z0-9_\-./+=]{20,}", value):
        # Usually a variable/expression such as args.mycelia_token.
        return False

    classes = [
        bool(re.search(r"[a-z]", value)),
        bool(re.search(r"[A-Z]", value)),
        bool(re.search(r"\d", value)),
        bool(re.search(r"[_\-+/=]", value)),
    ]
    class_count = sum(classes)
    if class_count >= 2 and _entropy(value) >= 2.8:
        return True
    # Long hex/base64-like values are suspicious even with fewer character classes.
    if re.fullmatch(r"[A-Fa-f0-9]{32,}", value):
        return True
    if re.fullmatch(r"[A-Za-z0-9+/=_-]{32,}", value) and _entropy(value) >= 3.2:
        return True
    return False


def _iter_files(root: Path, ignore_dirs: set[str]):
    if root.is_file():
        if not _is_ignored_path(root, ignore_dirs):
            yield root
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if _is_ignored_path(p, ignore_dirs):
            continue
        yield p


def scan_paths(
    roots: list[str],
    extensions: list[str],
    max_file_mb: int = 5,
    literal_secret: str | None = None,
    ignore_dirs: list[str] | set[str] | None = None,
) -> list[SecretHit]:
    hits: list[SecretHit] = []
    literal_hash = hashlib.sha256(literal_secret.encode("utf-8")).hexdigest() if literal_secret else None
    extset = set(e.lower() for e in extensions)
    ignored = set(DEFAULT_IGNORE_DIRS)
    if ignore_dirs:
        ignored.update(str(d) for d in ignore_dirs)

    for r in roots:
        root = Path(r)
        if not root.exists():
            continue
        for p in _iter_files(root, ignored):
            if p.suffix.lower() not in extset:
                continue
            try:
                if p.stat().st_size > max_file_mb * 1024 * 1024:
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            for lineno, line in enumerate(text.splitlines(), 1):
                if literal_secret and literal_secret and literal_secret in line:
                    hits.append(SecretHit(str(p), lineno, f"literal-local-token-sha256:{literal_hash[:16]}", _safe_preview(line)))
                    continue

                for name, pat in DIRECT_SECRET_PATTERNS:
                    if pat.search(line):
                        hits.append(SecretHit(str(p), lineno, name, _safe_preview(line)))

                m = ASSIGNMENT_PATTERN.search(line)
                if m and _looks_like_literal_secret_value(m.group("value")):
                    hits.append(SecretHit(str(p), lineno, "secret-assignment-literal", _safe_preview(line)))

    return hits
