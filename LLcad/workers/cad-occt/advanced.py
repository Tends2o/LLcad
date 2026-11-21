"""Bounded native constructors and their actual OCCT builder histories."""
from geometry import *
import time as _time
from OCP.gp import gp_Pln, gp_Ax3, gp_Pnt2d, gp_Dir2d, gp_Lin2d
from OCP.gp import gp_GTrsf, gp_Mat, gp_XYZ
from OCP.GC import GC_MakeArcOfCircle
from OCP.Geom import Geom_CylindricalSurface
from OCP.Geom2d import Geom2d_Line
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex, BRepBuilderAPI_Sewing, BRepBuilderAPI_MakeSolid
from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform, BRepBuilderAPI_Copy
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell, BRepOffsetAPI_MakeOffsetShape
from OCP.BRepLib import BRepLib
from OCP.TopAbs import TopAbs_SHELL
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

TRACKED = {'point', 'line', 'arc', 'plane', 'trim_surface', 'cap', 'sew', 'regularize', 'thread',
           'extrude', 'revolve', 'loft', 'sweep', 'fillet', 'chamfer', 'shell', 'affine_transform', 'rotate', 'offset_solid',
           'draft', 'split_by_plane'}


def self_intersections(shape):
    """OCCT argument analyzer in self-intersection mode; a registered check, not a proof of manufacturability."""
    from OCP.BOPAlgo import BOPAlgo_ArgumentAnalyzer
    analyzer = BOPAlgo_ArgumentAnalyzer()
    analyzer.SetShape1(shape)
    analyzer.SelfInterMode = True
    analyzer.SmallEdgeMode = False
    analyzer.RebuildFaceMode = False
    analyzer.TangentMode = False
    analyzer.MergeVertexMode = False
    analyzer.MergeEdgeMode = False
    analyzer.ContinuityMode = False
    analyzer.CurveOnSurfaceMode = False
    analyzer.Perform()
    return bool(analyzer.HasFaulty())


def rotation_minimizing_frames(path, sections):
    """Double-reflection rotation-minimizing frames along a wire (Wang et al. 2008).

    Unlike a Frenet frame this stays defined where curvature vanishes and does not
    flip at inflections. Returns points, tangents and reference vectors.
    """
    from OCP.BRepAdaptor import BRepAdaptor_CompCurve
    import numpy as np
    curve = BRepAdaptor_CompCurve(as_wire(path))
    first, last = curve.FirstParameter(), curve.LastParameter()
    points, tangents = [], []
    for i in range(sections + 1):
        p = gp_Pnt(); d = gp_Vec()
        curve.D1(first + (last - first) * i / sections, p, d)
        t = np.array([d.X(), d.Y(), d.Z()]); n = np.linalg.norm(t)
        require(n > 1e-12, 'The sweep path has a singular tangent.', 'GEOMETRY_INVALID')
        points.append(np.array([p.X(), p.Y(), p.Z()])); tangents.append(t / n)
    t0 = tangents[0]
    seed = np.array([0., 0., 1.]) if abs(t0[2]) < 0.9 else np.array([1., 0., 0.])
    r = seed - np.dot(seed, t0) * t0; r /= np.linalg.norm(r)
    references = [r]
    for i in range(sections):
        v1 = points[i + 1] - points[i]; c1 = float(np.dot(v1, v1))
        if c1 < 1e-24:
            references.append(references[-1]); continue
        rl = references[-1] - (2 / c1) * np.dot(v1, references[-1]) * v1
        tl = tangents[i] - (2 / c1) * np.dot(v1, tangents[i]) * v1
        v2 = tangents[i + 1] - tl; c2 = float(np.dot(v2, v2))
        rn = rl if c2 < 1e-24 else rl - (2 / c2) * np.dot(v2, rl) * v2
        rn -= np.dot(rn, tangents[i + 1]) * tangents[i + 1]
        references.append(rn / np.linalg.norm(rn))
    return points, tangents, references


def sectioned_sweep(profile, path, twist, scale_end, sections):
    """Sweep with explicit twist and end scale: rotation-minimizing sections lofted into a solid."""
    import numpy as np
    from OCP.gp import gp_GTrsf, gp_Mat, gp_XYZ
    from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
    points, tangents, references = rotation_minimizing_frames(path, sections)
    def frame(i):
        t, r = tangents[i], references[i]
        return np.column_stack([r, np.cross(t, r), t])
    origin0, frame0 = points[0], frame(0)
    inverse0 = frame0.T
    maker = BRepOffsetAPI_ThruSections(True, False, 1e-7)
    maker.SetMutableInput(False)
    for i in range(sections + 1):
        s = i / sections
        angle = twist * s; scale = 1 + (scale_end - 1) * s
        rotation = np.array([[math.cos(angle), -math.sin(angle), 0], [math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
        linear = frame(i) @ (rotation * scale) @ inverse0
        translation = points[i] - linear @ origin0
        transform = gp_GTrsf(gp_Mat(*[float(x) for row in linear for x in row]), gp_XYZ(*map(float, translation)))
        placed = BRepBuilderAPI_GTransform(as_wire(profile), transform, True)
        require(placed.IsDone(), 'The sweep section could not be placed.')
        maker.AddWire(as_wire(placed.Shape()))
    maker.Build()
    return maker

class CopiedTransform:
    """Compose native copy and transform histories; never match by coordinates.

    With a gp_Trsf (rigid motion or uniform scale, possibly improper) the copy is
    moved by BRepBuilderAPI_Transform, which keeps planes, cylinders, cones and
    spheres exact. Only a gp_GTrsf (non-uniform scale or shear) goes through
    BRepBuilderAPI_GTransform, which always converts every face to B-splines."""
    def __init__(self,source,transform):
        self.copy=BRepBuilderAPI_Copy(source,False,False)
        clean=self.copy.Shape();BRepTools.Clean_s(clean)
        if isinstance(transform,gp_Trsf):self.transform=BRepBuilderAPI_Transform(clean,transform,True)
        else:self.transform=BRepBuilderAPI_GTransform(clean,transform,True)
    def Shape(self):return self.transform.Shape()
    def IsDone(self):return self.copy.IsDone() and self.transform.IsDone()
    def Modified(self,source):return self.transform.Modified(self.copy.ModifiedShape(source))
    def Generated(self,source):return self.transform.Generated(self.copy.ModifiedShape(source))

# Relative spread of the singular values up to which a matrix counts as a
# similarity (rotation, reflection, uniform scale). The IR carries decimal
# strings; rigid matrices written with ten or more digits sit near 1e-11.
SIMILARITY_TOLERANCE = 1e-9

def similarity(matrix, translation):
    """(gp_Trsf, scale, proper) when the 3x3 matrix is a rotation or reflection
    times one uniform scale, else None. The transform uses the nearest exact
    similarity s*U*Vt of the SVD; its deviation from the declared matrix is
    below SIMILARITY_TOLERANCE times the scale."""
    import numpy as np
    m=np.array([[float(x) for x in row] for row in matrix],float)
    if m.shape!=(3,3) or not np.all(np.isfinite(m)):return None
    u,s,vt=np.linalg.svd(m)
    scale=float(s.mean())
    if scale<=1e-12 or float(np.max(np.abs(s-scale)))>SIMILARITY_TOLERANCE*scale:return None
    rotation=u@vt
    proper=float(np.linalg.det(rotation))>0
    exact=scale*rotation
    t=[float(x) for x in translation]
    transform=gp_Trsf()
    transform.SetValues(*(float(v) for row,offset in zip(exact,t) for v in [*row,offset]))
    return transform,scale,proper

def elementary_only(shape):
    """Whether every face lies on a plane, cylinder, cone, sphere or torus."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Sphere, GeomAbs_Torus
    kinds = (GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Sphere, GeomAbs_Torus)
    faces = list(explore(shape, TopAbs_FACE))
    return bool(faces) and all(BRepAdaptor_Surface(TopoDS.Face_s(face), False).GetType() in kinds for face in faces)

CONSTRUCTION_REPORTS = {}
# Features whose shape is an exact rigid copy of their first input (set by build,
# gp_Trsf path only): main.py may carry that input's validity, volume, area and
# tolerances over. A B-spline-converted copy is measured afresh.
RIGID_COPIES = set()
# Set by main.py: profile and tolerance, so that operators can check the same gates
# at which the job would otherwise fail only after they have returned.
ACTIVE_PLAN = None


class _BlendBudget(Exception):
    """Search budget exhausted - explicitly NOT the same as incompatible edges."""


def warn(fid, message):
    """A construction warning the caller sees (cad_job_get warnings): never a silent change."""
    report = CONSTRUCTION_REPORTS.setdefault(fid, {})
    if message not in report.setdefault('warnings', []): report['warnings'].append(message)


class MovedMaker:
    """A builder followed by a rigid move, with the builder's history carried through the
    move (symmetric extrude: prism, then half its length back)."""
    def __init__(self, maker, transform):
        self.maker = maker; self.move = BRepBuilderAPI_Transform(maker.Shape(), transform, False)
    def Shape(self): return self.move.Shape()
    def IsDone(self): return self.maker.IsDone() and self.move.IsDone()
    def _map(self, shapes): return [self.move.ModifiedShape(s) for s in shapes]
    def Generated(self, s): return self._map(successors(self.maker, 'Generated', s))
    def Modified(self, s): return self._map(successors(self.maker, 'Modified', s))
    def FirstShape(self): return self.move.ModifiedShape(self.maker.FirstShape())
    def LastShape(self): return self.move.ModifiedShape(self.maker.LastShape())


def successors(maker, method, shape):
    result = getattr(maker, method)(shape)
    if isinstance(result, TopoDS_Shape): return [] if result.IsNull() else [result]
    return list(result)


def face_provenance(shape, trace):
    """(index map of the traced faces of `shape`, their origin lists)."""
    index = TopTools_IndexedMapOfShape(); origins = []
    for entry in trace or []:
        if entry['shape'].ShapeType() != TopAbs_FACE: continue
        slot = index.Add(entry['shape'])
        if slot > len(origins): origins.append(entry['origins'])
    return index, origins


def from_feature(origins, feature, role=None):
    return any(o.get('feature_id') == feature and (role is None or o.get('role') == role) for o in origins)


def faces_by_selector(shape, trace, selector, fid, what):
    """Faces of `shape` chosen by provenance {feature, role?} or by outward normal
    {normal, angle_tolerance_deg?}; at least one must match."""
    faces = [TopoDS.Face_s(face) for face in explore(shape, TopAbs_FACE)]
    if 'feature' in selector:
        require(trace, '%s by origin needs the face history of the input.' % what, 'AMBIGUOUS_SELECTION')
        index, origins = face_provenance(shape, trace)
        picked = [face for face in faces if index.FindIndex(face) and from_feature(origins[index.FindIndex(face) - 1], selector['feature'], selector.get('role'))]
    else:
        target = unit(selector['normal'])
        limit = math.cos(math.radians(float(selector.get('angle_tolerance_deg', '1'))))
        picked = [face for face in faces if (lambda n: n is not None and n.Dot(target) >= limit)(face_normal(face))]
    unique = []
    for face in picked:
        if not any(face.IsSame(u) for u in unique): unique.append(face)
    require(unique, '%s: no face matches %s.' % (what, selector), 'AMBIGUOUS_SELECTION')
    return unique


def wall_faces(shape, pull):
    """Faces whose normal is perpendicular to the pull direction everywhere sampled."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    result = []
    for face in explore(shape, TopAbs_FACE):
        face = TopoDS.Face_s(face); u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        adaptor = BRepAdaptor_Surface(face, True); ok = True
        for a, b in ((.5, .5), (.2, .2), (.8, .2), (.2, .8), (.8, .8)):
            props = BRepLProp_SLProps(adaptor, u0 + (u1 - u0) * a, v0 + (v1 - v0) * b, 1, 1e-9)
            if not props.IsNormalDefined() or abs(props.Normal().Dot(pull)) > 1e-6: ok = False; break
        if ok and not any(face.IsSame(r) for r in result): result.append(face)
    return result


def build(f, deps, traces=None):
    p, c = f['values'], f['construction']; op = c['operator']
    named = {}; maker = None
    origin = gp_Pnt(*(p.get(k, 0) for k in ('x', 'y', 'z')))
    if op == 'affine_transform':
        # OCCT 7.9.3 can leave invalid triangulation links when GTransform is
        # applied after meshing. Transform a native topology copy without its
        # derived mesh, retaining both histories and the original input hash.
        # The exact path is taken for similarities of shapes bounded only by
        # elementary surfaces, which it keeps exact (and fast). A free-form source
        # (extruded or lofted splines) stays on the B-spline conversion it was
        # always built with: measured on a real model, moving an extruded spline
        # exactly changed how a later boolean resolved a near-tangent contact and
        # raised its edge tolerance past the model gate.
        exact=similarity(c['matrix'],c['translation']) if elementary_only(deps[0]) else None
        if exact:
            transform,scale,proper=exact
            CONSTRUCTION_REPORTS.setdefault(f['id'],{})['transform_path']='similarity_gp_Trsf'
            if abs(scale-1)<=SIMILARITY_TOLERANCE:RIGID_COPIES.add(f['id'])
        else:
            transform=gp_GTrsf(gp_Mat(*[float(x) for row in c['matrix'] for x in row]),gp_XYZ(*map(float,c['translation'])))
            CONSTRUCTION_REPORTS.setdefault(f['id'],{})['transform_path']='general_gp_GTrsf_bspline'
        maker=CopiedTransform(deps[0],transform)
    elif op == 'rotate':
        transform=gp_Trsf();transform.SetRotation(gp_Ax1(gp_Pnt(*map(float,c['origin'])),gp_Dir(*map(float,c['axis']))),p['angle'])
        maker=BRepBuilderAPI_Transform(deps[0],transform,False)
    elif op == 'point':
        maker = BRepBuilderAPI_MakeVertex(origin)
    elif op == 'line':
        maker = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Pnt(*map(float,c['start'])), gp_Pnt(*map(float,c['end']))).Edge())
    elif op == 'arc':
        curve = GC_MakeArcOfCircle(*(gp_Pnt(*map(float,point)) for point in c['points']))
        require(curve.IsDone(), 'A circular arc needs three non-collinear points.')
        maker = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(curve.Value()).Edge())
    elif op == 'plane':
        maker = BRepBuilderAPI_MakeFace(gp_Pln(origin, gp_Dir(0,0,1)), 0., p['width'], 0., p['height'])
        named['surface'] = maker.Face()
    elif op == 'trim_surface':
        source = list(explore(deps[0], TopAbs_FACE))
        require(len(source) == 1, 'Trimming needs exactly one face.', 'AMBIGUOUS_SELECTION')
        surf = BRep_Tool.Surface_s(TopoDS.Face_s(source[0]))
        maker = BRepBuilderAPI_MakeFace(surf, p['u_min'], p['u_max'], p['v_min'], p['v_max'], 1e-7)
        require(maker.IsDone(), 'Parameter trimming failed.')
        named['trimmed_surface'] = maker.Face()
    elif op == 'cap':
        maker = BRepBuilderAPI_MakeFace(as_wire(deps[0]), True)
        require(maker.IsDone(), 'A cap face needs a closed planar profile.')
        for wire in deps[1:]: maker.Add(TopoDS.Wire_s(wire.Reversed()))
        named['cap'] = maker.Face()
    elif op == 'sew':
        sewing = BRepBuilderAPI_Sewing(p['tolerance'], True, True, True, False)
        for shape in deps: sewing.Add(shape)
        sewing.Perform(); shape = sewing.SewedShape()
        require(not shape.IsNull(), 'Sewing produces no geometry.')
        if c['make_solid']:
            require(sewing.NbFreeEdges() == 0, 'Open edges prevent a closed solid.')
            shells = list(explore(shape, TopAbs_SHELL))
            if shape.ShapeType() == TopAbs_SHELL: shells = [shape]
            require(len(shells) == 1, 'Solid sewing needs exactly one closed shell.')
            solid = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(shells[0])).Solid()
            require(BRepLib.OrientClosedSolid_s(solid), 'The closed shell cannot be oriented.')
            shape = solid
        return shape, sewing, named
    elif op == 'regularize':
        unify = ShapeUpgrade_UnifySameDomain(deps[0], True, True, True)
        unify.SetSafeInputMode(True); unify.Build()
        return unify.Shape(), unify.History(), named
    elif op == 'thread':
        # Explicit triangular or trapezoidal profile. An ISO basic profile is basic
        # geometry only; tolerance classes and fit are never claimed here.
        r, pitch, height, depth, width = (p[k] for k in ('root_radius','pitch','height','tooth_depth','tooth_width'))
        crest = p.get('crest_width', 0.0); runout = p.get('runout', 0.0)
        require(crest < width, 'The crest width must be smaller than the tooth width.')
        turns = height / pitch; direction = 1 if c['handedness'] == 'right' else -1
        surface = Geom_CylindricalSurface(gp_Ax3(gp_Pnt(0,0,0),gp_Dir(0,0,1)),r)
        du, dz = direction*2*math.pi*turns, height
        line = Geom2d_Line(gp_Lin2d(gp_Pnt2d(0,0),gp_Dir2d(du,dz)))
        edge = BRepBuilderAPI_MakeEdge(line,surface,0.,math.hypot(du,dz)).Edge()
        require(BRepLib.BuildCurves3d_s(edge), 'The helix could not be parameterised.')
        spine = BRepBuilderAPI_MakeWire(edge).Wire()
        polygon = BRepBuilderAPI_MakePolygon()
        profile = [(r-depth*.05,0,-width/2),(r+depth,0,-crest/2),(r+depth,0,crest/2),(r-depth*.05,0,width/2)] if crest > 0 else [(r-depth*.05,0,-width/2),(r+depth,0,0),(r-depth*.05,0,width/2)]
        for point in profile: polygon.Add(gp_Pnt(*point))
        polygon.Close()
        pipe = BRepOffsetAPI_MakePipeShell(spine); pipe.SetMode(True); pipe.Add(polygon.Wire()); pipe.Build()
        require(pipe.IsDone() and pipe.MakeSolid(), 'The thread profile cannot be swept along the helix.')
        core = BRepPrimAPI_MakeCylinder(r,height).Shape()
        ridge = pipe.Shape()
        clip = BRepPrimAPI_MakeCylinder(r+depth*2,height).Shape()
        if runout > 0:
            # Thread runout: the ridge is clipped by a body of revolution whose radius
            # tapers from the root radius to the full crest over the runout length.
            require(2 * runout < height, 'The thread runout must be shorter than half the thread height.')
            # The clip body stays clear of the crest cylinder so no coincident surfaces enter the boolean.
            outer = r + depth * 1.05
            taper = BRepBuilderAPI_MakePolygon()
            for point in [(0,0,0),(r,0,0),(outer,0,runout),(outer,0,height-runout),(r,0,height),(0,0,height)]: taper.Add(gp_Pnt(*point))
            taper.Close()
            clip = BRepPrimAPI_MakeRevol(BRepBuilderAPI_MakeFace(taper.Wire()).Face(),gp_Ax1(gp_Pnt(0,0,0),gp_Dir(0,0,1)),2*math.pi).Shape()
        shape = boolean('intersection', boolean('union', core, ridge), clip)
        if c['mode'] == 'internal': shape = boolean('difference',deps[0],shape)
        return shape, None, named
    elif op == 'offset_solid':
        distance = p['distance']
        maker = BRepOffsetAPI_MakeOffsetShape()
        maker.PerformByJoin(deps[0], distance, 1e-7)
        require(maker.IsDone(), 'The offset solid could not be formed.')
        shape = maker.Shape()
        require(not shape.IsNull() and properties(shape)['volume'] is not None, 'The offset does not yield a closed solid.')
        require(not self_intersections(shape), 'The offset solid has self-contact or self-intersection.', 'GEOMETRY_INVALID')
        return shape, maker, named
    elif op == 'extrude':
        # Along construction +Z (or `axis`, or the inherited normal of a placed profile).
        # '+' sweeps height along the axis, '-' against it, 'both' symmetrically (total
        # height). A profile wound clockwise about the axis is reversed with a warning,
        # so the prism never comes out inside-out.
        face = profile_face(deps[0]); height = p['height']
        axis = unit(f.get('extrude_axis') or c.get('axis') or ['0','0','1'], 'The extrusion axis must not be zero.')
        mode = c.get('direction', '+')
        normal = face_normal(face)
        if normal is not None and normal.Dot(axis) < -1e-9 and mode != '-':
            warn(f['id'], 'The profile is oriented clockwise to the extrusion direction; reversed for positive extrusion.')
            CONSTRUCTION_REPORTS[f['id']]['orientation_normalised'] = True
        sweep = gp_Vec(axis).Multiplied(-height if mode == '-' else height)
        if normal is not None and normal.Dot(gp_Dir(sweep)) < 0: face = TopoDS.Face_s(face.Reversed())
        maker = BRepPrimAPI_MakePrism(face, sweep)
        if mode == 'both':
            back = gp_Trsf(); back.SetTranslation(gp_Vec(axis).Multiplied(-height / 2)); maker = MovedMaker(maker, back)
    elif op == 'revolve':
        axis = gp_Ax1(gp_Pnt(*map(float, c.get('origin', ['0','0','0']))), unit(c.get('axis', ['0','0','1']), 'The rotation axis must not be zero.'))
        face = profile_face(deps[0])
        maker = BRepPrimAPI_MakeRevol(face, axis, p['angle'])
        volume = volume_only(maker.Shape()) if maker.IsDone() else None
        if volume is not None and volume < 0:
            maker = BRepPrimAPI_MakeRevol(TopoDS.Face_s(face.Reversed()), axis, p['angle'])
            warn(f['id'], 'The profile orientation gave a negative volume of revolution; profile reversed.')
            CONSTRUCTION_REPORTS[f['id']]['orientation_normalised'] = True
    elif op == 'draft':
        from OCP.BRepOffsetAPI import BRepOffsetAPI_DraftAngle
        pull = unit(c.get('pull_direction', ['0','0','1']), 'The pull direction must not be zero.')
        neutral = c.get('neutral_plane') or {}
        plane = gp_Pln(gp_Pnt(*map(float, neutral.get('point', ['0','0','0']))), unit(neutral.get('normal') or c.get('pull_direction', ['0','0','1'])))
        selector = c.get('faces', 'walls')
        faces = wall_faces(deps[0], pull) if selector == 'walls' else faces_by_selector(deps[0], traces, selector, f['id'], 'Draft')
        require(faces, 'Draft: no wall face parallel to the pull direction.', 'AMBIGUOUS_SELECTION')
        maker = BRepOffsetAPI_DraftAngle(deps[0])
        for face in faces:
            maker.Add(face, pull, p['angle'], plane, True)
            require(maker.AddDone(), 'The draft cannot be determined for a selected face (curved face without an intersection with the neutral plane?).')
        maker.Build()
        named['drafted_faces'] = len(faces)
    elif op == 'split_by_plane':
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
        plane_spec = c['plane']
        point = gp_Pnt(*map(float, plane_spec.get('point', ['0','0','0'])))
        normal = unit(plane_spec['normal'], 'The cutting plane needs a non-zero normal.')
        b = bounds(deps[0]); size = 2 * math.dist(b[:3], b[3:]) + 1 + max(abs(v) for v in (point.X(), point.Y(), point.Z()))
        tool = BRepBuilderAPI_MakeFace(gp_Pln(point, normal), -size, size, -size, size).Face()
        splitter = BRepAlgoAPI_Splitter(); arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
        arguments.Append(deps[0]); tools.Append(tool)
        splitter.SetArguments(arguments); splitter.SetTools(tools); splitter.SetRunParallel(False); splitter.Build()
        require(splitter.IsDone(), 'Splitting by the plane failed.')
        solids = [TopoDS.Solid_s(s) for s in explore(splitter.Shape(), TopAbs_SOLID)]
        def side(solid):
            props = GProp_GProps(); BRepGProp.VolumeProperties_s(solid, props)
            centre = props.CentreOfMass()
            return gp_Vec(point, centre).Dot(gp_Vec(normal))
        above = [s for s in solids if side(s) > 0]; below = [s for s in solids if side(s) <= 0]
        require(above and below, 'The plane does not cut the solid (all parts lie on one side).', 'GEOMETRY_INVALID')
        keep = c.get('keep', 'both')
        kept = solids if keep == 'both' else above if keep == 'above' else below
        split_faces = []
        for solid in kept:
            for face in explore(solid, TopAbs_FACE):
                n = face_normal(face); centre = GProp_GProps(); BRepGProp.SurfaceProperties_s(face, centre)
                if n is not None and abs(abs(n.Dot(normal)) - 1) < 1e-9 and abs(gp_Vec(point, centre.CentreOfMass()).Dot(gp_Vec(normal))) < 1e-7:
                    split_faces.append(face)
        class Kept:
            def __init__(self, shape): self.shape = shape
            def Shape(self): return self.shape
            def IsDone(self): return True
            def Modified(self, s): return successors(splitter, 'Modified', s)
            def Generated(self, s): return successors(splitter, 'Generated', s)
        if keep == 'both': maker = splitter
        else:
            result = TopoDS_Compound(); builder = BRep_Builder(); builder.MakeCompound(result)
            for solid in kept: builder.Add(result, solid)
            maker = Kept(result if len(kept) > 1 else kept[0])
        named['split_face'] = split_faces
        CONSTRUCTION_REPORTS.setdefault(f['id'], {}).update(solids_above=len(above), solids_below=len(below), kept=keep)
    elif op == 'loft':
        counts = [sum(1 for _ in explore(section_wire(shape), TopAbs_EDGE)) for shape in deps]
        if c.get('check_compatibility', True):
            require(len(set(counts)) == 1, 'Loft sections have different segment counts; correspondence cannot be controlled. Match the sections or switch off check_compatibility explicitly.', 'GEOMETRY_INVALID')
        maker = BRepOffsetAPI_ThruSections(True,bool(c.get('ruled', False)),1e-7)
        maker.SetMutableInput(False)
        for shape in deps: maker.AddWire(section_wire(shape))
        maker.Build()
        named['section_edge_counts'] = counts
    elif op == 'sweep':
        twist = p.get('twist', 0.0); scale_end = p.get('scale_end', 1.0); sections = int(p.get('sections', 16))
        if len(deps) == 3:
            # Guided sweep: the auxiliary spine (input 3) fixes the section's normal
            # direction at every point of the path; scale_end is a linear scaling law.
            from OCP.BRepFill import BRepFill_TypeOfContact
            from OCP.Law import Law_Linear
            require(not twist, 'A guided sweep twists through the auxiliary guide, not through twist.')
            maker = BRepOffsetAPI_MakePipeShell(as_wire(deps[1]))
            contact = {'none': BRepFill_TypeOfContact.BRepFill_NoContact, 'contact': BRepFill_TypeOfContact.BRepFill_Contact,
                       'contact_on_border': BRepFill_TypeOfContact.BRepFill_ContactOnBorder}[c.get('guide_contact', 'none')]
            maker.SetMode(as_wire(deps[2]), bool(c.get('curvilinear_equivalence', True)), contact)
            section = section_wire(deps[0])
            if scale_end != 1.0:
                law = Law_Linear(); law.Set(0., 1., 1., scale_end)
                maker.SetLaw(section, law, False, False)
            else:
                maker.Add(section, False, False)
            maker.Build()
            require(maker.IsDone(), 'The guided sweep failed (auxiliary guide and path must fit each other).')
            require(maker.MakeSolid(), 'The guided sweep does not yield a closed solid.')
            named['frame'] = 'auxiliary_spine'
        elif twist or scale_end != 1.0 or c.get('frame') == 'rotation_minimizing':
            maker = sectioned_sweep(deps[0], deps[1], twist, scale_end, sections)
            named['frame'] = 'rotation_minimizing_double_reflection'
        else:
            maker = BRepOffsetAPI_MakePipe(as_wire(deps[1]),profile_face(deps[0]))
            named['frame'] = 'occt_pipe_corrected_frenet'
    elif op in ('fillet','chamfer'):
        # Edge selection on compound solids: degenerate edges carry no blend
        # face, and the axis check needs a tolerance relative to the solid size,
        # because Boolean results carry coordinate noise.
        size = p['radius'] if op=='fillet' else p['distance']
        size_end = p.get('radius_end') if op=='fillet' else None
        selector = c['edge_selector']
        unique=TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(deps[0],TopAbs_EDGE,unique)
        bb=bounds(deps[0])
        diag=max(1e-9,((bb[3]-bb[0])**2+(bb[4]-bb[1])**2+(bb[5]-bb[2])**2)**0.5)
        tol=max(1e-6, diag*1e-7)
        picked=[]; degenerate=0
        between=None
        if isinstance(selector, dict):
            # Edges where a face of feature A meets a face of feature B (face provenance
            # of the input, i.e. which feature made each face), optionally by role.
            from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
            require(traces, 'between_features needs the face history of the input.', 'AMBIGUOUS_SELECTION')
            index, origins = face_provenance(deps[0], traces[0])
            a, b_ = selector['between_features']; roles = selector.get('roles') or [None, None]
            ancestors = TopTools_IndexedDataMapOfShapeListOfShape(); TopExp.MapShapesAndAncestors_s(deps[0], TopAbs_EDGE, TopAbs_FACE, ancestors)
            between = set()
            for i in range(1, ancestors.Extent()+1):
                faces = []
                for face in ancestors.FindFromIndex(i):
                    if not any(face.IsSame(x) for x in faces): faces.append(face)
                if len(faces) != 2: continue
                o1, o2 = (origins[index.FindIndex(x)-1] if index.FindIndex(x) else [] for x in faces)
                if (from_feature(o1, a, roles[0]) and from_feature(o2, b_, roles[1])) or (from_feature(o1, b_, roles[1]) and from_feature(o2, a, roles[0])):
                    between.add(unique.FindIndex(ancestors.FindKey(i)))
        for i in range(1,unique.Extent()+1):
            edge=TopoDS.Edge_s(unique.FindKey(i))
            if between is not None:
                if i not in between: continue
            else:
                b=bounds(edge)
                vertical=b[3]-b[0]<tol and b[4]-b[1]<tol; horizontal=b[5]-b[2]<tol
                if not (selector=='all' or selector=='vertical' and vertical or selector=='horizontal' and horizontal):
                    continue
            # Check only after the selector, otherwise the report counts degenerate edges
            # that were never candidates.
            if BRep_Tool.Degenerated_s(edge): degenerate+=1; continue
            picked.append(edge)
        require(len(picked)>0,
                'No match for edge_selector=%r among %d edges.' % (selector, unique.Extent()),
                'AMBIGUOUS_SELECTION')
        budget=[max(64, 8*len(picked))]
        deadline=[_time.monotonic()+8.0]
        def blend(edges):
            # IsDone() alone is not enough: validity and boundary tolerance are checked in
            # main.py, long after the operator has returned. Testing only IsDone() here
            # lets candidates through that the gate rejects later.
            if not edges: return None
            if budget[0] <= 0 or _time.monotonic() > deadline[0]: raise _BlendBudget()
            budget[0] -= 1
            m = BRepFilletAPI_MakeFillet(deps[0]) if op=='fillet' else BRepFilletAPI_MakeChamfer(deps[0])
            for e in edges:
                if size_end is not None: m.Add(size, size_end, e)
                else: m.Add(size, e)
            try:
                m.Build()
                if not m.IsDone(): return None
                props = properties(m.Shape())
            except Exception:
                return None
            if not props['valid']: return None
            plan = ACTIVE_PLAN or {}
            limit = plan.get('tolerance')
            if plan.get('profile') in ('precision_cad','manufacturing_candidate') and limit is not None:
                if max(props['native_tolerances_mm'].values()) > limit: return None
            return m
        try:
            maker = blend(picked)
        except _BlendBudget:
            maker = None
        dropped = 0
        if maker is None:
            # A partial result is acceptable since omissions reach the caller as a warning
            # (geometryWarnings in model-service). Without this channel a part with
            # missing edge breaks could not be told from a complete one; therefore
            # every omitted edge is counted.
            def keepable(edges):
                if not edges: return []
                if blend(edges) is not None: return edges
                if len(edges) == 1: return []
                h = len(edges)//2
                left = keepable(edges[:h]); right = keepable(edges[h:])
                both = left + right
                if both and blend(both) is not None: return both
                grown = []
                for e in both:
                    if blend(grown+[e]) is not None: grown.append(e)
                return grown
            hint = ('Individual edges cannot be filleted in the compound. Break the edges on the '
            'single solid before the Boolean operations, or choose edge_selector '
            'more narrowly.')
            try:
                keep = keepable(picked)
                # Edges discarded deep in a sub-branch were never checked against the final
                # set - a follow-up pass collects them.
                for e in picked:
                    if e not in keep and blend(keep+[e]) is not None: keep.append(e)
            except _BlendBudget:
                require(False,
                        'Edge blend on %d edges: the search for the compatible '
                        'subset exceeds the budget. %s' % (len(picked), hint),
                        'BUDGET_EXCEEDED')
            dropped = len(picked) - len(keep)
            # Beyond a third the result is something other than what was ordered;
            # even a warning does not help then.
            require(keep and dropped * 3 <= len(picked),
                    'Edge blend on %d edges: %d of them are incompatible. %s'
                    % (len(picked), dropped, hint), 'GEOMETRY_INVALID')
            try:
                maker = blend(keep)
            except _BlendBudget:
                maker = None
            require(maker is not None,
                    'Edge blend on %d edges failed. %s' % (len(picked), hint),
                    'GEOMETRY_INVALID')
        if dropped: named['blend_edges_skipped'] = dropped
        if degenerate: named['degenerate_edges_skipped'] = degenerate
    elif op == 'shell':
        opening = c['opening']
        if isinstance(opening, str):
            b=bounds(deps[0]); target=b[5] if opening=='top' else b[2]
            faces=[face for face in explore(deps[0],TopAbs_FACE) if abs(bounds(face)[2]-target)<1e-6 and abs(bounds(face)[5]-target)<1e-6]
            require(len(faces)==1,'The shell opening is not unique.','AMBIGUOUS_SELECTION')
        else:
            # Opening faces by provenance {feature, role?} or outward normal {normal}.
            faces = faces_by_selector(deps[0], traces[0] if traces else None, opening, f['id'], 'Shell opening')
            named['opening_faces'] = len(faces)
        remove=TopTools_ListOfShape()
        for face in faces: remove.Append(face)
        maker=BRepOffsetAPI_MakeThickSolid()
        maker.MakeThickSolidByJoin(deps[0],remove,-p['thickness'],1e-7)
    else: raise GeometryError('OUT_OF_SCOPE','Unregistered constructor.')
    require(maker is not None and maker.IsDone(),'Native construction failed.')
    shape=maker.Shape()
    report = {k: named.pop(k) for k in list(named) if k in ('frame', 'section_edge_counts', 'degenerate_edges_skipped', 'blend_edges_skipped', 'drafted_faces', 'opening_faces')}
    if op in ('extrude','revolve','loft','sweep'):
        for role,method in [('start_cap','FirstShape'),('end_cap','LastShape')]:
            face=getattr(maker,method)()
            if not face.IsNull() and face.ShapeType()==TopAbs_FACE: named[role]=face
    if op in ('sweep','loft'):
        # Reported, not changed: an existing model may consume such a body downstream.
        volume = volume_only(shape)
        if volume is not None and volume < 0:
            warn(f['id'], 'The result is oriented inside out (negative volume); check the section orientation.')
    if op == 'sweep' and c.get('self_contact_check', True):
        require(not self_intersections(shape), 'The sweep has self-contact or self-intersection; change the path curvature or profile size, or switch off self_contact_check explicitly.', 'GEOMETRY_INVALID')
    if report: CONSTRUCTION_REPORTS.setdefault(f['id'], {}).update(report)
    return shape, maker, named
