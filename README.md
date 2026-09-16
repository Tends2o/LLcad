# MathForge 3D

Eine lokale, ausführbare Umsetzung des [Mathematik-zuerst-Bauplans](Mathematik_First_3D_MCP_Bauplan.md): echte Open-CASCADE-Geometrie, versionierte mathematische Konstruktionen und 21 MCP-Werkzeuge. **Die Modellierung erfolgt vollständig über das LLM.** Ein Browser-Viewer ist optional. Änderungen durchlaufen **Kandidat → Prüfung → atomare Übernahme**.

**Status: getestete Entwicklungsversion 0.1.0.** Die vollständige Produktionsabnahme des Bauplans ist noch offen. Der lokale Codex-Transport ist eingerichtet und geprüft. Drei echte LLM-Sprachaufträge sind im lokalen Codex-Host geprüft. Offen bleiben die produktive Remote-OAuth-/HTTPS-Einrichtung sowie die in [Umsetzungsstand](docs/implementation-status.md) aufgeführten weiterführenden Funktionen. Diese Version meldet ausschließlich tatsächlich implementierte Fähigkeiten.

![MathForge-Viewer mit gemessener Nuttiefe](reports/viewer.png)

## Über das LLM bedienen

Das LLM entdeckt Modelle mit `cad_list_models`, erstellt mathematische Konstruktionen und findet Merkmale mit `cad_find` und `cad_inspect`. Es plant die Änderung, fragt Jobs ab, liest Prüfnachweise, übernimmt gültige Kandidaten und exportiert Dateien. Dazu sind weder eine manuelle Modellwahl noch Klicks im Viewer erforderlich. Echte Mehrdeutigkeiten werden über weitere Werkzeugabfragen oder eine kurze Rückfrage im Gespräch geklärt.

Auf diesem Rechner läuft bereits `llcad.service`; Codex ist mit dem gemeinsamen lokalen HTTP-Dienst verbunden und lädt die Zugangsdaten automatisch. Zwei gleichzeitige Verbindungen und ein vollständiger Modellierablauf wurden geprüft. Details stehen im [lokalen Codex-Betrieb](docs/local-codex.md).

Alternativ kann ein lokaler MCP-Host `deployment/start-mcp.sh` starten. Dieser Einstieg verwendet stdio, öffnet keinen Netzwerkport und benötigt keine Browseranmeldung oder Tokeneingabe. Der private Betriebssystemprozess bestimmt die lokale Identität. Die vorhandene exklusive Datensperre gilt weiterhin: HTTP- und stdio-Dienst dürfen dasselbe Datenverzeichnis nicht gleichzeitig öffnen.

```bash
npm run test:mcp
```

Der Integrationstest steuert Konstruktion, semantische Suche, Flächenauswahl, 20-µm-Nutänderung, Prüfung, Commit, Neuzuordnung und STEP-Export über das offizielle MCP-SDK. `reports/mcp-workflow.json` dokumentiert die Werkzeugaufrufe und **null Browserinteraktionen**. Dies ist ein SDK-/Controller-Test. Zusätzlich prüft `npm run test:host` den tatsächlich installierten Codex-App-Server; `reports/codex-host.json` enthält diesen Host-Transportnachweis. `npm run test:llm` ergänzt drei tatsächlich vom Modell bearbeitete Sprachaufträge: Nutkorrektur mit STEP-Export, Durchmesserkorrektur und Klärung mehrdeutiger Auswahl. Der Bericht `reports/llm-eval.json` enthält die tatsächlichen CAD-Aufrufe, numerische Prüfergebnisse, Host-/Modellversion, Latenz und Tokenverbrauch. Die Remote-OAuth-Abnahme und eine breite allgemeine LLM-Evaluation bleiben gesondert offen.

## Optionalen Viewer starten

Der optionale Viewer wird hier bereits vom laufenden Dienst bereitgestellt. Auf einer separaten Installation ohne laufenden Dienst:

```bash
npm start
```

Öffne **http://127.0.0.1:4310**. Der Server erzeugt beim ersten Start einen privaten lokalen Schlüssel. Zum Anzeigen in deinem Terminal:

```bash
cat data/local-token
```

Für die optionale manuelle Ansicht den Schlüssel im Anmeldedialog eingeben. Der lokale Modus bindet ausschließlich an Loopback. Die LLM-Bedienung benötigt diesen Dialog nicht.

## Auf einem neuen Rechner installieren

Getestet mit Linux x86-64, Node **24.19.0**, Python **3.13.5**, Bubblewrap und den exakt gesperrten Abhängigkeiten. Erforderliche Systempakete: `python3-venv`, `bubblewrap`, `libgl1`, `python3-openvdb=10.0.1-2.3+b1` (Debian 13, passend zu Python 3.13), `util-linux` (für `/usr/bin/flock`); die passende Python-/Node-Version muss installiert sein. User-Namespaces müssen für den Worker verfügbar sein.

```bash
npm run setup
npx playwright install --with-deps chromium
npm run verify
npm start
```

`deployment/setup.sh` verwendet `requirements.lock` und `npm ci`. Kein Shell-, Python- oder JavaScript-Code aus Modelldaten wird ausgeführt. Der Python-Adapter ruft die nativen OCCT-Bindings auf; jede Berechnung läuft in einem separaten Bubblewrap-Namespace ohne Netzzugang.

## Was funktioniert

- Typisierte Dezimalparameter mit Einheiten, gespeicherte mathematische Ausdrücke, DAG-Compiler, geschützte Parameter und begrenzte Ressourcen.
- Quader, Kugel, Zylinder, Kegel, Torus; Profile, Bézier-/B-Spline-Kurven, rationale Flächen; Extrusion, Rotation, Loft, Sweep; CSG; Bohrungen, Taschen, Nuten, Verrundungen, Fasen und Schalen innerhalb ihrer Operatorverträge.
- Punkte, Linien, Bögen, endliche Ebenen, UV-Trimmung, Deckflächen, Vernähen, Regularisierung und benutzerdefinierte Helixgewinde.
- Transformationen, Spiegelung, Instanzen sowie lineare und zyklische Muster mit gezielten Einzelvarianten; analytische inverse Volumenkonstruktion und gekoppelter begrenzter SLSQP-Maßsolver; tatsächliche B-Rep-Messungen und Abstandsjobs.
- Implizite Feldgraphen, kompakte lokale Änderungen, invertierbare lokale Deformation, konservative Lipschitz-Schranken und sparse Octree-Oberflächenextraktion.
- Lokale Kontrollpunktänderungen mit exakten rationalen C0-/C1-/C2-Schranken und Regularitätsprüfung für registrierte Patchanschlüsse; gebrochene Anschlüsse verhindern den Commit.
- Ausdrückliche Neuberechnung nach einem Buildwechsel mit `cad_rebuild`, vollständiger Prüfung und unveränderten alten Revisionen.
- Dauerhafte SQLite-Revisionen, isolierte Kandidaten, Inhaltscache, Jobs mit Fencing, Idempotenz, Pflichtprüfungen, Compare-and-Swap und Audit-/Outbox-Verarbeitung.
- Authentifizierung und objektbezogene Eigentümerprüfung für Modelle, Jobs, Auswahlen, Ressourcen und Dateien.
- Versionierte Projekte, Baugruppen und Teile, getrennte geometrische Hoheiten und hierarchische lokale Bezugsrahmen; LLM-Abfragen über `cad_structure`.
- Native Flächenherkunft für registrierte Primitive, boolesche Operationen, Nut/Bohrung/Tasche, Transformationen und Instanzen; gespeicherte Flächenhandles und ausdrückliche eindeutige Neuzuordnung. Teilungen, Zusammenführungen und unbekannte Herkunft werden sicher abgewiesen.
- Dauerhafte faire Jobverteilung zwischen Nutzern, Offline-Betriebssperre, geprüfter Backup-/Restorepfad und mandantenbezogene Löschung des aktiven Datenspeichers.
- IR-/STEP-/STL-/OpenVDB-Import (STEP wahlweise mit Produktstruktur als Rahmen, Baugruppen und Teile) sowie IR-, STEP-, B-Rep-, STL-, GLB- und OpenVDB-Export mit erneuter Prüfung. Mesh- und Feldvorschauen behalten ihren ausdrücklich begrenzten Nachweisstatus.
- Rigorose Intervallarithmetik mit Gradientenfluss-Zertifikaten für Feldänderungen, blendfreie Passregionen, Dual Contouring, implizite Krümmung, ISO-Gewindegrundprofile, Offsets, Sweeps mit rotationsminimierenden Rahmen, Netzreparatur, lokales Remeshing und ARAP-Deformation maßgeblicher Netze.
- Messungen mit ausgewiesener Beweisstärke: Chamfer-/Hausdorff-Proben, exakter Minimalabstand und IoU, abgetastete Wandstärke, Freigang entlang einer Bewegung; Profil `manufacturing_candidate` mit abgetasteten Prozessregeln ohne Zertifizierung.
- Adaptive Tessellation je Fläche mit Auflösungsbericht, räumliche Ausschnitte, SVG-Schnitt- und Projektionsansichten, Fehlerbudget, Reparaturketten, interne Veröffentlichung mit kurzlebigen signierten Links, Aufbewahrungspolitik, Kennzahlen und ein kleiner Pool vorgewärmter isolierter Worker.
- Viewer mit Struktur- und Featurebaum, Parametern, Messwerten, Revisionen, Schnitt, Drahtgitter, Zoom, orthografischer Ansicht, Punktmessung mit Markierungen, Vorher-/Nachher-Überlagerung, Schutz-/Änderungsregionen, Maßstabsbalken, Diagnosekanälen und Pixel-LOD.

Die aktuelle Liste kommt aus `cad_capabilities`. Eine Fertigungszertifizierung, GPU-Auswertung oder eine allgemeine OCAF-Flächenwiederauflösung werden nicht als verfügbar ausgegeben.

## Prüfbarer Beispielablauf

```bash
npm run demo
```

Den Server dafür zunächst stoppen: Ein Datenverzeichnis hat genau einen aktiven Dienst- oder Wartungsprozess. Der Befehl baut das Gehäuse auf, prüft und übernimmt die Basis, verändert die Nut von **0,80 auf 0,82 mm**, prüft erneut und schreibt den STEP-Roundtrip nach `reports/demo/`. Er erzeugt jeweils ein eigenes Modell.

Gemessen werden unter anderem **1,20 mm Nutbreite** und **2,18 mm Restwand in der deklarierten ebenen Boxzone**. Die ursprüngliche Revision bleibt erhalten. Der Bericht nennt die tatsächliche Abdeckung; die lokale Restwandprüfung ist keine globale Wandstärkenzertifizierung.

## MCP-Anbindung

Endpunkt: `POST /mcp`. Der lokale Entwicklungsschlüssel wird als `Authorization: Bearer …` übertragen. Für Remote-Nutzung ist der OAuth-Modus vorgesehen. Werkzeug-Argumente enthalten keine frei wählbare Nutzer-/Mandantenidentität.

Getrennt implementiert und lokal getestet:

| Protokoll | Adapter |
|---|---|
| `2025-03-26`, `2025-06-18`, `2025-11-25` | Offizielles TypeScript-SDK 1.30.0 mit `initialize`, Versionsaushandlung und Streamable HTTP |
| `2026-07-28` | Separater Adapter mit `server/discover`, erforderlichen Anfrage-Metadaten, Header-Abgleich und `resultType` |

Alle 23 Werkzeuge sind in [Schnittstellen](docs/api.md) beschrieben. Zusätzlich zum HTTP-Endpunkt ist der lokale stdio-Transport implementiert und mit Wiederverbindung getestet. Die [Kompatibilitätsmatrix](docs/compatibility-matrix.md) trennt Protokolltests von noch nicht ausgeführten Hosttests.

## Entwicklung und Betrieb

```bash
npm run verify          # TypeScript, Integration, native Geometrie, MCP-Ablauf, Build, Chromium
npm run test:llm        # Zwölf echte Sprachaufträge im installierten Codex-Host; Modellnutzung
npm run benchmark       # Gemessene lokale Latenzen und Ressourcen
npm run schemas         # JSON-Schemas und Operatorregister aus dem Quellcode
npm run format:check
npm run release:check   # Exit 2, solange Produktionsnachweise fehlen
```

Die Ergebnisse stehen in `reports/`; insbesondere `verification.json`, `benchmark.json`, `release-manifest.json` und `demo/report.json`. [Architektur](docs/architecture.md), [Mathematikverträge](docs/mathematical-contracts.md), [Intervallzertifikate](docs/intervals-and-certificates.md), [Messungen](docs/measures.md), [Importe](docs/imports.md), [Viewer](docs/viewer.md), [Bedrohungsmodell](docs/threat-model.md) und [Betriebsanleitung](docs/operating-runbook.md) beschreiben Grenzen und Wiederherstellung. `tsx scripts/minimize-fixture.ts <fixture.json> --expect <CODE>` verkleinert ein fehlschlagendes IR auf eine minimale Reproduktion; `tests/regression/corpus.ts` hält die geometrischen Grenzklassen mit deklarierten Erwartungen.

Die lokale systemd-Installation ist aktiv. Weitere systemd- und HTTPS-Proxy-Vorlagen liegen unter `deployment/`. Ein öffentlicher Dienst wurde nicht veröffentlicht.
