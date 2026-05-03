"Configuration loading for the SMQL Embedding Adapter."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(slots=True)
class AdapterConfig:
    vault_path: Path = Path(".smql_adapter")
    default_collection: str = "default"
    default_dimension: int = 384
    strict_vram_required: bool = False
    store_text_default: bool = False
    search_backend: str = "auto"  # auto | mycelia | sidecar
    sealed_mode: str = "auto"  # off | auto | required
    strict_no_cpu_ram_required: bool = False
    sealed_abi_path: str = ""


@dataclass(slots=True)
class LMStudioConfig:
    base_url: str = "http://127.0.0.1:1234/v1"
    embedding_model: str = "text-embedding-nomic-embed-text-v1.5"
    chat_model: str = "local-model"
    timeout_seconds: float = 120.0
    enabled: bool = False


@dataclass(slots=True)
class MyceliaConfig:
    base_url: str = "http://127.0.0.1:9999"
    token: str = ""
    token_env: str = "MYCELIA_LOCAL_TOKEN"
    token_file: Path | None = None
    timeout_seconds: float = 30.0
    enabled: bool = False
    smql_table: str = "mycelia_embeddings"


def _read_token_file(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return ""


def _candidate_token_paths() -> list[Path]:
    """Common MyceliaDB local transport token locations.

    MyceliaDB's platform service creates ``html/keys/local_transport.token`` by
    default.  The adapter is often launched from a sibling checkout such as
    ``C:/web_sicherheit/SMQL-Embedding-Adapter`` while MyceliaDB runs from
    ``C:/web_sicherheit/html``.  This auto-discovery is intentionally limited to
    local filesystem paths and never contacts the network.
    """
    cwd = Path.cwd()
    candidates = [
        Path(os.environ.get("MYCELIA_LOCAL_TRANSPORT_TOKEN_PATH", "")),
        cwd / "keys" / "local_transport.token",
        cwd / "html" / "keys" / "local_transport.token",
        cwd.parent / "html" / "keys" / "local_transport.token",
        cwd.parent / "keys" / "local_transport.token",
    ]
    # Drop empty environment path while preserving order.
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not str(candidate):
            continue
        resolved = candidate.expanduser()
        key = str(resolved)
        if key not in seen:
            result.append(resolved)
            seen.add(key)
    return result


def _discover_token_from_file(explicit: Path | None = None) -> tuple[str, Path | None]:
    if explicit is not None:
        token = _read_token_file(explicit)
        return token, explicit if token else None
    for candidate in _candidate_token_paths():
        token = _read_token_file(candidate)
        if token:
            return token, candidate
    return "", None


@dataclass(slots=True)
class Settings:
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    lmstudio: LMStudioConfig = field(default_factory=LMStudioConfig)
    mycelia: MyceliaConfig = field(default_factory=MyceliaConfig)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Settings":
        adapter_raw = data.get("adapter", {})
        lm_raw = data.get("lmstudio", {})
        my_raw = data.get("mycelia", {})

        adapter = AdapterConfig(
            vault_path=Path(str(adapter_raw.get("vault_path", ".smql_adapter"))),
            default_collection=str(adapter_raw.get("default_collection", "default")),
            default_dimension=int(adapter_raw.get("default_dimension", 384)),
            strict_vram_required=bool(adapter_raw.get("strict_vram_required", False)),
            store_text_default=bool(adapter_raw.get("store_text_default", False)),
            search_backend=str(adapter_raw.get("search_backend", "auto")).lower(),
            sealed_mode=str(adapter_raw.get("sealed_mode", "auto")).lower(),
            strict_no_cpu_ram_required=bool(adapter_raw.get("strict_no_cpu_ram_required", False)),
            sealed_abi_path=str(adapter_raw.get("sealed_abi_path", "")),
        )
        lmstudio = LMStudioConfig(
            base_url=str(lm_raw.get("base_url", "http://127.0.0.1:1234/v1")),
            embedding_model=str(
                lm_raw.get("embedding_model", "text-embedding-nomic-embed-text-v1.5")
            ),
            chat_model=str(lm_raw.get("chat_model", "local-model")),
            timeout_seconds=float(lm_raw.get("timeout_seconds", 120.0)),
            enabled=bool(lm_raw.get("enabled", False)),
        )
        token_env = str(my_raw.get("token_env", "MYCELIA_LOCAL_TOKEN"))
        token_file_raw = str(my_raw.get("token_file", "") or os.environ.get("MYCELIA_LOCAL_TRANSPORT_TOKEN_PATH", ""))
        token_file = Path(token_file_raw) if token_file_raw else None
        token = str(
            my_raw.get("token", "")
            or os.environ.get(token_env, "")
            or os.environ.get("MYCELIA_LOCAL_TRANSPORT_TOKEN", "")
        )
        if not token:
            discovered, discovered_path = _discover_token_from_file(token_file)
            token = discovered
            token_file = discovered_path or token_file
        mycelia = MyceliaConfig(
            base_url=str(my_raw.get("base_url", "http://127.0.0.1:9999")),
            token=token,
            token_env=token_env,
            token_file=token_file,
            timeout_seconds=float(my_raw.get("timeout_seconds", 30.0)),
            enabled=bool(my_raw.get("enabled", False)),
            smql_table=str(my_raw.get("smql_table", "mycelia_embeddings")),
        )
        return cls(adapter=adapter, lmstudio=lmstudio, mycelia=mycelia)


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        return Settings.from_mapping({})
    config_path = Path(path)
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    return Settings.from_mapping(data)


def write_default_config(path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        """[adapter]
vault_path = ".smql_adapter"
default_collection = "default"
default_dimension = 384
strict_vram_required = false
store_text_default = false
# auto delegates full-dimensional searches to MyceliaDB v1.22b/v1.22c when available.
# mycelia fails closed unless MyceliaDB returns a full-dimensional vector result.
# sidecar keeps all searches in the local adapter vault.
search_backend = "auto"

# v1.22c sealed ABI controls.
# off: do not call sealed commands.
# auto: prefer sealed commands, fall back to v1.22b native vector search.
# required: fail closed unless MyceliaDB exposes v1.22c sealed ABI commands.
sealed_mode = "auto"
strict_no_cpu_ram_required = false
# Optional path to native sealed ABI DLL/SO loaded by MyceliaDB. Usually configured
# on the MyceliaDB process via MYCELIA_SMQL_SEALED_ABI_DLL.
sealed_abi_path = ""

[lmstudio]
base_url = "http://127.0.0.1:1234/v1"
embedding_model = "text-embedding-nomic-embed-text-v1.5"
chat_model = "local-model"
timeout_seconds = 120
enabled = false

[mycelia]
base_url = "http://127.0.0.1:9999"
# MyceliaDB creates this file by default: ../html/keys/local_transport.token
# You can also pass --mycelia-token-file or set MYCELIA_LOCAL_TRANSPORT_TOKEN_PATH.
token_env = "MYCELIA_LOCAL_TOKEN"
token_file = ""
timeout_seconds = 30
enabled = false
smql_table = "mycelia_embeddings"
""",
        encoding="utf-8",
    )
    return target
