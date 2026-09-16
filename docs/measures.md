# Messungen und ihre Beweisstärke

Jede Zahl aus `cad_measure` trägt ihre Stärke (Bauplan 6.18, 17.4): `sampled` stammt aus endlich vielen Proben, `bounded` aus einem rigorosen Zertifikat, `exact_for_declared_domain` aus exakter Kernelrechnung innerhalb der Kerneltoleranzen. Stichproben werden nie als kontinuierliche Maxima ausgegeben.

| Metrik | Ergebnis | Stärke |
|---|---|---|
| `surface_distance` | Chamfer-Mittel und maximaler Probenabstand beider Richtungen aus flächengewichteten Oberflächenproben; exakter Minimalabstand (`BRepExtrema`); Volumen-IoU aus exakten booleschen Volumina | Proben `sampled`; Minimalabstand und IoU `exact_for_declared_domain`. Das IoU beweist keine kleinen Details. |
| `wall_thickness` | Strahl von jeder Oberflächenprobe entlang der Innennormalen bis zum ersten Austritt; Minimum, Mittel, 10-%-Quantil, Probenzahl | `sampled`, kein globales Minimum |
| `clearance` mit `motion` | Freigang an bis zu 65 Positionen einer linearen Verschiebung des zweiten Features; Kontakt oder Durchdringung über das exakte gemeinsame Volumen erkannt | `sampled`; kein Sweep-Nachweis zwischen den Positionen |
| `blend_activity` | Intervallnachweis, dass jede weiche Vereinigung in einer Region inaktiv ist | `bounded` oder `not_certified` |
| `surface_deviation` | Gradientenfluss-Zertifikat zwischen zwei Revisionen eines Feldes | `bounded` oder `not_certified` |
| `distance`, `angle`, `curvature`, `clearance` (statisch) | Native Punkt- und Extremwertauswertung | unverändert, siehe `mathematical-contracts.md` |

`clearance` ohne `motion` bleibt die statische Prüfung zweier Featuregeometrien. `wall_thickness` und `surface_distance` verlangen native B-Rep-Features.

## Fertigungskandidat

Das Profil `manufacturing_candidate` verlangt ausdrückliche Prozessregeln im IR (`manufacturing`: Prozess, `minimum_wall`, optional `minimum_hole_diameter`, `maximum_overhang`, `build_direction`). Der Worker tastet an jeder nativen Ausgabe die Wandstärke ab und, falls verlangt, den Überhangwinkel nach unten weisender Flächen gegen die Baurichtung; Flächen auf der Bauplatte werden übersprungen. Bohrungsdurchmesser werden aus den deklarierten Parametern registrierter `hole`-Features geprüft. Die Prüfungen heißen `manufacturing-wall-*`, `manufacturing-overhang-*` und `manufacturing-hole-*`; sie sind `sampled` beziehungsweise `exact_for_declared_domain` für Parameter. Native Gültigkeit, Toleranzen und das Solid-Gate gelten wie in `precision_cad`. `cad_inspect.quality_status.manufacturing_status` meldet dann `rules_sampled`; eine Fertigungszertifizierung gibt es nicht. Nachweise: `tests/geometry/manufacturing.test.ts`, `tests/geometry/measures.test.ts`, `tests/geometry/test_distances.py`.
