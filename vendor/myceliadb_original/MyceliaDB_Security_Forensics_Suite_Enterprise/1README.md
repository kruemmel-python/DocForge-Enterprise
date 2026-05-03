# MyceliaDB Enterprise Security Forensics Suite v2.0

Diese Suite prüft defensiv die Sicherheit und Betriebsfähigkeit eines lokalen MyceliaDB-Ökosystems:

- MyceliaDB Platform Server
- SMQL-Embedding-Adapter
- LM Studio
- MyceliaDB SCM-Webseite
- LM Studio / SMQL Web-Chat Plugin
- v1.22b OpenCL/VRAM Retrieval
- v1.22c RAM-Probe-Vorbereitung
- v1.22d Persistent Vector Rehydration

Die Suite ist für lokale Betreiber, Administratoren und Auditoren gedacht. Sie führt keine Angriffe auf fremde Systeme aus.

## Schnellstart

Voraussetzungen: MyceliaDB, LM Studio, Adapter und optional die Webseite laufen bereits.

```powershell
cd C:\web_sicherheit\MyceliaDB_Security_Forensics_Suite_Enterprise
python -m pip install -e .
.\run_all.ps1
```

Reports:

```text
reports/forensics_report.json
reports/forensics_report.md
reports/forensics_report.html
```

## Geprüfte Sicherheitsbereiche

| Bereich | Zweck |
|---|---|
| Connectivity | Prüft Adapter, LM Studio und MyceliaDB-Erreichbarkeit |
| Gateway Token Boundary | Prüft, ob MyceliaDB ohne Token und mit falschem Token blockt |
| Vector Index | Prüft `opencl-vram`, Collections und Vektoranzahl |
| Web JSON API | Prüft, ob `lmstudio_chat_api.php` JSON-only arbeitet |
| RAG Baseline | Prüft, ob Antworten Quellen und MyceliaDB-Backend nutzen |
| RAG Red Team | Testet Prompt-Injection- und Leak-Smoke-Cases |
| Secret Scan | Sucht Token-/Key-Leaks in lokalen Projektdateien |
| v1.22d Persistence | Prüft `smql_vector_index_v122d.jsonl` |
| RAM Probe Readiness | Prüft, ob der v1.22c RAM-Probe installiert ist |
| Live RAM Probe | Optionaler Live-Scan des MyceliaDB-Prozessspeichers |

## Konfiguration

Kopiere `configs/targets.example.toml` und passe Pfade/Ports an:

```powershell
Copy-Item configs\targets.example.toml configs\targets.local.toml
notepad configs\targets.local.toml
.\run_all.ps1 -Config configs\targets.local.toml
```

## Live RAM Probe

Die Live-RAM-Probe ist optional, weil sie eine MyceliaDB-PID benötigt.

PID finden:

```powershell
.\tools\find_mycelia_pid.ps1
```

Dann:

```powershell
python -m mycelia_security_forensics `
  --config configs\targets.local.toml `
  --mycelia-pid <PID> `
  --run-live-ram-probe
```

Oder direkt:

```powershell
.\tools\run_live_ram_probe.ps1 -MyceliaPid <PID>
```

## Restart-/Rehydration-Test

Dieser Test stoppt und startet MyceliaDB. Nur lokal und bewusst ausführen.

```powershell
.\tools\run_restart_rehydration_test.ps1 -MyceliaPid <PID> -Yes
```

Danach muss `mycelia-status` weiterhin `collections.demo > 0` und `backend=opencl-vram` zeigen.

## Web-Security-Panel

Read-only Panel starten:

```powershell
.\tools\start_security_panel.ps1
```

Browser:

```text
http://127.0.0.1:8090/security_forensics_panel.php
```

## Enterprise Gate

Die Suite berechnet ein einfaches Gate:

- `pass`: keine High/Critical Failures
- `warn`: Warnungen vorhanden
- `fail`: mindestens ein High/Critical Failure

Für CI/Automation:

```powershell
.\run_all.ps1 -Strict
```

Dann endet der Prozess mit Exit-Code 2, wenn das Enterprise Gate `fail` ist.

## v2.0.4 Hotfix Notes

- `WEB-API-001` auto-detects `/lmstudio_chat_api.php`, `/www/lmstudio_chat_api.php`, and `/html/www/lmstudio_chat_api.php`.
- A pure `404 Not Found` webroot mismatch is downgraded to `WARN` with attempted URLs.
- `DISK-SECRET-001` no longer crashes on `slots=True` dataclass serialization.
- `configs/targets.example.toml` may use `web_chat_api = "auto"`.

Diagnosis:

```powershell
.\tools\diagnose_web_chat_api.ps1 -BaseUrl http://127.0.0.1:8080
```


## v2.0.4 Hotfix

- ZIP root is `MyceliaDB_Security_Forensics_Suite_Enterprise`, so expanding with `-Force` updates the folder you actually run.
- `WEB-API-001` no longer fails the enterprise gate for pure 404 webroot discovery unless `web_chat_api_required = true` is configured.
- `DISK-SECRET-001` and report serialization are fixed for `slots=True` dataclasses.
- `run_all.ps1` prints the active module path, so stale installations are visible.
