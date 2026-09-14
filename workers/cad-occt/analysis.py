"""Revision-bound native differential measurements and static clearance."""
import math
from geometry import *
from topology import face_list, edge_list
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.gp import gp_Pnt2d
from OCP.TopAbs import TopAbs_IN, TopAbs_ON, TopAbs_REVERSED
from OCP.GeomAbs import GeomAbs_C1, GeomAbs_C2, GeomAbs_C3


def coords(v): return [v.X(),v.Y(),v.Z()]


def finite(v):
    value=float(v)
    require(math.isfinite(value) and abs(value)<=1e12,'Analyseparameter liegt außerhalb des Wertebereichs.','INVALID_SCHEMA')
    return value


def at_break(count,fill,continuity,parameter):
    n=count(continuity);intervals=TColStd_Array1OfReal(1,n+1);fill(intervals,continuity)
    epsilon=1e-12*max(1.,abs(intervals.Value(n+1)-intervals.Value(1)))
    return any(abs(parameter-intervals.Value(i))<=epsilon for i in range(2,n+1))


def sample(shape,selection,curvature=False):
    faces=face_list(shape)
    if faces:
        index=selection.get('face_index')
        require(index is not None and 0<=index<len(faces),'Native Fläche ist nicht eindeutig gebunden.','AMBIGUOUS_SELECTION')
        face=TopoDS.Face_s(faces[index]);bounds_uv=BRepTools.UVBounds_s(face)
        uv=selection.get('uv')
        u,v=map(finite,uv if uv is not None else [(bounds_uv[0]+bounds_uv[1])/2,(bounds_uv[2]+bounds_uv[3])/2])
        require(bounds_uv[0]<=u<=bounds_uv[1] and bounds_uv[2]<=v<=bounds_uv[3], 'UV-Punkt liegt außerhalb des Flächenbereichs.','OUT_OF_SCOPE')
        classifier=BRepClass_FaceClassifier(face,gp_Pnt2d(u,v),1e-9)
        require(classifier.State() in (TopAbs_IN,TopAbs_ON),'UV-Punkt liegt außerhalb der getrimmten Fläche.','OUT_OF_SCOPE')
        adaptor=BRepAdaptor_Surface(face,True);continuity=GeomAbs_C2 if curvature else GeomAbs_C1
        require(not at_break(adaptor.NbUIntervals,adaptor.UIntervals,continuity,u) and not at_break(adaptor.NbVIntervals,adaptor.VIntervals,continuity,v),
                'An dieser inneren Naht ist die benötigte Ableitung nicht stetig.','PRECISION_UNSUPPORTED')
        props=BRepLProp_SLProps(adaptor,u,v,2 if curvature else 1,1e-12)
        require(props.IsNormalDefined(),'Normale ist am gewählten Punkt singulär.','PRECISION_UNSUPPORTED')
        orientation=-1 if face.Orientation()==TopAbs_REVERSED else 1
        result={'kind':'surface','point_mm':coords(adaptor.Value(u,v)),'uv':[u,v],'uv_bounds':list(bounds_uv),
                'direction_kind':'oriented_face_normal','direction':[orientation*x for x in coords(props.Normal())]}
        if curvature:
            require(props.IsCurvatureDefined(),'Krümmung ist am gewählten Punkt nicht definiert.','PRECISION_UNSUPPORTED')
            principal=sorted([orientation*props.MinCurvature(),orientation*props.MaxCurvature()])
            result.update(principal_curvatures_per_mm=principal,mean_curvature_per_mm=sum(principal)/2,
                          gaussian_curvature_per_mm2=principal[0]*principal[1],signed_convention='OCCT_SLProps_with_face_orientation')
        return result
    edges=edge_list(shape)
    require(len(edges)==1,'Kurvenanalyse benötigt ein Feature mit genau einer nativen Kante.','AMBIGUOUS_SELECTION')
    require(selection.get('uv') is None and selection.get('face_index') is None,'Flächenparameter sind für eine Kurve nicht zulässig.','INVALID_SCHEMA')
    edge=TopoDS.Edge_s(edges[0]);adaptor=BRepAdaptor_Curve(edge)
    t=finite(selection.get('curve_parameter','0.5'))
    require(0<=t<=1,'Kurvenparameter muss normiert zwischen 0 und 1 liegen.','OUT_OF_SCOPE')
    native=adaptor.FirstParameter()+t*(adaptor.LastParameter()-adaptor.FirstParameter());finite(native)
    require(not at_break(adaptor.NbIntervals,adaptor.Intervals,GeomAbs_C2 if curvature else GeomAbs_C1,native),
            'Am gewählten Knoten ist die benötigte Kurvenableitung nicht stetig.','PRECISION_UNSUPPORTED')
    p=gp_Pnt();d1=gp_Vec();d2=gp_Vec()
    if curvature:adaptor.D2(native,p,d1,d2)
    else:adaptor.D1(native,p,d1)
    speed=d1.Magnitude();require(speed>1e-12,'Kurventangente ist singulär.','PRECISION_UNSUPPORTED')
    orientation=-1 if edge.Orientation()==TopAbs_REVERSED else 1
    result={'kind':'curve','point_mm':coords(p),'curve_parameter':t,'native_parameter':native,
            'direction_kind':'oriented_curve_tangent','direction':[orientation*x/speed for x in coords(d1)]}
    if curvature:
        cross=d1.Crossed(d2);norm=cross.Magnitude();torsion=None
        if norm>1e-12 and not at_break(adaptor.NbIntervals,adaptor.Intervals,GeomAbs_C3,native):
            d3=gp_Vec()
            try:
                adaptor.D3(native,p,d1,d2,d3);torsion=cross.Dot(d3)/(norm*norm)
            except Exception: pass
        result.update(curvature_per_mm=norm/speed**3,torsion_per_mm=torsion,
                      torsion_status='defined' if torsion is not None else 'undefined_or_C3_unavailable')
    return result


def measure(request,shapes):
    metric=request['metric'];a=shapes[request['feature_id']]
    common={'method':'OCCT_native_differential_geometry','coverage':'specified_local_parameter_points','certified_error_bound':None}
    if metric=='curvature':
        return dict(common,measurements=sample(a,request['selection'],True),unit='per_mm_and_per_mm2')
    b=shapes[request['other_feature_id']]
    if metric=='angle':
        first=sample(a,request['selection']);second=sample(b,request['other_selection'])
        angle=math.acos(max(-1.,min(1.,sum(x*y for x,y in zip(first['direction'],second['direction'])))))
        return dict(common,measurements={'angle_rad':angle,'angle_deg':math.degrees(angle),'first':first,'second':second},unit='rad')
    require(metric=='clearance','Nicht registrierte Analyse.','OUT_OF_SCOPE')
    evaluator=BRepExtrema_DistShapeShape(a,b);evaluator.Perform();require(evaluator.IsDone(),'Freiganganalyse fehlgeschlagen.')
    distance=evaluator.Value();inside=evaluator.InnerSolution();required=request.get('minimum_clearance',0)
    facts={'clearance_mm':distance,'contains_or_intersects_solid':inside,'requested_minimum_mm':required,
           'minimum_satisfied':not inside and distance>=required,'contact_within_native_tolerance':distance<=1e-7,'penetration_depth_mm':None}
    return {'measurements':facts,'unit':'mm','method':'OCCT_BRepExtrema_DistShapeShape','coverage':'two_static_BRep_occupied_regions',
            'certified_error_bound':None,'motion_or_global_wall_certificate':False}
