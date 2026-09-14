import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, statSync, chmodSync } from "node:fs";
import { join, resolve } from "node:path";
import { once } from "node:events";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import {
  decodeJwt,
  decodeProtectedHeader,
  importJWK,
  SignJWT,
  generateKeyPair,
  createLocalJWKSet,
  exportJWK,
} from "jose";
import { setup, call, principal } from "../helpers.js";
import { id, hash } from "../../packages/semantic-ir/hash.js";
import {
  ApprovalVerifier,
  signApproval,
} from "../../packages/policy/approvals.js";
import { createApp } from "../../packages/mcp-gateway/app.js";
import { ToolSchemas } from "../../packages/semantic-ir/schema.js";

test("signed OAuth principals see only current shared projects and HTTP resources follow revocation", async () => {
  const env = setup(),
    { privateKey, publicKey } = await generateKeyPair("RS256");
  const server = (await import("node:http")).createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const url = `http://127.0.0.1:${(server.address() as any).port}`;
  const config = {
    mode: "oauth" as const,
    publicURL: url.replace("http:", "https:"),
    dataRoot: env.dir,
    issuer: "https://identity.example.test",
    audience: "https://cad.example.test/mcp",
    jwksURL: "https://identity.example.test/jwks",
  };
  const { app, auth } = createApp(env.service, config);
  const jwk = await exportJWK(publicKey);
  jwk.kid = "fixture";
  (auth as any).jwks = createLocalJWKSet({ keys: [jwk] });
  server.on("request", app);
  const token = await new SignJWT({
    tenant_id: principal.tenant,
    scope: principal.scopes.join(" "),
  })
    .setProtectedHeader({ alg: "RS256", kid: "fixture" })
    .setSubject("bob")
    .setIssuer(config.issuer)
    .setAudience(config.audience)
    .setIssuedAt()
    .setExpirationTime("5m")
    .sign(privateKey);
  const headers = {
    Authorization: "Bearer " + token,
  };
  const get = (path: string) => fetch(url + path, { headers });
  try {
    const request = proposal(env),
      m = request.model_id;
    const artifact = env.service.store.artifact(
      principal,
      "private model bytes",
      "text/plain",
      m,
      request.base_revision,
      {},
    );
    assert.equal((await get(artifact.download)).status, 403);
    const audience = new URL(
        "/api/policy/approvals",
        config.publicURL,
      ).toString(),
      verifier = new ApprovalVerifier(env.dir, audience);
    env.service.store.access.approve(
      principal,
      request.approval_request_id,
      await verifier.verify(
        await signApproval(env.dir, audience, request, request.action_digest),
      ),
    );
    assert.equal((await (await get("/api/models")).json()).models.length, 1);
    assert.equal((await get(artifact.download)).status, 200);
    assert.equal(
      (await get("/api/resources?uri=" + encodeURIComponent(artifact.uri)))
        .status,
      200,
    );
    assert.equal(
      (await get("/api/policy/requests/" + request.approval_request_id)).status,
      403,
    );
    const revoked = call(env.service, "cad_access", {
      mode: "propose",
      model_id: m,
      base_revision: request.base_revision,
      change: { action: "revoke", recipient: "bob" },
      idempotency_key: id("revoke"),
    });
    env.service.store.access.approve(
      principal,
      revoked.approval_request_id,
      await verifier.verify(
        await signApproval(env.dir, audience, revoked, revoked.action_digest),
      ),
    );
    assert.equal((await (await get("/api/models")).json()).models.length, 0);
    assert.equal((await get(artifact.download)).status, 403);
    assert.equal(
      (await get("/api/resources?uri=" + encodeURIComponent(artifact.uri)))
        .status,
      403,
    );
  } finally {
    await new Promise<void>((r) => server.close(() => r()));
    await env.close();
  }
});

const audience = "https://policy.example.test/api/policy/approvals";
function proposal(
  env: ReturnType<typeof setup>,
  p = principal,
  model?: string,
  recipient = "bob",
) {
  const m = model
    ? env.service.store.model(p, model)
    : call(
        env.service,
        "cad_create_model",
        { name: "Approval fixture", idempotency_key: id("create") },
        p,
      );
  return call(
    env.service,
    "cad_access",
    {
      mode: "propose",
      model_id: model ?? m.model_id,
      base_revision: m.head ?? m.revision,
      change: {
        action: "grant",
        grant: {
          recipient,
          role: "editor",
          can_export: false,
          edit_scope: { kind: "model" },
          budget: { jobs: 4, seconds_per_job: 10 },
          expires_at: new Date(Date.now() + 3600000).toISOString(),
        },
      },
      idempotency_key: id("grant"),
    },
    p,
  );
}
test("approval assertions reject altered signatures, algorithms, purposes, actors, lifetimes and contents", async () => {
  const env = setup();
  try {
    const request = proposal(env),
      verifier = new ApprovalVerifier(env.dir, audience);
    const token = await signApproval(
      env.dir,
      audience,
      request,
      request.action_digest,
    );
    const valid = await verifier.verify(token),
      payload = decodeJwt(token),
      header = { ...decodeProtectedHeader(token), alg: "EdDSA" };
    assert.equal(valid.action_digest, hash(request.proposal));
    const privateKey = await importJWK(
      JSON.parse(
        readFileSync(join(env.dir, ".policy-approval-key.json"), "utf8"),
      ),
      "EdDSA",
    );
    const now = Math.floor(Date.now() / 1000);
    const wrongKey = (await generateKeyPair("EdDSA")).privateKey;
    const cases = [
      await new SignJWT(payload).setProtectedHeader(header).sign(wrongKey),
      await new SignJWT(payload)
        .setProtectedHeader({ ...header, alg: "HS256" })
        .sign(new Uint8Array(32)),
      ...(await Promise.all(
        [
          { iss: "urn:wrong" },
          { aud: "https://wrong.test" },
          { sub: "mallory" },
          { tenant_id: "foreign" },
          { exp: now - 1 },
          { exp: now + 121, iat: now },
          { iat: now + 20, exp: now + 100 },
          { jti: undefined },
          { action_digest: "0".repeat(64) },
          {
            proposal: {
              ...request.proposal,
              change: {
                ...request.proposal.change,
                grant: {
                  ...request.proposal.change.grant,
                  budget: { jobs: 1000, seconds_per_job: 45 },
                },
              },
            },
          },
        ].map((change) =>
          new SignJWT({ ...payload, ...change })
            .setProtectedHeader(header)
            .sign(privateKey),
        ),
      )),
      await new SignJWT(payload)
        .setProtectedHeader({ ...header, typ: "JWT" })
        .sign(privateKey),
      await new SignJWT(payload)
        .setProtectedHeader({ ...header, kid: "wrong" })
        .sign(privateKey),
    ];
    const parts = token.split(".");
    parts[1] = Buffer.from(
      JSON.stringify({ ...payload, sub: "mallory" }),
    ).toString("base64url");
    cases.push(parts.join("."));
    for (const assertion of cases)
      await assert.rejects(
        () => verifier.verify(assertion),
        (e: any) =>
          e.code === "NEEDS_APPROVAL" && !e.message.includes(assertion),
      );
    await assert.rejects(
      () => signApproval(env.dir, audience, request, "0".repeat(64)),
      (e: any) => e.code === "NEEDS_APPROVAL",
    );
    assert.equal(
      statSync(join(env.dir, ".policy-approval-key.json")).mode & 0o777,
      0o600,
    );
    assert.ok(
      !JSON.stringify(env.service.store.all("SELECT * FROM audit")).includes(
        token,
      ),
    );
    chmodSync(join(env.dir, ".policy-approval-key.json"), 0o644);
    assert.throws(
      () => new ApprovalVerifier(env.dir, audience),
      (e: any) => e.code === "AUTH_REQUIRED",
    );
  } finally {
    await env.close();
  }
});

test("approval consumption binds actor, request, model state and ACL generation and never replays", async () => {
  const env = setup(),
    access = env.service.store.access;
  try {
    const a = proposal(env),
      b = proposal(env),
      verifier = new ApprovalVerifier(env.dir, audience);
    const claims = await verifier.verify(
      await signApproval(env.dir, audience, a, a.action_digest),
    );
    assert.throws(
      () => access.approve(principal, b.approval_request_id, claims),
      (e: any) => e.code === "NEEDS_APPROVAL",
    );
    assert.throws(
      () =>
        access.approve(
          { ...principal, user: "mallory" },
          a.approval_request_id,
          claims,
        ),
      (e: any) => e.code === "ACCESS_DENIED",
    );
    const stale = proposal(env, principal, a.model_id, "carol");
    const staleClaims = await verifier.verify(
      await signApproval(env.dir, audience, stale, stale.action_digest),
    );
    access.approve(principal, a.approval_request_id, claims);
    assert.throws(
      () => access.approve(principal, a.approval_request_id, claims),
      (e: any) => e.code === "NEEDS_APPROVAL",
    );
    assert.throws(
      () => access.approve(principal, stale.approval_request_id, staleClaims),
      (e: any) => e.code === "STALE_REVISION",
    );
    const fresh = proposal(env, principal, b.model_id, "carol");
    const freshClaims = await verifier.verify(
      await signApproval(env.dir, audience, fresh, fresh.action_digest),
    );
    assert.throws(
      () =>
        access.approve(principal, fresh.approval_request_id, {
          ...freshClaims,
          jti: claims.jti,
        }),
      (e: any) => e.code === "NEEDS_APPROVAL",
    );
    const store = env.service.store,
      old = store.model(principal, b.model_id).head;
    store.run("UPDATE models SET head=? WHERE id=?", "rev-changed", b.model_id);
    assert.throws(
      () => access.approve(principal, fresh.approval_request_id, freshClaims),
      (e: any) => e.code === "STALE_REVISION",
    );
    store.run("UPDATE models SET head=? WHERE id=?", old, b.model_id);
    store.run(
      "UPDATE approval_requests SET expires=0 WHERE id=?",
      fresh.approval_request_id,
    );
    assert.throws(
      () => access.approve(principal, fresh.approval_request_id, freshClaims),
      (e: any) => e.code === "NEEDS_APPROVAL",
    );
    assert.equal(
      store.get("SELECT COUNT(*) AS n FROM approval_consumptions").n,
      1,
    );
    assert.equal(store.get("SELECT COUNT(*) AS n FROM model_grants").n, 1);
  } finally {
    await env.close();
  }
});

test("local trusted helper applies exactly the reviewed grant through authenticated HTTP without exposing tokens", async () => {
  const env = setup(),
    p = { ...principal, tenant: "local", user: "local-user" };
  const server = (await import("node:http")).createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const url = `http://127.0.0.1:${(server.address() as any).port}`;
  const { app, auth } = createApp(env.service, {
    mode: "local",
    publicURL: url,
    dataRoot: env.dir,
  });
  server.on("request", app);
  const headers = {
    Authorization: "Bearer " + auth.token,
    "Content-Type": "application/json",
  };
  try {
    const request = proposal(env, p);
    assert.equal(
      (await fetch(url + "/api/policy/requests/" + request.approval_request_id))
        .status,
      401,
    );
    for (const body of [
      {},
      { approval_jwt: auth.token },
      { approved: true },
      { approval_jwt: "fabricated", extra: true },
    ]) {
      const r = await fetch(
        url + "/api/policy/approvals/" + request.approval_request_id,
        { method: "POST", headers, body: JSON.stringify(body) },
      );
      assert.equal(r.status, 400);
      assert.equal((await r.json()).error.code, "NEEDS_APPROVAL");
    }
    assert.equal(
      ToolSchemas.cad_access.safeParse({
        mode: "approve",
        model_id: request.model_id,
      }).success,
      false,
    );
    const run = (digest: string) =>
      promisify(execFile)(
        process.execPath,
        [
          "--import",
          "tsx",
          resolve("scripts/approve-project.ts"),
          request.approval_request_id,
          digest,
        ],
        {
          cwd: process.cwd(),
          env: {
            ...process.env,
            MATHFORGE_DATA: env.dir,
            MATHFORGE_PUBLIC_URL: url,
          },
          timeout: 20000,
        },
      );
    await assert.rejects(() => run("0".repeat(64)));
    assert.equal(
      env.service.store.get("SELECT COUNT(*) AS n FROM model_grants").n,
      0,
    );
    const result = await run(request.action_digest);
    assert.equal(JSON.parse(result.stdout).status, "approved");
    assert.ok(!result.stdout.includes(auth.token));
    assert.ok(!result.stdout.includes("eyJ"));
    await assert.rejects(() => run(request.action_digest));
    assert.equal(
      call(env.service, "cad_list_models", {}, { ...p, user: "bob" }).total,
      1,
    );
    const view = await fetch(
      url + "/api/policy/requests/" + request.approval_request_id,
      { headers },
    );
    assert.equal((await view.json()).request_state, "approved");
  } finally {
    await new Promise<void>((r) => server.close(() => r()));
    await env.close();
  }
});
