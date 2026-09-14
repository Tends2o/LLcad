# Lokaler Codex-Betrieb

Auf diesem Rechner ist LLcad als `llcad.service` aktiviert. Der Dienst startet beim Booten und verwaltet `/root/projekte/LLcad/data`. Der vorhandene lokale Codex wurde um den MCP-Eintrag `llcad` ergänzt; andere Einstellungen wurden erhalten und vor der Ergänzung privat gesichert.

Der Endpunkt `http://127.0.0.1:4310/mcp` ist nur lokal erreichbar. Codex liest den Schlüssel automatisch über `deployment/local-headers.py`. Der Schlüssel bleibt in `data/local-token` mit privaten Dateirechten und steht weder in der Codex-Konfiguration noch in Prüfberichten. Mehrere Codex-Verbindungen verwenden denselben Dienst. Ein zweiter stdio-Server darf dieses Datenverzeichnis nicht öffnen.

Die Modellierung braucht keine Anmeldung im Viewer, keine Klicks und keine vom Nutzer gesuchten IDs. Das LLM findet Modelle und Features über die CAD-Werkzeuge. Bereits laufende Hosts haben ihren Werkzeugkatalog beim Start geladen; neue Hosts lesen den eingetragenen Server. Für die laufende Entwicklungsarbeit kann der Assistent die Werkzeuge über `scripts/codex-host-client.ts` beim tatsächlichen App-Server aufrufen, ohne einen weiteren LLM-Agenten zu starten.

## Ausgeführter Hosttest

`npm run test:host` verwendet die installierte Codex-Version und deren echte MCP-Anbindung. Zwei gleichzeitige Verbindungen entdecken die Werkzeuge; eine erstellt ein Gehäuse, findet die Dichtungsnut, ändert deren Tiefe von 0,80 auf 0,82 mm, prüft, übernimmt und exportiert STEP. Die zweite Verbindung liest dieselbe neue Revision. Das Beispiel bleibt zur Nachprüfung im lokalen Datenspeicher.

Der Bericht `reports/codex-host.json` enthält Hostversion, Buildbindung, Werkzeugaufrufe und Messergebnisse. `reports/codex-host-model.step` enthält den geprüften Export. Der Test beginnt **keinen Modellturn** und belegt damit Transport und Ausführung im echten Host, aber keine Bewertung selbstständig vom LLM entwickelter Konstruktionen. `npm run test:llm` startet dagegen tatsächliche Modellturns in einer temporären Testumgebung: drei Sprachaufträge mit numerischem Grader und Aufzeichnung tatsächlicher CAD-Aufrufe. Nur synthetische Testmodelle werden verwendet. Die lokalen Einstellungen für `cad_commit`, `cad_discard` und `cad_job_cancel` sind auf `approval_mode = "approve"` gesetzt, entsprechend dem Auftrag, ohne zusätzliche Klicks zu arbeiten. Die serverseitigen Rechte, Schutzbedingungen und Prüfdigests gelten unverändert. Der Test verwendet diese gespeicherten Werkzeugfreigaben; nur Endpunkt und Zugang werden auf den privaten Testdienst umgestellt. Der Code-Mode-Host bleibt für MCP verfügbar. Ein ChatGPT-Web-Connector sowie die Remote-OAuth-Abnahme sind getrennte Aufgaben.

## Wartung durch den Assistenten

Nach Quellcodeänderungen erst `npm run verify` abschließen, dann `systemctl restart llcad.service` und `npm run test:host` ausführen. Der Hosttest vergleicht den Implementierungshash des laufenden Dienstes mit dem Quellstand. Der Dienststart allein ist kein Geometrienachweis: Der isolierte Worker muss im tatsächlichen systemd-Kontext rechnen können. Die Vorlage erhält `NoNewPrivileges` und die eigene Bubblewrap-Isolation; `RestrictSUIDSGID` wird wegen eines hier reproduzierten inkompatiblen Syscallfilters nicht gesetzt.

Bei einem Geometrie-Buildwechsel meldet `cad_get_model` den Zustand `rebuild_required`. Das LLM kann mit `cad_rebuild` den aktuellen unveränderten Bauplan ausdrücklich für den neuen Registry-Hash planen und als Kandidaten berechnen. Erst nach Messvergleich, regulärer Prüfung und Commit entsteht eine neue Revision. Die frühere Revision und ihre alten Messwerte bleiben unverändert lesbar. Schutzverletzungen verhindern die Übernahme.

Die konkrete systemd-Konfiguration steht unter `deployment/llcad-local.service`. `deployment/mathforge.service` und die HTTPS-/OAuth-Vorlagen dienen weiterhin einer gesonderten Bereitstellung auf einem anderen Rechner.

Quellen für die Hostkonfiguration: [OpenAI MCP-Anbindung](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [Header-Helper-Konfiguration](https://learn.chatgpt.com/docs/config-file/config-reference). Die ältere MCP-Generation handelt eine unterstützte Fassung über `initialize` aus; unbekannte Fassungen werden mit einer getesteten Alternative beantwortet. Nachfolgende Anfragen müssen eine unterstützte Fassung tragen. [MCP-Versionsaushandlung](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)
