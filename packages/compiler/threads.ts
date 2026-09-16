import { requireThat } from "../semantic-ir/errors.js";
/** Versioned thread knowledge module: ISO 68-1:1998 basic profile with ISO 261:1998 coarse pitches.
 *
 * Only the basic profile geometry is derived here. Tolerance classes (ISO 965),
 * allowances, rounded roots and gauge conformity are not modeled, so no fit or
 * standard conformity is ever certified from these values.
 */
export const THREAD_LIBRARY = {
  name: "iso_metric_basic",
  version: 1,
  sources: [
    "ISO 68-1:1998 ISO general purpose screw threads - Basic profile - Part 1: Metric screw threads",
    "ISO 261:1998 ISO general purpose metric screw threads - General plan",
  ],
  validity:
    "basic_profile_geometry_only; tolerance_classes_and_fit_not_modeled",
  formulas: {
    fundamental_triangle_height: "H = (sqrt(3)/2) * P",
    pitch_diameter: "d2 = d - 0.649519 * P",
    minor_diameter: "d1 = d - 1.082532 * P",
    basic_depth: "5H/8 = 0.541266 * P",
    root_flat: "P/4",
    crest_flat: "P/8",
  },
  coarse_pitch_mm: {
    M1: "0.25",
    "M1.2": "0.25",
    "M1.6": "0.35",
    M2: "0.4",
    "M2.5": "0.45",
    M3: "0.5",
    M4: "0.7",
    M5: "0.8",
    M6: "1",
    M8: "1.25",
    M10: "1.5",
    M12: "1.75",
    M16: "2",
    M20: "2.5",
    M24: "3",
    M30: "3.5",
    M36: "4",
    M42: "4.5",
    M48: "5",
    M56: "5.5",
    M64: "6",
  } as Record<string, string>,
};
const SQRT3_2 = Math.sqrt(3) / 2;
export function isoBasicProfile(designation: string) {
  const match = /^M(\d+(?:\.\d+)?)(?:x(\d+(?:\.\d+)?))?$/.exec(designation);
  requireThat(
    match,
    "INVALID_SCHEMA",
    "Gewindebezeichnung muss der Form M<d> oder M<d>x<P> entsprechen.",
  );
  const major = Number(match[1]);
  const coarse = THREAD_LIBRARY.coarse_pitch_mm["M" + match[1]];
  const pitch = match[2] ? Number(match[2]) : coarse ? Number(coarse) : NaN;
  requireThat(
    Number.isFinite(pitch) && pitch > 0,
    "OUT_OF_SCOPE",
    "Für diese Nennweite ist keine Regelsteigung hinterlegt; Steigung ausdrücklich als M<d>x<P> angeben.",
  );
  requireThat(
    major >= 1 && major <= 300 && pitch < major / 2,
    "GEOMETRY_INVALID",
    "Nennweite oder Steigung außerhalb des Bibliotheksbereichs.",
  );
  const H = SQRT3_2 * pitch;
  return {
    designation,
    standard: THREAD_LIBRARY.name,
    library_version: THREAD_LIBRARY.version,
    major_diameter_mm: major,
    pitch_mm: pitch,
    pitch_source: match[2] ? "explicit_fine_pitch" : "ISO_261_coarse_table",
    fundamental_height_mm: H,
    pitch_diameter_mm: major - 0.649519 * pitch,
    minor_diameter_mm: major - 1.082532 * pitch,
    values: {
      root_radius: (major - 1.082532 * pitch) / 2,
      pitch,
      tooth_depth: (5 * H) / 8,
      tooth_width: 0.75 * pitch,
      crest_width: pitch / 8,
    },
    conformity: "basic_profile_only_not_certified",
  };
}
/** Basic-profile mating report between an external and an internal thread feature. */
export function threadFit(external: any, internal: any) {
  const same = (key: string) =>
    String(external.construction[key] ?? "") ===
    String(internal.construction[key] ?? "");
  const pitchOf = (f: any) =>
    f.construction.standard === "iso_metric_basic"
      ? isoBasicProfile(f.construction.designation).pitch_mm
      : Number(f.parameters.pitch?.value);
  return {
    method: "basic_profile_designation_pitch_and_handedness_comparison",
    external_mode: external.construction.mode,
    internal_mode: internal.construction.mode,
    designation_match:
      external.construction.standard === "iso_metric_basic" &&
      internal.construction.standard === "iso_metric_basic" &&
      same("designation"),
    pitch_match: Math.abs(pitchOf(external) - pitchOf(internal)) <= 1e-12,
    handedness_match: same("handedness"),
    basic_radial_clearance_mm: 0,
    tolerance_class_fit_modeled: false,
    conformity: "not_certified",
    note: "ISO basic profiles coincide; a real fit requires ISO 965 tolerance classes which are not modeled.",
  };
}
