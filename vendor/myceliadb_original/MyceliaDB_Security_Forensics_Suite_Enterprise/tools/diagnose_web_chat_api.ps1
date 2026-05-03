param(
  [string]$BaseUrl = "http://127.0.0.1:8080"
)
$ErrorActionPreference = "Continue"
$candidates = @(
  "$BaseUrl/lmstudio_chat_api.php",
  "$BaseUrl/www/lmstudio_chat_api.php",
  "$BaseUrl/html/www/lmstudio_chat_api.php"
)
Write-Host "MyceliaDB Web Chat API diagnosis"
foreach ($u in $candidates) {
  Write-Host "Testing: $u"
  try {
    $resp = Invoke-WebRequest -Uri $u -UseBasicParsing -Headers @{Accept="application/json"} -TimeoutSec 8
    $txt = $resp.Content
    $isJson = $false
    try { $null = $txt | ConvertFrom-Json; $isJson = $true } catch {}
    Write-Host "  Status: $($resp.StatusCode)"
    Write-Host "  Content-Type: $($resp.Headers["Content-Type"])"
    Write-Host "  JSON: $isJson"
    if ($isJson) { Write-Host "  WORKING_URL=$u" -ForegroundColor Green }
    else { Write-Host "  Preview: $($txt.Substring(0, [Math]::Min(180, $txt.Length)))" }
  } catch {
    Write-Host "  ERROR: $($_.Exception.Message)"
  }
  Write-Host ""
}
Write-Host "Use WORKING_URL as web_chat_api in configs\targets.example.toml."
