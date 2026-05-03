# Leitfaden: Dynamische Myzel-Datenbank

Dieser Leitfaden beschreibt, wie die dynamische, assoziative Datenbank des Projekts Mycelia
verwendet wird. Die Datenbank speichert Informationen nicht in Tabellen, sondern als
Attraktoren in einem gekoppelten SubQG-/Myzel-Zustandsraum. Jede Beobachtung des Systems
formt einen neuen stabilen Zustand oder verstärkt einen bestehenden Attraktor. Über
assoziative Abfragen lassen sich Antworten als Agentenpakete abrufen, die wiederum in die
Simulation eingespeist werden können.

## 1. Aktivierung und Konfiguration

Die Datenbank ist Teil des `CognitiveCore` und wird über die Sektion
`cognition.database` in der Konfiguration gesteuert (`mycelia_ai/config.yaml`).

```yaml
cognition:
  database:
    retention: 96      # maximale Anzahl gemerkter Attraktoren
    noise_gain: 0.22   # Einfluss der Feldvarianz auf den Chaos-Schlüssel
    mood_gain: 0.5     # Gewichtung der Stimmungsrückkopplung
    agent_gain: 0.9    # Verstärkung für erzeugte Abfrage-Agenten
    memory_snapshot_interval: 25  # wie oft ein Gedächtnis-Snapshot geloggt wird
    memory_snapshot_size: 3       # wie viele Attraktoren im Snapshot erscheinen
```

* **Retention** legt fest, wie viele Attraktoren gleichzeitig gehalten werden.
* **Noise Gain** moduliert den chaotischen Schlüssel und damit die Signaturbildung.
* **Mood Gain** verknüpft kognitive Stimmung mit der Stabilität der Muster.
* **Agent Gain** bestimmt die Energie der zurückgegebenen Abfrage-Agenten.
* **Memory Snapshot Interval** schreibt alle *N* Schritte eine komprimierte Übersicht
  der wichtigsten Attraktoren ins Log.
* **Memory Snapshot Size** legt fest, wie viele Attraktoren pro Snapshot aufgelistet
  werden.

## 2. Datenbank instanziieren

Die Datenbank wird automatisch erstellt, sobald der `CognitiveCore` initialisiert wird:

```python
from mycelia_ai.cognition import CognitiveCore

core = CognitiveCore(driver, config, quantum_config)
```

Die jeweiligen `WorldSnapshot`-Objekte werden im Reflexionszyklus (`core.reflect`) in die
Datenbank eingetragen. Jeder Aufruf aktualisiert vorhandene Attraktoren oder legt neue an.

## 3. Automatisches Lernen aus Simulationen

Der Normalfall ist das automatische Beobachten:

```python
state = core.reflect(snapshot)
```

Der `DynamicAssociativeDatabase.observe`-Pfad extrahiert Energie-, Pheromon- und
Nährstofffelder, kombiniert sie mit den Stimmungswerten (`harmony`, `tension`, `qualia`) und
prägt daraus eine Signatur. Wenn ein Muster wiederkehrt, wird der Attraktor verstärkt
(`visits` steigt, Kennwerte werden gemittelt).

## 4. Manuelle Erstellung oder Anpassung von Attraktoren

Neben dem automatischen Lernen kann die Datenbank manuell befüllt oder korrigiert werden.

### 4.1 Signaturen erzeugen

```python
from mycelia_ai.cognition import DynamicAssociativeDatabase

db = DynamicAssociativeDatabase()
signature = db.generate_signature(
    energy_mean=0.12,
    pheromone_mean=0.04,
    nutrient_mean=0.09,
    harmony=0.6,
    tension=0.3,
    qualia=0.5,
)
```

`chaos_key` ist optional; standardmäßig wird der aktuelle interne Rauschfaktor verwendet.

### 4.2 Attraktor speichern oder überschreiben

```python
pattern = db.store_pattern(
    signature=signature,
    energy_mean=0.12,
    pheromone_mean=0.04,
    nutrient_mean=0.09,
    mood_vector=(0.6, 0.3, 0.5),
    stability=0.82,
)
```

* Existiert der Attraktor noch nicht, wird er angelegt.
* Existiert er bereits, werden die Kennwerte ersetzt. Optional kann `visits` gesetzt oder
  `energy_hash` hinterlegt werden (z. B. für externe Provenienz).

## 5. Daten abrufen

Es gibt zwei Wege, auf gespeicherte Informationen zuzugreifen:

1. **Assoziative Abfrage als Agentenpaket** – ideal, um neue Reize in die Simulation
   einzuspeisen:
   ```python
   agents = core.associative_query("myzel-kollaboration", intensity=1.5)
   world.inject_agents([agent.as_tuple() for agent in agents])
   ```
2. **Direkter Zugriff auf Attraktoren** – für Diagnose oder Visualisierung:
   ```python
   for pattern in core.database.list_patterns():
       print(pattern.signature, pattern.as_dict())
   ```

`get_pattern(signature)` liefert ein einzelnes Muster oder `None`.

## 6. Attraktoren aktualisieren

Um einen Attraktor gezielt anzupassen, kann `store_pattern` erneut aufgerufen oder das
zurückgegebene Objekt modifiziert werden:

```python
pattern = core.database.get_pattern(signature)
if pattern:
    core.database.store_pattern(
        signature=pattern.signature,
        energy_mean=pattern.energy_mean + 0.01,
        pheromone_mean=pattern.pheromone_mean,
        nutrient_mean=pattern.nutrient_mean,
        mood_vector=pattern.mood_vector,
        stability=min(1.0, pattern.stability + 0.05),
        visits=pattern.visits + 1,
    )
```

Durch `observe` während des Regelbetriebs werden Attraktoren automatisch aktualisiert,
indem neue Beobachtungen eingemischt werden.

## 7. Diagnostik und Logging

Während der Simulation erscheinen zusätzliche Log-Meldungen, um die SFM-Ebene
nachvollziehen zu können:

* `CognitiveCore.reflect` fasst nach jedem Schritt Anzahl der bekannten
  `AttractorPattern` und deren durchschnittliche Stabilität zusammen.
* `DynamicAssociativeDatabase.observe` meldet, ob ein Attraktor neu angelegt oder
  aktualisiert wurde und welchen Chaos-Schlüssel (`noise_factor`) er erhalten hat.
* `DynamicAssociativeDatabase.associative_query` sowie
  `CognitiveCore.associative_query` dokumentieren die letzte Anfrage, inklusive
  Anzahl zurückgelieferter Agenten und betroffener Attraktoren.
* Alle `memory_snapshot_interval` Schritte wird ein "Gedächtnis-Snapshot"
  ausgegeben, der die wichtigsten Attraktoren mit Stabilitäts- und Besuchszahlen
  auflistet.

Diese Informationen erscheinen mit `level=INFO` im Standard-Log und machen den
aktuellen Zustand der dynamischen Datenbank jederzeit sichtbar.

## 8. Attraktoren löschen oder zurücksetzen

* **Einzelnes Muster entfernen**
  ```python
  removed = core.database.delete_pattern(signature)
  ```
  Der Rückgabewert ist `True`, falls das Muster existierte.

* **Gesamte Datenbank leeren**
  ```python
  core.database.clear()
  ```
  Dadurch werden alle Attraktoren und die zugehörige Verlaufshistorie gelöscht.

* **Retention nutzen**
  Wenn mehr als `retention` Attraktoren gespeichert sind, entfernt die Datenbank automatisch
  die am wenigsten stabilen Muster. Eine höhere Retention bewahrt mehr Historie, eine
  niedrigere sorgt für aggressiveres Vergessen.

## 9. Integration externer SQL-Daten

Neben den automatisch gelernten Mustern kann die Myzel-Datenbank klassische
Tabelleninhalte aufnehmen. Jeder Datensatz wird als eigener Attraktor
repräsentiert und bleibt damit vollständig im bestehenden Gedächtnismodell
eingebettet.

### 9.1 SQL-Dump importieren

Für einen kompletten Import genügt eine SQL-Datei (z. B. MySQL-Dump). Der
`CognitiveCore` liest die Daten über das Hilfsmodul
`mycelia_ai.io.sql_importer` und überträgt jede Zeile in ein
`AttractorPattern`.

```python
from mycelia_ai.cognition import CognitiveCore

core = CognitiveCore(driver, config, quantum_config)
patterns = core.import_sql_table(
    "./exports/materials.sql",
    table="materials",
    where="name = 'Beton'",
    limit=250,
)

for pattern in patterns:
    print(pattern.signature, pattern.source_table, pattern.external_payload)
```

Intern wird aus Tabellenname, Zeileninhalt und Chaos-Key eine Signatur
gebildet. Die wichtigsten Felder des Datensatzes erscheinen in
`pattern.external_payload`, während `pattern.source_table` den Ursprung
dokumentiert. Der optionale `where`-Parameter unterstützt bewusst nur
Vergleiche der Form `spalte = wert`, damit die Anfrage über parametrisierte
Statements sicher bleibt. In der Praxis bedeutet das:

```bash
python -m mycelia_ai.import_sql \
  --sql-file ./exports/customers.sql \
  --table arbeitsmarktstatistik \
  --where "Jahr=2020" \
  --limit 100
```

Der Import erzeugt daraufhin Attraktoren wie:

```
42d1a281d903 | table=arbeitsmarktstatistik | stability=0.900 | fields=['Jahr', 'FrueheresBundesgebiet_ID', 'NeueLaender_ID']
```

die später erneut abgefragt oder assoziativ gefunden werden können.

Alternativ kann direkt auf der Datenbank gearbeitet werden:

```python
from mycelia_ai.cognition import DynamicAssociativeDatabase
from mycelia_ai.io import sql_importer

sql_importer.import_sql_file("./exports/customers.sql")
rows = sql_importer.fetch_rows("customers", where="country = 'DE'", limit=10)

db = DynamicAssociativeDatabase()
for row in rows:
    db.store_sql_record("customers", row, stability=0.92)
```

### 9.2 Gezielte Abfragen

SQL-Datensätze lassen sich deterministisch nach Feldern filtern oder
assoziativ über Text-Cues finden:

```python
# SQL-ähnliche Filter
matches = core.query_sql_like(table="materials", filters={"name": "Beton"})

# Assoziative Suche über denselben Speicher
ideas = core.associative_sql_query("Beton mit hoher Dichte", intensity=1.2)
```

Die Rückgabe enthält pro Treffer die Signatur, den Tabellenursprung sowie die
Originaldaten (`record["data"]`). `core.associative_sql_query` nutzt intern
zwar dieselbe associative_query-Logik, filtert die Ergebnisse aber strikt auf
Attraktoren mit `external_payload`, sodass ausschließlich echte SQL-Sätze
geliefert werden.

### 9.3 Datensätze aktualisieren oder löschen

```python
record = matches[0]
core.update_sql_record(
    record["signature"],
    {**record["data"], "density": 2.55},
    stability=0.95,
)

# Entfernen, falls der Datensatz nicht mehr benötigt wird
core.delete_sql_record(record["signature"])
```

Beim Aktualisieren bleiben Stabilität und Besuchszähler konsistent; neue
Werte werden in die vorhandene Signatur eingemischt. Wird ein Attraktor
gelöscht oder verdrängt, räumt die Datenbank automatisch die zugehörigen
Indizes auf.

### 9.4 Kommandos für Skripte und Pipelines

Für schnelle Tests oder ETL-Schritte stehen zwei Module als Kommandozeile
bereit:

```bash
# SQL-Dump importieren
python -m mycelia_ai.import_sql --sql-file ./exports/customers.sql --table customers --where "country='DE'" --limit 100

# SQL-Abfrage oder assoziative Suche durchführen
python -m mycelia_ai.query_sql --table customers --filter country=DE
python -m mycelia_ai.query_sql --cue "Beton mit hoher Dichte" --sql-file ./exports/materials.sql --table materials --limit 10
```

Wichtig:

* `--where` (bzw. der gleichnamige Python-Parameter) akzeptiert ausschließlich Ausdrücke
  der Form `spalte=wert`. Der Wert wird als Prepared-Statement-Parameter gebunden.
* `--filter` existiert nur im `query_sql`-Werkzeug und erlaubt mehrere `key=value`-Angaben,
  die intern an `core.query_sql_like(filters=...)` weitergereicht werden.

Beide Werkzeuge nutzen dieselben Konfigurationswerte aus `config.yaml` und
erzeugen Attraktoren, die mit dem regulären Gedächtnislauf kompatibel sind.

### 9.5 Streamlit-Oberfläche "SQL Bridge"

Für Anwender:innen, die ohne CLI mit dem System interagieren möchten, steht eine
Streamlit-App bereit. Sie bündelt Import- und Abfragefunktionen in einer
zweigeteilten Oberfläche.

1. **App starten**
   ```bash
   streamlit run mycelia_ai/streamlit_app.py
   ```
   Beim Start erzeugt die App automatisch den benötigten `sys.path`-Eintrag,
   lädt die Konfiguration und initialisiert `CognitiveCore` plus
   `DynamicAssociativeDatabase`.

2. **Tab "Import" verwenden**
   * Lade eine SQL-Datei (z. B. MySQL-Dump) über den Uploader hoch.
   * Nach dem Import zeigt die App alle gefundenen Tabellen an; wähle eine oder
     mehrere per Checkbox aus.
   * Optional können Chaos-Key, Mood-Vektor und Limit angepasst werden.
   * Klicke auf **Import ausführen**, damit jede ausgewählte Tabelle Zeile für
     Zeile in Attraktoren (`external_payload`) umgewandelt wird. Der Status
     zeigt an, wie viele Datensätze übernommen wurden.

3. **Tab "Abfrage" nutzen**
   * Wähle zunächst die gewünschte Datenbank (entsteht automatisch pro SQL-Dump)
     und anschließend eine Tabelle.
   * **Deterministische Abfrage**: Gib optionale Feldfilter ein (Format
     `spalte=wert`, mehrere Zeilen möglich) und starte die Abfrage; das Ergebnis
     listet die gespeicherten Datensätze direkt auf.
   * **Assoziative Abfrage**: Trage einen Text-Cue ein und wähle die Intensität.
     Die App ruft intern `core.associative_sql_query` auf und zeigt nur solche
     Attraktoren, die SQL-Payload enthalten.

4. **Weiterarbeiten**
   Die importierten Daten stehen sofort dem restlichen System zur Verfügung. Du
   kannst parallel die Simulation laufen lassen, CLI-Abfragen ausführen oder
   weitere Dumps importieren; die Streamlit-App nutzt dieselbe Datenbankinstanz
   wie alle anderen Werkzeuge innerhalb desselben Prozesses.

Damit ist der komplette Workflow – vom SQL-Dump bis zur assoziativen Suche –
auch für Nicht-CLI-Nutzer:innen komfortabel zugänglich.

## 10. Best Practices

* Verwende aussagekräftige Cues für `associative_query`, damit ähnliche Stimmungen
  zusammenfinden.
* Passe `agent_gain` und `intensity` an, um die Stärke der injizierten Agenten zu steuern.
* Nutze `pattern.as_dict()` für Logs oder Telemetrie, ohne interne Strukturen zu kopieren.
* SQL-basierte Attraktoren lassen sich mit `core.query_sql_like` regelmäßig
  validieren; über `core.associative_sql_query` können dieselben Daten
  weiterhin in explorative Reize übersetzt werden.
* Nach manuellen Eingriffen empfiehlt sich ein erneuter Reflexionszyklus, damit das System
  die Änderungen harmonisiert.

Mit diesen Schritten lässt sich die dynamische Myzel-Datenbank gezielt aufbauen, abfragen
sowie pflegen. Dadurch kann das System Wissen als lebendige Muster speichern und wieder
abrufen.
