# Controls Matrix

| Control | Check ID | Expected |
|---|---|---|
| Adapter availability | CONN-ADAPTER-001 | `/health` returns JSON ok |
| LM Studio availability | CONN-LMSTUDIO-001 | `/v1/models` returns JSON |
| Token boundary | GW-TOKEN-001 | no token/bad token rejected, good token accepted |
| Native retrieval | MYC-STATUS-001 | backend `opencl-vram`, collection present |
| JSON-only web API | WEB-API-001 | no HTML redirects |
| RAG sources | RAG-CHAT-001 | sources >= min_sources |
| Prompt-injection smoke | RAG-REDTEAM-001 | no secret-like output |
| Secret leakage | DISK-SECRET-001 | no secrets outside token file |
| v1.22d persistence | PERSIST-001 | JSONL exists, parseable |
| RAM probe readiness | RAM-PROBE-READY-001 | py_compile ok |
| Live RAM probe | RAM-PROBE-LIVE-001 | optional negative vector-fragment scan |
