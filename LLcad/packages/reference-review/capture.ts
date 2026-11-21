import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { chromium } from "playwright";
import type { ModelService } from "../model-service/index.js";
import type { Principal } from "../policy/index.js";
import { readColours } from "../model-service/colours.js";
import { requireThat } from "../semantic-ir/errors.js";
import { bytesHash } from "../semantic-ir/hash.js";
import type { Camera } from "./schema.js";
import { viewerOverlays, readAllowedSvg, svgRoots } from "../viewer/server-routes.js";

export function rendererHash() {
  const root = resolve(process.env.MATHFORGE_ROOT ?? process.cwd());
  return bytesHash(readFileSync(resolve(root, "public/app.js")));
}
export type CaptureView = {
  id: string;
  label: string;
  camera: Camera;
  width?: number;
  height?: number;
  /** lit (default, Khronos PBR Neutral), albedo (flat declared colours) or ids. */
  channel?: "lit" | "albedo" | "ids";
};
export const DEFAULT_VIEWS: CaptureView[] = [
  {
    id: "front",
    label: "Front",
    camera: {
      direction: [0, 1, 0],
      up: [0, 0, 1],
      projection: "perspective",
      distance_factor: 2.5,
      fov: 28,
    },
  },
  {
    id: "back",
    label: "Back",
    camera: {
      direction: [0, -1, 0],
      up: [0, 0, 1],
      projection: "perspective",
      distance_factor: 2.5,
      fov: 28,
    },
  },
  {
    id: "left",
    label: "Left",
    camera: {
      direction: [1, 0, 0],
      up: [0, 0, 1],
      projection: "perspective",
      distance_factor: 2.5,
      fov: 28,
    },
  },
  {
    id: "right",
    label: "Right",
    camera: {
      direction: [-1, 0, 0],
      up: [0, 0, 1],
      projection: "perspective",
      distance_factor: 2.5,
      fov: 28,
    },
  },
  {
    id: "top",
    label: "Top",
    camera: {
      direction: [0, 0, 1],
      up: [0, 1, 0],
      projection: "perspective",
      distance_factor: 2.5,
      fov: 28,
    },
  },
  {
    id: "bottom",
    label: "Bottom",
    camera: {
      direction: [0, 0, -1],
      up: [0, 1, 0],
      projection: "perspective",
      distance_factor: 2.5,
      fov: 28,
    },
  },
];

/** The shipped viewer bundle and real read tools, with an isolated in-memory
 * transport bound to one principal and one immutable revision. No browser
 * token or listening port is needed; every unrelated network request is denied. */
export async function captureViews(
  service: ModelService,
  p: Principal,
  model: string,
  revision: string,
  preview: any,
  views: CaptureView[],
  signal: AbortSignal,
) {
  const root = resolve(process.env.MATHFORGE_ROOT ?? process.cwd());
  const row = service.store.model(p, model),
    rev = service.store.revision(p, model, revision);
  const colours = readColours(service.store, p, model, revision);
  // Decal sources are displayed by the viewer (visual only); their artifacts may
  // be fetched besides the bound preview, path sources come inline via overlays.
  const decalArtifacts = new Set<string>(
    (rev.ir.decals ?? []).flatMap((d: any) => (d?.source?.artifact_id ? [d.source.artifact_id] : [])),
  );
  const overlays = viewerOverlays(rev.ir, {
    model_id: model,
    revision,
    readSvg: (path) => readAllowedSvg(path, svgRoots(service.store.root)),
  });
  const browser = await chromium.launch({
    executablePath:
      process.env.LLCAD_CHROMIUM_PATH ??
      (existsSync("/usr/bin/chromium") ? "/usr/bin/chromium" : undefined),
    headless: true,
    args: [
      ...(process.getuid?.() === 0 ? ["--no-sandbox"] : []),
      "--disable-dev-shm-usage",
      "--use-angle=swiftshader",
      "--enable-unsafe-swiftshader",
    ],
  });
  const abort = () => {
    void browser.close();
  };
  signal.addEventListener("abort", abort, { once: true });
  try {
    signal.throwIfAborted();
    const context = await browser.newContext({
      viewport: { width: 1200, height: 1000 },
      deviceScaleFactor: 1,
      serviceWorkers: "block",
    });
    const errors: string[] = [];
    await context.route("**/*", async (route) => {
      try {
        const u = new URL(route.request().url());
        if (u.origin !== "http://llcad-review.localhost") return route.abort();
        const path = u.pathname;
        const json = (body: unknown) =>
          route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(body),
          });
        const assets: Record<string, [string, string]> = {
          "/": ["public/index.html", "text/html"],
          "/app.js": ["public/app.js", "text/javascript"],
          "/style.css": ["public/style.css", "text/css"],
          "/favicon.svg": ["public/favicon.svg", "image/svg+xml"],
        };
        if (assets[path])
          return route.fulfill({
            status: 200,
            contentType: assets[path][1],
            body: readFileSync(resolve(root, assets[path][0])),
          });
        if (path === "/api/config") return json({ auth_mode: "local" });
        if (path === "/api/models")
          return json({
            models: [
              {
                id: model,
                name: row.name,
                purpose: row.purpose,
                head: revision,
                created: row.created,
                features: rev.ir.features.length,
                hidden: 0,
                purpose_order: null,
              },
            ],
          });
        if (path === `/api/models/${model}/colours`) return json(colours);
        if (path === `/api/models/${model}/overlays`) return json(overlays);
        if (path === `/api/models/${model}/history`)
          return json({ revisions: [], head: revision });
        if (path === `/api/models/${model}/reference-review`)
          return json(
            service.references.report(p, {
              model_id: model,
              revision,
              offset: 0,
              limit: 3,
            }),
          );
        if (path === "/api/annotations") return json({ annotations: [] });
        if (path === "/api/agent/models")
          return json({ agy: { available: false, source: "fallback", models: [] } });
        if (path === "/api/agent/settings")
          return json({
            model: "opus",
            effort: "high",
            proposal_effort: "medium",
            permission_mode: "dontAsk",
            max_budget_usd: null,
            timeout_minutes: 45,
          });
        if (path.startsWith("/api/artifacts/")) {
          const aid = path.slice("/api/artifacts/".length);
          requireThat(
            preview.artifacts.some((a: any) => a.artifact_id === aid) ||
              decalArtifacts.has(aid),
            "ACCESS_DENIED",
            "Capture artifact outside bound preview.",
          );
          const a = service.store.getArtifact(p, aid);
          return route.fulfill({
            status: 200,
            contentType: a.mime,
            body: service.store.readBlob(a.hash),
          });
        }
        if (path.startsWith("/api/tools/")) {
          const tool = path.slice("/api/tools/".length),
            args = route.request().postDataJSON();
          requireThat(
            args.model_id === model,
            "ACCESS_DENIED",
            "Capture model mismatch.",
          );
          if (tool === "cad_render")
            return json({ status: "succeeded", ...preview });
          requireThat(
            [
              "cad_get_model",
              "cad_structure",
              "cad_inspect",
              "cad_find",
              "cad_measure",
            ].includes(tool),
            "ACCESS_DENIED",
            "Capture transport only permits read tools.",
          );
          if (tool === "cad_measure")
            requireThat(
              !["distance", "clearance", "angle", "curvature"].includes(
                args.metric,
              ),
              "ACCESS_DENIED",
              "No capture side-effect jobs.",
            );
          return json(
            await service.call(p, tool as any, { ...args, revision }),
          );
        }
        return route.fulfill({
          status: 404,
          contentType: "application/json",
          body: '{"error":{"message":"Capture route unavailable"}}',
        });
      } catch {
        errors.push("capture_transport_failed");
        return route.fulfill({ status: 500, body: "Capture request failed" });
      }
    });
    const page = await context.newPage();
    page.on("pageerror", (e) =>
      errors.push("viewer_runtime_error: " + e.message.slice(0, 200)),
    );
    await page.goto("http://llcad-review.localhost/#model=" + model, {
      waitUntil: "domcontentloaded",
      timeout: 45000,
    });
    try {
      await page.waitForFunction(
        () => (window as any).mathforgeViewer?.referenceReady?.(),
        null,
        { timeout: 60000 },
      );
    } catch {
      const state = await page.evaluate(() => ({
        toast: document.getElementById("toast")?.textContent?.slice(0, 300),
        busy: document.getElementById("busy-text")?.textContent?.slice(0, 200),
      }));
      requireThat(
        false,
        "VIEWER_CAPTURE_FAILED",
        "The viewer did not become ready to capture.",
        { ...state, errors: errors.slice(0, 3) },
      );
    }
    const captures = [];
    for (const view of views) {
      signal.throwIfAborted();
      service.store.model(p, model);
      // Absolute cameras carry their own frame; width/height (when given) only
      // set the pixel size the frame is mapped onto.
      const width = view.width ?? (view.camera as any).width_px ?? 1000,
        height = view.height ?? (view.camera as any).height_px ?? 1000;
      const result = await page.evaluate(
        async (v) => (window as any).mathforgeViewer.captureReference(v),
        {
          ...view.camera,
          width,
          height,
          channel: view.channel ?? "lit",
        },
      );
      const visible = [...result.visible_features].sort(),
        outputs = [...rev.ir.outputs].sort();
      // A default configuration may show a subset of the outputs; anything
      // outside the revision's outputs is never accepted.
      const complete =
        JSON.stringify(visible) === JSON.stringify(outputs) ||
        (!!(rev.ir.configurations ?? []).length &&
          visible.length > 0 &&
          visible.every((f: string) => outputs.includes(f)));
      requireThat(
        result.revision === revision && complete,
        "INTEGRITY_FAILURE",
        "The viewer capture does not belong entirely to the requested revision.",
      );
      const bytes = Buffer.from(result.png, "base64");
      captures.push({
        id: view.id,
        label: view.label,
        camera: view.camera,
        channel: view.channel ?? "lit",
        bytes,
        width,
        height,
        visible_features: result.visible_features,
        ids: (result.ids ?? []) as { index: number; feature_id: string; rgb: number[] }[],
        resolved_camera: result.camera ?? null,
      });
    }
    requireThat(
      errors.length === 0,
      "VIEWER_CAPTURE_FAILED",
      "The viewer reported errors during the capture.",
      { error_count: errors.length },
    );
    return { captures, renderer_hash: rendererHash(), errors };
  } finally {
    signal.removeEventListener("abort", abort);
    await browser.close();
  }
}
