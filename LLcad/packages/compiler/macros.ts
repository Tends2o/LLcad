/**
 * Compile-time macros (Ob10): a `macro` feature {template, version, params} is expanded
 * deterministically into primitive sub-features before the ordinary compile. The stored
 * IR keeps the macro call; only the compiled plan carries the sub-features.
 *
 * Ids: sub-feature n is `<macro-id>--<n>`, the last one keeps the macro's own id, so
 * outputs, part outputs and dependents keep referencing the macro. (The contract named
 * `<macro-id>/<n>`; `/` is not a valid LLcad id character, would break URL paths, fact
 * keys and result contracts, so `--` is used.)
 *
 * Numeric template parameters live in the feature's `parameters` (Quantities, so
 * set_parameter, expressions and model variables work); string options in
 * construction.params. Every sub-feature is built in the macro's construction frame:
 * the macro's placement/plane composed with the sub-feature's own local offset.
 */
import { Feature, Quantity, MACRO_TEMPLATES } from "../semantic-ir/schema.js";
import { quantity, parse } from "../semantic-ir/units.js";
import { requireThat } from "../semantic-ir/errors.js";
import { resolveExpressions } from "./expressions.js";

type Dimension = "length" | "angle" | "scalar";
export type MacroParam = {
  dimension: Dimension;
  min?: number;
  max?: number;
  integer?: boolean;
  /** Used when the parameter is omitted. */
  default?: number;
  doc: string;
};
export type Frame = { rotation: number[][]; translation: number[] };
type Unit = "mm" | "deg" | "1";
type Sub = {
  key: string;
  operator: string;
  name: string;
  parameters?: Record<string, [number, Unit]>;
  construction?: Record<string, unknown>;
  /** "#<key>" for an earlier sub-feature, "@<n>" for the macro's n-th input. */
  inputs?: string[];
  /** Offset inside the macro frame. */
  local?: Frame;
};
type Template = {
  version: number;
  summary: string;
  frame: string;
  inputs: string;
  params: Record<string, MacroParam>;
  options: Record<string, { values: string[]; default: string; doc: string }>;
  refs: (options: Record<string, string>) => [number, number];
  expand: (v: Record<string, number>, o: Record<string, string>) => Sub[];
  example: { parameters: Record<string, number>; params?: Record<string, string>; depends_on?: string[] };
};

const I = [
  [1, 0, 0],
  [0, 1, 0],
  [0, 0, 1],
];
/** Construction frame translated to (x, y, z). */
const at = (x = 0, y = 0, z = 0): Frame => ({ rotation: I, translation: [x, y, z] });
/** Sketch plane XZ through (x, y, z): sketch x -> X, sketch y -> Z, normal -Y. */
const xzAt = (x = 0, y = 0, z = 0): Frame => ({
  rotation: [
    [1, 0, 0],
    [0, 0, -1],
    [0, 1, 0],
  ],
  translation: [x, y, z],
});
export function compose(a: Frame, b: Frame): Frame {
  const rotation = a.rotation.map((row) =>
    [0, 1, 2].map((j) => row.reduce((sum, v, k) => sum + v * b.rotation[k][j], 0)),
  );
  const translation = a.rotation.map(
    (row, i) => row.reduce((sum, v, k) => sum + v * b.translation[k], 0) + a.translation[i],
  );
  return { rotation, translation };
}
/** Canonical decimal string (no exponent, at most 12 decimals, no trailing zeros). */
export function dec(v: number): string {
  requireThat(
    Number.isFinite(v) && Math.abs(v) <= 1e9,
    "GEOMETRY_INVALID",
    "Macro value outside the numeric range.",
  );
  if (Math.abs(v) < 5e-13) return "0";
  let text = v.toFixed(12);
  if (text.includes(".")) text = text.replace(/0+$/, "").replace(/\.$/, "");
  return text === "-0" ? "0" : text;
}
const P = (...v: number[]) => v.map(dec);
const P2 = (x: number, y: number) => [dec(x), dec(y)];
const L = (v: number): [number, Unit] => [v, "mm"];
const S = (v: number): [number, Unit] => [v, "1"];
const DEG = (v: number): [number, Unit] => [v, "deg"];
const len = (doc: string, extra: Partial<MacroParam> = {}): MacroParam => ({
  dimension: "length",
  min: 0.00001,
  max: 100000,
  doc,
  ...extra,
});
const coord = (doc: string): MacroParam => ({
  dimension: "length",
  min: -1000000,
  max: 1000000,
  default: 0,
  doc,
});
const XYZ = {
  x: coord("x of the anchor (construction frame)"),
  y: coord("y of the anchor"),
  z: coord("z of the anchor"),
};
function check(condition: unknown, message: string): asserts condition {
  requireThat(condition, "GEOMETRY_INVALID", message);
}

/** Closed section in the plane through the axis (y = y0) for a revolved ring. */
function ringSection(
  x: number,
  y: number,
  ri: number,
  ro: number,
  z0: number,
  z1: number,
  co: number,
  ci: number,
) {
  const p = (r: number, z: number) => P(x + r, y, z);
  const points: string[][] = [];
  if (co > 0) points.push(p(ro - co, z0), p(ro, z0 + co), p(ro, z1 - co), p(ro - co, z1));
  else points.push(p(ro, z0), p(ro, z1));
  if (ci > 0) points.push(p(ri + ci, z1), p(ri, z1 - ci), p(ri, z0 + ci), p(ri + ci, z0));
  else points.push(p(ri, z1), p(ri, z0));
  return points;
}

export const MACROS: Record<(typeof MACRO_TEMPLATES)[number], Template> = {
  ring: {
    version: 1,
    summary:
      "Annular ring (inner_radius 0: solid disc) about construction Z with optional 45-degree chamfers on the outer and inner circular edges: ONE exact section revolved once.",
    frame: "Axis = construction Z through (x, y); the ring spans z .. z + height.",
    inputs: "none",
    params: {
      outer_radius: len("outer radius"),
      inner_radius: len("inner radius (0 = solid disc)", { min: 0, default: 0 }),
      height: len("axial height"),
      chamfer_outer: len("45-degree chamfer at both outer circular edges", { min: 0, default: 0 }),
      chamfer_inner: len("45-degree chamfer at both inner circular edges", { min: 0, default: 0 }),
      ...XYZ,
    },
    options: {},
    refs: () => [0, 0],
    expand(v): Sub[] {
      const { outer_radius: ro, inner_radius: ri, height: h, chamfer_outer: co, chamfer_inner: ci } = v;
      check(ri < ro, "ring: inner_radius must be smaller than outer_radius.");
      check(co + ci < ro - ri && 2 * co < h && 2 * ci < h, "ring: chamfers exceed the wall thickness or half the height.");
      check(ri > 0 || ci === 0, "ring: chamfer_inner needs inner_radius > 0.");
      return [
        {
          key: "1",
          operator: "profile",
          name: "section",
          construction: { points: ringSection(v.x, v.y, ri, ro, v.z, v.z + h, co, ci) },
        },
        {
          key: "2",
          operator: "revolve",
          name: "ring",
          parameters: { angle: DEG(360) },
          construction: { axis: P(0, 0, 1), origin: P(v.x, v.y, 0) },
          inputs: ["#1"],
        },
      ];
    },
    example: { parameters: { outer_radius: 12, inner_radius: 9, height: 3, chamfer_outer: 0.4, z: 20 } },
  },
  knurl_ring: {
    version: 1,
    summary:
      "Knurled wheel/ring about construction Z: count straight grooves (profile v|square) of depth; optional bore (inner_radius), plain rims of rim_height at both ends and an outer chamfer. Without rims/chamfer ONE knurl feature, else ring + one multi-tool knurl cut.",
    frame: "Axis = construction Z through (x, y); spans z .. z + height.",
    inputs: "none",
    params: {
      radius: len("outer radius"),
      height: len("axial height"),
      count: { dimension: "scalar", min: 3, max: 1000, integer: true, doc: "number of grooves" },
      depth: len("groove depth (< radius)"),
      width: len("groove width at the outer radius (default: half the pitch)", { default: 0, min: 0 }),
      inner_radius: len("bore radius (0 = solid)", { min: 0, default: 0 }),
      rim_height: len("plain band without grooves at each end", { min: 0, default: 0 }),
      chamfer: len("45-degree chamfer at both outer circular edges", { min: 0, default: 0 }),
      ...XYZ,
    },
    options: {
      profile: { values: ["v", "square"], default: "v", doc: "groove profile" },
    },
    refs: () => [0, 0],
    expand(v, o): Sub[] {
      const width: Record<string, [number, Unit]> =
        v.width > 0 ? { width: L(v.width) } : {};
      check(v.inner_radius < v.radius - v.depth, "knurl_ring: inner_radius must lie inside the groove bottom.");
      if (v.rim_height === 0 && v.chamfer === 0)
        return [
          {
            key: "1",
            operator: "knurl",
            name: "knurl",
            parameters: {
              radius: L(v.radius),
              height: L(v.height),
              count: S(v.count),
              depth: L(v.depth),
              ...width,
              ...((v.inner_radius > 0
                ? { inner_radius: L(v.inner_radius) }
                : {}) as Record<string, [number, Unit]>),
              x: L(v.x),
              y: L(v.y),
              z: L(v.z),
            },
            construction: { profile: o.profile },
          },
        ];
      check(2 * v.rim_height < v.height, "knurl_ring: two rims must be shorter than the height.");
      check(2 * v.chamfer < v.height && v.chamfer < v.radius - v.inner_radius, "knurl_ring: chamfer too large.");
      return [
        {
          key: "1",
          operator: "profile",
          name: "section",
          construction: {
            points: ringSection(v.x, v.y, v.inner_radius, v.radius, v.z, v.z + v.height, v.chamfer, 0),
          },
        },
        {
          key: "2",
          operator: "revolve",
          name: "blank",
          parameters: { angle: DEG(360) },
          construction: { axis: P(0, 0, 1), origin: P(v.x, v.y, 0) },
          inputs: ["#1"],
        },
        {
          key: "3",
          operator: "knurl",
          name: "knurl",
          parameters: {
            radius: L(v.radius),
            height: L(v.height - 2 * v.rim_height),
            count: S(v.count),
            depth: L(v.depth),
            ...width,
            x: L(v.x),
            y: L(v.y),
            z: L(v.z + v.rim_height),
          },
          construction: { profile: o.profile },
          inputs: ["#2"],
        },
      ];
    },
    example: { parameters: { radius: 8, height: 4, count: 36, depth: 0.3, inner_radius: 3, rim_height: 0.5, chamfer: 0.2 } },
  },
  lens: {
    version: 1,
    summary:
      "Spherical lens about construction Z: diameter, center_thickness, radius_front / radius_back (0 = plane) with front/back convex|concave; ONE exact section (true arcs) revolved once. Edge thickness must stay >= 0.01 mm.",
    frame: "Axis = construction Z through (x, y); the back vertex lies at z, the front vertex at z + center_thickness (front faces +Z).",
    inputs: "none",
    params: {
      diameter: len("clear diameter"),
      center_thickness: len("thickness on the axis"),
      radius_front: len("radius of the front (+Z) surface, 0 = plane", { min: 0, default: 0 }),
      radius_back: len("radius of the back surface, 0 = plane", { min: 0, default: 0 }),
      ...XYZ,
    },
    options: {
      front: { values: ["convex", "concave"], default: "convex", doc: "front surface bulges out (+Z) or in" },
      back: { values: ["convex", "concave"], default: "convex", doc: "back surface bulges out (-Z) or in" },
    },
    refs: () => [0, 0],
    expand(v, o): Sub[] {
      const h = v.diameter / 2,
        ct = v.center_thickness;
      for (const R of [v.radius_front, v.radius_back])
        check(R === 0 || R > h * (1 + 1e-9), "lens: the surface radius must be larger than half the diameter.");
      const sag = (R: number, r: number) => (R === 0 ? 0 : R - Math.sqrt(R * R - r * r));
      const zb = (r: number) => (o.back === "convex" ? sag(v.radius_back, r) : -sag(v.radius_back, r));
      const zf = (r: number) => ct + (o.front === "convex" ? -sag(v.radius_front, r) : sag(v.radius_front, r));
      for (const r of [0, h / 4, h / 2, (3 * h) / 4, h])
        check(zf(r) - zb(r) >= 0.01, "lens: the lens becomes thinner than 0.01 mm somewhere (check the edge thickness or the radii).");
      const back =
        v.radius_back === 0
          ? { type: "line", to: P2(h, 0) }
          : { type: "arc3", via: P2(h / 2, zb(h / 2)), to: P2(h, zb(h)) };
      const front =
        v.radius_front === 0
          ? { type: "line", to: P2(0, ct) }
          : { type: "arc3", via: P2(h / 2, zf(h / 2)), to: P2(0, zf(0)) };
      return [
        {
          key: "1",
          operator: "sketch",
          name: "section",
          construction: {
            start: P2(0, zb(0)),
            segments: [back, { type: "line", to: P2(h, zf(h)) }, front, { type: "line", to: P2(0, zb(0)) }],
          },
          local: xzAt(v.x, v.y, v.z),
        },
        {
          key: "2",
          operator: "revolve",
          name: "lens",
          parameters: { angle: DEG(360) },
          construction: { axis: P(0, 0, 1), origin: P(v.x, v.y, 0) },
          inputs: ["#1"],
        },
      ];
    },
    example: { parameters: { diameter: 12, center_thickness: 3, radius_front: 20, radius_back: 40, z: 5 }, params: { back: "concave" } },
  },
  indicator_row: {
    version: 1,
    summary:
      "Row of count indicator windows/LEDs (shape stadium | round | rect) at pitch along X or Y, each an exact sketch extruded by height (+Z), as ONE pattern compound.",
    frame: "Base plane z; (x, y) is the centre of the row (align center) or of the first element (align start).",
    inputs: "none",
    params: {
      count: { dimension: "scalar", min: 1, max: 64, integer: true, doc: "number of elements" },
      pitch: len("centre distance (>= element size along the row)", { min: 0, default: 0 }),
      length: len("element size along the row (stadium: > width; round: ignored)", { default: 0, min: 0 }),
      width: len("element size across the row (round: diameter)"),
      height: len("extrusion height (+Z)"),
      corner_radius: len("rect corner radius", { min: 0, default: 0 }),
      ...XYZ,
    },
    options: {
      shape: { values: ["stadium", "round", "rect"], default: "stadium", doc: "element outline" },
      direction: { values: ["x", "y"], default: "x", doc: "row direction" },
      align: { values: ["center", "start"], default: "center", doc: "(x, y) is the row centre or the first element" },
    },
    refs: () => [0, 0],
    expand(v, o): Sub[] {
      const n = v.count,
        p = v.pitch,
        along = o.shape === "round" ? v.width : v.length;
      check(o.shape === "round" || v.length > 0, "indicator_row: length is missing.");
      check(o.shape !== "stadium" || v.length > v.width, "indicator_row: stadium needs length > width (otherwise use shape round).");
      check(n === 1 || p >= along, "indicator_row: pitch is smaller than the element size; elements would overlap.");
      const offset = o.align === "center" ? (-(n - 1) * p) / 2 : 0;
      const alongX = o.direction === "x";
      const angle = alongX ? 0 : 90;
      const preset =
        o.shape === "round"
          ? { kind: "circle", radius: dec(v.width / 2) }
          : o.shape === "stadium"
            ? { kind: "slot", length: dec(v.length), width: dec(v.width), angle_deg: dec(angle) }
            : {
                kind: "rounded_rect",
                width: dec(v.length),
                height: dec(v.width),
                radius: dec(v.corner_radius),
                angle_deg: dec(angle),
              };
      const subs: Sub[] = [
        {
          key: "1",
          operator: "sketch",
          name: "outline",
          construction: { preset },
          local: at(v.x + (alongX ? offset : 0), v.y + (alongX ? 0 : offset), v.z),
        },
        { key: "2", operator: "extrude", name: "element", parameters: { height: L(v.height) }, construction: {}, inputs: ["#1"] },
      ];
      if (n > 1)
        subs.push({
          key: "3",
          operator: "pattern",
          name: "row",
          parameters: { count: S(n), ...(alongX ? { dx: L(p) } : { dy: L(p) }) },
          construction: {},
          inputs: ["#2"],
        });
      return subs;
    },
    example: { parameters: { count: 3, pitch: 4, length: 2.6, width: 1.2, height: 0.3, z: 10 } },
  },
  control_key: {
    version: 1,
    summary:
      "Push key: rounded-rectangle outline (width x length, corner_radius) extruded by height, optional top-edge fillet (top_radius) and rib grooves across the top (rib_count at rib_pitch, rib_width, rib_depth) in one multi-tool cut. mode recess instead cuts the matching recess (outline + clearance, from z - clearance up through the surface) into the input housing.",
    frame: "(x, y, z) is the centre of the key's base; the key rises along +Z.",
    inputs: "mode key: none; mode recess: 1 housing solid",
    params: {
      width: len("size along X"),
      length: len("size along Y"),
      height: len("height above the base (+Z)"),
      corner_radius: len("outline corner radius", { min: 0, default: 0 }),
      top_radius: len("fillet of the top edges (0 = sharp)", { min: 0, default: 0 }),
      rib_count: { dimension: "scalar", min: 0, max: 64, integer: true, default: 0, doc: "number of grooves across the top" },
      rib_pitch: len("groove spacing", { min: 0, default: 0 }),
      rib_width: len("groove width", { min: 0, default: 0 }),
      rib_depth: len("groove depth", { min: 0, default: 0 }),
      clearance: len("recess: gap around the key and below its base", { min: 0, default: 0.1 }),
      recess_height: len("recess: tool height from z - clearance (default height + clearance + 1)", { min: 0, default: 0 }),
      ...XYZ,
    },
    options: {
      mode: { values: ["key", "recess"], default: "key", doc: "the key itself, or its recess cut into the input housing" },
      rib_direction: { values: ["y", "x"], default: "y", doc: "grooves run parallel to Y (spaced along X) or to X" },
    },
    refs: (o) => (o.mode === "recess" ? [1, 1] : [0, 0]),
    expand(v, o): Sub[] {
      if (o.mode === "recess") {
        const c = v.clearance;
        const tool = v.recess_height > 0 ? v.recess_height : v.height + c + 1;
        return [
          {
            key: "1",
            operator: "sketch",
            name: "recess outline",
            construction: {
              preset: {
                kind: "rounded_rect",
                width: dec(v.width + 2 * c),
                height: dec(v.length + 2 * c),
                radius: dec(v.corner_radius > 0 ? v.corner_radius + c : 0),
              },
            },
            local: at(v.x, v.y, v.z - c),
          },
          { key: "2", operator: "extrude", name: "recess tool", parameters: { height: L(tool) }, construction: {}, inputs: ["#1"] },
          { key: "3", operator: "difference", name: "housing with recess", construction: {}, inputs: ["@0", "#2"] },
        ];
      }
      check(2 * v.corner_radius <= Math.min(v.width, v.length), "control_key: corner_radius exceeds half the edge length.");
      const subs: Sub[] = [
        {
          key: "1",
          operator: "sketch",
          name: "outline",
          construction: {
            preset: { kind: "rounded_rect", width: dec(v.width), height: dec(v.length), radius: dec(v.corner_radius) },
          },
          local: at(v.x, v.y, v.z),
        },
        { key: "2", operator: "extrude", name: "key body", parameters: { height: L(v.height) }, construction: {}, inputs: ["#1"] },
      ];
      let last = "#2";
      if (v.top_radius > 0) {
        check(
          v.top_radius < v.height && v.top_radius < Math.min(v.width, v.length) / 2,
          "control_key: top_radius must be smaller than the height and half the width.",
        );
        subs.push({
          key: "3",
          operator: "fillet",
          name: "top edges",
          parameters: { radius: L(v.top_radius) },
          construction: { edge_selector: { between_features: ["#2", "#2"], roles: ["end_cap", null] } },
          inputs: ["#2"],
        });
        last = "#3";
      }
      if (v.rib_count >= 1) {
        const n = v.rib_count,
          p = v.rib_pitch,
          rw = v.rib_width,
          rd = v.rib_depth;
        check(rw > 0 && rd > 0 && rd < v.height, "control_key: ribs need rib_width > 0 and 0 < rib_depth < height.");
        check(n === 1 || p > rw, "control_key: rib_pitch must be larger than rib_width.");
        const acrossX = o.rib_direction === "y";
        const span = (n - 1) * p + rw;
        check(span < (acrossX ? v.width : v.length), "control_key: the rib field is wider than the key.");
        const box = acrossX
          ? { width: L(rw), depth: L(v.length + 2), height: L(rd + 1), x: L(v.x - span / 2), y: L(v.y - v.length / 2 - 1), z: L(v.z + v.height - rd) }
          : { width: L(v.width + 2), depth: L(rw), height: L(rd + 1), x: L(v.x - v.width / 2 - 1), y: L(v.y - span / 2), z: L(v.z + v.height - rd) };
        subs.push(
          { key: "4", operator: "box", name: "groove tool", parameters: box, construction: {} },
          {
            key: "5",
            operator: "cut_pattern",
            name: "ribbed key",
            parameters: { count: S(n), ...(n > 1 ? (acrossX ? { dx: L(p) } : { dy: L(p) }) : {}) },
            construction: { kind: "linear", operation: "difference" },
            inputs: [last, "#4"],
          },
        );
      }
      return subs;
    },
    example: { parameters: { width: 6, length: 9, height: 1.2, corner_radius: 1.5, top_radius: 0.4, z: 12 } },
  },
  usb_c_receptacle: {
    version: 1,
    summary:
      "USB Type-C receptacle as seen from outside: stadium shell (width x height outer, wall) open at z and going depth into -Z, closed back and the centre tongue; mode cutout instead cuts the opening (+ clearance) into the input housing. Defaults follow the Type-C receptacle outline (8.94 x 3.26 mm).",
    frame: "(x, y, z) is the centre of the opening; width along X, height along Y, the port goes into -Z.",
    inputs: "mode shell: none; mode cutout: 1 housing solid",
    params: {
      width: len("outer shell width (X)", { default: 8.94 }),
      height: len("outer shell height (Y)", { default: 3.26 }),
      wall: len("shell wall", { default: 0.25 }),
      depth: len("depth into -Z", { default: 6.6 }),
      tongue_width: len("tongue width", { default: 6.69 }),
      tongue_thickness: len("tongue thickness", { default: 0.7 }),
      tongue_length: len("tongue length from the back wall", { default: 4.4 }),
      clearance: len("cutout: gap around the shell", { min: 0, default: 0.05 }),
      ...XYZ,
    },
    options: {
      mode: { values: ["shell", "cutout"], default: "shell", doc: "receptacle body, or its opening cut into the input housing" },
    },
    refs: (o) => (o.mode === "cutout" ? [1, 1] : [0, 0]),
    expand(v, o): Sub[] {
      check(v.width > v.height, "usb_c_receptacle: width must be larger than height.");
      if (o.mode === "cutout") {
        const c = v.clearance;
        return [
          {
            key: "1",
            operator: "sketch",
            name: "opening",
            construction: { preset: { kind: "slot", length: dec(v.width + 2 * c), width: dec(v.height + 2 * c) } },
            local: at(v.x, v.y, v.z + 1),
          },
          {
            key: "2",
            operator: "extrude",
            name: "opening tool",
            parameters: { height: L(v.depth + 1 + c) },
            construction: { direction: "-" },
            inputs: ["#1"],
          },
          { key: "3", operator: "difference", name: "housing with port", construction: {}, inputs: ["@0", "#2"] },
        ];
      }
      const iw = v.width - 2 * v.wall,
        ih = v.height - 2 * v.wall;
      check(ih > 0 && iw > ih, "usb_c_receptacle: wall too thick for the cross section.");
      check(v.tongue_width < iw && v.tongue_thickness < ih, "usb_c_receptacle: the tongue does not fit into the opening.");
      check(v.tongue_length + v.wall < v.depth, "usb_c_receptacle: the tongue is longer than the shaft.");
      const outer = { kind: "slot", length: dec(v.width), width: dec(v.height) };
      return [
        {
          key: "1",
          operator: "sketch",
          name: "shell section",
          construction: { preset: outer, holes: [{ preset: { kind: "slot", length: dec(iw), width: dec(ih) } }] },
          local: at(v.x, v.y, v.z),
        },
        { key: "2", operator: "extrude", name: "shell", parameters: { height: L(v.depth) }, construction: { direction: "-" }, inputs: ["#1"] },
        { key: "3", operator: "sketch", name: "back outline", construction: { preset: outer }, local: at(v.x, v.y, v.z - v.depth) },
        { key: "4", operator: "extrude", name: "back wall", parameters: { height: L(v.wall) }, construction: {}, inputs: ["#3"] },
        {
          key: "5",
          operator: "box",
          name: "tongue",
          parameters: {
            width: L(v.tongue_width),
            depth: L(v.tongue_thickness),
            height: L(v.tongue_length + v.wall / 2),
            x: L(v.x - v.tongue_width / 2),
            y: L(v.y - v.tongue_thickness / 2),
            z: L(v.z - v.depth + v.wall / 2),
          },
          construction: {},
        },
        { key: "6", operator: "union", name: "receptacle", construction: {}, inputs: ["#2", "#4", "#5"] },
      ];
    },
    example: { parameters: { z: 0 }, params: { mode: "shell" } },
  },
};

export type MacroCall = { id: string; template: string; version: number; index: number };
/** Numeric values of a macro feature (defaults applied) after its own expressions. */
export function macroValues(f: Feature, variables: Record<string, Quantity>) {
  const c: any = f.construction;
  const template = MACROS[c.template as keyof typeof MACROS];
  requireThat(template, "INVALID_SCHEMA", `Unknown macro template ${c.template}.`);
  requireThat(
    c.version === template.version,
    "BUILD_MISMATCH",
    `Macro ${c.template} is registered in version ${template.version}, not ${c.version}.`,
    { supported_version: template.version },
  );
  resolveExpressions(f as any, template.params, variables);
  for (const k of Object.keys(f.parameters))
    requireThat(
      Object.hasOwn(template.params, k),
      "INVALID_SCHEMA",
      `Parameter ${k} is not registered for macro ${c.template}.`,
      { parameters: Object.keys(template.params) },
    );
  const values: Record<string, number> = {};
  for (const [k, spec] of Object.entries(template.params)) {
    const q = f.parameters[k];
    if (!q) {
      requireThat(spec.default !== undefined, "INVALID_SCHEMA", `Macro parameter ${k} is missing (${c.template}).`);
      values[k] = spec.default!;
      continue;
    }
    const value = quantity(q, spec.dimension);
    requireThat(
      value >= (spec.min ?? -Infinity) && value <= (spec.max ?? Infinity) && (!spec.integer || Number.isInteger(value)),
      "GEOMETRY_INVALID",
      `Macro parameter ${k} is outside its contract (${c.template}).`,
      { minimum: spec.min ?? null, maximum: spec.max ?? null, integer: !!spec.integer },
    );
    values[k] = value;
  }
  const options: Record<string, string> = {};
  for (const [k, spec] of Object.entries(template.options)) options[k] = spec.default;
  for (const [k, value] of Object.entries<string>(c.params ?? {})) {
    requireThat(
      Object.hasOwn(template.options, k) && template.options[k].values.includes(value),
      "INVALID_SCHEMA",
      `Macro option ${k}=${value} is not registered for ${c.template}.`,
      { options: Object.fromEntries(Object.entries(template.options).map(([n, o]) => [n, o.values])) },
    );
    options[k] = value;
  }
  return { template, values, options };
}
/** Expand one macro feature into canonical primitive features (last one = the macro id). */
export function expandMacro(
  f: Feature,
  frame: Frame | null,
  variables: Record<string, Quantity>,
): Feature[] {
  const c: any = f.construction;
  requireThat(
    f.authoritative_representation === "brep",
    "OUT_OF_SCOPE",
    "Macros produce B-Rep features.",
  );
  requireThat(f.id.length <= 120, "INVALID_SCHEMA", "Macro ids are limited to 120 characters (sub-features append --<n>).");
  const { template, values, options } = macroValues(f, variables);
  const [minRefs, maxRefs] = template.refs(options);
  requireThat(
    f.depends_on.length >= minRefs && f.depends_on.length <= maxRefs,
    "INVALID_SCHEMA",
    `Macro ${c.template} expects ${minRefs === maxRefs ? minRefs : `${minRefs}..${maxRefs}`} inputs.`,
  );
  const subs = template.expand(values, options);
  const ids = new Map(
    subs.map((s, i) => [s.key, i === subs.length - 1 ? f.id : `${f.id}--${s.key}`]),
  );
  const ref = (token: string) => {
    if (token.startsWith("@")) return f.depends_on[Number(token.slice(1))];
    if (token.startsWith("#")) return ids.get(token.slice(1))!;
    return token;
  };
  const resolve = (value: any): any =>
    typeof value === "string" && /^[#@][0-9]+$/.test(value)
      ? ref(value)
      : Array.isArray(value)
        ? value.map(resolve)
        : value && typeof value === "object"
          ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, resolve(v)]))
          : value;
  const base = frame ?? { rotation: I, translation: [0, 0, 0] };
  return subs.map((s, index) => {
    const placed = compose(base, s.local ?? at());
    const identity =
      placed.translation.every((t) => Math.abs(t) < 1e-15) &&
      placed.rotation.every((row, i) => row.every((v, j) => Math.abs(v - I[i][j]) < 1e-15));
    const feature = parse<Feature>(Feature, {
      id: ids.get(s.key)!,
      semantic_name: `${f.semantic_name} / ${s.name}`.slice(0, 200),
      kind: s.operator,
      owner_part: f.owner_part,
      local_frame: f.local_frame,
      authoritative_representation: "brep",
      parameters: Object.fromEntries(
        Object.entries(s.parameters ?? {}).map(([k, [v, unit]]) => [k, { value: dec(v), unit }]),
      ),
      construction: { operator: s.operator, ...resolve(s.construction ?? {}) },
      ...(identity
        ? {}
        : {
            placement: {
              origin: P(...placed.translation),
              x_axis: P(...placed.rotation.map((row) => row[0])),
              z_axis: P(...placed.rotation.map((row) => row[2])),
            },
          }),
      depends_on: (s.inputs ?? []).map(ref),
    });
    return Object.assign(feature, {
      macro_call: { id: f.id, template: c.template, version: c.version, index: index + 1 } as MacroCall,
    });
  });
}
/** Template documentation for agents (llcad://operators, schemas/operator-docs.json). */
export function macroDocs() {
  return Object.fromEntries(
    Object.entries(MACROS).map(([name, t]) => [
      name,
      {
        version: t.version,
        summary: t.summary,
        frame: t.frame,
        inputs: t.inputs,
        parameters: t.params,
        options: t.options,
        ids: "sub-features <id>--<n>; the last one keeps the macro id",
        example: {
          op: "add_feature",
          feature: {
            id: name.replaceAll("_", "-") + "-1",
            semantic_name: name,
            construction: { operator: "macro", template: name, version: t.version, ...(t.example.params ? { params: t.example.params } : {}) },
            parameters: t.example.parameters,
            ...(t.example.depends_on ? { depends_on: t.example.depends_on } : {}),
          },
        },
      },
    ]),
  );
}
