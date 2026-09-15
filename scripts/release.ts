import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { REGISTRY_HASH } from "../packages/compiler/index.js";
import { BUILD_HASH, IMPLEMENTATION_HASH } from "../packages/compiler/build.js";
import { POLICY_HASH } from "../packages/policy/index.js";
import { bytesHash } from "../packages/semantic-ir/hash.js";
import { auditPlan } from "./plan-audit.js";
const read = (path: string) =>
  existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : null;
const verification = read("reports/verification.json"),
  benchmark = read("reports/benchmark.json"),
  host = read("reports/target-host.json"),
  localHost = read("reports/codex-host.json"),
  llm = read("reports/llm-eval.json"),
  audit = read("reports/npm-audit.json"),
  pythonAudit = read("reports/python-audit.json");
const blockers = [];
const completionAudit = auditPlan();
if (completionAudit.status !== "verified")
  blockers.push(
    "Vollständiger Anforderungsabgleich zum Originalplan enthält offene oder unzureichend belegte Punkte.",
  );
if (
  verification?.status !== "passed" ||
  verification.registry_hash !== REGISTRY_HASH ||
  verification.implementation_hash !== IMPLEMENTATION_HASH
)
  blockers.push("Aktueller vollständiger lokaler Prüflauf fehlt.");
if (
  !benchmark ||
  benchmark.registry_hash !== REGISTRY_HASH ||
  benchmark.implementation_hash !== IMPLEMENTATION_HASH
)
  blockers.push("Benchmark für diesen Build fehlt.");
if (
  host?.status !== "passed" ||
  host.registry_hash !== REGISTRY_HASH ||
  host.implementation_hash !== IMPLEMENTATION_HASH
)
  blockers.push(
    "Vollständige LLM-/Remote-Zielhostabnahme fehlt; der lokale Codex-Transporttest ist gesondert dokumentiert.",
  );
if ((audit?.metadata?.vulnerabilities?.total ?? 1) > 0)
  blockers.push("JavaScript-Schwachstellenprüfung fehlt oder enthält Befunde.");
if (!pythonAudit || pythonAudit.dependencies?.some((d: any) => d.vulns?.length))
  blockers.push("Python-Schwachstellenprüfung fehlt oder enthält Befunde.");
if (
  !process.env.MATHFORGE_ISSUER ||
  !process.env.MATHFORGE_PUBLIC_URL?.startsWith("https://")
)
  blockers.push(
    "Produktiver HTTPS-Endpunkt und Identitätsanbieter sind nicht konfiguriert.",
  );
const manifest = {
  application_version: "0.1.0",
  ir_schema_version: "1",
  created: new Date().toISOString(),
  status: blockers.length ? "not_released" : "eligible_for_operator_release",
  operator_registry_hash: REGISTRY_HASH,
  worker_source_digest: BUILD_HASH,
  implementation_digest: IMPLEMENTATION_HASH,
  worker_image_digests: null,
  policy_bundle_hash: POLICY_HASH,
  dependency_lock_hashes: {
    npm: bytesHash(readFileSync("package-lock.json")),
    python: bytesHash(readFileSync("requirements.lock")),
  },
  supported_quality_profiles: [
    "precision_cad",
    "render_surface",
    "watertight_solid",
  ],
  protocol_compatibility_tests: "reports/tests.log",
  target_host_tests: host ?? { status: "not_run" },
  local_llm_tests: {
    report: "reports/llm-eval.json",
    status:
      llm?.status === "passed" &&
      llm.registry_hash === REGISTRY_HASH &&
      llm.implementation_hash === IMPLEMENTATION_HASH
        ? "passed"
        : "missing_failed_or_stale",
    model_turns: llm?.model_turns ?? 0,
    scope: llm?.scope ?? null,
  },
  local_codex_transport_tests: {
    report: "reports/codex-host.json",
    status:
      localHost?.status === "passed" &&
      localHost.registry_hash === REGISTRY_HASH &&
      localHost.implementation_hash === IMPLEMENTATION_HASH
        ? "passed"
        : "missing_or_stale",
    model_turns: localHost?.model_turns ?? null,
  },
  benchmark_report: "reports/benchmark.json",
  security_test_report: "reports/tests.log",
  restore_test_report: "reports/tests.log",
  blockers,
  completion_audit: completionAudit,
};
writeFileSync(
  "reports/release-manifest.json",
  JSON.stringify(manifest, null, 2) + "\n",
);
console.log(JSON.stringify(manifest, null, 2));
if (blockers.length) process.exitCode = 2;
