"""Planar sketch regions: exact lines, circular arcs, interpolated splines, corner radii
and presets (rounded_rect, slot, ellipse, circle, spline_through, superellipse).

A sketch is built in the XY plane (z = 0) of its construction frame (the feature's
plane/placement, then its local_frame) and returns ONE planar face: the outer loop
counter-clockwise seen from +Z, holes subtracted. Edges are exact OCCT curves (lines,
circles, ellipses, B-splines through points); nothing is tessellated. Input is compiler
output (decimal strings), never executable source.
"""
import math
from geometry import *
from OCP.gp import gp_Elips, gp_Pln
from OCP.GC import GC_MakeArcOfCircle
from OCP.GeomAPI import GeomAPI_Interpolate
from OCP.TColgp import TColgp_HArray1OfPnt
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet2d
from OCP.ChFi2d import ChFi2d_ConstructionError
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.TopAbs import TopAbs_WIRE

EPS = 1e-9
MAX_EDGES = 1024


def _p2(value):
    x, y = float(value[0]), float(value[1])
    require(math.isfinite(x) and math.isfinite(y) and abs(x) <= 1e6 and abs(y) <= 1e6, 'Sketch point out of range.')
    return (x, y)


def _pnt(p):
    return gp_Pnt(p[0], p[1], 0.)


def _dist(a, b):
    return math.hypot(a[0]-b[0], a[1]-b[1])


def _edge(maker, message):
    require(maker.IsDone(), message)
    return maker.Edge()


def _line(a, b):
    require(_dist(a, b) > EPS, 'Sketch segment with zero length.')
    return _edge(BRepBuilderAPI_MakeEdge(_pnt(a), _pnt(b)), 'The sketch line could not be formed.')


def _tangent(edge, at_end):
    curve = BRepAdaptor_Curve(edge)
    u = curve.LastParameter() if at_end else curve.FirstParameter()
    p, v = gp_Pnt(), gp_Vec()
    curve.D1(u, p, v)
    if edge.Orientation() == TopAbs_REVERSED: v.Reverse()
    n = v.Magnitude()
    require(n > 1e-12, 'The sketch segment has a singular tangent.')
    return (v.X()/n, v.Y()/n)


def _interpolate(points, periodic):
    array = TColgp_HArray1OfPnt(1, len(points))
    for i, p in enumerate(points, 1): array.SetValue(i, _pnt(p))
    fit = GeomAPI_Interpolate(array, periodic, 1e-7)
    fit.Perform()
    require(fit.IsDone(), 'The spline through the sketch points cannot be determined (duplicate or too close points?).')
    return fit.Curve()


def _segment_edge(seg, start, previous):
    """One segment from `start`; returns (edge, end point), its ends checked."""
    edge, to = _segment_curve(seg, start, previous)
    first, last = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(edge, True)), BRep_Tool.Pnt_s(TopExp.LastVertex_s(edge, True))
    require(math.hypot(first.X() - start[0], first.Y() - start[1]) <= 1e-7 and math.hypot(last.X() - to[0], last.Y() - to[1]) <= 1e-7,
            'Sketch segment %s does not end at its points.' % seg['type'])
    return edge, to


def _segment_curve(seg, start, previous):
    kind = seg['type']
    to = _p2(seg['to'])
    if kind == 'line':
        return _line(start, to), to
    require(_dist(start, to) > EPS, 'Sketch arc with zero length.')
    if kind == 'arc3':
        via = _p2(seg['via'])
        arc = GC_MakeArcOfCircle(_pnt(start), _pnt(via), _pnt(to))
        require(arc.IsDone(), 'arc3 needs three non-collinear points.')
        return _edge(BRepBuilderAPI_MakeEdge(arc.Value()), 'Sketch arc failed.'), to
    if kind == 'tangent_arc':
        require(previous is not None, 'tangent_arc needs a preceding segment.')
        t = _tangent(previous, True)
        chord = (to[0] - start[0], to[1] - start[1])
        require(abs(t[0] * chord[1] - t[1] * chord[0]) > 1e-9 * _dist(start, to),
                'tangent_arc: the end point lies on the tangent; a line belongs there.')
        arc = GC_MakeArcOfCircle(_pnt(start), gp_Vec(t[0], t[1], 0.), _pnt(to))
        require(arc.IsDone(), 'The tangent arc cannot be determined (the end point lies on the tangent).')
        return _edge(BRepBuilderAPI_MakeEdge(arc.Value()), 'Sketch arc failed.'), to
    if kind == 'center_arc':
        center = _p2(seg['center'])
        r0, r1 = _dist(start, center), _dist(to, center)
        require(r0 > EPS and abs(r0 - r1) <= max(1e-6, 1e-6 * r0),
                'center_arc: start and end point must be equally far from the centre (%.9g vs %.9g mm).' % (r0, r1))
        ccw = seg.get('direction', 'ccw') == 'ccw'
        circle = gp_Circ(gp_Ax2(_pnt(center), gp_Dir(0, 0, 1 if ccw else -1)), r0)
        arc = GC_MakeArcOfCircle(circle, _pnt(start), _pnt(to), True)
        require(arc.IsDone(), 'Center arc failed.')
        return _edge(BRepBuilderAPI_MakeEdge(arc.Value()), 'Sketch arc failed.'), to
    if kind == 'spline':
        points = [start, *(_p2(q) for q in seg.get('through', [])), to]
        for a, b in zip(points, points[1:]): require(_dist(a, b) > EPS, 'Spline support points coincide.')
        return _edge(BRepBuilderAPI_MakeEdge(_interpolate(points, False)), 'Sketch spline failed.'), to
    raise GeometryError('INVALID_SCHEMA', 'Unknown sketch segment type %r.' % kind)


def _wire(edges):
    maker = BRepBuilderAPI_MakeWire()
    for e in edges: maker.Add(e)
    require(maker.IsDone(), 'The sketch segments do not form a connected polyline.')
    wire = maker.Wire()
    require(BRep_Tool.IsClosed_s(wire), 'The sketch loop is not closed.')
    return wire


def _wire_samples(wire, per_edge=16):
    points = []
    explorer = BRepTools_WireExplorer(wire)
    while explorer.More():
        edge = explorer.Current(); curve = BRepAdaptor_Curve(edge)
        u0, u1 = curve.FirstParameter(), curve.LastParameter()
        us = [u0 + (u1 - u0) * i / per_edge for i in range(per_edge)]
        if edge.Orientation() == TopAbs_REVERSED: us = [u1 - (u - u0) for u in us]
        for u in us:
            p = curve.Value(u); points.append((p.X(), p.Y()))
        explorer.Next()
    return points


def signed_area(wire):
    pts = _wire_samples(wire)
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))


def _plane_face(wire):
    maker = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), wire, True)
    require(maker.IsDone(), 'The sketch face could not be formed.')
    return maker.Face()


def _transform2d(shape, center, angle_deg):
    angle = math.radians(angle_deg)
    if not angle and center == (0., 0.): return shape
    t = gp_Trsf()
    if angle: t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), angle)
    m = gp_Trsf(); m.SetTranslation(gp_Vec(center[0], center[1], 0.))
    return BRepBuilderAPI_Transform(shape, m.Multiplied(t), True).Shape()


def _num(value, name, positive=True, allow_zero=False):
    x = float(value)
    require(math.isfinite(x) and abs(x) <= 1e6, 'Sketch dimension %s out of range.' % name)
    if positive: require(x > 0 or allow_zero and x == 0, 'Sketch dimension %s must be positive.' % name)
    return x


def _chain(start, segments):
    """Edges of an explicit segment chain; a final line closes the loop when needed."""
    edges, corners, previous, current = [], [], None, start
    for index, seg in enumerate(segments):
        edge, end = _segment_edge(seg, current, previous)
        edges.append(edge); previous = edge
        corners.append((end, seg.get('corner_radius')))
        current = end
    closed_by_line = False
    if _dist(current, start) > 1e-7:
        edges.append(_line(current, start)); corners.append((start, None)); closed_by_line = True
    return edges, corners, closed_by_line


def _loop(spec, report, name):
    """(wire, corner requests) of one loop: explicit segments or a preset."""
    preset = spec.get('preset')
    single = bool(spec.get('single_edge'))
    if preset is None:
        require(spec.get('start') is not None and spec.get('segments'), 'A sketch loop needs start and segments or a preset.')
        edges, corners, closed = _chain(_p2(spec['start']), spec['segments'])
        if closed: report.setdefault('closing_line_added', []).append(name)
        default = spec.get('corner_radius')
        requests = [(pt, float(r), True) if r is not None else (pt, float(default), False)
                    for pt, r in corners if r is not None or default is not None]
        return _wire(edges), [q for q in requests if q[1] > 0]
    kind = preset['kind']
    center = _p2(preset.get('center', ['0', '0']))
    angle = float(preset.get('angle_deg', '0'))
    if kind == 'rounded_rect':
        w, h = _num(preset['width'], 'width'), _num(preset['height'], 'height')
        if preset.get('radii') is not None:
            bl, br, tr, tl = (_num(r, 'radii', allow_zero=True) for r in preset['radii'])
        else:
            bl = br = tr = tl = _num(preset.get('radius', '0'), 'radius', allow_zero=True)
        require(tr + br <= h + EPS and tl + bl <= h + EPS and tl + tr <= w + EPS and bl + br <= w + EPS,
                'rounded_rect: corner radii exceed the side lengths.')
        x, y = w / 2, h / 2
        # Counter-clockwise from the right side. With single_edge the loop starts on the
        # +X axis, the seam every preset shares, so lofts between presets do not twist.
        right_lo, right_hi = (x, -y + br), (x, y - tr)
        segs = []
        def arc_to(center_pt, end, r):
            if r > 0: segs.append({'type': 'center_arc', 'center': center_pt, 'to': end, 'direction': 'ccw'})
        def line_to(end):
            segs.append({'type': 'line', 'to': end})
        start_on_axis = single and right_lo[1] < -EPS and right_hi[1] > EPS
        start = (x, 0.) if start_on_axis else right_lo
        if right_hi[1] - start[1] > EPS: line_to(right_hi)
        arc_to((x - tr, y - tr), (x - tr, y), tr)
        if (x - tr) - (-x + tl) > EPS: line_to((-x + tl, y))
        arc_to((-x + tl, y - tl), (-x, y - tl), tl)
        if (y - tl) - (-y + bl) > EPS: line_to((-x, -y + bl))
        arc_to((-x + bl, -y + bl), (-x + bl, -y), bl)
        if (x - br) - (-x + bl) > EPS: line_to((x - br, -y))
        arc_to((x - br, -y + br), (x, -y + br), br)
        if start_on_axis: line_to((x, 0.))
        edges = []; current = start; previous = None
        for seg in segs:
            edge, current = _segment_edge(seg, current, previous)
            edges.append(edge); previous = edge
        require(_dist(current, start) <= 1e-7, 'rounded_rect is not closed.')
        wire = _wire(edges)
    elif kind == 'slot':
        length, width = _num(preset['length'], 'length'), _num(preset['width'], 'width')
        r = width / 2; a = length / 2 - r
        require(a > EPS, 'slot: length must be larger than width (otherwise use circle).')
        if single:
            start = (a + r, 0.)
            segs = [{'type': 'center_arc', 'center': (a, 0.), 'to': (a, r)}, {'type': 'line', 'to': (-a, r)},
                    {'type': 'center_arc', 'center': (-a, 0.), 'to': (-a, -r)}, {'type': 'line', 'to': (a, -r)},
                    {'type': 'center_arc', 'center': (a, 0.), 'to': (a + r, 0.)}]
        else:
            start = (a, -r)
            segs = [{'type': 'center_arc', 'center': (a, 0.), 'to': (a, r)}, {'type': 'line', 'to': (-a, r)},
                    {'type': 'center_arc', 'center': (-a, 0.), 'to': (-a, -r)}, {'type': 'line', 'to': (a, -r)}]
        edges, current, previous = [], start, None
        for seg in segs:
            edge, current = _segment_edge(seg, current, previous); edges.append(edge); previous = edge
        wire = _wire(edges)
    elif kind in ('ellipse', 'circle'):
        if kind == 'circle':
            rx = ry = _num(preset['radius'], 'radius')
        else:
            rx, ry = _num(preset['rx'], 'rx'), _num(preset['ry'], 'ry')
        if abs(rx - ry) <= EPS * max(rx, ry):
            curve = gp_Circ(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), gp_Dir(1, 0, 0)), rx)
        elif rx > ry:
            curve = gp_Elips(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), gp_Dir(1, 0, 0)), rx, ry)
        else:
            # gp_Elips needs major >= minor: the major axis is +Y, so the seam lies there.
            curve = gp_Elips(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), gp_Dir(0, 1, 0)), ry, rx)
            report['ellipse_seam'] = '+Y (major axis)'
        wire = _wire([_edge(BRepBuilderAPI_MakeEdge(curve), 'Ellipse failed.')])
    elif kind == 'spline_through':
        points = [_p2(q) for q in preset['points']]
        require(len(points) >= 3, 'spline_through needs at least three points.')
        for a_, b_ in zip(points, points[1:] + points[:1]):
            require(_dist(a_, b_) > EPS, 'spline_through: consecutive points coincide (do not repeat the start point).')
        periodic = preset.get('periodic', True)
        curve = _interpolate(points, True) if periodic else _interpolate(points + [points[0]], False)
        wire = _wire([_edge(BRepBuilderAPI_MakeEdge(curve), 'Spline failed.')])
    elif kind == 'superellipse':
        a_, b_ = _num(preset['a'], 'a'), _num(preset['b'], 'b')
        n = _num(preset.get('exponent', '2.5'), 'exponent')
        require(0.5 <= n <= 20, 'superellipse: exponent 0.5 .. 20.')
        count = int(preset.get('samples', 32))
        require(8 <= count <= 256, 'superellipse: samples 8 .. 256.')
        points = []
        for i in range(count):
            t = 2 * math.pi * i / count; c_, s_ = math.cos(t), math.sin(t)
            points.append((a_ * math.copysign(abs(c_) ** (2 / n), c_), b_ * math.copysign(abs(s_) ** (2 / n), s_)))
        wire = _wire([_edge(BRepBuilderAPI_MakeEdge(_interpolate(points, True)), 'Superellipse failed.')])
    else:
        raise GeometryError('INVALID_SCHEMA', 'Unknown sketch preset %r.' % kind)
    wire = TopoDS.Wire_s(_transform2d(wire, center, angle))
    return wire, []


def _fillet(face, requests, report, name):
    """2D corner radii at the requested loop vertices (exact circle arcs, ChFi2d)."""
    if not requests: return face
    vertices = TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(face, TopAbs_VERTEX, vertices)
    # Tangency of the two edges meeting at each vertex, from the wire order.
    wire = BRepTools.OuterWire_s(face)
    explorer = BRepTools_WireExplorer(wire); ordered = []
    while explorer.More(): ordered.append(TopoDS.Edge_s(explorer.Current())); explorer.Next()
    maker = BRepFilletAPI_MakeFillet2d(face)
    applied, skipped = 0, []
    for point, radius, explicit in requests:
        vertex = None
        for i in range(1, vertices.Extent() + 1):
            p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertices.FindKey(i)))
            if math.hypot(p.X() - point[0], p.Y() - point[1]) <= 1e-7: vertex = TopoDS.Vertex_s(vertices.FindKey(i)); break
        require(vertex is not None, 'Corner radius: corner (%.6g, %.6g) is not in the sketch.' % point)
        incoming = outgoing = None
        for index, edge in enumerate(ordered):
            end = BRep_Tool.Pnt_s(TopExp.LastVertex_s(edge, True)); begin = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(edge, True))
            if math.hypot(end.X() - point[0], end.Y() - point[1]) <= 1e-7: incoming = edge
            if math.hypot(begin.X() - point[0], begin.Y() - point[1]) <= 1e-7: outgoing = edge
        if incoming is not None and outgoing is not None:
            a, b = _tangent(incoming, True), _tangent(outgoing, False)
            if abs(a[0] * b[1] - a[1] * b[0]) < 1e-9 and a[0] * b[0] + a[1] * b[1] > 0:
                # A smooth joint has no corner to round. A default radius passes over it
                # quietly; an explicitly requested one is reported as skipped.
                if explicit: skipped.append([round(point[0], 6), round(point[1], 6)])
                continue
        maker.AddFillet(vertex, radius)
        require(maker.Status() == ChFi2d_ConstructionError.ChFi2d_IsDone,
                'Corner radius %.6g at (%.6g, %.6g) does not fit the adjacent segments (%s).' % (radius, point[0], point[1], maker.Status().name))
        applied += 1
    if skipped: report.setdefault('corner_radii_skipped_tangent', []).extend(skipped)
    if not applied: return face
    maker.Build()
    require(maker.IsDone(), 'The sketch corner radii could not be formed.')
    report['corner_radii_applied'] = report.get('corner_radii_applied', 0) + applied
    faces = list(explore(maker.Shape(), TopAbs_FACE))
    require(len(faces) == 1, 'The corner radii split the sketch face.')
    return TopoDS.Face_s(faces[0])


def _concatenate(wire):
    """The loop as ONE exact B-spline edge (arcs become rational spans), in wire order,
    so lofts between different presets see one edge per section."""
    from OCP.GeomConvert import GeomConvert, GeomConvert_CompCurveToBSplineCurve
    from OCP.Geom import Geom_TrimmedCurve
    joined = None
    explorer = BRepTools_WireExplorer(wire)
    while explorer.More():
        edge = explorer.Current()
        curve = BRepAdaptor_Curve(edge)
        piece = Geom_TrimmedCurve(BRep_Tool.Curve_s(edge, curve.FirstParameter(), curve.LastParameter()), curve.FirstParameter(), curve.LastParameter())
        spline = GeomConvert.CurveToBSplineCurve_s(piece)
        if edge.Orientation() == TopAbs_REVERSED: spline.Reverse()
        if joined is None: joined = GeomConvert_CompCurveToBSplineCurve(spline)
        else: require(joined.Add(spline, 1e-7, True), 'single_edge: the loop cannot be joined into one edge.')
        explorer.Next()
    require(joined is not None, 'single_edge: empty loop.')
    return _edge(BRepBuilderAPI_MakeEdge(joined.BSplineCurve()), 'single_edge: edge failed.')


def _region(spec, report, name):
    wire, requests = _loop(spec, report, name)
    if signed_area(wire) < 0:
        wire = TopoDS.Wire_s(wire.Reversed()); report.setdefault('orientation_normalised', []).append(name)
    face = _fillet(_plane_face(wire), requests, report, name)
    if spec.get('single_edge'):
        face = _plane_face(_wire([_concatenate(BRepTools.OuterWire_s(face))]))
        report['single_edge'] = True
    return face


def build(f):
    """(face, ordered outer and hole edges, report) of a sketch feature."""
    c = f['construction']; report = {}
    face = _region(c, report, 'outer')
    holes = [_region(h, report, 'hole%d' % i) for i, h in enumerate(c.get('holes') or [])]
    if holes:
        cut = boolean_maker('difference', [face], holes)
        faces = list(explore(cut.Shape(), TopAbs_FACE))
        require(len(faces) == 1, 'Sketch holes must lie inside the outer contour and must not split it (%d regions).' % len(faces))
        face = TopoDS.Face_s(faces[0])
        area = GProp_GProps(); BRepGProp.SurfaceProperties_s(face, area)
        require(area.Mass() > 0, 'The sketch face is empty.')
    edges = []
    for wire in explore(face, TopAbs_WIRE):
        explorer = BRepTools_WireExplorer(TopoDS.Wire_s(wire))
        while explorer.More(): edges.append(explorer.Current()); explorer.Next()
    require(len(edges) <= MAX_EDGES, 'The sketch has too many edges.', 'BUDGET_EXCEEDED')
    report['edges'] = len(edges)
    report['wires'] = sum(1 for _ in explore(face, TopAbs_WIRE))
    return face, report
