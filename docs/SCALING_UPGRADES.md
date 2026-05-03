# Scaling Upgrades

## Multi-Language Sharding

DocForge now uses language-aware shard boundaries beyond Python:

- Python: AST classes/functions
- Java, C#, C/C++, Go, Rust, PHP: declaration detection plus brace matching
- JavaScript/TypeScript/React: class/function/interface/type/const-arrow shards
- SQL: statement-level shards for schema and query objects
- Markdown: heading section shards

The generic character splitter is only used when no safer structural boundary is detected.

## JSON Fault Tolerance

The LM Studio client now applies a staged JSON recovery pipeline:

1. strict JSON parsing
2. markdown fence stripping
3. balanced object/array extraction
4. trailing comma repair
5. unquoted-key repair
6. Python-literal fallback for Python-ish dictionaries
7. optional second LM Studio repair call

The run metadata exposes `stats.json_repairs`.

## Worker Queue

Shard analysis can run with bounded parallelism:

```bash
docforge-enterprise project.zip --analysis-workers 3
```

The SQLite analysis store is thread-safe for this workload. Keep worker counts modest because local LM Studio inference remains the bottleneck.
