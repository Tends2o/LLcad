import { ModelIR, Quantity, SolverProblem } from "../semantic-ir/schema.js";
import { z } from "zod";
import { evaluate } from "./expressions.js";
import { quantity, equalQuantity } from "../semantic-ir/units.js";
import { requireThat } from "../semantic-ir/errors.js";

export function equationValues(
  ir: ModelIR,
  bindings: Record<string, { feature_id: string; parameter: string }>,
) {
  requireThat(
    Object.keys(bindings).length > 0 && Object.keys(bindings).length <= 12,
    "BUDGET_EXCEEDED",
    "Nebenbedingung benötigt 1 bis 12 Parameterbindungen.",
  );
  return Object.fromEntries(
    Object.entries(bindings).map(([name, binding]) => {
      const feature = ir.features.find((f) => f.id === binding.feature_id);
      const parameter = feature?.parameters[binding.parameter];
      requireThat(
        parameter,
        "INVALID_SCHEMA",
        "Unbekannte Parameterbindung in Nebenbedingung.",
      );
      return [name, parameter];
    }),
  );
}
export function checkEquation(equation: any, values: Record<string, Quantity>) {
  const result = evaluate(equation.expression, values);
  requireThat(
    result.length === 0 && result.angle === 0,
    "UNIT_MISMATCH",
    "Solvergleichungen müssen durch eine explizite Bezugsgröße dimensionslos normalisiert sein.",
  );
  const tolerance = Number(equation.tolerance);
  requireThat(
    tolerance >= 1e-10 && tolerance <= 0.01,
    "PRECISION_UNSUPPORTED",
    "Normierte Gleichungstoleranz: 1e-10 bis 0.01.",
  );
  const violation =
    equation.relation === "eq"
      ? Math.abs(result.value)
      : equation.relation === "ge"
        ? Math.max(0, -result.value)
        : Math.max(0, result.value);
  return {
    residual: result.value,
    violation,
    tolerance,
    passed: violation <= tolerance,
  };
}
export function solverRequest(
  ir: ModelIR,
  problem: z.infer<typeof SolverProblem>,
) {
  requireThat(
    new Set(problem.variables.map((v) => v.name)).size ===
      problem.variables.length &&
      new Set(problem.variables.map((v) => v.feature_id + ":" + v.parameter))
        .size === problem.variables.length &&
      new Set(problem.equations.map((e) => e.id)).size ===
        problem.equations.length,
    "INVALID_SCHEMA",
    "Doppelte Solverbindung oder Gleichungs-ID.",
  );
  const variables = problem.variables.map((v) => {
    const feature = ir.features.find((f) => f.id === v.feature_id),
      actual = feature?.parameters[v.parameter];
    requireThat(
      actual && equalQuantity(actual, v.expected),
      "STALE_REVISION",
      "Solverbasis stimmt nicht mit dem gelesenen Parameter überein.",
    );
    requireThat(
      !feature?.expressions[v.parameter] &&
        !ir.constraints.some(
          (c) =>
            c.feature_id === v.feature_id &&
            (c.kind === "protected_feature" ||
              (c.kind === "protected_parameter" &&
                c.parameter === v.parameter)),
        ),
      "OUT_OF_SCOPE",
      "Geschützte oder formelgesteuerte Parameter nicht durch den Solver ersetzen.",
    );
    const dimension = ["mm", "m", "um"].includes(actual.unit)
      ? "length"
      : ["rad", "deg"].includes(actual.unit)
        ? "angle"
        : "scalar";
    const lower = quantity(v.lower, dimension),
      upper = quantity(v.upper, dimension),
      initial = quantity(actual);
    requireThat(
      lower < upper && initial >= lower && initial <= upper,
      "CONSTRAINT_CONFLICT",
      "Startwert liegt nicht innerhalb gültiger Solvergrenzen.",
    );
    return {
      ...v,
      initial,
      lower_value: lower,
      upper_value: upper,
      unit: dimension === "length" ? "mm" : dimension === "angle" ? "rad" : "1",
    };
  });
  const values = Object.fromEntries(variables.map((v) => [v.name, v.expected]));
  const convert = (e: any): any => {
    if (e.constant) return { constant: quantity(e.constant) };
    if (e.parameter) return { parameter: e.parameter };
    requireThat(
      [
        "+",
        "-",
        "*",
        "/",
        "sqrt",
        "sin",
        "cos",
        "vec3",
        "dot",
        "norm",
      ].includes(e.fn),
      "OUT_OF_SCOPE",
      "Der SQP-Vertrag unterstützt glatte Ausdrücke; stückweise Funktionen benötigen eine andere Problemformulierung.",
    );
    return { fn: e.fn, args: e.args.map(convert) };
  };
  for (const equation of problem.equations) checkEquation(equation, values);
  return {
    variables,
    equations: problem.equations.map((e) => ({
      ...e,
      expression: convert(e.expression),
    })),
    max_iterations: problem.max_iterations,
  };
}
