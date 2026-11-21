export type Point3 = [number, number, number];
export type ExplosionPart = {
  feature: string;
  owner?: string;
  name?: string;
  assembly?: string;
  /** Bounds in the assembled model, never bounds of an already moved mesh. */
  bounds: [number, number, number, number, number, number];
  /** Assembled vertex extrema along the normalized options.axis, in mm.
   *  Without these, the planner conservatively projects the supplied AABB. */
  projectedBounds?: [number, number];
  /** Vertex extrema along the normalized, orthogonalized options.rowAxis. */
  rowBounds?: [number, number];
};
export type ExplosionTransform = {
  group: string;
  offset: Point3;
  /** All parts share the full animation interval; there are no phases. */
  start: 0;
  end: 1;
};
export type ExplosionPlan = {
  profile: "linear-parts";
  axis: Point3;
  gapMm: number;
  layout: "packed-rows" | "single-row";
  rowAxis: Point3 | null;
  rowGapMm: number;
  order: string[];
  features: Map<string, ExplosionTransform>;
};
export type ExplosionOptions = {
  axis?: Point3;
  gapMm?: number;
  rowAxis?: Point3;
  rowGapMm?: number;
};
type Group = {
  id: string;
  parts: ExplosionPart[];
  min: number;
  max: number;
  rowMin: number;
  rowMax: number;
};
const compareId = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);

function normalizedAxis(axis: Point3 | undefined): Point3 {
  if (!axis || axis.length !== 3 || !axis.every(Number.isFinite))
    return [1, 0, 0];
  // Scale first to normalize even very large or very small finite components.
  const scale = Math.max(...axis.map(Math.abs));
  if (scale === 0) return [1, 0, 0];
  const scaled = axis.map((value) => value / scale);
  const length = Math.hypot(...scaled);
  return scaled.map((value) => (value === 0 ? 0 : value / length)) as Point3;
}

function projectedBounds(
  part: ExplosionPart,
  axis: Point3,
  field: "projectedBounds" | "rowBounds" = "projectedBounds",
): [number, number] {
  const exact = part[field];
  if (
    exact?.length === 2 &&
    exact.every(Number.isFinite) &&
    exact[0] <= exact[1]
  )
    return exact;
  let min = 0,
    max = 0;
  for (let i = 0; i < 3; i++) {
    const a = axis[i] * part.bounds[i],
      b = axis[i] * part.bounds[i + 3];
    min += Math.min(a, b);
    max += Math.max(a, b);
  }
  return [min, max];
}

function perpendicularRowAxis(axis: Point3, rowAxis?: Point3): Point3 | null {
  if (
    !rowAxis ||
    rowAxis.length !== 3 ||
    !rowAxis.every(Number.isFinite) ||
    rowAxis.every((value) => value === 0)
  )
    return null;
  const row = normalizedAxis(rowAxis);
  const cross = (a: Point3, b: Point3): Point3 => [
    a[1] * b[2] - a[2] * b[1],
    a[2] * b[0] - a[0] * b[2],
    a[0] * b[1] - a[1] * b[0],
  ];
  const perpendicular = cross(axis, cross(row, axis));
  if (Math.hypot(...perpendicular) <= 32 * Number.EPSILON) return null;
  return normalizedAxis(perpendicular);
}

/** Earliest/latest feasible starts in the axial-order DAG. Their midpoint is
 *  also feasible, and centers short parts inside otherwise unused row space. */
function packedStarts(groups: Group[], gap: number, rowGap: number): number[] {
  const earliest = groups.map(() => 0),
    next = groups.map(() => [] as number[]),
    widths = groups.map((group) => group.max - group.min);
  for (let j = 0; j < groups.length; j++)
    for (let i = 0; i < j; i++) {
      const a = groups[i],
        b = groups[j];
      if (
        Math.max(a.rowMin, b.rowMin) <=
        Math.min(a.rowMax, b.rowMax) + rowGap
      ) {
        next[i].push(j);
        earliest[j] = Math.max(earliest[j], earliest[i] + widths[i] + gap);
      }
    }
  const length = earliest.reduce(
    (max, start, i) => Math.max(max, start + widths[i]),
    0,
  );
  const latest = widths.map((width) => length - width);
  for (let i = groups.length - 1; i >= 0; i--)
    for (const j of next[i])
      latest[i] = Math.min(latest[i], latest[j] - gap - widths[i]);
  return earliest.map((start, i) => start / 2 + latest[i] / 2);
}

/** Pure presentation layout: each semantic part moves along the same axis.
 *  Single-row layout has exact adjacent gaps; packed rows retain at least gapMm
 *  between parts whose row envelopes overlap (including rowGapMm padding).
 *  Vertex extrema describe the supplied mesh; AABB projections are conservative.
 *  Neither supplies a certified CAD surface distance or a physical teardown path. */
export function planExplosion(
  parts: ExplosionPart[],
  options: ExplosionOptions = {},
): ExplosionPlan {
  const axis = normalizedAxis(options.axis),
    rowAxis = perpendicularRowAxis(axis, options.rowAxis),
    rowGapMm = rowAxis
      ? Number.isFinite(options.rowGapMm) && options.rowGapMm! >= 0
        ? options.rowGapMm!
        : 2
      : 0,
    gapMm =
      Number.isFinite(options.gapMm) && options.gapMm! > 0
        ? options.gapMm!
        : 30,
    groups = new Map<string, Group>(),
    features = new Map<string, ExplosionTransform>();
  for (const part of parts) {
    if (
      !part.feature ||
      part.bounds.length !== 6 ||
      !part.bounds.every(Number.isFinite) ||
      !part.bounds.slice(0, 3).every((value, i) => value <= part.bounds[i + 3])
    )
      continue;
    const [min, max] = projectedBounds(part, axis);
    if (!Number.isFinite(min) || !Number.isFinite(max)) continue;
    const [rowMin, rowMax] = rowAxis
      ? projectedBounds(part, rowAxis, "rowBounds")
      : [0, 0];
    if (!Number.isFinite(rowMin) || !Number.isFinite(rowMax)) continue;
    const id = part.owner || `feature:${part.feature}`;
    const group = groups.get(id);
    if (group) {
      group.parts.push(part);
      group.min = Math.min(group.min, min);
      group.max = Math.max(group.max, max);
      group.rowMin = Math.min(group.rowMin, rowMin);
      group.rowMax = Math.max(group.rowMax, rowMax);
    } else groups.set(id, { id, parts: [part], min, max, rowMin, rowMax });
  }
  const ordered = [...groups.values()].sort(
    (a, b) =>
      a.min / 2 + a.max / 2 - (b.min / 2 + b.max / 2) || compareId(a.id, b.id),
  );
  // First pack projected envelopes, then translate the entire result until
  // even a part initially separated by a large gap moves forward by gapMm.
  // This common translation preserves every requested final inter-part gap.
  let cursor = ordered[0]?.min ?? 0;
  const packed = rowAxis ? packedStarts(ordered, gapMm, rowGapMm) : null;
  const offsets = ordered.map((group, index) => {
    if (packed) return cursor + packed[index] - group.min;
    const offset = cursor - group.min;
    cursor += group.max - group.min + gapMm;
    return offset;
  });
  const base =
    gapMm - offsets.reduce((min, offset) => Math.min(min, offset), 0);
  for (let index = 0; index < ordered.length; index++) {
    const group = ordered[index],
      distance = offsets[index] + base,
      transform: ExplosionTransform = {
        group: group.id,
        offset: axis.map((value) => value * distance) as Point3,
        start: 0,
        end: 1,
      };
    // All output meshes belonging to one part share the identical transform.
    for (const part of group.parts.sort((a, b) =>
      compareId(a.feature, b.feature),
    ))
      features.set(part.feature, transform);
  }
  return {
    profile: "linear-parts",
    axis,
    gapMm,
    layout: rowAxis ? "packed-rows" : "single-row",
    rowAxis,
    rowGapMm,
    order: ordered.map((group) => group.id),
    features,
  };
}

/** Absolute positioning retraces the same path on closing, without drift. */
export function explosionPosition(
  origin: readonly number[],
  info: ExplosionTransform,
  amount: number,
): Point3 {
  const progress = Number.isFinite(amount)
    ? Math.max(0, Math.min(1, amount))
    : 0;
  if (!progress) return [origin[0], origin[1], origin[2]];
  return [
    origin[0] + info.offset[0] * progress,
    origin[1] + info.offset[1] * progress,
    origin[2] + info.offset[2] * progress,
  ];
}
