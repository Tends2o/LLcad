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
    "A constraint needs 1 to 12 parameter bindings.",
  );
  return Object.fromEntries(
    Object.entries(bindings).map(([name, binding]) => {
      const feature = ir.features.find((f) => f.id === binding.feature_id);
      const parameter = feature?.parameters[binding.parameter];
      requireThat(
        parameter,
        "INVALID_SCHEMA",
        "Unknown parameter binding in constraint.",
      );
      return [name, parameter];
    }),
  );
}
export function checkEquation(
  equation: any,
  values: Record<string, Quantity>,
  variables: Record<string, Quantity> = {},
) {
  const result = evaluate(equation.expression, values, variables);
  requireThat(
    result.length === 0 && result.angle === 0,
    "UNIT_MISMATCH",
    "Solver equations must be normalised to dimensionless form by an explicit reference quantity.",
  );
  const tolerance = Number(equation.tolerance);
  requireThat(
    tolerance >= 1e-10 && tolerance <= 0.01,
    "PRECISION_UNSUPPORTED",
    "Normalised equation tolerance: 1e-10 to 0.01.",
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
const weight_of = (value: string) => Number(value);
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
    "Duplicate solver binding or equation id.",
  );
  const variables = problem.variables.map((v) => {
    const feature = ir.features.find((f) => f.id === v.feature_id),
      actual = feature?.parameters[v.parameter];
    requireThat(
      actual && equalQuantity(actual, v.expected),
      "STALE_REVISION",
      "The solver base does not match the parameter that was read.",
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
      "Do not replace protected or formula-driven parameters with the solver.",
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
      "The start value is not within valid solver bounds.",
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
    // Model variables are constants for the solver (it varies feature parameters).
    if (e.var) {
      requireThat(ir.variables?.[e.var], "INVALID_SCHEMA", `Unknown model variable ${e.var}.`);
      return { constant: quantity(ir.variables![e.var]) };
    }
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
      "The SQP contract supports smooth expressions; piecewise functions need a different problem formulation.",
    );
    return { fn: e.fn, args: e.args.map(convert) };
  };
  for (const equation of problem.equations)
    checkEquation(equation, values, ir.variables ?? {});
  requireThat(
    new Set(problem.objectives.map((o) => o.id)).size ===
      problem.objectives.length,
    "INVALID_SCHEMA",
    "Duplicate objective term id.",
  );
  for (const objective of problem.objectives) {
    const result = evaluate(objective.expression, values, ir.variables ?? {});
    requireThat(
      result.length === 0 && result.angle === 0,
      "UNIT_MISMATCH",
      "Objective terms must be normalised to dimensionless form.",
    );
    const weight = Number(objective.weight),
      scale = Number(objective.scale);
    requireThat(
      weight > 0 && weight <= 1e6 && scale >= 1e-9 && scale <= 1e9,
      "INVALID_SCHEMA",
      "The objective weight must be in (0, 1e6] and the scale in [1e-9, 1e9].",
    );
  }
  const regularization = Number(problem.regularization);
  requireThat(
    regularization >= 0 && regularization <= 1e6,
    "INVALID_SCHEMA",
    "Damping must be in [0, 1e6].",
  );
  requireThat(
    regularization > 0 || problem.objectives.length > 0,
    "CONSTRAINT_CONFLICT",
    "Without damping at least one objective term is required.",
  );
  return {
    variables,
    equations: problem.equations.map((e) => ({
      ...e,
      expression: convert(e.expression),
    })),
    objectives: problem.objectives.map((o) => ({
      id: o.id,
      expression: convert(o.expression),
      weight: weight_of(o.weight),
      scale: weight_of(o.scale),
      loss: o.loss,
    })),
    regularization,
    max_iterations: problem.max_iterations,
  };
}
