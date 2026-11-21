import { generateKeyPairSync } from "node:crypto";
import {
  constants,
  openSync,
  closeSync,
  fstatSync,
  readFileSync,
  writeFileSync,
  fsyncSync,
} from "node:fs";
import { join } from "node:path";
import { importJWK, jwtVerify, SignJWT } from "jose";
import { z } from "zod";
import { AccessProposal } from "../semantic-ir/access.js";
import { Id, Subject } from "../semantic-ir/identifiers.js";
import { hash, id } from "../semantic-ir/hash.js";
import { CadError, requireThat } from "../semantic-ir/errors.js";

const issuer = "urn:llcad:trusted-project-policy";
const type = "llcad-project-approval+jwt";
const Claims = z.strictObject({
  iss: z.literal(issuer),
  aud: z.string(),
  sub: Subject,
  tenant_id: Subject,
  iat: z.int().nonnegative(),
  exp: z.int().positive(),
  jti: Id,
  approval_request_id: Id,
  action_digest: z.string().regex(/^[a-f0-9]{64}$/),
  proposal: AccessProposal,
});
export type ApprovalClaims = z.infer<typeof Claims>;
function key(root: string) {
  const path = join(root, ".policy-approval-key.json");
  try {
    const fd = openSync(
      path,
      constants.O_WRONLY |
        constants.O_CREAT |
        constants.O_EXCL |
        constants.O_NOFOLLOW,
      0o600,
    );
    try {
      const pair = generateKeyPairSync("ed25519");
      writeFileSync(
        fd,
        JSON.stringify(pair.privateKey.export({ format: "jwk" })) + "\n",
      );
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    // Windows cannot fsync a directory; NTFS journals its entries.
    if (process.platform !== "win32") {
      const directory = openSync(
        root,
        constants.O_RDONLY | constants.O_DIRECTORY,
      );
      try {
        fsyncSync(directory);
      } finally {
        closeSync(directory);
      }
    }
  } catch (error: any) {
    if (error.code !== "EEXIST")
      throw new CadError(
        "AUTH_REQUIRED",
        "The approval key cannot be provided.",
      );
  }
  const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = fstatSync(fd);
    requireThat(
      info.isFile() &&
        info.size < 4096 &&
        // Windows has no POSIX modes or owners; the data folder's ACL keeps it private.
        (process.platform === "win32" ||
          ((info.mode & 0o077) === 0 && info.uid === process.getuid?.())),
      "AUTH_REQUIRED",
      "The approval key must be a private regular file.",
    );
    const value = JSON.parse(readFileSync(fd, "utf8"));
    requireThat(
      value.kty === "OKP" &&
        value.crv === "Ed25519" &&
        typeof value.x === "string" &&
        typeof value.d === "string",
      "AUTH_REQUIRED",
      "The approval key has an unknown format.",
    );
    return value;
  } catch {
    throw new CadError(
      "AUTH_REQUIRED",
      "The approval key is not available.",
    );
  } finally {
    closeSync(fd);
  }
}
/** Verification is separate from ordinary OAuth/session authentication. */
export class ApprovalVerifier {
  private publicKey: ReturnType<typeof importJWK>;
  private keyID: string;
  constructor(
    root: string,
    readonly audience: string,
  ) {
    const stored = key(root),
      publicJWK = { kty: stored.kty, crv: stored.crv, x: stored.x };
    this.keyID = hash(publicJWK);
    this.publicKey = importJWK(publicJWK, "EdDSA");
  }
  async verify(token: unknown): Promise<ApprovalClaims> {
    try {
      requireThat(
        typeof token === "string" && token.length <= 65536,
        "NEEDS_APPROVAL",
        "The signed confirmation is missing.",
      );
      const { payload, protectedHeader } = await jwtVerify(
        token,
        await this.publicKey,
        {
          algorithms: ["EdDSA"],
          issuer,
          audience: this.audience,
          typ: type,
          requiredClaims: ["iat", "exp", "sub", "jti"],
          maxTokenAge: "2m",
          clockTolerance: 0,
        },
      );
      const claims = Claims.parse(payload);
      requireThat(
        protectedHeader.kid === this.keyID &&
          claims.exp <= claims.iat + 120 &&
          claims.iat <= Math.floor(Date.now() / 1000) &&
          claims.proposal.actor.user === claims.sub &&
          claims.proposal.actor.tenant === claims.tenant_id &&
          hash(claims.proposal) === claims.action_digest,
        "NEEDS_APPROVAL",
        "The confirmation is not bound to this approval candidate.",
      );
      return claims;
    } catch {
      throw new CadError(
        "NEEDS_APPROVAL",
        "The confirmation is invalid, expired or meant for a different action.",
      );
    }
  }
}
/** Trusted policy helper only; never registered as a CAD tool or HTTP signer. */
export async function signApproval(
  root: string,
  audience: string,
  request: {
    approval_request_id: string;
    action_digest: string;
    proposal: AccessProposal;
    expires_at: string;
  },
  expectedDigest: string,
) {
  requireThat(
    request.action_digest === expectedDigest &&
      hash(request.proposal) === expectedDigest,
    "NEEDS_APPROVAL",
    "The request does not match the explicitly confirmed action.",
  );
  const stored = key(root),
    now = Math.floor(Date.now() / 1000),
    expires = Math.min(
      now + 120,
      Math.floor(Date.parse(request.expires_at) / 1000),
    );
  requireThat(
    expires > now,
    "NEEDS_APPROVAL",
    "The approval request has expired.",
  );
  const proposal = AccessProposal.parse(request.proposal);
  return new SignJWT({
    tenant_id: proposal.actor.tenant,
    approval_request_id: request.approval_request_id,
    action_digest: expectedDigest,
    proposal,
  })
    .setProtectedHeader({
      alg: "EdDSA",
      typ: type,
      kid: hash({ kty: stored.kty, crv: stored.crv, x: stored.x }),
    })
    .setIssuer(issuer)
    .setAudience(audience)
    .setSubject(proposal.actor.user)
    .setIssuedAt(now)
    .setExpirationTime(expires)
    .setJti(id("consent"))
    .sign(await importJWK(stored, "EdDSA"));
}
