import type { Express, NextFunction, Request, Response } from "express";
import { readFileSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, join, sep } from "node:path";
import type { ModelService } from "../model-service/index.js";
import type { Principal } from "../policy/index.js";
import { envPathList, pathInside } from "../semantic-ir/host.js";
/* --- Read routes the viewer needs beyond the tools --------------------------
Two small reads, both bound to the principal's access to the model:

GET /api/models/:id/overlays?revision=
  The visual-only parts of a revision's IR: its configurations (named output
  sets the viewer can switch between) and its decals (prints projected onto the
  surface, never in STEP). A decal whose source is a file path gets the SVG text
  inline, read only from the patch allowlist (LLCAD_PATCH_PATHS, default
  /root/projects, plus the data folder's uploads and annotations), only .svg,
  at most 2 MiB; an artifact source is fetched by the viewer itself.

GET /api/models/:id/feature-batches
  The latest feature batches (and lane runs stored alike) of a model with their
  per-task proposal artifacts, so the viewer can offer each proposal as a ghost
  candidate to accept or reject. Nothing here applies anything. */

type Middleware = (req: Request, res: Response, next: NextFunction) => unknown;
export const OVERLAY_SCHEMA = "llcad-viewer-overlays-1";
const MAX_SVG_BYTES = 2 * 1024 * 1024,
  MAX_INLINE_BYTES = 8 * 1024 * 1024,
  MAX_DECALS = 256,
  MAX_CONFIGURATIONS = 64;

export function svgRoots(dataRoot: string) {
  return [
    ...envPathList(process.env.LLCAD_PATCH_PATHS ?? "/root/projects", ","),
    join(dataRoot, "uploads"),
    join(dataRoot, "annotations"),
  ];
}
/** An SVG from the allowlist, or an error saying why not. */
export function readAllowedSvg(path: string, roots: string[]) {
  if (!isAbsolute(path)) throw new Error("Decal path must be absolute.");
  if (!/\.svg$/i.test(path))
    throw new Error("Decal path must name an .svg file.");
  let real: string;
  try {
    real = realpathSync(path);
  } catch {
    throw new Error("Decal file not found.");
  }
  const inside = roots.some((root) => {
    try {
      const base = realpathSync(root);
      return pathInside(real, base, sep);
    } catch {
      return false;
    }
  });
  if (!inside) throw new Error("Decal file lies outside the allowed folders.");
  const stat = statSync(real);
  if (!stat.isFile() || stat.size > MAX_SVG_BYTES)
    throw new Error("Decal file is not a file of at most 2 MiB.");
  const text = readFileSync(real, "utf8");
  if (!/<svg[\s>]/i.test(text)) throw new Error("Decal file is not an SVG.");
  return text;
}
const text = (v: unknown, max = 200) =>
  typeof v === "string" && v.trim() ? v.slice(0, max) : null;
/** Configurations and decals of one IR, in the shape the viewer reads. Old
 *  revisions have neither and get two empty lists. */
export function viewerOverlays(
  ir: any,
  context: {
    model_id: string;
    revision: string;
    readSvg?: (path: string) => string;
  },
) {
  const configurations = (
    Array.isArray(ir?.configurations) ? ir.configurations : []
  )
    .filter(
      (c: any) =>
        c && typeof c === "object" && text(c.id) && Array.isArray(c.outputs),
    )
    .slice(0, MAX_CONFIGURATIONS)
    .map((c: any) => ({
      id: text(c.id),
      name: text(c.name) ?? text(c.id),
      outputs: c.outputs.filter((o: unknown) => typeof o === "string"),
      default: c.default === true,
      ...(text(c.description, 1000)
        ? { description: text(c.description, 1000) }
        : {}),
    }));
  let inline = 0;
  const decals = (Array.isArray(ir?.decals) ? ir.decals : [])
    .filter((d: any) => d && typeof d === "object")
    .slice(0, MAX_DECALS)
    .map((d: any) => {
      // The IR keeps decimal strings (canonical form); the viewer computes with numbers.
      const num = (v: unknown) => (typeof v === "string" ? Number(v) : v);
      const out: Record<string, unknown> = {
        ...d,
        ...(Array.isArray(d.size_mm) ? { size_mm: d.size_mm.map(num) } : {}),
        ...(d.anchor && typeof d.anchor === "object"
          ? {
              anchor: {
                ...d.anchor,
                ...(Array.isArray(d.anchor.point_mm) ? { point_mm: d.anchor.point_mm.map(num) } : {}),
                ...(Array.isArray(d.anchor.normal) ? { normal: d.anchor.normal.map(num) } : {}),
              },
            }
          : {}),
        ...(d.rotation_deg !== undefined ? { rotation_deg: num(d.rotation_deg) } : {}),
      };
      const path = d.source?.path;
      if (!d.source?.artifact_id && typeof path === "string") {
        try {
          if (!context.readSvg)
            throw new Error("Path sources are not served here.");
          const svg = context.readSvg(path);
          inline += svg.length;
          if (inline > MAX_INLINE_BYTES)
            throw new Error("Too much inline SVG in one revision.");
          out.svg = svg;
        } catch (error) {
          out.source_error = (error as Error).message;
        }
      }
      return out;
    });
  return {
    schema: OVERLAY_SCHEMA,
    model_id: context.model_id,
    revision: context.revision,
    configurations,
    decals,
  };
}
/** One feature batch (or lane run) as the viewer lists it. */
export function summariseBatch(r: any) {
  return {
    batch_id: r.id,
    kind: r.kind ?? "feature_batch",
    model_id: r.model,
    base_revision: r.revision,
    status: r.status,
    stage: r.stage ?? null,
    started_at: r.started_at ?? null,
    finished_at: r.finished_at ?? null,
    proposal_artifact_id: r.proposal_artifact_id ?? null,
    error: r.error ?? null,
    tasks: (Array.isArray(r.tasks) ? r.tasks : []).map((t: any) => ({
      id: t.id,
      title: t.title ?? t.id,
      state: t.state,
      model: t.model ?? null,
      effort: t.effort ?? null,
      summary: t.summary ?? null,
      proposal_artifact_id: t.proposal_artifact_id ?? null,
      elapsed_ms: t.elapsed_ms ?? null,
      error: t.error ?? null,
    })),
  };
}
export function installViewerRoutes(
  app: Express,
  deps: {
    service: ModelService;
    authenticate: Middleware;
    rate: Middleware;
    dataRoot: string;
  },
) {
  app.get(
    "/api/models/:id/overlays",
    deps.authenticate,
    deps.rate,
    (req, res) => {
      const p = res.locals.principal as Principal,
        model = String(req.params.id),
        asked =
          typeof req.query.revision === "string" && req.query.revision
            ? req.query.revision
            : undefined;
      const revision = deps.service.store.revision(p, model, asked);
      const roots = svgRoots(deps.dataRoot);
      res.json(
        viewerOverlays(revision.ir, {
          model_id: model,
          revision: revision.id,
          readSvg: (path) => readAllowedSvg(path, roots),
        }),
      );
    },
  );
  app.get(
    "/api/models/:id/feature-batches",
    deps.authenticate,
    deps.rate,
    (req, res) => {
      const p = res.locals.principal as Principal,
        model = String(req.params.id);
      deps.service.store.model(p, model);
      let rows: any[] = [];
      try {
        rows = deps.service.store.all(
          "SELECT record FROM feature_batches WHERE model=? ORDER BY rowid DESC LIMIT 8",
          model,
        );
      } catch {
        rows = []; // no feature work has ever run on this data folder
      }
      res.json({
        model_id: model,
        batches: rows.map((row) => summariseBatch(JSON.parse(row.record))),
      });
    },
  );
}
