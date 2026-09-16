/** Axis-aligned bounding volume hierarchy over feature extents (Bauplan 15.5): a candidate
 * filter for spatial queries, never a proof of containment. Built per revision from the
 * stored world bounds of evaluated features. */
export type Box = [number, number, number, number, number, number];
type Node =
  { box: Box; leaf: string[] } | { box: Box; left: Node; right: Node };
const unionBox = (a: Box, b: Box): Box => [
  Math.min(a[0], b[0]),
  Math.min(a[1], b[1]),
  Math.min(a[2], b[2]),
  Math.max(a[3], b[3]),
  Math.max(a[4], b[4]),
  Math.max(a[5], b[5]),
];
const overlaps = (a: Box, b: Box) =>
  a[0] <= b[3] &&
  b[0] <= a[3] &&
  a[1] <= b[4] &&
  b[1] <= a[4] &&
  a[2] <= b[5] &&
  b[2] <= a[5];
export class BVH {
  private root: Node | null = null;
  readonly size: number;
  constructor(items: { id: string; box: Box }[]) {
    const valid = items.filter(
      (i) => i.box.length === 6 && i.box.every(Number.isFinite),
    );
    this.size = valid.length;
    if (valid.length) this.root = this.build(valid, 0);
  }
  private build(items: { id: string; box: Box }[], depth: number): Node {
    const box = items.map((i) => i.box).reduce(unionBox);
    if (items.length <= 4 || depth > 24)
      return { box, leaf: items.map((i) => i.id) };
    const axis = depth % 3;
    const sorted = [...items].sort(
      (a, b) => a.box[axis] + a.box[axis + 3] - (b.box[axis] + b.box[axis + 3]),
    );
    const middle = Math.floor(sorted.length / 2);
    return {
      box,
      left: this.build(sorted.slice(0, middle), depth + 1),
      right: this.build(sorted.slice(middle), depth + 1),
    };
  }
  /** Feature ids whose stored extents intersect the query box. */
  query(query: Box): string[] {
    const found: string[] = [];
    const visit = (node: Node | null) => {
      if (!node || !overlaps(node.box, query)) return;
      if ("leaf" in node) found.push(...node.leaf);
      else {
        visit(node.left);
        visit(node.right);
      }
    };
    visit(this.root);
    return found;
  }
  point(p: [number, number, number]) {
    return this.query([p[0], p[1], p[2], p[0], p[1], p[2]]);
  }
}
const cache = new Map<string, BVH>();
/** Per-revision index; revisions are immutable so the cache key is the revision id. */
export function revisionIndex(revision: any): BVH {
  const key = revision.id + ":" + (revision.geometry ? "g" : "n");
  const hit = cache.get(key);
  if (hit) return hit;
  const index = new BVH(
    revision.ir.features.flatMap((f: any) => {
      const box = revision.geometry?.facts?.[f.id]?.bounds;
      return Array.isArray(box) && box.length === 6
        ? [{ id: f.id, box: box as Box }]
        : [];
    }),
  );
  if (cache.size >= 64) cache.delete(cache.keys().next().value!);
  cache.set(key, index);
  return index;
}
