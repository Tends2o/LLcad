"""N4.10: mandatory dimensions of a profile shape. Pure function without OCP dependency: planner and check (checks.profile_findings)
read the same list.

A profile (features.profiles) carries `outline`: elements in the profile coordinate system (mm, plane perpendicular to the generatrix)
  {'t': 'line', 'p0': [x, y], 'p1': [x, y]}
  {'t': 'arc', 'c': [x, y], 'r': r, 'a0': degrees, 'span': degrees, 'convex': bool}
  {'t': 'spline', 'pts': [[x, y], ...]}      (polyline)
profile_dims(profile, skip_radii=()) -> [{'key', 'kind': 'diameter'|'radius'|'angle'|'linear', 'value', 'n'}]
Every quantity stands once (n = number of places where it occurs)."""
import math

VAL_TOL = 0.01        # mm or degrees*0.1: equal quantities
SYM_TOL = 0.05        # mm: distance of a rotated point to the contour in the rotational symmetry test
ONE_EIGHTY = 180.0 + 1e-6
MAX_ORDER = 12


def _pt(p):
    return (float(p[0]), float(p[1]))


def sample(e, step=0.05):
    """Points of the contour of an element (distance at most step)."""
    if e['t'] == 'line':
        a, b = _pt(e['p0']), _pt(e['p1'])
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / step))
        return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]
    if e['t'] == 'arc':
        c, r = _pt(e['c']), float(e['r'])
        n = max(2, int(math.radians(e['span']) * r / step))
        return [(c[0] + r * math.cos(math.radians(e['a0'] + e['span'] * i / n)), c[1] + r * math.sin(math.radians(e['a0'] + e['span'] * i / n))) for i in range(n + 1)]
    P = [_pt(p) for p in e['pts']]
    out = [P[0]]
    for a, b in zip(P, P[1:]):
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / step))
        out += [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(1, n + 1)]
    return out


def _seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if L < 1e-18 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def dist_to(p, e):
    """Distance of a point to the element."""
    if e['t'] == 'line':
        return _seg_dist(p, _pt(e['p0']), _pt(e['p1']))
    if e['t'] == 'arc':
        c = _pt(e['c'])
        ang = math.degrees(math.atan2(p[1] - c[1], p[0] - c[0])) - e['a0']
        if (ang % 360.0) <= e['span']:
            return abs(math.hypot(p[0] - c[0], p[1] - c[1]) - e['r'])
        ends = [sample(e)[0], sample(e)[-1]]
        return min(math.hypot(p[0] - q[0], p[1] - q[1]) for q in ends)
    pts = [_pt(q) for q in e['pts']]
    return min(_seg_dist(p, pts[i], pts[i + 1]) for i in range(len(pts) - 1)) if len(pts) > 1 else math.hypot(p[0] - pts[0][0], p[1] - pts[0][1])


def _rot(p, c, deg):
    s, k = math.sin(math.radians(deg)), math.cos(math.radians(deg))
    x, y = p[0] - c[0], p[1] - c[1]
    return (c[0] + k * x - s * y, c[1] + s * x + k * y)


def rotation_order(outline, center):
    """Largest order k (2..MAX_ORDER) at which the rotation by 360/k maps the contour onto itself; otherwise 1."""
    pts = [p for e in outline for p in sample(e, 0.2)]
    for k in range(MAX_ORDER, 1, -1):
        if all(min(dist_to(_rot(p, center, 360.0 / k), e) for e in outline) <= SYM_TOL for p in pts):
            return k
    return 1


def _put(out, key, kind, value, n=1, src='arc', geom=None, alt=()):
    """Entry, every quantity (kind and value) only once: an equal quantity raises n (geometry and alternatives of the first occurrence stay).
    src: 'arc' (Ø, R: own face of the arc), 'line' (segments, angles, grooves: residual faces slant/cone/plane), 'spline' (free-form contour).
    geom: foot points in the profile system for the planner ({'p1','p2'} or {'vertex','p1','p2'} or {'c','r'[,'a']}); alt: equivalent
    dimensions of the same mandatory quantity [{'key','kind','value','geom'}], the checker accepts any, the planner sets the first one."""
    v = round(float(value) + 0.0, 2)
    for o in out:
        if o['kind'] == kind and abs(o['value'] - v) <= VAL_TOL:
            o['n'] += n
            return
    out.append({'key': key, 'kind': kind, 'value': v, 'n': int(n), 'src': src, 'geom': geom,
                'alt': [{**a, 'value': round(float(a['value']) + 0.0, 2)} for a in alt]})


def _groups(arcs):
    """Arcs of equal radius and equal centre: [[arc]]."""
    out = []
    for a in arcs:
        for g in out:
            b = g[0]
            if abs(a['r'] - b['r']) <= 0.005 and math.hypot(a['c'][0] - b['c'][0], a['c'][1] - b['c'][1]) <= 0.02:
                g.append(a)
                break
        else:
            out.append([a])
    return out


CURVE_STEP = 0.02     # mm: step width of the evaluation of the spline curve
CURVE_NOISE = 0.05    # share of the largest slope or curvature below which a sign run is noise


def _crossings(t, f, floor):
    """Zeros of f (sign change, linear between the support points t); only between two runs whose largest magnitude reaches floor (noise produces no inflection point)."""
    f = [float(v) for v in f]
    cuts = [i for i in range(len(f) - 1) if f[i] * f[i + 1] < 0 or (f[i] == 0.0 and i > 0 and f[i - 1] * f[i + 1] < 0)]
    bounds = [0] + [i + 1 for i in cuts] + [len(f)]
    big = [max(abs(v) for v in f[a:b]) >= floor for a, b in zip(bounds, bounds[1:])]
    out = []
    for k, i in enumerate(cuts):
        left = next((j for j in range(k, -1, -1) if big[j]), None)
        right = next((j for j in range(k + 1, len(big)) if big[j]), None)
        if left is None or right is None or (f[bounds[left]] > 0) == (f[bounds[right]] > 0):
            continue
        d = f[i] - f[i + 1]
        out.append(t[i] if d == 0 else t[i] + (t[i + 1] - t[i]) * f[i] / d)
    return out


def curve_points(pts):
    """Extreme (x or y) and inflection points of a spline curve from the curve itself: cubic spline through the support points (by arc length), finely evaluated; points (x, y)."""
    import numpy as np
    from scipy.interpolate import CubicSpline
    P = np.asarray([_pt(p) for p in pts], float)
    d = np.hypot(np.diff(P[:, 0]), np.diff(P[:, 1]))
    P = P[np.r_[True, d > 1e-9]]
    if len(P) < 4:
        return []
    s = np.r_[0.0, np.cumsum(np.hypot(np.diff(P[:, 0]), np.diff(P[:, 1])))]
    cs = CubicSpline(s, P, bc_type='natural')
    t = np.linspace(0.0, s[-1], max(200, int(s[-1] / CURVE_STEP)))
    d1, d2 = cs(t, 1), cs(t, 2)
    ts = []
    for k in (0, 1):
        ts += _crossings(t, d1[:, k], CURVE_NOISE * float(np.abs(d1[:, k]).max()))
    kappa = d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]
    ts += _crossings(t, kappa, CURVE_NOISE * float(np.abs(kappa).max()))
    return [tuple(float(v) for v in cs(x)) for x in sorted(ts)]


def _thickness(a, b):
    """Constant thickness of two splines: offset in y (or x) at equal x (y), otherwise smallest distance; (thickness, point on a, opposite point on b) or None if not constant."""
    A, B = [_pt(p) for p in a['pts']], [_pt(p) for p in b['pts']]
    for k in (1, 0):
        o = 1 - k
        Bs = sorted(B, key=lambda p: p[o])
        xs = [p[o] for p in Bs]
        ys = [p[k] for p in Bs]
        off = []
        for p in A:
            if xs[0] - 1e-6 <= p[o] <= xs[-1] + 1e-6:
                off.append((abs(_interp(p[o], xs, ys) - p[k]), p))
        if len(off) >= 3 and max(v for v, _ in off) - min(v for v, _ in off) <= 0.02 * max(1.0, max(v for v, _ in off)):
            v, p = off[len(off) // 2]
            q = [0.0, 0.0]
            q[o], q[k] = p[o], _interp(p[o], xs, ys)
            return sum(v for v, _ in off) / len(off), p, (q[0], q[1])
    d = [(min(_seg_dist(p, B[i], B[i + 1]) for i in range(len(B) - 1)), p) for p in A]
    if not d or max(v for v, _ in d) - min(v for v, _ in d) > 0.02 * max(1.0, max(v for v, _ in d)):
        return None
    v, p = d[len(d) // 2]
    best = min(((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2, q) for q in B)[1]
    return sum(v for v, _ in d) / len(d), p, best


def _interp(x, xs, ys):
    import bisect
    i = min(max(bisect.bisect_left(xs, x), 1), len(xs) - 1)
    x0, x1 = xs[i - 1], xs[i]
    return ys[i - 1] if x1 - x0 < 1e-12 else ys[i - 1] + (ys[i] - ys[i - 1]) * (x - x0) / (x1 - x0)


def _at(c, r, deg):
    return [c[0] + r * math.cos(math.radians(deg)), c[1] + r * math.sin(math.radians(deg))]


def _r4(p):
    return [round(float(p[0]), 4), round(float(p[1]), 4)]


def _lobe_dims(out, o, center, g, k):
    """Rotational symmetry of order k about center with the lobe arcs g (equal radius and centre): pitch (angle between two lobe centres), opening of the groove (angle between the arc ends, equivalent:
    the chord between them) and groove bottom (distance of the centre to the deepest contour point of the groove; equivalent: depth against the lobe radius, distance of the centre to the centre of the bottom arc).
    All foot points lie on the contour (arc ends, arc centres, deepest point) or in the centre."""
    R = g[0]['r']
    A = min(g, key=lambda a: a['a0'] % 360.0)
    mA = A['a0'] + A['span'] / 2
    _put(out, 'pitch', 'angle', 360.0 / k, k, 'line', {'vertex': _r4(center), 'p1': _r4(_at(center, R, mA)), 'p2': _r4(_at(center, R, mA + 360.0 / k))})
    each = sum(a['span'] for a in g) / k
    gap = 360.0 / k - each
    e0 = A['a0'] + A['span']
    pts = [q for e in o for q in sample(e, 0.05)]
    sector = [q for q in pts if ((math.degrees(math.atan2(q[1] - center[1], q[0] - center[0])) - e0) % 360.0) <= gap]
    Q = min(sector or pts, key=lambda q: math.hypot(q[0] - center[0], q[1] - center[1]))
    rmin = math.hypot(Q[0] - center[0], Q[1] - center[1])
    if gap > 0.05:
        chord = 2 * R * math.sin(math.radians(gap) / 2)
        g2 = {'vertex': _r4(center), 'p1': _r4(_at(center, R, e0)), 'p2': _r4(_at(center, R, e0 + gap))}
        _put(out, 'opening', 'angle', gap, k, 'line', g2, [{'key': 'opening_chord', 'kind': 'linear', 'value': chord, 'geom': {'p1': g2['p1'], 'p2': g2['p2']}}])
    if R - rmin > VAL_TOL:
        alt = [{'key': 'depth', 'kind': 'linear', 'value': R - rmin, 'geom': {'p1': _r4(center), 'p2': _r4(Q)}}]
        bottom = next((e for e in o if e['t'] == 'arc' and not e.get('convex') and dist_to(Q, e) <= 0.02), None)
        if bottom is not None:
            alt.append({'key': 'bottom_center', 'kind': 'linear', 'value': math.hypot(bottom['c'][0] - center[0], bottom['c'][1] - center[1]), 'geom': {'p1': _r4(center), 'p2': _r4(bottom['c'])}})
        _put(out, 'bottom', 'linear', rmin, k, 'line', {'p1': _r4(center), 'p2': _r4(Q)}, alt)


def profile_dims(profile, skip_radii=(), skip_diameters=()):
    """Mandatory entries of a profile (N4.10, ISO 129-1: every dimension once, n× for equal ones); every entry {'key','kind','value','n','src','geom','alt'} (see _put):
    - arcs (src 'arc'): one entry per class of equal radii; Ø for arcs of equal centre that together span more than 180°, otherwise R with n;
        radii in skip_radii or Ø in skip_diameters (already covered by a general note or a dimension of their own) are omitted.
    - rotational symmetry of order k (src 'line'): pitch angle 360/k with n = k, per groove opening (angle or chord) and groove bottom (distance of the centre, depth or centre of the bottom arc); segments are omitted.
    - otherwise segments (length) and angles between adjoining segments (src 'line'), equal values (mirror halves) once with n.
    - spline (src 'spline'): envelope (width, height), extreme and inflection points as coordinate dimensions to the envelope edge (equivalent: to the opposite edge), constant thickness once."""
    o = list(profile.get('outline') or [])
    out = []
    if not o:
        return out
    arcs = [e for e in o if e['t'] == 'arc']
    lines = [e for e in o if e['t'] == 'line']
    splines = [e for e in o if e['t'] == 'spline']
    groups = _groups(arcs)
    big = [g for g in groups if sum(a['span'] for a in g) > ONE_EIGHTY]
    pts = [p for e in o for p in sample(e, 0.5)]
    if big:
        c0 = max(big, key=lambda g: sum(a['span'] for a in g))[0]['c']
        center = _pt(c0)
    else:
        center = ((min(p[0] for p in pts) + max(p[0] for p in pts)) / 2, (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2)
    skip = [float(r) for r in skip_radii]
    skip_d = [float(d) for d in skip_diameters]
    for g in big:
        if not any(abs(2 * g[0]['r'] - d) <= 0.01 for d in skip_d):
            _put(out, f"d{g[0]['r'] * 2:.2f}", 'diameter', 2 * g[0]['r'], 1, 'arc', {'c': _r4(g[0]['c']), 'r': g[0]['r']})
    small = [a for g in groups if g not in big for a in g]
    for a in small:
        if not any(abs(a['r'] - r) <= 0.005 for r in skip):
            _put(out, f"r{a['r']:.2f}", 'radius', a['r'], 1, 'arc', {'c': _r4(a['c']), 'r': a['r'], 'a': a['a0'] + a['span'] / 2})
    closed = any(sum(a['span'] for a in g) >= 359.9 for g in big)       # full circle: no pitch
    k = rotation_order(o, center) if not splines and not closed else 1
    if k > 1 and big:
        _lobe_dims(out, o, center, max(big, key=lambda g: sum(a['span'] for a in g)), k)
    elif not splines:      # polygon and contour without rotational symmetry: segments and angles between segments
        for e in lines:
            _put(out, 'len', 'linear', math.hypot(e['p1'][0] - e['p0'][0], e['p1'][1] - e['p0'][1]), 1, 'line', {'p1': _r4(e['p0']), 'p2': _r4(e['p1'])})
        for i, a in enumerate(lines):
            for b in lines[i + 1:]:
                for pa, qa in ((a['p0'], a['p1']), (a['p1'], a['p0'])):
                    for pb, qb in ((b['p0'], b['p1']), (b['p1'], b['p0'])):
                        if math.hypot(pa[0] - pb[0], pa[1] - pb[1]) <= 0.02:
                            u = (qa[0] - pa[0], qa[1] - pa[1]); v = (qb[0] - pb[0], qb[1] - pb[1])
                            c = (u[0] * v[0] + u[1] * v[1]) / (math.hypot(*u) * math.hypot(*v) or 1.0)
                            _put(out, 'angle', 'angle', math.degrees(math.acos(max(-1.0, min(1.0, c)))), 1, 'line', {'vertex': _r4(pa), 'p1': _r4(qa), 'p2': _r4(qb)})
    if splines:
        verts = [_pt(p) for e in splines for p in e['pts']]
        xs, ys = [p[0] for p in verts], [p[1] for p in verts]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        W, H = x1 - x0, y1 - y0
        edge = lambda j, hi: min(verts, key=lambda p: -p[j]) if hi else min(verts, key=lambda p: p[j])
        _put(out, 'hull_x', 'linear', W, 1, 'spline', {'p1': _r4(edge(0, False)), 'p2': _r4(edge(0, True)), 'axis': 0})
        _put(out, 'hull_y', 'linear', H, 1, 'spline', {'p1': _r4(edge(1, False)), 'p2': _r4(edge(1, True)), 'axis': 1})
        known = [W, H]
        for s in splines[:1]:      # the second contour follows via the constant thickness
            for q in curve_points(s['pts']):
                for j, lo, ext in ((0, x0, W), (1, y0, H)):
                    v = q[j] - lo
                    if v > VAL_TOL and all(abs(v - w) > VAL_TOL for w in known):
                        known.append(v)
                        alt = [{'key': 'coord_far', 'kind': 'linear', 'value': ext - v, 'geom': {'p1': _r4(edge(j, True)), 'p2': _r4(q), 'axis': j}}] if ext - v > VAL_TOL else []
                        _put(out, 'coord', 'linear', v, 1, 'spline', {'p1': _r4(edge(j, False)), 'p2': _r4(q), 'axis': j}, alt)
        if len(splines) >= 2:
            t = _thickness(splines[0], splines[1])
            if t is not None and t[0] > VAL_TOL and all(abs(t[0] - w) > VAL_TOL for w in known):
                _put(out, 'thickness', 'linear', t[0], 1, 'spline', {'p1': _r4(t[1]), 'p2': _r4(t[2])})
    return out


def char_len(profile, need=()):
    """Characteristic length of the profile (mm): shortest line or arc element of the contour; for spline contours the smaller envelope edge and the thickness from the mandatory list. It determines the standard scale of the
    detail view (N4.4: smallest profile element >= SMALL_MM on paper)."""
    o = list(profile.get('outline') or [])
    vals = [_elen(e) for e in o if e['t'] != 'spline' and _elen(e) > 1e-6]
    sp = [q for e in o if e['t'] == 'spline' for q in e['pts']]
    if sp:
        xs, ys = [q[0] for q in sp], [q[1] for q in sp]
        vals += [max(xs) - min(xs), max(ys) - min(ys)] + [x['value'] for x in need if x['key'] == 'thickness']
        two = [e for e in o if e['t'] == 'spline']
        if len(two) >= 2 and (t := _thickness(two[0], two[1])) is not None:
            vals.append(t[0])      # the thickness is not listed again in the mandatory list if it coincides with a coordinate dimension
    return min(vals) if vals else 0.0


SRC_OF_KIND = {'slant': 'line', 'cone': 'line', 'plane': 'line', 'freeform_region': 'spline'}


def required_dims(profile, residual_kinds):
    """Mandatory entries only for the residual geometry of the profile. residual_kinds = kinds of the members (slant, cone, plane, freeform_region) that are covered only by the row "Undimensioned
    forms according to 3D model": segments, angles and grooves (src 'line') belong to the slants, cones and planes, the spline dimensions (src 'spline') to the free-form regions. Ø and R belong to the
    arcs, shafts and fillets that run via their own coverage path (e.g. "77× Ø9.2", general note R1): they do not stand again in the profile (no double dimensions)."""
    want = {SRC_OF_KIND[k] for k in residual_kinds if k in SRC_OF_KIND}
    return [x for x in profile_dims(profile) if x['src'] in want]


# ---------------------------------------------------------------- congruence (classes of congruent copies)

CONG_TOL = 0.1       # mm: largest deviation of the contours for congruence (sampling step 0.1 mm: half a step plus rounding of the coordinates)
CONG_STEP = 0.1
LEN_TOL = 0.5        # mm: the sum of the element lengths may deviate by at most this (preselection)


def _elen(e):
    if e['t'] == 'line':
        return math.hypot(e['p1'][0] - e['p0'][0], e['p1'][1] - e['p0'][1])
    if e['t'] == 'arc':
        return math.radians(e['span']) * e['r']
    P = e['pts']
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(P, P[1:]))


def _mirror(e):
    """Mirroring at the y axis (x -> -x)."""
    if e['t'] == 'line':
        return {'t': 'line', 'p0': [-e['p0'][0], e['p0'][1]], 'p1': [-e['p1'][0], e['p1'][1]]}
    if e['t'] == 'arc':
        return {**e, 'c': [-e['c'][0], e['c'][1]], 'a0': (180.0 - e['a0'] - e['span']) % 360.0}
    return {'t': 'spline', 'pts': [[-p[0], p[1]] for p in e['pts']]}


def _anchors(e):
    """Two anchor points of an element (segment and spline: start and end; arc: centre and arc midpoint)."""
    if e['t'] == 'arc':
        m = e['a0'] + e['span'] / 2
        c = _pt(e['c'])
        return c, (c[0] + e['r'] * math.cos(math.radians(m)), c[1] + e['r'] * math.sin(math.radians(m)))
    if e['t'] == 'line':
        return _pt(e['p0']), _pt(e['p1'])
    return _pt(e['pts'][0]), _pt(e['pts'][-1])


def _same_size(a, b):
    if a['t'] != b['t']:
        return False
    if a['t'] == 'arc':
        return abs(a['r'] - b['r']) <= 0.05 and abs(a['span'] - b['span']) <= 0.5 and a['convex'] == b['convex']
    return abs(_elen(a) - _elen(b)) <= LEN_TOL


class Shape:
    """Contour for the congruence comparison (rotation, mirroring, translation): point cloud and search tree once per contour."""

    def __init__(self, outline):
        import numpy as np
        from scipy.spatial import cKDTree
        self.outline = list(outline)
        self.length = sum(_elen(e) for e in self.outline)
        self.cloud = np.array([p for e in self.outline for p in sample(e, CONG_STEP)], float)
        self.tree = cKDTree(self.cloud)
        self.ref = max(self.outline, key=_elen) if self.outline else None

    def matches(self, outline, tol=CONG_TOL):
        """Does a rotation with or without mirroring plus translation map outline onto this contour (both directions up to tol)? Anchor points of the longest element of this contour
        against all elements of equal kind and size of outline; the whole contour is checked so that arrangement and angles between the elements count (a square and a rhombus of equal
        side length differ)."""
        import numpy as np
        from scipy.spatial import cKDTree
        if self.ref is None or not outline or abs(self.length - sum(_elen(e) for e in outline)) > LEN_TOL:
            return False
        a0, a1 = _anchors(self.ref)
        va = math.atan2(a1[1] - a0[1], a1[0] - a0[0])
        for mirror in (False, True):
            els = [_mirror(e) for e in outline] if mirror else list(outline)
            B = np.array([p for e in els for p in sample(e, CONG_STEP)], float)
            for e in els:
                if not _same_size(self.ref, e):
                    continue
                for b0, b1 in (_anchors(e), _anchors(e)[::-1]):
                    th = va - math.atan2(b1[1] - b0[1], b1[0] - b0[0])
                    c, s_ = math.cos(th), math.sin(th)
                    R = np.array([[c, -s_], [s_, c]])
                    t = np.array(a0) - R @ np.array(b0)
                    Bt = B @ R.T + t
                    if self.tree.query(Bt)[0].max() <= tol and cKDTree(Bt).query(self.cloud)[0].max() <= tol:
                        return True
        return False


def congruent(a, b, tol=CONG_TOL):
    """Are two contours (outline) equal up to rotation, mirroring and translation?"""
    return Shape(a).matches(b, tol)
