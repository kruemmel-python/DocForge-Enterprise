from __future__ import annotations
import argparse, json, threading
from pathlib import Path
from .config import Settings
from .pipeline import DocumentationPipeline
from myceliadb_embedded.gateway import start_server
def build_parser():
    p=argparse.ArgumentParser(prog="docforge-enterprise")
    p.add_argument("input",type=Path); p.add_argument("--config",type=Path); p.add_argument("--workspace",type=Path); p.add_argument("--project-name",default="")
    p.add_argument("--chat-model",default=""); p.add_argument("--embedding-model",default=""); p.add_argument("--lmstudio-url",default="")
    p.add_argument("--embedded-mycelia",action="store_true"); p.add_argument("--sidecar-only",action="store_true"); p.add_argument("--mycelia-url",default=""); p.add_argument("--mycelia-token",default=""); p.add_argument("--embedded-mycelia-port",type=int,default=9999)
    p.add_argument("--dry-run",action="store_true"); p.add_argument("--force-rebuild",action="store_true"); p.add_argument("--language", choices=["de","en"], default="de", help="GUI/documentation output language")
    p.add_argument("--profile",choices=["quick","balanced","enterprise"],default="balanced"); p.add_argument("--chapters",default=""); p.add_argument("--single-pass-final",action="store_true"); p.add_argument("--disable-module-reduce",action="store_true"); p.add_argument("--max-final-chapters",type=int,default=0); p.add_argument("--estimate-only",action="store_true")
    p.add_argument("--analysis-workers",type=int,default=1); p.add_argument("--max-analysis-workers",type=int,default=2); p.add_argument("--retrieval-limit",type=int,default=0); p.add_argument("--chat-timeout",type=float,default=0); p.add_argument("--embedding-timeout",type=float,default=0); p.add_argument("--gateway-timeout",type=float,default=0); p.add_argument("--final-timeout",type=float,default=0); p.add_argument("--llm-retries",type=int,default=-1); p.add_argument("--retry-backoff",type=float,default=0); p.add_argument("--max-chars-per-shard",type=int,default=0); p.add_argument("--max-embedding-batch-size",type=int,default=0); p.add_argument("--analysis-max-tokens",type=int,default=0); p.add_argument("--chapter-max-tokens",type=int,default=0)
    p.add_argument("--no-adaptive-shard",action="store_true",help="Disable adaptive smaller-prompt retry after shard timeout.")
    p.add_argument("--fail-on-timeout",action="store_true",help="Abort on timeout-prone failures instead of continuing with fallbacks.")
    p.add_argument("--allow-missing-shard-analyses",action="store_true",help="Continue with source fallback if File-Reduce has no shard analyses.")
    p.add_argument("--json",action="store_true")
    return p
def apply(settings,args):
    if args.workspace: settings.pipeline.workspace=args.workspace; settings.mycelia.vault_path=args.workspace/"mycelia_vault"
    if args.project_name: settings.pipeline.project_name=args.project_name
    if args.chat_model: settings.lmstudio.chat_model=args.chat_model
    if args.embedding_model: settings.lmstudio.embedding_model=args.embedding_model
    if args.lmstudio_url: settings.lmstudio.base_url=args.lmstudio_url
    if args.mycelia_url: settings.mycelia.base_url=args.mycelia_url
    if args.mycelia_token: settings.mycelia.token=args.mycelia_token
    if args.sidecar_only: settings.mycelia.enabled=False; settings.mycelia.search_backend="sidecar"
    if args.dry_run: settings.pipeline.dry_run=True; settings.mycelia.enabled=False
    if args.force_rebuild: settings.pipeline.force_rebuild=True
    settings.pipeline.output_language=args.language
    settings.pipeline.profile=args.profile; settings.pipeline.chapters=args.chapters; settings.pipeline.single_pass_final|=args.single_pass_final; settings.pipeline.disable_module_reduce|=args.disable_module_reduce; settings.pipeline.max_final_chapters=args.max_final_chapters; settings.pipeline.estimate_only=args.estimate_only
    settings.pipeline.analysis_workers=args.analysis_workers; settings.pipeline.max_analysis_workers=args.max_analysis_workers
    if args.retrieval_limit: settings.pipeline.retrieval_limit=args.retrieval_limit
    if args.chat_timeout: settings.lmstudio.chat_timeout_seconds=args.chat_timeout
    if args.embedding_timeout: settings.lmstudio.embedding_timeout_seconds=args.embedding_timeout
    if args.gateway_timeout: settings.lmstudio.gateway_timeout_seconds=args.gateway_timeout
    if args.final_timeout: settings.lmstudio.final_timeout_seconds=args.final_timeout
    if args.llm_retries>=0: settings.lmstudio.request_retries=args.llm_retries
    if args.retry_backoff: settings.lmstudio.retry_backoff_seconds=args.retry_backoff
    if args.max_chars_per_shard: settings.pipeline.max_chars_per_shard=args.max_chars_per_shard
    if args.max_embedding_batch_size: settings.pipeline.max_embedding_batch_size=args.max_embedding_batch_size
    if args.analysis_max_tokens: settings.lmstudio.max_json_tokens=args.analysis_max_tokens
    if args.chapter_max_tokens: settings.lmstudio.max_chapter_tokens=args.chapter_max_tokens
    if args.no_adaptive_shard: settings.pipeline.adaptive_shard_on_timeout=False
    if args.fail_on_timeout: settings.pipeline.continue_on_timeout=False
    if args.allow_missing_shard_analyses: settings.pipeline.fail_on_missing_shards=False
    retrieval_override = int(args.retrieval_limit or 0)
    settings.normalize()
    if retrieval_override > 0:
        settings.pipeline.retrieval_limit = retrieval_override
    return settings
def main(argv=None):
    args=build_parser().parse_args(argv); settings=apply(Settings.from_toml(args.config),args)
    server=None
    if args.embedded_mycelia:
        server=start_server(host="127.0.0.1",port=args.embedded_mycelia_port,root=settings.pipeline.workspace/"embedded_myceliadb",token=settings.mycelia.token,quiet=True)
        threading.Thread(target=server.serve_forever,daemon=True).start(); settings.mycelia.enabled=True; settings.mycelia.base_url=f"http://127.0.0.1:{args.embedded_mycelia_port}"
    pipe=DocumentationPipeline(input_path=args.input,settings=settings)
    try: res=pipe.run()
    finally:
        pipe.close()
        if server: server.shutdown(); server.server_close()
    if args.json: print(json.dumps({"output_paths":res.output_paths,"metadata":res.metadata},ensure_ascii=False,indent=2))
    else:
        print("DocForge Enterprise finished."); [print(f"{k}: {v}") for k,v in res.output_paths.items()]
    return 0
if __name__=="__main__": raise SystemExit(main())
