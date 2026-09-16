"""Geometric distance and thickness measures with explicit proof strength (Bauplan 6.18, 17.4).

Every number carries its strength: `sampled` values come from finitely many surface samples and
never bound the continuous distance; `exact_for_declared_domain` values come from the native
kernel's exact extrema or boolean volumes within its tolerances. Chamfer/Hausdorff samples are
never reported as maximal continuous errors, and a volume IoU never proves small details.
"""
import math
import numpy as np
from geometry import require, explore, bounds, boolean, properties, TopoDS, TopAbs_FACE, TopAbs_SOLID, gp_Pnt, gp_Dir, gp_Vec, gp_Trsf, BRepBuilderAPI_Transform, BRepExtrema_DistShapeShape, BRepTools
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.gp import gp_Pnt2d, gp_Lin
from OCP.TopAbs import TopAbs_IN, TopAbs_ON, TopAbs_REVERSED
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp

MAX_SAMPLES = 400


def surface_samples(shape, budget=MAX_SAMPLES, seed=7):
    """Deterministic quasi-random surface samples with outward normals, weighted by face area."""
    faces = [TopoDS.Face_s(f) for f in explore(shape, TopAbs_FACE)]
    require(faces, 'Abtastung benötigt native Flächen.', 'OUT_OF_SCOPE')
    areas = []
    for face in faces:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        areas.append(max(props.Mass(), 0.0))
    total = sum(areas)
    require(total > 0, 'Abtastung benötigt Flächen mit positivem Inhalt.', 'GEOMETRY_INVALID')
    rng = np.random.default_rng(seed)
    samples = []
    for face, area in zip(faces, areas):
        count = max(4, int(round(budget * area / total)))
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        adaptor = BRepAdaptor_Surface(face, True)
        orientation = -1.0 if face.Orientation() == TopAbs_REVERSED else 1.0
        attempts = 0
        accepted = 0
        while accepted < count and attempts < count * 8:
            attempts += 1
            u = u0 + (u1 - u0) * rng.random()
            v = v0 + (v1 - v0) * rng.random()
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-9).State() not in (TopAbs_IN, TopAbs_ON):
                continue
            props = BRepLProp_SLProps(adaptor, u, v, 1, 1e-12)
            if not props.IsNormalDefined():
                continue
            p = adaptor.Value(u, v)
            n = props.Normal()
            samples.append(((p.X(), p.Y(), p.Z()), (orientation * n.X(), orientation * n.Y(), orientation * n.Z())))
            accepted += 1
    require(samples, 'Keine gültigen Oberflächenproben.', 'GEOMETRY_INVALID')
    return samples


def _nearest(point, shape):
    evaluator = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex(), shape)
    evaluator.Perform()
    require(evaluator.IsDone(), 'Abstandsauswertung fehlgeschlagen.')
    return evaluator.Value()


def surface_distance(a, b, budget=MAX_SAMPLES):
    """Sampled Chamfer and Hausdorff estimates, exact minimum distance and exact volume IoU."""
    forward = [_nearest(p, b) for p, _ in surface_samples(a, budget, 11)]
    backward = [_nearest(p, a) for p, _ in surface_samples(b, budget, 13)]
    evaluator = BRepExtrema_DistShapeShape(a, b)
    evaluator.Perform()
    require(evaluator.IsDone(), 'Abstandsauswertung fehlgeschlagen.')
    solids_a = sum(1 for _ in explore(a, TopAbs_SOLID))
    solids_b = sum(1 for _ in explore(b, TopAbs_SOLID))
    iou = None
    if solids_a and solids_b:
        va, vb = properties(a)['volume'], properties(b)['volume']
        try:
            common = properties(boolean('intersection', a, b))['volume']
        except Exception:
            common = 0.0
        union = va + vb - common
        iou = {'value': common / union if union > 0 else None, 'intersection_mm3': common, 'union_mm3': union,
               'guarantee': 'exact_for_declared_domain', 'method': 'OCCT_boolean_volumes_within_kernel_tolerance',
               'note': 'a volume ratio does not prove small or thin details'}
    return {
        'sampled_chamfer_mm': {'value': (float(np.mean(forward)) + float(np.mean(backward))) / 2, 'guarantee': 'sampled',
                               'samples': [len(forward), len(backward)], 'method': 'mean_nearest_distance_of_area_weighted_surface_samples'},
        'sampled_hausdorff_mm': {'value': max(max(forward), max(backward)), 'guarantee': 'sampled', 'samples': [len(forward), len(backward)],
                                 'directed_mm': [max(forward), max(backward)],
                                 'note': 'maximum over finitely many samples; not an upper bound of the continuous Hausdorff distance'},
        'exact_minimum_distance_mm': {'value': evaluator.Value(), 'guarantee': 'exact_for_declared_domain', 'method': 'OCCT_BRepExtrema_DistShapeShape',
                                      'overlapping_solids': bool(evaluator.InnerSolution())},
        'volume_iou': iou,
    }


def wall_thickness(shape, budget=MAX_SAMPLES):
    """Sampled wall thickness: inward ray from each surface sample to its first exit."""
    solids = sum(1 for _ in explore(shape, TopAbs_SOLID))
    require(solids >= 1, 'Wandstärke benötigt einen Volumenkörper.', 'OUT_OF_SCOPE')
    intersector = IntCurvesFace_ShapeIntersector()
    intersector.Load(shape, 1e-9)
    b = bounds(shape)
    diagonal = math.sqrt(sum((b[i + 3] - b[i]) ** 2 for i in range(3))) + 1.0
    values = []
    for point, normal in surface_samples(shape, budget, 17):
        inward = gp_Dir(-normal[0], -normal[1], -normal[2])
        intersector.Perform(gp_Lin(gp_Pnt(*point), inward), 1e-6, diagonal)
        if not intersector.IsDone():
            continue
        distances = [intersector.WParameter(i) for i in range(1, intersector.NbPnt() + 1) if intersector.WParameter(i) > 1e-6]
        if distances:
            values.append(min(distances))
    require(values, 'Keine gültigen Wandstärkenproben.', 'GEOMETRY_INVALID')
    arr = np.array(values)
    return {'minimum_mm': float(arr.min()), 'mean_mm': float(arr.mean()), 'p10_mm': float(np.percentile(arr, 10)), 'samples': int(arr.size),
            'guarantee': 'sampled', 'method': 'inward_normal_ray_to_first_exit_over_area_weighted_surface_samples',
            'coverage': 'finite_surface_samples_not_a_global_minimum_certificate'}


def motion_clearance(a, b, translation, steps, minimum=0.0):
    """Clearance sampled along a linear motion of b; no continuous sweep certificate."""
    require(1 <= steps <= 64, 'Bewegungsschritte: 1 bis 64.', 'BUDGET_EXCEEDED')
    samples = []
    for i in range(steps + 1):
        t = i / steps
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(*[t * float(x) for x in translation]))
        moved = BRepBuilderAPI_Transform(b, trsf, True).Shape()
        evaluator = BRepExtrema_DistShapeShape(a, moved)
        evaluator.Perform()
        require(evaluator.IsDone(), 'Freiganganalyse fehlgeschlagen.')
        overlap = 0.0
        if evaluator.Value() <= 1e-7:
            # Contact or penetration: the exact common volume decides between touching and overlapping.
            try:
                overlap = properties(boolean('intersection', a, moved))['volume']
            except Exception:
                overlap = 0.0
        samples.append({'t': t, 'clearance_mm': evaluator.Value(), 'contains_or_intersects_solid': bool(evaluator.InnerSolution()) or overlap > 1e-9,
                        'overlap_volume_mm3': overlap})
    worst = min(samples, key=lambda s: (-1 if s['contains_or_intersects_solid'] else s['clearance_mm']))
    return {'samples': samples, 'minimum_sampled_clearance_mm': worst['clearance_mm'], 'collision_sampled': any(s['contains_or_intersects_solid'] for s in samples),
            'at_parameter': worst['t'], 'requested_minimum_mm': minimum,
            'minimum_satisfied_at_samples': all(not s['contains_or_intersects_solid'] and s['clearance_mm'] >= minimum for s in samples),
            'guarantee': 'sampled', 'coverage': 'linear_translation_path_sampled_at_%d_positions' % (steps + 1),
            'note': 'positions between samples are not checked; swept volumes are not certified'}
