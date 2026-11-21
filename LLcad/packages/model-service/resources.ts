/**
 * Static MCP resources (resources/list, resources/read): the workflow knowledge an agent
 * needs before its first patch, served by the server itself so every MCP host gets it.
 * - llcad://skill/modeling   workflow: MVP first, turn economy, frames, look, gates, stop
 * - llcad://operators        every operator from the registry: frame, anchor, sign, inputs,
 *                            parameters with limits, pitfalls and ONE valid example
 * - llcad://pitfalls         error codes -> cause -> fix, and the live limits
 * - llcad://run-contract     scope, budget and acceptance of one run (.llcad/run.json)
 * Generated from the running build (registry, LIMITS, error guide), cached per process.
 */
import { operatorDocs } from "../compiler/operator-docs.js";
import { LIMITS, REGISTRY_HASH } from "../compiler/index.js";
import { ERROR_GUIDE } from "./next-actions.js";

export const RESOURCES = [
  {
    uri: "llcad://skill/modeling",
    name: "LLcad modelling workflow",
    description:
      "Read first: MVP-first reconstruction, one-call transactions (patch_ref, wait_ms, validate/commit), frames instead of transform chains, the mandatory multi-view look, gates as a floor, stopping and reporting.",
    mimeType: "text/markdown",
  },
  {
    uri: "llcad://operators",
    name: "LLcad operators",
    description:
      "Every operator of the running registry: frame, anchor, sign, inputs, parameters with limits, pitfalls and one valid add_feature example; plus macros.",
    mimeType: "text/markdown",
  },
  {
    uri: "llcad://pitfalls",
    name: "LLcad error codes and pitfalls",
    description:
      "Error code -> cause -> fix, the live limits (request/answer bytes, operations per patch, features) and the behaviour that cost past runs the most time.",
    mimeType: "text/markdown",
  },
  {
    uri: "llcad://run-contract",
    name: "LLcad run contract",
    description:
      "Scope (in/out), time budget and acceptance of one modelling run, as .llcad/run.json in the agent's working directory.",
    mimeType: "text/markdown",
  },
];

const SKILL = `# LLcad modelling workflow

LLcad is a mathematics-first CAD service: a model is a versioned construction (IR) of
features; every change is a candidate that is evaluated by the OpenCascade kernel,
validated against the model's profile and committed as a new immutable revision.
Wall time in agent runs is model output, not kernel time: few large patches, no polling,
compute in code.

## 1 Order of work
1. **Scope** (llcad://run-contract): what is in (default: visible exterior + main
   internals) and out (variants, states, accessories unless asked; then as IR
   configurations, never as displaced copies). Name the model after what it contains.
2. **Measured references**: datasheet dimensions, a scale from the silhouette, colours
   measured on product images, logos from the original vectors. Write assumptions down.
3. **MVP first**: frames, envelope, every part slot with its colour, silhouette and
   proportions in 1-3 patches, committed. Details come after the MVP looks right.
4. **Details by priority**: controls, windows, optics, markings (text / svg_emblem relief
   >= 0.05 mm), blends. Group corrections; do not rebuild the product.
5. **Look after every milestone commit** (below), fix the largest residual, stop.

## 2 Turn economy
- Compute coordinates, sections, poles and glyph layouts in code, never in thought.
- Write the patch to a file and submit it by reference, in ONE call:
  \`cad_apply_patch {model_id, base_revision: <head>, idempotency_key, patch_ref: {path: "/abs/p1.json"},
  validate: true, commit: "if_valid", wait_ms: 100000}\` -> apply, evaluate, validate, commit.
  The answer carries status (committed | validation_failed | running | failed), the new
  revision, the failed checks with measured/requested values, and warnings. "running"
  means: call again with identical arguments; it resumes. Up to 64 operations per patch.
- \`dry_run: true\` compiles only (dependencies, estimate) - like cad_plan_edit.
- Asynchronous tools (render, export, measure, validate, rebuild, import, revert,
  solve_constraints) take \`wait_ms\` (<= 110000) and answer with the finished job.
  cad_job_get takes \`wait_ms\` too; never poll in a loop.
- Reads: \`detail: "brief"\` on cad_get_model / cad_structure / cad_inspect, cad_job_get is
  brief by default. For a local edit, cad_context {feature_ids | region_mm} returns in one
  table the parameters (expected values), construction/feature/context hashes, owner parts
  with their output hashes, dependencies and dependents.
- Contacts: cad_measure {all_pairs: {}} or {pairs: [[a, b], ...]} is ONE job for the whole
  contact graph (distances, floating parts, coincident faces between parts).
- Plain JSON numbers are accepted wherever a decimal string is expected.
- A new idempotency_key for every changed request; the same key with identical arguments
  replays or resumes.

## 3 Construction conventions (details: llcad://operators)
- A structure frame per tilted/offset sub-assembly; features name it as local_frame and
  are built in it (no transform chains). A feature may carry its own \`placement\`
  {origin, x_axis, z_axis} or \`plane\` preset (XY, XZ=front, YZ=side, back).
- box = min corner; cylinder/cone = base centre along +Z; hole/pocket/groove cut DOWN
  from z; profiles are closed, planar, counter-clockwise (normalised with a warning).
- Booleans take at most 16 inputs; blend single bodies before booleans; keep cuts
  >= 0.5 mm off tangency or exactly on it.
- Every model output is in exactly one part's outputs and that feature's owner_part is the
  part (add_part / set_part_outputs deltas instead of whole set_structure edits).
- Parts carry their colour in the name: \`Material #RRGGBB · Part\`.
- Photo reconstructions may opt into the explicit preset
  set_model_settings {profile_preset: visual_reconstruction} (0.01 mm); it is recorded.
- Model-level: set_variable + {var: name} expressions, set_configurations (variants on
  request), set_decals (visual-only prints; geometry prints are text/svg_emblem relief),
  macros (ring, knurl_ring, lens, indicator_row, control_key, usb_c_receptacle),
  instantiate_fragment (copy from another model), cad_model_admin (rename, fork).

## 4 The mandatory look
After every milestone commit look at the WHOLE model from several views (front, side,
back, iso; plus a detail view of what changed): a viewer capture
(cad_reference_check phase inspect), cad_view when available, or a render. Check: nothing
outside the device, parts where they belong (not side by side), controls and windows
visible, logo position/orientation/colour, no coplanar layers (z-fighting), the model name
matches the content. Feature counts never prove resemblance.

## 5 Gates are a floor, not a finish
Validation and deterministic gate packs (cad_gates when available: envelope, contacts,
floating parts, coincident faces, visibility, image residuals) say what is certainly
wrong. Passing them is the floor. Then fix the largest remaining residual or state why it
is source-limited. Stop when the floor passed and the look shows no remaining visible
difference in scope, when the residual no longer improves, or when the time budget of the
run contract is spent. Never report "done"/"identical" from a gate alone.

## 6 Report
Model id and head revision; what matches (with numbers); remaining residuals;
assumptions (estimates, source limits); out-of-scope items; exports (made once, from the
final revision). Short, no self-praise.
`;

const RUN_CONTRACT = `# LLcad run contract

One modelling run is bound by a small contract file, \`.llcad/run.json\` in the agent's
working directory. Hosts with hooks (Claude Code skill llcad-modelling) enforce it with a
stop gate and time signals; without hooks the agent follows it itself.

\`\`\`json
{
  "model_id": "model-...",
  "product_name": "Example handheld torch T1",
  "started_at": "2026-09-23T10:00:00Z",
  "budget_min": 60,
  "scope": {
    "in": ["visible exterior", "main internals"],
    "out": ["variant", "accessory", "state"]
  },
  "acceptance": { "gates": "floor", "ledger": "advisory", "assumptions": "required" },
  "assumptions_file": ".llcad/assumptions.md",
  "max_stop_blocks": 6
}
\`\`\`

- **scope.in / scope.out**: the one-shot default is the visible exterior plus the main
  internals. Variants, states and accessories are out unless the user asked; when asked
  they become IR configurations (set_configurations), never displaced copies.
- **budget_min**: time box. At 50 %: MVP committed and looked at. At 75 %: details frozen.
  At 90 %: report.
- **acceptance.gates = "floor"**: deterministic gates must pass on the head; they are a floor,
  the look and the largest residuals decide the rest (llcad://skill/modeling section 5).
- **assumptions_file**: every estimate ([C] class) and source limit, written before the end.
- **max_stop_blocks**: how often a stop gate may send the agent back before it gives up and
  writes a stop report.

A run ends with: no open candidate (commit or cad_discard), the gate floor passed on the
head, the model name matching its content, nothing out of scope in the model, the
assumptions list written, and a short report.
`;

function operatorsMarkdown() {
  const docs: any = operatorDocs();
  const entries = Object.entries<any>(docs.operators);
  // The construction-frame sentence is the same for most operators: state it once.
  const frames = new Map<string, number>();
  for (const [, o] of entries) frames.set(o.frame, (frames.get(o.frame) ?? 0) + 1);
  const common = [...frames.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
  const range = (p: any) =>
    [
      p.dimension,
      p.min !== undefined || p.max !== undefined
        ? `${p.min ?? "-inf"}..${p.max ?? "inf"}`
        : null,
      p.integer ? "integer" : null,
      p.optional ? "optional" : null,
      p.default !== undefined ? `default ${p.default}` : null,
    ]
      .filter(Boolean)
      .join(", ");
  const lines = [
    "# LLcad operators",
    "",
    `Registry ${REGISTRY_HASH.slice(0, 16)}. Every feature: {id, semantic_name, parameters, construction: {operator, ...}, depends_on, owner_part?, local_frame?, placement? | plane?}. Compact add_feature: kind defaults to the operator, parameters may be plain numbers (mm / deg / 1 by dimension).`,
    "",
    "## Conventions",
    ...docs.conventions.map((c: string) => "- " + c),
    ...(common ? ["- Default frame (where an operator says 'construction frame'): " + common] : []),
    "",
  ];
  for (const [name, o] of entries) {
    lines.push(`## ${name}`, "", o.summary, "");
    lines.push(
      `- output ${o.output}; inputs (depends_on) ${o.refs?.[0]}..${o.refs?.[1]}: ${o.inputs}`,
    );
    if (o.frame && o.frame !== common) lines.push("- frame: " + o.frame);
    else lines.push("- frame: construction frame");
    if (o.anchor) lines.push("- anchor: " + o.anchor);
    if (o.sign) lines.push("- sign: " + o.sign);
    const params = Object.entries<any>(o.parameters ?? {});
    if (params.length)
      lines.push(
        "- parameters: " + params.map(([k, p]) => `${k} (${range(p)})`).join("; "),
      );
    if (o.limits) lines.push("- limits: " + o.limits);
    for (const pitfall of o.pitfalls ?? []) lines.push("- pitfall: " + pitfall);
    lines.push("- example: `" + JSON.stringify(o.example) + "`", "");
  }
  const macros = Object.entries<any>(docs.macros ?? {});
  if (macros.length) {
    lines.push(
      "# Macros (construction.operator \"macro\", expanded by the compiler into ordinary features)",
      "",
    );
    for (const [name, m] of macros) {
      lines.push(`## macro ${name} (version ${m.version})`, "", m.summary ?? "", "");
      if (m.frame) lines.push("- frame: " + m.frame);
      if (m.inputs) lines.push("- inputs: " + m.inputs);
      const params = Object.entries<any>(m.parameters ?? {});
      if (params.length)
        lines.push(
          "- parameters: " +
            params
              .map(([k, p]) => `${k} (${range(p)}${p.doc ? "; " + p.doc : ""})`)
              .join("; "),
        );
      if (m.options) lines.push("- options: " + JSON.stringify(m.options));
      if (m.example) lines.push("- example: `" + JSON.stringify(m.example) + "`");
      lines.push("");
    }
  }
  return lines.join("\n");
}

function pitfallsMarkdown() {
  const lines = [
    "# LLcad error codes and pitfalls",
    "",
    "Every failed call answers status failed with errors[{code, message, details}] and",
    "recommended_next_actions. INVALID_SCHEMA lists up to 32 issues with path, expected and",
    "received type, allowed_keys, valid options and limits: fix them all in one retry.",
    "",
    "| code | cause | fix |",
    "|---|---|---|",
    ...Object.entries(ERROR_GUIDE).map(
      ([code, g]) => `| ${code} | ${g.cause} | ${g.fix} |`,
    ),
    "",
    "## Limits of this build",
    "",
    `- request: ${LIMITS.request_bytes} bytes; answer: ${LIMITS.response_bytes} bytes (page with limit/offset, detail brief)`,
    "- operations per patch: 64 (patch_ref documents too); a model: " +
      `${(LIMITS as any).features ?? 4096} features, 512 outputs; booleans: 16 inputs`,
    `- open jobs per user: ${LIMITS.max_queued_per_user}; wait_ms: at most 110000 per call`,
    "",
    "## Frequent structural errors",
    "",
    "- 'Model outputs must be registered as part outputs' (model outputs must be part outputs): details.unregistered_outputs; add them with set_part_outputs {part_id, expected_hash, outputs} (the hash and current outputs come from cad_context parts[] or cad_structure detail brief) or add_part.",
    "- 'Part outputs must be unique own features': a part lists a feature whose owner_part is another part.",
    "- 'A feature needs its own part ...': owner_part is not a declared part of the same representation.",
    "- Structure ids (parts, frames, assemblies) must differ from feature ids; 'world' is reserved.",
    "- STALE_REVISION on set_parameter/set_construction/remove_feature: expected value or hash is not current: cad_context {feature_ids}.",
    "",
    "## Behaviour that cost past runs the most time",
    "",
    "- Polling job status in a loop: use wait_ms (one call) instead.",
    "- Re-typing generated patch JSON as a tool argument: write it to a file, pass patch_ref.",
    "- Many tiny patches with validate + commit each: one patch per phase, validate/commit in the same call.",
    "- Arithmetic in thought: compute coordinates in code.",
    "- Reading server source to learn semantics: llcad://operators answers it.",
    "- 0.01 mm coplanar print layers (z-fighting): relief >= 0.05 mm or decals.",
    "- Variants as displaced copies next to the product: configurations, on request only.",
    "- A model named 'complete' that is not: name = content; look at several views.",
    "- Exporting before the last change: export once, at the end.",
  ];
  return lines.join("\n");
}

const cache = new Map<string, string>();
/** Text of a static resource, or null for any other URI. */
export function staticResource(uri: string) {
  const entry = RESOURCES.find((r) => r.uri === uri);
  if (!entry) return null;
  let text = cache.get(uri);
  if (text === undefined) {
    text =
      uri === "llcad://skill/modeling"
        ? SKILL
        : uri === "llcad://operators"
          ? operatorsMarkdown()
          : uri === "llcad://pitfalls"
            ? pitfallsMarkdown()
            : RUN_CONTRACT;
    cache.set(uri, text);
  }
  return { uri, mimeType: entry.mimeType, text };
}
