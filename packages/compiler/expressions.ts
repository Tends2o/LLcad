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
        | "clamp"
        | "vec3"
        | "dot"
        | "norm";
      args: Expression[];
    };
type Scalar = { value: number; length: number; angle: number };
type Vector = { value: number[]; length: number; angle: number };
type Value = Scalar | Vector;
function scalar(value: Value): Scalar {
  requireThat(
    typeof value.value === "number",
    "UNIT_MISMATCH",
    "Skalar erforderlich.",
  );
  return value as Scalar;
}
function vector(value: Value): Vector {
  requireThat(
    Array.isArray(value.value),
    "UNIT_MISMATCH",
    "Dreidimensionaler Vektor erforderlich.",
  );
  return value as Vector;
}
function checked<T extends Value>(result: T): T {
  const values = Array.isArray(result.value) ? result.value : [result.value];
  requireThat(
    values.every((value) => Number.isFinite(value) && Math.abs(value) <= 1e12),
    "BUDGET_EXCEEDED",
    "Formelergebnis außerhalb des Bereichs.",
  );
  return result;
}
export function evaluate(
  expression: Expression,
  parameters: Record<string, Quantity>,
  depth = 0,
  state = { nodes: 0 },
): Scalar {
  return scalar(evaluateValue(expression, parameters, depth, state));
}
function evaluateValue(
  expression: Expression,
  parameters: Record<string, Quantity>,
  depth: number,
  state: { nodes: number },
): Value {
  requireThat(
    depth <= 32 && ++state.nodes <= 4096,
    "BUDGET_EXCEEDED",
    "Ausdruck überschreitet das Budget.",
  );
  if ("constant" in expression || "parameter" in expression) {
    if ("parameter" in expression)
      requireThat(
        Object.hasOwn(parameters, expression.parameter),
        "INVALID_SCHEMA",
        "Unbekannter Formelparameter.",
      );
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
      "vec3",
      "dot",
      "norm",
    ].includes(expression.fn),
    "INVALID_SCHEMA",
    "Funktion nicht registriert.",
  );
  const args = expression.args.map((e) =>
    evaluateValue(e, parameters, depth + 1, state),
  );
  const fn = expression.fn;
  const arity = ["sqrt", "sin", "cos", "abs", "norm"].includes(fn)
    ? 1
    : ["clamp", "vec3"].includes(fn)
      ? 3
      : 2;
  requireThat(
    args.length === arity,
    "INVALID_SCHEMA",
    "Falsche Funktionsargumente.",
  );
  const compatible = () =>
    requireThat(
      args.every(
        (v) => v.length === args[0].length && v.angle === args[0].angle,
      ),
      "UNIT_MISMATCH",
      "Vektoreinheiten stimmen nicht überein.",
    );
  if (fn === "vec3") {
    compatible();
    return checked({
      value: args.map((v) => scalar(v).value),
      length: args[0].length,
      angle: args[0].angle,
    });
  }
  if (fn === "dot") {
    const a = vector(args[0]),
      b = vector(args[1]);
    return checked({
      value: a.value.reduce((sum, x, i) => sum + x * b.value[i], 0),
      length: a.length + b.length,
      angle: a.angle + b.angle,
    });
  }
  if (fn === "norm") {
    const a = vector(args[0]);
    return checked({
      value: Math.hypot(...a.value),
      length: a.length,
      angle: a.angle,
    });
  }
  if (args.some((a) => Array.isArray(a.value))) {
    if (fn === "+" || fn === "-") {
      const a = vector(args[0]),
        b = vector(args[1]);
      compatible();
      return checked({
        value: a.value.map((x, i) =>
          fn === "+"
            ? new Decimal(x).plus(b.value[i]).toNumber()
            : new Decimal(x).minus(b.value[i]).toNumber(),
        ),
        length: a.length,
        angle: a.angle,
      });
    }
    if (fn === "*") {
      const [a, b] = Array.isArray(args[0].value)
        ? [vector(args[0]), scalar(args[1])]
        : [vector(args[1]), scalar(args[0])];
      return checked({
        value: a.value.map((x) => x * b.value),
        length: a.length + b.length,
        angle: a.angle + b.angle,
      });
    }
    if (fn === "/") {
      const a = vector(args[0]),
        b = scalar(args[1]);
      requireThat(b.value !== 0, "CONSTRAINT_CONFLICT", "Division durch null.");
      return checked({
        value: a.value.map((x) => x / b.value),
        length: a.length - b.length,
        angle: a.angle - b.angle,
      });
    }
    requireThat(
      false,
      "UNIT_MISMATCH",
      "Funktion unterstützt keine Vektorargumente.",
    );
  }
  const a = args.map(scalar);
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
