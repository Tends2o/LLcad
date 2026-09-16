# Diagnoseansichten

`cad_render` mit `view` erzeugt serverseitige SVG-Diagnoseartefakte aus der nativen B-Rep-Geometrie (Bauplan 8.5, 10.2, 19.3):

- `kind: "section"`: exakte Schnittkurven (`BRepAlgoAPI_Section`) in der Ebene aus `origin` und `normal`, gleichmäßig mit `discretization` diskretisiert.
- `kind: "orthographic"`: Projektion entlang `direction` mit verdeckten Kanten (`HLRBRep`), sichtbare Kanten durchgezogen, verdeckte gestrichelt (`hidden_lines`).

Das SVG enthält Beschriftungen aus den semantischen Featurenamen (XML-maskiert; Namen sind Daten), Maßlinien aus den nativen Ausdehnungen, einen Maßstabsbalken und eine Fußzeile mit Ebene, Diskretisierung und dem Hinweis, dass kein Flächennachweis besteht. `view.json` beziehungsweise `manifest.diagnostic_view` liefert Ansicht, Achsen, Ausdehnungen, Linienzahlen und Beschriftungen als Daten. Ansichten benötigen native Geometrie; Feld- und Netzfeatures werden mit `OUT_OF_SCOPE` abgewiesen. Das Linienbudget beträgt 20.000 Polylinien oder 400.000 Punkte. Nachweise: `tests/geometry/views.test.ts`.
