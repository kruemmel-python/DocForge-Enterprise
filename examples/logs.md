Starting DocForge Enterprise job.
Command: 'D:\docforge_enterprise_new\.venv\Scripts\python.exe' -m docforge_enterprise.cli '.docforge_webgui\jobs\c12fe0de1d8b\input\sample_project.zip' --workspace '.docforge_webgui\jobs\c12fe0de1d8b\workspace' --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --analysis-workers 1 --chat-timeout 600.0 --embedding-timeout 300.0 --gateway-timeout 180.0 --final-timeout 600.0 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --chapter-max-tokens 3500 --llm-retries 3 --retry-backoff 2.0 --profile quick --single-pass-final --lmstudio-url http://127.0.0.1:1234/v1 --project-name test --retrieval-limit 8 --max-analysis-workers 2 --mycelia-url http://127.0.0.1:9999 --mycelia-token ba1EEvB3MJXBWCjeaA071_rcfvV7-FX6fDJ-FUG7yj4 --force-rebuild --fail-on-timeout --embedded-mycelia --embedded-mycelia-port 9999
[DocForge] Work estimate:
[DocForge]   profile: quick
[DocForge]   single_pass_final: True
[DocForge]   disable_module_reduce: True
[DocForge]   files: 3
[DocForge]   shards: 4
[DocForge]   modules: 2
[DocForge]   chapters: 4
[DocForge]   estimated_shard_analysis_calls: 4
[DocForge]   estimated_file_reduce_calls: 3
[DocForge]   estimated_module_reduce_calls: 0
[DocForge]   estimated_chapter_render_calls: 1
[DocForge]   estimated_embedding_ingest_batches: 1
[DocForge]   estimated_retrieval_embedding_calls: 5
[DocForge]   estimated_llm_chat_calls: 8
[DocForge]   estimated_embedding_calls: 6
DocForge Enterprise finished.
markdown: .docforge_webgui\jobs\c12fe0de1d8b\workspace\output\enterprise_documentation.md
html: .docforge_webgui\jobs\c12fe0de1d8b\workspace\output\enterprise_documentation.html
metadata: .docforge_webgui\jobs\c12fe0de1d8b\workspace\output\run_metadata.json
DocForge Enterprise finished successfully.