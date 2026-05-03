param(
  [string]$SuiteRoot = (Get-Location).Path,
  [int]$Port = 8090
)
$ErrorActionPreference = "Stop"
Write-Host "Starting PHP panel on http://127.0.0.1:$Port/security_forensics_panel.php"
php -S 127.0.0.1:$Port -t "$SuiteRoot\www"
