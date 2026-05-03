# Embedded MyceliaDB

DocForge Enterprise includes a bundled MyceliaDB-compatible gateway so a target
user does not need to install MyceliaDB separately.

## Components

- `myceliadb_embedded.gateway.EmbeddedMyceliaEngine`
- `myceliadb_embedded.cli`
- `smql_embedding_adapter`
- `MMapVectorStore`
- append-only `vectors.f32`
- `index.jsonl`
- `ledger.jsonl`
- `manifest.json`

## Runtime modes

### Embedded per-run mode

```bash
docforge-enterprise project.zip --embedded-mycelia
```

The CLI starts the gateway in-process, runs the documentation pipeline and shuts
it down afterwards.

### Long-running local gateway

```bash
embedded-myceliadb --port 9999 --root .docforge_workspace/embedded_myceliadb
```

Use this mode when multiple documentation runs should reuse the same semantic
index.

## API compatibility

Implemented commands:

| Command | Purpose |
|---|---|
| `check_integrity` | Health and integrity probe |
| `store_embedding` | Store one vector record |
| `store_embedding_sealed` | Compatibility alias with explicit sealed-status flags |
| `find_embedding` | Full-dimensional cosine retrieval |
| `find_embedding_sealed` | Compatibility alias with explicit sealed-status flags |
| `smql_vector_index_status` | Collection status |
| `smql_sealed_abi_status` | Sealed ABI diagnostic |
| `smql_forensic_attestation` | Local forensic metadata |

## Non-goals

The embedded gateway is not a full replacement for every historical MyceliaDB
web-platform feature. It is a self-contained enterprise documentation runtime
for SMQL embeddings and retrieval.

It reports native VRAM/sealed residency as false unless a future native backend
is connected.
