import { Feature } from "../semantic-ir/schema.js";
import { requireThat } from "../semantic-ir/errors.js";
import * as B from "./bernstein.js";
type Homogeneous = B.Poly[];
const sum = (p: B.Poly[]) => p.reduce(B.plus, [B.zero]);
const two = B.fraction(2n);
function edge(poles: string[][][], weights: string[][], last: boolean) {
  const rows: Homogeneous[] = poles.map((row, u) =>
    [0, 1, 2, 3].map((axis) =>
      row.map((p, v) =>
        B.mul(
          axis === 3 ? B.one : B.decimal(p[axis]),
          B.decimal(weights[u][v]),
        ),
      ),
    ),
  );
  const at = (offset: number) => rows[last ? rows.length - 1 - offset : offset];
  const h = at(0),
    n = rows.length - 1;
  const du = h.map((p, axis) =>
    B.times(
      last ? B.minus(p, at(1)[axis]) : B.minus(at(1)[axis], p),
      B.fraction(BigInt(n)),
    ),
  );
  const duu = h.map((p, axis) =>
    n < 2
      ? [B.zero]
      : B.times(
          B.plus(B.minus(p, B.times(at(1)[axis], two)), at(2)[axis]),
          B.fraction(BigInt(n * (n - 1))),
        ),
  );
  return {
    h,
    du,
    dv: h.map(B.derivative),
    duu,
    duv: du.map(B.derivative),
    dvv: h.map((p) => B.derivative(B.derivative(p))),
  };
}
function rationalFirst(h: Homogeneous, d: Homogeneous) {
  return h
    .slice(0, 3)
    .map((p, i) => B.minus(B.product(d[i], h[3]), B.product(p, d[3])));
}
function rationalSecond(
  h: Homogeneous,
  a: Homogeneous,
  b: Homogeneous,
  ab: Homogeneous,
) {
  const w = h[3];
  return h
    .slice(0, 3)
    .map((p, i) =>
      sum([
        B.product(ab[i], B.power(w, 2)),
        B.times(B.product(B.product(a[i], w), b[3]), B.fraction(-1n)),
        B.times(B.product(B.product(b[i], w), a[3]), B.fraction(-1n)),
        B.times(B.product(B.product(p, w), ab[3]), B.fraction(-1n)),
        B.times(B.product(B.product(p, a[3]), b[3]), two),
      ]),
    );
}
function differenceBound(
  a: B.Poly[],
  b: B.Poly[],
  wa: B.Poly,
  wb: B.Poly,
  power: number,
) {
  const denominator = B.mul(wa.reduce(B.min), wb.reduce(B.min));
  let lower = B.one;
  for (let i = 0; i < power; i++) lower = B.mul(lower, denominator);
  return a
    .map((p, i) =>
      B.div(
        B.bound(
          B.minus(
            B.product(p, B.power(wb, power)),
            B.product(b[i], B.power(wa, power)),
          ),
        ),
        lower,
      ),
    )
    .reduce(B.add, B.zero);
}
function areaLower(h: Homogeneous, du: Homogeneous, dv: Homogeneous) {
  const u = rationalFirst(h, du),
    v = rationalFirst(h, dv);
  const cross = [0, 1, 2].map((i) =>
    B.minus(
      B.product(u[(i + 1) % 3], v[(i + 2) % 3]),
      B.product(u[(i + 2) % 3], v[(i + 1) % 3]),
    ),
  );
  const component = cross
    .map((p) => B.max(B.zero, B.max(p.reduce(B.min), B.neg(p.reduce(B.max)))))
    .reduce(B.max, B.zero);
  const w = h[3].reduce(B.max);
  return B.div(component, B.mul(B.mul(w, w), B.mul(w, w)));
}

/** Exact homogeneous Bernstein numerators bound the entire rational seam.
 * Positive weights bound denominators away from zero. All polynomial arithmetic
 * uses BigInt fractions; decimal result bounds are rounded outwards.
 */
export function patchContinuity(a: Feature, b: Feature) {
  requireThat(
    a.id !== b.id &&
      a.construction.operator === "nurbs_surface" &&
      b.construction.operator === "nurbs_surface",
    "OUT_OF_SCOPE",
    "Patchanschluss benötigt zwei verschiedene NURBS-Patches.",
  );
  for (const c of [a.construction, b.construction]) {
    requireThat(
      c.poles.length <= 6 && c.poles.every((row) => row.length <= 6),
      "BUDGET_EXCEEDED",
      "Exakte Anschlussprüfung ist auf Patches bis Grad 5 pro Richtung begrenzt.",
    );
    requireThat(
      c.weights.length === c.poles.length &&
        c.weights.every(
          (row, i) =>
            row.length === c.poles[i].length &&
            row.every((w) => B.cmp(B.decimal(w), B.zero) > 0),
        ),
      "INVALID_SCHEMA",
      "Rationale Anschlussprüfung benötigt passende positive Gewichte.",
    );
  }
  const left = edge(a.construction.poles, a.construction.weights, true),
    right = edge(b.construction.poles, b.construction.weights, false);
  const wa = left.h[3],
    wb = right.h[3];
  const position = differenceBound(
    left.h.slice(0, 3),
    right.h.slice(0, 3),
    wa,
    wb,
    1,
  );
  const first = (["du", "dv"] as const)
    .map((k) =>
      differenceBound(
        rationalFirst(left.h, left[k]),
        rationalFirst(right.h, right[k]),
        wa,
        wb,
        2,
      ),
    )
    .reduce(B.max);
  const second = (
    [
      ["du", "du", "duu"],
      ["du", "dv", "duv"],
      ["dv", "dv", "dvv"],
    ] as const
  )
    .map(([x, y, xy]) =>
      differenceBound(
        rationalSecond(left.h, left[x], left[y], left[xy]),
        rationalSecond(right.h, right[x], right[y], right[xy]),
        wa,
        wb,
        3,
      ),
    )
    .reduce(B.max);
  const regularity = B.min(
    areaLower(left.h, left.du, left.dv),
    areaLower(right.h, right.du, right.dv),
  );
  return {
    position_bound_mm: B.upper(position),
    derivative_bound_mm_per_parameter: B.upper(first),
    second_derivative_bound_mm_per_parameter2: B.upper(second),
    jacobian_area_lower_mm2: B.lower(regularity),
    regularity_proved: B.cmp(regularity, B.zero) > 0,
    domain: "A(u=1,v) to B(u=0,v), v in [0,1], same v orientation",
    norm: "L1_upper_bound_on_L2",
    method: "exact_rational_Bernstein_homogeneous_bounds",
  };
}
