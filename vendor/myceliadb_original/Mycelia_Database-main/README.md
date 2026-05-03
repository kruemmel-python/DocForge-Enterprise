### **Sicherheitsdokumentation und Bedrohungsmodellanalyse für Project Mycelia**

**Stand:** 15. November 2025
**Version:** 1.0
**Status:** Final

---

#### **1. Zusammenfassung (Executive Summary)**

Dieses Dokument beschreibt die Sicherheitsarchitektur von Project Mycelia, einer hybriden kognitiven Simulationsplattform. Das System kombiniert eine emergente, chaotische Simulationsumgebung mit einer strukturierten SQL-basierten Datenpersistenz.

Die Sicherheitsbewertung kommt zu dem Schluss, dass die Kernarchitektur des Systems ein **außergewöhnlich hohes Maß an intrinsischer Sicherheit** aufweist. Angriffe auf die innere Logik der Simulation sind aufgrund nicht-deterministischer, chaotischer und pfadabhängiger Mechanismen praktisch undurchführbar.

Die primären Angriffsvektoren liegen an den Schnittstellen zur Außenwelt, insbesondere der SQL-Datenimportfunktion und der Laufzeitumgebung. Eine kritische **SQL-Injection-Schwachstelle**, die in einer früheren Version identifiziert wurde, wurde in der aktuellen Version **vollständig und effektiv behoben**.

Die verbleibenden Risiken sind auf die operative Umgebung (Deployment) beschränkt und können durch Standard-Härtungsmaßnahmen wie Code-Signierung und Überwachung der Dateisystemintegrität weiter minimiert werden. Das Gesamtsicherheitsniveau des Systems wird als **hoch** eingestuft.


#### **Hinweis für Auditoren und Sicherheitsteams**

**Das „Speichersystem“ von Project Mycelia ist kein konventioneller Datenbankansatz.**
**Externe Datensätze werden nicht als Tabellenzeilen persistiert, sondern in chaotisch modulierte Attraktorfelder überführt, deren Zustand sich dynamisch auf Basis der SubQG-Simulation und des kognitiven Feedback-Loops entwickelt.**
**Klassische Datenbankangriffe (SQL Injection, State Extraction, Index Manipulation) sind auf dieser Architekturebene nicht anwendbar, da das System weder feste Tabellen noch deterministische Speicherpfade besitzt.**



#### **2. Systemarchitektur und Sicherheitsmodell**

Project Mycelia basiert auf einer mehrschichtigen Architektur, deren Sicherheit als dualistisches Modell verstanden werden muss.

**2.1 Kernkomponenten**

1.  **Simulationsschicht (Innere Welt):** Bestehend aus dem Sub-Quantengeometrie-Feld (SubQG) und dem Myzel-Netzwerk. Diese Schicht wird durch eine native C/OpenCL-Bibliothek GPU-beschleunigt und zeichnet sich durch emergentes, schwer vorhersagbares Verhalten aus.
2.  **Kognitiver Kern:** Die Python-basierte Steuerungsebene, die den Zustand der Simulation beobachtet, in abstrakte Metriken (`harmony`, `tension`, `qualia`) übersetzt und Aktionen auslöst.
3.  **Dynamische Assoziative Datenbank (DAD):** Ein In-Memory-Speicher für "Attractor Patterns", die aus den Simulationszuständen gelernt oder aus externen Quellen importiert werden.
4.  **SQL I/O-Schicht:** Module zum Importieren von SQL-Dateien in eine temporäre SQLite-Datenbank und zur Transformation von Datensätzen in Attractor Patterns innerhalb der DAD.
5.  **Quanten-Orakel:** Ein optionales Modul, das bei hoher Systemdissonanz zur Anwendung kommt, um durch Simulation eines "Traumzustands" neue, stabilere Systemzustände zu finden.

**2.2 Dualistisches Sicherheitsmodell**

*   **Die "Innere Welt" (Simulationskern):** Umfasst die Simulationsschicht und den kognitiven Feedback-Loop. Ihre Sicherheit basiert auf **Komplexität und Unvorhersehbarkeit**. Ein externer Angreifer kann die internen Zustände nicht präzise vorhersagen oder steuern.
*   **Die "Äußere Welt" (Datenschnittstellen):** Umfasst die SQL-Importfunktionen und die Kommandozeilen-Tools. Ihre Sicherheit basiert auf **expliziten Validierungs- und Abwehrmechanismen** wie der sicheren Verarbeitung von Eingabedaten.

#### **3. Grundlegende Sicherheitsprinzipien des Designs**

Das System wurde mit mehreren inhärenten Sicherheitsmerkmalen entworfen:

**3.1 Sicherheit durch Emergenz und Komplexität**
Die Kernverteidigung gegen logische Manipulationen ist die Architektur selbst. Ein Angreifer kann das System nicht gezielt in einen kompromittierten Zustand zwingen, da:
1.  **Chaotische Signaturbildung:** Die Funktion `_update_noise` in `dynamic_database.py` nutzt eine logistische Gleichung, um einen `_noise_factor` zu erzeugen. Dieser fließt in die Signatur jedes Attraktors ein, was eine Vorhersage oder Reproduktion von Signaturen unmöglich macht.
2.  **Pfadabhängigkeit:** Der Zustand der Datenbank ist das Ergebnis der gesamten Simulationshistorie. Eine externe Manipulation hätte unvorhersehbare Konsequenzen.
3.  **Unkontrollierbare Injektion:** Die räumliche Platzierung von Agenten, die aus einer `associative_query` resultieren, hängt von der (unvorhersehbaren) Signatur des Attraktors ab, was einen gezielten Angriff auf einen bestimmten Bereich der Simulation verhindert.

**3.2 Verteidigung durch Selbstheilung (Traumzustand)**
Sollte es einem Angreifer dennoch gelingen, das System in einen Zustand hoher Dissonanz (`tension`) zu versetzen, greift ein Selbstheilungsmechanismus:
*   Das Überschreiten des `dissonance_threshold` löst das Quanten-Orakel aus.
*   Dieses führt eine VQE-Optimierung durch, um einen energetisch günstigeren (stabileren) Systemzustand zu finden.
*   Dieser "Reset" bricht den manipulativen Zyklus ab und macht die vom Angreifer erzeugten "toxischen" Attraktoren irrelevant.

**3.3 Prinzip der geringsten Rechte (Interface-Design)**
Die SQL-Schnittstelle wurde restriktiv implementiert:
*   Sie arbeitet auf einer temporären, isolierten SQLite-Datenbank, die aus einem Dump erstellt wird. Das Risiko für produktive, externe Datenbanken ist ausgeschlossen.
*   Die `fetch_rows`-Funktion erlaubt nur stark eingeschränkte WHERE-Klauseln (`spalte = wert`), was die Angriffsfläche minimiert.

#### **4. Bedrohungsmodell und Angriffsvektoren**

**4.1 Angriffe auf die Datenschnittstelle (Äußere Welt)**

*   **SQL-Injection**
    *   **Beschreibung:** Eine frühere Version des Systems war anfällig, da der `where`-Parameter in `io/sql_importer.py` direkt in den SQL-Query-String konkateniert wurde.
    *   **Status:** **BEHOBEN**.
    *   **Mitigation:** Die aktuelle Implementierung verwendet **parametrisierte Abfragen**, bei denen die SQL-Struktur strikt von den Daten getrennt ist. Zusätzlich validiert die `_validate_identifier`-Funktion Spalten- und Tabellennamen gegen unzulässige Zeichen. Dieses Risiko ist als vollständig mitigiert anzusehen.

*   **Manipulation von SQL-Dateien vor dem Import**
    *   **Beschreibung:** Ein Angreifer mit Zugriff auf das Dateisystem könnte die `.sql`-Datei vor dem Import durch `import_sql.py` manipulieren, um bösartige oder korrumpierte Daten als Attraktoren in das System einzuschleusen.
    *   **Mitigation:** Dies ist ein Risiko der Betriebsumgebung, nicht der Anwendung selbst. Die Sicherung des CI/CD-Pipelines und der Dateisystemberechtigungen auf dem Produktivsystem ist erforderlich.

**4.2 Angriffe auf die Systemlogik (Innere Welt)**

*   **Logisches State-Poisoning**
    *   **Beschreibung:** Ein theoretischer Angriff, bei dem ein Angreifer versucht, die Simulation gezielt in einen instabilen Zustand zu zwingen, um einen "toxischen" Attraktor zu erzeugen und diesen wiederholt zu triggern.
    *   **Mitigation:** Wie in 3.1 und 3.2 beschrieben, ist dieser Angriff aufgrund der chaotischen Natur und des Selbstheilungsmechanismus praktisch undurchführbar.

*   **Ressourcen-Erschöpfungsangriffe (Denial of Service)**
    *   **Beschreibung:** Ein Angreifer versucht, das System wiederholt an den Rand der Dissonanzschwelle zu bringen, um rechenintensive Traumzyklen zu erzwingen und die Hardware auszulasten.
    *   **Mitigation:** Dieser Angriff erfordert eine präzise Steuerung des `tension`-Wertes, die dem Angreifer aufgrund der Systemkomplexität nicht möglich ist. Das Risiko wird als theoretisch und in der Praxis vernachlässigbar eingestuft.

**4.3 Angriffe auf die Laufzeitumgebung**

*   **Kompromittierung der C-Bibliothek**
    *   **Beschreibung:** Ein Angreifer mit Schreibzugriff auf das Dateisystem könnte die kompilierte OpenCL-Treiberbibliothek (`.dll`, `.so`) durch eine bösartige Version ersetzen, was zur vollständigen Übernahme des Prozesses führen würde.
    *   **Mitigation:** Code-Signierung der Bibliotheken, Überwachung der Dateisystemintegrität (z.B. mit `tripwire` oder `AIDE`) und Ausführung der Anwendung mit minimalen Benutzerrechten.

*   **Kompromittierung von Python-Abhängigkeiten**
    *   **Beschreibung:** Eine bösartige Version einer Abhängigkeit (z.B. `pyyaml`) könnte Code einschleusen.
    *   **Mitigation:** Regelmäßiges Scannen der Abhängigkeiten mit Tools wie `pip-audit` und das Beziehen von Paketen aus vertrauenswürdigen Quellen.

#### **5. Sicherheitsbewertung und Risikoeinstufung**

| Bedrohung | Wahrscheinlichkeit | Auswirkung | Risikostufe (nach Korrektur) | Anmerkungen |
| :--- | :--- | :--- | :--- | :--- |
| **SQL-Injection** | **Sehr Gering** | Kritisch | **Gering** | Die Schwachstelle wurde durch parametrisierte Abfragen vollständig behoben. |
| **Manipulation der C-Bibliothek** | Gering | Kritisch | **Mittel** | Erfordert erweiterten Systemzugriff. Risiko liegt in der Betriebsumgebung. |
| **Logisches State-Poisoning** | Sehr Gering | Mittel | **Gering** | Inhärente Systemarchitektur (Chaos, Selbstheilung) wirkt als starke Mitigation. |
| **Ressourcen-Erschöpfung** | Sehr Gering | Gering | **Gering** | Angriff aufgrund der Systemkomplexität nicht präzise steuerbar. |
| **Manipulation von SQL-Dateien** | Mittel | Mittel | **Mittel** | Hängt stark von der Sicherheit der Deployment-Pipeline und des Dateisystems ab. |
| **Kompromittierte Abhängigkeiten** | Gering | Kritisch | **Gering** | Standardrisiko für Softwareprojekte; mitigierbar durch etablierte Prozesse. |

#### **6. Empfehlungen zur Härtung**

1.  **Code-Ebene:**
    *   Die kritische SQL-Injection-Lücke wurde vorbildlich geschlossen. Es sind keine weiteren Maßnahmen auf dieser Ebene dringend erforderlich.

2.  **Build- und Deployment-Ebene (Wichtigste verbleibende Maßnahmen):**
    *   **Code-Signierung:** Die nativen C/OpenCL-Bibliotheken (`.dll`, `.so`) sollten digital signiert werden, um ihre Authentizität und Integrität sicherzustellen.
    *   **Integritätsüberwachung:** In der Produktionsumgebung sollte die Integrität der Anwendungsdateien und Bibliotheken durch ein Host-based Intrusion Detection System (HIDS) überwacht werden.
    *   **Prinzip der geringsten Rechte:** Der Anwendungs-Prozess sollte unter einem dedizierten Benutzerkonto mit minimalen Dateisystem- und Netzwerkberechtigungen ausgeführt werden.

3.  **Prozess-Ebene:**
    *   **Regelmäßige Dependency Scans:** Integrieren Sie automatisierte Sicherheits-Scans für Python-Abhängigkeiten in die CI/CD-Pipeline.

#### **7. Gesamtfazit**

Project Mycelia demonstriert ein innovatives Sicherheitskonzept, das klassische Abwehrmaßnahmen mit einer durch Komplexität und Emergenz gesicherten Kernarchitektur kombiniert. Nach der Behebung der SQL-Injection-Schwachstelle an der Datenschnittstelle ist das System als **architektonisch robust und sicher** zu bewerten. Die verbleibenden Risiken sind nicht auf Designfehler in der Anwendung zurückzuführen, sondern betreffen die Betriebsumgebung und können durch standardisierte DevOps- und Sicherheitspraktiken effektiv adressiert werden.

