# MCP und Werkzeugverträge

Die Eingabeschemas unter `schemas/cad_*.schema.json` und die werkzeugspezifischen Ausgänge unter `schemas/cad_*.result.schema.json` werden aus denselben strikten Zod-Verträgen wie die Laufzeitprüfung erzeugt. Antworten tragen `result_schema_version: "1"`; nicht belegte Bindungen sind ausdrücklich `null`. Unbekannte Felder werden abgelehnt. Status und Fehler bleiben maschinenlesbar; interne Pfade, Tokens und Stacktraces werden nicht in Toolfehlern zurückgegeben. [Antwortverträge und Nachweisversionen](result-contracts.md) beschreiben Metadatenfelder, Ressourcenbudgets und die unveränderte Lesbarkeit älterer Datensätze.

## Werkzeuge

| Werkzeug | Ergebnis und Wirkung |
|---|---|
| `cad_capabilities` | Tatsächliche Operatoren, Formate, Limits und Grenzen |
| `cad_list_models` | Eigene und ausdrücklich freigegebene Modelle nach Name/Zweck suchen und seitenweise lesen; keine bekannten Modell-IDs oder Viewer-Bedienung nötig |
| `cad_access` | Aktuelle Rolle, Feature-Grenze und Budget lesen; als Eigentümer gebundene Grant-/Widerrufsanträge erstellen und ihren Status lesen; gesonderte vertrauenswürdige Bestätigung erforderlich |
| `cad_create_model` | Privates leeres Modell; eigener Idempotenzschlüssel |
| `cad_get_model` | Revisionsübersicht mit Buildkompatibilität und Seiten von höchstens 64 Features |
| `cad_structure` | Versionierte Projekt-, Baugruppen-, Teil- und Rahmenpakete mit Suche, Paging, Definitionen, Ausdehnungen und Strukturhash |
| `cad_find` | Semantische/räumliche Kandidaten und kurzlebige Auswahlhandles |
| `cad_inspect` | Feature, Konstruktion, Maße, Schutzregeln, native Flächen mit Paging, Auswahlhandle und ausdrückliche Neuzuordnung |
| `cad_measure` | Vorliegende Messwerte; Abstand zwischen zwei B-Reps als Job |
| `cad_plan_edit` | Kompilierter Eingriff, gelöste Parameter, Abhängigkeiten und Budget |
| `cad_solve_constraints` | Begrenzte gekoppelte Maßlösung; Job liefert Patchvorschläge und gespeicherte Gleichungsconstraints, danach regulär prüfen/übernehmen |
| `cad_apply_patch` | Transaktion und isolierter Berechnungsjob; kein Commit |
| `cad_validate` | Vollständiger Pflichtprofiljob für den Kandidaten |
| `cad_compare` | Geänderte Merkmale und deklarierte Messdifferenzen |
| `cad_commit` | Compare-and-Swap mit gebundenem Prüfdigest |
| `cad_discard` | Nicht übernommenen Kandidaten verwerfen |
| `cad_revert` | Frühere Konstruktion als neu zu prüfender Kandidat |
| `cad_rebuild` | Unveränderte Konstruktion ausdrücklich für den Ziel-Build planen und als neu zu prüfenden Kandidaten berechnen |
| `cad_render` | Autorisiertes abgeleitetes Vorschauartefakt |
| `cad_import` | Eigenes hochgeladenes Artefakt in einen Kandidaten einlesen |
| `cad_export` | Geprüfte Revision mit Format-/Roundtrip-Bericht exportieren |
| `cad_job_get` | Dauerhafter Jobstatus und Ergebnis |
| `cad_job_cancel` | Fachlicher Jobabbruch mit Fencing |

Alle mutierenden Modelloperationen verlangen eine exakte Basisrevision und einen 16–128 Zeichen langen Idempotenzschlüssel. Ein Schlüssel darf pro Eigentümer nur für dieselbe normalisierte Operation wiederverwendet werden. Für Planen, Anwenden, Validieren und Committen jeweils unterschiedliche Schlüssel verwenden.

Projektrollen, Budgetreservierungen, Ablauf, Widerruf und den gesonderten Policy-Pfad beschreibt [Projektfreigaben](project-access.md). Die Feature-Grenze betrifft Änderungen; Leserechte umfassen das gesamte Modell einschließlich Historie.

## Ablauf

Der primäre Bedienweg ist das LLM über MCP. Der Viewer ist optional. Das LLM kann alle folgenden Schritte selbst ausführen und liest vorhandene Modelle zuvor mit `cad_list_models`. Identitäten und Rechte stammen aus dem Transport, niemals aus Werkzeugargumenten. Der lokale stdio-Einstieg benötigt keine interaktive Anmeldung.

1. `cad_create_model`, anschließend Features mit `cad_apply_patch` hinzufügen oder eine eigene hochgeladene Datei mit `cad_import` einlesen.
2. `cad_job_get` abfragen, bis `succeeded`, `failed` oder `cancelled` vorliegt. Eine erfolgreiche Berechnung liefert `candidate_revision` und `transaction_id`.
3. `cad_validate` mit Modell, Basisrevision und Transaktions-ID; erneut Job abfragen.
4. Nur bei `checks_passed_within_profile`: `cad_commit` mit exakt dem zurückgegebenen `validation_digest`.
5. Die neue maßgebliche Revision für spätere Bearbeitung, Messung oder Export verwenden.

Patchvarianten: `add_feature`, `set_outputs`, `add_constraint`, `set_parameter`, `set_expression`, `solve_volume`, `set_surface_poles`, `insert_surface_knots`, `set_field`, `set_construction`, `set_pattern_occurrence`, `set_structure`, `set_feature_context`. Es gibt keine beliebigen Payloads oder ausführbaren Programme.

Projekt-, Baugruppen- und Teilnamen werden mit `cad_structure` aufgelöst. `kind` wählt die Ebene, `query` die semantischen Suchwörter; `entity_id` begrenzt optional auf ein bekanntes Element. Für Features innerhalb eines gefundenen Teils dient `cad_find.owner_part`. `set_structure` bindet an den zurückgegebenen `structure_hash`; `set_feature_context` bindet an `cad_inspect.context_hash`. Struktur- und Kontextänderungen können im selben Kandidatenpatch stehen. Definierte Teilausgaben und Modellausgaben müssen zusammenpassen. Details zu Koordinaten und Cachegrenzen stehen in `docs/structure-and-frames.md`.

## Übergang auf einen neuen Build

`cad_get_model.build_compatibility` meldet `current`, `rebuild_required` oder `unsupported_construction`. Bei `rebuild_required` den aktuellen `registry_hash` aus `cad_capabilities` lesen. `cad_rebuild` erwartet Modell, exakte Basisrevision, Idempotenzschlüssel und `target_registry_hash`. Der Standardmodus `plan` liefert Aufwand, IR-Hashes und Schutzbedingungen ohne Kandidat. `mode: "candidate"` mit eigenem Schlüssel berechnet anschließend die unveränderte Konstruktion. Alte Revisionen bleiben erhalten; alle Schutzbedingungen gelten weiterhin. Kandidaten müssen regulär geprüft und übernommen werden. Ein veralteter Ziel-Hash wird abgewiesen. `unsupported_construction` verlangt eine gesonderte Migration der nicht mehr kompilierbaren Konstruktion.

## Native Flächen und LLM-Auswahl

`cad_inspect` liefert `face_page` mit `geometry_feature_id`, gemessenen Flächendaten, `origins`, `face_id` und `next_offset`. `face_offset` und `face_limit` (maximal 16) begrenzen die Antwort. Flächen können vollständig über diese Daten abgefragt werden; ein Viewer-Klick ist keine Voraussetzung.

Zum Binden einer Fläche `model_id`, **ausdrückliche** `revision`, die ID des angezeigten Geometriefeatures und `face_id` angeben. `selected_entities` bezeichnet das eindeutig belegte erzeugende Merkmal; `selected_face.geometry_feature_id` bezeichnet dessen resultierende Geometrie. Das zurückgegebene `selection_handle` kann einen Patch ausschließlich auf dieses Merkmal begrenzen. Globale `set_outputs`-/Hinzufügeoperationen sind mit einem solchen Handle nicht zulässig.

Für die Weiterverwendung nach einer Änderung `selection_handle`, die ausdrückliche neue `revision` und `rebind: true` an `cad_inspect` senden. Nur ein eindeutiger Nachfolger in **jeder** Zwischenrevision erzeugt ein neues Handle. Ein geteiltes, zusammengeführtes, entferntes, fremdes oder abgelaufenes Ziel wird nicht still ersetzt. Neue Flächen-IDs sind revisionsbezogene Adressen, keine über Revisionen stabilen Namen. Unbekannte Herkunft wird mit `AMBIGUOUS_SELECTION` gemeldet; das LLM kann dann über `cad_find` das gemeinte Merkmal ermitteln oder eine inhaltliche Rückfrage stellen.

## HTTP-Beispiel für 2026-07-28

```http
POST /mcp
Authorization: Bearer <Zugriffstoken>
Content-Type: application/json
Accept: application/json, text/event-stream
Mcp-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: cad_capabilities

{
  "jsonrpc":"2.0",
  "id":1,
  "method":"tools/call",
  "params":{
    "name":"cad_capabilities",
    "arguments":{},
    "_meta":{
      "io.modelcontextprotocol/protocolVersion":"2026-07-28",
      "io.modelcontextprotocol/clientCapabilities":{},
      "io.modelcontextprotocol/clientInfo":{"name":"my-client","version":"1"}
    }
  }
}
```

Dieser Adapter setzt keine ältere Initialisierung voraus. Der SDK-Adapter unterstützt `2025-03-26`, `2025-06-18` und `2025-11-25` über `initialize`. Für andere angefragte Fassungen bietet er die getestete Fassung `2025-11-25` an. Dies ist Versionsaushandlung innerhalb des älteren Initialisierungsablaufs; der Adapter für `2026-07-28` bleibt getrennt.

## Ressourcen und Dateien

Logische Ressourcen sind `cad://models/{model_id}/revisions/{revision}/summary`, `…/features/{feature_id}`, `cad://transactions/{transaction_id}/validation` und `cad://artifacts/{artifact_id}/manifest`. Jeder Zugriff verlangt Authentifizierung und Objektberechtigung.

Der lokale Viewer lädt Dateien über `POST /api/uploads` mit `application/octet-stream` hoch. Die Antwort enthält die servergenerierte Artefakt-ID. `GET /api/artifacts/{artifact_id}` liefert nur autorisierte Inhalte. Es gibt keine frei abrufbare Hashadresse und keine automatische externe Veröffentlichung.

Toolresultate sind auf 32 KiB begrenzt. Vollständige Validierungsberichte werden über ihre Ressourcen-URI gelesen; Geometrien bleiben im Artefaktspeicher. Ein Transportabbruch widerruft keinen bereits angenommenen dauerhaften Job. Dazu dient `cad_job_cancel`.

Antwortform, endliche Zahlen, Zielbindung und Budget werden vor der Speicherung schreibender Ergebnisse geprüft. Ein `OUTPUT_CONTRACT_VIOLATION` oder überschrittenes Antwortbudget rollt die zugehörige SQL-Transaktion einschließlich des neuen Idempotenzdatensatzes zurück. Die Anfrage kann nach Behebung der Ursache mit demselben Schlüssel wiederholt werden. Neue Prüfnachweise verwenden Version 2 mit Kandidaten-, Engine- und Geometriebindung pro Einzelcheck. Der bei `cad_job_get` gelieferte Digest gehört zum vollständigen Ressourcenbericht, auch wenn nur die ersten fehlgeschlagenen Checks angezeigt werden.
