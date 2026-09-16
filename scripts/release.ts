import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { REGISTRY_HASH } from "../packages/compiler/index.js";
import { BUILD_HASH, IMPLEMENTATION_HASH } from "../packages/compiler/build.js";
import { POLICY_HASH } from "../packages/policy/index.js";
import { bytesHash } from "../packages/semantic-ir/hash.js";
import { auditPlan } from "./plan-audit.js";
import { RETENTION_POLICY } from "../packages/model-service/maintenance.js";
import { PIPELINE_POLICY } from "../hooks/server-registry/index.js";
const read = (path: string) =>
  existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : null;
const licenses = read("reports/license-check.json");
const verification = read("reports/verification.json"),
  benchmark = read("reports/benchmark.json"),
  host = read("reports/target-host.json"),
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
  blockers.push("Vollständige LLM-/Remote-Zielhostabnahme fehlt.");
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
if (licenses?.distribution_review?.status !== "acknowledged")
  blockers.push(
    "Lizenz-/Distributionsprüfung fehlt oder verlangt eine Betreiberfreigabe (reports/license-check.json).",
  );
// The isolated worker is not an OCI image; its environment digest binds the exact
// mounted sources, pinned Python lock, native package pins and the checker binary.
const workerEnvironmentDigest = bytesHash(
  JSON.stringify({
    worker_source_digest: BUILD_HASH,
    requirements_lock: bytesHash(readFileSync("requirements.lock")),
    native_packages: bytesHash(readFileSync("deployment/native-packages.json")),
    meshcheck: JSON.parse(
      readFileSync("workers/cad-occt/.meshcheck-build.json", "utf8"),
    ).binary_sha256,
    sandbox: "bubblewrap_ro_usr_venv_worker_single_use",
  }),
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
  worker_environment_digest: workerEnvironmentDigest,
  worker_environment_note:
    "No container image is built; the digest binds mounted worker sources, the Python lock, native package pins and the mesh checker binary.",
  license_check: licenses
    ? {
        report: "reports/license-check.json",
        status: licenses.distribution_review.status,
        summary: licenses.summary,
      }
    : { status: "missing" },
  retention_policy: RETENTION_POLICY,
  pipeline_policy_version: PIPELINE_POLICY.version,
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
