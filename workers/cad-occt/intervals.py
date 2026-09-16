"""Rigorous interval evaluation of the field AST with interval gradients.

Every binary64 operation is widened outward by one unit in the last place, so an
interval contains the exact mathematical value on the whole cell regardless of
rounding (IEEE 754 correctly rounded +,-,*,/,sqrt). Trigonometric functions use
numpy implementations, whose results are widened by TRIG_ULPS units plus an
absolute margin; the certificate declares this assumption instead of hiding it.

Gradients use forward-mode interval differentiation plus tracked bounds on the
gradient norm (triangle inequality through the tree), which keeps the unit
gradient of exact distance primitives visible under compact local edits.
Piecewise operators (min, max, abs, clamp) mark a cell as non-smooth when both
branches are possible; a non-smooth cell never receives a derivative
certificate. Nothing here evaluates executable input: nodes are the same
registered JSON operators as fields.py.
"""
import json
import math
import numpy as np
from geometry import require

TRIG_ULPS = 16
TRIG_ABSOLUTE = 1e-15
MAX_CELLS = 400000
MAX_LEVELS = 12
INF = float('inf')
ERROR_MODEL = ('IEEE754_binary64_directed_rounding_per_operation; numpy sin/cos widened by %d ulp plus %g'
               % (TRIG_ULPS, TRIG_ABSOLUTE))


def _down(x):
    return np.nextafter(x, -INF)


def _up(x):
    return np.nextafter(x, INF)


def _clean(lo, hi):
    """Undefined products (0*inf) become the whole line; never a silent NaN."""
    lo = np.where(np.isnan(lo), -INF, lo)
    hi = np.where(np.isnan(hi), INF, hi)
    return lo, hi


class IV:
    """Batch of closed intervals [lo, hi] as float arrays of equal shape."""
    __slots__ = ('lo', 'hi')

    def __init__(self, lo, hi):
        self.lo = np.asarray(lo, float)
        self.hi = np.asarray(hi, float)

    @staticmethod
    def exact(value, shape):
        v = np.full(shape, float(value))
        return IV(v, v.copy())

    @staticmethod
    def whole(shape):
        return IV(np.full(shape, -INF), np.full(shape, INF))

    def __add__(self, o):
        if isinstance(o, IV):
            return IV(_down(self.lo + o.lo), _up(self.hi + o.hi))
        return IV(_down(self.lo + o), _up(self.hi + o))

    __radd__ = __add__

    def __neg__(self):
        return IV(-self.hi, -self.lo)

    def __sub__(self, o):
        if isinstance(o, IV):
            return IV(_down(self.lo - o.hi), _up(self.hi - o.lo))
        return IV(_down(self.lo - o), _up(self.hi - o))

    def __rsub__(self, o):
        return IV(_down(o - self.hi), _up(o - self.lo))

    def __mul__(self, o):
        with np.errstate(invalid='ignore'):
            if isinstance(o, IV):
                products = np.stack([self.lo * o.lo, self.lo * o.hi, self.hi * o.lo, self.hi * o.hi])
                bad = np.isnan(products).any(axis=0)
                products = np.where(np.isnan(products), 0.0, products)
                return IV(np.where(bad, -INF, _down(products.min(axis=0))), np.where(bad, INF, _up(products.max(axis=0))))
            a, b = self.lo * o, self.hi * o
            lo, hi = _clean(np.minimum(a, b), np.maximum(a, b))
            return IV(_down(lo), _up(hi))

    __rmul__ = __mul__

    def sqr(self):
        with np.errstate(invalid='ignore'):
            lo2, hi2 = self.lo * self.lo, self.hi * self.hi
        straddles = (self.lo <= 0) & (self.hi >= 0)
        lo = np.where(straddles, 0.0, _down(np.minimum(lo2, hi2)))
        return IV(lo, _up(np.maximum(lo2, hi2)))

    def sqrt(self):
        require(bool(np.all(self.hi >= 0)), 'Wurzel eines negativen Intervalls.')
        return IV(_down(np.sqrt(np.maximum(self.lo, 0.0))), _up(np.sqrt(np.maximum(self.hi, 0.0))))

    def abs(self):
        straddles = (self.lo <= 0) & (self.hi >= 0)
        lo = np.where(straddles, 0.0, np.minimum(np.abs(self.lo), np.abs(self.hi)))
        return IV(lo, np.maximum(np.abs(self.lo), np.abs(self.hi)))

    def min(self, o):
        return IV(np.minimum(self.lo, o.lo), np.minimum(self.hi, o.hi))

    def max(self, o):
        return IV(np.maximum(self.lo, o.lo), np.maximum(self.hi, o.hi))

    def clip(self, lo, hi):
        return IV(np.maximum(self.lo, lo), np.minimum(self.hi, hi))

    def contains_zero(self):
        return (self.lo <= 0) & (self.hi >= 0)

    def magnitude_max(self):
        return np.maximum(np.abs(self.lo), np.abs(self.hi))

    def magnitude_min(self):
        return np.where(self.contains_zero(), 0.0, np.minimum(np.abs(self.lo), np.abs(self.hi)))

    def finite(self):
        return np.isfinite(self.lo) & np.isfinite(self.hi)


def _trig(fn, x, period_shift):
    """Interval sin/cos: endpoints widened, extrema included when a critical point may lie inside."""
    lo, hi = x.lo, x.hi
    finite = np.isfinite(lo) & np.isfinite(hi)
    lo_s, hi_s = np.where(finite, lo, 0.0), np.where(finite, hi, 0.0)
    a, b = fn(lo_s), fn(hi_s)
    out_lo, out_hi = np.minimum(a, b), np.maximum(a, b)
    for _ in range(TRIG_ULPS):
        out_lo, out_hi = _down(out_lo), _up(out_hi)
    out_lo, out_hi = out_lo - TRIG_ABSOLUTE, out_hi + TRIG_ABSOLUTE
    margin = 1e-12 * np.maximum(1.0, np.maximum(np.abs(lo_s), np.abs(hi_s)))
    wide = ~finite | ((hi_s - lo_s) >= 2 * math.pi - 1e-9)
    kmax = np.ceil((lo_s - margin - period_shift) / (2 * math.pi))
    has_max = wide | (period_shift + 2 * math.pi * kmax <= hi_s + margin)
    kmin = np.ceil((lo_s - margin - period_shift - math.pi) / (2 * math.pi))
    has_min = wide | (period_shift + math.pi + 2 * math.pi * kmin <= hi_s + margin)
    return IV(np.where(has_min, -1.0, np.maximum(out_lo, -1.0)), np.where(has_max, 1.0, np.minimum(out_hi, 1.0)))


def iv_sin(x):
    return _trig(np.sin, x, math.pi / 2)


def iv_cos(x):
    return _trig(np.cos, x, 0.0)


class GV:
    """Interval value, interval gradient, per-cell smoothness and gradient-norm bounds."""
    __slots__ = ('v', 'g', 'smooth', 'nlo', 'nhi')

    def __init__(self, v, g, smooth, nlo=None, nhi=None):
        self.v, self.g, self.smooth = v, g, smooth
        shape = v.lo.shape
        self.nlo = np.zeros(shape) if nlo is None else np.asarray(nlo, float)
        self.nhi = np.full(shape, INF) if nhi is None else np.asarray(nhi, float)

    def norm_bounds(self):
        """Certified [lower, upper] bounds of |gradient| combining components and tracked bounds."""
        total_lo = np.zeros(self.v.lo.shape)
        total_hi = np.zeros(self.v.lo.shape)
        for component in self.g:
            m = component.magnitude_min()
            total_lo = _down(total_lo + _down(m * m))
            M = component.magnitude_max()
            with np.errstate(invalid='ignore'):
                total_hi = _up(total_hi + _up(M * M))
        lo = np.maximum(_down(np.sqrt(np.maximum(total_lo, 0.0))), self.nlo)
        hi = np.minimum(_up(np.sqrt(np.where(np.isnan(total_hi), INF, total_hi))), self.nhi)
        return lo, hi


def _zero_grad(shape):
    return [IV.exact(0.0, shape) for _ in range(3)]


def g_const(value, shape):
    return GV(IV.exact(value, shape), _zero_grad(shape), np.ones(shape, bool), 0.0, 0.0)


def g_add(a, b):
    alo, ahi = a.norm_bounds()
    blo, bhi = b.norm_bounds()
    nlo = np.maximum(0.0, np.maximum(_down(alo - bhi), _down(blo - ahi)))
    return GV(a.v + b.v, [x + y for x, y in zip(a.g, b.g)], a.smooth & b.smooth, nlo, _up(ahi + bhi))


def g_neg(a):
    return GV(-a.v, [-x for x in a.g], a.smooth, a.nlo, a.nhi)


def g_sub(a, b):
    return g_add(a, g_neg(b))


def g_scale(a, c):
    alo, ahi = a.norm_bounds()
    return GV(a.v * c, [x * c for x in a.g], a.smooth, _down(alo * abs(c)), _up(ahi * abs(c)))


def g_mul(a, b):
    alo, ahi = a.norm_bounds()
    blo, bhi = b.norm_bounds()
    amin, amax = a.v.magnitude_min(), a.v.magnitude_max()
    bmin, bmax = b.v.magnitude_min(), b.v.magnitude_max()
    with np.errstate(invalid='ignore'):
        nlo = np.maximum(0.0, np.maximum(_down(amin * blo - bmax * ahi), _down(bmin * alo - amax * bhi)))
        nhi = _up(amax * bhi + bmax * ahi)
    nlo, nhi = _clean(nlo, nhi)
    return GV(a.v * b.v, [x * b.v + a.v * y for x, y in zip(a.g, b.g)], a.smooth & b.smooth, nlo, nhi)


def g_sqr(a):
    return g_mul(a, a)


def g_sqrt(a):
    root = a.v.sqrt()
    positive = a.v.lo > 0
    tiny = np.finfo(float).tiny
    inv_lo = np.where(positive, _down(0.5 / np.maximum(root.hi, tiny)), -INF)
    inv_hi = np.where(positive, _up(0.5 / np.maximum(root.lo, tiny)), INF)
    inverse = IV(inv_lo, inv_hi)
    alo, ahi = a.norm_bounds()
    nlo = np.where(positive, _down(alo * inv_lo), 0.0)
    with np.errstate(invalid='ignore'):
        nhi = np.where(positive, _up(ahi * inv_hi), INF)
    return GV(root, [x * inverse for x in a.g], a.smooth & positive, nlo, nhi)


def g_abs(a):
    pos, neg = a.v.lo > 0, a.v.hi < 0
    grad = [IV(np.where(pos, x.lo, np.where(neg, -x.hi, np.minimum(x.lo, -x.hi))),
               np.where(pos, x.hi, np.where(neg, -x.lo, np.maximum(x.hi, -x.lo)))) for x in a.g]
    return GV(a.v.abs(), grad, a.smooth & (pos | neg), a.nlo, a.nhi)


def _select(a, b, take_a, take_b):
    grad = [IV(np.where(take_a, x.lo, np.where(take_b, y.lo, np.minimum(x.lo, y.lo))),
               np.where(take_a, x.hi, np.where(take_b, y.hi, np.maximum(x.hi, y.hi)))) for x, y in zip(a.g, b.g)]
    nlo = np.where(take_a, a.nlo, np.where(take_b, b.nlo, np.minimum(a.nlo, b.nlo)))
    nhi = np.where(take_a, a.nhi, np.where(take_b, b.nhi, np.maximum(a.nhi, b.nhi)))
    # A branch that is certainly not taken cannot spoil smoothness of the selected one.
    return grad, (take_a & a.smooth) | (take_b & b.smooth), nlo, nhi


def g_min(a, b):
    grad, smooth, nlo, nhi = _select(a, b, a.v.hi < b.v.lo, b.v.hi < a.v.lo)
    return GV(a.v.min(b.v), grad, smooth, nlo, nhi)


def g_max(a, b):
    grad, smooth, nlo, nhi = _select(a, b, a.v.lo > b.v.hi, b.v.lo > a.v.hi)
    return GV(a.v.max(b.v), grad, smooth, nlo, nhi)


def _g_trig(value, derivative, a):
    alo, ahi = a.norm_bounds()
    dmin, dmax = derivative.magnitude_min(), derivative.magnitude_max()
    return GV(value, [x * derivative for x in a.g], a.smooth, _down(alo * dmin), _up(ahi * dmax))


def g_sin(a):
    return _g_trig(iv_sin(a.v), iv_cos(a.v), a)


def g_cos(a):
    return _g_trig(iv_cos(a.v), -iv_sin(a.v), a)


def g_norm(vec, unit_gradient=None):
    """|v| with gradient sum(u_i grad v_i), u_i = v_i/|v| clipped to [-1,1]; smooth off the origin.

    unit_gradient=(smin, smax) states that v is the query point mapped through a
    linear chain with those singular value bounds, so |grad| lies in [smin, smax].
    """
    total = vec[0].v.sqr() + vec[1].v.sqr() + vec[2].v.sqr()
    norm = total.sqrt()
    positive = norm.lo > 0
    shape = norm.lo.shape
    grad = [IV.exact(0.0, shape) for _ in range(3)]
    units = []
    for component in vec:
        with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
            u_lo = np.where(positive, _down(np.minimum(component.v.lo / norm.lo, component.v.lo / norm.hi)), -1.0)
            u_hi = np.where(positive, _up(np.maximum(component.v.hi / norm.lo, component.v.hi / norm.hi)), 1.0)
        u = IV(u_lo, u_hi).clip(-1.0, 1.0)
        units.append(u)
        grad = [gk + u * ck for gk, ck in zip(grad, component.g)]
    smooth = positive
    for component in vec:
        smooth = smooth & component.smooth
    if unit_gradient is not None:
        nlo = np.where(positive, unit_gradient[0], 0.0)
        nhi = np.full(shape, unit_gradient[1])
    else:
        nlo = np.zeros(shape)
        nhi = np.zeros(shape)
        for component in vec:
            _, chi = component.norm_bounds()
            nhi = _up(nhi + chi)
    return GV(norm, grad, smooth, nlo, nhi)


def g_wendland(q):
    """phi(q)=(1-q)^4(4q+1)=1-10q^2+20q^3-15q^4+4q^5 for q<1, else 0. C^2 everywhere."""
    shape = q.v.lo.shape
    one_minus = IV.exact(1.0, shape) - q.v
    inside = q.v.hi < 1
    outside = q.v.lo >= 1
    del one_minus, inside
    qa = np.clip(q.v.lo, 0.0, 1.0)
    qb = np.clip(q.v.hi, 0.0, 1.0)

    def phi_of(t):
        u = IV(t, t.copy())
        one_minus = IV.exact(1.0, shape) - u
        c2 = one_minus.sqr()
        return c2 * c2 * (u * 4.0 + 1.0)

    def dphi_of(t):
        u = IV(t, t.copy())
        one_minus = IV.exact(1.0, shape) - u
        return u * one_minus * one_minus.sqr() * -20.0
    # phi decreases monotonically on [0,1]: exact endpoint evaluation bounds the whole cell.
    at_a, at_b = phi_of(qa), phi_of(qb)
    phi = IV(np.where(outside, 0.0, np.minimum(at_b.lo, 0.0) * 0 + at_b.lo), np.where(outside, 0.0, at_a.hi))
    phi = IV(np.maximum(phi.lo, 0.0), np.minimum(phi.hi, 1.0))
    # phi'(q) = -20 q (1-q)^3 has its single interior extremum at q = 1/4.
    da, db = dphi_of(qa), dphi_of(qb)
    dlo = np.minimum(da.lo, db.lo)
    dhi = np.maximum(da.hi, db.hi)
    quarter = (qa <= 0.25) & (qb >= 0.25)
    dq = dphi_of(np.full(shape, 0.25))
    dlo = np.where(quarter, np.minimum(dlo, dq.lo), dlo)
    dhi = np.where(quarter, np.maximum(dhi, dq.hi), dhi)
    dphi = IV(np.where(outside, 0.0, dlo), np.where(outside, 0.0, dhi))
    qlo, qhi = q.norm_bounds()
    dmax = dphi.magnitude_max()
    # The radial derivative vanishes at q=0, so the composition stays C^2 at the centre.
    return GV(phi, [x * dphi for x in q.g], np.ones(shape, bool), 0.0, _up(qhi * dmax))


def _number(value):
    result = float(value)
    require(math.isfinite(result), 'Nichtendliche Feldkonstante.')
    return result


def _vector(value):
    result = np.array(value, float)
    require(result.shape == (3,) and np.isfinite(result).all(), 'Ungültiger Feldvektor.')
    return result


def _point(lo, hi):
    shape = lo.shape[0]
    return [GV(IV(lo[:, i], hi[:, i]), [IV.exact(1.0 if j == i else 0.0, shape) for j in range(3)],
               np.ones(shape, bool), 1.0, 1.0) for i in range(3)]


def _linear_map(matrix, point):
    rows = []
    for row in matrix:
        acc = None
        for coefficient, component in zip(row, point):
            term = g_scale(component, float(coefficient))
            acc = term if acc is None else g_add(acc, term)
        rows.append(acc)
    return rows


def _box_distance(q, shape):
    """||max(q,0)|| + min(max q, 0). Where at most one component can be positive this equals
    max(q) exactly, which keeps the distance smooth across a face interior."""
    zero = g_const(0.0, shape)
    positive_count = sum((c.v.hi > 0).astype(int) for c in q)
    general = g_add(g_norm([g_max(c, zero) for c in q]), g_min(g_max(g_max(q[0], q[1]), q[2]), zero))
    single = g_max(g_max(q[0], q[1]), q[2])
    use_single = positive_count <= 1
    pick = lambda x, y: IV(np.where(use_single, x.lo, y.lo), np.where(use_single, x.hi, y.hi))
    return GV(pick(single.v, general.v), [pick(x, y) for x, y in zip(single.g, general.g)],
              np.where(use_single, single.smooth, general.smooth), np.where(use_single, single.nlo, general.nlo),
              np.where(use_single, single.nhi, general.nhi))


def _unit_where_smooth(result, frame):
    """Exact distance functions have a unit local gradient wherever they are differentiable."""
    return GV(result.v, result.g, result.smooth, np.where(result.smooth, frame[0], result.nlo),
              np.where(result.smooth, frame[1], result.nhi))


def _shift(point, center):
    shape = point[0].v.lo.shape
    return [g_sub(point[i], g_const(center[i], shape)) for i in range(3)]


def _wendland_at(point, center, radius, frame):
    return g_wendland(g_scale(g_norm(_shift(point, center), frame), 1.0 / radius))


def _singular_values(matrix):
    values = np.linalg.svd(np.asarray(matrix, float), compute_uv=False)
    return float(_down(values.min())) * (1 - 1e-12), float(_up(values.max())) * (1 + 1e-12)


def evaluate_interval(node, lo, hi, expand=0.0):
    """Interval value and gradient of the registered field expression over boxes [lo, hi] (N,3)."""
    lo = np.asarray(lo, float)
    hi = np.asarray(hi, float)
    require(lo.ndim == 2 and lo.shape == hi.shape and lo.shape[1] == 3, 'Ungültige Zellenliste.')
    if expand:
        lo, hi = lo - expand, hi + expand
    return _visit(node, _point(lo, hi), 0, (1.0, 1.0))


def _take(iv, k, axis):
    return IV(np.take(iv.lo, k, axis=axis), np.take(iv.hi, k, axis=axis))


def _stack(items, axis):
    return IV(np.stack([x.lo for x in items], axis=axis), np.stack([x.hi for x in items], axis=axis))


def _bezier_axis(block, axis):
    """Bernstein control net of one uniform cubic B-spline segment along an axis (knots i-1..i+2)."""
    c = [_take(block, k, axis) for k in range(4)]
    b0 = (c[0] + c[1] * 4.0 + c[2]) * (1.0 / 6.0)
    b1 = (c[1] * 2.0 + c[2]) * (1.0 / 3.0)
    b2 = (c[1] + c[2] * 2.0) * (1.0 / 3.0)
    b3 = (c[1] + c[2] * 4.0 + c[3]) * (1.0 / 6.0)
    return _stack([b0, b1, b2, b3], axis)


def _lerp(x, y, s):
    return x * (1.0 - s) + y * s


def _subdivide_axis(net, axis, s_lo, s_hi):
    """Control net of the Bernstein form restricted to [s_lo, s_hi] (per cell) along an axis.

    Parameters are rounded outward so the restricted segment covers the requested sub-box.
    """
    # Taken slices drop the processed axis: parameters broadcast over the remaining dimensions.
    shape = [1] * (net.lo.ndim - 1)
    shape[0] = -1
    hi = np.minimum(_up(s_hi), 1.0).reshape(shape)
    ratio = np.where(hi.reshape(-1) > 0, _down(s_lo / np.where(hi.reshape(-1) > 0, hi.reshape(-1), 1.0)), 0.0)
    ratio = np.clip(ratio, 0.0, 1.0).reshape(shape)
    b = [_take(net, k, axis) for k in range(4)]
    # Left part [0, hi] of the segment.
    m = [_lerp(b[k], b[k + 1], hi) for k in range(3)]
    n = [_lerp(m[k], m[k + 1], hi) for k in range(2)]
    p = _lerp(n[0], n[1], hi)
    left = [b[0], m[0], n[0], p]
    # Right part [ratio, 1] of the left segment.
    m = [_lerp(left[k], left[k + 1], ratio) for k in range(3)]
    n = [_lerp(m[k], m[k + 1], ratio) for k in range(2)]
    p = _lerp(n[0], n[1], ratio)
    return _stack([p, n[1], m[2], left[3]], axis)


def _sampled_grid(g, point, frame):
    """Enclosure of an interpolating cubic B-spline grid over boxes.

    Boxes covering at most two knot cells per axis are split at the knot planes; on each piece the
    spline is one tricubic polynomial whose Bernstein control net, restricted to the piece, gives
    value hulls and (through the differenced net) partial-derivative hulls of width O(box size).
    Wider boxes use the convex hull of the covering coefficient window. Inside the active box the
    field is C^2; a box leaving it meets the clamped continuation, receives a zero-including
    partial derivative and is marked non-smooth. All arithmetic is interval arithmetic.
    """
    coeff, dims, spacing = g['coefficients'], g['dims'], g['spacing_mm']
    origin, voxel, scale, base = g['origin'], g['voxel'], g['scale'], g['base']
    n_cells = point[0].v.lo.shape[0]
    u_lo, u_hi, f_lo, f_hi, inside = [], [], [], [], np.ones(n_cells, bool)
    for a in range(3):
        lo = _down(_down(_down(point[a].v.lo / scale) - origin[a]) / voxel[a]) - base[a]
        hi = _up(_up(_up(point[a].v.hi / scale) - origin[a]) / voxel[a]) - base[a]
        inside &= (lo >= 0) & (hi <= dims[a] - 1)
        lo, hi = np.clip(lo, 0, dims[a] - 1), np.clip(hi, 0, dims[a] - 1)
        first = np.minimum(np.floor(lo), dims[a] - 2).astype(int)
        last = np.clip(np.ceil(hi) - 1, first, dims[a] - 2).astype(int)
        u_lo.append(lo)
        u_hi.append(hi)
        f_lo.append(first)
        f_hi.append(last)
    v_lo, v_hi = np.full(n_cells, INF), np.full(n_cells, -INF)
    d_lo = [np.full(n_cells, INF) for _ in range(3)]
    d_hi = [np.full(n_cells, -INF) for _ in range(3)]

    def merge(rows, value, partials):
        v_lo[rows] = np.minimum(v_lo[rows], value.lo)
        v_hi[rows] = np.maximum(v_hi[rows], value.hi)
        for a in range(3):
            d_lo[a][rows] = np.minimum(d_lo[a][rows], partials[a].lo)
            d_hi[a][rows] = np.maximum(d_hi[a][rows], partials[a].hi)
    spans = [f_hi[a] - f_lo[a] + 1 for a in range(3)]
    fine = (spans[0] <= 2) & (spans[1] <= 2) & (spans[2] <= 2)
    for offsets in np.ndindex(2, 2, 2):
        rows = np.nonzero(fine & (offsets[0] < spans[0]) & (offsets[1] < spans[1]) & (offsets[2] < spans[2]))[0]
        if not rows.size:
            continue
        cells = [f_lo[a][rows] + offsets[a] for a in range(3)]
        idx = [cells[a][:, None] + np.arange(0, 4)[None, :] for a in range(3)]
        block = coeff[idx[0][:, :, None, None], idx[1][:, None, :, None], idx[2][:, None, None, :]]
        net = IV(block, block.copy())
        widths = []
        for a in range(3):
            s_lo = np.clip(u_lo[a][rows] - cells[a], 0.0, 1.0)
            s_hi = np.clip(u_hi[a][rows] - cells[a], 0.0, 1.0)
            s_hi = np.maximum(s_hi, np.minimum(s_lo + 1e-9, 1.0))
            s_lo = np.minimum(s_lo, s_hi - 1e-9 * (s_hi >= 1e-9))
            net = _subdivide_axis(_bezier_axis(net, 1 + a), 1 + a, s_lo, s_hi)
            widths.append(_down(np.maximum(s_hi - s_lo, 1e-12)))
        flat_lo, flat_hi = net.lo.reshape(rows.size, -1), net.hi.reshape(rows.size, -1)
        value = IV(flat_lo.min(axis=1), flat_hi.max(axis=1))
        partials = []
        for a in range(3):
            diff = _take(net, [1, 2, 3], 1 + a) - _take(net, [0, 1, 2], 1 + a)
            factor = _up(3.0 / _down(widths[a] * spacing[a]))
            shape = [1] * net.lo.ndim
            shape[0] = -1
            scaled = diff * factor.reshape(shape)
            partials.append(IV(scaled.lo.reshape(rows.size, -1).min(axis=1), scaled.hi.reshape(rows.size, -1).max(axis=1)))
        merge(rows, value, partials)
    for c in np.nonzero(~fine)[0]:
        block = coeff[tuple(slice(int(f_lo[a][c]), int(f_hi[a][c]) + 4) for a in range(3))]
        value = IV(np.array([block.min()]), np.array([block.max()]))
        partials = []
        for a in range(3):
            d = np.diff(block, axis=a) / spacing[a]
            partials.append(IV(np.array([_down(d.min())]), np.array([_up(d.max())])))
        merge(np.array([c]), value, partials)
    value = IV(_down(v_lo), _up(v_hi))
    partials = []
    for a in range(3):
        lo_a, hi_a = _down(d_lo[a]), _up(d_hi[a])
        partials.append(IV(np.where(inside, lo_a, np.minimum(lo_a, 0.0)), np.where(inside, hi_a, np.maximum(hi_a, 0.0))))
    grad = []
    for j in range(3):
        acc = None
        for a in range(3):
            term = partials[a] * point[a].g[j]
            acc = term if acc is None else acc + term
        grad.append(acc)
    local_hi = _up(np.sqrt(sum(p.magnitude_max() ** 2 for p in partials)))
    local_lo = np.maximum(_down(np.sqrt(sum(p.magnitude_min() ** 2 for p in partials))), 0.0)
    smooth = inside.copy()
    for a in range(3):
        smooth &= point[a].smooth
    return GV(value, grad, smooth, local_lo * frame[0], local_hi * frame[1])


def _visit(n, point, depth, frame):
    require(depth <= 32, 'Intervall-AST zu tief.', 'BUDGET_EXCEEDED')
    op = n['op']
    shape = point[0].v.lo.shape
    if op == 'sampled_grid':
        from fields import sampled_grid
        return _sampled_grid(sampled_grid(n), point, frame)
    if op == 'gyroid':
        origin = _vector(n['origin'])
        factor = 2 * math.pi / _number(n['period'])
        x, y, z = [g_scale(c, factor) for c in _shift(point, origin)]
        value = g_add(g_add(g_mul(g_sin(x), g_cos(y)), g_mul(g_sin(y), g_cos(z))), g_mul(g_sin(z), g_cos(x)))
        return g_sub(value, g_const(_number(n['threshold']), shape))
    if op == 'sphere':
        return g_sub(g_norm(_shift(point, _vector(n['center'])), frame), g_const(_number(n['radius']), shape))
    if op == 'box':
        half = _vector(n['half_size'])
        q = [g_sub(g_abs(c), g_const(half[i], shape)) for i, c in enumerate(_shift(point, _vector(n['center'])))]
        return _unit_where_smooth(_box_distance(q, shape), frame)
    if op == 'plane':
        normal = _vector(n['normal'])
        normal = normal / np.linalg.norm(normal)
        value = None
        for i in range(3):
            term = g_scale(point[i], float(normal[i]))
            value = term if value is None else g_add(value, term)
        result = g_sub(value, g_const(_number(n['offset']), shape))
        return GV(result.v, result.g, result.smooth, frame[0] * (1 - 1e-15), frame[1] * (1 + 1e-15))
    if op == 'cylinder':
        local = _shift(point, _vector(n['center']))
        radial = g_sub(g_norm([local[0], local[1], g_const(0.0, shape)], frame), g_const(_number(n['radius']), shape))
        axial = g_sub(g_abs(local[2]), g_const(_number(n['half_height']), shape))
        return _unit_where_smooth(_box_distance([radial, axial, g_const(-INF, shape)], shape), frame)
    if op == 'capsule':
        a = _vector(n['start'])
        axis = _vector(n['end']) - a
        length2 = float(np.dot(axis, axis))
        radius = _number(n['radius'])
        rel = _shift(point, a)
        if length2 == 0:
            return g_sub(g_norm(rel, frame), g_const(radius, shape))
        t = None
        for i in range(3):
            term = g_scale(rel[i], float(axis[i] / length2))
            t = term if t is None else g_add(t, term)
        t = g_min(g_max(t, g_const(0.0, shape)), g_const(1.0, shape))
        closest = [g_sub(rel[i], g_scale(t, float(axis[i]))) for i in range(3)]
        result = g_sub(g_norm(closest), g_const(radius, shape))
        return _unit_where_smooth(result, frame)
    if op == 'torus':
        local = _shift(point, _vector(n['center']))
        ring = g_sub(g_norm([local[0], local[1], g_const(0.0, shape)], frame), g_const(_number(n['major']), shape))
        result = g_sub(g_norm([ring, local[2], g_const(0.0, shape)]), g_const(_number(n['minor']), shape))
        return _unit_where_smooth(result, frame)
    if op in ('union', 'intersection', 'difference', 'smooth_union'):
        a = _visit(n['a'], point, depth + 1, frame)
        b = _visit(n['b'], point, depth + 1, frame)
        if op == 'union':
            return g_min(a, b)
        if op == 'intersection':
            return g_max(a, b)
        if op == 'difference':
            return g_max(a, g_neg(b))
        k = _number(n['k'])
        diff = g_sub(a, b)
        plain = g_min(a, b)
        h = g_add(g_scale(g_sub(b, a), 1.0 / (2 * k)), g_const(0.5, shape))
        one_minus = g_sub(g_const(1.0, shape), h)
        blended = g_sub(g_add(g_mul(one_minus, b), g_mul(h, a)), g_scale(g_mul(h, one_minus), k))
        inside = (diff.v.hi < k) & (diff.v.lo > -k)
        separated = (diff.v.lo >= k) | (diff.v.hi <= -k)
        pick = lambda x, y: IV(np.where(inside, x.lo, np.where(separated, y.lo, np.minimum(x.lo, y.lo))),
                               np.where(inside, x.hi, np.where(separated, y.hi, np.maximum(x.hi, y.hi))))
        value = pick(blended.v, plain.v)
        grad = [pick(x, y) for x, y in zip(blended.g, plain.g)]
        smooth = np.where(inside, blended.smooth, np.where(separated, plain.smooth, False))
        nlo = np.where(inside, blended.nlo, np.where(separated, plain.nlo, 0.0))
        nhi = np.where(inside, blended.nhi, np.where(separated, plain.nhi, np.maximum(blended.nhi, plain.nhi)))
        return GV(value, grad, smooth, nlo, nhi)
    require(op in ('convert_field_unit', 'transform', 'affine_transform', 'rotate', 'local_deform', 'offset', 'shell', 'local_field_delta'),
            'Unregistrierter Feldoperator.', 'OUT_OF_SCOPE')
    if op == 'convert_field_unit':
        q = n['reference_length']
        reference = _number(q['value']) * {'mm': 1, 'm': 1000, 'um': .001}[q['unit']]
        return g_scale(_visit(n['source'], point, depth + 1, frame), reference if n['to'] == 'length' else 1 / reference)
    if op in ('transform', 'affine_transform', 'rotate'):
        local, inner, factor = _mapped_point(n, point, frame)
        return g_scale(_visit(n['source'], local, depth + 1, inner), factor)
    if op == 'local_deform':
        radius = float(np.linalg.norm(_vector(n['displacement'])))
        widened = [GV(IV(c.v.lo - radius, c.v.hi + radius), c.g, c.smooth) for c in point]
        source = _visit(n['source'], widened, depth + 1, frame)
        return GV(source.v, [IV.whole(shape) for _ in range(3)], np.zeros(shape, bool), 0.0, INF)
    source = _visit(n['source'], point, depth + 1, frame)
    if op == 'offset':
        return g_sub(source, g_const(_number(n['distance']), shape))
    if op == 'shell':
        thickness = g_const(_number(n['thickness']), shape)
        for term in n.get('variations', []):
            thickness = g_add(thickness, g_scale(_wendland_at(point, _vector(term['center']), _number(term['radius']), frame), _number(term['amplitude'])))
        return g_sub(g_abs(source), g_scale(thickness, 0.5))
    return g_add(source, g_scale(_wendland_at(point, _vector(n['center']), _number(n['radius']), frame), _number(n['amplitude'])))


def _mapped_point(n, point, frame):
    """Local query coordinates and frame bounds below a transform, affine or rotate node."""
    op = n['op']
    shape = point[0].v.lo.shape
    if op == 'transform':
        scale = _vector(n['scale'])
        require(bool(np.all(scale > 0)), 'Positive Feldskalierung erforderlich.')
        local = [g_scale(c, 1.0 / scale[i]) for i, c in enumerate(_shift(point, _vector(n['translation'])))]
        return local, (frame[0] * float(min(1 / scale)), frame[1] * float(max(1 / scale))), float(min(scale))
    if op == 'affine_transform':
        from fields import affine_constants
        inverse, scale = affine_constants(tuple(tuple(row) for row in n['matrix']))
        smin, smax = _singular_values(inverse)
        return _linear_map(inverse, _shift(point, _vector(n['translation']))), (frame[0] * smin, frame[1] * smax), scale
    from fields import rotation_matrix
    origin = _vector(n['origin'])
    angle = n['angle']
    inverse = rotation_matrix(tuple(n['axis']), angle['value'], angle['unit']).T
    local = [g_add(c, g_const(origin[i], shape)) for i, c in enumerate(_linear_map(inverse, _shift(point, origin)))]
    return local, (frame[0] * (1 - 1e-12), frame[1] * (1 + 1e-12)), 1.0


def _canon(n):
    return json.dumps(n, sort_keys=True, separators=(',', ':'))


def difference_magnitude(f, g, point, frame=(1.0, 1.0), depth=0):
    """Upper bound of |g - f| on the cells, propagated through compatible structure.

    Identical subtrees contribute exactly zero. Compact edits contribute their
    amplitude times phi. min/max/smooth minimum are 1-Lipschitz in each argument,
    so their difference is bounded by the larger argument difference. Otherwise
    the generic interval difference is used, which is rigorous but not tight.
    """
    require(depth <= 64, 'Differenzbaum zu tief.', 'BUDGET_EXCEEDED')
    shape = point[0].v.lo.shape
    if _canon(f) == _canon(g):
        return np.zeros(shape)
    for outer, inner, sign in ((g, f, 1), (f, g, -1)):
        if outer['op'] == 'local_field_delta' and _canon(outer) != _canon(inner):
            phi = _wendland_at(point, _vector(outer['center']), _number(outer['radius']), frame).v
            return _up(difference_magnitude(inner, outer['source'], point, frame, depth + 1) + _up(abs(_number(outer['amplitude'])) * phi.hi))
        if outer['op'] == 'local_deform' and _canon(outer) != _canon(inner):
            displacement = float(np.linalg.norm(_vector(outer['displacement'])))
            widened = [GV(IV(c.v.lo - displacement, c.v.hi + displacement), c.g, c.smooth, c.nlo, c.nhi) for c in point]
            _, lipschitz = _visit(outer['source'], widened, depth + 1, frame).norm_bounds()
            phi = _wendland_at(point, _vector(outer['center']), _number(outer['radius']), frame).v
            moved = _up(np.where(np.isfinite(lipschitz), lipschitz, INF) * _up(displacement * phi.hi))
            return _up(difference_magnitude(inner, outer['source'], point, frame, depth + 1) + moved)
    if f['op'] == g['op']:
        op = f['op']
        if op == 'sphere' and _canon(f['center']) == _canon(g['center']):
            return np.full(shape, _up(abs(_number(f['radius']) - _number(g['radius']))))
        if op == 'plane' and _canon(f['normal']) == _canon(g['normal']):
            return np.full(shape, _up(abs(_number(f['offset']) - _number(g['offset']))))
        if op == 'gyroid' and _canon(f['origin']) == _canon(g['origin']) and f['period'] == g['period']:
            return np.full(shape, _up(abs(_number(f['threshold']) - _number(g['threshold']))))
        if op in ('union', 'intersection', 'difference'):
            return np.maximum(difference_magnitude(f['a'], g['a'], point, frame, depth + 1), difference_magnitude(f['b'], g['b'], point, frame, depth + 1))
        if op == 'smooth_union' and f['k'] == g['k']:
            return np.maximum(difference_magnitude(f['a'], g['a'], point, frame, depth + 1), difference_magnitude(f['b'], g['b'], point, frame, depth + 1))
        if op == 'offset':
            return _up(difference_magnitude(f['source'], g['source'], point, frame, depth + 1) + _up(abs(_number(f['distance']) - _number(g['distance']))))
        if op == 'convert_field_unit' and _canon(f['reference_length']) == _canon(g['reference_length']) and f['to'] == g['to']:
            q = f['reference_length']
            reference = _number(q['value']) * {'mm': 1, 'm': 1000, 'um': .001}[q['unit']]
            factor = reference if f['to'] == 'length' else 1 / reference
            return _up(difference_magnitude(f['source'], g['source'], point, frame, depth + 1) * factor)
        if op in ('transform', 'affine_transform', 'rotate') and _canon({k: v for k, v in f.items() if k != 'source'}) == _canon({k: v for k, v in g.items() if k != 'source'}):
            local, inner_frame, factor = _mapped_point(f, point, frame)
            return _up(difference_magnitude(f['source'], g['source'], local, inner_frame, depth + 1) * factor)
        if op == 'shell' and f.get('variations', []) == g.get('variations', []):
            return _up(difference_magnitude(f['source'], g['source'], point, frame, depth + 1) + _up(abs(_number(f['thickness']) - _number(g['thickness'])) / 2))
    generic = _visit(g, point, depth + 1, frame).v - _visit(f, point, depth + 1, frame).v
    return generic.magnitude_max()


def value_interval(node, lo, hi):
    return evaluate_interval(node, lo, hi).v


def certified_empty(node, lo, hi):
    """True for cells whose interval excludes zero: no surface can cross them."""
    return ~value_interval(node, lo, hi).contains_zero()


def difference_supports(a, b, depth=0):
    """Compact supports on which b may differ from a, or None when the difference is global."""
    canon = lambda n: json.dumps(n, sort_keys=True, separators=(',', ':'))
    if canon(a) == canon(b):
        return []
    supports = []

    def peel(node, stop):
        peeled = []
        for _ in range(64):
            if canon(node) == canon(stop) or node['op'] not in ('local_field_delta', 'local_deform'):
                return node, peeled
            radius = float(node['radius'])
            if node['op'] == 'local_deform':
                radius += float(np.linalg.norm(_vector(node['displacement'])))
            peeled.append((_vector(node['center']), radius))
            node = node['source']
        return node, peeled
    inner_b, from_b = peel(b, a)
    inner_a, from_a = peel(a, inner_b)
    if canon(inner_a) != canon(inner_b):
        return None
    supports.extend(from_b)
    supports.extend(from_a)
    return supports


def _box_ball_disjoint(lo, hi, center, radius):
    nearest = np.minimum(np.maximum(center, lo), hi)
    distance = np.sqrt(((nearest - center) ** 2).sum(axis=1))
    return _down(distance) >= radius + 1e-12


def certify_deviation(node_f, node_g, domain, cell_size, epsilon, budget=MAX_CELLS):
    """Prove d_H(Z_f, Z_g) <= epsilon inside the declared domain, or report exactly why not.

    For each band cell C (interval of f or g contains zero) with C' = C expanded by
    epsilon inside the domain: f and g are C^1 on C' with |grad| >= m > 0 and
    |f-g| <= delta on C. Following the gradient flow from a zero of g reaches a
    zero of f within distance delta/m <= epsilon (and symmetrically). Cells outside
    every differing compact support have f == g exactly and need no derivative.
    """
    epsilon = float(epsilon)
    require(math.isfinite(epsilon) and epsilon > 0, 'Positive Abweichungsschranke erforderlich.')
    lo0 = np.array(domain['min'], float)
    hi0 = np.array(domain['max'], float)
    require(lo0.shape == (3,) and bool(np.all(hi0 > lo0)), 'Ungültige Zertifikatsdomäne.')
    supports = difference_supports(node_f, node_g)
    lo, hi = lo0[None, :].copy(), hi0[None, :].copy()
    evaluated = 0
    level = 0
    target = float(cell_size)
    report = {'method': 'interval_arithmetic_gradient_flow_certificate', 'epsilon_mm': epsilon,
              'evaluation_error_model': ERROR_MODEL, 'coverage': 'entire_declared_domain', 'levels': 0,
              'cells_evaluated': 0, 'band_cells': 0, 'identical_cells': 0, 'certified_cells': 0, 'failed_cells': 0,
              'failure_reasons': {}, 'certified_hausdorff_bound_mm': None, 'gradient_lower_bound': None,
              'max_field_difference': None,
              'difference_supports': None if supports is None else [{'center': c.tolist(), 'radius': r} for c, r in supports]}
    while True:
        evaluated += lo.shape[0]
        require(evaluated <= budget, 'Zertifikatszellen überschreiten das Budget.', 'BUDGET_EXCEEDED')
        vf = value_interval(node_f, lo, hi)
        vg = value_interval(node_g, lo, hi)
        band = vf.contains_zero() | vg.contains_zero()
        size = float((hi - lo).max())
        if size <= target * (1 + 1e-9) or level >= MAX_LEVELS or not band.any():
            break
        lo, hi = lo[band], hi[band]
        mid = (lo + hi) / 2
        parts_lo, parts_hi = [], []
        for a in (0, 1):
            for b in (0, 1):
                for c in (0, 1):
                    corner = np.array([a, b, c], float)
                    parts_lo.append(np.where(corner == 0, lo, mid))
                    parts_hi.append(np.where(corner == 0, mid, hi))
        lo, hi = np.concatenate(parts_lo), np.concatenate(parts_hi)
        level += 1
    report['levels'] = level
    report['cells_evaluated'] = int(evaluated)
    report['final_cell_size_mm'] = float((hi - lo).max()) if lo.shape[0] else 0.0
    lo, hi = lo[band], hi[band]
    report['band_cells'] = int(lo.shape[0])
    if lo.shape[0] == 0:
        report.update(status='certified', certified_hausdorff_bound_mm=0.0, note='no_surface_in_domain')
        return report
    identical = np.zeros(lo.shape[0], bool)
    if supports is not None:
        identical = np.ones(lo.shape[0], bool)
        for center, radius in supports:
            identical &= _box_ball_disjoint(lo, hi, center, radius)
    report['identical_cells'] = int(identical.sum())
    work_lo, work_hi = lo[~identical], hi[~identical]
    reasons = {}
    bound = 0.0
    if work_lo.shape[0]:
        boundary = ~(np.all(work_lo - epsilon >= lo0 - 1e-12, axis=1) & np.all(work_hi + epsilon <= hi0 + 1e-12, axis=1))
        gf = evaluate_interval(node_f, work_lo, work_hi, expand=epsilon)
        gg = evaluate_interval(node_g, work_lo, work_hi, expand=epsilon)
        delta = difference_magnitude(node_f, node_g, _point(work_lo, work_hi))
        mf, _ = gf.norm_bounds()
        mg, _ = gg.norm_bounds()
        finite = gf.v.finite() & gg.v.finite() & np.isfinite(delta)
        smooth = gf.smooth & gg.smooth & finite
        # Mean-value form: |f-g| <= |f(c)-g(c)| + sup|grad(f-g)| * radius, valid where both are C^1
        # on the expanded cell; it is O(cell) tight where the generic interval difference is not.
        centre = (work_lo + work_hi) / 2
        at_centre = (value_interval(node_f, centre, centre) - value_interval(node_g, centre, centre)).magnitude_max()
        spread = np.zeros(work_lo.shape[0])
        for j in range(3):
            spread = _up(spread + _up((gf.g[j] - gg.g[j]).magnitude_max() ** 2))
        radius = _up(np.sqrt((((work_hi - work_lo) / 2) ** 2).sum(axis=1)))
        mean_value = _up(at_centre + _up(_up(np.sqrt(spread)) * radius))
        delta = np.where(smooth & np.isfinite(mean_value), np.minimum(delta, mean_value), delta)
        report['difference_bound'] = 'min(structural_or_interval_difference, mean_value_form_on_smooth_cells)'
        positive = (mf > 0) & (mg > 0)
        safe_f = np.where(positive, mf, 1.0)
        safe_g = np.where(positive, mg, 1.0)
        ratio = np.where(positive, _up(np.maximum(_up(delta / safe_f), _up(delta / safe_g))), INF)
        ok = ~boundary & smooth & positive & (ratio <= epsilon)
        for name, mask in [('surface_reaches_domain_margin', boundary),
                           ('nonsmooth_or_singular_gradient', ~smooth & ~boundary),
                           ('gradient_lower_bound_zero', smooth & ~positive & ~boundary),
                           ('deviation_exceeds_epsilon', smooth & positive & ~boundary & (ratio > epsilon))]:
            if int(mask.sum()):
                reasons[name] = int(mask.sum())
        report['certified_cells'] = int(ok.sum())
        report['failed_cells'] = int((~ok).sum())
        if bool(ok.any()):
            bound = float(ratio[ok].max())
            report['gradient_lower_bound'] = float(min(mf[ok].min(), mg[ok].min()))
            report['max_field_difference'] = float(delta[ok].max())
    report['failure_reasons'] = reasons
    certified = report['failed_cells'] == 0
    report['status'] = 'certified' if certified else 'not_certified'
    report['certified_hausdorff_bound_mm'] = min(bound, epsilon) if certified else None
    return report


def value_range(node, domain, cell_size, budget=MAX_CELLS):
    """Certified global bounds of the field over the domain by adaptive interval refinement."""
    lo0 = np.array(domain['min'], float)
    hi0 = np.array(domain['max'], float)
    lo, hi = lo0[None, :].copy(), hi0[None, :].copy()
    evaluated = 0
    low, high = INF, -INF
    for level in range(MAX_LEVELS + 1):
        evaluated += lo.shape[0]
        require(evaluated <= budget, 'Intervallzellen überschreiten das Budget.', 'BUDGET_EXCEEDED')
        v = value_interval(node, lo, hi)
        size = float((hi - lo).max())
        if size <= float(cell_size) * (1 + 1e-9) or level == MAX_LEVELS:
            return {'lower': float(v.lo.min()), 'upper': float(v.hi.max()), 'cells': int(lo.shape[0]), 'evaluation_error_model': ERROR_MODEL}
        # Only cells that may still hold the running extremes need refinement.
        low, high = min(low, float(v.hi.min())), max(high, float(v.lo.max()))
        keep = (v.lo <= low) | (v.hi >= high)
        lo, hi = lo[keep], hi[keep]
        mid = (lo + hi) / 2
        parts_lo, parts_hi = [], []
        for a in (0, 1):
            for b in (0, 1):
                for c in (0, 1):
                    corner = np.array([a, b, c], float)
                    parts_lo.append(np.where(corner == 0, lo, mid))
                    parts_hi.append(np.where(corner == 0, mid, hi))
        lo, hi = np.concatenate(parts_lo), np.concatenate(parts_hi)
    return {'lower': float(low), 'upper': float(high), 'cells': int(lo.shape[0]), 'evaluation_error_model': ERROR_MODEL}


def blend_activity(node, domain, cell_size, budget=MAX_CELLS):
    """Certify that every smooth_union in the expression is inactive on the domain (Bauplan 6.8).

    A polynomial smooth minimum equals the exact minimum wherever |a - b| >= k. For each
    smooth_union node the interval of a - b is evaluated on refined cells of the region, in the
    node's own local coordinates below any transforms. A region where every blend stays inactive
    keeps CAD mating faces dimensionally exact; otherwise the report names the active blends.
    """
    lo0 = np.array(domain['min'], float)
    hi0 = np.array(domain['max'], float)
    require(lo0.shape == (3,) and bool(np.all(hi0 > lo0)), 'Ungültige Passregion.')
    blends = []

    def collect(n, path):
        if n['op'] == 'smooth_union':
            blends.append((path, n))
        for key in ('a', 'b', 'source'):
            if key in n:
                collect(n[key], path + [key])
    collect(node, [])
    report = {'method': 'interval_arithmetic_blend_inactivity', 'evaluation_error_model': ERROR_MODEL, 'blends': [],
              'coverage': 'entire_declared_region', 'status': 'certified'}
    if not blends:
        report['note'] = 'no_smooth_union_in_expression'
        return report

    def local_point(path, point, frame):
        # Walk down to the blend node, mapping the query box through every transform on the way.
        n = node
        for key in path:
            if n['op'] in ('transform', 'affine_transform', 'rotate'):
                point, frame, _ = _mapped_point(n, point, frame)
            elif n['op'] == 'local_deform':
                radius = float(np.linalg.norm(_vector(n['displacement'])))
                point = [GV(IV(c.v.lo - radius, c.v.hi + radius), c.g, c.smooth) for c in point]
            n = n[key]
        return point
    for path, blend in blends:
        k = _number(blend['k'])
        lo, hi = lo0[None, :].copy(), hi0[None, :].copy()
        evaluated = 0
        active = None
        for level in range(MAX_LEVELS + 1):
            evaluated += lo.shape[0]
            require(evaluated <= budget, 'Passregionszellen überschreiten das Budget.', 'BUDGET_EXCEEDED')
            point = local_point(path, _point(lo, hi), (1.0, 1.0))
            diff = g_sub(_visit(blend['a'], point, 0, (1.0, 1.0)), _visit(blend['b'], point, 0, (1.0, 1.0))).v
            inactive = (diff.lo >= k) | (diff.hi <= -k)
            if bool(inactive.all()):
                active = 0
                break
            size = float((hi - lo).max())
            if size <= float(cell_size) * (1 + 1e-9) or level == MAX_LEVELS:
                active = int((~inactive).sum())
                break
            lo, hi = lo[~inactive], hi[~inactive]
            mid = (lo + hi) / 2
            parts_lo, parts_hi = [], []
            for a in (0, 1):
                for b in (0, 1):
                    for c in (0, 1):
                        corner = np.array([a, b, c], float)
                        parts_lo.append(np.where(corner == 0, lo, mid))
                        parts_hi.append(np.where(corner == 0, mid, hi))
            lo, hi = np.concatenate(parts_lo), np.concatenate(parts_hi)
        entry = {'path': '/'.join(path) or 'root', 'k': k, 'cells_evaluated': int(evaluated), 'undecided_or_active_cells': active,
                 'status': 'inactive_certified' if active == 0 else 'possibly_active'}
        report['blends'].append(entry)
        if active != 0:
            report['status'] = 'not_certified'
    return report
