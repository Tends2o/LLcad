"""Bounded native constructors and their actual OCCT builder histories."""
from geometry import *
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
           'extrude', 'revolve', 'loft', 'sweep', 'fillet', 'chamfer', 'shell', 'affine_transform', 'rotate', 'offset_solid'}


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
        require(n > 1e-12, 'Sweep-Pfad hat eine singuläre Tangente.', 'GEOMETRY_INVALID')
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
        require(placed.IsDone(), 'Sweep-Sektion konnte nicht platziert werden.')
        maker.AddWire(as_wire(placed.Shape()))
    maker.Build()
    return maker

class CopiedTransform:
    """Compose native copy and transform histories; never match by coordinates."""
    def __init__(self,source,transform):
        self.copy=BRepBuilderAPI_Copy(source,False,False)
        clean=self.copy.Shape();BRepTools.Clean_s(clean)
        self.transform=BRepBuilderAPI_GTransform(clean,transform,True)
    def Shape(self):return self.transform.Shape()
    def IsDone(self):return self.copy.IsDone() and self.transform.IsDone()
    def Modified(self,source):return self.transform.Modified(self.copy.ModifiedShape(source))
    def Generated(self,source):return self.transform.Generated(self.copy.ModifiedShape(source))

CONSTRUCTION_REPORTS = {}


def build(f, deps):
    p, c = f['values'], f['construction']; op = c['operator']
    named = {}; maker = None
    origin = gp_Pnt(*(p.get(k, 0) for k in ('x', 'y', 'z')))
    if op == 'affine_transform':
        # OCCT 7.9.3 can leave invalid triangulation links when GTransform is
        # applied after meshing. Transform a native topology copy without its
        # derived mesh, retaining both histories and the original input hash.
        transform=gp_GTrsf(gp_Mat(*[float(x) for row in c['matrix'] for x in row]),gp_XYZ(*map(float,c['translation'])))
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
        require(curve.IsDone(), 'Kreisbogen benötigt drei nicht kollineare Punkte.')
        maker = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(curve.Value()).Edge())
    elif op == 'plane':
        maker = BRepBuilderAPI_MakeFace(gp_Pln(origin, gp_Dir(0,0,1)), 0., p['width'], 0., p['height'])
        named['surface'] = maker.Face()
    elif op == 'trim_surface':
        source = list(explore(deps[0], TopAbs_FACE))
        require(len(source) == 1, 'Trimmung benötigt genau eine Fläche.', 'AMBIGUOUS_SELECTION')
        surf = BRep_Tool.Surface_s(TopoDS.Face_s(source[0]))
        maker = BRepBuilderAPI_MakeFace(surf, p['u_min'], p['u_max'], p['v_min'], p['v_max'], 1e-7)
        require(maker.IsDone(), 'Parametertrimmung fehlgeschlagen.')
        named['trimmed_surface'] = maker.Face()
    elif op == 'cap':
        maker = BRepBuilderAPI_MakeFace(as_wire(deps[0]), True)
        require(maker.IsDone(), 'Deckfläche benötigt ein geschlossenes planares Profil.')
        for wire in deps[1:]: maker.Add(TopoDS.Wire_s(wire.Reversed()))
        named['cap'] = maker.Face()
    elif op == 'sew':
        sewing = BRepBuilderAPI_Sewing(p['tolerance'], True, True, True, False)
        for shape in deps: sewing.Add(shape)
        sewing.Perform(); shape = sewing.SewedShape()
        require(not shape.IsNull(), 'Vernähen erzeugt keine Geometrie.')
        if c['make_solid']:
            require(sewing.NbFreeEdges() == 0, 'Offene Kanten verhindern einen geschlossenen Körper.')
            shells = list(explore(shape, TopAbs_SHELL))
            if shape.ShapeType() == TopAbs_SHELL: shells = [shape]
            require(len(shells) == 1, 'Solid-Vernähen benötigt genau eine geschlossene Schale.')
            solid = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(shells[0])).Solid()
            require(BRepLib.OrientClosedSolid_s(solid), 'Geschlossene Schale kann nicht orientiert werden.')
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
        require(crest < width, 'Kammbreite muss kleiner als die Zahnbreite sein.')
        turns = height / pitch; direction = 1 if c['handedness'] == 'right' else -1
        surface = Geom_CylindricalSurface(gp_Ax3(gp_Pnt(0,0,0),gp_Dir(0,0,1)),r)
        du, dz = direction*2*math.pi*turns, height
        line = Geom2d_Line(gp_Lin2d(gp_Pnt2d(0,0),gp_Dir2d(du,dz)))
        edge = BRepBuilderAPI_MakeEdge(line,surface,0.,math.hypot(du,dz)).Edge()
        require(BRepLib.BuildCurves3d_s(edge), 'Helix konnte nicht parametrisiert werden.')
        spine = BRepBuilderAPI_MakeWire(edge).Wire()
        polygon = BRepBuilderAPI_MakePolygon()
        profile = [(r-depth*.05,0,-width/2),(r+depth,0,-crest/2),(r+depth,0,crest/2),(r-depth*.05,0,width/2)] if crest > 0 else [(r-depth*.05,0,-width/2),(r+depth,0,0),(r-depth*.05,0,width/2)]
        for point in profile: polygon.Add(gp_Pnt(*point))
        polygon.Close()
        pipe = BRepOffsetAPI_MakePipeShell(spine); pipe.SetMode(True); pipe.Add(polygon.Wire()); pipe.Build()
        require(pipe.IsDone() and pipe.MakeSolid(), 'Gewindeprofil kann nicht entlang der Helix geführt werden.')
        core = BRepPrimAPI_MakeCylinder(r,height).Shape()
        ridge = pipe.Shape()
        clip = BRepPrimAPI_MakeCylinder(r+depth*2,height).Shape()
        if runout > 0:
            # Thread runout: the ridge is clipped by a body of revolution whose radius
            # tapers from the root radius to the full crest over the runout length.
            require(2 * runout < height, 'Gewindeauslauf muss kürzer als die halbe Gewindehöhe sein.')
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
        require(maker.IsDone(), 'Versatzkörper konnte nicht gebildet werden.')
        shape = maker.Shape()
        require(not shape.IsNull() and properties(shape)['volume'] is not None, 'Versatz ergibt keinen geschlossenen Körper.')
        require(not self_intersections(shape), 'Versatzkörper hat Selbstkontakt oder Selbstdurchdringung.', 'GEOMETRY_INVALID')
        return shape, maker, named
    elif op == 'extrude': maker = BRepPrimAPI_MakePrism(as_face(deps[0]),gp_Vec(0,0,p['height']))
    elif op == 'revolve': maker = BRepPrimAPI_MakeRevol(as_face(deps[0]),gp_Ax1(gp_Pnt(0,0,0),gp_Dir(0,0,1)),p['angle'])
    elif op == 'loft':
        counts = [sum(1 for _ in explore(shape, TopAbs_EDGE)) for shape in deps]
        if c.get('check_compatibility', True):
            require(len(set(counts)) == 1, 'Loft-Querschnitte haben unterschiedliche Segmentzahlen; Korrespondenz nicht kontrollierbar. Querschnitte angleichen oder check_compatibility ausdrücklich abschalten.', 'GEOMETRY_INVALID')
        maker = BRepOffsetAPI_ThruSections(True,bool(c.get('ruled', False)),1e-7)
        maker.SetMutableInput(False)
        for shape in deps: maker.AddWire(as_wire(shape))
        maker.Build()
        named['section_edge_counts'] = counts
    elif op == 'sweep':
        twist = p.get('twist', 0.0); scale_end = p.get('scale_end', 1.0); sections = int(p.get('sections', 16))
        if twist or scale_end != 1.0 or c.get('frame') == 'rotation_minimizing':
            maker = sectioned_sweep(deps[0], deps[1], twist, scale_end, sections)
            named['frame'] = 'rotation_minimizing_double_reflection'
        else:
            maker = BRepOffsetAPI_MakePipe(as_wire(deps[1]),as_face(deps[0]))
            named['frame'] = 'occt_pipe_corrected_frenet'
    elif op in ('fillet','chamfer'):
        maker = BRepFilletAPI_MakeFillet(deps[0]) if op=='fillet' else BRepFilletAPI_MakeChamfer(deps[0])
        count=0; unique=TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(deps[0],TopAbs_EDGE,unique)
        for i in range(1,unique.Extent()+1):
            edge=unique.FindKey(i); b=bounds(edge)
            vertical=b[3]-b[0]<1e-6 and b[4]-b[1]<1e-6; horizontal=b[5]-b[2]<1e-6
            if c['edge_selector']=='all' or c['edge_selector']=='vertical' and vertical or c['edge_selector']=='horizontal' and horizontal:
                maker.Add(p['radius'] if op=='fillet' else p['distance'],TopoDS.Edge_s(edge)); count+=1
        require(count>0,'Keine eindeutigen Kanten für den Filter.','AMBIGUOUS_SELECTION'); maker.Build()
    elif op == 'shell':
        b=bounds(deps[0]); target=b[5] if c['opening']=='top' else b[2]
        faces=[face for face in explore(deps[0],TopAbs_FACE) if abs(bounds(face)[2]-target)<1e-6 and abs(bounds(face)[5]-target)<1e-6]
        require(len(faces)==1,'Schalenöffnung nicht eindeutig.','AMBIGUOUS_SELECTION')
        remove=TopTools_ListOfShape(); remove.Append(faces[0]); maker=BRepOffsetAPI_MakeThickSolid()
        maker.MakeThickSolidByJoin(deps[0],remove,-p['thickness'],1e-7)
    else: raise GeometryError('OUT_OF_SCOPE','Nicht registrierter Konstruktor.')
    require(maker is not None and maker.IsDone(),'Native Konstruktion fehlgeschlagen.')
    shape=maker.Shape()
    report = {k: named.pop(k) for k in list(named) if k in ('frame', 'section_edge_counts')}
    if op in ('extrude','revolve','loft','sweep'):
        for role,method in [('start_cap','FirstShape'),('end_cap','LastShape')]:
            face=getattr(maker,method)()
            if not face.IsNull() and face.ShapeType()==TopAbs_FACE: named[role]=face
    if op == 'sweep' and c.get('self_contact_check', True):
        require(not self_intersections(shape), 'Sweep hat Selbstkontakt oder Selbstdurchdringung; Pfadkrümmung oder Profilgröße ändern oder self_contact_check ausdrücklich abschalten.', 'GEOMETRY_INVALID')
    if report: CONSTRUCTION_REPORTS[f['id']] = report
    return shape, maker, named
