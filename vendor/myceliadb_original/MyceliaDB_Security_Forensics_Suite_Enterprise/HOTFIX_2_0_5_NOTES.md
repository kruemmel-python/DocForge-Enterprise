# MyceliaDB Security Forensics Suite v2.0.5 Hotfix

Änderungen:

- `web_chat_api = "auto"` für portable Webroot-Erkennung.
- Auto-Erkennung testet jetzt auch Port `8081`.
- Default-Pfade sind auf `C:\MyceliaDB` umgestellt.
- Secret-Scanner ignoriert `.venv`, `site-packages`, `reports`, `state`, `snapshots`, `.smql_adapter` usw.
- Secret-Scanner unterscheidet besser zwischen Quellcode-Variablen (`args.mycelia_token`) und echten Literal-Secrets.

Nach dem Entpacken:

```powershell
cd C:\MyceliaDB\MyceliaDB_Security_Forensics_Suite_Enterprise
python -m pip install -e .
.\run_all.ps1
```
