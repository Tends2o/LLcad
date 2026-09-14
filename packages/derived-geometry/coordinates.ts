/** Derived mesh storage only. The authoritative world vertices remain untouched. */
export function relativePositions(vertices: number[][], unit = 1) {
  if (!vertices.length || !Number.isFinite(unit) || unit <= 0)
    throw new RangeError("Invalid mesh coordinate domain.");
  const min = [Infinity, Infinity, Infinity],
    max = [-Infinity, -Infinity, -Infinity];
  for (const p of vertices) {
    if (p.length !== 3 || !p.every(Number.isFinite))
      throw new RangeError("Mesh positions must be finite triples.");
    for (let i = 0; i < 3; i++) {
      min[i] = Math.min(min[i], p[i]);
      max[i] = Math.max(max[i], p[i]);
    }
  }
  const origin = min.map((v, i) => v / 2 + max[i] / 2);
  const positions = new Float32Array(vertices.length * 3);
  let maxCoordinateError = 0;
  vertices.forEach((p, j) => {
    for (let i = 0; i < 3; i++) {
      const k = 3 * j + i;
      positions[k] = (p[i] - origin[i]) / unit;
      if (!Number.isFinite(positions[k]))
        throw new RangeError("Relative mesh coordinates overflow Float32.");
      maxCoordinateError = Math.max(
        maxCoordinateError,
        Math.abs(positions[k] * unit + origin[i] - p[i]),
      );
    }
  });
  return { origin, positions, maxCoordinateError };
}
