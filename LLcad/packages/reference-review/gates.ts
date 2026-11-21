import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { ModelService, CallContext } from "../model-service/index.js";
import type { Principal } from "../policy/index.js";
import { hash, id } from "../semantic-ir/hash.js";
import { imagePython } from "../semantic-ir/host.js";
import { CadError, requireThat, safeError } from "../semantic-ir/errors.js";
import { checkedToolResponse, failureResponse } from "../model-service/result-contracts.js";
import { readStoredPreview } from "../model-service/stored-preview.js";
import { readColours } from "../model-service/colours.js";
import { Contract, isAbsolute } from "./schema.js";

/* --- Deterministic gates and the vision core ------------------------------------
cad_view      renders a revision (VTK offscreen, render_vtk.py) in lit / albedo / ids
              channels for preset, absolute or reference cameras; answers images
              (artifacts + MCP image content) and a per-part table.
cad_reference_register
              fits an absolute camera to a reference image (register.py: landmark
              pose + silhouette/part-class refinement, mirrored starts, mirror-image
              handedness test) and stores it with its residuals; registered only
              when every threshold holds.
cad_gates     the deterministic floor: native checks (declared dimensions, contact
              graph -> floating parts and coincident faces between differently
              coloured parts, control visibility, sightline, stations, handedness
              sides, counts, parts outside the envelope, name vs content, open
              candidates) and image checks per reference camera (gates.py).
              Answers {floor_passed, residuals ranked}; never "done".
Everything runs in the OCCT venv (vtk, numpy, scipy, PIL) as a child process. */

const execFileAsync = promisify(execFile);
/** Helpers run without a console window (the service itself has none on Windows). */
const execute = ((file: string, args: string[], options: any) =>
  execFileAsync(file, args, { windowsHide: true, ...options })) as typeof execFileAsync;
/** Symbol under which a tool answer carries MCP image content (not JSON). */
export const MCP_IMAGES = Symbol.for("llcad.mcp.images");
const PREVIEW_MIME = "model/x-mathforge-preview";
const GATES_VERSION = 3;
const DEFAULT_VIEWS = [
  { id: "front", preset: "front" },
  { id: "left", preset: "left" },
  { id: "back", preset: "back" },
  { id: "iso", preset: "iso" },
];
const BRIEF_BYTES = 1300;
const TERMINAL = ["succeeded", "failed", "cancelled"];
const SEVERITY_ORDER: Record<string, number> = { fail: 0, warn: 1, pass: 2, info: 3 };
const ORTHO_SOURCES = new Set(["orthographic_turntable", "orthographic_render", "datasheet_render"]);

const round = (v: number, d = 3) => (Number.isFinite(v) ? Number(v.toFixed(d)) : v);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export class VisionTools {
  /** Registrations are CPU-heavy: one at a time, the next waits its turn. */
  private registerQueue: Promise<unknown> = Promise.resolve();
  constructor(private service: ModelService) {
    service.store.run(
      "CREATE TABLE IF NOT EXISTS vision_cache(key TEXT PRIMARY KEY,model TEXT NOT NULL,tool TEXT NOT NULL,created TEXT NOT NULL,payload TEXT NOT NULL)",
    );
  }
  private get root() {
    return resolve(process.env.MATHFORGE_ROOT ?? process.cwd());
  }
  private work(kind: string) {
    const folder = join(this.service.store.root, "vision-work", kind, id("w"));
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    return folder;
  }
  private async python(script: string, action: string, spec: any, folder: string, timeout: number, context: CallContext) {
    const file = join(folder, `${action}-spec.json`);
    writeFileSync(file, JSON.stringify(spec), { mode: 0o600 });
    const started = Date.now();
    const timer = context.progress
      ? setInterval(() => {
          try {
            context.progress!({ elapsed_s: Math.round((Date.now() - started) / 1000), message: `${action} running` });
          } catch {
            /* a lost progress notification never fails the call */
          }
        }, 5000)
      : null;
    try {
      const { stdout } = await execute(
        imagePython(this.root),
        [join(this.root, "packages/reference-review", script), action, file],
        {
          timeout,
          maxBuffer: 16 * 1024 * 1024,
          signal: context.signal,
          env: { ...process.env, EGL_LOG_LEVEL: "fatal", OMP_NUM_THREADS: "2", PYTHONWARNINGS: "ignore" },
        },
      );
      return JSON.parse(stdout);
    } catch (error: any) {
      if (error instanceof CadError) throw error;
      const tail = String(error?.stderr ?? error?.message ?? "").trim().split("\n").slice(-2).join(" ").slice(0, 300);
      throw new CadError(
        error?.killed || error?.signal === "SIGTERM" ? "BUDGET_EXCEEDED" : "KERNEL_FAILURE",
        `${script} ${action}: ${tail || "failed"}`,
      );
    } finally {
      if (timer) clearInterval(timer);
    }
  }
  private contractRow(model: string): Contract | null {
    const row = this.service.store.get("SELECT contract FROM reference_contracts WHERE model=?", model);
    return row ? Contract.parse(JSON.parse(row.contract)) : null;
  }
  /** The tessellation of a revision on disk: the stored full preview (viewer default,
   *  0.02 mm), else a render job at the same key; content-addressed and reused. */
  private async preview(p: Principal, model: string, revision: string, deadline: number, context: CallContext) {
    const store = this.service.store;
    let result: any = readStoredPreview(store, p, model, revision);
    if (!result) {
      result = await this.service.call(p, "cad_render", {
        model_id: model,
        revision,
        deflection: { value: "0.02", unit: "mm" },
        idempotency_key: `vision-preview-${hash([model, revision]).slice(0, 40)}`,
      });
      if (result?.status === "failed") {
        const e = result.errors?.[0] ?? {};
        throw new CadError(e.code ?? "KERNEL_FAILURE", `Preview: ${e.message ?? "failed"}`, e.details ?? {});
      }
      if (result.job_id && !result.artifacts?.length) {
        const job = await this.waitJob(p, result.job_id, deadline, context);
        requireThat(job.status === "succeeded", "KERNEL_FAILURE", `Preview job ${job.status}.`, { job_id: result.job_id });
        result = job.result;
      }
    }
    const artifacts: any[] = result.artifacts ?? [];
    const chosen = artifacts.find((x) => x.mime === PREVIEW_MIME) ?? artifacts.find((x) => x.manifest?.filename === "preview.json");
    requireThat(chosen, "KERNEL_FAILURE", "Preview without a mesh.");
    const artifact = store.getArtifact(p, chosen.artifact_id);
    const folder = join(store.root, "vision-work", "previews");
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    const file = join(folder, artifact.hash + (chosen.mime === PREVIEW_MIME ? ".bin" : ".json"));
    if (!existsSync(file)) writeFileSync(file, store.readBlob(artifact.hash), { mode: 0o600 });
    return file;
  }
  private async waitJob(p: Principal, jobId: string, deadline: number, context: CallContext) {
    let job = this.service.jobs.get(p, jobId);
    let reported = Date.now();
    while (!TERMINAL.includes(job.status) && Date.now() < deadline && !context.signal?.aborted) {
      await sleep(150);
      job = this.service.jobs.get(p, jobId);
      if (context.progress && Date.now() - reported >= 5000) {
        reported = Date.now();
        try {
          context.progress({ elapsed_s: 0, message: `job ${job.status}` });
        } catch {
          /* ignore */
        }
      }
    }
    return job;
  }
  /** Declared appearance per part and per output feature (colours.ts is the source
   *  of what the viewer shows: names, saved overrides). */
  private appearance(p: Principal, model: string, r: any) {
    const colours: any = readColours(this.service.store, p, model, r.id);
    const parts: Record<string, any> = {};
    const info: Record<string, any> = {};
    for (const part of r.ir.structure?.parts ?? []) {
      const e = colours.parts?.[part.id];
      if (!e) continue;
      const glass = e.finish === "clear glass" || (typeof e.transmission === "number" && e.transmission > 0);
      const emissive = !!e.emissive;
      parts[part.id] = { name: String(e.name ?? part.id).slice(0, 80), colour: e.colour, finish: e.finish, glass, emissive };
      for (const f of part.outputs ?? []) info[f] = { part: part.id, colour: e.colour, glass, emissive };
    }
    return { parts, info, digest: hash({ parts }) };
  }
  private referenceFile(p: Principal, contract: Contract, ref: any, folder: string) {
    const store = this.service.store;
    const artifact = store.getArtifact(p, ref.artifact_id);
    requireThat(
      artifact.hash === contract.reference_hashes[ref.id],
      "INTEGRITY_FAILURE",
      "The reference image was replaced after setup.",
    );
    const data = store.readBlob(artifact.hash);
    const ext = data.subarray(0, 4).toString("hex") === "89504e47" ? ".png" : ".img";
    const file = join(folder, `ref-${ref.id}${ext}`);
    writeFileSync(file, data, { mode: 0o600 });
    let mask: string | undefined;
    if (ref.mask_artifact_id) {
      const m = store.getArtifact(p, ref.mask_artifact_id);
      mask = join(folder, `mask-${ref.id}.png`);
      writeFileSync(mask, store.readBlob(m.hash), { mode: 0o600 });
    }
    return { file, mask };
  }
  /** Landmarks with feature ids resolved to the feature's bound centre in r. */
  private landmarkPoints(r: any, landmarks: any[]) {
    const facts = r.geometry?.facts ?? {};
    return landmarks.map((l, i) => {
      if (l.point_mm) return { id: l.id ?? `lm${i + 1}`, px: l.px, point_mm: l.point_mm, role: l.role ?? "fit" };
      const b = facts[l.feature_id]?.bounds;
      requireThat(
        Array.isArray(b) && b.length === 6,
        "INVALID_SCHEMA",
        `Landmark ${l.id ?? i + 1}: feature ${l.feature_id} has no geometry in the revision.`,
      );
      return {
        id: l.id ?? l.feature_id,
        px: l.px,
        point_mm: [(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2],
        role: l.role ?? "fit",
        feature_id: l.feature_id,
      };
    });
  }
  private cached(key: string) {
    const row = this.service.store.get("SELECT payload FROM vision_cache WHERE key=?", key);
    return row ? JSON.parse(row.payload) : null;
  }
  private remember(key: string, model: string, tool: string, payload: any) {
    this.service.store.run(
      "INSERT INTO vision_cache(key,model,tool,created,payload) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,created=excluded.created",
      key,
      model,
      tool,
      new Date().toISOString(),
      JSON.stringify(payload),
    );
  }

  // --- cad_view --------------------------------------------------------------------
  async view(p: Principal, a: any, trace: string, context: CallContext = {}) {
    try {
      const store = this.service.store;
      const m = store.model(p, a.model_id);
      const r = store.revision(p, a.model_id, a.revision ?? m.head);
      const look = this.appearance(p, a.model_id, r);
      const contract = this.contractRow(a.model_id);
      const specs: any[] = (a.views ?? DEFAULT_VIEWS).map((v: any, i: number) => ({ ...v, id: v.id ?? v.preset ?? v.reference_id ?? `view${i + 1}` }));
      requireThat(new Set(specs.map((v) => v.id)).size === specs.length, "INVALID_SCHEMA", "View ids must be unique.");
      const folder = this.work("view");
      const views: any[] = [];
      for (const v of specs) {
        if (v.reference_id) {
          const ref: any = contract?.references.find((x) => x.id === v.reference_id);
          requireThat(ref, "INVALID_SCHEMA", `Reference ${v.reference_id} is not in the reference contract.`);
          requireThat(ref.camera, "REFERENCE_REGISTRATION_REQUIRED", `Reference ${v.reference_id} has no camera (cad_reference_register).`);
          const entry: any = { id: v.id, reference_id: ref.id };
          if (isAbsolute(ref.camera)) entry.camera = ref.camera;
          else entry.relative = ref.camera;
          if (ref.registered && isAbsolute(ref.camera)) entry.photo = this.referenceFile(p, contract!, ref, folder).file;
          views.push(entry);
        } else if (v.camera) {
          views.push(isAbsolute(v.camera) ? { id: v.id, camera: v.camera } : { id: v.id, relative: v.camera });
        } else views.push({ id: v.id, preset: v.preset, projection: v.projection ?? "perspective", fov_deg: v.fov_deg ?? 28 });
      }
      const key = hash({
        v: GATES_VERSION,
        tool: "cad_view",
        revision: r.id,
        views: views.map(({ photo, ...rest }) => ({ ...rest, photo: photo ? contract?.hash : null })),
        width: a.width,
        height: a.height,
        channels: a.channels,
        look: look.digest,
      });
      let out = this.cached(key);
      const cached = !!out;
      if (!out) {
        const preview = await this.preview(p, a.model_id, r.id, Date.now() + 100000, context);
        out = await this.python(
          "render_vtk.py",
          "view",
          { preview, info: look.info, parts: look.parts, views, width: a.width, height: a.height, channels: a.channels, out_dir: folder },
          folder,
          120000,
          context,
        );
        for (const v of out.views) {
          v.artifacts = {};
          for (const [channel, path] of Object.entries<string>(v.images)) {
            const data = readFileSync(path);
            const art = store.artifact(p, data, "image/png", a.model_id, r.id, {
              filename: `${v.id}-${channel}.png`,
              kind: "view_render",
              renderer: "vtk-offscreen",
              channel,
              view_id: v.id,
              camera: v.camera,
            });
            v.artifacts[channel] = art.artifact_id;
          }
          delete v.images;
        }
        this.remember(key, a.model_id, "cad_view", out);
      }
      rmSync(folder, { recursive: true, force: true });
      const only = a.parts ? new Set<string>(a.parts) : null;
      const limit = a.detail === "full" ? 64 : 24;
      const images: any[] = [];
      const payload = {
        model_id: a.model_id,
        revision: r.id,
        views: out.views.map((v: any) => {
          const rows = v.parts.filter((row: any) => !only || only.has(row.part_id));
          const spec = views.find((x) => x.id === v.id);
          const imgs = a.channels.map((channel: string) => {
            const artifact_id = v.artifacts[channel];
            if (a.return_images) images.push({ artifact_id, view: v.id, channel });
            return { channel, artifact_id, width: v.camera.width_px, height: v.camera.height_px };
          });
          return {
            id: v.id,
            camera: v.camera,
            mm_per_px: v.mm_per_px,
            ...(spec?.reference_id ? { reference_id: spec.reference_id } : {}),
            photo_compared: !!spec?.photo,
            images: imgs,
            parts: rows.slice(0, limit),
            parts_visible: rows.length,
          };
        }),
        parts: Object.fromEntries(
          Object.entries(look.parts).map(([pid, e]: [string, any]) => [
            pid,
            { name: e.name, hex_declared: e.colour, class: e.glass ? "glass" : e.emissive ? "emissive" : "opaque" },
          ]),
        ),
        channels: a.channels,
        images_returned: images.length,
        cached,
        seconds: round(out.seconds ?? 0, 2),
        note:
          "Image coordinates: pixels from the top-left. delta_e00 compares the declared hex with the lit render (lighting shift, not a gate); photo_delta_e00 compares it with the registered photo under the part mask. Images are artifacts (GET /api/artifacts/{id}); MCP clients also receive them as image content.",
      };
      const response: any = checkedToolResponse("cad_view" as any, payload, trace);
      if (images.length) {
        const blocks = images.map((img) => {
          const art = store.getArtifact(p, img.artifact_id);
          return { type: "image", data: store.readBlob(art.hash).toString("base64"), mimeType: "image/png" };
        });
        Object.defineProperty(response, MCP_IMAGES, { value: blocks, enumerable: false });
      }
      return response;
    } catch (error) {
      return failureResponse(error, trace);
    }
  }

  // --- cad_reference_register -----------------------------------------------------------
  async register(p: Principal, a: any, trace: string, context: CallContext = {}) {
    const turn = this.registerQueue.then(() => this.registerNow(p, a, trace, context));
    this.registerQueue = turn.catch(() => undefined);
    return turn;
  }
  private async registerNow(p: Principal, a: any, trace: string, context: CallContext) {
    try {
      const store = this.service.store;
      const m = store.model(p, a.model_id, "model:edit");
      const r = store.revision(p, a.model_id, a.revision ?? m.head);
      const contract = this.contractRow(a.model_id);
      requireThat(contract, "REFERENCE_REGISTRATION_REQUIRED", "No reference contract: run cad_reference_setup first.");
      requireThat(
        !a.expected_contract_hash || a.expected_contract_hash === contract!.hash,
        "STALE_REVISION",
        "The reference contract has changed in the meantime.",
        { contract_hash: contract!.hash },
      );
      const ref: any = contract!.references.find((x) => x.id === a.reference_id);
      requireThat(ref, "INVALID_SCHEMA", `Reference ${a.reference_id} is not in the reference contract.`);
      if (a.mask_artifact_id) store.getArtifact(p, a.mask_artifact_id);
      const sourceType = a.source_type ?? ref.source_type ?? "perspective_photo";
      const mode = a.mode ?? (ORTHO_SOURCES.has(sourceType) ? "orthographic" : "perspective");
      const landmarks = a.landmarks ?? ref.landmarks ?? [];
      const points = this.landmarkPoints(r, landmarks);
      const exclusions = [...(ref.exclusions ?? []), ...(a.exclusion_boxes ?? [])];
      const folder = this.work("register");
      const deadline = Date.now() + 110000;
      const preview = await this.preview(p, a.model_id, r.id, deadline, context);
      const look = this.appearance(p, a.model_id, r);
      const files = this.referenceFile(p, contract!, { ...ref, mask_artifact_id: a.mask_artifact_id ?? ref.mask_artifact_id }, folder);
      const iouMin = contract!.gates?.thresholds?.[ORTHO_SOURCES.has(sourceType) ? "orthographic" : sourceType === "closeup" ? "closeup" : "perspective"]?.iou_min;
      const spec: any = {
        preview,
        info: look.info,
        image: files.file,
        ...(files.mask ? { mask: files.mask } : {}),
        exclusions,
        landmarks: points,
        mode,
        source_type: sourceType,
        out_dir: folder,
        max_seconds: a.max_seconds,
        ...(iouMin !== undefined ? { iou_min: iouMin } : {}),
      };
      if (ref.camera && isAbsolute(ref.camera)) spec.init_camera = ref.camera;
      else if (ref.camera) spec.init_relative = ref.camera;
      const out = await this.python("register.py", "register", spec, folder, (a.max_seconds + 45) * 1000, context);
      const overlayPng = readFileSync(out.overlay);
      rmSync(folder, { recursive: true, force: true });
      const overlay = store.artifact(p, overlayPng, "image/png", a.model_id, r.id, {
        filename: `registration-${ref.id}.png`,
        kind: "registration_overlay",
        reference_id: ref.id,
        legend: "grey both, blue only in the image, red only in the model; green circle = landmark in the image, red cross = projected feature",
      });
      const record = {
        method: `landmark pose + silhouette/part-class Powell, mirrored starts (${mode})`,
        revision: r.id,
        registered_at: new Date().toISOString(),
        source_type: sourceType,
        registered: !!out.registered,
        reasons: (out.reasons ?? []).slice(0, 12).map((s: string) => s.slice(0, 300)),
        residuals: Object.fromEntries(
          Object.entries(out.residuals ?? {}).filter(([, v]) => v === null || typeof v === "number"),
        ) as Record<string, number | null>,
        thresholds: out.thresholds ?? {},
        facing_margin: out.facing?.margin ?? null,
        handedness: { verdict: out.handedness?.verdict ?? "not_checked", margin: out.handedness?.margin ?? null },
        overlay_artifact_id: overlay.artifact_id,
      };
      let stored = false;
      let contractHash = contract!.hash;
      const notes: string[] = [...(out.notes ?? [])];
      if (a.store) {
        const keepOld = ref.registered && !out.registered;
        if (keepOld) notes.push("The reference stays registered with its earlier camera; this attempt is only reported.");
        else {
          const updated = {
            ...ref,
            camera: out.camera,
            registered: !!out.registered,
            source_type: sourceType,
            ...(landmarks.length ? { landmarks } : {}),
            ...(exclusions.length ? { exclusions } : {}),
            ...(a.mask_artifact_id ? { mask_artifact_id: a.mask_artifact_id } : {}),
            registration: record,
          };
          const { hash: _old, ...body } = contract as any;
          body.references = contract!.references.map((x: any) => (x.id === ref.id ? updated : x));
          const frozen = Contract.parse({ ...body, hash: hash(body) });
          store.run("UPDATE reference_contracts SET contract=? WHERE model=?", JSON.stringify(frozen), a.model_id);
          store.audit("reference_registered", {
            model_id: a.model_id,
            revision: r.id,
            reference_id: ref.id,
            registered: !!out.registered,
            contract_hash: frozen.hash,
          });
          stored = true;
          contractHash = frozen.hash;
        }
      }
      const payload = {
        model_id: a.model_id,
        revision: r.id,
        reference_id: ref.id,
        registered: !!out.registered,
        reasons: out.reasons ?? [],
        camera: out.camera,
        residuals: record.residuals,
        thresholds: record.thresholds,
        landmarks: (out.landmarks ?? []).map((l: any) => ({ ...l, id: l.id ?? null })),
        facing: out.facing ?? {},
        handedness: out.handedness ?? {},
        contract_hash: contractHash,
        stored,
        overlay_artifact_id: overlay.artifact_id,
        evaluations: out.evaluations ?? 0,
        seconds: round(out.seconds ?? 0, 2),
        note: [
          "registered is true only when landmark RMS, silhouette IoU (per source type), landmark-triple orientation and the mirror-image test all hold; otherwise the camera is a hypothesis and image gates on it are advisory.",
          "handedness compares the model with its own mirror image (X mid-plane): mirror_fits_better means a left/right error somewhere (check landmarks show where).",
          ...notes,
        ].join(" "),
      };
      return checkedToolResponse("cad_reference_register" as any, payload, trace);
    } catch (error) {
      return failureResponse(error, trace);
    }
  }

  // --- cad_gates --------------------------------------------------------------------
  async gates(p: Principal, a: any, trace: string, context: CallContext = {}) {
    try {
      const store = this.service.store;
      const started = Date.now();
      const deadline = started + (a.wait_ms ?? 100000);
      const m = store.model(p, a.model_id);
      const r = store.revision(p, a.model_id, a.revision ?? m.head);
      const contract = this.contractRow(a.model_id);
      requireThat(
        !a.contract_hash || (contract && contract.hash === a.contract_hash),
        "STALE_REVISION",
        "The reference contract does not match contract_hash.",
        { contract_hash: contract?.hash ?? null },
      );
      const look = this.appearance(p, a.model_id, r);
      const want = new Set<string>(a.checks ?? []);
      const on = (c: string) => !want.size || want.has(c);
      const key = hash({
        v: GATES_VERSION,
        tool: "cad_gates",
        revision: r.id,
        contract: contract?.hash ?? null,
        checks: [...want].sort(),
        views: a.views ?? null,
        look: look.digest,
      });
      const hit = this.cached(key);
      if (hit) return checkedToolResponse("cad_gates" as any, this.gatesPayload(hit, a, true), trace);
      const gspec: any = contract?.gates ?? {};
      const rows: any[] = [];
      const pending: string[] = [];
      let graph: "done" | "pending" | "skipped" | "failed" = "skipped";
      let cg: any = null;
      const tolerance = gspec.contact_tolerance_mm ?? 0.05;
      // contact graph (one measure job, deduplicated by revision and tolerance)
      if (on("contacts")) {
        const res: any = await this.service.call(p, "cad_measure", {
          model_id: a.model_id,
          revision: r.id,
          idempotency_key: `gates-contacts-${hash([r.id, tolerance, GATES_VERSION]).slice(0, 40)}`,
          all_pairs: { tolerance_mm: tolerance },
        });
        if (res?.status === "failed") {
          graph = "failed";
          rows.push(this.row("contact_graph", "measured", String(res.errors?.[0]?.message ?? "failed").slice(0, 160), "", "warn", "model", 0));
        } else if (res?.job_id) {
          const job = await this.waitJob(p, res.job_id, deadline, context);
          if (job.status === "succeeded") {
            graph = "done";
            cg = job.result?.measurements;
          } else if (TERMINAL.includes(job.status)) {
            graph = "failed";
            rows.push(this.row("contact_graph", "measured", `job ${job.status}`, "", "warn", "model", 0));
          } else {
            graph = "pending";
            pending.push(`contact graph job ${res.job_id}`);
          }
        }
      }
      // declared feature contacts of the contract (one pairs job)
      let declared: any = null;
      if (on("contacts") && contract?.contacts.length) {
        const features = new Set(r.ir.features.map((f: any) => f.id));
        const pairs = contract.contacts.filter((c) => features.has(c.feature_id) && features.has(c.other_feature_id));
        for (const c of contract.contacts)
          if (!pairs.includes(c))
            rows.push(this.row("declared_contact", `<= ${c.maximum_gap_mm}`, "feature missing", "mm", "fail", c.id, 1));
        if (pairs.length) {
          const res: any = await this.service.call(p, "cad_measure", {
            model_id: a.model_id,
            revision: r.id,
            idempotency_key: `gates-declared-${hash([r.id, contract.hash]).slice(0, 40)}`,
            pairs: pairs.map((c) => [c.feature_id, c.other_feature_id]),
          });
          if (res?.job_id) {
            const job = await this.waitJob(p, res.job_id, deadline, context);
            if (job.status === "succeeded") declared = { pairs, graph: job.result?.measurements };
            else if (!TERMINAL.includes(job.status)) pending.push(`declared contacts job ${res.job_id}`);
            else rows.push(this.row("declared_contact", "measured", `job ${job.status}`, "", "warn", "contract", 0));
          } else if (res?.status === "failed")
            rows.push(this.row("declared_contact", "measured", String(res.errors?.[0]?.message ?? "failed").slice(0, 160), "", "warn", "contract", 0));
        }
      }
      // python: mesh and image checks
      const folder = this.work("gates");
      const preview = await this.preview(p, a.model_id, r.id, Math.max(deadline, Date.now() + 30000), context);
      const views: any[] = [];
      const wantedViews = a.views ? new Set<string>(a.views) : null;
      if (on("images") && contract)
        for (const ref of contract.references as any[]) {
          if (wantedViews && !wantedViews.has(ref.id)) continue;
          if (!ref.camera || !isAbsolute(ref.camera)) continue;
          const files = this.referenceFile(p, contract, ref, folder);
          let landmarks: any[] = [];
          try {
            landmarks = this.landmarkPoints(r, ref.landmarks ?? []);
          } catch (error) {
            rows.push(this.row("landmark", "resolvable", safeError(error).message.slice(0, 160), "", "warn", ref.id, 0));
          }
          views.push({
            reference_id: ref.id,
            registered: !!ref.registered,
            image: files.file,
            ...(files.mask ? { mask: files.mask } : {}),
            camera: ref.camera,
            source_type: ref.source_type ?? (ref.camera.projection === "orthographic" ? "orthographic_render" : "perspective_photo"),
            exclusions: ref.exclusions ?? [],
            landmarks,
            markings: (gspec.markings ?? []).filter((x: any) => x.reference_id === ref.id),
            must_be_visible: (gspec.must_be_visible ?? []).filter((x: any) => !x.reference_ids || x.reference_ids.includes(ref.id)),
            counts: (gspec.counts ?? [])
              .filter((c: any) => c.visible && c.visible[ref.id] !== undefined)
              .map((c: any) => ({ id: c.id, feature_ids: c.feature_ids, expected_visible: c.visible[ref.id] })),
            registration: ref.registration ?? null,
            revision: r.id,
          });
        }
      const dims = contract?.dimensions ?? [];
      const out = await this.python(
        "gates.py",
        "gates",
        {
          preview,
          info: look.info,
          parts: look.parts,
          checks: [...want].map((c) => (c === "controls" ? "controls" : c)),
          controls: on("controls") ? gspec.controls ?? [] : [],
          optical_axes: on("sightline") ? gspec.optical_axes ?? [] : [],
          stations: on("stations") ? gspec.stations ?? [] : [],
          sides: on("sides") ? gspec.sides ?? [] : [],
          counts: on("counts") ? gspec.counts ?? [] : [],
          envelope: on("envelope") ? dims.filter((d) => !d.feature_id).map((d) => ({ id: d.id, axis: d.axis, value_mm: d.value_mm, tolerance_mm: d.tolerance_mm })) : [],
          thresholds: gspec.thresholds ?? {},
          views,
        },
        folder,
        150000,
        context,
      );
      rmSync(folder, { recursive: true, force: true });
      rows.push(...out.rows);
      const visible = new Set<string>(out.visible_parts ?? []);
      // declared dimensions against the evaluated bounds
      if (on("envelope"))
        for (const d of dims) {
          const facts = r.geometry?.facts ?? {};
          const b = d.feature_id ? facts[d.feature_id]?.bounds : r.geometry?.aggregate?.bounds;
          const k = { x: 0, y: 1, z: 2 }[d.axis];
          if (!Array.isArray(b)) {
            rows.push(this.row("dimension", `${d.value_mm} ± ${d.tolerance_mm}`, "no bounds", "mm", "fail", d.id, 1));
            continue;
          }
          const measured = b[k + 3] - b[k];
          const err = Math.abs(measured - d.value_mm);
          rows.push(
            this.row(
              "dimension",
              `${d.value_mm} ± ${d.tolerance_mm}`,
              round(measured, 3),
              "mm",
              err > d.tolerance_mm ? "fail" : "pass",
              `${d.id}${d.feature_id ? " (" + d.feature_id + ")" : " (overall " + d.axis + ")"}`,
              err > d.tolerance_mm ? err / d.tolerance_mm - 1 : 0,
            ),
          );
        }
      // contact graph: floating parts, coincident faces between different appearances
      if (cg) {
        for (const part of cg.floating_parts ?? [])
          rows.push(
            this.row("floating_part", "touches a neighbour", "floating", "", visible.has(part) ? "fail" : "warn", `${part}${visible.has(part) ? "" : " (hidden)"}`, 1),
          );
        for (const part of cg.undetermined_parts ?? [])
          rows.push(this.row("contact_undetermined", "determined", "undetermined", "", "warn", part, 0));
        const differ = (x: string, y: string) => {
          const pa = look.parts[x], pb = look.parts[y];
          if (!pa || !pb) return true;
          return String(pa.colour).toLowerCase() !== String(pb.colour).toLowerCase() || pa.finish !== pb.finish;
        };
        let same = 0,
          seated = 0;
        const seenPairs = new Set<string>();
        for (const c of cg.coincident ?? []) {
          const pairKey = [c.a, c.b, c.feature_a, c.feature_b].join("|");
          if (seenPairs.has(pairKey)) continue;
          seenPairs.add(pairKey);
          // Face-to-face contact (outward normals opposite) is a seated part, never
          // visible, so it cannot flicker; only coplanar skins facing the same way can.
          if (c.sense === "opposite") seated++;
          else if (differ(c.a, c.b))
            rows.push(
              this.row(
                "coincident_faces",
                `0 between different appearances (gap >= ${tolerance})`,
                round(c.gap_mm ?? 0, 4),
                "mm gap",
                "fail",
                `${c.a}/${c.b} (${c.feature_a}:${c.face_a?.slice(0, 13)} ~ ${c.feature_b})`,
                Math.min(10, (c.overlap_area_mm2_estimate ?? 1) / 100),
              ),
            );
          else same++;
        }
        const cut = cg.truncated?.coincident ?? 0;
        if (cut) rows.push(this.row("coincident_faces", "all listed", `${cut} more not listed`, "pairs", "warn", "contact graph truncated", 0));
        if (same) rows.push(this.row("coincident_faces", "same appearance", same, "pairs", "info", "no flicker between equal colours", 0));
        if (seated) rows.push(this.row("coincident_faces", "face-to-face contacts", seated, "pairs", "info", "seated parts (opposite normals), no flicker", 0));
        for (const pr of cg.pairs ?? [])
          if (pr.inner_solution)
            rows.push(this.row("interference", "no containment", "one inside the other", "", "warn", `${pr.a}/${pr.b}`, 0.5));
        rows.push(this.row("contact_graph", "all pairs", `${cg.checked_pairs ?? 0} checked, ${cg.pruned_by_aabb ?? 0} pruned`, "pairs", "info", `tolerance ${tolerance} mm`, 0));
      }
      if (declared) {
        const pairs = declared.graph?.pairs ?? [];
        for (const c of declared.pairs) {
          const hitPair = pairs.find(
            (x: any) => (x.a === c.feature_id && x.b === c.other_feature_id) || (x.b === c.feature_id && x.a === c.other_feature_id),
          );
          const gap = hitPair ? Number(hitPair.distance_mm) : null;
          rows.push(
            this.row(
              "declared_contact",
              `<= ${c.maximum_gap_mm}`,
              gap === null ? "not measured" : round(gap, 4),
              "mm",
              gap === null || gap > c.maximum_gap_mm ? "fail" : "pass",
              `${c.id} (${c.feature_id} ~ ${c.other_feature_id})`,
              gap === null ? 1 : gap > c.maximum_gap_mm ? Math.min(10, (gap - c.maximum_gap_mm) / Math.max(c.maximum_gap_mm, 0.02)) : 0,
            ),
          );
        }
      }
      // model name vs content, open candidates
      if (on("name")) {
        const name = `${m.name ?? ""} ${m.purpose ?? ""}`.toLowerCase();
        const outputs = r.ir.outputs?.length ?? 0;
        const parts = (r.ir.structure?.parts ?? []).filter((x: any) => (x.outputs ?? []).length).length;
        rows.push(this.row("model_content", ">= 1 output", outputs, "outputs", outputs ? "pass" : "fail", String(m.name ?? a.model_id).slice(0, 80), outputs ? 0 : 1));
        for (const term of gspec.name_terms ?? [])
          rows.push(
            this.row("model_name", `contains "${term}"`, name.includes(String(term).toLowerCase()) ? "yes" : "no", "", name.includes(String(term).toLowerCase()) ? "pass" : "fail", String(m.name ?? "").slice(0, 80), 1),
          );
        if (/(complete|full system|assembly|system)/.test(name) && parts <= 3)
          rows.push(this.row("model_name", "content matches a completeness claim", `${parts} parts`, "", "warn", String(m.name ?? "").slice(0, 80), 0.5));
      }
      if (on("candidates")) {
        const open = store.all(
          "SELECT id,state FROM transactions WHERE model=? AND base=? AND state IN ('planned','executing','candidate_ready')",
          a.model_id,
          m.head,
        );
        rows.push(
          this.row("open_candidates", "0", open.length, "candidates", open.length ? "warn" : "pass", open.map((t: any) => t.id).slice(0, 3).join(", ") || "head", open.length ? 0.2 : 0),
        );
      }
      const result = {
        status: pending.length ? "running" : "ok",
        model_id: a.model_id,
        revision: r.id,
        contract_hash: contract?.hash ?? null,
        rows,
        pending,
        graph,
        views: out.views ?? [],
        seconds: round((Date.now() - started) / 1000, 2),
      };
      if (!pending.length) this.remember(key, a.model_id, "cad_gates", result);
      return checkedToolResponse("cad_gates" as any, this.gatesPayload(result, a, false), trace);
    } catch (error) {
      return failureResponse(error, trace);
    }
  }
  private row(check: string, target: any, measured: any, unit: string, severity: string, where: string, excess: number) {
    return { check, target, measured, unit, severity, where: String(where).slice(0, 160), excess: round(Number(excess) || 0, 4) };
  }
  private gatesPayload(result: any, a: any, cached: boolean) {
    const rows: any[] = [...result.rows].sort(
      (x, y) => SEVERITY_ORDER[x.severity] - SEVERITY_ORDER[y.severity] || (y.excess ?? 0) - (x.excess ?? 0),
    );
    const summary = { fail: 0, warn: 0, pass: 0, info: 0 } as Record<string, number>;
    for (const r of rows) summary[r.severity] = (summary[r.severity] ?? 0) + 1;
    const views: any[] = result.views ?? [];
    const imageRows = rows.filter((r) => views.some((v) => String(r.where).startsWith(v.reference_id)));
    let shown: any[];
    if (a.detail === "full") shown = rows.slice(0, 200);
    else {
      shown = [];
      let bytes = 0;
      for (const r of rows) {
        if (r.severity === "pass" || r.severity === "info") break;
        const size = JSON.stringify(r).length + 1;
        if (bytes + size > BRIEF_BYTES) break;
        shown.push(r);
        bytes += size;
      }
    }
    const pending = result.status === "running";
    return {
      status: result.status,
      model_id: result.model_id,
      revision: result.revision,
      contract_hash: result.contract_hash,
      floor_passed: pending ? null : summary.fail === 0,
      summary: summary as any,
      coverage: {
        native_checks: rows.length - imageRows.length,
        image_views: views.length,
        registered_views: views.filter((v) => v.registered).length,
        advisory_views: views.filter((v) => !v.registered).length,
        contact_graph: result.graph,
      },
      residuals: shown,
      truncated: rows.length - shown.length,
      ...(pending ? { pending: result.pending } : {}),
      ...(a.detail === "full" ? { views } : {}),
      cached,
      seconds: result.seconds,
      note: pending
        ? "Still measuring: call cad_gates again with the same arguments (floor_passed stays null until then)."
        : "floor_passed = no hard gate failed; it is a floor, not a finish: fix the largest residual next. Image checks on unregistered references are advisory (warn). detail full lists every row.",
    };
  }
}
