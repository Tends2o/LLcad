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
