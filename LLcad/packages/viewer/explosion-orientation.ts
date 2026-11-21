import type { Point3 } from "./explosion.js";

function normalized(vector: Point3): Point3 | null {
  if (vector.length !== 3 || !vector.every(Number.isFinite)) return null;
  const scale = Math.max(...vector.map(Math.abs));
  if (scale === 0) return null;
  const scaled = vector.map((value) => value / scale);
  const length = Math.hypot(...scaled);
  return scaled.map((value) => (value === 0 ? 0 : value / length)) as Point3;
}
const cross = (a: Point3, b: Point3): Point3 => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];

/** Dominant eigenvector of a real symmetric 3-by-3 matrix (Jacobi rotations). */
function principalDirection(matrix: number[][]): Point3 {
  const vectors = [
    [1, 0, 0],
    [0, 1, 0],
    [0, 0, 1],
  ];
  const pairs = [
    [0, 1],
    [0, 2],
    [1, 2],
  ];
  const tolerance =
    16 * Number.EPSILON * (matrix[0][0] + matrix[1][1] + matrix[2][2]);
  for (let iteration = 0; iteration < 32; iteration++) {
    let p = 0,
      q = 1;
    for (const [a, b] of pairs)
      if (Math.abs(matrix[a][b]) > Math.abs(matrix[p][q])) {
        p = a;
        q = b;
      }
    if (Math.abs(matrix[p][q]) <= tolerance) break;
    const angle = Math.atan2(2 * matrix[p][q], matrix[q][q] - matrix[p][p]) / 2;
    const c = Math.cos(angle),
      s = Math.sin(angle);
    const pp = matrix[p][p],
      qq = matrix[q][q],
      pq = matrix[p][q];
    matrix[p][p] = c * c * pp - 2 * c * s * pq + s * s * qq;
    matrix[q][q] = s * s * pp + 2 * c * s * pq + c * c * qq;
    matrix[p][q] = matrix[q][p] = 0;
    for (let k = 0; k < 3; k++) {
      if (k !== p && k !== q) {
        const kp = matrix[k][p],
          kq = matrix[k][q];
        matrix[k][p] = matrix[p][k] = c * kp - s * kq;
        matrix[k][q] = matrix[q][k] = s * kp + c * kq;
      }
      const vp = vectors[k][p],
        vq = vectors[k][q];
      vectors[k][p] = c * vp - s * vq;
      vectors[k][q] = s * vp + c * vq;
    }
  }
  let largest = 0;
  for (let i = 1; i < 3; i++)
    if (matrix[i][i] > matrix[largest][largest] + tolerance) largest = i;
  const direction = normalized(vectors.map((row) => row[largest]) as Point3)!;
  let signAxis = 0;
  // Numerically tied components keep the X, then Y, then Z preference.
  for (let i = 1; i < 3; i++)
    if (
      Math.abs(direction[i]) >
      Math.abs(direction[signAxis]) + 32 * Number.EPSILON
    )
      signAxis = i;
  const sign = direction[signAxis] < 0 ? -1 : 1;
  return direction.map((value) => (value === 0 ? 0 : value * sign)) as Point3;
}

/** Area-weighted normal tensor of the supplied triangles, independent of winding.
 *  For a flat lens its dominant direction is the surface normal. A symmetric
 *  non-flat object need not have a unique geometrically meaningful lens axis. */
export function dominantSurfaceNormal(
  positions: ArrayLike<number>,
  indices: ArrayLike<number> | null,
): Point3 | null {
  const count = Math.floor(positions.length / 3);
  if (count < 3) return null;
  const lower = [Infinity, Infinity, Infinity],
    upper = [-Infinity, -Infinity, -Infinity];
  let coordinateScale = 0;
  for (let vertex = 0; vertex < count; vertex++)
    for (let axis = 0; axis < 3; axis++) {
      const value = positions[vertex * 3 + axis];
      if (!Number.isFinite(value)) continue;
      lower[axis] = Math.min(lower[axis], value);
      upper[axis] = Math.max(upper[axis], value);
      coordinateScale = Math.max(coordinateScale, Math.abs(value));
    }
  let scale = Math.max(...upper.map((value, axis) => value - lower[axis]));
  if (!Number.isFinite(scale)) scale = coordinateScale;
  if (!scale) return null;
  // A common scale changes only the magnitude of the tensor, not its eigenvectors,
  // and avoids overflow/underflow in cross products for very large/small models.
  const tensor = [0, 0, 0, 0, 0, 0],
    compensation = [0, 0, 0, 0, 0, 0];
  const add = (index: number, value: number) => {
    const corrected = value - compensation[index],
      next = tensor[index] + corrected;
    compensation[index] = next - tensor[index] - corrected;
    tensor[index] = next;
  };
  const length = indices ? indices.length : count;
  let triangles = 0;
  for (let first = 0; first + 2 < length; first += 3) {
    const ids = [0, 1, 2].map((i) =>
      indices ? indices[first + i] : first + i,
    );
    if (ids.some((id) => !Number.isInteger(id) || id < 0 || id >= count))
      continue;
    const points = ids.map((id) => [
      positions[id * 3],
      positions[id * 3 + 1],
      positions[id * 3 + 2],
    ]);
    if (points.some((point) => !point.every(Number.isFinite))) continue;
    const edges = [1, 2].map(
      (i) =>
        points[i].map((value, axis) => {
          const difference = value - points[0][axis];
          return Number.isFinite(difference)
            ? difference / scale
            : value / scale - points[0][axis] / scale;
        }) as Point3,
    );
    const n = cross(edges[0], edges[1]),
      weight = Math.hypot(...n);
    if (weight === 0 || !Number.isFinite(weight)) continue;
    const [x, y, z] = n.map((value) => value / weight);
    // weight * unitNormal * unitNormal^T = n * n^T / |n|.
    [x * x, x * y, x * z, y * y, y * z, z * z].forEach((value, i) =>
      add(i, weight * value),
    );
    triangles++;
  }
  if (!triangles) return null;
  const [xx, xy, xz, yy, yz, zz] = tensor;
  return principalDirection([
    [xx, xy, xz],
    [xy, yy, yz],
    [xz, yz, zz],
  ]);
}

/** Camera direction points from target to camera. With this up vector, a
 *  positive axis translation projects horizontally to the right in the view. */
export function explosionViewBasis(axis: Point3): {
  up: Point3;
  direction: Point3;
} {
  const along = normalized(axis) ?? [1, 0, 0];
  // Equivalent to reference - dot(reference, along)*along, without cancellation
  // of the small vertical component when the axis is almost parallel to Z.
  const orthogonal = (reference: Point3): Point3 =>
    cross(along, cross(reference, along));
  let vertical = orthogonal([0, 0, 1]);
  if (Math.hypot(...vertical) <= 32 * Number.EPSILON)
    vertical = orthogonal([0, 1, 0]);
  const up = normalized(vertical)!;
  const across = cross(along, up);
  const direction = normalized(
    across.map((value, i) => value + 0.6 * along[i]) as Point3,
  )!;
  return { up, direction };
}
