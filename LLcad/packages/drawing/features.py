"""Form features per part with stable IDs (spec 5.2 item 0): holes/shafts, planes, square bar, spheres, cones,
chamfers, fillets, arcs and bend tables from the analyze building blocks; plus the freeform flag, a
fingerprint for congruent bodies and the visibility per standard view (ray from the probe point to the camera).

ID = f"{part}#{kind}{n}", n from 1 per kind in the order of a geometry key rounded to 0.001 mm
(independent of the face order of the B-rep)."""
import math
import re
import numpy as np
from shapely.geometry import MultiPoint, Polygon, box
from shapely.ops import unary_union
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Curve2d, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepTools import BRepTools
from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
from OCP.GProp import GProp_GProps
from OCP.Geom import Geom_ConicalSurface, Geom_SphericalSurface
from OCP.GeomAbs import (GeomAbs_Cone, GeomAbs_Sphere, GeomAbs_BSplineSurface, GeomAbs_BezierSurface,
                         GeomAbs_SurfaceOfRevolution, GeomAbs_SurfaceOfExtrusion, GeomAbs_OffsetSurface, GeomAbs_Plane,
                         GeomAbs_Cylinder, GeomAbs_Torus, GeomAbs_Line, GeomAbs_Circle, GeomAbs_Ellipse, GeomAbs_BSplineCurve,
                         GeomAbs_BezierCurve)
from OCP.GeomConvert import GeomConvert_SurfToAnaSurf
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BndLib import BndLib
from OCP.IntCurvesFace import IntCurvesFace_Intersector
from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_WIRE, TopAbs_IN, TopAbs_REVERSED
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape
from OCP.gp import gp_Ax3, gp_Circ, gp_Dir, gp_Lin, gp_Pln, gp_Pnt, gp_Pnt2d, gp_Trsf
import analyze as an
from checks import DRAFT_DEG
import cache
from checks import MINOR_SHARE      # area share from which a minor face is a functional face (A)
import projection as prj

VERSION = 29      # form feature cache: increment on every change of the detection (part of the key, see features_of)
VIEWS = ('front', 'back', 'left', 'right', 'top', 'bottom')
ELEMENTS = ('pocket', 'step', 'slot', 'plane_pattern')      # composite form features, members: plane
MEMBER_ORDER = ('plane_pattern', 'pocket', 'slot', 'step')  # member_of of a plane in several elements: first by this order
KINDS = ('hole', 'shaft', 'plane', 'slant', 'notch', 'square', 'sphere', 'cone', 'chamfer', 'fillet', 'arc', 'bend_table',
         'pattern', 'thread', 'knurl', 'engraving', 'profile') + ELEMENTS
LIFT = 0.01      # probe points this far above the face (in the air): a facing-away face is hit by the ray beyond HIT
HIT = 1e-3       # hit beyond this = hidden
BOX_PAD = 0.01   # mm, bounding box per face for the ray pre-selection larger by this much (+ 10 x largest form tolerance)
ANG_TOL = 0.1    # degrees, tolerance of the circumferential angle limits 90°/180°
CHILD_KINDS = ('slant', 'cone')   # sub-features of pattern members that are dimensioned once on the instance (generic mechanism)
CHILD_RADIAL = 2.0     # a sub-feature belongs to a member if its point lies at most this many Ø from the member axis
CHILD_TOL, CHILD_ANG = 0.05, 0.5     # tolerance of the local signature (mm, degrees)
PRISM_TOL = 1e-3   # N4.10: a face is a lateral face of a prism of direction d if all sampled normals have |n·d| <= this (free-form faces of ribs 0.0, cylinders/planes exact; a value that tolerates the rounding of the B-spline fit and excludes 0.06° of inclination)
PROFILE_FACES = 2  # at least this many lateral faces (residual/partial cylinder faces) form a profile; a single fillet or chamfer never
PROFILE_MAX_ELEMENTS = 60  # more contour elements are surface structure (knurl, toothing), not a shape to be dimensioned individually (typical profiles have fewer than 20 elements)
PROFILE_SPLINE_PTS = 200  # support points per spline contour in the profile (simplification with growing tolerance from 0.002 mm)
PROFILE_PAD = 0.05  # mm (+ 1 % of the diagonal): an end face connects lateral groups if its outline lies within the outline of these groups
PROFILE_SAMPLE = 6  # normal grid per free-form face (n x n)
FACET_MIN = 3  # from this many connected slanted faces (slant, no chamfers) the group forms a facet region
REGION_MIN = 0.01  # share of the surface from which a connected freeform face is a freeform region
FREEFORM = 0.05  # share of non-analytic faces of the surface from which the part is freeform
WIRE = 0.8       # share of tube faces (tori + cylinders with wire radius) from which a bend table describes the part
CAP_AXIAL = 3.0  # obliquely cut wire end: edge points at most this many wire radii axially from the table end (up to ~70° cut inclination)
SQUARE_TOL = 0.03  # cross-section outer contour against size x size square: permitted area deviation (edge rounding)
TANGENT_TOL = 0.01  # mm, arc tangent to a plane: centre ± R against the plane coordinate (as checks.TOL)
EL_TOL = 0.01    # mm, pocket/step/slot/plane sequence: position of floor, wall, top edge and pitch (as checks.TOL)
BEVEL_DEPTH = 0.5   # fillet/chamfer at floor or top edge at most this share of the depth (height) of the element: beyond that it is a sloping wall
BEVEL_WIDTH = 0.25  # ... and at most this share of the smaller transverse dimension (standard practice: chamfers/fillets are small compared with the pocket)
CHAMFER_DEG = (15.0, 75.0)  # the window stays at 15° (measurement: from 10° upward, parts disintegrated into chamfer classes per size, each with one counting leader; the edge breaks of 13.9-14.3° are narrow minor faces, narrow, line "Undimensioned forms"): chamfers only with an angle to the supporting face in this range (standard practice 30°/45°/60°); flatter faces are moulding/print slants, steeper ones ribs
DRILL_TIP_DEG = (110.0, 130.0)  # drill point only with this cone angle (standard point 118°); 60°/140° at a blind hole bottom stays its own cone with obligation
CARRY_COS = 0.9  # axis of an opening hole/thread against the face normal: |cos| >= this (when in doubt the face carries an A element)
DEGENERATE_AREA = 1e-3  # inclined planar faces smaller than this (mm²) or narrower than DEGENERATE_AREA mm are degenerate (area 2e-6, width < 1e-6) and carry neither feature nor obligation
LEG_CARRIER = 0.5   # each leg of a planar chamfer at most this share of the extent of its carrier face across the edge, or (union) CHAMFER_LEG x smallest bounding edge; set to 0.5, not varied (chamfers 0.93x37° on 1.17 mm wide carriers require the union)
PAIR_COVER = 0.9    # a partner plane covers a plane if its bounding rectangle (not the face itself) contains at least this share of the bounding rectangle of the plane in the projection; chosen, not measured against other values
CHAMFER_LEG = 0.25  # planar chamfer = slanted face between two planes, both legs at most this share of the smallest bounding box edge (standard practice: chamfers are small compared with the part; wider slopes are own faces with a dimensioning obligation)
EXT_TOL = 0.02   # mm, plane sequence: equal extent of the members across the pitch (as POS_TOL of the hole patterns)
_CONVERTIBLE = (GeomAbs_BSplineSurface, GeomAbs_BezierSurface, GeomAbs_SurfaceOfRevolution, GeomAbs_SurfaceOfExtrusion,
                GeomAbs_OffsetSurface)


def _r(v, n=4):
    """Rounded and without -0.0 (JSON, key)."""
    if np.ndim(v):
        return [round(float(x), n) + 0.0 for x in v]
    return round(float(v), n) + 0.0


def _canon(v):
    """Direction with fixed sign (largest component positive)."""
    v = np.asarray(v, float)
    return v * (1.0 if v[int(np.argmax(np.abs(v)))] > 0 else -1.0)


def _perp(d):
    """Two unit vectors perpendicular to d (fixed, depends on d only)."""
    x = prj.unit(np.cross(d, np.eye(3)[int(np.argmin(np.abs(d)))]))
    return x, np.cross(d, x)


def _span_deg(angles, step):
    """Circumferential angle (degrees) from angles of edge points; gaps up to the sampling width (rad) count as closed."""
    span = float(an._arc_range(np.asarray(angles))[1])
    return 360.0 if span >= 2 * np.pi - 1.5 * step else float(np.degrees(span))


def _step(defl, r):
    """Largest angle step (rad) when sampling a circle of radius r with chordal deviation defl."""
    return 2 * np.arccos(max(-1.0, 1 - defl / r))


def _surface(face):
    """analyze.analytic, extended by cone and sphere (also stored as B-spline): (kind, gp geometry) or (None, None)."""
    kind, g = an.analytic(face)
    if kind:
        return kind, g
    s = BRepAdaptor_Surface(face)
    t = s.GetType()
    if t == GeomAbs_Cone:
        return 'cone', s.Cone()
    if t == GeomAbs_Sphere:
        return 'sphere', s.Sphere()
    if t in _CONVERTIBLE:
        for tl in (1e-4, 1e-3, 5e-3):
            try:
                a = GeomConvert_SurfToAnaSurf(BRep_Tool.Surface_s(face)).ConvertToAnalytical(tl)
            except Exception:
                a = None
            if isinstance(a, Geom_ConicalSurface):
                return 'cone', a.Cone()
            if isinstance(a, Geom_SphericalSurface):
                return 'sphere', a.Sphere()
            if a is not None:
                break
    return None, None


def _face_table(shape):
    out = []
    for f in an.faces(shape):
        kind, g = _surface(f)
        gp = GProp_GProps(); BRepGProp.SurfaceProperties_s(f, gp)
        out.append({'face': f, 'kind': kind, 'geom': g, 'area': gp.Mass()})
    return out


# ---------------------------------------------------------------- Congruence

def _axes_in_degenerate_plane(shape, E, c, i, j):
    """Equal principal moments: the axes in the plane (E[i], E[j]) are arbitrary. Choose fixed: rectangle of smallest
    area around the projected edge points."""
    size = np.linalg.norm(np.array(an.bbox(shape)['size']))
    pts = an.boundary_points(shape, max(1e-3 * size, 1e-4)) - c
    rect = MultiPoint(np.c_[pts @ E[i], pts @ E[j]].tolist()).minimum_rotated_rectangle
    if rect.geom_type != 'Polygon':
        return E
    (x0, y0), (x1, y1) = list(rect.exterior.coords)[:2]
    th = np.arctan2(y1 - y0, x1 - x0)
    E = E.copy()
    E[i], E[j] = np.cos(th) * E[i] + np.sin(th) * E[j], -np.sin(th) * E[i] + np.cos(th) * E[j]
    return E


def fingerprint(shape):
    """[volume, 3 principal moments of inertia ascending, 3 extents in the principal axis system ascending]."""
    g = GProp_GProps(); BRepGProp.VolumeProperties_s(shape, g)
    pp = g.PrincipalProperties()
    lam = np.array(pp.Moments(), float)
    E = np.array([an.v3(a) for a in (pp.FirstAxisOfInertia(), pp.SecondAxisOfInertia(), pp.ThirdAxisOfInertia())])
    c = an.v3(g.CentreOfMass())
    tol = 1e-6 * max(np.abs(lam).max(), 1e-12)
    same = [(i, j) for i, j in ((0, 1), (1, 2), (0, 2)) if abs(lam[i] - lam[j]) <= tol]
    if len(same) > 1:        # sphere-like: third axis = normal of the largest planar face, otherwise all axes are equally good
        planes = [(x['area'], x['face']) for x in _face_table(shape) if x['kind'] == 'plane']
        if planes:
            _, n = an.face_point_normal(max(planes, key=lambda a: a[0])[1])
            E = np.array([*_perp(prj.unit(n)), prj.unit(n)])
            E = _axes_in_degenerate_plane(shape, E, c, 0, 1)
    elif same:
        E = _axes_in_degenerate_plane(shape, E, c, *same[0])
    t = gp_Trsf(); t.SetTransformation(gp_Ax3(gp_Pnt(*c), gp_Dir(*E[2]), gp_Dir(*E[0])))
    ext = an.bbox(shape.Moved(TopLoc_Location(t)))['size']
    return [float(g.Mass()), *sorted(float(x) for x in lam), *sorted(float(x) for x in ext)]


def congruent(a, b, rel=1e-3):
    """Equal fingerprints up to rel (each value relative to the larger magnitude)."""
    return len(a) == len(b) and all(abs(x - y) <= rel * max(abs(x), abs(y)) + 1e-9 for x, y in zip(a, b))


# ---------------------------------------------------------------- Visibility

_FORMULA = (GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Sphere)
_CURVE_BOX = (GeomAbs_Line, GeomAbs_Circle, GeomAbs_Ellipse)
_PCURVES = _CURVE_BOX + (GeomAbs_BSplineCurve, GeomAbs_BezierCurve)


def _pcurves_safe(face):
    """Do the parameter curves reliably enclose the UV range of the face? BRepTools.UVBounds computes them via
    BndLib_Add2dCurve: lines, circles and ellipses with formulas, B-spline/Bezier curves via control points."""
    ex = TopExp_Explorer(face, TopAbs_EDGE)
    while ex.More():
        if BRepAdaptor_Curve2d(TopoDS.Edge_s(ex.Current()), face).GetType() not in _PCURVES:
            return False
        ex.Next()
    return True


def _face_box(face):
    """(lo, hi) of a box that safely contains the face, or None (unknown: the face is queried for every ray).
    BRepBndLib.Add (without triangulation) is only safe for cylinder, cone and sphere: formulas over the UV range
    enclosed by the parameter curves (_pcurves_safe). Otherwise it samples: torus (minor circle in 45° steps),
    B-spline faces whose UV range leaves the parameter limits, extrusion/revolution/offset (grid) and boundary curves of
    planar faces (support points and estimated deflection). Therefore done here directly: torus = arc of the centre circle over the
    U range (formula) extended by the minor radius; B-spline/Bezier = all control points of the face (convex hull);
    planar face = its boundary (convex hull): lines, circles, ellipses with formulas, B-spline/Bezier curves via all
    control points. All other cases None."""
    kind = BRepAdaptor_Surface(face, False).GetType()
    b, grow = Bnd_Box(), 0.0
    try:
        if kind == GeomAbs_Plane:
            ex = TopExp_Explorer(face, TopAbs_EDGE)
            while ex.More():
                e = TopoDS.Edge_s(ex.Current())
                c = BRepAdaptor_Curve(e)
                t = c.GetType()
                if t in _CURVE_BOX:
                    BRepBndLib.Add_s(e, b, False)
                elif t in (GeomAbs_BSplineCurve, GeomAbs_BezierCurve):
                    g = c.BSpline() if t == GeomAbs_BSplineCurve else c.Bezier()
                    for i in range(1, g.NbPoles() + 1):
                        b.Add(g.Pole(i))
                else:
                    return None
                ex.Next()
        elif kind in (GeomAbs_BSplineSurface, GeomAbs_BezierSurface):
            s = BRepAdaptor_Surface(face, False)
            g = s.BSpline() if kind == GeomAbs_BSplineSurface else s.Bezier()
            for i in range(1, g.NbUPoles() + 1):
                for j in range(1, g.NbVPoles() + 1):
                    b.Add(g.Pole(i, j))
        elif not _pcurves_safe(face):
            return None
        elif kind in _FORMULA:
            BRepBndLib.Add_s(face, b, False)
        elif kind == GeomAbs_Torus:
            s = BRepAdaptor_Surface(face)                     # with the UV range of the face
            t = s.Torus()
            u0, u1 = s.FirstUParameter(), s.LastUParameter()
            circle = gp_Circ(t.Position().Ax2(), t.MajorRadius())
            if u1 - u0 >= 2 * math.pi:
                BndLib.Add_s(circle, 0.0, b)
            else:
                BndLib.Add_s(circle, u0, u1, 0.0, b)
            grow = t.MinorRadius()
        else:
            return None
    except Exception:
        return None
    if b.IsVoid():
        return None
    x0, y0, z0, x1, y1, z1 = b.Get()
    return np.array([x0, y0, z0]) - grow, np.array([x1, y1, z1]) + grow


class _Rays:
    """Ray test with the result of IntCurvesFace_ShapeIntersector (Load(shape, 1e-6), Perform(ray, HIT, 1e9), free =
    no intersection point), faster for large parts: only faces are queried whose safe bounding box (_face_box, enlarged by
    BOX_PAD + 10 x largest form tolerance) the ray hits in the parameter range - elsewhere no face yields a
    point -, faces without a safe box always; faces with few edges first (classifying a hit costs
    per edge), and the first hit occludes."""

    def __init__(self, shape):
        faces = list(an.faces(shape))
        pad = BOX_PAD + 10 * ShapeAnalysis_ShapeTolerance().Tolerance(shape, 1)
        lo, hi, edges = [], [], []
        for f in faces:
            box = _face_box(f)
            lo.append((-np.inf,) * 3 if box is None else box[0] - pad)
            hi.append((np.inf,) * 3 if box is None else box[1] + pad)
            m = TopTools_IndexedMapOfShape()
            TopExp.MapShapes_s(f, TopAbs_EDGE, m)
            edges.append(m.Extent())
        order = sorted(range(len(faces)), key=lambda i: (edges[i], i))
        self.faces = [faces[i] for i in order]
        self.lo = np.array([lo[i] for i in order], float).reshape(-1, 3)
        self.hi = np.array([hi[i] for i in order], float).reshape(-1, 3)
        self.t0 = HIT - pad
        self.ix = [None] * len(order)

    def free(self, p, N, tmax=1e9):
        """Ray from point p (3 values) in direction N (gp_Dir) without an intersection point in the parameter range [HIT, tmax]?"""
        p, d = np.asarray(p, float), np.array([N.X(), N.Y(), N.Z()])
        ok = np.ones(len(self.faces), bool)
        t0, t1 = np.full(len(self.faces), self.t0), np.full(len(self.faces), float(tmax))
        for k in range(3):
            if d[k] == 0:
                ok &= (self.lo[:, k] <= p[k]) & (p[k] <= self.hi[:, k])
            else:
                a, b = (self.lo[:, k] - p[k]) / d[k], (self.hi[:, k] - p[k]) / d[k]
                t0, t1 = np.maximum(t0, np.minimum(a, b)), np.minimum(t1, np.maximum(a, b))
        L = gp_Lin(gp_Pnt(*map(float, p)), N)
        for i in np.nonzero(ok & (t0 <= t1))[0]:
            x = self.ix[i]
            if x is None:
                x = self.ix[i] = IntCurvesFace_Intersector(self.faces[i], 1e-6)
            x.Perform(L, HIT, float(tmax))
            if x.IsDone() and x.NbPnt() > 0:
                return False
        return True

    def first_hit(self, p, N, tmax=1e9):
        """Smallest ray parameter of a hit in the range [HIT, tmax] (ray from point p in direction N, gp_Dir) or None: the first hit
        is the foremost (which part does the view see first at an image point)."""
        p, d = np.asarray(p, float), np.array([N.X(), N.Y(), N.Z()])
        ok = np.ones(len(self.faces), bool)
        t0, t1 = np.full(len(self.faces), self.t0), np.full(len(self.faces), float(tmax))
        for k in range(3):
            if d[k] == 0:
                ok &= (self.lo[:, k] <= p[k]) & (p[k] <= self.hi[:, k])
            else:
                a, b = (self.lo[:, k] - p[k]) / d[k], (self.hi[:, k] - p[k]) / d[k]
                t0, t1 = np.maximum(t0, np.minimum(a, b)), np.minimum(t1, np.maximum(a, b))
        L, best = gp_Lin(gp_Pnt(*map(float, p)), N), None
        for i in np.nonzero(ok & (t0 <= t1))[0]:
            x = self.ix[i]
            if x is None:
                x = self.ix[i] = IntCurvesFace_Intersector(self.faces[i], 1e-6)
            x.Perform(L, HIT, float(tmax))
            if x.IsDone():
                for j in range(1, x.NbPnt() + 1):
                    t = float(x.WParameter(j))
                    best = t if best is None else min(best, t)
        return best


def _intersector(shape):
    return _Rays(shape)


class LazyRays(dict):
    """{part: ray test} that is built only on first access to a part (in the render the check usually asks only neighbours)."""

    def __init__(self, shapes):
        super().__init__(dict.fromkeys(shapes))
        self.shapes = shapes

    def __getitem__(self, pid):
        if dict.__getitem__(self, pid) is None:
            dict.__setitem__(self, pid, _intersector(self.shapes[pid]))
        return dict.__getitem__(self, pid)


class LazyView(dict):
    """Section of a LazyRays on the parts ids (keys), access passes through to the original."""

    def __init__(self, base, ids):
        super().__init__(dict.fromkeys(ids))
        self.base = base

    def __getitem__(self, pid):
        return self.base[pid]


BALLOON_CLEAR_MM = 2.0      # distance of the tip of an item number to foreign parts on paper


def balloon_rings(k):
    """Check rings (model mm) for planner and checker: BALLOON_CLEAR_MM paper at scale k, plus half the ring."""
    r = BALLOON_CLEAR_MM / float(k)
    return [round(r / 2, 4), round(r, 4)]


def first_owner(ixs, q, N, far):
    """Part (key of ixs) that the view in direction -N hits first at image point q (world), otherwise None (background). The ray
    starts far before q on the camera side (N points to the camera). ixs = {part: _Rays}."""
    start, d = np.asarray(q, float) + far * np.asarray(N, float), gp_Dir(*map(float, -np.asarray(N, float)))
    best = (None, None)
    for pid, ix in ixs.items():
        t = ix.first_hit(start, d, 1e9 if best[1] is None else best[1])     # only hits before the best so far: fewer faces
        if t is not None and (best[1] is None or t < best[1]):
            best = (pid, t)
    return best[0]


def view_clearance(ixs, pid, p, frame, rings, far, n=8, boxes=None):
    """(c, foreign) for the point p on the surface of part pid in a view with frame = (N, Vx, Vy): c = largest radius from rings
    (ascending, model mm) up to which the view in the image circle around p (n samples per ring) hits no foreign part first (0: already at p or
    in the first ring); foreign = the first foreign part that the view hits first in this range (None: background at most, or own edge). Item numbers
    end in the interior of a visible face of their own part, never at an edge or face of a foreign one. boxes {part: (min, max)}
    (optional) saves the rays if no foreign bounding box in the image reaches into the neighbourhood."""
    N, Vx, Vy = frame
    if boxes is not None:       # fast path: no foreign part reaches into the largest ring in the image -> free, without rays
        q, R = np.array([float(np.asarray(p) @ Vx), float(np.asarray(p) @ Vy)]), max(rings)
        near = []
        for o, (lo, hi) in boxes.items():
            if o == pid:
                continue
            c = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])], float)
            u, v = c @ Vx, c @ Vy
            if u.min() - R <= q[0] <= u.max() + R and v.min() - R <= q[1] <= v.max() + R:
                near.append(o)
        if not near:
            return float(max(rings)), None
        ixs = {o: ixs[o] for o in ixs if o == pid or o in near}       # (with LazyRays only this builds the ray test of the neighbours)
    if boxes is None:
        ixs = {o: ixs[o] for o in ixs}
    owner = first_owner(ixs, p, N, far)
    if owner is not None and owner != pid:
        return 0.0, owner
    c = 0.0
    for r in rings:
        for k in range(n):
            a = 2 * np.pi * k / n
            o = first_owner(ixs, np.asarray(p, float) + r * (np.cos(a) * Vx + np.sin(a) * Vy), N, far)
            if o is not None and o != pid:
                return c, o              # background (own edge) does not limit the distance: a free-standing part keeps its point
        c = r
    return c, None


def _visible(ix, pts, presets):
    out = {}
    for name in presets:
        N = gp_Dir(*map(float, prj.unit(prj.PRESETS[name][0])))
        out[name] = False
        for p in np.atleast_2d(pts):
            if ix.free(p, N):
                out[name] = True
                break
    return out


def visible_from(shape, pts, presets=VIEWS):
    """{preset: visible}: ray from the probe point towards the camera; a hit beyond HIT hides it, the preset
    is visible if at least one probe point is free."""
    return _visible(_intersector(shape), pts, presets)


def _probes(faces, cap=32):
    """Probe points of a form feature: UV grid on its faces, raised by LIFT along the outer normal."""
    pts = []
    for f in faces:
        u0, u1, v0, v1 = BRepTools.UVBounds_s(f)
        cls = BRepTopAdaptor_FClass2d(f, 1e-7)
        s = BRepAdaptor_Surface(f)
        for n in (4, 9):                            # finer grid only if the coarse one misses the face
            got = 0
            for i in range(n):
                for j in range(n):
                    u, v = u0 + (u1 - u0) * (i + 0.5) / n, v0 + (v1 - v0) * (j + 0.5) / n
                    if cls.Perform(gp_Pnt2d(u, v)) != TopAbs_IN:
                        continue
                    pr = BRepLProp_SLProps(s, u, v, 1, 1e-7)
                    if not pr.IsNormalDefined():
                        continue
                    nn = an.v3(pr.Normal())
                    if f.Orientation() == TopAbs_REVERSED:
                        nn = -nn
                    pts.append(an.v3(pr.Value()) + LIFT * nn); got += 1
            if got:
                break
    pts = np.array(pts) if pts else np.zeros((0, 3))
    if len(pts) > cap:
        pts = pts[np.linspace(0, len(pts) - 1, cap).round().astype(int)]
    return pts


# ---------------------------------------------------------------- Detection

def _wire(shape, table, total):
    """(bend table, tube faces) if the part is a wire/tube sweep (tube faces >= WIRE of the surface).
    A single torus (fillet at a shaft shoulder) would otherwise already give a 'complete' table."""
    bt = an.bend_table(shape)
    if not bt:
        return None, []
    r = bt['wire_diameter'] / 2
    tube = [x for x in table if (x['kind'] == 'cylinder' and abs(x['geom'].Radius() - r) < 1e-3)
            or (x['kind'] == 'torus' and abs(x['geom'].MinorRadius() - r) < 1e-3)]
    if sum(x['area'] for x in tube) < WIRE * total:
        return None, []
    return bt, [x['face'] for x in tube]


def _caps(bt):
    """Check for wire end faces: edge = circle with wire radius around the start or end point of the table, or (obliquely
    cut end, also disintegrated into several planar partial faces) every edge point lies in the wire cross-section of the last
    straight section (distance to the axis through the end <= r) and axially at most CAP_AXIAL × r before or behind the end."""
    r = bt['wire_diameter'] / 2
    pts = [np.array(p['xyz'], float) for p in bt['points']]
    ends = [(pts[0], pts[1] if len(pts) > 1 else None), (pts[-1], pts[-2] if len(pts) > 1 else None)]

    def inside(bp, E, prev):
        if prev is None or np.linalg.norm(E - prev) < 1e-9:
            return False
        d = (E - prev) / np.linalg.norm(E - prev)
        w = bp - E
        ax = w @ d
        radial = np.linalg.norm(w - np.outer(ax, d), axis=1)
        return bool(np.all(radial <= r + 1e-3) and np.all(np.abs(ax) <= CAP_AXIAL * r))

    def is_cap(face):
        bp = an.boundary_points(face)
        if not len(bp):
            return False
        return any(np.all(np.abs(np.linalg.norm(bp - E, axis=1) - r) < 1e-3) or inside(bp, E, prev) for E, prev in ends)
    return is_cap


def _cylinders(shape, table, bb_min, skip_r):
    """hole/shaft (circumference > 180°), fillet (<= 90° and R <= 0.2 * smallest bounding box edge), otherwise arc.
    Blind hole (blind): start = open end, end = closed end (floor), axis points from start to end."""
    faces = []
    for x in table:
        if x['kind'] != 'cylinder':
            continue
        ax = x['geom'].Axis(); r = x['geom'].Radius()
        o, d = an.v3(ax.Location()), prj.unit(an.v3(ax.Direction()))
        p, n = an.face_point_normal(x['face'])
        if p is None:
            continue
        radial = (p - o) - ((p - o) @ d) * d
        faces.append({'face': x['face'], 'o': o, 'd': d, 'r': r, 'hole': float(n @ radial) < 0,
                      'bp': an.boundary_points(x['face'], 0.002 * r)})
    R = np.array([m['r'] for m in faces], float)
    D, O = np.array([m['d'] for m in faces], float).reshape(-1, 3), np.array([m['o'] for m in faces], float).reshape(-1, 3)
    H = np.array([m['hole'] for m in faces], bool)
    out, recs = [], []
    for rec in an.cylinders(shape):
        r = rec['radius']
        if skip_r is not None and abs(r - skip_r) < 1e-3:
            continue
        d, a, L = np.array(rec['axis']), np.array(rec['start']), rec['depth']
        # pre-selection (radius, axis direction and position with margin against rounding); the condition below decides
        near = np.nonzero((np.abs(R - r) < 1e-3) & (H == rec['hole']) & (np.abs(np.abs(D @ d) - 1) < 1e-6 + 1e-9)
                          & (np.linalg.norm(np.cross(O - a, d), axis=1) < 1e-3 + 1e-9))[0]
        mem = [m for m in (faces[i] for i in near)
               if abs(m['r'] - r) < 1e-3 and abs(abs(m['d'] @ d) - 1) < 1e-6 and m['hole'] == rec['hole']
               and np.linalg.norm(np.cross(m['o'] - a, d)) < 1e-3
               and ((m['bp'] - a) @ d).min() > -1e-3 and ((m['bp'] - a) @ d).max() < L + 1e-3]
        if not mem:
            continue
        X, Y = _perp(d)
        q = np.vstack([m['bp'] for m in mem]) - a
        span = _span_deg(np.arctan2(q @ Y, q @ X), _step(0.002 * r, r))
        if span > 180 + ANG_TOL:
            kind = 'hole' if rec['hole'] else 'shaft'
            start, end = rec['start'], rec['end']
            if rec['hole'] and not rec['open_start'] and rec['open_end']:   # blind hole start = opening, end = floor
                start, end, d = end, start, -d                              # end - start stays ∥ axis
            f = {'kind': kind, 'size': rec['diameter'], 'diameter': rec['diameter'], 'axis': _r(d, 6),
                 'start': _r(start), 'end': _r(end),
                 'blind': bool(rec['hole'] and not (rec['open_start'] and rec['open_end'])), 'span_deg': _r(span, 2)}
        else:
            kind = 'fillet' if span <= 90 + ANG_TOL and r <= 0.2 * bb_min else 'arc'
            f = {'kind': kind, 'size': r, 'radius': r, 'center': _r(a + d * L / 2), 'axis': _r(d, 6), 'span_deg': _r(span, 2)}
            a0, sp = an._arc_range(np.arctan2(q @ Y, q @ X))      # directions centre -> arc start/middle/end (leader on the arc edge)
            f['dirs'] = [_r(np.cos(t) * X + np.sin(t) * Y, 6) for t in (a0, a0 + sp / 2, a0 + sp)]
        out.append((f, [m['face'] for m in mem]))
        recs.append(rec)
    return out, recs


def _tori(shape, table, skip_r):
    """Tori outside a complete bend table: fillet with the tube radius; circumference = angle in the tube cross-section."""
    out = []
    for t in an.tori(shape):
        R, rr = t['major'], t['minor']
        if skip_r is not None and abs(rr - skip_r) < 1e-3:
            continue
        C, A = np.array(t['center']), np.array(t['axis'])
        mem = [x['face'] for x in table if x['kind'] == 'torus' and abs(x['geom'].MajorRadius() - R) < 1e-4
               and abs(x['geom'].MinorRadius() - rr) < 1e-4 and np.linalg.norm(an.v3(x['geom'].Location()) - C) < 1e-3
               and abs(abs(an.v3(x['geom'].Axis().Direction()) @ A) - 1) < 1e-6]
        if not mem:
            continue
        q = np.vstack([an.boundary_points(f, 0.002 * rr) for f in mem]) - C
        h = q @ A
        rho = np.linalg.norm(q - np.outer(h, A), axis=1)
        span = _span_deg(np.arctan2(h, rho - R), _step(0.002 * rr, rr))
        out.append(({'kind': 'fillet', 'size': rr, 'radius': rr, 'center': _r(C), 'axis': _r(A, 6), 'span_deg': _r(span, 2),
                     'major_radius': R}, mem))
    return out


RIM_BIN = 10.0        # degrees: resolution of the rim coverage of a cone end


def _rim(bps, apex, e, s0, s1):
    """Which circumferential directions the rim circles of the two cone ends really carry (edge points of the face at the end, counted
    by angle in RIM_BIN-degree bins around the axis; reference direction x, y = _perp(e)). The planner places outline points of the length dimensions
    only where the edge of the face is drawn (a truncated cone has only parts of the circle)."""
    pts = np.vstack(bps)
    X, Y = _perp(e)
    q = pts - apex
    s = q @ e
    ang = np.degrees(np.arctan2((q - np.outer(s, e)) @ Y, (q - np.outer(s, e)) @ X)) % 360.0
    eps = 1e-3 + 1e-4 * (s1 - s0)
    cov = lambda m: sorted({int(a // RIM_BIN) % int(360 / RIM_BIN) for a in ang[m]})
    return {'x': _r(X, 6), 'y': _r(Y, 6), 'c0': cov(s <= s0 + eps), 'c1': cov(s >= s1 - eps)}


def _cones(table, cyl_recs):
    """Cone, length <= 0.1 * Ø at the end of a coaxial cylinder of equal rim radius -> chamfer, otherwise cone."""
    groups, apexes = [], np.zeros((len(table), 3))
    for x in table:
        if x['kind'] != 'cone':
            continue
        g = x['geom']
        apex, h = an.v3(g.Apex()), abs(g.SemiAngle())
        bp = an.boundary_points(x['face'])
        e = prj.unit(an.v3(g.Axis().Direction()))
        if float(np.mean((bp - apex) @ e)) < 0:     # axis from the apex to the wide end
            e = -e
        s = (bp - apex) @ e
        near = np.nonzero(np.linalg.norm(apexes[:len(groups)] - apex, axis=1) < 1e-4 + 1e-9)[0]   # pre-selection with margin
        for gr in (groups[i] for i in near):
            if np.linalg.norm(gr['apex'] - apex) < 1e-4 and gr['e'] @ e > 1 - 1e-6 and abs(gr['h'] - h) < 1e-6:
                gr['s0'], gr['s1'] = min(gr['s0'], s.min()), max(gr['s1'], s.max()); gr['faces'].append(x['face']); gr['area'] += x['area']
                gr['bp'].append(bp)
                break
        else:
            apexes[len(groups)] = apex
            groups.append({'apex': apex, 'e': e, 'h': h, 's0': float(s.min()), 's1': float(s.max()), 'faces': [x['face']], 'area': x['area'], 'bp': [bp]})
    D = np.array([rec['axis'] for rec in cyl_recs], float).reshape(-1, 3)
    S = np.array([rec['start'] for rec in cyl_recs], float).reshape(-1, 3)
    out = []
    for gr in groups:
        apex, e, h, s0, s1 = gr['apex'], gr['e'], gr['h'], gr['s0'], gr['s1']
        width = s1 - s0
        f, host = None, None
        near = np.nonzero((np.abs(np.abs(D @ e) - 1) <= 1e-6 + 1e-9)            # pre-selection of coaxial cylinders with margin
                          & (np.linalg.norm(np.cross(apex - S, D), axis=1) <= 1e-3 + 1e-9))[0]
        for rec in (cyl_recs[i] for i in near):
            d, rc = np.array(rec['axis']), rec['radius']
            if abs(abs(d @ e) - 1) > 1e-6 or np.linalg.norm(np.cross(apex - np.array(rec['start']), d)) > 1e-3:
                continue
            for s in (s0, s1):
                P = apex + e * s
                at_end = min(np.linalg.norm(P - np.array(rec['start'])), np.linalg.norm(P - np.array(rec['end']))) < 1e-3
                if at_end and abs(s * np.tan(h) - rc) < 1e-3:
                    host = host or (s == s0 and s0 != s1, bool(rec['hole']))
                    if width <= 0.1 * 2 * rc + 1e-9 and (rec['hole'] or s == s1):      # on a shaft only the wide end at the cylinder is a chamfer (the narrow end at the cylinder is a cone head)
                        f = {'kind': 'chamfer', 'size': _r(width), 'width': _r(width), 'angle_deg': _r(np.degrees(h), 3),
                             'at': {'center': _r(P), 'radius': rc, 'axis': _r(d, 6)}}
                        break
            if f:
                break
        if f is None:
            d1, d2 = 2 * s0 * np.tan(h), 2 * s1 * np.tan(h)
            sm, u = (s0 + s1) / 2, _perp(e)[0]
            # countersink = cone whose narrow end joins the end of a hole (widens outward); otherwise cone (shaft)
            f = {'kind': 'cone', 'size': _r(d2), 'apex': _r(apex), 'axis': _r(e, 6), 'half_angle_deg': _r(np.degrees(h), 3),
                 'd1': _r(d1), 'd2': _r(d2), 'angle_deg': _r(2 * np.degrees(h), 3), 'length': _r(width),
                 'countersink': bool(host and host[0] and host[1]), 'pt': _r(apex + e * sm + u * sm * np.tan(h)),
                 'area': _r(gr['area'], 3), 'free': host is None, 'host_hole': bool(host and host[1]),
                 'rim': _rim(gr['bp'], apex, e, s0, s1)}      # free = coaxial to no hole/shaft (minor face)
            # cone head - the narrow end borders a shaft of equal Ø, the wide one is larger than it (countersunk head, no end cone/chamfer)
            f['head'] = bool(host and host[0] and not host[1] and d1 > 1e-3 and d2 > d1 + 1e-3)
            if host and not host[0] and host[1] and d1 <= 1e-3 and DRILL_TIP_DEG[0] <= f['angle_deg'] <= DRILL_TIP_DEG[1]:      # point runs out, wide end at the hole end = drill point (~118°)
                f['drill_tip'] = True
        out.append((f, gr['faces']))
    return out


SHORT_CONE_MM = 4.0           # a cone up to this axial length ...
CHAMFER_CONE_DEG = (15.0, 75.0)   # ... with half angle in the chamfer window ...
CHAMFER_CONE_TOL = 0.05       # ... and rim circle in the plane or at the cylinder is a chamfer (mm)


def _mark_chamfer_cones(feats):
    """Sets chamfer_cone on short cones that are geometrically a chamfer - length <= SHORT_CONE_MM, half angle in the
    chamfer window, wide Ø > narrow Ø > 0, length <= half the wide Ø, no countersink, no drill point - and that
    border a plane or hole/shaft: a coaxial cylinder at one end (host: not free) or a planar face (plane/slant) transverse to the axis that contains the rim circle
    of one end (inner cones and nests likewise; a component without a neighbour stays a cone)."""
    planar = [f for f in feats if f['kind'] in ('plane', 'slant') and f.get('normal') is not None and f.get('pt') is not None]
    for f in feats:
        if (f['kind'] != 'cone' or f.get('countersink') or f.get('head') or f.get('drill_tip') or f.get('length') is None or f.get('half_angle_deg') is None
                or float(f['length']) > SHORT_CONE_MM + 1e-9 or not CHAMFER_CONE_DEG[0] <= float(f['half_angle_deg']) <= CHAMFER_CONE_DEG[1]
                or not 1e-3 < float(f['d1']) < float(f['d2']) or float(f['length']) > float(f['d2']) / 2 + 1e-9):
            continue
        apex, e = np.asarray(f['apex'], float), prj.unit(np.asarray(f['axis'], float))
        t = np.tan(np.radians(float(f['half_angle_deg'])))
        ends = [apex + e * float(d) / 2 / t for d in (f['d1'], f['d2'])]
        if not f.get('free') or any(abs(float(np.asarray(q['normal'], float) @ e)) > 1 - 1e-6
                                    and abs(float((np.asarray(q['pt'], float) - P) @ np.asarray(q['normal'], float))) <= CHAMFER_CONE_TOL
                                    for q in planar for P in ends):
            f['chamfer_cone'] = True


def _spheres(table):
    groups = []
    for x in table:
        if x['kind'] != 'sphere':
            continue
        c, r = an.v3(x['geom'].Location()), x['geom'].Radius()
        for gr in groups:
            if np.linalg.norm(gr['c'] - c) < 1e-4 and abs(gr['r'] - r) < 1e-4:
                gr['faces'].append(x['face'])
                break
        else:
            groups.append({'c': c, 'r': r, 'faces': [x['face']]})
    return [({'kind': 'sphere', 'size': _r(2 * g['r']), 'center': _r(g['c']), 'diameter': _r(2 * g['r'])}, g['faces'])
            for g in groups]


def _planes(table, bb_lo, caps):
    """Planar faces with normal ∥ world axis, one plane per unique coordinate per axis; pt = point of the outer edge
    of the largest face closest to the minimum corner of the bounding box; bbox = {min, max} of the same face (does
    a section hit it? The union of several faces at the same position would also do so in gaps)."""
    items = []
    for x in table:
        if x['kind'] != 'plane':
            continue
        p, n = an.face_point_normal(x['face'])
        if p is None:
            continue
        k = int(np.argmax(np.abs(n)))
        if abs(n[k]) < 1 - 1e-6 or (caps and caps(x['face'])):
            continue
        items.append((k, float(p[k]), prj.unit(n), x))
    out = []
    for k in range(3):
        groups = []
        for it in sorted((it for it in items if it[0] == k), key=lambda it: it[1]):
            if groups and it[1] - groups[-1][-1][1] < 1e-4:
                groups[-1].append(it)
            else:
                groups.append([it])
        for g in groups:
            big = max(g, key=lambda it: it[3]['area'])
            bp = an.boundary_points(BRepTools.OuterWire_s(big[3]['face']))
            pt = bp[int(np.argmin(np.linalg.norm(bp - bb_lo, axis=1)))] if len(bp) else an.face_point_normal(big[3]['face'])[0]
            fb = an.bbox(big[3]['face'])
            feature = {'kind': 'plane', 'size': None, 'axis_name': 'xyz'[k], 'coord': _r(np.mean([it[1] for it in g])),
                       'normal': _r(big[2], 6), 'pt': _r(pt), 'bbox': {'min': _r(fb['min']), 'max': _r(fb['max'])}}
            if len(g) > 1:
                # The representative box stays for the section choice. A dimension may also
                # attach to the actually visible partial face of the same plane feature.
                boxes = [an.bbox(it[3]['face']) for it in g if it[2] @ big[2] > 1 - 1e-6]
                feature['patch_bounds'] = [list(box) for box in sorted({tuple(b['min'] + b['max']) for b in boxes})]
            out.append((feature, [it[3]['face'] for it in g]))
    return out


def _merge_collinear(lines):
    """Straight edge pieces (length, start, end, neighbour) with the same neighbour on a line count as one edge (interrupted edge: the span from the first
    to the last piece)."""
    out = []
    for L, a, b, j in lines:
        d = (b - a) / L
        for k, (L0, a0, b0, j0) in enumerate(out):
            d0 = (b0 - a0) / L0
            if j0 == j and abs(float(d @ d0)) > 1 - 1e-9 and float(np.linalg.norm(np.cross(a - a0, d0))) <= 1e-6 * (1 + L0):
                pts = [a0, b0, a, b]
                t = [float(q @ d0) for q in pts]
                lo, hi = pts[int(np.argmin(t))], pts[int(np.argmax(t))]
                out[k] = (float(np.linalg.norm(hi - lo)), lo, hi, j)
                break
        else:
            out.append((L, a, b, j))
    return out


def _planar_chamfer(T, i, p, n, bmin):
    """Inclined planar face i as planar chamfer, otherwise None. A chamfer is a face without inner contour whose two longest
    straight edges are parallel and each border another planar face, and which bevels the edge of these two faces (parallel to
    their line of intersection): both legs (distance of the chamfer edge from the theoretical edge, measured in the respective
    face) are at most LEG_CARRIER x extent of their carrier face across the edge. The outer contour counts (holes in
    the face are inner contours, interrupted edges disintegrate into collinear pieces that count as one edge). Characteristic values: size = longer leg a, angle =
    acute angle of the chamfer to the face that carries a (tan = shorter leg / a, standard practice a×α; 2 × 45° for equal
    legs). Every other face (wide slope, neighbour cylinder, cone, free-form) is a slanted face (kind slant)."""
    face = T.table[i]['face']
    outer = BRepTools.OuterWire_s(face)      # only the outer contour (a hole in the face is an inner contour and does not change the chamfer)
    lines = []
    for e in _iter(outer, TopAbs_EDGE):
        others = [nb for nb in T.emap.FindFromKey(e) if not nb.IsSame(face)] if T.emap.Contains(e) else []
        c = BRepAdaptor_Curve(TopoDS.Edge_s(e))
        if len(others) != 1 or c.GetType() != GeomAbs_Line:
            continue
        j = T.index(others[0])
        if j is None or T.table[j]['kind'] != 'plane':
            continue
        a, b = an.v3(c.Value(c.FirstParameter())), an.v3(c.Value(c.LastParameter()))
        if np.linalg.norm(b - a) > 1e-6:
            lines.append((float(np.linalg.norm(b - a)), a, b, j))
    lines = _merge_collinear(lines)
    lines.sort(key=lambda t: -t[0])
    if len(lines) < 2:
        return None
    (L1, a1, b1, j1), nxt = lines[0], [t for t in lines[1:] if t[3] != lines[0][3]]
    if not nxt:
        return None
    L2, a2, b2, j2 = nxt[0]
    d1, d2 = prj.unit(b1 - a1), prj.unit(b2 - a2)
    q1, n1 = an.face_point_normal(T.table[j1]['face'])
    q2, n2 = an.face_point_normal(T.table[j2]['face'])
    if q1 is None or q2 is None or abs(d1 @ d2) < 1 - 1e-6 or abs(n1 @ n2) > 1 - 1e-6 or abs(n @ n1) > 1 - 1e-6 or abs(n @ n2) > 1 - 1e-6:
        return None
    concave = float(n1 @ (p - q1)) > 1e-6 or float(n2 @ (p - q2)) > 1e-6      # a chamfer sits at a convex edge (the face lies in the material in both neighbour planes)
    if concave and (abs(n1).max() < 1 - 1e-6 or abs(n2).max() < 1 - 1e-6):      # two flanks that meet at a concave edge (the neighbour face is itself inclined) are a notch
        return None
    for e in _iter(outer, TopAbs_EDGE):      # if the face runs tangentially into a cylinder (fillet R8 at the end of a corner slope), it is a wall of the contour, not a chamfer
        for nb in (T.emap.FindFromKey(e) if T.emap.Contains(e) else []):
            j = T.index(nb)
            if j is not None and not nb.IsSame(face) and T.table[j]['kind'] == 'cylinder':
                g = T.table[j]['geom']
                ax = prj.unit(an.v3(g.Axis().Direction()))
                if abs(float(ax @ n)) < 1e-6 and abs(abs(float(n @ (an.v3(g.Location()) - p))) - float(g.Radius())) <= 1e-3 * max(1.0, float(g.Radius())):
                    return None
    c = prj.unit(np.cross(n1, n2))
    if abs(c @ d1) < 1 - 1e-4:
        return None
    P0 = np.linalg.lstsq(np.array([n1, n2, c]), np.array([n1 @ q1, n2 @ q2, 0.0]), rcond=None)[0]
    legs = [float(np.linalg.norm(np.cross((a + b) / 2 - P0, c))) for a, b in ((a1, b1), (a2, b2))]
    if min(legs) < 1e-3:
        return None
    for leg, j, nj in zip(legs, (j1, j2), (n1, n2)):      # legs at most as much as the larger of the two limits: CHAMFER_LEG x smallest bounding edge (so far) or LEG_CARRIER x extent of the carrier face across the edge (loses no previous chamfer)
        bp = an.boundary_points(T.table[j]['face'])
        across = bp @ prj.unit(np.cross(nj, d1)) if len(bp) else np.zeros(1)
        if leg > max(CHAMFER_LEG * bmin, LEG_CARRIER * float(across.max() - across.min())) + 1e-9:
            return None
    big = int(np.argmax(legs))
    nb = (n1, n2)[big]
    angle = float(np.degrees(np.arccos(min(1.0, abs(n @ nb)))))
    if not CHAMFER_DEG[0] <= angle <= CHAMFER_DEG[1]:      # slanted face (slant), at < 3° draft angle
        return None
    return {'kind': 'chamfer', 'size': _r(legs[big]), 'width': _r(legs[big]), 'angle_deg': _r(angle, 3),
            'legs': _r(legs), 'normal': _r(n, 6), 'pt': _r((a1 + b1) / 2), 'edge': [_r(a1), _r(b1)], 'concave': bool(concave)}      # concave: inner chamfer between two axis-parallel planes (floor/wall), stays a chamfer


def _notch_flank(T, i, p, n):
    """Inclined planar face i as flank of a notch: a straight edge (tip edge) borders another inclined planar face j, the edge is concave
    (j lies in front of the plane of i and vice versa), and the face has a second, parallel straight edge (opening edge) at a further face. -> (j, tip edge (a, b),
    centre of the opening edge) or None."""
    face = T.table[i]['face']
    if sum(1 for _ in _iter(face, TopAbs_WIRE)) != 1:
        return None
    lines = []
    for e in _iter(face, TopAbs_EDGE):
        others = [nb for nb in T.emap.FindFromKey(e) if not nb.IsSame(face)] if T.emap.Contains(e) else []
        c = BRepAdaptor_Curve(TopoDS.Edge_s(e))
        if len(others) != 1 or c.GetType() != GeomAbs_Line:
            continue
        j = T.index(others[0])
        a, b = an.v3(c.Value(c.FirstParameter())), an.v3(c.Value(c.LastParameter()))
        if j is not None and np.linalg.norm(b - a) > 1e-6:
            lines.append((float(np.linalg.norm(b - a)), a, b, j))
    lines.sort(key=lambda t: -t[0])
    for L, a, b, j in lines:
        if T.table[j]['kind'] != 'plane':
            continue
        q, nj = an.face_point_normal(T.table[j]['face'])
        if q is None or abs(nj).max() >= 1 - 1e-6 or abs(float(n @ nj)) > 1 - 1e-6:
            continue
        if float(nj @ (p - a)) <= 1e-6 or float(n @ (q - a)) <= 1e-6:      # concave: every flank lies in front of the plane of the other
            continue
        d = prj.unit(b - a)
        outer = [t for t in lines if t[3] != j and T.table[t[3]]['kind'] == 'plane' and abs(float(prj.unit(t[2] - t[1]) @ d)) > 1 - 1e-6]      # opening edge at a planar face (knurl grooves on cylinders are no notches)
        if outer:
            return j, (a, b), (outer[0][1] + outer[0][2]) / 2
    return None


NOTCH_SYM, NOTCH_LONG = 0.1, 3.0      # legs at most 10 % different, length of the tip edge at least 3 x depth
NOTCH_DEG = (20.0, 120.0)      # opening angle of a notch (flatter valleys are slanted faces, no notches); width at most CHAMFER_LEG x smallest bounding box edge


def _notches(flanks, bmin):
    """Notches from the flank pairs {i: (j, (a, b), O)} of _inclined: per pair (i, j) with mutual reference [(feature, [i, j])]. Width = distance of the opening edges, depth = distance of the
    tip edge from their connection, opening angle between the flanks, position pt = centre of the opening."""
    out = []
    for i, (j, (a, b), Oi) in sorted(flanks.items()):
        if j <= i or flanks.get(j, (None,))[0] != i:
            continue
        Oj = flanks[j][2]
        d, M = prj.unit(b - a), (a + b) / 2
        w = float(np.linalg.norm(Oj - Oi))
        if w < 1e-6 or w > CHAMFER_LEG * bmin + 1e-9:
            continue
        u = (Oj - Oi) / w
        depth = float(np.linalg.norm(np.cross(M - Oi, u)))
        v1, v2 = prj.unit(Oi - M), prj.unit(Oj - M)
        ang = float(np.degrees(np.arccos(np.clip(v1 @ v2, -1, 1))))
        mid = (Oi + Oj) / 2
        li, lj = float(np.linalg.norm(Oi - M)), float(np.linalg.norm(Oj - M))
        length = float(np.linalg.norm(b - a))
        if not NOTCH_DEG[0] <= ang <= NOTCH_DEG[1] or abs(li - lj) > NOTCH_SYM * max(li, lj) or length < NOTCH_LONG * depth:      # isosceles and prismatically long (short teeth/sliver faces are no notches)
            continue
        out.append(({'kind': 'notch', 'size': _r(w), 'width': _r(w), 'depth': _r(depth), 'angle_deg': _r(ang, 3), 'axis': _r(_canon(d), 6),
                     'length': _r(float(np.linalg.norm(b - a))), 'pt': _r(mid), 'open': [_r(Oi), _r(Oj)], 'apex': _r(M),
                     'dir': _r(prj.unit(M - mid), 6)}, [i, j]))
    return out


def audit_notch_flanks(shape):
    """Acceptance metric: number of notch flanks that would be classified as a chamfer - inclined planar faces that _planar_chamfer takes as a chamfer AND that form
    a notch flank with a neighbour face mutually (two planar flanks at a concave edge). Should be 0; inner chamfers between axis-parallel planes do not count."""
    table = _face_table(shape)
    bmin = min(an.bbox(shape)['size'])
    T, flanks, chamfers = _Topo(shape, table), {}, set()
    for i, x in enumerate(table):
        if x['kind'] != 'plane':
            continue
        p, n = an.face_point_normal(x['face'])
        if p is None or abs(n).max() >= 1 - 1e-6 or x['area'] < DEGENERATE_AREA:
            continue
        if _planar_chamfer(T, i, p, n, bmin):
            chamfers.add(i)
        fl = _notch_flank(T, i, p, n)
        if fl:
            flanks[i] = fl
    return sum(1 for i in chamfers if i in flanks and flanks.get(flanks[i][0], (None,))[0] == i)


def _iter(shape, kind):
    ex = TopExp_Explorer(shape, kind)
    while ex.More():
        yield ex.Current()
        ex.Next()


def _inclined(shape, table, bb, caps):
    """Faces without a world-axis normal (_planes skips them) - per face a planar chamfer (chamfer) or a slanted face
    (kind slant) with width (smallest extent in the face), length, inclination (angle of the normal to the nearest world axis,
    0 < inclination <= 54.74°), normal, point on the face and bbox. No inclined planar face stays without feature and obligation."""
    T, out, flanks = None, [], {}
    bmin = min(bb['size'])
    for i, x in enumerate(table):
        if x['kind'] != 'plane':
            continue
        p, n = an.face_point_normal(x['face'])
        if p is None or abs(n).max() >= 1 - 1e-6 or (caps and caps(x['face'])):
            continue
        if x['area'] < DEGENERATE_AREA:      # face without area (model artefact) is not a slope (no "∠33°/B0/L4.1")
            continue
        T = T or _Topo(shape, table)
        ch = _planar_chamfer(T, i, p, n, bmin)
        if ch:
            out.append((ch, [x['face']]))
            continue
        fl = _notch_flank(T, i, p, n)
        if fl:
            flanks[i] = fl
        bp = an.boundary_points(x['face'])
        u, w = _perp(n)
        q = np.array([[(v - p) @ u, (v - p) @ w] for v in bp]) if len(bp) else np.zeros((1, 2))
        q = q - q.mean(0)
        axes = np.linalg.svd(q, full_matrices=False)[2] if len(q) > 1 else np.eye(2)
        ext = [float((q @ a).max() - (q @ a).min()) for a in axes]
        if min(ext) < DEGENERATE_AREA:      # sliver (width 0)
            continue
        fb = an.bbox(x['face'])
        out.append(({'kind': 'slant', 'size': _r(min(ext)), 'width': _r(min(ext)), 'length': _r(max(ext)),
                     'angle_deg': _r(np.degrees(np.arccos(min(1.0, float(abs(n).max())))), 3), 'normal': _r(n, 6),
                     'narrow': bool(min(ext) <= CHAMFER_LEG * bmin + 1e-9),      # narrow minor face (width <= 0.25 x smallest bounding edge)
                     'coord': _r(float(p @ n)), 'pt': _r(p), 'area': _r(x['area'], 3),
                     'bbox': {'min': _r(fb['min']), 'max': _r(fb['max'])}}, [x['face']]))
    notches = _notches(flanks, bmin)       # flank pairs at concave edges are a notch, their faces are no slanted faces
    gone = {k for _, ij in notches for k in ij}
    return [o for o in out if not any(o[1][0].IsSame(table[k]['face']) for k in gone)] + [(f, [table[k]['face'] for k in ij]) for f, ij in notches]


def _section(shape, ax, u, w, at, half):
    """Cross-section ⟂ ax through the point at as a shapely face in (u, w) coordinates, inner contours filled."""
    face = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(*map(float, at)), gp_Dir(*map(float, ax))), -half, half, -half, half).Face()
    polys = [prj._face_polygon(f, (ax, u, w)) for f in an.faces(BRepAlgoAPI_Common(shape, face).Shape())]
    polys = [Polygon(g.exterior) for p in polys if p is not None and not p.is_empty
             for g in getattr(p, 'geoms', [p]) if g.geom_type == 'Polygon']
    return unary_union(polys) if polys else None


def _squares(shape, table, bb):
    """Square bar: two thickness pairs of equal thickness, perpendicular to each other, that bound the same square prism:
    common axial length of their four faces >= 3 x thickness, and a cross-section therein has as outer contour the
    size x size square (holes inside do not count, SQUARE_TOL for edge rounding). L-profiles, ribs and
    mixed pairs of stepped square bars are thereby excluded."""
    th = [g for g in an.plane_gaps(shape, limit=10 ** 6) if g['kind'] == 'thickness']
    planes = []
    for x in table:
        if x['kind'] == 'plane':
            p, n = an.face_point_normal(x['face'])
            if p is not None:
                planes.append((p, prj.unit(n), x['face']))
    half = float(np.linalg.norm(bb['size'])) + 1.0
    out, seen = [], set()
    for i in range(len(th)):
        for j in range(i + 1, len(th)):
            a, b = th[i], th[j]
            u, w = np.array(a['normal']), np.array(b['normal'])
            size = a['distance']
            if abs(u @ w) > 1e-6 or abs(size - b['distance']) > 1e-3:
                continue
            ax = _canon(prj.unit(np.cross(u, w)))
            sides, faces = [], []                    # per bounding plane: faces with a matching outer normal
            for g, n in ((a, u), (b, w)):
                for pt, sgn in ((g['p1'], 1.0), (g['p2'], -1.0)):
                    c = float(np.array(pt) @ n)
                    fs = [f for p, m, f in planes if sgn * (m @ n) > 1 - 1e-6 and abs(p @ n - c) < 1e-3]
                    bp = np.vstack([an.boundary_points(f) for f in fs]) if fs else np.zeros((0, 3))
                    if not len(bp):
                        break
                    sides.append((c, float((bp @ ax).min()), float((bp @ ax).max())))
                    faces += fs
            if len(sides) < 4:
                continue
            t0, t1 = max(s[1] for s in sides), min(s[2] for s in sides)
            if t1 - t0 < 3 * size:
                continue
            (u0, u1), (w0, w1) = sorted(s[0] for s in sides[:2]), sorted(s[0] for s in sides[2:])
            mid = u * (u0 + u1) / 2 + w * (w0 + w1) / 2
            sq = box(u0, w0, u1, w1)
            ok = False
            for k in (0.5, 0.3, 0.7, 0.1, 0.9):     # centre first; other positions in case a cross hole lies there
                region = _section(shape, ax, u, w, mid + ax * (t0 + (t1 - t0) * k), half)
                if region is not None and region.symmetric_difference(sq).area <= SQUARE_TOL * size ** 2:
                    ok = True
                    break
            if not ok:
                continue
            center = mid + ax * (t0 + t1) / 2
            key = (_r(size, 3), *_r(ax, 3), *_r(center, 3))
            if key in seen:
                continue
            seen.add(key)
            out.append(({'kind': 'square', 'size': size, 'axis': _r(ax, 6), 'center': _r(center)}, faces))
    return out


# ---------------------------------------------------------------- Pocket, step, slot, plane sequence

class _Topo:
    """Edge adjacency of the faces of the face table; planar position (axis, sign of the normal, coordinate) and
    bounding box per face on demand."""

    def __init__(self, shape, table):
        self.table = table
        self.emap = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, self.emap)
        self.fmap, self.idx, self._pl, self._bb = TopTools_IndexedMapOfShape(), {}, {}, {}
        for i, x in enumerate(table):
            self.idx.setdefault(self.fmap.Add(x['face']), i)

    def index(self, face):
        return self.idx.get(self.fmap.FindIndex(face))

    def plane(self, i):
        """(k, ±1, coordinate) of a planar face with normal ∥ world axis k, otherwise None."""
        if i not in self._pl:
            got = None
            if self.table[i]['kind'] == 'plane':
                p, n = an.face_point_normal(self.table[i]['face'])
                k = int(np.argmax(np.abs(n))) if p is not None else 0
                if p is not None and abs(n[k]) >= 1 - 1e-6:
                    got = (k, 1 if n[k] > 0 else -1, float(p[k]))
            self._pl[i] = got
        return self._pl[i]

    def bbox(self, i):
        if i not in self._bb:
            b = an.bbox(self.table[i]['face'])
            self._bb[i] = (np.array(b['min']), np.array(b['max']))
        return self._bb[i]

    def neighbours(self, face, wire=None):
        """Indices of the neighbouring faces via the edges of wire (default: all edges of the face); None for an edge
    with more or fewer than one neighbouring face (seam, non-manifold)."""
        out, ex = [], TopExp_Explorer(wire if wire is not None else face, TopAbs_EDGE)
        while ex.More():
            e = ex.Current()
            others = [nb for nb in self.emap.FindFromKey(e) if not nb.IsSame(face)] if self.emap.Contains(e) else []
            if len(others) != 1:
                return None
            j = self.index(others[0])
            if j is None:
                return None
            out.append(j)
            ex.Next()
        return list(dict.fromkeys(out))


def _slant(T, j, k, s, C, floor, dress):
    """Companion face j of an element with floor/edge normal s·e_k: at the floor a fillet (cylinder ⟂ k) or chamfer
    (inclined plane), at the top edge a chamfer (inclined plane or cone ∥ k). The face must be open upward (s·n_k > 0) and
    towards the centre C (concave or opening chamfer) and be represented by an own obligation feature (fillet/chamfer, dress),
    otherwise there is no dimensioning obligation for it and the element is not recognised; otherwise False."""
    if j not in dress:
        return False
    x = T.table[j]
    p, n = an.face_point_normal(x['face'])
    if p is None or s * n[k] <= EL_TOL:
        return False
    h = n.copy(); h[k] = 0
    if h @ (C - p) <= EL_TOL:
        return False
    if x['kind'] == 'plane':
        return T.plane(j) is None
    if x['kind'] in ('cylinder', 'cone'):
        a = x['geom'].Axis().Direction()
        ak = abs([a.X(), a.Y(), a.Z()][k])
        return ak <= 1e-6 if (floor and x['kind'] == 'cylinder') else (ak >= 1 - 1e-6 and not floor and x['kind'] == 'cone')
    return False


def _recess(T, i, dress):
    """Pocket, step or slot with the planar face i as floor (conservative, otherwise None): across each edge of the outer contour
    borders either a wall - planar face ⟂ floor or (pocket only) corner cylinder ∥ floor normal, outer normal towards the
    floor, from the floor to the common top edge, there at a carrier face with the floor normal - or an
    open side (planar face ⟂ floor, outer normal away from the floor). Pocket: exactly two walls per transverse axis, no open
    side; slot: two walls of one transverse axis, open at both ends of the other; step: exactly one wall, open opposite.
    Companion faces: floor fillets and chamfers between floor and wall as well as chamfers at the top edge do not belong
    to the members (faces), the characteristic values refer to the theoretical edges (intersection of the planes). Sloping walls,
    split floors and all other neighbouring faces lead to termination."""
    info = T.plane(i)
    if info is None:
        return None
    k, s, cf = info
    face = T.table[i]['face']
    nbs = T.neighbours(face, BRepTools.OuterWire_s(face))
    if not nbs:
        return None
    lo, hi = T.bbox(i)
    C = (lo + hi) / 2
    walls, corners, opens, bevels, wall_js = {}, [], {}, {}, set()

    def classify(j, floor_at):
        """Wall, open side or corner cylinder j that starts at coordinate floor_at (in k); False on termination."""
        x, (jl, jh) = T.table[j], T.bbox(j)
        base, top = (jl[k], jh[k]) if s > 0 else (jh[k], jl[k])
        rises = abs(base - floor_at) <= EL_TOL and s * (top - floor_at) > EL_TOL
        pj = T.plane(j) if x['kind'] == 'plane' else None
        if pj is not None and pj[0] != k:
            k2, s2, c2 = pj
            side = s2 * (C[k2] - c2)
            if side > EL_TOL and rises:
                walls.setdefault((k2, round(c2, 3), s2), []).append((j, float(top)))
                wall_js.add(j)
            elif side < -EL_TOL:
                opens.setdefault((k2, round(c2, 3), s2), []).append(j)
            else:
                return False
            return True
        if x['kind'] == 'cylinder' and rises and floor_at == cf:
            a = x['geom'].Axis().Direction()
            p, n = an.face_point_normal(x['face'])
            if p is None or abs(abs([a.X(), a.Y(), a.Z()][k]) - 1) > 1e-6 or n @ (C - p) <= EL_TOL:
                return False
            corners.append((j, float(x['geom'].Radius()), float(top)))
            return True
        return False

    for j in nbs:
        if _slant(T, j, k, s, C, True, dress):
            bevels[j] = float((T.bbox(j)[1][k]) if s > 0 else T.bbox(j)[0][k])
        elif not classify(j, cf):
            return None
    for j, bt in bevels.items():              # walls and sides behind the fillet/chamfer: start at its top edge
        nb = T.neighbours(T.table[j]['face'])
        if nb is None or abs(bt - cf) <= EL_TOL:
            return None
        for m in nb:
            if m != i and m not in bevels and m not in wall_js and not classify(m, bt):
                return None
        if not any(m in wall_js for m in nb):
            return None
    if bevels:                                # theoretical floor extent: floor including companion faces
        bb = [T.bbox(j) for j in bevels]
        lo, hi = np.min([lo] + [b[0] for b in bb], axis=0), np.max([hi] + [b[1] for b in bb], axis=0)
    wc = wall_js | {j for j, _, _ in corners}
    known = {i} | set(bevels) | wc | {j for g in opens.values() for j in g}
    slants = {}
    for j in wc:
        for m in T.neighbours(T.table[j]['face']) or []:
            if m not in known and _slant(T, m, k, s, C, False, dress):
                slants.setdefault(j, set()).add(m)
    sl = {m for g in slants.values() for m in g}

    def chamfer_top(m):
        """Top edge (carrier plane) of the top edge chamfer m: neighbours only walls, chamfers and carrier planes of the same height."""
        nb, cs = T.neighbours(T.table[m]['face']), set()
        if nb is None or not any(q in wc for q in nb):
            return None
        for q in nb:
            pq = T.plane(q)
            if q in wc or q in sl:
                continue
            if pq is None or pq[:2] != (k, s):
                return None
            cs.add(round(pq[2], 3))
        if len(cs) != 1:
            return None
        c = cs.pop()
        ml, mh = T.bbox(m)
        top_m, base_m = (mh[k], ml[k]) if s > 0 else (ml[k], mh[k])
        return c if abs(top_m - c) <= EL_TOL and s * (c - base_m) > EL_TOL else None

    ctop = {m: chamfer_top(m) for m in sl}
    if None in ctop.values():
        return None
    tops, carried = [], set()
    for j in sorted(wc):
        if j in slants:
            cs = {ctop[m] for m in slants[j]}
            if len(cs) != 1:
                return None
            tops.append(cs.pop())
            carried.add(j)
            continue
        top_j = float(T.bbox(j)[1][k] if s > 0 else T.bbox(j)[0][k])
        if any(T.plane(m) is not None and T.plane(m)[:2] == (k, s) and abs(T.plane(m)[2] - top_j) <= EL_TOL
               for m in (T.neighbours(T.table[j]['face']) or [])):
            carried.add(j)                      # carrier face at the top edge
        tops.append(top_j)
    if any(not any(j in carried for j, _ in g) for g in walls.values()):    # per wall plane at least one wall with a carrier
        return None
    if not walls or any(abs(t - tops[0]) > EL_TOL for t in tops):
        return None
    top = tops[0]
    size = [abs(bt - cf) for bt in bevels.values()] + [abs(ctop[m] - float(T.bbox(m)[0][k] if s > 0 else T.bbox(m)[1][k])) for m in sl]
    cross_min = min(float(hi[m] - lo[m]) for m in range(3) if m != k)
    if size and (max(size) > BEVEL_DEPTH * abs(top - cf) + EL_TOL or max(size) > BEVEL_WIDTH * cross_min + EL_TOL):
        return None                             # large chamfer/fillet: practically a sloping wall
    by_axis = {}
    for (k2, c2, s2) in sorted(walls):
        by_axis.setdefault(k2, []).append((c2, s2))
    cross = [m for m in range(3) if m != k]
    two = lambda m: len(by_axis.get(m, [])) == 2 and [sg for _, sg in by_axis[m]] == [1, -1] and         abs(by_axis[m][0][0] - lo[m]) <= EL_TOL and abs(by_axis[m][1][0] - hi[m]) <= EL_TOL
    ends = lambda m: {round(c, 3) for (km, c, _) in opens if km == m}
    faces = [T.table[j]['face'] for j in [i] + [j for g in walls.values() for j, _ in g] + [j for j, _, _ in corners]]
    head = {'size': None, 'axis_name': 'xyz'[k], 'normal': _r(s * np.eye(3)[k], 6), 'floor': _r(cf), 'top': _r(top),
            'depth': _r(abs(top - cf))}
    if not opens and set(by_axis) == set(cross) and all(two(m) for m in cross):
        radii = [r for _, r, _ in corners]
        if radii and max(radii) - min(radii) > EL_TOL:
            return None
        wl = {'xyz'[m]: [_r(by_axis[m][0][0]), _r(by_axis[m][1][0])] for m in cross}
        ext = sorted((w[1] - w[0] for w in wl.values()), reverse=True)
        center = [(by_axis[m][0][0] + by_axis[m][1][0]) / 2 if m != k else cf for m in range(3)]
        return {'kind': 'pocket', **head, 'walls': wl, 'length': _r(ext[0]), 'width': _r(ext[1]),
                'corner_radius': _r(radii[0]) if radii else None, 'center': _r(center)}, faces
    if corners or len(by_axis) != 1:
        return None
    (m1,) = by_axis
    m2 = next(m for m in cross if m != m1)
    if two(m1) and {km for (km, _, _) in opens} == {m2} and ends(m2) == {round(lo[m2], 3), round(hi[m2], 3)}:
        a1, a2 = by_axis[m1][0][0], by_axis[m1][1][0]
        center = [(a1 + a2) / 2 if m == m1 else cf if m == k else C[m] for m in range(3)]
        return {'kind': 'slot', **head, 'walls': {'xyz'[m1]: [_r(a1), _r(a2)]}, 'width': _r(a2 - a1), 'direction': 'xyz'[m2],
                'center': _r(center)}, faces
    if len(by_axis[m1]) == 1:
        w = by_axis[m1][0][0]
        edge = lo[m1] if abs(w - hi[m1]) <= EL_TOL else hi[m1] if abs(w - lo[m1]) <= EL_TOL else None
        if edge is not None and round(edge, 3) in ends(m1):
            head['height'] = head.pop('depth')
            return {'kind': 'step', **head, 'across': 'xyz'[m1], 'wall': _r(w), 'edge': _r(edge), 'width': _r(abs(w - edge))}, faces
    return None


def _thickness(T, faces, k, sg, c):
    """Thickness of the rib/lamella whose one side is the faces (normal sg·e_k, coordinate c): distance to the opposite side
    (planar face with normal -sg·e_k on the material side) that is adjacent via a connecting face (top face, end face) which lies
    entirely between both planes. Exactly one value (EL_TOL), otherwise None (no counterpart or ambiguous)."""
    ts = []
    for face in faces:
        nb = T.neighbours(face)
        if nb is None:
            return None
        for g in nb:
            if T.table[g]['kind'] != 'plane' or (T.plane(g) is not None and T.plane(g)[0] == k):
                continue
            gl, gh = T.bbox(g)
            for h in T.neighbours(T.table[g]['face']) or []:
                ph = T.plane(h)
                if ph is None or ph[0] != k or ph[1] != -sg or (ph[2] - c) * -sg <= EL_TOL:
                    continue
                lo, hi = sorted((c, ph[2]))
                if gl[k] >= lo - EL_TOL and gh[k] <= hi + EL_TOL:
                    ts.append(abs(ph[2] - c))
    return ts[0] if ts and max(ts) - min(ts) <= EL_TOL else None


def _plane_patterns(T, planes):
    """Plane sequences: >= 3 planes of the same axis, all faces with the same normal, equal pitch (EL_TOL) and equal
    thickness. If every plane has an opposite side (rib, lamella: _thickness), the thicknesses must be equal (EL_TOL); the
    transverse extent - length and height of the ribs - may differ in both transverse axes (varies: transverse axes
    with different extent (EXT_TOL) - they cover pitch, thickness and position, not the lengths and heights). Without an opposite side
    all extents must be equal. In turn the longest sequence (tie: smaller pitch, then smaller
    start coordinate), until none with 3 members remains."""
    items = []
    for f, faces in planes:
        sg = {(T.plane(T.index(x)) or (None, None))[1] for x in faces}
        if len(sg) != 1 or None in sg or not f.get('bbox'):
            continue
        k = 'xyz'.index(f['axis_name'])
        sg = sg.pop()
        ext = {m: (f['bbox']['min'][m], f['bbox']['max'][m]) for m in range(3) if m != k}
        items.append((k, sg, ext, f['coord'], faces, f['bbox'], _thickness(T, faces, k, sg, f['coord'])))
    same = lambda a, b, m: max(abs(a[2][m][0] - b[2][m][0]), abs(a[2][m][1] - b[2][m][1])) <= EXT_TOL

    def compatible(a, b):
        if (a[6] is None) != (b[6] is None) or (a[6] is not None and abs(a[6] - b[6]) > EL_TOL):
            return False
        return all(same(a, b, m) for m in a[2]) if a[6] is None else True      # ribs: equal thickness (EL_TOL) and equal pitch suffice

    out = []
    for key in sorted({it[:2] for it in items}):
        rest = sorted((it for it in items if it[:2] == key), key=lambda it: (it[3], *[round(v, 3) for e in it[2].values() for v in e]))
        while len(rest) >= 3:
            best = None
            for a in range(len(rest)):
                for b in range(a + 1, len(rest)):
                    p = rest[b][3] - rest[a][3]
                    if p <= EL_TOL or not compatible(rest[a], rest[b]):
                        continue
                    run = [a, b]
                    while True:
                        nxt = next((c for c in range(run[-1] + 1, len(rest)) if abs(rest[c][3] - rest[run[-1]][3] - p) <= EL_TOL
                                    and compatible(rest[a], rest[c])), None)
                        if nxt is None:
                            break
                        run.append(nxt)
                    if best is None or (len(run), -p, -rest[a][3]) > (len(best[0]), -best[1], -rest[best[0][0]][3]):
                        best = (run, p)
            if best is None or len(best[0]) < 3:
                break
            mem = [rest[c] for c in best[0]]
            k, s, n = mem[0][0], mem[0][1], len(mem)
            length = mem[-1][3] - mem[0][3]
            varies = ['xyz'[m] for m in mem[0][2] if not all(same(mem[0], it, m) for it in mem)]
            out.append(({'kind': 'plane_pattern', 'size': None, 'axis_name': 'xyz'[k], 'normal': _r(s * np.eye(3)[k], 6),
                         'count': n, 'pitch': _r(length / (n - 1)), 'first': _r(mem[0][3]), 'length': _r(length),
                         'coords': [_r(it[3]) for it in mem], 'bbox': mem[0][5], 'varies': varies},
                        [x for it in mem for x in it[4]]))
            rest = [it for c, it in enumerate(rest) if c not in best[0]]
    return out


def _dressing(T, found):
    """Indices of the faces represented by a fillet or chamfer feature (only they can be companion faces)."""
    return {T.index(x) for f, faces in found if f['kind'] in ('fillet', 'chamfer') for x in faces} - {None}


def _elements(shape, table, found, bb):
    """Pockets, steps and slots (_recess per planar face as floor) and plane sequences (from the planes in found), each
    (feature, faces). A step is a concave edge between two faces, both readings (floor/wall swapped)
    yield the same faces: the one with the floor normal along the smallest partial extent applies, then the one with the
    larger floor, then the smaller axis."""
    T = _Topo(shape, table)
    dress = _dressing(T, found)
    best = {}
    for i, x in enumerate(table):
        got = _recess(T, i, dress) if x['kind'] == 'plane' else None
        if got:
            key = frozenset(T.index(y) for y in got[1])
            k = 'xyz'.index(got[0]['axis_name'])
            rank = (bb['size'][k], -x['area'], k)
            if key not in best or rank < best[key][0]:
                best[key] = (rank, i, got)
    out = [got for _, _, got in sorted(best.values(), key=lambda b: b[1])]
    return out + _plane_patterns(T, [(f, faces) for f, faces in found if f['kind'] == 'plane'])


def _key(f):
    """Rounded geometry key for the numbering."""
    k, q = f['kind'], lambda v: _r(v, 3)
    if k in ('hole', 'shaft'):
        a, b = sorted([q(f['start']), q(f['end'])])
        return (q(f['diameter']), *a, *b)
    if k == 'plane':
        return (f['axis_name'], q(f['coord']))
    if k == 'square':
        return (q(f['size']), *q(_canon(f['axis'])), *q(f['center']))
    if k == 'sphere':
        return (q(f['diameter']), *q(f['center']))
    if k == 'cone':
        return (q(f['d1']), q(f['d2']), *q(f['apex']))
    if k == 'chamfer':
        return (q(f['width']), q(f['at']['radius']), *q(f['at']['center'])) if f.get('at') else (q(f['width']), -1.0, *q(f['pt']))
    if k == 'slant':
        return (q(f['width']), q(f['length']), *q(f['normal']), q(f['coord']), *q(f['pt']))
    if k == 'notch':
        return (q(f['width']), q(f['depth']), *q(f['pt']))
    if k in ('fillet', 'arc'):
        return (q(f['radius']), q(f.get('major_radius') or 0), *q(f['center']), *q(_canon(f['axis'])))
    if k == 'pattern':
        return (q(f['size']), f['count'], f['rows'], *q(f['origin']), *q(_canon(f['axis'])))
    if k == 'thread':
        return (q(f['major_d']), int(f['internal']), q(f['pitch']), *(q(f['start']) if f.get('start') else (0, 0, 0)))
    if k == 'knurl':
        return (q(f['pitch_mm'] or 0), f['count'] or 0, f['profile'] or '')
    if k == 'engraving':
        return (q(f['cap_height'] or 0), q(f['depth'] or 0), f['text'] or '')
    if k == 'plane_pattern':
        return (f['axis_name'], *q(f['normal']), q(f['first']), q(f['pitch']), f['count'])
    if k in ('pocket', 'step', 'slot'):
        return (f['axis_name'], *q(f['normal']), q(f['floor']), q(f['top']),
                *[q(c) for a in sorted(f.get('walls') or {}) for c in f['walls'][a]], q(f.get('wall') or 0), q(f.get('edge') or 0))
    return (q(f.get('wire_diameter') or 0),)


def _tangents(shape, numbered):
    """{arc ID: {axis: plane ID}} - planar face that shares an edge with the arc, with normal ⟂ arc axis and
    coordinate = centre ± R along its axis (TANGENT_TOL). Edge adjacency instead of mere coordinate equality:
    an equally positioned face elsewhere on the part does not fix the arc."""
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    pmap, owner = TopTools_IndexedMapOfShape(), []
    for fid, f, faces in numbered:
        if f['kind'] == 'plane':
            for face in faces:
                if pmap.Add(face) > len(owner):
                    owner.append(f | {'id': fid})
    out = {}
    for fid, f, faces in numbered:
        if f['kind'] != 'arc':
            continue
        A, C, r, tan = np.array(f['axis']), np.array(f['center']), f['radius'], {}
        for face in faces:
            ex = TopExp_Explorer(face, TopAbs_EDGE)
            while ex.More():
                if emap.Contains(ex.Current()):
                    for nb in emap.FindFromKey(ex.Current()):
                        i = pmap.FindIndex(nb)
                        pf = owner[i - 1] if i else None
                        k = 'xyz'.index(pf['axis_name']) if pf else None
                        if pf and abs(A[k]) < 1e-6 and min(abs(pf['coord'] - C[k] - r), abs(pf['coord'] - C[k] + r)) <= TANGENT_TOL:
                            tan.setdefault(pf['axis_name'], pf['id'])
                ex.Next()
        out[fid] = dict(sorted(tan.items()))
    return out


def _regions(shape, table, claimed, part_id, total, owner=None):
    """Freeform regions = connected groups of non-analytic faces (kind None, not taken over by a
    hint), connected by common edges. An analytic face never belongs to one. Per region from REGION_MIN of the
    surface {id '<part>#freeform_region<n>' (largest face first), part, kind, size None, area, min, max (bounding box), pt
    (point on the largest face), ends (per axis edge point at the smallest/largest coordinate)}."""
    T = _Topo(shape, table)
    free = [i for i, x in enumerate(table) if x['kind'] is None and i not in claimed]
    up = {i: i for i in free}

    def find(i):
        while up[i] != i:
            up[i] = up[up[i]]
            i = up[i]
        return i
    for i in free:
        ex = TopExp_Explorer(table[i]['face'], TopAbs_EDGE)
        while ex.More():
            e = ex.Current()
            for nb in (T.emap.FindFromKey(e) if T.emap.Contains(e) else []):
                j = T.index(nb)
                if j in up and j != i:
                    up[find(i)] = find(j)
            ex.Next()
    groups = {}
    for i in free:
        groups.setdefault(find(i), []).append(i)
    ranked = sorted(((sum(table[i]['area'] for i in g), min(g), g) for g in groups.values()), key=lambda t: (-t[0], t[1]))
    out = []
    for area, _, g in ranked:
        if area < REGION_MIN * total:
            continue
        out.append(_region(T, table, g, area, f'{part_id}#freeform_region{len(out) + 1}', part_id))
        out[-1]['share'] = round(float(area / total), 5)
        if owner is not None:
            owner.update({i: out[-1]['id'] for i in g})
    return out


def _region(T, table, g, area, rid, part_id):
    """Region record of a face group g (table indices): {id, part, kind 'freeform_region', size None, area, min, max
    (bounding box), pt (point on the largest face), ends (per axis edge point at the smallest/largest coordinate)}."""
    boxes = [T.bbox(i) for i in g]
    lo, hi = np.min([b[0] for b in boxes], 0), np.max([b[1] for b in boxes], 0)
    big = max(g, key=lambda i: table[i]['area'])
    pts = _probes([table[big]['face']], cap=1)
    mid = (lo + hi) / 2
    bp = np.vstack([an.boundary_points(table[i]['face'], max(0.01, 1e-3 * float(np.linalg.norm(hi - lo)))) for i in g])
    ends = []      # per axis the point on the edge of the region at the smallest/largest coordinate (several: the one nearest the centre)
    for k in range(3):
        pair = []
        for c in (lo[k], hi[k]):
            near = bp[np.abs(bp[:, k] - c) <= 1e-3 + 1e-4 * abs(c)] if len(bp) else bp
            q = near[int(np.argmin(np.linalg.norm(near - mid, axis=1)))] if len(near) else np.where(np.arange(3) == k, c, mid)
            pair.append([round(float(x), 4) for x in q])
        ends.append(pair)
    return {'ends': ends, 'id': rid, 'part': part_id, 'kind': 'freeform_region', 'size': None,
            'area': round(float(area), 3), 'min': [round(float(x), 4) for x in lo], 'max': [round(float(x), 4) for x in hi],
            'pt': [round(float(x), 4) for x in (pts[0] if len(pts) else (lo + hi) / 2)]}


def _carry(numbered, total):
    """Per slanted face and free-standing cone the area share `share` (area / part surface) and, for slanted faces,
    `carries`: a hole opens into it (an open end lies in the plane and within the face bbox, axis ∥ normal
    up to CARRY_COS). Both are set on the feature sets themselves before the facet regions are formed."""
    holes = [f for _, f, _ in numbered if f['kind'] == 'hole' and f.get('start') is not None]
    for _, f, _ in numbered:
        if f['kind'] not in ('slant', 'cone') or f.get('area') is None:
            continue
        f['share'] = _r(f['area'] / total, 5)
        if f['kind'] != 'slant':
            continue
        n, p = prj.unit(np.asarray(f['normal'], float)), np.asarray(f['pt'], float)
        lo, hi = np.asarray(f['bbox']['min'], float), np.asarray(f['bbox']['max'], float)
        f['carries'] = any(
            abs(prj.unit(np.asarray(h['axis'], float)) @ n) >= CARRY_COS
            and any(abs((q - p) @ n) <= 0.05 and np.all(q >= lo - h['diameter'] / 2 - 0.05) and np.all(q <= hi + h['diameter'] / 2 + 0.05)
                    for q in [np.asarray(h['start'], float)] + ([] if h.get('blind') else [np.asarray(h['end'], float)]))
            for h in holes)


def _plane_fields(numbered, total, bb):
    """Per plane (plane) face `area` (sum of the partial faces), area share `share` (÷ part surface), `hull` (lies on a bounding box face of the part), `carries`
    (a hole or shaft opens into it: axis ∥ normal up to CARRY_COS, one end in the plane and in the face bbox) and `pair`/`pair_rel` (distance of the nearest parallel
    plane of the same part whose bounding rectangle covers the bounding rectangle of the plane (not the face itself) in the projection - at least PAIR_COVER of it -, in mm or ÷ largest bounding edge; None without partner).
    The classification as minor face is made by checks.minor_ids alone."""
    planes = [(f, faces) for _, f, faces in numbered if f['kind'] == 'plane']
    holes = [f for _, f, _ in numbered if f['kind'] in ('hole', 'shaft') and f.get('start') is not None and f.get('axis') is not None]
    lo, hi, big = np.asarray(bb['min'], float), np.asarray(bb['max'], float), float(max(bb['size']))
    for f, faces in planes:
        area, boxes = 0.0, [an.bbox(x) for x in faces]
        for x in faces:
            gp = GProp_GProps(); BRepGProp.SurfaceProperties_s(x, gp)
            area += gp.Mass()
        k = 'xyz'.index(f['axis_name'])
        n, p = prj.unit(np.asarray(f['normal'], float)), np.asarray(f['pt'], float)
        blo, bhi = np.min([b['min'] for b in boxes], axis=0), np.max([b['max'] for b in boxes], axis=0)
        f['area'], f['share'] = _r(area, 3), _r(area / total, 5)
        f['hull'] = bool(abs(f['coord'] - lo[k]) <= 1e-3 or abs(f['coord'] - hi[k]) <= 1e-3)
        f['carries'] = any(
            abs(prj.unit(np.asarray(h['axis'], float)) @ n) >= CARRY_COS
            and any(abs((q - p) @ n) <= 0.05 and np.all(q >= blo - h['diameter'] / 2 - 0.05) and np.all(q <= bhi + h['diameter'] / 2 + 0.05)
                    for q in [np.asarray(h['start'], float)] + ([] if h.get('blind') else [np.asarray(h['end'], float)]))
            for h in holes)
    for f, _ in planes:
        o = [j for j in range(3) if j != 'xyz'.index(f['axis_name'])]
        mine = (np.asarray(f['bbox']['min'], float)[o], np.asarray(f['bbox']['max'], float)[o])
        own = float(np.prod(mine[1] - mine[0]))
        best = None
        for g, _ in planes:
            d = abs(g['coord'] - f['coord'])
            if g is f or g['axis_name'] != f['axis_name'] or d <= 1e-4 or (best is not None and d >= best):
                continue
            ov = np.clip(np.minimum(mine[1], np.asarray(g['bbox']['max'], float)[o]) - np.maximum(mine[0], np.asarray(g['bbox']['min'], float)[o]), 0.0, None)
            if own > 1e-9 and float(np.prod(ov)) >= PAIR_COVER * own:
                best = d
        f['pair'], f['pair_rel'] = (None, None) if best is None else (_r(best, 4), _r(best / big, 5))


def _transitions(shape, numbered):
    """{arc ID: (True, [neighbour IDs])} if the arc runs tangentially into at least two different planar faces (plane or slant, via a common edge): normal ⟂
    arc axis and distance of the face from the axis = R (TANGENT_TOL). Such a transition arc is fixed by its two neighbours and needs only R."""
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    owners, owner = TopTools_IndexedMapOfShape(), {}      # face -> ID of the planar-face feature (plane, slant, chamfer)
    for fid, f, faces in numbered:
        if f['kind'] in ('plane', 'slant', 'chamfer'):
            for x in faces:
                owner.setdefault(owners.Add(x), fid)
    out = {}
    for fid, f, faces in numbered:
        if f['kind'] != 'arc':
            continue
        A, C, r = prj.unit(np.asarray(f['axis'], float)), np.asarray(f['center'], float), float(f['radius'])
        seen, normals, ids = TopTools_IndexedMapOfShape(), [], []
        for face in faces:
            for e in _iter(face, TopAbs_EDGE):
                for nb in (emap.FindFromKey(e) if emap.Contains(e) else []):
                    if nb.IsSame(face) or any(nb.IsSame(x) for x in faces) or _surface(TopoDS.Face_s(nb))[0] != 'plane':
                        continue
                    q, n = an.face_point_normal(TopoDS.Face_s(nb))
                    if q is not None and abs(float(prj.unit(n) @ A)) <= 1e-6 and abs(abs(float(prj.unit(n) @ (C - q))) - r) <= TANGENT_TOL * max(1.0, r):
                        if seen.Add(nb) > len(normals):
                            normals.append(prj.unit(n))
                            ids.append(owner.get(owners.FindIndex(nb)))
        out[fid] = (any(abs(float(a @ b)) < 1 - 1e-6 for a in normals for b in normals), [i for i in ids if i])      # two non-parallel neighbours fix the centre (two parallel walls of a groove leave it free along the groove)
    return out


def _facets(shape, table, numbered, part_id, claimed=(), total=1.0):
    """Facet regions = connected groups (common edge) of at least FACET_MIN slanted faces (slant; chamfers
    do not count). Per group one region like a freeform region (_region) with facet True, id '<part>#facet_region<n>' (largest
    face first) and members (the IDs of the slanted faces, which thereby drop out of the individual obligation). Single slopes and
    pairs stay own features; faces taken over by a hint (claimed, e.g. knurl flanks) form no region.
    Returns ([region], {slant ID})."""
    sl = [(fid, faces[0]) for fid, f, faces in numbered if f['kind'] == 'slant' and faces and f['angle_deg'] >= DRAFT_DEG
          and not f.get('carries') and f.get('share', 0.0) < MINOR_SHARE]      # draft slopes are covered by the hint per part
    if len(sl) < FACET_MIN:
        return [], set()
    T = _Topo(shape, table)
    idx = {T.index(face): fid for fid, face in sl if T.index(face) not in claimed}
    up = {i: i for i in idx}

    def find(i):
        while up[i] != i:
            up[i] = up[up[i]]
            i = up[i]
        return i
    for i in idx:
        for e in _iter(table[i]['face'], TopAbs_EDGE):
            for nb in (T.emap.FindFromKey(e) if T.emap.Contains(e) else []):
                j = T.index(nb)
                if j in up and j != i:
                    up[find(i)] = find(j)
    groups = {}
    for i in idx:
        groups.setdefault(find(i), []).append(i)
    ranked = sorted(((sum(table[i]['area'] for i in g), min(g), g) for g in groups.values() if len(g) >= FACET_MIN), key=lambda t: (-t[0], t[1]))
    out, gone = [], set()
    for area, _, g in ranked:
        r = _region(T, table, g, area, f'{part_id}#facet_region{len(out) + 1}', part_id)
        r['facet'] = True
        r['share'] = round(float(area / total), 5)
        r['members'] = sorted(idx[i] for i in g)
        gone.update(r['members'])
        out.append(r)
    return out, gone


def _bpts(face, defl=0.01, dense=48):
    """Edge points of a face; edges that are neither a line nor a circle (B-splines) additionally in equal parameter steps (the chordal-deviation sampling returns only the end points for
    some spline edges)."""
    pts = [an.boundary_points(face, defl)]
    for e in _iter(face, TopAbs_EDGE):
        c = BRepAdaptor_Curve(TopoDS.Edge_s(e))
        if c.GetType() not in (GeomAbs_Line, GeomAbs_Circle):
            u0, u1 = c.FirstParameter(), c.LastParameter()
            pts.append(np.array([an.v3(c.Value(u0 + (u1 - u0) * k / dense)) for k in range(dense + 1)]))
    return np.vstack(pts)


def _normals(face, n=PROFILE_SAMPLE):
    """Sampled face normals of a free-form face (n x n in the UV domain)."""
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    s = BRepAdaptor_Surface(face)
    out = []
    for a in np.linspace(u0, u1, n):
        for b in np.linspace(v0, v1, n):
            pr = BRepLProp_SLProps(s, float(a), float(b), 1, 1e-7)
            if pr.IsNormalDefined():
                out.append(an.v3(pr.Normal()))
    return np.array(out).reshape(-1, 3)


def _free_dir(N):
    """Generatrix of a face from its normals: smallest singular vector if all normals lie (almost) in one plane (prism), otherwise None."""
    if len(N) < 3:
        return None
    sv, vt = np.linalg.svd(N, full_matrices=True)[1:]
    d = vt[-1]
    return prj.unit(d) if float(np.abs(N @ d).max()) <= PRISM_TOL and sv[1] > 1e-3 else None


def _same_dir(a, b):
    return abs(float(a @ b)) >= 1 - 1e-6


def _profile_outline(table, idx, d, X, Y, origin):
    """Profile curve of the lateral faces idx (without Boolean operation): the edge points of every lateral face lie on the profile curve after projection along d.
    Plane -> segment, cylinder (axis ∥ d) -> arc, otherwise polyline. Coordinates relative to origin in the basis X, Y."""
    out = []
    for i in idx:
        x = table[i]['face']
        bp = _bpts(x, 0.01)
        if not len(bp):
            continue
        q = np.c_[(bp - origin) @ X, (bp - origin) @ Y]
        kind = table[i]['kind']
        if kind == 'plane':
            p, n = an.face_point_normal(x)
            n2 = np.array([n @ X, n @ Y])
            n2 = n2 / np.linalg.norm(n2)
            t2 = np.array([-n2[1], n2[0]])
            s = q @ t2
            c = float(q[0] @ n2)
            out.append({'t': 'line', 'p0': _r(c * n2 + s.min() * t2, 4), 'p1': _r(c * n2 + s.max() * t2, 4)})
        elif kind == 'cylinder':
            g = table[i]['geom']
            loc = an.v3(g.Axis().Location()) - origin
            c2 = np.array([loc @ X, loc @ Y])
            ang = np.arctan2(q[:, 1] - c2[1], q[:, 0] - c2[0])
            a0, span = an._arc_range(ang)
            p, n = an.face_point_normal(x)
            rp = np.array([(p - origin) @ X, (p - origin) @ Y]) - c2
            out.append({'t': 'arc', 'c': _r(c2, 4), 'r': _r(float(g.Radius()), 4), 'a0': _r(np.degrees(a0), 3), 'span': _r(np.degrees(span), 3),
                        'convex': bool(float(np.array([n @ X, n @ Y]) @ rp) > 0)})
        else:
            pts = np.unique(np.round(q, 4), axis=0)
            if len(pts) < 2:
                continue
            b = pts[int(np.argmax(np.linalg.norm(pts - pts.mean(0), axis=1)))]
            a = pts[int(np.argmax(np.linalg.norm(pts - b, axis=1)))]
            rest, cur, chain = pts[np.abs(pts - a).max(1) > 1e-9], a, [a]
            while len(rest):
                j = int(np.argmin(np.hypot(rest[:, 0] - cur[0], rest[:, 1] - cur[1])))
                cur = rest[j]
                chain.append(cur)
                rest = np.delete(rest, j, axis=0)
            from shapely.geometry import LineString
            ls, tol = LineString(chain), 0.002
            simp = ls.simplify(tol)
            while len(simp.coords) > PROFILE_SPLINE_PTS:      # raise the tolerance until the number of points fits (never truncate)
                tol *= 2
                simp = ls.simplify(tol)
            out.append({'t': 'spline', 'pts': [_r(p, 4) for p in simp.coords]})
    return _join_arcs(out)


def _join_arcs(out):
    """Cylinder faces that are split at the seam (a lobe across the seam) give two adjoining arcs of equal centre and radius: one arc."""
    arcs = [e for e in out if e['t'] == 'arc']
    done = True
    while done:
        done = False
        for a in arcs:
            for b in arcs:
                if a is not b and abs(a['r'] - b['r']) < 1e-3 and np.hypot(a['c'][0] - b['c'][0], a['c'][1] - b['c'][1]) < 1e-3 and a['convex'] == b['convex']                         and abs((a['a0'] + a['span'] - b['a0'] + 180) % 360 - 180) < 0.05:
                    a['span'] = _r(a['span'] + b['span'], 3)
                    arcs.remove(b)
                    out.remove(b)
                    done = True
                    break
            if done:
                break
    return out


def _profile_views(ix, it, X, Y, d):
    """Normal views in which an end of the specimen is visible (view along ±d: ray from points of the contour at the end, LIFT outside)."""
    import profiles
    if np.abs(d).max() < 1 - 1e-3:
        return []
    pts = []
    for e in it['outline']:
        s = profiles.sample(e, 1.0)
        pts += s[:: max(1, len(s) // 3)]
    out = []
    base = it['origin'] - d * float(it['origin'] @ d)
    for sign, coord in ((1, it['hi']), (-1, it['lo'])):
        if any(ix.free(base + X * p[0] + Y * p[1] + d * (coord + sign * LIFT), gp_Dir(*map(float, sign * d))) for p in pts):
            out += [v for v in VIEWS if np.allclose(prj.unit(prj.PRESETS[v][0]), sign * d, atol=1e-6)]
    return out


def _end_closes(T, table, j, core, d, nbrs):
    """An end face j (normal ∥ d) connects lateral groups only if its outer boundary contour consists essentially of edges of the lateral faces (core): the edges to all
    other faces (caps of a rib, attachment to a body) may together be at most half as long as the lateral edges. A rib between two contours (2 x 32.9 mm
    spline, 2 x 4 mm cap) satisfies that, the top face of a box with corner roundings (4 roundings against 4 walls) does not."""
    from OCP.BRepTools import BRepTools
    wire = BRepTools.OuterWire_s(table[j]['face'])
    core_len = other_len = 0.0
    for e in _iter(wire, TopAbs_EDGE):
        gp = GProp_GProps()
        BRepGProp.LinearProperties_s(e, gp)
        others = [nb for nb in (T.emap.FindFromKey(e) if T.emap.Contains(e) else []) if not nb.IsSame(table[j]['face'])]
        if len(others) == 1 and T.index(others[0]) in core:
            core_len += gp.Mass()
        else:
            other_len += gp.Mass()
    return core_len > 0 and other_len <= 0.5 * core_len


def _profiles(shape, table, numbered, claimed, total, part_id, owner, ix):
    """N4.10: prismatic profile shapes. Candidates are residual faces (slanted, fillet, arc and free-form faces) together with adjoining
    partial cylinders; the generatrix d comes from the cylinder axes and the normal plane of the free-form faces. Lateral faces of one direction
    (all normals ⟂ d up to PRISM_TOL), connected via common edges - groups that an end face (normal ∥ d) connects whose
    outline lies within the outline of the groups (rib with two contours), together - with at least PROFILE_FACES faces, among them a curved one,
    are a profile. Congruent ones (profiles.Shape.matches: rotation, mirroring, translation, tolerance) form a class; from MINOR_SHARE of the part surface a feature
    {id '<part>#profile<n>', kind 'profile', axis, members (IDs of the specimen: form features, freeform regions), copies, copy_members, length,
    outline, frame, share, area, visible_from}."""
    T = _Topo(shape, table)
    fid_of = {}
    for fid, f, faces in numbered:
        for x in faces:
            fid_of.setdefault(T.index(x), (fid, f['kind']))
    seeds = {i for i, (fid, kind) in fid_of.items() if kind in ('slant', 'fillet', 'arc') and table[i]['kind'] in ('plane', 'cylinder')}
    seeds |= {i for i, x in enumerate(table) if x['kind'] is None and i not in claimed}
    if len(seeds) < PROFILE_FACES:
        return []
    nbr = {}

    def nbrs(i):
        if i not in nbr:
            out = set()
            for e in _iter(table[i]['face'], TopAbs_EDGE):
                for nb in (T.emap.FindFromKey(e) if T.emap.Contains(e) else []):
                    j = T.index(nb)
                    if j is not None and j != i:
                        out.add(j)
            nbr[i] = out
        return nbr[i]
    info = {}

    def geo(i):
        """('plane', n) | ('cyl', axis) | ('free', normals)."""
        if i not in info:
            k = table[i]['kind']
            if k == 'plane':
                info[i] = ('plane', prj.unit(an.face_point_normal(table[i]['face'])[1]))
            elif k == 'cylinder':
                info[i] = ('cyl', prj.unit(an.v3(table[i]['geom'].Axis().Direction())))
            else:
                info[i] = ('free', _normals(table[i]['face']))
        return info[i]

    def lateral(i, d):
        k, v = geo(i)
        if k == 'plane':
            return abs(float(v @ d)) <= PRISM_TOL
        if k == 'cyl':
            return _same_dir(v, d)
        return len(v) > 0 and float(np.abs(v @ d).max()) <= PRISM_TOL
    dirs = []
    for i in sorted(seeds):
        k, v = geo(i)
        ds = [v] if k == 'cyl' else [_free_dir(v)] if k == 'free' else [geo(j)[1] for j in sorted(nbrs(i)) if table[j]['kind'] == 'cylinder']      # slanted face: axes of adjoining cylinders
        for d in ds:
            if d is not None and not any(_same_dir(d, e) for e in dirs):
                dirs.append(d)
    partial = {}

    def is_partial(i):
        """Cylinder face that does not close the circumference (partial cylinder)."""
        if i not in partial:
            g = table[i]['geom']
            A, o, r = prj.unit(an.v3(g.Axis().Direction())), an.v3(g.Axis().Location()), float(g.Radius())
            bp = _bpts(table[i]['face'], 0.002 * r) - o
            X, Y = _perp(A)
            partial[i] = _span_deg(np.arctan2(bp @ Y, bp @ X), _step(0.002 * r, r)) < 360.0 - ANG_TOL
        return partial[i]
    cands = []
    for d in dirs:
        d = _canon(d)
        core = {i for i in seeds if lateral(i, d)}
        for i in list(core):
            for j in nbrs(i):
                if j not in core and table[j]['kind'] == 'cylinder' and _same_dir(geo(j)[1], d) and is_partial(j):
                    core.add(j)
        up = {i: i for i in core}

        def find(i):
            while up[i] != i:
                up[i] = up[up[i]]
                i = up[i]
            return i
        for i in core:
            for j in nbrs(i):
                if j in up:
                    up[find(i)] = find(j)
        comps = {}
        for i in core:
            comps.setdefault(find(i), set()).add(i)
        X, Y = _perp(d)
        flat = lambda P: np.c_[P @ X, P @ Y]
        pts = {r: flat(np.vstack([_bpts(table[i]['face'], 0.05) for i in c])) for r, c in comps.items()}
        ends = {}
        for r, c in comps.items():
            for i in c:
                for j in nbrs(i):
                    if j not in core and table[j]['kind'] == 'plane' and _same_dir(geo(j)[1], d):
                        ends.setdefault(j, set()).add(r)
        for j, rs in ends.items():
            P = np.vstack([pts[r] for r in rs])
            lo, hi = P.min(0), P.max(0)
            E = flat(_bpts(table[j]['face'], 0.05))
            pad = PROFILE_PAD + 0.01 * float(np.linalg.norm(hi - lo))
            if np.all(E.min(0) >= lo - pad) and np.all(E.max(0) <= hi + pad) and _end_closes(T, table, j, core, d, nbrs):
                rs = sorted(rs)
                for r in rs[1:]:
                    up[find(r)] = find(rs[0])
        groups = {}
        for i in core:
            groups.setdefault(find(i), set()).add(i)
        for g in groups.values():
            if len(g) >= PROFILE_FACES and any(geo(i)[0] != 'plane' for i in g) and g & seeds:
                cands.append((d, g))
    cands.sort(key=lambda c: (-len(c[1]), min(c[1])))
    used, insts = set(), []
    for d, g in cands:
        if g & used:
            continue
        used |= g
        X, Y = _perp(d)
        P = np.vstack([_bpts(table[i]['face'], 0.05) for i in g])
        s = P @ d
        q = np.c_[P @ X, P @ Y]
        c2 = (q.min(0) + q.max(0)) / 2
        origin = X * c2[0] + Y * c2[1] + d * (float(s.min() + s.max()) / 2)
        outline = _profile_outline(table, sorted(g), d, X, Y, origin)
        if len(outline) > PROFILE_MAX_ELEMENTS:
            continue
        insts.append({'d': d, 'faces': sorted(g), 'origin': origin, 'outline': outline,
                      'length': float(s.max() - s.min()), 'area': sum(table[i]['area'] for i in g), 'lo': float(s.min()), 'hi': float(s.max())})
    import profiles
    classes = []      # congruence classes by tolerance comparison (rotation, mirroring, translation; equal length along d) against the first member
    for it in insts:
        for shape, cl in classes:
            if abs(cl[0]['length'] - it['length']) <= 0.05 and shape.matches(it['outline']):
                cl.append(it)
                break
        else:
            classes.append((profiles.Shape(it['outline']), [it]))
    out = []
    for cl in sorted((c for _, c in classes), key=lambda c: (-sum(i['area'] for i in c), min(i['faces'] for i in c))):
        share = sum(i['area'] for i in cl) / total
        if share < MINOR_SHARE:
            continue
        cl.sort(key=lambda i: (tuple(np.round(i['origin'], 2)), i['faces'][0]))
        ex = cl[0]
        ids = lambda it: sorted({fid_of[i][0] for i in it['faces'] if i in fid_of} | {owner[i] for i in it['faces'] if i in owner})
        d = ex['d']
        X, Y = _perp(d)
        out.append({'id': f'{part_id}#profile{len(out) + 1}', 'part': part_id, 'kind': 'profile', 'size': None, 'axis': _r(d, 6), 'members': ids(ex),
                    'copies': len(cl), 'copy_members': [ids(i) for i in cl[1:]], 'length': _r(ex['length'], 4), 'outline': ex['outline'],
                    'frame': {'origin': _r(ex['origin'], 4), 'u': _r(X, 6), 'v': _r(Y, 6), 'd': _r(d, 6)},
                    'share': _r(share, 5), 'area': _r(sum(i['area'] for i in cl), 3),
                    'visible_from': _profile_views(ix, ex, X, Y, d) if ix is not None else []})
    return out


def _safe_profiles(*a):
    """The profile detection must never abort the remaining form feature detection, but not silently: -> (profiles, error). On an exception ([], 'type: message'); part_features passes the
    error on as the marker profiles_error, features_of does not put such a result into the form feature cache, the render reports coverage_error, the planner an open-items line."""
    try:
        return _profiles(*a), None
    except Exception as e:
        return [], f'{type(e).__name__}: {e}'[:300]


def part_features(shape, part_id, hints=None):
    """{'features': [feature], 'freeform': bool, 'fingerprint': [...]} of a part. With hints (IR hints of this part)
    also 'hints' (match_hints: thread/knurl/engraving, each with absorbs) and 'hint_notes': faces that a hint
    takes over (_claims) do not count towards the freeform share; form features consisting only of such faces are named by absorbs of their
    hint (features_of removes them from the list). The IDs of all form features are created beforehand, independent of hints."""
    table = _face_table(shape)
    total = sum(x['area'] for x in table) or 1.0
    bb = an.bbox(shape)
    hints = norm_hints(hints)
    fx = _Faces(shape, table) if hints else None
    claims = _claims(fx, hints, bb) if hints else {}
    claimed = {i for c in claims.values() for i in c['faces']}
    freeform = sum(x['area'] for i, x in enumerate(table) if x['kind'] is None and i not in claimed) > FREEFORM * total
    bt, tube = _wire(shape, table, total)
    skip_r, caps, found = None, None, []
    if bt:
        found.append(({'kind': 'bend_table', 'size': None, 'wire_diameter': bt['wire_diameter'], 'complete': bt['complete']}, tube))
        if bt['complete']:          # arcs, lines and end faces are described by the table (P0 .. Pn)
            skip_r, caps = bt['wire_diameter'] / 2, _caps(bt)
    cyl, recs = _cylinders(shape, table, min(bb['size']), skip_r)
    found += cyl + _tori(shape, table, skip_r) + _cones(table, recs) + _spheres(table)
    found += _planes(table, np.array(bb['min']), caps) + _squares(shape, table, bb) + _inclined(shape, table, bb, caps)
    found += _elements(shape, table, found, bb)
    numbered = []
    for kind in KINDS:
        items = sorted((it for it in found if it[0]['kind'] == kind), key=lambda it: _key(it[0]))
        numbered += [(f'{part_id}#{kind}{n}', f, faces) for n, (f, faces) in enumerate(items, 1)]
    _carry(numbered, total)
    _plane_fields(numbered, total, bb)
    squares = [(fid, faces) for fid, f, faces in numbered if f['kind'] == 'square']
    tangents, trans = _tangents(shape, numbered), _transitions(shape, numbered)
    # Members (plane with a face of the element) per element; an element with a hint face (claimed) is
    # none (dropped, taken over completely it stands under absorbs of its hint), its planes stay single features.
    # member_of = first remaining element in number order; planes of a square bar stay its members
    fmap, fidx = TopTools_IndexedMapOfShape(), {}
    for fid, f, faces in numbered:
        if f['kind'] == 'plane' or f['kind'] in ELEMENTS:
            fidx[fid] = {fmap.Add(x) for x in faces}
    planes = [(fid, fidx[fid]) for fid, f, _ in numbered if f['kind'] == 'plane']
    members, dead = {}, set()
    for fid, f, faces in numbered:
        if f['kind'] in ELEMENTS:
            members[fid] = [pid for pid, s in planes if s & fidx[fid]]
            if claimed and {fx.index(x) for x in faces} & claimed:
                dead.add(fid)
    member_of = {}
    for fid in sorted(members, key=lambda fid: MEMBER_ORDER.index(fid.split('#')[-1].rstrip('0123456789'))):
        if fid not in dead:
            for pid in members[fid]:
                member_of.setdefault(pid, fid)
    ix = _intersector(shape)
    feats = []
    for fid, f, faces in numbered:
        if fid in dead:
            continue
        rest = {k: v for k, v in f.items() if k not in ('kind', 'size')}
        if f['kind'] == 'plane':    # plane of a parallel pair of a square refers to its ID (first by n)
            sq = next((sid for sid, sf in squares if any(a.IsSame(b) for a in faces for b in sf)), None)
            if sq:
                rest['square'] = sq
            elif fid in member_of:
                rest['member_of'] = member_of[fid]
        if f['kind'] == 'arc':      # directions in which a tangential plane fixes the arc
            rest['tangent'] = tangents[fid]
            rest['trans'], rest['trans_nb'] = trans[fid]      # transition arc (tangential into two non-parallel planar faces) and the IDs of these neighbours
        if f['kind'] in ELEMENTS:
            rest['members'] = members[fid]
        probes = _probes(faces)
        vis = _visible(ix, probes, VIEWS)
        A = prj.unit(np.asarray(f['axis'], float)) if f['kind'] in ('hole', 'shaft', 'arc') and f.get('axis') is not None else None
        if A is not None and np.abs(A).max() < 1 - 1e-3:   # inclined axis - visible from the +axis (1) or -axis (-1) side
            rest['visible_along'] = [s for s in (1, -1) if any(ix.free(q, gp_Dir(*map(float, s * A))) for q in np.atleast_2d(probes))]
        feats.append({'id': fid, 'part': part_id, 'kind': f['kind'], 'size': f['size'],
                      'visible_from': [v for v in VIEWS if vis[v]], **rest})
    _mark_chamfer_cones(feats)
    facets, gone_facets = _facets(shape, table, numbered, part_id, claimed, total)
    feats = [f for f in feats if f['id'] not in gone_facets]       # members of a facet region drop out of the individual obligation
    owner = {}
    regions = (_regions(shape, table, claimed, part_id, total, owner) if freeform else []) + facets
    profs, profs_error = _safe_profiles(shape, table, numbered, claimed, total, part_id, owner, ix)
    out = {'features': feats, 'freeform': bool(freeform), 'fingerprint': fingerprint(shape), 'regions': regions, 'profiles': profs}
    if profs_error:
        out['profiles_error'] = profs_error      # marker instead of a silent empty list
    if not hints:
        return out
    # form feature consisting only of taken-over faces -> absorbs of the hint with most of its faces (tie: first)
    owner = {}
    for hi in sorted(claims):
        for i in claims[hi]['faces']:
            owner.setdefault(i, hi)
    for c in claims.values():
        c['absorbs'] = []
    for fid, f, fs in numbered:
        idx = [fx.index(x) for x in fs]
        if idx and all(i in owner for i in idx):
            n = {}
            for i in idx:
                n[owner[i]] = n.get(owner[i], 0) + 1
            claims[min(n, key=lambda hi: (-n[hi], hi))]['absorbs'].append(fid)
    for c in claims.values():
        if c.get('axis') is not None:                # thread without target cylinder: visibility of its region
            vis = _visible(ix, _probes([table[i]['face'] for i in c['faces']]), VIEWS)
            c['visible_from'] = [v for v in VIEWS if vis[v]]
    gone = {fid for c in claims.values() for fid in c['absorbs']}
    out['hints'], out['hint_notes'] = match_hints([f for f in feats if f['id'] not in gone], hints, claims)
    return out


# ---------------------------------------------------------------- Form feature cache

_RESULT = ('features', 'freeform', 'fingerprint', 'patterns', 'hints', 'hint_notes', 'regions', 'profiles')
_code = []


def _version():
    """VERSION and whatever else determines the detection: source text of features/analyze/projection, OCP, numpy, shapely."""
    if not _code:
        import os, hashlib, shapely, OCP
        h = hashlib.sha256()
        for mod in (__file__, an.__file__, prj.__file__):
            with open(os.path.abspath(mod), 'rb') as f:
                h.update(f.read())
        _code.append({'source': h.hexdigest(), 'ocp': getattr(OCP, '__version__', ''), 'numpy': np.__version__,
                      'shapely': shapely.__version__})
    return {'features': VERSION, **_code[0]}


def features_of(shape, part_id, cache_dir=None, brep=None, selection=None, hints=None):
    """({features, freeform, fingerprint, patterns, hints, hint_notes}, from the cache?) of a part; hints = IR hints
    of this part (plan and render give the same ones). features without the form features that a hint takes over
    (absorbs, part_features), patterns from all form features (IDs as without hints) without patterns with taken-over
    members, hints = match_hints. With cache_dir and brep via the form feature cache (plan and render share it):
    key = B-rep digest, part ID, part selection selection ({single_root, solids} like analyze.part_selection),
    _version() and the hints (norm_hints: order irrelevant). Without cache_dir it only computes. If the profile detection failed, the result additionally carries
    profiles_error and does not go into the cache (the next run recomputes)."""
    hints = norm_hints(hints)
    key = None
    if cache_dir and brep:
        key = cache.feature_key(cache.brep_digest(brep), part_id, selection, {**_version(), 'hints': hints})
        got = cache.load_json(cache_dir, key)
        if isinstance(got, dict) and tuple(got) == _RESULT:
            return got, True
    r = part_features(shape, part_id, hints)
    gone = {fid for h in r.get('hints') or [] for fid in h.get('absorbs') or []}
    out = {'features': [f for f in r['features'] if f['id'] not in gone], 'freeform': r['freeform'],
           'fingerprint': r['fingerprint'], 'patterns': [p for p in patterns(r['features']) if not gone & set(p['members'])],
           'hints': r.get('hints', []), 'hint_notes': r.get('hint_notes', []), 'regions': r.get('regions', []), 'profiles': r.get('profiles', [])}
    if r.get('profiles_error'):
        out['profiles_error'] = r['profiles_error']      # do not cache, otherwise the profile detection would stay silently off for the part permanently
    elif key:
        cache.store_json(cache_dir, key, out)
    return out, False


# ---------------------------------------------------------------- Patterns

POS_TOL = 0.02   # mm, position tolerance of the pattern members to the grid point


def _depth(f):
    return float(np.linalg.norm(np.array(f['end']) - np.array(f['start'])))


def _lo(f):
    """Position along the axis (smaller of the two end coordinates)."""
    d = _canon(f['axis'])
    return float(min(np.array(f['start']) @ d, np.array(f['end']) @ d))


def _lattice(pts):
    """(indices, v1, v2|None, n1, n2) of the first pattern from 2D points or None. From the lexicographically smallest
    point p0: v1 = shortest vector to a neighbour, v2 = shortest non-parallel one. Grid only if complete
    (n1, n2 >= 2), otherwise a line in v1 direction (>= 3 members). Indices row by row (n2 rows of n1 points each)."""
    def find(p):
        d = np.linalg.norm(pts - p, axis=1)
        k = int(np.argmin(d))
        return k if d[k] <= POS_TOL else None

    def run(p0, v):
        n = 1
        while find(p0 + n * v) is not None:
            n += 1
        return n

    for i0 in sorted(range(len(pts)), key=lambda i: tuple(np.round(pts[i], 3))):
        p0 = pts[i0]
        vecs = [pts[j] - p0 for j in range(len(pts)) if j != i0]
        vecs = sorted((v for v in vecs if np.linalg.norm(v) > POS_TOL),
                      key=lambda v: (round(float(np.linalg.norm(v)), 3), *np.round(v, 3)))
        if not vecs:
            continue
        v1 = vecs[0]
        v2 = next((v for v in vecs[1:] if abs(v1[0] * v[1] - v1[1] * v[0]) > POS_TOL * np.linalg.norm(v1)), None)
        n1 = run(p0, v1)
        if v2 is not None:
            n2 = run(p0, v2)
            if n1 >= 2 and n2 >= 2 and all(find(p0 + i * v1 + j * v2) is not None for i in range(n1) for j in range(n2)):
                return [find(p0 + i * v1 + j * v2) for j in range(n2) for i in range(n1)], v1, v2, n1, n2
        if n1 >= 3:
            return [find(p0 + i * v1) for i in range(n1)], v1, None, n1, 1
    return None


def patterns(features):
    """Pattern (kind 'pattern') of at least 3 equal holes/shafts of a part (same kind, Ø +-0.005, depth
    +-0.01, parallel axes, same position along the axis) on a line with equal pitch (rows=1) or in
    a complete grid. members: row by row from the smallest point; cols counts in direction dirs[0]
    (shortest neighbour vector), rows in direction dirs[1]; origin = start point of the first member."""
    groups = []
    for f in features:
        if f['kind'] not in ('hole', 'shaft'):
            continue
        d = _canon(f['axis'])
        for g in groups:
            h = g[0]
            if (h['part'], h['kind']) == (f['part'], f['kind']) and abs(h['diameter'] - f['diameter']) <= 0.005 \
                    and abs(_depth(h) - _depth(f)) <= 0.01 and abs(abs(_canon(h['axis']) @ d) - 1) < 1e-6 \
                    and abs(_lo(h) - _lo(f)) <= 0.01:
                g.append(f)
                break
        else:
            groups.append([f])
    found = []
    for g in groups:
        d = _canon(g[0]['axis'])
        X, Y = _perp(d)
        rest = list(g)
        while len(rest) >= 3:
            lat = _lattice(np.array([[np.array(f['start']) @ X, np.array(f['start']) @ Y] for f in rest]))
            if lat is None:
                break
            idx, v1, v2, n1, n2 = lat
            mem = [rest[i] for i in idx]
            to3 = lambda v: None if v is None else v[0] * X + v[1] * Y
            found.append({'kind': 'pattern', 'part': g[0]['part'], 'size': g[0]['diameter'], 'member_kind': g[0]['kind'],
                          'members': [m['id'] for m in mem], 'count': len(mem), 'rows': n2, 'cols': n1,
                          'pitch': [_r(np.linalg.norm(v1)), None if v2 is None else _r(np.linalg.norm(v2))],
                          'dirs': [_r(prj.unit(to3(v)), 6) if v is not None else None for v in (v1, v2)],
                          'axis': _r(d, 6), 'origin': mem[0]['start'],
                          'visible_from': [v for v in VIEWS if any(v in m['visible_from'] for m in mem)]})
            ids = {m['id'] for m in mem}
            rest = [f for f in rest if f['id'] not in ids]
    used = {m for p in found for m in p['members']}
    found += _polar_patterns([f for f in features if f['kind'] in ('hole', 'shaft') and f['id'] not in used])
    out = []
    for part in dict.fromkeys(f['part'] for f in found):
        items = sorted((f for f in found if f['part'] == part), key=_key)
        out += [{'id': f"{part}#pattern{n}", **f} for n, f in enumerate(items, 1)]
    _children(features, out)
    return out


POLAR_ANG = 0.5      # degrees: deviation of a member from the angular grid (best-fit line). A manufactured grid (CAD input in whole degrees, rounding to 4 places)
                     # lies far below, a deliberately offset member (>= 1 degree) above
POLAR_POS = 0.05     # mm: axes intersect the rotation axis, equal centre distance, axial position on the line - like POS_TOL of the grids a position tolerance of manufacturing
POLAR_MIN_STEP = 1.0     # degrees: a smaller pitch is a duplicate of the same location, no sequence
POLAR_DENSITY = 0.75     # minimum occupancy of the grid (members / grid places first..last): at most every fourth place empty; more gaps (e.g. every second place of a grid half as fine) are no grid but a coarser pitch with an outlier


def _sequence(rest):
    """A polar or helical sequence of equal members (>= 3) or None. A member belongs to it if its axis intersects the common rotation axis A perpendicularly
    (axes perpendicular to A, in the plane across A through one point = centre), its centre distance to the rotation axis is equal (+- POLAR_POS), its angle lies on a
    grid with constant step (+- POLAR_ANG, gaps as whole multiples; occupancy >= POLAR_DENSITY) and its axial position lies on a line in the grid index
    (+- POLAR_POS). Rise per step 0 (total offset <= POLAR_POS): polar, otherwise helical. Deviating members are left out.
    Result: dict with mem (in grid order), idx (grid index per member from 0), A, center (point on A at the height of index 0), step, radius, rise (axial offset per step,
    sign with respect to A), steps (last index), full, start (angle of index 0 to the world-axis reference direction ref, degrees, clockwise about A)."""
    n = len(rest)
    E = np.array([prj.unit(np.asarray(f['axis'], float)) for f in rest])
    P = np.array([np.asarray(f['start'], float) for f in rest])
    A, keep = None, []
    for i in range(min(n, 4)):                       # rotation axis: perpendicular to as many member axes as possible
        for j in range(i + 1, n):
            c = np.cross(E[i], E[j])
            if np.linalg.norm(c) > 1e-3:
                a_ = prj.unit(c)
                k_ = [m for m in range(n) if abs(float(E[m] @ a_)) <= 1e-3]
                if len(k_) > len(keep):
                    A, keep = a_, k_
    if A is None or len(keep) < 3:
        return None
    A = _canon(A)
    X, Y = _perp(A)
    P2 = np.c_[P @ X, P @ Y]
    E2 = np.c_[E @ X, E @ Y]
    N2 = np.c_[-E2[:, 1], E2[:, 0]]
    N2 = N2 / np.maximum(np.linalg.norm(N2, axis=1), 1e-12)[:, None]
    best, C2 = [], None
    for ii in range(min(len(keep), 4)):              # centre: intersection of two axis lines with the most members on their lines
        for jj in range(ii + 1, len(keep)):
            i, j = keep[ii], keep[jj]
            M = np.array([N2[i], N2[j]])
            if abs(np.linalg.det(M)) < 1e-3:
                continue
            c = np.linalg.solve(M, [N2[i] @ P2[i], N2[j] @ P2[j]])
            near = [m for m in keep if abs(float(N2[m] @ (c - P2[m]))) <= POLAR_POS]
            if len(near) > len(best):
                best, C2 = near, c
    if len(best) < 3:
        return None
    Pe = np.array([np.asarray(f['end'], float) if f.get('end') is not None else np.asarray(f['start'], float) for f in rest])
    Pe2 = np.c_[Pe @ X, Pe @ Y]
    radii = {'start': np.linalg.norm(P2 - C2, axis=1), 'end': np.linalg.norm(Pe2 - C2, axis=1)}
    pick = {}
    for src, rr in radii.items():          # pitch circle: the start or the end of the axis (the face in which the hole begins is of different depth per member, e.g. at a slot end)
        a_ = max(best, key=lambda m: (sum(1 for q in best if abs(rr[q] - rr[m]) <= POLAR_POS), -m))
        pick[src] = (sum(1 for q in best if abs(rr[q] - rr[a_]) <= POLAR_POS), a_)
    src = max(radii, key=lambda k_: (pick[k_][0], k_ == 'start'))          # on a tie the start
    rad, anchor = radii[src], pick[src][1]
    best = [m for m in best if abs(rad[m] - rad[anchor]) <= POLAR_POS and rad[m] > 1e-6]
    if len(best) < 3:
        return None
    sub = np.array(best)
    ang = np.degrees(np.arctan2(P2[sub, 1] - C2[1], P2[sub, 0] - C2[0])) % 360.0
    alongs = P[sub] @ A
    m_ = len(sub)
    D = np.mod(ang[None, :] - ang[:, None], 360.0)          # D[i, j]: angle from member i forward to member j
    srt = np.sort(ang)
    gaps = np.diff(np.r_[srt, srt[0] + 360.0])
    steps = {}
    for g_ in gaps:
        if g_ > POLAR_MIN_STEP:
            steps.setdefault(round(float(g_) * 5) / 5, []).append(float(g_))
    cands = sorted(steps, key=lambda s_: (-len(steps[s_]), s_))[:6]
    winner = None            # (key, start member, members, grid index, step, rise, axis intercept)
    for s0 in cands:
        tol = 2 * POLAR_ANG
        okm = np.abs(np.mod(D + tol, s0) - tol) <= tol
        order = sorted(range(m_), key=lambda i: (-int(okm[i].sum()), float(ang[i])))
        for i in order[:6]:
            if okm[i].sum() < 3:
                break
            idx = {int(j): int(round(D[i, j] / s0)) for j in np.nonzero(okm[i])[0]}
            seen, sel = set(), []
            for j in idx:                                   # two members on the same grid place: the first stays
                if idx[j] not in seen:
                    seen.add(idx[j])
                    sel.append(j)
            step = s0
            while len(sel) >= 3:
                mm = np.array([idx[j] for j in sel], float)
                dd = np.array([D[i, j] for j in sel])
                step = float(mm @ dd / (mm @ mm))               # best-fit line through the start member (index 0)
                err = np.abs(dd - step * mm)
                if err.max() <= POLAR_ANG:
                    break
                sel.pop(int(np.argmax(err)))
                if i not in sel:
                    sel = []
            if len(sel) < 3 or step <= POLAR_MIN_STEP:
                continue
            slope = icpt = 0.0
            while len(sel) >= 3:                               # axial position on a line in the grid index
                mm = np.array([idx[j] for j in sel], float)
                aa = np.array([alongs[j] for j in sel])
                slope, icpt = (float(x) for x in np.polyfit(mm, aa, 1))
                err = np.abs(aa - (icpt + slope * mm))
                if err.max() <= POLAR_POS:
                    break
                sel.pop(int(np.argmax(err)))
            if len(sel) < 3:
                continue
            dense = False
            while len(sel) >= 3:
                ixs = sorted(idx[j] for j in sel)
                span = ixs[-1] - ixs[0] + 1
                if len(sel) / span >= POLAR_DENSITY and (len(sel) >= span or len(sel) >= 4):      # gaps carry a grid only from 4 members (three members with a gap are chance)
                    dense = True
                    break
                cut = ixs[int(np.argmax(np.diff(ixs)))]               # occupancy too thin: cut off the end behind the largest gap (the smaller side) and check again
                lo_, hi_ = [j for j in sel if idx[j] <= cut], [j for j in sel if idx[j] > cut]
                sel = lo_ if len(lo_) >= len(hi_) else hi_
            if not dense:
                continue
            key = (len(sel), -span, -float(ang[i]))
            if winner is None or key > winner[0]:
                winner = (key, i, sel, idx, step, slope, icpt)
    if winner is None:
        return None
    _, i0, sel, idx, step, slope, icpt = winner
    sel = sorted(sel, key=lambda j: idx[j])
    ix = [idx[j] for j in sel]
    m0 = ix[0]
    ix = [x - m0 for x in ix]                    # grid index from the first occupied place (after cutting off an end the start member may no longer be included)
    icpt += slope * m0
    if abs(slope) * max(ix) <= POLAR_POS:
        slope, icpt = 0.0, float(np.mean([alongs[j] for j in sel]))
    nfull = int(round(360.0 / step))
    full = len(sel) == nfull and abs(nfull * step - 360.0) <= POLAR_ANG
    mem = [rest[best[j]] for j in sel]
    c0 = C2[0] * X + C2[1] * Y + icpt * A
    k_ref = int(np.argmin(np.abs(A)))
    R0 = prj.unit(np.eye(3)[k_ref] - A[k_ref] * A)
    u0 = P[best[sel[0]]] - c0
    u0 = u0 - float(u0 @ A) * A
    start = float(np.degrees(np.arctan2(u0 @ np.cross(A, R0), u0 @ R0)))
    # Reference ray: one of the four rays +-R0, +-(A x R0) (the centre lines of the view along A if A is a world axis), chosen so that the whole sequence in the counting sense of the
    # indices (clockwise about the canonical axis A) lies within 0..180 degrees from the ray: start angle and gap angle are then positive angles to this ray
    span_deg = max(ix) * step
    cands = [(c_, (start - c_ + POLAR_ANG) % 360.0 - POLAR_ANG) for c_ in (0.0, 90.0, 180.0, 270.0)]      # an angle just below 0 (rounding) counts as 0, not as 360
    good = [x_ for x_ in cands if x_[1] + span_deg <= 180.0 + POLAR_ANG]
    c_, start = min(good or cands, key=lambda x_: x_[1])      # none fits (span above 90 degrees): the ray with the smallest start angle (< 90): start angle, pitch and gaps all count in the counting sense of the indices from this ray (no short/long way)
    R0 = R0 * np.cos(np.radians(c_)) + np.cross(A, R0) * np.sin(np.radians(c_))
    return {'mem': mem, 'idx': ix, 'A': A, 'center': c0, 'step': step, 'radius': float(np.mean([rad[best[j]] for j in sel])),
            'rise': slope, 'steps': max(ix), 'full': full, 'start': start, 'ref': R0, 'radius_at': src}


def _sequences(group):
    """All sequences of a group of equal members (kind, Ø +-0.005, for blind holes depth +-0.01), the largest per pass; the rest stays single."""
    out, rest = [], [f for f in group if f.get('axis') is not None and f.get('start') is not None]
    while len(rest) >= 3:
        s = _sequence(rest)
        if s is None:
            break
        out.append(s)
        ids = {id(f) for f in s['mem']}
        rest = [f for f in rest if id(f) not in ids]
    return out


def _equal_groups(features):
    groups = []
    for f in features:
        if f.get('axis') is None or f.get('start') is None:
            continue
        for g in groups:
            h = g[0]
            if (h['part'], h['kind']) == (f['part'], f['kind']) and abs(h['diameter'] - f['diameter']) <= 0.005 and (
                    not (h.get('blind') or f.get('blind')) or abs(_depth(h) - _depth(f)) <= 0.01):      # through holes: the wall thickness is no feature of the sequence (slot end)
                g.append(f)
                break
        else:
            groups.append([f])
    return groups


def sequence_check(members):
    """Recount from the geometry (checks). Returns (sequence | None, ids): the sequence with the most members among the given features and their IDs.
    The same computation as the detection (no second yardstick); members that it does not find do not belong to the sequence."""
    best = None
    for g in _equal_groups(members):
        for s in _sequences(g):
            if best is None or len(s['mem']) > len(best['mem']):
                best = s
    return best, ([m['id'] for m in best['mem']] if best else [])


def _polar_patterns(holes):
    """Circle patterns of radial holes/shafts (polar) and screw sequences (helical: axial offset per step) - at least 3 equal ones (kind, Ø +-0.005,
    for blind holes depth +-0.01), detection in _sequence. Result like a pattern (kind 'pattern'), dirs [None, None] (no linear pitch), mode 'polar'|'helical', plus 'polar' {center, axis,
    angle (pitch in degrees), radius, full, rise (axial offset per step), steps (steps first..last grid member), index (grid index per member in the order of members),
    missing (empty grid places), start (angle of the first grid place to the reference direction ref), ref, first/last (first/last grid member)}. members: the specimen first
    (axis nearest to a world axis: its position gives the ordinary way), then the others by grid index."""
    out = []
    for g in _equal_groups(holes):
        if len(g) < 3:
            continue
        for s in _sequences(g):
            mem, ix = s['mem'], s['idx']
            first, last = mem[0], mem[-1]
            lead = max(range(len(mem)), key=lambda k: (round(float(np.abs(prj.unit(np.asarray(mem[k]['axis'], float))).max()), 3), -k))      # specimen: axis nearest to a world axis
            order = [lead] + [k for k in range(len(mem)) if k != lead]
            mem, ix = [mem[k] for k in order], [ix[k] for k in order]
            have = set(ix)
            out.append({'kind': 'pattern', 'part': g[0]['part'], 'size': g[0]['diameter'], 'member_kind': g[0]['kind'], 'mode': 'polar' if s['rise'] == 0.0 else 'helical',
                        'members': [m['id'] for m in mem], 'count': len(mem), 'rows': 1, 'cols': len(mem), 'pitch': [_r(s['step']), None],
                        'dirs': [None, None], 'axis': _r(_canon(prj.unit(np.asarray(mem[0]['axis'], float))), 6), 'origin': mem[0]['start'],
                        'polar': {'center': _r(s['center']), 'axis': _r(s['A'], 6), 'angle': _r(s['step'], 3), 'radius': _r(s['radius']), 'radius_at': s['radius_at'], 'full': s['full'],
                                  'rise': _r(s['rise'], 4), 'steps': s['steps'], 'index': ix, 'start': _r(s['start'], 3), 'ref': _r(s['ref'], 6),
                                  'missing': [] if s['full'] else [k for k in range(s['steps'] + 1) if k not in have],
                                  'first': first['id'], 'last': last['id']},
                        'visible_from': [v for v in VIEWS if any(v in m['visible_from'] for m in mem)]})
    return out


def _children(features, pats):
    """Sub-features (CHILD_KINDS) of pattern members. Each sub-feature belongs via its point to the nearest member axis
    (radially at most CHILD_RADIAL x Ø, axially within the member range +- Ø). Local signature in the member frame: axis coordinate relative to the
    member start, azimuth and radial distance to the axis, width, inclination, length, normal in the member frame. The reference member is the first among the members whose set
    is reproduced completely by most members (cardinality, not majority per signature); every member with exactly
    this set sets on its sub-features via_member (instance in the reference member) and via_pattern (pattern ID, changes the
    feature dicts in place). Deviating members (different number or signature) keep all sub-features individually."""
    kids = [f for f in features if f['kind'] in CHILD_KINDS and f.get('pt') is not None]
    byid = {f['id']: f for f in features}
    for p in pats:
        mem = [byid[m] for m in p['members'] if m in byid]
        if len(mem) < 3 or p.get('polar'):        # circular pattern: the members have different axis frames (no common signature)
            continue
        d = _canon(mem[0]['axis'])
        X, Y = _perp(d)
        own = {m['id']: [] for m in mem}
        for k in (k for k in kids if k['part'] == p['part'] and 'via_member' not in k):
            q = np.asarray(k['pt'], float)
            best = None
            for m in mem:
                lo, hi = sorted((np.asarray(m['start'], float) @ d, np.asarray(m['end'], float) @ d))
                ax, r = q @ d, q - (q @ d) * d - (np.asarray(m['start'], float) - (np.asarray(m['start'], float) @ d) * d)
                rad = float(np.linalg.norm(r))
                if lo - m['diameter'] <= ax <= hi + m['diameter'] and rad <= CHILD_RADIAL * m['diameter']:
                    if best is None or rad < best[0]:
                        nv = np.asarray(k.get('normal') or [0, 0, 0], float)
                        best = (rad, m, ax - lo, math.degrees(math.atan2(r @ Y, r @ X)), (nv @ d, nv @ X, nv @ Y))
            if best is not None:
                if k['kind'] == 'cone':        # cones are rotationally symmetric (coaxial): azimuth, radial distance and normal of the face point are arbitrary
                    sig = (best[2], 0.0, 0.0, k['d2'], k['angle_deg'], k['length'], 0.0, 0.0, 0.0)
                else:
                    sig = (best[2], best[3], best[0], k['width'], k['angle_deg'], k['length'], *best[4])
                k['pattern_member'] = best[1]['id']          # member of the pattern the sub-feature belongs to (also with deviating members)
                own[best[1]['id']].append((k, sig))
        # signature: (axis position, azimuth, radial distance, width, inclination, length, normal n.d / n.X / n.Y in the member frame)
        close = lambda a, b: (abs(a[0] - b[0]) <= CHILD_TOL and abs(((a[1] - b[1] + 180) % 360) - 180) <= CHILD_ANG
                              and abs(a[2] - b[2]) <= CHILD_TOL and abs(a[3] - b[3]) <= CHILD_TOL and abs(a[4] - b[4]) <= CHILD_ANG
                              and abs(a[5] - b[5]) <= CHILD_TOL and all(abs(x - y) <= CHILD_TOL for x, y in zip(a[6:], b[6:])))

        def matching(ref, other):
            free, pairs = list(other), []
            for k, sig in ref:
                j = next((j for j, (_, s2) in enumerate(free) if close(sig, s2)), None)
                if j is None:
                    return None
                pairs.append((k, free.pop(j)[0]))
            return pairs if not free else None
        sets = [own[m['id']] for m in mem]
        score = [sum(1 for o in sets if matching(s, o) is not None) for s in sets]
        top = max(score)
        if top < 2 or not sets[score.index(top)]:
            continue
        ref = sets[score.index(top)]
        for o in sets:
            for k0, k in matching(ref, o) or []:
                if k is not k0:
                    k['via_member'], k['via_pattern'] = k0['id'], p['id']


# ---------------------------------------------------------------- IR hints

_ISO = {'1.6': 0.35, '2': 0.4, '2.5': 0.45, '3': 0.5, '4': 0.7, '5': 0.8, '6': 1.0, '8': 1.25, '10': 1.5, '12': 1.75,
        '14': 2.0, '16': 2.0, '20': 2.5, '24': 3.0, '30': 3.5}
K_EXT, K_INT = 1.22687, 1.08253     # core Ø = d - K * P (external / internal)


def thread_dims(designation):
    """(nominal Ø, core Ø external, pitch) from M<d>, M<d>x<P>, <a>/<b>"-<n> UNC|UNF or <d>"-<n>; otherwise None.
    The core Ø internal is d - 1.08253 * P (match_hints)."""
    t = designation.strip().replace(',', '.')
    m = re.fullmatch(r'M\s*(\d+(?:\.\d+)?)(?:\s*[xX×]\s*(\d+(?:\.\d+)?))?', t)
    if m:
        major = float(m.group(1))
        pitch = float(m.group(2)) if m.group(2) else _ISO.get(m.group(1))
    else:
        m = re.fullmatch(r'(\d+/\d+|\d*\.?\d+)\s*"\s*-\s*(\d+)(?:\s*(?:UNC|UNF|UN))?', t, re.I)
        if not m:
            return None
        a = m.group(1)
        major = (float(a.split('/')[0]) / float(a.split('/')[1]) if '/' in a else float(a)) * 25.4
        pitch = 25.4 / int(m.group(2))
    if not pitch or not major:
        return None
    return major, major - K_EXT * pitch, pitch


# A thread hint without a designation (LLcad operator thread with standard custom carries only the profile: root_radius, tooth_depth, pitch)
# gets it verbatim from the model text if exactly one designation named there fits pitch and nominal Ø; otherwise the hint stays as it is.
PITCH_TOL = 0.002     # mm: pitch of the hint against that of the designation. Smallest distance of an ISO pitch to a UN pitch: 0.35 against 72 threads/inch (0.3528), 0.0028;
                      # M8 1.25 against 20 threads/inch 1.27 and M5 0.8 against 32 threads/inch 0.794 stay separate; the IR rounds to 1e-9
MAJOR_UNDER = 0.25    # x pitch: by this much the modelled outer Ø (2 (root_radius + tooth_depth); crest outside, root inside) may lie below the nominal Ø: the tolerance field
                      # 6g or 2A ends 0.13 P (M24x2) to 0.25 P (M3) below the nominal Ø (1/4"-20 2A: 0.234 mm = 0.18 P); above that only model noise 2 RAD_TOL as in match_hints
_DESIGNATION_RX = re.compile(r'(?<![\w.,/])(?:M\s?\d+(?:[.,]\d+)?(?:\s?[xX×]\s?\d+(?:[.,]\d+)?)?|(?:\d+/\d+|\d*\.\d+|\d+)\s?"\s?-\s?\d+(?:\s?UN[CF]?)?)(?![\w.,])')


def text_designations(texts):
    """Thread designations that stand verbatim in the texts (M<d>[x<P>], <a>/<b>"-<n> [UNC|UNF|UN]), only those that thread_dims reads; order of first occurrence, without duplicates."""
    out = []
    for t in texts or []:
        for m in _DESIGNATION_RX.finditer(str(t or '')):
            des = m.group(0).strip()
            if des not in out and thread_dims(des) is not None:
                out.append(des)
    return out


def thread_fits(hint, designation):
    """Does the designation fit the thread hint? Pitch |P_hint - P| <= PITCH_TOL and modelled outer Ø 2 (root_radius + tooth_depth) in
    [nominal Ø - MAJOR_UNDER P, nominal Ø + 2 RAD_TOL]. Without pitch or profile dimensions in the hint nothing fits (the geometry is checked afterwards by _thread_claim with the thread window)."""
    dims = thread_dims(str(designation or ''))
    p, r, t = _num(hint.get('pitch')), _num(hint.get('root_radius')), _num(hint.get('tooth_depth'))
    if dims is None or not p or not r or not t or r <= 0 or t <= 0:
        return False
    major, _, pitch = dims
    outer = 2.0 * (r + t)
    return abs(p - pitch) <= PITCH_TOL and major - MAJOR_UNDER * pitch <= outer <= major + 2 * RAD_TOL


def named_thread_hints(hints, texts):
    """(hints, [(index, designation or None)]) - every thread hint without a designation gets the designation from texts
    if exactly one thread size (equal dimensions, thread_dims) fits (thread_fits; for several spellings of the same size the first); otherwise it stays unchanged (None in the list).
    Hints with a designation and other operators stay as they are and are not in the list."""
    names = text_designations(texts)
    out, found = [], []
    for i, h in enumerate(hints or []):
        if h.get('operator') != 'thread' or str(h.get('designation') or '').strip():
            out.append(h)
            continue
        fit = [d for d in names if thread_fits(h, d)]
        sizes = {tuple(round(float(x), 4) for x in thread_dims(d)) for d in fit}
        des = fit[0] if len(sizes) == 1 else None
        out.append({**h, 'designation': des} if des else h)
        found.append((i, des))
    return out, found


# ---------------------------------------------------------------- IR hints: assigning faces
# A face belongs to a hint only if it lies unambiguously within its region geometrically; in doubt
# it stays part of a form feature of its own (better one dimension too many than an undimensioned element). Check points per face: boundary
# (analyze.boundary_points) and an inner UV grid (SAMPLE_N x SAMPLE_N), e.g. the centre of an end disc.

ENGRAVE = ('text', 'svg_emblem')     # relief operators of the IR (lettering, SVG emblem): engraving
CLAIM_TOL = 0.02     # mm: engraving depth against the hint depth, axial limits of thread and knurl, distance to the part edge
REGION_TOL = 0.05    # mm: engraving test points against the tool/text box of the IR (region)
GLYPH_K = 1.5        # without region: glyph at most GLYPH_K x text height wide/high, per character this much text width
LINE_K = 2.0         # without region: line spacing at most LINE_K x text height
RAD_TOL = 0.05       # mm: radius window of thread and knurl (match_hints: Ø ± 0.1)
SAMPLE_N = 3         # inner UV grid per face


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def norm_hints(hints):
    """IR hints in fixed form (keys sorted, list by its JSON): assignment, numbers and cache key
    do not depend on the order in the IR."""
    import json
    hs = [dict(sorted(h.items())) for h in hints or []]
    return sorted(hs, key=lambda h: json.dumps(h, sort_keys=True, default=str))


class _Faces:
    """Faces of a part (index = row of _face_table) with test points, safe bounding box and adjacency via
    common edges, each computed only on demand."""

    def __init__(self, shape, table):
        self.shape, self.table = shape, table
        self.fmap = TopTools_IndexedMapOfShape()
        for x in table:
            self.fmap.Add(x['face'])
        self._rim, self._pts, self._box, self._topo, self._plane = {}, {}, {}, None, {}

    def index(self, face):
        return self.fmap.FindIndex(face) - 1

    def rim(self, i):
        """Edge points of the face."""
        if i not in self._rim:
            self._rim[i] = an.boundary_points(self.table[i]['face'], 0.01)
        return self._rim[i]

    def pts(self, i):
        """Edge points and inner UV grid (points in the face); None if the face cannot be sampled (error in the
        UV grid, no edge points): such a face can never be assigned (within/in_slab False)."""
        if i not in self._pts:
            f = self.table[i]['face']
            out = [self.rim(i)]
            try:
                u0, u1, v0, v1 = BRepTools.UVBounds_s(f)
                cls, s = BRepTopAdaptor_FClass2d(f, 1e-7), BRepAdaptor_Surface(f)
                for a in range(SAMPLE_N):
                    for b in range(SAMPLE_N):
                        u, v = u0 + (u1 - u0) * (a + 0.5) / SAMPLE_N, v0 + (v1 - v0) * (b + 0.5) / SAMPLE_N
                        if cls.Perform(gp_Pnt2d(u, v)) == TopAbs_IN:
                            out.append(an.v3(s.Value(u, v))[None])
            except Exception:
                out = []
            out = [p for p in out if len(p)]
            self._pts[i] = np.vstack(out) if out else None
        return self._pts[i]

    def span(self, i, d, o=None):
        """(min, max, safe) of the face along d (from o): from the safe bounding box, otherwise from the test points
        (unbounded without test points)."""
        if i not in self._box:
            self._box[i] = _face_box(self.table[i]['face'])
        o = np.zeros(3) if o is None else o
        if self._box[i] is not None:
            lo, hi = self._box[i]
            q = (np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]) - o) @ d
            return float(q.min()), float(q.max()), True
        p = self.pts(i)
        if p is None:
            return -np.inf, np.inf, False
        q = (p - o) @ d
        return float(q.min()), float(q.max()), False

    def within(self, i, o, d, r0, r1, t0=None, t1=None):
        """All test points of the face in the radius range [r0, r1] around the axis (o, d) and axially in [t0, t1] (± CLAIM_TOL)."""
        if t0 is not None:
            lo, hi, _ = self.span(i, d, o)
            if hi < t0 - CLAIM_TOL or lo > t1 + CLAIM_TOL:
                return False
        p = self.pts(i)
        if p is None:
            return False
        p = p - o
        t = p @ d
        r = np.linalg.norm(p - np.outer(t, d), axis=1)
        ok = r.min() >= r0 and r.max() <= r1
        return bool(ok and (t0 is None or (t.min() >= t0 - CLAIM_TOL and t.max() <= t1 + CLAIM_TOL)))

    def in_slab(self, i, n, a, b):
        """Does the face lie entirely between the planes x·n = a and x·n = b? The safe bounding box suffices, otherwise test points;
        a face that cannot be sampled (pts None) never lies in it."""
        lo, hi, sure = self.span(i, n)
        if sure and lo >= a and hi <= b:
            return self.pts(i) is not None
        s = self.rim(i) @ n                          # one edge point outside decides without the inner grid
        if not len(s) or s.min() < a or s.max() > b:
            return False
        p = self.pts(i)
        if p is None:
            return False
        s = p @ n
        return bool(s.min() >= a and s.max() <= b)

    def plane(self, i):
        """(outer normal, position x·n) of a planar face or None."""
        if i not in self._plane:
            self._plane[i] = None
            if self.table[i]['kind'] == 'plane':
                p, n = an.face_point_normal(self.table[i]['face'])
                if p is not None:
                    n = prj.unit(n)
                    self._plane[i] = (n, float(p @ n))
        return self._plane[i]

    def topo(self):
        """(edge map, neighbouring faces per face, edge indices per face)."""
        if self._topo is None:
            em = TopTools_IndexedDataMapOfShapeListOfShape()
            TopExp.MapShapesAndAncestors_s(self.shape, TopAbs_EDGE, TopAbs_FACE, em)
            nb, fe = [set() for _ in self.table], [set() for _ in self.table]
            for e in range(1, em.Extent() + 1):
                idx = {self.fmap.FindIndex(f) - 1 for f in em.FindFromIndex(e)} - {-1}
                for a in idx:
                    fe[a].add(e)
                    nb[a] |= idx - {a}
            self._topo = (em, nb, fe)
        return self._topo

    def outer_edges(self, i):
        em, ex, out = self.topo()[0], TopExp_Explorer(BRepTools.OuterWire_s(self.table[i]['face']), TopAbs_EDGE), set()
        while ex.More():
            out.add(em.FindIndex(ex.Current()))
            ex.Next()
        return out

    def cylinder(self, i):
        """(point, direction (_canon), radius, hole?) of a cylindrical face."""
        g = self.table[i]['geom']
        o, d = an.v3(g.Axis().Location()), _canon(prj.unit(an.v3(g.Axis().Direction())))
        p, n = an.face_point_normal(self.table[i]['face'])
        hole = p is not None and float(n @ ((p - o) - ((p - o) @ d) * d)) < 0
        return o, d, g.Radius(), hole


def _axes(F, keep):
    """[(point, direction, [cylindrical faces])] per axis line of the cylindrical faces with keep(radius, hole?); point and
    direction from the largest true cylindrical face of the group (those converted from B-splines carry rounding errors)."""
    groups = []
    for i, x in enumerate(F.table):
        if x['kind'] != 'cylinder':
            continue
        o, d, r, hole = F.cylinder(i)
        if not keep(r, hole):
            continue
        g = next((g for g in groups if abs(g[1] @ d) > 1 - 1e-6 and np.linalg.norm(np.cross(o - g[0], g[1])) < 1e-3), None)
        if g is None:
            groups.append((o, d, [i]))
        else:
            g[2].append(i)
    out = []
    for _, _, members in groups:
        best = max(members, key=lambda i: (BRepAdaptor_Surface(F.table[i]['face']).GetType() == GeomAbs_Cylinder,
                                           F.table[i]['area'], -i))
        out.append((*F.cylinder(best)[:2], members))
    return out


def _coaxial(F, i, o, d):
    if F.table[i]['kind'] != 'cylinder':
        return False
    oi, di, _, _ = F.cylinder(i)
    return abs(di @ d) > 1 - 1e-6 and np.linalg.norm(np.cross(oi - o, d)) < 1e-3


def _thread_claim(F, h):
    """Modelled thread: {faces, axis, start, end} or None. Axis = line of the cylindrical faces of the thread side
    (outside shaft, inside hole) with radius in the window [core Ø/2 - RAD_TOL, nominal Ø/2 + RAD_TOL]; thread region = axial
    extent of the non-analytic faces (flanks) entirely in the window around exactly one such axis (none or several:
    no assignment). This includes the faces entirely in the window and in the region: flanks, coaxial core/crest cylinders and
    planar faces with normal ⟂ axis (profile ends of the helix). Planar faces with normal ∥ axis (thread end, shoulder: they
    fix the thread length), cones (chamfers), tori and other cylinders stay own form features."""
    dims = thread_dims(str(h.get('designation') or ''))
    if dims is None:
        return None
    major, _, pitch = dims
    internal = h.get('mode') == 'internal'
    r0, r1 = (major - (K_INT if internal else K_EXT) * pitch) / 2 - RAD_TOL, major / 2 + RAD_TOL
    found = []
    for o, d, _ in _axes(F, lambda r, hole: r0 <= r <= r1 and hole == internal):
        flanks = [i for i, x in enumerate(F.table) if x['kind'] is None and F.within(i, o, d, r0, r1)]
        if flanks:
            found.append((o, d, flanks))
    if len(found) != 1:
        return None
    o, d, flanks = found[0]
    t = np.concatenate([(F.pts(i) - o) @ d for i in flanks])
    t0, t1 = float(t.min()), float(t.max())
    radial = lambda i: F.plane(i) is not None and abs(F.plane(i)[0] @ d) < 1e-6
    faces = [i for i, x in enumerate(F.table) if (x['kind'] is None or radial(i) or _coaxial(F, i, o, d))
             and F.within(i, o, d, r0, r1, t0, t1)]
    return {'faces': faces, 'axis': _r(d, 6), 'start': _r(o + d * t0), 'end': _r(o + d * t1)}


def _knurl_claim(F, h):
    """Knurl: {faces} or None. Axis = line of the shaft cylinder faces with radius ~ radius (crests, envelope cylinder);
    groove faces = planar faces with normal ⟂ axis, non-analytic faces and axis-parallel, non-coaxial cylinders
    entirely in the band [radius - depth - RAD_TOL, radius + RAD_TOL] and axially in the region of the crests. Assigned only if exactly
    one axis has at least count groove faces. The crests stay the envelope cylinder (knurl leader, Ø dimension)."""
    R, dep, n = _num(h.get('radius')), _num(h.get('depth')), _num(h.get('count'))
    if not (R and dep and n) or not 0 < dep < R:
        return None
    r0, r1 = R - dep - RAD_TOL, R + RAD_TOL
    found = []
    for o, d, crest in _axes(F, lambda r, hole: abs(r - R) <= RAD_TOL and not hole):
        ps = [F.pts(i) for i in crest]
        if any(p is None for p in ps):              # crest that cannot be sampled: region unknown
            continue
        t = np.concatenate([(p - o) @ d for p in ps])
        t0, t1 = float(t.min()), float(t.max())
        grooves = []
        for i, x in enumerate(F.table):
            if i in crest:
                continue
            k = x['kind']
            if k == 'plane':
                pl = F.plane(i)
                ok = pl is not None and abs(pl[0] @ d) < 1e-6
            elif k == 'cylinder':
                di = F.cylinder(i)[1]
                ok = abs(di @ d) > 1 - 1e-6 and not _coaxial(F, i, o, d)
            else:
                ok = k is None
            if ok and F.within(i, o, d, r0, r1, t0, t1):
                grooves.append(i)
        if len(grooves) >= n:
            found.append(grooves)
    return {'faces': found[0]} if len(found) == 1 else None


def _relief(F, comp, n, c, G, bb):
    """Pocket (engraving) or raised area of connected faces comp on the carrier (planar faces G with outer normal n at
    position c): {faces, depth, raised} or None. Conditions: entirely on one side of the carrier; no planar face turned against n
    (otherwise it would be the opposite side of a thin part); the edge lies on inner contours of the carrier, a
    carrier face with an edge on its outer contour is a completely enclosed island (the inside of an "O"); in the plane
    not at the edge of the part. uv = (u min, u max, v min, v max) in the carrier plane (_perp(n)), size = larger extent."""
    s = np.concatenate([F.pts(k) @ n for k in comp]) - c
    if s.max() <= CLAIM_TOL and s.min() < -CLAIM_TOL:
        raised, depth = False, -float(s.min())
    elif s.min() >= -CLAIM_TOL and s.max() > CLAIM_TOL:
        raised, depth = True, float(s.max())
    else:
        return None
    if any(F.plane(k) is not None and F.plane(k)[0] @ n < -1 + 1e-6 for k in comp):
        return None
    fe = F.topo()[2]
    E = set().union(*(fe[k] for k in comp))
    inner = False
    for i in G:
        shared = fe[i] & E
        if not shared:
            continue
        outer = F.outer_edges(i)
        if shared & outer:
            if not outer <= E:
                return None
        else:
            inner = True
    if not inner:
        return None
    P = np.vstack([F.pts(k) for k in comp])
    for k in range(3):
        if abs(n[k]) < 1e-6 and (P[:, k].min() <= bb['min'][k] + CLAIM_TOL or P[:, k].max() >= bb['max'][k] - CLAIM_TOL):
            return None
    X, Y = _perp(n)
    u, v = P @ X, P @ Y
    uv = [round(float(x), 4) for x in (u.min(), u.max(), v.min(), v.max())]
    return {'faces': sorted(comp), 'depth': round(depth, 4), 'raised': raised, 'uv': uv,
            'size': round(max(uv[1] - uv[0], uv[3] - uv[2]), 4)}


def _reliefs(F, t_max, bb):
    """[((carrier normal, position), pocket/raised area)]: per carrier (planar faces of equal outer normal and position) the
    connected neighbouring faces that lie entirely in the layer ± t_max (+ CLAIM_TOL) around the carrier and border only on it
    (a neighbouring face outside the layer rejects the whole group), checked with _relief. Pockets before
    raised areas: the island of an "O" is a raised area seen from the pocket floor, but belongs to the pocket of the surface.
    If two findings overlap, the first applies (pockets, then carriers by normal and position)."""
    nb = F.topo()[1]
    groups = []
    for i in range(len(F.table)):
        pl = F.plane(i)
        if pl is None:
            continue
        g = next((g for g in groups if g[0] @ pl[0] > 1 - 1e-6 and abs(g[1] - pl[1]) < 1e-4), None)
        if g is None:
            groups.append((pl[0], pl[1], [i]))
        else:
            g[2].append(i)
    groups.sort(key=lambda g: (tuple(np.round(g[0], 6)), round(g[1], 4)))
    found = []
    for n, c, members in groups:
        G = set(members)
        seen = set()
        for j in sorted({m for i in members for m in nb[i]} - G):
            if j in seen:
                continue
            comp, stack, mark, ok = [], [j], {j}, True
            while stack:
                k = stack.pop()
                if not F.in_slab(k, n, c - t_max - CLAIM_TOL, c + t_max + CLAIM_TOL):
                    ok = False
                    continue
                comp.append(k)
                for m in nb[k]:
                    if m not in G and m not in mark:
                        mark.add(m)
                        stack.append(m)
            seen |= mark
            got = _relief(F, comp, n, c, G, bb) if ok else None
            if got:
                found.append(((tuple(float(v) for v in np.round(n, 6)), round(float(c), 4)), got))
    out, taken = [], set()
    for key, got in sorted(found, key=lambda it: it[1]['raised']):         # stable: pockets first
        if not taken & set(got['faces']):
            out.append((key, got))
            taken |= set(got['faces'])
    return out


def _boxes(region):
    """Box [x0, y0, z0, x1, y1, z1] of the hint field region as (lo, hi) list; None without a valid specification."""
    if not isinstance(region, list) or not region:
        return None
    out = []
    for b in region:
        v = [_num(x) for x in b] if isinstance(b, list) and len(b) == 6 else None
        if v is None or None in v or any(v[k] > v[k + 3] for k in range(3)):
            return None
        out.append((np.array(v[:3]) - REGION_TOL, np.array(v[3:]) + REGION_TOL))
    return out


def _glyph_rects(font_file, cap, tracking, spacing, lines, align, dy):
    """[(x0, x1, y0, y1)] per glyph in the text frame (reads +X, capitals +Y), as the text operator places them
    (workers/cad-occt/lettering.py text_faces): font size from the cap height of "H", advances AdvanceX with
    kerning, tracking, line spacing (default of the font), alignment align, height offset dy; box = exact
    outline of the glyph (StdPrs_BRepFont.RenderGlyph). None without a readable font file: no estimated region, then
    the strict rule without position applies (an estimate would be too wide)."""
    import os
    font = None
    if isinstance(font_file, str) and os.path.isfile(font_file):
        try:
            from OCP.StdPrs import StdPrs_BRepFont
            from OCP.NCollection import NCollection_Utf8String

            def opened(size):
                f = StdPrs_BRepFont()
                return f if f.Init(NCollection_Utf8String(font_file), size, 0) else None
            probe = opened(100.0)
            hy = an.bbox(probe.RenderGlyph('H'))['max'][1] if probe is not None else 0
            font = opened(100.0 * cap / hy) if hy > 0 else None
        except Exception:
            font = None
    shift = lambda width: {'left': 0.0, 'center': -width / 2, 'right': -width}[align]
    if font is None:
        return None
    spacing = spacing or font.LineSpacing()
    rects = []
    try:
        for row, line in enumerate(lines):
            pen, items = 0.0, []
            for i, ch in enumerate(line):
                nxt = line[i + 1] if i + 1 < len(line) else ' '
                advance = font.AdvanceX(ch, nxt)
                if not ch.isspace():
                    g = font.RenderGlyph(ch)
                    if not g.IsNull():
                        b = an.bbox(g)
                        items.append((b['min'][0] + pen, b['max'][0] + pen, b['min'][1], b['max'][1]))
                pen += advance + tracking
            sx, sy = shift(pen - tracking if line else 0.0), -row * spacing + dy
            rects += [(x0 + sx, x1 + sx, y0 + sy, y1 + sy) for x0, x1, y0, y1 in items]
    except Exception:
        return None
    return rects or None


def _text_boxes(h):
    """World box [x0, y0, z0, x1, y1, z1] per glyph of a text that is cut or placed directly into the part:
    field = world frame of the text construction {origin, x_axis, y_axis, z_axis} (irHints), glyphs by _glyph_rects
    (text, cap_height, tracking, line_spacing, align, valign, font_file), depth from depth to overlap. Otherwise None."""
    fld, text, cap, depth = h.get('field'), h.get('text'), _num(h.get('cap_height')), _num(h.get('depth'))
    if not (isinstance(fld, dict) and isinstance(text, str) and text.strip() and cap and cap > 0 and depth):
        return None
    try:
        O, X, Y, Z = (np.array([float(v) for v in fld[k]], float) for k in ('origin', 'x_axis', 'y_axis', 'z_axis'))
    except (KeyError, TypeError, ValueError):
        return None
    if any(v.shape != (3,) or not np.all(np.isfinite(v)) for v in (O, X, Y, Z)):
        return None
    align, valign = h.get('align') or 'left', h.get('valign') or 'baseline'
    if align not in ('left', 'center', 'right') or valign not in ('baseline', 'center', 'top'):
        return None
    dy = {'baseline': 0.0, 'center': -cap / 2, 'top': -cap}[valign]
    rects = _glyph_rects(h.get('font_file'), cap, _num(h.get('tracking')) or 0.0, _num(h.get('line_spacing')),
                         text.split('\n'), align, dy)
    if not rects:
        return None
    overlap = max(_num(h.get('overlap')) or 0.0, 0.0)
    z0, z1 = (depth, overlap) if depth < 0 else (-overlap, depth)
    out = []
    for x0, x1, y0, y1 in rects:
        P = np.array([O + x * X + y * Y + z * Z for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)])
        out.append([float(v) for v in (*P.min(0), *P.max(0))])
    return out


def _engraving_claim(F, h, found):
    """Engraving: {faces, depth, raised} or None from the pockets/raised areas found (_reliefs) with depth/height <= |depth| +
    CLAIM_TOL; relief (deboss/emboss, from irHints via the operator or its use) fixes the direction.
    Region: region (world box of the lettering/emblem tool from the geometry facts) or, for text directly in the part, the
    glyph boxes from field (_text_boxes). With a region only findings with all test points in one of the boxes (± REGION_TOL)
    and glyph-sized: at most GLYPH_K x text height, without text height (emblem) GLYPH_K x the short side of the box in
    the carrier plane. Without a region only with text and text height: findings up to GLYPH_K x text height, all on exactly one
    carrier, together not larger than number of characters x GLYPH_K x text height (+ one text height) or lines x
    line spacing; otherwise no assignment (better one dimension too many)."""
    depth = _num(h.get('depth'))
    if not depth:
        return None
    want = h.get('relief')
    cand = [(key, r) for key, r in found if r['depth'] <= abs(depth) + CLAIM_TOL
            and (want not in ('deboss', 'emboss') or r['raised'] == (want == 'emboss'))]
    cap = _num(h.get('cap_height'))
    boxes = _boxes(h.get('region'))
    if boxes is None:
        boxes = _boxes(_text_boxes(h))
    if boxes is not None:
        def inside(r):
            P = np.vstack([F.pts(k) for k in r['faces']])
            ok = np.zeros(len(P), bool)
            for lo, hi in boxes:
                ok |= np.all((P >= lo) & (P <= hi), axis=1)
            return bool(ok.all())

        glyphs = _boxes(_text_boxes(h))      # glyph layout (if available) for merged glyph pockets

        def merged_glyphs(key, r):
            """Finding from several touching glyphs: at most as large as the union of the glyph rectangles it intersects."""
            P = np.vstack([F.pts(k) for k in r['faces']])
            lo, hi = P.min(0), P.max(0)
            hit = [(a, b) for a, b in glyphs if np.all(lo <= b) and np.all(hi >= a)]
            if not hit:
                return False
            X, Y = _perp(np.asarray(key[0], float))
            C = np.array([[x, y, z] for a, b in hit for x in (a[0], b[0]) for y in (a[1], b[1]) for z in (a[2], b[2])])
            return r['size'] <= max(np.ptp(C @ X), np.ptp(C @ Y)) + 2 * REGION_TOL

        def glyph_sized(key, r):
            if cap and cap > 0:
                return r['size'] <= GLYPH_K * cap or (glyphs is not None and merged_glyphs(key, r))
            X, Y = _perp(np.asarray(key[0], float))
            short = 0.0
            for lo, hi in boxes:
                C = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
                short = max(short, min(np.ptp(C @ X), np.ptp(C @ Y)))
            return r['size'] <= GLYPH_K * short
        cand = [(key, r) for key, r in cand if glyph_sized(key, r) and inside(r)]
    else:
        text = h.get('text')
        if not cap or cap <= 0 or not isinstance(text, str) or not text.strip():
            return None
        cand = [(key, r) for key, r in cand if r['size'] <= GLYPH_K * cap]
        if len({key for key, _ in cand}) != 1:
            return None
        lines = text.split('\n')
        e = np.array([r['uv'] for _, r in cand])
        du, dv = e[:, 1].max() - e[:, 0].min(), e[:, 3].max() - e[:, 2].min()
        if (max(du, dv) > max(len(s) for s in lines) * GLYPH_K * cap + cap
                or min(du, dv) > (len(lines) - 1) * LINE_K * cap + GLYPH_K * cap):
            return None
    if not cand:
        return None
    return {'faces': [k for _, r in cand for k in r['faces']], 'depth': max(r['depth'] for _, r in cand),
            'raised': all(r['raised'] for _, r in cand)}


def _claims(F, hints, bb):
    """{index in hints: {faces, ...}} of the faces that lie unambiguously in the region of a hint: thread
    (_thread_claim, with axis/region), knurl (_knurl_claim), engraving (_engraving_claim on the findings of _reliefs;
    depth = largest measured depth, raised = all raised). A face already taken over by an earlier hint
    stays with that one."""
    out = {}
    for i, h in enumerate(hints):
        got = _thread_claim(F, h) if h['operator'] == 'thread' else _knurl_claim(F, h) if h['operator'] == 'knurl' else None
        if got and got['faces']:
            out[i] = got
    eng = [i for i, h in enumerate(hints) if h['operator'] in ENGRAVE and _num(h.get('depth'))]
    if eng:
        found = _reliefs(F, max(abs(_num(hints[i]['depth'])) for i in eng), bb)
        for i in eng:
            got = _engraving_claim(F, hints[i], found)
            if got:
                out[i] = got
    seen = set()
    for i in sorted(out):
        out[i]['faces'] = [k for k in out[i]['faces'] if k not in seen]
        seen |= set(out[i]['faces'])
    return {i: c for i, c in out.items() if c['faces']}


def _on_zone(f, zone):
    """Cylinder f coaxial to the thread region zone and axially overlapping it (by more than CLAIM_TOL)."""
    d, o = np.asarray(zone['axis'], float), np.asarray(zone['start'], float)
    a, s, e = np.asarray(f['axis'], float), np.asarray(f['start'], float), np.asarray(f['end'], float)
    if abs(abs(a @ d) - 1) > 1e-6 or np.linalg.norm(np.cross(s - o, d)) > 1e-3:
        return False
    lo, hi = sorted((float((s - o) @ d), float((e - o) @ d)))
    return min(hi, float((np.asarray(zone['end'], float) - o) @ d)) - max(lo, 0.0) > CLAIM_TOL


def match_hints(features, hints, claims=None):
    """(new features, hint texts): thread/knurl/engraving from IR hints {part, operator, ...}; operator text and
    svg_emblem (relief from SVG vectors) yield engraving. claims = {index in hints: assignment (_claims, with absorbs)}:
    every hint feature names the taken-over form features under absorbs ([] without assignment).
    features serves to assign a thread to exactly one cylinder of the part (external: shaft, internal: hole) with Ø in
    [core Ø - 0.1; nominal Ø + 0.1], for a modelled thread only coaxial to its region; with several the one with length ~
    height (+-10 %). Without a cylinder a modelled thread carries axis, start and end of its region (target None),
    otherwise target None + hint text. Engraving: depth = measured depth of the taken-over pockets (raised: raised), otherwise
    magnitude of the hint depth; an SVG emblem without assigned geometry yields no feature (no text, depth unknown)."""
    notes, found, claims = [], [], claims or {}
    for i, h in enumerate(hints):
        part, op, cl = h['part'], h['operator'], claims.get(i)
        absorbs = {'absorbs': list(cl['absorbs']) if cl else []}
        if op == 'thread':
            des = h.get('designation') or ''
            dims = thread_dims(des)
            if dims is None:
                notes.append(f"Thread {des!r} on {part}: designation not legible, no thread specification")
                continue
            major, _, pitch = dims
            internal = h.get('mode') == 'internal'
            minor = major - (K_INT if internal else K_EXT) * pitch
            kind = 'hole' if internal else 'shaft'
            zone = cl if cl and cl.get('axis') is not None else None
            cand = [f for f in features if f['part'] == part and f['kind'] == kind and minor - 0.1 <= f['diameter'] <= major + 0.1
                    and (zone is None or _on_zone(f, zone))]
            if len(cand) > 1 and h.get('height'):
                cand = [f for f in cand if abs(_depth(f) - float(h['height'])) <= 0.1 * float(h['height'])]
            tgt = cand[0] if len(cand) == 1 else None
            if tgt is None and zone is None:
                notes.append(f"Thread {des} on {part}: " + ("no matching cylinder" if not cand else
                             f"{len(cand)} matching cylinders, no unambiguous assignment"))
            at = tgt or ({'start': zone['start'], 'end': zone['end'], 'visible_from': zone.get('visible_from', [])} if zone else None)
            found.append({'kind': 'thread', 'part': part, 'size': major, 'designation': des, 'major_d': major,
                          'minor_d': _r(minor), 'pitch': pitch, 'internal': internal, 'target': tgt['id'] if tgt else None,
                          'start': at['start'] if at else None, 'end': at['end'] if at else None,
                          'visible_from': at['visible_from'] if at else [],
                          **({'axis': zone['axis']} if zone and tgt is None else {}), **absorbs})
        elif op == 'knurl':
            r, n = h.get('radius'), h.get('count')
            found.append({'kind': 'knurl', 'part': part, 'size': 2 * r if r else None, 'profile': h.get('profile'),
                          'count': n, 'radius': r, 'pitch_mm': math.pi * 2 * r / n if r and n else None, 'visible_from': [],
                          **absorbs})
        elif op in ENGRAVE:
            if op == 'svg_emblem' and not cl:
                continue
            depth = _num(h.get('depth'))
            depth = cl['depth'] if cl else (abs(depth) if depth is not None else None)
            found.append({'kind': 'engraving', 'part': part, 'size': h.get('cap_height'), 'text': h.get('text'),
                          'cap_height': h.get('cap_height'), 'depth': depth, 'visible_from': [],
                          **({'raised': True} if cl and cl.get('raised') else {}), **absorbs})
    out = []
    for part in dict.fromkeys(f['part'] for f in found):
        for kind in ('thread', 'knurl', 'engraving'):
            items = sorted((f for f in found if f['part'] == part and f['kind'] == kind), key=_key)
            out += [{'id': f"{part}#{kind}{n}", **f} for n, f in enumerate(items, 1)]
    return out, notes


# ---------------------------------------------------------------- Quantity

def quantity(solid_fingerprints, name):
    """(quantity, note): number of bodies if all are congruent to the first, otherwise 1. A deviating
    '4x' / 'x4' in the name (not part of a word such as M4x10) yields the note 'Quantity according to name n, geometry m'."""
    fps = solid_fingerprints
    n = len(fps) if fps and all(congruent(fps[0], f) for f in fps[1:]) else 1
    m = re.search(r'(?<![\w.,])(\d+)\s*[xX×](?!\w)', name) or re.search(r'(?<![\w.,])[xX×]\s*(\d+)(?![\w.,])', name)
    if m and int(m.group(1)) != n:
        return n, f"Quantity according to name {int(m.group(1))}, geometry {n}"
    return n, None
