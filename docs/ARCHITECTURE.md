# Architektur: DocForge Enterprise

## Ziel

DocForge Enterprise erzeugt Enterprise-Dokumentation aus großen Codebasen, ohne das Kontextlimit lokaler LM-Studio-Modelle zu überfordern.

## Kernprinzip

```text
Nicht: Projekt -> ein Prompt -> Dokumentation
Sondern: Projekt -> semantisches Beweisnetz -> kapitelweise Dokumentation
```

## Schichten

### 1. Input Layer

- ZIP-Extraktion mit Zip-Slip-Schutz
- Markdown-Code-Dump-Unterstützung
- Quellverzeichnis-Unterstützung
- Vendor-, Secret- und Binärfilter

### 2. Shard Layer

- Python-AST-Sharding für Klassen/Funktionen
- sprachspezifisches Symbol-/Brace-Sharding für Java, C#, C/C++, Go, Rust, PHP, JavaScript/TypeScript
- strukturierte Shards für SQL-Statements und Markdown-Sections
- generisches Textsharding nur als letzter Fallback
- stabile Shard-IDs über Datei, Hash und Span
- Overlap zur Kontextstabilisierung

### 3. Semantic Layer

- vendored `smql_embedding_adapter`
- LM Studio Embeddings
- MyceliaDB Gateway, wenn verfügbar
- mmap-Sidecar-Fallback
- Merkle-Ledger für auditierbare Retrieval-Historie

### 4. Analysis Layer

- Shard-Analyse
- Datei-Reduktion
- Modul-Reduktion
- Kapitel-Generierung

### 5. Evidence Layer

- SQLite-Datenbank
- File Hashes
- Shard Hashes
- Analyse-Records
- Retrieval Events
- Mycelia/SMQL Merkle Heads

### 6. Output Layer

- Markdown
- HTML
- JSON-Metadaten
- Analyseartefakte

## Kontrollfluss

```text
prepare_input()
  -> iter_project_files()
  -> shard_project()
  -> SemanticIndex.ingest()
  -> _analyze_shards() über sequenziellen Runner oder begrenzte Worker-Queue
  -> _reduce_files()
  -> _reduce_modules()
  -> _generate_chapters()
  -> write_outputs()
```

## Warum MyceliaDB/SMQL?

Der lokale Vektorindex verhindert, dass das Modell blind einzelne Dateien sieht. Jeder Shard wird mit semantisch verwandten Nachbarn angereichert:

```text
aktueller Codeabschnitt
+ ähnliche Shards
+ relevante Konfiguration
+ verwandte Dokumentation
= besserer Analyseprompt
```

## Failure Modes

| Fehler | Verhalten |
|---|---|
| LM Studio nicht erreichbar | Dry-Run verwenden oder Analyse schlägt kontrolliert fehl |
| MyceliaDB nicht erreichbar | Sidecar-Fallback, wenn nicht `search_backend = "mycelia"` |
| Instabile JSON-Antwort | mehrstufige JSON-Recovery + optionaler LLM-Reparaturversuch + kontrollierter Fallback |
| JSON-Antwort ungültig | Fehler wird in Analyse-Record dokumentiert |
| Secret-Datei gefunden | Datei wird standardmäßig ausgeschlossen |
| Große Datei | Datei wird standardmäßig übersprungen |

## Enterprise-Härtung für Produktion

Für produktiven Einsatz empfohlen:

1. dedizierter Worker-Queue-Runner
2. LLM-Retry mit JSON-Reparaturprompt
3. feingranulare Policy für Geheimnisse
4. SBOM-Erzeugung
5. Architekturgraph als GraphML/Mermaid
6. Kapitel-Faktenprüfung gegen gespeicherte Evidenz
7. Web-UI für Review und Freigabe
8. DOCX/PDF-Renderer
