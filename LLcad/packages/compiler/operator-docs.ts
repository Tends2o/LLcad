/**
 * Operator semantics for agents and documentation (Oa6): per operator the frame, axis,
 * anchor and sign conventions, required inputs, limits, pitfalls and ONE minimal valid
 * add_feature example. Checked against the worker code (workers/cad-occt). Kept out of
 * OPERATORS on purpose: documentation edits must not change the registry hash and with
 * it every cache key. `npm run schemas` merges it into schemas/operator-registry.json
 * (per-operator `semantics`) and writes schemas/operator-docs.json.
 */
import { OPERATORS } from "./index.js";
import { macroDocs } from "./macros.js";

export type OperatorSemantics = {
  summary: string;
  /** Where the operator builds: construction frame, axes, what the numbers mean. */
  frame: string;
  /** Which point the position parameters name. */
  anchor?: string;
  /** Sign and orientation conventions. */
  sign?: string;
  /** Kinds of depends_on inputs, in order. */
  inputs: string;
  limits?: string;
  pitfalls?: string[];
  example: unknown;
};

const q = (value: string, unit = "mm") => ({ value, unit });
const P = (...v: number[]) => v.map(String);
const P2 = (x: number, y: number) => [String(x), String(y)];
function add(
  id: string,
  operator: string,
  parameters: Record<string, { value: string; unit: string }> = {},
  construction: Record<string, unknown> = {},
  depends_on: string[] = [],
  extra: Record<string, unknown> = {},
) {
  return {
    op: "add_feature",
    feature: {
      id,
      semantic_name: id,
      kind: operator,
      parameters,
      construction: { operator, ...construction },
      depends_on,
      ...extra,
    },
  };
}

const LOCAL =
  "Built in the feature's construction frame: its local_frame (a structure frame, default world), refined by the feature's own `placement` {origin, x_axis, z_axis} or `plane` preset (XY | XZ | YZ | front | side | back) when given; inputs from other frames are transformed into it automatically.";
const NONE = "none";

export const COMMON_CONVENTIONS = [
  'Units: every parameter is {"value": "<decimal string>", "unit": ...}; lengths mm, angles deg (or rad), scalars "1".',
  "Every feature may carry `placement` {origin, x_axis, z_axis} (orthogonal axes) or `plane` (XY | XZ=front: x->X, y->Z, normal -Y | YZ=side: x->Y, y->Z, normal +X | back: x->-X, y->Z, normal +Y). The feature is then built in that frame: its inputs are seen in it and its result is placed back. B-Rep features only.",
  "A profile/sketch placed on a plane is extruded along that plane's normal automatically (unless the extrude names its own axis or placement).",
  "Closed profiles are normalised to counter-clockwise before extrude/revolve; the reversal is reported as a warning (cad_job_get warnings), never silent.",
  "precision_cad outputs must be closed solids: curves, profiles, sketches and sheets are construction inputs only.",
  "Booleans take at most 16 inputs and run as ONE multi-tool operation; stage bigger ones as a tree or use cut_pattern.",
  "Relief (text, svg_emblem): depth >= 0.05 mm; + embosses (fused into the target), - debosses (cut). Thinner prints belong in decals, not geometry.",
];

export const OPERATOR_SEMANTICS: Record<string, OperatorSemantics> = {
  point: {
    summary: "Vertex at (x, y, z). Construction helper, never an output.",
    frame: LOCAL,
    inputs: NONE,
    example: add("pt", "point", { x: q("1") }),
  },
  line: {
    summary: "Straight wire from construction.start to construction.end.",
    frame: LOCAL,
    inputs: NONE,
    pitfalls: ["start and end must differ."],
    example: add("ln", "line", {}, { start: P(0, 0, 0), end: P(10, 0, 0) }),
  },
  arc: {
    summary: "Circular arc wire through three points: start, a point on the arc, end.",
    frame: LOCAL,
    inputs: NONE,
    pitfalls: ["The three points must not be collinear."],
    example: add("ar", "arc", {}, { points: [P(5, 0, 0), P(0, 5, 0), P(-5, 0, 0)] }),
  },
  plane: {
    summary: "Planar rectangular sheet (a face, not a solid).",
    frame: LOCAL,
    anchor: "(x, y, z) is the corner; width along +X, height along +Y.",
    inputs: NONE,
    example: add("pl", "plane", { width: q("20"), height: q("10") }),
  },
  trim_surface: {
    summary: "Trims the single face of its input to the normalised parameter box [u_min,u_max] x [v_min,v_max].",
    frame: "Parameter space of the input face (0..1).",
    inputs: "1 face (nurbs_surface)",
    example: add("tr", "trim_surface", { u_min: q("0", "1"), u_max: q("0.5", "1"), v_min: q("0", "1"), v_max: q("1", "1") }, {}, ["srf"]),
  },
  cap: {
    summary: "Planar face bounded by the closed planar wire of input 1; further input wires become holes.",
    frame: LOCAL,
    inputs: "1..16 closed planar wires (first = outer)",
    example: add("cp", "cap", {}, {}, ["circ"]),
  },
  sew: {
    summary: "Sews 2..128 faces/shells within `tolerance`; make_solid (default true) needs a closed shell and returns a solid.",
    frame: LOCAL,
    inputs: "2..128 faces or shells",
    example: add("sw", "sew", { tolerance: q("0.001") }, { make_solid: true }, ["fa", "fb"]),
  },
  regularize: {
    summary: "Merges same-domain faces and edges (UnifySameDomain) after booleans; geometry unchanged.",
    frame: LOCAL,
    inputs: "1 solid",
    example: add("rg", "regularize", {}, {}, ["body"]),
  },
  thread: {
    summary: "Helical thread about construction +Z from z = 0 to height. external: ridge on a root cylinder; internal: cut from its input. standard iso_metric_basic + designation (M6, M8x1) derives pitch and depths.",
    frame: LOCAL,
    anchor: "Axis = construction Z through the origin (use placement to move it).",
    sign: "handedness right|left.",
    inputs: "external: none; internal: 1 solid",
    limits: "at most 32 turns per feature; runout < height/2",
    example: add("th", "thread", { height: q("12") }, { mode: "external", handedness: "right", standard: "iso_metric_basic", designation: "M6" }),
  },
  offset_solid: {
    summary: "Offsets every face of a solid by distance; self-intersecting results are rejected.",
    frame: LOCAL,
    sign: "+ grows, - shrinks.",
    inputs: "1 solid",
    example: add("of", "offset_solid", { distance: q("-0.5") }, {}, ["body"]),
  },
  box: {
    summary: "Axis-aligned box.",
    frame: LOCAL,
    anchor: "(x, y, z) is the MIN CORNER, not the centre; width along +X, depth +Y, height +Z.",
    inputs: NONE,
    pitfalls: ["Centre = (x + width/2, y + depth/2, z + height/2)."],
    example: add("bx", "box", { width: q("30"), depth: q("20"), height: q("10"), x: q("-15"), y: q("-10") }),
  },
  sphere: {
    summary: "Sphere.",
    frame: LOCAL,
    anchor: "(x, y, z) is the centre.",
    inputs: NONE,
    example: add("sp", "sphere", { radius: q("5"), z: q("10") }),
  },
  cylinder: {
    summary: "Cylinder along construction +Z from z to z + height.",
    frame: LOCAL,
    anchor: "(x, y, z) is the BASE centre.",
    inputs: NONE,
    pitfalls: ["For another axis give the cylinder a placement/plane instead of a rotate feature."],
    example: add("cy", "cylinder", { radius: q("4"), height: q("12"), z: q("10") }),
  },
  cone: {
    summary: "Truncated cone along +Z; top_radius 0 gives an apex.",
    frame: LOCAL,
    anchor: "(x, y, z) is the base centre (radius there, top_radius at z + height).",
    inputs: NONE,
    example: add("co", "cone", { radius: q("6"), top_radius: q("3"), height: q("8") }),
  },
  torus: {
    summary: "Torus in the construction XY plane, axis Z.",
    frame: LOCAL,
    anchor: "(x, y, z) is the centre.",
    inputs: NONE,
    limits: "major_radius > minor_radius",
    example: add("to", "torus", { major_radius: q("10"), minor_radius: q("1.5") }),
  },
  profile: {
    summary: "Closed straight-segment polygon wire (3..256 points, closed automatically). Prefer `sketch` for arcs, corner radii and splines.",
    frame: LOCAL,
    sign: "Counter-clockwise seen from +Z of its plane; clockwise input is reversed at extrude/revolve with a warning.",
    inputs: NONE,
    pitfalls: ["Do not repeat the first point.", "Hand-tessellated arcs give faceted bodies: use sketch arcs/corner_radius."],
    example: add("pr", "profile", {}, { points: [P(0, 0, 0), P(20, 0, 0), P(20, 10, 0), P(0, 10, 0)] }),
  },
  sketch: {
    summary: "Planar region with EXACT edges: segments line | arc3 (through a point) | tangent_arc | center_arc (cw/ccw) | spline (through points), per-vertex corner_radius (plus a default corner_radius), or ONE preset: rounded_rect (radius or radii [bl, br, tr, tl]), slot, ellipse, circle, spline_through (periodic by default), superellipse. `holes` are further loops subtracted from the outer one. Output: one planar face, usable by extrude, revolve, loft (outer wire), sweep and cap.",
    frame: `${LOCAL} Points are 2D [x, y] in the construction XY plane; construction.plane is an alias of the feature plane.`,
    anchor: "Presets are centred on `center` (default origin) and rotated by angle_deg; the seam (loop start) of every preset with single_edge lies on its +X axis.",
    sign: "Loops are normalised counter-clockwise (reported as orientation_normalised); corner_radius at a segment rounds the vertex at its `to` point; tangent joints are left sharp (reported when explicitly requested).",
    inputs: NONE,
    limits: "256 segments per loop, 32 holes, 1024 edges",
    pitfalls: [
      "A preset takes its radii in the preset (radius / radii), not corner_radius.",
      "Lofts need equal edge counts per section: use the same preset for every section, or single_edge: true (one exact B-spline edge per loop) to loft a rounded_rect into splines/ellipses.",
      "center_arc: start and end must be equidistant from the centre.",
    ],
    example: add("sk", "sketch", {}, { preset: { kind: "rounded_rect", width: "30", height: "20", radius: "3" }, holes: [{ preset: { kind: "circle", radius: "2", center: P2(8, 0) } }] }, [], { plane: "XZ" }),
  },
  text: {
    summary: "Lettering as ONE feature: glyph outlines of an installed open font (DejaVu Sans [Condensed], Liberation Sans [Narrow], Inter; styles regular/bold/italic/bold_italic, Inter also medium/semibold) through OCCT StdPrs_BRepFont, with kerning. cap_height sets the height of capitals; tracking adds spacing (mm); align left|center|right; valign baseline|center|top; multi-line with \\n (line_spacing). arc {radius, center_angle_deg, side outside|inside, direction cw|ccw, surface plane|cylinder} sets the text on a circle in the plane or wraps it round a cylinder about construction Z.",
    frame: `${LOCAL} Text reads along +X with capitals up (+Y), relief along +Z.`,
    anchor: "Origin = start (left), centre or end of the baseline per align; on an arc the anchor sits at center_angle_deg (measured from +X, CCW) on the circle of `radius`.",
    sign: "depth + embosses (from z = 0 up; with a target fused into it, reaching `overlap` below z = 0), - debosses (cut from z = 0 down to depth). Plane arc: side outside = glyphs outside the circle reading clockwise, inside = inside reading counter-clockwise. Cylinder: side outside reads left-to-right seen from outside; the relief is radial.",
    inputs: "none (lettering solid only) or 1 target solid (fused/cut in one Boolean)",
    limits: "512 glyphs, 16 lines, |depth| >= 0.05 mm, cap_height >= 0.1 mm",
    pitfalls: [
      "Emboss letters must touch the target: place z = 0 on the target face (placement/plane) or raise overlap for curved faces.",
      "Cylinder wrap lays each glyph flat on its tangent plane (max_sagitta_mm in the report); fine for glyphs small against the radius.",
      "Nimbus/URW fonts are never used (licence).",
    ],
    example: add("lbl", "text", { cap_height: q("2.5"), depth: q("0.1") }, { string: "LLcad", font: "DejaVu Sans", style: "bold", align: "center" }, [], { placement: { origin: P(0, 0, 10), x_axis: P(1, 0, 0), z_axis: P(0, -1, 0) } }),
  },
  svg_emblem: {
    summary: "Logo/emblem from ORIGINAL SVG vectors as ONE feature: path, polygon, polyline, rect, circle, ellipse elements (transforms applied, arcs as cubic Beziers) become exact line/Bezier wires with holes by the element's fill rule; element_filter picks elements by ids, classes, fills (e.g. [\"#FFFFFF\"]) and exclude_ids. Source: artifact_id (uploaded file) or path (allowed folders, LLCAD_ASSET_PATHS; `sha256` pins the file).",
    frame: `${LOCAL} The SVG is read with y up (SVG y is flipped), relief along +Z.`,
    anchor: "anchor center (default) | bottom_left | bottom_center | top_left | svg_origin of the reference box; fit_to viewbox (default: sub-elements keep their place in the whole drawing) or selection.",
    sign: "Scale by width_mm OR height_mm of the reference box; without both the file's physical width (mm/cm/in/pt/px units) is used. depth +/- as for text.",
    inputs: "none or 1 target solid",
    limits: "1 MiB file, 4096 contours, |depth| >= 0.05 mm",
    pitfalls: ["Never redraw brand marks: reference the original SVG.", "Unsupported elements (<use>, text, strokes without fill) are skipped and reported."],
    example: add("logo", "svg_emblem", { depth: q("0.1") }, { path: "/root/projects/example-device/vectors/logo.svg", element_filter: { fills: ["#FFFFFF"] }, width_mm: "7.03" }),
  },
  strip: {
    summary: "Thick polyline solid: a width-wide band along each path (round joints, optional rectangular pads), all in ONE z plane, extruded +Z by height; one connected region.",
    frame: LOCAL,
    sign: "Orientation fixed automatically.",
    inputs: NONE,
    example: add("st", "strip", { width: q("0.3"), height: q("0.1") }, { paths: [[P(0, 0, 0), P(5, 0, 0), P(5, 3, 0)]] }),
  },
  circle: {
    summary: "Circle wire (one edge) in the construction XY plane.",
    frame: LOCAL,
    anchor: "(x, y, z) is the centre.",
    inputs: NONE,
    example: add("ci", "circle", { radius: q("5") }),
  },
  bezier: {
    summary: "Single Bezier curve wire; points are control points.",
    frame: LOCAL,
    inputs: NONE,
    example: add("bz", "bezier", {}, { points: [P(0, 0, 0), P(5, 5, 0), P(10, 0, 0)] }),
  },
  bspline: {
    summary: "B-spline FITTED THROUGH the points (approximation, not control points).",
    frame: LOCAL,
    inputs: NONE,
    example: add("bs", "bspline", {}, { points: [P(0, 0, 0), P(5, 2, 0), P(10, 0, 0)] }),
  },
  nurbs_curve: {
    summary: "Exact rational B-spline wire from poles, weights and a clamped basis; C0 knots become edge breaks.",
    frame: LOCAL,
    sign: "A closed curve (last pole = first) is normalised counter-clockwise at extrude/revolve (warning).",
    inputs: NONE,
    example: add("nc", "nurbs_curve", {}, { poles: [P(0, 0, 0), P(3, 0, 0), P(6, 0, 0), P(9, 0, 0)], weights: ["1", "1", "1", "1"], basis: { degree: 3, knots: ["0", "1"], multiplicities: [4, 4] } }),
  },
  nurbs_surface: {
    summary: "Rational B-spline face from a pole grid (2..16 per direction); u/v bases default to Bezier.",
    frame: LOCAL,
    inputs: NONE,
    example: add("ns", "nurbs_surface", {}, { poles: [[P(0, 0, 0), P(0, 10, 0)], [P(10, 0, 0), P(10, 10, 1)]], weights: [["1", "1"], ["1", "1"]] }),
  },
  extrude: {
    summary: "Prism of the input region (profile/circle/curve wire or sketch face, holes kept) by height.",
    frame: `${LOCAL} Direction: construction.axis if given, else the normal of a placed/plane input profile, else construction +Z.`,
    sign: "direction '+' (default) along the axis, '-' against it, 'both' symmetric about the profile plane (total = height). A clockwise profile is reversed with a warning.",
    inputs: "1 closed planar wire or sketch face",
    pitfalls: ["Give the profile (or sketch) a plane/placement to extrude in other directions; no rotate chain needed."],
    example: add("ex", "extrude", { height: q("5") }, { direction: "both" }, ["sk"]),
  },
  revolve: {
    summary: "Revolves the input region about an axis by angle.",
    frame: `${LOCAL} Axis: construction.axis through construction.origin (default construction Z through the origin).`,
    sign: "Positive angle turns counter-clockwise about the axis; a profile orientation that would give a negative volume is reversed (warning).",
    inputs: "1 closed planar wire or sketch face (on one side of the axis)",
    limits: "|angle| <= 360 deg",
    example: add("rv", "revolve", { angle: q("360", "deg") }, { axis: P(0, 0, 1), origin: P(0, 0, 0) }, ["sec"]),
  },
  loft: {
    summary: "Solid through 2..16 section wires in order (sketch faces contribute their outer wire).",
    frame: LOCAL,
    inputs: "2..16 closed section wires or sketches",
    pitfalls: ["Sections need equal edge counts (check_compatibility): same preset per section or single_edge sketches.", "Align section seams (start points) to avoid twist; single_edge presets all start on +X."],
    example: add("lf", "loft", {}, { ruled: false }, ["sec0", "sec1", "sec2"]),
  },
  sweep: {
    summary: "Sweeps the profile (input 1) along the spine wire (input 2). With a third input the sweep is GUIDED: the auxiliary spine fixes the section orientation along the path (guide_contact none|contact|contact_on_border) and scale_end is a linear scaling law. Without a guide: twist / scale_end / frame rotation_minimizing use sectioned sweeps.",
    frame: LOCAL,
    inputs: "profile (wire or sketch face), spine wire, optional auxiliary spine wire",
    pitfalls: ["twist is not combined with a guide.", "self_contact_check (default on) rejects self-intersecting sweeps."],
    example: add("sw", "sweep", { scale_end: q("0.5", "1") }, { guide_contact: "none" }, ["prof", "spine", "guide"]),
  },
  union: {
    summary: "Boolean fuse of 2..16 solids in one multi-tool operation.",
    frame: LOCAL,
    inputs: "2..16 solids",
    example: add("un", "union", {}, {}, ["bx", "cy"]),
  },
  intersection: {
    summary: "Boolean common of 2..16 solids (pairwise chain).",
    frame: LOCAL,
    inputs: "2..16 solids",
    example: add("is", "intersection", {}, {}, ["bx", "sp"]),
  },
  difference: {
    summary: "First input minus the other 1..15 inputs, one multi-tool cut.",
    frame: LOCAL,
    inputs: "target solid, 1..15 tools",
    example: add("df", "difference", {}, {}, ["bx", "cy"]),
  },
  hole: {
    summary: "Cylindrical cut of radius going DOWN (-Z) from (x, y, z) by depth; must remove material.",
    frame: LOCAL,
    anchor: "(x, y, z) is the centre of the hole opening; put z on the top face (or give the hole a placement whose +Z points out of the face).",
    inputs: "1 solid",
    example: add("hl", "hole", { radius: q("2"), depth: q("5"), z: q("10") }, {}, ["bx"]),
  },
  pocket: {
    summary: "Rectangular cut centred at (x, y), width along X, length along Y, DOWN from z by depth.",
    frame: LOCAL,
    anchor: "(x, y, z) is the centre of the opening.",
    inputs: "1 solid",
    example: add("pk", "pocket", { width: q("8"), length: q("4"), depth: q("2"), z: q("10") }, {}, ["bx"]),
  },
  groove: {
    summary: "Annular cut (mean radius, width) centred at (x, y), DOWN from z by depth.",
    frame: LOCAL,
    anchor: "(x, y, z) is the axis point on the opening plane.",
    inputs: "1 solid",
    limits: "radius > width / 2",
    example: add("gv", "groove", { radius: q("6"), width: q("1"), depth: q("1"), z: q("10") }, {}, ["bx"]),
  },
  fillet: {
    summary: "Fillet of the edges chosen by edge_selector: all | vertical | horizontal (bounding box in the construction frame) or {between_features: [A, B], roles?: [roleA|null, roleB|null]} = edges where a face made by feature A meets a face made by feature B (face provenance of the input). radius_end makes the radius vary linearly along each edge.",
    frame: LOCAL,
    inputs: "1 solid",
    pitfalls: ["Fillet single bodies before booleans where possible; incompatible edges are skipped (warning, at most 1/3).", "Blends on freeform surfaces may exceed a 0.001 mm model tolerance."],
    example: add("fl", "fillet", { radius: q("1"), radius_end: q("2") }, { edge_selector: { between_features: ["bx", "cy"] } }, ["un"]),
  },
  chamfer: {
    summary: "Equal-distance chamfer; same edge selectors as fillet (including between_features).",
    frame: LOCAL,
    inputs: "1 solid",
    example: add("ch", "chamfer", { distance: q("0.5") }, { edge_selector: "horizontal" }, ["bx"]),
  },
  shell: {
    summary: "Hollows a solid inward by thickness, removing the opening face(s): top | bottom (the single planar face at max/min z) or a face selector {feature, role?} (faces made by that feature) or {normal, angle_tolerance_deg?} (faces whose outward normal matches).",
    frame: LOCAL,
    inputs: "1 solid",
    example: add("sh", "shell", { thickness: q("1.2") }, { opening: { normal: P(0, 0, 1) } }, ["bx"]),
  },
  draft: {
    summary: "Draft (taper) of faces by angle relative to a pull direction about a neutral plane (BRepOffsetAPI_DraftAngle). faces: walls (default, every face parallel to the pull direction) or a face selector {feature, role?} / {normal}.",
    frame: `${LOCAL} pull_direction default construction +Z; neutral_plane {point, normal} default through the origin, normal = pull direction.`,
    sign: "Positive angle tapers the faces inward along the pull direction (mould release: the body gets smaller away from the neutral plane).",
    inputs: "1 solid",
    limits: "|angle| <= 80 deg, non-zero",
    example: add("dr", "draft", { angle: q("2", "deg") }, { faces: "walls", pull_direction: P(0, 0, 1), neutral_plane: { point: P(0, 0, 0) } }, ["bx"]),
  },
  split_by_plane: {
    summary: "Splits a solid by a plane (BRepAlgoAPI_Splitter): keep both (compound of the pieces), above (the side the normal points to) or below. For colour zones and parting lines: one feature per side, each in its own part.",
    frame: LOCAL,
    inputs: "1 solid",
    pitfalls: ["The plane must cut the body (else GEOMETRY_INVALID)."],
    example: add("za", "split_by_plane", {}, { plane: { point: P(0, 0, 8), normal: P(0, 0, 1) }, keep: "above" }, ["bx"]),
  },
  knurl: {
    summary: "Straight knurl of count grooves (profile v | square, depth, width at the outer radius) about the construction Z axis at (x, y), from z to z + height. Without input: the knurled cylinder/ring itself as ONE notched profile extruded once (inner_radius makes a ring). With a target: all grooves cut in ONE multi-tool Boolean.",
    frame: LOCAL,
    anchor: "(x, y, z) is the axis point at the start height.",
    inputs: "none or 1 target solid",
    limits: "3..1000 grooves; depth < radius",
    example: add("kn", "knurl", { radius: q("10"), height: q("5"), count: q("40", "1"), depth: q("0.4"), inner_radius: q("4") }, { profile: "v" }),
  },
  cut_pattern: {
    summary: "count copies of the tool (input 2) on a linear (i * (dx, dy, dz)) or circular (about axis through origin, angle = total span, end excluded) pattern, removed from (operation difference, default) or added to (union) the target (input 1) in ONE multi-tool Boolean.",
    frame: LOCAL,
    inputs: "target solid, tool solid",
    example: add("cp", "cut_pattern", { count: q("8", "1"), angle: q("360", "deg") }, { kind: "circular", axis: P(0, 0, 1), origin: P(0, 0, 0) }, ["body", "tool"]),
  },
  transform: {
    summary: "Uniform scale about the origin, then rotation by angle about Z THROUGH THE ORIGIN, then translation (x, y, z).",
    frame: LOCAL,
    inputs: "1 shape",
    pitfalls: ["Prefer a placement/plane on the source feature to a transform chain."],
    example: add("tf", "transform", { x: q("5"), angle: q("90", "deg") }, {}, ["bx"]),
  },
  affine_transform: {
    summary: "p' = M p + t (rows of construction.matrix, translation). Rigid/uniform-scale matrices keep exact surfaces; non-uniform scale converts to B-splines.",
    frame: LOCAL,
    inputs: "1 shape",
    example: add("af", "affine_transform", {}, { matrix: [P(1, 0, 0), P(0, 0, -1), P(0, 1, 0)], translation: P(0, 5, 0) }, ["bx"]),
  },
  rotate: {
    summary: "Rotation by angle about construction.axis through construction.origin.",
    frame: LOCAL,
    sign: "Right-hand rule about the axis.",
    inputs: "1 shape",
    example: add("ro", "rotate", { angle: q("30", "deg") }, { axis: P(1, 0, 0), origin: P(0, 0, 0) }, ["bx"]),
  },
  instance: {
    summary: "Placed copy: rotation by angle about Z through the origin, then translation (x, y, z).",
    frame: LOCAL,
    inputs: "1 shape",
    example: add("in", "instance", { x: q("40") }, {}, ["bx"]),
  },
  mirror: {
    summary: "Mirror image across construction.plane {normal, point} (default: the YZ plane through the origin, x -> -x).",
    frame: LOCAL,
    inputs: "1 shape",
    example: add("mi", "mirror", {}, { plane: { normal: P(0, 1, 0), point: P(0, 0, 0) } }, ["bx"]),
  },
  pattern: {
    summary: "count copies, copy i translated by i * (dx, dy, dz); compound result, i = 0 is the original.",
    frame: LOCAL,
    inputs: "source (+ variant sources of overrides)",
    example: add("pa", "pattern", { count: q("4", "1"), dx: q("5") }, {}, ["cy"]),
  },
  circular_pattern: {
    summary: "count copies rotated about axis through origin; angle is the TOTAL span, samples include 0 and exclude the end.",
    frame: LOCAL,
    inputs: "source (+ variant sources of overrides)",
    example: add("cpa", "circular_pattern", { count: q("6", "1"), angle: q("360", "deg") }, { axis: P(0, 0, 1), origin: P(0, 0, 0) }, ["cy"]),
  },
  assembly: {
    summary: "Compound of 1..128 inputs without a Boolean (groups separate solids).",
    frame: LOCAL,
    inputs: "1..128 shapes",
    example: add("as", "assembly", {}, {}, ["bx", "cy"]),
  },
  field: {
    summary: "Implicit field (SDF expression tree) over a domain box with a cell size; implicit representation, not a precision_cad B-Rep output.",
    frame: "Local frame of the feature (structure frames only; no placement).",
    inputs: NONE,
    example: add("fd", "field", {}, { expression: { op: "sphere", center: P(0, 0, 0), radius: "5" }, domain: { min: P(-6, -6, -6), max: P(6, 6, 6) }, cell_size: q("0.5") }, [], { authoritative_representation: "implicit", owner_part: "part-field" }),
  },
  imported: {
    summary: "Geometry from an uploaded STEP/STL/VDB artifact (created by cad_import).",
    frame: LOCAL,
    inputs: NONE,
    example: add("im", "imported", {}, { artifact_id: "art-0123456789abcdef", format: "step", source_unit: "mm" }),
  },
  mesh_repair: {
    summary: "Mesh representation: weld, orient, fill small holes of a mesh input.",
    frame: "Frame of its mesh input.",
    inputs: "1 mesh",
    example: add("mr", "mesh_repair", {}, { orient: true }, ["msh"], { authoritative_representation: "mesh", owner_part: "part-mesh" }),
  },
  remesh_region: {
    summary: "Mesh representation: local remeshing inside a sphere at (x, y, z).",
    frame: "Frame of its mesh input.",
    inputs: "1 mesh",
    example: add("rm", "remesh_region", { radius: q("5"), target_edge: q("0.5") }, {}, ["msh"], { authoritative_representation: "mesh", owner_part: "part-mesh" }),
  },
  local_mesh_deform: {
    summary: "Mesh representation: handle-based local deformation inside a sphere.",
    frame: "Frame of its mesh input.",
    inputs: "1 mesh",
    example: add("md", "local_mesh_deform", { radius: q("5") }, { handles: [{ point: P(0, 0, 0), displacement: P(0, 0, 1) }] }, ["msh"], { authoritative_representation: "mesh", owner_part: "part-mesh" }),
  },
  tessellate: {
    summary: "B-Rep -> mesh representation at a chordal deflection.",
    frame: "Frame of its input.",
    inputs: "1 B-Rep solid",
    example: add("ts", "tessellate", { deflection: q("0.05") }, {}, ["bx"], { authoritative_representation: "mesh", owner_part: "part-mesh" }),
  },
  extract_isosurface: {
    summary: "Implicit field -> mesh representation.",
    frame: "Frame of its field input.",
    inputs: "1 field",
    example: add("xi", "extract_isosurface", {}, {}, ["fd"], { authoritative_representation: "mesh", owner_part: "part-mesh" }),
  },
};

/** Registry-aligned export: parameters and input counts from OPERATORS, semantics from here. */
export function operatorDocs() {
  const missing = Object.keys(OPERATORS).filter((name) => !OPERATOR_SEMANTICS[name]);
  if (missing.length) throw new Error("operator semantics missing for: " + missing.join(", "));
  return {
    conventions: COMMON_CONVENTIONS,
    /** Compile-time macro templates (construction.operator "macro"). */
    macros: macroDocs(),
    operators: Object.fromEntries(
      Object.entries(OPERATORS).map(([name, contract]) => [
        name,
        {
          ...OPERATOR_SEMANTICS[name],
          parameters: contract.params,
          refs: contract.refs,
          output: contract.output,
          input_representation: contract.inputs,
        },
      ]),
    ),
  };
}
