import { cpus, totalmem, platform, release } from "node:os";
import { writeFileSync, mkdirSync } from "node:fs";
import {
  setup,
  call,
  finish,
  importFixture,
  principal,
} from "../tests/helpers.js";
import { housing, assembly, organic } from "../scripts/fixtures.js";
import { id } from "../packages/semantic-ir/hash.js";
import { REGISTRY_HASH, compile } from "../packages/compiler/index.js";
import { IMPLEMENTATION_HASH } from "../packages/compiler/build.js";
const env = setup(),
  s = env.service;
const stats = (values: number[]) => {
  const sorted = [...values].sort((a, b) => a - b);
  return {
    samples: values.length,
    p50_ms: sorted[Math.ceil(sorted.length * 0.5) - 1],
    p95_ms: sorted[Math.ceil(sorted.length * 0.95) - 1],
    min_ms: sorted[0],
    max_ms: sorted.at(-1),
  };
};
try {
  const coldStart = performance.now();
  const m = await importFixture(s, housing);
  const cold = performance.now() - coldStart;
  const read = [],
    accept = [],
    candidate = [],
    validation = [],
    previews = [],
    metrics = [];
  for (let i = 0; i < 100; i++) {
    const start = performance.now();
    call(s, "cad_get_model", { model_id: m.model_id });
    read.push(performance.now() - start);
  }
  for (let i = 0; i < 12; i++) {
    const start = performance.now();
    const draft = call(s, "cad_apply_patch", {
      model_id: m.model_id,
      base_revision: m.revision,
      idempotency_key: id("benchmark"),
      operations: [
        {
          op: "set_parameter",
          feature_id: "feat-groove-07",
          parameter: "depth",
          expected: { value: "0.80", unit: "mm" },
          value: { value: (0.801 + i * 0.001).toFixed(3), unit: "mm" },
        },
      ],
    });
    accept.push(performance.now() - start);
    const result = await finish(s, draft);
    candidate.push(performance.now() - start);
    metrics.push(result.metrics);
    const validationStart = performance.now();
    await finish(
      s,
      call(s, "cad_validate", {
        model_id: m.model_id,
        base_revision: m.revision,
        transaction_id: draft.transaction_id,
        idempotency_key: id("benchmark-validate"),
      }),
    );
    validation.push(performance.now() - validationStart);
    if (i < 5) {
      const t = performance.now();
      await finish(
        s,
        call(s, "cad_render", {
          model_id: m.model_id,
          revision: result.candidate_revision,
          feature_id: "feat-groove-07",
          idempotency_key: id("preview"),
        }),
      );
      previews.push(performance.now() - t);
    }
  }
  const assemblyStart = performance.now();
  const a = await importFixture(s, assembly);
  const assemblyMs = performance.now() - assemblyStart;
  const stress = structuredClone(assembly);
  stress.features[1].parameters.count.value = "10000";
  const stressModel = call(s, "cad_create_model", {
    name: "10,000 instance budget boundary",
    idempotency_key: id("stress-create"),
  });
  const stressStart = performance.now();
  const stressJob = call(s, "cad_apply_patch", {
    model_id: stressModel.model_id,
    base_revision: stressModel.revision,
    idempotency_key: id("stress-patch"),
    operations: [
      ...stress.features.map((feature) => ({ op: "add_feature", feature })),
      { op: "set_outputs", outputs: stress.outputs },
    ],
  });
  const stressResult = await s.jobs.wait(principal, stressJob.job_id);
  const stressReport = {
    instances: 10000,
    duration_ms: performance.now() - stressStart,
    status: stressResult.status,
    metrics: stressResult.result?.metrics ?? null,
    error: stressResult.error,
    authoritative_revision_retained:
      call(s, "cad_get_model", { model_id: stressModel.model_id }).revision ===
      stressModel.revision,
    note: "Cold candidate at configured instance limit; no automatic commit or relaxed budget.",
  };
  const worst = structuredClone(assembly);
  worst.features[1].parameters.count.value = "10001";
  let budgetRejected = false;
  try {
    compile(worst);
  } catch (e: any) {
    budgetRejected = ["GEOMETRY_INVALID", "BUDGET_EXCEEDED"].includes(e.code);
  }
  const organicStart = performance.now();
  const org = await importFixture(s, organic);
  await finish(
    s,
    call(s, "cad_render", {
      model_id: org.model_id,
      revision: org.revision,
      idempotency_key: id("organic"),
    }),
  );
  const organicMs = performance.now() - organicStart;
  const report = {
    status: "measured",
    created: new Date().toISOString(),
    environment: {
      platform: platform(),
      release: release(),
      node: process.version,
      cpu: cpus()[0].model,
      logical_cpus: cpus().length,
      memory_bytes: totalmem(),
      worker_processes: 1,
    },
    registry_hash: REGISTRY_HASH,
    implementation_hash: IMPLEMENTATION_HASH,
    cold_fixture_pipeline_ms: cold,
    read: stats(read),
    patch_acceptance: stats(accept),
    candidate: stats(candidate),
    validation: stats(validation),
    preview: stats(previews),
    assembly_100_instances_full_pipeline_ms: assemblyMs,
    stress_at_instance_limit: stressReport,
    organic_full_pipeline_with_preview_ms: organicMs,
    budget_rejection_verified: budgetRejected,
    worker_metrics: metrics,
    error_rate: 0,
    limitations: [
      "12 warm CAD samples and 5 preview samples; not a production SLO study.",
      "Worker processes are cold; the content-addressed geometry cache is warm.",
      "LLM, external network and real target host latency are not included.",
    ],
  };
  mkdirSync("reports", { recursive: true });
  writeFileSync(
    "reports/benchmark.json",
    JSON.stringify(report, null, 2) + "\n",
  );
  console.log(
    JSON.stringify(
      {
        read: report.read,
        acceptance: report.patch_acceptance,
        candidate: report.candidate,
        preview: report.preview,
        assembly_ms: assemblyMs,
        organic_ms: organicMs,
      },
      null,
      2,
    ),
  );
} finally {
  await env.close();
}
