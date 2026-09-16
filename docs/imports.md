# Importe: OpenVDB-Felder und STEP-Baugruppen

Importe bleiben isolierte, private Dekodierungen (Bauplan 20.2). Dateityp, Größe, Archivsignaturen, externe STEP-Referenzen und Einheiten werden vor dem Job geprüft; der native Parser läuft im Bubblewrap-Worker. Originale werden nie verändert.

## OpenVDB als Gitterfeld

`cad_import format=vdb` liest genau ein skalares Float-Grid (bei mehreren Grids `grid` angeben). Zulässig sind achsparallele Transformationen aus Skalierung und Verschiebung; gedrehte oder gescherte Gitter werden mit `OUT_OF_SCOPE` abgewiesen. Die aktive Voxelbox darf höchstens zwei Millionen Voxel umfassen. Koordinaten und längenwertige Samples werden von `source_unit` nach Millimeter konvertiert; eine widersprechende Einheit in den Dateimetadaten ist `UNIT_MISMATCH`. Fehlt die Werteinheit in der Datei, gilt `length` und `value_unit_assumed: true`.

Das Feature erhält `authoritative_representation: implicit`. Der Worker interpoliert die Samples mit einem natürlichen kubischen B-Spline (exakte Interpolation an den Knoten, C² im Inneren, geklemmt außerhalb). Die Lipschitz-Schranke wird aus den Koeffizientendifferenzen je Achse mit gerichteter Rundung gemessen; an Knicken der Quelle (Distanzkegel, Bandabschnitte) ist sie ehrlich größer als die der analytischen Quelle. Domäne und Zellweite folgen aus der aktiven Box und der Voxelgröße. `import_report` nennt Gitter, Voxelgröße, Wertebereiche, Koeffizientenbereiche, Interpolationsfehler an Probeknoten und verlorene Semantik. Ein kontinuierlicher Distanznachweis wird nicht behauptet.

Als Feldknoten `sampled_grid {artifact_id, lipschitz, value_unit, source_unit, grid?}` lässt sich dasselbe Gitter in beliebige Feldausdrücke einsetzen. `lipschitz` ist eine deklarierte Schranke; der Worker verweigert mit `CONSTRAINT_CONFLICT`, wenn die gemessene Schranke größer ist, und mit `UNIT_MISMATCH`, wenn die Werteinheit nicht zur Datei passt. Artefaktzugriff wird für jeden Knoten wie für Importe geprüft und gebunden. Zertifikate gegen ein analytisches Referenzfeld stehen in `intervals-and-certificates.md`.

## STEP mit Produktstruktur

`cad_import format=step structure=preserve` startet einen `probe`-Job. Der Worker liest die Datei mit XCAF und liefert den Vorkommensbaum: für jedes Vorkommen Pfad, Labeleintrag, Prototyp, Name des Vorkommens beziehungsweise Prototyps, Elternvorkommen sowie die starre Platzierung relativ zum Elternvorkommen als Verschiebung, Achse und Winkel. Skalierende oder spiegelnde Platzierungen werden abgewiesen. Höchstens 128 Vorkommen, 64 Baugruppen und 16 Ebenen sind zulässig.

Der Modellservice setzt die Fortsetzung innerhalb desselben Jobs um: Jedes Vorkommen mit nicht identischer Platzierung erhält einen Rahmen mit Elternrahmen; Baugruppenvorkommen werden Baugruppen mit `parent_assembly`, Blattvorkommen werden Teile mit genau einem importierten Feature (`component` = Prototyp-Labeleintrag). Der Kandidat durchläuft denselben Bewertungs-, Prüf- und Commit-Pfad wie jeder andere. Das Probe-Ergebnis (`cad_job_get`) enthält den Strukturbericht und die Fortsetzung (`continuation`): den Kandidatenjob oder einen ehrlichen Fehler, etwa `STALE_REVISION`, wenn sich der Modellkopf zwischenzeitlich verschoben hat. Die Fortsetzung läuft in einem Sicherungspunkt; ein Fehler hinterlässt keine Teilzustände.

Beim Berechnen lädt jedes Feature nur den ungeplatzierten Prototyp seines Eintrags; die Lage kommt ausschließlich aus der deklarierten Rahmenhierarchie. Zwei Vorkommen desselben Prototyps bleiben getrennte Teile mit eigenem Rahmen. `cad_structure` zeigt Baugruppen, Teile und Weltausdehnungen; `structure=flatten` (Standard) liest die Datei weiterhin als eine einzige Geometrie.

Nachweise: `tests/geometry/step-assembly.test.ts` (verschachtelte Baugruppe mit zwei Instanzen und einer 90°-Drehung, Weltausdehnungen, Export, Flatten-Vergleich, stale Fortsetzung), `tests/geometry/vdb-import.test.ts` und `tests/geometry/test_volume_import.py`.
