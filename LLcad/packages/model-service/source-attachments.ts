import type { Store } from "./store.js";
import { authorize, type Principal } from "../policy/index.js";
import { requireThat } from "../semantic-ir/errors.js";
import { attached, attachFile, read, writeRecord } from "./annotations.js";
import { readBuildPlan } from "./build-plan.js";

/** Preserve original references while giving each build independently owned attachments. */
export function ensureBuildAttachments(
  store: Store,
  p: Principal,
  annotation: string,
) {
  authorize(p, "model:read");
  authorize(p, "model:edit");
  const build = read(store, p, annotation);
  store.model(p, build.model_id, "model:edit");
  requireThat(
    build.payload.kind === "model_build",
    "INVALID_SCHEMA",
    "Attachments can only be adopted into a build order.",
  );
  if (attached(build.payload).length) return build;
  const source = store.get(
    `SELECT id,payload FROM annotations WHERE tenant=? AND model=?
      AND json_extract(payload,'$.kind')='model_purpose'
      AND COALESCE(json_array_length(payload,'$.files'),json_array_length(payload,'$.images'),0)>0
      ORDER BY created DESC,id DESC LIMIT 1`,
    p.tenant,
    build.model_id,
  );
  if (!source) return build;
  const files = attached(JSON.parse(source.payload)).map((image) => {
    const artifact = store.getArtifact(p, image.artifact_id);
    requireThat(
      artifact.model === build.model_id,
      "ACCESS_DENIED",
      "The reference does not belong to this model.",
    );
    return {
      kind: image.kind,
      mime: artifact.mime,
      data: store.readBlob(artifact.hash),
    };
  });
  store.atomic(() => {
    for (const file of files)
      attachFile(store, p, annotation, file.kind, file.data, file.mime);
    const payload = read(store, p, annotation).payload;
    payload.source_purpose_annotation_id = source.id;
    store.run(
      "UPDATE annotations SET payload=? WHERE id=?",
      JSON.stringify(payload),
      annotation,
    );
  });
  writeRecord(store, annotation);
  return read(store, p, annotation);
}

/** The part of the model service a build preparation needs: its store and the
 *  tool dispatcher (the same one MCP clients reach). */
export type ToolHost = {
  store: Store;
  call(p: Principal, tool: any, input: unknown): any;
};
const RESERVED = ["front", "back", "left", "right", "top", "bottom"];
const REQUIREMENT_CATEGORIES = [
  "silhouette",
  "curvature",
  "placement",
  "continuity",
  "detail",
  "text",
  "material",
  "optics",
];
function vector(v: unknown): [number, number, number] | null {
  return Array.isArray(v) &&
    v.length === 3 &&
    v.every((x) => typeof x === "number" && Number.isFinite(x)) &&
    Math.hypot(...(v as number[])) > 0.001
    ? (v as [number, number, number])
    : null;
}
/** A camera estimate from the build plan in the reference contract's (bbox
 *  relative) form. It stays an estimate: registered is never set here. */
function cameraFrom(estimate: any) {
  const direction = vector(estimate?.direction);
  if (!direction) return undefined;
  const n = Math.hypot(...direction),
    d = direction.map((x) => x / n);
  let up = vector(estimate?.up) ?? [0, 0, 1];
  const cross = Math.hypot(
    d[1] * up[2] - d[2] * up[1],
    d[2] * up[0] - d[0] * up[2],
    d[0] * up[1] - d[1] * up[0],
  );
  if (cross / Math.hypot(...up) < 0.05) up = [0, 1, 0];
  const fov =
    typeof estimate?.fov_deg === "number" && Number.isFinite(estimate.fov_deg)
      ? Math.min(90, Math.max(10, estimate.fov_deg))
      : 28;
  return {
    direction,
    up,
    projection:
      estimate?.projection === "orthographic" ? "orthographic" : "perspective",
    fov,
  };
}
const stem = (name: string) =>
  String(name ?? "")
    .split(/[\\/]/)
    .pop()!
    .replace(/\.[a-z0-9]+$/i, "")
    .toLowerCase();
/** Bind the intake pictures to the model as its reference contract, when the
 *  build plan names them as reference views and the model has no contract yet.
 *  The pictures are copied into artifacts of their own, so deleting an order
 *  never pulls an image out from under the contract. Never throws: the build
 *  goes ahead either way, and the outcome is recorded on the build order. */
export async function bindIntakeReferences(
  service: ToolHost,
  p: Principal,
  annotation: string,
) {
  const store = service.store;
  const record = (setup: Record<string, unknown>) => {
    const payload = read(store, p, annotation).payload;
    payload.reference_setup = { ...setup, at: new Date().toISOString() };
    store.run(
      "UPDATE annotations SET payload=? WHERE id=?",
      JSON.stringify(payload),
      annotation,
    );
    writeRecord(store, annotation);
    return payload.reference_setup;
  };
  try {
    const build = read(store, p, annotation),
      model = build.model_id;
    const existing = store.get(
      "SELECT contract FROM reference_contracts WHERE model=?",
      model,
    );
    if (existing) {
      const contract = JSON.parse(existing.contract);
      return record({
        status: "bound",
        source: "existing",
        contract_hash: contract.hash,
        reference_ids: (contract.references ?? []).map((r: any) => r.id),
        reason:
          "The model already has a reference contract; it stays unchanged.",
      });
    }
    const plan = readBuildPlan(store, model)?.json;
    const wanted: any[] = Array.isArray(plan?.references)
      ? plan!.references
      : [];
    if (!wanted.length)
      return record({
        status: "skipped",
        reason: plan
          ? "The build plan names no reference views."
          : "No structured build plan available.",
      });
    const images = attached(build.payload).filter((f) =>
      String(f.mime ?? "").startsWith("image/"),
    );
    const head = store.get("SELECT head FROM models WHERE id=?", model)?.head;
    const used = new Set<string>(),
      ids = new Set<string>(),
      planned = new Map<string, string>();
    const references: any[] = [];
    for (const [index, ref] of wanted.entries()) {
      if (references.length >= 12) break;
      const file = images.find(
        (f) =>
          !used.has(f.file) &&
          (stem(f.file) === stem(ref?.source) || f.kind === stem(ref?.source)),
      );
      if (!file) continue;
      used.add(file.file);
      let rid = String(ref?.id ?? "")
        .replace(/[^a-zA-Z0-9_-]+/g, "-")
        .replace(/^[^a-zA-Z0-9]+/, "")
        .slice(0, 80);
      if (
        !rid ||
        RESERVED.includes(rid) ||
        rid.startsWith("registered-") ||
        rid.startsWith("difference-") ||
        ids.has(rid)
      )
        rid = `ref-${stem(file.file).replace(/[^a-z0-9_-]+/g, "-")}-${index + 1}`;
      ids.add(rid);
      if (ref?.id) planned.set(String(ref.id), rid);
      const original = store.getArtifact(p, file.artifact_id);
      const copy = store.artifact(
        p,
        store.readBlob(original.hash),
        original.mime,
        model,
        head ?? null,
        {
          filename: file.file,
          source: "intake_reference",
          annotation,
          trusted: false,
        },
      );
      const camera = cameraFrom(ref?.camera_estimate);
      references.push({
        id: rid,
        artifact_id: copy.artifact_id,
        label: `${ref?.view ?? "view"}: ${ref?.label ?? file.file}`.slice(
          0,
          200,
        ),
        source:
          `Viewer attachment ${file.file}${ref?.mm_per_px ? `, ${ref.mm_per_px} mm/px` : ""}${ref?.notes ? ` — ${ref.notes}` : ""}`.slice(
            0,
            1000,
          ),
        ...(camera ? { camera } : {}),
      });
    }
    if (!references.length)
      return record({
        status: "skipped",
        reason:
          "No reference view of the build plan matches an image attachment of the order.",
      });
    const env = plan?.envelope ?? {};
    const dimensions = (["x", "y", "z"] as const)
      .map((axis) => ({ axis, m: env[axis] }))
      .filter(
        ({ m }) =>
          typeof m?.value_mm === "number" && m.value_mm > 0 && m.value_mm < 1e6,
      )
      .map(({ axis, m }) => ({
        id: `envelope-${axis}`,
        axis,
        value_mm: m.value_mm,
        tolerance_mm: Math.min(
          100,
          typeof m.tolerance_mm === "number" && m.tolerance_mm > 0
            ? m.tolerance_mm
            : Math.max(0.5, m.value_mm * 0.02),
        ),
        source:
          `Build plan envelope ${axis.toUpperCase()} [${m.class ?? "?"}]: ${String(m.source ?? "intake")}`.slice(
            0,
            500,
          ),
      }));
    const bound = references.map((r) => r.id);
    const seen = new Set<string>();
    const requirements = (Array.isArray(plan?.checks) ? plan!.checks : [])
      .filter(
        (c: any) =>
          REQUIREMENT_CATEGORIES.includes(c?.category) &&
          typeof c?.description === "string" &&
          c.description.trim().length >= 5,
      )
      .map((c: any, i: number) => {
        let id = String(c.id ?? "")
          .replace(/[^a-zA-Z0-9_-]+/g, "-")
          .replace(/^[^a-zA-Z0-9]+/, "")
          .slice(0, 100);
        if (!id || seen.has(id)) id = `check-${i + 1}`;
        seen.add(id);
        const refs = (Array.isArray(c.reference_ids) ? c.reference_ids : [])
          .map((r: string) => (bound.includes(r) ? r : planned.get(String(r))))
          .filter((r: any) => r && bound.includes(r));
        return {
          id,
          description: c.description.trim().slice(0, 600),
          category: c.category,
          reference_ids: [...new Set(refs.length ? refs : bound)].slice(0, 12),
        };
      })
      .slice(0, 128);
    // A schema that is stricter than this build expects (an older image limit,
    // a changed camera form) costs one retry, not the binding.
    const attempts = [
      { limit: 2576, cameras: true },
      { limit: 2000, cameras: true },
      { limit: 2000, cameras: false },
    ];
    let last: any = null;
    for (const [n, attempt] of attempts.entries()) {
      const result = await service.call(p, "cad_reference_setup", {
        model_id: model,
        base_revision: head,
        idempotency_key: `intake-references-${annotation}-${n}`,
        expected_contract_hash: null,
        references: references.map(({ camera, ...r }) =>
          attempt.cameras && camera ? { ...r, camera } : r,
        ),
        requirements,
        dimensions,
        image_pixel_limit: attempt.limit,
      });
      if (result?.contract_hash)
        return record({
          status: "bound",
          source: "intake",
          contract_hash: result.contract_hash,
          reference_ids: bound,
          requirements: requirements.length,
          dimensions: dimensions.length,
          image_pixel_limit: attempt.limit,
          cameras: attempt.cameras
            ? references.filter((r) => r.camera).length
            : 0,
        });
      last = result;
      if (result?.errors?.[0]?.code !== "INVALID_SCHEMA") break;
    }
    return record({
      status: "failed",
      reason: `cad_reference_setup: ${last?.errors?.[0]?.code ?? "?"} ${String(last?.errors?.[0]?.message ?? "").slice(0, 300)}`,
    });
  } catch (error: any) {
    try {
      return record({
        status: "failed",
        reason: String(error?.message ?? error).slice(0, 300),
      });
    } catch {
      return { status: "failed" };
    }
  }
}

/** Everything a build needs before its agent starts: the intake pictures as
 *  its own files, and — when the build plan names reference views — those
 *  pictures bound to the model as its reference contract. */
export async function prepareBuild(
  service: ToolHost,
  p: Principal,
  annotation: string,
) {
  ensureBuildAttachments(service.store, p, annotation);
  await bindIntakeReferences(service, p, annotation);
  return read(service.store, p, annotation);
}
