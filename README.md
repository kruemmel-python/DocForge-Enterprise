# DocForge Enterprise

DocForge Enterprise erzeugt professionelle, belegorientierte Enterprise-Dokumentation aus:

- ZIP-Projekten
- Markdown-Code-Dumps
- lokalen Quellcode-Verzeichnissen

Das System nutzt eine mehrstufige Pipeline:

```text
Input
  -> sichere Extraktion und Secret-/Vendor-Filter
  -> semantisches Sharding
  -> SMQL Embedding Adapter
  -> MyceliaDB Gateway oder mmap Sidecar
  -> LM Studio Analyse pro Shard
  -> Datei- und Modul-Reduktion
  -> Kapitelweise Enterprise-Dokumentation
  -> Markdown, HTML, JSON, SQLite, Mycelia-Vault
```

## Warum dieses Design?

LM-Studio-Modelle können keine beliebig großen Projekte in einem Prompt verarbeiten. DocForge Enterprise sendet deshalb viele kleine Anfragen nacheinander und kombiniert sie mit lokalem Retrieval:

```text
aktueller Shard + Top-K semantisch relevante Nachbarn -> LM Studio
```

Dadurch bleiben Kontextfenster klein, aber die Dokumentation kann trotzdem projektweite Zusammenhänge erkennen.

## Enthaltene Enterprise-Komponenten

Dieses Projekt vendort den relevanten `smql_embedding_adapter` aus der bereitgestellten MyceliaDB-ZIP:

- `EmbeddingAdapter`
- `MMapVectorStore`
- `LMStudioClient`
- `MyceliaGatewayClient`
- Merkle-Ledger
- SMQL parser/retrieval plumbing
- sidecar fallback via `vectors.f32`, `index.jsonl`, `ledger.jsonl`

Nicht übernommen wurden `.venv`, Binärartefakte, Schlüssel, Tokens, Datenbanken und generierte Cache-Dateien.


## Skalierungs-Upgrade

Diese Version enthält drei Enterprise-Härtungen:

- Multi-Language-Sharding: Python nutzt weiterhin `ast`; Java, C#, C/C++, Go, Rust, PHP, JavaScript/TypeScript, SQL und Markdown erhalten sprachspezifische Symbol-/Block-Shards statt reinem Textsplit.
- Robuste JSON-Recovery: Modellantworten werden über striktes JSON, balancierte Objekt-Extraktion, Trailing-Comma-Reparatur, Key-Quoting, Python-Literal-Fallback und optionalen LLM-Reparaturversuch normalisiert.
- Begrenzte Worker-Queue: Shard-Analysen können mit `--analysis-workers N` parallelisiert werden. Für lokale Einzel-GPU-Modelle sind meist `2` bis `4` sinnvoll; `1` bleibt der deterministische Standard.

Beispiel:

```bash
docforge-enterprise project.zip \
  --embedded-mycelia \
  --analysis-workers 3 \
  --chat-model qwen2.5-coder-7b-instruct \
  --embedding-model text-embedding-nomic-embed-text-v1.5 \
  --force-rebuild
```



## v0.5: Quick / Balanced / Enterprise Profile

DocForge Enterprise unterstützt jetzt drei Dokumentationsprofile, damit kleine Projekte nicht unnötig viele LLM-Aufrufe erzeugen:

| Profil | Zweck | Finale Generierung | Typische Nutzung |
|---|---|---|---|
| `quick` | schnelle Dokumentation für Samples, kleine Projekte und Smoke-Tests | ein Single-Pass-Finalaufruf | wenige Dateien, erste Sichtung |
| `balanced` | gute technische Dokumentation mit reduzierter Kapitelanzahl | kapitelweise, aber weniger Kapitel | normale Projekte |
| `enterprise` | vollständige auditierbare Enterprise-Pipeline | jedes Kapitel separat | große Codebasen, Architektur-/Security-Doku |

Vor jedem Lauf schreibt DocForge eine Work-Estimate-Ausgabe in Log und Metadaten:

```text
estimated_shard_analysis_calls
estimated_file_reduce_calls
estimated_module_reduce_calls
estimated_chapter_render_calls
estimated_llm_chat_calls
estimated_embedding_calls
```

Nur schätzen, ohne LM Studio aufzurufen:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --estimate-only --force-rebuild
```

Quick-Profil als PowerShell-Einzeiler mit deinen Modellen:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

Balanced-Profil:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile balanced --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

Enterprise-Profil:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile enterprise --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

Gezielte Kapitel-Auswahl:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile enterprise --chapters "Executive Summary,Systemüberblick,Sicherheitsbetrachtung" --embedded-mycelia --force-rebuild
```

Finale Dokumentation mit nur einem LLM-Aufruf rendern:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile enterprise --single-pass-final --embedded-mycelia --force-rebuild
```


## Installation

```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/macOS

pip install -e .
```

Optional für Entwicklung:

```bash
pip install -e ".[dev]"
```

## LM Studio vorbereiten

1. LM Studio öffnen.
2. Ein Chat-Modell laden.
3. Ein Embedding-Modell laden.
4. Local Server aktivieren.

Typische Werte:

```text
LM Studio Base URL: http://127.0.0.1:1234/v1
Chat Model:        google_gemma-4-e4b-it
Embedding Model:   text-embedding-nomic-embed-text-v2-moe
```

## MyceliaDB

DocForge Enterprise kann mit MyceliaDB Gateway arbeiten:

```text
http://127.0.0.1:9999
```

Wenn MyceliaDB nicht läuft, kann das System im Sidecar-Modus mit mmap-Vektorspeicher arbeiten:

```bash
docforge-enterprise projekt.zip --sidecar-only
```

## Schnellstart

Dry-Run ohne LLM:

```bash
docforge-enterprise examples/sample_project.zip --dry-run --force-rebuild
```

Mit LM Studio und Sidecar-Vectorstore:

```bash
docforge-enterprise mein_projekt.zip ^
  --chat-model qwen2.5-coder-7b-instruct ^
  --embedding-model text-embedding-nomic-embed-text-v1.5 ^
  --sidecar-only ^
  --force-rebuild
```

Mit MyceliaDB:

```bash
set MYCELIA_LOCAL_TOKEN=...
docforge-enterprise mein_projekt.zip ^
  --chat-model qwen2.5-coder-7b-instruct ^
  --embedding-model text-embedding-nomic-embed-text-v1.5 ^
  --mycelia-url http://127.0.0.1:9999 ^
  --force-rebuild
```

Linux/macOS:

```bash
export MYCELIA_LOCAL_TOKEN=...
docforge-enterprise ./mein_projekt.zip \
  --chat-model qwen2.5-coder-7b-instruct \
  --embedding-model text-embedding-nomic-embed-text-v1.5 \
  --mycelia-url http://127.0.0.1:9999 \
  --force-rebuild
```



## Timeout-Resilience v0.3

Wenn LM Studio mit `TimeoutError: timed out` abbricht, nutze die neuen Budget-Flags:

```bash
docforge-enterprise project.zip \
  --embedded-mycelia \
  --analysis-workers 1 \
  --chat-timeout 600 \
  --embedding-timeout 300 \
  --gateway-timeout 180 \
  --max-chars-per-shard 2500 \
  --max-embedding-batch-size 4 \
  --analysis-max-tokens 900 \
  --llm-retries 3 \
  --force-rebuild
```

v0.3 trennt Chat-, Embedding-, Gateway- und Final-Rendering-Timeouts, nutzt Retry mit Backoff, schreibt Checkpoints und kann nach einem Shard-Timeout mit verkleinertem Prompt erneut analysieren.

Details: `docs/TIMEOUT_RESILIENCE.md`.


## PowerShell-Schnellstart mit empfohlenen Modellen

Die folgenden Beispiele verwenden diese LM-Studio-Modelle:

```text
Chat Model:      google_gemma-4-e4b-it
Embedding Model: text-embedding-nomic-embed-text-v2-moe
```

Stelle in LM Studio sicher, dass beide Modelle geladen sind und der lokale Server unter `http://127.0.0.1:1234/v1` läuft.

### Token generieren

Für den eingebetteten MyceliaDB-Gateway wird ein lokaler Transport-Token empfohlen. Er ist ein frei erzeugtes Shared Secret zwischen DocForge Enterprise und dem eingebetteten Gateway.

PowerShell-Einzeiler zum Generieren:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Der ausgegebene Wert wird anschließend als `MYCELIA_LOCAL_TOKEN` gesetzt.

### Token nur für die aktuelle PowerShell-Sitzung setzen

Dieser Token gilt nur für das aktuelle Terminalfenster. Nach dem Schließen oder Neustart der PowerShell muss ein neuer Token generiert oder erneut gesetzt werden.

```powershell
$env:MYCELIA_LOCAL_TOKEN="HIER_DEN_GENERIERTEN_TOKEN_EINFÜGEN"
```

Empfohlener vollständiger Einzeiler:

```powershell
$env:MYCELIA_LOCAL_TOKEN="HIER_DEN_GENERIERTEN_TOKEN_EINFÜGEN"; docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

Beispiel mit bereits eingesetztem Token:

```powershell
$env:MYCELIA_LOCAL_TOKEN="cXIYrutqxAnDlAXqQuSpfP5TzqtiN5pwcVHs-Gx2CI0"; docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

### Token automatisch generieren und direkt starten

Dieser Einzeiler erzeugt bei jedem Start automatisch einen neuen Token und nutzt ihn sofort für denselben Lauf:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

### Token dauerhaft für den Windows-Benutzer setzen

Soll derselbe Token auch nach einem Neustart oder in neuen PowerShell-Fenstern verfügbar sein, setze ihn dauerhaft auf Benutzerebene:

```powershell
[Environment]::SetEnvironmentVariable("MYCELIA_LOCAL_TOKEN", "HIER_DEN_GENERIERTEN_TOKEN_EINFÜGEN", "User")
```

Danach ein neues PowerShell-Fenster öffnen und prüfen:

```powershell
echo $env:MYCELIA_LOCAL_TOKEN
```

Dauerhafter Einzeiler mit Startbefehl danach:

```powershell
[Environment]::SetEnvironmentVariable("MYCELIA_LOCAL_TOKEN", "HIER_DEN_GENERIERTEN_TOKEN_EINFÜGEN", "User"); $env:MYCELIA_LOCAL_TOKEN=[Environment]::GetEnvironmentVariable("MYCELIA_LOCAL_TOKEN","User"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

Hinweis: Für maximale lokale Sicherheit ist ein neu generierter Token pro Sitzung sinnvoll. Für Komfort kann er dauerhaft gesetzt werden, sollte dann aber nicht in öffentlichen Skripten, Screenshots oder Repositories landen.


## Ausgabe

Standardmäßig entsteht:

```text
.docforge_workspace/
  analysis/
    docforge.sqlite3
    files.json
    shards.json
    file_summaries.json
    module_summaries.json
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

## Sicherheitsmodell

Standardmäßig ausgeschlossen:

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
- Binärdateien
- große Dateien über `max_file_bytes`

Außerdem werden erkannte Secret-Werte im Text redigiert.

## Konfiguration

Siehe `configs/docforge.example.toml`.

```bash
docforge-enterprise mein_projekt.zip --config configs/docforge.example.toml
```

## Architektur

Siehe `docs/ARCHITECTURE.md`.

## Tests

```bash
pytest
```

## Wichtige Einsatzgrenzen

DocForge Enterprise dokumentiert, was im Projekt sichtbar ist. Es kann Business-Regeln nicht zuverlässig aus externen Systemen, Produktwissen oder fehlenden Konfigurationen rekonstruieren. Modellantworten werden als Analysehinweise behandelt und durch gespeicherte Quellen, Hashes, Retrieval-Events und Zwischenartefakte auditierbar gemacht.

## Integrated MyceliaDB

This distribution is self-contained. It does **not** assume that MyceliaDB is
already installed on the target machine.

DocForge Enterprise ships two Mycelia-related layers:

1. `src/myceliadb_embedded/`  
   A Python 3.12 embedded MyceliaDB-compatible gateway implementing the SMQL
   Embedding Adapter commands required by DocForge:
   - `check_integrity`
   - `store_embedding`
   - `store_embedding_sealed`
   - `find_embedding`
   - `find_embedding_sealed`
   - `smql_vector_index_status`
   - `smql_sealed_abi_status`
   - `smql_forensic_attestation`

2. `vendor/myceliadb_original/`  
   A sanitized source subset from the provided MyceliaDB archive for offline
   inspection and future native integration. Secrets, runtime databases,
   private keys, tokens, virtual environments, binaries, snapshots and generated
   state files are intentionally excluded. See
   `vendor/myceliadb_original/SANITIZATION_MANIFEST.json`.

### One-command local run

```bash
python -m docforge_enterprise.cli examples/sample_project.zip \
  --embedded-mycelia \
  --dry-run \
  --force-rebuild
```

With LM Studio running:

```bash
python -m docforge_enterprise.cli my_project.zip \
  --embedded-mycelia \
  --chat-model qwen2.5-coder-7b-instruct \
  --embedding-model text-embedding-nomic-embed-text-v1.5 \
  --force-rebuild
```

The `--embedded-mycelia` flag starts the bundled gateway for the duration of the
documentation run and points the SMQL adapter to it automatically.

### Start MyceliaDB-compatible gateway separately

```bash
python -m myceliadb_embedded.cli --host 127.0.0.1 --port 9999 \
  --root .docforge_workspace/embedded_myceliadb
```

or after installation:

```bash
embedded-myceliadb --port 9999
```

Then run DocForge against it:

```bash
docforge-enterprise my_project.zip \
  --mycelia-url http://127.0.0.1:9999 \
  --chat-model <your-chat-model> \
  --embedding-model <your-embedding-model>
```

### Security posture

The embedded gateway binds to `127.0.0.1` by default. For production-like local
runs, set a transport token:

```bash
export MYCELIA_LOCAL_TOKEN="replace-with-local-secret"
python -m myceliadb_embedded.cli --port 9999
```

and use the same token for DocForge:

```bash
docforge-enterprise my_project.zip \
  --mycelia-token "$MYCELIA_LOCAL_TOKEN" \
  --mycelia-url http://127.0.0.1:9999
```

The embedded gateway provides API compatibility and append-only mmap persistence.
It does not claim native GPU/VRAM sealed residency proof; those flags are
reported explicitly as `false`.


## WebGUI

Ab Version `0.4.0` enthält DocForge Enterprise eine lokale Weboberfläche. Sie ist bewusst dependency-arm und nutzt nur die Python-Standardbibliothek plus die bereits vorhandenen Projektkomponenten.

Start:

```powershell
docforge-webgui
```

PowerShell-Einzeiler mit Token-Generierung pro Start:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860
```

Danach im Browser öffnen:

```text
http://127.0.0.1:7860
```

Die WebGUI unterstützt:

- Upload einer `.zip`- oder `.md`-Datei
- alternativ lokalen Projektpfad auf dem Rechner, auf dem die WebGUI läuft
- Auswahl des Betriebsmodus:
  - `Mit LM Studio + Embedded MyceliaDB`
  - `Mit LM Studio + Sidecar-Vectorstore`
  - `Mit externer MyceliaDB-URL`
  - `Dry-Run ohne LLM`
- alle wichtigen CLI-Einstellungen:
  - Chat-Modell
  - Embedding-Modell
  - LM-Studio-URL
  - MyceliaDB-URL
  - Local Token
  - Worker-Anzahl
  - Timeouts
  - Shard-Größe
  - Embedding-Batchgröße
  - Analyse-/Kapitel-Tokens
  - Retries und Backoff
  - Retrieval Top-K
  - Force-Rebuild
  - Adaptive-Shard-Timeout-Verhalten
- Live-Loganzeige
- Erfolgs- und Fehlermeldungen
- Dokumentationsvorschau nach Abschluss
- Links auf Markdown und Run-Metadaten

### Empfohlener WebGUI-Workflow

1. LM Studio starten.
2. Chat-Modell laden:

```text
google_gemma-4-e4b-it
```

3. Embedding-Modell laden:

```text
text-embedding-nomic-embed-text-v2-moe
```

4. Token generieren:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

5. WebGUI starten:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui
```

6. Browser öffnen:

```text
http://127.0.0.1:7860
```

### Token-Hinweis

`MYCELIA_LOCAL_TOKEN` ist ein lokales Shared Secret zwischen DocForge und dem eingebetteten MyceliaDB-kompatiblen Gateway.

Temporär für eine PowerShell-Sitzung:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

Ein temporärer Token gilt nur für diese Terminal-Sitzung beziehungsweise den daraus gestarteten WebGUI-Prozess. Nach einem Neustart oder einer neuen Shell muss er neu gesetzt werden.

Dauerhaft für den Windows-Benutzer setzen:

```powershell
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"; [Environment]::SetEnvironmentVariable("MYCELIA_LOCAL_TOKEN", $token, "User")
```

Danach neue PowerShell öffnen, damit die Variable geladen wird.

Aktuellen Token prüfen:

```powershell
$env:MYCELIA_LOCAL_TOKEN
```

### PowerShell-Einzeiler: WebGUI starten

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860
```

### PowerShell-Einzeiler: CLI-Generierung mit deinen Modellen

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

### Betriebsmodi in der WebGUI

| Modus | Bedeutung | Nutzt LM Studio | Nutzt MyceliaDB |
|---|---|---:|---:|
| Mit LM Studio + Embedded MyceliaDB | Startet das eingebettete Gateway automatisch für den Job | Ja | Ja, eingebettet |
| Mit LM Studio + Sidecar-Vectorstore | Keine Gateway-Abhängigkeit, mmap-Vektorstore im Workspace | Ja | Nein |
| Mit externer MyceliaDB-URL | Verwendet eine bereits laufende Gateway-URL | Ja | Ja, extern |
| Dry-Run ohne LLM | Erzeugt strukturelle Fallback-Dokumentation | Nein | Nein |

### Sicherheit der WebGUI

Die WebGUI ist für lokale Nutzung gedacht. Standardmäßig bindet sie nur an:

```text
127.0.0.1
```

Nicht ohne zusätzliche Absicherung auf `0.0.0.0` oder einem öffentlichen Server betreiben.



## WebGUI mit Profil-Auswahl

Start:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860
```

Dann öffnen:

```text
http://127.0.0.1:7860
```

In der WebGUI kannst du jetzt zusätzlich auswählen:

- Ausführungsmodus: Embedded MyceliaDB, Sidecar, externe MyceliaDB oder Dry-Run
- Dokumentationsprofil: Quick, Balanced oder Enterprise
- optionale Kapitel-Liste
- Single-Pass-Finale
- Modul-Reduktion überspringen
- nur Aufwand schätzen
- alle Timeouts, Worker, Tokenlimits und Modellnamen


## LLM-Arbeit verstehen

Eine ausführliche Erklärung, warum DocForge auch bei kleinen Projekten mehrere LLM- und Embedding-Aufrufe ausführt und wie die Profile `quick`, `balanced` und `enterprise` die Arbeit steuern, steht in [`Info.md`](Info.md).
