import { requireThat } from "../semantic-ir/errors.js";
export type Vec = [number, number, number];
export type Mat3 = [Vec, Vec, Vec];
export const dot = (a: Vec, b: Vec) => a.reduce((s, x, i) => s + x * b[i], 0);
export const cross = (a: Vec, b: Vec): Vec => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
export const norm = (a: Vec) => Math.hypot(...a);
export const multiply = (m: Mat3, v: Vec): Vec =>
  m.map((row) => dot(row, v)) as Vec;
export function inverse(m: Mat3): Mat3 {
  const det = dot(m[0], cross(m[1], m[2]));
  const scale = Math.max(...m.flat().map(Math.abs));
  requireThat(
    Number.isFinite(det) && Math.abs(det) > 1e-12 * scale ** 3,
    "GEOMETRY_INVALID",
    "Singuläre oder schlecht konditionierte affine Abbildung.",
  );
  const rows = [cross(m[1], m[2]), cross(m[2], m[0]), cross(m[0], m[1])];
  return [0, 1, 2].map((i) => rows.map((v) => v[i] / det)) as Mat3;
}
export function normalTransform(m: Mat3, n: Vec): Vec {
  const inv = inverse(m);
  const result = [0, 1, 2].map((i) =>
    inv.reduce((v, row, j) => v + row[i] * n[j], 0),
  ) as Vec;
  const length = norm(result);
  requireThat(length > 0, "GEOMETRY_INVALID", "Normale ist null.");
  return result.map((x) => x / length) as Vec;
}
export const transformPoint = (matrix: Mat3, translation: Vec, p: Vec): Vec =>
  multiply(matrix, p).map((x, i) => x + translation[i]) as Vec;
export function bezier(points: Vec[], t: number): Vec {
  requireThat(
    points.length >= 2 && points.length <= 256 && t >= 0 && t <= 1,
    "INVALID_SCHEMA",
    "Ungültige Bézier-Auswertung.",
  );
  let rows = points.map((p) => [...p]);
  while (rows.length > 1)
    rows = rows
      .slice(1)
      .map((p, i) => p.map((x, j) => (1 - t) * rows[i][j] + t * x));
  return rows[0] as Vec;
}
export function bsplineBasis(
  i: number,
  degree: number,
  knots: number[],
  u: number,
): number {
  requireThat(
    degree >= 0 &&
      degree <= 15 &&
      knots.length <= 512 &&
      i >= 0 &&
      i + degree + 1 < knots.length,
    "INVALID_SCHEMA",
    "Ungültige B-Spline-Basis.",
  );
  const end = knots[knots.length - degree - 1];
  if (u === end) return i === knots.length - degree - 2 ? 1 : 0;
  const memo = new Map<string, number>();
  const basis = (j: number, p: number): number => {
    const key = j + ":" + p;
    if (memo.has(key)) return memo.get(key)!;
    if (p === 0) return knots[j] <= u && u < knots[j + 1] ? 1 : 0;
    const a = knots[j + p] - knots[j],
      b = knots[j + p + 1] - knots[j + 1];
    const value =
      (a ? ((u - knots[j]) / a) * basis(j, p - 1) : 0) +
      (b ? ((knots[j + p + 1] - u) / b) * basis(j + 1, p - 1) : 0);
    memo.set(key, value);
    return value;
  };
  return basis(i, degree);
}
