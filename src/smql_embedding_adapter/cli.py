"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .adapter import EmbeddingAdapter
from .chunking import TextChunker
from .config import Settings, load_settings, write_default_config
from .embeddings import DeterministicLocalEmbedder
from .server import run_server
from .mycelia_client import MyceliaGatewayClient
from .exceptions import MyceliaGatewayError


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = load_settings(args.config) if getattr(args, "config", None) else Settings()

    if getattr(args, "vault", None):
        settings.adapter.vault_path = Path(args.vault)
    if getattr(args, "collection", None):
        settings.adapter.default_collection = str(args.collection)
    if getattr(args, "lmstudio_url", None):
        settings.lmstudio.base_url = str(args.lmstudio_url)
        settings.lmstudio.enabled = True
    if getattr(args, "embedding_model", None):
        settings.lmstudio.embedding_model = str(args.embedding_model)
        settings.lmstudio.enabled = True
    if getattr(args, "mycelia_url", None):
        settings.mycelia.base_url = str(args.mycelia_url)
        settings.mycelia.enabled = True
    if getattr(args, "mycelia_token_file", None):
        token_file = Path(str(args.mycelia_token_file))
        settings.mycelia.token_file = token_file
        settings.mycelia.token = token_file.read_text(encoding="utf-8").strip()
        settings.mycelia.enabled = True
    if getattr(args, "mycelia_token", None):
        settings.mycelia.token = str(args.mycelia_token)
        settings.mycelia.enabled = True
    if getattr(args, "dimension", None):
        settings.adapter.default_dimension = int(args.dimension)
    if getattr(args, "search_backend", None):
        settings.adapter.search_backend = str(args.search_backend).lower()
    if getattr(args, "strict_vram_required", None):
        settings.adapter.strict_vram_required = bool(args.strict_vram_required)
    if getattr(args, "sealed_mode", None):
        settings.adapter.sealed_mode = str(args.sealed_mode).lower()
    if getattr(args, "strict_no_cpu_ram_required", None):
        settings.adapter.strict_no_cpu_ram_required = bool(args.strict_no_cpu_ram_required)
    if getattr(args, "sealed_abi_path", None):
        settings.adapter.sealed_abi_path = str(args.sealed_abi_path)
    return settings


def _adapter(settings: Settings) -> EmbeddingAdapter:
    return EmbeddingAdapter(settings)


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def cmd_init(args: argparse.Namespace) -> int:
    path = write_default_config(args.path)
    _print({"status": "ok", "config": str(path)})
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    adapter = EmbeddingAdapter(settings, embedder=DeterministicLocalEmbedder(settings.adapter.default_dimension))
    collection = args.collection or "selftest"
    adapter.store(collection).reset()
    ingest = adapter.ingest_texts(
        [
            "MyceliaDB speichert Attraktoren in einer dynamischen assoziativen Datenbank.",
            "LM Studio stellt lokale OpenAI-kompatible Embeddings bereit.",
            "Cosine Similarity vergleicht normalisierte Vektoren.",
        ],
        ids=["mycelia", "lmstudio", "cosine"],
        collection=collection,
    )
    query = adapter.query_text("lokale embeddings von lm studio", collection=collection, limit=2)
    ok = query.count >= 1 and query.results[0].id in {"lmstudio", "mycelia", "cosine"}
    _print(
        {
            "status": "ok" if ok else "error",
            "ingest": adapter.result_to_json(ingest),
            "query": adapter.result_to_json(query),
        }
    )
    return 0 if ok else 2


def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    run_server(settings, host=args.host, port=args.port)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    adapter = _adapter(settings)
    store = adapter.store(args.collection or settings.adapter.default_collection)
    _print(
        {
            "collection": store.collection,
            "vault": str(store.path),
            "dimension": store.dimension,
            "active_records": store.count,
            "merkle_head": store.ledger.head,
            "vectors_path": str(store.vectors_path),
            "index_path": str(store.index_path),
        }
    )
    return 0


def cmd_mycelia_status(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.mycelia.enabled:
        _print(
            {
                "status": "not-configured",
                "message": "Pass --mycelia-url and, if token binding is enabled, --mycelia-token-file or --mycelia-token.",
            }
        )
        return 2
    client = MyceliaGatewayClient(
        base_url=settings.mycelia.base_url,
        token=settings.mycelia.token,
        timeout_seconds=settings.mycelia.timeout_seconds,
        smql_table=settings.mycelia.smql_table,
    )

    base: dict[str, Any] = {
        "mycelia_url": settings.mycelia.base_url,
        "token_file": str(settings.mycelia.token_file) if settings.mycelia.token_file else None,
        "token_present": bool(settings.mycelia.token),
    }

    try:
        probe = client.probe_connection()
    except MyceliaGatewayError as exc:
        _print(
            {
                **base,
                "status": "error",
                "message": str(exc),
                "hint": (
                    "HTTP 403 means the X-Mycelia-Local-Token value is missing or wrong. "
                    "Use --mycelia-token-file ..\\html\\keys\\local_transport.token. "
                    "Connection refused means mycelia_platform.py is not listening on this URL."
                ),
            }
        )
        return 1

    response: dict[str, Any] = {
        **base,
        "status": probe.get("status", "unknown"),
        "probe_command": "check_integrity",
        "driver_mode": probe.get("driver_mode"),
        "opencl_active": probe.get("opencl_active"),
        "gpu_crypto_active": probe.get("gpu_crypto_active"),
        "attractors": probe.get("attractors"),
        "snapshot_exists": probe.get("snapshot_exists"),
        "message": "Local transport token accepted. Protected status commands may still require an Engine-Session.",
    }

    # Best-effort: the richer transport status is session-bound in current
    # MyceliaDB builds.  Report that explicitly instead of failing the probe.
    try:
        protected = client.transport_status()
    except MyceliaGatewayError as exc:
        response["protected_status"] = {
            "status": "error",
            "message": str(exc),
        }
    else:
        protected_status = str(protected.get("status", "unknown"))
        protected_message = str(protected.get("message", ""))
        if protected_status == "error" and "Engine-Session" in protected_message:
            response["protected_status"] = {
                "status": "engine-session-required",
                "message": protected_message,
            }
        else:
            response["protected_status"] = protected

    try:
        response["vector_index"] = client.vector_index_status()
    except MyceliaGatewayError as exc:
        message = str(exc)
        if "Unbekannter Befehl" in message or "unknown command" in message.lower():
            response["vector_index"] = {
                "status": "unavailable",
                "message": "smql_vector_index_status requires MyceliaDB v1.22b patch.",
            }
        else:
            response["vector_index"] = {"status": "error", "message": message}

    try:
        response["sealed_abi"] = client.sealed_abi_status()
    except MyceliaGatewayError as exc:
        message = str(exc)
        if "Unbekannter Befehl" in message or "unknown command" in message.lower():
            response["sealed_abi"] = {
                "status": "unavailable",
                "message": "smql_sealed_abi_status requires MyceliaDB v1.22c patch.",
            }
        else:
            response["sealed_abi"] = {"status": "error", "message": message}

    _print(response)
    return 0 if response.get("status") == "ok" else 1



def cmd_forensic_attestation(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if not settings.mycelia.enabled:
        _print({"status": "not-configured", "message": "Pass --mycelia-url and token options."})
        return 2
    client = MyceliaGatewayClient(
        base_url=settings.mycelia.base_url,
        token=settings.mycelia.token,
        timeout_seconds=settings.mycelia.timeout_seconds,
        smql_table=settings.mycelia.smql_table,
    )
    try:
        response = client.forensic_attestation(collection=args.collection or settings.adapter.default_collection)
    except MyceliaGatewayError as exc:
        _print({"status": "error", "message": str(exc)})
        return 1
    _print(response)
    return 0 if response.get("status") == "ok" else 1

def cmd_reset_collection(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    adapter = _adapter(settings)
    store = adapter.store(args.collection or settings.adapter.default_collection)
    if not args.yes:
        _print(
            {
                "status": "error",
                "message": "Refusing to reset without --yes. "
                "This deletes the active collection index/vectors/ledger.",
                "collection": store.collection,
                "vault": str(store.path),
            }
        )
        return 2
    store.reset()
    _print(
        {
            "status": "ok",
            "collection": store.collection,
            "vault": str(store.path),
            "dimension": store.dimension,
            "merkle_head": store.ledger.head,
        }
    )
    return 0


def cmd_ingest_file(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    chunker = TextChunker(chunk_chars=args.chunk_chars, overlap_chars=args.overlap_chars)
    chunks = chunker.chunk_file(args.file)
    adapter = _adapter(settings)
    result = adapter.ingest_texts(
        [c.text for c in chunks],
        ids=[c.id for c in chunks],
        metadata=[
            {"source": str(Path(args.file).resolve()), "start": c.start, "end": c.end}
            for c in chunks
        ],
        collection=args.collection or settings.adapter.default_collection,
        store_text=args.store_text,
    )
    _print(adapter.result_to_json(result))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    adapter = _adapter(settings)
    result = adapter.query_text(
        args.text,
        collection=args.collection or settings.adapter.default_collection,
        limit=args.limit,
    )
    _print(adapter.result_to_json(result))
    return 0



def cmd_chat(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    if getattr(args, "chat_model", None):
        settings.lmstudio.chat_model = str(args.chat_model)
        settings.lmstudio.enabled = True
    adapter = _adapter(settings)
    result = adapter.rag_chat(
        args.question,
        collection=args.collection or settings.adapter.default_collection,
        limit=args.limit,
        temperature=args.temperature,
        max_context_chars=args.max_context_chars,
    )
    _print(result)
    return 0 if result.get("status") == "ok" else 1


def cmd_smql(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    adapter = _adapter(settings)
    result = adapter.query_smql(
        args.query,
        collection=args.collection or settings.adapter.default_collection,
    )
    _print(adapter.result_to_json(result))
    return 0


def _add_runtime_options(target: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    """Register adapter-wide options.

    argparse normally accepts global options only before the subcommand.  The same
    options are also attached to subcommands with suppressed defaults so commands
    like ``ingest-file README.md --collection demo`` work without overwriting a
    value provided before the subcommand.
    """
    default: Any = argparse.SUPPRESS if suppress_defaults else None
    target.add_argument("--config", default=default, help="Path to TOML config")
    target.add_argument("--vault", default=default, help="Vault directory")
    target.add_argument("--collection", default=default, help="Collection name")
    target.add_argument("--dimension", default=default, type=int, help="Fallback embedding dimension")
    target.add_argument("--lmstudio-url", default=default, help="LM Studio base URL, e.g. http://127.0.0.1:1234/v1")
    target.add_argument("--embedding-model", default=default, help="LM Studio embedding model")
    target.add_argument("--mycelia-url", default=default, help="MyceliaDB Gateway URL, e.g. http://127.0.0.1:9999")
    target.add_argument("--mycelia-token", default=default, help="MyceliaDB local transport token")
    target.add_argument("--mycelia-token-file", default=default, help="Path to MyceliaDB keys/local_transport.token")
    target.add_argument(
        "--search-backend",
        default=default,
        choices=("auto", "mycelia", "sidecar"),
        help="Retrieval path: auto delegates to MyceliaDB v1.22b when available; mycelia fails closed; sidecar disables delegation.",
    )
    target.add_argument(
        "--strict-vram-required",
        default=default,
        action="store_true",
        help="Fail closed unless MyceliaDB reports a VRAM-resident native vector backend.",
    )
    target.add_argument(
        "--sealed-mode",
        default=default,
        choices=("off", "auto", "required"),
        help="v1.22c sealed ABI mode: off, auto, or required.",
    )
    target.add_argument(
        "--strict-no-cpu-ram-required",
        default=default,
        action="store_true",
        help="Fail closed unless MyceliaDB returns a v1.22c forensic no-CPU-RAM attestation.",
    )
    target.add_argument(
        "--sealed-abi-path",
        default=default,
        help="Path to native sealed ABI DLL/SO. Usually set for the MyceliaDB process.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smql-adapter")
    _add_runtime_options(parser)

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Write default config")
    p_init.add_argument("path", nargs="?", default="configs/local.toml")
    _add_runtime_options(p_init, suppress_defaults=True)
    p_init.set_defaults(func=cmd_init)

    p_self = sub.add_parser("selftest", help="Run offline smoke test")
    _add_runtime_options(p_self, suppress_defaults=True)
    p_self.set_defaults(func=cmd_selftest)

    p_info = sub.add_parser("info", help="Show vault/collection metadata")
    _add_runtime_options(p_info, suppress_defaults=True)
    p_info.set_defaults(func=cmd_info)

    p_reset = sub.add_parser("reset-collection", help="Delete one collection vault")
    p_reset.add_argument("--yes", action="store_true", help="Confirm destructive reset")
    _add_runtime_options(p_reset, suppress_defaults=True)
    p_reset.set_defaults(func=cmd_reset_collection)

    p_mycelia = sub.add_parser("mycelia-status", help="Check MyceliaDB local transport/auth status")
    _add_runtime_options(p_mycelia, suppress_defaults=True)
    p_mycelia.set_defaults(func=cmd_mycelia_status)

    p_attest = sub.add_parser("forensic-attestation", help="Check v1.22c sealed ABI no-CPU-RAM proof state")
    _add_runtime_options(p_attest, suppress_defaults=True)
    p_attest.set_defaults(func=cmd_forensic_attestation)

    p_serve = sub.add_parser("serve", help="Run HTTP sidecar")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    _add_runtime_options(p_serve, suppress_defaults=True)
    p_serve.set_defaults(func=cmd_serve)

    p_ingest = sub.add_parser("ingest-file", help="Chunk and ingest a text file")
    p_ingest.add_argument("file")
    p_ingest.add_argument("--chunk-chars", type=int, default=1600)
    p_ingest.add_argument("--overlap-chars", type=int, default=160)
    p_ingest.add_argument("--store-text", action="store_true")
    _add_runtime_options(p_ingest, suppress_defaults=True)
    p_ingest.set_defaults(func=cmd_ingest_file)

    p_query = sub.add_parser("query", help="Semantic query")
    p_query.add_argument("text")
    p_query.add_argument("--limit", type=int, default=3)
    _add_runtime_options(p_query, suppress_defaults=True)
    p_query.set_defaults(func=cmd_query)

    p_chat = sub.add_parser("chat", help="RAG chat via LM Studio using SMQL retrieval")
    _add_runtime_options(p_chat, suppress_defaults=True)
    p_chat.add_argument("question")
    p_chat.add_argument("--chat-model", default=None, help="LM Studio chat model id")
    p_chat.add_argument("--limit", type=int, default=4)
    p_chat.add_argument("--temperature", type=float, default=0.15)
    p_chat.add_argument("--max-context-chars", type=int, default=12000)
    p_chat.set_defaults(func=cmd_chat)

    p_smql = sub.add_parser("smql", help="Run SMQL embedding query")
    p_smql.add_argument("query")
    _add_runtime_options(p_smql, suppress_defaults=True)
    p_smql.set_defaults(func=cmd_smql)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
