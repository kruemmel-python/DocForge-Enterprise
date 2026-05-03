# Enterprise Playbook

## Täglicher Schnellcheck

```powershell
.\run_all.ps1
```

Akzeptanz:

- Adapter health: pass
- LM Studio: pass/warn nur bei Modellnamensabweichung
- Gateway Token Boundary: pass
- MyceliaDB Vector Index: pass
- RAG Baseline: pass
- Secret Scan: pass

## Nach jedem MyceliaDB-Patch

1. MyceliaDB neu starten.
2. `mycelia-status` prüfen.
3. `.\run_all.ps1` ausführen.
4. Web-Chat manuell mit „Was ist MyceliaDB?“ testen.
5. Optional Live-RAM-Probe starten.

## Bei `cpu-vector-fallback`

- MyceliaDB-Status prüfen.
- `vector_index.total_vectors` prüfen.
- v1.22d Persistenzdatei prüfen.
- Falls leer: einmalig neu ingestieren und Rehydration-Test durchführen.

## Bei fehlenden Quellen

- `/v1/rag_chat` direkt testen.
- Collection-Name prüfen.
- README/RAG-Dokumentation ingestieren.
- `sources` im JSON prüfen.

## Bei JSON-/HTML-Fehler in Webseite

- `lmstudio_chat_api.php` direkt öffnen.
- Es muss JSON liefern, nie HTML.
- Zero-Logic-Gateway darf nicht per Klartext-POST von PHP angesprochen werden.
