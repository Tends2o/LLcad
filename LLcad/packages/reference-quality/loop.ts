/** Bounded, fail-closed reference verification. Adapters own cancellable work. */
export type Issue = {
  id: string;
  metric: string;
  measured: number;
  limit: number;
};
export type Inspection = {
  complete: boolean;
  geometry_passed: boolean;
  issues: Issue[];
  evidence: string;
};
export interface Adapter<State> {
  inspect(state: State, signal: AbortSignal): Promise<Inspection>;
  correct(state: State, issues: Issue[], signal: AbortSignal): Promise<State>;
  accept(state: State, signal: AbortSignal): Promise<State>;
  cleanup(): Promise<void>;
}
export type RunReport<State> = {
  status: "verified_within_contract" | "not_verified";
  reason: string;
  rounds: number;
  elapsed_ms: number;
  state: State;
  inspections: Inspection[];
  timings: { stage: string; elapsed_ms: number }[];
  cleanup_error?: string;
};

export async function verifyAndCorrect<State>(
  initial: State,
  adapter: Adapter<State>,
  options: { maxRounds?: number; budgetMs?: number; signal?: AbortSignal } = {},
): Promise<RunReport<State>> {
  const maxRounds = options.maxRounds ?? 3;
  const budgetMs = options.budgetMs ?? 900_000;
  if (
    !Number.isInteger(maxRounds) ||
    maxRounds < 0 ||
    maxRounds > 3 ||
    !Number.isInteger(budgetMs) ||
    budgetMs < 1 ||
    budgetMs > 900_000
  )
    throw new Error("Invalid bounds: 0–3 rounds and 1–900000 ms required");
  const started = performance.now();
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(new Error("budget_exhausted")),
    budgetMs,
  );
  const signal = options.signal
    ? AbortSignal.any([controller.signal, options.signal])
    : controller.signal;
  const report: RunReport<State> = {
    status: "not_verified",
    reason: "not_started",
    rounds: 0,
    elapsed_ms: 0,
    state: initial,
    inspections: [],
    timings: [],
  };
  const guard = () => {
    signal.throwIfAborted();
    if (performance.now() - started >= budgetMs)
      throw new Error("budget_exhausted");
  };
  const stage = async <T>(name: string, work: () => Promise<T>): Promise<T> => {
    guard();
    const begin = performance.now();
    try {
      const result = await work();
      guard();
      return result;
    } finally {
      report.timings.push({
        stage: name,
        elapsed_ms: performance.now() - begin,
      });
    }
  };
  const signatures = new Set<string>();
  try {
    while (true) {
      const check = await stage("inspect", () =>
        adapter.inspect(report.state, signal),
      );
      report.inspections.push(check);
      if (
        check.complete !== true ||
        typeof check.evidence !== "string" ||
        !check.evidence ||
        check.geometry_passed !== true ||
        !Array.isArray(check.issues) ||
        check.issues.some(
          (i) =>
            !i.id || !Number.isFinite(i.measured) || !Number.isFinite(i.limit),
        )
      ) {
        report.reason = "incomplete_or_failed_check";
        break;
      }
      if (check.issues.length === 0) {
        report.state = await stage("accept", () =>
          adapter.accept(report.state, signal),
        );
        report.status = "verified_within_contract";
        report.reason = "all_declared_checks_passed";
        break;
      }
      const signature = JSON.stringify(
        check.issues.map((i) => [i.id, i.metric, i.measured, i.limit]).sort(),
      );
      if (signatures.has(signature)) {
        report.reason = "no_progress";
        break;
      }
      signatures.add(signature);
      if (report.rounds >= maxRounds) {
        report.reason = "round_limit";
        break;
      }
      report.rounds++;
      report.state = await stage("correct", () =>
        adapter.correct(report.state, check.issues, signal),
      );
    }
  } catch (error) {
    report.reason = signal.aborted
      ? String(signal.reason?.message ?? "cancelled")
      : String(error instanceof Error ? error.message : error);
  } finally {
    clearTimeout(timer);
    try {
      await adapter.cleanup();
    } catch (error) {
      report.cleanup_error = String(error);
      report.status = "not_verified";
      report.reason = "cleanup_failed";
    }
    report.elapsed_ms = performance.now() - started;
    if (
      report.elapsed_ms >= budgetMs &&
      report.status === "verified_within_contract"
    ) {
      report.status = "not_verified";
      report.reason = "budget_exhausted";
    }
  }
  return report;
}
