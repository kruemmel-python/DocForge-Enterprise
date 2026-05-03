param(
  [int]$MyceliaPid,
  [string]$MyceliaRoot = "C:\web_sicherheit\html",
  [string]$AdapterRoot = "C:\web_sicherheit\SMQL-Embedding-Adapter",
  [string]$TokenFile = "C:\web_sicherheit\html\keys\local_transport.token",
  [string]$MyceliaUrl = "http://127.0.0.1:9999",
  [switch]$Yes
)

$ErrorActionPreference = "Stop"

if (-not $Yes) {
  Write-Host "This will stop the MyceliaDB process PID=$MyceliaPid and restart mycelia_platform.py."
  $answer = Read-Host "Type YES to continue"
  if ($answer -ne "YES") { throw "Aborted by operator." }
}

Write-Host "Stopping MyceliaDB PID $MyceliaPid ..."
Stop-Process -Id $MyceliaPid -Force
Start-Sleep -Seconds 2

Write-Host "Starting MyceliaDB ..."
$proc = Start-Process -FilePath "python" -ArgumentList "mycelia_platform.py" -WorkingDirectory $MyceliaRoot -PassThru
Start-Sleep -Seconds 8

Write-Host "Checking rehydration status ..."
Push-Location $AdapterRoot
python -m smql_embedding_adapter.cli --mycelia-url $MyceliaUrl --mycelia-token-file $TokenFile mycelia-status
Pop-Location

Write-Host "New MyceliaDB PID: $($proc.Id)"
