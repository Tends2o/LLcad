"""Symmetry axes for generated single-part views. All geometry quantities in model mm."""
from collections import defaultdict

import numpy as np
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.gp import gp_Pnt

import analyze as an
import checks
import projection as prj


POS_TOL = 0.02        # mm: centroids of mirrored and original faces count as equal (centre error of the bounding box up to 1e-3 mm)
SIZE_TOL = 0.05       # mm: envelope sizes of the faces
AREA_TOL = 2e-3       # relative: face areas
DIR_TOL = 1e-3        # components of the face normal (plane) or axis direction (cylinder, cone, torus)
SURFACE_TOL = 0.01    # mm: a mirrored support point lies at most this far beside the partner face
SAMPLES = 2000        # support points on the surface in total, distributed evenly over the faces (at least one per face)
MAX_PER_FACE = 200
from rules import MIN_AXIS_MM     # mm on paper: shorter symmetry lines (stubs on a single edge) are not generated and never count as shown


def _surface_rows(shape, mid):
    """Per face: kind, centroid relative to mid, area, envelope sizes, direction and whether the direction is a normal (plane, outward) or an unsigned axis
    (cylinder, cone, torus; otherwise 0). The normal belongs in the signature: two slanted faces inclined in the same sense are no mirror pair."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Torus
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Vec
    props = GProp_GProps()
    faces = list(an.faces(shape))
    kinds, centres, areas, sizes, dirs, signed = [], [], [], [], [], []
    for face in faces:
        BRepGProp.SurfaceProperties_s(face, props)
        centres.append(an.v3(props.CentreOfMass()) - mid)
        areas.append(props.Mass())
        surf = BRepAdaptor_Surface(face)
        kind = surf.GetType()
        kinds.append(int(kind))
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(face, box, False, False)
        sizes.append(an.v3(box.CornerMax()) - an.v3(box.CornerMin()))
        d, is_normal = np.zeros(3), False
        if kind == GeomAbs_Plane:
            u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
            p, n = gp_Pnt(), gp_Vec()
            BRepGProp_Face(face).Normal((u0 + u1) / 2, (v0 + v1) / 2, p, n)       # outward normal including face orientation (not just the axis of the plane)
            d, is_normal = an.v3(n) / max(float(np.linalg.norm(an.v3(n))), 1e-12), True
        elif kind == GeomAbs_Cylinder:
            d = an.v3(surf.Cylinder().Axis().Direction())
        elif kind == GeomAbs_Cone:
            d = an.v3(surf.Cone().Axis().Direction())
        elif kind == GeomAbs_Torus:
            d = an.v3(surf.Torus().Axis().Direction())
        dirs.append(d)
        signed.append(is_normal)
    return faces, np.asarray(kinds), np.asarray(centres), np.asarray(areas), np.asarray(sizes), np.asarray(dirs), np.asarray(signed, bool)


def _canonical(vectors):
    """Unsigned directions: first significant component positive."""
    out = np.array(vectors, float)
    for row in out:
        nz = np.flatnonzero(np.abs(row) > 1e-6)
        if len(nz) and row[nz[0]] < 0:
            row *= -1
    return out


def _features(kinds, centres, areas, sizes, dirs, signed):
    """Feature vector per face, scaled in tolerances: two faces match if all components deviate by at most 1 (maximum norm)."""
    d = np.where(signed[:, None], dirs, _canonical(dirs))
    return np.column_stack((np.asarray(kinds) * 1e6, centres / POS_TOL, np.log(np.maximum(areas, 1e-9)) / AREA_TOL, sizes / SIZE_TOL, d / DIR_TOL))


def _pairing(rows, k):
    """Pairing of the faces mirrored at plane k with the original faces by tolerance (no rounding): match[i] = index of the original face that fits the mirrored face i;
    None if a mirrored face has no free partner left."""
    from scipy.spatial import cKDTree
    _, kinds, centres, areas, sizes, dirs, signed = rows
    original = _features(kinds, centres, areas, sizes, dirs, signed)
    mc, md = centres.copy(), dirs.copy()
    mc[:, k] *= -1
    md[:, k] *= -1
    mirrored = _features(kinds, mc, areas, sizes, md, signed)
    tree = cKDTree(original)
    taken = np.zeros(len(kinds), bool)
    match = np.full(len(kinds), -1)
    for i in np.argsort(-np.asarray(areas), kind='stable'):
        best, cost = -1, np.inf
        for j in tree.query_ball_point(mirrored[i], r=1.0, p=np.inf):
            if not taken[j]:
                c = float(np.sum((original[j] - mirrored[i]) ** 2))
                if c < cost:
                    best, cost = j, c
        if best < 0:
            return None
        taken[best], match[i] = True, best
    return match


def mirror_planes(shape, mid):
    """Prefilter: world axes k whose central plane (normal ek through mid) could mirror the whole part. Centroid of the volume in the plane, and every mirrored face
    has an original face of its own of equal kind, centroid, area, envelope sizes and (mirrored) normal or axis direction, paired by tolerance.
    Without Boolean operation and without face limit. An equal signature with a different shape is possible; body_planes therefore checks afterwards."""
    mid = np.asarray(mid, float)
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    candidates = np.flatnonzero(np.abs(an.v3(props.CentreOfMass()) - mid) < 1e-3)
    if not len(candidates):
        return []
    rows = _surface_rows(shape, mid)
    if not len(rows[0]):
        return []
    return [int(k) for k in candidates if _pairing(rows, int(k)) is not None]


def _verified(shape, mid, planes):
    """Check by support points on the surface: evenly distributed points in the face interior per face (deterministically seeded, at least SAMPLES in total, at least one per
    face; if no sample hits the face interior, the parameter midpoint of the face applies, and if it does not lie in it either, the plane is rejected), mirrored at the plane and checked against the paired face: distance to its face <= SURFACE_TOL, position inside (not outside) and mirrored outward normal equal
    to the normal there. Thus the mirrored surface lies on the surface (within the sample density); hole against pin, offset and other shapes of equal signature
    fail. Returns: the axes of planes that passed."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.BRepTools import BRepTools
    from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.TopAbs import TopAbs_IN, TopAbs_OUT
    from OCP.gp import gp_Pnt2d, gp_Vec
    mid = np.asarray(mid, float)
    rows = _surface_rows(shape, mid)
    faces = rows[0]
    per_face = int(min(MAX_PER_FACE, max(1, -(-SAMPLES // max(1, len(faces))))))
    tools = {}

    def tool(i):
        if i not in tools:
            face = faces[i]
            tools[i] = (BRepGProp_Face(face), ShapeAnalysis_Surface(BRep_Tool.Surface_s(face)), BRepTopAdaptor_FClass2d(face, 1e-7), BRepTools.UVBounds_s(face))
        return tools[i]

    def normal_at(i, u, v):
        gf, sas, _, _ = tool(i)
        p, n = sas.Value(u, v), gp_Vec()
        gf.Normal(u, v, p, n)
        nn = an.v3(n)
        length = float(np.linalg.norm(nn))
        return an.v3(p), (nn / length if length > 0 else nn)

    def check(k, match, i, u, v):
        p, n = normal_at(i, u, v)
        q, nq = p - mid, n.copy()
        q[k] *= -1
        nq[k] *= -1
        q += mid
        j = int(match[i])
        _, sas_j, fc_j, _ = tool(j)
        uv = sas_j.ValueOfUV(gp_Pnt(*q), SURFACE_TOL)
        if np.linalg.norm(an.v3(sas_j.Value(uv)) - q) > SURFACE_TOL or fc_j.Perform(uv, True) == TopAbs_OUT:
            return False
        return float(normal_at(j, uv.X(), uv.Y())[1] @ nq) >= 0.99

    def holds(k, match):
        for i in range(len(faces)):
            _, _, fc_i, (u0, u1, v0, v1) = tool(i)
            taken = 0
            for u, v in np.random.default_rng(1000 + i).random((per_face * 12, 2)) * [u1 - u0, v1 - v0] + [u0, v0]:
                if taken >= per_face:
                    break
                if fc_i.Perform(gp_Pnt2d(u, v), True) != TopAbs_IN:
                    continue
                taken += 1
                if not check(k, match, i, u, v):
                    return False
            if taken == 0:                              # sliver without a sample inside: parameter midpoint; if it does not lie on the face, the plane is rejected conservatively
                um, vm = (u0 + u1) / 2, (v0 + v1) / 2
                if fc_i.Perform(gp_Pnt2d(um, vm), True) != TopAbs_IN or not check(k, match, i, um, vm):
                    return False
        return True
    ok = []
    for k in planes:
        match = _pairing(rows, int(k))
        if match is not None and holds(int(k), match):
            ok.append(int(k))
    return ok


def body_planes(shape, mid):
    """Central planes (world axes k, normal ek through mid) at which the whole part mirrors itself. Without face limit, also for large parts: prefilter
    mirror_planes (signature with normal, paired by tolerance), then _verified (support points on the surface). Chosen over the Boolean intersection (16 s for a large part) and
    point classification in the volume (6 to 8 ms per point, 2000 points about 15 s)."""
    mid = np.asarray(mid, float)
    pre = mirror_planes(shape, mid)
    return _verified(shape, mid, pre) if pre else []


def hole_rows(features, mid):
    """Equal coaxially aligned holes/shafts with mirrored centres on the same row."""
    rows = defaultdict(list)
    for f in features:
        if f['kind'] not in ('hole', 'shaft') or f.get('start') is None:
            continue
        c = (np.asarray(f['start']) + f['end']) / 2
        for k in range(3):
            if abs(f['axis'][k]) < 1e-6:
                key = (k, checks.equal_key(f), tuple(np.round(np.delete(c, k), 3)))
                rows[key].append((c, f))
    result = defaultdict(list)
    for (k, _, _), members in rows.items():
        if checks.mirrored_positions([f for _, f in members], k, mid[k]):
            result[k].append(members)
    return result


def _uncovered(lo, hi, spans):
    for a, b in sorted(spans):
        if b <= lo or a >= hi:
            continue
        if lo < a:
            yield lo, a
        lo = max(lo, b)
    if hi > lo:
        yield lo, hi


def part_symmetry(ctx, pid, dr=None):
    """(Centre of the bounding box, {world axis: coordinate} of the whole-part planes, hole rows) of part pid, once per part in the cache ctx['symmetry']: the dimensioning (plan_dims.dimension) and the symmetry lines
    (add_axes, N2.2) read the same answer. Planes from dr.planes_of (measurement and generation share them), without a Drawing from body_planes on ctx['shapes'] (the same function)."""
    cache = ctx.setdefault('symmetry', {})
    if pid not in cache:
        bb = ctx['P'][pid]['part']['bbox']
        mid = (np.asarray(bb['min']) + bb['max']) / 2
        planes = dr.planes_of(pid, mid) if dr is not None else {int(k): float(mid[k]) for k in body_planes(ctx['shapes'][pid], mid)}
        cache[pid] = mid, planes, hole_rows(ctx['P'][pid]['feats'], mid)
    return cache[pid]


def add_axes(spec, ctx, dr):
    """Planner step N2.2: symmetry line (center, symmetry_axis) in every view of a single part in which a symmetry plane of the whole part appears as a line (normal of the
    plane perpendicular to the viewing direction): normal, section, rotated and oblique views, also with centerlines: false; detail views only if their circle contains the plane (chord, limited to the clipped content plus overhang). Atomic: appending only at the end.
    Assembly views (more than one part) get no axis. A section keeps the symmetry because its plane is perpendicular to the mirror plane. Idempotent: drawn
    or already existing lines in the plane (from the core or symmetry_axis) are not drawn twice. Planes: dr.planes_of (the same answer as the measurement)."""
    new = []                                            # atomic: collect first, append at the end; an error leaves the spec unchanged
    for sd in spec['sheets']:
        for v in sd['views']:
            ids = v.get('parts') or []
            if len(ids) != 1 or ids[0] not in ctx['P']:
                continue
            pid = ids[0]
            N, X, Y = frame = prj.frame_of(v)
            mid, full, rows = part_symmetry(ctx, pid, dr)
            content = dr.view_content(v, sd['scale'])
            if not content['vis']:
                continue
            origin = prj.project([mid], frame)[0]
            detail = v.get('detail')
            openings = [a for a in sd['annotations'] if a.get('view') == v['id'] and a.get('opening_axis')]
            existing = [(prj.project(a['points'], frame), 'model') for a in sd['annotations'] if
                        a.get('view') == v['id'] and a.get('type') == 'polyline' and (a.get('symmetry_axis') or a.get('style') == 'center') and a.get('points')]
            ks = []                                     # order as before: image axes X, Y; then further planes perpendicular to the viewing direction (rotated, oblique)
            for u in (X, Y):
                k = int(np.argmax(np.abs(u)))
                if abs(u[k]) > 1 - 1e-6 and k not in ks:
                    ks.append(k)
            ks += [k for k in range(3) if k not in ks and abs(N[k]) < 1e-6]
            allpts = np.vstack(content['vis'] + content['hid']) - origin
            for k in ks:
                e = np.eye(3)[k]
                if abs(N @ e) > 1e-6:
                    continue
                d = np.cross(N, e)
                dimg = np.array([d @ X, d @ Y])
                if dimg[np.argmax(np.abs(dimg))] < 0:
                    d, dimg = -d, -dimg
                nimg = np.array([-dimg[1], dimg[0]])
                over = 2.0 / content['k']
                spans = []
                if detail:
                    c2 = prj.project([detail['center']], frame)[0] - origin
                    off = abs(float(c2 @ nimg))
                    r = float(detail['radius'])
                    if k in full and off < r:
                        half = float(np.sqrt(r * r - off * off))
                        t = allpts @ dimg                    # clipped content of the detail view along the axis; overhang only within the circle (chord)
                        lo, hi = max(float(t.min()) - over, float(c2 @ dimg) - half), min(float(t.max()) + over, float(c2 @ dimg) + half)
                        if hi > lo:
                            spans.append((lo, hi))
                elif k in full:
                    t = allpts @ dimg
                    spans.append((float(t.min()) - over, float(t.max()) + over))
                else:
                    stations = [(prj.project([a['p1'], a['p2']], frame).mean(axis=0) - origin) @ dimg for a in openings if a['opening_axis'] == 'xyz'[k]]
                    if stations:
                        spans.append((min(stations) - over, max(stations) + over))
                    for members in rows.get(k, ()):
                        centers = []
                        for c, f in members:
                            if abs(np.dot(f['axis'], N)) < 1 - 1e-6:
                                break
                            q = prj.project([c], frame)[0]
                            r = f['diameter'] / 2
                            t = np.arange(16) * (2*np.pi/16)
                            if dr.visible_points(v, sd['scale'], q + r*np.column_stack((np.cos(t), np.sin(t)))).mean() < 0.6:
                                break
                            centers.append(float((q - origin) @ dimg))
                        else:
                            spans.append((min(centers)-r-over, max(centers)+r+over))
                drawn = []
                for p, kind in [*content['centers'], *existing]:
                    if kind == 'model' and np.max(np.abs((p - origin) @ nimg)) < 1e-4:
                        t = (p - origin) @ dimg
                        drawn.append((float(t.min()), float(t.max())))
                for lo, hi in sorted(spans):
                    for a, b in _uncovered(lo, hi, drawn):
                        if (b-a) * content['k'] < MIN_AXIS_MM:
                            continue
                        points = [mid + t * d for t in (a, b)]
                        new.append((sd, {'type': 'polyline', 'style': 'center', 'view': v['id'],
                                         'points': np.round(points, 6).tolist(), 'symmetry_axis': 'xyz'[k]}))
                    drawn.append((lo, hi))
    for sd, a in new:
        sd['annotations'].append(a)
