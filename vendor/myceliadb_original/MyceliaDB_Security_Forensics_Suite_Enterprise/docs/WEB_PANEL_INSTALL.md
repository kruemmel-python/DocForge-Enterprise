# Web Panel Installation

Das Panel ist read-only und zeigt nur den letzten Report.

## Standalone

```powershell
.\tools\start_security_panel.ps1
```

## In MyceliaDB SCM kopieren

Kopiere:

```text
www/security_forensics_panel.php
www/security_forensics_api.php
www/assets/security-forensics.css
```

in den `www`-Ordner deiner SCM-Webseite. Stelle sicher, dass der relative Pfad zu `reports/forensics_report.json` angepasst wird, falls du Reports außerhalb des Suite-Ordners speicherst.
