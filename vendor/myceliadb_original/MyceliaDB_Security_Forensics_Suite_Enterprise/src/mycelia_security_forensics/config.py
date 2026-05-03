from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
import tomllib


@dataclass(slots=True)
class SuiteConfig:
    adapter_url: str = "http://127.0.0.1:8765"
    mycelia_url: str = "http://127.0.0.1:9999"
    lmstudio_url: str = "http://127.0.0.1:1234"
    web_chat_api: str = "auto"
    web_chat_api_required: bool = False
    token_file: str = r"C:\MyceliaDB\html\keys\local_transport.token"
    adapter_root: str = r"C:\MyceliaDB\SMQL-Embedding-Adapter"
    mycelia_root: str = r"C:\MyceliaDB\html"
    collection: str = "demo"
    chat_model: str = "google_gemma-4-e4b-it"
    embedding_model: str = "text-embedding-nomic-embed-text-v2-moe"
    reports_dir: str = "reports"
    require_mycelia_backend: bool = True
    require_opencl_vram: bool = True
    min_sources: int = 1
    rag_limit: int = 5
    http_timeout_seconds: float = 8.0
    scan_max_file_mb: int = 5
    secret_scan_ignore_dirs: list[str] = field(default_factory=lambda: [
        ".venv", "venv", "env", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
        "site-packages", "node_modules", "build", "dist", "reports", "snapshots", "state",
        ".smql_adapter"
    ])
    secret_scan_extensions: list[str] = field(default_factory=lambda: [
        ".py", ".php", ".js", ".json", ".jsonl", ".toml", ".ini", ".md", ".txt", ".ps1", ".html", ".css"
    ])
    redteam_corpus: str = "configs/rag_redteam_corpus.de.jsonl"
    mycelia_pid: int | None = None
    run_live_ram_probe: bool = False
    ram_probe_seconds: int = 8
    strict_exit_code: bool = False

    def to_public_dict(self) -> dict:
        d = asdict(self)
        # Do not include token value. token_file path is okay for local operator reports.
        return d


def load_config(path: str | None) -> SuiteConfig:
    cfg = SuiteConfig()
    if not path:
        return cfg
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with p.open("rb") as f:
        data = tomllib.load(f)
    flat = {}
    for section in data.values():
        if isinstance(section, dict):
            flat.update(section)
    for k, v in flat.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg


def overlay_cli(cfg: SuiteConfig, args) -> SuiteConfig:
    for key in [
        "adapter_url", "mycelia_url", "lmstudio_url", "web_chat_api", "token_file",
        "adapter_root", "mycelia_root", "collection", "chat_model", "embedding_model",
        "reports_dir", "redteam_corpus", "mycelia_pid"
    ]:
        val = getattr(args, key, None)
        if val not in (None, ""):
            setattr(cfg, key, val)
    for key in ["run_live_ram_probe", "strict_exit_code"]:
        if getattr(args, key, False):
            setattr(cfg, key, True)
    return cfg
