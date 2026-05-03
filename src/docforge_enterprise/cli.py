from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

from .config import Settings
from .pipeline import DocumentationPipeline
from myceliadb_embedded.gateway import start_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docforge-enterprise",
        description="Generate enterprise documentation from a ZIP project, Markdown code dump or directory.",
    )
    parser.add_argument("input", type=Path, help="Path to .zip, .md or source directory.")
    parser.add_argument("--config", type=Path, default=None, help="Path to TOML configuration.")
    parser.add_argument("--workspace", type=Path, default=None, help="Override workspace directory.")
    parser.add_argument("--project-name", default="", help="Override project name.")
    parser.add_argument("--chat-model", default="", help="LM Studio chat model.")
    parser.add_argument("--embedding-model", default="", help="LM Studio embedding model.")
    parser.add_argument("--lmstudio-url", default="", help="LM Studio OpenAI-compatible base URL.")
    parser.add_argument("--mycelia-url", default="", help="MyceliaDB gateway URL.")
    parser.add_argument("--mycelia-token", default="", help="MyceliaDB local token. Prefer env var in production.")
    parser.add_argument("--sidecar-only", action="store_true", help="Disable MyceliaDB gateway and use mmap sidecar only.")
    parser.add_argument("--embedded-mycelia", action="store_true", help="Start the bundled embedded MyceliaDB-compatible gateway for this run.")
    parser.add_argument("--embedded-mycelia-root", type=Path, default=None, help="Persistent root for the embedded MyceliaDB vault.")
    parser.add_argument("--embedded-mycelia-port", type=int, default=9999, help="Port for --embedded-mycelia.")
    parser.add_argument("--dry-run", action="store_true", help="Do not call LM Studio; produce structural fallback docs.")
    parser.add_argument("--force-rebuild", action="store_true", help="Clear/rebuild workspace and cached analyses.")
    parser.add_argument("--max-chars-per-shard", type=int, default=0, help="Override shard size.")
    parser.add_argument("--retrieval-limit", type=int, default=0, help="Override semantic retrieval Top-K.")
    parser.add_argument("--analysis-workers", type=int, default=0, help="Parallel shard analysis workers. Use 1 for strict sequential execution.")
    parser.add_argument("--max-analysis-workers", type=int, default=0, help="Hard cap for local LM Studio concurrency.")
    parser.add_argument("--chat-timeout", type=float, default=0.0, help="Timeout in seconds for LM Studio chat requests.")
    parser.add_argument("--embedding-timeout", type=float, default=0.0, help="Timeout in seconds for LM Studio embedding requests.")
    parser.add_argument("--gateway-timeout", type=float, default=0.0, help="Timeout in seconds for MyceliaDB gateway requests.")
    parser.add_argument("--final-timeout", type=float, default=0.0, help="Timeout in seconds for chapter rendering requests.")
    parser.add_argument("--llm-retries", type=int, default=-1, help="Retry count for timeout-prone local HTTP calls.")
    parser.add_argument("--retry-backoff", type=float, default=0.0, help="Base exponential backoff in seconds.")
    parser.add_argument("--max-embedding-batch-size", type=int, default=0, help="Limit embedding batch size to avoid local-model timeouts.")
    parser.add_argument("--analysis-max-tokens", type=int, default=0, help="Max output tokens for JSON analysis calls.")
    parser.add_argument("--chapter-max-tokens", type=int, default=0, help="Max output tokens for chapter rendering.")
    parser.add_argument("--no-adaptive-shard", action="store_true", help="Disable smaller-prompt retry after a shard timeout.")
    parser.add_argument("--fail-on-timeout", action="store_true", help="Abort instead of writing fallback records after timeout.")
    parser.add_argument("--profile", choices=["quick", "balanced", "enterprise"], default="", help="Documentation depth profile. quick uses one compact final pass; enterprise is full chapter rendering.")
    parser.add_argument("--chapters", default="", help="Comma-separated chapter titles to render. Overrides profile chapter defaults.")
    parser.add_argument("--single-pass-final", action="store_true", help="Render final documentation with one LLM call instead of one call per chapter.")
    parser.add_argument("--disable-module-reduce", action="store_true", help="Skip LLM module reduction and build structural module summaries.")
    parser.add_argument("--max-final-chapters", type=int, default=0, help="Limit number of final chapters after profile/custom chapter selection.")
    parser.add_argument("--estimate-only", action="store_true", help="Only estimate LLM/embedding calls and write metadata; no LM Studio calls.")
    parser.add_argument("--quiet-plan", action="store_true", help="Do not print detailed LLM work estimate.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result JSON.")
    return parser


def apply_cli_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.workspace is not None:
        settings.pipeline.workspace = args.workspace
        # Keep default vault inside the selected workspace unless config explicitly changed it.
        settings.mycelia.vault_path = args.workspace / "mycelia_vault"
    if args.project_name:
        settings.pipeline.project_name = args.project_name
    if args.chat_model:
        settings.lmstudio.chat_model = args.chat_model
    if args.embedding_model:
        settings.lmstudio.embedding_model = args.embedding_model
    if args.lmstudio_url:
        settings.lmstudio.base_url = args.lmstudio_url
    if args.mycelia_url:
        settings.mycelia.base_url = args.mycelia_url
    if args.mycelia_token:
        settings.mycelia.token = args.mycelia_token
    if args.sidecar_only:
        settings.mycelia.enabled = False
        settings.mycelia.search_backend = "sidecar"
    if args.dry_run:
        settings.pipeline.dry_run = True
        settings.mycelia.enabled = False
        settings.mycelia.search_backend = "sidecar"
    if args.force_rebuild:
        settings.pipeline.force_rebuild = True
    if args.max_chars_per_shard > 0:
        settings.pipeline.max_chars_per_shard = args.max_chars_per_shard
    if args.retrieval_limit > 0:
        settings.pipeline.retrieval_limit = args.retrieval_limit
    if args.analysis_workers > 0:
        settings.pipeline.analysis_workers = args.analysis_workers
    if args.max_analysis_workers > 0:
        settings.pipeline.max_analysis_workers = args.max_analysis_workers
    if args.chat_timeout > 0:
        settings.lmstudio.chat_timeout_seconds = args.chat_timeout
    if args.embedding_timeout > 0:
        settings.lmstudio.embedding_timeout_seconds = args.embedding_timeout
    if args.gateway_timeout > 0:
        settings.lmstudio.gateway_timeout_seconds = args.gateway_timeout
    if args.final_timeout > 0:
        settings.lmstudio.final_timeout_seconds = args.final_timeout
    if args.llm_retries >= 0:
        settings.lmstudio.request_retries = args.llm_retries
    if args.retry_backoff > 0:
        settings.lmstudio.retry_backoff_seconds = args.retry_backoff
    if args.max_embedding_batch_size > 0:
        settings.pipeline.max_embedding_batch_size = args.max_embedding_batch_size
    if args.analysis_max_tokens > 0:
        settings.lmstudio.max_json_tokens = args.analysis_max_tokens
    if args.chapter_max_tokens > 0:
        settings.lmstudio.max_chapter_tokens = args.chapter_max_tokens
    if args.no_adaptive_shard:
        settings.pipeline.adaptive_shard_on_timeout = False
    if args.fail_on_timeout:
        settings.pipeline.continue_on_timeout = False
    if args.profile:
        settings.pipeline.profile = args.profile
    if args.chapters:
        settings.pipeline.chapters = args.chapters
    if args.single_pass_final:
        settings.pipeline.single_pass_final = True
    if args.disable_module_reduce:
        settings.pipeline.disable_module_reduce = True
    if args.max_final_chapters > 0:
        settings.pipeline.max_final_chapters = args.max_final_chapters
    if args.estimate_only:
        settings.pipeline.estimate_only = True
        settings.mycelia.enabled = False
        settings.mycelia.search_backend = "sidecar"
    if args.quiet_plan:
        settings.pipeline.explain_llm_calls = False
    settings.normalize()
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings.from_toml(args.config)
    settings = apply_cli_overrides(settings, args)

    embedded_server = None
    if args.embedded_mycelia:
        root = args.embedded_mycelia_root or (settings.pipeline.workspace / "embedded_myceliadb")
        embedded_server = start_server(
            host="127.0.0.1",
            port=args.embedded_mycelia_port,
            root=root,
            token=settings.mycelia.token,
            quiet=True,
        )
        thread = threading.Thread(target=embedded_server.serve_forever, name="embedded-myceliadb", daemon=True)
        thread.start()
        settings.mycelia.enabled = True
        settings.mycelia.base_url = f"http://127.0.0.1:{args.embedded_mycelia_port}"
        settings.mycelia.search_backend = "auto"

    pipeline = DocumentationPipeline(input_path=args.input, settings=settings)
    try:
        result = pipeline.run()
    finally:
        pipeline.close()
        if embedded_server is not None:
            embedded_server.shutdown()
            embedded_server.server_close()

    if args.json:
        print(json.dumps({"output_paths": result.output_paths, "metadata": result.metadata}, ensure_ascii=False, indent=2))
    else:
        print("DocForge Enterprise finished.")
        for kind, path in result.output_paths.items():
            print(f"{kind}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
