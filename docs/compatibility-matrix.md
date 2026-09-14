# Protokoll- und Hostkompatibilität

Stand der lokalen Verifikation: 14. September 2026. Maßgeblich für einen erneuten Lauf ist `reports/verification.json` samt Buildhash.

| System | Status | Nachweis / Grenze |
|---|---|---|
| MCP `2025-03-26`, `2025-06-18`, `2025-11-25` | Lokal getestet | Echter Client und Streamable-HTTP-Transport aus SDK 1.30.0; Initialisierung, Tools, strukturierte Ergebnisse |
| Lokaler MCP-stdio-Transport | Lokal getestet | Automatischer Prozessstart, gleiche Werkzeugverträge, privater lokaler Nutzer, persistente Modelle nach Wiederverbindung; keine Browseranmeldung |
| Vollständiger Ablauf ausschließlich über MCP | Lokal getestet | Konstruktion, semantische Auswahl, Flächenauswahl, Änderung, Prüfung, Commit, Neuzuordnung und STEP-Export; `reports/mcp-workflow.json`; kein LLM-Reasoning-Test |
| MCP `2026-07-28` | Lokal getesteter eigener Adapter | Discovery, Tools, erforderliche Metadaten, Headerabweichungen, unbekannte Version/Methoden |
| Andere MCP-Fassungen | Nicht freigegeben | Werden nicht als unterstützt beworben |
| Tasks-Erweiterung, Sampling, Elicitation, Subscriptions | Nicht angeboten | Eigene dauerhafte CAD-Jobs als portabler Ablauf |
| Chromium | Getestet | Anmeldung, Modellaufbau, Detailänderung, Prüfung, Commit, Export; Screenshots unter `reports/` |
| OAuth-Ressourcenserver | Lokal geprüft | Signatur, Issuer, Audience, Ablauf, Scopes und signierte Mandantenbindung |
| Realer OAuth-Anbieter mit PKCE und Zielhost | Offen | Kein kundenspezifischer Issuer, Callback oder freigegebener Client vorgegeben |
| Lokaler Codex 0.154.0 | Transport und Werkzeugausführung geprüft | Echter App-Server, zwei gleichzeitige Verbindungen, Aufbau/Änderung/Prüfung/Commit/STEP; `reports/codex-host.json`; ohne Modellturn |
| Lokale LLM-Sprachaufträge im Codex-Host | Begrenzter Testsatz geprüft | Sieben echte Modellturns mit numerischem Grader und 102 CAD-Aufrufen auf dem aktuellen geprüften Build; `reports/llm-eval.json` |
| Breite LLM-Evaluation und ChatGPT-Web-Host | Offen | Sieben lokale Aufträge ersetzen weder einen unabhängigen breiten Testsatz noch die reale Web-/OAuth-Verknüpfung |
| Eingebettete Host-UI | Nicht implementiert | Eigenständiger authentifizierter Browser-Viewer vorhanden |
| Öffentliche Bereitstellung / Plugin-Publikation | Nicht erfolgt | Deploymentvorlagen vorhanden; keine Veröffentlichung ausgeführt |

## Quellen der Adapterentscheidung

OpenAI empfiehlt den offiziellen MCP-SDK-Aufbau mit typisierten Tools und Streamable HTTP. Die Implementierung folgt dieser Struktur für die vom installierten SDK unterstützte ältere Fassung. [OpenAI: Build an MCP server](https://developers.openai.com/plugins/build/mcp-server)

Die neue Fassung ist ein eigenständiger Adapter. Ihre HTTP-Header müssen die passenden JSON-Felder spiegeln; die Spezifikation beschreibt auch Discovery und die Pflichtfelder pro Anfrage. [MCP: Streamable HTTP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http), [MCP: Schema 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/schema)

Für den realen Remotezugang muss der Betreiber einen passenden OAuth-Anbieter anschließen; dessen PKCE-, Ressourcen- und Client-Konfiguration ist Teil des Hosttests. Der Dienst implementiert keinen eigenen Autorisierungsserver. [OpenAI: Authentication](https://developers.openai.com/plugins/build/auth)

## Noch auszuführender Hosttest

Mit tatsächlichem HTTPS-Endpunkt und Identitätsanbieter prüfen: Auth-Metadaten, Clientregistrierung, erlaubte Callbacks, PKCE, Token-Audience, Scopes, Tool-Discovery, Aufbau eines privaten Modells, Änderung/Prüfung/Commit, Wiederholung bei Verbindungsabbruch und Zugriff auf private Exportdateien. Ergebnis, Datum, Hostversion, genutzte Protokollfassung und aktueller Registry-Hash werden in `reports/target-host.json` dokumentiert. Diese Datei wird nicht automatisch mit einem erfundenen Erfolg angelegt.
