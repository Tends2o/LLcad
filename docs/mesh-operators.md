# Netzoperatoren

Maßgebliche Netze (`authoritative_representation: mesh`) besitzen registrierte lokale Operatoren mit Berichten (Bauplan 7.5, 9.2). Jeder Operator erzeugt ein neues Netzfeature; das Original bleibt als eigene Revision erhalten.

| Operator | Wirkung | Bericht |
|---|---|---|
| `mesh_repair` | Entfernt degenerierte und doppelte Dreiecke, orientiert konsistent, schließt kleine Löcher (`fill_holes_max_edges`), optional Verschweißen innerhalb `weld_tolerance` | Zahlen je Reparaturschritt, verbleibende Randkanten |
| `remesh_region` | Isotropes Remeshing innerhalb einer Kugel: Kantenteilung, -kollaps, -flip und Tangentialrelaxation mit Gültigkeitsprüfung; Punkte außerhalb bleiben fest | Kantenlängenstatistik vor/nach, Iterationen, abgelehnte Schritte |
| `local_mesh_deform` | ARAP-Deformation (Bauplan Gl. 67) mit Zielpunkten innerhalb einer Kugel, Rotationen in SO(3), fixierter Außenregion | Energie je Iteration, Verschiebung der Handles |
| `tessellate` | Maßgebliches Netz aus einem B-Rep mit Konvertierungsbericht | Abweichung, verlorene Semantik |
| `extract_isosurface` | Maßgebliches Netz aus einem analytischen Feld | Zellen, Bandbreite, verlorene Semantik |

`cad_measure metric=fit_primitives` liefert Primitiv- und Symmetriehypothesen mit Residuen und Abdeckung; sie erfinden keine Konstruktionsgeschichte. Kollisionsfreiheit nach einer Deformation ist eine getrennte Prüfung; ein kleiner Energiewert ist kein Nachweis. Alle Ergebnisse werden mit den exakten CGAL-Prädikaten des `watertight_solid`-Profils erneut geprüft (`mesh-quality.md`). Nachweise: `tests/geometry/mesh-operators.test.ts`, `tests/geometry/test_mesh_ops.py`.
