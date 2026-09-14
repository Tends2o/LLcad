"""Bounded native constructors and their actual OCCT builder histories."""
from geometry import *
from OCP.gp import gp_Pln, gp_Ax3, gp_Pnt2d, gp_Dir2d, gp_Lin2d
from OCP.GC import GC_MakeArcOfCircle
from OCP.Geom import Geom_CylindricalSurface
from OCP.Geom2d import Geom2d_Line
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex, BRepBuilderAPI_Sewing, BRepBuilderAPI_MakeSolid
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
from OCP.BRepLib import BRepLib
from OCP.TopAbs import TopAbs_SHELL
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

TRACKED = {'point', 'line', 'arc', 'plane', 'trim_surface', 'cap', 'sew', 'regularize', 'thread',
           'extrude', 'revolve', 'loft', 'sweep', 'fillet', 'chamfer', 'shell'}

def build(f, deps):
    p, c = f['values'], f['construction']; op = c['operator']
    named = {}; maker = None
    origin = gp_Pnt(*(p.get(k, 0) for k in ('x', 'y', 'z')))
    if op == 'point':
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
        # Explicit custom triangular profile, never a claim of ISO/DIN fit.
        r, pitch, height, depth, width = (p[k] for k in ('root_radius','pitch','height','tooth_depth','tooth_width'))
        turns = height / pitch; direction = 1 if c['handedness'] == 'right' else -1
        surface = Geom_CylindricalSurface(gp_Ax3(gp_Pnt(0,0,0),gp_Dir(0,0,1)),r)
        du, dz = direction*2*math.pi*turns, height
        line = Geom2d_Line(gp_Lin2d(gp_Pnt2d(0,0),gp_Dir2d(du,dz)))
        edge = BRepBuilderAPI_MakeEdge(line,surface,0.,math.hypot(du,dz)).Edge()
        require(BRepLib.BuildCurves3d_s(edge), 'Helix konnte nicht parametrisiert werden.')
        spine = BRepBuilderAPI_MakeWire(edge).Wire()
        polygon = BRepBuilderAPI_MakePolygon()
        for point in [(r-depth*.05,0,-width/2),(r+depth,0,0),(r-depth*.05,0,width/2)]: polygon.Add(gp_Pnt(*point))
        polygon.Close()
        pipe = BRepOffsetAPI_MakePipeShell(spine); pipe.SetMode(True); pipe.Add(polygon.Wire()); pipe.Build()
        require(pipe.IsDone() and pipe.MakeSolid(), 'Gewindeprofil kann nicht entlang der Helix geführt werden.')
        core = BRepPrimAPI_MakeCylinder(r,height).Shape()
        ridge = pipe.Shape()
        clip = BRepPrimAPI_MakeCylinder(r+depth*2,height).Shape()
        shape = boolean('intersection', boolean('union', core, ridge), clip)
        if c['mode'] == 'internal': shape = boolean('difference',deps[0],shape)
        return shape, None, named
    elif op == 'extrude': maker = BRepPrimAPI_MakePrism(as_face(deps[0]),gp_Vec(0,0,p['height']))
    elif op == 'revolve': maker = BRepPrimAPI_MakeRevol(as_face(deps[0]),gp_Ax1(gp_Pnt(0,0,0),gp_Dir(0,0,1)),p['angle'])
    elif op == 'loft':
        maker = BRepOffsetAPI_ThruSections(True,False,1e-7)
        maker.SetMutableInput(False)
        for shape in deps: maker.AddWire(as_wire(shape))
        maker.Build()
    elif op == 'sweep': maker = BRepOffsetAPI_MakePipe(as_wire(deps[1]),as_face(deps[0]))
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
    if op in ('extrude','revolve','loft','sweep'):
        for role,method in [('start_cap','FirstShape'),('end_cap','LastShape')]:
            face=getattr(maker,method)()
            if not face.IsNull() and face.ShapeType()==TopAbs_FACE: named[role]=face
    return shape, maker, named
