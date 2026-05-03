param(
  [int]$MyceliaPid,
  [string]$MyceliaRoot = "C:\web_sicherheit\html",
  [string]$AdapterRoot = "C:\web_sicherheit\SMQL-Embedding-Adapter",
  [string]$Collection = "demo",
  [string]$Query = "Was ist SMQL?",
  [string]$LmStudioUrl = "http://127.0.0.1:1234",
  [string]$EmbeddingModel = "text-embedding-nomic-embed-text-v2-moe",
  [int]$Seconds = 8
)

$ErrorActionPreference = "Stop"
$probe = Join-Path $MyceliaRoot "tools\mycelia_memory_probe.py"
if (-not (Test-Path $probe)) { throw "Probe tool not found: $probe" }

$out = Join-Path $MyceliaRoot "v122c_vector_ram_probe_enterprise.json"
$during = Join-Path $AdapterRoot "scripts\probe_during_smql.cmd"

if (-not (Test-Path $during)) {
  New-Item -ItemType Directory -Force (Split-Path $during) | Out-Null
  Set-Content -Path $during -Encoding ASCII -Value @"
@echo off
cd /d $AdapterRoot
.\.venv\Scripts\python.exe -m smql_embedding_adapter.cli --mycelia-url http://127.0.0.1:9999 --mycelia-token-file $MyceliaRoot\keys\local_transport.token --lmstudio-url $LmStudioUrl --embedding-model $EmbeddingModel --collection $Collection --search-backend mycelia smql "FIND ASSOCIATED WITH TEXT '$Query' LIMIT 3"
"@
}

python $probe `
  --pid $MyceliaPid `
  --during-smql-vector-search `
  --query-text $Query `
  --lmstudio-url $LmStudioUrl `
  --embedding-model $EmbeddingModel `
  --adapter-vault "$AdapterRoot\.smql_adapter" `
  --collection $Collection `
  --max-vault-vectors 50 `
  --ascii-decimal-fragments `
  --during-command $during `
  --during-command-cwd $AdapterRoot `
  --scan-duration-seconds $Seconds `
  --json-out $out

Get-Content $out | ConvertFrom-Json | ConvertTo-Json -Depth 20
