# Forensic Evidence Model

## Evidence-Klassen

1. **Operational Evidence**  
   Dienst erreichbar, Backend aktiv, Quellen geliefert.

2. **Configuration Evidence**  
   Tokenpfade, Modellnamen, Collection, Ports.

3. **Persistence Evidence**  
   JSONL-Ledger vorhanden und parsebar.

4. **Memory Evidence**  
   Externer RAM-Probe findet keine bekannten Vektorfragmente.

5. **RAG Evidence**  
   Antworten enthalten Quellen und geben keine geheimen Muster aus.

## Grenzen

Ein negativer RAM-Scan ist ein starker Hinweis, aber allein kein absoluter Beweis. Für `strict_vram_residency_proven=true` braucht es zusätzlich sealed native ABI Attestation.
