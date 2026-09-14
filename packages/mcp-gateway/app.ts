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
export function createApp(service: ModelService, config: AuthConfig) {
  const app = express(),
    auth = new Auth(config);
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
        typeof req.body?.token === "string" &&
        auth.checkLocal(req.body.token),
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
      } else modern(service, res.locals.principal, req, res);
    } catch (error) {
      next(error);
    }
  });
  app.all("/mcp", authenticate, (_req, res) =>
    res.status(405).set("Allow", "POST").end(),
  );
  app.post("/api/tools/:name", authenticate, rate, (req, res) =>
    res.json(
      service.call(res.locals.principal, req.params.name as ToolName, req.body),
    ),
  );
  app.get("/api/models", authenticate, rate, (req, res) => {
    const p = res.locals.principal;
    authorize(p, "model:read");
    res.json({
      models: service.store.all(
        "SELECT id,name,head,created FROM models WHERE tenant=? AND owner=? ORDER BY created DESC LIMIT 100",
        p.tenant,
        p.user,
      ),
    });
  });
  app.get("/api/artifacts/:id", authenticate, rate, (req, res) => {
    const a = service.store.getArtifact(
      res.locals.principal,
      String(req.params.id),
    );
    res.type(a.mime);
    res.setHeader(
      "Content-Disposition",
      `inline; filename="${a.manifest.filename ?? a.id}"`,
    );
    res.send(service.store.readBlob(a.hash));
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
  return { app, auth };
}
