import express, { Request, Response, NextFunction } from "express";
import { resolve, join } from "node:path";
import { ModelService } from "../model-service/index.js";
import { Auth, AuthConfig } from "./auth.js";
import { Principal, authorize, SCOPES } from "../policy/index.js";
import { safeError, requireThat } from "../semantic-ir/errors.js";
import { ToolName } from "../semantic-ir/schema.js";
import { LIMITS, checkDepth } from "../compiler/index.js";
import { modern } from "./adapters/protocol_2026_07_28.js";
import { legacy } from "./adapters/legacy_tested.js";
import { LEGACY_VERSIONS } from "./versions.js";
import { ApprovalVerifier } from "../policy/approvals.js";
import { AccessPayload } from "../semantic-ir/access.js";
import { assertResult } from "../model-service/result-contracts.js";
import { DownloadTokens } from "../policy/downloads.js";
import { gzipSync } from "node:zlib";
import { randomBytes } from "node:crypto";
export function createApp(service: ModelService, config: AuthConfig) {
  const app = express(),
    auth = new Auth(config);
  // Single-use login codes let cad_viewer_open hand the browser a ready session
  // without exposing the long-lived local key in tool results or URLs.
  const bootstrapCodes = new Map<string, number>();
  const issueBootstrapCode = () => {
    const now = Date.now();
    for (const [code, expires] of bootstrapCodes)
      if (expires < now) bootstrapCodes.delete(code);
    const code = randomBytes(24).toString("base64url");
    bootstrapCodes.set(code, now + 300000);
    return code;
  };
  const redeemBootstrapCode = (code: unknown) => {
    if (typeof code !== "string" || !bootstrapCodes.has(code)) return false;
    const expires = bootstrapCodes.get(code)!;
    bootstrapCodes.delete(code);
    return expires >= Date.now();
  };
  const approvals = new ApprovalVerifier(
    config.dataRoot,
    new URL("/api/policy/approvals", config.publicURL).toString(),
  );
  app.disable("x-powered-by");
  const publicOrigin = new URL(config.publicURL).origin,
    host = new URL(config.publicURL).host;
  app.use((req, res, next) => {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("Referrer-Policy", "no-referrer");
    res.setHeader("Cache-Control", "no-store");
    res.setHeader(
      "Content-Security-Policy",
      "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    );
    if (req.headers.host !== host)
      return res.status(403).json({ error: "Host not allowed" });
    if (req.headers.origin && req.headers.origin !== publicOrigin)
      return res.status(403).json({ error: "Origin not allowed" });
    next();
  });
  app.get("/healthz", (_req, res) =>
    res.json({ status: "ok", application_version: "0.1.0" }),
  );
  app.get("/api/config", (_req, res) => res.json({ auth_mode: config.mode }));
  app.get("/.well-known/oauth-protected-resource", (_req, res) =>
    res.json({
      resource: config.audience ?? config.publicURL,
      authorization_servers: config.issuer ? [config.issuer] : [],
      scopes_supported: SCOPES,
    }),
  );
  const authenticate = async (
    req: Request,
    res: Response,
    next: NextFunction,
  ) => {
    try {
      res.locals.principal = await auth.authenticate(req);
      next();
    } catch (error) {
      res.setHeader(
        "WWW-Authenticate",
        `Bearer resource_metadata="${publicOrigin}/.well-known/oauth-protected-resource"`,
      );
      res.status(401).json({ error: safeError(error) });
    }
  };
  const rates = new Map<string, { start: number; count: number }>();
  const rate = (req: Request, res: Response, next: NextFunction) => {
    const p = res.locals.principal as Principal;
    const key = p.tenant + ":" + p.user;
    let r = rates.get(key);
    if (!r || Date.now() - r.start > 60000) {
      r = { start: Date.now(), count: 0 };
      rates.set(key, r);
    }
    if (rates.size > 10000)
      for (const [k, v] of rates)
        if (Date.now() - v.start > 60000) rates.delete(k);
    if (++r.count > 600)
      return res.status(429).json({
        error: {
          code: "BUDGET_EXCEEDED",
          message: "Requestbudget pro Minute überschritten.",
        },
      });
    next();
  };
  app.post(
    "/api/uploads",
    authenticate,
    rate,
    express.raw({
      type: "application/octet-stream",
      limit: LIMITS.max_artifact_bytes,
    }),
    (req, res) => {
      authorize(res.locals.principal, "model:edit");
      requireThat(
        Buffer.isBuffer(req.body) && req.body.length > 0,
        "INVALID_SCHEMA",
        "Binäre Upload-Daten erforderlich.",
      );
      const a = service.store.artifact(
        res.locals.principal,
        req.body,
        "application/octet-stream",
        null,
        null,
        { source: "user_upload", trusted: false },
      );
      res.status(201).json(a);
    },
  );
  app.use(express.json({ limit: LIMITS.request_bytes }));
  app.use((req, res, next) => {
    try {
      if (req.body) checkDepth(req.body);
      next();
    } catch (error) {
      res.status(400).json({ error: safeError(error) });
    }
  });
  app.post("/api/session", (req, res) => {
    requireThat(
      config.mode === "local" &&
        ((typeof req.body?.token === "string" &&
          auth.checkLocal(req.body.token)) ||
          redeemBootstrapCode(req.body?.code)),
      "AUTH_REQUIRED",
      "Zugangsschlüssel ungültig.",
    );
    res.setHeader(
      "Set-Cookie",
      `mathforge_session=${auth.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800`,
    );
    res.json({ status: "authenticated" });
  });
  app.delete("/api/session", authenticate, (req, res) => {
    res.setHeader(
      "Set-Cookie",
      "mathforge_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
    );
    res.json({ status: "logged_out" });
  });
  app.post("/mcp", authenticate, rate, async (req, res, next) => {
    try {
      const version = req.header("mcp-protocol-version");
      if (
        LEGACY_VERSIONS.includes(version ?? "") ||
        (!version && req.body?.method === "initialize")
      ) {
        await legacy(service, res.locals.principal, req, res);
      } else await modern(service, res.locals.principal, req, res);
    } catch (error) {
      next(error);
    }
  });
  app.all("/mcp", authenticate, (_req, res) =>
    res.status(405).set("Allow", "POST").end(),
  );
  app.post("/api/tools/:name", authenticate, rate, async (req, res, next) => {
    try {
      res.json(
        await service.call(
          res.locals.principal,
          req.params.name as ToolName,
          req.body,
        ),
      );
    } catch (error) {
      next(error);
    }
  });
  app.get("/api/policy/requests/:id", authenticate, rate, (req, res) => {
    const row = service.store.get(
      "SELECT model FROM approval_requests WHERE id=?",
      String(req.params.id),
    );
    requireThat(row, "ACCESS_DENIED", "Freigabeantrag nicht zugänglich.");
    const result = service.store.access.request(
      res.locals.principal,
      row.model,
      String(req.params.id),
    );
    assertResult(AccessPayload, result, "access_request");
    res.json(result);
  });
  app.post(
    "/api/policy/approvals/:id",
    authenticate,
    rate,
    async (req, res, next) => {
      try {
        requireThat(
          req.body &&
            Object.keys(req.body).length === 1 &&
            typeof req.body.approval_jwt === "string",
          "NEEDS_APPROVAL",
          "Gebundene signierte Bestätigung erforderlich.",
        );
        const claims = await approvals.verify(req.body.approval_jwt);
        res.json(
          service.store.access.approve(
            res.locals.principal,
            String(req.params.id),
            claims,
          ),
        );
      } catch (error) {
        next(error);
      }
    },
  );
  app.get("/api/models", authenticate, rate, (req, res) => {
    const p = res.locals.principal;
    authorize(p, "model:read");
    const visible = service.store.access.visible(p);
    res.json({
      models: service.store.all(
        "SELECT id,name,head,created FROM models WHERE " +
          visible.sql +
          " ORDER BY created DESC LIMIT 100",
        ...visible.params,
      ),
    });
  });
  const downloads = new DownloadTokens(
    config.dataRoot,
    new URL("/api/artifacts", config.publicURL).toString(),
  );
  service.downloads = downloads;
  const tokenOrSession = async (
    req: Request,
    res: Response,
    next: NextFunction,
  ) => {
    if (typeof req.query.token !== "string")
      return authenticate(req, res, next);
    try {
      const claims = await downloads.verify(req.query.token);
      requireThat(
        claims.artifact_id === String(req.params.id),
        "AUTH_REQUIRED",
        "Downloadtoken gilt für ein anderes Artefakt.",
      );
      res.locals.principal = {
        tenant: claims.tenant_id,
        user: claims.sub,
        scopes: ["model:read"],
      } satisfies Principal;
      next();
    } catch (error) {
      res.status(401).json({ error: safeError(error) });
    }
  };
  const sendCompressed = (req: Request, res: Response, body: Buffer) => {
    if (
      body.length > 1024 &&
      /\bgzip\b/.test(req.header("accept-encoding") ?? "") &&
      /^(application\/json|model\/|text\/)/.test(
        String(res.getHeader("content-type") ?? ""),
      )
    ) {
      res.setHeader("Content-Encoding", "gzip");
      res.setHeader("Vary", "Accept-Encoding");
      return res.send(gzipSync(body));
    }
    res.send(body);
  };
  app.get("/api/artifacts/:id", tokenOrSession, rate, (req, res) => {
    const a = service.store.getArtifact(
      res.locals.principal,
      String(req.params.id),
    );
    res.type(a.mime);
    res.setHeader(
      "Content-Disposition",
      `inline; filename="${a.manifest.filename ?? a.id}"`,
    );
    sendCompressed(req, res, service.store.readBlob(a.hash));
  });
  app.get("/api/metrics", authenticate, rate, (req, res) => {
    authorize(res.locals.principal, "model:read");
    res.json(service.jobs.metrics.snapshot());
  });
  app.get("/api/resources", authenticate, rate, (req, res) => {
    requireThat(
      typeof req.query.uri === "string",
      "INVALID_SCHEMA",
      "Ressourcen-URI fehlt.",
    );
    res.json(service.resource(res.locals.principal, req.query.uri));
  });
  app.get("/api/examples/:name", authenticate, rate, (req, res) => {
    const names: Record<string, string> = {
      housing: "micro-details/housing.json",
      sphere: "analytic/sphere.json",
      organic: "organic/sphere.json",
      assembly: "assemblies/pins.json",
    };
    const name = names[String(req.params.name)];
    requireThat(name, "INVALID_SCHEMA", "Beispiel unbekannt.");
    res.sendFile(resolve("fixtures", name));
  });
  app.use(
    express.static(resolve("public"), {
      index: "index.html",
      dotfiles: "deny",
    }),
  );
  app.use((error: any, req: Request, res: Response, _next: NextFunction) => {
    const status =
      error.type === "entity.too.large"
        ? 413
        : error.type === "entity.parse.failed"
          ? 400
          : error.code === "AUTH_REQUIRED"
            ? 401
            : error.code === "ACCESS_DENIED"
              ? 403
              : 400;
    res.status(status).json(
      req.path === "/mcp"
        ? {
            jsonrpc: "2.0",
            id: req.body?.id,
            error: { code: -32602, message: "Invalid request" },
          }
        : { error: safeError(error) },
    );
  });
  return { app, auth, issueBootstrapCode };
}
