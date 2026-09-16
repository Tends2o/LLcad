"""Level-set curvature of implicit fields (Bauplan eq. 41) with certified regularity checks.

The Hessian is a central finite difference of the compiled field. Regularity and
smoothness of the cell around the point are decided by interval branch analysis
(intervals.py), so a CSG crease or a vanishing gradient is refused instead of
being divided through. The sign convention matches OCCT's oriented surface
normals: negative inside, outward normal n = grad f / |grad f|, sphere H = -1/r.
"""
import math
import numpy as np
from geometry import require
from intervals import evaluate_interval


def hessian(compiled, point, h):
    p = np.asarray(point, float)
    H = np.zeros((3, 3))
    f0 = compiled(p)
    for i in range(3):
        ei = np.eye(3)[i] * h
        H[i, i] = (compiled(p + ei) - 2 * f0 + compiled(p - ei)) / (h * h)
        for j in range(i + 1, 3):
            ej = np.eye(3)[j] * h
            H[i, j] = H[j, i] = (compiled(p + ei + ej) - compiled(p + ei - ej) - compiled(p - ei + ej) + compiled(p - ei - ej)) / (4 * h * h)
    return f0, H


def implicit_curvature(node, compiled, point, step):
    p = np.asarray(point, float)
    require(p.shape == (3,) and np.isfinite(p).all() and np.abs(p).max() <= 1e6, 'Ungültiger Auswertepunkt.', 'INVALID_SCHEMA')
    require(math.isfinite(step) and 1e-7 <= step <= 1.0, 'Differenzenschrittweite außerhalb des Vertrags.', 'PRECISION_UNSUPPORTED')
    cell = evaluate_interval(node, (p - 2 * step)[None, :], (p + 2 * step)[None, :])
    lower, upper = cell.norm_bounds()
    smooth = bool(cell.smooth[0])
    require(smooth, 'Das Feld ist im Auswertebereich nicht nachweislich glatt (CSG-Kante, Betrag oder Kegelspitze).', 'PRECISION_UNSUPPORTED')
    require(float(lower[0]) > 0, 'Der Gradient ist im Auswertebereich nicht nachweislich von null verschieden.', 'PRECISION_UNSUPPORTED')
    f0, H = hessian(compiled, p, step)
    g = compiled.gradient(p, step)
    norm = float(np.linalg.norm(g))
    require(norm >= float(lower[0]) * 0.5, 'Numerischer Gradient widerspricht der Intervallschranke.', 'PRECISION_UNSUPPORTED')
    n = g / norm
    trace = float(np.trace(H))
    quadratic = float(g @ H @ g)
    mean = -(norm * norm * trace - quadratic) / (2 * norm ** 3)
    adjugate = np.array([[H[1, 1] * H[2, 2] - H[1, 2] * H[2, 1], H[0, 2] * H[2, 1] - H[0, 1] * H[2, 2], H[0, 1] * H[1, 2] - H[0, 2] * H[1, 1]],
                         [H[1, 2] * H[2, 0] - H[1, 0] * H[2, 2], H[0, 0] * H[2, 2] - H[0, 2] * H[2, 0], H[0, 2] * H[1, 0] - H[0, 0] * H[1, 2]],
                         [H[1, 0] * H[2, 1] - H[1, 1] * H[2, 0], H[0, 1] * H[2, 0] - H[0, 0] * H[2, 1], H[0, 0] * H[1, 1] - H[0, 1] * H[1, 0]]])
    gaussian = float(g @ adjugate @ g) / norm ** 4
    discriminant = max(mean * mean - gaussian, 0.0)
    principal = sorted([mean - math.sqrt(discriminant), mean + math.sqrt(discriminant)])
    require(all(math.isfinite(x) for x in [mean, gaussian, *principal]), 'Nichtendliche Krümmung.', 'PRECISION_UNSUPPORTED')
    # Second-order central differences carry an O(step^2) truncation term; report it instead of hiding it.
    return {'kind': 'implicit', 'point_mm': p.tolist(), 'field_value': f0, 'level_set': 'through_the_query_point',
            'gradient': g.tolist(), 'gradient_norm': norm, 'direction_kind': 'outward_level_set_normal', 'direction': n.tolist(),
            'mean_curvature_per_mm': mean, 'gaussian_curvature_per_mm2': gaussian, 'principal_curvatures_per_mm': principal,
            'signed_convention': 'negative_inside_outward_normal_matching_OCCT_orientation',
            'method': 'central_finite_difference_hessian', 'step_mm': step, 'truncation_order': 'O(step^2)',
            'regularity': {'certified_gradient_lower_bound': float(lower[0]), 'certified_gradient_upper_bound': float(upper[0]),
                           'smoothness': 'certified_by_interval_branch_analysis_on_2_step_cell'}}
