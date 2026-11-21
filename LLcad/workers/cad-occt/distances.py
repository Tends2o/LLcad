"""Geometric distance and thickness measures with explicit proof strength.

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
    require(faces, 'Sampling needs native faces.', 'OUT_OF_SCOPE')
    areas = []
    for face in faces:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        areas.append(max(props.Mass(), 0.0))
    total = sum(areas)
    require(total > 0, 'Sampling needs faces with positive area.', 'GEOMETRY_INVALID')
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
    require(samples, 'No valid surface samples.', 'GEOMETRY_INVALID')
    return samples


def _nearest(point, shape):
    evaluator = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex(), shape)
    evaluator.Perform()
    require(evaluator.IsDone(), 'Distance evaluation failed.')
    return evaluator.Value()


def surface_distance(a, b, budget=MAX_SAMPLES):
    """Sampled Chamfer and Hausdorff estimates, exact minimum distance and exact volume IoU."""
    forward = [_nearest(p, b) for p, _ in surface_samples(a, budget, 11)]
    backward = [_nearest(p, a) for p, _ in surface_samples(b, budget, 13)]
    evaluator = BRepExtrema_DistShapeShape(a, b)
    evaluator.Perform()
    require(evaluator.IsDone(), 'Distance evaluation failed.')
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
    require(solids >= 1, 'Wall thickness needs a solid.', 'OUT_OF_SCOPE')
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
    require(values, 'No valid wall thickness samples.', 'GEOMETRY_INVALID')
    arr = np.array(values)
    return {'minimum_mm': float(arr.min()), 'mean_mm': float(arr.mean()), 'p10_mm': float(np.percentile(arr, 10)), 'samples': int(arr.size),
            'guarantee': 'sampled', 'method': 'inward_normal_ray_to_first_exit_over_area_weighted_surface_samples',
            'coverage': 'finite_surface_samples_not_a_global_minimum_certificate'}


MAX_LISTED_PAIRS = 200
# Serialized size of the listed items; the MCP answer (with envelope) must stay below 30 KiB.
RESPONSE_BUDGET_BYTES = 22000
MAX_LISTED_COINCIDENT = 64
MAX_FACE_PAIR_CHECKS = 20000
PARALLEL_SINE = 0.02  # about 1.1 degrees; the sampled gap test below decides


def _box_gap(a, b):
    """Euclidean lower bound of the distance between two axis-aligned boxes."""
    return math.sqrt(sum(max(0.0, a[i] - b[i + 3], b[i] - a[i + 3]) ** 2 for i in range(3)))


def _union_box(boxes):
    return [*(min(b[i] for b in boxes) for i in range(3)), *(max(b[i + 3] for b in boxes) for i in range(3))]


def _fast_box(shape, pad):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid():
        return None
    b = list(box.Get())
    return [b[0] - pad, b[1] - pad, b[2] - pad, b[3] + pad, b[4] + pad, b[5] + pad]


def _overlaps(a, b):
    return all(a[i] <= b[i + 3] and b[i] <= a[i + 3] for i in range(3))


class _Face:
    """One planar or cylindrical face of a contact-graph node, with what the
    coincidence test needs: world surface description, its native surface in
    local coordinates (for projection and trimming tests) and a padded box."""
    def __init__(self, node, feature, index, face, face_id, area, pad):
        from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder
        from OCP.TopLoc import TopLoc_Location
        from OCP.BRep import BRep_Tool
        self.node, self.feature, self.index, self.face, self.face_id = node, feature, index, TopoDS.Face_s(face), face_id
        adaptor = BRepAdaptor_Surface(self.face, True)
        kind = adaptor.GetType()
        self.kind = 'plane' if kind == GeomAbs_Plane else 'cylinder' if kind == GeomAbs_Cylinder else None
        if self.kind is None:
            return
        if self.kind == 'plane':
            ax = adaptor.Plane().Axis()
            self.radius = None
        else:
            cylinder = adaptor.Cylinder()
            ax = cylinder.Axis()
            self.radius = cylinder.Radius()
        self.origin = np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()])
        self.direction = np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()])
        self.adaptor = adaptor
        self.location = TopLoc_Location()
        self.surface = BRep_Tool.Surface_s(self.face, self.location)
        self.inverse = self.location.Transformation().Inverted()
        self.uv = BRepTools.UVBounds_s(self.face)
        self.box = _fast_box(self.face, pad)
        if area is None:
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(self.face, props)
            area = props.Mass()
        self.area = max(float(area), 0.0)

    def compatible(self, other, tolerance):
        if self.kind != other.kind or self.box is None or other.box is None or not _overlaps(self.box, other.box):
            return False
        if float(np.linalg.norm(np.cross(self.direction, other.direction))) > PARALLEL_SINE:
            return False
        offset = other.origin - self.origin
        if self.kind == 'plane':
            return abs(float(np.dot(offset, self.direction))) <= tolerance + 1e-9
        axis_gap = float(np.linalg.norm(np.cross(offset, self.direction)))
        return axis_gap <= tolerance + 1e-9 and abs(self.radius - other.radius) <= tolerance + 1e-9

    def samples(self, count=7):
        """World points (with their UV) on a count x count UV grid inside the trimmed face."""
        u0, u1, v0, v1 = self.uv
        points = []
        for i in range(count):
            for j in range(count):
                u = u0 + (u1 - u0) * (i + 0.5) / count
                v = v0 + (v1 - v0) * (j + 0.5) / count
                if BRepClass_FaceClassifier(self.face, gp_Pnt2d(u, v), 1e-9).State() not in (TopAbs_IN, TopAbs_ON):
                    continue
                points.append((self.adaptor.Value(u, v), u, v))
        return points

    def normal(self, u, v):
        """Outward unit normal (face orientation applied) at surface parameters (u, v)."""
        point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
        self.adaptor.D1(u, v, point, du, dv)
        n = du.Crossed(dv)
        if n.Magnitude() < 1e-12:
            return None
        n.Normalize()
        sign = -1.0 if self.face.Orientation() == TopAbs_REVERSED else 1.0
        return np.array([n.X(), n.Y(), n.Z()]) * sign

    def covers(self, point, tolerance):
        """Whether a world point lies within tolerance of this face's surface and
        projects inside its trimmed boundary."""
        from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
        local = point.Transformed(self.inverse)
        projection = GeomAPI_ProjectPointOnSurf(local, self.surface)
        if not projection.IsDone() or projection.NbPoints() == 0 or projection.LowerDistance() > tolerance + 1e-9:
            return False, None, None
        u, v = projection.LowerDistanceParameters()
        if self.kind == 'cylinder':
            period = 2 * math.pi
            while u < self.uv[0] - 1e-9:
                u += period
            while u > self.uv[0] + period + 1e-9:
                u -= period
        state = BRepClass_FaceClassifier(self.face, gp_Pnt2d(u, v), 1e-7).State()
        return state in (TopAbs_IN, TopAbs_ON), projection.LowerDistance(), (u, v)


def _coincidence(fa, fb, tolerance):
    """Sampled overlap of two compatible faces in both directions: the share of
    each face's samples that lie on the other face, times that face's area.
    The fifth value is the sense of the outward normals on the overlap: 'same'
    (both faces look the same way: coplanar skins, the flicker case) or
    'opposite' (face-to-face contact of two seated parts, never visible)."""
    best = None
    for source, target in ((fa, fb), (fb, fa)):
        points = source.samples()
        if not points:
            continue
        hits, gaps, dots = 0, [], []
        for point, u, v in points:
            inside, gap, uv = target.covers(point, tolerance)
            if inside:
                hits += 1
                gaps.append(gap)
                ns, nt = source.normal(u, v), target.normal(*uv)
                if ns is not None and nt is not None:
                    dots.append(float(np.dot(ns, nt)))
        if hits >= 2:
            estimate = source.area * hits / len(points)
            sense = None if not dots else 'same' if sum(1 for d in dots if d > 0) * 2 > len(dots) else 'opposite'
            if best is None or estimate > best[0]:
                best = (estimate, max(gaps), hits, len(points), sense)
    return best


def _vertex_bound(faces_a, faces_b, box_b):
    """An upper bound of the distance between two face sets: from the face vertex
    of A nearest to B's box centre to the face vertex of B nearest to that one."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_VERTEX
    def points(faces):
        return [np.array([p.X(), p.Y(), p.Z()]) for face, _ in faces
                for p in (BRep_Tool.Pnt_s(TopoDS.Vertex_s(v)) for v in explore(face, TopAbs_VERTEX))]
    pa, pb = points(faces_a), points(faces_b)
    if not pa or not pb:
        return None
    centre = np.array([(box_b[i] + box_b[i + 3]) / 2 for i in range(3)])
    p = min(pa, key=lambda x: float(np.linalg.norm(x - centre)))
    return min(float(np.linalg.norm(p - q)) for q in pb)


def _boundary_distance(faces_a, faces_b, box_a, box_b, tolerance):
    """Exact minimum distance between the faces of two parts, computed on as few
    faces as possible. The closest points of A and B lie within the true
    distance d of the other's box; so with each part's faces near the other's box
    grown by `pad`, every closest pair is kept whenever d <= pad, and the result
    r (always >= d) then equals d. First pad = tolerance (a contact is decided
    there); otherwise pad = an upper bound of d, which makes the second query
    exact by the same argument."""
    from geometry import compound

    def query(pad):
        grown_b = [box_b[i] - pad for i in range(3)] + [box_b[i + 3] + pad for i in range(3)]
        grown_a = [box_a[i] - pad for i in range(3)] + [box_a[i + 3] + pad for i in range(3)]
        near_a = [f for f, b in faces_a if _overlaps(b, grown_b)]
        near_b = [f for f, b in faces_b if _overlaps(b, grown_a)]
        if not near_a or not near_b:
            return None
        evaluator = BRepExtrema_DistShapeShape()
        evaluator.LoadS1(near_a[0] if len(near_a) == 1 else compound(near_a))
        evaluator.LoadS2(near_b[0] if len(near_b) == 1 else compound(near_b))
        evaluator.SetMultiThread(True)
        evaluator.Perform()
        require(evaluator.IsDone(), 'Distance evaluation failed.')
        return evaluator.Value()

    pad = max(tolerance, 1e-6)
    first = query(pad)
    if first is not None and first <= pad:
        return first
    bound = first if first is not None else _vertex_bound(faces_a, faces_b, box_b)
    if bound is None:
        return None
    exact = query(bound * (1 + 1e-9) + 1e-9)
    require(exact is not None, 'Distance evaluation failed.')
    return exact


def _contains(solid_shape, other):
    """Whether a vertex of `other` lies inside a solid of `solid_shape`: with no
    boundary contact, one part then sits entirely inside the other."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_VERTEX
    vertex = next(explore(other, TopAbs_VERTEX), None)
    if vertex is None:
        return False
    point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertex))
    for solid in explore(solid_shape, TopAbs_SOLID):
        if BRepClass3d_SolidClassifier(solid, point, 1e-7).State() == TopAbs_IN:
            return True
    return False


def contact_graph(nodes, shapes, face_records, facts, pairs, tolerance, deadline):
    """Contacts between parts in one job.

    nodes: [{id, features:[...]}] - a part is the compound of its native features.
    pairs: [[a, b], ...] names node pairs to measure; None measures all node pairs
    whose axis-aligned boxes come within `tolerance` of each other (a box gap is a
    lower bound of the true distance, so a pruned pair is never in contact).
    distance_mm is the exact minimum distance between the parts' boundaries
    (OCCT BRepExtrema_DistShapeShape on the faces near each other, see
    _boundary_distance), or 0 when one part lies inside the other's solid
    (inner_solution). contact means distance <= tolerance. For touching pairs,
    planar or cylindrical faces of the two parts that lie within tolerance of each
    other with a sampled overlapping area are reported as coincident (the classic
    z-fighting and seat contacts); that area is a sample estimate, never a
    certificate. Pairs left when the time budget runs out are listed as unchecked
    and their parts are never called floating.
    """
    import time
    require(0 < tolerance <= 10, 'Contact tolerance: 0 to 10 mm.', 'INVALID_SCHEMA')
    from topology import face_list
    from geometry import compound
    ids = [n['id'] for n in nodes]
    require(len(set(ids)) == len(ids) and nodes, 'The contact graph needs unique nodes.', 'INVALID_SCHEMA')
    members = {n['id']: list(n['features']) for n in nodes}
    shape, box = {}, {}
    for node in ids:
        parts = [shapes[fid] for fid in members[node]]
        shape[node] = parts[0] if len(parts) == 1 else compound(parts)
        declared = [facts[fid]['bounds'] for fid in members[node] if (facts.get(fid) or {}).get('bounds')]
        box[node] = _union_box(declared) if len(declared) == len(parts) else _fast_box(shape[node], 0.0)
    mode = 'pairs' if pairs else 'all_pairs'
    candidates, pruned = [], 0
    if pairs:
        for a, b in pairs:
            require(a in shape and b in shape and a != b, 'A pair names an unknown node or the same node twice.', 'INVALID_SCHEMA')
            candidates.append((a, b))
        candidates = list(dict.fromkeys(candidates))
    else:
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if box[a] and box[b] and _box_gap(box[a], box[b]) > tolerance:
                    pruned += 1
                else:
                    candidates.append((a, b))
    all_faces, typed_faces = {}, {}

    def faces_of(node):
        """Every face of a node with its fast box, and its face records."""
        if node not in all_faces:
            items = []
            for fid in members[node]:
                records = (face_records.get(fid) or {}).get('faces') or []
                for index, face in enumerate(face_list(shapes[fid])):
                    items.append((fid, index, face, _fast_box(face, 0.0), records[index] if index < len(records) else {}))
            all_faces[node] = [item for item in items if item[3] is not None]
        return all_faces[node]

    def typed(node, other_box):
        """The node's planar and cylindrical faces near the other node's box."""
        if node not in typed_faces:
            items = []
            for fid, index, face, _, record in faces_of(node):
                item = _Face(node, fid, index, face, record.get('face_id'), record.get('area'), tolerance)
                if item.kind is not None and item.box is not None:
                    items.append(item)
            typed_faces[node] = items
        grown = [other_box[i] - tolerance for i in range(3)] + [other_box[i + 3] + tolerance for i in range(3)]
        return [f for f in typed_faces[node] if _overlaps(f.box, grown)]

    results, coincident, unchecked, face_checks, face_checks_skipped = [], [], [], 0, 0
    for a, b in candidates:
        if time.monotonic() > deadline:
            unchecked.append([a, b])
            continue
        faces_a = [(item[2], item[3]) for item in faces_of(a)]
        faces_b = [(item[2], item[3]) for item in faces_of(b)]
        box_a = box[a] or _fast_box(shape[a], 0.0)
        box_b = box[b] or _fast_box(shape[b], 0.0)
        distance = _boundary_distance(faces_a, faces_b, box_a, box_b, tolerance) if faces_a and faces_b else None
        inner = False
        if distance is None or distance > tolerance:
            if _contains(shape[a], shape[b]) or _contains(shape[b], shape[a]):
                distance, inner = 0.0, True
        if distance is None:
            # Neither part has a face (wire or point features): the full kernel query.
            evaluator = BRepExtrema_DistShapeShape(shape[a], shape[b])
            evaluator.Perform()
            require(evaluator.IsDone(), 'Distance evaluation failed.')
            distance, inner = evaluator.Value(), bool(evaluator.InnerSolution())
        entry = {'a': a, 'b': b, 'distance_mm': distance, 'contact': distance <= tolerance,
                 'inner_solution': inner, 'coincident_faces': 0}
        if entry['contact'] and not inner:
            for fa in typed(a, box_b):
                for fb in typed(b, box_a):
                    if face_checks >= MAX_FACE_PAIR_CHECKS or time.monotonic() > deadline:
                        face_checks_skipped += 1
                        continue
                    face_checks += 1
                    if not fa.compatible(fb, tolerance):
                        continue
                    found = _coincidence(fa, fb, tolerance)
                    if found is None:
                        continue
                    entry['coincident_faces'] += 1
                    coincident.append({'a': a, 'b': b, 'feature_a': fa.feature, 'face_a': fa.face_id, 'feature_b': fb.feature, 'face_b': fb.face_id,
                                       'surface': fa.kind, 'gap_mm': found[1], 'overlap_area_mm2_estimate': found[0],
                                       'samples_on_other_face': [found[2], found[3]], 'sense': found[4], 'guarantee': 'sampled'})
        results.append(entry)
    touching = {n: False for n in ids}
    for entry in results:
        if entry['contact']:
            touching[entry['a']] = touching[entry['b']] = True
    undetermined = sorted({x for pair in unchecked for x in pair if not touching[x]})
    floating = [n for n in ids if not touching[n] and n not in undetermined] if len(ids) > 1 else []
    if mode == 'pairs':
        listed = {x for pair in candidates for x in pair}
        floating = [n for n in floating if n in listed]
    results.sort(key=lambda e: (not e['contact'], e['distance_mm'], e['a'], e['b']))
    coincident.sort(key=lambda c: (-c['overlap_area_mm2_estimate'], c['a'], c['b']))
    # The job answer has a fixed size budget: contacts and the largest coincident
    # areas are listed first, and whatever does not fit is counted, not dropped
    # silently. checked_pairs, floating_parts and the counts always cover everything.
    listed_pairs, listed_coincident, listed_unchecked = results[:MAX_LISTED_PAIRS], coincident[:MAX_LISTED_COINCIDENT], unchecked[:MAX_LISTED_PAIRS]
    import json as _json
    def size():
        return len(_json.dumps([listed_pairs, listed_coincident, listed_unchecked, floating, undetermined]))
    while size() > RESPONSE_BUDGET_BYTES and (len(listed_coincident) > 8 or len(listed_pairs) > 16 or len(listed_unchecked) > 16):
        if len(listed_coincident) > 8:
            listed_coincident = listed_coincident[:max(8, len(listed_coincident) * 3 // 4)]
        elif len(listed_unchecked) > 16:
            listed_unchecked = listed_unchecked[:max(16, len(listed_unchecked) * 3 // 4)]
        else:
            listed_pairs = listed_pairs[:max(16, len(listed_pairs) * 3 // 4)]
    return {
        'mode': mode, 'tolerance_mm': tolerance, 'nodes': len(ids),
        'pairs': listed_pairs, 'floating_parts': floating, 'undetermined_parts': undetermined,
        'coincident': listed_coincident,
        'checked_pairs': len(results), 'pruned_by_aabb': pruned, 'unchecked_pairs': listed_unchecked,
        'truncated': {'pairs': len(results) - len(listed_pairs), 'coincident': len(coincident) - len(listed_coincident),
                      'unchecked': len(unchecked) - len(listed_unchecked), 'face_pair_checks_skipped': face_checks_skipped,
                      'contacts_total': sum(1 for e in results if e['contact'])},
        'method': 'aabb_prefilter; exact boundary distance by OCCT BRepExtrema_DistShapeShape on nearby faces; '
                  'containment by solid classification; coincident faces by plane/cylinder compatibility and sampled two-way projection',
        'coverage': ('listed_pairs' if mode == 'pairs' else 'all_node_pairs_with_box_gap_within_tolerance') +
                    ('; floating_parts_relative_to_listed_pairs' if mode == 'pairs' else '') +
                    ('; unchecked_pairs_after_time_budget' if unchecked else ''),
        'guarantee': {'distance_mm': 'exact_for_declared_domain', 'contact': 'exact_for_declared_domain', 'coincident': 'sampled'},
    }


def motion_clearance(a, b, translation, steps, minimum=0.0):
    """Clearance sampled along a linear motion of b; no continuous sweep certificate."""
    require(1 <= steps <= 64, 'Motion steps: 1 to 64.', 'BUDGET_EXCEEDED')
    samples = []
    for i in range(steps + 1):
        t = i / steps
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(*[t * float(x) for x in translation]))
        moved = BRepBuilderAPI_Transform(b, trsf, True).Shape()
        evaluator = BRepExtrema_DistShapeShape(a, moved)
        evaluator.Perform()
        require(evaluator.IsDone(), 'Clearance analysis failed.')
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
