import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { sphere } from "../../scripts/fixtures.js";
import { id } from "../../packages/semantic-ir/hash.js";
import {
  ApprovalVerifier,
  signApproval,
} from "../../packages/policy/approvals.js";
import { createApp } from "../../packages/mcp-gateway/app.js";
import { DownloadTokens } from "../../packages/policy/downloads.js";

test("internal publication needs a bound approval, reaches only the named recipient and issues short-lived artifact links", async () => {
  const env = setup(),
    s = env.service;
  const server = (await import("node:http")).createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const url = `http://127.0.0.1:${(server.address() as any).port}`;
  const { app, auth } = createApp(s, {
    mode: "local",
    publicURL: url,
    dataRoot: env.dir,
  });
  server.on("request", app);
  try {
    const m = await importFixture(s, sphere);
    const exported = await finish(
      s,
      call(s, "cad_export", {
        model_id: m.model_id,
        revision: m.revision,
        format: "step",
        idempotency_key: id("export"),
      }),
    );
    const pkg = exported.package_manifest;
    const recipient = { ...principal, user: "carol" };
    // Before publication the recipient sees nothing, neither model nor artifacts.
    assert.equal(
      s.call(recipient, "cad_get_model", { model_id: m.model_id }).errors[0]
        .code,
      "ACCESS_DENIED",
    );
    assert.throws(
      () => s.store.getArtifact(recipient, pkg.artifact_id),
      (e: any) => e.code === "ACCESS_DENIED",
    );
    const proposal = call(s, "cad_access", {
      mode: "propose",
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("publish"),
      change: {
        action: "publish",
        recipient: "carol",
        revision: m.revision,
        package_artifact_id: pkg.artifact_id,
        package_hash: pkg.hash,
        validity_days: 7,
      },
    });
    assert.equal(proposal.status, "needs_approval");
    assert.equal(proposal.proposal.change.action, "publish");
    // A wrong package hash is refused at proposal time; a bound approval is required to publish.
    assert.equal(
      s.call(principal, "cad_access", {
        mode: "propose",
        model_id: m.model_id,
        base_revision: m.revision,
        idempotency_key: id("bad-hash"),
        change: { ...proposal.proposal.change, package_hash: "0".repeat(64) },
      }).errors[0].code,
      "INTEGRITY_FAILURE",
    );
    assert.throws(
      () => s.store.getArtifact(recipient, pkg.artifact_id),
      (e: any) => e.code === "ACCESS_DENIED",
    );
    const audience = new URL("/api/policy/approvals", url).toString();
    const verifier = new ApprovalVerifier(env.dir, audience);
    const decision: any = s.store.access.approve(
      principal,
      proposal.approval_request_id,
      await verifier.verify(
        await signApproval(env.dir, audience, proposal, proposal.action_digest),
      ),
    );
    assert.equal(decision.action, "publish");
    assert.ok(decision.artifacts.length >= 4);
    assert.ok(
      s.store.all("SELECT * FROM audit WHERE event='before_publish'").length >=
        1,
    );
    // The recipient can read exactly the published package components, nothing else about the model.
    assert.ok(s.store.getArtifact(recipient, pkg.artifact_id));
    assert.equal(
      s.call(recipient, "cad_get_model", { model_id: m.model_id }).errors[0]
        .code,
      "ACCESS_DENIED",
    );
    const listing = call(s, "cad_access", { mode: "publications" }, recipient);
    assert.equal(listing.publications.length, 1);
    const link = listing.publications[0].artifacts.find(
      (a: any) => a.artifact_id === pkg.artifact_id,
    );
    assert.match(link.signed_download, /token=/);
    assert.ok(Date.parse(link.expires_at) > Date.now());
    const anonymous = await fetch(url + link.signed_download);
    assert.equal(anonymous.status, 200);
    assert.equal(
      JSON.parse(await anonymous.text()).package_kind,
      "LLcad_revision_export",
    );
    const stranger = { ...principal, user: "dave" };
    assert.throws(
      () => s.store.getArtifact(stranger, pkg.artifact_id),
      (e: any) => e.code === "ACCESS_DENIED",
    );
    const tokens = new DownloadTokens(
      env.dir,
      new URL("/api/artifacts", url).toString(),
    );
    const foreign = await tokens.issue({
      tenant: principal.tenant,
      user: "dave",
      artifact_id: pkg.artifact_id,
      publication_id: null,
    });
    assert.equal(
      (
        await fetch(
          url + "/api/artifacts/" + pkg.artifact_id + "?token=" + foreign.token,
        )
      ).status,
      403,
    );
    const other = await tokens.issue({
      tenant: principal.tenant,
      user: "carol",
      artifact_id: "art-other",
      publication_id: null,
    });
    assert.equal(
      (
        await fetch(
          url + "/api/artifacts/" + pkg.artifact_id + "?token=" + other.token,
        )
      ).status,
      401,
    );
    assert.equal(decision.recipient, "carol");
    assert.equal(
      s.call(principal, "cad_access", {
        mode: "propose",
        model_id: m.model_id,
        base_revision: m.revision,
        idempotency_key: id("owner"),
        change: { ...proposal.proposal.change, recipient: principal.user },
      }).errors[0].code,
      "OUT_OF_SCOPE",
    );
  } finally {
    await new Promise<void>((r) => server.close(() => r()));
    await env.close();
  }
});
