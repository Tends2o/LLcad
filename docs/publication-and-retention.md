# Interne Veröffentlichung und Aufbewahrung

## Veröffentlichung

`cad_access` mit `change.action: "publish"` erstellt als Eigentümer einen gebundenen Antrag, ein geprüftes Exportpaket einer Revision an einen internen Empfänger freizugeben. Der Antrag bindet Akteur, Modell, Revision, Paket-Hash, Empfänger und Ablauf; die Bestätigung läuft über den getrennten vertrauenswürdigen Policy-Pfad (`project-access.md`). Das Pflichtgate `before_publish` prüft Paketbindung und Verluste, bevor eine Veröffentlichung gespeichert wird. `mode: "publications"` listet eigene und empfangene Veröffentlichungen. Empfänger erhalten kurzlebige, mit dem privaten Policy-Schlüssel (Ed25519) signierte Downloadlinks mit fünf Minuten Gültigkeit; jeder Abruf wird serverseitig autorisiert. Eine externe Veröffentlichung, ein Versand an Dritte oder eine öffentliche Adresse existieren nicht (Policy-Standard `external_publication_without_bound_approval: deny`).

## Aufbewahrung

`RETENTION_POLICY` (Version 1) löscht ausschließlich abgeleitete Daten: Vorschauartefakte nach sieben Tagen, verwaiste Cachegenerationen ohne Revisionsbezug nach 30 Tagen, abgelaufene Veröffentlichungen nach einem Tag Karenz. Revisionen, Prüfnachweise, Exportpakete, importierte Originale und das Audit werden nie gesammelt. `npm run maintenance` wendet die Politik an und schreibt einen Auditeintrag `retention_applied`. Externe Backups unterliegen der gesonderten Betreiber-Aufbewahrung und -Löschung (`operating-runbook.md`). Nachweise: `tests/security/retention-policy.test.ts`, `tests/security/publication.test.ts`.
