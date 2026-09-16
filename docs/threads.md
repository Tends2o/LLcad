# Gewinde

`thread` baut ein echtes Helixgewinde als nativen Körper oder Schnitt (Bauplan 6.14). Zwei Standards sind registriert:

- `custom`: explizites Profil aus Kernradius, Steigung, Höhe, Zahntiefe, Zahnbreite, optional Kammbreite (Trapez) und Auslauf.
- `iso_metric_basic` (Bibliothek `packages/compiler/threads.ts`, Version 1): Grundprofil nach ISO 68-1 und Regelsteigungen nach ISO 261 für die Bezeichnungen `M1` bis einschließlich der in `cad_capabilities.thread_library.designations` genannten Größen; Feingewinde als `M<d>x<P>`. Der Compiler leitet Kernradius, Steigung, Zahntiefe `5H/8`, Kammabflachung `P/8` und Grundabflachung `P/4` her und speichert das Profil als `thread_profile` im Feature.

Der Auslauf (`runout`) beschneidet den Zahn am Ende mit einem Rotationskörper. Selbstdurchdringungen des erzeugten Körpers prüft `BOPAlgo_ArgumentAnalyzer`; das Ergebnis steht im `construction_report`. `cad_measure metric=thread_fit` vergleicht die Grundprofile eines Außen- und eines Innengewindes (Kern-, Flanken- und Außendurchmesser, Steigung, Gangrichtung). Toleranzklassen, Passungen und Normkonformität werden nicht behauptet; Höchstens 32 Windungen sind zulässig. Nachweise: `tests/geometry/native-operators.test.ts`, `tests/geometry/test_advanced.py`.
