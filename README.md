# DocForge Enterprise

**DocForge Enterprise** is a local-first enterprise documentation platform for software projects. It generates professional, evidence-oriented technical documentation from ZIP archives, Markdown code dumps, and local source directories by combining semantic code indexing, local LLM analysis, MyceliaDB-backed retrieval, and a hardened WebGUI.

DocForge is designed for environments where source code, architecture knowledge, and intellectual property must remain under local control.

---

## Core Capabilities

- Generate enterprise-grade documentation from source projects.
- Work with ZIP archives, Markdown code dumps, or local directories.
- Use LM Studio with local chat and embedding models.
- Use embedded MyceliaDB, an external MyceliaDB gateway, or a local sidecar vector store.
- Analyze code in stages: shards, files, modules, chapters.
- Produce Markdown, HTML, JSON metadata, SQLite analysis records, and vector-index artifacts.
- Support Quick, Balanced, and Enterprise documentation profiles.
- Provide a local WebGUI with authentication, role foundation, CSRF protection, upload limits, and audit logging.
- Validate documentation claims against extracted source evidence.
- Report evidence coverage and mark unsupported claims.
- Run completely local-first without requiring cloud LLM APIs.

---

## What Problem Does DocForge Solve?

Local LLMs cannot safely or reliably ingest large repositories in one prompt. DocForge avoids the “one giant prompt” model by building a structured documentation pipeline:

```text
Input project
  -> secure extraction and filtering
  -> semantic sharding
  -> embeddings
  -> retrieval context
  -> shard analysis
  -> file summaries
  -> module summaries
  -> claim and evidence validation
  -> chapter rendering
  -> Markdown / HTML / JSON output
```

This architecture keeps prompts small, improves context quality, and creates traceable intermediate artifacts.

---

## Supported Inputs

DocForge accepts:

- `.zip` project archives
- `.md` code dumps
- local source directories

Typical project content may include:

```text
src/
tests/
docs/
README.md
pyproject.toml
package.json
pom.xml
Dockerfile
```

Vendor directories, binary files, virtual environments, secrets, tokens, local databases, and generated caches are filtered by default.

---

## Documentation Profiles

DocForge provides three documentation profiles so that small projects do not need to run the full enterprise pipeline.

| Profile | Purpose | Final Rendering | Typical Use |
|---|---|---|---|
| `quick` | Fast overview with reduced LLM work | compact single-pass final rendering | samples, smoke tests, small tools |
| `balanced` | Practical technical documentation | reduced chapter set | normal project documentation |
| `enterprise` | Deep, auditable documentation | full chapter-oriented pipeline | large codebases, architecture reviews, security reviews |

### Quick Profile

Use this for a first pass or small projects:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

### Balanced Profile

Use this for normal projects:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\path\to\project.zip" --profile balanced --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

### Enterprise Profile

Use this for deep, audit-oriented documentation:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\path\to\project.zip" --profile enterprise --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

---

## Estimate-Only Mode

Before spending local model runtime, you can estimate the expected work:

```powershell
docforge-enterprise "D:\path\to\project.zip" --profile quick --estimate-only --force-rebuild
```

The estimate includes:

```text
files
shards
modules
chapters
estimated shard-analysis calls
estimated file-reduce calls
estimated module-reduce calls
estimated chapter-render calls
estimated LLM chat calls
estimated embedding calls
```

This is useful before running large projects on local hardware.

---

## Architecture

DocForge uses a staged, auditable architecture.

```text
                 ┌──────────────────────┐
                 │ Input Project         │
                 │ ZIP / MD / Directory  │
                 └──────────┬───────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ Secure Extraction     │
                 │ filtering / redaction │
                 └──────────┬───────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ Semantic Sharding     │
                 │ AST / language-aware  │
                 └──────────┬───────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ Embedding Index       │
                 │ MyceliaDB / Sidecar   │
                 └──────────┬───────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ LLM Analysis          │
                 │ shard / file / module │
                 └──────────┬───────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ Evidence Validation   │
                 │ coverage / claims     │
                 └──────────┬───────────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ Documentation Output  │
                 │ MD / HTML / JSON      │
                 └──────────────────────┘
```

---

## Language-Aware Sharding

DocForge does not rely only on fixed-size text chunks. It uses language-aware splitting where possible.

| Language / Format | Sharding Strategy |
|---|---|
| Python | `ast`-based class/function extraction |
| Java / C# / C / C++ / Go / Rust / PHP | declaration and brace-aware segmentation |
| JavaScript / TypeScript / React | function, class, interface, type, and arrow-function detection |
| SQL | statement-level segmentation |
| Markdown | heading-section segmentation |
| Other formats | safe generic text sharding |

This improves retrieval quality and reduces the chance that important implementation details are separated from their context.

---

## LLM Workflow

DocForge uses the LLM for several distinct tasks:

```text
1. understand code shards
2. extract interfaces and dependencies
3. identify business rules
4. identify security and operational risks
5. create evidence-oriented shard summaries
6. reduce shard summaries into file summaries
7. reduce file summaries into module summaries
8. render final documentation chapters
9. validate claims and evidence coverage
```

The goal is not only text generation. The goal is to build an auditable knowledge model from source code.

---

## LM Studio Setup

1. Open LM Studio.
2. Load a chat model.
3. Load an embedding model.
4. Start the local server.

Recommended default values:

```text
LM Studio Base URL: http://127.0.0.1:1234/v1
Chat Model:        google_gemma-4-e4b-it
Embedding Model:   text-embedding-nomic-embed-text-v2-moe
```

---

## MyceliaDB Modes

DocForge supports multiple semantic storage modes.

### Embedded MyceliaDB

Starts the bundled MyceliaDB-compatible local gateway automatically for the run.

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\path\to\project.zip" --embedded-mycelia --profile balanced --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --force-rebuild
```

### Sidecar Vector Store

Uses the local mmap-based vector store without a gateway process.

```powershell
docforge-enterprise "D:\path\to\project.zip" --sidecar-only --profile balanced --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --force-rebuild
```

### External MyceliaDB Gateway

Uses a running MyceliaDB-compatible gateway.

```powershell
docforge-enterprise "D:\path\to\project.zip" --mycelia-url http://127.0.0.1:9999 --profile enterprise --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --force-rebuild
```

---

## Token Management

`MYCELIA_LOCAL_TOKEN` is a local shared secret between DocForge and the embedded MyceliaDB-compatible gateway.

Generate a token:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set a temporary token for the current PowerShell session:

```powershell
$env:MYCELIA_LOCAL_TOKEN="PASTE_GENERATED_TOKEN_HERE"
```

Generate and use a temporary token in one line:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860
```

Set a persistent token for the current Windows user:

```powershell
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"; [Environment]::SetEnvironmentVariable("MYCELIA_LOCAL_TOKEN", $token, "User")
```

Open a new PowerShell window after setting a persistent user environment variable.

Security recommendation:

```text
Use a fresh token per session for maximum local safety.
Use a persistent token only for convenience on trusted machines.
Do not commit tokens to Git or paste them into issue trackers, logs, or screenshots.
```

---

## Installation

Create and activate a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

For development:

```powershell
pip install -e ".[dev]"
```

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## CLI Usage

Dry-run without LLM calls:

```powershell
docforge-enterprise "D:\path\to\project.zip" --dry-run --profile quick --force-rebuild
```

Run with embedded MyceliaDB:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\path\to\project.zip" --embedded-mycelia --profile balanced --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --force-rebuild
```

Run with selected chapters:

```powershell
docforge-enterprise "D:\path\to\project.zip" --profile enterprise --chapters "Executive Summary,Systemüberblick,Sicherheitsbetrachtung" --embedded-mycelia --force-rebuild
```

Run with one final rendering call:

```powershell
docforge-enterprise "D:\path\to\project.zip" --profile enterprise --single-pass-final --embedded-mycelia --force-rebuild
```

Limit final chapters:

```powershell
docforge-enterprise "D:\path\to\project.zip" --profile enterprise --max-final-chapters 5 --embedded-mycelia --force-rebuild
```

---

## Timeout and Resilience Controls

Local models can be slow depending on hardware, model size, quantization, context length, and output length. DocForge provides explicit budget controls.

Recommended stable settings:

```powershell
docforge-enterprise "D:\path\to\project.zip" --profile balanced --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

Important flags:

| Flag | Purpose |
|---|---|
| `--analysis-workers` | controls concurrent shard analysis |
| `--chat-timeout` | timeout for chat completions |
| `--embedding-timeout` | timeout for embedding requests |
| `--gateway-timeout` | timeout for MyceliaDB gateway requests |
| `--max-chars-per-shard` | limits shard prompt size |
| `--max-embedding-batch-size` | limits embedding batch size |
| `--analysis-max-tokens` | limits JSON analysis output |
| `--llm-retries` | retry count for timeout-prone calls |
| `--retry-backoff` | exponential backoff base |

---

## WebGUI

DocForge includes a local WebGUI for configuring and running documentation jobs.

Start:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860 --max-upload-mb 100
```

Open:

```text
http://127.0.0.1:7860
```

The WebGUI supports:

- project upload
- local project path input
- LM Studio settings
- MyceliaDB mode selection
- Quick / Balanced / Enterprise profile selection
- custom chapter selection
- estimate-only execution
- timeout and retry controls
- worker controls
- live logs
- success and error display
- generated documentation preview
- Markdown and metadata links

---

## WebGUI Security

The WebGUI is designed for local administrative use and includes several hardening features:

- registration and login
- Mycelia-backed identity store
- password hashing via PBKDF2-HMAC-SHA256
- first registered user becomes administrator
- role foundation for `admin`, `operator`, and `viewer`
- session cookies with `HttpOnly` and `SameSite=Strict`
- CSRF protection for mutating actions
- upload size limits
- optional read-only mode
- audit log for WebGUI actions

Start in read-only mode:

```powershell
docforge-webgui --host 127.0.0.1 --port 7860 --read-only
```

Recommended production posture:

```text
Run behind a trusted reverse proxy.
Use TLS.
Restrict network access.
Apply OS-level file permissions.
Store generated artifacts in a protected workspace.
Review audit logs.
```

The WebGUI should not be exposed directly to the public internet.

---

## Audit and Evidence Validation

DocForge stores intermediate analysis data and validates generated documentation claims against extracted project evidence.

Produced audit artifacts include:

```text
analysis/audit_validation.json
output/run_metadata.json
output/enterprise_documentation.md
output/enterprise_documentation.html
```

Audit validation includes:

- claim coverage metrics
- unsupported claim marking
- evidence references
- source file references
- run metadata
- vector index metadata
- retrieval events

The generated documentation should still be reviewed by a human for final approval, especially for security, compliance, and architecture decisions.

---

## Output Structure

A typical run creates:

```text
.docforge_workspace/
  analysis/
    docforge.sqlite3
    files.json
    shards.json
    file_summaries.json
    module_summaries.json
    audit_validation.json
  mycelia_vault/
    docforge_code/
      vectors.f32
      index.jsonl
      ledger.jsonl
      manifest.json
  output/
    enterprise_documentation.md
    enterprise_documentation.html
    run_metadata.json
```

---

## Configuration

Use the example configuration:

```powershell
docforge-enterprise "D:\path\to\project.zip" --config configs/docforge.example.toml
```

Configuration areas include:

```text
[lmstudio]
[mycelia]
[pipeline]
[security]
```

Typical settings include model names, timeouts, retries, shard limits, documentation profile, chapter selection, MyceliaDB mode, and security filters.

---

## Security Model

DocForge excludes sensitive and irrelevant inputs by default:

- `.git`
- `.venv`
- `venv`
- `node_modules`
- `build`
- `dist`
- `__pycache__`
- `keys`
- `secrets`
- `.env`
- `*.pem`
- `*.key`
- `*.token`
- `*.db`
- `*.sqlite`
- large files above the configured limit
- binary files unless explicitly allowed

Detected secret-like values are redacted before analysis when redaction is enabled.

---

## Local-First Design

DocForge is built for closed enterprise environments.

```text
Source code stays local.
LLM inference runs through LM Studio.
Embeddings are stored locally.
MyceliaDB can run embedded.
Generated documentation remains in the local workspace.
```

No cloud API is required by the default local workflow.

---

## Recommended Workflow

1. Start LM Studio.
2. Load the chat model.
3. Load the embedding model.
4. Generate a local token.
5. Start the WebGUI or run the CLI.
6. Run `--estimate-only` for large projects.
7. Start with `quick` or `balanced`.
8. Move to `enterprise` for final audit-grade documentation.
9. Review evidence coverage and unsupported claims.
10. Approve and publish the generated Markdown/HTML through your internal process.

---

## Testing

Run the test suite:

```powershell
pytest
```

Run a smoke test without LLM calls:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --estimate-only --force-rebuild
```

Run a dry-run:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --dry-run --profile quick --force-rebuild
```

---

## Documentation Files

Additional project documentation is available in:

```text
docs/ARCHITECTURE.md
docs/OPERATIONS.md
docs/EMBEDDED_MYCELIADB.md
docs/TIMEOUT_RESILIENCE.md
docs/WEBGUI.md
docs/SECURITY_HARDENING.md
docs/PROFILES_AND_TRANSPARENCY.md
Info.md
```

---

## Limitations

DocForge documents what is visible in the provided project. It cannot reliably infer:

- external business rules not present in code or documentation
- runtime-only configuration from unavailable systems
- production topology that is not represented in files
- secrets or credentials that were correctly filtered
- undocumented organizational processes

LLM output should be treated as evidence-assisted analysis, not as an unquestioned source of truth. Final documentation should go through human review and approval.

---

## License

See `LICENSE`.

---

## Project Summary

DocForge Enterprise turns source code into structured, evidence-oriented documentation through local semantic indexing, staged LLM analysis, claim validation, and configurable documentation depth.

It is designed for teams that need:

```text
local-first operation
auditability
enterprise documentation quality
repository-scale analysis
source evidence
security-conscious workflows
controlled LLM usage
```
