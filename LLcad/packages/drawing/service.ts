import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { ModelService, CallContext } from "../model-service/index.js";
import type { Principal } from "../policy/index.js";
import { hash, id } from "../semantic-ir/hash.js";
import { imagePython } from "../semantic-ir/host.js";
import { CadError, requireThat } from "../semantic-ir/errors.js";
import { checkedToolResponse, failureResponse } from "../model-service/result-contracts.js";
import { readColours } from "../model-service/colours.js";
import { DrawingPlanResult, DrawingSpec } from "./schema.js";
import { LIMITS, ownPlacement } from "../compiler/index.js";
import { resolveFont } from "../compiler/fonts.js";
import { compileStructure } from "../compiler/structure.js";

/* --- Technical drawings of a committed revision ---------------------------------
cad_drawing_analyze  parts, holes/shafts, plane gaps, bend tables of wire sweeps,
                     view extents and a format/scale suggestion.
cad_drawing_render   a sheet set (views by hidden-line projection, dimensions computed
                     from 3D points, tables, parts list, title block) -> PDF + PNG per
                     sheet + automatic findings; images as MCP image content. With autofix
                     the kernel repairs layout findings first (autofix.py) and the
                     repaired spec becomes a JSON artifact.
cad_drawing_publish  attaches a checked render (0 blocking findings, every sheet looked
                     at) to its revision; the viewer offers its PDF.
The kernel is packages/drawing/drawing.py in the OCCT venv; geometry comes from the
revision's own B-rep export (cad_export brep), never from a rebuild. */

const execFileAsync = promisify(execFile);
const execute = ((file: string, args: string[], options: any) =>
  execFileAsync(file, args, { windowsHide: true, ...options })) as typeof execFileAsync;
const MCP_IMAGES = Symbol.for("llcad.mcp.images");
const TERMINAL = ["succeeded", "failed", "cancelled"];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const MAX_FINDINGS = 40;
const MAX_ANNOTATIONS = 60;
/** Room for the payload inside LIMITS.response_bytes (32 KB) next to the envelope. */
const PARTS_BUDGET = 22000;
const RENDER_BUDGET = 30000;
const bytes = (v: unknown) => Buffer.byteLength(JSON.stringify(v));

/** One page of analyzed parts, filled by size (not by count): a part with many holes
 *  or a long bend table is several KB. A part too large on its own is slimmed. */
export function packParts(parts: any[], offset: number, limit: number, budget = PARTS_BUDGET) {
  const slim = (x: any, n: number) => ({
    ...x,
    holes: (x.holes ?? []).slice(0, n),
    shafts: (x.shafts ?? []).slice(0, n),
    plane_gaps: (x.plane_gaps ?? []).slice(0, Math.min(8, n)),
    bend_tables: (x.bend_tables ?? []).map((b: any) => ({ ...b, points: (b.points ?? []).slice(0, 8 * n) })),
    holes_total: (x.holes ?? []).length,
    shafts_total: (x.shafts ?? []).length,
  });
  const page: any[] = [];
  let used = 2;
  for (let i = offset; i < parts.length && page.length < limit; i++) {
    let item = slim(parts[i], 12);
    for (let n = 6; bytes(item) > budget - 2 && n >= 1; n = Math.floor(n / 2)) item = slim(parts[i], n);
    const size = bytes(item) + 1;
    if (page.length && used + size > budget) break;
    page.push(item);
    used += size;
  }
  const next = offset + page.length;
  return { parts: page, next_offset: next < parts.length ? next : null };
}

/** A render answer that always fits: annotations go first, then the view frames,
 *  then findings (most important first); render_id and blocking always stay. */
export function boundRender<T extends Record<string, any>>(payload: T, budget = RENDER_BUDGET): T & { truncated: string[] } {
  const out: any = { ...payload, truncated: [] as string[] };
  const mark = (what: string) => {
    if (!out.truncated.includes(what)) out.truncated.push(what);
  };
  while (bytes(out) > budget) {
    if (out.annotations?.length) {
      out.annotations = out.annotations.slice(0, Math.floor(out.annotations.length / 2));
      mark("annotations");
    } else if (out.view_frames && Object.keys(out.view_frames).length) {
      out.view_frames = {};
      mark("view_frames");
    } else if (out.findings?.length > 1) {
      out.findings = out.findings.slice(0, Math.ceil(out.findings.length / 2));
      mark("findings");
    } else break;
  }
  return out;
}

/** Why a revision cannot be drawn at all (no outputs, outputs without native B-rep),
 *  or null: checked before a paid run is started. */
export function drawingBlocker(ir: any): string | null {
  const outputs: string[] = ir?.outputs ?? [];
  if (!outputs.length) return "The revision has no output: there is nothing to draw.";
  const byId = new Map<string, any>((ir?.features ?? []).map((f: any) => [f.id, f]));
  const foreign = outputs
    .map((o) => [o, byId.get(o)?.authoritative_representation ?? "brep"] as const)
    .filter(([, rep]) => rep !== "brep");
  return foreign.length
    ? `Drawings need native B-Rep solids; these outputs are not: ${foreign
        .slice(0, 6)
        .map(([o, rep]) => `${o} (${rep})`)
        .join(", ")}.`
    : null;
}

export interface IrHint {
  part: string;
  operator: "thread" | "knurl" | "text" | "svg_emblem";
  /** relief lettering only, optional: world boxes [x0, y0, z0, x1, y1, z1] of the lettering */
  region?: number[][];
  /** relief lettering only, optional: cut into (deboss) or raised from (emboss) the part */
  relief?: "deboss" | "emboss";
  /** text cut into or fused onto its target, optional: world construction frame of the lettering */
  field?: Frame;
  [key: string]: string | number | number[][] | Frame | undefined;
}
const HINT_OPERATORS = new Set(["thread", "knurl", "text", "svg_emblem"]);
const LETTERING = new Set(["text", "svg_emblem"]);
/** Operators that only move or copy their first input (the lettering keeps its shape). */
const MOVES = new Set(["transform", "affine_transform", "rotate", "mirror", "instance", "pattern", "circular_pattern"]);
const TO_MM: Record<string, number> = { m: 1000, mm: 1, um: 0.001 };

const box6 = (b: any) =>
  Array.isArray(b) && b.length === 6 && b.every((x) => Number.isFinite(Number(x))) ? b.map((x) => Math.round(Number(x) * 1e6) / 1e6) : null;

/** Where a lettering TOOL (text/svg_emblem without input) ends up and how it is used: following
 *  the features that only move or copy it (MOVES) to the first feature that uses it otherwise;
 *  region = the world bounds (revision geometry facts) of the tool or its last moved copies,
 *  relief = deboss if every use subtracts it (difference, not as the first input), emboss if
 *  every use fuses it (union). Missing facts give no region, mixed or other uses no relief. */
function toolUse(features: any[], facts: any, root: string) {
  const users = (fid: string) => features.filter((g) => (g?.depends_on ?? []).includes(fid));
  const region: number[][] = [];
  const uses = new Set<string>();
  let located = !!facts;
  const stack = [root];
  const seen = new Set<string>();
  while (stack.length) {
    const fid = stack.pop()!;
    if (seen.has(fid)) continue;
    seen.add(fid);
    let terminal = false;
    for (const g of users(fid)) {
      const op = g.construction?.operator;
      if (MOVES.has(op) && g.depends_on[0] === fid) stack.push(g.id);
      else {
        terminal = true;
        uses.add(op === "difference" && g.depends_on[0] !== fid ? "deboss" : op === "union" ? "emboss" : "other");
      }
    }
    if (terminal) {
      const b = box6(facts?.[fid]?.bounds);
      if (b) region.push(b);
      else located = false;
    }
  }
  const [use] = uses;
  return { region: located && region.length ? region : undefined, relief: uses.size === 1 && use !== "other" ? (use as "deboss" | "emboss") : undefined };
}

type Frame = { origin: number[]; x_axis: number[]; y_axis: number[]; z_axis: number[] };
/** World construction frame of a `text` feature cut into or fused onto its target (reads along
 *  x_axis, capitals along y_axis, relief along z_axis): its placement (ownPlacement) through its
 *  local frame (compileStructure). The kernel lays the glyphs out in it with the font (advances,
 *  outlines; features.py _text_boxes). None for text on an arc or a cylinder, for an unknown
 *  frame, and when a later feature moves or copies the result (the text would lie elsewhere). */
function textFrame(ir: any, f: any): Frame | undefined {
  const c = f.construction;
  if (c.operator !== "text" || c.arc || typeof c.string !== "string") return undefined;
  const features: any[] = ir?.features ?? [];
  const stack = [f.id],
    seen = new Set<string>();
  while (stack.length) {
    const fid = stack.pop()!;
    if (seen.has(fid)) continue;
    seen.add(fid);
    for (const g of features.filter((x) => (x?.depends_on ?? []).includes(fid))) {
      if (MOVES.has(g.construction?.operator)) return undefined;
      stack.push(g.id);
    }
  }
  let own: { rotation: number[][]; translation: number[] } | null, frame: { rotation: number[][]; translation: number[] } | undefined;
  try {
    own = ownPlacement(f);
    frame = f.local_frame && f.local_frame !== "world" ? compileStructure(ir).placements[f.local_frame] : undefined;
  } catch {
    return undefined;
  }
  if (f.local_frame && f.local_frame !== "world" && !frame) return undefined;
  const R = own?.rotation ?? [
      [1, 0, 0],
      [0, 1, 0],
      [0, 0, 1],
    ],
    t = own?.translation ?? [0, 0, 0];
  const turn = (v: number[]) => (frame ? frame.rotation.map((row) => row[0] * v[0] + row[1] * v[1] + row[2] * v[2]) : v);
  const round = (v: number[]) => v.map((x) => Math.round(x * 1e9) / 1e9 + 0);
  const axis = (k: number) => round(turn([R[0][k], R[1][k], R[2][k]]));
  const origin = round(frame ? turn(t).map((x, i) => x + frame!.translation[i]) : t);
  const out: Frame = { origin, x_axis: axis(0), y_axis: axis(1), z_axis: axis(2) };
  return Object.values(out).every((v) => v.length === 3 && v.every(Number.isFinite)) ? out : undefined;
}

/** Host file and sha256 of the font a `text` feature uses (resolved like the compiler does), if installed. */
function fontFile(c: any): { host: string; sha256: string } | undefined {
  try {
    const f = resolveFont(c.font ?? "DejaVu Sans", c.style ?? "regular");
    return { host: f.host, sha256: f.sha256 };
  } catch {
    return undefined;
  }
}

/** Thread, knurl and relief lettering (text, svg_emblem) features of the IR as hints for
 *  the planner and every render: owner part, numeric parameters (lengths in mm) and
 *  the construction options the kernel reads. The kernel assigns the modelled faces of a
 *  hint to it (features.py), so engraved glyphs, thread flanks and knurl grooves are no
 *  features of their own and no freeform. Relief lettering also gets, where the IR fixes it,
 *  `relief` (deboss/emboss) and either `region` (world boxes of a lettering tool from the
 *  revision's geometry facts) or, for text cut into its target, `field` + `align`/`valign` +
 *  `font_file` with its `font_sha256` (the kernel lays the glyphs out exactly): the kernel then only assigns
 *  glyph-sized pockets and bosses inside them, and without either only glyph-sized ones on a
 *  single face. */
export function irHints(ir: any, facts?: any): IrHint[] {
  const hints: IrHint[] = [];
  const features: any[] = ir?.features ?? [];
  for (const f of features) {
    const c = f?.construction;
    if (!c || !HINT_OPERATORS.has(c.operator) || !f.owner_part) continue;
    const hint: IrHint = { part: String(f.owner_part), operator: c.operator };
    for (const k of ["designation", "mode", "profile"]) if (typeof c[k] === "string") hint[k] = c[k];
    const text = c.text ?? c.string;
    if (typeof text === "string") hint.text = text;
    for (const [k, q] of Object.entries<any>(f.parameters ?? {})) {
      const n = Number(q?.value) * (TO_MM[q?.unit] ?? 1);
      if (Number.isFinite(n) && !(k in hint)) hint[k] = Math.round(n * 1e9) / 1e9;
    }
    if (LETTERING.has(c.operator)) {
      if ((f.depends_on ?? []).length) {
        // cut into / fused onto its target: direction from the sign of depth, glyphs laid out by the kernel
        const frame = textFrame(ir, f);
        if (frame) {
          hint.field = frame;
          for (const k of ["align", "valign"]) if (typeof c[k] === "string") hint[k] = c[k];
          const file = fontFile(c);
          if (file) {
            hint.font_file = file.host;
            // in the hint (and so in the feature cache key): an exchanged font file is a cache miss
            hint.font_sha256 = file.sha256;
          }
        }
        const depth = Number(hint.depth);
        if (depth) hint.relief = depth < 0 ? "deboss" : "emboss";
      } else {
        const placed = toolUse(features, facts, f.id);
        if (placed.region) hint.region = placed.region;
        if (placed.relief) hint.relief = placed.relief;
      }
    }
    hints.push(hint);
  }
  return hints;
}

export type PartTexts = { semantic_name: string; description?: string; feature_names: string[] };

/** Every model text of a part for the planner, verbatim and bounded: the full
 *  semantic name (the display name keeps only one " · " segment), the part description and
 *  the names of the features it owns (names that only repeat the feature id carry no text).
 *  The planner reads materials, names, forms, roles and tolerances out of them without a model. */
export function partTexts(ir: any): Record<string, PartTexts> {
  const out: Record<string, PartTexts> = {};
  const features: any[] = ir?.features ?? [];
  for (const pt of ir?.structure?.parts ?? []) {
    const id = String(pt.id);
    const names: string[] = [];
    for (const f of features) {
      const name = String(f?.semantic_name ?? "").trim();
      if (f?.owner_part !== pt.id || !name || name === String(f.id) || names.includes(name)) continue;
      if (names.length < 64) names.push(name.slice(0, 200));
    }
    const description = String(pt.purpose?.value ?? "").trim().slice(0, 300);
    out[id] = { semantic_name: String(pt.semantic_name ?? id).slice(0, 300), ...(description ? { description } : {}), feature_names: names };
  }
  return out;
}

export const CONVENTIONS =
  "Coordinates in model mm. Presets (camera direction = target -> camera): front +Y (sheet x = -X, y = +Z), back -Y, left +X (x = +Y), right -X, top +Z (x = -X, y = -Y), bottom -Z, iso, iso_back; or direction [x,y,z] + up. " +
  "Projection method 1 (ISO 5456-2) around the front view: top BELOW, left RIGHT of it, right LEFT, bottom ABOVE. Sheet mm, origin bottom left; drawing area inside the frame (left 20, others 10 mm); title block bottom right (190 x 39 mm), change block and parts list stack above it. " +
  "Scales ISO 5455 (50:1 ... 1:100). Dimension values are computed from the given 3D points (projected into the view plane); text only as prefix/suffix/tolerance or explicit override.";

export class DrawingTools {
  constructor(private service: ModelService) {
    service.store.run(
      "CREATE TABLE IF NOT EXISTS drawing_renders(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,model TEXT NOT NULL,revision TEXT NOT NULL,created TEXT NOT NULL,sheets INTEGER NOT NULL,blocking INTEGER NOT NULL,pdf_artifact TEXT NOT NULL,spec_hash TEXT NOT NULL)",
    );
    service.store.run(
      "CREATE TABLE IF NOT EXISTS drawings(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,model TEXT NOT NULL,revision TEXT NOT NULL,render_id TEXT NOT NULL,pdf_artifact TEXT NOT NULL,sheets INTEGER NOT NULL,created TEXT NOT NULL,owner TEXT NOT NULL,statement TEXT NOT NULL)",
    );
  }
  private get root() {
    return resolve(process.env.MATHFORGE_ROOT ?? process.cwd());
  }
  private work(kind: string) {
    const folder = join(this.service.store.root, "drawing-work", kind, id("w"));
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    return folder;
  }
  /** Company lines and title defaults: <store>/drawing-config.json (not in the repo). */
  config(): { company: string[]; title_defaults: Record<string, string>; semi_finished: { material: string; form: string; text: string }[]; part_roles: Record<string, string[]> } {
    const file = join(this.service.store.root, "drawing-config.json");
    try {
      const c = JSON.parse(readFileSync(file, "utf8"));
      const company = Array.isArray(c.company) ? c.company.slice(0, 3).map((s: unknown) => String(s).slice(0, 80)) : [];
      const title_defaults: Record<string, string> = {};
      for (const [k, v] of Object.entries(c.title_defaults ?? {})) if (typeof v === "string") title_defaults[k.slice(0, 40)] = v.slice(0, 200);
      const semi_finished = (Array.isArray(c.semi_finished) ? c.semi_finished : [])
        .filter((e: any) => e && typeof e === "object")
        .slice(0, 200)
        .map((e: any) => ({ material: String(e.material ?? "").slice(0, 80), form: String(e.form ?? "").slice(0, 40), text: String(e.text ?? "").slice(0, 200) }));
      // Keyword list for purchased/standard parts: simple words/phrases, limited (per role 100 entries of 80 characters)
      const part_roles: Record<string, string[]> = {};
      for (const role of ["purchased", "standard"]) {
        const list = (c.part_roles ?? {})[role];
        if (Array.isArray(list)) part_roles[role] = list.filter((w: unknown) => typeof w === "string" && w.trim()).slice(0, 100).map((w: string) => w.trim().slice(0, 80));
      }
      return { company, title_defaults, semi_finished, part_roles };
    } catch {
      return { company: [], title_defaults: {}, semi_finished: [], part_roles: {} };
    }
  }
  private async python(action: string, spec: any, folder: string, timeout: number, context: CallContext) {
    const file = join(folder, `${action}-spec.json`);
    writeFileSync(file, JSON.stringify(spec), { mode: 0o600 });
    const started = Date.now();
    const timer = context.progress
      ? setInterval(() => {
          try {
            context.progress!({ elapsed_s: Math.round((Date.now() - started) / 1000), message: `drawing ${action} running` });
          } catch {
            /* a lost progress notification never fails the call */
          }
        }, 5000)
      : null;
    let out: any;
    try {
      const { stdout } = await execute(imagePython(this.root), [join(this.root, "packages/drawing/drawing.py"), action, file], {
        timeout,
        maxBuffer: 32 * 1024 * 1024,
        signal: context.signal,
        env: { ...process.env, OMP_NUM_THREADS: "2", PYTHONWARNINGS: "ignore", MPLBACKEND: "Agg" },
      });
      out = JSON.parse(stdout);
    } catch (error: any) {
      const stdout = String(error?.stdout ?? "").trim();
      if (stdout.startsWith("{")) {
        try {
          out = JSON.parse(stdout);
        } catch {
          /* fall through */
        }
      }
      if (!out) {
        const tail = String(error?.stderr ?? error?.message ?? "").trim().split("\n").slice(-2).join(" ").slice(0, 300);
        throw new CadError(error?.killed || error?.signal === "SIGTERM" ? "BUDGET_EXCEEDED" : "KERNEL_FAILURE", `drawing.py ${action}: ${tail || "failed"}`);
      }
    } finally {
      if (timer) clearInterval(timer);
    }
    if (out?.error) throw new CadError("KERNEL_FAILURE", `drawing.py ${action}: ${String(out.error).slice(0, 400)}`);
    return out;
  }
  private async waitJob(p: Principal, jobId: string, deadline: number, context: CallContext) {
    let job = this.service.jobs.get(p, jobId);
    while (!TERMINAL.includes(job.status) && Date.now() < deadline && !context.signal?.aborted) {
      await sleep(150);
      job = this.service.jobs.get(p, jobId);
    }
    return job;
  }
  /** The revision's own B-rep (cad_export brep, deduplicated by its idempotency key),
   *  as a content-addressed file. Revisions without native B-rep fail clearly. */
  private async brep(p: Principal, model: string, revision: string, context: CallContext) {
    const stable = `drawing-brep-${hash([model, revision]).slice(0, 40)}`;
    const request = (key: string) =>
      this.service.call(p, "cad_export", { model_id: model, revision, format: "brep", reference_policy: "draft", idempotency_key: key });
    // A deterministic refusal stays; a failed job (restart, timeout) is not replayed
    // forever under the stable key: one more attempt with a fresh key.
    const lasting = ["OUT_OF_SCOPE", "BUILD_MISMATCH", "ACCESS_DENIED", "INVALID_SCHEMA", "STALE_REVISION"];
    let res: any = await request(stable);
    if (res?.status === "failed" && !lasting.includes(res.errors?.[0]?.code)) res = await request(`${stable}-r${Date.now().toString(36)}`);
    else if (res?.job_id && !(res.artifacts ?? []).some((a: any) => a.manifest?.filename === "model.brep")) {
      const job = await this.waitJob(p, res.job_id, Date.now() + 240000, context);
      if (job.status === "failed" || job.status === "cancelled") res = await request(`${stable}-r${Date.now().toString(36)}`);
    }
    const fail = (e: any) => {
      const code = String(e?.code ?? "KERNEL_FAILURE");
      if (code === "BUILD_MISMATCH")
        throw new CadError(
          code,
          "The revision needs its original geometry build. First run cad_rebuild (plan, candidate, validate, commit), then draw the new revision.",
          e?.details ?? {},
        );
      if (code === "OUT_OF_SCOPE")
        throw new CadError(code, `Drawings need native B-Rep geometry (solids); this revision has none: ${String(e?.message ?? "").slice(0, 200)}`, e?.details ?? {});
      throw new CadError(code, `B-Rep export for the drawing: ${String(e?.message ?? "failed").slice(0, 300)}`, e?.details ?? {});
    };
    if (res?.status === "failed") fail(res.errors?.[0]);
    let artifacts: any[] = res?.artifacts ?? [];
    if (res?.job_id && !artifacts.some((a) => a.manifest?.filename === "model.brep")) {
      const job = await this.waitJob(p, res.job_id, Date.now() + 240000, context);
      if (job.status !== "succeeded") fail(job.error ?? { code: job.status === "failed" ? "KERNEL_FAILURE" : "BUDGET_EXCEEDED", message: `Exportjob ${job.status}` });
      artifacts = job.result?.artifacts ?? [];
    }
    const chosen = artifacts.find((a) => a.manifest?.filename === "model.brep") ?? artifacts.find((a) => /brep/i.test(String(a.mime)));
    requireThat(chosen, "KERNEL_FAILURE", "The B-Rep export produced no model.brep.");
    const art = this.service.store.getArtifact(p, chosen.artifact_id);
    const folder = join(this.service.store.root, "drawing-work", "breps");
    mkdirSync(folder, { recursive: true, mode: 0o700 });
    const file = join(folder, `${art.hash}.brep`);
    if (!existsSync(file)) writeFileSync(file, this.service.store.readBlob(art.hash), { mode: 0o600 });
    return file;
  }
  /** Parts with the export positions of their outputs (export order = ir.outputs). */
  private parts(p: Principal, model: string, r: any) {
    const outputs: string[] = r.ir.outputs ?? [];
    const colours: any = readColours(this.service.store, p, model, r.id);
    const declared: any[] = r.ir.structure?.parts ?? [];
    const rows = declared
      .map((pt: any) => ({
        id: String(pt.id),
        name: String(colours.parts?.[pt.id]?.name ?? pt.semantic_name ?? pt.id).slice(0, 120),
        solid_indices: (pt.outputs ?? []).map((o: string) => outputs.indexOf(o)).filter((i: number) => i >= 0),
      }))
      .filter((pt) => pt.solid_indices.length);
    const parts = rows.length
      ? rows
      : outputs.map((fid, i) => ({ id: fid, name: fid, solid_indices: [i] }));
    return { parts, single_root: outputs.length === 1 };
  }
  private revisionOf(p: Principal, a: any) {
    const m = this.service.store.model(p, a.model_id);
    return { m, r: this.service.store.revision(p, a.model_id, a.revision ?? m.head) };
  }

  // --- cad_drawing_analyze ------------------------------------------------------------
  async analyze(p: Principal, a: any, trace: string, context: CallContext = {}) {
    let folder: string | null = null;
    try {
      const { r } = this.revisionOf(p, a);
      const file = await this.brep(p, a.model_id, r.id, context);
      const { parts, single_root } = this.parts(p, a.model_id, r);
      const wanted = a.parts ? new Set<string>(a.parts) : null;
      const chosen = wanted ? parts.filter((x) => wanted.has(x.id)) : parts;
      requireThat(chosen.length, "INVALID_SCHEMA", "None of the requested parts has outputs in this revision.");
      folder = this.work("analyze");
      const out = await this.python("analyze", { brep: file, parts: chosen, single_root }, folder, 180000, context);
      const cfg = this.config();
      const total = out.parts.length;
      const payload = {
        model_id: a.model_id,
        revision: r.id,
        ...packParts(out.parts, a.offset, a.limit),
        parts_total: total,
        extents: out.extents ?? {},
        suggestion: out.suggestion ?? {},
        title_defaults: cfg.title_defaults,
        company: cfg.company,
        conventions: CONVENTIONS,
      };
      return checkedToolResponse("cad_drawing_analyze" as any, payload, trace);
    } catch (error) {
      return failureResponse(error, trace);
    } finally {
      if (folder) rmSync(folder, { recursive: true, force: true });
    }
  }

  // --- cad_drawing_plan ---------------------------------------------------------------
  async plan(p: Principal, a: any, trace: string, context: CallContext = {}) {
    let folder: string | null = null;
    try {
      const store = this.service.store;
      const { m, r } = this.revisionOf(p, a);
      const file = await this.brep(p, a.model_id, r.id, context);
      const { parts, single_root } = this.parts(p, a.model_id, r);
      // the planner also gets every model text of each part (new optional fields, display name unchanged)
      const texts = partTexts(r.ir);
      const cfg = this.config();
      folder = this.work("plan");
      const started = Date.now();
      const out = await this.python(
        "plan",
        {
          brep: file,
          single_root,
          parts: parts.map((x) => ({ ...x, ...(texts[x.id] ?? {}) })),
          model: { id: a.model_id, name: String(m.name ?? "").slice(0, 120), purpose: String(m.purpose ?? "").slice(0, 300), revision: r.id },
          facts: a.facts ?? {},
          ir_hints: irHints(r.ir, r.geometry?.facts),
          semi_finished: cfg.semi_finished,
          part_roles: cfg.part_roles,
          title_defaults: cfg.title_defaults,
          company: cfg.company.length ? cfg.company : ["", "", ""],
          max_candidates: a.max_candidates,
          cache_dir: join(store.root, "drawing-work", "hlr-cache"),
          feature_cache_dir: join(store.root, "drawing-work", "feature-cache"),
        },
        folder,
        600000,
        context,
      );
      const candidates = (out.candidates ?? []).map((c: any) => {
        const parsed = DrawingSpec.safeParse(c.spec);
        requireThat(
          parsed.success,
          "KERNEL_FAILURE",
          `Planner returned an invalid spec (${c.candidate_id}): ${parsed.success ? "" : parsed.error.issues.slice(0, 3).map((i) => `${i.path.join(".")}: ${i.message}`).join("; ")}`,
        );
        const cid = String(c.candidate_id);
        const art = store.artifact(p, JSON.stringify(parsed.data), "application/json", a.model_id, r.id, {
          kind: "drawing_spec",
          filename: `plan-${cid}.json`,
          candidate_id: cid,
        });
        return { candidate_id: cid, spec_artifact_id: art.artifact_id, metrics: c.metrics ?? {} };
      });
      const payload = {
        model_id: a.model_id,
        revision: r.id,
        candidates,
        parts: out.parts ?? [],
        notes: (out.notes ?? []).map(String),
        open: (out.open ?? []).map(String),
        seconds: Math.round((Date.now() - started) / 10) / 100,
      };
      DrawingPlanResult.parse(payload);
      return checkedToolResponse("cad_drawing_plan" as any, payload, trace);
    } catch (error) {
      return failureResponse(error, trace);
    } finally {
      if (folder) rmSync(folder, { recursive: true, force: true });
    }
  }

  // --- cad_drawing_render -------------------------------------------------------------
  async render(p: Principal, a: any, trace: string, context: CallContext = {}) {
    let folder: string | null = null;
    try {
      const store = this.service.store;
      const { m, r } = this.revisionOf(p, a);
      let spec: any = a.spec;
      if (!spec) {
        let text: string;
        if (a.spec_ref.artifact_id) {
          const bytes = store.readBlob(store.getArtifact(p, a.spec_ref.artifact_id).hash);
          requireThat(bytes.length <= LIMITS.request_bytes, "BUDGET_EXCEEDED", "spec_ref is larger than one request.", { limit_bytes: LIMITS.request_bytes });
          text = bytes.toString("utf8");
        } else text = readFileSync((this.service as any).patchPath(a.spec_ref.path), "utf8");
        let raw: unknown;
        try {
          raw = JSON.parse(text);
        } catch {
          throw new CadError("INVALID_SCHEMA", "spec_ref contains no valid JSON.");
        }
        const parsed = DrawingSpec.safeParse(raw);
        requireThat(parsed.success, "INVALID_SCHEMA", `spec_ref: ${parsed.success ? "" : parsed.error.issues.slice(0, 3).map((i) => `${i.path.join(".")}: ${i.message}`).join("; ")}`);
        spec = parsed.data;
      }
      const file = await this.brep(p, a.model_id, r.id, context);
      const { parts, single_root } = this.parts(p, a.model_id, r);
      const given = new Map<string, any>((spec.parts ?? []).map((x: any) => [x.id, x]));
      const cfg = this.config();
      const today = new Date();
      const date = `${String(today.getDate()).padStart(2, "0")}.${String(today.getMonth() + 1).padStart(2, "0")}.${today.getFullYear()}`;
      const render_id = id("drw");
      folder = this.work("render");
      const started = Date.now();
      const autofix = a.autofix === true;
      const out = await this.python(
        autofix ? "autofix" : "render",
        {
          ...spec,
          brep: file,
          single_root,
          parts: parts.map((x) => {
            const g = given.get(x.id);
            return { ...x, name: g?.name ?? x.name, ...(g?.role ? { role: g.role } : {}), ...(g?.quantity ? { quantity: g.quantity } : {}) };
          }),
          strict: a.strict === true,
          autofix,
          // internal kernel input, never a DrawingSpec field (the schema rejects it)
          ir_hints: irHints(r.ir, r.geometry?.facts),
          out_dir: folder,
          company: spec.company ?? (cfg.company.length ? cfg.company : ["", "", ""]),
          title: { created_date: date, ...cfg.title_defaults, ...(spec.title ?? {}) },
          crops: a.crops ?? [],
          cache_dir: join(store.root, "drawing-work", "hlr-cache"),
          feature_cache_dir: join(store.root, "drawing-work", "feature-cache"),
        },
        folder,
        autofix ? 900000 : 300000,
        context,
      );
      // autofix: the kernel moved only placement fields of the sheets; the repaired spec is
      // the given one with those sheets, stored like a plan candidate.
      let repaired: any = null;
      if (autofix) {
        const fix = out.autofix ?? {};
        const parsed = DrawingSpec.safeParse({ ...spec, sheets: fix.spec?.sheets });
        requireThat(
          parsed.success,
          "KERNEL_FAILURE",
          `Autofix returned an invalid spec: ${parsed.success ? "" : parsed.error.issues.slice(0, 3).map((i) => `${i.path.join(".")}: ${i.message}`).join("; ")}`,
        );
        spec = parsed.data;
        const changes: any[] = Array.isArray(fix.changes) ? fix.changes : [];
        const errors: any[] = Array.isArray(fix.errors) ? fix.errors : [];
        const art = store.artifact(p, JSON.stringify(spec), "application/json", a.model_id, r.id, {
          kind: "drawing_spec",
          filename: `autofix-${render_id}.json`,
          render_id,
        });
        repaired = {
          rounds: Number(fix.rounds ?? 0),
          // no_moves | max_rounds | budget (P1); optional for older kernels
          ...(typeof fix.ended_by === "string" ? { ended_by: fix.ended_by } : {}),
          changes_count: changes.length,
          changes: changes.slice(0, 30),
          spec_artifact_id: art.artifact_id,
          // renders that threw inside the loop (their moves count as failed); never silent
          ...(errors.length ? { errors: errors.slice(0, 10).map((e) => ({ round: e.round ?? null, error: String(e.error ?? "").slice(0, 300) })) } : {}),
          // slim probe and full render disagreed: from that round on the kernel rendered fully (P1)
          ...(fix.fallback && typeof fix.fallback === "object"
            ? { fallback: { round: Number(fix.fallback.round ?? 0), slim: fix.fallback.slim ?? null, full: fix.fallback.full ?? null } }
            : {}),
          // full render of the best probed state ranked below an earlier full render, which is delivered instead (P1)
          ...(fix.dropped && typeof fix.dropped === "object"
            ? { dropped: { blocking: Number(fix.dropped.blocking ?? 0), layout: Number(fix.dropped.layout ?? 0), changes: Number(fix.dropped.changes ?? 0) } }
            : {}),
        };
      }
      const base = { kind: "drawing", render_id, model_name: String(m.name ?? "").slice(0, 120) };
      const pdf = store.artifact(p, readFileSync(out.pdf), "application/pdf", a.model_id, r.id, {
        ...base,
        filename: `drawing-${r.id.slice(0, 16)}.pdf`,
        sheets: out.sheets.length,
      });
      const sheets = out.sheets.map((s: any) => ({
        index: s.index,
        artifact_id: store.artifact(p, readFileSync(s.png), "image/png", a.model_id, r.id, { ...base, filename: `sheet-${s.index + 1}.png`, sheet: s.index }).artifact_id,
        format: String(s.format),
        orientation: String(s.orientation),
        width_px: s.width_px,
        height_px: s.height_px,
        size_mm: [s.size_mm[0], s.size_mm[1]] as [number, number],
      }));
      const crops = (out.crops ?? []).map((c: any) => ({
        sheet: c.sheet,
        box_mm: c.box_mm.map((v: number) => Math.round(v * 100) / 100) as [number, number, number, number],
        artifact_id: store.artifact(p, readFileSync(c.png), "image/png", a.model_id, r.id, { ...base, filename: `crop-${c.sheet + 1}.png`, sheet: c.sheet, box_mm: c.box_mm }).artifact_id,
      }));
      const findings: any[] = out.findings ?? [];
      const blocking = findings.filter((f) => f.severity === "block").length;
      store.run(
        "INSERT INTO drawing_renders VALUES(?,?,?,?,?,?,?,?,?)",
        render_id,
        p.tenant,
        a.model_id,
        r.id,
        new Date().toISOString(),
        sheets.length,
        blocking,
        pdf.artifact_id,
        hash(spec),
      );
      const ordered = [...findings.filter((f) => f.severity === "block"), ...findings.filter((f) => f.severity !== "block")];
      const images: string[] = [];
      if (a.images === "all") images.push(...sheets.map((s: any) => s.artifact_id));
      if (a.images !== "none") images.push(...crops.map((c: any) => c.artifact_id));
      const payload = {
        model_id: a.model_id,
        revision: r.id,
        render_id,
        pdf_artifact_id: pdf.artifact_id,
        sheets,
        crops,
        blocking,
        warnings_count: findings.length - blocking,
        findings: ordered.slice(0, MAX_FINDINGS).map((f) => ({
          code: String(f.code),
          severity: f.severity === "block" ? ("block" as const) : ("warn" as const),
          sheet: Number(f.sheet ?? 0),
          bbox_mm: Array.isArray(f.bbox_mm) ? f.bbox_mm.map(Number) : null,
          message: String(f.message ?? "").slice(0, 300),
        })),
        findings_total: findings.length,
        annotations: (out.annotations ?? []).slice(0, MAX_ANNOTATIONS).map((x: any) => {
          const { p1_sheet, p2_sheet, rows, ...rest } = x;
          return rest;
        }),
        view_frames: out.view_frames ?? {},
        ...(out.coverage ? { coverage: out.coverage } : {}),
        ...(repaired ? { autofix: repaired } : {}),
        images_returned: images.length,
        seconds: Math.round((Date.now() - started) / 10) / 100,
        note:
          "Look at every sheet image (and crops for details) before publishing: findings catch overlaps, area and completeness, not whether the drawing is sensible. Fix by changing the spec and render again; publish only a render with blocking 0.",
      };
      const response: any = checkedToolResponse("cad_drawing_render" as any, boundRender(payload), trace);
      if (images.length) {
        const blocks = images.map((aid) => {
          const art = store.getArtifact(p, aid);
          return { type: "image", data: store.readBlob(art.hash).toString("base64"), mimeType: "image/png" };
        });
        Object.defineProperty(response, MCP_IMAGES, { value: blocks, enumerable: false });
      }
      return response;
    } catch (error) {
      return failureResponse(error, trace);
    } finally {
      if (folder) rmSync(folder, { recursive: true, force: true });
    }
  }

  // --- cad_drawing_publish ------------------------------------------------------------
  publish(p: Principal, a: any, trace: string) {
    try {
      const store = this.service.store;
      const row = store.get("SELECT * FROM drawing_renders WHERE id=? AND tenant=? AND model=?", a.render_id, p.tenant, a.model_id);
      requireThat(row, "CONSTRAINT_CONFLICT", "No render with this ID for this model; run cad_drawing_render first.");
      requireThat(row.revision === a.revision, "CONSTRAINT_CONFLICT", `The render belongs to revision ${row.revision}, not to ${a.revision}.`, {
        render_revision: row.revision,
      });
      store.revision(p, a.model_id, a.revision);
      requireThat(row.blocking === 0, "CONSTRAINT_CONFLICT", `${row.blocking} blocking findings are open; change the specification and render again.`, {
        blocking: row.blocking,
      });
      requireThat(a.review.sheets_viewed >= row.sheets, "CONSTRAINT_CONFLICT", `View all ${row.sheets} sheets (viewed: ${a.review.sheets_viewed}).`, {
        sheets: row.sheets,
      });
      const drawing_id = id("dwg");
      store.run(
        "INSERT INTO drawings VALUES(?,?,?,?,?,?,?,?,?,?)",
        drawing_id,
        p.tenant,
        a.model_id,
        a.revision,
        a.render_id,
        row.pdf_artifact,
        row.sheets,
        new Date().toISOString(),
        p.user,
        a.review.statement,
      );
      store.audit("drawing_published", { model: a.model_id, revision: a.revision, drawing: drawing_id, render: a.render_id });
      return checkedToolResponse(
        "cad_drawing_publish" as any,
        {
          model_id: a.model_id,
          revision: a.revision,
          drawing_id,
          render_id: a.render_id,
          pdf_artifact_id: row.pdf_artifact,
          sheets: row.sheets,
          published: true as const,
          download: `/api/artifacts/${row.pdf_artifact}`,
        },
        trace,
      );
    } catch (error) {
      return failureResponse(error, trace);
    }
  }

  /** Published drawings of a model, newest first (viewer). */
  list(p: Principal, model: string) {
    this.service.store.model(p, model);
    return this.service.store
      .all("SELECT * FROM drawings WHERE tenant=? AND model=? ORDER BY created DESC", p.tenant, model)
      .map((d: any) => ({
        drawing_id: d.id,
        revision: d.revision,
        created: d.created,
        pdf_artifact_id: d.pdf_artifact,
        sheets: d.sheets,
        render_id: d.render_id,
      }));
  }
}
