$ErrorActionPreference = "Stop"

if (-not $env:MYCELIA_LOCAL_TOKEN) {
    $env:MYCELIA_LOCAL_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
    Write-Host "Generated temporary MYCELIA_LOCAL_TOKEN for this PowerShell session."
}

docforge-webgui --host 127.0.0.1 --port 7860
