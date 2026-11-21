"""Geometry evaluation for drawings: bounding box, volume, holes/shafts (cylindrical faces,
including those stored as B-spline, via GeomConvert_SurfToAnaSurf), plane distances, bend tables of
wire/tube sweeps (tori + cylinders of equal tube radius) and a suggestion for sheet size/scale."""
import numpy as np
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_EDGE
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool
from OCP.BRepTools import BRepTools
from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp
from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Torus, GeomAbs_BSplineSurface, GeomAbs_BezierSurface, GeomAbs_SurfaceOfRevolution, GeomAbs_SurfaceOfExtrusion, GeomAbs_OffsetSurface
from OCP.GeomConvert import GeomConvert_SurfToAnaSurf
from OCP.Geom import Geom_CylindricalSurface, Geom_ToroidalSurface, Geom_Plane
import projection as prj

SCALES = [(50, 1), (20, 1), (10, 1), (5, 1), (2, 1), (1, 1), (1, 2), (1, 5), (1, 10), (1, 20), (1, 50), (1, 100)]
ORDER = ['A4', 'A3', 'A2', 'A1', 'A0']


def v3(p):
    return np.array([p.X(), p.Y(), p.Z()])


def faces(shape):
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        yield TopoDS.Face_s(ex.Current()); ex.Next()


def bbox(shape):
    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, b, False, False)
    x0, y0, z0, x1, y1, z1 = b.Get()
    mn, mx = np.array([x0, y0, z0]), np.array([x1, y1, z1])
    return {'min': mn.round(4).tolist(), 'max': mx.round(4).tolist(), 'size': (mx - mn).round(4).tolist()}


def volume(shape):
    g = GProp_GProps(); BRepGProp.VolumeProperties_s(shape, g)
    return float(g.Mass())


def boundary_points(face, defl=0.01):
    """Boundary points of all edges of face (face, wire or edge), per edge with the shared sampling projection.sample_curve."""
    pts = []
    ex = TopExp_Explorer(face, TopAbs_EDGE)
    while ex.More():
        pts += [v3(p) for p in prj.sample_curve(BRepAdaptor_Curve(TopoDS.Edge_s(ex.Current())), defl)]
        ex.Next()
    return np.array(pts) if pts else np.zeros((0, 3))


def face_point_normal(face):
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    s = BRepAdaptor_Surface(face)
    pr = BRepLProp_SLProps(s, (u0 + u1) / 2, (v0 + v1) / 2, 1, 1e-7)
    if not pr.IsNormalDefined():
        return None, None
    n = v3(pr.Normal())
    if face.Orientation() == TopAbs_REVERSED:
        n = -n
    return v3(pr.Value()), n


def analytic(face, tol=1e-4):
    """Analytic surface (Geom_Plane/Cylindrical/Toroidal) of the face, also from B-splines."""
    s = BRepAdaptor_Surface(face)
    t = s.GetType()
    if t == GeomAbs_Plane:
        return 'plane', s.Plane()
    if t == GeomAbs_Cylinder:
        return 'cylinder', s.Cylinder()
    if t == GeomAbs_Torus:
        return 'torus', s.Torus()
    if t in (GeomAbs_BSplineSurface, GeomAbs_BezierSurface, GeomAbs_SurfaceOfRevolution, GeomAbs_SurfaceOfExtrusion, GeomAbs_OffsetSurface):
        a = None
        g = BRep_Tool.Surface_s(face)
        for tl in (tol, 10 * tol, 50 * tol):      # sweeps are stored approximated as B-splines
            try:
                a = GeomConvert_SurfToAnaSurf(g).ConvertToAnalytical(tl)
            except Exception:
                a = None
            if a is not None:
                break
        if a is None:
            return None, None
        for cls, kind, getter in ((Geom_CylindricalSurface, 'cylinder', 'Cylinder'), (Geom_ToroidalSurface, 'torus', 'Torus'), (Geom_Plane, 'plane', 'Pln')):
            try:
                obj = cls.DownCast(a) if hasattr(cls, 'DownCast') else None
            except Exception:
                obj = None
            if obj is None and isinstance(a, cls):
                obj = a
            if obj is not None:
                return kind, getattr(obj, getter)()
    return None, None


def cylinders(shape):
    """Cylindrical faces, grouped per (axis, radius): hole (material outside) or shaft."""
    out = []
    for f in faces(shape):
        kind, c = analytic(f)
        if kind != 'cylinder':
            continue
        ax = c.Axis(); d = prj.unit(v3(ax.Direction())); o = v3(ax.Location()); r = c.Radius()
        bp = boundary_points(f)
        if not len(bp):
            continue
        t = (bp - o) @ d
        p, n = face_point_normal(f)
        if p is None:
            continue
        radial = (p - o) - ((p - o) @ d) * d
        is_hole = float(n @ radial) < 0
        out.append({'o': o, 'd': d, 'r': r, 't0': float(t.min()), 't1': float(t.max()), 'hole': is_hole})
    merged = []
    for c in out:
        for m in merged:
            if abs(m['r'] - c['r']) < 1e-4 and abs(abs(m['d'] @ c['d']) - 1) < 1e-6 and np.linalg.norm(np.cross(c['o'] - m['o'], m['d'])) < 1e-4 and m['hole'] == c['hole']:
                s = 1.0 if m['d'] @ c['d'] > 0 else -1.0
                off = (c['o'] - m['o']) @ m['d']
                a, b = sorted((off + s * c['t0'], off + s * c['t1']))
                if a <= m['t1'] + 1e-4 and b >= m['t0'] - 1e-4:
                    m['t0'], m['t1'] = min(m['t0'], a), max(m['t1'], b)
                    break
        else:
            merged.append(dict(c))
    res, cls = [], None
    for m in merged:
        a = m['o'] + m['d'] * m['t0']; b = m['o'] + m['d'] * m['t1']
        rec = {'diameter': round(2 * m['r'], 4), 'radius': round(m['r'], 4), 'depth': round(m['t1'] - m['t0'], 4),
               'axis': m['d'].round(6).tolist(), 'start': a.round(4).tolist(), 'end': b.round(4).tolist(), 'hole': m['hole']}
        if m['hole']:
            eps = max(1e-3, 0.02 * m['r'])
            if cls is None:
                cls = _classifier(shape)
            rec['open_start'] = _outside(cls, a - m['d'] * eps)
            rec['open_end'] = _outside(cls, b + m['d'] * eps)
        res.append(rec)
    return res


def _classifier(shape):
    """Point classifier of the shape, loaded once (loading costs many times one test for large parts)."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    return BRepClass3d_SolidClassifier(shape)


def _outside(cls, p):
    """Is p outside? cls from _classifier; Perform(p, 1e-6) like the constructor with a point."""
    from OCP.TopAbs import TopAbs_OUT
    from OCP.gp import gp_Pnt
    cls.Perform(gp_Pnt(*map(float, p)), 1e-6)
    return cls.State() == TopAbs_OUT


def tori(shape):
    out = []
    for f in faces(shape):
        kind, g = analytic(f)
        if kind != 'torus':
            continue
        ax = g.Position()
        out.append({'center': v3(ax.Location()).round(5).tolist(), 'axis': prj.unit(v3(ax.Direction())).round(6).tolist(),
                    'major': round(g.MajorRadius(), 5), 'minor': round(g.MinorRadius(), 5)})
    uniq = []
    for t in out:
        if not any(np.allclose(t['center'], u['center'], atol=1e-4) and abs(t['major'] - u['major']) < 1e-4 and abs(t['minor'] - u['minor']) < 1e-4 for u in uniq):
            uniq.append(t)
    return uniq


def plane_gaps(shape, limit=24):
    pl = []
    for f in faces(shape):
        kind, p = analytic(f)
        if kind != 'plane':
            continue
        pt, n = face_point_normal(f)
        if pt is None:
            continue
        pl.append((pt, prj.unit(n)))
    out, seen = [], set()
    for i in range(len(pl)):
        for j in range(i + 1, len(pl)):
            (p1, n1), (p2, n2) = pl[i], pl[j]
            if n1 @ n2 > -1 + 1e-7:
                continue
            dist = float((p2 - p1) @ n1)
            kind = 'thickness' if dist < 0 else 'gap'
            key = (tuple(np.round(np.abs(n1), 4)), round(abs(dist), 4), kind)
            if key in seen or abs(dist) < 1e-6:
                continue
            seen.add(key)
            out.append({'kind': kind, 'distance': round(abs(dist), 4), 'normal': n1.round(6).tolist(),
                        'p1': p1.round(4).tolist(), 'p2': p2.round(4).tolist()})
    out.sort(key=lambda g: -g['distance'])
    return out[:limit]


def _arc_range(angles):
    a = np.sort(np.mod(angles, 2 * np.pi))
    gaps = np.diff(np.r_[a, a[0] + 2 * np.pi])
    k = int(np.argmax(gaps))
    start = a[(k + 1) % len(a)]
    span = 2 * np.pi - gaps[k]
    return start, span


def bend_table(shape, tol=1e-3):
    """Bend table of a wire/tube sweep: points start, virtual bend points (intersection of the tangents), end."""
    tori, cyls = [], []
    for f in faces(shape):
        kind, g = analytic(f)
        if kind == 'torus':
            ax = g.Position(); C = v3(ax.Location()); A = prj.unit(v3(ax.Direction()))
            X = prj.unit(v3(ax.XDirection())); Y = np.cross(A, X)
            R, r = g.MajorRadius(), g.MinorRadius()
            bp = boundary_points(f)
            if not len(bp):
                continue
            ang = np.arctan2((bp - C) @ Y, (bp - C) @ X)
            a0, span = _arc_range(ang)
            S = C + R * (np.cos(a0) * X + np.sin(a0) * Y); E = C + R * (np.cos(a0 + span) * X + np.sin(a0 + span) * Y)
            tori.append({'C': C, 'A': A, 'R': R, 'r': r, 'S': S, 'E': E, 'span': span, 'X': X, 'Y': Y, 'a': a0, 'dir': 1.0})
        elif kind == 'cylinder':
            ax = g.Axis(); d = prj.unit(v3(ax.Direction())); o = v3(ax.Location())
            bp = boundary_points(f)
            if not len(bp):
                continue
            t = (bp - o) @ d
            cyls.append({'r': g.Radius(), 'S': o + d * t.min(), 'E': o + d * t.max()})
    if not tori:
        return None
    r = float(np.median([t['r'] for t in tori]))
    segs = [dict(kind='arc', **t) for t in tori] + [dict(kind='line', **c) for c in cyls if abs(c['r'] - r) < 1e-3]
    uniq = []
    for s in segs:
        if not any(np.linalg.norm(s['S'] - u['S']) + np.linalg.norm(s['E'] - u['E']) < tol or
                   np.linalg.norm(s['S'] - u['E']) + np.linalg.norm(s['E'] - u['S']) < tol for u in uniq):
            uniq.append(s)
    segs = uniq
    ends = [(i, k) for i in range(len(segs)) for k in ('S', 'E')]

    def partner(i, k):
        p = segs[i][k]
        for j, kk in ends:
            if j != i and np.linalg.norm(segs[j][kk] - p) < 10 * tol:
                return j, kk
        return None
    start = next(((i, k) for i, k in ends if partner(i, k) is None), (0, 'S'))
    chain, used, cur = [], set(), start
    while cur and cur[0] not in used:
        i, k = cur; used.add(i)
        s = dict(segs[i])
        if k == 'E':
            s['S'], s['E'] = s['E'], s['S']
            if s['kind'] == 'arc':                      # traversed backwards
                s['a'], s['dir'] = s['a'] + s['span'], -1.0
        chain.append(s)
        cur = partner(i, 'E' if k == 'S' else 'S')
    pts = [{'name': 'P0', 'xyz': chain[0]['S'].round(4).tolist(), 'radius': None}]
    for s in chain:
        if s['kind'] == 'arc':
            # Split large arcs (180° U-bend and the like) into equal sub-arcs <= 90°: each has
            # a finite virtual bend point (intersection of its tangents).
            n = max(1, int(np.ceil(s['span'] / (np.pi / 2) - 1e-6)))
            part = s['span'] / n
            for k in range(n):
                mid = s['a'] + s['dir'] * part * (k + 0.5)
                V = s['C'] + (np.cos(mid) * s['X'] + np.sin(mid) * s['Y']) * s['R'] / np.cos(part / 2)
                pts.append({'name': f'P{len(pts)}', 'xyz': V.round(4).tolist(), 'radius': round(s['R'], 4),
                            'angle_deg': round(float(np.degrees(part)), 3)})
    pts.append({'name': f'P{len(pts)}', 'xyz': chain[-1]['E'].round(4).tolist(), 'radius': None})
    length = sum(np.linalg.norm(s['E'] - s['S']) if s['kind'] == 'line' else s['R'] * s['span'] for s in chain)
    return {'wire_diameter': round(2 * r, 4), 'points': pts, 'segments': len(chain), 'complete': len(used) == len(segs),
            'centerline_length': round(float(length), 3)}


def extents(shapes):
    corners = []
    for s in shapes:
        b = bbox(s); mn, mx = np.array(b['min']), np.array(b['max'])
        corners += [[x, y, z] for x in (mn[0], mx[0]) for y in (mn[1], mx[1]) for z in (mn[2], mx[2])]
    out = {}
    for name in prj.PRESETS:
        p2 = prj.project(corners, prj.frame_of({'preset': name}))
        w, h = p2.max(0) - p2.min(0)
        out[name] = {'w': round(float(w), 3), 'h': round(float(h), 3)}
    return out


def scale_str(s):
    a, b = s
    return f'{a}:{b}'


def scale_value(text):
    a, b = str(text).replace(' ', '').split(':')
    return float(a) / float(b)


MARGIN = 30.0        # dimensioning space per view (mm on paper)
FILL = 0.6           # at most this much of the free drawing area is planned (gaps, dimensions, texts)
ONE_SHEET_MAX = 'A2'  # largest sheet size that a small set is merged onto


def _views_of(e):
    """Normal views of a part (w, h), largest first."""
    return sorted([(e['front']['w'], e['front']['h']), (e['left']['w'], e['left']['h']), (e['top']['w'], e['top']['h'])],
                  key=lambda wh: -wh[0] * wh[1])


def layout_plan(ext, parts):
    """What belongs on a sheet: assembly (3 views + isometric) and, with several parts, per part its
    manufacturing views (bent part: 1 view + bend table; otherwise 2 views, section for holes)."""
    out = []
    for p in parts:
        bend = p['bend_rows'] > 0
        out.append({'part': p['id'], 'views': 1 if bend else (2 if len(parts) > 1 else 0),
                    'section': p['holes'] > 0, 'bend_table': bend})
    return out


def content_area(ext, parts, plan, k):
    """Estimated space requirement in mm² on paper at scale k (views with dimensioning space, tables)."""
    M = MARGIN
    fw, fh, lw, th = ext['front']['w'], ext['front']['h'], ext['left']['w'], ext['top']['h']
    area = ((fw + lw) * k + 2 * M) * ((fh + th) * k + 2 * M)
    area += (ext['iso']['w'] * k + M) * (ext['iso']['h'] * k + M)
    for p, lp in zip(parts, plan):
        views = _views_of(p['ext'])
        for w, h in views[:lp['views']]:
            area += (w * k + M) * (h * k + M)
        if lp['section']:
            w, h = views[-1]
            area += (w * k + M) * (h * k + M)
        if lp['bend_table']:
            area += 110.0 * (p['bend_rows'] + 2) * 5.0
    return area


def suggestion(ext, parts):
    """One sheet for everything as long as it fits legibly: scale first (largest standard scale per ISO 5455 with the
    largest part at most ~150 mm, down to 1/2.5 of that), then the smallest sheet size up to A2. Only if nothing fits,
    several sheets in the size in which the assembly has room."""
    from sheet import sheet_size, usable_area
    n_parts = len(parts)
    fw, fh, lw = ext['front']['w'], ext['front']['h'], ext['left']['w']
    th = ext['top']['h']
    biggest = max(fw, fh, lw, th, 1e-6)
    ideal = next((s for s in SCALES if biggest * s[0] / s[1] <= 150), SCALES[-1])
    ik = ideal[0] / ideal[1]
    plan = layout_plan(ext, parts)
    scales = [s for s in SCALES if s[0] / s[1] <= ik and s[0] / s[1] >= ik / 2.5]

    def assembly_fits(fmt, orient, k):
        W, H = sheet_size(fmt, orient)
        return (fw + lw) * k + 2 * MARGIN <= W - 30 and (fh + th) * k + 2 * MARGIN <= H - 20 - 45

    def result(fmt, orient, s, sheets, fill, note):
        return {'format': fmt, 'orientation': orient, 'scale': scale_str(s), 'sheets': sheets, 'parts_list': n_parts > 1,
                'views': ['front', 'top', 'left'], 'layout': {'assembly': ['front', 'top', 'left', 'iso'], 'parts': plan, 'fill': round(fill, 2)},
                'note': note}

    one = ORDER[:ORDER.index(ONE_SHEET_MAX) + 1]
    for s in scales:
        k = s[0] / s[1]
        need = content_area(ext, parts, plan, k)
        for fmt in one:
            orient = 'portrait' if fmt == 'A4' else 'landscape'
            fill = need / usable_area(fmt, orient, n_parts)
            if fill <= FILL and assembly_fits(fmt, orient, k):
                return result(fmt, orient, s, 1, fill,
                              'Everything on ONE sheet: assembly with item numbers and parts list, beside it per part its manufacturing views '
                              '(sections, bend tables); small form features as a detail view at a larger scale on the same sheet.'
                              if n_parts > 1 else 'One sheet; sections and detail views as needed on the same sheet.')
    for fmt in ORDER:
        orient = 'portrait' if fmt == 'A4' else 'landscape'
        for s in scales or [SCALES[-1]]:
            k = s[0] / s[1]
            if assembly_fits(fmt, orient, k):
                need = content_area(ext, parts, plan, k)
                cap = FILL * usable_area(fmt, orient, n_parts)
                sheets = max(1, int(-(-need // cap)))
                return result(fmt, orient, s, sheets, need / (sheets * cap / FILL),
                              'Too much for a legible sheet: assembly with parts list on sheet 1, single parts on as few following sheets as possible.')
    return result('A0', 'landscape', SCALES[-1], 1, 1.0, 'Suggestion')


def part_selection(spec, part):
    """Which B-Rep outputs a part uses (key component of the form feature cache): {single_root, solids}."""
    return {'single_root': bool(spec.get('single_root')),
            'solids': [int(i) for i in (part.get('solid_indices') or [part.get('solid_index', 0)])]}


def part_shapes(spec):
    """(parts, {part id: shape}) from spec: solid_index or solid_indices (several outputs per part)."""
    root = prj.load(spec['brep'])
    kids = prj.children(root, bool(spec.get('single_root')))
    parts = spec.get('parts') or [{'id': f'solid-{i}', 'name': f'Body {i + 1}', 'solid_index': i} for i in range(len(kids))]
    shapes = {}
    for p in parts:
        idx = part_selection(spec, p)['solids']
        bad = [i for i in idx if i >= len(kids) or i < 0]
        if bad:
            raise ValueError(f"Part {p['id']}: output {bad[0]} missing (only {len(kids)} outputs in the B-Rep)")
        shapes[p['id']] = kids[idx[0]] if len(idx) == 1 else prj.compound([kids[i] for i in idx])
    return parts, shapes


def analyze_spec(spec):
    parts, by_id = part_shapes(spec)
    out, shapes = [], []
    for p in parts:
        s = by_id[p['id']]; shapes.append(s)
        cyl = cylinders(s)
        bt = bend_table(s)
        out.append({**p, 'bbox': bbox(s), 'volume': round(volume(s), 4),
                    'holes': [c for c in cyl if c['hole']], 'shafts': [c for c in cyl if not c['hole']],
                    'plane_gaps': plane_gaps(s), 'bend_tables': [bt] if bt else []})
    ext = extents(shapes) if shapes else {}
    info = [{'id': p['id'], 'ext': extents([sh]), 'holes': len(p['holes']),
             'bend_rows': max((len(b.get('points') or []) for b in p['bend_tables']), default=0)} for p, sh in zip(out, shapes)]
    return {'parts': out, 'extents': ext, 'suggestion': suggestion(ext, info) if shapes else None}
