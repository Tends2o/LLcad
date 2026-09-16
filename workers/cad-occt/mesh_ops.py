"""Registered operators on authoritative indexed meshes: explicit repair, region remeshing,
locally supported as-rigid-as-possible deformation and primitive/symmetry hypotheses.

Every operation reports exactly what it changed. Nothing is welded, deleted or
reoriented without an explicit option and a loss report; results stay previews
until the registered watertight checks accept them.
"""
import math
from collections import defaultdict
import numpy as np
from geometry import require

MAX_TRIANGLES = 100000


def _arrays(mesh):
    v = np.asarray(mesh['vertices'], float)
    t = np.asarray(mesh['triangles'], int)
    require(v.ndim == 2 and v.shape[1] == 3 and t.ndim == 2 and t.shape[1] == 3, 'Ungültiges Netz.', 'INVALID_SCHEMA')
    return v, t


def _areas(v, t):
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    return 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)


def _edges(t):
    edges = defaultdict(list)
    for i, tri in enumerate(t):
        for j in range(3):
            a, b = int(tri[j]), int(tri[(j + 1) % 3])
            edges[(min(a, b), max(a, b))].append((i, 1 if a < b else -1))
    return edges


def _boundary_loops(t):
    edges = _edges(t)
    boundary = [e for e, uses in edges.items() if len(uses) == 1]
    nxt = defaultdict(list)
    for a, b in boundary:
        nxt[a].append(b)
        nxt[b].append(a)
    seen = set()
    loops = []
    for start in nxt:
        if start in seen:
            continue
        loop = [start]
        seen.add(start)
        current = start
        previous = None
        ok = True
        for _ in range(len(boundary) + 1):
            candidates = [x for x in nxt[current] if x != previous and x not in seen]
            if not candidates:
                ok = any(x == start for x in nxt[current]) and len(loop) >= 3
                break
            previous, current = current, candidates[0]
            loop.append(current)
            seen.add(current)
        if ok:
            loops.append(loop)
    return loops


def repair(mesh, options):
    """Explicit repair pipeline; each step is optional and counted."""
    v, t = _arrays(mesh)
    report = {'method': 'explicit_indexed_mesh_repair', 'steps': [], 'welded_vertices': 0, 'removed_degenerate_triangles': 0,
              'removed_duplicate_triangles': 0, 'flipped_triangles': 0, 'nonorientable_components': 0,
              'filled_holes': 0, 'filled_hole_area_mm2': 0.0, 'skipped_holes': 0, 'added_vertices': 0}
    weld = options.get('weld_tolerance')
    if weld:
        weld = float(weld)
        require(0 < weld <= 1.0, 'Schweißtoleranz außerhalb des Vertrags.', 'PRECISION_UNSUPPORTED')
        # Grid quantization: points in the same cell of size weld are merged. Points closer than
        # weld across a cell border are not merged; this stays a declared, bounded tolerance.
        keys = np.floor(v / weld).astype(np.int64)
        index = {}
        remap = np.zeros(len(v), int)
        kept = []
        for i, key in enumerate(map(tuple, keys)):
            if key not in index:
                index[key] = len(kept)
                kept.append(v[i])
            remap[i] = index[key]
        report['welded_vertices'] = int(len(v) - len(kept))
        v = np.array(kept)
        t = remap[t]
        report['steps'].append({'step': 'weld', 'tolerance_mm': weld, 'method': 'grid_quantization'})
    if options.get('remove_degenerate', True):
        keep = np.array([len({int(a), int(b), int(c)}) == 3 for a, b, c in t]) if len(t) else np.zeros(0, bool)
        if len(t):
            keep &= _areas(v, t) > 1e-18
        report['removed_degenerate_triangles'] = int((~keep).sum()) if len(t) else 0
        t = t[keep]
        report['steps'].append({'step': 'remove_degenerate'})
    if options.get('remove_duplicates', True):
        seen = set()
        keep = []
        for tri in t:
            key = tuple(sorted(int(x) for x in tri))
            keep.append(key not in seen)
            seen.add(key)
        keep = np.array(keep, bool) if len(t) else np.zeros(0, bool)
        report['removed_duplicate_triangles'] = int((~keep).sum()) if len(t) else 0
        t = t[keep]
        report['steps'].append({'step': 'remove_duplicates'})
    if options.get('orient', True) and len(t):
        t, flipped, nonorientable = orient_consistently(v, t)
        report['flipped_triangles'] = flipped
        report['nonorientable_components'] = nonorientable
        report['steps'].append({'step': 'orient', 'method': 'component_traversal_then_outward_signed_volume'})
    max_edges = int(options.get('fill_holes_max_edges', 0) or 0)
    if max_edges:
        require(max_edges <= 256, 'Lochgröße für die Füllung überschreitet das Budget.', 'BUDGET_EXCEEDED')
        loops = _boundary_loops(t)
        added = []
        for loop in loops:
            if len(loop) > max_edges:
                report['skipped_holes'] += 1
                continue
            centre = v[loop].mean(axis=0)
            ci = len(v) + len(added)
            added.append(centre)
            new = []
            for i in range(len(loop)):
                a, b = loop[i], loop[(i + 1) % len(loop)]
                new.append([b, a, ci])
            new = np.array(new, int)
            # Orient the fan like the neighbouring boundary triangle.
            edges = _edges(t)
            a, b = loop[0], loop[1]
            uses = edges.get((min(a, b), max(a, b)), [])
            if uses:
                tri = t[uses[0][0]]
                forward = any(int(tri[j]) == a and int(tri[(j + 1) % 3]) == b for j in range(3))
                # The neighbour traverses a->b, so the fan must traverse b->a (the default); otherwise flip.
                if not forward:
                    new = new[:, [1, 0, 2]]
            v_all = np.vstack([v, np.array(added)])
            report['filled_hole_area_mm2'] += float(_areas(v_all, new).sum())
            t = np.vstack([t, new])
            report['filled_holes'] += 1
        if added:
            v = np.vstack([v, np.array(added)])
        report['added_vertices'] = len(added)
        report['steps'].append({'step': 'fill_holes', 'max_edges': max_edges, 'method': 'centroid_fan'})
    require(0 < len(t) <= MAX_TRIANGLES, 'Reparaturergebnis ist leer oder überschreitet das Budget.', 'BUDGET_EXCEEDED')
    report['lost_semantics'] = ['original_vertex_indices'] if report['welded_vertices'] or report['added_vertices'] else []
    report['topology_changes'] = [k for k in ('welded_vertices', 'removed_degenerate_triangles', 'removed_duplicate_triangles', 'flipped_triangles', 'filled_holes') if report[k]]
    return {'vertices': v.tolist(), 'triangles': t.tolist()}, report


def orient_consistently(v, t):
    edges = _edges(t)
    adjacency = defaultdict(list)
    for (a, b), uses in edges.items():
        if len(uses) == 2:
            (i, si), (j, sj) = uses
            adjacency[i].append((j, si, sj))
            adjacency[j].append((i, sj, si))
    t = t.copy()
    visited = np.zeros(len(t), bool)
    flipped = 0
    nonorientable = 0
    component_of = np.full(len(t), -1)
    components = 0
    for seed in range(len(t)):
        if visited[seed]:
            continue
        stack = [seed]
        visited[seed] = True
        component_of[seed] = components
        members = [seed]
        consistent = True
        while stack:
            i = stack.pop()
            for j, si, sj in adjacency[i]:
                # After flipping, recompute the shared edge orientation from current index order.
                shared = None
                for a_ in range(3):
                    for b_ in range(3):
                        if t[i][a_] == t[j][b_] and t[i][(a_ + 1) % 3] == t[j][(b_ + 2) % 3]:
                            shared = 'consistent'
                        if t[i][a_] == t[j][b_] and t[i][(a_ + 1) % 3] == t[j][(b_ + 1) % 3]:
                            shared = 'inconsistent'
                if shared is None:
                    continue
                if not visited[j]:
                    if shared == 'inconsistent':
                        t[j] = t[j][[0, 2, 1]]
                        flipped += 1
                    visited[j] = True
                    component_of[j] = components
                    members.append(j)
                    stack.append(j)
                elif shared == 'inconsistent':
                    consistent = False
        if not consistent:
            nonorientable += 1
        else:
            member_t = t[members]
            closed = all(len(edges[(min(int(a), int(b)), max(int(a), int(b)))]) == 2 for tri in member_t for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])))
            if closed:
                a, b, c = v[member_t[:, 0]], v[member_t[:, 1]], v[member_t[:, 2]]
                volume = float(np.einsum('ij,ij->i', a, np.cross(b, c)).sum() / 6)
                if volume < 0:
                    t[members] = t[members][:, [0, 2, 1]]
                    flipped += len(members)
        components += 1
    return t, int(flipped), int(nonorientable)


def remesh_region(mesh, center, radius, target_edge, iterations):
    """Isotropic remeshing (split long, collapse short, flip for valence, tangential relaxation)
    restricted to vertices inside a sphere. Vertices outside the region never move or vanish."""
    v, t = _arrays(mesh)
    center = np.asarray(center, float)
    require(radius > 0 and target_edge > 0 and 1 <= iterations <= 20, 'Ungültige Remeshing-Parameter.', 'INVALID_SCHEMA')
    inside = lambda p: float(np.linalg.norm(p - center)) < radius
    counts = {'splits': 0, 'collapses': 0, 'flips': 0, 'relaxations': 0}
    long_edge, short_edge = 4 / 3 * target_edge, 4 / 5 * target_edge
    v = [np.array(p) for p in v]
    tris = {i: [int(x) for x in tri] for i, tri in enumerate(t)}
    vt = defaultdict(set)
    for i, tri in tris.items():
        for x in tri:
            vt[x].add(i)
    next_id = len(tris)

    def edge_tris(a, b):
        return vt[a] & vt[b]

    def remove(i):
        for x in tris[i]:
            vt[x].discard(i)
        del tris[i]

    def add(tri):
        nonlocal next_id
        tris[next_id] = tri
        for x in tri:
            vt[x].add(next_id)
        next_id += 1
        require(len(tris) <= MAX_TRIANGLES, 'Remeshing überschreitet das Dreiecksbudget.', 'BUDGET_EXCEEDED')

    def all_edges():
        seen = set()
        for tri in list(tris.values()):
            for j in range(3):
                a, b = tri[j], tri[(j + 1) % 3]
                key = (min(a, b), max(a, b))
                if key not in seen:
                    seen.add(key)
                    yield key

    def normal(tri):
        return np.cross(v[tri[1]] - v[tri[0]], v[tri[2]] - v[tri[0]])

    def crosses(a, b):
        # Long edges spanning the region (ruled surfaces have no interior vertices) must be split too.
        d = v[b] - v[a]
        length2 = float(np.dot(d, d))
        if length2 == 0:
            return inside(v[a])
        t_ = float(np.clip(np.dot(center - v[a], d) / length2, 0.0, 1.0))
        return float(np.linalg.norm(v[a] + t_ * d - center)) < radius
    for _ in range(iterations):
        for a, b in list(all_edges()):
            if not crosses(a, b) or np.linalg.norm(v[a] - v[b]) <= long_edge:
                continue
            incident = list(edge_tris(a, b))
            if not incident:
                continue
            m = len(v)
            v.append((v[a] + v[b]) / 2)
            for i in incident:
                tri = tris[i]
                k = next(j for j in range(3) if {tri[j], tri[(j + 1) % 3]} == {a, b})
                x, y, z = tri[k], tri[(k + 1) % 3], tri[(k + 2) % 3]
                remove(i)
                add([x, m, z])
                add([m, y, z])
            counts['splits'] += 1
        for a, b in list(all_edges()):
            if a not in vt or b not in vt or not (vt[a] and vt[b]):
                continue
            if len(edge_tris(a, b)) != 2 or not (inside(v[a]) and inside(v[b])):
                continue
            if np.linalg.norm(v[a] - v[b]) >= short_edge:
                continue
            ok = True
            for i in vt[b]:
                tri = tris[i]
                if a in tri:
                    continue
                new = [a if x == b else x for x in tri]
                n_new, n_old = normal(new), normal(tri)
                p_, q_, r_ = v[new[0]], v[new[1]], v[new[2]]
                lengths2 = float(np.dot(q_ - p_, q_ - p_) + np.dot(r_ - q_, r_ - q_) + np.dot(p_ - r_, p_ - r_))
                quality = 2 * math.sqrt(3) * np.linalg.norm(n_new) / lengths2 if lengths2 > 0 else 0.0
                if np.linalg.norm(n_new) < 1e-14 or np.dot(n_new, n_old) <= 0 or quality < 0.1:
                    ok = False
                    break
            if not ok:
                continue
            for i in list(vt[b]):
                tri = tris[i]
                remove(i)
                if a not in tri:
                    add([a if x == b else x for x in tri])
            counts['collapses'] += 1
        valence = {x: len(vt[x]) for x in vt}
        for a, b in list(all_edges()):
            incident = list(edge_tris(a, b))
            if len(incident) != 2 or not (inside(v[a]) and inside(v[b])):
                continue
            i, j = incident
            c = [x for x in tris[i] if x not in (a, b)][0]
            d = [x for x in tris[j] if x not in (a, b)][0]
            if c == d or d in vt and c in vt and edge_tris(c, d):
                continue
            before = sum(abs(valence.get(x, 0) - 6) for x in (a, b, c, d))
            after = abs(valence[a] - 7) + abs(valence[b] - 7) + abs(valence[c] - 5) + abs(valence[d] - 5)
            if after >= before:
                continue
            k = next(idx for idx in range(3) if {tris[i][idx], tris[i][(idx + 1) % 3]} == {a, b})
            x, y = tris[i][k], tris[i][(k + 1) % 3]
            new_i, new_j = [c, x, d], [d, y, c]
            if np.dot(normal(new_i), normal(tris[i])) <= 0 or np.dot(normal(new_j), normal(tris[j])) <= 0:
                continue
            remove(i)
            remove(j)
            add(new_i)
            add(new_j)
            valence[a] -= 1
            valence[b] -= 1
            valence[c] += 1
            valence[d] += 1
            counts['flips'] += 1
        normals = defaultdict(lambda: np.zeros(3))
        neighbours = defaultdict(set)
        for tri in tris.values():
            n = normal(tri)
            for j in range(3):
                normals[tri[j]] += n
                neighbours[tri[j]].update((tri[(j + 1) % 3], tri[(j + 2) % 3]))
        boundary = {x for a, b in all_edges() if len(edge_tris(a, b)) == 1 for x in (a, b)}
        moved = 0
        for i in list(neighbours):
            if i in boundary or not inside(v[i]):
                continue
            n = normals[i]
            if np.linalg.norm(n) < 1e-14:
                continue
            n = n / np.linalg.norm(n)
            centroid = np.mean([v[k] for k in neighbours[i]], axis=0)
            delta = centroid - v[i]
            delta = delta - np.dot(delta, n) * n
            candidate = v[i] + delta
            if not inside(candidate):
                continue
            # Reject moves that fold or squash an incident triangle; relaxation must stay a valid remeshing step.
            previous = v[i]
            v[i] = candidate
            ok = True
            for k in vt[i]:
                tri = tris[k]
                nn = normal(tri)
                a_, b_, c_ = v[tri[0]], v[tri[1]], v[tri[2]]
                lengths2 = float(np.dot(b_ - a_, b_ - a_) + np.dot(c_ - b_, c_ - b_) + np.dot(a_ - c_, a_ - c_))
                quality = 2 * math.sqrt(3) * np.linalg.norm(nn) / lengths2 if lengths2 > 0 else 0.0
                if np.dot(nn, n) <= 0 or quality < 0.1:
                    ok = False
                    break
            if ok:
                moved += 1
            else:
                v[i] = previous
        counts['relaxations'] += moved
    used = sorted({x for tri in tris.values() for x in tri})
    remap = {old: new for new, old in enumerate(used)}
    result = {'vertices': [v[i].tolist() for i in used], 'triangles': [[remap[x] for x in tri] for tri in tris.values()]}
    lengths = [float(np.linalg.norm(v[a] - v[b])) for a, b in all_edges() if inside(v[a]) and inside(v[b])]
    report = {'method': 'isotropic_remeshing_split_collapse_flip_relax', 'region': {'center': center.tolist(), 'radius': radius},
              'target_edge_mm': target_edge, 'iterations': iterations, **counts,
              'region_edge_length_mm': {'min': min(lengths) if lengths else None, 'max': max(lengths) if lengths else None, 'mean': float(np.mean(lengths)) if lengths else None},
              'outside_region': 'vertices_unchanged_triangles_only_rewired_at_region_border',
              'lost_semantics': ['original_vertex_indices_inside_region'], 'topology_changes': ['triangulation_inside_region'],
              'certified_bound_or_null': None, 'note': 'Tangential relaxation has no reference surface; shape is preserved only up to the local tangent-plane projection.'}
    return result, report


def arap_deform(mesh, center, radius, handles, iterations):
    """Locally supported as-rigid-as-possible deformation (Bauplan eq. 67) with fixed exterior.

    Handles pin the vertex nearest to each handle point at point + displacement. All vertices
    outside the region are fixed. Uniform weights, local rotations from SVD (det +1 enforced).
    """
    from scipy.sparse import lil_matrix, csr_matrix
    from scipy.sparse.linalg import spsolve
    v, t = _arrays(mesh)
    center = np.asarray(center, float)
    require(radius > 0 and 1 <= iterations <= 50 and 1 <= len(handles) <= 32, 'Ungültige Deformationsparameter.', 'INVALID_SCHEMA')
    n = len(v)
    neighbours = defaultdict(set)
    for tri in t:
        for j in range(3):
            neighbours[int(tri[j])].update((int(tri[(j + 1) % 3]), int(tri[(j + 2) % 3])))
    inside = np.linalg.norm(v - center, axis=1) < radius
    constrained = {}
    for h in handles:
        point = np.asarray(h['point'], float)
        displacement = np.asarray(h['displacement'], float)
        index = int(np.argmin(np.linalg.norm(v - point, axis=1)))
        require(inside[index], 'Handle liegt außerhalb der erlaubten Region.', 'OUT_OF_SCOPE')
        constrained[index] = v[index] + displacement
    for i in range(n):
        if not inside[i]:
            constrained[i] = v[i]
    free = [i for i in range(n) if i not in constrained]
    require(free, 'Keine freien Vertices in der Region.', 'GEOMETRY_INVALID')
    position = {i: k for k, i in enumerate(free)}
    L = lil_matrix((len(free), len(free)))
    for i in free:
        L[position[i], position[i]] = len(neighbours[i])
        for j in neighbours[i]:
            if j in position:
                L[position[i], position[j]] = -1.0
    L = csr_matrix(L)
    p = v.copy()
    for i, target in constrained.items():
        p[i] = target
    rotations = [np.eye(3) for _ in range(n)]

    def energy():
        total = 0.0
        for i in range(n):
            for j in neighbours[i]:
                d = (p[i] - p[j]) - rotations[i] @ (v[i] - v[j])
                total += float(d @ d)
        return total
    initial = energy()
    for _ in range(iterations):
        # local step: best rotation per vertex
        for i in range(n):
            if not neighbours[i]:
                continue
            P = np.array([v[i] - v[j] for j in neighbours[i]])
            Q = np.array([p[i] - p[j] for j in neighbours[i]])
            S = P.T @ Q
            u, _, vt = np.linalg.svd(S)
            R = vt.T @ u.T
            if np.linalg.det(R) < 0:
                u[:, -1] *= -1
                R = vt.T @ u.T
            rotations[i] = R
        # global step
        b = np.zeros((len(free), 3))
        for i in free:
            for j in neighbours[i]:
                b[position[i]] += 0.5 * (rotations[i] + rotations[j]) @ (v[i] - v[j])
                if j in constrained:
                    b[position[i]] += p[j]
        solution = np.column_stack([spsolve(L, b[:, k]) for k in range(3)])
        for i in free:
            p[i] = solution[position[i]]
    final = energy()
    displacement = np.linalg.norm(p - v, axis=1)
    report = {'method': 'as_rigid_as_possible_local_global_uniform_weights', 'iterations': iterations,
              'region': {'center': center.tolist(), 'radius': radius}, 'handles': len(handles),
              'fixed_vertices': int((~inside).sum()), 'free_vertices': len(free), 'moved_vertices': int((displacement > 1e-12).sum()),
              'max_displacement_mm': float(displacement.max()), 'energy_initial': initial, 'energy_final': final,
              'rotations': 'SO3_via_SVD_with_determinant_correction', 'collision_certificate': None,
              'lost_semantics': [], 'topology_changes': [], 'certified_bound_or_null': None,
              'note': 'A small energy proves neither collision freedom nor dimensional intent; the registered mesh checks run afterwards.'}
    return {'vertices': p.tolist(), 'triangles': t.tolist()}, report


def fit_primitives(mesh, angle_threshold_deg=8.0, min_triangles=6, sample_limit=20000):
    """Segment by normal continuity and fit plane/sphere/cylinder hypotheses with residuals and coverage."""
    v, t = _arrays(mesh)
    require(len(t) <= MAX_TRIANGLES, 'Netz überschreitet das Budget.', 'BUDGET_EXCEEDED')
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    normals = np.cross(b - a, c - a)
    areas = 0.5 * np.linalg.norm(normals, axis=1)
    total_area = float(areas.sum())
    with np.errstate(invalid='ignore', divide='ignore'):
        normals = normals / np.maximum(np.linalg.norm(normals, axis=1)[:, None], 1e-18)
    centroids = (a + b + c) / 3
    edges = _edges(t)
    adjacency = defaultdict(list)
    for uses in edges.values():
        if len(uses) == 2:
            adjacency[uses[0][0]].append(uses[1][0])
            adjacency[uses[1][0]].append(uses[0][0])
    threshold = math.cos(math.radians(angle_threshold_deg))
    segment = np.full(len(t), -1)
    segments = []
    for seed in range(len(t)):
        if segment[seed] >= 0 or areas[seed] <= 1e-18:
            continue
        members = [seed]
        segment[seed] = len(segments)
        stack = [seed]
        while stack:
            i = stack.pop()
            for j in adjacency[i]:
                if segment[j] < 0 and areas[j] > 1e-18 and float(normals[i] @ normals[j]) >= threshold:
                    segment[j] = len(segments)
                    members.append(j)
                    stack.append(j)
        segments.append(members)
    hypotheses = []
    for members in segments:
        if len(members) < min_triangles:
            continue
        pts = np.unique(t[members].ravel())
        P = v[pts]
        area = float(areas[members].sum())
        candidates = []
        # plane
        centre = P.mean(axis=0)
        _, s, vt = np.linalg.svd(P - centre)
        normal = vt[-1]
        residual = (P - centre) @ normal
        candidates.append({'kind': 'plane', 'normal': normal.tolist(), 'point': centre.tolist(),
                           'rms_residual_mm': float(np.sqrt(np.mean(residual ** 2))), 'max_residual_mm': float(np.abs(residual).max())})
        # sphere (algebraic least squares)
        if len(P) >= 5:
            A = np.column_stack([2 * P, np.ones(len(P))])
            rhs = (P ** 2).sum(axis=1)
            sol, *_ = np.linalg.lstsq(A, rhs, rcond=None)
            centre_s = sol[:3]
            r2 = float(sol[3] + centre_s @ centre_s)
            if r2 > 0:
                radius = math.sqrt(r2)
                residual = np.linalg.norm(P - centre_s, axis=1) - radius
                candidates.append({'kind': 'sphere', 'center': centre_s.tolist(), 'radius_mm': radius,
                                   'rms_residual_mm': float(np.sqrt(np.mean(residual ** 2))), 'max_residual_mm': float(np.abs(residual).max())})
        # cylinder: axis = direction least represented by triangle normals; radius by least squares
        N = normals[members]
        w = areas[members]
        cov = (N * w[:, None]).T @ N
        eigen, vectors = np.linalg.eigh(cov)
        axis = vectors[:, 0]
        if eigen[0] < 0.2 * eigen[-1] and len(P) >= 6:
            basis = np.linalg.svd(np.eye(3) - np.outer(axis, axis))[0][:, :2]
            Q = (P - centre) @ basis
            A = np.column_stack([2 * Q, np.ones(len(Q))])
            sol, *_ = np.linalg.lstsq(A, (Q ** 2).sum(axis=1), rcond=None)
            cc = sol[:2]
            r2 = float(sol[2] + cc @ cc)
            if r2 > 0:
                radius = math.sqrt(r2)
                residual = np.linalg.norm(Q - cc, axis=1) - radius
                axis_point = centre + basis @ cc
                candidates.append({'kind': 'cylinder', 'axis': axis.tolist(), 'point': axis_point.tolist(), 'radius_mm': radius,
                                   'rms_residual_mm': float(np.sqrt(np.mean(residual ** 2))), 'max_residual_mm': float(np.abs(residual).max())})
        best = min(candidates, key=lambda h: h['rms_residual_mm'])
        hypotheses.append({**best, 'status': 'hypothesis', 'confidence': 'not_calibrated',
                           'segment_triangles': len(members), 'segment_area_mm2': area, 'area_fraction': area / total_area if total_area else 0.0,
                           'sample_vertices': int(len(P)), 'alternatives': [{'kind': h['kind'], 'rms_residual_mm': h['rms_residual_mm']} for h in candidates if h is not best]})
    hypotheses.sort(key=lambda h: -h['area_fraction'])
    # mirror symmetry hypotheses about principal planes through the centroid
    sample = v if len(v) <= sample_limit else v[np.random.default_rng(7).choice(len(v), sample_limit, replace=False)]
    centroid = v.mean(axis=0)
    _, _, axes = np.linalg.svd(sample - centroid)
    from scipy.spatial import cKDTree
    tree = cKDTree(v)
    extent = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
    symmetries = []
    for normal in axes:
        reflected = sample - 2 * ((sample - centroid) @ normal)[:, None] * normal
        distances, _ = tree.query(reflected)
        symmetries.append({'kind': 'mirror_plane', 'normal': normal.tolist(), 'point': centroid.tolist(), 'status': 'hypothesis',
                           'confidence': 'not_calibrated', 'sampled_chamfer_rms_mm': float(np.sqrt(np.mean(distances ** 2))),
                           'sampled_max_mm': float(distances.max()), 'relative_to_extent': float(distances.max() / extent) if extent else None,
                           'samples': int(len(sample)), 'guarantee': 'sampled_vertex_to_nearest_vertex'})
    covered = sum(h['area_fraction'] for h in hypotheses)
    return {'method': 'normal_region_growing_with_least_squares_primitive_fits', 'segments': len(segments),
            'hypotheses': hypotheses[:32], 'symmetry_hypotheses': symmetries, 'coverage_area_fraction': covered,
            'angle_threshold_deg': angle_threshold_deg, 'construction_history': 'unknown_not_reconstructed',
            'hidden_geometry': 'not_inferred', 'guarantee': 'sampled'}
