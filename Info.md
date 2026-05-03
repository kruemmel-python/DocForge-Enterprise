# DocForge Enterprise v5.0.0 — Was macht das LLM eigentlich?

Diese Datei erklärt, warum DocForge Enterprise scheinbar viele LLM-Aufrufe ausführt, obwohl ein Beispielprojekt wie `sample_project.zip` nur wenige Dateien enthält.

Die Kurzfassung:

> DocForge Enterprise ist kein einfacher „Code rein, Dokumentation raus“-Prompt.  
> Es baut zuerst ein auditierbares Analysemodell aus Shards, Embeddings, Datei-Zusammenfassungen, Modul-Zusammenfassungen und anschließend daraus die finale Dokumentation.

Ab v5.0.0 gibt es dafür drei Profile:

```text
quick       -> wenige LLM-Aufrufe, gut für kleine Projekte und Tests
balanced    -> mittlere Analyse, guter Standardmodus
enterprise  -> vollständige Analyse, tiefe Dokumentation, viele LLM-Aufrufe
```

---

## 1. Warum wirkt die LLM-Arbeit bei kleinen Projekten so groß?

Ein Projekt mit nur drei Dateien sieht zum Beispiel so aus:

```text
README.md
src/app.py
src/auth.py
```

Trotzdem kann daraus mehr Arbeit entstehen als nur drei LLM-Aufrufe, weil DocForge nicht auf Dateiebene stoppt.

Die Pipeline zerlegt, analysiert, verdichtet und rendert in mehreren Ebenen:

```text
1. Projekt lesen
2. Dateien filtern
3. Dateien in semantische Shards zerlegen
4. Shards einbetten
5. Shards vom LLM analysieren lassen
6. Shard-Analysen pro Datei reduzieren
7. Datei-Zusammenfassungen pro Modul reduzieren
8. relevante Kontexte per Retrieval suchen
9. finale Dokumentation erstellen
10. Ergebnis als Markdown, HTML und JSON speichern
```

Das ist für große Enterprise-Codebasen wichtig. Für ein Mini-Sample wirkt es absichtlich überdimensioniert, weil das Sample die komplette Pipeline demonstriert.

---

## 2. Was ist ein Shard?

Ein Shard ist ein dokumentierbarer Ausschnitt einer Datei.

Bei Python verwendet DocForge AST-basiertes Sharding. Dadurch wird eine Datei nicht nur grob nach Zeichenlänge zerlegt, sondern nach semantischen Einheiten wie:

```text
Imports
Klassen
Funktionen
Methoden
Top-Level-Code
```

Beispiel:

```text
src/app.py
  ├── import shard: from .auth import issue_token
  └── function shard: def main(user: str) -> str
```

Bei einem Mini-Projekt entstehen daher aus drei Dateien zum Beispiel vier Shards:

```text
README.md
src/app.py import shard
src/app.py main function shard
src/auth.py issue_token shard
```

Jeder Shard kann separat analysiert werden.

---

## 3. Warum werden Embeddings erzeugt?

Diese Logausgabe:

```text
Received request to embed multiple:
"src/app.py python from .auth import issue_token"
```

bedeutet:

```text
Dieser Code-Shard wird als Vektor gespeichert.
```

Das ist wichtig für Retrieval.

Wenn später `src/app.py` analysiert wird, kann DocForge semantisch verwandte Shards finden:

```text
src/auth.py ist relevant, weil issue_token dort definiert ist.
```

Dadurch kann das LLM nicht nur den isolierten Ausschnitt analysieren, sondern auch relevante Nachbarschaft sehen.

Ohne Embeddings wäre die Analyse oft lokaler und blinder:

```text
src/app.py sieht issue_token,
weiß aber nicht zuverlässig, was issue_token wirklich tut.
```

Mit Embeddings und Retrieval kann DocForge den passenden Kontext nachladen:

```text
src/app.py ruft issue_token auf.
src/auth.py definiert issue_token.
Die Sicherheit hängt von src/auth.py ab.
```

---

## 4. Was macht die Shard-Analyse?

Jeder Shard wird vom LLM in ein strukturiertes JSON übersetzt.

Beispielhafte Felder:

```json
{
  "file_path": "src/auth.py",
  "shard_id": "abc123",
  "purpose": "Definiert die Token-Ausstellung.",
  "important_symbols": ["issue_token"],
  "dependencies": [],
  "business_rules": ["Ein Benutzername muss vorhanden sein."],
  "interfaces": ["issue_token(user: str) -> str"],
  "security_notes": ["Token ist nur ein Platzhalter."],
  "risks": ["Nicht produktionssicher."],
  "evidence": [
    {
      "file_path": "src/auth.py",
      "span": "0-128",
      "claim": "issue_token erzeugt einen Token."
    }
  ]
}
```

Das LLM schreibt hier noch nicht die finale Dokumentation.  
Es extrahiert Belege, Schnittstellen, Risiken, Regeln und technische Bedeutung.

---

## 5. Was ist File-Reduce?

Nach der Shard-Analyse werden alle Shards einer Datei zusammengeführt.

Beispiel:

```text
src/app.py import shard
src/app.py main function shard
        ↓
Datei-Zusammenfassung für src/app.py
```

Die Datei-Zusammenfassung enthält dann:

```text
Zweck der Datei
öffentliche API
interne Logik
Abhängigkeiten
Risiken
Security Notes
Operations Notes
Evidence
```

Das ist ein weiterer LLM-Aufruf pro Datei, wenn das Profil ihn aktiviert.

---

## 6. Was ist Module-Reduce?

Danach gruppiert DocForge Dateien nach Modul.

Bei einem Sample-Projekt entstehen zum Beispiel:

```text
root -> README.md
src  -> src/app.py + src/auth.py
```

Daraus werden Modul-Zusammenfassungen:

```text
root Modul-Zusammenfassung
src Modul-Zusammenfassung
```

Das hilft besonders bei größeren Projekten:

```text
src/api
src/auth
src/database
src/workers
src/frontend
```

Die Modul-Reduktion erzeugt eine Architekturperspektive oberhalb einzelner Dateien.

---

## 7. Was passiert beim finalen Rendering?

Die finale Enterprise-Dokumentation kann auf zwei Arten erzeugt werden:

```text
Single-Pass-Final
Kapitelweises Rendering
```

### Single-Pass-Final

Ein einzelner LLM-Aufruf erzeugt die gesamte Dokumentation.

Vorteil:

```text
weniger LLM-Aufrufe
schneller bei kleinen Projekten
einfacher zu verstehen
```

Risiko:

```text
größere Prompts
höheres Timeout-Risiko
weniger Kapitelkontrolle
```

### Kapitelweises Rendering

Jedes Kapitel wird separat erzeugt.

Beispiele:

```text
Executive Summary
Systemüberblick
Architektur
Modulübersicht
Datenflüsse
Externe Abhängigkeiten
APIs und Schnittstellen
Konfigurationsmodell
Sicherheitsbetrachtung
Deployment
Risiken
Erweiterungspunkte
Glossar
Anhang
```

Pro Kapitel passiert typischerweise:

```text
1. Retrieval-Query als Embedding
2. relevante Kontexte aus dem Vectorstore holen
3. Kapitel vom LLM schreiben lassen
```

Das erzeugt mehr LLM-Arbeit, aber stabilere und detailliertere Enterprise-Dokumente.

---

## 8. Warum gab es früher so viele Calls beim Sample?

Vor v5.0.0 lief das Sample faktisch im Enterprise-Stil.

Bei drei Dateien konnten ungefähr entstehen:

| Stufe | Anzahl |
|---|---:|
| Shard-Embeddings | ca. 4 |
| Shard-LLM-Analysen | ca. 4 |
| File-Reduce-LLM | ca. 3 |
| Module-Reduce-LLM | ca. 2 |
| Kapitel-Retrieval-Embeddings | ca. 10–14 |
| Kapitel-LLM-Rendering | ca. 10–14 |
| Gesamt LLM Chat Calls | ca. 19–23 |
| Gesamt Embedding Calls | ca. 14–18 |

Das war für große Projekte sinnvoll, aber für ein Mini-Projekt zu schwergewichtig.

Darum gibt es ab v5.0.0 Profile.

---

# 9. Die neuen Profile in v5.0.0

## 9.1 Quick Profile

Für kleine Projekte, Tests, Demos und schnelle Vorschauen.

Typisches Verhalten:

```text
Shard-Analyse: ja
File-Reduce: reduziert oder minimal
Module-Reduce: optional/aus
Finale Dokumentation: häufig Single-Pass
Kapitelzahl: stark begrenzt
```

Ziel:

```text
wenige LLM-Aufrufe
schnelle Rückmeldung
ausreichende Dokumentation für kleine Projekte
```

Geeignet für:

```text
Beispielprojekte
Proof of Concept
kleine Tools
erste Analyse
CI-Schnelllauf
```

PowerShell-Beispiel:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

---

## 9.2 Balanced Profile

Für normale Projekte.

Typisches Verhalten:

```text
Shard-Analyse: ja
File-Reduce: ja
Module-Reduce: ja
Finale Dokumentation: begrenzte Kapitel
Retrieval: ja
```

Ziel:

```text
gute Dokumentation
moderate LLM-Kosten
überschaubare Laufzeit
```

Geeignet für:

```text
kleine bis mittlere Repositories
interne Tools
Backend-Services
Review-Dokumentation
```

PowerShell-Beispiel:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile balanced --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

---

## 9.3 Enterprise Profile

Für vollständige, auditierbare Dokumentation.

Typisches Verhalten:

```text
Shard-Analyse: ja
File-Reduce: ja
Module-Reduce: ja
Kapitelweises Rendering: ja
Retrieval pro Kapitel: ja
Evidence: ja
Risiko-/Security-/Operations-Fokus: stark
```

Ziel:

```text
maximale Abdeckung
tiefe Architekturperspektive
auditierbare Dokumentation
```

Geeignet für:

```text
größere Enterprise-Repositories
Legacy-Systeme
Security Reviews
Architektur-Reviews
technische Due Diligence
Übergabe- und Betriebsdokumentation
```

PowerShell-Beispiel:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile enterprise --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

---

# 10. Estimate-Only: Vorher sehen, was passieren wird

Ab v5.0.0 kann DocForge vor dem Lauf abschätzen, wie viel Arbeit entsteht.

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --estimate-only --force-rebuild
```

Der Schätzlauf zeigt ungefähr:

```text
Dateien
Shards
Module
geplante Kapitel
geschätzte Chat-LLM-Calls
geschätzte Embedding-Calls
Profil
Single-Pass oder Kapitelmodus
```

Das ist besonders nützlich, bevor man ein großes Projekt mit LM Studio laufen lässt.

---

# 11. Kapitel-Auswahl

Du kannst die finale Dokumentation beschränken.

Beispiel:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile balanced --chapters "Executive Summary,Systemüberblick,Sicherheitsbetrachtung" --embedded-mycelia --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --force-rebuild
```

Damit werden nur die angegebenen Kapitel erzeugt.

Das reduziert:

```text
LLM-Aufrufe
Embedding-Queries
Laufzeit
Timeout-Risiko
```

---

# 12. WebGUI-Verhalten

Die WebGUI bietet die gleichen Kernentscheidungen wie die CLI.

Wichtige Einstellungen:

```text
Profil:
  quick
  balanced
  enterprise

Modus:
  Dry-Run ohne LLM
  LM Studio + Embedded MyceliaDB
  LM Studio + Sidecar Vectorstore
  externe MyceliaDB

Kapitel:
  Standardkapitel
  eigene Kapitel-Auswahl
  maximale Kapitelanzahl

Transparenz:
  estimate-only
  Live-Logs
  Fehleranzeige
  Erfolgsmeldung
  Dokumentationsvorschau
```

Empfohlener Start:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860
```

Dann öffnen:

```text
http://127.0.0.1:7860
```

---

# 13. Welche Modi sollte ich wann verwenden?

## Kleines Projekt oder Sample

```text
profile: quick
single-pass-final: ja
module-reduce: aus oder minimal
max-final-chapters: 3
```

Empfehlung:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --single-pass-final --max-final-chapters 3 --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --force-rebuild
```

## Mittelgroßes Projekt

```text
profile: balanced
module-reduce: ja
Kapitel: 5–8
workers: 1 oder 2
```

## Großes Enterprise-Projekt

```text
profile: enterprise
module-reduce: ja
kapitelweise Generierung: ja
workers: 1 bei langsamem LM Studio
workers: 2 bei schneller GPU
```

---

# 14. Warum sind lokale Modelle langsam?

Lokale Modelle wie `google_gemma-4-e4b-it` laufen über LM Studio auf deiner lokalen Hardware.

Die Laufzeit hängt stark ab von:

```text
GPU/CPU-Leistung
RAM/VRAM
Kontextlänge
Output-Länge
Quantisierung
Parallelität
Promptgröße
Anzahl Kapitel
```

Wenn du Logs siehst wie:

```text
prompt_tokens: 3510
completion_tokens: 603
total_tokens: 4113
```

dann verarbeitet das Modell mehrere tausend Tokens für einen einzigen Kapitelaufruf.

Bei kleinen Modellen oder CPU-Ausführung kann das lange dauern.

---

# 15. Warum ist nicht jeder LLM-Aufruf gleich teuer?

Ein Shard-Analyse-Aufruf kann klein sein:

```text
prompt_tokens: 700–1500
completion_tokens: 300–700
```

Ein Kapitelaufruf kann größer sein:

```text
prompt_tokens: 3000–8000
completion_tokens: 500–2000
```

Ein finaler Single-Pass-Aufruf kann sehr groß sein:

```text
prompt_tokens: 5000–20000+
completion_tokens: 1500–6000
```

Darum ist `single-pass-final` nicht immer schneller.  
Für kleine Projekte ja, für große Projekte kann es eher Timeout-Probleme erzeugen.

---

# 16. Was ist Dry-Run?

Dry-Run nutzt kein echtes LLM.

Stattdessen erzeugt DocForge synthetische Analyseergebnisse.

Gut für:

```text
Installation testen
WebGUI testen
Pipeline prüfen
Output-Verzeichnis prüfen
Fehler in Dateifiltern finden
```

Nicht geeignet für:

```text
echte technische Dokumentation
Security Review
Architekturanalyse
```

Dry-Run über CLI:

```powershell
docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --dry-run --profile quick --force-rebuild
```

---

# 17. Was ist Sidecar-Vectorstore?

Der Sidecar-Vectorstore ist der lokale Vektor-Fallback ohne externe MyceliaDB.

Er speichert typischerweise:

```text
vectors.f32
index.jsonl
ledger.jsonl
manifest.json
```

Vorteil:

```text
kein separater Datenbankserver nötig
gut für lokale Entwicklung
einfacher Start
```

Nachteil:

```text
weniger Betriebsfeatures als eine dedizierte MyceliaDB
lokal an den Workspace gebunden
```

---

# 18. Was ist Embedded MyceliaDB?

Embedded MyceliaDB startet einen lokalen MyceliaDB-kompatiblen Gateway mit.

Vorteil:

```text
kein extern installiertes MyceliaDB nötig
kompatibel mit dem Adapter
lokale semantische Speicherung
einfacher Enterprise-Modus
```

Wichtig:

```text
MYCELIA_LOCAL_TOKEN muss gesetzt sein.
```

Token erzeugen:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Token temporär setzen:

```powershell
$env:MYCELIA_LOCAL_TOKEN="DEIN_TOKEN_HIER"
```

Token automatisch pro Start erzeugen und direkt starten:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-webgui --host 127.0.0.1 --port 7860
```

Dauerhaft für den Windows-Benutzer setzen:

```powershell
[Environment]::SetEnvironmentVariable("MYCELIA_LOCAL_TOKEN", "DEIN_TOKEN_HIER", "User")
```

Danach ein neues Terminal öffnen.

Sicherheitshinweis:

```text
Für maximale Sicherheit kann der Token pro Sitzung neu generiert werden.
Für Komfort kann er dauerhaft gesetzt werden.
Wenn er dauerhaft gesetzt wird, sollte er nicht in Git, Logs oder Screenshots landen.
```

---

# 19. Was ist „zu viel LLM-Arbeit“?

Es ist zu viel, wenn:

```text
das Projekt sehr klein ist
du nur testen willst
du nur eine grobe Übersicht brauchst
LM Studio sehr langsam läuft
du Timeouts bekommst
```

Dann nutze:

```text
--profile quick
--single-pass-final
--max-final-chapters 3
--analysis-workers 1
```

Es ist nicht zu viel, wenn:

```text
du ein großes Projekt dokumentierst
du Belege brauchst
du Security-/Operations-Kapitel brauchst
du Architekturzusammenhänge erkennen willst
du Dokumentation für Übergabe oder Audit brauchst
```

Dann nutze:

```text
--profile enterprise
```

---

# 20. Praktische Entscheidungsregel

```text
Ich teste nur:
  quick + dry-run

Ich will eine schnelle echte Doku:
  quick + LM Studio

Ich will eine brauchbare Projektdoku:
  balanced + LM Studio + Embedded MyceliaDB

Ich will Enterprise-Dokumentation:
  enterprise + LM Studio + Embedded MyceliaDB

Ich habe Timeouts:
  workers 1, kleinere Shards, weniger Kapitel, quick oder balanced
```

---

# 21. Was die LLM-Arbeit fachlich leistet

Die LLM-Arbeit ist nicht nur Textgenerierung.

Sie umfasst:

```text
Code verstehen
Schnittstellen erkennen
Risiken extrahieren
Business Rules ableiten
Security Notes formulieren
Operations Notes formulieren
Belege zuordnen
Dateiperspektive verdichten
Modulperspektive verdichten
Kapitel schreiben
Kontext aus Retrieval nutzen
```

DocForge versucht also, aus Code ein auditierbares Wissensmodell zu erstellen.

Die finale Dokumentation ist nur die letzte sichtbare Schicht.

---

# 22. Warum das neue Profil-System wichtig ist

Vor v5.0.0 hatte DocForge vor allem einen Enterprise-Arbeitsstil.

Ab v5.0.0 kann man die Tiefe steuern:

```text
quick       -> weniger Arbeit, schneller
balanced    -> Standard
enterprise  -> maximale Tiefe
```

Das löst genau das Problem aus dem Sample:

```text
Kleine Projekte sollen nicht wie riesige Enterprise-Systeme behandelt werden.
Große Projekte sollen trotzdem tief und auditierbar dokumentiert werden.
```

---

# 23. Empfohlener erster Lauf

Für den ersten echten Lauf:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\docforge_enterprise_new\examples\sample_project.zip" --profile quick --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

Danach für größere Projekte:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\Pfad\zum\projekt.zip" --profile balanced --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

Und erst danach:

```powershell
$env:MYCELIA_LOCAL_TOKEN=(python -c "import secrets; print(secrets.token_urlsafe(32))"); docforge-enterprise "D:\Pfad\zum\projekt.zip" --profile enterprise --chat-model google_gemma-4-e4b-it --embedding-model text-embedding-nomic-embed-text-v2-moe --embedded-mycelia --analysis-workers 1 --chat-timeout 600 --embedding-timeout 300 --gateway-timeout 180 --max-chars-per-shard 2500 --max-embedding-batch-size 4 --analysis-max-tokens 900 --llm-retries 3 --force-rebuild
```

---

# 24. Fazit

DocForge Enterprise v5.0.0 macht viel LLM-Arbeit, wenn du viel Tiefe anforderst.

Das ist gewollt:

```text
Tiefe Dokumentation braucht mehrere Analyseebenen.
Auditierbarkeit braucht Zwischenartefakte.
Kontextqualität braucht Embeddings.
Enterprise-Kapitel brauchen separate Rendering-Schritte.
```

Aber ab v5.0.0 musst du das nicht immer bezahlen.

Du kannst wählen:

```text
quick       -> schnell
balanced    -> sinnvoller Standard
enterprise  -> tief und vollständig
```

Für kleine Samples ist `quick` richtig.  
Für echte Enterprise-Dokumentation ist `enterprise` richtig.
