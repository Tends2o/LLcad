# Viewer

Der Viewer zeigt abgeleitete Darstellungen; der Geometriedienst entscheidet (Bauplan 19). Jede Änderung läuft über dieselben Werkzeuge, Rechte, Kandidaten- und Commit-Gates. Netze werden mit lokalen Ursprüngen an die GPU übergeben, damit Details bei großen Weltkoordinaten keine vermeidbaren Float32-Verluste erleiden (`preview-coordinates.md`).

## Umfang

- Revision, Qualitätsstatus, Konstruktionsbaum und Struktur: `cad_structure` liefert Baugruppen mit `parent_assembly` und Teile mit Repräsentation und Featurezahl; ein Teil filtert den Featurebaum und wählt sein erstes Ausgabefeature.
- Sichere Auswahl: Ein Klick auf eine native Fläche sendet einen semantischen Anker (`point`, `normal`, baryzentrische Koordinaten, Dreiecksindex, Kamerarichtung) an `cad_inspect`; der Dienst löst das erzeugende Merkmal über die gespeicherte Flächenherkunft auf und gibt `selection_anchor` mit lokalem Rahmen und Kamerabezug zurück. Der Dreiecksindex allein ist nie die Auswahl.
- Typisierte Parameter, gemessene Geometrie, Schutzregeln, Messung zwischen zwei Oberflächenpunkten mit Markierungen, Schnitt, Drahtgitter, Isolation, Explosion und Vorher-/Nachher-Überlagerung.
- Schutz- und Änderungsregionen des gewählten Merkmals werden im Weltraum eingezeichnet (`protected_region` als Box im Featurerahmen, `change_region` als Kugel); Regionen in fremden Rahmen werden benannt, nicht geraten.
- Maßstab: Ein Balken zeigt eine runde Länge in Millimetern und die aktuelle Auflösung in mm/px aus Kamera und Viewport.
- Darstellungskanäle: beleuchtet, unbeleuchtet (Diagnose), Normalen und native Krümmung je Fläche aus der adaptiven Vorschau. Texturen oder Normalmaps werden nirgends als Geometrie behandelt.
- Pixel-LOD: Der Knopf leitet die Zielabweichung aus der Pixelgröße ab (halbe Millimeter je Pixel, begrenzt auf 0,005–0,2 mm), nutzt die adaptive Tessellation je Fläche und beschränkt große Modelle auf einen Ausschnitt um den Blickpunkt (`region`). Die Fußzeile nennt die absolute Auflösung und das kleinste aufgelöste Merkmal aus dem Vorschaubericht; sie ist keine Geometriebestätigung.

## Grenzen

Vorschauen bleiben `preview_only` ohne Flächennachweis. Der Ausschnitt einer Pixel-LOD-Vorschau entwertet Flächenbereiche; die Flächenauswahl fällt dann auf das Feature zurück. Eine eingebettete Host-UI ist nicht Teil dieses Viewers. `scripts/browser-test.ts` führt Anmeldung, Struktur, Kanäle, LOD, Anker, Messmarkierungen, Schnitt, Bearbeitung und Commit in Chromium tatsächlich aus (`reports/browser.json`).
