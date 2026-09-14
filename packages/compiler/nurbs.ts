import type { Feature } from "../semantic-ir/schema.js";
import { requireThat } from "../semantic-ir/errors.js";
import * as Q from "./bernstein.js";

export type Basis = {
  degree: number;
  knots: string[];
  multiplicities: number[];
};
export type Surface = Extract<
  Feature["construction"],
  { operator: "nurbs_surface" }
>;
export type Net = Q.Q[][][]; // [u][v][homogeneous coordinate]
export const bezierBasis = (count: number): Basis => ({
  degree: count - 1,
  knots: ["0", "1"],
  multiplicities: [count, count],
});
export const surfaceBasis = (c: Surface, axis: "u" | "v") =>
  c[axis === "u" ? "u_basis" : "v_basis"] ??
  bezierBasis(axis === "u" ? c.poles.length : c.poles[0].length);
export const expandedKnots = (basis: Basis) =>
  basis.knots.flatMap((k, i) => Array<string>(basis.multiplicities[i]).fill(k));

export function validateBasis(b: Basis, count: number) {
  requireThat(
    Number.isInteger(b.degree) &&
      b.degree >= 1 &&
      b.degree <= 15 &&
      count > b.degree &&
      b.knots.length === b.multiplicities.length &&
      b.knots.length >= 2 &&
      Q.cmp(Q.decimal(b.knots[0]), Q.zero) === 0 &&
      Q.cmp(Q.decimal(b.knots.at(-1)!), Q.one) === 0 &&
      b.knots.every(
        (k, i) =>
          Number.isFinite(Number(k)) &&
          (i === 0 ||
            Q.cmp(
              Q.sub(Q.decimal(k), Q.decimal(b.knots[i - 1])),
              Q.decimal("0.000000001"),
            ) >= 0),
      ) &&
      b.multiplicities.every(
        (m, i) =>
          Number.isInteger(m) &&
          (i === 0 || i === b.knots.length - 1
            ? m === b.degree + 1
            : m >= 1 && m <= b.degree),
      ) &&
      b.multiplicities.reduce((a, v) => a + v, 0) === count + b.degree + 1,
    "INVALID_SCHEMA",
    "NURBS benötigt einen geklemmten Knotenvektor auf [0,1], passende Polzahl und mindestens 1e-9 Knotenabstand.",
  );
}
export function validateSurface(c: Surface) {
  const columns = c.poles[0].length;
  requireThat(
    c.poles.every((r) => r.length === columns) &&
      c.poles.flat().every((p) => p.every((v) => Math.abs(Number(v)) <= 1e6)) &&
      c.weights.length === c.poles.length &&
      c.weights.every(
        (r) =>
          r.length === columns &&
          r.every((w) => Number(w) >= 1e-12 && Number(w) <= 1e6),
      ),
    "INVALID_SCHEMA",
    "NURBS-Kontrollnetz und Gewichte im Bereich [1e-12,1e6] müssen zusammenpassen.",
  );
  validateBasis(surfaceBasis(c, "u"), c.poles.length);
  validateBasis(surfaceBasis(c, "v"), columns);
}
export const homogeneous = (c: Surface): Net =>
  c.poles.map((row, u) =>
    row.map((p, v) => [
      ...p.map((x) => Q.mul(Q.decimal(x), Q.decimal(c.weights[u][v]))),
      Q.decimal(c.weights[u][v]),
    ]),
  );
export const transpose = (net: Net): Net =>
  net[0].map((_, v) => net.map((row) => row[v]));

/** One exact Boehm insertion in homogeneous coordinates; no native or decimal rounding. */
export function insert(
  net: Net,
  basis: Basis,
  value: string,
): { net: Net; basis: Basis } {
  const u = Q.decimal(value),
    knots = expandedKnots(basis).map(Q.decimal),
    p = basis.degree;
  const match = basis.knots.findIndex((k) => Q.cmp(Q.decimal(k), u) === 0);
  const multiplicity = match < 0 ? 0 : basis.multiplicities[match];
  requireThat(
    Q.cmp(u, Q.zero) > 0 && Q.cmp(u, Q.one) < 0 && multiplicity < p,
    "OUT_OF_SCOPE",
    "Nur innere Knoten bis zur Vielfachheit des Grades können eingefügt werden.",
  );
  requireThat(
    match >= 0 ||
      basis.knots.every(
        (k) =>
          Q.cmp(Q.abs(Q.sub(Q.decimal(k), u)), Q.decimal("0.000000001")) >= 0,
      ),
    "PRECISION_UNSUPPORTED",
    "Neue NURBS-Knoten müssen mindestens 1e-9 von vorhandenen Knoten entfernt liegen.",
  );
  let span = p;
  while (span + 1 < knots.length && Q.cmp(knots[span + 1], u) <= 0) span++;
  const next: Net = Array.from({ length: net.length + 1 }, (_, i) => {
    if (i <= span - p) return net[i];
    if (i >= span - multiplicity + 1) return net[i - 1];
    const alpha = Q.div(Q.sub(u, knots[i]), Q.sub(knots[i + p], knots[i]));
    return net[i].map((point, j) =>
      point.map((x, axis) =>
        Q.add(Q.mul(alpha, x), Q.mul(Q.sub(Q.one, alpha), net[i - 1][j][axis])),
      ),
    );
  });
  const result = structuredClone(basis);
  if (match >= 0) result.multiplicities[match]++;
  else {
    const i = result.knots.findIndex((k) => Q.cmp(Q.decimal(k), u) > 0);
    result.knots.splice(i, 0, value);
    result.multiplicities.splice(i, 0, 1);
  }
  return { net: next, basis: result };
}
/** Fixed 36-place rounding is bounded afterwards in exact rational arithmetic. */
function rounded(q: Q.Q): string {
  const scale = 10n ** 36n,
    abs = Q.abs(q),
    n = (abs.n * scale * 2n + abs.d) / (2n * abs.d);
  const s = n.toString().padStart(37, "0");
  const result = (s.slice(0, -36) + "." + s.slice(-36))
    .replace(/0+$/, "")
    .replace(/\.$/, "");
  return (q.n < 0n && n !== 0n ? "-" : "") + result;
}
export function refineSurface(
  source: Surface,
  axis: "u" | "v",
  values: string[],
) {
  validateSurface(source);
  requireThat(
    (axis === "u" ? source.poles.length : source.poles[0].length) +
      values.length <=
      16,
    "BUDGET_EXCEEDED",
    "Verfeinerung überschreitet 16 Kontrollpunkte pro Richtung.",
  );
  let net = homogeneous(source),
    basis = surfaceBasis(source, axis);
  if (axis === "v") net = transpose(net);
  for (const value of values) ({ net, basis } = insert(net, basis, value));
  if (axis === "v") net = transpose(net);
  const poles = net.map((row) =>
    row.map(
      (h) =>
        h.slice(0, 3).map((x) => rounded(Q.div(x, h[3]))) as [
          string,
          string,
          string,
        ],
    ),
  );
  const weights = net.map((row) => row.map((h) => rounded(h[3])));
  const construction: Surface = {
    ...source,
    poles,
    weights,
    u_basis: surfaceBasis(source, "u"),
    v_basis: surfaceBasis(source, "v"),
  };
  construction[axis === "u" ? "u_basis" : "v_basis"] = basis;
  validateSurface(construction);
  const stored = homogeneous(construction).flat(),
    exact = net.flat();
  const weightError = exact
    .map((h, i) => Q.abs(Q.sub(h[3], stored[i][3])))
    .reduce(Q.max, Q.zero);
  const minimumWeight = exact.map((h) => h[3]).reduce(Q.min);
  // Nonnegative basis + partition of unity bound numerator/denominator errors over the entire UV square.
  const error = [0, 1, 2]
    .map((axis) => {
      const numeratorError = exact
        .map((h, i) => Q.abs(Q.sub(h[axis], stored[i][axis])))
        .reduce(Q.max, Q.zero);
      const coordinateBound = poles
        .flat()
        .map((p) => Q.abs(Q.decimal(p[axis])))
        .reduce(Q.max, Q.zero);
      return Q.div(
        Q.add(numeratorError, Q.mul(coordinateBound, weightError)),
        minimumWeight,
      );
    })
    .reduce(Q.add, Q.zero);
  return {
    construction,
    report: {
      method: "exact_homogeneous_Boehm_insertion_with_outward_rounding_bound",
      coverage: "entire_rational_IR_surface_on_unit_UV_domain",
      parameterization_preserved: true,
      geometric_error_bound_mm: Q.upper(error, 36),
      native_floating_point_error_bound: null,
      control_points_before: [source.poles.length, source.poles[0].length],
      control_points_after: [poles.length, poles[0].length],
    },
  };
}
