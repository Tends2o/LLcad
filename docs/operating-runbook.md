# Betrieb und Wiederherstellung

## Lokaler Betrieb

Auf diesem Rechner sind `llcad.service` und die lokale Codex-Verbindung bereits eingerichtet. Der Dienst läuft dauerhaft auf Loopback; mehrere LLM-Verbindungen teilen ihn. Zugangsdaten werden automatisch gelesen. Konkrete Pfade und Hostprüfungen stehen in [Lokaler Codex-Betrieb](local-codex.md).

`npm start` startet den gebauten Dienst auf `127.0.0.1:4310`. `npm run dev` startet TypeScript direkt. Daten liegen standardmäßig in `data/`. Der lokale Schlüssel liegt mit Dateimodus 0600 in `data/local-token`; er gehört nicht ins Repository oder in Logs.

`MATHFORGE_DATA`, `MATHFORGE_ROOT`, `HOST` und `PORT` sind konfigurierbar. Lokale Authentifizierung darf nur an Loopback lauschen. Neue Schlüssel entstehen, wenn der Betreiber den Dienst stoppt, die Schlüsseldatei ersetzt und erneut startet; bestehende Browsercookies sind dann ungültig.

Ein Datenverzeichnis wird durch eine lebenslang gehaltene `flock`-Sperre exklusiv geöffnet. Zweiter Server, Demo oder Offline-Wartungsprozess erhalten `STORE_BUSY`. Vor Offline-Wartung den Dienst stoppen; nach einem Prozessabsturz gibt das Betriebssystem die Sperre frei. Die Datei `.store.lock` bleibt absichtlich bestehen und darf bei laufendem Dienst nicht ersetzt oder gelöscht werden. Datenbankschemata 1 und 2 werden beim Öffnen additiv auf Schema 3 erweitert (dauerhafte Scheduler-Reihenfolge und Flächenbindungen). Unbekannte Versionen werden ohne Änderung abgewiesen.

Für einen lokalen LLM-Host steht `deployment/start-mcp.sh` als stdio-Startkommando bereit. Der Host startet und beendet den Prozess; es gibt keine Browseranmeldung und keinen Netzwerklistener. Derselbe private lokale Nutzer wie im lokalen HTTP-Modus wird verwendet. `MATHFORGE_DATA` kann ein eigenes Datenverzeichnis festlegen. Nur vertrauenswürdige lokale Prozesse dürfen diesen Einstieg starten; eine Remote-HTTP-Verbindung benötigt weiterhin die vorhandene Authentifizierung.

Die Flächenhistorie wird zusammen mit jedem B-Rep als privater, gehashter Blob gespeichert. Cache-Wiederverwendung prüft die Fingerprints sämtlicher Flächen. Backup, Restore, GC und Mandantenlöschung erfassen diese Blobs über dieselben Revisions-/Cachebezüge. Das Löschen einer Auswahl entfernt ihre Flächenbindung über einen Fremdschlüssel.

Die Jobqueue arbeitet dauerhaft reihum zwischen wartenden Nutzern und innerhalb eines Nutzers in Eingangsreihenfolge. Der Schedulerzustand überlebt einen Neustart. Es läuft bewusst ein isolierter Worker gleichzeitig; ein Pool oder eine Zusicherung von Laufzeitfairness bei unterschiedlich langen Jobs ist damit nicht verbunden.

## Remote-Bereitstellung vorbereiten

1. Code und gesperrte Abhängigkeiten unter `/opt/mathforge` installieren; dedizierten Nutzer `mathforge` und privates Datenverzeichnis `/var/lib/mathforge` anlegen.
2. `deployment/environment.example` als Betreiberkonfiguration übernehmen und alle Beispieladressen durch tatsächliche Werte ersetzen. Keine echten Schlüssel im Repository speichern.
3. `MATHFORGE_AUTH=oauth` aktivieren. Der IdP muss signierte Tokens mit `sub`, `tenant_id`, `scope`, `iat`, `exp` und der exakt konfigurierten Audience ausstellen. JWKS und Issuer sind fest konfigurierte HTTPS-Adressen.
4. Systemd-Vorlage an die tatsächliche Node-Installation anpassen. Sie erwartet standardmäßig `/usr/bin/node`; in dieser Entwicklungsumgebung liegt Node unter nvm. Der Vorlagenpfad ist daher hier nicht als installierter Dienst ausführbar.
5. Den HTTPS-Proxy aus `deployment/Caddyfile` mit eigener Domain konfigurieren. Er muss den ursprünglichen öffentlichen Host erhalten. Der Gateway-Port bleibt an Loopback gebunden.
6. Bubblewrap unter dem echten Servicekonto prüfen. AppArmor-/User-Namespace-Vorgaben der Zielmaschine können den Start verhindern. Diese Vorgaben nicht ungeprüft global deaktivieren; der Dienst scheitert geschlossen.
7. `npm run verify`, Benchmarks und den echten Hosttest ausführen. Erst danach die Betreiberfreigabe erteilen.

Die Systemd-Vorlage begrenzt Speicher, Tasks, Schreibpfade und Privilegien. Sie ist eine Installationsvorlage; weder Systemdienst noch HTTPS-Proxy wurden in dieser Sitzung öffentlich installiert.

## Backup und Restore

```bash
npx tsx scripts/maintenance.ts backup data /sicherer/pfad/backup-2026-09-13
npx tsx scripts/maintenance.ts restore /sicherer/pfad/backup-2026-09-13 /neues/datenverzeichnis
```

Beide Ziele müssen neu sein. Den Dienst zuerst stoppen. Die Datenbank wird mit der SQLite-Backup-API gesichert. Alle Blobs werden vor dem Restore gegen Manifest und Dateigröße geprüft; die Datenbank besteht zusätzlich `PRAGMA integrity_check`. Das Ausgangsverzeichnis wird nicht überschrieben.

Der automatisierte Restore-Test erstellt eine echte zweite Datenbank, liest daraus die ursprüngliche Revision und kontrolliert ihre Blobs. Ein absichtlich beschädigter Backup-Blob wird abgewiesen.

## Ausfälle

Mit `MATHFORGE_DEBUG_ERRORS=1` schreibt der Dienst bei fehlgeschlagenen Jobs die interne Ausnahme und die letzten 2000 Zeichen des nativen Worker-stderr nach stderr. Diese Diagnose wird nie gespeichert und nie an Clients zurückgegeben, weil sie importierte Daten enthalten kann; sie ist nur für die Betreiberanalyse gedacht. `MATHFORGE_WORKERS` (1–4) begrenzt parallele Sandboxes, `MATHFORGE_WARM_WORKERS` (0–2) den vorgewärmten Pool.

- **Worker beendet:** Jobstatus enthält eine sichere Diagnose; die maßgebliche Revision bleibt erhalten. Jeder Worker besitzt eine eigene Prozessgruppe, die bei Abbruch, Timeout und Prozessende bereinigt wird. Auch ein Absturz während des Bubblewrap-Starts darf keinen geerbten stderr-Kanal offen halten und die Queue blockieren. Bei verlorenem Prozess mit gültigem Lease erfolgt der Wiederanlauf nach dessen Ablauf.
- **Job dauert zu lange:** `cad_job_cancel` stoppt den Prozess. Ein Budgetfehler verändert keine Toleranz. Kleinere Domäne oder weniger Instanzen explizit wählen.
- **Stale Revision:** Aktuelle Revision erneut lesen, Änderung neu planen und neuen Kandidaten prüfen. Kein stilles Rebase.
- **`BUILD_MISMATCH`:** Der gespeicherte Kandidat oder die Revision gehört zu einem anderen Compiler-/Worker-Build. Für eine weiterhin kompilierbare maßgebliche Revision `cad_rebuild` im Planmodus mit dem aktuellen Registry-Hash aufrufen. Danach einen ausdrücklichen Kandidaten berechnen, Messwerte vergleichen, prüfen und als neue Revision übernehmen. Alte Fakten und IR bleiben erhalten. Veraltete unübernommene Kandidaten verwerfen und aus der aktuellen Basis neu planen. Eine nicht mehr kompilierbare Konstruktion benötigt weiterhin eine gesonderte Schema-/Operatormigration.
- **Validierung fehlgeschlagen:** Bericht über `cad://transactions/{id}/validation` lesen. Kandidat korrigieren oder verwerfen; keine erfundenen Prüffelder senden.
- **`SANDBOX_UNAVAILABLE`:** Bubblewrap, User-Namespaces und Bind-Mountrechte unter dem Dienstkonto untersuchen. Keine unsandboxed-Ausweichoption aktivieren.
- **OAuth abgewiesen:** Issuer, Audience, Zeit, Scopes und signierte Mandantenclaims prüfen. Tokens nicht zur Fehlersuche protokollieren.
- **Commit-Outbox blockiert:** Pflicht-Hook reparieren und Dienst weiterlaufen lassen. Die bereits übernommene Revision bleibt gültig; die Outbox wird wiederholt verarbeitet.

## Aufbewahrung

```bash
npx tsx scripts/maintenance.ts gc data
```

Die GC ist ein Offline-Betreiberbefehl. Sie löscht ausschließlich unreferenzierte Blobs ab sieben Tagen Alter. Aktuelle Revisionen, Kandidaten, Cachebezüge und Exportartefakte bleiben erhalten.

Mandantenbezogene Löschung des aktiven Datenspeichers, nach Stoppen des Dienstes:

```bash
npx tsx scripts/maintenance.ts erase-tenant data MANDANT
npx tsx scripts/maintenance.ts erase-tenant data MANDANT --apply
```

Der erste Aufruf zeigt die betroffenen Objektzahlen; der zweite führt die Löschung aus. Entfernt werden Modelle, Revisionen, Kandidaten, Jobs, Auswahlen, Uploads, Exporte, Cacheeinträge, Idempotenzdaten, Schedulerzustand und zugeordnete Outboxereignisse. Anschließend werden sämtliche verwaisten Blobs entfernt; von anderen Mandanten weiter referenzierte Inhalte bleiben bestehen. SQLite erhält `secure_delete`, WAL-Checkpoint und `VACUUM`.

Zugeordnete Auditereignisse werden redigiert; die Reihenfolge und ursprünglichen Zeiten der verbleibenden Ereignisse bleiben erhalten. Ihre Hashkette wird neu aufgebaut. Das Ereignis `retention_redaction` hält den vorherigen Kettenabschluss und die Zahl entfernter Ereignisse fest. Dieser ausdrücklich protokollierte Wechsel ist bei extern gespeicherten Auditankern zu berücksichtigen.

**Backups, Dateisystemsnapshots und bereits heruntergeladene Kopien bleiben außerhalb dieses Befehls.** Der Betreiber muss sie im Löschinventar berücksichtigen, nach der geltenden Aufbewahrungsfrist entfernen und bis dahin gegen Rücksicherung sperren. Der Ergebnisbericht nennt diese Grenze. Eine forensische Löschgarantie für SSDs oder externe Speicherdienste wird nicht gegeben. Tests belegen die aktive Löschung und den Erhalt eines zweiten Mandanten einschließlich gemeinsam referenzierter Geometrie.

## Release-Manifest

`npm run release:check` schreibt `reports/release-manifest.json`. Exitcode 2 bedeutet offene Abnahmekriterien. Aktuelle Build-/Registry-/Dependency-Hashes, Tests und Benchmark müssen übereinstimmen. Ein zusätzlicher Implementierungsdigest bindet die Nachweise an Gateway, Viewer, Jobs, Tests und Deploymentcode. Ein fehlender Zielhosttest oder eine fehlende HTTPS-/IdP-Konfiguration wird nicht mit einem Platzhaltererfolg ersetzt.
