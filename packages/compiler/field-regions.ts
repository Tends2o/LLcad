import { hash } from "../semantic-ir/hash.js";
import * as Q from "./bernstein.js";

type Box = { min: string[]; max: string[] };
/** Exact decimal proof for distance(center, box) >= radius + |displacement|.
 * Squaring is only used after checking the sign of both sides.
 */
export function compactSupportDisjoint(node: any, region: Box): boolean {
  if (!["local_field_delta", "local_deform"].includes(node.op)) return false;
  const square = (x: Q.Q) => Q.mul(x, x);
  const distance2 = node.center
    .map((value: string, axis: number) => {
      const x = Q.decimal(value);
      return square(
        Q.max(
          Q.zero,
          Q.max(
            Q.sub(Q.decimal(region.min[axis]), x),
            Q.sub(x, Q.decimal(region.max[axis])),
          ),
        ),
      );
    })
    .reduce(Q.add, Q.zero);
  const radius2 = square(Q.decimal(node.radius));
  const displacement2 =
    node.op === "local_deform"
      ? node.displacement
          .map((x: string) => square(Q.decimal(x)))
          .reduce(Q.add, Q.zero)
      : Q.zero;
  const remainder = Q.sub(distance2, Q.add(radius2, displacement2));
  return (
    Q.cmp(remainder, Q.zero) >= 0 &&
    Q.cmp(
      square(remainder),
      Q.mul(Q.fraction(4n), Q.mul(radius2, displacement2)),
    ) >= 0
  );
}

/** Remove only outer compact edits proved irrelevant throughout this region.
 * Different formulas or transformations with unproved influence fail closed.
 */
export function sameFieldInRegion(before: any, after: any, region: Box) {
  const restrict = (source: any) => {
    let node = source;
    for (let depth = 0; depth <= 32; depth++) {
      if (!compactSupportDisjoint(node, region)) break;
      node = node.source;
    }
    return node;
  };
  return hash(restrict(before)) === hash(restrict(after));
}

type Ball = { center: string[]; radius: string; compute_margin?: string };
/** Peel differing compact edits from two expressions; null when the change is not compact. */
export function differenceSupports(before: any, after: any) {
  const canon = (n: any) => hash(n);
  if (canon(before) === canon(after)) return [];
  const peel = (node: any, stop: any) => {
    const peeled: any[] = [];
    for (let depth = 0; depth < 64; depth++) {
      if (
        canon(node) === canon(stop) ||
        !["local_field_delta", "local_deform"].includes(node.op)
      )
        return { node, peeled };
      peeled.push(node);
      node = node.source;
    }
    return { node, peeled };
  };
  const fromAfter = peel(after, before);
  const fromBefore = peel(before, fromAfter.node);
  if (canon(fromBefore.node) !== canon(fromAfter.node)) return null;
  return [...fromAfter.peeled, ...fromBefore.peeled];
}
/** Exact decimal proof that a compact support ball (plus displacement reserve) lies inside the region ball. */
export function compactSupportWithin(node: any, region: Ball): boolean {
  const square = (x: Q.Q) => Q.mul(x, x);
  const reserve =
    node.op === "local_deform"
      ? node.displacement
          .map((x: string) => Q.abs(Q.decimal(x)))
          .reduce(Q.add, Q.zero)
      : Q.zero;
  // Allowed distance between the centres: R - r - |d| with the 1-norm reserve bounding |d|.
  const allowed = Q.sub(
    Q.sub(Q.decimal(region.radius), Q.decimal(node.radius)),
    reserve,
  );
  if (Q.cmp(allowed, Q.zero) < 0) return false;
  const distance2 = node.center
    .map((value: string, axis: number) =>
      square(Q.sub(Q.decimal(value), Q.decimal(region.center[axis]))),
    )
    .reduce(Q.add, Q.zero);
  return Q.cmp(distance2, square(allowed)) <= 0;
}
