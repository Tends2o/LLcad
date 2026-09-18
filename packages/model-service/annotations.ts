import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { Store } from "./store.js";
import { Principal, authorize } from "../policy/index.js";
import { id } from "../semantic-ir/hash.js";
import { requireThat } from "../semantic-ir/errors.js";
/** A mark drawn on the frozen view: what the person circled, what it touches in
 *  the model, and what they want changed there. The record is kept twice — as a
 *  row, so it can be listed and searched, and as a folder of plain files, so the
 *  agent that carries out the change can read it without an API. */
const MAX_PAYLOAD_BYTES = 512 * 1024;
/** Two pictures belong to a mark on the frozen view; every order may bring its
 *  own material: pictures (`bild-n`) and files such as a mesh (`datei-n`). */
const FILE_KIND = /^(region|view|(bild|datei)-([1-9]|10))$/;
const EXTENSIONS: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "model/stl": "stl",
  "application/sla": "stl",
  "application/octet-stream": "bin",
};
export const ATTACHMENT_TYPES = Object.keys(EXTENSIONS);
/** What an annotation is about: a mark on the model, or the order that asked
 *  for a new model in the first place. */
export const ANNOTATION_KINDS = [
  "mark",
  "model_purpose",
  "model_build",
] as const;
export const ANNOTATION_STATES = [
  "captured",
  "queued",
  "running",
  "proposed",
  "succeeded",
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
    "Der Änderungsauftrag braucht einen Text (höchstens 4000 Zeichen).",
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
    "Markierungsdaten zu groß.",
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
    "Unbekannte Anhangsart: region, view, bild-1…10 oder datei-1…10.",
  );
  requireThat(
    EXTENSIONS[mime] !== undefined,
    "OUT_OF_SCOPE",
    "Nur Bilder (PNG, JPEG) und Netze (STL) gehören an einen Auftrag.",
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
export function listAnnotations(
  store: Store,
  p: Principal,
  model: string | null,
  kind: string = "mark",
) {
  authorize(p, "model:read");
  const rows = store.all(
    `SELECT id,model,revision,created,note,state,region_artifact,view_artifact,run,
            COALESCE(json_extract(payload,'$.kind'),'mark') AS kind,
            COALESCE(json_array_length(payload,'$.files'),json_array_length(payload,'$.images'),0) AS images
       FROM annotations WHERE tenant=? AND COALESCE(json_extract(payload,'$.kind'),'mark')=?` +
      (model ? " AND model=?" : "") +
      " ORDER BY created DESC LIMIT 100",
    ...(model ? [p.tenant, kind, model] : [p.tenant, kind]),
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
    "Der Auftrag läuft gerade; zuerst abbrechen.",
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
    "Markierung nicht zugänglich.",
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
  // A build order carries the plan the model already has.
  const plan =
    payload.kind === "model_build"
      ? (store.get("SELECT purpose FROM models WHERE id=?", row.model)
          ?.purpose ?? "")
      : "";
  writeFileSync(join(directory, "auftrag.md"), brief(row, payload, plan), {
    mode: 0o600,
  });
  return directory;
}
/** The order a new model was asked for: the words, the pictures, nothing else
 *  — there is no geometry yet to describe. */
function creationBrief(row: any, payload: any) {
  const images = attached(payload);
  return [
    `# Erstellungsauftrag ${row.id}`,
    "",
    `**Auftrag:** ${row.note || "(kein Text — die Bilder sind der Auftrag)"}`,
    "",
    `- Modellname: ${payload.model_name ?? "—"}`,
    `- Modell: \`${row.model}\` (noch leer, Revision \`${row.revision}\`)`,
    `- Angelegt: ${payload.captured_at ?? row.created}`,
    "",
    "## Anhänge",
    "",
    images.length
      ? images
          .map(
            (i: any) =>
              `- \`${i.file}\`${i.bytes ? ` (${Math.round(i.bytes / 1024)} KiB)` : ""}`,
          )
          .join("\n")
      : "- (keine)",
    "",
  ].join("\n");
}
/** The order to build a model that has a plan. */
function buildBrief(row: any, payload: any, plan: string) {
  return [
    `# Bauauftrag ${row.id}`,
    "",
    `- Modell: ${payload.model_name ?? "—"} (\`${row.model}\`)`,
    `- Basis-Revision: \`${row.revision}\``,
    `- Angelegt: ${payload.captured_at ?? row.created}`,
    row.note ? `- Zusatz: ${row.note}` : "",
    "",
    "## Bauplan",
    "",
    plan || "(kein Plan hinterlegt)",
    "",
    "## Unterlagen",
    "",
    "- `werkzeuge.md` — die Form eines Patches und alle Operatoren dieses Dienstes",
    "",
  ].join("\n");
}
function brief(row: any, payload: any, plan = "") {
  if (payload.kind === "model_build") return buildBrief(row, payload, plan);
  if (payload.kind === "model_purpose") return creationBrief(row, payload);
  const number = (value: unknown) =>
    typeof value === "number" ? value.toFixed(3) : "—";
  const point = (p: number[] | undefined) =>
    p ? `${number(p[0])} | ${number(p[1])} | ${number(p[2])} mm` : "—";
  const parts = (payload.parts ?? []) as any[];
  const features = (payload.features ?? []) as any[];
  return [
    `# Änderungsauftrag ${row.id}`,
    "",
    `**Auftrag:** ${row.note}`,
    "",
    `- Modell: ${payload.model_name ?? row.model} (\`${row.model}\`)`,
    `- Revision: \`${row.revision}\``,
    `- Markiert am: ${payload.captured_at ?? row.created}`,
    `- Mittelpunkt der Markierung: ${point(payload.world?.centre)}`,
    `- Bereich: ${point(payload.world?.min)} bis ${point(payload.world?.max)}`,
    `- Blickrichtung: ${point(payload.view?.direction)}, Abstand ${number(payload.view?.distance_mm)} mm, ${number(payload.view?.mm_per_pixel)} mm/px`,
    "",
    "## Berührte Teile",
    "",
    parts.length
      ? parts
          .map(
            (part) =>
              `- **${part.name ?? part.part_id}** (\`${part.part_id}\`)${
                part.assembly_name ? ` in ${part.assembly_name}` : ""
              } — ${number(part.coverage * 100)} % der Markierungsfläche`,
          )
          .join("\n")
      : "- (keine – die Markierung traf keine Geometrie)",
    "",
    "## Berührte Features",
    "",
    features.length
      ? features
          .map(
            (f) =>
              `- **${f.semantic_name ?? f.feature_id}** (\`${f.feature_id}\`, ${f.kind ?? "?"}${
                f.operator ? `/${f.operator}` : ""
              }) — ${number(f.coverage * 100)} %, Treffpunkt ${point(f.point)}${
                f.face_id ? `, Fläche \`${f.face_id}\`` : ""
              }`,
          )
          .join("\n")
      : "- (keine)",
    "",
    "## Unterlagen",
    "",
    "- `annotation.json` — alle Messwerte dieser Markierung",
    "- `region.png` — Ausschnitt der Ansicht mit der roten Markierung",
    "- `view.jpg` — die ganze Ansicht zum Zeitpunkt der Markierung",
    ...attached(payload).map(
      (f: any) =>
        `- \`${f.file}\` — vom Auftraggeber angehängt${f.bytes ? ` (${Math.round(f.bytes / 1024)} KiB)` : ""}`,
    ),
    "",
  ].join("\n");
}
