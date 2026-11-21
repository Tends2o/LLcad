import { requireThat } from "../semantic-ir/errors.js";
import type { Store } from "../model-service/store.js";
import type { Principal } from "../policy/index.js";
import { readColours } from "../model-service/colours.js";
import { hash } from "../semantic-ir/hash.js";
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { bytesHash } from "../semantic-ir/hash.js";
import { BASE_SCOPE, type Summary } from "./schema.js";
import { DEFAULT_JUDGE, type JudgeConfig } from "./judge.js";

/** Acceptance rule of a frozen contract. Contracts frozen before the policy
 *  existed carry no field and keep the rule they were made under (two_vlm). */
export function acceptanceOf(contract: any): "gates_judge" | "two_vlm" {
  return contract?.acceptance ?? "two_vlm";
}
export function judgeOf(contract: any): JudgeConfig {
  return { ...DEFAULT_JUDGE, ...(contract?.judge ?? {}) };
}

export function captureBuildHash() {
  const p = resolve(
    process.env.MATHFORGE_ROOT ?? process.cwd(),
    "public/app.js",
  );
  return existsSync(p) ? bytesHash(readFileSync(p)) : hash("viewer_missing");
}
export function fingerprint(
  store: Store,
  p: Principal,
  model: string,
  revision: string,
  contractHash: string,
) {
  const r = store.revision(p, model, revision);
  return {
    contract_hash: contractHash,
    ir_hash: r.ir_hash,
    geometry_hash: hash(r.geometry?.facts ?? null),
    appearance_hash: hash(readColours(store, p, model, revision).parts),
    renderer_hash: captureBuildHash(),
  };
}
export function nativeFingerprint(store: Store, p: Principal, model: string, revision: string, contract: any) {
  const r = store.revision(p, model, revision);
  return hash({ ir_hash: r.ir_hash, geometry_hash: hash(r.geometry?.facts ?? null), engine_build: r.geometry?.engine_build ?? null, dimensions: contract.dimensions, contacts: contract.contacts });
}
export function referenceState(
  store: Store,
  p: Principal,
  model: string,
  revision: string,
): { summary: Summary; contract: any; run: any } {
  store.model(p, model);
  const empty: Summary = {
    status: "not_configured",
    contract_hash: null,
    run_id: null,
    required: 0,
    matched: 0,
    mismatched: 0,
    unknown: 0,
    exact_replica_certified: false,
    scope: BASE_SCOPE,
    report_artifact_id: null,
  };
  // Read-only callers and legacy unit fixtures do not create tables.
  if (
    !store.get(
      "SELECT name FROM sqlite_master WHERE type='table' AND name='reference_contracts'",
    )
  )
    return { summary: empty, contract: null, run: null };
  const row = store.get(
    "SELECT contract FROM reference_contracts WHERE model=?",
    model,
  );
  if (!row) return { summary: empty, contract: null, run: null };
  const contract = JSON.parse(row.contract),
    fp = hash(fingerprint(store, p, model, revision, contract.hash));
  const exact = store.get(
    "SELECT record FROM reference_runs WHERE model=? AND fingerprint=? AND phase='compare' ORDER BY created DESC LIMIT 1",
    model,
    fp,
  );
  const recent =
    exact ??
    store.get(
      "SELECT record FROM reference_runs WHERE model=? AND phase='compare' ORDER BY created DESC LIMIT 1",
      model,
    );
  const run = recent ? JSON.parse(recent.record) : null;
  const required = contract.requirements.filter((r: any) => r.required).length;
  let status: Summary["status"] = !run
    ? "not_checked"
    : !exact
      ? "stale"
      : ["queued", "running"].includes(run.state)
        ? "running"
        : run.state === "failed"
          ? "failed"
          : (run.summary?.status ?? "insufficient_evidence");
  return {
    contract,
    run,
    summary: {
      ...empty,
      ...(exact && run?.summary ? run.summary : {}),
      status,
      contract_hash: contract.hash,
      run_id: run?.id ?? null,
      required,
      unknown: exact && run?.summary ? run.summary.unknown : required,
      scope: contract.scope,
      report_artifact_id: exact ? (run?.report_artifact_id ?? null) : null,
    },
  };
}

export function adjudicate(
  requirements: any[],
  reviews: any[],
  native: any[],
  additionalIssues: number,
  minimumConfidence: number,
) {
  const findings = requirements.map((req) => {
    const observations = reviews.map((review) => ({
      model: review.model,
      findings: review.result.findings.filter(
        (f: any) => f.requirement_id === req.id,
      ),
    }));
    const evidence = observations.flatMap((o) =>
      o.findings.map((f: any) => ({ ...f, model: o.model })),
    );
    const sourceUnknown = evidence.some(
      (f: any) => f.reference_support !== "supported",
    );
    const mismatch =
      !sourceUnknown && evidence.some((f: any) => f.verdict === "mismatch");
    const matched =
      new Set(reviews.map((r) => r.model)).size >= 2 &&
      observations.every(
        (o) =>
          o.findings.length === 1 &&
          o.findings[0].reference_support === "supported" &&
          o.findings[0].verdict === "match" &&
          o.findings[0].confidence >= minimumConfidence,
      );
    return {
      id: req.id,
      description: req.description,
      category: req.category,
      required: req.required,
      verdict: mismatch ? "mismatch" : matched ? "match" : "uncertain",
      observations: evidence,
    };
  });
  const required = findings.filter((f) => f.required),
    mismatched = required.filter((f) => f.verdict === "mismatch").length,
    unknown = required.filter((f) => f.verdict === "uncertain").length;
  const nativeFailed = native.some((c) => c.status === "failed"),
    nativeUnknown = native.some((c) => c.status === "unknown");
  const status =
    mismatched || nativeFailed || additionalIssues
      ? "needs_correction"
      : unknown || nativeUnknown || !required.length || reviews.length < 2
        ? "insufficient_evidence"
        : "matches_declared_visible_checks";
  return {
    findings,
    counts: {
      status,
      required: required.length,
      matched: required.filter((f) => f.verdict === "match").length,
      mismatched,
      unknown,
      exact_replica_certified: false,
    },
  };
}

export function assertReferenceExport(
  store: Store,
  p: Principal,
  model: string,
  revision: string,
  policy: "require_pass" | "draft" = "require_pass",
) {
  const { summary, contract, run } = referenceState(store, p, model, revision);
  // The pass condition follows the contract's acceptance rule. gates_judge: the
  // exact revision's run passed the deterministic cad_gates floor and has no
  // confirmed or unaccepted-unconfirmed critical judge finding; two_vlm: two
  // models matched every required item. Either way only an exact, current run
  // with status matches_declared_visible_checks counts.
  const acceptance = acceptanceOf(contract);
  const passed =
    summary.status === "matches_declared_visible_checks" &&
    (acceptance !== "gates_judge" ||
      (run?.summary?.floor_passed === true && (run?.summary?.open_findings ?? 0) === 0));
  requireThat(
    !contract || policy === "draft" || passed,
    "REFERENCE_REVIEW_REQUIRED",
    acceptance === "gates_judge"
      ? "A reference-bound export needs a current review with a passed cad_gates floor and without open critical judge findings (fix confirmed ones, fix unconfirmed ones or record them as accepted_assumptions); an explicitly chosen draft can be exported as reference_policy draft."
      : "A reference-bound export needs passed current visual reviews. Fix open findings first; an explicitly chosen draft can be exported as reference_policy draft.",
    {
      reference_status: summary.status,
      run_id: summary.run_id,
      acceptance,
      ...(acceptance === "gates_judge"
        ? { floor_passed: run?.summary?.floor_passed ?? null, open_findings: run?.summary?.open_findings ?? null }
        : {}),
    },
  );
}
