# Betrieb

## Komponenten

- Python 3.12+
- LM Studio Local Server
- optional MyceliaDB Gateway
- lokaler mmap-Vektorstore
- SQLite Analyse-Datenbank

## Empfohlener Ablauf

```bash
docforge-enterprise projekt.zip --dry-run --force-rebuild
docforge-enterprise projekt.zip --sidecar-only --chat-model <model> --embedding-model <embedding-model> --force-rebuild
docforge-enterprise projekt.zip --mycelia-url http://127.0.0.1:9999 --force-rebuild
```

## Monitoring

Wichtige Dateien:

```text
.docforge_workspace/output/run_metadata.json
.docforge_workspace/analysis/docforge.sqlite3
.docforge_workspace/mycelia_vault/docforge_code/ledger.jsonl
```

## Security

Tokens sollten über Umgebungsvariablen gesetzt werden:

```bash
export MYCELIA_LOCAL_TOKEN=...
```

Nicht empfohlen:

```bash
--mycelia-token secret
```

weil Shell-History und Prozesslisten Secrets enthalten können.


## Embedded MyceliaDB operations

For a completely self-contained run:

```bash
docforge-enterprise project.zip --embedded-mycelia --force-rebuild
```

For a persistent local gateway:

```bash
embedded-myceliadb --host 127.0.0.1 --port 9999 --root .docforge_workspace/embedded_myceliadb
```

Use `MYCELIA_LOCAL_TOKEN` or `--mycelia-token` when the gateway should reject
unauthenticated local requests.


## Parallelisierung

Shard-Analysen laufen standardmäßig sequenziell:

```bash
docforge-enterprise project.zip --analysis-workers 1
```

Für große Projekte kann eine kleine Worker-Queue aktiviert werden:

```bash
docforge-enterprise project.zip --analysis-workers 3
```

Empfehlung:

- CPU-only Modell: `1`
- einzelne Consumer-GPU: `2` bis `3`
- schneller lokaler Server mit Queueing: `4` bis `8`

Zu viele Worker verschlechtern die Laufzeit, weil LM Studio und das geladene Modell der eigentliche Engpass bleiben.

## JSON-Recovery

Bei kleinen oder instabilen Modellen kann `json_repair_attempts` in der TOML-Konfiguration erhöht werden:

```toml
[lmstudio]
json_repair_attempts = 2
```

Der Default ist bewusst niedrig, damit fehlerhafte Modellantworten nicht endlos neue Modellaufrufe erzeugen.
