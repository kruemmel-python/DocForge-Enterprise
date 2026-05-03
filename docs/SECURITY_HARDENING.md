# Security Hardening v5.0.1

## WebGUI Auth

Die WebGUI verwendet den Mycelia Identity Store unter:

```text
.docforge_webgui/mycelia_auth/mycelia_identity.sqlite3
```

Features:

- Registrierung
- Login
- PBKDF2-HMAC-SHA256
- erster Benutzer wird admin
- Session-Token
- CSRF-Token
- rollenbasierte Job-Erstellung
- Audit-Log

## Rollen

| Rolle | Rechte |
|---|---|
| admin | Jobs starten, Token generieren, Logs sehen |
| operator | Jobs starten |
| viewer | Jobs ansehen |

## CSRF

Mutierende Endpunkte verlangen:

```text
X-CSRF-Token
```

## Upload Limits

Start:

```powershell
docforge-webgui --max-upload-mb 100
```

## Read-only

```powershell
docforge-webgui --read-only
```

## Grenzen

Für öffentliche Nutzung zusätzlich empfohlen:

- TLS
- Reverse Proxy Auth
- Rate Limiting
- getrennte User-Verzeichnisse
- Backup/Retention-Policy
- Security Review
