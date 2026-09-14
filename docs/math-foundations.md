# Mathematische Einordnung der Näherungsbasis

Abschnitt 6.1 des Originalplans begründet eine endliche Darstellungsbasis. Er ist keine Behauptung, dass der Dienst eine beliebige kompakte Menge automatisch erkennt oder exakt in ein Gitter umwandelt.

Für ein Dreieck beschreiben nichtnegative baryzentrische Gewichte mit Summe eins genau seine konvexe Hülle. Die Vereinigung der durch eine endliche Indexliste beschriebenen Dreiecke ist damit das entsprechende endliche Netz. Gekrümmte CAD-Flächen werden durch diese Darstellung im Allgemeinen approximiert. LLcad hält abgeleitete indexierte Vorschauen deshalb getrennt von der maßgeblichen Konstruktion; Mesh- und Exportberichte machen aus der Ansicht keinen CAD-Prüfnachweis.

Für die Gitteraussage sei K eine nichtleere kompakte Menge und h > 0. Ihre Beschränktheit stellt sicher, dass nur endlich viele geschlossene Würfel eines festen Gitters mit K zusammentreffen. Jeder Punkt von K liegt in wenigstens einem ausgewählten Würfel, also in deren Vereinigung K_h. Für jeden Punkt x aus K_h gibt es einen ausgewählten Würfel C mit x in C und einen Punkt y aus K geschnitten C. Damit ist der Abstand von x zu K höchstens der Durchmesser von C, also √3 h. Der umgekehrte gerichtete Abstand von K zu K_h ist null. Das Maximum beider gerichteter Abstände ergibt die behauptete Hausdorff-Schranke.

Diese Aussage begrenzt weder den Abstand der Ränder noch die Topologie oder Rechenkosten. Ein kleiner Hohlraum kann beispielsweise durch ausgewählte Würfel ausgefüllt werden. Die Schranke wird in LLcad daher nicht als Zertifikat für eine aus Feldsamples extrahierte Oberfläche verwendet. Deren Berichte kennzeichnen fehlende kontinuierliche Fehler- und Subzelltopologienachweise ausdrücklich.

Der konkrete Pfad für indexierte native und implizite Vorschauen steht in `workers/cad-occt/geometry.py` und `workers/cad-occt/fields.py`. Ausgeführte Vorschau-/Roundtriptests stehen in `tests/roundtrip/exports.test.ts` und `tests/geometry/test_native.py`. Die obige Gitterherleitung ist ein mathematischer Nachweis mit den genannten Voraussetzungen, kein statistischer Test einer universellen Rekonstruktion.
