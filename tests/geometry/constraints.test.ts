import test from "node:test";
import assert from "node:assert/strict";
import { setup, call, finish, importFixture, principal } from "../helpers.js";
import { ModelIR } from "../../packages/semantic-ir/schema.js";
import { id } from "../../packages/semantic-ir/hash.js";
const q = (value: string, unit = "mm") => ({ value, unit });
const param = (parameter: string) => ({ parameter });
const constant = (value: string) => ({ constant: q(value) });
const fn = (fn: string, ...args: any[]) => ({ fn, args });
const ir = ModelIR.parse({
  schema_version: "1",
  unit: "mm",
  features: [
    {
      id: "plate",
      semantic_name: "Platte",
      kind: "box",
      construction: { operator: "box" },
      parameters: { width: q("10"), depth: q("5"), height: q("2") },
    },
  ],
  outputs: ["plate"],
});
const problem = () => ({
  variables: [
    {
      name: "w",
      feature_id: "plate",
      parameter: "width",
      expected: q("10"),
      lower: q("7"),
      upper: q("12"),
    },
    {
      name: "d",
      feature_id: "plate",
      parameter: "depth",
      expected: q("5"),
      lower: q("3"),
      upper: q("7"),
    },
  ],
  equations: [
    {
      id: "area-48",
      relation: "eq",
      tolerance: "0.00000001",
      expression: fn(
        "-",
        fn(
          "/",
          fn("*", param("w"), param("d")),
          fn("*", constant("8"), constant("6")),
        ),
        { constant: q("1", "1") },
      ),
    },
    {
      id: "sum-14",
      relation: "eq",
      tolerance: "0.00000001",
      expression: fn(
        "-",
        fn("/", fn("+", param("w"), param("d")), constant("14")),
        { constant: q("1", "1") },
      ),
    },
    {
      id: "width-greater",
      relation: "ge",
      tolerance: "0.00000001",
      expression: fn("-", fn("/", param("w"), param("d")), {
        constant: q("1", "1"),
      }),
    },
  ],
});
test("coupled nonlinear constraints produce a checked proposal and persistent relationships", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, ir);
    const solved = await finish(
      s,
      call(s, "cad_solve_constraints", {
        model_id: m.model_id,
        base_revision: m.revision,
        problem: problem(),
        idempotency_key: id("solve"),
      }),
    );
    assert.equal(solved.solver.global_optimum_claimed, false);
    assert.equal(
      call(s, "cad_get_model", { model_id: m.model_id }).revision,
      m.revision,
    );
    const draft = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      operations: solved.operations,
      idempotency_key: id("apply"),
    });
    const candidate = await finish(s, draft);
    assert.ok(Math.abs(candidate.measurements.volume - 96) < 1e-6);
    const tx = {
      model_id: m.model_id,
      base_revision: m.revision,
      transaction_id: draft.transaction_id,
    };
    const validation = await finish(
      s,
      call(s, "cad_validate", { ...tx, idempotency_key: id("validate") }),
    );
    assert.equal(validation.status, "checks_passed_within_profile");
    const committed = call(s, "cad_commit", {
      ...tx,
      validation_digest: validation.digest,
      idempotency_key: id("commit"),
    });
    const detail = call(s, "cad_inspect", {
      model_id: m.model_id,
      feature_id: "plate",
    });
    assert.ok(Math.abs(Number(detail.parameters.width.value) - 8) < 1e-6);
    assert.equal(detail.protected_constraints.length, 3);
    const broken = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: committed.revision,
      operations: [
        {
          op: "set_parameter",
          feature_id: "plate",
          parameter: "width",
          expected: detail.parameters.width,
          value: q("9"),
        },
      ],
      idempotency_key: id("break"),
    });
    await finish(s, broken);
    const invalid = await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: committed.revision,
        transaction_id: broken.transaction_id,
        idempotency_key: id("invalid"),
      }),
    );
    assert.equal(invalid.status, "failed");
  } finally {
    await env.close();
  }
});
test("solver rejects infeasible systems, wrong dimensions and foreign or protected parameters", async () => {
  const env = setup(),
    s = env.service;
  try {
    const m = await importFixture(s, ir),
      args = {
        model_id: m.model_id,
        base_revision: m.revision,
        problem: problem(),
        idempotency_key: id("solve"),
      };
    assert.equal(
      s.call({ ...principal, user: "bob" }, "cad_solve_constraints", args)
        .errors[0].code,
      "ACCESS_DENIED",
    );
    const dimensional = problem();
    dimensional.equations[0].expression = param("w") as any;
    assert.equal(
      s.call(principal, "cad_solve_constraints", {
        ...args,
        problem: dimensional,
      }).errors[0].code,
      "UNIT_MISMATCH",
    );
    const impossible = problem();
    impossible.equations[1].expression = fn(
      "-",
      fn("/", fn("+", param("w"), param("d")), constant("8")),
      { constant: q("1", "1") },
    );
    const draft = call(s, "cad_solve_constraints", {
      ...args,
      problem: impossible,
    });
    const failed = await s.jobs.wait(principal, draft.job_id);
    assert.equal(failed.status, "failed");
    assert.equal(failed.error.code, "CONSTRAINT_CONFLICT");
    const protectedModel = await importFixture(s, {
      ...ir,
      constraints: [
        {
          id: "fixed",
          kind: "protected_parameter",
          feature_id: "plate",
          parameter: "width",
        },
      ],
    });
    assert.equal(
      s.call(principal, "cad_solve_constraints", {
        ...args,
        model_id: protectedModel.model_id,
        base_revision: protectedModel.revision,
        idempotency_key: id("protected"),
      }).errors[0].code,
      "OUT_OF_SCOPE",
    );
  } finally {
    await env.close();
  }
});
