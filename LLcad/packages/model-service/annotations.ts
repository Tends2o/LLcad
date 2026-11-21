import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { Store } from "./store.js";
import { Principal, authorize } from "../policy/index.js";
import { id } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";
import { copyBuildPlan, readBuildPlan } from "./build-plan.js";
/** A mark drawn on the frozen view: what the person circled, what it touches in
 *  the model, and what they want changed there. The record is kept twice — as a
 *  row, so it can be listed and searched, and as a folder of plain files, so the
 *  agent that carries out the change can read it without an API. */
const MAX_PAYLOAD_BYTES = 512 * 1024;
/** Two pictures belong to a mark on the frozen view; every order may bring its
 *  own material: pictures (`image-n`) and files such as a mesh (`file-n`). */
const FILE_KIND = /^(region|view|(image|file)-([1-9]|10))$/;
const EXTENSIONS: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "model/stl": "stl",
  "application/sla": "stl",
  "application/octet-stream": "bin",
};
export const ATTACHMENT_TYPES = Object.keys(EXTENSIONS);
/** Pictures are kept as they arrive — the original, never re-encoded here. The
 *  viewer sends up to 2576 px on the long edge (what Opus 5.5 reads at full
 *  detail); larger originals are accepted too, and the prompts tell the agent
 *  to crop them. Only absurd sizes are refused: a decoder would choke on them. */
export const MODEL_IMAGE_EDGE_PX = 2576;
export const MAX_IMAGE_EDGE_PX = 16384;
/** Width and height from a PNG or JPEG header, without decoding the picture.
 *  Anything unreadable simply has no size; the bytes are kept regardless. */
export function imageSize(
  data: Buffer,
): { width: number; height: number } | null {
  if (
    data.length >= 24 &&
    data.readUInt32BE(0) === 0x89504e47 &&
    data.toString("ascii", 12, 16) === "IHDR"
  )
    return { width: data.readUInt32BE(16), height: data.readUInt32BE(20) };
  if (data.length < 4 || data[0] !== 0xff || data[1] !== 0xd8) return null;
  let i = 2;
  while (i + 9 < data.length) {
    if (data[i] !== 0xff) {
      i++;
      continue;
    }
    const marker = data[i + 1];
    if (
      marker === 0xd8 ||
      marker === 0x01 ||
      (marker >= 0xd0 && marker <= 0xd7)
    ) {
      i += 2;
      continue;
    }
    const length = data.readUInt16BE(i + 2);
    // Start-of-frame markers carry the size; DHT, JPG and DAC share the range.
    if (
      marker >= 0xc0 &&
      marker <= 0xcf &&
      marker !== 0xc4 &&
      marker !== 0xc8 &&
      marker !== 0xcc
    )
      return {
        height: data.readUInt16BE(i + 5),
        width: data.readUInt16BE(i + 7),
      };
    if (length < 2) return null;
    i += 2 + length;
  }
  return null;
}
/** What an annotation is about: a mark on the model, or the order that asked
 *  for a new model in the first place. */
export const ANNOTATION_KINDS = [
  "mark",
  "model_purpose",
  "model_build",
  // A technical drawing (DIN/ISO PDF) of one committed revision.
  "model_drawing",
] as const;
export const ANNOTATION_STATES = [
  "captured",
  "queued",
  "running",
  "proposed",
  "succeeded",
  // A writing run that ended without a committed result, or with a candidate
  // it never committed: not a failure of the tool, not a success either. The
  // reason is in run.incomplete_reason.
  "incomplete",
  "failed",
  "cancelled",
] as const;
export function annotationDirectory(store: Store, annotation: string) {
  return join(store.root, "annotations", annotation);
}
export function createAnnotation(store: Store, p: Principal, input: any) {
  authorize(p, "model:edit");
  const model = store.model(p, input.model_id),
    revision = store.revision(p, input.model_id, input.revision);
  const note = String(input.note ?? "").trim(),
    kind = ANNOTATION_KINDS.includes(input.kind) ? input.kind : "mark";
  requireThat(
    // A creation order may arrive as pictures alone; a mark needs words.
    (kind !== "mark" || note.length > 0) && note.length <= 4000,
    "INVALID_SCHEMA",
    "A change order needs a text (at most 4000 characters).",
  );
  const payload = {
    ...input,
    kind,
    images: [],
    note,
    model_id: model.id,
    model_name: model.name,
    revision: revision.id,
    captured_at: new Date().toISOString(),
    unit: "mm",
  };
  const serialised = JSON.stringify(payload);
  requireThat(
    Buffer.byteLength(serialised) <= MAX_PAYLOAD_BYTES,
    "BUDGET_EXCEEDED",
    "Mark data too large.",
  );
  const aid = id("mark");
  store.run(
    "INSERT INTO annotations(id,tenant,owner,model,revision,created,note,state,payload,region_artifact,view_artifact,run) VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
    aid,
    p.tenant,
    p.user,
    model.id,
    revision.id,
    new Date().toISOString(),
    note,
    "captured",
    serialised,
  );
  writeRecord(store, aid);
  return read(store, p, aid);
}
export function attachFile(
  store: Store,
  p: Principal,
  annotation: string,
  kind: string,
  data: Buffer,
  mime = "image/png",
) {
  requireThat(
    FILE_KIND.test(kind),
    "INVALID_SCHEMA",
    "Unknown attachment kind: region, view, image-1…10 or file-1…10.",
  );
  requireThat(
    EXTENSIONS[mime] !== undefined,
    "OUT_OF_SCOPE",
    "Only images (PNG, JPEG) and meshes (STL) belong to an order.",
  );
  const size = mime.startsWith("image/") ? imageSize(data) : null;
  requireThat(
    !size ||
      (size.width > 0 &&
        size.height > 0 &&
        Math.max(size.width, size.height) <= MAX_IMAGE_EDGE_PX),
    "BUDGET_EXCEEDED",
    `Image too large: at most ${MAX_IMAGE_EDGE_PX} px on the long edge.`,
  );
  const row = row_(store, p, annotation),
    extension = EXTENSIONS[mime],
    file = `${kind}.${extension}`;
  const artifact = store.artifact(p, data, mime, row.model, row.revision, {
    filename: file,
    annotation,
    source: "viewer_markup",
    quality: "preview_only",
  });
  if (kind === "region" || kind === "view")
    store.run(
      `UPDATE annotations SET ${kind}_artifact=? WHERE id=?`,
      artifact.artifact_id,
      annotation,
    );
  else {
    // What a person hands in is kept in the record itself: there can be
    // several, and they leave again once they have been read.
    const payload = JSON.parse(row.payload);
    payload.files = [
      ...(attached(payload) as any[]).filter((i) => i.kind !== kind),
      {
        kind,
        artifact_id: artifact.artifact_id,
        file,
        mime,
        bytes: data.length,
        ...(size ? { width: size.width, height: size.height } : {}),
      },
    ];
    delete payload.images;
    store.run(
      "UPDATE annotations SET payload=? WHERE id=?",
      JSON.stringify(payload),
      annotation,
    );
  }
  writeFileSync(join(annotationDirectory(store, annotation), file), data, {
    mode: 0o600,
  });
  writeRecord(store, annotation);
  return artifact;
}
/** Everything a person attached to an order, whatever it was called. */
export function attached(payload: any): any[] {
  return payload.files ?? payload.images ?? [];
}
/** One attachment in words: name, kind, pixel size, bytes. */
export function describeAttachment(f: any) {
  const image = !String(f.file).endsWith(".stl") && f.mime !== "model/stl";
  const size =
    image && f.width && f.height ? `, ${f.width}×${f.height} px` : "";
  const large =
    image && Math.max(f.width ?? 0, f.height ?? 0) > MODEL_IMAGE_EDGE_PX
      ? ", larger than 2576 px: Read downsizes it — crop details with python3/PIL"
      : "";
  return `${f.file} (${image ? "image" : "STL mesh"}${size}${f.bytes ? `, ${Math.round(f.bytes / 1024)} KiB` : ""}${large})`;
}
/** Working material, not a record: once an order has been read, its files go —
 *  the rows, the copies on disk and the bytes nothing else refers to. */
export function dropFiles(store: Store, annotation: string) {
  const row = store.get("SELECT * FROM annotations WHERE id=?", annotation);
  if (!row) return 0;
  const payload = JSON.parse(row.payload),
    images = attached(payload),
    directory = annotationDirectory(store, annotation);
  for (const image of images) {
    const artifact = store.get(
      "SELECT hash FROM artifacts WHERE id=?",
      image.artifact_id,
    );
    store.run(
      "DELETE FROM artifact_models WHERE artifact=?",
      image.artifact_id,
    );
    store.run("DELETE FROM artifacts WHERE id=?", image.artifact_id);
    if (
      artifact &&
      !store.get("SELECT 1 FROM artifacts WHERE hash=?", artifact.hash) &&
      !store.get("SELECT 1 FROM cache WHERE blob=?", artifact.hash)
    )
      rmSync(store.path(artifact.hash), { force: true });
    rmSync(join(directory, image.file), { force: true });
  }
  if (!images.length) return 0;
  payload.files = [];
  delete payload.images;
  payload.files_removed = (payload.files_removed ?? 0) + images.length;
  store.run(
    "UPDATE annotations SET payload=? WHERE id=?",
    JSON.stringify(payload),
    annotation,
  );
  writeRecord(store, annotation);
  return images.length;
}
/** Orders of one kind, several kinds (comma-separated) or all of them. Build
 *  orders carry their report (summary, assumptions, open points) in run. */
export function listAnnotations(
  store: Store,
  p: Principal,
  model: string | null,
  kind: string = "mark",
) {
  authorize(p, "model:read");
  const kinds =
    kind === "all"
      ? [...ANNOTATION_KINDS]
      : kind
          .split(",")
          .map((k) => k.trim())
          .filter((k) => (ANNOTATION_KINDS as readonly string[]).includes(k));
  const chosen = kinds.length ? kinds : ["mark"];
  const rows = store.all(
    `SELECT id,model,revision,created,note,state,region_artifact,view_artifact,run,
            COALESCE(json_extract(payload,'$.kind'),'mark') AS kind,
            COALESCE(json_array_length(payload,'$.files'),json_array_length(payload,'$.images'),0) AS images
       FROM annotations WHERE tenant=? AND COALESCE(json_extract(payload,'$.kind'),'mark') IN (${chosen.map(() => "?").join(",")})` +
      (model ? " AND model=?" : "") +
      " ORDER BY created DESC LIMIT 100",
    p.tenant,
    ...chosen,
    ...(model ? [model] : []),
  );
  return { annotations: rows.map(summarise) };
}
export function read(store: Store, p: Principal, annotation: string) {
  const row = row_(store, p, annotation);
  return { ...summarise(row), payload: JSON.parse(row.payload) };
}
export function remove(store: Store, p: Principal, annotation: string) {
  const row = row_(store, p, annotation);
  requireThat(
    row.state !== "running",
    "CONSTRAINT_CONFLICT",
    "The order is running right now; cancel it first.",
  );
  // An order that goes takes its working material with it.
  dropFiles(store, annotation);
  for (const kind of ["region", "view"] as const)
    if (row[`${kind}_artifact`])
      store.run("DELETE FROM artifacts WHERE id=?", row[`${kind}_artifact`]);
  rmSync(annotationDirectory(store, annotation), {
    recursive: true,
    force: true,
  });
  store.run("DELETE FROM annotations WHERE id=?", annotation);
  return { status: "deleted", annotation_id: annotation };
}
export function setState(
  store: Store,
  annotation: string,
  state: (typeof ANNOTATION_STATES)[number],
  run?: Record<string, unknown>,
) {
  store.run(
    "UPDATE annotations SET state=?,run=COALESCE(?,run) WHERE id=?",
    state,
    run ? JSON.stringify(run) : null,
    annotation,
  );
}
function summarise(row: any) {
  return {
    annotation_id: row.id,
    model_id: row.model,
    revision: row.revision,
    created: row.created,
    note: row.note,
    state: row.state,
    region: row.region_artifact
      ? `/api/artifacts/${row.region_artifact}`
      : null,
    view: row.view_artifact ? `/api/artifacts/${row.view_artifact}` : null,
    run: row.run ? JSON.parse(row.run) : null,
    ...(row.kind ? { kind: row.kind, images: row.images } : {}),
  };
}
function row_(store: Store, p: Principal, annotation: string) {
  authorize(p, "model:read");
  const row = store.get("SELECT * FROM annotations WHERE id=?", annotation);
  requireThat(
    row && row.tenant === p.tenant,
    "ACCESS_DENIED",
    "Mark not accessible.",
  );
  store.model(p, row.model);
  return row;
}
/** The folder the agent works in: the measurements as JSON, the two images, and
 *  the order itself in the words of the person who wrote it. */
export function writeRecord(store: Store, annotation: string) {
  const row = store.get("SELECT * FROM annotations WHERE id=?", annotation);
  if (!row) return;
  const directory = annotationDirectory(store, annotation),
    payload = JSON.parse(row.payload);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  writeFileSync(
    join(directory, "annotation.json"),
    JSON.stringify(
      { annotation_id: row.id, state: row.state, ...payload },
      null,
      1,
    ),
    { mode: 0o600 },
  );
  // A build order carries the plan the model already has: the structured
  // build plan as files next to the order, the purpose text as its short form.
  const building = payload.kind === "model_build";
  const plan = building
    ? (store.get("SELECT purpose FROM models WHERE id=?", row.model)?.purpose ??
      "")
    : "";
  const structured = building && copyBuildPlan(store, row.model, directory);
  writeFileSync(
    join(directory, "order.md"),
    brief(row, payload, plan, structured),
    { mode: 0o600 },
  );
  return directory;
}
/** The build plan of a model, as the build order and the viewer see it. */
export function modelBuildPlan(store: Store, p: Principal, model: string) {
  authorize(p, "model:read");
  store.model(p, model);
  return readBuildPlan(store, model);
}
/** The order a new model was asked for: the words, the pictures, nothing else
 *  — there is no geometry yet to describe. */
function creationBrief(row: any, payload: any) {
  const images = attached(payload);
  return [
    `# Creation order ${row.id}`,
    "",
    `**Order:** ${row.note || "(no text — the images are the order)"}`,
    "",
    `- Model name: ${payload.model_name ?? "—"}`,
    `- Model: \`${row.model}\` (still empty, revision \`${row.revision}\`)`,
    `- Created: ${payload.captured_at ?? row.created}`,
    "",
    "## Attachments",
    "",
    images.length
      ? images.map((i: any) => `- ${describeAttachment(i)}`).join("\n")
      : "- (none)",
    "",
  ].join("\n");
}
/** The order to draw a committed revision: no geometry change, a PDF. */
function drawingBrief(row: any, payload: any) {
  return [
    `# Drawing order ${row.id}`,
    "",
    "**Order:** A complete, standards-compliant manufacturing drawing (ISO) of this revision as a PDF.",
    "",
    `- Model: ${payload.model_name ?? "—"} (\`${row.model}\`)`,
    `- Revision: \`${row.revision}\``,
    `- Created: ${payload.captured_at ?? row.created}`,
    ...(row.note ? ["", `Note from the person: ${row.note}`] : []),
    "",
  ].join("\n");
}
/** The order to build a model that has a plan. */
function buildBrief(row: any, payload: any, plan: string, structured = false) {
  const files = attached(payload),
    references = payload.reference_setup;
  return [
    `# Build order ${row.id}`,
    "",
    `- Model: ${payload.model_name ?? "—"} (\`${row.model}\`)`,
    `- Base revision: \`${row.revision}\``,
    `- Created: ${payload.captured_at ?? row.created}`,
    row.note ? `- Addition: ${row.note}` : "",
    "",
    "## Build plan",
    "",
    ...(structured
      ? [
          "The complete build plan is in this folder as `build-plan.md` (readable) and `build-plan.json` (schema llcad-build-plan-1). Summary:",
          "",
        ]
      : []),
    plan || "(no plan stored)",
    "",
    "## Reference images",
    "",
    files.length
      ? files.map((f: any) => `- ${describeAttachment(f)}`).join("\n")
      : "- (no images with the order)",
    "",
    ...(references
      ? [
          "## Reference contract",
          "",
          references.status === "bound"
            ? `- bound: contract_hash \`${references.contract_hash}\`, references ${(references.reference_ids ?? []).map((r: string) => `\`${r}\``).join(", ")}`
            : `- not bound: ${references.reason ?? "—"}`,
          "",
        ]
      : []),
    "## Documents",
    "",
    ...(structured ? ["- `build-plan.md`, `build-plan.json` — the build plan"] : []),
    "- `.llcad/run.json` — run contract: time budget, scope, acceptance (stop gate)",
    "- `tools.md` — the shape of a patch and all operators of this service",
    "",
  ].join("\n");
}
/** The blue strokes of a mark: the person's sketch of the change itself. */
export function sketchStrokes(payload: any): any[] {
  return Array.isArray(payload?.sketch?.strokes) ? payload.sketch.strokes : [];
}
/** One blue stroke in a line: where it lies and on what — not what it is;
 *  that is read from the picture and the note. */
export function describeSketchStroke(
  s: any,
  point: (p: number[] | undefined) => string,
  number: (value: unknown) => string,
) {
  const on = (s.surfaces ?? [])
    .slice(0, 3)
    .map(
      (f: any) =>
        `${f.semantic_name ?? f.feature_id}${f.face_id ? ` / face \`${f.face_id}\`` : ""} (${Math.round((f.share ?? 0) * 100)} %)`,
    )
    .join(", ");
  return `- **${s.id}**: ${s.closed ? "closed" : "open"} stroke, ${number(s.length_mm)} mm${
    s.length_along_surface_mm != null
      ? ` (${number(s.length_along_surface_mm)} mm along the surface)`
      : ""
  }, from ${point(s.start_world)} to ${point(s.end_world)}; ${
    s.on_model ? `lies on ${on}` : "drawn freely in space (on the image plane through the pivot point)"
  }`;
}
function sketchSection(
  payload: any,
  point: (p: number[] | undefined) => string,
  number: (value: unknown) => string,
) {
  const strokes = sketchStrokes(payload);
  if (!strokes.length) return [];
  return [
    "## Blue sketch",
    "",
    "Red marks the affected region, **blue is the drawn change itself** (e.g. new edge, hole, contour, cut, extension). The order text says what the strokes mean; you see the shape in `region.png`. The world coordinates (mm) of every stroke are in `annotation.json` under `sketch.strokes[]`: `path_world` (simplified path), `samples` (ray hits with feature/face), `plane` (image plane on which points without a surface hit lie).",
    "",
    ...strokes.map((s) => describeSketchStroke(s, point, number)),
    "",
  ];
}
function brief(row: any, payload: any, plan = "", structured = false) {
  if (payload.kind === "model_build")
    return buildBrief(row, payload, plan, structured);
  if (payload.kind === "model_purpose") return creationBrief(row, payload);
  if (payload.kind === "model_drawing") return drawingBrief(row, payload);
  const number = (value: unknown) =>
    typeof value === "number" ? value.toFixed(3) : "—";
  const point = (p: number[] | undefined) =>
    p ? `${number(p[0])} | ${number(p[1])} | ${number(p[2])} mm` : "—";
  const parts = (payload.parts ?? []) as any[];
  const features = (payload.features ?? []) as any[];
  return [
    `# Change order ${row.id}`,
    "",
    `**Order:** ${row.note}`,
    "",
    `- Model: ${payload.model_name ?? row.model} (\`${row.model}\`)`,
    `- Revision: \`${row.revision}\``,
    `- Marked on: ${payload.captured_at ?? row.created}`,
    `- Centre of the mark: ${point(payload.world?.centre)}`,
    `- Region: ${point(payload.world?.min)} to ${point(payload.world?.max)}`,
    `- View direction: ${point(payload.view?.direction)}, distance ${number(payload.view?.distance_mm)} mm, ${number(payload.view?.mm_per_pixel)} mm/px`,
    "",
    "## Touched parts",
    "",
    parts.length
      ? parts
          .map(
            (part) =>
              `- **${part.name ?? part.part_id}** (\`${part.part_id}\`)${
                part.assembly_name ? ` in ${part.assembly_name}` : ""
              } — ${number(part.coverage * 100)} % of the marked area`,
          )
          .join("\n")
      : "- (none — the mark hit no geometry)",
    "",
    "## Touched features",
    "",
    features.length
      ? features
          .map(
            (f) =>
              `- **${f.semantic_name ?? f.feature_id}** (\`${f.feature_id}\`, ${f.kind ?? "?"}${
                f.operator ? `/${f.operator}` : ""
              }) — ${number(f.coverage * 100)} %, hit point ${point(f.point)}${
                f.face_id ? `, face \`${f.face_id}\`` : ""
              }`,
          )
          .join("\n")
      : "- (none)",
    "",
    ...sketchSection(payload, point, number),
    "## Documents",
    "",
    "- `annotation.json` — all measurements of this mark",
    `- \`region.png\` — crop of the view with the red mark${sketchStrokes(payload).length ? " and the blue sketch (labelled B1, B2, …)" : ""}`,
    "- `view.jpg` — the whole view at the time of the mark",
    ...attached(payload).map(
      (f: any) =>
        `- \`${f.file}\` — attached by the requester: ${describeAttachment(f)}`,
    ),
    "",
  ].join("\n");
}
