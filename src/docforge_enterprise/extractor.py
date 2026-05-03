from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Iterable

from .config import Settings
from .hashing import sha256_text
from .models import ArtifactKind, ProjectFile
from .security import is_probably_binary, redact_secrets, should_skip_path


TEXT_EXTENSIONS = {
    ".py": ("python", "code"),
    ".js": ("javascript", "code"),
    ".ts": ("typescript", "code"),
    ".tsx": ("typescript-react", "code"),
    ".jsx": ("javascript-react", "code"),
    ".java": ("java", "code"),
    ".cs": ("csharp", "code"),
    ".go": ("go", "code"),
    ".rs": ("rust", "code"),
    ".php": ("php", "code"),
    ".rb": ("ruby", "code"),
    ".sql": ("sql", "data"),
    ".yaml": ("yaml", "config"),
    ".yml": ("yaml", "config"),
    ".json": ("json", "config"),
    ".toml": ("toml", "config"),
    ".ini": ("ini", "config"),
    ".xml": ("xml", "config"),
    ".html": ("html", "code"),
    ".css": ("css", "code"),
    ".md": ("markdown", "documentation"),
    ".rst": ("rst", "documentation"),
    ".txt": ("text", "documentation"),
    ".sh": ("shell", "code"),
    ".ps1": ("powershell", "code"),
    ".bat": ("batch", "code"),
    ".cmd": ("batch", "code"),
    ".dockerfile": ("dockerfile", "config"),
}


def _safe_extract_zip(zip_path: Path, target: Path, *, fail_on_zip_slip: bool = True) -> None:
    target_resolved = target.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            out_path = target / info.filename
            try:
                resolved = out_path.resolve()
            except FileNotFoundError:
                resolved = out_path.parent.resolve() / out_path.name
            if not str(resolved).startswith(str(target_resolved)):
                if fail_on_zip_slip:
                    raise ValueError(f"Unsafe ZIP path rejected: {info.filename}")
                continue
            zf.extract(info, target)


def prepare_input(input_path: Path, workspace: Path, settings: Settings) -> Path:
    extracted = workspace / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)

    if input_path.suffix.lower() == ".zip":
        _safe_extract_zip(
            input_path,
            extracted,
            fail_on_zip_slip=settings.security.fail_on_zip_slip,
        )
        return extracted

    if input_path.suffix.lower() == ".md":
        target = extracted / input_path.name
        target.write_bytes(input_path.read_bytes())
        return extracted

    if input_path.is_dir():
        return input_path

    raise ValueError("Input must be a .zip file, .md code dump, or directory.")


def detect_language_and_kind(path: Path) -> tuple[str, ArtifactKind]:
    if path.name.lower() == "dockerfile":
        return "dockerfile", "config"
    language, kind = TEXT_EXTENSIONS.get(path.suffix.lower(), ("unknown", "unknown"))
    return language, kind  # type: ignore[return-value]


def iter_project_files(root: Path, settings: Settings) -> Iterable[ProjectFile]:
    security = settings.security
    for path in root.rglob("*"):
        if not path.is_file():
            continue

        relative = str(path.relative_to(root))
        decision = should_skip_path(Path(relative), security)
        if decision.allowed:
            continue

        try:
            size = path.stat().st_size
        except OSError:
            continue

        if size > security.max_file_bytes:
            continue

        raw = path.read_bytes()
        if not security.allow_binary and is_probably_binary(raw):
            continue

        language, kind = detect_language_and_kind(path)
        if language == "unknown":
            continue

        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = raw.decode("latin-1", errors="replace")

        if security.redact_secrets:
            content, _ = redact_secrets(content)

        yield ProjectFile(
            path=path,
            relative_path=relative.replace("\\", "/"),
            language=language,
            kind=kind,
            content=content,
            sha256=sha256_text(content),
            size_bytes=size,
        )
