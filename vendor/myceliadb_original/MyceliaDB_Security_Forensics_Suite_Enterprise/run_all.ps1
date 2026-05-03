param(
  [string]$Config = ".\configs\targets.example.toml",
  [switch]$Strict
)

$ErrorActionPreference = "Stop"

Write-Host "== MyceliaDB Enterprise Security Forensics Suite =="
python -m pip install -e . | Write-Host

$args = @(
  "--config", $Config,
  "--json-out", "reports\forensics_report.json",
  "--md-out", "reports\forensics_report.md",
  "--html-out", "reports\forensics_report.html"
)
if ($Strict) { $args += "--strict-exit-code" }

python -m mycelia_security_forensics @args
Write-Host ""
Write-Host "Reports:"
Write-Host "  reports\forensics_report.json"
Write-Host "  reports\forensics_report.md"
Write-Host "  reports\forensics_report.html"
