# DocForge Enterprise v5.0.1

Local-first Enterprise-Dokumentationsgenerator für ZIP-Projekte, Markdown-Code-Dumps und Quellcodeverzeichnisse.

## Neu in v5.0.1

v5.0.1 geht die zuvor identifizierten Audit- und WebGUI-Härtungspunkte an:

### Audit-Strenge

- Claim-Validation gegen Originalquellen
- Evidence-Coverage-Metriken
- automatische Markierung nicht belegter Claims in `run_metadata.json`
- Audit-Validation-Abschnitt in der finalen Dokumentation
- persistente Analyseartefakte unter `workspace/analysis/`
- Grundlage für Review-/Freigabeprozesse

### Gehärtete WebGUI

- Registrierung und Login über den Mycelia Identity Store
- Passwort-Hashing mit PBKDF2-HMAC-SHA256
- rollenbasiertes Modell:
  - erster Benutzer wird `admin`
  - weitere Benutzer werden `viewer`
- Session-Cookies mit `HttpOnly` und `SameSite=Strict`
- CSRF-Schutz für mutierende Aktionen
- Upload-Größenlimits
- Read-only-Modus
- WebGUI-Audit-Log
- lokale Bindung an `127.0.0.1` als Default

Wichtig: Die WebGUI ist jetzt als gehärtete lokale Multi-User-Webapp ausgelegt. Für öffentliche Exposition werden zusätzlich TLS, Reverse Proxy Hardening und Netzwerkzugriffskontrolle empfohlen.

## Installation

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

## Token erzeugen

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Temporär setzen:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))")
```

Dauerhaft setzen:

```powershell
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"; [Environment]::SetEnvironmentVariable("MYCELIA_LOCAL_TOKEN", $token, "User")
```

## CLI Quick Start

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

## Estimate Only

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --estimate-only --force-rebuild
```

## Secure WebGUI starten

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860 --max-upload-mb 100
```

Dann öffnen:

```text
http://127.0.0.1:7860
```

Beim ersten Registrieren wird der erste Benutzer automatisch `admin`.

## Read-only WebGUI

```powershell
docforge-webgui --host 127.0.0.1 --port 7860 --read-only
```

## Profile

- `quick`: schnelle Dokumentation, Single-Pass, wenig LLM-Arbeit
- `balanced`: Standardmodus
- `enterprise`: volle Kapitelpipeline

## Artefakte

```text
workspace/output/enterprise_documentation.md
workspace/output/enterprise_documentation.html
workspace/output/run_metadata.json
workspace/analysis/audit_validation.json
workspace/analysis/files.json
workspace/analysis/shards.json
```

Siehe `Info.md` für eine ausführliche Erklärung der LLM-Arbeit und `docs/SECURITY_HARDENING.md` für die WebGUI-Härtung.
