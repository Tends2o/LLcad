import { createRemoteJWKSet, jwtVerify } from "jose";
import { timingSafeEqual, randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { Request } from "express";
import { Principal, SCOPES } from "../policy/index.js";
import { requireThat, CadError } from "../semantic-ir/errors.js";
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
        "OAuth benötigt HTTPS-Issuer, JWKS, Ressource und Audience.",
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
        "Lokale Authentifizierung ist auf Loopback beschränkt.",
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
        "Lokaler Zugangsschlüssel ist zu kurz.",
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
          "Browseranforderung ist nicht an diesen Ursprung gebunden.",
        );
    }
    requireThat(token, "AUTH_REQUIRED", "Zugangsschlüssel erforderlich.");
    if (this.config.mode === "local") {
      requireThat(
        this.checkLocal(token),
        "AUTH_REQUIRED",
        "Zugangsschlüssel ungültig.",
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
        "Identitätsclaims fehlen.",
      );
      return {
        tenant: payload.tenant_id,
        user: payload.sub,
        scopes: payload.scope.split(" ").filter((s) => SCOPES.includes(s)),
      };
    } catch {
      throw new CadError(
        "AUTH_REQUIRED",
        "Zugriffstoken ungültig oder abgelaufen.",
      );
    }
  }
}
