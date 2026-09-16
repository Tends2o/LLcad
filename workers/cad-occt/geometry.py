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
from OCP.TopoDS import TopoDS, TopoDS_Shape, TopoDS_Compound, TopoDS_Iterator
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
    tolerances={}
    for name,kind,cast in [('vertex',TopAbs_VERTEX,TopoDS.Vertex_s),('edge',TopAbs_EDGE,TopoDS.Edge_s),('face',TopAbs_FACE,TopoDS.Face_s)]:
        values=[BRep_Tool.Tolerance_s(cast(item)) for item in explore(shape,kind)]
        require(all(math.isfinite(x) and x>=0 for x in values),'Ungültige native Toleranz.')
        tolerances[name]=max(values,default=0.)
    result = dict(volume=v.Mass() if solid_only else None, area=a.Mass(), bounds=b, volume_integration_relative_error_estimate=volume_error if solid_only else None,
                  native_tolerances_mm=tolerances,
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

class CompoundBuilder:
    """Own only each occurrence's outer topology container.

    OCCT Add locks its child's shared TShape (Free=False). Directly adding a
    placed source therefore mutates source serialization. EmptyCopied keeps
    geometry and descendants shared while giving the compound its own lock.
    Root-face/edge replacements have explicit history, too.
    """
    def __init__(self,shapes):
        self.shape=TopoDS_Compound();builder=BRep_Builder();builder.MakeCompound(self.shape)
        self.inputs=TopTools_IndexedMapOfShape();self.copies={}
        for source in shapes:
            wrapper=source.EmptyCopied();wrapper.Free(True)
            # Add compensates the parent's orientation/location; supply the
            # composed child placement to retain exact TopLoc identity.
            children=TopoDS_Iterator(source,True,True)
            while children.More():
                builder.Add(wrapper,children.Value());children.Next()
            builder.Add(self.shape,wrapper)
            index=self.inputs.Add(source);self.copies.setdefault(index,[]).append(wrapper)
    def Shape(self):return self.shape
    def Modified(self,source):return self.copies.get(self.inputs.FindIndex(source),[])

def compound(shapes):
    return CompoundBuilder(shapes).Shape()

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

def pattern_placements(f):
    """Compact source references and rigid placements; never copy source geometry.

    Circular samples include zero and exclude the end of the angular span.
    Overrides translate in feature-local millimetres after the base placement.
    """
    p,c=f['values'],f['construction'];count=int(p['count'])
    require(1 <= count <= 10000 and count == p['count'], 'Ungültige Musteranzahl.', 'BUDGET_EXCEEDED')
    overrides={o['index']:o for o in c.get('overrides',[])}
    for index in range(count):
        override=overrides.get(index,{})
        slot=f['depends_on'].index(override['source']) if 'source' in override else 0
        transform=gp_Trsf()
        if c['operator']=='circular_pattern':
            transform.SetRotation(gp_Ax1(gp_Pnt(*map(float,c['origin'])),gp_Dir(*map(float,c['axis']))),p['angle']*index/count)
        else:
            transform.SetTranslation(gp_Vec(*(index*p.get(k,0) for k in ('dx','dy','dz'))))
        translation=gp_Trsf();translation.SetTranslation(gp_Vec(*map(float,override.get('translation',[0,0,0]))))
        yield index,slot,translation.Multiplied(transform)

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
    if op in ('pattern','circular_pattern'):
        return compound(BRepBuilderAPI_Transform(deps[slot],transform,False).Shape() for _,slot,transform in pattern_placements(f))
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
        if c['format']=='step' and c.get('component'):
            import step_structure;shape=step_structure.component_shape(path,c['component'])
        elif c['format']=='step':
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
            if len(triangles)>500000:
                raise GeometryError('BUDGET_EXCEEDED','Dreiecksbudget überschritten; gröbere Vorschauabweichung, kleinerer Ausschnitt (region) oder einzelnes Feature wählen.')
        if faces is not None:
            face_ranges.append({'face_id':faces[face_index]['face_id'],'first_triangle':first_triangle,'triangle_count':len(triangles)-first_triangle})
    require(triangles,'Drahtgeometrie benötigt einen eigenen Kurvenviewer.','OUT_OF_SCOPE')
    return dict(vertices=vertices,triangles=triangles,face_ranges=face_ranges,feature_id=feature_id,deflection=deflection,quality='preview_only',certified_bound=None)

def face_curvature_and_feature_size(face):
    """Sampled maximum principal curvature and the smallest bounding extent / edge length of a face."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.GCPnts import GCPnts_AbscissaPoint
    adaptor=BRepAdaptor_Surface(TopoDS.Face_s(face),True);u0,u1,v0,v1=BRepTools.UVBounds_s(TopoDS.Face_s(face))
    kappa=0.;defined=0
    for i in range(1,4):
        for j in range(1,4):
            props=BRepLProp_SLProps(adaptor,u0+(u1-u0)*i/4,v0+(v1-v0)*j/4,2,1e-9)
            if props.IsCurvatureDefined():
                kappa=max(kappa,abs(props.MaxCurvature()),abs(props.MinCurvature()));defined+=1
    b=bounds(face);extents=[b[i+3]-b[i] for i in range(3)];size=min([e for e in extents if e>1e-9] or [max(extents)])
    for edge in explore(face,TopAbs_EDGE):
        try:length=GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(TopoDS.Edge_s(edge)))
        except Exception:continue
        if length>1e-9:size=min(size,length)
    return kappa if defined else None,size

def adaptive_mesh(shape,policy,feature_id='output',faces=None):
    """Per-face deflection h(x) <= min(h_max, alpha d_feature, h_curvature) (Bauplan eq. 55-57).

    The chordal deviation is the registered control; edge lengths from curvature are a
    heuristic and reported as such. Faces are meshed individually, so shared edges may carry
    different polygons: this is a preview, not a watertight body.
    """
    target=policy['target_error_mm'];alpha=policy.get('feature_factor',0.5);ceiling=policy.get('max_deflection_mm',target)
    require(target>=1e-5 and ceiling>=target and 0.01<=alpha<=10,'Ungültige adaptive Vorschauvorgabe.','PRECISION_UNSUPPORTED')
    vertices,triangles,face_ranges=[],[],[];used=[]
    for face_index,face in enumerate(explore(shape,TopAbs_FACE)):
        kappa,size=face_curvature_and_feature_size(face)
        deflection=min(target,ceiling);note=[]
        if kappa:
            feature_cap=kappa*(alpha*size)**2/8
            if feature_cap<deflection:deflection=max(feature_cap,1e-5);note.append('feature_size_cap')
            if deflection>1/kappa:deflection=1/kappa;note.append('sagitta_bound_clamped_to_radius')
        BRepTools.Clean_s(face)
        mesher=BRepMesh_IncrementalMesh(face,deflection,False,0.15,False);mesher.Perform()
        location=TopLoc_Location();tri=BRep_Tool.Triangulation_s(TopoDS.Face_s(face),location)
        if tri is None:continue
        base=len(vertices)
        for i in range(1,tri.NbNodes()+1):
            pnt=tri.Node(i).Transformed(location.Transformation());vertices.append([pnt.X(),pnt.Y(),pnt.Z()])
        first=len(triangles)
        for i in range(1,tri.NbTriangles()+1):
            a,b,c=tri.Triangle(i).Get()
            if face.Orientation()==TopAbs_REVERSED:b,c=c,b
            triangles.append([base+a-1,base+b-1,base+c-1])
            require(len(triangles)<=500000,'Dreiecksbudget überschritten.','BUDGET_EXCEEDED')
        used.append(deflection)
        entry={'first_triangle':first,'triangle_count':len(triangles)-first,'deflection_mm':deflection,'curvature_max_per_mm':kappa,
               'feature_size_mm':size,'edge_length_heuristic_mm':(math.sqrt(8*deflection/kappa) if kappa else None),
               'sagitta_valid':((not kappa) or deflection<=1/kappa),'policy_notes':note}
        if faces is not None:entry['face_id']=faces[face_index]['face_id']
        face_ranges.append(entry)
    require(triangles,'Drahtgeometrie benötigt einen eigenen Kurvenviewer.','OUT_OF_SCOPE')
    b=bounds(shape)
    resolution={'method':'per_face_chordal_deflection_with_feature_size_and_sagitta_policy','absolute_resolution_mm':max(used),'finest_deflection_mm':min(used),
                'reference_scale_mm':math.sqrt(sum((b[i+3]-b[i])**2 for i in range(3))),'minimum_feature_resolved_mm':2*max(used),
                'policy':{'target_error_mm':target,'feature_factor':alpha,'max_deflection_mm':ceiling},
                'edge_length_rule':'heuristic_sqrt(8*epsilon/kappa)_not_a_universal_bound','watertight':False,'certified_surface_bound':None}
    return dict(vertices=vertices,triangles=triangles,face_ranges=face_ranges,feature_id=feature_id,deflection=max(used),quality='preview_only',certified_bound=None,resolution=resolution)

def clip_mesh(mesh_data,center,radius):
    """Spatial excerpt: keep triangles whose centroid lies inside the sphere; vertices are compacted."""
    import numpy as np
    v=np.array(mesh_data['vertices']);keep=[];total=len(mesh_data['triangles'])
    for t in mesh_data['triangles']:
        if np.linalg.norm(v[t].mean(axis=0)-np.asarray(center,float))<=radius:keep.append(t)
    used=sorted({i for t in keep for i in t});remap={old:new for new,old in enumerate(used)}
    result=dict(mesh_data,vertices=[mesh_data['vertices'][i] for i in used],triangles=[[remap[i] for i in t] for t in keep])
    result['clip']={'center':list(map(float,center)),'radius':float(radius),'triangles_before':total,'triangles_after':len(keep),'face_ranges_invalidated':True}
    result.pop('face_ranges',None)
    return result

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
