# Vektoren in gespeicherten Formeln

Das sichere Ausdrucks-AST unterstützt `vec3(a,b,c)`, `dot(a,b)` und `norm(v)`. Alle drei Komponenten eines Vektors müssen dieselbe Dimension besitzen. Einheiten werden vor der Rechnung auf Millimeter beziehungsweise Radiant umgerechnet. Vektoraddition und -subtraktion benötigen gleiche Dimensionen; Multiplikation und Division mit Skalaren führen die Dimensionen mit. Vektor mal Vektor ist ohne ausdrückliches Skalarprodukt ungültig.

`norm(vec3(3 mm,4 mm,12 mm))` liefert einen Längenwert von 13 mm. Das Skalarprodukt zweier Längenvektoren hat die Dimension einer Fläche; durch Wurzel oder eine ausdrücklich angegebene Bezugsgröße kann daraus der gewünschte skalare Vertrag entstehen. Modellparameter und normalisierte Solvergleichungen erhalten weiterhin skalare Ergebnisse. Ein Vektor wird nicht still auf eine Komponente reduziert.

Die Funktionen sind feste Interpreterzweige und strikte Schemawerte. Unbekannte Funktionen und Parameter, einschließlich geerbter Objekteigenschaften, werden abgewiesen. Die bisherigen Definitionsbereichs-, Einheiten-, Größen- und Tiefengrenzen gelten auch für Vektorausdrücke. Benutzerformeln erhalten keine Datei-, Netz-, Import- oder Programmausführungsfähigkeiten.

Der SLSQP-Pfad unterstützt diese Vektorausdrücke mit analytischen Vorwärtsableitungen. Die Kettenregel berücksichtigt beide Argumente eines Skalarprodukts sowie variable Skalen und Nenner. Die Norm ist am Nullvektor als Wert definiert, hat dort aber keine eindeutige glatte Ableitung. Dieser Solververtrag weist Normwerte bis 1e-15 deshalb ab, statt eine Ableitung zu erfinden. Allgemeine gespeicherte Formeln dürfen weiterhin den Wert null für die Norm eines Nullvektors liefern. Lokale Konvergenz ist kein globaler Optimalitätsnachweis.

`tests/geometry/vector-expressions.test.ts` prüft 150 zufällige Vektorkombinationen, gemischte Längeneinheiten, falsche Werttypen und einen tatsächlich vermessenen CAD-Körper mit Formelparameter. Ein weiterer Ablauf löst vier gekoppelte Parameter über Norm, Skalarprodukte und variable Skalierung, erzeugt native Geometrie, validiert und übernimmt sie. Eine spätere Verletzung der gespeicherten Vektorgleichungen scheitert an der Validierung. `tests/geometry/test_solver_vectors.py` prüft die tatsächlich an den Optimierer übergebenen Ableitungen gegen zentrale Differenzen für drei Gleichungen an je 13 Punkten und die Ablehnungsgrenze am Nullvektor.

Diese Rechnungen sind begrenzte numerische Auswertungen. Sie liefern keinen zusätzlichen universellen Rundungs- oder globalen Geometriefehlernachweis.

Der geprüfte Vektor-Build wurde am 14. September 2026 in den lokalen Dienst übernommen. Der tatsächlich installierte Codex-Host hat den Transport geprüft; neun ältere, nichtleere Modelle wurden mit unveränderter Konstruktion neu berechnet, validiert und als zusätzliche Revisionen übernommen. Die alten Revisionen und ihre Messwerte bleiben erhalten. Dieser Hostlauf verwendet keinen Modellturn.
