# Threat Model

## Schutzgüter

- Local Transport Token
- MyceliaDB Direct-Ingest / Zero-Logic Gateway
- Vektorindex und Embeddings
- LM Studio Modelle und Antworten
- RAG-Kontext und Quellen
- Web-Sessions der SCM-Oberfläche
- Persistenzdateien und Logs

## Angriffsflächen

1. HTTP-Gateway `127.0.0.1:9999`
2. SMQL-Embedding-Adapter `127.0.0.1:8765`
3. LM Studio OpenAI-kompatible API `127.0.0.1:1234`
4. PHP-Webseite und Chat-API
5. lokale Log-/Report-/Config-Dateien
6. Prozessspeicher von MyceliaDB und Adapter
7. RAG-Prompt-Injection über Nutzerfragen oder Dokumente

## Sicherheitsannahmen

- Dienste laufen lokal oder in einem kontrollierten Firmennetz.
- Der Betreiber kontrolliert die Maschine.
- Token-Dateien sind lokal geschützt.
- Die Suite testet defensiv und beobachtend.

## Nicht-Ziele

- Keine Exploit-Entwicklung gegen fremde Systeme.
- Keine Umgehung von Zugriffskontrollen.
- Keine Garantie, dass ein negativer RAM-Scan ein mathematischer Beweis ist.
