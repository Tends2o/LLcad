import { requireThat } from "../../packages/semantic-ir/errors.js";
export const MANDATORY = [
  "before_request",
  "before_import",
  "before_compile",
  "after_compile",
  "before_resolve",
  "after_resolve",
  "before_execute",
  "after_execute",
  "before_validate",
  "after_validate",
  "before_commit",
  "after_commit",
  "before_export",
  "after_export",
] as const;
export type Hook = (typeof MANDATORY)[number];
/** Static operator-owned gates. Never loaded from model data or request arguments. */
export class Gates {
  private enabled = new Set<string>(MANDATORY);
  constructor(
    private recorder: (event: string, data: Record<string, unknown>) => void,
  ) {}
  run(
    name: Hook,
    context: Record<string, unknown>,
    check: () => void = () => {},
  ) {
    requireThat(
      this.enabled.has(name),
      "POLICY_GATE_FAILED",
      `Pflichtprüfung ${name} fehlt.`,
    );
    const start = performance.now();
    check();
    requireThat(
      performance.now() - start < 1000,
      "POLICY_GATE_FAILED",
      "Pflichtprüfung hat das Zeitbudget überschritten.",
    );
    this.recorder(name, context);
  }
  /** Operator fault injection for fail-closed tests, inaccessible through MCP. */
  disableForTest(name: Hook) {
    this.enabled.delete(name);
  }
}
