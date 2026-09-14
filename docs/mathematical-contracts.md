# Mathematische Verträge

Die verpflichtenden Grenzwerte und Operatoren werden aus `packages/compiler/index.ts` nach `schemas/operator-registry.json` erzeugt. Die Capability-Ausgabe ist maßgeblich für den tatsächlich verfügbaren Umfang.

## Einheiten und Koordinaten

Das IR verwendet Dezimalzeichenfolgen und eine kanonische Geometrieeinheit Millimeter. Zulässig sind `m`, `mm`, `um`, `rad`, `deg`, `1`; Volumenziele tragen separat `mm3`. Die Konvertierung erfolgt kontrolliert mit Decimal.js und anschließend endlichen Worker-Zahlen. Die Modell-Toleranz liegt zwischen 0,00001 und 0,1 mm. Der Standard beträgt 0,001 mm.

Weltkoordinaten sind rechtshändig mit Z nach oben. Das v1-IR unterstützt ausdrücklich deklarierte hierarchische starre Bezugsrahmen; native Konstruktionen werden lokal berechnet. Die Vertragsdetails stehen in `structure-and-frames.md`. Allgemeine affine Transformationen bleiben ausdrückliche Feature-Operatoren. Viewer und GLB verwenden lokale Netzursprünge vor der Float32-Konvertierung. GLB wandelt Millimeter in Meter um, rotiert Z nach Y und misst die Verluste nach dem Einlesen der Positionsbytes und Knotentransformationen. Vorschauauflösung und Modell-Toleranz sind getrennt. Die Exportkonvention folgt der [glTF-Spezifikation](https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#coordinate-system-and-units).

## Sichere Formeln

`feature.expressions` speichert Formeln als Daten. `constant`, `parameter` und die registrierten skalaren Funktionen werden ohne `eval` ausgewertet. Einheiten werden durch Addition, Multiplikation, Division, Wurzel und Trigonometrie geführt. Zyklische Parameterabhängigkeiten, ungültige Definitionsbereiche und zu tiefe Ausdrücke werden abgewiesen.

Beispiel für einen Radius, der doppelt so groß wie ein anderer Längenparameter ist:

```json
{"fn":"*","args":[{"parameter":"x"},{"constant":{"value":"2","unit":"1"}}]}
```

`solve_volume` besitzt analytische Verträge für Kugeln, Zylinder und Quader. Die Lösung ist ein normaler Kandidat mit einem zusätzlichen Volumenconstraint. Das Ziel wird am erzeugten B-Rep mit adaptiver OCCT-Integration geprüft. `cad_solve_constraints` ergänzt eine begrenzte gekoppelte Lösung mit SciPy SLSQP: höchstens zwölf kontinuierliche Variablen, 32 Gleichungen/Ungleichungen und 200 Iterationen. Residuen müssen durch explizite Einheiten normalisiert sein. Grenzen, geschützte Parameter, glatte Definitionsbereiche und gerundete Lösung werden geprüft. Die Rückgabe enthält Patchvorschläge und dauerhaft gespeicherte Gleichungsconstraints; die anschließende native Kandidatenprüfung und Commitbindung bleiben erforderlich. Lokale Konvergenz ist kein Beweis eines globalen Optimums.

## Native CAD-Operationen

Profile liegen in expliziten Koordinaten. Extrusion erfolgt entlang +Z, Rotation um die Z-Achse. Sweep nimmt Profil und Pfad, Loft eine geordnete Folge kompatibler Profile. Die rationale Oberfläche verwendet ein geklemmtes einzelnes rationales Bézier-/B-Spline-Patch aus Kontrollpunkten und positiven Gewichten.

Bohrung, Tasche und Kreisnut schneiden ab der angegebenen Z-Höhe nach unten. Nutradius bezeichnet die Mittellinie, `width` die radiale Breite. Kantenfilter für Verrundung/Fase sind `all`, `vertical` und `horizontal`; eine eindeutige Schalenöffnung ist die obere oder untere ebene Fläche. Nicht gültige Operationen scheitern mit Diagnose.

Grundkörpermaße, Nuttiefe/-breite, Bohrungsradius/-tiefe und Taschendimensionen werden an tatsächlich entstandenen Geometriegrenzen gemessen. Jede registrierte Pflichtdimension wird unabhängig von zusätzlich angelegten Nutzerconstraints geprüft. Der Bericht nennt die Auswertungsstufe des Features. Nachfolgende Operationen können die endgültige Oberfläche ändern; nicht unterstützte globale Nachweise werden nicht daraus abgeleitet.

Die lokale Restwand ist nur für die Kreisnut in einer ebenen Boxbasis implementiert. `protected_bounds` schützt die sechs AABB-Grenzen; das ist kein vollständiger Nachweis einer unveränderten Außenkontur. `protected_feature` vergleicht den gesamten gespeicherten Feature-Geometriehash.

## Lokale Patchänderungen und Anschlüsse

`set_surface_poles` ersetzt das Kontrollnetz eines einzelnen NURBS-Patches. `expected_hash` bindet die Änderung an das bisherige Netz; Dimensionen und Gewichte müssen weiterhin zusammenpassen. Der normale Dirty-Graph und sämtliche Schutzconstraints gelten auch für diese Änderung.

Der Constraint `patch_continuity` prüft den Anschluss von `feature_id` bei `u=1` an `neighbor_feature_id` bei `u=0`, jeweils mit gleicher v-Richtung. Registriert sind `C0`, `C1` und `C2`, unterschiedliche Grade entlang v sowie beliebige positive rationale Gewichte. Die genaue Prüfung ist auf acht Anschlüsse und Grad fünf pro Richtung begrenzt. Andere Randzuordnungen sind nicht registriert. C2 verlangt zusätzlich eine nachgewiesene positive untere Schranke für die Oberflächen-Jakobifläche entlang der gesamten Naht.

Homogene rationale Polynome, positive Nennergrenzen und die Konvexhülleneigenschaft der Bernstein-Basis begrenzen den Fehler entlang der **gesamten** Naht. Die Polynomrechnung verwendet exakte BigInt-Brüche; ausgegebene Dezimalschranken werden nach außen gerundet. Obere L1-Schranken begrenzen die euklidischen Positionsfehler sowie erste und zweite Ableitungsfehler. C1 verlangt Quer- und Tangentialableitungen, C2 zusätzlich alle zweiten partiellen Ableitungen. Die normierten u-/v-Parameter sind dimensionslos; ihre Ableitungen werden in mm pro Parametereinheit angegeben. Dieser Nachweis betrifft die deklarierte mathematische Naht. Er behauptet weder eine vernähte B-Rep-Topologie noch einen zertifizierten Rundungsfehler der nativen Darstellung.

Ein ausgeführter Integrationstest verändert ausschließlich die inneren Kontrollpunkte eines Patches, erhält die C1-Naht und den geschützten Nachbarpatch und übernimmt den geprüften Kandidaten. Ein zweiter Kandidat mit gebrochener Naht wird beim Commit abgewiesen.

## Implizite Geometrie

Es gilt negativ innen. Kugel, Box und regulärer Torus haben den Vertrag `exact_sdf`. CSG, glatte Vereinigung, Offsets und Detailänderungen werden ausdrücklich als allgemeine implizite Felder geführt. Nichtuniforme Skalierung liefert einen begrenzten Distanzschätzer; sie erhält nicht die exakte Distanzsemantik.

Lokale Feldänderungen verwenden die kompakte Wendland-Funktion

\[
\phi(q)=(1-q)_+^4(4q+1).
\]

Die maximale radiale Ableitung beträgt 2,109375. Der Compiler addiert daraus eine konservative Lipschitz-Schranke. Eine lokale Deformation verwendet dieselbe Basis; die Schranke für den Deformationsgradienten muss kleiner als 0,75 bleiben. Die inverse Abbildung wird mit begrenzter Fixpunktiteration berechnet, andernfalls abgelehnt.

`protected_region` beweist für geeignete lokale Felddeltas, dass ihre gesamte kompakte Stütze die geschützte AABB nicht berührt. Bei Deformationen wird der Stützradius um den maximalen Verschiebungsbetrag erweitert. Veränderte Domänen oder komplexe, nicht herleitbare Ausdrücke erzeugen keinen pauschalen Unverändertheitsnachweis.

Oberflächenzellen werden mit einer Lipschitz-Schranke räumlich ausgeschlossen. Verbleibende Blattzellen werden mit Marching Tetrahedra extrahiert. Die gewählte Zellweite beweist keine erhaltene Subzelltopologie. Die Ausgabe trägt `preview_only` und `certified_bound: null`.

## Volumetrische Dateien und Wiederverwendung

OpenVDB 10 exportiert maximal 125.000 Float32-Samples eines einzelnen analytischen Feldes. Ein explizites Band begrenzt gespeicherte Werte; Metadaten unterscheiden ursprüngliche Feldsemantik, gespeicherte Samples, Domäne und tatsächliches Rasterende. Jeder gespeicherte Rasterwert wird nach dem Dateiexport erneut gelesen und verglichen. Dies zertifiziert weder kontinuierliche Distanz noch Topologie unterhalb der Rasterweite. VDB-Import ist nicht registriert.

Ein privater räumlicher Samplecache ist an Mandant, Eigentümer, Modell, Feature und Registry gebunden. Gleiche Punkte können zwischen Auflösungen wiederverwendet werden; außerhalb der kompakten Stütze eines lokalen Edits wird der unveränderte Ausdruck erkannt. Innerhalb inverser Deformationen wird keine unzulässige Gleichheit des Quellpunkts angenommen. Pro Feature bleiben maximal 160.000 Samples erhalten. Native Berechnungen laufen weiter in frischen isolierten Prozessen; diese Trennung verhindert die Übernahme veränderlichen Workerzustands in den nächsten Auftrag.

## Zusätzliche native Konstruktionen

Punkte, Linien, Dreipunktbögen, endliche XY-Ebenen, rechteckige UV-Trimmung eines einzelnen Patches, planare Deckflächen, Vernähen und Regularisierung sind registriert. Vernähen zu einem Solid verlangt eine geschlossene Einzelschale. Freie Trimmschleifen über mehrere rationale Patches sind nicht registriert.

`thread` baut ein echtes dreieckiges Helixgewinde, rechts- oder linksgängig, außen oder als Schnitt in einen vorhandenen Körper. Der explizite Standard heißt `custom`; eine ISO-/DIN-Konformität wird nicht behauptet. Zahnweite muss kleiner als Steigung, Zahntiefe kleiner als Kernradius sein. Höchstens 32 Windungen sind erlaubt.

## Nachweisgrenzen

`checks_passed_within_profile` bedeutet ausschließlich, dass die tatsächlich registrierten Prüfungen des Profils bestanden sind. `null` ist kein Fehler von null. Selbstschnittfreiheit beliebiger Meshes, allgemeine Mindestwandstärke, dynamischer Freigang, Statik und Fertigungsprozessregeln gehören nicht zu den freigegebenen Profilen dieser Version.
