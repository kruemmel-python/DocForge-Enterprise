# Timeout-Resilience in DocForge Enterprise v0.3

Lokale LM-Studio-Modelle sind nicht wie Cloud-APIs dimensioniert. Ein einzelnes Modell kann bei langen Shards, großen Embedding-Batches oder mehreren parallelen Workern blockieren. v0.3 führt deshalb explizite Request-Budgets ein.

## CLI-Flags

```bash
docforge-enterprise project.zip \
  --embedded-mycelia \
  --analysis-workers 1 \
  --max-analysis-workers 2 \
  --chat-timeout 600 \
  --embedding-timeout 300 \
  --gateway-timeout 180 \
  --final-timeout 900 \
  --llm-retries 3 \
  --retry-backoff 2 \
  --max-chars-per-shard 2500 \
  --max-embedding-batch-size 4 \
  --analysis-max-tokens 900 \
  --chapter-max-tokens 2500 \
  --force-rebuild
```

## Empfohlene Profile

### Stabile lokale Default-Nutzung

```bash
--analysis-workers 1 \
--chat-timeout 300 \
--embedding-timeout 180 \
--gateway-timeout 120 \
--max-chars-per-shard 3500 \
--max-embedding-batch-size 8
```

### Schwaches CPU- oder 7B-Modell

```bash
--analysis-workers 1 \
--chat-timeout 600 \
--embedding-timeout 300 \
--max-chars-per-shard 2500 \
--max-embedding-batch-size 4 \
--analysis-max-tokens 900
```

### Stärkere lokale GPU

```bash
--analysis-workers 2 \
--max-analysis-workers 2 \
--chat-timeout 300 \
--embedding-timeout 180 \
--max-chars-per-shard 4500
```

## Was v0.3 intern tut

- Chat-, Embedding-, Gateway- und Final-Rendering-Timeouts sind getrennt.
- Timeout-Fehler werden mit Exponential Backoff erneut versucht.
- Embedding-Batches sind kleiner voreingestellt.
- Shard-Analyse kann nach Timeout mit reduziertem Prompt erneut versucht werden.
- Kapitel-Rendering nutzt begrenzte Datei-/Modulkontexte.
- SQLite-Checkpoints werden während der Analyse geschrieben.
- Der Lauf kann bei einzelnen Timeout-Fehlern mit Fallback-Records fortgesetzt werden.

## Harte Abbruchstrategie

Standardmäßig versucht DocForge weiterzulaufen. Für CI/CD oder strenge Qualitätstore:

```bash
--fail-on-timeout
```

Dann bricht ein Timeout den Lauf ab.

## Adaptive Shard-Retry deaktivieren

```bash
--no-adaptive-shard
```

Das ist sinnvoll, wenn du keine verkürzten Analyse-Prompts akzeptieren möchtest.
