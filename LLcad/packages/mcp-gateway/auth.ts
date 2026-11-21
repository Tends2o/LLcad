import { createRemoteJWKSet, jwtVerify } from "jose";
import { timingSafeEqual, randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { Request } from "express";
import { Principal, SCOPES } from "../policy/index.js";
import { requireThat, CadError } from "../semantic-ir/errors.js";
import { workerPrincipal } from "../feature-work/access.js";
export type AuthConfig = {
  mode: "local" | "oauth";
  publicURL: string;
  dataRoot: string;
  issuer?: string;
  jwksURL?: string;
  audience?: string;
  localToken?: string;
};
export class Auth {
  readonly token: string;
  private jwks?: ReturnType<typeof createRemoteJWKSet>;
  constructor(public config: AuthConfig) {
    if (config.mode === "oauth") {
      requireThat(
        config.issuer?.startsWith("https://") &&
          config.jwksURL?.startsWith("https://") &&
          config.audience &&
          config.publicURL.startsWith("https://"),
        "AUTH_REQUIRED",
        "OAuth needs an HTTPS issuer, JWKS, resource and audience.",
      );
      this.jwks = createRemoteJWKSet(new URL(config.jwksURL!), {
        timeoutDuration: 5000,
        cooldownDuration: 30000,
      });
      this.token = "";
    } else {
      requireThat(
        ["127.0.0.1", "localhost", "[::1]"].includes(
          new URL(config.publicURL).hostname,
        ),
        "AUTH_REQUIRED",
        "Local authentication is restricted to loopback.",
      );
      const path = join(config.dataRoot, "local-token");
      mkdirSync(config.dataRoot, { recursive: true, mode: 0o700 });
      if (config.localToken) this.token = config.localToken;
      else {
        if (!existsSync(path))
          writeFileSync(path, randomBytes(32).toString("base64url"), {
            mode: 0o600,
          });
        this.token = readFileSync(path, "utf8").trim();
      }
      requireThat(
        this.token.length >= 32,
        "AUTH_REQUIRED",
        "The local access key is too short.",
      );
    }
  }
  checkLocal(token: string) {
    const a = Buffer.from(token),
      b = Buffer.from(this.token);
    return (
      this.config.mode === "local" &&
      a.length === b.length &&
      timingSafeEqual(a, b)
    );
  }
  async authenticate(req: Request): Promise<Principal> {
    const authorization = req.header("authorization");
    let token = authorization?.startsWith("Bearer ")
      ? authorization.slice(7)
      : undefined;
    if (!token && this.config.mode === "local") {
      token = req.headers.cookie
        ?.split(";")
        .map((c) => c.trim())
        .find((c) => c.startsWith("mathforge_session="))
        ?.slice("mathforge_session=".length);
      if (token && req.method !== "GET")
        requireThat(
          req.header("x-mathforge-client") === "viewer" &&
            req.header("origin") === new URL(this.config.publicURL).origin,
          "ACCESS_DENIED",
          "The browser request is not bound to this origin.",
        );
    }
    requireThat(token, "AUTH_REQUIRED", "Access key required.");
    // Validated feature workers: short-lived per-task bearer tokens (designer grant on
    // one model, issued and revoked by cad_feature_batch).
    const worker = authorization ? workerPrincipal(token) : null;
    if (worker) return worker;
    if (this.config.mode === "local") {
      requireThat(
        this.checkLocal(token),
        "AUTH_REQUIRED",
        "Access key invalid.",
      );
      return { tenant: "local", user: "local-user", scopes: SCOPES };
    }
    try {
      const { payload } = await jwtVerify(token, this.jwks!, {
        issuer: this.config.issuer,
        audience: this.config.audience,
        algorithms: ["RS256", "ES256"],
        requiredClaims: ["exp", "iat", "sub"],
      });
      requireThat(
        typeof payload.sub === "string" &&
          typeof payload.tenant_id === "string" &&
          payload.tenant_id.length <= 128 &&
          typeof payload.scope === "string",
        "AUTH_REQUIRED",
        "Identity claims are missing.",
      );
      return {
        tenant: payload.tenant_id,
        user: payload.sub,
        scopes: payload.scope.split(" ").filter((s) => SCOPES.includes(s)),
      };
    } catch {
      throw new CadError(
        "AUTH_REQUIRED",
        "Access token invalid or expired.",
      );
    }
  }
}
