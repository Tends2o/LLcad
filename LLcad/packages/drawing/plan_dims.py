"""Planner III: dimensioning, notes, tables and item numbers (ISO 129-1).

dimension() returns (annotations, note lines, tables) of a part for the ViewPlans from plan_views. Rules:
- Views: only those of the part whose viewing direction runs along a world axis (normal views, sections, detail views) and
  auxiliary views along oblique axes (there: diameter/radius in the circle, position across the axis from a part edge, position along
  a world axis as usual via the tree), never isometric. Order: larger own scale first (detail view,
  cross-section), otherwise the plan order.
- Linear dimensions per world axis as a tree over the coordinates (union-find, ±TOL): first the overall dimension from datum to the
  opposite face, then distances with a meaning of their own (square, bounding box of a freeform part, pitch of a pattern,
  blind-hole depth from the opening to the bottom; if none of their ends is attached to the datum yet, they first get their
  datum coordinate - first member, opening face - as their baseline dimension), finally, for each coordinate not yet connected to the
  datum, a baseline dimension from datum. So there is no duplicate distance and no closed chain.
  Compound elements as a whole, after the other distances with a meaning of their own: plane series
  "{n-1}× {pitch} (={length})" from the member nearest the datum (a second series of equal pitch and count on the same axis
  is attached to the first one with the distance of its first member, e.g. rib thickness), pocket length x width x depth, groove
  width x depth, step height x depth; position via the wall nearest the datum, none for a pocket/groove on the median plane.
  Coordinates: planes (not those of a square bar, not the members of these elements) and axes of
  hole/shaft/arc or sphere centres across the axis, except in the median plane of the bounding box, except pattern members
  after the first and except arcs in directions of a tangential plane (there the plane fixes the arc, only the
  R dimension).
- Polar/helical sequences (features._sequence): "n× Ø" at the instance, pitch "k× s° (=total°)", pitch circle radius "R…", start angle to the world-axis reference direction, for a
  helical sequence the axial offset first-last member; the angles in a view along the axis of rotation (_sequence_view; without it single dimensions of the members). Gaps of the grid
  appear as "(without room …)" at the Ø.
- A dimension appears in the first view in which its direction lies on sheet x/y (oblique pitch: in the image plane)
  and both end points lie on form features that the view shows (plane: pt, in a detail view the point in the
  circle; axis: start/middle/end; sphere: outline at the height of the centre, plus a centre cross; arc: tangent point;
  bounding-box edge: a point there that the outline reaches in every such view). Basic dimensions (baseline, overall dimension)
  try normal views and sections before detail views, form and chain dimensions the larger scale first. If there is
  none, the first view with a suitable direction applies (better a dimension on a hidden edge than none).
- Basic dimensions top/left, form and chain dimensions (square, pitch, depth) bottom/right; per view and side the shortest inside,
  at most MAX_STACK per view side (overflow: another view with room, then the opposite side, never dropped).
- R dimensions only on visible edges (view along the axis, otherwise leader line "R...", otherwise hidden with a finding in the render);
  arc centres serve as dimension reference only in the view along the axis and in the part; equal dimensions per part only once.
- Diameter in the circle, otherwise in the section; turned parts and coaxial sections of equal diameter along the axis. Every annotation carries
  'feature' (thread: the target cylinder whose size the designation gives).
- Tolerance (8, 'h11'): exactly one dimension with value 8 ± 0.005 and matching kind (small: outside dimension = shaft, square, SØ or
  plane pair with outward-pointing normals; large: hole or plane pair with inward-pointing normals) and,
  if the text names the form feature (square/square bar, hole, shaft/pin/bolt, Ø), a matching form feature ->
  'tolerance', otherwise the note "Tolerance from model text: ..." (TOL_NOTE).
- Engraving: note line engraving_note (depth as absolute value, "recessed"/"raised" only when measured).
- part['freeform'] (from features.part_features) marks a part with a freeform portion. It stays normally mandatory for all analytical
  elements; in addition the bounding-box dimensions of the part, the note FREEFORM_TEXT
  and for each freeform region (part['regions']) envelope dimensions (position + extent) and a leader line CONTOUR_TEXT (model_ref).
- Function dimensions: numbers from model, part and form-feature names (texts, plan_facts.function_values) that match exactly
  one distance between two planes are dimensioned directly (priority over the chain derivation); value always from the geometry,
  ambiguous or matching diameters/radii only in the report (never guess)."""
import collections
import re
from collections import defaultdict
import numpy as np
import analyze as an
import checks
import plan_roles
import profiles
import projection as prj
from rules import MIN_AXIS_MM
from sheet import fmt

RIM_BIN = 10.0          # degrees: as features.RIM_BIN
TOL = 0.01                  # mm, same coordinate (as checks.TOL)
MAX_STACK = 8               # at most this many stacked parallel dimensions per view side
LOCATED = ('hole', 'shaft', 'arc', 'sphere')
ELEMENTS = ('pocket', 'step', 'slot', 'plane_pattern')             # compound elements (features.ELEMENTS)
ELEMENTS_PLAN = ('plane_pattern', 'pocket', 'slot', 'step')        # order of their distances in the tree
LEADER_MM = 14.0            # leader line of an item number on paper
STEP_DEG = 20.0             # minimum angle between two item-number leader lines
BEND_NOTE = 'Bend points = intersections of the tangents; coordinates relative to P0'
# Note texts that checks.py recognises by their wording (checks.FREEFORM_RX, checks.MINOR_TEXT, the coverage
# words for knurls and engravings, the family words of general notes); change them only together with checks.py.
FREEFORM_TEXT = 'Shape according to 3D model'
FREEFORM_MINOR_TEXT = 'Shape and undimensioned forms according to 3D model'
CONTOUR_TEXT = 'Contour according to 3D model'
GENERAL_PREFIX = 'Undimensioned'
KNURL_WORD = 'Knurl'
ENGRAVING_EMBLEM_TEXT = 'Engraving according to 3D model'
AX = 'xyz'


def _v(p):
    return np.asarray(p, float)


def _r(p):
    return [round(float(x), 4) + 0.0 for x in p]


def _world(d):
    """Index of the world axis parallel to d, or None."""
    k = int(np.argmax(np.abs(d)))
    return k if abs(d[k]) > 1 - 1e-6 else None


class _Tree:
    """Coordinates of an axis (equal up to TOL): join only takes a distance if it does not already follow from the ones taken (otherwise it would be duplicate or close a chain).
    "follows" is decided by the rank oracle of the check (chains.Rank, the same computation as chains.derivable): every distance is the row x_b - x_a = b - a.
    Mirror closure: with the symmetry plane m, join(a, b) brings the mirror row x_c + x_c' = 2m for both ends (c' = 2m - c; the same rows as checks.chain_mirrors
    in the check, once per coordinate, before the decision about the distance). Thus every distance is also known as a mirror image (a mirror member would be derivable), and a distance
    across the median plane (a + b = 2m) is fixed absolutely: two such distances determine every dimension between their ends (union-find without signs did not
    see that). join(..., mirror=False) brings no mirror rows (view without symmetry line). plain carries the same distances without mirror rows: closed(a, b) means "only the closure
    connects", mirror_drops counts the distances that it rejected."""

    def __init__(self, m=None):
        import chains
        self.rank = chains.Rank(TOL)
        self.m = m
        self.plain = _Tree() if m is not None else None
        self.mirror_drops = 0
        self._mirrored = set()            # columns with a mirror row

    def _row(self, a, b):
        i, j = self.rank.node(a), self.rank.node(b)
        return ({j: 1.0, i: -1.0} if i != j else None), float(b) - float(a)

    def same(self, a, b):
        coef, d = self._row(a, b)
        return coef is None or self.rank.classify(coef, d) != 'new'

    def _join(self, a, b):
        coef, d = self._row(a, b)
        return coef is not None and self.rank.add(coef, d)

    def _anchor(self, c):
        """Mirror row x_c + x_c' = 2m of the coordinate c (a coordinate in the plane: x_c = m)."""
        i, j = self.rank.node(c), self.rank.node(2 * self.m - c)
        if i in self._mirrored:
            return
        self._mirrored.update((i, j))
        self.rank.add({i: 2.0} if i == j else {i: 1.0, j: 1.0}, 2.0 * self.m)

    def closed(self, a, b):
        """Does only the mirror closure connect the two coordinates (without it they would be separate)?"""
        return self.m is not None and self.same(a, b) and not self.plain.same(a, b)

    def join(self, a, b, mirror=True):
        if self.m is None:
            return self._join(a, b)
        fresh = self.plain.join(a, b)
        if mirror:
            self._anchor(a)
            self._anchor(b)
        new = self._join(a, b)
        if fresh and not new:
            self.mirror_drops += 1
        return new


# ------------------------------------------------------------------ Views and anchor points

def _views(views, pid):
    out = []
    for i, v in enumerate(views):
        if v.get('part') != pid:
            continue
        fr = prj.frame_of(v)
        if _world(fr[0]) is None and not v.get('auxiliary'):
            continue                                   # oblique view: lengths distorted (auxiliary view: true in the image plane)
        det = v.get('detail')
        sc = det.get('scale') if det else v.get('scale')
        out.append({'id': v['id'], 'fr': fr, 'shows': set(v.get('features') or []), 'i': i, 'section': bool(v.get('section')),
                    'aux': bool(v.get('auxiliary')), 'members': set(v.get('aux_members') or v.get('features') or []),
                    'cut': _v(v['section']['point']) if v.get('auxiliary') and v.get('section') else None,
                    'sect': _v(v['section']['point']) if v.get('section') else None,
                    'k': an.scale_value(sc) if sc else 0.0, 'det': (_v(det['center']), float(det['radius'])) if det else None})
    by = {w['id']: w for w in out}
    for w in out:
        host = by.get(next((v.get('detail_circle_in') for v in views if v['id'] == w['id'] and v.get('pattern_detail')), None))
        if host is not None:          # pattern detail - its dimensions count for the mirror closure like those of the view it comes from (host)
            w['pat_host'] = host
    return sorted(out, key=lambda w: (-w['k'], w['i']))


def _inside(w, p):
    if w['det'] is None:
        return True
    q = prj.project([p, w['det'][0]], w['fr'])
    return float(np.linalg.norm(q[0] - q[1])) <= w['det'][1] + 1e-9


def _orient(w, d):
    N, Vx, Vy = w['fr']
    if abs(d @ Vx) > 1 - 1e-6:
        return 'h'
    if abs(d @ Vy) > 1 - 1e-6:
        return 'v'
    return 'aligned' if abs(d @ N) < 1e-6 else None


def _on_face(f, c):
    """Point of plane f at c (plane coordinate inserted), if it lies in its face bbox."""
    if not f.get('bbox'):
        return None
    q = _v(c).copy()
    q[AX.index(f['axis_name'])] = f['coord']
    lo, hi = f['bbox']['min'], f['bbox']['max']
    return q if all(lo[j] - TOL <= q[j] <= hi[j] + TOL for j in range(3)) else None


def _outline(m, w, d):
    """Point on the drawn outline of a sphere or an arc (marker from _anchors) with the coordinate of its
    centre along the dimension direction d: sphere at the edge across d on the side of the basic dimensions (left or top), only if it lies
    in the bounding box of the part (dome: the equator is missing); arc at the tangent point in the direction of the arc, only in the
    view along its axis. Otherwise None (fallback: centre)."""
    N, Vx, Vy = w['fr']
    c, r = _v(m['center']), float(m['radius'])
    if m['kind'] == 'sphere':
        q = c + r * (-Vx if abs(d @ Vy) > 0.5 else Vy)
        return q if all(m['bb']['min'][j] - TOL <= q[j] <= m['bb']['max'][j] + TOL for j in range(3)) else None
    u = np.cross(N, d)
    s = float(u @ m['dir'])
    if abs(s) <= 1e-9 and _world(u) is not None and AX[_world(u)] in m['tan']:
        s = 1.0                                    # tangential on both sides across d (slot end): both points lie on the arc
    return c + r * np.sign(s) * u if abs(_v(m['axis']) @ N) > 0.99 and abs(s) > 1e-9 else None


def _pick(w, anchors, strict, d):
    """First anchor (ID, point, plane|sphere/arc marker|None) whose form feature the view shows (ID None: always)
    and which lies in the circle in a detail view; strict=False ignores visibility. Before the point itself: for a
    plane in a detail view its point in the circle, for sphere/arc the outline point (the centre is not a drawn line)."""
    for fid, p, f in anchors:
        if strict and fid is not None and fid not in w['shows']:
            continue
        pts = [p]
        if f is not None and f['kind'] == 'plane' and w['det'] is not None:
            pts.insert(0, _on_face(f, w['det'][0]))
        elif f is not None and f['kind'] in ('sphere', 'arc'):
            pts.insert(0, _outline(f, w, d))
            if f['kind'] == 'arc':                     # only points on the arc; the centre only in the view along the axis and in the part
                inside = all(f['bb']['min'][j] - TOL <= p[j] <= f['bb']['max'][j] + TOL for j in range(3))
                pts = pts[:1] + ([p] if abs(f['axis'] @ w['fr'][0]) > 0.99 and inside else [])
        q = next((q for q in pts if q is not None and _inside(w, q)), None)
        if q is not None:
            return q
    return None


def _place(ws, opts, room=None, only_strict=False):
    """(view, orientation, p1, p2, index of the option) for a linear dimension; opts = [(direction, anchor1, anchor2)];
    room(view, orientation) -> False skips a view side that is already full (budget per view side);
    only_strict: only views that show the form features."""
    for strict in (True,) if only_strict else (True, False):
        for w in ws:
            for i, (d, a1, a2) in enumerate(opts):
                o = _orient(w, d)
                if room is not None and o and not room(w, o):
                    continue
                p1, p2 = (_pick(w, a1, strict, d), _pick(w, a2, strict, d)) if o else (None, None)
                if p1 is not None and p2 is not None:
                    return w, o, p1, p2, i
    return None


def _axis_pts(f):
    if f.get('start') is not None:
        s, e = _v(f['start']), _v(f['end'])
        return [s, (s + e) / 2, e]
    return [_v(f['center'])]


def _anchors(f, feats, bb):
    """Anchors of the position of a hole/shaft/arc/sphere: axis points; for sphere and arc the outline marker first."""
    out = [(f['id'], p, None) for p in _axis_pts(f)]
    if f['kind'] == 'arc':
        out = []                                       # the centre of an arc is not a drawn edge (fallback in _pick)
    if f['kind'] == 'sphere':
        out.insert(0, (f['id'], _v(f['center']), {'kind': 'sphere', 'center': f['center'], 'radius': f['diameter'] / 2, 'bb': bb}))
    elif f['kind'] == 'arc':
        A, c = _v(f['axis']), _v(f['center'])
        out.insert(0, (f['id'], c, {'kind': 'arc', 'center': c, 'radius': f['radius'], 'axis': A, 'tan': set(f.get('tangent') or {}), 'bb': bb,
                                    'dir': _toward(c, f['radius'], [k for k in range(3) if abs(A[k]) < 1e-6], feats)}))
    return out


def oblique_reference(feats, N, d, near, cut=None):
    """Reference point of a position dimension along the oblique image direction d of an auxiliary view (view N): point on a
    part edge across d - intersection line of two planes of different axes whose face bboxes both reach there;
    in an auxiliary section (cut = point of the cutting plane perpendicular to N) only their part behind the plane, plus the intersection lines of the planes with
    the cutting plane (edge of the cut face) - that lies furthest down or left (smallest coordinate along d:
    outline, hence visible); on the edge the point nearest near. None without such an edge (turned part, fastener: the position across
    the axis follows from the symmetry)."""
    planes = [f for f in feats if f['kind'] == 'plane' and f.get('bbox')]
    near, best = _v(near), None
    h = None if cut is None else float(_v(cut) @ N)

    def take(pt):
        nonlocal best
        key = (round(float(pt @ d), 6), round(float(np.linalg.norm(pt - near)), 6))
        if best is None or key < best[0]:
            best = (key, pt)
    for i, p in enumerate(planes):
        for q in planes[i + 1:]:
            a, b = AX.index(p['axis_name']), AX.index(q['axis_name'])
            c = 3 - a - b
            if a == b or abs(d[c]) > 1e-6 or abs(N[c]) > 1 - 1e-6:
                continue                               # same axis, edge not across d or pointing at the camera
            P, Q = p['bbox'], q['bbox']
            if not (Q['min'][a] - TOL <= p['coord'] <= Q['max'][a] + TOL and P['min'][b] - TOL <= q['coord'] <= P['max'][b] + TOL):
                continue
            lo, hi = max(P['min'][c], Q['min'][c]), min(P['max'][c], Q['max'][c])
            if h is not None:                          # only behind the cutting plane: N[a]·pa + N[b]·pb + N[c]·t < h
                rest = h - TOL - N[a] * p['coord'] - N[b] * q['coord']
                if abs(N[c]) < 1e-9:
                    lo, hi = (lo, hi) if rest > 0 else (1.0, 0.0)
                elif N[c] > 0:
                    hi = min(hi, rest / N[c])
                else:
                    lo = max(lo, rest / N[c])
            if hi - lo <= TOL:
                continue
            pt = np.zeros(3)
            pt[a], pt[b], pt[c] = p['coord'], q['coord'], min(max(float(near[c]), lo), hi)
            take(pt)
    for p in planes if h is not None else []:          # edge of the cut face: plane ∩ cutting plane, horizontal or vertical
        a = AX.index(p['axis_name'])
        for b in (k for k in range(3) if k != a):
            c = 3 - a - b
            if abs(N[b]) < 1e-6 or abs(float(np.cross(np.eye(3)[a], N) @ d)) > 1e-6:
                continue
            pt = np.zeros(3)
            pt[a], pt[c] = p['coord'], float(near[c])
            pt[b] = (h - N[a] * pt[a] - N[c] * pt[c]) / N[b]
            if all(p['bbox']['min'][j] - TOL <= pt[j] <= p['bbox']['max'][j] + TOL for j in range(3)):
                take(pt)
    return None if best is None else best[1]


def _outside(f, bb):
    """Arc whose centre lies outside the part bounding box or that runs tangentially into two planar faces (transition arc): only gets its R, no position dimension."""
    return f['kind'] == 'arc' and bool(f.get('trans') or any(f['center'][k] < bb['min'][k] - TOL or f['center'][k] > bb['max'][k] + TOL for k in range(3)))


def _extreme(k, c, feats, bb):
    """Anchor of the bounding-box edge c along k (ID None: the outline reaches the edge in every view with k in the
    image plane): plane there, centre of a face that reaches c, finally the bounding-box corner."""
    if min(abs(c - bb['min'][k]), abs(c - bb['max'][k])) > TOL:
        return []
    own, side = [], []
    for f in feats:
        if f['kind'] != 'plane':
            continue
        if f['axis_name'] == AX[k] and abs(f['coord'] - c) <= TOL:
            own.append((None, _v(f['pt']), f))
        elif f.get('bbox') and min(abs(f['bbox']['min'][k] - c), abs(f['bbox']['max'][k] - c)) <= TOL:
            q = (_v(f['bbox']['min']) + _v(f['bbox']['max'])) / 2
            q[k] = c
            side.append((None, q, None))
    q = _v(bb['min']).copy()
    q[k] = c
    return own + side + [(None, q, None)]


def _edge_anchors(j, c, A, feats, bb):
    """Reference of a position across an axis-parallel oblique edge A: the edge plane of the bounding box at c along j, otherwise the
    nearest plane of this direction (a drawn edge); never the bounding-box corner or a face centre (nothing invented). [] without a plane."""
    own = [a for a in _extreme(j, c, feats, bb) if a[2] is not None]
    if own:
        return own
    near = sorted((f for f in feats if f['kind'] == 'plane' and f['axis_name'] == AX[j] and abs(f['coord'] - A[j]) > 0.01),
                  key=lambda f: (abs(f['coord'] - A[j]), f['id']))
    return [(near[0]['id'], _v(near[0]['pt']), near[0])] if near else []


def _at(k, c, feats, bb):
    """Anchors of all form features at the coordinate c along k: planes, axes across k, ends of axes parallel to k."""
    e = np.eye(3)[k]
    planes, axes = [], []
    for f in feats:
        if f['kind'] == 'plane':
            if f['axis_name'] == AX[k] and abs(f['coord'] - c) <= TOL:
                planes.append((f['id'], _v(f['pt']), f))
        elif f['kind'] in LOCATED and not _outside(f, bb):
            pts, ax = _axis_pts(f), f.get('axis')
            if ax is None or abs(_v(ax) @ e) < 1e-6:
                axes += _anchors(f, feats, bb) if abs(pts[0] @ e - c) <= TOL else []
            elif f['kind'] in ('hole', 'shaft') and abs(abs(_v(ax) @ e) - 1) < 1e-6:
                axes += [(f['id'], p, None) for p in pts[::2] if abs(p @ e - c) <= TOL]
    return planes + axes + _extreme(k, c, feats, bb)


def _art(k, c1, c2, feats):
    """'ext' (outside dimension) if planes on both coordinates point outward, 'int' if inward, otherwise None."""
    lo, hi = sorted((c1, c2))
    n = lambda c: next((f['normal'][k] for f in feats if f['kind'] == 'plane' and f['axis_name'] == AX[k]
                        and abs(f['coord'] - c) <= TOL), 0.0)
    a, b = n(lo), n(hi)
    return 'ext' if a < 0 < b else 'int' if b < 0 < a else None


# ------------------------------------------------------------------ Size dimensions

def _size_view(ws, cands, ax, circle_first):
    """(view, form feature, point) for a size dimension; cands = [(form feature, points)], ax = axis or None (sphere).
    Circle views first (circle_first) or views with the axis in the image plane, sections first there."""
    def kind(w):
        if ax is None:
            return 0
        c = abs(ax @ w['fr'][0])
        return 0 if c > 0.99 else 1 if c < 1e-3 else None
    good = [w for w in ws if kind(w) is not None]
    order = sorted(good, key=lambda w: (kind(w) != (0 if circle_first else 1), kind(w) == 1 and not w['section']))
    # a circle that does not fit into the circle of the detail view is not dimensioned there (e.g. Ø6.3 at the centre, the circle itself
    # lies outside); only if no other view remains, there after all
    for strict, fit in ((True, True), (False, True), (True, False), (False, False)):
        for w in order:
            for f, pts in cands:
                if strict and f['id'] not in w['shows']:
                    continue
                if fit and w['det'] is not None and f.get('diameter') and f['diameter'] / 2 > w['det'][1] + 1e-9:
                    continue
                p = next((q for q in pts if _inside(w, q)), None)
                if p is not None:
                    return w, f, p
    return None


def _coaxial(f, g):
    a = _v(f['axis'])
    if abs(abs(a @ _v(g['axis'])) - 1) > 1e-6:
        return False
    q = _v(g['start']) - _v(f['start'])
    return float(np.linalg.norm(q - (q @ a) * a)) <= TOL


def _toward(c3, r, dirs, feats, cyl=None):
    """Direction from the arc centre c3 to the arc: towards the tangential planes at distance r along the world axes dirs;
    cyl = (axis, centre, major radius, w) additionally checks coaxial cylinders of radius major radius ± r for tori, whose
    axial range contains the torus centre. If tangential faces lie on both sides, the direction does not count."""
    out = np.zeros(3)
    for k in dirs:
        s = [sg for sg in (1, -1) if any(f['kind'] == 'plane' and f['axis_name'] == AX[k]
                                         and abs(f['coord'] - (c3[k] + sg * r)) <= TOL for f in feats)]
        if len(s) == 1:
            out[k] += s[0]
    if cyl is not None:
        A, C, R, w = cyl
        span = lambda f: sorted((float((_v(f['start']) - C) @ A), float((_v(f['end']) - C) @ A)))
        s = [sg for sg in (1, -1) if any(f['kind'] in ('hole', 'shaft') and abs(f['diameter'] / 2 - (R + sg * r)) <= TOL
                                         and _coaxial(f, {'axis': A, 'start': C}) and span(f)[0] - TOL <= 0 <= span(f)[1] + TOL
                                         for f in feats)]
        if len(s) == 1:
            out += s[0] * w
    return out


def _radius_ann(ws, f, feats, visible_only=True, ray=None):
    """R dimension of a fillet/arc: cylinder in the view along the axis, torus (shaft shoulder) with the axis
    in the image plane at the tube centre. Angle to the arc via the tangential faces, otherwise 45°. Only views that show the
    form feature visibly; visible_only=False is the last resort (hidden edge, the render reports it)."""
    A, C, r = _v(f['axis']), _v(f['center']), float(f['radius'])
    R = f.get('major_radius')
    for strict in (True,) if visible_only else (True, False):
        for w in sorted(ws, key=lambda w: w['cut'] is not None):         # auxiliary sections last (their "shows" does not know the section: the R dimension then stood next to the arc in empty space)
            if strict and f['id'] not in w['shows']:
                continue
            N, Vx, Vy = w['fr']
            if R:
                if abs(A @ N) > 1e-3:
                    continue
                u = prj.unit(np.cross(N, A))
                u = u if u @ Vy >= 0 else -u
                c3 = C + R * u
                d = _toward(c3, r, [k for k in range(3) if abs(abs(A[k]) - 1) < 1e-6], feats, (A, C, R, u))
            else:
                if abs(A @ N) < 0.99:
                    continue
                c3 = C
                d = _toward(c3, r, [k for k in range(3) if abs(A[k]) < 1e-6], feats)
            if not _inside(w, c3):
                continue
            if not R and not np.linalg.norm(d) and f.get('dirs'):      # without a tangential plane the R dimension points to the centre of the arc (dirs), not blindly at 45° into empty space
                d = _v(f['dirs'][1])
            q = np.array([d @ Vx, d @ Vy])
            ang = float(np.degrees(np.arctan2(q[1], q[0]))) if np.linalg.norm(q) > 1e-9 else 45.0
            out = {'type': 'radius', 'view': w['id'], 'center': _r(c3), 'radius': r, 'angle': round(ang, 3), 'feature': f['id']}
            if f.get('span_deg') and not R:        # angular range of the arc (°) on the sheet; autofix turns the tip only within it (a pull to 45° left an "2x R19.85" next to the arc in empty space)
                out['span'] = round(float(f['span_deg']), 1)
            if strict and ray is not None and _hidden(out, w, feats, ray):        # the tip lies behind the part in this view (ray test): next view, otherwise the leader line
                continue
            return out
    return None


def _arc_tips(w, C, r, A, arc):
    """Tips of a leader line on the visible edge of the arc (arc['dirs'] = directions centre -> start/middle/end)
    in view w: view along the axis - arc centre (drawn circle edge); axis in the image plane - the outline point of the generatrix
    (direction N x A, both signs) if it lies in the angular range of the arc, otherwise the surface point at the arc centre (the tip
    lies on the arc surface, never on the ideal circle next to it)."""
    N = w['fr'][0]
    c = abs(A @ N)
    d0, dm, d1 = (_v(x) for x in arc['dirs'])
    if c > 0.99:
        return [C + r * dm]
    if c < 1e-3:
        u = prj.unit(np.cross(N, A))
        edge = [C + r * sg * u for sg in (1, -1) if float(sg * u @ dm) >= np.cos(np.radians(float(arc['span_deg']) / 2)) - 1e-6]
        return edge or [C + r * dm]                    # without outline: the surface point at the arc centre (on the arc surface itself)
    return [C + r * dm]


def _surface_leader(ws, fid, C, r, A, text, shows_id=None, visible_only=False, arc=None):
    """Leader line to the edge (radius r around the axis A through C): axis in the image plane first, otherwise circle view. With arc (arc with
    directions) the tip lies on a visible edge of the arc itself (_arc_tips), not on the ideal circle."""
    for strict in (True,) if visible_only else (True, False):
        for w in sorted(ws, key=lambda w: abs(A @ w['fr'][0]) > 1e-3):
            N, Vx, Vy = w['fr']
            if strict and (shows_id or fid) not in w['shows']:
                continue
            c = abs(A @ N)
            if arc is not None and arc.get('dirs'):
                tip = next((t for t in _arc_tips(w, C, r, A, arc) if _inside(w, t)), None)
            else:
                tip = C + r * prj.unit(np.cross(N, A)) if c < 1e-3 else C + r * (Vx + Vy) / np.sqrt(2) if c > 0.99 else None
                tip = tip if tip is not None and _inside(w, tip) else None
            if tip is not None:
                out = {'type': 'leader', 'view': w['id'], 'p': _r(tip), 'text': text, 'feature': fid}
                if checks.RADIUS_RX.search(text or ''):
                    out['to'] = _radius_to(w, tip, C)
                return out
    return None


RADIUS_LEADER_MM = 10.0      # leader line of an R dimension on paper (without a collision this suffices for the text to stand clear)
RADIUS_LEADER_MAX = 15.0     # ... and at most this long as long as no collision (autofix: rotate, then stretch) demands more


def _radius_to(w, tip, C):
    """Leader line of an R dimension as a sheet vector 'to' (length RADIUS_LEADER_MM): from the arc outwards (direction centre -> tip in the
    image plane), the text stands at the arc and not over foreign views; without a direction (axis along the viewing direction) 45°."""
    N, Vx, Vy = w['fr']
    d = np.array([(tip - C) @ Vx, (tip - C) @ Vy])
    d = d / np.linalg.norm(d) if np.linalg.norm(d) > 1e-6 else np.array([1.0, 1.0]) / np.sqrt(2)
    return [round(float(x), 2) for x in RADIUS_LEADER_MM * d]


GENERAL_PER_LINE = 6     # sizes per note line of a collective general note (cones/countersinks, oblique faces)


def _point_leader(ws, fid, pt, text, visible_only=False):
    """Leader line to a point of the face (inclined plane chamfer, cone/countersink, oblique face) in the first view that shows the
    form feature visibly and contains the point (views by scale, detail views first); otherwise (no way out) in every one."""
    for strict in (True,) if visible_only else (True, False):
        for w in ws:
            if strict and fid not in w['shows']:
                continue
            if _inside(w, _v(pt)):
                return {'type': 'leader', 'view': w['id'], 'p': _r(_v(pt)), 'text': text, 'feature': fid}
    return None


# ------------------------------------------------------------------ Slants and cones according to the standard
SLANT_ARC_MM = 10.0        # radius of the angle arc on paper


def _slant_segment(f, N):
    """(A, B): end points of the oblique edge in the elevation along N (N perpendicular to the face normal), both at the depth of the face point.
    The face appears there as a line (true length): its extent follows from the corners of the face bbox that lie in the plane,
    otherwise from the intersection of the line through the face point with the bbox. None without face point, normal or bbox."""
    if not (f.get('pt') and f.get('normal') and f.get('bbox')):
        return None
    n, p = prj.unit(f['normal']), _v(f['pt'])
    t = np.cross(N, n)
    if np.linalg.norm(t) < 1e-9:
        return None
    t = t / np.linalg.norm(t)
    if f.get('_outline') and np.min(np.abs(n)) > 1e-3:
        points = np.asarray(f['_outline'])
        s = points @ t
        if s.max() - s.min() <= TOL:
            return None
        # With the same projection the foremost real edge point is the visible dimension reference.
        ends = [points[np.abs(s - limit) < 1e-5] for limit in (s.min(), s.max())]
        return tuple(side[np.argmax(side @ N)] for side in ends)
    lo, hi = _v(f['bbox']['min']), _v(f['bbox']['max'])
    corners = [np.array([x, y, z]) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    ss = sorted(float((c - p) @ t) for c in corners if abs(float((c - p) @ n)) <= 0.02)
    if len(ss) >= 2 and ss[-1] - ss[0] > TOL:
        s0, s1 = ss[0], ss[-1]
    else:                                      # fallback: line through the face point, clipped at the bbox
        s0, s1 = -np.inf, np.inf
        for j in range(3):
            if abs(t[j]) > 1e-9:
                a, b = sorted(((lo[j] - p[j]) / t[j], (hi[j] - p[j]) / t[j]))
                s0, s1 = max(s0, a), min(s1, b)
        if not (np.isfinite(s0) and np.isfinite(s1)) or s1 - s0 <= TOL:
            return None
    depth = lambda q: q + (float(p @ N) - float(q @ N)) * N
    return depth(p + s0 * t), depth(p + s1 * t)


def _slant_reference(f, w, A, B, feats):
    """(vertex, end of the oblique edge on the leg, point on the reference leg, [extension lines]) of the angle dimension of a slant in
    view w: both legs are real lines. Ranking of the references: (a) edge of a plane (world-axis normal in the image plane) at the
    end of the oblique edge, (a2) on its extension up to the intersection with the plane edge (the piece up to the vertex is a thinly
    drawn extension line, at most twice the slant length), (b) drawn centre line of a hole/shaft in the image plane. The vertex
    must lie on the reference edge, its far end point is the point of the second leg. Then: the inclination itself (angle equal to
    angle_deg), acute before obtuse - if only the obtuse angle is real at the existing edge, the acute supplementary angle stands against the
    thinly drawn extension of this edge. None without a reference edge (fallback c)."""
    N = w['fr'][0]
    inview = [j for j in range(3) if abs(np.eye(3)[j] @ N) < 1e-6]        # world axes in the image plane: planes with this normal appear as an edge
    k = int(np.argmax(np.abs(_v(f['normal']))))
    a_deg = float(f.get('angle_deg') or 0.0)
    B = B - ((B - A) @ N) * N       # construction of the angle legs in the image plane
    L = B - A
    Ln = float(np.linalg.norm(L))
    q2 = lambda p: prj.project([p], w['fr'])[0]
    cands = []                                  # (rank, vertex, s, direction ei, range lo, hi, plane across the main inclination)
    for j in inview:
        if abs(L[j]) < 1e-9:
            continue
        ei = prj.unit(np.cross(N, np.eye(3)[j]))                          # direction of this edge in the image (world axis or, in the auxiliary view, horizontal)
        for q in feats:
            if q['kind'] != 'plane' or not q.get('bbox') or q['axis_name'] != AX[j]:
                continue
            s_ = (q['coord'] - A[j]) / L[j]
            V = A + s_ * L
            lo, hi = _v(q['bbox']['min']), _v(q['bbox']['max'])
            cs = [np.array([x, y, z]) @ ei for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
            if min(cs) - TOL <= float(V @ ei) <= max(cs) + TOL:
                cands.append((0, V, s_, ei, min(cs), max(cs), j != k))
    for g in feats:                                                       # b: drawn centre line
        if g['kind'] not in ('hole', 'shaft') or g.get('axis') is None or g.get('start') is None:
            continue
        e = prj.unit(g['axis'])
        if abs(float(e @ N)) > 1e-6:
            continue
        S0, S1 = _v(g['start']), _v(g['end'])
        M = np.array([q2(B) - q2(A), -(q2(S1) - q2(S0))]).T
        if abs(float(np.linalg.det(M))) < 1e-9:
            continue
        s_, _ = np.linalg.solve(M, q2(S0) - q2(A))
        ends = sorted((float(S0 @ e), float(S1 @ e)))
        V = A + s_ * L
        if ends[0] - TOL <= float(V @ e) <= ends[1] + TOL:
            cands.append((2, V, float(s_), e, ends[0], ends[1], True))
    best = None
    for rank, V, s_, ei, qlo, qhi, off_main in cands:
        near, far = (A, B) if np.linalg.norm(V - A) <= np.linalg.norm(V - B) else (B, A)
        ext = 0.0 if -1e-6 <= s_ <= 1.0 + 1e-6 else float(np.linalg.norm(V - near))
        if ext > 2.0 * Ln or np.linalg.norm(far - V) < 1e-6:
            continue
        rk = rank if rank == 2 else (1 if ext > TOL else 0)
        t = prj.unit(far - V)
        at = float(V @ ei)
        lines = [(near, V)] if ext > TOL else []
        for sg in (1, -1):
            room = (qhi - at) if sg > 0 else (at - qlo)
            ang = float(np.degrees(np.arccos(np.clip(t @ (sg * ei), -1, 1))))
            opts = []
            if room > 0.5:
                opts.append((ang, False, V + sg * ei * room, lines))
            if ang > 90.0 and (qhi - at if -sg > 0 else at - qlo) <= 0.5:      # only the obtuse angle is real: acute one against the drawn extension
                d = -sg * ei
                opts.append((180.0 - ang, True, V + d * 0.05, lines + [(V, V + d * 0.6 * float(np.linalg.norm(far - V)))]))
            for a_, virtual, P2, ln in opts:
                if rk == 2 and abs(a_ - 90.0) < 1.0:          # centre line perpendicular to the edge (end face of an oblique shaft): no meaningful angle dimension
                    continue
                key = (rk, round(ext, 3), abs(a_ - a_deg) > 0.2, a_ > 90.0, virtual, not off_main)
                if best is None or key < best[0]:
                    best = (key, V, far, P2, ln)
    return None if best is None else best[1:]


def _linear_value(a, ws):
    """Value of a linear dimension a: length of the segment, for a horizontal/vertical dimension its projection on the image axis."""
    d = _v(a['p2']) - _v(a['p1'])
    w = next((w for w in ws if w['id'] == a['view']), None)
    if w is not None and a.get('axis') in ('h', 'v'):
        d = d @ w['fr'][1 if a['axis'] == 'h' else 2]
        return round(abs(float(d)), 4)
    return round(float(np.linalg.norm(d)), 4)


def _mouth_widths(f, mate, ws, bb, ray):
    """Clear widths and course of two mirror-equal, opposed slanted faces in normal views."""
    if not f.get('_outline') or not mate.get('_outline') or np.min(np.abs(f['normal'])) < 1e-3:
        return []
    a, b = [np.unique(np.round(g['_outline'], 5), axis=0) for g in (f, mate)]
    n, m = np.asarray(f['normal']), np.asarray(mate['normal'])
    mid = (np.asarray(bb['min']) + bb['max'])/2
    normal_views = [w for w in ws if not w['aux'] and not w['det'] and _world(w['fr'][0]) is not None]
    for k in range(3):
        d = np.eye(3)[k]
        if not np.allclose(n-2*n[k]*d, m, atol=1e-4) or (a[:,k]-mid[k]).min() * (a[:,k]-mid[k]).max() <= 0:
            continue
        stations = []
        for limit in (a[:,k].min(), a[:,k].max()):
            pairs = []
            for p in a[np.abs(a[:,k]-limit) < 0.001]:
                mirror = p + 2*(mid[k]-limit)*d
                q = b[np.argmin(np.linalg.norm(b-mirror, axis=1))]
                if np.linalg.norm(q-mirror) < 0.02:
                    pairs.append((p,q))
            if not pairs:
                break
            stations.append(pairs)
        if len(stations) != 2 or min(abs(p[0][k]-mid[k]) for station in stations for p in station) < 0.01:
            continue

        def measure(pairs, axis):
            vec = np.eye(3)[axis]
            for w in sorted(normal_views, key=lambda w: (w['section'], w['i'])):
                orient = _orient(w, vec)
                if orient not in ('h', 'v'):
                    continue
                for p,q in pairs:
                    if abs((q-p) @ vec) < TOL:
                        continue
                    if w['sect'] is not None and max((p-w['sect']) @ w['fr'][0], (q-w['sect']) @ w['fr'][0]) > TOL:
                        continue
                    if hasattr(ray, 'contour') and not ray.contour(w,[p,q]).all():
                        continue
                    if ray and any(_covered(w,pt,[],ray,vec) for pt in (p,q)):
                        continue
                    return {'type':'linear', 'view':w['id'], 'p1':_r(p), 'p2':_r(q), 'axis':orient,
                            'side':'above' if orient=='h' else 'left', 'feature':f['id'], 'also':[mate['id']],
                            'orthographic_stations': True}
            return None

        widths = [measure(station, k) for station in stations]
        if not all(widths):
            continue
        for width in widths:
            width['opening_axis'] = AX[k]
        # The same real edge connects the stations. Its longitudinal and height offset appears in standard views.
        # Sort the ends in the same sense so that the dimensions do not describe a diagonal between the two edges.
        across = np.cross(n,d)
        stations = [sorted(station,key=lambda pair: pair[0] @ across) for station in stations]
        joins = [(stations[0][i][j],stations[1][i][j]) for i in (0,-1) for j in (0,1)]
        trace = [measure(joins, i) for i in range(3) if i != k and abs((joins[0][1]-joins[0][0])[i]) > TOL]
        if all(trace):
            return widths + trace
    return []


REFLINE_FOOT = 0.05      # mm: foot point of the angle leg on the reference line behind the vertex (like the drawn extension in _slant_reference): it lies on the line, otherwise dim_point_off_geometry
SLANT_REF = 'slant_ref'  # marker of the planner-generated reference line (checks.bare_polyline): polylines from other sources do not carry it
REFLINE_FRAC = 0.6       # thin reference line: this fraction of the slant length


def _slant_views(f, ws):
    """Candidates [(view, A, B, length dimension, shown)] of a slant: views in which the face appears as a line (normal perpendicular to the viewing direction), both end points of the
    edge lie in the view (detail views) and the edge can carry a dimension; views showing it visibly first, then the others, each in view order."""
    n = prj.unit(f['normal'])
    seen, out = set(), []
    for strict in (True, False):
        for w in ws:
            if w['id'] in seen or (strict and f['id'] not in w['shows']):
                continue
            N = w['fr'][0]
            if abs(float(n @ N)) > 1e-3:
                continue
            seg = _slant_segment(f, N)
            if seg is None or not all(_inside(w, q) for q in seg):
                continue
            A, B = seg
            o = _orient(w, prj.unit(B - A - ((B - A) @ N) * N))
            if o is None:
                continue
            seen.add(w['id'])
            out.append((w, A, B, {'type': 'linear', 'view': w['id'], 'p1': _r(A), 'p2': _r(B), 'axis': o, 'side': {'h': 'above'}.get(o, 'left'), 'feature': f['id']}, strict))
    return out


def _slant_refline(f, w, A, B, bb):
    """Last rank of the angle reference if no view supplies a body edge: a thin auxiliary line parallel to a world axis of the image plane through an end point of the slanted edge
    (only then does the angle value match checks.slant_angle_ok: inclination or complement 90 -+ inclination, 180 - inclination). (vertex, farther edge end point, foot point V + d * 0.05 on the line, [(V, end)])
    like _slant_reference, or None (no world axis in the image plane, edge parallel to an axis, angle value does not match, e.g. doubly inclined). The angle value first: equal to the inclination, acute before obtuse, only on a
    tie the direction into the open (line end outside the bounding box). In a detail view the line end must lie in the circle, otherwise no reference line arises there."""
    N = w['fr'][0]
    B = B - ((B - A) @ N) * N
    L = float(np.linalg.norm(B - A))
    if L < 1e-9:
        return None
    a_deg = float(f.get('angle_deg') or 0.0)
    allowed = (a_deg, 180.0 - a_deg, 90.0 - a_deg, 90.0 + a_deg)
    best = None
    for n, (V, far) in enumerate(((A, B), (B, A))):
        t = (far - V) / L
        for j in range(3):
            e = np.eye(3)[j]
            if abs(float(e @ N)) > 1e-6:
                continue
            for sg in (1.0, -1.0):
                d = sg * e
                theta = float(np.degrees(np.arccos(np.clip(t @ d, -1.0, 1.0))))
                if theta < 1.0 or theta > 179.0 or abs(theta - 90.0) < 1.0 or not any(abs(theta - x) <= 0.1 for x in allowed):      # perpendicular to the edge: no angle dimension (flat face, inclination 0)
                    continue
                end = V + d * REFLINE_FRAC * L
                if not _inside(w, end):                                  # detail view: the line stays in the circle (otherwise it leaves the visible image)
                    continue
                inside = all(bb['min'][i] - TOL <= end[i] <= bb['max'][i] + TOL for i in range(3))
                key = (abs(theta - a_deg) > 0.2, theta > 90.0, inside, n, j)
                if best is None or key < best[0]:
                    best = (key, V, far, V + d * REFLINE_FOOT, [(V, end)])
    return None if best is None else best[1:]


def _slant_options(f, ws, feats, bb):
    """All dimensioning options of a slant (N5.3: length along the slant and angle): [(rank, view, annotations, shown)]. Rank 0 = angle against a real body edge,
    rank 1 = against the thin reference line (_slant_refline), only where no body edge reference exists. All views are searched (not only the first suitable one). Annotations: [angle, length, thin
    lines]. Empty if no view shows the edge with a reference."""
    out = []
    for w, A, B, length, shown in _slant_views(f, ws):
        ref = _slant_reference(f, w, A, B, feats)
        rank = 0
        if ref is None:
            ref, rank = _slant_refline(f, w, A, B, bb), 1
        if ref is None:
            continue
        V, O, P2, ext = ref
        anns = [{'type': 'angle', 'view': w['id'], 'vertex': _r(V), 'p1': _r(O), 'p2': _r(P2), 'radius': SLANT_ARC_MM, 'arc': 'free', 'feature': f['id']}, length]
        anns += [{'type': 'polyline', 'view': w['id'], 'points': [_r(p), _r(q)], 'style': 'thin', 'role': SLANT_REF, 'feature': f['id']} for p, q in ext]
        out.append((rank, w, anns, shown))
    return out


def _slant_pick(opts):
    """The preferred option: a view that shows the slant visibly before one that does not; then body edge before reference line; then view order."""
    return min(opts, key=lambda o: (not o[3], o[0]))


def _slant_legs(f, ws, bb, feats):
    """Last resort without any angle reference (no view with a world axis in the image plane, auxiliary views): both extents of the edge in the image plane, no angle. An edge parallel to an axis that
    appears like this in one view only (held) gets its length and its position relative to the envelope face across it. -> annotations or []."""
    held = None
    for w, A, B, length, _ in _slant_views(f, ws):
        legs = [{'type': 'linear', 'view': w['id'], 'p1': _r(A), 'p2': _r(B), 'axis': ax, 'side': sd, 'feature': f['id']}
                for ax, sd, d in (('h', 'above', w['fr'][1]), ('v', 'left', w['fr'][2])) if abs(float((B - A) @ d)) > 0.5]
        if len(legs) >= 2:
            return legs
        if legs and held is None:                                       # edge parallel to an axis: first try the other views (appear inclined)
            held = (w, A, B, length, legs)
    if held is not None:
        # only in this view the edge appears axis-parallel: second, different dimension = position of the edge relative to the envelope face across it
        w, A, B, length, legs = held
        ed = (B - A) / (np.linalg.norm(B - A) or 1.0)
        for d in (w['fr'][1], w['fr'][2]):
            j = int(np.argmax(np.abs(d)))
            if abs(float(d @ ed)) < 1e-6 and abs(abs(d[j]) - 1) < 1e-6:
                c = float(bb['min'][j]) if abs(A[j] - bb['min'][j]) <= abs(A[j] - bb['max'][j]) else float(bb['max'][j])
                got = _place([w], [(np.eye(3)[j], _edge_anchors(j, c, A, feats, bb), [(f['id'], A, None)])], only_strict=True)
                if got is not None and abs(float((_v(got[3]) - _v(got[2])) @ d)) > 0.01:
                    return legs + [{'type': 'linear', 'view': w['id'], 'p1': _r(got[2]), 'p2': _r(got[3]), 'axis': got[1],
                                    'side': {'h': 'above'}.get(got[1], 'left'), 'feature': f['id']}]
        return legs + [length]
    return []


def _slant_dims(f, ws, feats, bb):
    """Angle dimension and edge length in true length of a slanted face (N5.3) in the preferred view (_slant_options, _slant_pick): angle against a body edge where any view
    shows one, otherwise against the thin reference line. Only without any angle reference (_slant_legs) do the two extents stand (last resort). -> annotations or []."""
    opts = _slant_options(f, ws, feats, bb)
    return _slant_pick(opts)[2] if opts else _slant_legs(f, ws, bb, feats)


def _rim_proven(f, feats, q, end, e):
    """Does the outline point q at cone end end (0 narrow, 1 wide) provably lie on the drawn edge of the cone face? The edge coverage
    (features: rim) carries the circumferential direction of q, or a plane across the axis at this end (position dimension of the bounding plane) encloses q
    in its face bbox. A tip (radius 0) counts as an edge."""
    apex = _v(f['apex'])
    d = q - apex - float((q - apex) @ e) * e
    if float(np.linalg.norm(d)) < 1e-6:
        return True
    rim = f.get('rim')
    if rim:
        a = float(np.degrees(np.arctan2(d @ _v(rim['y']), d @ _v(rim['x'])))) % 360.0
        n = int(360 / RIM_BIN)
        cov = set(rim['c1' if end else 'c0'])
        b = int(a // RIM_BIN) % n
        if b in cov or ((b - 1) % n in cov and (b + 1) % n in cov):
            return True
    c = float(q @ e)
    for g in feats:
        if g['kind'] == 'plane' and g.get('bbox') and abs(abs(float(_v(g['normal']) @ e)) - 1) < 1e-6 and abs(float(_v(g['pt']) @ e) - c) <= 0.02:
            lo, hi = g['bbox']['min'], g['bbox']['max']
            if all(lo[j] - TOL <= q[j] <= hi[j] + TOL for j in range(3)):
                return True
    return False


ZONE_GAP, ZONE_STEP = 10.0, 7.0       # mm on paper: first dimension line next to the contour, spacing of further ones (stacking rule of the core 10 + 7 · n)


def _zone_offset(w, c, e, anns, bb, k):
    """Position (offset, paper mm) of the dimension line of a diameter whose axis e lies in the image plane of a section (cone, trapezoid): the line stands
    in the dimension zone next to the contour (on the nearer side of the part extent along the axis, stacked at distance 10 + 7 · n from diameter lines already
    there), not at the cone edge in the middle of the section. None without paper scale k, outside sections and with the axis across the image plane."""
    N, Vx, Vy = w['fr']
    if not k or not w['section'] or abs(float(e @ N)) > 1e-3:
        return None
    lo, hi = (bb['min'], bb['max'])
    ts = [float(np.array([x, y, z]) @ e) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    tc = float(c @ e)
    side = 1.0 if max(ts) - tc <= tc - min(ts) else -1.0                # nearer edge along +e or -e
    kv = w['k'] or k
    u = prj.unit(np.cross(N, e))
    n2 = np.array([-float(u @ Vy), float(u @ Vx)])                        # left normal to the dimension direction (offset > 0 lies there)
    sgn = 1.0 if float(n2 @ np.array([float(e @ Vx), float(e @ Vy)])) * side >= 0 else -1.0
    taken = sum(1 for a in anns if a.get('type') == 'diameter' and a.get('view') == w['id'] and a.get('offset') is not None and a['offset'] * sgn > 0)
    return round(sgn * ((max(ts) - tc if side > 0 else tc - min(ts)) * kv + ZONE_GAP + ZONE_STEP * taken), 2)


def _cone_dims(f, ws, items, bb, feats=(), k=None):
    """Dimensions of a shaft cone in the trapezoid (axis in the image plane; an inner cone - hole host or free-standing - only in the section,
    where both generatrices are drawn): each diameter of its two ends that is not already dimensioned as the diameter of a coaxial cylinder, and the
    cone length along the axis between the outline points of the ends (for an oblique axis as projection on the axis direction).
    -> [(annotation, value)]; diameters without a view are missing (the coverage reports it)."""
    apex, e = _v(f['apex']), prj.unit(f['axis'])
    t = np.tan(np.radians(float(f['half_angle_deg'])))
    r0, r1 = float(f['d1']) / 2, float(f['d2']) / 2
    P0, P1 = apex + e * r0 / t, apex + e * r1 / t
    internal = bool(f.get('free') or f.get('host_hole'))
    secs = [w for w in ws if w['section'] and f['id'] in w['shows']]
    pool = secs if internal and secs else ws
    out = []
    covered = lambda r: any(a['type'] == 'diameter' and a.get('radius') is not None and abs(a['radius'] - r) <= 0.005
                            and float(np.linalg.norm(np.cross(_v(a['center']) - apex, e))) <= 0.05 for _, a, _, _ in items)
    ends = []
    head = bool(f.get('head'))       # cone head - Ø at the wide end and cone angle (apex angle), the narrow end carries the shaft Ø, no length
    for r, P, end in ((r1, P1, 1), (r0, P0, 0)):
        if r <= 1e-6 or covered(r) or (head and end == 0):
            continue
        shown = [w for w in pool if f['id'] in w['shows']]
        got = (_size_view(shown, [(f, [P])], e, False) if shown else None) or _size_view(ws, [(f, [P])], e, False)
        if not got:
            continue
        wd = prj.unit(np.cross(got[0]['fr'][0], e))
        sides = ((bool(_rim_proven(f, feats, _v(got[2]) + wd * r, end, e)), bool(_rim_proven(f, feats, _v(got[2]) - wd * r, end, e)))
                 if f.get('rim') and abs(float(e @ got[0]['fr'][0])) < 1e-3 else (True, True))
        ends.append((r, got, sides))
    ends0 = list(ends)      # edge evidence before the fallback (it feigns them so that no cone remains without a dimension)
    if not any(p or m for _, _, (p, m) in ends):      # no end proven: full Ø as before (never a cone without a dimension)
        inb = lambda q: all(bb['min'][j] - TOL <= q[j] <= bb['max'][j] + TOL for j in range(3))      # fallback: only the side in the part (the rest of the line would hang in the void)
        ends = [(r, got, tuple(inb(_v(got[2]) + sg * prj.unit(np.cross(got[0]['fr'][0], e)) * r) for sg in (1, -1))) for r, got, _ in ends]
        if not any(p or m for _, _, (p, m) in ends):
            ends = [(r, got, (True, True)) for r, got, _ in ends]
    n_out, nest_skipped = checks.axis_on_outline(f, feats), False
    if n_out is not None:       # the axis lies in a planar face of the part (half nest): no Ø (its half dimension line would lie on the outline edge), instead the depth from this face
        tip = lambda r, got: _v(got[2]) - n_out * r      # edge point of the end: depth R from the face into the material
        nest = ([(r, got, s) for r, got, s in ends0 if _rim_proven(f, feats, tip(r, got), 1 if abs(r - r1) < 1e-9 else 0, e)]      # only ends with a proven rim; if none is proven, the ends whose edge point lies in the part (never a dimension into the air, e.g. R 28.81 > block height 25.61); otherwise all (never a cone without a dimension)
                or [(r, got, s) for r, got, s in ends0 if all(bb['min'][j] - TOL <= tip(r, got)[j] <= bb['max'][j] + TOL for j in range(3))] or ends)
        nest_skipped = len(nest) < len(ends)
        for r, got, _ in nest:     # per end: from the axis (face) perpendicular into the material up to the edge point of the Ø = R of the end (ISO 129-1: depth/R instead of half Ø)
            o = _orient(got[0], n_out)
            if o in ('h', 'v'):
                P = _v(got[2])
                out.append(({'type': 'linear', 'view': got[0]['id'], 'p1': _r(P), 'p2': _r(P - n_out * r), 'axis': o, 'side': 'above' if o == 'h' else 'left',
                             'feature': f['id']}, round(r, 4)))
        ends = []
    skipped, side = n_out is not None and nest_skipped, 0
    for r, got, (plus, minus) in ends:
        if not (plus or minus):                      # no shell point proven at the end: no diameter for this end (diameter + cone angle cover)
            skipped = True
            continue
        a = {'type': 'diameter', 'view': got[0]['id'], 'center': _r(got[2]), 'radius': r, 'axis': _r(e), 'feature': f['id']}
        if plus != minus:                            # exactly one shell point proven: diameter with half dimension line at the real point (half = side)
            a['half'] = 1 if plus else -1
        side = side or (1 if plus else -1)
        off = _zone_offset(got[0], _v(got[2]), e, [x for _, x, _, _ in items] + [x for x, _ in out], bb, k)
        if off is not None:
            a['offset'] = off
        out.append((a, 2 * r))
    if head and out:                                 # apex angle between both generatrices, at the intersection with the axis (apex); Ø and angle cover without length (checks.cone_ok)
        sm = (float(f['d1']) + float(f['d2'])) / 4 / t
        # the angle arc does not stand in the view of the Ø (there arc, Ø line and both texts overlapped), but in another view that shows the cone with the axis in the image plane
        alt = next((x for x in ws if x['id'] != out[0][0]['view'] and f['id'] in x['shows'] and abs(float(e @ x['fr'][0])) < 1e-3 and not x['aux'] and x['det'] is None
                    and _inside(x, apex + e * sm)), None)
        w = alt['id'] if alt is not None else out[0][0]['view']
        wd = prj.unit(np.cross(next(x for x in ws if x['id'] == w)['fr'][0], e))
        out.append(({'type': 'angle', 'view': w, 'vertex': _r(apex), 'p1': _r(apex + e * sm - wd * sm * t), 'p2': _r(apex + e * sm + wd * sm * t),
                     'radius': SLANT_ARC_MM, 'arc': 'free', 'feature': f['id']}, None))
        return out
    def cone_angle():                                # cone angle (half angle to the axis) in the same view, at the intersection of the generatrix with the axis (apex)
        w = out[0][0]['view'] if out else next((x['id'] for v in (pool, ws) for x in v if f['id'] in x['shows'] and abs(float(e @ x['fr'][0])) < 1e-3), None)      # without a Ø of its own (a coaxial shaft carries it): a view with the axis in the image plane
        if w is None:
            return
        sm = (float(f['d1']) + float(f['d2'])) / 4 / t
        wd = prj.unit(np.cross(next(x for x in ws if x['id'] == w)['fr'][0], e))
        out.append(({'type': 'angle', 'view': w, 'vertex': _r(apex), 'p1': _r(apex + e * sm), 'p2': _r(apex + e * sm + (side or 1) * wd * sm * t),
                     'radius': SLANT_ARC_MM, 'arc': 'free', 'feature': f['id']}, None))
    if skipped and out:
        cone_angle()
    unproven = False                                 # case “no rim”: without proven generatrix points at both ends there is no depth dimension
    for strict, views in ((True, pool), (False, pool)) + (() if internal and secs else ((True, ws), (False, ws))):      # internal cone with axial section: only there
        for w in views:
            N, Vx, Vy = w['fr']
            if (strict and f['id'] not in w['shows']) or abs(float(e @ N)) > 1e-3:
                continue
            o = _orient(w, e)
            if o is None:
                continue
            u = prj.unit(np.cross(N, e))                # outline side top or left, if material lies there (otherwise the other one): both generatrices are drawn
            u = u if (u @ Vy >= 0 if o != 'v' else u @ Vx <= 0) else -u
            proven = [(a_, b_) for a_, b_ in ((P0 + u * r0, P1 + u * r1), (P0 - u * r0, P1 - u * r1))
                      if _rim_proven(f, feats, a_, 0, e) and _rim_proven(f, feats, b_, 1, e)]
            if proven:                                  # (1) both outline points provably lie on drawn edges of the cone face
                q0, q1 = proven[0]
            else:                                       # no evidence at one end: neither depth dimension nor Ø there (Ø + cone angle cover, cone_ok); formerly points near the axis in the part
                unproven = True
                continue
            if _inside(w, q0) and _inside(w, q1):
                a = {'type': 'linear', 'view': w['id'], 'p1': _r(q0), 'p2': _r(q1), 'axis': o if o != 'aligned' else 'along',
                     'side': {'h': 'above'}.get(o, 'left'), 'feature': f['id']}
                if o == 'aligned':
                    a['dir'] = [round(float(x), 6) + 0.0 for x in e]
                return out + [(a, round(float(f['length']), 4))]
    if unproven and not skipped and not any(a['type'] == 'angle' for a, _ in out):      # no length possible: the cone angle replaces it (cone_ok: Ø + angle; the Ø may be carried by the coaxial shaft)
        cone_angle()
    return out


# ------------------------------------------------------------------ Dimensioning of a part
LABEL_VIEW_GAIN = 1.0      # mm on paper: another view must separate the points this much better to replace the first
WIRE_STRAIGHT_MIN = 3.0    # mm: shorter straight sections do not carry the wire diameter


def label_spread(points, view, k=1.0):
    """Smallest distance of the projected bend points in the view view (dict with preset/direction, optional 'scale') on the
    paper in mm at sheet scale k; points on the same image point: 0."""
    sc = an.scale_value(view['scale']) if view.get('scale') else k
    q = prj.project(np.asarray(points, float), prj.frame_of(view)) * sc
    return min((float(np.linalg.norm(q[i] - q[j])) for i in range(len(q)) for j in range(i + 1, len(q))), default=float('inf'))


def bend_label_view(bt, views, pid, k=1.0):
    """View for the point labels and the axis cross of a bend table: among the overall views of the part (no sections,
    detail views, auxiliary views; isometric too) the one with the largest smallest distance of the projected points; the earliest in
    the plan order wins as long as a later one does not separate better by LABEL_VIEW_GAIN mm. None without a view."""
    pts = [p['xyz'] for p in bt['points']]
    best, best_s = None, -1.0
    for v in views:
        if v.get('part') != pid or v.get('section') or v.get('detail') or v.get('auxiliary'):
            continue
        sp = label_spread(pts, v, k)
        if best is None or sp > best_s + LABEL_VIEW_GAIN:
            best, best_s = v['id'], sp
    return best


VIEW_NAMES = {'front': 'Front view', 'back': 'Rear view', 'left': 'Left side view', 'right': 'Right side view',
              'top': 'Top view', 'bottom': 'Bottom view'}      # as drawing.Drawing.NAMES


def view_title(v):
    """Name of a view on the sheet as the core gives it in the reference line of a bend table (Drawing.view_name):
    normal view - its name by viewing direction, isometric or oblique viewing direction - "ISO"; sections, detail views and auxiliary views
    already carry their title ("A-A", "Detail B", "View C")."""
    if v.get('preset') in VIEW_NAMES:
        return VIEW_NAMES[v['preset']]
    N = prj.frame_of(v)[0]
    if float(np.abs(N).max()) < 1 - 1e-9:
        return 'ISO'
    return VIEW_NAMES[max(VIEW_NAMES, key=lambda p: float(prj.unit(prj.PRESETS[p][0]) @ N))]


def label_bend_views(views, tables):
    """Views with the title (label) of their name when a bend table refers to their axis cross (labels_in): the table
    names the view, so the name must appear on the sheet. Copies where something changes; views with a title stay."""
    ids = {t['labels_in'] for t in tables if t.get('bend_table') and t.get('labels_in')}
    return [{**v, 'label': view_title(v)} if v['id'] in ids and not v.get('label') else v for v in views]


def wire_diameter(part, ws, fid):
    """Diameter dimension of the wire at the longest straight section (cylinder with the wire diameter, at least WIRE_STRAIGHT_MIN long) in
    the view in which its axis lies best in the image plane (plan order on a tie); tag = bend table. None without such a
    section or view."""
    bt = next((b for b in part.get('bend_tables') or [] if b.get('complete')), None)
    if not bt or not ws:
        return None
    d = float(bt['wire_diameter'])
    cyl = [c for c in part.get('shafts') or [] if abs(c['diameter'] - d) <= 1e-3 and c['depth'] >= WIRE_STRAIGHT_MIN]
    if not cyl:
        return None
    c = max(cyl, key=lambda c: (c['depth'], c['start']))
    A = np.asarray(c['axis'], float)
    cands = [w for w in ws if not w['section'] and w['det'] is None and not w['aux']]
    w = min(cands, key=lambda w: (round(abs(float(w['fr'][0] @ A)), 3), cands.index(w)), default=None)
    if w is None or abs(float(w['fr'][0] @ A)) > 0.5:
        return None
    mid = (np.asarray(c['start'], float) + np.asarray(c['end'], float)) / 2
    return {'type': 'diameter', 'view': w['id'], 'center': _r(mid), 'radius': d / 2, 'axis': _r(A), 'offset': 8.0, 'feature': fid}


SEQ_ARC_MM = (14.0, 9.0)       # radius of the angle arcs (pitch, start angle) on paper
SEQ_MAX_SPAN = 175.0           # degrees: an angle arc does not show larger total angles (smaller angle between the rays): then the pitch stands between the first two grid places


def _sequence_view(ws, p, by_id):
    """View for the sequence p: viewing direction ∥ axis of rotation (the axis appears as a point, the pitch in true angle), showing the most members visibly (on a tie
    the plan order: larger own scale first); no detail view. In addition the axis of rotation must be a world axis if the sequence is helical (the axial offset stands in
    a view in which the axis lies in the image plane). None: no such view (then the single dimensions of the members apply)."""
    A = _v(p['polar']['axis'])
    best = None
    for n, w in enumerate(ws):
        if w['det'] is not None or abs(float(w['fr'][0] @ A)) < 1 - 1e-6:
            continue
        shown = sum(1 for m in p['members'] if m in w['shows'])
        if best is None or shown > best[0]:
            best = (shown, w)
    if best is None or (p['mode'] == 'helical' and _world(A) is None):
        return None
    return best[1]


def _sequence_dims(w, ws, p, by_id, feats=()):
    """(ISO 129-1, equally distributed elements): the dimensions of a polar/helical sequence in the view w along the axis of rotation, all with the tag of the sequence: pitch “k× s° (=total°)” (full circle
    or total angle > SEQ_MAX_SPAN: “n× s°” between the first two grid places), pitch circle radius “R…” from the axis of rotation to the first member (start or end of its axis, where all members have the same distance), start angle of the first grid place to the
    reference direction (world axis, only if not 0) and for a helical sequence the axial offset first-last member in a view with the axis in the image plane. If there is no view for the axial offset,
    the dimension is missing (the check then reports the sequence as uncovered). Return [(annotation, value)]."""
    po = p['polar']
    A, R0, C0 = _v(po['axis']), _v(po['ref']), _v(po['center'])
    step, r, steps = float(po['angle']), float(po['radius']), int(po['steps'])
    first, last = by_id[po['first']], by_id[po['last']]
    P1, P2 = _v(first['start']), _v(last['start'])
    c1 = C0 + float((P1 - C0) @ A) * A                 # point on the axis of rotation at the height of the first member
    out = []

    def flat(q):                                        # ray in the plane of the pitch circle (height of the first member): the angle then lies exactly in the image plane (viewing direction ∥ A)
        u = q - c1
        return c1 + u - float(u @ A) * A

    def rot(q, deg):
        t, u = np.radians(deg), flat(q) - c1
        return c1 + u * np.cos(t) + np.cross(A, u) * np.sin(t)
    total = steps * step
    if not po['full'] and total <= SEQ_MAX_SPAN:
        out.append(({'type': 'angle', 'view': w['id'], 'vertex': _r(c1), 'p1': _r(flat(P1)), 'p2': _r(flat(P2)), 'radius': SEQ_ARC_MM[0], 'arc': 'free', 'feature': p['id'],
                     'prefix': f"{steps}× {fmt(step, 2)}° (=", 'suffix': ')'}, None))
    else:
        n = steps + 1 if po['full'] else steps
        out.append(({'type': 'angle', 'view': w['id'], 'vertex': _r(c1), 'p1': _r(flat(P1)), 'p2': _r(rot(P1, step)), 'radius': SEQ_ARC_MM[0], 'arc': 'free', 'feature': p['id'],
                     'prefix': f'{n}× ', 'decimals': 2}, None))
    Pr = _v(first['end' if po.get('radius_at') == 'end' else 'start'])          # the point of the axis whose distance to the axis of rotation is equal for all members (features._sequence)
    if checks.sequence_arc(feats, po) is None:         # the pitch circle is already dimensioned as the R of an equivalent arc (R dimension of the rounding): no second R
        out.append(({'type': 'linear', 'view': w['id'], 'p1': _r(c1), 'p2': _r(Pr), 'axis': 'aligned', 'side': 'left', 'feature': p['id'], 'prefix': 'R'}, round(r, 4)))
    if min(float(po['start']), 360.0 - float(po['start'])) > 0.05:      # start angle from the reference ray (centre line of the view) to the first grid place; the ray ends on the largest coaxial circle (drawn edge)
        cyl = [float(f['diameter']) / 2 for f in feats if f['kind'] in ('hole', 'shaft') and f.get('axis') is not None and abs(float(_v(f['axis']) @ A)) > 1 - 1e-6
               and float(np.linalg.norm(np.cross(_v(f['start']) - c1, A))) <= 0.05 and f['diameter'] / 2 <= r]
        out.append(({'type': 'angle', 'view': w['id'], 'vertex': _r(c1), 'p1': _r(c1 + R0 * max(cyl, default=r)), 'p2': _r(flat(P1)), 'radius': SEQ_ARC_MM[1], 'arc': 'free', 'feature': p['id']}, None))
    if p['mode'] == 'helical':
        k = _world(A)
        got = _place(ws, [(np.eye(3)[k], [(first['id'], P1, None)], [(last['id'], P2, None)])]) if k is not None else None
        if got is not None:
            w2, o, q1, q2, _ = got
            a = {'type': 'linear', 'view': w2['id'], 'p1': _r(q1), 'p2': _r(q2), 'axis': o, 'side': {'h': 'below', 'v': 'right'}.get(o, 'left'), 'feature': p['id'],
                 'prefix': f"{steps}× {fmt(abs(float(po['rise'])), 2)} (=", 'suffix': ')'}      # “26× 0.15 (=3.9)”: offset per step, steps, sum
            out.append((a, _linear_value(a, ws)))
    return out


def _leader_out(w, c, bb, k=None):
    """(angle in degrees, length in mm on paper) of a leader line from the circle around c (axis towards the camera) such that the text stands behind the bounding box of the part in the view w
    (not on the wall edge and hatching): among 8 directions the one with the shortest way out of the bounding box, plus 5 mm clearance."""
    N, Vx, Vy = (np.asarray(x, float) for x in w['fr'])
    c = np.asarray(c, float)
    corners = [np.array([x, y, z]) for x in (bb['min'][0], bb['max'][0]) for y in (bb['min'][1], bb['max'][1]) for z in (bb['min'][2], bb['max'][2])]
    uv = np.array([[float((q - c) @ Vx), float((q - c) @ Vy)] for q in corners])
    lo, hi = uv.min(axis=0), uv.max(axis=0)
    best = None
    for ang in (45, 135, 0, 90, 180, 225, 315, 270):
        d = np.array([np.cos(np.radians(ang)), np.sin(np.radians(ang))])
        t = min((hi[i] if d[i] > 1e-9 else lo[i]) / d[i] for i in (0, 1) if abs(d[i]) > 1e-9)
        t = max(t, 0.0)
        if best is None or t < best[0] - 1e-9:
            best = (t, ang)
    return best[1], round(best[0] * (k or 1.0) + 5.0, 1)


def _gap_text(missing, start, step):
    """Empty grid places (index from 0) as angles to the same reference ray as the start angle (start, degrees), per contiguous group “40°–50°” or “45°”."""
    runs, first = [], None
    for m in missing:
        if first is None:
            first = prev = m
        elif m == prev + 1:
            prev = m
        else:
            runs.append((first, prev)); first = prev = m
    if first is not None:
        runs.append((first, prev))
    ang = lambda k: fmt((start + k * step) % 360.0, 1) + '°'
    return ', '.join(ang(a_) if a_ == b_ else f'{ang(a_)}–{ang(b_)}' for a_, b_ in runs)


def _axis_view_ok(w, k, bb, block_k=None, fid=None):
    """Does symmetry.add_axes show the symmetry line of the plane x_k = m in this view (and does checks.symmetry_axis_shown count it)? Yes if the plane appears as a line (viewing direction perpendicular
    to the plane normal), the view is not a detail view and the line, computed at least over the bounding box, becomes MIN_AXIS_MM long on paper (no stub). The mirror closure
    omits a mirror dimension only where this answer is yes: the axis is generated there according to the plan (variant “force axis” of the final check)."""
    if w['det'] is not None:       # a pattern detail carries the mirror dimensions of its view (checks.symmetry_axis_shown counts the axis of this view), otherwise a detail view never shows the axis
        if fid is not None and not checks.PATTERN_ID_RX.search(str(fid)):
            return False           # per dimension: only an edge that itself carries a plane series (fid = tag of the edge); without fid (axis choice per part) as before
        return w.get('pat_host') is not None and w['pat_host'] is not w and _axis_view_ok(w['pat_host'], k, bb, block_k)
    N = np.asarray(w['fr'][0], float)
    if abs(float(N[k])) > 1e-6:
        return False
    d = np.cross(N, np.eye(3)[k])
    extent = float(np.abs(d) @ np.asarray(bb['size'], float))
    return extent * float(w.get('k') or block_k or 1.0) >= MIN_AXIS_MM - 1e-9


def derivable_left(items, ws, bb, nums, mirrors, by_id=None):
    """N5.2 as a message (nothing deleted): number of length dimensions per world axis that the rank test (chains.derivable, the same input as checks._chain_findings: checks.chain_measure,
    checks.chain_mirrors) still finds derivable from others in the finished dimensions. items (key, annotation, value, kind) as in dimension; mirrors {axis: plane}: only axes at which the
    symmetry line stands in the view (mirror rows). -> {axis: count} (only axes with dimensions)."""
    import chains
    wby = {w['id']: w for w in ws}
    per = {}
    for _, a, _, _ in items:
        w = wby.get(a.get('view'))
        if a['type'] != 'linear' or w is None or a.get('axis') not in ('h', 'v') or a.get('p1') is None or a.get('p2') is None:
            continue
        k = _world(_meas_dir(w, a['axis']))
        if k is None:
            continue
        lo, hi = sorted((float(a['p1'][k]), float(a['p2'][k])))
        if hi - lo > TOL:
            per.setdefault(k, []).append((a, lo, hi))
    out = {}
    for k, rows in per.items():
        measures, coords = [], []
        for n, (a, lo, hi) in enumerate(rows):
            text = f"{a.get('prefix') or ''}{a.get('text') or ''}"
            mm, cs = checks.chain_measure(n, text, lo, hi, bb, k, nums, (by_id or {}).get(a.get('feature')), by_id or {})
            mm['locked'] = mm['locked'] or bool(a.get('tolerance'))      # toleranced dimensions stay (like checks._toleranced on the text)
            measures.append(mm)
            coords += cs
        mir = checks.chain_mirrors(coords, float(mirrors[k])) if k in mirrors else []
        out[k] = len(chains.derivable(measures, mirrors=mir, twins=checks.chain_twins((by_id or {}).values(), k))['derivable'])
    return out


def dimension(part, cls, feats, views, datum, tolerances, model_ref, texts=(), report=None, ray=None, k=None, sym=None):
    """(annotations with 'feature', note lines, tables without 'at') of a part; rules in the module docstring. texts = model,
    part and form feature names (function dimensions); report (list) takes one entry per recognized text value; ray(p, N, n) -> bool (freely visible?)
    is the ray test of the part body for dimension anchors on faces (plan._ray), None: only the check for axis-parallel planes (_covered). k = scale
    of the part block (diameter dimensions of cones in the section stand in the dimension zone next to the outline, the position needs the paper scale); None: without.
    sym {world axis: coordinate}: symmetry planes of the whole part (the same detection as the symmetry lines, symmetry.body_planes, without hole rows): the baseline tree closes there
    to mirror images (_Tree(m)), a mirror dimension stands only once (the datum-near side), and only in views in which the symmetry line arises (_axis_view_ok)."""
    pid, bb = part['id'], part['bbox']
    block_k = k                                     # (k is reused below as an axis index)
    if cls == 'standard_part':
        return [], [], []
    unbuilt = {i for v in views for i in v.get('profile_unbuilt') or []}
    held_p = lambda f: bool(f.get('profile')) and f['profile'] not in unbuilt      # residual member of a profile with a buildable view (otherwise it stays in the row “Undimensioned forms”)
    ws = _views([v for v in views if not v.get('profile_own')], pid)      # the profile view (detail/section of the profile) carries only the profile dimensions (profile_annotations), no general dimensions
    by_id = {f['id']: f for f in feats}
    order = {w['id']: n for n, w in enumerate(ws)}
    items, notes, tables = [], [], []          # items: (sort key, annotation, value, kind)

    def add(a, value=None, art=None):
        key = (0, order[a['view']], a['axis'], a['side'], value) if a['type'] == 'linear' else (1, len(items))
        items.append((key, a, value, art))

    slants = [f for f in feats if f['kind']=='slant' and not f.get('via_member') and not f.get('minor')]
    congruent, station_ids, station_links = {}, set(), defaultdict(list)
    for f in slants:
        congruent.setdefault(checks.equal_key(f) or f['id'], []).append(f)
    for group in congruent.values():
        if len(group) != 2:
            continue
        stations = _mouth_widths(*group, ws, bb, ray)
        if stations:
            station_ids.update(f['id'] for f in group)
        for a in stations:
            add(a, _linear_value(a, ws))
            w = next(w for w in ws if w['id'] == a['view'])
            k = _world(_meas_dir(w, a['axis']))
            shown_axis = k in {int(x) for x in (sym or {})} and _axis_view_ok(w, k, bb, block_k, a.get('feature') or '')      # the station dimension also closes to mirror images if its view shows the symmetry line
            station_links[k].append((a['p1'][k], a['p2'][k], shown_axis))
            if a.get('opening_axis'):
                station_links[k].append((a['p1'][k], (a['p1'][k] + a['p2'][k])/2, shown_axis))

    if any(f['kind'] == 'bend_table' for f in feats):
        bt = next((b for b in part.get('bend_tables') or [] if b.get('points')), None)
        first = bend_label_view(bt, views, pid) if bt else next((v['id'] for v in views if v.get('part') == pid), None)
        t = {'bend_table': pid, **({'labels_in': first} if first else {})}
        if bt:
            t['origin'] = _r(bt['points'][0]['xyz'])
        tables.append(t)
        notes.append(BEND_NOTE)
        if cls == 'wire_bend':                 # bent part: bend table (with axis cross) and wire diameter on a straight section
            ann = wire_diameter(part, ws, next(f['id'] for f in feats if f['kind'] == 'bend_table'))
            return ([ann] if ann else []), notes, tables

    freeform = bool(part.get('freeform'))
    pats = [p for p in feats if p['kind'] == 'pattern' and p.get('members') and all(m in by_id for m in p['members'])]
    seq_view = {p['id']: _sequence_view(ws, p, by_id) for p in pats if p.get('polar')}      # sequences only with a view along the axis of rotation, otherwise single dimensions of the members (previous placement rule)
    pats = [p for p in pats if not p.get('polar') or seq_view[p['id']] is not None]
    rest = {m for p in pats for m in p['members'][1:]}
    first_of = {p['members'][0]: p for p in pats}
    threads = [t for t in feats if t['kind'] == 'thread']
    threaded = {t['target'] for t in threads if t.get('target') in by_id}
    turned = None if freeform else checks.part_axis(feats)       # common axis of rotation as datum, no position across it
    edges = {k: [] for k in range(3)}          # primary distances per axis
    sym_planes = {int(k_): float(m_) for k_, m_ in (sym or {}).items()}      # (sym is reused below as a marker of the position symmetry)
    loose = []                                 # pitches oblique to the world axes
    sym_skip = set()                           # (member id, axis): position by symmetry to the median plane, no baseline node

    def visible_place(k, c1, c2, a1, a2, fid=None):
        """Placement of an envelope dimension (bounding box, freeform region) only in a view in which both ends carry visible edges: points not in the cut-away
        part of a section (in front of the cutting plane) and not behind the part (plane/ray test). None if no view carries both ends (then plan._content creates a profile view)."""
        d = np.eye(3)[k]
        for w in ws:
            if w['aux'] or w['det'] is not None:
                continue
            o = _orient(w, d)
            if o not in ('h', 'v'):
                continue
            p1, p2 = _pick(w, a1, False, d), _pick(w, a2, False, d)
            if p1 is None or p2 is None:
                continue
            if w.get('sect') is not None and any(float((p - np.asarray(w['sect'], float)) @ np.asarray(w['fr'][0], float)) > CUT_TOL for p in (p1, p2)):
                continue
            a = {'type': 'linear', 'view': w['id'], 'p1': _r(p1), 'p2': _r(p2), 'axis': o, **({'feature': fid} if fid else {})}      # with a form feature: ray test of the foot points on faces as in _visibility
            if not _hidden(a, w, feats, ray, by_id):
                return w, o, p1, p2, 0
        return None

    def edge(k, c1, c2, a1, a2, fid, chain, art=None, d=None, placed=None, first=None, link=None, hull=False, join=None, prio=False, **extra):
        """first: coordinate (tuple: the one nearest the datum) that gets its baseline dimension before this distance (opening of a
        depth, first pattern member, wall of an element); link: if first is already attached to the datum, measures first from there instead."""
        return {'k': k, 'c': (c1, c2), 'a': (a1, a2), 'fid': fid, 'chain': chain, 'art': art,
                'd': np.eye(3)[k] if d is None else d, 'placed': placed, 'first': first, 'link': link, 'extra': extra, 'hull': hull, 'join': join, 'prio': prio}

    # square: dimension between the side planes across the square axis, in the first view that shows one of the transverse directions
    for s in (f for f in feats if f['kind'] == 'square'):
        A, c, h = _v(s['axis']), _v(s['center']), s['size'] / 2
        opts, ks = [], [k for k in range(3) if abs(A[k]) < 1e-6]
        for k in ks if len(ks) == 2 else []:
            ends = []
            for sg in (-1, 1):
                q = c.copy()
                q[k] += sg * h
                pl = [(f['id'], _v(f['pt']), f) for f in feats if f['kind'] == 'plane' and f.get('square') == s['id']
                      and f['axis_name'] == AX[k] and abs(f['coord'] - q[k]) <= TOL]
                ends.append(pl + [(s['id'], q, None)])
            opts.append((np.eye(3)[k], *ends))
        got = _place(ws, opts)
        if got:
            k = ks[got[4]]
            edges[k].append(edge(k, c[k] - h, c[k] + h, *opts[got[4]][1:], s['id'], True, 'ext', placed=got, prefix='□'))

    if freeform:                               # envelope dimensions and note; the analytical faces stay normally mandatory
        for k in range(3):
            lo, hi = bb['min'][k], bb['max'][k]
            ea, eb = _extreme(k, lo, feats, bb), _extreme(k, hi, feats, bb)
            edges[k].append(edge(k, lo, hi, ea, eb, f'{pid}#extent_{AX[k]}', False, 'ext', placed=visible_place(k, lo, hi, ea, eb), hull=True))      # only at visible edges
        freeform_note = f'{FREEFORM_TEXT} {model_ref}'.strip()
        notes.append(freeform_note)
    for r in (r for r in part.get('regions') or [] if not r.get('minor')):    # envelope dimensions per freeform or facet region (position + extent) and reference with leader line; only major regions, the rest stand in the "undimensioned shapes" line
        rlo, rhi = _v(r['min']), _v(r['max'])
        for k in range(3):
            if rhi[k] - rlo[k] <= TOL:
                continue
            anchors = []
            for n, c in enumerate((rlo[k], rhi[k])):
                q = (rlo + rhi) / 2
                q[k] = c
                q = _v(r['ends'][k][n]) if r.get('ends') else q           # edge point of the region, otherwise centre of the bounding-box face
                anchors.append(_at(k, c, feats, bb) + [(None, q, None)])
            flat = [w for w in ws if not w['section'] and w['det'] is None and not w['aux']]    # edge points do not lie in the section
            edges[k].append(edge(k, rlo[k], rhi[k], anchors[0], anchors[1], r['id'], True,
                                 placed=_place(flat, [(np.eye(3)[k], anchors[0], anchors[1])]), hull=True))

    # pitch: first to last member per direction, "{n-1}x {pitch} (={length})"
    for p in pats:
        mem = [by_id[m] for m in p['members']]
        cols, rows = p.get('cols') or len(mem), p.get('rows') or 1
        for i, d in enumerate(p.get('dirs') or []):
            if d is None:
                continue
            n, last = (cols, mem[cols - 1]) if i == 0 else (rows, mem[(rows - 1) * cols])
            d = _v(d)
            k = _world(d)                     # coordinates in the space of the tree: along k (dirs may be -x), oblique: along d
            pos = (lambda m: float(_axis_pts(m)[0][k])) if k is not None else (lambda m: float(_axis_pts(m)[0] @ d))
            if k is None:      # oblique pitch in a coordinate plane -> axis-parallel components (Δx, Δy per “n× … (=…)”), no diagonal; the coverage assumes both for the pitch
                A, B = _axis_pts(mem[0])[0], _axis_pts(last)[0]
                comps = [kk for kk in range(3) if abs(float(B[kk] - A[kk])) > TOL]
                if len(comps) >= 2:
                    for kk in comps:
                        step = abs(float(p['pitch'][i]) * float(d[kk]))
                        edges[kk].append(edge(kk, float(A[kk]), float(B[kk]), [(mem[0]['id'], q, None) for q in _axis_pts(mem[0])],
                                              [(last['id'], q, None) for q in _axis_pts(last)], p['id'], True, first=float(A[kk]),
                                              **({'prefix': f"{n - 1}× {fmt(step)} (=", 'suffix': ')'} if n > 2 else {})))      # two members: only the distance
                    continue
            sym = False
            if k is not None:      # a pattern whose members lie symmetrically to the median plane of the part needs no position dimension of its first member (position by symmetry, checks.located)
                sym = checks.mirrored_positions(mem, k, (bb['min'][k] + bb['max'][k]) / 2)
                if sym:
                    sym_skip.add((mem[0]['id'], k))
            ed = edge(k if k is not None else 0, pos(mem[0]), pos(last),
                      [(mem[0]['id'], q, None) for q in _axis_pts(mem[0])], [(last['id'], q, None) for q in _axis_pts(last)],
                      p['id'], True, d=d, first=None if sym else pos(mem[0]), **({'prefix': f"{n - 1}× {fmt(p['pitch'][i])} (=", 'suffix': ')'} if n > 2 else {}))      # two members: only the distance
            (edges[k] if k is not None else loose).append(ed)

    # blind-hole depth: opening (start) to bottom (end) where the hole is shown (in the section); for patterns
    # once at the first member. Substitute points on the same coordinate count only together with the hole.
    for f in feats:
        if f['kind'] != 'hole' or not f.get('blind') or f['id'] in rest:
            continue
        k = _world(_v(f['axis']))
        if k is None:
            continue
        s, e = _v(f['start']), _v(f['end'])
        tag = first_of[f['id']]['id'] if f['id'] in first_of else f['id']
        own = lambda p: [(f['id'], p, None)] + [(f['id'], q, pf) for _, q, pf in _at(k, p[k], feats, bb)]
        edges[k].append(edge(k, s[k], e[k], own(s), own(e), tag, True, first=s[k]))

    # Notches (two flanks at a concave edge) - width at the opening and depth, congruent notches once “n× …” (also), the position of each notch individually
    # (baseline node at the opening edge); only with an axis-parallel opening and depth direction (otherwise planner note, the coverage reports it)
    notch_pos, notch_lead = {}, set()
    notch_groups = {}
    for f in feats:
        if f['kind'] == 'notch':
            notch_groups.setdefault(checks.equal_key(f) or f['id'], []).append(f)
    for grp in notch_groups.values():
        for f in grp:
            o0, o1, apex = _v(f['open'][0]), _v(f['open'][1]), _v(f['apex'])
            ku, kd = _world(prj.unit(o1 - o0)), _world(prj.unit(apex - (o0 + o1) / 2))
            if ku is None or kd is None:
                notes.append({BUDGET_NOTE: f"Part {pid}: {checks.ART['notch']} {f['id'].rpartition('#')[2]}: opening not axis-parallel (no true length in a view)", 'excess': 0})
                continue
            c0, c1 = sorted((float(o0[ku]), float(o1[ku])))
            p0, p1 = (o0, o1) if o0[ku] <= o1[ku] else (o1, o0)
            notch_pos[f['id']] = (ku, c0, c1, p0, p1)
            if f is grp[0]:
                notch_lead.add(f['id'])
                extra = {'prefix': f'{len(grp)}× ', 'also': [g['id'] for g in grp[1:]]} if len(grp) > 1 else {}
                edges[ku].append(edge(ku, c0, c1, [(f['id'], p0, None)], [(f['id'], p1, None)], f['id'], True, first=(c0, c1), **extra))
                mid = (o0 + o1) / 2
                edges[kd].append(edge(kd, float(mid[kd]), float(apex[kd]), [(f['id'], mid, None), (f['id'], p0, None)], [(f['id'], apex, None)], f['id'], True,
                                      first=float(mid[kd]), **extra))
    # Rows of congruent notches at the same opening face: the first (nearest the datum) from the datum, the others via the distance of the opening edges (one datum per axis)
    rows = {}
    for grp in notch_groups.values():
        for f in grp:
            if f['id'] in notch_pos:
                ku = notch_pos[f['id']][0]
                kd = _world(prj.unit(_v(f['apex']) - (_v(f['open'][0]) + _v(f['open'][1])) / 2))
                rows.setdefault((id(grp), ku, round(float(_v(f['apex'])[kd]), 2), round(float(_v(f['open'][0])[kd]), 2)), []).append(f)
    for (_, ku, _, _), fs in rows.items():
        fs.sort(key=lambda f: (abs(notch_pos[f['id']][1] - float(datum[AX[ku]])), f['id']))
        for a, b in zip(fs, fs[1:]):
            (_, ca, _, pa, _), (_, cb, _, pb, _) = notch_pos[a['id']], notch_pos[b['id']]
            edges[ku].append(edge(ku, ca, cb, [(a['id'], pa, None)], [(b['id'], pb, None)], b['id'], True, first=ca))

    # equal elements in a row (equal holes/shafts/arcs, key checks.equal_key) per axis: the one nearest the datum from the datum, the others via the
    # centre distance to the predecessor (chain of distances, no baseline row from the edge); elements on the same coordinate form no row (one dimension, the coverage applies to all)
    for k in range(3):
        e, d0 = np.eye(3)[k], float(datum[AX[k]])
        mid = (bb['min'][k] + bb['max'][k]) / 2
        rowsets = {}
        for f in feats:
            if f['kind'] in LOCATED and f['id'] not in rest and not _outside(f, bb) and checks.equal_key(f) is not None and not (turned is not None and abs(turned[1] @ e) < 1e-6):
                ax, c = f.get('axis'), float(_axis_pts(f)[0] @ e)
                if (ax is None or abs(_v(ax) @ e) < 1e-6) and abs(c - mid) > TOL and AX[k] not in (f.get('tangent') or {}):
                    rowsets.setdefault(checks.equal_key(f), []).append((c, f))
        for members in rowsets.values():
            reps = []
            for c, f in sorted(members, key=lambda m: (abs(m[0] - d0), m[1]['id'])):
                if not reps or abs(c - reps[-1][0]) > TOL:
                    reps.append((c, f))
            sym_row = checks.mirrored_positions([m for _, m in members], k, mid)
            if sym_row:
                sym_skip.update((f_['id'], k) for _, f_ in members)
            for (ca, fa), (cb, fb) in zip(reps, reps[1:]):
                edges[k].append(edge(k, ca, cb, _anchors(fa, feats, bb) + _at(k, ca, feats, bb), _anchors(fb, feats, bb) + _at(k, cb, feats, bb), fb['id'], True, first=None if sym_row else ca))

    # two planes (web edges) symmetric to the axis of a hole/shaft: the position dimension of the axis and the width between the edges determine both edges (no two baseline dimensions from the edge)
    for k in range(3):
        e, d0 = np.eye(3)[k], float(datum[AX[k]])
        pls = [f for f in feats if f['kind'] == 'plane' and f['axis_name'] == AX[k] and not f.get('square') and not f.get('member_of') and not f.get('minor')]
        seen_pair = []
        for f in feats:
            if f['kind'] not in ('hole', 'shaft') or f['id'] in rest or _outside(f, bb) or f.get('axis') is None or abs(_v(f['axis']) @ e) > 1e-6 or (turned is not None and abs(turned[1] @ e) < 1e-6):
                continue
            ca = float(_axis_pts(f)[0] @ e)
            if abs(ca - (bb['min'][k] + bb['max'][k]) / 2) <= TOL:
                continue
            best = None
            for p1 in pls:
                p2 = next((g for g in pls if abs(g['coord'] - (2 * ca - p1['coord'])) <= TOL), None)
                if p1['coord'] < ca - TOL and p2 is not None and all(abs(q['coord'] - h_) > TOL for q in (p1, p2) for h_ in (bb['min'][k], bb['max'][k])):
                    if best is None or ca - p1['coord'] < ca - best[0]['coord']:
                        best = (p1, p2)
            if best and not any(abs(best[0]['coord'] - c_) <= TOL for c_ in seen_pair):
                seen_pair.append(best[0]['coord'])
                p1, p2 = best
                edges[k].append(edge(k, p1['coord'], p2['coord'], _at(k, p1['coord'], feats, bb), _at(k, p2['coord'], feats, bb), p1['id'], True, first=ca, join=ca))

    # profile view for the part height (smallest bounding-box edge): only if a normal view without a section shows it in the image plane
    k_profile = int(np.argmin(bb['size']))
    profile_ok = any(_orient(w, np.eye(3)[k_profile]) in ('h', 'v') and not w['section'] and w['det'] is None and not w['aux'] for w in ws)
    regions_h = [r for r in part.get('regions') or [] if r.get('minor') and not held_p(r) and not r.get('facet') and r['max'][k_profile] - r['min'][k_profile] > TOL]

    # pocket, step, groove, plane series as a whole; their members are no baseline nodes
    els = [f for f in feats if f['kind'] in ELEMENTS and f.get('members')]
    grouped = {m for f in els for m in f['members']}
    at = lambda k, c: _at(k, c, feats, bb)
    bbmid = lambda k: (bb['min'][k] + bb['max'][k]) / 2
    seen_pp = []
    for f in sorted(els, key=lambda f: (ELEMENTS_PLAN.index(f['kind']), f['id'])):
        k = AX.index(f['axis_name'])
        if f['kind'] == 'plane_pattern':
            c1, c2 = f['first'], f['first'] + f['length']
            twin = next((g for g in seen_pp if g['axis_name'] == f['axis_name'] and g['count'] == f['count']
                         and abs(g['pitch'] - f['pitch']) <= TOL), None)
            edges[k].append(edge(k, c1, c2, at(k, c1), at(k, c2), f['id'], True, first=(c1, c2), link=twin['first'] if twin else None,
                                 prefix=f"{f['count'] - 1}× {fmt(f['pitch'])} (=", suffix=')'))
            seen_pp.append(f)
            continue
        if f['kind'] == 'step':
            m = AX.index(f['across'])
            edges[m].append(edge(m, f['edge'], f['wall'], at(m, f['edge']), at(m, f['wall']), f['id'], True,
                                 _art(m, f['edge'], f['wall'], feats), first=f['edge']))
        else:
            for a in sorted(f['walls']):
                m, (w1, w2) = AX.index(a), f['walls'][a]
                sym = abs((w1 + w2) / 2 - bbmid(m)) <= TOL
                edges[m].append(edge(m, w1, w2, at(m, w1), at(m, w2), f['id'], True, _art(m, w1, w2, feats),
                                     first=None if sym else (w1, w2)))
        edges[k].append(edge(k, f['top'], f['floor'], at(k, f['top']), at(k, f['floor']), f['id'], True, first=f['top']))

    # function dimensions - numbers from the model texts that match exactly one geometry dimension are dimensioned directly
    # (distance between the two reference planes before any chain derivation; the value comes from the geometry)
    for res in _function_dims(texts, feats):
        if res['status'] == 'direct':
            k, c1, c2, fid = res['axis'], res['c'][0], res['c'][1], res['fid']
            edges[k].insert(0, edge(k, c1, c2, _at(k, c1, feats, bb), _at(k, c2, feats, bb), fid, True, _art(k, c1, c2, feats),
                                    first=(c1, c2), prio=True))
        entry = {'part': pid, **{key: v for key, v in res.items() if key not in ('axis', 'c', 'fid')}}
        if report is not None and entry not in report:        # variants (scale, format) plan the same dimensions repeatedly
            report.append(entry)

    # Baseline per axis from the datum: first the overall dimension to the opposite face, then the distances with a meaning of their own (before
    # each of them its datum coordinate: opening of a depth, first pattern member), finally all not yet connected ones
    ws_base = sorted(ws, key=lambda w: (w['det'] is not None, w['i']))     # basic dimensions: detail views last
    mirror_state = {'dropped': set(), 'used': {}}
    grouped0 = set(grouped)
    closure = {k_: m_ for k_, m_ in sym_planes.items() if any(_axis_view_ok(w, k_, bb, block_k) for w in ws)}      # axes with mirror closure; if one drops out (see below), the tree is recomputed without it

    # planes through the centre of a sphere (or through the axis of a hole/shaft in the median plane of the part) are fixed by the element - no dimension of their own for the position
    # (checks.center_plane, the same function as the coverage); the sphere gets its centre cross for it (below)
    try:
        fnums = checks.function_numbers(texts)[0]
    except Exception:
        fnums = []

    def rank_sorted(eds, k):
        """Distances of an axis in the intake order of the rank test (chains.rank_order via checks.chain_measure: overall dimension, function dimension, pattern “n× …”, rest; text as on the sheet)."""
        import chains
        ms = [checks.chain_measure(n, f"{e['extra'].get('prefix') or ''}{e['extra'].get('suffix') or ''}", min(e['c']), max(e['c']), bb, k, fnums, by_id.get(e['fid']), by_id)[0]
              for n, e in enumerate(eds)]
        return [eds[n] for n in chains.rank_order(ms)]
    centred = {f['id']: checks.center_plane(f, feats, bb) for f in feats if f['kind'] == 'plane'}
    centred = {i: g for i, g in centred.items() if g is not None}

    def baselines(grouped, failed=(), mirror=None):
        """Tree per axis. mirror {axis: plane}: mirror closure (_Tree(m)); the distances rejected by the closure stand in mirror_state['dropped'] (id of the distance), the number of dimensions
        discarded by the closure per axis in mirror_state['used'] (then the remaining dimensions of the axis must stand in views with a symmetry line)."""
        mirror = mirror or {}
        mirror_state.update(dropped=set(), used={})
        accepted = list(loose)
        for k in range(3):
            e, d0 = np.eye(3)[k], float(datum[AX[k]])
            mid = (bb['min'][k] + bb['max'][k]) / 2
            nodes = []
            for f in feats:
                if f['kind'] == 'plane':           # members of an element: only as reference coordinate (first), no dimension of their own
                    if f['axis_name'] == AX[k] and not f.get('square'):
                        nodes.append((f['coord'], f, [(f['id'], _v(f['pt']), f)]))
                elif f['kind'] == 'notch':          # position of the opening edges (the lead notch hangs via its width dimension, each further notch with an edge at the datum)
                    pos = notch_pos.get(f['id'])
                    if pos is not None and pos[0] == k:
                        nodes += [(pos[1], f, [(f['id'], pos[3], None)])] + ([(pos[2], f, [(f['id'], pos[4], None)])] if f['id'] in notch_lead else [])
                elif f['kind'] in LOCATED and f['id'] not in rest and not _outside(f, bb):
                    ax, c = f.get('axis'), float(_axis_pts(f)[0] @ e)
                    if turned is not None and abs(turned[1] @ e) < 1e-6:
                        continue
                    if (ax is None or abs(_v(ax) @ e) < 1e-6) and abs(c - mid) > TOL and AX[k] not in (f.get('tangent') or {}) and (f['id'], k) not in sym_skip:
                        nodes.append((c, f, _anchors(f, feats, bb)))
            if k == k_profile and profile_ok:      # web height and offset - the edges of the freeform regions (webs) along the part height, in the profile view
                for r in regions_h:
                    for n_, c_ in enumerate((r['min'][k], r['max'][k])):
                        if abs(c_ - d0) > TOL and all(abs(c_ - h_) > TOL for h_ in (bb['min'][k], bb['max'][k])) and not any(abs(c_ - m_[0]) <= TOL for m_ in nodes):
                            q_ = _v(r['ends'][k][n_]) if r.get('ends') else (_v(r['min']) + _v(r['max'])) / 2
                            nodes.append((float(c_), r, [(None, q_, None)], True))
            nodes.sort(key=lambda n: (abs(n[0] - d0), n[1]['id']))
            tree = _Tree(mirror.get(k))
            tree.join(d0, d0)
            for left, right, shown_axis in station_links[k]:
                tree.join(left, right, mirror=shown_axis)      # the station dimensions already stand; mirror closure only if their view shows the symmetry line (_axis_view_ok)
            far = float(bb['max'][k] if abs(d0 - bb['min'][k]) <= TOL else bb['min'][k])

            def bind(c):
                n = next((n for n in nodes if abs(n[0] - c) <= TOL), None)
                if n is None:
                    return
                ref = far if (abs(n[0] - far) + TOL < abs(n[0] - d0) and tree.same(d0, far) and abs(far - d0) > TOL) else d0      # position from the nearest body-fixed datum (envelope face that already hangs at the datum via the overall dimension), no staggering right across the view
                a2 = n[2] + _at(k, n[0], feats, bb)
                pl_ = visible_place(k, ref, n[0], _at(k, ref, feats, bb), a2, n[1]['id']) if len(n) > 3 else None
                if ref != d0 and pl_ is None and len(n) <= 3:      # the opposite face as datum only if a view carries both foot points visibly (otherwise foot point on a hidden edge: dim_on_hidden_edge)
                    pl_ = visible_place(k, ref, n[0], _at(k, ref, feats, bb), a2, n[1]['id'])
                    if pl_ is None:
                        ref = d0
                if tree.join(ref, n[0]):
                    accepted.append(edge(k, ref, n[0], _at(k, ref, feats, bb), a2, n[1]['id'], False, _art(k, ref, n[0], feats), placed=pl_))
            bind(bb['max'][k] if abs(d0 - bb['min'][k]) <= TOL else bb['min'][k])
            eds = [e for e in edges[k] if e['fid'] not in failed]
            if k in mirror:        # mirror closure: the distances near the datum first (function dimensions before), then the side at the datum stays and the mirror member is dropped
                eds.sort(key=lambda e: (not e['prio'], min(abs(c - d0) for c in e['c'])))
            eds = rank_sorted(eds, k)      # rank classes like the rank test of the check (chains.rank_order), within the class the previous order
            for ed in eds:      # if an end already hangs at the datum, the distance connects directly (otherwise it would be lost)
                if tree.closed(*ed['c']):          # only the mirror closure connects both ends: the distance is the mirror image of an assumed one (no dimension, not even a baseline dimension of its first member)
                    mirror_state['dropped'].add(id(ed))
                    tree.mirror_drops += 1
                    continue
                before = tree.mirror_drops
                twin_done = False
                if ed['first'] is not None and not any(tree.same(d0, c) for c in ed['c']):
                    f0 = min(ed['first'], key=lambda c: (abs(c - d0), c)) if isinstance(ed['first'], tuple) else ed['first']
                    if ed['link'] is not None and tree.same(d0, ed['link']) and tree.join(ed['link'], f0):
                        accepted.append(edge(k, ed['link'], f0, at(k, ed['link']), at(k, f0), ed['fid'], True))
                        pit = float((by_id.get(ed['fid']) or {}).get('pitch') or 0.0)
                        c_first = ed['first'][0] if isinstance(ed['first'], tuple) else ed['first']
                        twin_done = pit > 0 and abs(c_first - ed['link']) < pit - TOL      # twin flank (nested, like checks.pattern_groups): pitch of the first sequence + thickness fix the distance (checks.chain_twins), it is not measured once more; equal twins at a distance (another pattern group) keep their dimension
                    else:
                        bind(f0)
                if twin_done:
                    tree.join(*ed['c'])
                    mirror_state['dropped'].add(id(ed))
                    continue
                if tree.join(*ed['c']):
                    accepted.append(ed)
                    if ed.get('join') is not None:      # web symmetric to the hole axis: the axis hangs at the datum, its web edges via this axis (no second position dimension of the edges)
                        tree.join(ed['c'][0], ed['join'])
                    pat = by_id.get(ed['fid'])
                    for m in (pat['members'] if pat is not None and pat['kind'] == 'pattern' and not isinstance(pat.get('members'), str) else []):      # the pitch fixes all members - their coordinates also hang at the datum (no second distance dimension of a member)
                        if m in by_id and by_id[m].get('start') is not None:
                            tree.join(ed['c'][0], float(_axis_pts(by_id[m])[0] @ ed['d']))
                elif tree.mirror_drops > before:
                    mirror_state['dropped'].add(id(ed))
            for n in nodes:
                if n[1]['id'] not in grouped and not n[1].get('minor') and n[1]['id'] not in centred:      # minor planes (slot flanks) stand in the row “Undimensioned forms”, no position dimension of their own (as a datum coordinate they stay nodes); a plane through the centre of a dimensioned body of revolution likewise
                    bind(n[0])
            if tree.mirror_drops:
                mirror_state['used'][k] = tree.mirror_drops
        return accepted

    def placeable(ed):
        return ed['placed'] or _place(ws if ed['chain'] else ws_base, [(ed['d'], *ed['a'])])

    side_of = lambda o, chain: {'h': ('above', 'below'), 'v': ('left', 'right')}.get(o, ('left', 'left'))[chain]

    def build(closure):
        """Baseline trees (with mirror closure of the axes in closure), element members and placement. -> (assumptions, view choice, axes whose mirror dimensions found no view with a symmetry line)."""
        nonlocal grouped
        grouped = set(grouped0)
        hull_notes.clear()      # only the last pass counts: a recompute fallback would otherwise append the envelope notes twice
        accepted = baselines(grouped, mirror=closure)
        # Members are dropped only if all distances of their element are assumed and placeable; otherwise they keep their single dimension (distances rejected by the mirror closure do not count as failed)
        ok_ids = {id(ed) for ed in accepted if placeable(ed)}
        failed = {f['id'] for f in els for ed in (e for k in range(3) for e in edges[k]) if ed['fid'] == f['id'] and id(ed) not in ok_ids and id(ed) not in mirror_state['dropped']}
        if failed:
            grouped = {m for f in els if f['id'] not in failed for m in f['members']}
            accepted = baselines(grouped, failed, mirror=closure)
        used = dict(mirror_state['used'])
        stack, put, bad = {}, [], set()               # budget: stacked parallel dimensions per view side (view, orientation, side)
        for ed in accepted:
            full = ws if ed['chain'] else ws_base
            k_ed = _world(ed['d'])
            gated = k_ed is not None and k_ed in used
            pool, opts = ([w for w in full if _axis_view_ok(w, k_ed, bb, block_k, ed['fid'])] if gated else full), [(ed['d'], *ed['a'])]
            got, flip = ed['placed'], False
            if gated and got is not None and got[0] not in pool:
                got = None                                # pre-placed in a view without a symmetry line
            for fl in (False, True):               # first a view with room (other view), then the opposite side, finally over budget
                if got is None:
                    flip = fl
                    got = _place(pool, opts, lambda w, o: stack.get((w['id'], o, side_of(o, bool(ed['chain']) != fl)), 0) < MAX_STACK, True)   # only views that show the element visibly
            if got is None:
                got, flip = _place(pool, opts), False
                if got is not None and ed.get('hull'):      # fallback - no view carries both ends at visible edges (plan._content creates a profile view)
                    hull_notes.append({BUDGET_NOTE: f"Part {pid}: envelope/range dimension {ed['fid'].split('#')[-1]} in view {got[0]['id']}: no view shows both ends as visible edges", 'excess': 0, 'needs_profile_view': True, 'axis': ed['k']})
            if got is None:
                if gated and (ed['placed'] or _place(full, opts)):
                    bad.add(k_ed)                         # placeable only outside the views with a symmetry line: final check triggers
                continue
            w, o, p1, p2, _ = got
            side = side_of(o, bool(ed['chain']) != flip)
            stack[(w['id'], o, side)] = stack.get((w['id'], o, side), 0) + 1
            put.append((ed, w, o, p1, p2, side))
        return accepted, put, used, bad

    # Final check of the mirror closure, variant “force axis”: a dimension that replaces a mirror dimension may only stand in a view in which symmetry.add_axes generates the symmetry line
    # (_axis_view_ok, the same predicate); otherwise the mirror dimension would not be covered. If that does not succeed for all dimensions of an axis, the tree for this axis is recomputed without mirror closure.
    # Chosen against “re-dimension the view without closure”: the dimensioning is per part, not per view, re-dimensioning a view would change the layout after the measurement.
    hull_notes = []
    while True:
        accepted, put, used, bad = build(closure)
        if not bad:
            break
        closure = {k_: m_ for k_, m_ in closure.items() if k_ not in bad}
    notes.extend(hull_notes)
    for ed, w, o, p1, p2, side in put:
        add({'type': 'linear', 'view': w['id'], 'p1': _r(p1), 'p2': _r(p2), 'axis': o, 'side': side, 'feature': ed['fid'],
             **ed['extra']}, round(abs(ed['c'][1] - ed['c'][0]), 4), ed['art'])

    # position across the oblique axis in the auxiliary view - for each image axis that is not a world axis (the world axis is carried by the
    # tree above), one dimension from the part edge (oblique_reference) to the axis; same coordinate (aligned axes) only once
    for w in (w for w in ws if w['aux'] and turned is None):
        N = w['fr'][0]
        for o, d in (('h', w['fr'][1]), ('v', w['fr'][2])):
            if _world(d) is not None:
                continue
            done = []
            for f in feats:
                if (f['id'] not in w['members'] or f['kind'] not in LOCATED or f.get('axis') is None or f['id'] in rest
                        or _outside(f, bb) or abs(_v(f['axis']) @ N) < 0.99):
                    continue
                pts = _axis_pts(f)                         # axis centre; arc: tangent point on the arc across d, otherwise the centre
                c = pts[len(pts) // 2] if f['kind'] != 'arc' else _pick(w, _anchors(f, feats, bb), False, d)
                ref = None if c is None else oblique_reference(feats, N, d, c, w['cut'])
                if ref is None or any(abs(c @ d - x) <= TOL for x in done):
                    continue
                done.append(float(c @ d))
                add({'type': 'linear', 'view': w['id'], 'p1': _r(ref), 'p2': _r(c), 'axis': o, 'side': 'left' if o == 'v' else 'above',
                     'feature': f['id']}, round(abs(float((c - ref) @ d)), 4))
        for f in (f for f in feats if f['kind'] == 'arc' and f['id'] in w['members']):     # arc centre as datum: centre mark
            if any(a['type'] == 'linear' and a['view'] == w['id'] and np.allclose(_v(f['center']), _v(q), atol=1e-4)
                   for _, a, _, _ in items if a['type'] == 'linear' for q in (a['p1'], a['p2'])):
                add({'type': 'centermark', 'view': w['id'], 'center': _r(f['center']), 'feature': f['id']})

    for f in (f for f in feats if f['kind'] == 'sphere'):     # centre cross where the sphere is positioned
        for vid in dict.fromkeys(a['view'] for _, a, _, _ in items if a['type'] == 'linear' and a['feature'] == f['id']):
            add({'type': 'centermark', 'view': vid, 'center': _r(f['center']), 'feature': f['id']})

    # diameter: holes/shafts (pattern: one "n x diameter"), spheres "SØ"; thread target cylinders get the thread designation
    sunk_ids = set()
    for f in feats:
        if f['kind'] in ('hole', 'shaft') and f['id'] not in rest and f['id'] not in threaded:
            pat = first_of.get(f['id'])
            mem = [by_id[m] for m in pat['members']] if pat else [f]
            ax = prj.unit(f['axis'])
            sib = any(g is not f and g['kind'] == f['kind'] and abs(g['diameter'] - f['diameter']) <= 0.005 and _coaxial(f, g)
                      for g in feats if g['kind'] in ('hole', 'shaft'))
            got = _size_view(ws, [(m, _axis_pts(m)[1:2] + _axis_pts(m)[::2]) for m in mem], ax, cls != 'turned' and not sib)
            if got and pat and pat.get('polar'):      # the members have different axes: the Ø at the member and in the view in which it does not lie on a hidden edge (each member its own axis)
                got = None
                for m in mem:
                    axm = prj.unit(m['axis'])
                    g2 = _size_view(ws, [(m, _axis_pts(m)[1:2] + _axis_pts(m)[::2])], axm, cls != 'turned' and not sib)
                    if g2 is None:
                        continue
                    probe = {'type': 'diameter', 'view': g2[0]['id'], 'center': _r(g2[2]), 'radius': f['diameter'] / 2, 'axis': _r(axm)}
                    if got is None:
                        got, ax = g2, axm                           # fallback: the first member with a view
                    if not _hidden(probe, g2[0], feats, ray, by_id):
                        got, ax = g2, axm
                        break
            if got:
                a = {'type': 'diameter', 'view': got[0]['id'], 'center': _r(got[2]), 'radius': f['diameter'] / 2,
                     'axis': _r(ax), 'feature': pat['id'] if pat else f['id']}
                if pat and pat.get('polar') and abs(float(_v(ax) @ _v(got[0]['fr'][0]))) > 0.99:      # circular view: the text of the sequence stands outside the part (leader line), not on the wall edge
                    a['angle'], a['leader'] = _leader_out(got[0], got[2], bb, block_k)
                if pat:
                    a['prefix'] = f"{pat['count']}× Ø"
                    if pat.get('polar') and pat['polar'].get('missing'):      # empty grid places of the sequence stand in the specification
                        a['suffix'] = f" (without {_gap_text(pat['polar']['missing'], pat['polar']['start'], pat['polar']['angle'])})"
                cones = [c for m in mem if m['kind'] == 'hole' for c in checks.countersinks_of(m, feats) if not c.get('minor')]
                if cones:       # countersinks stand in the hole designation ("Ø5 ⌵Ø10×90°"), no cone text of their own
                    a['suffix'] = a.get('suffix', '') + ' ' + ' '.join(dict.fromkeys(checks.size_text('cone', checks.collective_size(c)) for c in cones))
                    sunk_ids.update(c['id'] for c in cones)
                add(a, f['diameter'], 'int' if f['kind'] == 'hole' else 'ext')
                if pat and pat.get('polar') and seq_view.get(pat['id']) is not None:      # pitch, pitch circle radius, start angle, axial offset of the sequence
                    for sa, sv in _sequence_dims(seq_view[pat['id']], ws, pat, by_id, feats):
                        add(sa, sv)
        elif f['kind'] == 'sphere':
            center = _v(f['center'])
            cuts = [w for w in ws if w['sect'] is not None and abs((center-w['sect']) @ w['fr'][0]) < TOL]
            got = _size_view(cuts if f.get('internal') and cuts else ws, [(f, [center])], None, True)
            if got:
                add({'type': 'diameter', 'view': got[0]['id'], 'center': _r(got[2]), 'radius': f['diameter'] / 2,
                     'prefix': 'SØ', 'feature': f['id']}, f['diameter'], 'ext')
                if any(g is f for g in centred.values()) and not any(a['type'] == 'centermark' and a.get('feature') == f['id'] for _, a, _, _ in items):
                    add({'type': 'centermark', 'view': got[0]['id'], 'center': _r(f['center']), 'feature': f['id']})      # the cross shows the centre through which the plane passes
        elif f['kind'] == 'arc' and checks.collective_family(f) is None:   # corner radii: bottom
            a = _radius_ann(ws, f, feats, ray=ray)
            if a is None:                                  # (b)/(c) as for the members of a class
                a = _surface_leader(ws, f['id'], _v(f['center']), float(f['radius']), prj.unit(f['axis']),
                                    checks.size_text('radius', float(f['radius'])), visible_only=True, arc=f)                     or _radius_ann(ws, f, feats, visible_only=False)
            if a:
                add(a)

    # fillets/corner radii and chamfers per size class: one member individually (A); from two members a collective designation
    # (B) - the class with the most members (on a tie the smaller size) as the only general note per kind in the
    # notes, further ones counted on one member if a view shows all members, otherwise member by member individually (A)
    def single(f, text=None, wv=None):
        """Dimension of one member: chamfer = leader line; fillet/arc = R dimension in a view that shows it visibly, otherwise
        (b) leader line "R..." at its visible edge, finally (c) as before at the hidden edge (finding in the render)."""
        if f['kind'] in ('chamfer', 'cone', 'slant') and not f.get('at'):      # face without a circular edge
            return _point_leader(wv or ws, f['id'], f['pt'], text or checks.size_text(checks.collective_family(f) or f['kind'], checks.collective_size(f)))
        if f['kind'] == 'chamfer':
            at = f['at']
            return _surface_leader(wv or ws, f['id'], _v(at['center']), at['radius'], prj.unit(at['axis']),
                                   text or checks.size_text('chamfer', checks.collective_size(f)))
        a = _radius_ann(wv or ws, f, feats, ray=ray)
        if a is None and not f.get('major_radius'):
            a = _surface_leader(wv or ws, f['id'], _v(f['center']), float(f['radius']), prj.unit(f['axis']),
                                text or checks.size_text('radius', float(f['radius'])), visible_only=True, arc=f)
        if a is None and wv is None:
            a = _radius_ann(ws, f, feats, visible_only=False)
        return a

    classes = checks.size_classes([f for f in feats if not f.get('minor') and f['id'] not in sunk_ids and (f['kind'] != 'cone' or f.get('countersink') or checks.end_chamfer(f))])      # minor faces and countersinks named in the hole designation are excluded; shaft cones go via _cone_dims, countersinks without a hole designation as a general note
    per_size = {}          # cones/countersinks, oblique faces: sizes with a general note (one line per GENERAL_PER_LINE sizes)
    for family in checks.FAMILIES:
        multi = sorted((c for c in classes if c[0] == family and len(c[2]) >= 2), key=lambda c: (-len(c[2]), checks._order(c[1])))
        for fam, size, mem in (c for c in classes if c[0] == family):
            size = checks.norm_size(fam, size)
            if len(mem) == 1:
                a = single(mem[0], checks.size_text(fam, size) if fam in checks.PER_SIZE else None)
                if a:
                    add(a)
                elif fam in checks.PER_SIZE:     # no view shows the face: general note (never a face without a dimension)
                    per_size.setdefault(fam, []).append(size)
                continue
            if fam in checks.PER_SIZE:       # one general note per size (shaft cones of equal size; countersinks stand in the hole designation, free-standing cones in the minor-faces line), never hundreds of individual dimensions
                per_size.setdefault(fam, []).append(size)
                continue
            ids = [f['id'] for f in mem]
            both = [w for w in ws if all(i in w['shows'] for i in ids)]
            if not both and fam == 'radius' and not any(_radius_ann(ws, f, feats, ray=ray) for f in mem):
                both = [w for w in ws if any(i in w['shows'] for i in ids)]   # (a): no member has a visible axis view
            a = None
            if mem is not multi[0][2] and both:
                text = f"{len(mem)}× {checks.size_text(fam, size)}"
                a = next((x for x in (single(f, text, both) for f in mem) if x), None)
            if a:
                a.pop('feature', None)
                a['features'] = ids
                if a['type'] == 'radius':
                    a['radius'], a['prefix'] = size, f'{len(mem)}× R'
                add(a)
            elif mem is multi[0][2]:
                notes.append({'text': checks.general_text(fam, size), 'general': {'kind': fam, 'size': size, 'part': pid}})
            else:                                          # at most one general note per kind and part: the rest individually (A)
                for f in mem:
                    a = single(f)
                    if a:
                        add(a)

    twin_of = set(station_ids)
    # N5.3, N5.1: every slant carries length along the slant and angle (_slant_options). Equal angles (checks.slant_angle_key: inclination and normal magnitudes, i.e.
    # parallel and mirrored slants) and equal lengths (checks.slant_profile_key from the element value) of a part stand once each as “n× …” in the view that shows most of the group in
    # true size visibly (tie: body edge reference before reference line, then a view that already carries dimensions, then the first view = main view), the other slants hang in also - angle and length form groups of their own.
    slant_opts = {}
    opts_of = lambda g: slant_opts.setdefault(g['id'], _slant_options(g, ws, feats, bb) if g.get('normal') else [])
    todo = [f for f in slants if f['id'] not in twin_of]
    have = [f for f in todo if opts_of(f)]
    shown_in = lambda g: {o[1]['id'] for o in opts_of(g) if o[3]}
    shown = lambda g: bool(shown_in(g))      # n× and also only at members that any view shows visibly; the others keep their own dimension
    view_no = {w['id']: n for n, w in enumerate(ws)}
    held = set()

    def carrier(group, kind):
        """(member, option) of the group: view with the most visibly shown members, then body edge reference (angle), then view order; in the member of this view with a visible
        option and the lowest rank."""
        per_view = {}
        for g in group:
            for o in opts_of(g):
                per_view.setdefault(o[1]['id'], []).append((g, o))
        used = {a['view'] for _, a, _, _ in items} if len(group) > 1 else set()      # group: a view that already carries dimensions before an empty one (so the empty one is dropped, prune_views); a single slant stays in the first view (detail view of the instance)
        vid = min(per_view, key=lambda v: (-sum(1 for _, o in per_view[v] if o[3]), min(o[0] for _, o in per_view[v]) if kind == 'angle' else 0, v not in used, view_no[v]))
        return min(per_view[vid], key=lambda go: (not go[1][3], go[1][0], group.index(go[0])))

    def by_view(group, kind):
        """(carrier, option, members) per carrier: a member stands in also of the carrier only if its view shows it visibly in the final view set; the others form a group of their own with
        the carrier in the view that shows them (or a single dimension). If a view disappears during pruning, its member thus becomes a single dimension on a not shown edge (`unseen`) and the removal is rejected."""
        pending = list(group)
        while pending:
            g, o = carrier(pending, kind)
            mine = [m for m in pending if m is g or o[1]['id'] in shown_in(m)]
            yield g, o, mine
            pending = [m for m in pending if m not in mine]

    def own_length(g):
        """Length specification that g carries in its preferred option."""
        return next(a for a in _slant_pick(opts_of(g))[2] if a['type'] == 'linear')

    def length_key(g, a):
        """Key of the length specification a at g (checks.copy_key, the same function as the n× check): profile-equal via the element value, otherwise full congruence."""
        return checks.copy_key(g, {'kind': 'linear', 'p1': a['p1'], 'p2': a['p2']})

    def split(members, keyf):
        out = {}
        for g in members:
            key = keyf(g)
            out.setdefault(('single', g['id']) if key is None or not shown(g) else key, []).append(g)
        return list(out.values())

    for grp in split(have, checks.slant_angle_key):
        carriers = []
        for g, o, group in by_view(grp, 'angle'):
            anns = [a for a in o[2] if a['type'] in ('angle', 'polyline')]
            ang = next(a for a in anns if a['type'] == 'angle')
            if len(group) > 1:
                ang.update(prefix=f'{len(group)}× ', also=[m['id'] for m in group if m is not g])
            held.add(id(ang))      # the group stands per showing view; _merge_equal does not merge it again across views
            for a in anns:
                add(a)
            carriers.append((o[1]['id'], [m['id'] for m in group]))
        if len(carriers) > 1:      # documented limit of N5.1 - equal angles stand several times because no view shows all members; the checker reports it (warn)
            notes.append({BUDGET_NOTE: f"Part {pid}: equal angle {fmt(float(grp[0]['angle_deg']), 1)}° at {len(grp)} slants stands in {len(carriers)} views, no view shows all: "
                                       + '; '.join(f"{v}: {', '.join(i.rpartition('#')[2] for i in ids)}" for v, ids in carriers) + ' (documented limit of N5.1)', 'excess': 0})
    for g, o, group in (t for grp in split(have, lambda g: length_key(g, own_length(g))) for t in by_view(grp, 'linear')):
        a = next(a for a in o[2] if a['type'] == 'linear')
        value = _linear_value(a, ws)
        same = [m for m in group if length_key(m, a) == length_key(g, a)]      # n× only at members that the carrier's dimension really covers
        kept = same if len(same) > 1 else [g]
        if len(kept) > 1:
            a.update(prefix=f'{len(kept)}× ', also=[m['id'] for m in kept if m is not g])
        add(a, value)
        rest = [m for m in group if m not in kept]      # members that the carrier does not cover carry their own length - identical ones (copy_key) once as n×, not per member
        while rest:
            g0 = rest[0]
            alt = own_length(g0)
            same_rest = [m for m in rest if length_key(m, alt) == length_key(g0, alt) and (m is g0 or alt['view'] in shown_in(m))]
            if len(same_rest) > 1:
                alt.update(prefix=f'{len(same_rest)}× ', also=[m['id'] for m in same_rest if m is not g0])
            add(alt, _linear_value(alt, ws))
            rest = [m for m in rest if m not in same_rest]
    for f in (f for f in todo if f not in have):
        # no angle reference in any view (last resort): extents of the edge; congruent slants (mirror pair) once “n× …”, the others in also
        if f['id'] in twin_of:
            continue
        got = _slant_legs(f, ws, bb, feats) if f.get('normal') else []
        twins = [g for g in congruent[checks.equal_key(f) or f['id']] if g is not f and g.get('normal') and g['id'] not in twin_of and g in todo and g not in have]
        if twins and got:
            for a in got:
                if a['type'] in ('angle', 'linear'):
                    a['prefix'], a['also'] = f'{1 + len(twins)}× ', [g['id'] for g in twins]
            twin_of.update(g['id'] for g in twins)
        if got:
            for a in got:
                add(a, _linear_value(a, ws) if a['type'] == 'linear' else None)
        else:
            notes.append({BUDGET_NOTE: f"Part {pid}: {checks.ART['slant']} {f['id'].rpartition('#')[2]} ∠{fmt(float(f['angle_deg']), 1)}°: no view shows it in true length", 'excess': 0})      # planner note, no sheet text

    for f in (f for f in feats if f['kind'] == 'cone' and not f.get('minor') and not f.get('drill_tip') and not f.get('via_member')
              and not f.get('countersink') and f['id'] not in sunk_ids and not checks.end_chamfer(f)):             # shaft cones in the trapezoid: diameter and length, no private notation
        got = _cone_dims(f, ws, items, bb, feats, block_k)
        for a, value in got:
            add(a, value, 'ext' if a['type'] == 'diameter' else None)
        if not f.get('head') and not any(a['type'] == 'linear' for a, _ in got):      # the cone head carries Ø + angle without length, the note would not apply there
            notes.append({BUDGET_NOTE: f"Part {pid}: {checks.ART['cone']} {f['id'].rpartition('#')[2]}: no view shows it with the axis in the image plane", 'excess': 0})      # planner note, no sheet text

    for fam, sizes in per_size.items():
        for k in range(0, len(sizes), GENERAL_PER_LINE):
            chunk = sizes[k:k + GENERAL_PER_LINE]
            notes.append({'text': f"{GENERAL_PREFIX} {checks.FAMILY_WORD[fam]} " + '; '.join(checks.size_text(fam, z) for z in chunk),
                          'general': [{'kind': fam, 'size': z, 'part': pid} for z in chunk]})

    # threads, knurls, engravings
    for t in threads:
        # target cylinder; a modelled thread without a cylinder carries axis, start and end of its region itself
        tgt = by_id.get(t.get('target')) or (t if t.get('axis') is not None and t.get('start') is not None else None)
        got = _size_view(ws, [(tgt, _axis_pts(tgt)[1:2] + _axis_pts(tgt)[::2])], prj.unit(tgt['axis']), False) if tgt else None
        if got:
            add({'type': 'thread', 'view': got[0]['id'], 'feature': tgt['id'], 'start': _r(tgt['start']), 'end': _r(tgt['end']),
                 'major_d': t['major_d'], 'minor_d': t['minor_d'], 'internal': bool(t['internal']), 'designation': t['designation']})
        else:
            notes.append(f"Thread {t['designation']} (position per 3D data set)")
    for kn in (f for f in feats if f['kind'] == 'knurl'):
        text = ' '.join(x for x in (KNURL_WORD, kn.get('profile'), f"t={fmt(kn['pitch_mm'])}" if kn.get('pitch_mm') else None) if x)
        sh = next((g for g in feats if g['kind'] == 'shaft' and kn.get('radius') and abs(g['diameter'] - 2 * kn['radius']) <= 0.1), None)
        a = _surface_leader(ws, kn['id'], _axis_pts(sh)[1], sh['diameter'] / 2, prj.unit(sh['axis']), text, sh['id']) if sh else None
        if a:
            add(a)
        else:
            notes.append(text)
    for g in (f for f in feats if f['kind'] == 'engraving'):
        line = engraving_note(g)                                   # identical lines only once
        if line not in notes:
            notes.append(line)

    minor = sorted(f['id'] for f in list(feats) + list(part.get('regions') or []) if f.get('minor') and not held_p(f))      # the residual members of a profile stand in the profile, not in this row
    if minor:       # exactly one row per part for all minor faces (draft slants, edge breaks, facets, free-standing cones, small regions)
        text = checks.MINOR_TEXT
        if freeform:                    # the shape note and the "undimensioned shapes" note are one line (both coverages stay, checks.FREEFORM_RX)
            notes.remove(freeform_note)
            text = f'{FREEFORM_MINOR_TEXT} {model_ref}'.strip()
        notes.append({'text': text, 'model_ref': minor, 'minor': {'part': pid}})
    for r in (r for r in part.get('regions') or [] if not r.get('minor')):     # reference to the 3D data set with a leader line on the region (major regions only)
        normal = [w for w in ws if w['det'] is None and not w['aux'] and not w['section']]
        probe = lambda w: {'type': 'leader', 'view': w['id'], 'p': _r(r['pt'])}
        w = next((w for w in normal if r['id'] in w['shows'] and not _hidden(probe(w), w, feats, ray)), None) or next(iter(normal), None)      # the view in which the region point is visible (ray test), otherwise as before the first normal view
        if w is not None:
            add({'type': 'leader', 'view': w['id'], 'p': _r(r['pt']), 'text': CONTOUR_TEXT, 'model_ref': [r['id']],
                 'feature': r['id'], 'end': 'dot'})       # (ISO 128-22) a face means the leader line with a dot on it, not an arrowhead on an edge

    items = _budget(_visibility(_merge_equal(_dedupe(items, ws), by_id, order, held), ws, feats, ray, by_id, notes, pid), ws, feats, notes, pid, ray, by_id)
    items.sort(key=lambda it: it[0])
    anns = [a for _, a, _, _ in items]
    # Tolerance from the model text: attached to the one dimension whose nominal value, kind (inside/outside) and - if the text
    # names it ("square bar 8 h11", "hole 10 H7") - form feature match; otherwise a note line (the planner lists it under Open)
    seen = set()
    for text in tolerances:
        for value, field, start, _ in plan_roles.tolerance_spans(text):
            kind = next((k for k, rx in _TOL_KIND if rx.search((text or '')[:start])), None)
            if (value, field, kind) in seen:
                continue
            seen.add((value, field, kind))
            want = 'int' if field[0].isupper() else 'ext'
            hit = [a for _, a, v, art in items if v is not None and art == want and abs(v - value) <= 0.005
                   and _tol_fits(a, kind, by_id)]
            if len(hit) == 1:
                hit[0]['tolerance'] = field
            else:
                notes.append(f'{TOL_NOTE}{fmt(value)} {field}')
    try:      # N5.2 as a message: what the rank test still finds in the finished dimensions; nothing is deleted here
        nums = checks.function_numbers(texts)[0]
    except Exception:
        nums = []
    left = derivable_left(items, ws, bb, nums, closure, by_id)
    total = sum(left.values())
    if total:      # only if something remains: planner note with the number (plan._build_part_content sums it to the candidate metric derivable_left), no sheet text
        notes.append({BUDGET_NOTE: f"Part {pid}: {total} length dimensions still follow from others after the rank test (" + ', '.join(f'{AX[k_]}: {n}' for k_, n in sorted(left.items()) if n) + ')',
                      'excess': 0, 'derivable_left': total, 'by_axis': {AX[k_]: n for k_, n in sorted(left.items()) if n}})
    anns += profile_annotations(part, views, pid, anns)
    return anns, notes, tables


PROFILE_ARC_MM = 12.0     # smallest radius of the angle arc of the profile dimensions on paper; arc='free' (drawing.angle_free_radius): the renderer chooses the nearest radius at which arc and text are free of outline and lines


def profile_annotations(part, views, pid, existing=()):
    """N4.10: dimensions of the profiles with residual members (part['profiles'] = [{'profile', 'need', 'residual'}], plan._context), from the canonical mandatory list profiles.required_dims (per size
    the first specification, every size once, “n× …” for equal ones). View: the profile view of its own (profile_of) or the existing view that carries the dimensions (profile_dims). The foot points stand on the outline
    of the profile (coordinates of the profile, mapped via profile['frame'] into the middle of the instance length: there lies the section or the silhouette of the lateral faces), angles around the centre of rotation; a
    centre-line cross shows centre and axes for rotational symmetry. Tag `feature` = profile id (profile_findings). existing = the general dimensions of the part: a length dimension whose distance they already fix (the same distance, e.g. the part height 13 of the web, or a chain run in the same direction; checks.profile_stated via checks.chain_determined, where the profile dimensions already set count too) is not set (each dimension once; the check takes it as stated, checks.profile_missing, and reports no closed chain)."""
    out = []
    frames_ = {x['id']: prj.frame_of(x) for x in views if x.get('part') == pid}

    def stated_of(a):
        """(direction, smaller, larger end coordinate) of a general length dimension of the part (like the check: checks.profile_stated)."""
        fr_ = frames_.get(a.get('view'))
        if a.get('type') != 'linear' or fr_ is None or a.get('p1') is None:
            return None
        p1_, p2_ = _v(a['p1']), _v(a['p2'])
        d_ = {'h': fr_[1], 'v': fr_[2]}.get(a.get('axis')) if a.get('axis') in ('h', 'v') else (_v(a['dir']) if a.get('axis') == 'along' and a.get('dir') is not None else p2_ - p1_)
        n_ = float(np.linalg.norm(d_))
        return None if n_ < 1e-9 else (d_ / n_, *sorted(float(q @ (d_ / n_)) for q in (p1_, p2_)))
    elsewhere = [s_ for s_ in (stated_of(a) for a in existing) if s_ is not None]
    for pp in part.get('profiles') or []:
        P = pp['profile']
        if any(P['id'] in (x.get('profile_unbuilt') or ()) for x in views):
            continue
        v = next((x for x in views if x.get('part') == pid and x.get('profile_of') == P['id']), None) or next(
            (x for x in views if x.get('part') == pid and P['id'] in (x.get('profile_dims') or ())), None)
        if v is None or P.get('frame') is None:
            continue
        fr = prj.frame_of(v)
        o, u, w = (np.asarray(P['frame'][k], float) for k in ('origin', 'u', 'v'))
        P3 = lambda q: _r(o + q[0] * u + q[1] * w)
        view_dir = lambda vec: prj.project([vec], fr)[0]
        start = len(out)
        for x in pp['need']:
            g, n = x['geom'] or {}, int(x['n'])
            a = {'view': v['id'], 'feature': P['id']}
            if n > 1:
                a['prefix'] = f'{n}× '
            if x['kind'] == 'angle' and 'vertex' in g:
                out.append({'type': 'angle', 'vertex': P3(g['vertex']), 'p1': P3(g['p1']), 'p2': P3(g['p2']), 'radius': PROFILE_ARC_MM, 'arc': 'free', **a})
            elif x['kind'] == 'linear' and 'p1' in g:
                p1, p2 = P3(g['p1']), P3(g['p2'])
                if g.get('axis') is not None:        # coordinate along a profile axis (envelope, position to the edge): dimension of the projection onto this axis
                    vec = (u, w)[int(g['axis'])]
                    q = view_dir(vec)
                    if abs(q[1]) < 1e-6 * max(1.0, abs(q[0])):
                        a['axis'] = 'h'
                    elif abs(q[0]) < 1e-6 * max(1.0, abs(q[1])):
                        a['axis'] = 'v'
                    else:
                        a.update(axis='along', dir=_r(vec))
                else:
                    q = view_dir(_v(p2) - _v(p1))
                    a['axis'] = 'h' if abs(q[1]) < 1e-6 * max(1.0, abs(q[0])) else 'v' if abs(q[0]) < 1e-6 * max(1.0, abs(q[1])) else 'aligned'
                if checks.profile_stated(P, x, elsewhere):
                    continue                      # the distance is already fixed by dimensions of the part (the same distance or chain run: one decision with the check)
                out.append({'type': 'linear', 'p1': p1, 'p2': p2, **a})
                if (s_ := stated_of(out[-1])) is not None:
                    elsewhere.append(s_)          # the check also sees the profile dimensions already set - what is fixed with them, the planner does not set (otherwise chain_closed)
            elif x['kind'] == 'diameter' and 'c' in g:
                out.append({'type': 'diameter', 'center': P3(g['c']), 'radius': g['r'], 'axis': _r(P['axis']), **a})
            elif x['kind'] == 'radius' and 'c' in g:
                out.append({'type': 'radius', 'center': P3(g['c']), 'radius': g['r'], 'angle': round(float(g.get('a', 45.0)), 3), **a})
        ctr = next((x['geom']['vertex'] for x in pp['need'] if (x['geom'] or {}).get('vertex') is not None), None) or next(
            (x['geom']['p1'] for x in pp['need'] if x['key'] in ('bottom',)), None)
        if v.get('profile_own') and ctr is not None:      # centre of the rotational symmetry: centre-line cross along the image axes (also the foot point of the distance dimensions)
            half = 1.15 * max(float(np.hypot(q[0] - ctr[0], q[1] - ctr[1])) for e in P['outline'] for q in profiles.sample(e, 0.5))
            c3 = _v(P3(ctr))
            out[start:start] = [{'type': 'polyline', 'style': 'center', 'view': v['id'], 'points': [_r(c3 - ax * half), _r(c3 + ax * half)]}
                                for ax in (fr[1], fr[2])]       # the centre-line cross stands before the dimensions of the profile (drawing order): the angle arc (arc='free') avoids it
    return out


LEADER_TO = ((8, 8), (-8, 8), (8, -8), (-8, -8))       # only short leader lines: longer ones enlarge the measured occupancy of the view and shift the block layout
LEADER_CHAR_MM, LEADER_TEXT_H = 2.6, 3.5      # text width per character (conservative, osifont 3.5 mm with digits/special characters) for the staggering; box with 2 mm clearance


def _stagger_leaders(anns, ws, k):
    """Stagger leader lines without their own 'to' (core: 8, 8 towards the upper right). The text of each leader line gets the first direction from LEADER_TO whose box on the paper does not cover any other text of the view (only if the default direction is covered and a free place exists): other leader lines (also with their own 'to'), dimension texts (middle
    of the dimension segment, offset 10 mm to the dimension side), diameter/R texts at the anchor, identifying letters of section lines and view arrows (at the line ends). Without paper scale k
    nothing; the planner calls it after the marking (plan._content), autofix moves further if it has to."""
    if not k:
        return
    wby = {w['id']: w for w in ws}
    sheet = lambda w, pt: (lambda q: np.array([float(q @ w['fr'][1]), float(q @ w['fr'][2])]) * (w['k'] or k))(_v(pt))
    square = lambda c, r: [c[0] - r, c[1] - r, c[0] + r, c[1] + r, None]
    placed = {}                                          # view -> text boxes [x0, y0, x1, y1] (paper)

    def text_box(p, to, text):
        L = LEADER_CHAR_MM * len(str(text or '')) + 2.0
        x = p[0] + to[0]
        return [x if to[0] >= 0 else x - L, p[1] + to[1] - 2.0, x + L if to[0] >= 0 else x, p[1] + to[1] + LEADER_TEXT_H + 2.0, text]
    for a in anns:                                       # obstacles: all texts except the leader lines still to be placed
        w = wby.get(a.get('view'))
        if w is None:
            continue
        t, boxes = a.get('type'), placed.setdefault(a['view'], [])
        if t == 'leader' and a.get('to') is not None and a.get('p') is not None:
            boxes.append(text_box(sheet(w, a['p']), a['to'], a.get('text')))
        elif t == 'linear' and a.get('p1') is not None:
            q1, q2 = sheet(w, a['p1']), sheet(w, a['p2'])
            off = {'above': (0, 10), 'below': (0, -10), 'left': (-10, 0), 'right': (10, 0)}.get(a.get('side') or ('above' if a.get('axis') == 'h' else 'left'), (0, 10))
            c = (q1 + q2) / 2 + np.array(off, float)
            boxes.append([c[0] - 8.0, c[1] - 2.5, c[0] + 8.0, c[1] + 4.5, None])
        elif t in ('diameter', 'radius') and a.get('center') is not None:
            c = sheet(w, a['center'])
            boxes.append([c[0], c[1], c[0] + 16.0, c[1] + 6.0, None])
        elif t == 'section_line':
            boxes += [square(sheet(w, a[key]), 6.0) for key in ('p1', 'p2')]
        elif t == 'view_arrow' and a.get('p') is not None:
            boxes.append(square(sheet(w, a['p']), 6.0))
    hit = lambda b, c: b[0] < c[2] and c[0] < b[2] and b[1] < c[3] and c[1] < b[3]
    for a in anns:
        w = wby.get(a.get('view'))
        if a.get('type') != 'leader' or a.get('to') is not None or w is None or a.get('p') is None:
            continue
        if not checks.CHAMFER_RX.search(str(a.get('text') or '')):      # only chamfer notes are moved: moving contour/knurl notes made a test part worse (104 against 97), autofix clears them on collision
            continue
        p, boxes = sheet(w, a['p']), placed.setdefault(a['view'], [])
        def conflicts(t):
            return [c for c in boxes if hit(text_box(p, t, a.get('text')), c) and c[4] != a.get('text')]       # same text = duplicate leader line, no space problem
        to, bad = LEADER_TO[0], conflicts(LEADER_TO[0])
        if bad:                                          # evade away from the covered text: if it lies right of the anchor, to the left, otherwise to the lower right first
            right = float(np.mean([(c[0] + c[2]) / 2 for c in bad])) > p[0]
            order = [(-8, 8), (-8, -8), (8, -8)] if right else [(8, -8), (-8, 8), (-8, -8)]
            to = next((t for t in order if not conflicts(t)), LEADER_TO[0])      # no free place (dense view): default direction, autofix decides
        if to != LEADER_TO[0]:
            a['to'] = list(to)
        boxes.append(text_box(p, to, a.get('text')))


stagger_leaders = _stagger_leaders


VALUE_TOL = 0.005           # mm, value from the text against a geometry dimension (as the tolerance assignment)


def _function_dims(texts, feats):
    """For each number from the model texts (plan_facts.function_values) [{value, kind, text, count, status, measure, axis, c, fid}].
    Geometry dimensions: distances per world axis between two planes (length), diameter of hole/shaft/sphere, R of arc/fillet. If
    the value (kind: length/diameter/radius, any = any) matches exactly one dimension, status is 'direct' (distance, not yet dimensioned:
    the distance is dimensioned directly) or 'existing' (diameter/R have a dimension of their own, distance of a square/element is already
    direct); otherwise 'ambiguous' (several, change nothing) or 'none' (none, angle not supported)."""
    import plan_facts
    planes = {k: [] for k in range(3)}
    for f in feats:
        if f['kind'] == 'plane' and f.get('axis_name') in AX:
            planes[AX.index(f['axis_name'])].append(f)
    coords = {}
    for k, fs in planes.items():
        cs = []
        for f in sorted(fs, key=lambda f: f['coord']):
            if not cs or f['coord'] - cs[-1][0] > TOL:
                cs.append([f['coord'], []])
            cs[-1][1].append(f)
        coords[k] = cs
    diam = sorted({round(float(f['diameter']), 4) for f in feats if f['kind'] in ('hole', 'shaft', 'sphere')})
    rad = sorted({round(float(f['radius']), 4) for f in feats if f['kind'] in ('arc', 'fillet') and f.get('radius') is not None})
    out = []
    for item in plan_facts.function_values(texts):
        v, kind = item['value'], item['kind']
        cands = []
        if kind in ('length', 'any'):
            for k, cs in coords.items():
                for i, (c1, f1) in enumerate(cs):
                    cands += [('length', k, c1, c2, f1, f2) for c2, f2 in cs[i + 1:] if abs(c2 - c1 - v) <= VALUE_TOL]
        if kind in ('diameter', 'any'):
            cands += [('diameter', d) for d in diam if abs(d - v) <= VALUE_TOL]
        if kind in ('radius', 'any'):
            cands += [('radius', r) for r in rad if abs(r - v) <= VALUE_TOL]
        res = {**item, 'status': 'none' if not cands else 'ambiguous', 'measure': None}
        if len(cands) == 1:
            c = cands[0]
            if c[0] == 'length':
                _, k, c1, c2, f1, f2 = c
                own = any(f.get('square') or f.get('member_of') for f in f1 + f2)       # square/element dimension already stands directly
                res.update(status='existing' if own else 'direct', measure=f'{AX[k]} {fmt(c1)} .. {fmt(c2)}', axis=k, c=(c1, c2),
                           fid=min(f['id'] for f in f2))
            else:
                res.update(status='existing', measure=('Ø' if c[0] == 'diameter' else 'R') + fmt(c[1]))
        out.append(res)
    return out


TOL_NOTE = 'Tolerance from model text: '
# form-feature keyword directly before the nominal value -> kind of dimension the tolerance may belong to
_TOL_KIND = (('square', re.compile(r'(?:\bsquare(?: bar)?|□)\s*$', re.I)), ('hole', re.compile(r'\bhole\s*$', re.I)),
             ('shaft', re.compile(r'\b(?:shaft|pin|bolt)\s*$', re.I)), ('diameter', re.compile(r'Ø\s*$')))


def _tol_fits(a, kind, by_id):
    """Does annotation a fit the form-feature keyword kind (None: any dimension with matching value and kind)?"""
    if kind is None:
        return True
    if kind == 'square':
        return a.get('prefix') == '□'
    if a['type'] != 'diameter':
        return False
    f = by_id.get(a.get('feature')) or {}
    return kind == 'diameter' or (f.get('member_kind') or f.get('kind')) == kind


def _merge_equal(items, by_id, order=None, held=()):
    """n× for equal elements of a part (replaces the twin-Ø merge): Ø dimensions of equal value, text and addition
    of holes/shafts with the same key (checks.equal_key: kind, Ø, axis direction, depth, blind - crossing or coaxial holes
    of different depth never merge) stand once as “n× Ø…” at the first member, the others in 'also'; likewise R dimensions of equal arcs; equal
    angle dimensions of the same key (checks.slant_angle_key: inclination, normal magnitudes; without view) as “n× 35°” (with the number of all slants thereby covered, also the congruent ones from 'also'; their
    extension lines are dropped as well). n counts the actually equal elements, not the views. items as in _dedupe. held: id() of angle dimensions that dimension() has already grouped per showing view (no group across views) and keep their dimension."""
    groups, drop, order = {}, set(), order or {}

    def ids_of(a):
        """All form features that a Ø/R specification covers (tag, also, features; a pattern with its members), and whether it already is an n× specification."""
        out = []
        for t in ([a['feature']] if a.get('feature') else []) + list(a.get('also') or []) + list(a.get('features') or []):
            g = by_id.get(t)
            out += list(g['members']) if g is not None and g['kind'] == 'pattern' and g.get('members') else [t]
        return list(dict.fromkeys(out))
    for n, (key, a, v, art) in enumerate(items):
        counted = bool(a.get('also') or a.get('prefix') or a.get('features'))
        if a['type'] in ('diameter', 'radius') and counted:        # already counted specifications (pattern, also, collective specification) of the same equal elements are merged as well (e.g. “2× Ø4” + “3× Ø4”)
            ids = [i for i in ids_of(a) if i in by_id]
            f = by_id.get(ids[0]) if ids else None
            if f is None or a.get('half'):
                continue
            ek = checks.equal_key(f, a['type'])
            if ek is None or any(checks.equal_key(by_id[i], a['type']) != ek for i in ids):
                continue
            if a['type'] == 'diameter' and f['kind'] in ('hole', 'shaft'):
                groups.setdefault(('diameter', round(float(a['radius']), 3), a.get('suffix'), a.get('tolerance'), ek), []).append(n)
            elif a['type'] == 'radius' and f['kind'] in ('arc', 'fillet') and not (a.get('features') and f['kind'] == 'fillet'):
                groups.setdefault(('radius', round(float(a['radius']), 3), ek), []).append(n)
            continue
        f = by_id.get(a.get('feature')) if a.get('feature') else None
        if f is None:
            continue
        ek = checks.equal_key(f, a['type'])
        if ek is None:
            continue
        if a['type'] == 'diameter' and f['kind'] in ('hole', 'shaft'):
            gk = ('diameter', round(float(a['radius']), 3), a.get('suffix'), a.get('tolerance'), ek)
        elif a['type'] == 'radius' and f['kind'] == 'arc':
            gk = ('radius', round(float(a['radius']), 3), ek)
        elif a['type'] == 'leader' and f['kind'] == 'arc' and checks.RADIUS_RX.search(a.get('text') or ''):      # R leader line and R dimension of equal arcs are one statement
            gk = ('radius', round(float(f['radius']), 3), ek)
        elif a['type'] == 'angle' and f['kind'] == 'slant' and checks.slant_angle_key(f) is not None and id(a) not in held:      # angle key without view - only for remainders, the grouping in dimension() already sets equal angles once
            gk = ('angle', checks.slant_angle_key(f))
        elif a['type'] == 'diameter' and f['kind'] == 'cone' and not a.get('half'):        # congruent cones (equal Ø, angle, length) carry their Ø once
            gk = ('diameter', round(float(a['radius']), 3), a.get('suffix'), None, ek)
        elif a['type'] == 'linear' and f['kind'] == 'cone' and v is not None and abs(float(v) - float(f['length'])) <= 0.011 and not a.get('prefix'):      # ... likewise their length
            gk = ('cone_length', round(float(v), 3), ek)
        else:
            continue
        groups.setdefault(gk, []).append(n)
    for gk, ns in groups.items():
        if len(ns) < 2:
            continue
        is_pat = lambda n: by_id.get(items[n][1].get('feature'), {}).get('kind') == 'pattern'
        first = next((n for n in ns if items[n][1]['type'] == 'leader'), None)      # the leader line sits on a visible arc edge (_arc_tips), an R dimension can stand in a view that does not show the arc
        if first is None:      # otherwise a member with its own tag (no pattern, no collective specification): the count check counts tag + also
            first = next((n for n in ns if items[n][1].get('feature') and not is_pat(n)), None)
        if first is None:
            continue
        keep = items[first][1]
        feats_ = list(dict.fromkeys([keep['feature']] + [x for n in ns for x in (ids_of(items[n][1]) if gk[0] != 'angle'
                                                                                 else [items[n][1]['feature']] + list(items[n][1].get('also') or []))]))
        keep['also'] = [x for x in feats_ if x != keep['feature']]
        keep.pop('features', None)
        if gk[0] == 'cone_length':
            keep['prefix'] = f'{len(feats_)}× '
        elif keep['type'] == 'leader':
            keep['text'] = f"{len(feats_)}× {re.sub(r'^[0-9]+×\s*', '', keep['text'])}"
        else:
            keep['prefix'] = {'diameter': f'{len(feats_)}× Ø', 'radius': f'{len(feats_)}× R', 'angle': f'{len(feats_)}× '}[gk[0]]
        for n in (m for m in ns if m != first):
            drop.add(n)
            if gk[0] == 'angle':        # extension lines of the angle dimension that is dropped
                drop.update(m for m, (_, b, _, _) in enumerate(items)
                            if b['type'] == 'polyline' and b.get('feature') == items[n][1]['feature'] and b.get('view') == items[n][1]['view'])
    firsts = {}                     # a pattern statement "7× 8 (=56)" stands once per sheet and part (in the first view); equal series elsewhere
    for n, (key, a, v, art) in enumerate(items):      # keep only their position dimension (tag: the pitch stays covered)
        f = by_id.get(a.get('feature')) if a.get('feature') else None
        if n in drop or a['type'] != 'linear' or not a.get('prefix') or f is None or f['kind'] != 'plane_pattern':
            continue
        k = (a['prefix'], a.get('suffix'), v)
        firsts.setdefault(k, []).append(n)
    for ns in firsts.values():
        for n in sorted(ns, key=lambda n: (order.get(items[n][1]['view'], len(order)), n))[1:]:
            items[n][1].pop('prefix'), items[n][1].pop('suffix', None)
    return [it for n, it in enumerate(items) if n not in drop]


BUDGET_NOTE = 'plan_note'      # note object {BUDGET_NOTE: text, excess: number over budget}: planner note, no sheet text (plan._content separates it)


SURFACE_KINDS = ('plane', 'plane_pattern', 'pocket', 'step', 'slot', 'slant', 'chamfer', 'square')      # dimension anchors on faces/edges (not axes, centres)


def _surface_anchor(a, by_id):
    """Do the foot points of a linear dimension lie on faces of the part (ray test meaningful)? Not for axes/centre points (centre lines are
    drawn), diameter dimensions, leader lines."""
    f = (by_id or {}).get(a.get('feature')) if a.get('feature') else None
    return (a['type'] == 'linear' and not a.get('orthographic_stations') and a.get('p1') is not None
            and a.get('axis') in ('h', 'v') and f is not None and f.get('kind') in SURFACE_KINDS)


def _meas_dir(w, o):
    """Dimension direction (world vector) of a horizontal ('h') or vertical ('v') dimension in view w: the face normal of the dimension planes."""
    return np.asarray(w['fr'][1 if o == 'h' else 2], float)


def _covered(w, p, feats, ray=None, n=None):
    """Does a plane of a form feature lie in front of point p (view w looks at it) (face in its bbox, not cut away) - so the point is not
    on a visible edge? Only planes (axis-parallel); curved and inclined faces are unknown to the check (it reports only what is certainly hidden; with slants as occluders blocking rose 30 -> 32 without less hidden)."""
    N = w['fr'][0]
    if ray is not None and n is not None:     # ray test (curved and inclined occluders)
        if w.get('sect') is None:
            return not ray(p, N, n)
        tmax = float((np.asarray(w['sect'], float) - np.asarray(p, float)) @ N)      # in a section only the material between point and cutting plane counts, in front of it it is cut away
        return False if tmax <= 0.02 else not ray(p, N, n, tmax)
    for f in feats:
        if f['kind'] != 'plane' or not f.get('bbox'):
            continue
        a = AX.index(f['axis_name'])
        if abs(N[a]) < 1e-9:
            continue
        t = (f['coord'] - p[a]) / N[a]
        if t <= 0.02:
            continue
        q = p + t * N
        if w.get('sect') is not None and float((q - w['sect']) @ N) > TOL:
            continue                                   # in front of the cutting plane: cut away
        lo, hi = f['bbox']['min'], f['bbox']['max']
        if all(lo[j] + 0.05 <= q[j] <= hi[j] - 0.05 for j in range(3) if j != a):      # truly in the interior of the face: points on the outline stay visible
            return True
    return False


RING_PROBES = 24         # probe points on the reference circle of a diameter in the circle view (as drawing.hidden_reference, only coarser)
RING_VISIBLE = 0.1       # drawing.RING_VIS: from this share of visible probe points the circle counts as drawn visible


def _probes(a, w, by_id):
    """Anchors of an annotation in view w for the visibility test, [(point, normals)]: the anchor counts as visible if the
    ray is free for one of the normals (lift to both sides, ft.LIFT). Linear dimension on faces: foot points with the dimension direction; diameter with axis
    in the image plane: the outline points c ± w·r across the axis (for a half dimension line only the real one), in the circle view: probe points of the reference circle
    with their radial direction (circle, see drawing.hidden_reference); leader line: the tip with both image axes; otherwise points without normal
    (only the plane check of _covered)."""
    t = a['type']
    N, Vx, Vy = (np.asarray(x, float) for x in w['fr'])
    if t == 'linear' and a.get('p1') is not None:
        n = _meas_dir(w, a['axis']) if _surface_anchor(a, by_id) else None
        return [(q, [n] if n is not None else [None]) for q in (_v(a['p1']), _v(a['p2']))]
    if t == 'diameter' and a.get('center') is not None:
        c, r = _v(a['center']), float(a.get('radius') or 0.0)
        ax = _v(a['axis']) if a.get('axis') is not None else None
        if r <= 0:
            return [(c, [None])]
        if ax is not None and float(np.linalg.norm(ax)) > 1e-9 and abs(float(prj.unit(ax) @ N)) < 0.99:
            u = prj.unit(np.cross(N, ax))
            sg = [int(np.sign(a['half']))] if a.get('half') else [-1, 1]
            return [(c + s * u * r, [u]) for s in sg]
        return [(c + r * (np.cos(t_) * Vx + np.sin(t_) * Vy), [np.cos(t_) * Vx + np.sin(t_) * Vy])
                for t_ in np.linspace(0, 2 * np.pi, RING_PROBES, endpoint=False)]
    if t == 'leader' and a.get('p') is not None:
        return [(_v(a['p']), [Vx, Vy])]
    if t == 'radius' and a.get('center') is not None and a.get('radius'):        # the tip of the R dimension on the arc (direction angle in the image plane)
        u = np.cos(np.radians(float(a.get('angle', 45)))) * Vx + np.sin(np.radians(float(a.get('angle', 45)))) * Vy
        return [(_v(a['center']) + float(a['radius']) * u, [u])]
    return [(q, [None]) for q in _anchor_points(a)]


def _hidden(a, w, feats, ray=None, by_id=None):
    """Does an anchor of annotation a in view w lie on a hidden edge (ray test ray, for sections the plane check)? Without ray
    only the plane check of the anchor points (_covered) applies. Diameter in the circle view: hidden if fewer than RING_VISIBLE of the probe points
    of the reference circle are visible."""
    if ray is None:
        return any(_covered(w, p, feats) for p in _anchor_points(a))
    probes = _probes(a, w, by_id)
    if not probes:
        return False
    cover = [all(_covered(w, p, feats, ray, n) for n in ns) for p, ns in probes]
    if a['type'] == 'diameter' and len(probes) > 2:
        return sum(1 for c in cover if not c) < RING_VISIBLE * len(cover)
    return any(cover)


def _moves(a, ws, feats, ray=None, by_id=None):
    """Views into which annotation a can be moved, with the changed fields: [(view, {field: value})]. Movable are linear dimensions
    (horizontal/vertical), diameter dimensions (same kind of view: circle or axis in the image plane) and leader lines - in views that show the form feature
    visibly (shows) and whose cut-out contains the points. Angle, R and thread dimensions stay (their references are bound to the view)."""
    fids = [a['feature']] if a.get('feature') else list(a.get('features') or [])[:1]
    if not fids:
        return []
    if a.get('orthographic_stations'):
        return []       # Already proven on both sides at visible edges of the two opposite faces.
    shows = lambda w: any(fid in w['shows'] for fid in fids)
    out = []
    if a['type'] == 'linear' and a.get('axis') in ('h', 'v') and 'dir' not in a:
        p1, p2 = _v(a['p1']), _v(a['p2'])
        old = next((w for w in ws if w['id'] == a['view']), None)
        if old is None:
            return []
        d = _meas_dir(old, a['axis'])
        if abs(float((p2-p1) @ d)) < 1e-9:
            return []
        base = {'h': {'above': 'above', 'left': 'above', 'below': 'below', 'right': 'below'},
                'v': {'left': 'left', 'above': 'left', 'right': 'right', 'below': 'right'}}
        for w in ws:
            o = _orient(w, d)
            if w['id'] == a['view'] or o not in ('h', 'v') or not shows(w) or (w.get('aux') and not old.get('aux')):
                continue
            b = {**a, 'view': w['id'], 'axis': o}
            kept = w['sect'] is None or max(float((p - w['sect']) @ w['fr'][0]) for p in (p1, p2)) <= CUT_TOL
            if kept and not _hidden(b, w, feats, ray, by_id) and _inside(w, p1) and _inside(w, p2):
                out.append((w, {'view': w['id'], 'axis': o, 'side': base[o].get(a.get('side') or 'above', base[o]['above'])}))
    elif a['type'] == 'diameter' and a.get('axis') is not None and not a.get('half'):      # (half dimension line: the side follows the viewing direction, stays)
        ax, old = _v(a['axis']), next((w for w in ws if w['id'] == a['view']), None)
        circle = old is not None and abs(float(ax @ old['fr'][0])) > 0.99
        for w in ws:
            c = abs(float(ax @ w['fr'][0]))
            if (w['id'] != a['view'] and shows(w) and (c > 0.99 if circle else c < 1e-3) and _inside(w, _v(a['center']))
                    and not _hidden({**a, 'view': w['id']}, w, feats, ray, by_id)):
                out.append((w, {'view': w['id']}))
    elif a['type'] == 'leader' and a.get('p') is not None:
        for w in ws:
            if w['id'] != a['view'] and shows(w) and _inside(w, _v(a['p'])) and not _hidden({**a, 'view': w['id']}, w, feats, ray, by_id):
                out.append((w, {'view': w['id']}))
    return out


def _anchor_points(a):
    """Points that a movable annotation puts on the drawing (end points, centre or tip); [] otherwise."""
    if a['type'] == 'linear' and a.get('p1') is not None:
        return [_v(a['p1']), _v(a['p2'])]
    if a['type'] == 'diameter' and a.get('center') is not None:
        return [_v(a['center'])]
    if a['type'] == 'leader' and a.get('p') is not None:
        return [_v(a['p'])]
    return []


OWN_FACE_TOL = 0.5     # mm: a foot point belongs to the face if it lies in its bbox (extended by the dimension)
REANCHOR_GRID = 12     # sampling points per axis of the dimension plane for replacement foot points


def _axial_datum(a, w, feature, bbox, ray, on_contour):
    """Position dimension of a hole/shaft: a drawn axis and a real body point on the same envelope-dimension plane."""
    direction = _meas_dir(w, a['axis'])
    k = _world(direction)
    if k is None or not hasattr(ray, 'extreme_vertices') or feature.get('end') is None:
        return False
    start, middle, end = _axis_pts(feature)
    if np.linalg.norm(end-start) < TOL:
        return False
    axis = prj.unit(end-start)
    if abs(axis @ direction) > 1e-6:
        return False
    feet = [_v(a['p1']), _v(a['p2'])]
    on_axis = [i for i,p in enumerate(feet) if np.linalg.norm(np.cross(p-start, axis)) <= 0.001
               and -0.001 <= (p-start) @ axis <= np.linalg.norm(end-start)+0.001]
    if len(on_axis) != 1:
        return False
    center_id = on_axis[0]
    datum = feet[1-center_id]
    if min(abs(datum[k]-bbox[side][k]) for side in ('min','max')) > 0.0001:
        return False

    def retained(p):
        return _inside(w,p) and (w['sect'] is None or (p-w['sect']) @ w['fr'][0] <= CUT_TOL)

    center = next((p for p in (feet[center_id], start, middle, end) if retained(p) and on_contour(p)), None)
    if center is None:
        return False
    points = sorted(ray.extreme_vertices(k, datum[k]), key=lambda p: np.linalg.norm(p - datum))
    witness = next((p for p in points if retained(p) and on_contour(p) and not _covered(w,p,(),ray,direction)), None)
    if witness is None:
        return False
    feet[center_id], feet[1-center_id] = center, witness
    a['p1'], a['p2'] = (_r(p) for p in feet)
    return True


def _reanchor(a, w, feats, ray, on_contour=None):
    """A linear dimension between faces fixes only the coordinate along the dimension direction; the position of the foot points across it is free.
    If a foot point lies behind the part in the view w (ray test), it is moved to a visible point of the same dimension plane (coordinate along the dimension direction of the view) (own face of the foot point: face bbox with the point, grid
    REANCHOR_GRID^2, nearest first; the point must lie in the section, on the face (ray.on_face) and be freely visible). True if afterwards
    no foot point is hidden any more (a is changed), otherwise a stays unchanged."""
    p = [_v(a['p1']), _v(a['p2'])]
    d = _meas_dir(w, a['axis'])
    k = _world(d)
    own = next((g for g in feats if g['id'] == a.get('feature') and g['kind'] == 'slant' and g.get('normal') is not None and g.get('bbox')), None)      # slant dimension
    if not hasattr(ray, 'on_face') or (k is None and own is None):
        return False
    n = np.eye(3)[k] if k is not None else None
    def usable(p):
        if on_contour is not None:
            if w.get('sect') is not None and float((p - w['sect']) @ w['fr'][0]) > CUT_TOL:
                return False
            if not on_contour(p):
                return False
        return not _covered(w, p, feats, ray, d)

    new = []
    for q0 in p:
        if usable(q0):
            new.append(q0)
            continue
        best = None
        if on_contour is not None and hasattr(ray, 'boundaries'):
            normal = prj.unit(own['normal']) if own else n
            support = own or next((f for f in feats if f['id'] == a.get('feature') and f['kind'] == 'plane'
                                   and f.get('bbox') and f['axis_name'] == AX[k] and abs(f['coord']-q0[k]) < TOL), None)
            section = (w['sect'], w['fr'][0]) if w.get('sect') is not None else None
            best = next((q for q in ray.boundaries(q0, normal, d, support, section) if _inside(w, q) and usable(q)), None)
            if best is not None:
                new.append(best)
                continue
        if own is not None:       # the foot point moves on the face of the slant across the dimension direction (depth in the view: dimension value and image position stay), to the nearest visible point of the face
            ns = prj.unit(np.asarray(own['normal'], float))
            u = np.cross(d, ns)
            lo, hi = _v(own['bbox']['min']), _v(own['bbox']['max'])
            if float(np.linalg.norm(u)) > 1e-6:
                u, span = u / np.linalg.norm(u), float(np.linalg.norm(hi - lo)) + 2 * OWN_FACE_TOL
                for s in sorted(np.linspace(-span, span, 8 * REANCHOR_GRID + 1), key=abs):
                    q = q0 + s * u
                    if all(lo[j] - OWN_FACE_TOL <= q[j] <= hi[j] + OWN_FACE_TOL for j in range(3)) and _inside(w, q) and ray.on_face(q, ns) and usable(q):
                        best = q
                        break
            if best is None:
                return False
            new.append(best)
            continue
        for f in feats:
            if f['kind'] != 'plane' or f['axis_name'] != AX[k] or abs(f['coord'] - q0[k]) > TOL or not f.get('bbox'):
                continue
            lo, hi = f['bbox']['min'], f['bbox']['max']
            if not all(lo[j] - OWN_FACE_TOL <= q0[j] <= hi[j] + OWN_FACE_TOL for j in range(3) if j != k):
                continue                       # only the own face of the foot point, not every coplanar plane of the part
            axes = [j for j in range(3) if j != k]
            grid = [np.linspace(lo[j] + 0.05, hi[j] - 0.05, REANCHOR_GRID) if hi[j] - lo[j] > 0.1 else np.array([(lo[j] + hi[j]) / 2]) for j in axes]
            if on_contour is not None:
                grid = [np.unique(np.r_[lo[j], g, hi[j], np.clip(q0[j], lo[j], hi[j])]) for j, g in zip(axes, grid)]
            cand = []
            for x in grid[0]:
                for y in grid[1]:
                    q = q0.copy()
                    q[axes[0]], q[axes[1]] = x, y
                    cand.append(q)
            for q in sorted(cand, key=lambda q: float(np.linalg.norm(q - q0))):
                if _inside(w, q) and usable(q) and ray.on_face(q, n):
                    best = q
                    break
            if best is not None:
                break
        if best is None:
            return False
        new.append(best)
    a['p1'], a['p2'] = _r(new[0]), _r(new[1])
    return True


CUT_TOL = 0.05     # mm: anchors up to this far in front of the cutting plane still lie on the cut face
CUT_SCAN = 24      # sampling points along the free transverse axis when a foot point in the section is moved onto the cutting plane


def _to_cut(a, w, ray):
    """The view draws, of a face that the viewing direction sees only edge-on, only the edges - in a section above all the edge of the cut face
    in the cutting plane (reference_state: visible edges, section contour). A foot point anywhere on the face may nevertheless lie on a hidden piece of its edge
    (the ray test was free, the drawn edge ends 0.6 mm before). Therefore: if the face of the foot points lies behind the cutting plane, the foot point is moved
    along the viewing direction into the cutting plane and, where it no longer lies on the face there, along the free transverse axis to the nearest point that lies on it
    (ray test on_face); only across the dimension direction, the dimension coordinate stays."""
    if not hasattr(ray, 'on_face'):
        return
    N, n = np.asarray(w['fr'][0], float), _meas_dir(w, a['axis'])
    k = _world(n)
    jn = _world(N)
    if k is None or jn is None or jn == k:
        return
    j = ({0, 1, 2} - {k, jn}).pop()
    for key in ('p1', 'p2'):
        p = _v(a[key])
        t = float((np.asarray(w['sect'], float) - p) @ N)
        if t <= 0.02:
            continue
        q = p + t * N
        if ray.on_face(q, n):
            a[key] = _r(q)
            continue
        step = np.eye(3)[j]
        for d in sorted(np.linspace(-60.0, 60.0, 2 * CUT_SCAN + 1), key=abs):
            if d and ray.on_face(q + d * step, n):
                a[key] = _r(q + d * step)
                break


def _visibility(items, ws, feats, ray=None, by_id=None, notes=None, pid=None):
    """Visibility check per counted annotation: if an anchor (foot point, centre, tip) lies in the own view behind the part
    itself (ray test ray for anchors on faces, otherwise _covered: axis-parallel planes), i.e. on a hidden edge, the annotation moves
    into another view that shows the anchor visibly (_moves). The budget applies (checks.BUDGET_VIEW): preferably the least loaded
    view under the budget; if none is under the budget, the annotation stays (no new view from here, better a dimension on a hidden
    edge than none). items as in _dedupe."""
    wby = {w['id']: w for w in ws}
    load = {}
    for _, a, _, _ in items:
        if a['type'] in checks.BUDGET_TYPES and a.get('view') in wby:
            load[a['view']] = load.get(a['view'], 0) + 1
    order = {w['id']: n for n, w in enumerate(ws)}
    for _, a, _, _ in items:
        w = wby.get(a.get('view'))
        pts = _anchor_points(a)
        if w is None or not pts:
            continue
        use = _surface_anchor(a, by_id)
        if use and ray is not None and w.get('sect') is not None and w.get('cut') is None:
            _to_cut(a, w, ray)
        if notes is not None and w.get('sect') is not None and a['type'] == 'linear' and '#extent_' in str(a.get('feature')) and any(
                float((p - np.asarray(w['sect'], float)) @ np.asarray(w['fr'][0], float)) > CUT_TOL for p in pts):
            notes.append({BUDGET_NOTE: f"Part {pid}: envelope dimension {a['feature'].split('#')[-1]} in view {a['view']}: a foot lies in the cut-away part, no edge of the view carries it "
                                       '(no other view shows the dimension); extension line runs up to there', 'excess': 0, 'needs_profile_view': True,
                                       'axis': AX.index(str(a['feature'])[-1])})
        if not _hidden(a, w, feats, ray, by_id):
            continue
        _ok = use and ray is not None and _reanchor(a, w, feats, ray)
        if _ok:          # foot points on a visible place of the same dimension planes: the dimension stays in its view
            continue
        moves = [(load.get(v['id'], 0), order[v['id']], v, patch) for v, patch in _moves(a, ws, feats, ray, by_id)
                 if load.get(v['id'], 0) < checks.BUDGET_VIEW]
        if moves:
            _, _, v, patch = min(moves, key=lambda m: m[:2])
            load[a['view']] -= 1
            load[v['id']] = load.get(v['id'], 0) + 1
            a.update(patch)
    return items


def _why(a, ws, feats, load, ray=None, by_id=None):
    """Reason why annotation a cannot leave its view (for the planner note of the budget)."""
    if a['type'] not in ('linear', 'diameter', 'leader') or (a['type'] == 'linear' and (a.get('axis') not in ('h', 'v') or 'dir' in a)):
        return {'angle': 'angle dimension (bound to its view)', 'radius': 'R dimension (bound to its view)',
                'thread': 'thread (bound to its view)'}.get(a['type'], 'dimension oblique to the image axis (true length only in this view)')
    fids = [a['feature']] if a.get('feature') else list(a.get('features') or [])[:1]
    others = [w for w in ws if w['id'] != a['view']]
    if not any(fid in w['shows'] for w in others for fid in fids):
        return f"{ {'linear': 'length dimension', 'diameter': 'diameter dimension', 'leader': 'leader line'}[a['type']] } - no other view of the part shows the element"
    if _moves(a, ws, feats, ray, by_id):
        return 'movable, but the target views are full (budget reached)'
    return f"{ {'linear': 'length dimension', 'diameter': 'diameter dimension', 'leader': 'leader line'}[a['type']] } - hidden in the other views, outside the cut-out or not in the image direction"


def _budget(items, ws, feats, notes, pid, ray=None, by_id=None):
    """Hard stacking budget per view (checks.BUDGET_VIEW): surplus statements move into the view with the lowest load that shows the
    element legibly (_moves; the later dimensions first - form, diameter, note statements, before the basic dimensions); never dropped, never from A to C
    (nothing is deleted, the coverage stays). If the budget cannot be held this way, notes gets a planner note with view, number and
    reason (BUDGET_NOTE); a new view is created only by plan._content (plan_views.extra_view) if no existing view shows the element. items as in _dedupe."""
    wby = {w['id']: w for w in ws}
    counted = lambda a: a['type'] in checks.BUDGET_TYPES and a.get('view') in wby
    load = {}
    for _, a, _, _ in items:
        if counted(a):
            load[a['view']] = load.get(a['view'], 0) + 1
    order = {w['id']: n for n, w in enumerate(ws)}
    for vid in sorted((v for v in load if load[v] > checks.BUDGET_VIEW), key=lambda v: (-load[v], order[v])):
        while load[vid] > checks.BUDGET_VIEW:
            best = None
            for n, (_, a, _, _) in enumerate(items):
                if a.get('view') != vid or not counted(a):
                    continue
                for w, patch in _moves(a, ws, feats, ray, by_id):
                    if load.get(w['id'], 0) < checks.BUDGET_VIEW:
                        rank = (load.get(w['id'], 0), -n, order[w['id']])
                        if best is None or rank < best[0]:
                            best = (rank, a, w, patch)
            if best is None:
                break
            _, a, w, patch = best
            a.update(patch)
            load[vid] -= 1
            load[w['id']] = load.get(w['id'], 0) + 1
        if load[vid] > checks.BUDGET_VIEW:
            why = collections.Counter()
            for _, a, _, _ in items:
                if a.get('view') == vid and counted(a):
                    why[_why(a, ws, feats, load, ray, by_id)] += 1
            reasons = '; '.join(f'{n}× {text}' for text, n in sorted(why.items(), key=lambda t: (-t[1], t[0])))
            notes.append({BUDGET_NOTE: f'Part {pid}: view {vid} carries {load[vid]} statements (budget {checks.BUDGET_VIEW}): {reasons}',
                          'excess': load[vid] - checks.BUDGET_VIEW})
    return items


def _dedupe(items, ws):
    """The same dimension (kind, form feature or reference points, value) only once per part: the view with the largest scale
    (preferably where the element is visible) keeps it; items = [(key, annotation, value, kind)], order of the others stays."""
    rank = {w['id']: n for n, w in enumerate(ws)}
    wby = {w['id']: w for w in ws}
    r3 = lambda p: tuple(round(float(x), 3) + 0.0 for x in p)
    # centre crosses arise per view and carry neither value nor text: the view belongs to the key
    ident = lambda a, v: (a['type'], a.get('feature') or tuple(a.get('features') or ()), None if v is None else round(float(v), 3),
                          a.get('prefix'), a.get('text'), (frozenset({r3(a['p1']), r3(a['p2'])}), a.get('axis')) if a['type'] == 'linear' else None,      # the dimension direction belongs to it (Δx and Δy between the same members)
                          a.get('view') if a['type'] == 'centermark' else None)

    def pref(a):                 # visible first, then largest scale, then rank in ws
        w = wby.get(a.get('view')) or {}
        fs = [a['feature']] if a.get('feature') else list(a.get('features') or ())
        seen = bool(fs) and all(f in (w.get('shows') or ()) for f in fs)
        return (not seen, -float(w.get('k') or 0.0), rank.get(a.get('view'), len(rank)))
    best = {}
    for n, (_, a, v, _) in enumerate(items):
        k = ident(a, v)
        if k not in best or pref(a) < pref(items[best[k]][1]):
            best[k] = n
    return [it for n, it in enumerate(items) if best[ident(it[1], it[2])] == n]


def engraving_note(g):
    """Note line of an engraving (the font has no curly quotes: ASCII quotation marks): text or "per 3D data set" (SVG emblem),
    direction as "recessed"/"raised" only if it is measured (adopted pockets/raised areas, absorbs), depth or
    height always as an absolute value (no sign from the operator semantics)."""
    depth = abs(float(g['depth'])) if g.get('depth') not in (None, 0) else None
    word = ' raised' if g.get('raised') else ' recessed' if g.get('absorbs') else ''
    return ((f"Engraving \"{g['text']}\"" if g.get('text') else ENGRAVING_EMBLEM_TEXT) + word
            + (f", text height {fmt(g['cap_height'])}" if g.get('cap_height') else '')
            + (f", {'height' if g.get('raised') else 'depth'} {fmt(depth)}" if depth else ''))


# ------------------------------------------------------------------ Item numbers

BALLOON_EVAL = 8           # at most this many candidates per part are checked for distance to foreign edges


def balloons(assembly_view, parts, positions, visible_pts, clear=None, want=0.0):
    """Per part with an item number one item number at the visible point that lies farthest from the view centre
    (centre of the bounding box of all parts of the view in the image plane); leader line radially outward, LEADER_MM
    long. If its angle lies closer than STEP_DEG to one already set, it is turned on by STEP_DEG.
    With clear(part, point) -> distance (model mm) of the point to everything that is not the part itself, the planner takes the
    farthest point with distance >= want, otherwise the one with the greatest distance (per part at most BALLOON_EVAL checked candidates, farthest
    first); a free-standing part thus keeps its point."""
    fr = prj.frame_of(assembly_view)
    ids = assembly_view.get('parts') or [p['id'] for p in parts]
    corners = [[x, y, z] for p in parts if p['id'] in ids and p.get('bbox') for b in [p['bbox']]
               for x in (b['min'][0], b['max'][0]) for y in (b['min'][1], b['max'][1]) for z in (b['min'][2], b['max'][2])]
    corners = corners or [q for pid in ids for q in visible_pts.get(pid) or []]
    if not corners:
        return []
    q = prj.project(corners, fr)
    center = (q.min(0) + q.max(0)) / 2
    out, used = [], []
    for p in parts:
        pid = p['id']
        if pid not in ids or pid not in positions or not visible_pts.get(pid):
            continue
        pts = _v(visible_pts[pid])
        d2 = prj.project(pts, fr) - center
        far = np.linalg.norm(d2, axis=1)
        i = int(np.argmax(far))
        best = None
        if clear is not None:
            best = -1.0
            for j in [int(x) for x in np.argsort(-far, kind='stable')][:BALLOON_EVAL]:
                c = clear(pid, pts[j])
                if c > best + 1e-9:
                    best, i = c, j
                if c >= want - 1e-9:
                    break
        ang = float(np.degrees(np.arctan2(d2[i][1], d2[i][0]))) if np.linalg.norm(d2[i]) > 1e-9 else 45.0
        for _ in range(int(360 / STEP_DEG)):
            if all(abs((ang - u + 180) % 360 - 180) >= STEP_DEG - 1e-9 for u in used):
                break
            ang += STEP_DEG
        used.append(ang)
        t = np.radians(ang)
        out.append({'type': 'balloon', 'view': assembly_view['id'], 'p': _r(pts[i]), 'part': pid, 'pos': str(positions[pid]),
                    'to': [round(LEADER_MM * float(np.cos(t)), 6), round(LEADER_MM * float(np.sin(t)), 6)],
                    **({'_c': best} if best is not None else {})})      # _c: distance of the tip to foreign parts (model mm), only for the planner
    return out
