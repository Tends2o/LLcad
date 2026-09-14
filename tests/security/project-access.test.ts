import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { sphere, housing } from "../../scripts/fixtures.js";
import { id } from "../../packages/semantic-ir/hash.js";
import {
  ApprovalVerifier,
  signApproval,
} from "../../packages/policy/approvals.js";
import { Worker } from "../../packages/job-service/worker.js";
import { Store } from "../../packages/model-service/store.js";
import { DatabaseSync } from "node:sqlite";
import { join } from "node:path";
import { rmSync } from "node:fs";
import { eraseTenant } from "../../packages/model-service/maintenance.js";

const bob = { ...principal, user: "bob" },
  reviewer = { ...principal, user: "reviewer" };
const audience = "https://policy.example.test/api/policy/approvals";
type Env = ReturnType<typeof setup>;
function propose(env: Env, model: string, change: any) {
  return call(env.service, "cad_access", {
    mode: "propose",
    model_id: model,
    base_revision: env.service.store.model(principal, model).head,
    change,
    idempotency_key: id("access"),
  });
}
async function approve(env: Env, request: any) {
  const verifier = new ApprovalVerifier(env.dir, audience);
  const token = await signApproval(
    env.dir,
    audience,
    request,
    request.action_digest,
  );
  const claims = await verifier.verify(token);
  const result = env.service.store.access.approve(
    principal,
    request.approval_request_id,
    claims,
  );
  return { token, claims, result, verifier };
}
async function grant(
  env: Env,
  model: string,
  recipient = bob.user,
  overrides: any = {},
) {
  const request = propose(env, model, {
    action: "grant",
    grant: {
      recipient,
      role: "editor",
      can_export: false,
      edit_scope: { kind: "model" },
      budget: { jobs: 12, seconds_per_job: 45 },
      expires_at: new Date(Date.now() + 3600000).toISOString(),
      ...overrides,
    },
  });
  assert.equal(request.status, "needs_approval");
  return { request, ...(await approve(env, request)) };
}
const radius = (model: any) => ({
  model_id: model.model_id,
  base_revision: model.revision,
  idempotency_key: id("patch"),
  operations: [
    {
      op: "set_parameter",
      feature_id: "sphere",
      parameter: "radius",
      expected: { value: "10", unit: "mm" },
      value: { value: "11", unit: "mm" },
    },
  ],
});

test("schema 4 migration preserves historical rows and binds only already imported private sources", async () => {
  const env = setup(),
    s = env.service;
  let reopened: Store | undefined,
    closed = false;
  try {
    const m = await importFixture(s, sphere);
    const step = await finish(
      s,
      call(s, "cad_export", {
        model_id: m.model_id,
        revision: m.revision,
        format: "step",
        idempotency_key: id("export"),
      }),
    );
    const file = step.artifacts.find(
      (a: any) => a.manifest.filename === "model.step",
    );
    const upload = s.store.artifact(
      principal,
      s.store.readBlob(file.hash),
      "application/step",
      null,
      null,
      {},
    );
    const imported = call(s, "cad_create_model", {
      name: "Imported source",
      idempotency_key: id("create"),
    });
    const draft = call(s, "cad_import", {
      model_id: imported.model_id,
      base_revision: imported.revision,
      artifact_id: upload.artifact_id,
      format: "step",
      source_unit: "mm",
      idempotency_key: id("import"),
    });
    await finish(s, draft);
    const binding = {
      model_id: imported.model_id,
      base_revision: imported.revision,
      transaction_id: draft.transaction_id,
    };
    const proof = await finish(
      s,
      call(s, "cad_validate", { ...binding, idempotency_key: id("validate") }),
    );
    call(s, "cad_commit", {
      ...binding,
      validation_digest: proof.digest,
      idempotency_key: id("commit"),
    });
    const before = Object.fromEntries(
      ["models", "revisions", "transactions", "jobs", "artifacts"].map(
        (table) => [table, s.store.all(`SELECT * FROM ${table} ORDER BY id`)],
      ),
    );
    await s.close();
    closed = true;
    const db = new DatabaseSync(join(env.dir, "models.sqlite"));
    for (const table of [
      "artifact_models",
      "transaction_authorizations",
      "grant_actions",
      "job_authorizations",
      "approval_consumptions",
      "approval_requests",
      "model_grants",
      "model_acl",
    ])
      db.exec(`DROP TABLE ${table}`);
    db.exec("PRAGMA user_version=4");
    db.close();
    reopened = new Store(env.dir);
    assert.equal(reopened.get("PRAGMA user_version").user_version, 5);
    for (const [table, rows] of Object.entries(before))
      assert.deepEqual(
        reopened.all(`SELECT * FROM ${table} ORDER BY id`),
        rows,
      );
    assert.deepEqual(
      reopened.all("SELECT * FROM artifact_models").map((r) => ({ ...r })),
      [{ artifact: upload.artifact_id, model: imported.model_id }],
    );
    assert.equal(reopened.get("SELECT COUNT(*) AS n FROM model_grants").n, 0);
    assert.throws(
      () => reopened!.getArtifact(bob, upload.artifact_id),
      (e: any) => e.code === "ACCESS_DENIED",
    );
  } finally {
    reopened?.close();
    if (closed) rmSync(env.dir, { recursive: true, force: true });
    else await env.close();
  }
});

test("expired grants fence completed candidates and tenant deletion erases grants, proofs of consent and usage", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    await grant(env, m.model_id);
    const draft = call(s, "cad_apply_patch", radius(m), bob);
    await finish(s, draft, bob);
    s.store.run("UPDATE model_grants SET expires=0 WHERE model=?", m.model_id);
    assert.equal(call(s, "cad_list_models", {}, bob).total, 0);
    assert.equal(
      s.call(principal, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: draft.transaction_id,
        idempotency_key: id("validate"),
      }).errors[0].code,
      "ACCESS_DENIED",
    );
    await s.jobs.close();
    eraseTenant(s.store, principal.tenant, true);
    for (const table of [
      "model_grants",
      "model_acl",
      "approval_requests",
      "approval_consumptions",
      "job_authorizations",
      "transaction_authorizations",
      "grant_actions",
      "artifact_models",
    ])
      assert.equal(
        s.store.get(`SELECT COUNT(*) AS n FROM ${table}`).n,
        0,
        table,
      );
  } finally {
    await env.close();
  }
});

test("project roles intersect scopes, keep discovery private and separate design, review and export", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    await importFixture(s, sphere);
    assert.equal(call(s, "cad_list_models", {}, bob).total, 0);
    assert.equal(
      s.call(bob, "cad_get_model", { model_id: m.model_id }).errors[0].code,
      "ACCESS_DENIED",
    );
    await grant(env, m.model_id, bob.user, { role: "designer" });
    const visible = call(s, "cad_list_models", {}, bob);
    assert.deepEqual(
      visible.models.map((m: any) => m.model_id),
      [m.model_id],
    );
    const access = call(
      s,
      "cad_access",
      { mode: "inspect", model_id: m.model_id },
      bob,
    );
    assert.equal(access.role, "designer");
    assert.deepEqual(access.members, []);
    assert.equal(
      s.call({ ...bob, scopes: ["model:read"] }, "cad_apply_patch", radius(m))
        .errors[0].code,
      "ACCESS_DENIED",
    );
    const draft = call(s, "cad_apply_patch", radius(m), bob);
    await finish(s, draft, bob);
    const binding = {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
    };
    const proof = await finish(
      s,
      call(
        s,
        "cad_validate",
        { ...binding, idempotency_key: id("validate") },
        bob,
      ),
      bob,
    );
    const commit = {
      ...binding,
      validation_digest: proof.digest,
      idempotency_key: id("commit"),
    };
    assert.equal(
      s.call(bob, "cad_commit", commit).errors[0].code,
      "ACCESS_DENIED",
    );
    await grant(env, m.model_id, reviewer.user, {
      role: "reviewer",
      budget: { jobs: 0, seconds_per_job: 45 },
    });
    assert.equal(
      s.call(reviewer, "cad_apply_patch", radius(m)).errors[0].code,
      "ACCESS_DENIED",
    );
    const done = call(s, "cad_commit", commit, reviewer);
    assert.equal(
      call(
        s,
        "cad_measure",
        { model_id: m.model_id, feature_id: "sphere", metric: "radius" },
        bob,
      ).measurements,
      11,
    );
    const exp = {
      model_id: m.model_id,
      revision: done.revision,
      format: "step",
      idempotency_key: id("export"),
    };
    assert.equal(
      s.call(bob, "cad_export", exp).errors[0].code,
      "ACCESS_DENIED",
    );
    await grant(env, m.model_id, bob.user, {
      role: "reader",
      can_export: true,
    });
    const exported = await finish(s, call(s, "cad_export", exp, bob), bob);
    assert.ok(exported.package_manifest);
    assert.ok(s.store.getArtifact(bob, exported.artifacts[0].artifact_id));
    const selected = call(
      s,
      "cad_find",
      { model_id: m.model_id, query: "Kugel" },
      bob,
    ).matches[0];
    assert.equal(
      s.call(reviewer, "cad_inspect", {
        model_id: m.model_id,
        selection_handle: selected.selection_handle,
      }).errors[0].code,
      "ACCESS_DENIED",
    );
    assert.equal(
      s.call({ ...bob, tenant: "foreign" }, "cad_get_model", {
        model_id: m.model_id,
      }).errors[0].code,
      "ACCESS_DENIED",
    );
  } finally {
    await env.close();
  }
});

test("feature grants cover dependent geometry and reject wider changes without reserving work", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, housing);
    await grant(env, m.model_id, bob.user, {
      edit_scope: { kind: "features", feature_ids: ["feat-groove-07"] },
    });
    const request = {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("patch"),
      operations: [
        {
          op: "set_parameter",
          feature_id: "feat-groove-07",
          parameter: "depth",
          expected: { value: "0.80", unit: "mm" },
          value: { value: "0.82", unit: "mm" },
        },
      ],
    };
    const before = s.store.get("SELECT COUNT(*) AS n FROM jobs").n;
    assert.equal(
      s.call(bob, "cad_apply_patch", request).errors[0].code,
      "NEEDS_APPROVAL",
    );
    assert.equal(s.store.get("SELECT COUNT(*) AS n FROM jobs").n, before);
    assert.equal(
      call(s, "cad_access", { mode: "inspect", model_id: m.model_id }, bob)
        .own_grant.used_jobs,
      0,
    );
    await grant(env, m.model_id, bob.user, {
      edit_scope: {
        kind: "features",
        feature_ids: ["feat-groove-07", "feat-hole-01"],
      },
    });
    const draft = call(s, "cad_apply_patch", request, bob);
    await finish(s, draft, bob);
    const proof = await finish(
      s,
      call(
        s,
        "cad_validate",
        {
          model_id: m.model_id,
          base_revision: m.revision,
          transaction_id: draft.transaction_id,
          idempotency_key: id("validate"),
        },
        bob,
      ),
      bob,
    );
    call(
      s,
      "cad_commit",
      {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: draft.transaction_id,
        validation_digest: proof.digest,
        idempotency_key: id("commit"),
      },
      bob,
    );
    const current = s.store.revision(principal, m.model_id);
    assert.deepEqual(current.ir.features[0], housing.features[0]);
    const broad = {
      model_id: m.model_id,
      base_revision: current.id,
      idempotency_key: id("outside"),
      operations: [
        {
          op: "set_parameter",
          feature_id: "feat-base",
          parameter: "height",
          expected: { value: "3", unit: "mm" },
          value: { value: "4", unit: "mm" },
        },
      ],
    };
    assert.equal(
      s.call(bob, "cad_apply_patch", broad).errors[0].code,
      "NEEDS_APPROVAL",
    );
  } finally {
    await env.close();
  }
});

test("revocation fences existing candidates, handles, artifacts, resources and idempotent replays", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    await grant(env, m.model_id);
    const request = radius(m),
      draft = call(s, "cad_apply_patch", request, bob);
    await finish(s, draft, bob);
    const binding = {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
    };
    const proof = await finish(
      s,
      call(
        s,
        "cad_validate",
        { ...binding, idempotency_key: id("validate") },
        bob,
      ),
      bob,
    );
    const preview = await finish(
      s,
      call(
        s,
        "cad_render",
        {
          model_id: m.model_id,
          revision: m.revision,
          idempotency_key: id("render"),
        },
        bob,
      ),
      bob,
    );
    const handle = call(
      s,
      "cad_inspect",
      { model_id: m.model_id, feature_id: "sphere" },
      bob,
    ).selection_handle;
    await approve(
      env,
      propose(env, m.model_id, { action: "revoke", recipient: bob.user }),
    );
    assert.equal(call(s, "cad_list_models", {}, bob).total, 0);
    assert.equal(
      s.store.transaction(principal, draft.transaction_id).state,
      "aborted",
    );
    for (const [tool, args] of [
      ["cad_apply_patch", request],
      ["cad_job_get", { job_id: draft.job_id }],
      ["cad_inspect", { model_id: m.model_id, selection_handle: handle }],
    ] as const)
      assert.equal(s.call(bob, tool, args).errors[0].code, "ACCESS_DENIED");
    assert.throws(
      () => s.store.getArtifact(bob, preview.artifacts[0].artifact_id),
      (e: any) => e.code === "ACCESS_DENIED",
    );
    assert.throws(
      () =>
        s.resource(
          bob,
          `cad://transactions/${draft.transaction_id}/validation`,
        ),
      (e: any) => e.code === "ACCESS_DENIED",
    );
    await grant(env, m.model_id);
    assert.equal(
      s.call(bob, "cad_commit", {
        ...binding,
        validation_digest: proof.digest,
        idempotency_key: id("commit"),
      }).errors[0].code,
      "ACCESS_DENIED",
    );
    assert.equal(s.store.model(principal, m.model_id).head, m.revision);
  } finally {
    await env.close();
  }
});

test("project job budgets include duplicate protection and synchronous IR exports", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, sphere);
    await grant(env, m.model_id, bob.user, {
      can_export: true,
      budget: { jobs: 1, seconds_per_job: 45 },
    });
    const request = {
      model_id: m.model_id,
      revision: m.revision,
      format: "ir",
      idempotency_key: id("export"),
    };
    const exported = call(s, "cad_export", request, bob);
    assert.deepEqual(
      call(s, "cad_export", request, bob).artifacts,
      exported.artifacts,
    );
    const counts = {
      jobs: s.store.get("SELECT COUNT(*) AS n FROM jobs").n,
      artifacts: s.store.get("SELECT COUNT(*) AS n FROM artifacts").n,
    };
    assert.equal(
      call(s, "cad_access", { mode: "inspect", model_id: m.model_id }, bob)
        .own_grant.used_jobs,
      1,
    );
    assert.equal(
      s.call(bob, "cad_export", { ...request, idempotency_key: id("export") })
        .errors[0].code,
      "NEEDS_APPROVAL",
    );
    assert.equal(
      s.call(bob, "cad_render", {
        model_id: m.model_id,
        idempotency_key: id("render"),
      }).errors[0].code,
      "NEEDS_APPROVAL",
    );
    assert.equal(s.store.get("SELECT COUNT(*) AS n FROM jobs").n, counts.jobs);
    assert.equal(
      s.store.get("SELECT COUNT(*) AS n FROM artifacts").n,
      counts.artifacts,
    );
  } finally {
    await env.close();
  }
});

test("revocation terminates a live isolated worker and prevents publication", async () => {
  const env = setup(),
    s = env.service,
    run = Worker.prototype.run;
  let workerPID = 0,
    wake!: () => void;
  const started = new Promise<void>((resolve) => {
    wake = resolve;
  });
  try {
    const m = await importFixture(s, sphere);
    await grant(env, m.model_id);
    Worker.prototype.run = function (...args) {
      const result = run.apply(this, args);
      if (this.process?.pid) {
        workerPID = this.process.pid;
        process.kill(workerPID, "SIGSTOP");
        wake();
      }
      return result;
    };
    const draft = call(s, "cad_apply_patch", radius(m), bob);
    let startTimer: NodeJS.Timeout | undefined;
    try {
      await Promise.race([
        started,
        new Promise<void>((_, reject) => {
          startTimer = setTimeout(
            () => reject(new Error("Worker did not start")),
            10000,
          );
        }),
      ]);
    } finally {
      clearTimeout(startTimer);
    }
    process.kill(workerPID, 0);
    await approve(
      env,
      propose(env, m.model_id, { action: "revoke", recipient: bob.user }),
    );
    const job = await s.jobs.wait(principal, draft.job_id);
    assert.equal(job.status, "cancelled");
    await s.jobs.close();
    assert.throws(
      () => process.kill(workerPID, 0),
      (e: any) => e.code === "ESRCH",
    );
    assert.equal(s.store.model(principal, m.model_id).head, m.revision);
    assert.equal(
      s.store.transaction(principal, draft.transaction_id).state,
      "aborted",
    );
    assert.equal(
      s.store.get(
        "SELECT COUNT(*) AS n FROM revisions WHERE id=?",
        draft.candidate_revision,
      ).n,
      0,
    );
  } finally {
    Worker.prototype.run = run;
    if (workerPID) {
      try {
        process.kill(workerPID, "SIGKILL");
      } catch {}
    }
    await env.close();
  }
});
