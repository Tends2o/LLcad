import {
  ToolSchemas,
  READ_TOOLS,
  ToolName,
  inputJSONSchema,
} from "../semantic-ir/tool-registry.js";
import { outputJSONSchema } from "../semantic-ir/results.js";
import { RESOURCES } from "../model-service/resources.js";
import type { ModelService } from "../model-service/index.js";
import type { Principal } from "../policy/index.js";
export const SERVER_INFO = { name: "mathforge-3d", version: "0.1.0" };
export const INSTRUCTIONS = [
  "LLcad: mathematical CAD over MCP (versioned construction -> candidate -> validation -> commit). Before modelling read llcad://skill/modeling; llcad://operators has every operator with one valid example, llcad://pitfalls the error codes with fixes and limits, llcad://run-contract scope, budget and acceptance of a run.",
  "MVP first: silhouette, proportions, all major parts and dominant colours from measured references (datasheet dimensions, silhouette scale, colours measured on the images, logos from the original vectors); details only after the MVP is committed and looked at. Scope per run contract: visible exterior plus main internals; variants, states and accessories only on request, as configurations.",
  "Turn economy: compute geometry in code and submit a whole phase as one patch file: cad_apply_patch {patch_ref: {path}, validate: true, commit: \"if_valid\", wait_ms: 100000} applies, evaluates, validates and commits in one call (status running -> call again with identical arguments). Asynchronous tools and cad_job_get take wait_ms; never poll in loops. Read with detail brief, cad_context for local edits, cad_measure all_pairs for contacts in one job.",
  "Look after every milestone commit: the whole model from several views (front, side, back, iso) plus the changed detail (cad_view returns the images directly). Validation and gates are a floor, not a finish: then fix the largest visible residual. Feature counts never prove resemblance; never claim identity.",
  "Photos become measurements only when registered: cad_reference_register with >= 4 spread landmarks (incl. asymmetric ones such as button side or lens facing) fits an absolute camera; cad_gates then checks silhouette, landmarks, markings, colours and handedness in mm next to the native checks (contacts, covered controls, coincident faces). floor_passed is a floor, never done.",
  "References: cad_reference_setup binds the original images and known dimensions; cad_reference_check phase inspect gives free local viewer captures; phase compare is the final complete review, not a step after every small change. Reference-bound final exports need a passing current review unless a draft is requested explicitly.",
  "Optional after the MVP: cad_feature_batch runs feature workers on one frozen revision (default claude-opus-5-5 for Claude, gpt-6-astra for GPT/Codex, agy:gemini-4-neon for Antigravity/Gemini orchestrators; 2 at a time, max_parallel up to 4); each builds and validates its own candidate (never commits) and returns a patch plus before/after crops. Apply the reported waves by reference: cad_apply_patch {patch_ref: {artifact_id}, validate: true, commit: \"if_valid\"}, one transaction per wave. Parallel build lanes (lanes: true) only when the user asked for them.",
].join("\n");
export const descriptions: Record<ToolName, string> = {
  cad_feature_batch:
    "After a committed, inspected MVP: 1-8 independent feature tasks on one frozen revision, max_parallel workers at a time (default 2, 1-4). Worker: worker.model (default claude-opus-5-5 for Claude, gpt-6-astra for GPT/Codex, agy:gemini-4-neon for Antigravity orchestrators; any Claude/Codex model, e.g. claude-fable-5-1, or agy:<model> run through the agy CLI, is kept exactly; an agy family takes the task effort, clamped to the efforts it offers); effort per task: task.effort > worker.effort > static table by kind (medium geometry/edge/marking, high optics/curvature, low colour; Jev may pick a neighbour, a Jev failure falls back to the table). validation worker_candidate (default when the service knows its MCP URL): each worker gets a designer grant (edit, never commit) on this model, builds and validates its own candidate and returns a validated patch (<= 64 ops, own namespace task_id-, owned features only) plus before/after crops. lanes: true (opt-in, only when asked): tasks may add parts/frames pt-<id>-/fr-<id>- and change outputs of owned_part_ids. timeout_seconds default 600 (30-1800). Poll cad_feature_batch_report (wait_ms). Model calls cost money.",
  cad_feature_batch_report:
    "Progress, actual model/effort (effort_source), validation status and crops per task, and waves: patch documents of <= 64 operations (one transaction each) to apply in order by reference with cad_apply_patch {patch_ref: {artifact_id: patch_artifact_id}, validate: true, commit: \"if_valid\"}; applicable says whether the current head still leaves the wave's tasks untouched (the server re-checks on apply). wait_ms waits until the batch ends. applied is always false; a stale batch is never ready to apply.",
  cad_reference_setup:
    "Bind uploaded reference image artifacts, visible requirements, known dimensions and mating/contact pairs to a model. Read the head first; expected_contract_hash is null for the first setup, else the hash from cad_reference_report. Updating the contract invalidates earlier reviews. Keep source provenance; separate measured dimensions from photo estimates. New contracts use acceptance gates_judge: the cad_gates floor on the reviewed revision decides, plus one advisory judge (judge {provider claude_cli|openrouter, model default claude-opus-5-5, effort, max_parallel, pairwise, max_cost_usd}) whose critical findings block only when confirmed deterministically or while not listed in accepted_assumptions [{requirement_id, note}]; acceptance two_vlm keeps the two-OpenRouter-model rule. references[].camera may be absolute {mode:'absolute', projection, position_mm, target_mm, up, fov_deg | mm_per_px, width_px, height_px, principal_px?} (never rescaled); requirements may add feature_ids, a source region and up to 5 binary judge questions.",
  cad_reference_check:
    "phase inspect: free local viewer screenshots of 1-3 chosen views (default front) for the look after milestone commits; open the images. inventory: paid extraction of ambiguous source details. compare: full review of every frozen requirement, final review only. Never edits geometry or certifies identity. compare under gates_judge: native checks, cad_gates and the judge on registered crop triples (source | render | residual), unconfirmed findings listed as assumptions; unchanged captures and judge answers are reused.",
  cad_reference_report:
    "Review status, findings, image artifacts, measured checks and uncertainties of a revision; run_id polls a run, offset/limit pages. Changes make earlier proof stale. Not an accuracy certificate.",
  cad_reference_register:
    "Fit an absolute camera {mode: absolute, position_mm, target_mm, up, fov_deg | mm_per_px, width_px, height_px} to one contract reference image (CPU, 30-90 s): landmark pose (>= 4 spread landmarks px <-> feature_id/point_mm, incl. asymmetric ones; role check = reported, not fitted), then silhouette + dark-part refinement with mirrored starts, and a handedness test against the model's mirror image. registered: true only if landmark RMS <= 1% of height, IoU >= the source-type threshold (orthographic 0.97, perspective photo 0.92), triple orientation and mirror test hold; else reasons. Stores camera, landmarks, exclusions and residuals in the contract (new contract_hash) and an overlay artifact.",
  cad_view:
    "Look at a revision without a contract: renders (VTK offscreen, about 0.1-1 s per view) preset views (front +Y, left +X, back, right, top, bottom, iso, iso_back), absolute cameras or a reference's camera in channels lit / albedo (flat declared colours) / ids (one colour per part), <= 2576 px. Answers the images as MCP image content and artifacts plus per view and part {visible_px, bbox_px, centroid_px, mean_rgb, delta_e00 lit vs declared; photo_rgb/photo_delta_e00 for registered references}. Default: front, left, back, iso at 1024 px.",
  cad_gates:
    "Deterministic gate pack of a revision (the floor, never a finish): declared dimensions, contact graph (floating parts, coincident faces between differently coloured parts), control visibility (ray samples), sightline, stations, handedness sides, counts, parts outside the envelope, name vs content, open candidates; per reference camera silhouette IoU/chamfer/p95 mm, landmark residuals mm and triple orientation, marking centroid mm/size ratio/visible fraction, part colours (dE00 or hue family), visible counts. Registered references are hard gates, others advisory. Declarations come from the contract's gates block. Answers {floor_passed, summary, residuals ranked (~2 KB brief), coverage}; status running -> call again.",
  cad_drawing_analyze:
    "Start of a technical drawing (DIN/ISO) of a committed revision: per part bbox, volume, holes/shafts {diameter, depth, axis, start, end, open_start/open_end}, plane gaps {thickness|gap, distance, p1, p2}, bend tables of wire/tube sweeps {wire_diameter, points P0..Pn = start, virtual bend points (tangent intersections) with radius, end}; view extents per preset; suggestion {format, orientation, scale, parts_list}; title_defaults and company lines of the service; conventions (presets, projection method 1, sheet coordinates). Uses the revision's own B-rep export (BUILD_MISMATCH -> cad_rebuild first; no native B-rep -> OUT_OF_SCOPE).",
  cad_drawing_plan:
    "Deterministic drawing planner: 1-4 complete candidate specs (views, sections, details, ISO 129 dimensions with feature tags, parts list, title block) from the revision's geometry and optional facts (materials, names, roles, tolerances quoted from the model texts). Render a candidate with cad_drawing_render {spec_ref: {artifact_id}}. Answers {candidates[{candidate_id, spec_artifact_id, metrics}], parts, notes, open (what is missing: fill it in or leave it empty)}.",
  cad_drawing_render:
    "Render a drawing set (spec or spec_ref {path} | {artifact_id}) of a revision to PDF + one PNG per sheet (returned as images) + findings. spec {title{title, supplementary_title, drawing_number, revision, material, surface, document_type, general_tolerance, tolerancing, created_by, created_date, ...}, company?, parts?[{id,name,role? manufactured|standard|purchased|reference|duplicate,quantity?}], strict? (default false: new findings block instead of warn), autofix? (default false: the kernel repairs layout findings itself), changes?[{index,date,name,text}], sheets[{format A4|A3|A2|A1|A0, orientation landscape|portrait, scale 'a:b', " +
    "views[{id, preset|direction(+up), at [x,y] sheet mm of the view centre | place {relative_to, side below|above|left|right, gap mm (default 25)} (aligned with the reference by projection), parts?[ids], scale?, hidden? (dashed hidden edges, default false), centerlines? (default true for axis views), section? {point [x,y,z]} (cuts away the camera side of the plane through point, normal = view direction, hatched), detail? {center, radius} (clipped circle, use scale e.g. '5:1'), label?, thin_parts? [part ids drawn as thin sections]}], " +
    "annotations[{type linear (view, p1, p2 3D model points, axis h|v|aligned, side above|below|left|right or offset mm, prefix e.g. '□', suffix, tolerance, decimals) | diameter (view, center on the axis, angle?, offset?, leader?) | radius (view, center, which center|inner|outer for bends, angle) | angle (view, vertex, p1, p2) | balloon (view, p on the part, part, to [dx,dy]) | leader (view+p or at_sheet, text, to, end arrow|dot) | point_label | polyline (points, style center|thin|hidden|thick) | centermark | text (at_sheet [x,y] or view+at_view, text, h) | thread (view, feature?, start, end, major_d, minor_d, internal, designation, angle?) | section_line (view, p1, p2, label, look [x,y,z]) | detail_circle (view, center, radius, label); radius/leader/text may carry features? [ids] = counted collective '4× R2' / '2× 0,5×45°' (equal fillets, corner radii or chamfers only) or model_ref? [ids] = reference to the 3D data set (freeform only)}], " +
    "tables[{at [x,y] top left, title, columns[{title,width}], rows} | {bend_table: part_id, at, origin?, labels_in?: view, label_to?: {P1:[dx,dy]}}], parts_list?[{pos, qty, name, part, norm, material}] (above the title block, required for >= 2 parts, every listed part needs a balloon), notes?[lines | {text, general {kind radius|chamfer, size R|[width, angle], part?}} (general note for all otherwise uncovered elements of that size)]}], sheets_reason? (only if the person asked for separate sheets)}. Small parts and assemblies go on ONE sheet (assembly, parts list and part details; details at their own scale); prefer a larger format over a second sheet. " +
    "Dimension values are computed from the 3D points. Findings (severity block|warn): out_of_area, overlaps_block, view_overlap, text_overlap, text_on_line, text_on_geometry, parts_list_missing, balloon_missing, part_not_shown, feature_not_dimensioned, feature_too_small, dimension_redundant, chain_closed, leader_crossing, text_too_small, section_inconsistent, view_without_information, coverage_error, collective_count_mismatch, collective_size_mismatch, collective_not_allowed, model_ref_not_allowed (new findings block only with strict, else warn); the result adds coverage {required, dimensioned, missing, modes {A, B, C}, parts}, title_field_empty, annotation_error, view_error, part_without_dimension, projection_arrangement, sheets_mergeable (the content of all sheets fits on one sheet: merge). crops [{sheet, box_mm}] return zoomed images. view_frames give origin/scale/axes of every view. Look at every sheet, fix and render again until blocking is 0 and the drawing is complete, then cad_drawing_publish.",
  cad_drawing_publish:
    "Attach a checked render to its revision so the viewer offers the PDF: requires a render of exactly this revision with blocking 0 and review {sheets_viewed >= sheets, statement of what was checked}. A later model change makes the drawing stale (it stays bound to its revision).",
  cad_access:
    "Your role, feature scope and job budget on a model. Owners inspect grants or propose a grant/revocation (confirmed separately, never by this tool); mode request reads a proposal. Permission changes invalidate pending work.",
  cad_capabilities:
    "Operators, formats, precision profiles, limits and build hashes (target_registry_hash for cad_rebuild). Operator semantics and examples: resource llcad://operators.",
  cad_list_models:
    "Find your own or shared models by name or purpose (paged). Use before asking the user for a model id.",
  cad_create_model:
    "Create an empty private model; add geometry with cad_apply_patch. settings {profile_preset: visual_reconstruction} opts into 0.01 mm tolerance for photo reconstructions (default precision_cad 0.001 mm); recorded in the IR and every export manifest. Later changes: patch op set_model_settings.",
  cad_get_model:
    "Revision summary, feature page, quality, build compatibility, measurements and model_level (settings, variables, configurations, decals and the hashes their patch ops expect). detail brief: one compact row per feature {id, name, op, part (omitted = part-main), frame (omitted = world), deps, params as 'value unit'} and a measurement summary.",
  cad_structure:
    "Project, assembly, part and frame summaries with structure_hash, world bounds and placements, paged by kind (filter entity_id/query). detail brief: parts with outputs and outputs_hash (expected_hash of set_part_outputs), frames with translation/axis/angle, identity placements omitted. Prefer add_part/add_frame/set_part_outputs deltas over set_structure.",
  cad_find:
    "Find features in a revision by tokens, kind, owner_part or a world point/box (BVH of evaluated bounds). Several results need an explicit choice.",
  cad_inspect:
    "One feature: parameters, construction, facts, hashes, dependents and paged native faces (face_limit 1..16, face_offset). face_id plus an explicit revision selects a face's source feature; selection_handle + rebind:true follows it through unique successors. detail brief: parameters, hashes, facts summary, dependents, edit ops; faces only with sections [faces].",
  cad_context:
    "One table for a local edit: features by feature_ids or world region_mm (plus neighbours up to depth; neither = model outputs) with parameters (expected values of set_parameter), construction_hash/feature_hash/context_hash (expected_hash of set_construction/remove_feature/set_feature_context), dependencies, dependents, bounds, and owner parts with outputs_hash (set_part_outputs). Replaces series of find/inspect/structure reads.",
  cad_measure:
    "Read-only measurements of an exact revision (never changes geometry). volume/area/bounds/... answer at once; distance, clearance, angle, curvature, surface_distance, wall_thickness are jobs (idempotency_key, wait_ms). Contact graph of many pairs in ONE job: pairs or all_pairs (see their fields). Multi-face features need face_id (cad_inspect) and an explicit revision; clearance certifies no motion or global wall.",
  cad_plan_edit:
    "Compile a patch without building it: changed/dependent features, estimate, resolved parameters, sensitivity. Same operations as cad_apply_patch (prefer cad_apply_patch {dry_run: true}). Pattern occurrences: set_pattern_occurrence with the construction_hash from cad_inspect/cad_context and the zero-based index; overrides translate in the pattern's local frame; null restores the shared occurrence.",
  cad_solve_constraints:
    "Solve up to 12 continuous parameters against normalized equations/inequalities (SciPy SLSQP). Job result: suggested patch operations plus persistent equation constraints; nothing changes until you apply, validate and commit them. Local convergence only, never a global optimum claim.",
  cad_apply_patch:
    "Build a candidate from registered operations (<= 64; add_feature in compact form, set_parameter, set_construction, remove_feature, set_outputs, add_part, set_part_outputs, set_variable, set_model_settings, set_configurations, set_decals, instantiate_fragment, ...; fields and examples: llcad://operators). Pass operations inline or patch_ref to a generated file (never re-type JSON). validate: true + commit: \"if_valid\" = apply, evaluate, validate, commit in ONE call -> {status committed|validation_failed|running|failed, revision, validation {digest, failed_checks with measured/requested}, warnings}; running -> repeat the identical call. gates: true adds the cad_gates floor + top residuals of the committed revision. wait_ms alone returns the evaluate job; dry_run: true plans only. Base = head.",
  cad_validate:
    "Run the profile checks on one candidate (job). With wait_ms it runs at once and answers the finished job: result.status (checks_passed_within_profile|failed), result.check_count, result.checks (failed only, measured/requested) and result.digest = validation_digest for cad_commit. cad_apply_patch {validate, commit} does this in the same call.",
  cad_compare:
    "Compare two exact revisions: changed/removed features, measured dimensions and volume delta.",
  cad_commit:
    "Atomically commit the validated candidate with its validation_digest (result.digest of the validate job) if its base is still the head. Answers committed with the new revision, or failed with errors (stale base, mismatched digest).",
  cad_discard:
    "Discard an uncommitted candidate and cancel its remaining jobs.",
  cad_revert:
    "Propose an earlier committed construction as a new candidate, keeping the current protected constraints (validate and commit normally; wait_ms returns the evaluate job).",
  cad_rebuild:
    "Rebuild the current construction for target_registry_hash from cad_capabilities after a build upgrade (cad_get_model build_status rebuild_required): mode plan first, then mode candidate with a new key; validate and commit normally. Keeps all constraints and the old revision.",
  cad_render:
    "Preview artifacts (mesh, orthographic/section views) of a revision, feature or configuration; cached previews answer at once, else a job (wait_ms). To look at the model prefer viewer captures (cad_reference_check phase inspect) or cad_view.",
  cad_import:
    "Decode an uploaded artifact (ir, step, stl, vdb; explicit units) into a new candidate. format ir with mode replace imports a generated IR into a NON-empty model as a diff candidate: unchanged features keep their cache, import_diff reports changes, current constraints are kept, protected features must stay untouched. wait_ms returns the job inline.",
  cad_export:
    "Export a committed revision (step, stl, glb, brep, vdb: job; ir: immediate) and re-read it with measured losses; configuration selects a variant's outputs. Reference-bound exports need a passing current review unless reference_policy draft. Export once, from the final revision. Download via GET /api/artifacts/{artifact_id}.",
  cad_job_get:
    "Status and result of a job; waits server-side (wait_ms <= 110000, default 10000). brief (default): {job_id, status, phase, elapsed_s, eta_s, result, error}; full: complete view. warnings: inputs the kernel normalised or elements it left out.",
  cad_job_cancel:
    "Cancel an unfinished job. Committed revisions stay immutable.",
  cad_viewer_open:
    "Open the optional browser viewer for the person you work with: a URL with a single-use login code (5 min), optionally preselecting model_id; launch_browser (default true) starts the local browser. Modelling never requires it.",
  cad_model_admin:
    "Model lifecycle. rename: new name, same id (owner only). fork: a new private model from a revision (default head), built as a candidate from the shared cache; validate and commit it on the new model (fork.model_id, fork.base_revision, fork.transaction_id). Forking a shared model needs a grant that may export.",
  cad_viewer_close:
    "Close the viewer started by cad_viewer_open (stdio: stops the on-demand viewer; HTTP: the service viewer stays and the result says so).",
  cad_fit_parameters:
    "Fit up to 12 model variables (set_variable) to registered reference views: silhouette IoU, edge chamfer and landmark residuals of stand-in renders, optimum confirmed on a real candidate. Returns a proposal patch (set_variable operations) with before/after metrics; nothing is committed. Waits up to wait_ms (default 100 s); poll a running fit with fit_id.",
};
export function toolDefinitions() {
  return Object.entries(ToolSchemas).map(([name, schema]) => ({
    name,
    title: name.replace("cad_", "").replaceAll("_", " "),
    description: descriptions[name as ToolName],
    inputSchema: inputJSONSchema(name as ToolName),
    outputSchema: outputJSONSchema(name as ToolName),
    annotations: {
      // cad_measure never changes geometry; some metrics create compute jobs, which
      // is not a mutation of the model (plan-mode agents may measure).
      readOnlyHint: READ_TOOLS.has(name as ToolName),
      destructiveHint: ["cad_commit", "cad_discard", "cad_job_cancel"].includes(
        name,
      ),
      idempotentHint: true,
      openWorldHint: name === "cad_reference_check" || name === "cad_feature_batch",
    },
  }));
}
export const resourceTemplates = [
  {
    uriTemplate: "cad://models/{model_id}/revisions/{revision}/summary",
    name: "Model revision summary",
    mimeType: "application/json",
  },
  {
    uriTemplate:
      "cad://models/{model_id}/revisions/{revision}/features/{feature_id}",
    name: "Feature detail",
    mimeType: "application/json",
  },
  {
    uriTemplate: "cad://transactions/{transaction_id}/validation",
    name: "Bound validation evidence",
    mimeType: "application/json",
  },
  {
    uriTemplate: "cad://artifacts/{artifact_id}/manifest",
    name: "Artifact manifest",
    mimeType: "application/json",
  },
];
/** Rendered images (cad_view) travel next to the JSON as MCP image content. */
const MCP_IMAGES = Symbol.for("llcad.mcp.images");
export function toolResult(result: any) {
  const images: any[] = (result && result[MCP_IMAGES]) || [];
  return {
    isError: result.status === "failed",
    structuredContent: result,
    content: [
      { type: "text" as const, text: JSON.stringify(result) },
      ...images.map((i) => ({ type: "image" as const, data: String(i.data), mimeType: String(i.mimeType) })),
    ],
  };
}
/** resources/list: the static guidance resources. */
export const resources = RESOURCES;
/** resources/read: static guidance as text, model resources as JSON. */
export function resourceContents(service: ModelService, p: Principal, uri: string) {
  const value: any = service.resource(p, uri);
  if (value && typeof value.text === "string" && typeof value.mimeType === "string" && value.uri === uri)
    return [{ uri, mimeType: value.mimeType, text: value.text }];
  return [{ uri, mimeType: "application/json", text: JSON.stringify(value) }];
}
