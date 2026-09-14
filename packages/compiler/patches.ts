import { Feature } from "../semantic-ir/schema.js";
import { requireThat } from "../semantic-ir/errors.js";
import * as B from "./bernstein.js";
import {
  homogeneous,
  insert,
  surfaceBasis,
  transpose,
  validateSurface,
  type Basis,
  type Net,
  type Surface,
} from "./nurbs.js";
type Homogeneous = B.Poly[];
const sum = (p: B.Poly[]) => p.reduce(B.plus, [B.zero]);
const two = B.fraction(2n);
function edge(net: Net, last: boolean, uWidth: B.Q, vWidth: B.Q) {
  const rows: Homogeneous[] = net.map((row) =>
    [0, 1, 2, 3].map((axis) => row.map((p) => p[axis])),
  );
  const at = (offset: number) => rows[last ? rows.length - 1 - offset : offset];
  const h = at(0),
    n = rows.length - 1;
  const du = h.map((p, axis) =>
    B.times(
      last ? B.minus(p, at(1)[axis]) : B.minus(at(1)[axis], p),
      B.div(B.fraction(BigInt(n)), uWidth),
    ),
  );
  const duu = h.map((p, axis) =>
    n < 2
      ? [B.zero]
      : B.times(
          B.plus(B.minus(p, B.times(at(1)[axis], two)), at(2)[axis]),
          B.div(B.fraction(BigInt(n * (n - 1))), B.mul(uWidth, uWidth)),
        ),
  );
  return {
    h,
    du,
    dv: h.map((p) => B.times(B.derivative(p), B.div(B.one, vWidth))),
    duu,
    duv: du.map((p) => B.times(B.derivative(p), B.div(B.one, vWidth))),
    dvv: h.map((p) =>
      B.times(
        B.derivative(B.derivative(p)),
        B.div(B.one, B.mul(vWidth, vWidth)),
      ),
    ),
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
function seamBounds(
  left: ReturnType<typeof edge>,
  right: ReturnType<typeof edge>,
) {
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
  return { position, first, second, regularity };
}

/** Extract exact homogeneous Bézier pieces, without serializing intermediate poles. */
function seamPieces(c: Surface, commonV: string[], last: boolean) {
  let net = homogeneous(c),
    ub = surfaceBasis(c, "u"),
    vb = surfaceBasis(c, "v");
  const complete = (knots: string[], basis: Basis, source: Net) => {
    for (const k of knots.slice(1, -1)) {
      let i = basis.knots.findIndex(
        (v) => B.cmp(B.decimal(v), B.decimal(k)) === 0,
      );
      while (i < 0 || basis.multiplicities[i] < basis.degree) {
        ({ net: source, basis } = insert(source, basis, k));
        i = basis.knots.findIndex(
          (v) => B.cmp(B.decimal(v), B.decimal(k)) === 0,
        );
      }
    }
    return { net: source, basis };
  };
  ({ net, basis: ub } = complete(ub.knots, ub, net));
  ({ net, basis: vb } = complete(commonV, vb, transpose(net)));
  net = transpose(net);
  const uWidth = last
    ? B.sub(B.one, B.decimal(ub.knots.at(-2)!))
    : B.decimal(ub.knots[1]);
  const end = last ? net.slice(-(ub.degree + 1)) : net.slice(0, ub.degree + 1);
  return commonV.slice(1).map((right, i) =>
    edge(
      end.map((row) => row.slice(i * vb.degree, i * vb.degree + vb.degree + 1)),
      last,
      uWidth,
      B.sub(B.decimal(right), B.decimal(commonV[i])),
    ),
  );
}

export function patchContinuity(a: Feature, b: Feature) {
  requireThat(
    a.id !== b.id &&
      a.construction.operator === "nurbs_surface" &&
      b.construction.operator === "nurbs_surface",
    "OUT_OF_SCOPE",
    "Patchanschluss benötigt zwei verschiedene NURBS-Flächen.",
  );
  for (const c of [a.construction, b.construction]) {
    requireThat(
      surfaceBasis(c, "u").degree <= 5 && surfaceBasis(c, "v").degree <= 5,
      "BUDGET_EXCEEDED",
      "Exakte Anschlussprüfung ist auf Grad 5 pro Richtung begrenzt.",
    );
    validateSurface(c);
  }
  const allV = [
    ...surfaceBasis(a.construction, "v").knots,
    ...surfaceBasis(b.construction, "v").knots,
  ].sort((a, b) => B.cmp(B.decimal(a), B.decimal(b)));
  const commonV = allV.filter(
    (k, i) => i === 0 || B.cmp(B.decimal(k), B.decimal(allV[i - 1])) !== 0,
  );
  requireThat(
    commonV.length <= 17,
    "BUDGET_EXCEEDED",
    "Exakte Anschlussprüfung ist auf 16 gemeinsame Knotenspannen begrenzt.",
  );
  const left = seamPieces(a.construction, commonV, true),
    right = seamPieces(b.construction, commonV, false);
  const bounds = left.map((edge, i) => seamBounds(edge, right[i]));
  const position = bounds.map((b) => b.position).reduce(B.max),
    first = bounds.map((b) => b.first).reduce(B.max);
  const second = bounds.map((b) => b.second).reduce(B.max),
    regularity = bounds.map((b) => b.regularity).reduce(B.min);
  // Check both one-sided derivatives exactly at every piece junction. Knot
  // multiplicity alone understates the continuity of shape-preserving insertions.
  const endpoint = (piece: ReturnType<typeof edge>, last: boolean) =>
    Object.fromEntries(
      Object.entries(piece).map(([name, polynomials]) => [
        name,
        polynomials.map((p) => [last ? p.at(-1)! : p[0]]),
      ]),
    ) as ReturnType<typeof edge>;
  const junctions = [left, right].flatMap((pieces) =>
    pieces
      .slice(1)
      .map((next, i) =>
        seamBounds(endpoint(pieces[i], true), endpoint(next, false)),
      ),
  );
  const firstDefined = junctions.every((j) => B.cmp(j.first, B.zero) === 0);
  const secondDefined =
    firstDefined && junctions.every((j) => B.cmp(j.second, B.zero) === 0);
  return {
    position_bound_mm: B.upper(position),
    derivative_bound_mm_per_parameter: B.upper(first),
    second_derivative_bound_mm_per_parameter2: B.upper(second),
    jacobian_area_lower_mm2: B.lower(regularity),
    regularity_proved: B.cmp(regularity, B.zero) > 0,
    domain: "A(u=1,v) to B(u=0,v), v in [0,1], same v orientation",
    norm: "L1_upper_bound_on_L2",
    method: "exact_rational_Bernstein_homogeneous_bounds",
    knot_spans: bounds.length,
    first_derivatives_defined_at_interior_knots: firstDefined,
    second_derivatives_defined_at_interior_knots: secondDefined,
  };
}
