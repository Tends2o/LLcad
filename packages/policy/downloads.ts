import {
  constants,
  openSync,
  closeSync,
  fstatSync,
  readFileSync,
} from "node:fs";
import { join } from "node:path";
import { createPrivateKey, sign as signBytes } from "node:crypto";
import { importJWK, jwtVerify } from "jose";
import { z } from "zod";
import { CadError, requireThat } from "../semantic-ir/errors.js";
import { hash, id } from "../semantic-ir/hash.js";
/** Short-lived, artifact-bound download tokens (Bauplan 10.3, 18.5).
 *
 * A token is a bearer capability for exactly one artifact, one recipient and a
 * few minutes. It is signed with the private policy key (Ed25519, synchronously
 * through node:crypto so tool responses stay synchronous) and verified with the
 * established jose verifier. Every use is still checked against the current
 * publication state; the token never replaces that authorization.
 */
const issuer = "urn:llcad:artifact-download";
const type = "llcad-download+jwt";
export const DOWNLOAD_TTL_SECONDS = 300;
const Claims = z.strictObject({
  iss: z.literal(issuer),
  aud: z.string(),
  sub: z.string().min(1).max(128),
  tenant_id: z.string().min(1).max(128),
  artifact_id: z.string().min(1).max(128),
  publication_id: z.string().min(1).max(128).nullable(),
  iat: z.int().nonnegative(),
  exp: z.int().positive(),
  jti: z.string().min(1).max(128),
});
export type DownloadClaims = z.infer<typeof Claims>;
function readKey(root: string) {
  const fd = openSync(
    join(root, ".policy-approval-key.json"),
    constants.O_RDONLY | constants.O_NOFOLLOW,
  );
  try {
    const info = fstatSync(fd);
    requireThat(
      info.isFile() && (info.mode & 0o077) === 0 && info.size < 4096,
      "AUTH_REQUIRED",
      "Downloadschlüssel muss eine private reguläre Datei sein.",
    );
    const value = JSON.parse(readFileSync(fd, "utf8"));
    requireThat(
      value.kty === "OKP" && value.crv === "Ed25519" && value.x && value.d,
      "AUTH_REQUIRED",
      "Downloadschlüssel hat ein unbekanntes Format.",
    );
    return value;
  } finally {
    closeSync(fd);
  }
}
const base64url = (data: Buffer | string) =>
  Buffer.from(data).toString("base64url");
export class DownloadTokens {
  constructor(
    private root: string,
    readonly audience: string,
  ) {}
  /** Synchronous compact JWS (EdDSA) so that tool responses can carry links without async signing. */
  issue(claims: {
    tenant: string;
    user: string;
    artifact_id: string;
    publication_id: string | null;
  }) {
    const stored = readKey(this.root),
      now = Math.floor(Date.now() / 1000);
    const header = base64url(
      JSON.stringify({
        alg: "EdDSA",
        typ: type,
        kid: hash({ kty: stored.kty, crv: stored.crv, x: stored.x }),
      }),
    );
    const payload = base64url(
      JSON.stringify({
        iss: issuer,
        aud: this.audience,
        sub: claims.user,
        tenant_id: claims.tenant,
        artifact_id: claims.artifact_id,
        publication_id: claims.publication_id,
        iat: now,
        exp: now + DOWNLOAD_TTL_SECONDS,
        jti: id("download"),
      }),
    );
    const key = createPrivateKey({ key: stored, format: "jwk" });
    const signature = signBytes(null, Buffer.from(header + "." + payload), key);
    return {
      token: header + "." + payload + "." + base64url(signature),
      expires_at: new Date((now + DOWNLOAD_TTL_SECONDS) * 1000).toISOString(),
    };
  }
  async verify(token: unknown): Promise<DownloadClaims> {
    try {
      requireThat(
        typeof token === "string" && token.length <= 4096,
        "AUTH_REQUIRED",
        "Downloadtoken fehlt.",
      );
      const stored = readKey(this.root);
      const publicJWK = { kty: stored.kty, crv: stored.crv, x: stored.x };
      const { payload, protectedHeader } = await jwtVerify(
        token,
        await importJWK(publicJWK, "EdDSA"),
        {
          algorithms: ["EdDSA"],
          issuer,
          audience: this.audience,
          typ: type,
          requiredClaims: ["iat", "exp", "sub", "jti"],
          maxTokenAge: DOWNLOAD_TTL_SECONDS + "s",
          clockTolerance: 0,
        },
      );
      const claims = Claims.parse(payload);
      requireThat(
        protectedHeader.kid === hash(publicJWK) &&
          claims.exp <= claims.iat + DOWNLOAD_TTL_SECONDS,
        "AUTH_REQUIRED",
        "Downloadtoken ist nicht an diesen Dienst gebunden.",
      );
      return claims;
    } catch {
      throw new CadError(
        "AUTH_REQUIRED",
        "Downloadtoken ungültig, abgelaufen oder für ein anderes Artefakt bestimmt.",
      );
    }
  }
}
