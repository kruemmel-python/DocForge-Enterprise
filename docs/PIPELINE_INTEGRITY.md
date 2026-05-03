# Pipeline Integrity Checks

DocForge Enterprise refuses to run File-Reduce with empty shard-analysis input.

## Problem prevented

A regression can lead to prompts such as:

```json
{
  "file": "src/app.py",
  "documentation": "Keine Shard-Analysen verfügbar."
}
```

This is not a valid documentation result. It means File-Reduce was invoked without the required shard analyses.

## Guard rails

The pipeline now verifies, before File-Reduce:

- how many shards were created per file
- how many shard analysis records exist per file
- whether each expected shard has a stored analysis payload
- whether File-Reduce would receive an empty analysis list

If a file has expected shards but zero shard analyses, the run fails by default.

## Outputs

The integrity report is written to:

```text
analysis/pipeline_integrity_report.json
```

It is also embedded in:

```text
output/run_metadata.json
```

## Debug log

During execution the pipeline prints:

```text
[DocForge][Integrity] stage=post-shard file=src/app.py expected_shards=2 shard_analyses_found=2 status=ok
[DocForge][Integrity] file=src/app.py expected_shards=2 shard_analyses_found=2
```

## Strict vs fallback mode

Default behavior is strict:

```text
fail_on_missing_shards = true
```

To continue with a source-code fallback instead of failing:

```powershell
docforge-enterprise project.zip --allow-missing-shard-analyses
```

This mode is not recommended for audit-grade documentation, but can be useful for debugging damaged intermediate state.
