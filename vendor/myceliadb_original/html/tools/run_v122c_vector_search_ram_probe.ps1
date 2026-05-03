param(
  [Parameter(Mandatory=$true)][int]$MyceliaPid,
  [string]$AdapterRoot = "C:\web_sicherheit\SMQL-Embedding-Adapter",
  [string]$MyceliaRoot = "C:\web_sicherheit\html",
  [string]$Collection = "demo",
  [string]$Query = "Was ist SMQL?",
  [string]$LMStudioUrl = "http://127.0.0.1:1234",
  [string]$EmbeddingModel = "text-embedding-nomic-embed-text-v2-moe",
  [string]$TokenFile = "C:\web_sicherheit\html\keys\local_transport.token",
  [string]$JsonOut = "v122c_vector_ram_probe.json",
  [int]$DurationSeconds = 8,
  [int]$IntervalMs = 250,
  [string]$VectorId = ""
)

$ErrorActionPreference = "Stop"

$Probe = Join-Path $MyceliaRoot "tools\mycelia_memory_probe.py"
$Vault = Join-Path $AdapterRoot ".smql_adapter"

# Keep the query command simple and let the probe hash stdout/stderr instead of printing them.
$EscapedQuery = $Query.Replace('"', '\"')
$QueryCommand = @"
cd /d "$AdapterRoot" && python -m smql_embedding_adapter.cli --mycelia-url http://127.0.0.1:9999 --mycelia-token-file "$TokenFile" --lmstudio-url "$LMStudioUrl" --embedding-model "$EmbeddingModel" --collection "$Collection" --search-backend mycelia --sealed-mode auto smql "FIND ASSOCIATED WITH TEXT '$EscapedQuery' LIMIT 3"
"@

$argsList = @(
  $Probe,
  "--pid", "$MyceliaPid",
  "--during-smql-vector-search",
  "--query-text", $Query,
  "--lmstudio-url", $LMStudioUrl,
  "--embedding-model", $EmbeddingModel,
  "--adapter-vault", $Vault,
  "--collection", $Collection,
  "--during-command", $QueryCommand,
  "--scan-duration-seconds", "$DurationSeconds",
  "--scan-interval-ms", "$IntervalMs",
  "--json-out", $JsonOut
)

if ($VectorId -ne "") {
  $argsList += @("--vector-id", $VectorId)
}

python @argsList
