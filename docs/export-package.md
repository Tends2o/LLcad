# Revisionsgebundene Exportkomponenten

Jeder Export liefert die gewünschte Geometriedatei sowie `manifest.json`, `validation.json` und `model.ir.json` als private, authentifizierte Artefakte. `package_manifest` verweist direkt auf das Manifest. Die Komponenten bilden einen verknüpften Dateisatz; sie sind noch kein ZIP-Archiv. Der Aufrufer kann alle Dateien über die im Manifest aufgeführten Downloadpfade abrufen.

Das Manifest bindet Modellrevision, ursprünglichen IR-/Geometriehash, Registry und Policy des Übernahmenachweises, Exporter-Build, Maßeinheit, Koordinatenkonvention, Qualitätsstatus und jede Komponente über SHA-256 und Dateilänge. GLB enthält Meterkoordinaten und die Rotation von Z-oben nach Y-oben. Die übrigen Geometrieadapter arbeiten in Millimetern; STL benötigt diese separate Einheitenangabe.

Der IR-Sidecar erhält semantische Feature-IDs, Parameter, Quellenstatus, Absicht, Abhängigkeiten, Schutzbedingungen und Annahmen. Bei importierten Teilen verweist `source_assets` zusätzlich auf die geprüften, unveränderten Originaldateien. Ein solcher IR-Sidecar allein ist ohne diese Originale kein vollständig eigenständiges Archiv. Es werden keine ursprünglichen parametrischen Features für Fremdgeometrie erfunden.

Der Prüfbericht enthält den gespeicherten Commitnachweis samt überprüftem Digest und die tatsächlich ausgeführten formatspezifischen Roundtrips. Das Manifest unterscheidet angeforderte Toleranzen, gemessene Exportabweichungen und nicht bewiesene kombinierte Oberflächenfehler. Ein leeres Zertifikatsfeld wird nicht durch die angeforderte Tessellierungsauflösung ersetzt. Vorhandene Rundungsberichte zu NURBS-Verfeinerungen werden aus der zugehörigen Transaktion übernommen.

Nachweise: `tests/roundtrip/exports.test.ts` prüft IR, STEP, STL, B-Rep und GLB, Sidecar-/Dateihashes, Semantikerhalt, Rechte und die Abweisung eines nicht zur Revision passenden Prüfnachweises. Der OpenVDB-Rasterroundtrip wird zusätzlich in `tests/geometry/volume.test.ts` geprüft. Alle Links bleiben privat; ein Export veröffentlicht oder versendet kein Modell.
