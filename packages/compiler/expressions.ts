import { Decimal } from "decimal.js";
import { Quantity } from "../semantic-ir/schema.js";
import { quantity } from "../semantic-ir/units.js";
import { requireThat } from "../semantic-ir/errors.js";
export type Expression =
  | { constant: Quantity }
  | { parameter: string }
  | {
      fn:
        | "+"
        | "-"
        | "*"
        | "/"
        | "sqrt"
        | "sin"
        | "cos"
        | "atan2"
        | "min"
        | "max"
        | "abs"
        | "clamp";
      args: Expression[];
    };
type Value = { value: number; length: number; angle: number };
export function evaluate(
  expression: Expression,
  parameters: Record<string, Quantity>,
  depth = 0,
  state = { nodes: 0 },
): Value {
  requireThat(
    depth <= 32 && ++state.nodes <= 4096,
    "BUDGET_EXCEEDED",
    "Ausdruck überschreitet das Budget.",
  );
  if ("constant" in expression || "parameter" in expression) {
    const q =
      "constant" in expression
        ? expression.constant
        : parameters[expression.parameter];
    requireThat(q, "INVALID_SCHEMA", "Unbekannter Formelparameter.");
    return {
      value: quantity(q),
      length: ["mm", "m", "um"].includes(q.unit) ? 1 : 0,
      angle: ["deg", "rad"].includes(q.unit) ? 1 : 0,
    };
  }
  requireThat(
    [
      "+",
      "-",
      "*",
      "/",
      "sqrt",
      "sin",
      "cos",
      "atan2",
      "min",
      "max",
      "abs",
      "clamp",
    ].includes(expression.fn),
    "INVALID_SCHEMA",
    "Funktion nicht registriert.",
  );
  const a = expression.args.map((e) =>
    evaluate(e, parameters, depth + 1, state),
  );
  const fn = expression.fn;
  const arity = ["sqrt", "sin", "cos", "abs"].includes(fn)
    ? 1
    : fn === "clamp"
      ? 3
      : 2;
  requireThat(
    a.length === arity,
    "INVALID_SCHEMA",
    "Falsche Funktionsargumente.",
  );
  let { length, angle } = a[0];
  let value = 0;
  const same = () =>
    requireThat(
      a.every((x) => x.length === length && x.angle === angle),
      "UNIT_MISMATCH",
      "Formeleinheiten stimmen nicht überein.",
    );
  if (["+", "-", "min", "max", "clamp", "atan2"].includes(fn)) same();
  switch (fn) {
    case "+":
      value = new Decimal(a[0].value).plus(a[1].value).toNumber();
      break;
    case "-":
      value = new Decimal(a[0].value).minus(a[1].value).toNumber();
      break;
    case "*":
      value = a[0].value * a[1].value;
      length += a[1].length;
      angle += a[1].angle;
      break;
    case "/":
      requireThat(
        a[1].value !== 0,
        "CONSTRAINT_CONFLICT",
        "Division durch null.",
      );
      value = a[0].value / a[1].value;
      length -= a[1].length;
      angle -= a[1].angle;
      break;
    case "sqrt":
      requireThat(
        a[0].value >= 0 && length % 2 === 0 && angle % 2 === 0,
        "UNIT_MISMATCH",
        "Ungültige reelle Wurzel.",
      );
      value = Math.sqrt(a[0].value);
      length /= 2;
      angle /= 2;
      break;
    case "sin":
    case "cos":
      requireThat(
        length === 0 && (angle === 0 || angle === 1),
        "UNIT_MISMATCH",
        "Trigonometrie benötigt einen Winkel.",
      );
      value = Math[fn](a[0].value);
      angle = 0;
      break;
    case "atan2":
      requireThat(
        a.some((x) => x.value !== 0),
        "CONSTRAINT_CONFLICT",
        "Winkel am Ursprung ist undefiniert.",
      );
      value = Math.atan2(a[0].value, a[1].value);
      length = 0;
      angle = 1;
      break;
    case "abs":
      value = Math.abs(a[0].value);
      break;
    case "min":
      value = Math.min(...a.map((x) => x.value));
      break;
    case "max":
      value = Math.max(...a.map((x) => x.value));
      break;
    case "clamp":
      requireThat(
        a[1].value <= a[2].value,
        "CONSTRAINT_CONFLICT",
        "Vertauschte Grenzen.",
      );
      value = Math.max(a[1].value, Math.min(a[2].value, a[0].value));
  }
  requireThat(
    Number.isFinite(value) && Math.abs(value) <= 1e12,
    "BUDGET_EXCEEDED",
    "Formelergebnis außerhalb des Bereichs.",
  );
  return { value, length, angle };
}
/** Bounded one-parameter inverse construction; no claim of a global multivariate optimum. */
export function solveMonotone(
  measure: (x: number) => number,
  target: number,
  lower: number,
  upper: number,
  tolerance = 1e-8,
) {
  requireThat(
    Number.isFinite(target) && lower < upper && tolerance > 0,
    "INVALID_SCHEMA",
    "Ungültiges Solverintervall.",
  );
  let lo = measure(lower) - target,
    hi = measure(upper) - target;
  requireThat(
    Number.isFinite(lo) && Number.isFinite(hi) && lo * hi <= 0,
    "CONSTRAINT_CONFLICT",
    "Ziel liegt nicht im geklammerten Intervall.",
  );
  for (let i = 0; i < 100; i++) {
    const x = (lower + upper) / 2,
      residual = measure(x) - target;
    requireThat(
      Number.isFinite(residual),
      "GEOMETRY_INVALID",
      "Nichtendlicher Solverwert.",
    );
    if (Math.abs(residual) <= tolerance)
      return { value: x, residual, iterations: i + 1, status: "converged" };
    if (residual * lo > 0) {
      lower = x;
      lo = residual;
    } else upper = x;
  }
  throw new Error("Solver iteration budget exhausted");
}
