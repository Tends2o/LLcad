"""Planner II: views, sections and detail views per part and for the assembly.

ViewPlan = {id, part (None = assembly), preset | direction + up, role, features, ...} with these rules:
- direction = unit vector from the model to the camera (like projection.PRESETS and frame_of), up = image y.
  preset is only set if a preset has exactly this frame (viewing direction and image x).
- The first view of a part is its anchor: the main view (role 'main') or the section that replaces it
  (turned part). Further views are neighbors of the anchor by first-angle projection: viewing direction -Vx (to the right),
  +Vx (to the left), +Vy (below), -Vy (above); role 'side' or 'top'.
- section = {point, label}: cutting plane through point, normal = viewing direction; section_line_in = view in which the
  plane appears as a line. Enlarged cross-sections additionally carry scale ('a:b'); they take the frame of a
  neighbor if one looks along the bar axis, otherwise direction = bar axis, up = viewing direction of the anchor.
- detail = {center, radius (model mm), label, scale}: circle in the view detail_circle_in; same frame and
  same section as that one.
- features = ids of the form features the view shows (unhidden or exposed in the section).
- auxiliary = {label}: auxiliary view along an oblique axis (ISO 128-3), role 'auxiliary'; arrow_in = view
  with the viewing arrow, arrow_at = center of the form features (target of the arrow); aux_members = these form features, features =
  those of them visible from the viewing direction (features.visible_along). If none is visible: auxiliary section across the axis
  through its center (additionally section {point, label}, section_line_in instead of arrow_in/arrow_at; features = all)."""
import numpy as np
import analyze as an
import projection as prj
import checks
import profiles
import plan_layout as pl
from checks import MIN_FEATURE_MM, SMALL_KINDS, collective_family, end_chamfer
import json
from plan_dims import oblique_reference

VIEWS = ('front', 'top', 'left', 'back', 'bottom', 'right')   # precedence on a tie
SMALL_MM = 5.0         # smaller on paper -> detail view or cross-section (square bar: MIN_FEATURE_MM)
DETAIL_MM = 10.0       # the form feature should become at least this large there
DETAIL_MAX_R = 30.0    # largest detail circle (radius on paper, mm)
EXEMPLAR_MAX = 2       # at most this many detail views of pattern member exemplars per part (in addition to MAX_ENLARGED), one per pattern
CLUSTER_MAX = 2        # at most this many detail views of clusters of individually required slants per part
CLUSTER_MIN = 4        # cluster = at least this many slants ...
CLUSTER_R = 6.0        # ... within at most this many mm (model) of the first one (radius of the detail view at 5:1 at most DETAIL_MAX_R)
EXEMPLAR_MIN_SCALE = 5.0   # enlargement of the exemplar detail view at least 5:1 (standard scale by smallest characteristic length: 5:1, 10:1, 20:1)
MAX_ENLARGED = 4       # at most this many detail views/cross-sections per part
PATTERN_DETAIL_MAX = 2       # N4.9: at most this many pattern excerpts per part (own quota, in addition to MAX_ENLARGED, EXEMPLAR_MAX and CLUSTER_MAX)
PATTERN_DETAIL_MAX_R = 100.0  # N4.9: own cap of the pattern excerpt (radius on paper, mm); DETAIL_MAX_R = 30 is not enough for a whole pattern envelope (e.g. 70 mm at 2:1)
PROFILE_DETAIL_MAX_R = 60.0   # N4.10: own cap of the profile detail view (radius on paper, mm); DETAIL_MAX_R = 30 is not enough for Ø9.2 at 10:1 (55 mm)
PROFILE_SECTION_MAX_MM = 260.0  # N4.10: longest side of the profile section on paper (partial envelope · scale); above it the section stays at the sheet scale
PROFILE_MARGIN = 0.1         # margin of the detail circle over the profile outline (share) plus 0.5 mm
PATTERN_MARGIN = 0.03        # N4.9: margin of the circle over the envelope of all members (share of the radius) plus 0.5 mm
WIRE_SNAP_DEG = 10.0   # round the bend plane normal to a preset up to this much deviation
WIRE_FLAT = 0.05       # residual distance of the bend points from the plane above which the wire is not flat (share of the size)
AUX_MAX = 2            # at most this many auxiliary views (oblique axes) per part
AXIAL = ('hole', 'shaft', 'arc')   # form features with an axis whose characteristic dimensions need a view along/across the axis
TOL = 1e-3
READABLE_MM = SMALL_MM   # a detail view is only needed if a characteristic length at the sheet scale is below it (= minimum size, plan_layout.MIN_FEATURE_MM)


def _r(v, n=6):
    return [round(float(x), n) + 0.0 for x in v]


def _frame(N, Vx):
    N, Vx = prj.unit(N), prj.unit(Vx)
    return N, Vx, np.cross(N, Vx)


def _pframe(p):
    return _frame(*prj.PRESETS[p])


def _spec(fr):
    for p in VIEWS:
        n, x = prj.PRESETS[p]
        if np.allclose(prj.unit(n), fr[0], atol=1e-9) and np.allclose(prj.unit(x), fr[1], atol=1e-9):
            return {'preset': p}
    return {'direction': _r(fr[0], 12), 'up': _r(fr[2], 12)}   # precise enough for the alignment in the core (1e-9)


def _dir(N):
    """Preset with this viewing direction (or the nearest one) for visible_from."""
    return max(VIEWS, key=lambda p: float(prj.unit(prj.PRESETS[p][0]) @ N))


def _neighbours(fr):
    """(role, frame) of the neighbor views by method 1: from the left, from above, from the right, from below."""
    N, Vx, Vy = fr
    return [('side', _frame(-Vx, N)), ('top', _frame(Vy, Vx)), ('side', _frame(Vx, -N)), ('top', _frame(-Vy, Vx))]


def _vis(f):
    """An empty visible_from means: no probe points found (narrow/trimmed face) - unknown, not hidden."""
    if '_edge_views' in f:
        return f['_edge_views']
    return f.get('visible_from') or VIEWS


def _canon(v):
    v = np.asarray(v, float)
    return v if v[int(np.argmax(np.abs(v)))] > 0 else -v


def _mid(f):
    if f.get('start') is not None:
        return (np.array(f['start'], float) + np.array(f['end'], float)) / 2
    if f['kind'] in ('slant', 'notch') and f.get('pt'):        # point on the slanted face; centre of the notch opening
        return np.array(f['pt'], float)
    if f['kind'] == 'cone':             # center of the cone axis between the two ends
        t = np.tan(np.radians(float(f['half_angle_deg'])))
        return np.array(f['apex'], float) + prj.unit(f['axis']) * (float(f['d1']) + float(f['d2'])) / 4 / t
    return np.array(f['center'], float)


def _points(f, by_id):
    """Reference points of a form feature; [] = never cut away entirely."""
    if f['kind'] == 'pattern' or f.get('members') is not None:      # patterns and composite elements: their members
        return [p for m in f['members'] if m in by_id for p in _points(by_id[m], by_id)]
    if f['kind'] == 'plane':
        return [f['pt']]
    if f.get('at'):
        return [f['at']['center']]
    if f['kind'] in ('slant', 'chamfer', 'cone', 'notch') and f.get('pt'):      # face without a circular edge; notch
        return [f['pt']]
    return [f[k] for k in ('start', 'end', 'center', 'apex') if f.get(k) is not None]


def _round(f, by_id):
    """Small round element (hole, pin, sphere) or a pattern of them: on its own no context for a detail view."""
    if f['kind'] == 'pattern' or f.get('members') is not None:
        return all(_round(by_id[m], by_id) for m in f.get('members') or [] if m in by_id)
    return f['kind'] in ROUND_KINDS


def _span(f, N):
    """(min, max) of the face bbox of a plane along N; None without bbox (older feature dicts)."""
    if not f.get('bbox'):
        return None
    lo, hi = f['bbox']['min'], f['bbox']['max']
    q = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])], float) @ N
    return float(q.min()), float(q.max())


def _profile_windows(f, N):
    """Section positions [(from, to)] along N at which both ends of the complete slant profile lie on the trimmed face: convex face from the outline; non-convex
    face (cut-outs) from the edges of the profile ends (plan._profile_spans, extrusion along a world axis), otherwise none."""
    if f.get('normal') is None or abs(np.asarray(f['normal']) @ N) > 1e-5:
        return []
    if f.get('_convex_profile') and f.get('_outline'):
        points = np.asarray(f['_outline'])
        station = points @ prj.unit(np.cross(N, f['normal']))
        depths = [points[np.abs(station-end) < 1e-4] @ N for end in (station.min(),station.max())]
        low, high = max(depth.min() for depth in depths), min(depth.max() for depth in depths)
        return [(low, high)] if high-low > 2*TOL else []
    k, spans = f.get('_profile_axis'), f.get('_profile_spans')
    if spans and k is not None and abs(abs(float(N[k])) - 1) < 1e-9:
        return [tuple(sorted((float(N[k]) * lo, float(N[k]) * hi))) for lo, hi in spans]
    return []


def _profile_window(f, N):
    """The longest section position from _profile_windows (position of a new section, sharing of an existing one) or None."""
    return max(_profile_windows(f, N), key=lambda w: w[1] - w[0], default=None)


def _shows(f, fr, cut, by_id):
    N = fr[0]
    if cut is not None and f['kind'] == 'slant':
        if any(w[0]+TOL < cut @ N < w[1]-TOL for w in _profile_windows(f, N)):
            return True
    span = _span(f, N) if cut is not None and f['kind'] == 'plane' else None
    if span is not None:                               # position of the face relative to the cutting plane
        lo, hi = span[0] - cut @ N, span[1] - cut @ N
        if lo < -TOL and hi > TOL:
            return True                                # the cutting plane hits the face: exposed
        if lo > -TOL:
            return False                               # entirely on the cut-away camera side
    if _dir(N) not in _vis(f):
        return False
    if cut is None or span is not None:
        return True
    if f['kind'] == 'plane' and abs(np.eye(3)['xyz'.index(f['axis_name'])] @ N) < 0.5:
        return True                                    # without bbox: plane as an edge extends beyond the cutting plane
    pts = _points(f, by_id)
    return not pts or not all((np.array(p, float) - cut) @ N > TOL for p in pts)


def _hidden(feats, bb):
    """(blind-hole groups, inner planes). Group = blind holes of one pattern (one suffices) or a single one.
    Inner plane: not on the bounding box and visible from no direction in which it appears as an edge; N4.1: no minor face (minor, line "Undimensioned
    forms") - it does not require a section (engraving floors would otherwise attract two sections of their own)."""
    by_id = {f['id']: f for f in feats}
    groups, seen = [], set()
    for pat in (f for f in feats if f['kind'] == 'pattern'):
        mem = [by_id[m] for m in pat['members'] if m in by_id and by_id[m].get('blind')]
        if mem:
            groups.append(mem)
            seen |= {m['id'] for m in mem}
    groups += [[f] for f in feats if f['kind'] == 'hole' and f.get('blind') and f['id'] not in seen]
    groups += [[f] for f in feats if f['kind'] == 'sphere' and f.get('internal')]
    for f in feats:       # a shaft cone that no normal view across the axis shows visibly gets a section through its axis
        if f['kind'] == 'cone' and f.get('axis') is not None and not (f.get('minor') or f.get('countersink') or f.get('drill_tip')
                                                                        or f.get('via_member') or end_chamfer(f)):
            a = prj.unit(f['axis'])
            internal = f.get('free') or f.get('host_hole')      # internal cone: only a section through the axis shows its profile
            if internal or not any(abs(float(prj.unit(prj.PRESETS[p][0]) @ a)) < 1e-3 and p in _vis(f) for p in VIEWS):
                groups.append([f])
    planes = []
    for f in feats:
        if f['kind'] != 'plane' or f.get('minor'):
            continue
        i = 'xyz'.index(f['axis_name'])
        if min(abs(f['coord'] - bb['min'][i]), abs(f['coord'] - bb['max'][i])) < TOL:
            continue
        if not {p for p in VIEWS if abs(prj.PRESETS[p][0][i]) < 0.5} & set(_vis(f)):
            planes.append(f)
    return groups, planes


def reduce_cuts(covers):
    """Indices of the sections that stay: smallest set that covers the same elements as all. covers = per section
    the set of elements its plane shows (order of the greedy choice). In turn, smallest set first
    (on a tie the later one), a section is dropped if its elements are already covered by the rest; a section stays only
    if it covers an element that no other covers."""
    keep = list(range(len(covers)))
    for i in sorted(range(len(covers)), key=lambda i: (len(covers[i]), -i)):
        rest = set().union(*[covers[j] for j in keep if j != i])
        if covers[i] <= rest:
            keep.remove(i)
    return keep


def _cuts(groups, planes, cands, rank, bb, axis_pt=None):
    """[[frame, point, ids, coverage]]: greedily per section the position (candidate frame, coordinate along the viewing direction) that shows the
    most hidden elements. Blind-hole group: one member with its axis in the image plane (otherwise as close as possible)
    exactly on the cutting plane. Inner plane: as an edge, and the cutting plane meets its face (bbox); positions from
    the hole axes and the face centres. Planes without bbox: every direction in which they are an edge (bounding-box centre).
    N4.1: axis_pt = point on the rotation axis of a turned part (checks.part_axis; all cands contain the axis): the position through the axis comes
    first and wins on equal coverage (e.g. the middle of a thread run-out step lay 3.96 mm beside the axis)."""
    def ok(h, fr):
        if h['kind'] == 'sphere' and not h.get('profile_axis'):
            return True
        a = np.array(h.get('profile_axis') or h['axis'], float)
        return abs(a @ fr[0]) <= min(abs(a @ c[0]) for c in cands) + TOL

    def edge(p, fr):
        return abs(np.eye(3)['xyz'.index(p['axis_name'])] @ fr[0]) < TOL

    def hits(p, fr, c):
        span = _span(p, fr[0])
        return edge(p, fr) and (span is None or span[0] + TOL < c < span[1] - TOL)
    options = [(i, np.asarray(axis_pt, float)) for i in range(len(cands))] if axis_pt is not None else []
    options += [(i, _mid(h)) for g in groups for h in g for i, fr in enumerate(cands) if ok(h, fr)]
    for p in planes:
        b = p.get('bbox') or bb
        options += [(i, (np.array(b['min'], float) + np.array(b['max'], float)) / 2) for i, fr in enumerate(cands) if edge(p, fr)]
    def cover(fr, pt):
        """Everything that the cutting plane through pt in viewing direction fr shows: blind hole groups (key = id of the first member) and planes."""
        c = pt @ fr[0]
        got = {g[0]['id'] for g in groups if any(ok(x, fr) and abs(_mid(x) @ fr[0] - c) < TOL for x in g)}
        return got | {p['id'] for p in planes if hits(p, fr, c)}
    cuts, todo_g, todo_p = [], list(groups), list(planes)
    while todo_g or todo_p:
        best = None
        for j, (i, pt) in enumerate(options):
            fr = cands[i]
            c = pt @ fr[0]
            on = [x for g in todo_g for x in g if ok(x, fr) and abs(_mid(x) @ fr[0] - c) < TOL]
            gs = [g for g in todo_g if any(x in on for x in g)]
            ps = [p for p in todo_p if hits(p, fr, c)]
            if gs or ps:
                key = (-(len(gs) + len(ps)), rank[i], j)
                if best is None or key < best[0]:
                    best = (key, fr, pt, on, gs, ps)
        if best is None:
            break                                      # remaining planes: no candidate direction shows them as an edge
        _, fr, pt, on, gs, ps = best
        cuts.append([fr, pt, [x['id'] for x in on] + [p['id'] for p in ps], cover(fr, pt)])
        todo_g = [g for g in todo_g if not any(g is x for x in gs)]
        todo_p = [p for p in todo_p if not any(p is x for x in ps)]
    return cuts


def _wire_frames(part, rank):
    """Bend plane by PCA of the bend points; normal rounded to a preset (<= WIRE_SNAP_DEG), otherwise exact.
    Not flat -> second view (neighbor that shows the larger extent in the plane with the normal)."""
    bt = next(b for b in part['bend_tables'] if b.get('complete'))
    P = np.array([p['xyz'] for p in bt['points']], float)
    C = P - P.mean(0)
    n = _canon(np.linalg.svd(C)[2][-1])
    j = int(np.argmax(np.abs(n)))
    if np.degrees(np.arccos(min(1.0, abs(n[j])))) <= WIRE_SNAP_DEG:
        main = min((_pframe(p) for p in VIEWS if abs(prj.PRESETS[p][0][j]) > 0.5), key=rank)
    else:
        main = prj.frame_of({'direction': n, 'up': (0, 0, 1)})
    out = [('main', main)]
    if np.abs(C @ n).max() > WIRE_FLAT * max(part['bbox']['size']):
        side, top = _neighbours(main)[:2]
        out.append(top if np.ptp(P @ main[1]) >= np.ptp(P @ main[2]) else side)
    return out


def _turned_frames(part):
    """Frame with horizontal axis: viewing direction ⟂ axis, image x ∥ axis (preset if one fits)."""
    cyl = (part.get('shafts') or []) + (part.get('holes') or [])
    a = _canon(prj.unit(cyl[0]['axis']))
    Ns = [prj.unit(prj.PRESETS[p][0]) for p in VIEWS if abs(prj.unit(prj.PRESETS[p][0]) @ a) < TOL]
    if not Ns:
        e = np.eye(3)[int(np.argmin(np.abs(a)))]
        Ns = [prj.unit(e - (e @ a) * a)]
    return [_frame(N, s * a) for N in Ns for s in (1, -1)]


def _turned_frame(part, rank):
    return min(_turned_frames(part), key=rank)


def _bar_axis(f, cls, part, feats):
    """Direction of the bar at the form feature if a cross-section fits instead of a detail view: wire (leg of the
    bend table nearest to the form feature) or cross hole of a square bar; otherwise None."""
    c = _mid(f)
    if cls == 'wire_bend':
        P = [np.array(p['xyz'], float) for p in next(b for b in part['bend_tables'] if b.get('complete'))['points']]

        def dist(a, b):
            t = np.clip((c - a) @ (b - a) / max((b - a) @ (b - a), 1e-12), 0, 1)
            return float(np.linalg.norm(a + t * (b - a) - c))
        a, b = min(zip(P, P[1:]), key=lambda s: dist(*s))
        return prj.unit(b - a)
    if f['kind'] != 'hole':
        return None
    for sq in feats:
        if sq['kind'] == 'square':
            ax, q = prj.unit(sq['axis']), c - np.array(sq['center'], float)
            if abs(ax @ f['axis']) < TOL and np.linalg.norm(q - (q @ ax) * ax) <= sq['size'] / 2 + TOL:
                return ax
    return None


def _circle(members, fr, cap):
    """(2D center, radius) of a detail circle around the reference points of the form features plus 0.75 · largest extent.
    Larger than cap (only one element: long hole) -> radius cap, center such that the end (blind hole: bottom) lies
    in the circle at half the radius distance."""
    Q = prj.project([q for g in members for q in _points(g, {})], fr)
    c = (Q.min(0) + Q.max(0)) / 2
    ext = max(2 * g['radius'] if g['kind'] == 'arc' else g['size'] for g in members)
    r = float(np.linalg.norm(Q - c, axis=1).max()) + 0.75 * ext
    if r <= cap or len(members) > 1:
        return c, r
    f = members[0]
    if f.get('start') is not None:
        s2, e2 = prj.project([f['start'], f['end']], fr)
        if np.linalg.norm(e2 - s2) > TOL:
            c = e2 - 0.5 * cap * (e2 - s2) / np.linalg.norm(e2 - s2)
    return c, cap


def _min_circle(points):
    """(centre, radius) of the smallest circle over all points (n x 2): first the convex hull (monotone chain), then the smallest circle through two (diameter) or three (circumcircle) hull points
    that contains all of them. Few points (corner points of the members of a pattern), hence no Welzl."""
    P = sorted({(round(float(x), 9), round(float(y), 9)) for x, y in np.asarray(points, float)})
    if len(P) == 1:
        return np.array(P[0]), 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for q in P:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    for q in reversed(P):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    H = np.array(lower[:-1] + upper[:-1], float)
    best = None
    covers = lambda c, r: bool(np.all(np.linalg.norm(H - c, axis=1) <= r + 1e-7))
    for i in range(len(H)):
        for j in range(i + 1, len(H)):
            c = (H[i] + H[j]) / 2
            r = float(np.linalg.norm(H[i] - c))
            if (best is None or r < best[1]) and covers(c, r):
                best = (c, r)
    for i in range(len(H)):
        for j in range(i + 1, len(H)):
            for l in range(j + 1, len(H)):
                a, b, c3 = H[i], H[j], H[l]
                d = 2 * (a[0] * (b[1] - c3[1]) + b[0] * (c3[1] - a[1]) + c3[0] * (a[1] - b[1]))
                if abs(d) < 1e-12:
                    continue
                ux = ((a @ a) * (b[1] - c3[1]) + (b @ b) * (c3[1] - a[1]) + (c3 @ c3) * (a[1] - b[1])) / d
                uy = ((a @ a) * (c3[0] - b[0]) + (b @ b) * (a[0] - c3[0]) + (c3 @ c3) * (b[0] - a[0])) / d
                c = np.array([ux, uy])
                r = float(np.linalg.norm(a - c))
                if (best is None or r < best[1]) and covers(c, r):
                    best = (c, r)
    return best


def _world(d):
    return float(np.abs(d).max()) > 1 - 1e-6


def _oblique_axis(f):
    """Canonical unit axis of a hole/shaft/arc with an oblique axis (not a world axis), otherwise None;
    roundings/corner radii (collective note) not."""
    if f['kind'] not in AXIAL or f.get('axis') is None or collective_family(f) is not None:
        return None
    A = prj.unit(np.asarray(f['axis'], float))
    return None if float(np.abs(A).max()) >= 1 - TOL else _canon(A)        # otherwise the axis counts as axis-parallel


def _seen_along(f, N):
    """Does the viewing direction N (along the axis of f) show the form feature? features: visible_along (rays along the
    axis); if missing (older cache), it counts as visible."""
    if f.get('visible_along') is None:
        return True
    return int(np.sign(float(np.asarray(f['axis'], float) @ N))) in f['visible_along']


def _aux_frame(A, members, bb):
    """Frame of an auxiliary view along A: camera side from which most elements are visible (visible_along),
    then the one from which the nearest preset sees them (visible_from), then the one with less material in front of them, finally the
    preset order; image y = projection of the world axis z (or y), so the part edges across it lie horizontal (calm
    placement without arbitrary rotation angle)."""
    corners = np.array([[x, y, z] for x in (bb['min'][0], bb['max'][0]) for y in (bb['min'][1], bb['max'][1])
                        for z in (bb['min'][2], bb['max'][2])], float)
    mid = np.mean([_mid(f) for f in members], 0)

    def key(N):
        along = sum(1 for f in members if _seen_along(f, N))
        seen = sum(1 for f in members if _dir(N) in _vis(f))
        return -along, -seen, round(float((corners @ N).max() - mid @ N), 6), VIEWS.index(_dir(N))
    N = min((A, -A), key=key)
    e = next(e for e in np.eye(3)[::-1] if abs(e @ N) < 0.9)
    return prj.frame_of({'direction': N, 'up': prj.unit(e - (e @ N) * N)})


def _aux_views(feats, used, bb):
    """[(frame, form features)] of the auxiliary views for the viewing directions used of the existing views: holes,
    shafts and arcs with an oblique axis whose characteristic dimensions no view shows true otherwise - arc: no view along the
    axis (only there R is true); hole/shaft: none along the axis and none across it (Ø), or the position across the
    axis can be tied to a part edge there (plan_dims.oblique_reference; turned part without part edge: the position follows
    from the symmetry, no view). Parallel axes share a view; more than AUX_MAX directions: none (not
    unambiguous - the elements stay uncovered as a finding). Order: more elements first, then the axis."""
    groups = []
    for f in feats:
        A = _oblique_axis(f)
        if A is None:
            continue
        g = next((g for g in groups if abs(g[0] @ A) > 1 - TOL), None)
        if g is None:
            groups.append((A, [f]))
        else:
            g[1].append(f)
    if len(groups) > AUX_MAX:
        return []
    out = []
    for A, mem in sorted(groups, key=lambda g: (-len(g[1]), tuple(np.round(g[0], 6)))):
        if any(abs(N @ A) > 1 - TOL for N in used):
            continue
        fr = _aux_frame(A, mem, bb)
        cross = any(abs(N @ A) < TOL for N in used)
        refs = all(oblique_reference(feats, fr[0], d, _mid(mem[0])) is not None for d in fr[1:] if not _world(d))
        if any(f['kind'] == 'arc' for f in mem) or not cross or refs:
            out.append((fr, mem))
    return out


def _detail_scale(size):
    for s in sorted(an.SCALES, key=lambda s: s[0] / s[1]):
        if size * s[0] / s[1] >= DETAIL_MM:
            return s
    return max(an.SCALES, key=lambda s: s[0] / s[1])


def _ranking(part, feats):
    """(rank, info) of the main view choice: info = projected area and number of form features shown, each normalized to the
    maximum over the presets (equally weighted, 0..2); rank sorts by info, then preset frames first."""
    bb = part['bbox']

    def area(fr):
        corners = [[x, y, z] for x in (bb['min'][0], bb['max'][0]) for y in (bb['min'][1], bb['max'][1])
                   for z in (bb['min'][2], bb['max'][2])]
        q = prj.project(corners, fr)
        return float(np.prod(q.max(0) - q.min(0)))
    amax = max(area(_pframe(p)) for p in VIEWS) or 1.0

    def count(fr):   # planes only where they appear as a face (normal ∥ viewing direction), not as an edge
        N, p = fr[0], _dir(fr[0])
        return sum(1 for f in feats if p in _vis(f)
                   and (f['kind'] != 'plane' or abs(np.eye(3)['xyz'.index(f['axis_name'])] @ N) > 0.5))

    cmax = max(count(_pframe(p)) for p in VIEWS) or 1

    def info(fr):
        return area(fr) / amax + count(fr) / cmax

    def rank(fr):
        return -round(info(fr), 9), 'preset' not in _spec(fr)
    return rank, info


def _kenn(f):
    """Characteristic length of a form feature (model mm) or None if unknown: size, for slants the smaller face (checks.kenn_length)."""
    return checks.kenn_length(f)


def _coverage(res):
    """What a dimensioning result (annotations, notes, tables) covers: pairs (form feature, dimension kind) and note texts."""
    anns, notes = res[0], res[1]
    return ({(f, a['type']) for a in anns if a.get('feature') and a['type'] != 'centermark' and not checks.bare_polyline(a) for f in [a['feature']] + list(a.get('also') or [])},      # n× also covers the twins (also); thin auxiliary lines carry no coverage
            {json.dumps(n, sort_keys=True, ensure_ascii=False) for n in notes if not (isinstance(n, dict) and 'plan_note' in n)})      # planner notes (budget) are no coverage


ROUND_KINDS = ('hole', 'shaft', 'sphere')   # small round elements alone give no context for a detail view
CONTEXT_GROW = 3.0     # the detail circle grows to at most three times its size to include context (another element)
CONTEXT_MARGIN = 0.5   # margin over the distance to the next element (share of the previous radius)
PROBES_PER_PART = 4    # at most this many new probes (re-dimensionings) of the weight rule per part
SOFT_MM = checks.SOFT_MM   # soft: characteristic length of the element in the other view below X_MIN_MM (plan_layout)
PLAIN_ROLES = ('side', 'back', 'top', 'top2', 'bottom', 'right')      # normal views (not anchor, isometric, wire view) that are dropped without an element of their own


def prune_views(views, feats, k, redim, notes=None, extents=None):
    """(views, result of redim, [(id, reason)]): detail views and sections without a covered element of their own are dropped.
    redim(views) -> (annotations, notes, tables) re-dimensions the part. Detail view: dropped if it carries no
    annotation of its own, or if all its elements have at least READABLE_MM characteristic length at the sheet scale k (the
    enlargement is not needed, a single Ø then sits at the parent view); section: if it carries no own annotation
    and is not host of a remaining detail view; auxiliary view: likewise if its dimensions sit as n× in another view. Anchor (first view) and isometric views stay.
    Every removal is tried with redim and only counts if all (form feature, dimension kind) pairs and notes are still covered
    afterwards - the coverage never drops. Sections and normal views only stay with weight, computed statically from the features lists of the other
    views (section_weight): an element of its own is hard if no other view shows it visibly (slant: in true length, cone: with the
    axis in the image plane), soft if one shows it, but budget or X_MIN would be violated there. With one hard or two soft elements
    the view stays without a probe, otherwise exactly one probe (removal + re-dimensioning: coverage, stack budget, dimensions on elements not shown) confirms that the dimensions
    migrate legibly. Statically replaceable views (hard 0, soft 0) are always probed; for the others (one soft element) at most PROBES_PER_PART new probes apply, the views with the fewest
    elements first, the rest is listed as "not checked (probe budget)" in notes (list, optional); a refused probe of a statically replaceable view is listed there with the reason (coverage, stack budget, hidden, collision). The tightness of the target view is also soft: stack budget, density Y (only with extents(views) -> {id: {w, h}}) or
    characteristic length X. The probe only accepts a removal if the collision load (pl.collisions: near anchors, short dimensions; planner estimate of
    text_overlap/text_on_line) does not rise either. n× covers the twins (also).
    N4.1: an axial section of a turned part (section without a label letter, checks.axis_section) shows the coaxial elements (checks.coaxial) in true size: in the weight
    it counts for them as a showing view, and the probe for removing another view adds their coaxial elements to its features (the dimensions visibly migrate there)."""
    by_id = {f['id']: f for f in feats}
    memo, probes = {}, [PROBES_PER_PART]
    unlabeled = [v for v in views if isinstance(v.get('section'), dict) and not v['section'].get('label') and not v.get('detail')]      # only axial sections carry no label letter
    axis = checks.part_axis(feats) if unlabeled else None
    axial = {v['id'] for v in unlabeled if axis is not None and checks.axis_section(v['section'].get('point'), prj.frame_of(v)[0], feats)}

    def key(vs):
        return tuple((o['id'], len(o.get('features') or ())) for o in vs)

    def redo(vs):          # the same set of views is re-dimensioned in several passes - compute once
        if key(vs) not in memo:
            memo[key(vs)] = redim(vs)
        return memo[key(vs)]
    cur, res, dropped = list(views), redo(list(views)), []
    unchecked, replaceable, refused = [], set(), {}

    def mine(v):
        return [a for a in res[0] if a.get('view') == v['id'] and a['type'] != 'centermark' and not checks.bare_polyline(a)]      # polylines without a dimension (datum line, extension) do not make a view informative

    def hosted(v):
        return any(o is not v and v['id'] in (o.get('detail_circle_in'), o.get('section_line_in'), o.get('arrow_in')) for o in cur)

    def excess(r):
        return sum(n.get('excess', 0) for n in r[1] if isinstance(n, dict) and 'plan_note' in n)

    def elements(own):
        """Form features (id -> number of their dimensions) that carry the dimensions of the view."""
        out = {}
        for a in own:
            for f in ([a['feature']] if a.get('feature') else list(a.get('features') or ())):
                out[f] = out.get(f, 0) + 1
        return out

    def unseen(r, views):
        """Number of dimensions (length, Ø, R, angle) whose element the view does not show visibly (dimension at a hidden edge, not legible)."""
        shows = {o['id']: set(o.get('features') or ()) for o in views}
        return sum(1 for a in r[0] if a.get('type') in ('linear', 'diameter', 'radius', 'angle') and a.get('view') in shows
                   and any(i not in shows[a['view']] for i in ([a['feature']] if a.get('feature') else list(a.get('features') or ()))))

    def section_weight(v, own):
        """(hard, soft) of the own elements of v, static: other remaining views (without detail views) and their features lists. The computation lives once in checks.view_weight (the planner and the
        view_replaceable check share it); here only the tightness of the target view from the planner's dimensions: budget, density Y or characteristic length X."""
        others = [o for o in cur if o is not v and not o.get('detail')]
        load, total = {}, {}
        for a in res[0]:
            total[a.get('view')] = total.get(a.get('view'), 0) + 1
            if a.get('type') in checks.BUDGET_TYPES:
                load[a.get('view')] = load.get(a.get('view'), 0) + 1

        def tight(o, f, n):
            """Target view too tight after relocation: budget, density Y or characteristic length X."""
            cnt = total.get(o['id'], 0) + n
            dens = 0.0
            if extents is not None and cnt >= pl.Y_MIN_ANNS:
                e = extents([o]).get(o['id'])
                kv = pl.view_scale(o, k)
                dens = cnt / (((e['w'] * kv + 2 * pl.OVER) * (e['h'] * kv + 2 * pl.OVER)) / 1e4) if e else 0.0
            sc = an.scale_value(o['scale']) if o.get('scale') else k
            return (load.get(o['id'], 0) + n > checks.BUDGET_VIEW or dens > pl.Y_MAX_DM2
                    or (f is not None and _kenn(f) is not None and _kenn(f) * sc < SOFT_MM))
        v_feats = set(v.get('features') or ())

        def shows(o, fid):
            if fid in (o.get('features') or ()):
                return True
            f = by_id.get(fid)
            if f is not None and o['id'] in axial and checks.coaxial(f, axis):      # the axial section exposes the coaxial elements
                return True
            return fid not in v_feats and f is not None and f.get('kind') == 'plane' and _dir(prj.frame_of(o)[0]) in _vis(f)
        return checks.view_weight(elements(own), others, by_id, shows, lambda o: prj.frame_of(o)[0], tight)

    def weighed(v, own):
        """Reason for the probe or None (has weight or probe budget exhausted). N4.8: hard 0 and soft 0 = statically replaceable: this view is always probed, outside PROBES_PER_PART
        (otherwise it would stay without a check and the view_replaceable measurement would report it); the budget only applies to the soft case (one soft element)."""
        hard, soft = section_weight(v, own)
        if hard >= 1 or hard + soft >= 2:
            return None
        replaceable.add(v['id'])
        if hard + soft == 1 and key(trial_of(v)) not in memo:
            if probes[0] <= 0:
                if v['id'] not in unchecked:
                    unchecked.append(v['id'])
                return None
            probes[0] -= 1
        return 'elements legible in another view'

    def refusal(r, trial, v):
        """Reason why the removal of v fails (None: it holds): coverage, stack budget, hidden or collision (becomes a planner note)."""
        have, notes0 = _coverage(res)
        got, gnotes = _coverage(r)
        if not (have <= got and notes0 <= gnotes):
            return 'coverage'
        if excess(r) > excess(res):
            return 'stack budget'
        if unseen(r, trial) > unseen(res, cur):
            return 'hidden'
        if pl.collisions(r[0], trial, k) > pl.collisions(res[0], cur, k):
            return 'collision'
        return None

    def trial_of(v):
        """Views without v; an axial section takes the coaxial elements of the dimensions of v into its features (they visibly migrate there)."""
        moved = [i for i in elements(mine(v)) if i in by_id and checks.coaxial(by_id[i], axis)] if axial else []
        return [dict(o, features=list(o.get('features') or ()) + [i for i in moved if i not in (o.get('features') or ())]) if o['id'] in axial and moved else o
                for o in cur if o is not v]

    def try_drop(v, why):
        nonlocal cur, res
        trial = trial_of(v)
        r = redo(trial)
        reason = refusal(r, trial, v)
        if reason is None:
            cur, res = trial, r
            dropped.append((v['id'], why))
            refused.pop(v['id'], None)
            return True
        refused[v['id']] = reason
        return False
    weighty = PLAIN_ROLES + ('section',)
    changed = True
    while changed:
        changed = False
        for v in sorted(list(cur), key=lambda v: len(elements(mine(v))) if v.get('role') in weighty else 0):
            if v is cur[0] or v not in cur or v.get('profile_own'):      # the profile view carries the required dimensions of the profile (N4.10), it is never dropped
                continue
            own = mine(v)
            if v.get('auxiliary'):          # an auxiliary view whose dimensions sit as n× in another view (mirror image) is dropped like an empty section
                why = 'no element of its own' if not own and not hosted(v) else None
            elif v.get('detail'):
                fs = [by_id.get(a.get('feature')) for a in own]
                # a Ø on an element that is still MIN_FEATURE_MM large at the sheet scale sits legibly as a leader line outside the parent view
                # (drawing.circle_fits): the enlargement is not needed
                readable = bool(own) and all(f is not None and _kenn(f) is not None and (_kenn(f) * k >= READABLE_MM or (
                    a.get('type') == 'diameter' and _kenn(f) * k >= checks.MIN_FEATURE_MM)) for a, f in zip(own, fs))
                why = ('no element of its own' if not own else 'no context (only small round elements in the circle)' if v.get('no_context')
                       else 'enlargement not needed' if readable else None)
            elif v.get('section') and v.get('role') == 'section':
                why = None if hosted(v) else 'no element of its own' if not own else weighed(v, own)
            elif v.get('role') in PLAIN_ROLES:     # normal view that carries no dimension after n×/relocation, refers to no table and hosts nothing
                free = not hosted(v) and not any(t.get('labels_in') == v['id'] for t in res[2])
                why = None if not free else 'no element of its own' if not own else weighed(v, own)
            else:
                why = None
            if why and try_drop(v, why):
                changed = True
    if notes is not None:
        notes += [f"{i} not checked (probe budget)" for i in unchecked if any(o['id'] == i for o in cur)]
        notes += [f"{i} statically replaceable, removal refused ({r})" for i, r in refused.items() if i in replaceable and any(o['id'] == i for o in cur)]      # the reason for the refusal
    return cur, res, dropped


def extra_view(part, feats, views, axis=None):
    """A further normal view for the overflow of the stack budget (plan_dims._budget) when no existing view shows the element: among the
    not yet occupied neighbouring views of the first view (method 1, plus the opposite view) the one with the most form features shown, on a
    tie the better one according to the main view choice. None if no direction is free any more (wire bending parts get none)."""
    pid = part['id']
    mine = [v for v in views if v.get('part') == pid]
    if not mine or any(v.get('role') == 'wire' for v in mine):
        return None
    anchor = prj.frame_of(mine[0])
    used = [prj.frame_of(v)[0] for v in mine if not v.get('detail')]
    free = [(r, fr) for r, fr in _neighbours(anchor) + [('back', _frame(-anchor[0], -anchor[1]))] if not any(np.allclose(fr[0], u, atol=1e-6) for u in used)]
    if axis is not None:      # profile view - the world axis axis must lie in the image plane (envelope dimension, web height), a section does not carry it
        free = [(r, fr) for r, fr in free if abs(float(fr[0][axis])) < 0.5]
    if not free:
        return None
    by_id = {f['id']: f for f in feats}
    rank = _ranking(part, feats)[0]
    shown = lambda fr: [f['id'] for f in feats if _shows(f, fr, None, by_id)]
    role, fr = min(free, key=lambda rf: (-len(shown(rf[1])), rank(rf[1])))
    ids = {v['id'] for v in views}
    vid, n = f'{pid}-{role}', 1
    while vid in ids:
        n += 1
        vid = f'{pid}-{role}{n}'
    return {'id': vid, 'part': pid, **_spec(fr), 'role': role, 'features': shown(fr)}


def main_options(part, cls, feats):
    """[(frame as spec dict, information value)] of possible main views, best first, per viewing axis only the best
    (the opposite direction shows the same mirrored); a direction that shows the same form features with the same
    information value as one already taken (turned part without cross elements) is no alternative.
    Wire bend parts: only the planned view."""
    rank, info = _ranking(part, feats)
    if cls == 'wire_bend':
        frames = [_wire_frames(part, rank)[0][1]]
    elif cls == 'turned' and ((part.get('shafts') or []) + (part.get('holes') or [])):
        frames = _turned_frames(part)
    else:
        frames = [_pframe(p) for p in VIEWS]
    sig = lambda fr: (round(info(fr), 6), frozenset(f['id'] for f in feats if _dir(fr[0]) in _vis(f)))
    kept = []
    for fr in sorted(frames, key=rank):
        if all(abs(fr[0] @ g[0]) < 0.5 and sig(fr) != sig(g) for g in kept):
            kept.append(fr)
    return [(_spec(fr), round(info(fr), 6)) for fr in kept]


def fr_n(rf):
    """Viewing direction of a (role, frame) pair of the neighbor views."""
    return rf[1][0]


def _mirror_twin(f, g, k, m):
    """Is the slant g the mirror image of the slant f at the plane x_k = m (same inclination and dimensions, normal parallel to the mirrored value, centre mirrored)? Then the section through g also shows f."""
    if g is f or f.get('kind') != 'slant' or g.get('kind') != 'slant' or f.get('normal') is None or g.get('normal') is None:
        return False
    if any(abs(float(f.get(q) or 0) - float(g.get(q) or 0)) > 1e-3 for q in ('angle_deg', 'width', 'length')):
        return False
    nf = np.array(prj.unit(f['normal']), float)
    nf[k] = -nf[k]
    if float(nf @ prj.unit(g['normal'])) < 1 - 1e-6:        # without abs: antiparallel normals are no twin
        return False
    mf = np.array(_mid(f), float)
    mf[k] = 2 * m - mf[k]
    return float(np.linalg.norm(mf - _mid(g))) <= 0.1


def part_views(part, cls, feats, k, labels, main=None, ray=None, sym=None):
    """ViewPlans of a part per spec 5.2 points 3-4 (see module doc); labels supplies the label letters. main
    (spec dict from main_options) prescribes the main view (not for wire bending parts). ray(p, N, n[, tmax]) -> bool (plan._ray, only called where needed): ray test for the choice of the view
    of a pattern excerpt (N4.9), None: without. sym {world axis: coordinate}: symmetry planes of the whole part; a slant whose mirror image a section already shows (_mirror_twin) gets no section of its own, it is in the features
    of that section (the dimensions appear there once as "n× ..." for both; two sections would show the same)."""
    pid, bb = part['id'], part['bbox']
    by_id = {f['id']: f for f in feats}
    rank = _ranking(part, feats)[0]
    views, frames, n_role = [], {}, {}
    block_k = k                 # (k is reused further below as an axis index)

    def make(role, fr, cut=None, tag=None, extra=()):
        n_role[role] = n_role.get(role, 0) + 1
        vid = f'{pid}-{tag or role}' + ('' if tag or n_role[role] == 1 else str(n_role[role]))
        frames[vid] = fr
        shown = [f['id'] for f in feats if f['id'] in extra or _shows(f, fr, cut, by_id)]
        return {'id': vid, 'part': pid, **_spec(fr), 'role': role, 'features': shown}

    def host_for(fr):
        """View without section in which a cutting plane with normal fr[0] appears as a line; otherwise a new side view."""
        for v in views:
            if 'section' not in v and not v.get('auxiliary') and abs(frames[v['id']][0] @ fr[0]) < TOL:
                return v['id']
        nb = min((f for r, f in _neighbours(anchor) if r == 'side' and abs(f[0] @ fr[0]) < TOL), key=rank, default=None)
        if nb is None:
            return views[0]['id']
        views.append(make('side', nb))
        return views[-1]['id']

    if cls == 'wire_bend':
        planned = _wire_frames(part, rank)
    elif main is not None:
        planned = [('main', _frame(*prj.frame_of(main)[:2]))]
    elif cls == 'turned' and ((part.get('shafts') or []) + (part.get('holes') or [])):
        planned = [('main', _turned_frame(part, rank))]
    else:
        planned = [('main', min((_pframe(p) for p in VIEWS), key=rank))]
    anchor = planned[0][1]
    views += [make(role, fr) for role, fr in planned]
    nbs = _neighbours(anchor)

    # Hidden form features -> section through their axis; it replaces the view of the same direction
    if cls != 'wire_bend':
        # N4.1 (ISO 128-3): turned part with a common axis (checks.part_axis, as in the check), main view across the axis: the section that replaces the main view lies
        # preferably in the axis. An axial section (checks.axis_section) gets neither a label letter nor a cutting line. The view in which its plane appears as a line is created
        # as before (host_for), but is no host: whether it is dropped is decided by prune_views per N4.8 (checks.view_weight; the axial section shows the coaxial elements there).
        axis = None if part.get('freeform') or cls != 'turned' else checks.part_axis(feats)
        axial = axis is not None and abs(float(prj.unit(axis[1]) @ anchor[0])) < TOL
        cands = [anchor] if cls == 'turned' else [fr for _, fr in nbs]
        groups, planes = _hidden(feats, bb)
        rk = [rank(fr) for fr in cands]
        sections = []
        cuts = _cuts(groups, planes, cands, rk, bb, axis[0] if axial else None)
        keep = reduce_cuts([c[3] for c in cuts])          # only sections with an element of their own (merge sections in the same direction)
        key_of = {x['id']: g[0]['id'] for g in groups for x in g}
        for n, (fr, pt, ids, cov) in enumerate(cuts):
            if n not in keep:
                continue
            # the elements of dropped sections are shown by the first remaining section whose plane hits them
            ids = ids + [x for m, c in enumerate(cuts) if m not in keep for x in c[2] if x not in ids and key_of.get(x, x) in cov
                         and next(j for j in keep if key_of.get(x, x) in cuts[j][3]) == n]
            if axial and checks.axis_section(pt, fr[0], feats):
                sec = make('section', fr, pt, None, set(ids))
                sec['section'] = {'point': _r(pt)}
            else:
                label = next(labels)
                sec = make('section', fr, pt, f'section-{label}', set(ids))
                sec['section'] = {'point': _r(pt), 'label': label}
            j = next((i for i, v in enumerate(views) if 'section' not in v and np.allclose(frames[v['id']][0], fr[0])), None)
            if j is None:
                views.append(sec)
            else:
                views[j] = sec
            sections.append(sec)
        for sec in sections:
            host = host_for(frames[sec['id']])
            if sec['section'].get('label'):
                sec['section_line_in'] = host
        if cls in ('plate', 'prismatic') and len(views) == 1:     # third axis missing
            role, fr = min(nbs, key=lambda rf: rank(rf[1]))
            views.append(make(role, fr))
        while True:                                               # otherwise invisible form features
            shown = {i for v in views for i in v['features']}
            rest = [f for f in feats if f.get('visible_from') and f['id'] not in shown and not f.get('minor')]      # minor faces (line "Undimensioned forms") do not demand a view
            used = [frames[v['id']][0] for v in views]
            back = ('back', _frame(-anchor[0], -anchor[1]))                # opposite view: elements only visible from behind
            free = [(r, fr) for r, fr in nbs + [back] if not any(np.allclose(fr[0], u) for u in used)]
            gain = lambda rf: sum(1 for f in rest if _dir(rf[1][0]) in f['visible_from'])
            best = min(free, key=lambda rf: (-gain(rf), rank(rf[1])), default=None)
            if best is None or gain(best) == 0:
                break
            views.append(make(*best))
        # Arcs: their R dimension needs the view along the axis (plan_dims._radius_ann). If no view shows it (the arc is visible from
        # the main view, but only as an edge), the neighbor view along the axis is added; arcs with an
        # oblique axis (auxiliary view, see below) and collective notes need none
        if True:
            for f in feats:
                if f['kind'] != 'arc' or f.get('axis') is None or collective_family(f) is not None:
                    continue
                A = np.asarray(f['axis'], float)
                A = A / (np.linalg.norm(A) or 1.0)
                if max(abs(A)) < 1 - TOL or any(abs(frames[v['id']][0] @ A) > 1 - TOL for v in views):
                    continue
                fits = [rf for rf in nbs if abs(rf[1][0] @ A) > 1 - TOL]
                seen = [rf for rf in fits if _dir(rf[1][0]) in _vis(f)]      # prefer the side from which the arc is visible
                fit = min(seen or fits, key=lambda rf: rank(rf[1]), default=None)
                if fit is not None:
                    views.append(make(fit[0], fit[1], extra={f['id']}))     # arc contour in the view along the axis
        # Slants (normal) and shaft cones (axis) are dimensioned in true length - for that a view must look along a world axis
        # perpendicular to them. If it is missing, the neighbor view that shows them (slant as a line, cone as a trapezoid) is added.
        # Pattern sub-elements only need it at the exemplar (via_member is dropped), minor faces not at all.
        for f in feats:
            vec = f.get('normal') if f['kind'] == 'slant' else f.get('axis') if f['kind'] == 'cone' and not f.get('countersink') and not end_chamfer(f) else None
            if vec is None or f.get('minor') or f.get('via_member') or f.get('drill_tip'):
                continue
            n = prj.unit(vec)
            if any(abs(float(frames[v['id']][0] @ n)) < 1e-3 and f['id'] in v['features'] for v in views if not v.get('detail')):
                continue      # a view in true length shows the element visibly (a hidden profile needs the section)
            fit = min((rf for rf in nbs if abs(float(rf[1][0] @ n)) < 1e-3), key=lambda rf: rank(rf[1]), default=None)
            if fit is not None:
                # Section through the face point instead of a mere view: an inner slant (funnel wall) lies in the view behind other
                # geometry, in the section its profile appears as a visible contour; sections of the same direction and position are shared
                pt = _mid(f)
                N = fit[1][0]
                window = _profile_window(f, N)
                ex = next((v for v in views if 'section' in v and not v.get('auxiliary') and abs(float(frames[v['id']][0] @ fr_n(fit))) > 1 - TOL
                           and (abs(float((pt - np.array(v['section']['point'], float)) @ N)) < TOL or
                                window and window[0]+TOL < np.asarray(v['section']['point']) @ N < window[1]-TOL)), None)
                twins = []
                if ex is None and sym and f['kind'] == 'slant':      # the mirror image of this slant is already shown by a section in this direction: the same sections, no further one (the dimensions appear there once as "n× ...")
                    ka = int(np.argmax(np.abs(N)))
                    ma = sym.get(ka)
                    if ma is not None:
                        twins = [v for v in views if 'section' in v and not v.get('auxiliary') and abs(float(frames[v['id']][0] @ fr_n(fit))) > 1 - TOL
                                 and any(_mirror_twin(f, by_id[i], ka, ma) for i in v['features'] if i in by_id)]
                if twins:
                    for v in twins:      # the mirror image appears in every section that shows its counterpart: prune_views decides which of them stays
                        v['features'] = list(v['features']) + [f['id']]
                elif ex is not None:
                    ex['features'] = list(ex['features']) + [f['id']]
                else:
                    if window:
                        pt = pt + (sum(window)/2-pt@N)*N
                    label = next(labels)
                    sec = make('section', fit[1], pt, f'section-{label}', {f['id']})
                    sec['section'] = {'point': _r(pt), 'label': label}
                    sec['section_line_in'] = host_for(fit[1])
                    views.append(sec)
            elif f['kind'] == 'slant':
                # Slant with three normal components: auxiliary view along its contour line (intersection of the face with the plane perpendicular to the
                # strongest normal component) - there the face appears as a line at the true inclination angle to the horizontal
                k = int(np.argmax(np.abs(n)))
                h = _canon(prj.unit(np.cross(n, np.eye(3)[k])))
                ex = next((v for v in views if v.get('auxiliary') and not v.get('section') and abs(float(frames[v['id']][0] @ h)) > 1 - TOL), None)
                if ex is not None:
                    ex['features'] = list(ex['features']) + [f['id']]
                    ex['aux_members'] = list(ex['aux_members']) + [f['id']]
                else:
                    label = next(labels)
                    v = make('auxiliary', _aux_frame(h, [f], bb), tag=f'aux-{label}', extra={f['id']})
                    v['auxiliary'] = {'label': label}
                    v['aux_members'] = [f['id']]
                    hosts = [u for u in views if not u.get('detail') and not u.get('auxiliary')]
                    host = max(hosts, key=lambda u: (round(float(np.linalg.norm(np.cross(frames[u['id']][0], frames[v['id']][0]))), 6),
                                                     'section' not in u, -hosts.index(u)))
                    v['arrow_in'], v['arrow_at'] = host['id'], _r(_mid(f))
                    views.append(v)
        if sym:      # every section also shows the mirror images of its slants (independent of the order in which the sections were created): they appear as "n× ..." at their counterpart
            for v in views:
                if 'section' not in v or v.get('auxiliary') or v.get('detail'):
                    continue
                N = frames[v['id']][0]
                ka = int(np.argmax(np.abs(N)))
                ma = sym.get(ka)
                if ma is None or abs(float(N[ka])) < 1 - TOL:
                    continue
                shown = [by_id[i] for i in v['features'] if i in by_id and by_id[i]['kind'] == 'slant']
                v['features'] = list(v['features']) + [f['id'] for f in feats if f['kind'] == 'slant' and f['id'] not in v['features'] and any(_mirror_twin(f, g, ka, ma) for g in shown)]
        # an inner cone (hole host, free-standing) only gets its dimension in a section through its axis in which both ends
        # are drawn as an edge. If _cuts found no such section (the axis lies in the main plane, the view was replaced), an
        # additional section in a direction across the axis is added (also the main direction); the cutting point lies just inside the part, not on its
        # surface (otherwise the plane would cut nothing).
        for f in feats:
            if (f['kind'] != 'cone' or f.get('axis') is None or f.get('minor') or f.get('countersink') or f.get('drill_tip') or f.get('via_member')
                    or end_chamfer(f) or not (f.get('free') or f.get('host_hole'))):
                continue
            a = prj.unit(f['axis'])
            if any('section' in v and not v.get('auxiliary') and abs(float(frames[v['id']][0] @ a)) < 1e-3 and f['id'] in v['features'] for v in views):
                continue
            fr = min([anchor] + [r for _, r in nbs], key=lambda r: abs(float(r[0] @ a)))
            if abs(float(fr[0] @ a)) > 1e-3:
                continue
            pt = _mid(f)
            corners = np.array([[x, y, z] for x in (bb['min'][0], bb['max'][0]) for y in (bb['min'][1], bb['max'][1]) for z in (bb['min'][2], bb['max'][2])]) @ fr[0]
            c = float(pt @ fr[0])
            pt = pt - 5e-4 * fr[0] if c > corners.max() - 1e-3 else pt + 5e-4 * fr[0] if c < corners.min() + 1e-3 else pt
            ex = next((v for v in views if 'section' in v and not v.get('auxiliary') and abs(float(frames[v['id']][0] @ fr[0])) > 1 - TOL
                       and abs(float((pt - np.array(v['section']['point'], float)) @ fr[0])) < 1e-3), None)
            if ex is not None:
                ex['features'] = list(ex['features']) + [f['id']]
            else:
                label = next(labels)
                sec = make('section', fr, pt, f'section-{label}', {f['id']})
                sec['section'] = {'point': _r(pt), 'label': label}
                sec['section_line_in'] = host_for(fr)
                views.append(sec)
        # Auxiliary views for form features with an oblique axis (ISO 128-3): view along the axis, label letter, viewing arrow
        # in the view that shows the viewing direction least foreshortened (on a tie rather no section, then the
        # earlier one). If none of the elements is seen along the axis (visible_along empty, e.g. a groove closed on both sides),
        # it becomes a section across the axis through its center with a section line instead of an arrow
        if True:
            for fr, mem in _aux_views(feats, [frames[v['id']][0] for v in views], bb):
                label, ids = next(labels), {f['id'] for f in mem}
                at = np.mean([_mid(f) for f in mem], 0)
                cut = not any(_seen_along(f, fr[0]) for f in mem)
                v = make('auxiliary', fr, tag=f'aux-{label}', extra=ids)
                # shown: those visible from here or exposed by the section (the view attracts no other dimensions,
                # R dimension of hidden arcs stays with the fallback chain in plan_dims); aux_members: all, their position is given here
                v['features'] = [f['id'] for f in mem if cut or _seen_along(f, fr[0])]
                v['aux_members'] = [f['id'] for f in mem]
                v['auxiliary'] = {'label': label}
                hosts = [u for u in views if not u.get('detail') and not u.get('auxiliary')]
                host = max(hosts, key=lambda u: (round(float(np.linalg.norm(np.cross(frames[u['id']][0], fr[0]))), 6), 'section' not in u,
                                                 -hosts.index(u)))
                if cut:
                    v['section'] = {'point': _r(at), 'label': label}
                    v['section_line_in'] = host['id']
                else:
                    v['arrow_in'], v['arrow_at'] = host['id'], _r(at)
                views.append(v)

    # Small form features (size · k < SMALL_MM), smallest first -> detail view or cross-section at wire/square bar
    # cross hole; one per not yet covered element, neighbors combined in it, at most MAX_ENLARGED
    group = {}               # a member in a detail view covers its whole pattern (like checks)
    for p in feats:
        if p['kind'] == 'pattern':
            for m in p['members']:
                group.setdefault(m, set()).update(p['members'])
    limit = lambda f: MIN_FEATURE_MM if f['kind'] == 'square' else SMALL_MM   # □ dimension at the outer contour: legible from 2 mm
    small = sorted((f for f in feats if f['kind'] in SMALL_KINDS and f.get('size') and f['size'] * k < limit(f)),
                   key=lambda f: (f['size'], f['id']))
    covered, n_enlarged = set(), 0
    for f in small:
        if f['id'] in covered or n_enlarged == MAX_ENLARGED:
            continue
        n_enlarged += 1
        s = _detail_scale(f['size'])
        cap = DETAIL_MAX_R * s[1] / s[0]
        in_sec = next((v for v in views if 'section' in v and not v.get('auxiliary') and f['id'] in v['features']), None)
        ax = None if in_sec else _bar_axis(f, cls, part, feats)
        if ax is not None and abs(ax @ anchor[0]) < 1 - TOL:
            fits = [fr for _, fr in nbs if abs(fr[0] @ ax) > 1 - TOL]
            fr = min(fits, key=rank) if fits else prj.frame_of({'direction': ax, 'up': anchor[0]})
            label = next(labels)
            v = make('section', fr, _mid(f), f'section-{label}', {f['id']})
            v['section'] = {'point': _r(_mid(f)), 'label': label}
            v['scale'] = an.scale_str(s)
            v['section_line_in'] = host_for(fr)
            got = set(v['features'])
        else:
            host = in_sec or next((v for v in views if f['id'] in v['features'] and not v.get('auxiliary')), views[0])
            fr, label = frames[host['id']], next(labels)
            members, (c2, radius) = [f], _circle([f], fr, cap)
            pool = [g for g in small if g is not f and g['id'] not in covered and g['id'] in host['features']]
            for g in sorted(pool, key=lambda g: float(np.linalg.norm(prj.project([_mid(g)], fr)[0] - c2))):
                if len(members) == 1 and radius >= cap:
                    break                                  # already capped: add nothing
                c, r = _circle(members + [g], fr, cap)
                if r < cap:
                    members, c2, radius = members + [g], c, r
            near = lambda g: bool(_points(g, by_id)) and min(np.linalg.norm(prj.project(_points(g, by_id), fr) - c2, axis=1)) <= radius
            got = {g['id'] for g in feats if (g['id'] in host['features'] or g in members) and near(g)}
            # a detail view shows the surrounding geometry, not just a circle. If the excerpt only carries small round elements (hole,
            # pin, sphere), the circle grows to the next other element, at most to CONTEXT_GROW · radius or cap; if none is found,
            # no_context stays set and prune_views drops the detail view (the specification then sits at the main view).
            if all(_round(by_id[i], by_id) for i in got if i in by_id):
                reach = {g['id']: min(np.linalg.norm(prj.project(_points(g, by_id), fr) - c2, axis=1)) for g in feats
                         if g['id'] in host['features'] and g['id'] not in got and not _round(g, by_id) and _points(g, by_id)}
                if reach:
                    d = min(reach.values())
                    if d + CONTEXT_MARGIN * radius <= min(cap, CONTEXT_GROW * radius):
                        radius = d + CONTEXT_MARGIN * radius
                        got = {g['id'] for g in feats if (g['id'] in host['features'] or g in members) and near(g)}
            no_ctx = all(_round(by_id[i], by_id) for i in got if i in by_id)
            depth = float(np.mean([_mid(g) @ fr[0] for g in members]))
            center = c2[0] * fr[1] + c2[1] * fr[2] + depth * fr[0]
            v = make('detail', fr, None, f'detail-{label}')
            v['features'] = [g['id'] for g in feats if g['id'] in got]
            if 'section' in host:
                v['section'] = dict(host['section'])
            v['detail'] = {'center': _r(center), 'radius': round(float(radius), 4), 'label': label, 'scale': an.scale_str(s)}
            v['detail_circle_in'] = host['id']
            if no_ctx:
                v['no_context'] = True
        views.append(v)
        covered |= got | {m for i in got for m in group.get(i, ())}

    # Sub-elements (slants) of the exemplar member of a pattern stand in a detail view of the exemplar (standard scale
    # by smallest characteristic length, at least EXEMPLAR_MIN_SCALE) instead of leader lines in the main view; one per pattern, at most
    # EXEMPLAR_MAX per part, patterns in id order. Likewise (CLUSTER_MAX) one detail view per cluster of individually required slants
    # (deviating member, e.g. a peg with 14 slants): at least CLUSTER_MIN slants within at most CLUSTER_R mm of the first one.
    # The detail view shows only these slants (other dimensions stay in their views).
    def true_length(f, fr):
        """The form feature appears in this view in true length - the normal of a slant or the axis of a cone
        is perpendicular to the viewing direction (slant as a line, cone as a trapezoid)."""
        v = f.get('normal') if f['kind'] == 'slant' else f.get('axis') if f['kind'] == 'cone' else None
        return v is not None and abs(float(prj.unit(v) @ fr[0])) < 1e-3

    def slant_detail(group, tag):
        # View of the detail view: preferably one in which the elements appear in true length (angle and length dimension stand there), then
        # the one with the most elements shown visibly
        hosts = [v for v in views if not v.get('detail') and not v.get('auxiliary')
                 and any(f['id'] in v['features'] or true_length(f, frames[v['id']]) for f in group)]
        if not group or not hosts:
            return False
        host = max(hosts, key=lambda v: (sum(true_length(f, frames[v['id']]) for f in group), sum(f['id'] in v['features'] for f in group),
                                         -hosts.index(v)))
        shown = [f for f in group if f['id'] in host['features'] or true_length(f, frames[host['id']])]
        fr, label = frames[host['id']], next(labels)
        Q = prj.project([f['pt'] for f in shown], fr)
        c2 = (Q.min(0) + Q.max(0)) / 2
        r_mm = float(np.linalg.norm(Q - c2, axis=1).max()) + 0.5 * max(max(f['length'], f.get('width', f.get('d2'))) for f in shown) * 1.1
        feat_len = min(min(f.get('width', f.get('d2')), f['length']) for f in shown)
        fits = [s for s in sorted(an.SCALES, key=lambda s: s[0] / s[1]) if s[0] / s[1] >= max(EXEMPLAR_MIN_SCALE, 1.0001 * k)]
        want = next((s for s in fits if feat_len * s[0] / s[1] >= DETAIL_MM), fits[-1])
        s = next((s for s in reversed(fits[:fits.index(want) + 1]) if r_mm * s[0] / s[1] <= DETAIL_MAX_R), fits[0])
        depth = float(np.mean([np.asarray(f['pt'], float) @ fr[0] for f in shown]))
        center = c2[0] * fr[1] + c2[1] * fr[2] + depth * fr[0]
        v = make('detail', fr, None, f'detail-{label}')
        v['features'] = [f['id'] for f in shown]
        if 'section' in host:
            v['section'] = dict(host['section'])
        v['detail'] = {'center': _r(center), 'radius': round(r_mm, 4), 'label': label, 'scale': an.scale_str(s), 'exemplar': tag}
        v['detail_circle_in'] = host['id']
        views.append(v)
        return True

    exemplars = {}
    for f in feats:
        if f.get('via_member') and f.get('via_pattern'):
            exemplars.setdefault(f['via_pattern'], set()).add(f['via_member'])
    n_ex = 0
    for pat_id, ids in sorted(exemplars.items()):
        if n_ex < EXEMPLAR_MAX and slant_detail([by_id[i] for i in sorted(ids) if i in by_id and by_id[i].get('pt') is not None
                                                 and not by_id[i].get('minor')], pat_id):      # only main content, no empty detail view for minor faces
            n_ex += 1
    lone = [f for f in feats if f['kind'] == 'slant' and not f.get('via_member') and f.get('pt') is not None
            and not f.get('minor') and f['id'] not in {i for ids in exemplars.values() for i in ids}]
    n_cl = 0
    while lone and n_cl < CLUSTER_MAX:
        seed = lone[0]
        group = [f for f in lone if float(np.linalg.norm(np.asarray(f['pt'], float) - np.asarray(seed['pt'], float))) <= CLUSTER_R]
        lone = [f for f in lone if f not in group]
        if len(group) >= CLUSTER_MIN and slant_detail(group, 'cluster'):
            n_cl += 1

    # N4.9: a plane pattern (grid, ribs) whose characteristic length at the sheet scale is below PATTERN_MIN_MM gets an excerpt over the whole pattern envelope (all members of all
    # sequences of the pattern group) at the smallest standard scale above the sheet scale at which characteristic length · scale ≥ PATTERN_MIN_MM holds. Groups of equal pitch, count and characteristic length (mirror and
    # identical twins) share the excerpt as long as it stays below PATTERN_DETAIL_MAX_R, otherwise the first group gets it. Own quota, own cap; pitch ("n× t (=L)") and thickness are shown there.
    pats = [{**f, 'part': pid} for f in feats if f['kind'] == 'plane_pattern' and f.get('members')]
    fams = []
    for g in (g for g in checks.pattern_groups(pats) if g['char_len'] * block_k < checks.PATTERN_MIN_MM - 1e-9):
        fam = next((x for x in fams if x[0]['axis_name'] == g['axis_name'] and x[0]['count'] == g['count'] and abs(x[0]['pitch'] - g['pitch']) <= TOL
                    and abs(x[0]['char_len'] - g['char_len']) <= TOL), None)
        if fam is None:
            fams.append([g])
        else:
            fam.append(g)

    def pattern_detail(groups):
        ids = [i for g in groups for i in g['ids']]
        mem = [m for i in ids for m in by_id[i]['members'] if m in by_id and by_id[m].get('bbox')]
        e = np.eye(3)[groups[0]['k']]
        hosts = [v for v in views if not v.get('detail') and not v.get('auxiliary') and abs(float(frames[v['id']][0] @ e)) < TOL]
        def seen(v):      # members shown visibly: v['features'] and (except in sections) the ray test at the foot point along the pattern axis (hidden foot points would move the dimensions out of the excerpt)
            return sum(1 for m in mem if m in v['features'] and (ray is None or v.get('section') or by_id[m].get('pt') is None or ray(by_id[m]['pt'], frames[v['id']][0], e)))
        host = max(hosts, key=lambda v: (seen(v), sum(m in v['features'] for m in mem), -hosts.index(v)), default=None)
        if host is None or not mem or not any(m in host['features'] for m in mem):
            return False
        fr = frames[host['id']]
        corners = [[b[x][0], b[y][1], b[z][2]] for m in mem for b in [(by_id[m]['bbox']['min'], by_id[m]['bbox']['max'])] for x in (0, 1) for y in (0, 1) for z in (0, 1)]
        Q = prj.project(corners, fr)
        c2, r0 = _min_circle(Q)
        r = r0 * (1 + PATTERN_MARGIN) + 0.5
        char_len = min(g['char_len'] for g in groups)
        s = next((s for s in sorted(an.SCALES, key=lambda s: s[0] / s[1]) if s[0] / s[1] > block_k * (1 + 1e-9) and char_len * s[0] / s[1] >= checks.PATTERN_MIN_MM - 1e-9), None)
        if s is None or r * s[0] / s[1] > PATTERN_DETAIL_MAX_R:
            return False
        depth = float(np.mean([((np.asarray(by_id[m]['bbox']['min'], float) + np.asarray(by_id[m]['bbox']['max'], float)) / 2) @ fr[0] for m in mem]))
        label = next(labels)
        v = make('detail', fr, None, f'detail-{label}')
        shown = set(ids) | set(mem)
        v['features'] = [f['id'] for f in feats if f['id'] in shown]
        if 'section' in host:
            v['section'] = dict(host['section'])
        v['detail'] = {'center': _r(c2[0] * fr[1] + c2[1] * fr[2] + depth * fr[0]), 'radius': round(r, 4), 'label': label, 'scale': an.scale_str(s)}
        v['detail_circle_in'] = host['id']
        v['pattern_detail'] = sorted(ids)
        views.append(v)
        return True
    def profile_view(pp):
        P, need = pp['profile'], pp['need']
        d = prj.unit(P['axis'])
        if not need or P.get('frame') is None or any(P['id'] == v.get('profile_of') or P['id'] in (v.get('profile_dims') or ()) for v in views):
            return
        if not checks.profile_candidates(d, anchor):      # same candidate choice as the check (checks.profile_buildable): no view along d buildable, the profile is not promoted
            views[0].setdefault('profile_unbuilt', []).append(P['id'])
            return
        origin = np.asarray(P['frame']['origin'], float)
        char_len = profiles.char_len(P, need)
        r_mm = max(float(np.hypot(q[0], q[1])) for e in P['outline'] for q in profiles.sample(e, 0.2)) * (1 + PROFILE_MARGIN) + 0.5
        normal = [v for v in views if not v.get('detail') and not v.get('auxiliary') and 'section' not in v and not v.get('profile_own')
                  and abs(float(frames[v['id']][0] @ d)) > 1 - TOL]
        seen = [v for v in normal if _dir(frames[v['id']][0]) in (P.get('visible_from') or [])]
        if seen and char_len * k >= SMALL_MM:
            seen[0].setdefault('profile_dims', []).append(P['id'])
            return
        scales = sorted(an.SCALES, key=lambda s_: s_[0] / s_[1])
        if seen:
            s_ = next((s_ for s_ in scales if s_[0] / s_[1] > k * (1 + 1e-9) and char_len * s_[0] / s_[1] >= SMALL_MM - 1e-9 and r_mm * s_[0] / s_[1] <= PROFILE_DETAIL_MAX_R), None)
            if s_ is not None:
                host, label = seen[0], next(labels)
                fr = frames[host['id']]
                c2 = prj.project([origin], fr)[0]
                v = make('detail', fr, None, f'detail-{label}')
                v['features'] = []
                v['detail'] = {'center': _r(c2[0] * fr[1] + c2[1] * fr[2] + float(origin @ fr[0]) * fr[0]), 'radius': round(r_mm, 4), 'label': label, 'scale': an.scale_str(s_)}
                v['detail_circle_in'] = host['id']
                v['profile_of'], v['profile_own'] = P['id'], True
                views.append(v)
                return
        cands = [fr for fr in [anchor] + [f_ for _, f_ in nbs] + [_frame(-anchor[0], -anchor[1])] if abs(float(fr[0] @ d)) > 1 - TOL]
        if not cands:
            return                                    # oblique generatrix without a normal view: no profile section (an auxiliary view is a matter of slant planning)
        fr = min(cands, key=rank)
        s_ = next((s_ for s_ in scales if s_[0] / s_[1] > k * (1 + 1e-9) and char_len * s_[0] / s_[1] >= SMALL_MM - 1e-9), None)
        corners = prj.project([[x, y, z] for x in (bb['min'][0], bb['max'][0]) for y in (bb['min'][1], bb['max'][1]) for z in (bb['min'][2], bb['max'][2])], fr)
        ext = float(np.ptp(corners, axis=0).max())
        if s_ is not None and ext * s_[0] / s_[1] > PROFILE_SECTION_MAX_MM:
            s_ = None
        label = next(labels)
        sec = make('section', fr, origin, f'section-{label}', set())
        sec['section'] = {'point': _r(origin), 'label': label}
        sec['section_line_in'] = host_for(fr)
        if s_ is not None:
            sec['scale'] = an.scale_str(s_)
        sec['profile_of'], sec['profile_own'] = P['id'], True
        views.append(sec)

    n_pat = 0
    for fam in fams:
        if n_pat == PATTERN_DETAIL_MAX:
            break
        if len(fam) > 1 and pattern_detail(fam):
            n_pat += 1
            continue
        for g in fam:               # if the common excerpt is too large, each group of the family gets its own (as long as the quota lasts), no bare dimension in the main view
            if n_pat == PATTERN_DETAIL_MAX:
                break
            if pattern_detail([g]):
                n_pat += 1

    # N4.10: a profile with remaining members (required list profile['need'], residual geometry only) is shown in a view looking along d in true size. If the characteristic length of the profile is already
    # legible at the sheet scale (SMALL_MM), the existing view along d gets the dimensions (no new block). Otherwise a detail view of the exemplar from an existing view in which one end lies
    # free (smallest standard scale above the sheet scale, characteristic length ≥ SMALL_MM, own cap PROFILE_DETAIL_MAX_R), otherwise a section perpendicular to d through the middle of the exemplar length (no section through a
    # pattern group: the exemplar excerpt suffices). Both get a block of their own (profile_own, plan._content) and are excluded from the general dimensioning (plan_dims.dimension).
    for pp in part.get('profiles') or []:
        profile_view(pp)
    return views


def undetailed_patterns(feats, views, pid, block_k, sym=None):
    """Pattern groups of part pid with characteristic length · block_k below PATTERN_MIN_MM (N4.9) that appear in no pattern excerpt of the views (quota PATTERN_DETAIL_MAX or cap
    PATTERN_DETAIL_MAX_R reached, or no host) and are also no mirror image at sym of a group already shown in an excerpt: material of the planner note. [Groups from checks.pattern_groups]."""
    shown = {i for v in views if v.get('pattern_detail') for i in v['pattern_detail']}
    pats = [{**f, 'part': pid} for f in feats if f['kind'] == 'plane_pattern' and f.get('members')]
    groups = checks.pattern_groups(pats)
    detailed = [g for g in groups if set(g['ids']) <= shown]

    def mirrored(g):
        if not sym or g['k'] not in sym:
            return False
        mid = float(sym[g['k']])
        return any(h['k'] == g['k'] and h['count'] == g['count'] and abs(h['pitch'] - g['pitch']) <= TOL and abs(h['char_len'] - g['char_len']) <= TOL
                   and abs(g['lo'] + h['hi'] - 2 * mid) <= TOL and abs(g['hi'] + h['lo'] - 2 * mid) <= TOL for h in detailed)
    return [g for g in groups if g['char_len'] * block_k < checks.PATTERN_MIN_MM - 1e-9 and not set(g['ids']) <= shown and not mirrored(g)]


ISO_OCTANTS = ((1, 1, 1), (1, -1, 1), (-1, 1, 1), (-1, -1, 1), (1, 1, -1), (1, -1, -1), (-1, 1, -1), (-1, -1, -1))   # standard first (= preset iso)
ISO_OPENINGS = ('hole', 'pocket', 'slot', 'step')     # form features whose opening the isometric view should show
_OCTANT_VIEWS = {1: ('left', 'right'), 2: ('front', 'back'), 3: ('top', 'bottom')}


def iso_view(feats):
    """Viewing direction of the single-part isometric view: among the 8 isometric octants (camera at (±1, ±1, ±1), image up = +z) the one
    from which the most openings (holes, pockets, grooves, steps: ISO_OPENINGS) are visible; a form feature is visible
    if one of the three sides of the octant shows it (visible_from; empty = unknown, does not count). On a tie the first in
    ISO_OCTANTS (standard: preset iso), so the choice stays deterministic. -> view description {preset} or {direction, up}."""
    seen = [f for f in feats if f['kind'] in ISO_OPENINGS and f.get('visible_from')]

    def score(o):
        sides = {_OCTANT_VIEWS[k + 1][0 if o[k] > 0 else 1] for k in range(3)}
        return sum(1 for f in seen if sides & set(f['visible_from']))
    best = max(ISO_OCTANTS, key=lambda o: (score(o), -ISO_OCTANTS.index(o)))
    if best == (1, 1, 1):
        return {'preset': 'iso'}
    if best == (-1, -1, 1):
        return {'preset': 'iso_back'}
    return {'direction': _r(prj.unit(best), 12), 'up': [0.0, 0.0, 1.0]}


def assembly_views(parts, roles):
    """Assembly: front view, top view (below), left side view (to the right), isometric view.
    Parts with role 'reference' thin (thin_parts), 'duplicate' not included."""
    ids = [p['id'] for p in parts if roles.get(p['id']) != 'duplicate']
    thin = [i for i in ids if roles.get(i) == 'reference']
    out = []
    for preset, role in (('front', 'main'), ('top', 'top'), ('left', 'side'), ('iso', 'iso')):
        v = {'id': f'asm-{preset}', 'part': None, 'preset': preset, 'role': role, 'parts': list(ids), 'features': []}
        if thin:
            v['thin_parts'] = list(thin)
        out.append(v)
    return out
