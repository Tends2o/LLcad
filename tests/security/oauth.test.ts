import test from "node:test";
import assert from "node:assert/strict";
import { generateKeyPair, exportJWK, SignJWT, createLocalJWKSet } from "jose";
import { Auth } from "../../packages/mcp-gateway/auth.js";
test("OAuth verifies issuer, audience, signature, expiry and signed tenant claims", async () => {
  const { privateKey, publicKey } = await generateKeyPair("RS256");
  const jwk = await exportJWK(publicKey);
  jwk.kid = "key-1";
  const auth = new Auth({
    mode: "oauth",
    publicURL: "https://cad.example.test",
    dataRoot: "/tmp/unused",
    issuer: "https://auth.example.test",
    jwksURL: "https://auth.example.test/jwks",
    audience: "https://cad.example.test/mcp",
  });
  (auth as any).jwks = createLocalJWKSet({ keys: [jwk] });
  async function token(overrides: any = {}) {
    return new SignJWT({
      tenant_id: "tenant-1",
      scope: "model:read model:edit",
      ...overrides,
    })
      .setProtectedHeader({ alg: "RS256", kid: "key-1" })
      .setIssuedAt()
      .setSubject("user-1")
      .setIssuer(overrides.iss ?? "https://auth.example.test")
      .setAudience(overrides.aud ?? "https://cad.example.test/mcp")
      .setExpirationTime(overrides.exp ?? "5m")
      .sign(privateKey);
  }
  const request = (token: string) =>
    ({
      header: (name: string) =>
        name === "authorization" ? "Bearer " + token : undefined,
      headers: {},
      method: "POST",
    }) as any;
  assert.deepEqual(await auth.authenticate(request(await token())), {
    tenant: "tenant-1",
    user: "user-1",
    scopes: ["model:read", "model:edit"],
  });
  for (const overrides of [
    { iss: "https://evil.test" },
    { aud: "https://wrong.test" },
    { exp: 1 },
    { tenant_id: null },
  ])
    await assert.rejects(
      () => token(overrides).then((t) => auth.authenticate(request(t))),
      (e: any) => e.code === "AUTH_REQUIRED",
    );
  const signed = await token();
  const parts = signed.split(".");
  parts[1] = Buffer.from(
    JSON.stringify({ tenant_id: "other", scope: "model:commit" }),
  ).toString("base64url");
  await assert.rejects(
    () => auth.authenticate(request(parts.join("."))),
    (e: any) => e.code === "AUTH_REQUIRED",
  );
});
