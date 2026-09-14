# Architektur

Der ursprüngliche Bauplan bleibt unverändert im Repository. Diese Datei beschreibt den tatsächlich gebauten Stand.

```mermaid
flowchart TD
  V[Browser-Viewer] --> G[HTTP/MCP-Gateway]
  H[ChatGPT oder MCP-Client] --> G
  G --> A[Identität und Objekt-ACL]
  A --> M[Modellservice und Pflicht-Gates]
  M --> C[IR, Einheiten, Ausdrücke, DAG-Compiler]
  M --> D[(SQLite: Revisionen, Jobs, Audit, Outbox)]
  C --> J[Persistente Job-Queue mit Fencing]
  J --> W[Bubblewrap: OCCT und analytische Felder]
  W --> B[(Inhaltsadressierte private Blobs)]
  W --> P[Serverseitige Validierung]
  P --> K[Compare-and-Swap-Commit]
  K --> D
  B --> V
```

## Zustände und Atomarität

Ein neues Modell enthält eine leere Entwurfsrevision. `cad_apply_patch` kompiliert einen begrenzten Änderungsgraphen und schreibt Transaktion, Job, Idempotenzresultat und Dispatch-Outbox in derselben SQLite-Transaktion. Ein Worker erzeugt eine unveränderliche Kandidatenrevision. Sie ist lesbar, wird aber nicht zum aktuellen Modellzeiger.

`cad_validate` erzeugt einen serverseitigen Bericht. Der Digest bindet IR, Geometriefakten, Kandidatenrevision, Operatorregister, native Quellen und Policy. `cad_commit` prüft Rechte erneut und verlangt die noch aktuelle Basisrevision. Neue Revision, Modellzeiger, Audit und Commit-Outbox werden gemeinsam übernommen. Ein erneuter identischer Request liefert die bereits gespeicherte Wirkung.

Ein verlorener Worker-Lease darf nach Ablauf erneut ausgeführt werden. Ergebnisse werden nur angenommen, wenn Jobzustand und Fencing-Token weiterhin passen. Abbruch und Verwerfen setzen den Zustand vor einer möglichen späten Worker-Antwort. `after_commit` läuft über die Outbox; sein Ausfall macht einen vorhandenen Commit nicht rückgängig.

## Modulgrenzen

| Pfad | Verantwortung |
|---|---|
| `packages/semantic-ir` | Strikte Schemas, Dezimalgrößen, sichere Fehler, kanonische Hashes |
| `packages/compiler` | Operatorverträge, AST-Grenzen, Formeln, Abhängigkeiten, Dirty-Graph, Mathematik |
| `packages/policy` | Scopes und Eigentümerbindung |
| `packages/model-service` | Revisionen, Auswahl, Import/Export, Blobs, Backup/Restore |
| `packages/job-service` | Dauerhafte Queue, Fencing, Wiederanlauf und native Prozessgrenze |
| `packages/validation` | Ausgeführte Geometrie-/Parameterprüfungen und Schutzbedingungen |
| `packages/mcp-gateway` | Authentifizierung, Versionsadapter, HTTP und Tool-Annotationen |
| `workers/cad-occt` | OCCT-Bindings und analytischer Feldkern im isolierten Prozess |
| `packages/viewer` | Abgeleitete Three.js-Ansicht; sämtliche Änderungen über dieselben Tools |

## Bewusste technische Abweichungen

OCCT wird über die passenden Python-Bindings statt eines eigenen C++-RPC-Wrappers angesprochen. Der Geometriekern selbst ist nativ. SQLite/WAL und ein lokaler Objektspeicher ersetzen einen verteilten Datenbank-/Queue-Aufbau. Das ermöglicht die atomare Übernahme ohne zusätzliche Dienste.

Für organische Vorschauen wird ein eigener sparsamer Octree mit Lipschitz-Ausschluss und Marching Tetrahedra verwendet. Ein begrenzter OpenVDB-Exportadapter prüft alle gespeicherten Float32-Feldsamples nach dem Zurücklesen. Private Samples werden über Auflösungen und kompakte lokale Änderungen wiederverwendet. VDB-Import ist nicht registriert. Die CGAL-basierten zertifizierenden Meshprüfungen fehlen ebenfalls; deshalb existiert kein freigegebenes `watertight_solid`- oder `manufacturing_candidate`-Profil.

Ein Worker verarbeitet jeweils einen Job. Die Eingangs- und Ergebnisdaten sind begrenzt, die Quellen unveränderlich eingebunden und Cacheeinträge mandantenbezogen. Der native Prozess wird neu gestartet; das ist ein kalter Prozess mit gegebenenfalls warmem Geometriecache.
