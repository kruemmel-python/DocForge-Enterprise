# DocForge Enterprise WebGUI

Die WebGUI ist eine lokale Oberfläche für DocForge Enterprise. Sie kapselt die CLI,
zeigt Logs und Status an und öffnet nach Abschluss die generierte Dokumentation.

## Start

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860
```

Browser:

```text
http://127.0.0.1:7860
```

## Modi

| Modus | Beschreibung |
|---|---|
| Mit LM Studio + Embedded MyceliaDB | Startet pro Job das eingebettete Gateway. |
| Mit LM Studio + Sidecar-Vectorstore | Nutzt Embeddings und mmap-Sidecar ohne Gateway. |
| Mit externer MyceliaDB-URL | Nutzt eine laufende MyceliaDB-kompatible Gateway-URL. |
| Dry-Run ohne LLM | Keine LM-Studio-Aufrufe, strukturelle Fallback-Dokumentation. |

## Token

Token pro Start generieren:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Direkt beim Start setzen:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui
```

Dauerhaft für den Windows-Benutzer setzen:

```powershell
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"; [Environment]::SetEnvironmentVariable("MYCELIA_LOCAL_TOKEN", $token, "User")
```

Nach dauerhaftem Setzen eine neue PowerShell öffnen.

## Modellvorgaben

Die WebGUI ist mit diesen Defaults vorbelegt:

```text
Chat Model:      google_gemma-4-e4b-it
Embedding Model: text-embedding-nomic-embed-text-v2-moe
LM Studio URL:   http://127.0.0.1:1234/v1
```

## Artefakte

Jeder WebGUI-Job bekommt einen eigenen Workspace unter:

```text
.docforge_webgui/jobs/<job-id>/workspace
```

Nach Abschluss liegen die Dateien unter:

```text
.docforge_webgui/jobs/<job-id>/workspace/output/
  enterprise_documentation.md
  enterprise_documentation.html
  run_metadata.json
```

## Sicherheit

Die WebGUI ist für lokale Nutzung gedacht. Standardmäßig bindet sie nur an
`127.0.0.1`. Nicht öffentlich exponieren, solange keine Authentifizierung,
TLS-Absicherung und Zugriffskontrolle davor geschaltet sind.


## v0.5 Profile Controls

The WebGUI exposes the new documentation depth controls:

- **Quick**: fewer LLM calls, one compact final rendering call, module LLM reduction disabled.
- **Balanced**: reduced chapter plan, normal file and module reductions.
- **Enterprise**: full chapter plan and one LLM call per chapter.

Additional controls:

- custom comma-separated chapters
- `single_pass_final`
- `disable_module_reduce`
- `estimate_only`
- max final chapters

Use **estimate-only** to preview expected LLM chat calls and embedding calls before starting a full generation.
