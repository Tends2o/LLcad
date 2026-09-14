import { requireThat } from "../semantic-ir/errors.js";
import * as Q from "./bernstein.js";

/** Exact decimal-matrix analysis; separate from the kernel's binary arithmetic. */
export function affineContract(matrix: string[][], translation: string[]) {
  requireThat(
    matrix.length === 3 &&
      matrix.every((r) => r.length === 3) &&
      translation.length === 3 &&
      [...matrix.flat(), ...translation].every(
        (x) => Number.isFinite(Number(x)) && Math.abs(Number(x)) <= 1e6,
      ),
    "GEOMETRY_INVALID",
    "Affine Abbildung benötigt endliche 3×3-Koeffizienten und Translation im Bereich ±1e6.",
  );
  const a = matrix.map((r) => r.map(Q.decimal));
  const cross = (v: Q.Q[], w: Q.Q[]) =>
    [0, 1, 2].map((i) =>
      Q.sub(
        Q.mul(v[(i + 1) % 3], w[(i + 2) % 3]),
        Q.mul(v[(i + 2) % 3], w[(i + 1) % 3]),
      ),
    );
  const rows = [cross(a[1], a[2]), cross(a[2], a[0]), cross(a[0], a[1])];
  const det = a[0].map((x, i) => Q.mul(x, rows[0][i])).reduce(Q.add, Q.zero);
  requireThat(
    det.n !== 0n,
    "GEOMETRY_INVALID",
    "Singuläre affine Transformation ist für Festkörper und Feldtransport unzulässig.",
  );
  const inverse = [0, 1, 2].map((i) => rows.map((r) => Q.div(r[i], det)));
  // The entrywise 1-norm bounds the spectral norm. Both quantities are exact.
  const norm = a.flat().map(Q.abs).reduce(Q.add, Q.zero),
    inverseBound = inverse.flat().map(Q.abs).reduce(Q.add, Q.zero);
  requireThat(
    Q.cmp(inverseBound, Q.fraction(1000000n)) <= 0 &&
      Q.cmp(Q.mul(norm, inverseBound), Q.fraction(100000000n)) <= 0,
    "PRECISION_UNSUPPORTED",
    "Affine Abbildung überschreitet die Grenzen für inversen Maßstab oder Konditionierung.",
  );
  return {
    orientation: det.n < 0n ? "reversing" : "preserving",
    determinant: { numerator: det.n.toString(), denominator: det.d.toString() },
    inverse_spectral_norm_upper: Q.upper(inverseBound),
    field_distance_scale_lower: Q.lower(Q.div(Q.one, inverseBound)),
    normal_rule: "inverse_transpose_then_normalize",
    native_floating_point_error_bound: null,
  };
}
