# Intervallarithmetik und Feldzertifikate

`workers/cad-occt/intervals.py` wertet den registrierten Feld-AST rigoros über achsparallelen Zellen aus. Jede binary64-Operation wird um eine Einheit in der letzten Stelle nach außen gerundet; numpy-Trigonometrie wird zusätzlich um 16 ulp plus 1e-15 erweitert. Das Fehlermodell steht in jedem Bericht als `evaluation_error_model` (Bauplan 6.20, 16.3).

## Werte und Gradienten

Jede Zelle liefert ein Werteintervall, komponentenweise Gradientenintervalle aus Vorwärts-Differentiation und zusätzlich verfolgte Schranken der Gradientennorm. Exakte Distanzprimitive tragen die Einheitsnorm durch Transformationen: `transform`, `affine_transform` und `rotate` führen die singulären Werte ihrer Abbildung als Rahmen `(σ_min, σ_max)` mit. Stückweise Operatoren (`min`, `max`, `abs`, `smooth_union` außerhalb des Blendbereichs) markieren Zellen als nicht glatt, sobald beide Zweige möglich sind; solche Zellen erhalten keinen Ableitungsnachweis. Unbestimmte Produkte (`0·∞`) werden zur ganzen Zahlengeraden, nie zu stillen NaN.

## Gradientenfluss-Zertifikat

`certify_deviation(f, g, domain, cell_size, ε)` beweist `d_H(Z_f, Z_g) ≤ ε` im deklarierten Gebiet oder nennt den Grund, warum nicht. Zellen werden verfeinert, solange ein Werteintervall null enthält. Für jede verbleibende Bandzelle `C` mit ε-Erweiterung `C'` gilt: sind `f` und `g` auf `C'` glatt mit `|∇| ≥ m > 0` und `|f − g| ≤ δ` auf `C`, führt der Gradientenfluss von einer Nullstelle von `g` innerhalb `δ/m ≤ ε` zu einer Nullstelle von `f`, und symmetrisch. Zellen, die die Domänengrenze berühren, werden abgewiesen (`surface_reaches_domain_margin`).

`δ` ist das Minimum zweier rigoroser Schranken: der strukturellen Differenz (identische Teilbäume sind exakt null, kompakte Korrekturen tragen Amplitude·φ, `min`/`max`/weiche Minima sind 1-Lipschitz je Argument) und der Mittelwertform `|f(c) − g(c)| + sup|∇(f − g)|·r` mit Zellmittelpunkt `c` und halber Zelldiagonale `r`; letztere gilt nur, wo beide Funktionen auf `C'` glatt sind. Der Bericht nennt Zellenzahlen, Gradientenuntergrenze, maximale Felddifferenz und alle Ablehnungsgründe. Ein nicht zertifiziertes Ergebnis ist kein Gegenbeweis.

## Gitterfelder

`sampled_grid` (siehe `imports.md`) ist ein interpolierender natürlicher kubischer B-Spline. Über Zellen, die höchstens zwei Knotenzellen je Achse berühren, wird der Spline je Knotenzelle in Bernstein-Form gebracht, per de Casteljau auf die Teilzelle eingeschränkt und über die Konvexhülle der Kontrollpunkte eingeschlossen; partielle Ableitungen kommen aus dem differenzierten Kontrollnetz. Die Einschließung ist damit von der Ordnung der Zellgröße. Breitere Zellen verwenden die Hülle des Koeffizientenfensters. Innerhalb der aktiven Voxelbox ist das Feld C², außerhalb geklemmt (C0); Zellen, die die Box verlassen, gelten als nicht glatt. Ein Zertifikat zwischen einem abgetasteten und seinem analytischen Quellfeld gelingt, sobald Zellen klein gegen die Voxelweite sind (`tests/geometry/test_volume_import.py`, `tests/geometry/vdb-import.test.ts`).

## Blendfreie Passregionen

`blend_activity(expr, region, cell_size)` zertifiziert für jede `smooth_union` im Ausdruck, dass sie in einer Region inaktiv bleibt: dort ist `|a − b| ≥ k` als Intervallaussage bewiesen, das weiche Minimum ist dann exakt das Minimum und Passflächen behalten ihre CAD-Maßhaltigkeit (Bauplan 6.8). Transformationen oberhalb des Blends werden mitgeführt. Der Constraint `blend_free_region` prüft dieselbe Aussage bei jeder Validierung; `cad_measure metric=blend_activity` liefert sie als Job. Nicht zertifizierte Zellen heißen `possibly_active`.

## Weitere Nutzer

Intervallpruning der Extraktion (`extraction.pruning: "interval"`), zertifizierte globale Wertebereiche (`value_range`), leere Zellen (`certified_empty`) und die Regularitätsprüfung der impliziten Krümmung verwenden dieselbe Auswertung. `tests/geometry/test_intervals.py` prüft Einschließung, Gradienten, Rahmen und Zertifikate gegen Zufallsstichproben aller registrierten Operatoren.
