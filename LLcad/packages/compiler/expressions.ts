import { Decimal } from "decimal.js";
import { Quantity } from "../semantic-ir/schema.js";
import { quantity } from "../semantic-ir/units.js";
import { requireThat } from "../semantic-ir/errors.js";
export type Expression =
  | { constant: Quantity }
  | { parameter: string }
  | { var: string }
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
    "Scalar required.",
  );
  return value as Scalar;
}
function vector(value: Value): Vector {
  requireThat(
    Array.isArray(value.value),
    "UNIT_MISMATCH",
    "Three-dimensional vector required.",
  );
  return value as Vector;
}
function checked<T extends Value>(result: T): T {
  const values = Array.isArray(result.value) ? result.value : [result.value];
  requireThat(
    values.every((value) => Number.isFinite(value) && Math.abs(value) <= 1e12),
    "BUDGET_EXCEEDED",
    "Formula result out of range.",
  );
  return result;
}
/** Scalar value of an expression over the feature's own parameters and the model variables. */
export function evaluate(
  expression: Expression,
  parameters: Record<string, Quantity>,
  variables: Record<string, Quantity> = {},
): Scalar {
  return scalar(
    evaluateValue(expression, parameters, 0, { nodes: 0 }, variables),
  );
}
/** Model variables an expression reads ({var: name}). */
export function variablesOf(expression: any, found = new Set<string>(), depth = 0) {
  if (!expression || typeof expression !== "object" || depth > 64) return found;
  if (typeof expression.var === "string") found.add(expression.var);
  for (const arg of expression.args ?? []) variablesOf(arg, found, depth + 1);
  return found;
}
type Dimension = "length" | "angle" | "scalar";
/** Evaluate every expression-driven parameter of one feature in dependency order and
 *  write the result into its parameters (mm, rad or 1). A target parameter may be missing
 *  from `parameters` (compact authoring); it is then created. */
export function resolveExpressions(
  f: { parameters: Record<string, Quantity>; expressions: Record<string, any> },
  specs: Record<string, { dimension: Dimension }>,
  variables: Record<string, Quantity> = {},
) {
  const solved = new Set<string>(),
    solving = new Set<string>();
  const solve = (name: string) => {
    if (solved.has(name) || !Object.hasOwn(f.expressions, name)) return;
    requireThat(
      Object.hasOwn(specs, name),
      "INVALID_SCHEMA",
      "The formula target is not a registered parameter.",
    );
    requireThat(
      !solving.has(name),
      "CONSTRAINT_CONFLICT",
      "Cyclic parameter formula.",
    );
    solving.add(name);
    const references = (e: any) => {
      if ("parameter" in e) solve(e.parameter);
      if (e.args) e.args.forEach(references);
    };
    references(f.expressions[name]);
    const value = evaluate(f.expressions[name], f.parameters, variables),
      dimension = specs[name].dimension;
    requireThat(
      value.length === (dimension === "length" ? 1 : 0) &&
        value.angle === (dimension === "angle" ? 1 : 0),
      "UNIT_MISMATCH",
      "The formula result does not match the parameter dimension.",
    );
    f.parameters[name] = {
      value: value.value.toFixed(12),
      unit: dimension === "length" ? "mm" : dimension === "angle" ? "rad" : "1",
    };
    solving.delete(name);
    solved.add(name);
  };
  Object.keys(f.expressions).forEach(solve);
}
function evaluateValue(
  expression: Expression,
  parameters: Record<string, Quantity>,
  depth: number,
  state: { nodes: number },
  variables: Record<string, Quantity>,
): Value {
  requireThat(
    depth <= 32 && ++state.nodes <= 4096,
    "BUDGET_EXCEEDED",
    "The expression exceeds the budget.",
  );
  if (
    "constant" in expression ||
    "parameter" in expression ||
    "var" in expression
  ) {
    if ("parameter" in expression)
      requireThat(
        Object.hasOwn(parameters, expression.parameter),
        "INVALID_SCHEMA",
        "Unknown formula parameter.",
      );
    if ("var" in expression)
      requireThat(
        Object.hasOwn(variables, expression.var),
        "INVALID_SCHEMA",
        `Unknown model variable ${expression.var}; create it with set_variable first.`,
      );
    const q =
      "constant" in expression
        ? expression.constant
        : "var" in expression
          ? variables[expression.var]
          : parameters[expression.parameter];
    requireThat(q, "INVALID_SCHEMA", "Unknown formula parameter.");
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
    "Function not registered.",
  );
  const args = expression.args.map((e) =>
    evaluateValue(e, parameters, depth + 1, state, variables),
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
    "Wrong function arguments.",
  );
  const compatible = () =>
    requireThat(
      args.every(
        (v) => v.length === args[0].length && v.angle === args[0].angle,
      ),
      "UNIT_MISMATCH",
      "Vector units do not match.",
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
      requireThat(b.value !== 0, "CONSTRAINT_CONFLICT", "Division by zero.");
      return checked({
        value: a.value.map((x) => x / b.value),
        length: a.length - b.length,
        angle: a.angle - b.angle,
      });
    }
    requireThat(
      false,
      "UNIT_MISMATCH",
      "The function does not support vector arguments.",
    );
  }
  const a = args.map(scalar);
  let { length, angle } = a[0];
  let value = 0;
  const same = () =>
    requireThat(
      a.every((x) => x.length === length && x.angle === angle),
      "UNIT_MISMATCH",
      "Formula units do not match.",
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
        "Division by zero.",
      );
      value = a[0].value / a[1].value;
      length -= a[1].length;
      angle -= a[1].angle;
      break;
    case "sqrt":
      requireThat(
        a[0].value >= 0 && length % 2 === 0 && angle % 2 === 0,
        "UNIT_MISMATCH",
        "Invalid real root.",
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
        "Trigonometry needs an angle.",
      );
      value = Math[fn](a[0].value);
      angle = 0;
      break;
    case "atan2":
      requireThat(
        a.some((x) => x.value !== 0),
        "CONSTRAINT_CONFLICT",
        "The angle at the origin is undefined.",
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
        "Swapped bounds.",
      );
      value = Math.max(a[1].value, Math.min(a[2].value, a[0].value));
  }
  requireThat(
    Number.isFinite(value) && Math.abs(value) <= 1e12,
    "BUDGET_EXCEEDED",
    "Formula result out of range.",
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
    "Invalid solver interval.",
  );
  let lo = measure(lower) - target,
    hi = measure(upper) - target;
  requireThat(
    Number.isFinite(lo) && Number.isFinite(hi) && lo * hi <= 0,
    "CONSTRAINT_CONFLICT",
    "The target is not inside the bracketed interval.",
  );
  for (let i = 0; i < 100; i++) {
    const x = (lower + upper) / 2,
      residual = measure(x) - target;
    requireThat(
      Number.isFinite(residual),
      "GEOMETRY_INVALID",
      "Non-finite solver value.",
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
