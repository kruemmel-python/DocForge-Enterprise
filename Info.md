# DocForge Enterprise — System Intelligence, Audit & Secure WebGUI Model

<img width="853" height="480" alt="DocForge_Enterprise_Blueprint" src="https://github.com/user-attachments/assets/377e4a0d-1e4c-4807-8584-8825a57230f3" />

DocForge Enterprise is a local-first framework for generating professional, evidence-oriented software documentation from source code projects, ZIP archives, Markdown code dumps and local repositories.

Unlike simple documentation generators, DocForge does not send an entire project into one large prompt. It builds a structured analysis model through language-aware sharding, embeddings, semantic retrieval, staged reduction, profile-controlled rendering, evidence validation and secure local operation.

This document explains what happens internally, why multiple LLM and embedding calls may be required, how the documentation profiles work, how the multilingual WebGUI behaves, how audit integrity is enforced, and how to choose the correct execution mode.

---

## 1. Core Principle

DocForge Enterprise follows a staged analysis pipeline:

```text
Source Project
  -> Secure extraction and filtering
  -> Language-aware sharding
  -> Embedding and semantic indexing
  -> Shard-level LLM analysis
  -> Pipeline integrity validation
  -> File-level reduction
  -> Module-level reduction
  -> Evidence and claim validation
  -> Profile-controlled final rendering
  -> Markdown, HTML, JSON, SQLite and audit artifacts
```

The goal is not only to produce readable documentation. The goal is to create a traceable knowledge model from source code, intermediate summaries, evidence records, run metadata and validation artifacts.

This design is especially useful for local LLM environments, where context windows, latency and hardware capacity are limited.

---

## 2. Why Multiple LLM Calls Are Used

A small project may contain only a few files:

```text
README.md
src/app.py
src/auth.py
```

Even then, DocForge may perform more than three LLM calls because the system works on analysis layers rather than raw files.

A typical enterprise analysis may include:

```text
1. Read and filter project files
2. Split files into semantic shards
3. Embed every shard
4. Analyze each shard
5. Validate that shard analyses exist for every file
6. Summarize each file
7. Summarize each module
8. Retrieve relevant context for final chapters
9. Render the final documentation
10. Validate evidence coverage
11. Write audit metadata
```

For a small sample project this can feel excessive. For large repositories it prevents context overflow, missing files and shallow documentation.

---

## 3. Semantic Sharding

A shard is a meaningful piece of source code or documentation.

DocForge uses language-aware sharding where possible.

Examples:

```text
Python:
  -> imports
  -> functions
  -> classes
  -> methods

Java / C# / C / C++ / Go / Rust / PHP:
  -> declarations
  -> functions
  -> classes
  -> structural blocks

JavaScript / TypeScript:
  -> functions
  -> classes
  -> interfaces
  -> type declarations
  -> arrow functions

SQL:
  -> statements
  -> schema objects

Markdown:
  -> heading sections
```

For example, a simple Python file may become:

```text
src/app.py
  ├── import shard
  └── function shard: main(user: str) -> str
```

This allows DocForge to analyze code at the level where meaning actually exists.

---

## 4. Embeddings and Retrieval

Each shard can be embedded and stored in a vector index.

Example log line:

```text
Received request to embed multiple:
"src/app.py python from .auth import issue_token"
```

This means:

```text
The shard is being converted into a vector representation.
```

The vector index allows DocForge to find semantically related code later.

Example:

```text
src/app.py imports issue_token
src/auth.py defines issue_token
```

When analyzing `src/app.py`, retrieval can surface `src/auth.py` as related context. This helps the LLM reason across files instead of treating each snippet in isolation.

This is important for:

```text
cross-file dependencies
security analysis
business rule discovery
interface mapping
architecture inference
evidence linking
```

---

## 5. Shard-Level LLM Analysis

The first analytical LLM stage processes individual shards.

A shard analysis extracts structured information such as:

```json
{
  "file_path": "src/auth.py",
  "purpose": "Defines token generation logic.",
  "important_symbols": ["issue_token"],
  "dependencies": [],
  "business_rules": ["A user value is required to issue a token."],
  "interfaces": ["issue_token(user: str) -> str"],
  "security_notes": ["The token format is a placeholder."],
  "risks": ["Not suitable for production authentication."],
  "evidence": [
    {
      "file_path": "src/auth.py",
      "span": "0-128",
      "claim": "issue_token generates a token for a non-empty user."
    }
  ]
}
```

At this stage, the LLM is not writing the final documentation. It is extracting structured facts, risks, interfaces, rules and evidence.

---

## 6. Pipeline Integrity Between Stages

DocForge performs integrity checks between analysis stages so later prompts do not silently run on missing intermediate data.

A critical integrity rule is:

```text
File-level reduction must not run with empty shard analyses.
```

The system checks:

```text
file_path
expected shard IDs
stored shard analysis records
number of analyses found per file
```

A healthy run should show diagnostics similar to:

```text
[DocForge][Integrity] stage=post-shard file=README.md expected_shards=1 shard_analyses_found=1 status=ok
[DocForge][Integrity] stage=post-shard file=src/app.py expected_shards=2 shard_analyses_found=2 status=ok
[DocForge][Integrity] stage=post-shard file=src/auth.py expected_shards=1 shard_analyses_found=1 status=ok
```

This prevents weak prompts such as:

```text
Keine Shard-Analysen verfügbar.
```

or:

```text
No shard analyses available.
```

from becoming the basis for final documentation.

Integrity artifacts can include:

```text
analysis/pipeline_integrity_report.json
run_metadata.json -> pipeline_integrity_report
```

By default, missing shard analyses are treated as an integrity failure. A fallback mode can be enabled for debugging, but it is not recommended for audit-quality output.

---

## 7. File-Level Reduction

After shard analysis and integrity validation, DocForge combines all shard analyses for a file into one file-level summary.

Example:

```text
src/app.py import shard
src/app.py main function shard
        ↓
file summary for src/app.py
```

The file summary typically contains:

```text
file purpose
public API
internal logic
dependencies
business rules
interfaces
security notes
operations notes
risks
evidence
```

This makes later stages more efficient because they work on structured summaries instead of raw code.

---

## 8. Module-Level Reduction

Files are grouped into modules.

Example:

```text
root -> README.md
src  -> src/app.py + src/auth.py
```

Module summaries describe responsibilities at a higher architectural level:

```text
module responsibility
main flows
dependencies
interfaces
security concerns
operations notes
risks
evidence
```

This is especially useful for larger repositories with many directories and subsystems.

---

## 9. Final Documentation Rendering

The final documentation can be rendered in two main ways.

### Single-Pass Rendering

The documentation is generated with one final LLM call.

Advantages:

```text
fewer LLM calls
faster for small projects
simpler execution model
```

Trade-offs:

```text
larger prompt
less fine-grained chapter control
potential timeout risk on large projects
```

### Chapter-Based Rendering

Each documentation chapter is rendered separately.

Example chapters:

```text
Executive Summary
System Overview
Architecture
Module Overview
Data Flows
External Dependencies
APIs and Interfaces
Configuration Model
Security Analysis
Deployment
Risks and Technical Debt
Extension Points
Glossary
Appendix
```

For each chapter, DocForge may perform:

```text
1. Retrieval query
2. Context selection
3. LLM chapter rendering
```

This produces more LLM calls but improves depth, structure and stability for enterprise documentation.

---

## 10. Documentation Profiles

DocForge provides three documentation profiles so the analysis depth can match the project size and objective.

### Quick

Best for:

```text
small projects
sample repositories
smoke tests
first inspection
CI sanity checks
```

Characteristics:

```text
reduced LLM work
single-pass final rendering
minimal module reduction
small chapter set
fast feedback
```

Recommended command:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --language en --profile quick --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

### Balanced

Best for:

```text
normal repositories
internal tools
backend services
review documentation
medium-sized projects
```

Characteristics:

```text
file reduction enabled
module reduction enabled
reduced final chapter set
moderate LLM cost
good documentation depth
```

Recommended command:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --language en --profile balanced --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

### Enterprise

Best for:

```text
large repositories
legacy systems
architecture reviews
security reviews
operational handover
technical due diligence
audit-oriented documentation
```

Characteristics:

```text
full staged pipeline
file and module reduction
chapter-based final rendering
retrieval per chapter
evidence-oriented output
maximum documentation depth
```

Recommended command:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --language en --profile enterprise --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

---

## 11. Multilingual Output

DocForge can control the language of the generated documentation and LLM analysis output.

Supported language options:

```text
de -> German
en -> English
```

CLI examples:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --language de --profile quick --embedded-mycelia --force-rebuild
```

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --language en --profile quick --embedded-mycelia --force-rebuild
```

When the language is changed:

```text
WebGUI labels switch to the selected language.
LLM prompts request output in the selected language.
Shard analysis follows the selected language.
File summaries follow the selected language.
Module summaries follow the selected language.
Final documentation follows the selected language.
run_metadata.json records the selected output language.
```

Code identifiers remain unchanged:

```text
file paths
class names
function names
method names
API symbols
configuration keys
environment variables
```

Only natural-language explanations, notes, risks and generated documentation text are translated.

---

## 12. Estimate-Only Mode

Before investing local compute time, DocForge can estimate the amount of work.

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --estimate-only --force-rebuild
```

The estimate can include:

```text
number of files
number of shards
number of modules
selected profile
selected chapters
estimated shard analysis calls
estimated file reduction calls
estimated module reduction calls
estimated chapter rendering calls
estimated LLM chat calls
estimated embedding calls
```

Use this mode before running large repositories.

---

## 13. Custom Chapter Selection

You can restrict final documentation to selected chapters.

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --language en --profile balanced --chapters "Executive Summary,System Overview,Security Analysis" --embedded-mycelia --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --force-rebuild
```

German example:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --language de --profile balanced --chapters "Executive Summary,Systemüberblick,Sicherheitsbetrachtung" --embedded-mycelia --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --force-rebuild
```

This reduces:

```text
LLM calls
embedding queries
runtime
timeout risk
```

---

## 14. Secure WebGUI Execution Model

The WebGUI exposes the same key decisions as the CLI.

Supported controls include:

```text
language selection
execution mode
documentation profile
custom chapter list
single-pass final rendering
module reduction toggle
estimate-only mode
model names
timeout budgets
worker limits
upload limit
read-only mode
authentication
roles
CSRF protection
audit logging
```

Start the WebGUI:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860 --root ".docforge_webgui" --max-upload-mb 100
```

Open:

```text
http://127.0.0.1:7860
```

The WebGUI is useful for local and team-oriented documentation workflows.

For network-wide deployment, operate it behind appropriate enterprise controls:

```text
TLS termination
reverse proxy hardening
rate limiting
central access management
backup and retention policy
monitoring
network isolation
```

---

## 15. WebGUI Authentication and Session Model

The secure WebGUI provides registration, login and session handling.

Key properties:

```text
first registered user becomes admin
passwords are stored as PBKDF2-HMAC-SHA256 hashes
sessions use HttpOnly cookies
cookies use SameSite=Strict
mutating requests use CSRF protection
roles provide the foundation for admin/operator/viewer access
audit logs record WebGUI actions
```

Typical first start:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860 --root ".docforge_webgui" --max-upload-mb 100
```

Then open:

```text
http://127.0.0.1:7860
```

Expected flow:

```text
1. Session is checked.
2. If no session exists, the login/registration screen appears.
3. Register the first user.
4. The first user becomes admin.
5. Login.
6. The full documentation dashboard becomes available.
```

Session boot hardening prevents the WebGUI from polling protected endpoints before authentication is resolved. This avoids login-loop behavior and prevents unnecessary server tracebacks when browsers abort polling requests.

If the browser is stuck after an update:

```text
clear site data for 127.0.0.1
or use a fresh port
or use a private/incognito window
```

Example with a fresh port:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7864 --root ".docforge_webgui" --max-upload-mb 100
```

---

## 16. Execution Modes

DocForge supports multiple execution modes.

### Dry-Run

No LLM is used.

Useful for:

```text
installation checks
pipeline testing
WebGUI testing
output path verification
filter and extraction validation
```

Command:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --dry-run --profile quick --force-rebuild
```

### LM Studio with Sidecar Vectorstore

Uses LM Studio and the local mmap sidecar vectorstore.

Useful when:

```text
no MyceliaDB gateway should be started
a simple local vectorstore is sufficient
you want fewer moving parts
```

Command:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --sidecar-only --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --profile balanced --force-rebuild
```

### LM Studio with Embedded MyceliaDB

Starts the embedded MyceliaDB-compatible gateway automatically.

Useful when:

```text
you want the full local semantic index path
you do not want to install MyceliaDB separately
you want a self-contained enterprise run
```

Command:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --embedded-mycelia --profile balanced --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --force-rebuild
```

### External MyceliaDB Gateway

Uses an already running MyceliaDB-compatible gateway.

Useful when:

```text
a persistent shared semantic index is required
multiple runs should reuse infrastructure
a dedicated service process is preferred
```

---

## 17. Token Handling

`MYCELIA_LOCAL_TOKEN` is a local shared secret between DocForge and the embedded or external MyceliaDB-compatible gateway.

Generate a token:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set it for the current PowerShell session:

```powershell
$env:MYCELIA_LOCAL_TOKEN="YOUR_TOKEN_HERE"
```

Generate and set it automatically for one session:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

Set it permanently for the Windows user:

```powershell
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"; [Environment]::SetEnvironmentVariable("MYCELIA_LOCAL_TOKEN", $token, "User")
```

After setting it permanently, open a new terminal.

Security recommendation:

```text
For maximum local security, generate a fresh token per session.
For convenience, set it permanently at user level.
Never commit tokens to Git or include them in screenshots, logs or issue reports.
```

---

## 18. Audit and Evidence Model

DocForge stores several artifacts to support traceability:

```text
file hashes
shard hashes
analysis records
retrieval events
SQLite state
vectorstore metadata
ledger information
run metadata
pipeline integrity report
audit validation output
```

The documentation can include evidence references such as:

```text
file path
source span
claim
related shard
retrieved context
```

This helps reviewers understand where a statement came from.

Important distinction:

```text
Evidence improves traceability.
Evidence does not automatically make every LLM statement true.
```

For strict audits, reviewers should inspect generated claims, evidence coverage and unmatched or weakly supported statements.

---

## 19. Claim Validation and Evidence Coverage

DocForge can validate generated claims against original sources and intermediate artifacts.

Important outputs may include:

```text
analysis/audit_validation.json
analysis/pipeline_integrity_report.json
run_metadata.json
enterprise_documentation.md
enterprise_documentation.html
```

Useful metrics:

```text
number of claims
number of evidence-backed claims
number of unsupported claims
evidence coverage ratio
files with low evidence coverage
modules with weak evidence support
```

Unsupported or weakly supported claims should be treated as review items.

---

## 20. Security Model

DocForge is local-first by design.

Advantages:

```text
source code does not need to leave the machine
LM Studio can run locally
embedded MyceliaDB can run locally
sidecar vectorstore can run locally
workspace artifacts remain under local control
```

Default protections can include:

```text
Zip-Slip protection
secret and token redaction
binary file filtering
vendor directory filtering
large file limits
local gateway token
local WebGUI binding
upload size limits
CSRF protection
authentication
role foundation
WebGUI audit log
optional read-only mode
defensive session handling
browser aborted connection handling
```

Local-first significantly reduces IP exposure compared with cloud-based documentation workflows. It is not an absolute security guarantee. Local risks still exist.

Examples:

```text
local logs
workspace permissions
shell history
malicious uploads
prompt injection in source files
HTML output from generated text
local user permissions
misconfigured WebGUI binding
```

For broader enterprise deployment, use additional infrastructure controls.

---

## 21. Timeout and Local Hardware Considerations

Local models may be slow or inconsistent depending on:

```text
CPU/GPU
VRAM/RAM
model size
quantization
context length
prompt size
output length
parallel workers
embedding batch size
```

Recommended stable defaults:

```powershell
--analysis-workers 1
--chat-timeout 600
--embedding-timeout 300
--gateway-timeout 180
--max-chars-per-shard 2500
--max-embedding-batch-size 4
--analysis-max-tokens 900
--llm-retries 3
```

If timeouts occur:

```text
use profile quick or balanced
reduce max shard size
reduce embedding batch size
reduce final chapters
use single-pass only for small projects
keep workers at 1
increase chat timeout
```

---

## 22. Choosing the Right Profile

Use this decision model:

```text
I only want to test installation:
  dry-run + quick

I want a fast first documentation:
  quick + embedded MyceliaDB or sidecar

I want practical project documentation:
  balanced + LM Studio + embedded MyceliaDB

I want full audit-oriented documentation:
  enterprise + LM Studio + embedded MyceliaDB

I get timeouts:
  quick or balanced
  workers = 1
  smaller shards
  fewer chapters

I need exact control:
  custom chapters
  estimate-only first
```

---

## 23. Recommended First Runs

German output:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --language de --profile quick --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

English output:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --language en --profile quick --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

For larger projects:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\Pfad\zum\projekt.zip" --language en --profile balanced --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

For deep enterprise analysis:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\Pfad\zum\projekt.zip" --language en --profile enterprise --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

---

## 24. Operational Limits

DocForge documents what it can observe.

It cannot reliably reconstruct:

```text
business intent not present in code
external system behavior
production data semantics
undocumented operational processes
missing infrastructure configuration
human-only domain knowledge
```

LLM output should be treated as an assisted analysis, not as automatically verified truth.

For high-assurance use cases:

```text
review generated claims
check evidence coverage
inspect unsupported statements
validate security findings manually
approve final documentation through a human workflow
```

---

## 25. Summary

DocForge Enterprise turns source code into structured, evidence-oriented documentation through a local, multi-stage analysis pipeline.

It provides:

```text
language-aware sharding
embedding-based retrieval
local LLM analysis
pipeline integrity checks
file and module reduction
profile-controlled rendering
multilingual output
secure WebGUI and CLI operation
authentication and CSRF protection
audit artifacts
local-first security posture
claim and evidence validation foundations
```

The three profiles control analysis depth:

```text
quick       -> fast first result
balanced    -> practical documentation default
enterprise  -> deep audit-oriented documentation
```

The system is strongest when used as a controlled documentation and review framework, especially in environments where source code must remain local and traceable documentation is required.
