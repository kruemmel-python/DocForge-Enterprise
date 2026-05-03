# DocForge Enterprise v5.0.1 — LLM-Arbeit und Audit-Härtung

DocForge Enterprise baut aus Quellcode ein auditierbares Wissensmodell:

```text
Dateien -> Shards -> Embeddings -> Shard-Analyse -> File-Reduce -> Module-Reduce -> finale Dokumentation
```

Ab v5.0.1 wird zusätzlich geprüft, wie stark die erzeugten Aussagen durch Evidence gedeckt sind.

## Profile

| Profil | Zweck |
|---|---|
| quick | schnelle Analyse für kleine Projekte |
| balanced | sinnvoller Standard |
| enterprise | maximale Tiefe und kapitelweise Dokumentation |

## Audit-Validation

DocForge sammelt Claims aus Analyse-Records und prüft, ob sie Evidence auf existierende Originaldateien besitzen.

Ergebnis:

```text
claims_total
claims_supported
claims_unsupported
evidence_coverage_percent
unsupported_claims
```

Die Werte erscheinen in:

```text
run_metadata.json
analysis/audit_validation.json
Enterprise-Dokumentation Abschnitt "Audit-Validation"
```

## WebGUI-Härtung

v5.0.1 ergänzt:

```text
Registrierung/Login via Mycelia Identity Store
PBKDF2-Passworthashes
Session-Cookies
CSRF-Schutz
Upload-Limits
Read-only-Modus
Rollenmodell
Audit-Log für WebGUI-Aktionen
```

Der erste registrierte Benutzer wird automatisch `admin`.

## Empfehlung

Für kleine Projekte:

```powershell
docforge-enterprise sample_project.zip --profile quick --embedded-mycelia
```

Für echte Enterprise-Dokumentation:

```powershell
docforge-enterprise project.zip --profile enterprise --embedded-mycelia
```
