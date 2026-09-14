# Bedrohungsmodell

Geschützt werden private Modelldaten, ursprüngliche Revisionen, Identitäten, Rechenbudgets und Hostdateien. Modelltexte, Dateinamen, Kommentare, Formeln und importierte Geometrien gelten als untrusted input.

## Erzwungene Grenzen

| Risiko | Implementierung | Prüfung |
|---|---|---|
| Beliebige Codeausführung | Strikter AST, feste Operatoren, kein dynamisches Laden | Schema-, Ausdrucks- und Injectiontests |
| Modell-/Mandantenübergriff | Geprüfte Identität plus Mandant und Eigentümer an jedem Objekt | Modell-, Job-, Artefakt-, Ressourcen- und Importtests |
| Gefälschte Freigabe | Serverseitiger Prüfdigest und unmittelbare CAS-Prüfung | Fehlender/falscher Digest, stale Revision, fehlendes Gate |
| Native Parserfehler | Bubblewrap, separater Prozess, feste Mounts, kein Netzwerk, CPU/RAM/Dateilimits | Echter Worker-Abbruch und Wiederanlauf |
| Hostdateizugriff | Nur Systembibliotheken, Workerquellen, venv und Jobverzeichnis eingebunden | Private Verzeichnisse gehören nicht zum Mountsatz |
| Symlink-Ausbruch bei Ergebnissen | Nur bekannte Ausgabedateinamen, `lstat` vor Abruf, keine Symlinks | Worker-Ergebnisvalidierung |
| Wiederholte Requests | Atomare, eigentümerbezogene Idempotenz | Zwölf gleichzeitige identische Aufrufe erzeugen einen Job |
| Späte Queue-Antworten | Fencing-Lease und Zustandsprüfung vor Veröffentlichung | Ablauf-, Abbruch- und Wiederherstellungstests |
| SSRF | Keine beliebigen Download-URLs; externe STEP-Referenzen werden abgelehnt | Importvorgaben und kein Netz im Worker |
| Browserzugriffe | Exakter Host/Origin, HttpOnly-SameSite-Cookie, CSRF-Header, CSP | Rebinding-, Origin- und Cookie-Tests |
| Falsche OAuth-Identität | jose-Prüfung mit erlaubten Algorithmen, JWKS, Issuer/Audience/Ablauf | Tatsächlich signierte positive und negative JWT-Tests |
| Ressourcenerschöpfung | Eingangs-/Ausgangsgrenzen, Feature-/Zell-/Instanz-/Dreieckslimits, begrenzte Queue | Compiler-, Worker- und Budgettests |

Im lokalen Modus gibt es genau einen lokalen Eigentümer. OAuth nimmt den Mandanten ausschließlich aus dem signierten `tenant_id`-Claim und den Nutzer aus `sub`. Ein fremder Nutzer desselben Mandanten erhält ebenfalls keinen Objektzugriff. Projektfreigaben mit mehreren Rollen sind noch nicht implementiert.

## Workerprofil

Unprivilegierte UID/GID 65534, eigene Namespaces, `--unshare-all`, `--cap-drop ALL`, keine Host-Credentials, temporärer Jobbereich und schreibgeschützte System-/Quellenmounts. Ein fehlender Bubblewrap-Schutz erzeugt `SANDBOX_UNAVAILABLE`; es gibt keinen automatischen ungeschützten Rückfall.

Der native Prozess ist auf 40/42 CPU-Sekunden, 45 Sekunden Wallclock, 1536 MiB virtuellen Speicher, 34 MiB pro Datei und 128 offene Dateien begrenzt. Die Geometriequeue verwendet einen Worker je Dienstprozess. Ein laufender Job wird nach einem Absturz erst nach seinem gefenceten Lease erneut ausgeführt.

Für hochriskante fremde Parserdaten kann der Betreiber zusätzlich eine VM-Grenze benötigen. Die vorliegenden Tests stellen keinen Nachweis gegen sämtliche Kernel-/Container-Escapes dar.

## Daten und Lebenszyklus

Keine Modellgeometrie wird automatisch an LLM-Anbieter oder externe Empfänger versendet. Exporte bleiben im privaten Speicher. Es gibt weder Veröffentlichungstool noch Maschinensteuerung. Logs/Trace-IDs enthalten keine Zugangstokens; native Fehlermeldungen werden in sichere öffentliche Diagnosen übersetzt.

Backups enthalten Datenbank und überprüfte Blobs, aber keine lokalen Zugangsschlüssel. `gc` entfernt nur unreferenzierte Blobs nach der Aufbewahrungsfrist. Rechtliche Löschkonzepte, selektive Eigentümerlöschung, Backupablauffristen und eine vollständige Betreiber-Lizenzprüfung müssen vor produktiver Nutzung ergänzt bzw. festgelegt werden.
