"""Registered OCCT operations. Input is compiler output, never executable source."""
import io, json, math, hashlib, os
from functools import reduce
from OCP.gp import gp_Pnt, gp_Vec, gp_Dir, gp_Ax1, gp_Ax2, gp_Trsf, gp_Circ
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeSphere, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeTorus, BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire, BRepBuilderAPI_MakeFace, BRepBuilderAPI_Transform
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse, BRepAlgoAPI_Cut, BRepAlgoAPI_Common
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.TopExp import TopExp_Explorer, TopExp
from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX, TopAbs_SOLID, TopAbs_REVERSED
from OCP.TopoDS import TopoDS, TopoDS_Shape, TopoDS_Compound
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepTools import BRepTools
from OCP.TopTools import TopTools_FormatVersion_VERSION_3, TopTools_ListOfShape, TopTools_IndexedMapOfShape
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopLoc import TopLoc_Location
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections, BRepOffsetAPI_MakePipe, BRepOffsetAPI_MakeThickSolid
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet, BRepFilletAPI_MakeChamfer
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.Geom import Geom_BezierCurve, Geom_BSplineCurve, Geom_BSplineSurface
from OCP.GeomAPI import GeomAPI_PointsToBSpline
from OCP.TColgp import TColgp_Array1OfPnt, TColgp_Array2OfPnt
from OCP.TColStd import TColStd_Array1OfReal, TColStd_Array1OfInteger, TColStd_Array2OfReal
from OCP.STEPControl import STEPControl_Reader, STEPControl_Writer, STEPControl_AsIs
from OCP.IFSelect import IFSelect_RetDone
from OCP.StlAPI import StlAPI_Writer, StlAPI_Reader

BUILD = 'OCCT-7.9.3.1.1/mathforge-1'

class GeometryError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

def require(value, message, code='GEOMETRY_INVALID'):
    if not value:
        raise GeometryError(code, message)

def explore(shape, kind):
    it = TopExp_Explorer(shape, kind)
    while it.More():
        yield it.Current()
        it.Next()

def bounds(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    return list(box.Get()) if not box.IsVoid() else None

def properties(shape):
    v, a = GProp_GProps(), GProp_GProps()
    volume_error = BRepGProp.VolumeProperties_s(shape, v, 1e-9, False, False)
    BRepGProp.SurfaceProperties_s(shape, a)
    b = bounds(shape)
    solids=list(explore(shape, TopAbs_SOLID))
    solid_only=bool(solids)
    for kind in (TopAbs_FACE,TopAbs_EDGE,TopAbs_VERTEX):
        all_shapes,solid_shapes=TopTools_IndexedMapOfShape(),TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape,kind,all_shapes)
        for solid in solids:TopExp.MapShapes_s(solid,kind,solid_shapes)
        solid_only=solid_only and all_shapes.Extent()==solid_shapes.Extent()
    result = dict(volume=v.Mass() if solid_only else None, area=a.Mass(), bounds=b, volume_integration_relative_error_estimate=volume_error if solid_only else None,
                  solids=len(solids), precision_solid_only=solid_only,
                  faces=sum(1 for _ in explore(shape, TopAbs_FACE)),
                  valid=BRepCheck_Analyzer(shape, True).IsValid())
    require(b and all(math.isfinite(x) for x in b + [v.Mass(), a.Mass()]), 'Nichtendliche oder leere Geometrie.')
    return result

def write_brep(shape, path):
    require(BRepTools.Write_s(shape, path, False, False, TopTools_FormatVersion_VERSION_3), 'B-Rep konnte nicht gespeichert werden.')

def read_brep(path):
    s = TopoDS_Shape()
    require(BRepTools.Read_s(s, path, BRep_Builder()), 'B-Rep konnte nicht gelesen werden.')
    return s

def shape_hash(shape):
    stream = io.BytesIO()
    BRepTools.Write_s(shape, stream, False, False, TopTools_FormatVersion_VERSION_3)
    return hashlib.sha256(stream.getvalue()).hexdigest()

def compound(shapes):
    result = TopoDS_Compound(); builder = BRep_Builder(); builder.MakeCompound(result)
    for shape in shapes:
        builder.Add(result, shape)
    return result

def boolean(op, a, b):
    operation = {'union':BRepAlgoAPI_Fuse, 'difference':BRepAlgoAPI_Cut, 'intersection':BRepAlgoAPI_Common}[op](a,b)
    operation.SetRunParallel(False); operation.Build()
    require(operation.IsDone(), 'Boolesche Operation fehlgeschlagen.')
    return operation.Shape()

def translated(shape, x=0., y=0., z=0., angle=0., scale=1.):
    t = gp_Trsf(); t.SetScale(gp_Pnt(0,0,0), scale)
    if angle:
        r = gp_Trsf(); r.SetRotation(gp_Ax1(gp_Pnt(0,0,0), gp_Dir(0,0,1)), angle); t = r.Multiplied(t)
    v = gp_Trsf(); v.SetTranslation(gp_Vec(x,y,z)); t = v.Multiplied(t)
    return BRepBuilderAPI_Transform(shape, t, False).Shape()

def as_wire(shape):
    return TopoDS.Wire_s(shape)

def as_face(shape):
    return BRepBuilderAPI_MakeFace(as_wire(shape)).Face()

def spline_basis(definition,count):
    b=definition or {'degree':count-1,'knots':['0','1'],'multiplicities':[count,count]}
    k=TColStd_Array1OfReal(1,len(b['knots']));m=TColStd_Array1OfInteger(1,len(b['knots']))
    for i,(knot,multiplicity) in enumerate(zip(b['knots'],b['multiplicities']),1):
        k.SetValue(i,float(knot));m.SetValue(i,multiplicity)
    return k,m,b['degree']

def make_feature(f, deps):
    p, c = f['values'], f['construction']; op=c['operator']
    from advanced import TRACKED, build
    if op in TRACKED: return build(f, deps)[0]
    x,y,z=(p.get(k,0) for k in ('x','y','z')); origin=gp_Pnt(x,y,z)
    if op=='box': return BRepPrimAPI_MakeBox(origin,p['width'],p['depth'],p['height']).Shape()
    if op=='sphere': return BRepPrimAPI_MakeSphere(origin,p['radius']).Shape()
    if op=='cylinder': return BRepPrimAPI_MakeCylinder(gp_Ax2(origin,gp_Dir(0,0,1)),p['radius'],p['height']).Shape()
    if op=='cone': return BRepPrimAPI_MakeCone(gp_Ax2(origin,gp_Dir(0,0,1)),p['radius'],p['top_radius'],p['height']).Shape()
    if op=='torus': return BRepPrimAPI_MakeTorus(gp_Ax2(origin,gp_Dir(0,0,1)),p['major_radius'],p['minor_radius']).Shape()
    if op in ('union','difference','intersection'): return reduce(lambda a,b:boolean(op,a,b),deps)
    if op=='profile':
        poly=BRepBuilderAPI_MakePolygon()
        for point in c['points']: poly.Add(gp_Pnt(*map(float,point)))
        poly.Close();require(poly.IsDone(),'Profil ist degeneriert.');return poly.Wire()
    if op=='circle': return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(origin,gp_Dir(0,0,1)),p['radius'])).Edge()).Wire()
    if op in ('bezier','bspline'):
        pts=TColgp_Array1OfPnt(1,len(c['points']))
        for i,point in enumerate(c['points'],1):pts.SetValue(i,gp_Pnt(*map(float,point)))
        curve=Geom_BezierCurve(pts) if op=='bezier' else GeomAPI_PointsToBSpline(pts).Curve()
        return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(curve).Edge()).Wire()
    if op=='nurbs_curve':
        pts=TColgp_Array1OfPnt(1,len(c['poles']));weights=TColStd_Array1OfReal(1,len(c['poles']))
        for i,(point,weight) in enumerate(zip(c['poles'],c['weights']),1):
            pts.SetValue(i,gp_Pnt(*map(float,point)));weights.SetValue(i,float(weight))
        knots,mults,degree=spline_basis(c['basis'],len(c['poles']))
        curve=Geom_BSplineCurve(pts,weights,knots,mults,degree,False)
        return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(curve).Edge()).Wire()
    if op in ('hole','groove','pocket'):
        depth=p['depth']; ax=gp_Ax2(gp_Pnt(x,y,z-depth),gp_Dir(0,0,1))
        if op=='hole': cutter=BRepPrimAPI_MakeCylinder(ax,p['radius'],depth+1e-6).Shape()
        elif op=='pocket': cutter=BRepPrimAPI_MakeBox(gp_Pnt(x-p['width']/2,y-p['length']/2,z-depth),p['width'],p['length'],depth+1e-6).Shape()
        else:
            outer=BRepPrimAPI_MakeCylinder(ax,p['radius']+p['width']/2,depth+1e-6).Shape()
            inner=BRepPrimAPI_MakeCylinder(ax,p['radius']-p['width']/2,depth+1e-6).Shape()
            cutter=boolean('difference',outer,inner)
        result=boolean('difference',deps[0],cutter)
        require(properties(deps[0])['volume'] is not None and properties(result)['volume'] is not None,'Schneidoperation benötigt einen geschlossenen Körper.')
        require(properties(deps[0])['volume']-properties(result)['volume']>1e-10,'Schneidwerkzeug schneidet kein Material.')
        return result
    if op in ('instance','transform'):return translated(deps[0],x,y,z,p.get('angle',0),p.get('scale',1))
    if op=='mirror':
        t=gp_Trsf();t.SetMirror(gp_Ax2(gp_Pnt(0,0,0),gp_Dir(1,0,0)));return BRepBuilderAPI_Transform(deps[0],t,True).Shape()
    if op=='pattern':return compound(translated(deps[0],i*p.get('dx',0),i*p.get('dy',0),i*p.get('dz',0)) for i in range(int(p['count'])))
    if op=='assembly':return compound(deps)
    if op=='nurbs_surface':
        nu,nv=len(c['poles']),len(c['poles'][0]);pts=TColgp_Array2OfPnt(1,nu,1,nv);weights=TColStd_Array2OfReal(1,nu,1,nv)
        for i in range(nu):
            for j in range(nv):pts.SetValue(i+1,j+1,gp_Pnt(*map(float,c['poles'][i][j])));weights.SetValue(i+1,j+1,float(c['weights'][i][j]))
        uk,um,ud=spline_basis(c.get('u_basis'),nu);vk,vm,vd=spline_basis(c.get('v_basis'),nv)
        surf=Geom_BSplineSurface(pts,weights,uk,vk,um,vm,ud,vd,False,False)
        return BRepBuilderAPI_MakeFace(surf,1e-7).Shape()
    if op=='imported':
        path='input-'+c['artifact_id']+'.'+c['format']
        if c['format']=='step':
            reader=STEPControl_Reader();require(reader.ReadFile(path)==IFSelect_RetDone,'STEP-Datei ungültig.');reader.TransferRoots();shape=reader.OneShape()
            # OCCT's STEP reader converts declared file units to internal millimetres.
        else:
            shape=TopoDS_Shape();require(StlAPI_Reader().Read(shape,path),'STL-Datei ungültig.')
            shape=translated(shape,scale={'mm':1,'m':1000,'um':0.001}[c['source_unit']])
        return shape
    raise GeometryError('OUT_OF_SCOPE','Operator nicht im nativen Worker registriert.')

def dimensions(f, shape, deps):
    """Measure generated boundaries. Requested parameters alone are not measurements."""
    p,c=f['values'],f['construction'];op=c['operator'];result={}
    b=bounds(shape)
    if op=='box': result.update(width=b[3]-b[0],depth=b[4]-b[1],height=b[5]-b[2])
    if op in ('sphere','cylinder'):result['radius']=(b[3]-b[0])/2
    if op=='cylinder':result['height']=b[5]-b[2]
    if op in ('hole','groove','pocket'):
        removed=boolean('difference',deps[0],shape);rb=bounds(removed)
        require(rb,'Keine messbare Schnittregion.')
        result['depth']=rb[5]-rb[2]
        if op=='groove':
            from OCP.BRepAdaptor import BRepAdaptor_Surface
            from OCP.GeomAbs import GeomAbs_Cylinder
            radii=[]
            for face in explore(removed,TopAbs_FACE):
                a=BRepAdaptor_Surface(TopoDS.Face_s(face))
                if a.GetType()==GeomAbs_Cylinder:radii.append(a.Cylinder().Radius())
            if len(set(round(r,9) for r in radii))==2:result['width']=max(radii)-min(radii)
            # Limited analytic domain: a groove cut in a single planar box base.
            if f.get('base_operator')=='box':result['remaining_wall']=rb[2]-bounds(deps[0])[2]
        if op=='hole':result['radius']=(rb[3]-rb[0])/2
        if op=='pocket':result.update(width=rb[3]-rb[0],length=rb[4]-rb[1])
    return result

def mesh(shape,deflection=0.05,feature_id='output',faces=None):
    require(deflection>=1e-5,'Angeforderte Tessellierung zu fein.','PRECISION_UNSUPPORTED')
    mesher=BRepMesh_IncrementalMesh(shape,deflection,False,0.15,False);mesher.Perform()
    vertices,triangles,face_ranges=[],[],[]
    for face_index,face in enumerate(explore(shape,TopAbs_FACE)):
        location=TopLoc_Location();tri=BRep_Tool.Triangulation_s(TopoDS.Face_s(face),location)
        if tri is None:continue
        base=len(vertices)
        for i in range(1,tri.NbNodes()+1):
            p=tri.Node(i).Transformed(location.Transformation());vertices.append([p.X(),p.Y(),p.Z()])
        first_triangle=len(triangles)
        for i in range(1,tri.NbTriangles()+1):
            a,b,c=tri.Triangle(i).Get()
            if face.Orientation()==TopAbs_REVERSED:b,c=c,b
            triangles.append([base+a-1,base+b-1,base+c-1])
            require(len(triangles)<=500000,'Dreiecksbudget überschritten.','BUDGET_EXCEEDED')
        if faces is not None:
            face_ranges.append({'face_id':faces[face_index]['face_id'],'first_triangle':first_triangle,'triangle_count':len(triangles)-first_triangle})
    require(triangles,'Drahtgeometrie benötigt einen eigenen Kurvenviewer.','OUT_OF_SCOPE')
    return dict(vertices=vertices,triangles=triangles,face_ranges=face_ranges,feature_id=feature_id,deflection=deflection,quality='preview_only',certified_bound=None)

def export_shape(shape,fmt,path,deflection):
    if fmt=='brep':write_brep(shape,path);restored=read_brep(path)
    elif fmt=='step':
        w=STEPControl_Writer();require(w.Transfer(shape,STEPControl_AsIs)==IFSelect_RetDone,'STEP-Transfer fehlgeschlagen.')
        require(w.Write(path)==IFSelect_RetDone,'STEP-Export fehlgeschlagen.')
        r=STEPControl_Reader();require(r.ReadFile(path)==IFSelect_RetDone,'STEP-Roundtrip fehlgeschlagen.');r.TransferRoots();restored=r.OneShape()
    elif fmt=='stl':
        mesh(shape,deflection);w=StlAPI_Writer();w.ASCIIMode=False;require(w.Write(shape,path),'STL-Export fehlgeschlagen.')
        restored=TopoDS_Shape();require(StlAPI_Reader().Read(restored,path),'STL-Roundtrip fehlgeschlagen.')
    else:raise GeometryError('OUT_OF_SCOPE','Exportformat nicht registriert.')
    before,after=properties(shape),properties(restored)
    error=max(abs(a-b) for a,b in zip(before['bounds'],after['bounds']))
    require(error<=max(1e-6,deflection*2 if fmt=='stl' else 1e-5),'Roundtrip-Maße überschreiten den Exportvertrag.')
    require(after['valid'],'Roundtrip-Geometrie ungültig.')
    return dict(status='checks_passed_within_profile',method='roundtrip_bounds_and_occt_validity',before=before,after=after,measured_bounds_error=error,certified_surface_bound=None,unit='mm',lost_semantics=['feature_history'])
