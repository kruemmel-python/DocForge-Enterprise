# MyceliaDB v1.22c Vector RAM Probe

This patch upgrades `tools/mycelia_memory_probe.py` from a cleartext scanner into a
live SMQL vector-search residency probe.

## What it validates

During a real SMQL vector search it scans the MyceliaDB process for high-entropy
fragments of 768D embeddings in host CPU RAM.

The probe can look for:

- little-endian `float32` fragments
- little-endian `float64` fragments
- base64 fragments of the adapter's `float32-le-base64` transport
- optional ASCII decimal windows

The report never prints vectors or query text. It emits hashes, fragment labels,
hit counts and an evidence digest.

## What it does not prove by itself

A negative external scan is not an absolute hardware proof. It is a necessary
forensic evidence layer. `strict_vram_residency_proven=true` should only be set
when all of these are true:

1. MyceliaDB uses the sealed native ABI.
2. The sealed ABI attests no host vector copy / zeroized staging / kernel identity.
3. This external vector probe reports `strict_no_cpu_ram_external_probe_passed=true`.
4. Positive canary visibility is validated in the same scanning environment.

Without sealed ABI attestation the correct status remains fail-closed:
`strict_vram_residency_proven=false`.

## Manual workflow

Find the MyceliaDB PID:

```powershell
Get-Process python | Select-Object Id,ProcessName,Path,CommandLine
```

Run the probe while it triggers an adapter query:

```powershell
cd C:\web_sicherheit\html

python tools\mycelia_memory_probe.py `
  --pid <MYCELIA_PID> `
  --during-smql-vector-search `
  --query-text "Was ist SMQL?" `
  --lmstudio-url http://127.0.0.1:1234 `
  --embedding-model text-embedding-nomic-embed-text-v2-moe `
  --adapter-vault C:\web_sicherheit\SMQL-Embedding-Adapter\.smql_adapter `
  --collection demo `
  --during-command "cd /d C:\web_sicherheit\SMQL-Embedding-Adapter && python -m smql_embedding_adapter.cli --mycelia-url http://127.0.0.1:9999 --mycelia-token-file C:\web_sicherheit\html\keys\local_transport.token --lmstudio-url http://127.0.0.1:1234 --embedding-model text-embedding-nomic-embed-text-v2-moe --collection demo --search-backend mycelia smql ""FIND ASSOCIATED WITH TEXT 'Was ist SMQL?' LIMIT 3""" `
  --scan-duration-seconds 8 `
  --json-out v122c_vector_ram_probe.json
```

Or use the PowerShell wrapper:

```powershell
cd C:\web_sicherheit\html
.\tools\run_v122c_vector_search_ram_probe.ps1 `
  -MyceliaPid <MYCELIA_PID> `
  -AdapterRoot C:\web_sicherheit\SMQL-Embedding-Adapter `
  -Collection demo `
  -Query "Was ist SMQL?"
```

## Expected pass evidence

A negative external vector search probe contains:

```json
{
  "status": "ok",
  "vector_search_probe": {
    "enabled": true,
    "verdict": "pass:external-probe-negative",
    "vector_fragment_probe_count": 1,
    "vector_fragment_hits": 0,
    "vector_strict_hits": 0,
    "vector_negative": true,
    "strict_no_cpu_ram_external_probe_passed": true
  }
}
```

If fragments are found:

```json
{
  "vector_search_probe": {
    "verdict": "fail:vector-fragments-observed",
    "vector_strict_hits": 1
  }
}
```

## Probe sources

Use at least one source:

- `--query-text` embeds a query through LM Studio and scans for that query vector.
- `--adapter-vault .smql_adapter --collection demo` loads active stored vectors.
- `--vector-json vector.json` loads explicit test vectors.
- `--vector-f32-file vector.f32` loads raw float32 vectors.

For strict experiments, pair query-vector probes with stored-vector probes:

```powershell
--query-text "Was ist SMQL?" `
--adapter-vault C:\web_sicherheit\SMQL-Embedding-Adapter\.smql_adapter `
--collection demo
```

## Report submission

The resulting JSON can be kept as external evidence or submitted through your
existing `submit_external_memory_probe` workflow if enabled by your MyceliaDB
build. The report is intentionally hash-only and does not include raw vector data.
