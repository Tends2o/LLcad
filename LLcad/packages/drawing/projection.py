"""Views for drawings: hidden-line projection (OCCT HLRBRep) of solids,
normal views per projection method 1 (ISO 5456-2), sections with hatch faces, and detail views.

Convention of the directions (as cad_view): 'front' = camera on +Y, 'left' = camera on +X,
'top' = camera on +Z. Image coordinates: x along Vx, y = N x Vx (N points to the camera)."""
import numpy as np
from OCP.BRep import BRep_Builder
from OCP.BRepTools import BRepTools
from OCP.TopoDS import TopoDS_Shape, TopoDS_Compound, TopoDS, TopoDS_Iterator
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_WIRE
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.gp import gp_Ax2, gp_Pnt, gp_Dir, gp_Vec
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Line, GeomAbs_Circle, GeomAbs_Ellipse
from OCP.BRepTools import BRepTools_WireExplorer
from shapely.geometry import LineString, Polygon, MultiLineString, Point
from shapely.ops import unary_union, linemerge

S3 = 1 / np.sqrt(3)
PRESETS = {  # N (towards the camera), Vx (image x)
    'front': ((0, 1, 0), (-1, 0, 0)),
    'back': ((0, -1, 0), (1, 0, 0)),
    'left': ((1, 0, 0), (0, 1, 0)),
    'right': ((-1, 0, 0), (0, -1, 0)),
    'top': ((0, 0, 1), (-1, 0, 0)),
    'bottom': ((0, 0, -1), (-1, 0, 0)),
    'iso': ((S3, S3, S3), (-1 / np.sqrt(2), 1 / np.sqrt(2), 0)),
    'iso_back': ((-S3, -S3, S3), (1 / np.sqrt(2), -1 / np.sqrt(2), 0)),
}


def unit(v):
    v = np.asarray(v, float)
    return v / np.linalg.norm(v)


def frame_of(view):
    """(N, Vx, Vy) of a view description: preset or direction (+ up)."""
    if view.get('preset'):
        N, Vx = PRESETS[view['preset']]
        N, Vx = unit(N), unit(Vx)
    else:
        N = unit(view['direction'])
        up = unit(view.get('up', (0, 0, 1)))
        if abs(np.dot(up, N)) > 0.999:
            up = np.array([0.0, 1.0, 0.0])
        Vx = unit(np.cross(up, N))
    return N, Vx, np.cross(N, Vx)


def project(points, frame):
    N, Vx, Vy = frame
    P = np.atleast_2d(np.asarray(points, float))
    return np.c_[P @ Vx, P @ Vy]


def first_angle_side(frame, reference):
    """Side after a 90° folding according to method 1. The common image axis must run in the same direction."""
    N, x, y = frame
    _, rx, ry = reference
    for side, look, shared, own in (('right', -rx, ry, y), ('left', rx, ry, y),
                                     ('below', ry, rx, x), ('above', -ry, rx, x)):
        if np.allclose(N, look, atol=1e-6) and np.allclose(shared, own, atol=1e-6):
            return side
    return None


def first_angle_sides(frame, reference):
    side = first_angle_side(frame, reference)
    if side:
        return (side,)
    if np.allclose(frame[0], -reference[0], atol=1e-6) and np.allclose(frame[2], reference[2], atol=1e-6):
        return ('right', 'left')
    return ()


def load(path):
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, path, BRep_Builder())
    return shape


def children(shape, single_root=False):
    """Direct children of the compound = outputs in export order. If the model has only one
    output, the root itself is that output (single_root)."""
    from OCP.TopAbs import TopAbs_COMPOUND
    if single_root or shape.ShapeType() != TopAbs_COMPOUND:
        return [shape]
    out = []
    it = TopoDS_Iterator(shape)
    while it.More():
        out.append(it.Value()); it.Next()
    return out or [shape]


def compound(shapes):
    b = BRep_Builder(); c = TopoDS_Compound(); b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


# ------------------------------------------------------------------ Sampling of curves
SAMPLE_FRACS = (0.382, 0.618)   # irregular test points in the segment: catch double arcs on one side and waves with zeros at 1/4, 1/2, 3/4
SAMPLE_MIN_SPAN = 1 / 16        # only segments from 1/16 of the parameter range are tested (at most 16 tests per curve)
SAMPLE_DEPTH = 10               # bisection depth per tested segment (at most 2^10 pieces)
_EXACT = (GeomAbs_Line, GeomAbs_Circle, GeomAbs_Ellipse)     # without inflection point: QuasiUniformDeflection suffices


def sample_curve(c, defl):
    """Points (gp_Pnt) of the curve c (BRepAdaptor_Curve), at most about defl away from the curve. The one sampling for _edges (HLR lines),
    _face_polygon (section faces) and analyze.boundary_points (form features).

    GCPnts_QuasiUniformDeflection as before, plus a re-check. QuasiUniform estimates the deviation of a segment from the end tangents and
    the parameter midpoint; for an S-curve with an inflection point in the middle (likewise a double arc on one side, a wave with zeros at 1/4, 1/2, 3/4)
    the end tangents are equal and the midpoint lies on the chord: the curve would become a straight line with two points. For curves that are neither
    line nor circle nor ellipse, _needs_split tests every segment from SAMPLE_MIN_SPAN of the parameter range; on a finding it is halved
    (_bisect, depth SAMPLE_DEPTH). Segments without a finding stay as they are: correctly sampled curves keep the point list of QuasiUniform.
    If QuasiUniform fails, the end points apply as before; if the re-check fails (e.g. D2), the list of QuasiUniform stays."""
    try:
        d = GCPnts_QuasiUniformDeflection(c, defl)
        n = d.NbPoints()
        pts = [d.Value(i) for i in range(1, n + 1)]
    except Exception:
        return [c.Value(c.FirstParameter()), c.Value(c.LastParameter())]
    if n < 2 or c.GetType() in _EXACT:
        return pts
    try:
        par = [d.Parameter(i) for i in range(1, n + 1)]
        span = SAMPLE_MIN_SPAN * (c.LastParameter() - c.FirstParameter())
        out = [pts[0]]
        for i in range(n - 1):
            ua, ub, pa, pb = par[i], par[i + 1], pts[i], pts[i + 1]
            if ub - ua >= span:
                why = _needs_split(c, ua, ub, pa, pb, defl)
                if why:
                    inner = _bisect(c, ua, ub, pa, pb, defl, SAMPLE_DEPTH, [])
                    # direction change alone: only accept if the curve really deviates from the chord by more than defl (a correctly
                    # sampled inflection point keeps its segment)
                    if why == 'dev' or max(_off_chord(p, pa, pb) for p in inner) > defl:
                        out += inner
            out.append(pb)
        return out
    except Exception:
        return pts


def _off_chord(p, a, b):
    """Distance of the point p from the segment a-b (gp_Pnt)."""
    ax, ay, az = a.X(), a.Y(), a.Z()
    bx, by, bz = b.X() - ax, b.Y() - ay, b.Z() - az
    px, py, pz = p.X() - ax, p.Y() - ay, p.Z() - az
    l2 = bx * bx + by * by + bz * bz
    t = min(max((px * bx + py * by + pz * bz) / l2, 0.0), 1.0) if l2 > 0 else 0.0
    return float(np.sqrt((px - t * bx) ** 2 + (py - t * by) ** 2 + (pz - t * bz) ** 2))


def _bend(c, u):
    """(D1 x D2, curvature |D1 x D2| / |D1|^3) at the parameter u."""
    p, d1, d2 = gp_Pnt(), gp_Vec(), gp_Vec()
    c.D2(u, p, d1, d2)
    k = d1.Crossed(d2)
    n = d1.Magnitude()
    return k, (k.Magnitude() / n ** 3 if n > 1e-12 else 0.0)


def _needs_split(c, ua, ub, pa, pb, defl):
    """Finding of a segment (ua, ub) with end points pa, pb: 'dev' if a curve point at an irregular position (SAMPLE_FRACS) lies farther than defl
    from the chord; 'turn' if the curvature between the ends changes direction (scalar product of the D1 x D2 negative: every point-symmetric
    shape), both curvatures noticeable (an arc with the end curvature would deviate over the chord by more than defl, otherwise the direction is noise); otherwise None."""
    for f in SAMPLE_FRACS:
        if _off_chord(c.Value(ua + f * (ub - ua)), pa, pb) > defl:
            return 'dev'
    ka, ca = _bend(c, ua)
    kb, cb = _bend(c, ub)
    sag = pa.SquareDistance(pb) / 8
    if ca * sag > defl and cb * sag > defl and ka.Dot(kb) < 0:
        return 'turn'
    return None


def _bisect(c, ua, ub, pa, pb, defl, depth, out):
    """Inner points of (ua, ub) in curve order: the midpoint, before and after it the halves with a finding bisected further (up to depth depth)."""
    um = 0.5 * (ua + ub)
    pm = c.Value(um)
    if depth > 1 and _needs_split(c, ua, um, pa, pm, defl):
        _bisect(c, ua, um, pa, pm, defl, depth - 1, out)
    out.append(pm)
    if depth > 1 and _needs_split(c, um, ub, pm, pb, defl):
        _bisect(c, um, ub, pm, pb, defl, depth - 1, out)
    return out


def _edges(comp, defl):
    out = []
    if comp is None or comp.IsNull():
        return out
    ex = TopExp_Explorer(comp, TopAbs_EDGE)
    while ex.More():
        e = TopoDS.Edge_s(ex.Current()); c = BRepAdaptor_Curve(e)
        arr = np.array([[p.X(), p.Y()] for p in sample_curve(c, defl)])
        if len(arr) >= 2 and np.linalg.norm(arr[-1] - arr[0]) + np.sum(np.linalg.norm(np.diff(arr, axis=0), axis=1)) > 1e-6:
            out.append(arr)
        ex.Next()
    return out


def face_count(shapes):
    n = 0
    for s in shapes:
        ex = TopExp_Explorer(s, TopAbs_FACE)
        while ex.More():
            n += 1; ex.Next()
    return n


def hlr(shapes, frame, hidden=True, defl=0.002, mode='auto'):
    """Visible/hidden edges in image coordinates. mode 'exact' (HLRBRep_Algo), 'poly' (mesh,
    fast with many faces) or 'auto' (poly from 2500 faces)."""
    N, Vx, _ = frame
    if mode == 'poly' or (mode == 'auto' and face_count(shapes) > 2500):
        return _hlr_poly(shapes, frame, hidden, defl)
    algo = HLRBRep_Algo()
    for s in shapes:
        algo.Add(s)
    algo.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*N), gp_Dir(*Vx))))
    algo.Update(); algo.Hide()
    h = HLRBRep_HLRToShape(algo)
    vis = _edges(h.VCompound(), defl) + _edges(h.OutLineVCompound(), defl)
    hid = (_edges(h.HCompound(), defl) + _edges(h.OutLineHCompound(), defl)) if hidden else []
    return vis, hid


def _hlr_poly(shapes, frame, hidden, defl):
    from OCP.HLRBRep import HLRBRep_PolyAlgo, HLRBRep_PolyHLRToShape
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    N, Vx, _ = frame
    algo = HLRBRep_PolyAlgo()
    for s in shapes:
        BRepMesh_IncrementalMesh(s, 0.02, False, 0.2, True)
        algo.Load(s)
    algo.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*N), gp_Dir(*Vx))))
    algo.Update()
    h = HLRBRep_PolyHLRToShape(); h.Update(algo)
    vis = _edges(h.VCompound(), defl) + _edges(h.OutLineVCompound(), defl)
    hid = (_edges(h.HCompound(), defl) + _edges(h.OutLineHCompound(), defl)) if hidden else []
    return vis, hid


def clean_hidden(vis, hid, tol):
    """Remove hidden edges that lie on visible ones; join remaining pieces into continuous lines."""
    if not hid:
        return []
    vu = unary_union([LineString(p) for p in vis if len(p) >= 2]).buffer(tol) if vis else None
    keep = []
    for p in hid:
        ls = LineString(p)
        if ls.length < tol:
            continue
        d = ls.difference(vu) if vu is not None else ls
        for g in getattr(d, 'geoms', [d]):
            if g.length > 2.5 * tol:
                keep.append(g)
    if not keep:
        return []
    m = linemerge(unary_union(keep))
    return [np.array(g.coords) for g in getattr(m, 'geoms', [m]) if g.length > 2.5 * tol]


def section(shapes, frame, point):
    """Section: remove everything on the camera side of the plane through point (normal N).
    Returns: cut bodies and the section faces as 2D polygons (for hatching)."""
    N, Vx, Vy = frame
    big = 1e4
    ax = gp_Ax2(gp_Pnt(*np.asarray(point, float)), gp_Dir(*N), gp_Dir(*Vx))
    box = BRepPrimAPI_MakeBox(ax, 2 * big, 2 * big, big).Shape()
    # MakeBox(ax, dx, dy, dz) starts at the axis origin corner: shift so that the plane lies in the middle
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    t = gp_Trsf(); shift = -big * Vx - big * Vy
    t.SetTranslation(gp_Vec(*shift))
    box = BRepBuilderAPI_Transform(box, t, True).Shape()
    cut, faces = [], []
    p0 = np.asarray(point, float)
    for s in shapes:
        c = BRepAlgoAPI_Cut(s, box).Shape()
        cut.append(c)
        ex = TopExp_Explorer(c, TopAbs_FACE)
        while ex.More():
            f = TopoDS.Face_s(ex.Current()); srf = BRepAdaptor_Surface(f)
            if srf.GetType() == GeomAbs_Plane:
                pl = srf.Plane(); n = pl.Axis().Direction(); o = pl.Location()
                nn = np.array([n.X(), n.Y(), n.Z()]); oo = np.array([o.X(), o.Y(), o.Z()])
                if abs(abs(nn @ N) - 1) < 1e-6 and abs((oo - p0) @ N) < 1e-6:
                    poly = _face_polygon(f, frame)
                    if poly is not None and poly.area > 1e-6:
                        faces.append(poly)
            ex.Next()
    return cut, faces


def _face_polygon(face, frame):
    rings = []
    ex = TopExp_Explorer(face, TopAbs_WIRE)
    while ex.More():
        w = TopoDS.Wire_s(ex.Current()); pts = []
        we = BRepTools_WireExplorer(w)
        while we.More():
            seg = sample_curve(BRepAdaptor_Curve(we.Current()), 0.002)
            if we.Orientation() == 1:     # TopAbs_REVERSED
                seg = seg[::-1]
            pts += [[p.X(), p.Y(), p.Z()] for p in seg]
            we.Next()
        if len(pts) >= 3:
            rings.append(Polygon(project(pts, frame)).buffer(0))
        ex.Next()
    if not rings:
        return None
    rings.sort(key=lambda p: -p.area)
    outer = rings[0]
    for r in rings[1:]:
        outer = outer.difference(r)
    return outer


def hatch(poly, spacing, angle_deg=45.0):
    """Hatch lines (ISO 128-50) in a polygon, spacing in polygon units."""
    if poly is None or poly.is_empty:
        return []
    a = np.radians(angle_deg); d = np.array([np.cos(a), np.sin(a)]); n = np.array([-d[1], d[0]])
    minx, miny, maxx, maxy = poly.bounds
    c = np.array([(minx + maxx) / 2, (miny + maxy) / 2]); R = np.hypot(maxx - minx, maxy - miny)
    out = []
    for k in np.arange(-R, R + spacing, spacing):
        base = c + n * k
        seg = LineString([base - d * R, base + d * R]).intersection(poly)
        for g in getattr(seg, 'geoms', [seg]):
            if isinstance(g, LineString) and g.length > 0:
                out.append(np.array(g.coords))
    return out


def clip_circle(lines, center, radius):
    circ = Point(center).buffer(radius, 128)
    out = []
    for p in lines:
        g = LineString(p).intersection(circ)
        for gg in getattr(g, 'geoms', [g]):
            if isinstance(gg, LineString) and gg.length > 0:
                out.append(np.array(gg.coords))
    return out
