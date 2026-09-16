"""Batched surface extraction: interval or Lipschitz cell exclusion, marching tetrahedra or QEF dual contouring.

Leaves live on one uniform level, so dual contouring cannot produce cracks. Every
output stays a preview: neither method certifies sub-cell topology. The QEF vertex
(Bauplan eq. 58) is restricted to its cell and its rank deficiency is reported.
"""
import math
import numpy as np
from geometry import require
from intervals import certified_empty

OFFSETS = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]])
TETS = ((0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6), (0, 7, 4, 6), (0, 4, 5, 6), (0, 5, 1, 6))
EDGES = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
MAX_LEAVES = 250000
MAX_TRIANGLES = 500000


def leaf_cells(f, sampled, pruning):
    """Breadth-first refinement to the declared cell size with conservative exclusion."""
    c = f['construction']
    node = c['expression']
    lo = np.array(c['domain']['min'], float)
    hi = np.array(c['domain']['max'], float)
    step = f['field']['cell_size']
    L = f['field']['lipschitz']
    depth = max(0, math.ceil(math.log2(max(hi - lo) / step)))
    require(depth <= 10, 'Feldtiefe überschreitet das Budget.', 'BUDGET_EXCEEDED')
    n = 2 ** depth
    spacing = (hi - lo) / n
    cells = np.zeros((1, 3), int)
    size = n
    evaluated = 0
    pruned = 0
    while True:
        cell_lo = lo + spacing * cells
        cell_hi = lo + spacing * (cells + size)
        evaluated += cells.shape[0]
        require(evaluated <= MAX_LEAVES, 'Aktive Feldzellen überschreiten das Budget.', 'BUDGET_EXCEEDED')
        if pruning == 'interval':
            empty = certified_empty(node, cell_lo, cell_hi)
        else:
            radius = float(np.linalg.norm(spacing * size / 2))
            centers = (cell_lo + cell_hi) / 2
            values = np.array([sampled(p) for p in centers])
            empty = np.abs(values) > L * radius + 1e-12
        pruned += int(empty.sum())
        cells = cells[~empty]
        if size == 1 or cells.shape[0] == 0:
            break
        size //= 2
        cells = np.concatenate([cells + offset * size for offset in OFFSETS])
    return dict(cells=cells, n=n, spacing=spacing, lo=lo, evaluated=evaluated, pruned=pruned, depth=depth)


def corner_values(leaves, sampled):
    """Sample every distinct corner once through the private sampler."""
    values = {}
    cells = leaves['cells']
    lo, spacing = leaves['lo'], leaves['spacing']
    for cell in cells:
        for offset in OFFSETS:
            key = tuple(int(x) for x in cell + offset)
            if key not in values:
                values[key] = sampled(lo + spacing * np.array(key))
    return values


def _finish(vertices, triangles, f, leaves, method, extra):
    require(triangles, 'Keine Oberfläche in der Domäne gefunden.')
    return dict(vertices=vertices, triangles=triangles, feature_id=f['id'], quality='preview_only',
                field_semantics=f['field']['semantics'], value_unit=f['field'].get('value_unit', 'length'),
                deflection=float(np.linalg.norm(leaves['spacing'])), certified_bound=None,
                active_cells=int(leaves['evaluated']), pruned_cells=int(leaves['pruned']), leaf_cells=int(leaves['cells'].shape[0]),
                method=method, pruning=extra.pop('pruning'), **extra)


def marching_tetrahedra(f, sampled, compiled, pruning):
    leaves = leaf_cells(f, sampled, pruning)
    values = corner_values(leaves, sampled)
    lo, spacing = leaves['lo'], leaves['spacing']
    vertices, triangles, vertex_map = [], [], {}

    def vertex(a, b):
        va, vb = values[a], values[b]
        key = tuple(sorted((a, b)))
        if abs(va) < 1e-14:
            key = ('point', a)
        elif abs(vb) < 1e-14:
            key = ('point', b)
        if key not in vertex_map:
            t = va / (va - vb)
            point = lo + spacing * (np.array(a) + t * (np.array(b) - np.array(a)))
            vertex_map[key] = len(vertices)
            vertices.append(point.tolist())
        return vertex_map[key]

    def triangle(indices):
        if len(set(indices)) < 3:
            return
        a, b, d = (np.asarray(vertices[i]) for i in indices)
        normal = np.cross(b - a, d - a)
        if np.linalg.norm(normal) < 1e-18:
            return
        if np.dot(normal, compiled.gradient((a + b + d) / 3)) < 0:
            indices = [indices[0], indices[2], indices[1]]
        triangles.append(indices)
        require(len(triangles) <= MAX_TRIANGLES, 'Feld-Dreiecksbudget überschritten.', 'BUDGET_EXCEEDED')
    for cell in leaves['cells']:
        corners = [tuple(int(x) for x in cell + offset) for offset in OFFSETS]
        vs = [values[k] for k in corners]
        for tet in TETS:
            inside = [i for i in tet if vs[i] < 0]
            outside = [i for i in tet if vs[i] >= 0]
            if not inside or not outside:
                continue
            if len(inside) in (1, 3):
                one = inside if len(inside) == 1 else outside
                other = outside if len(inside) == 1 else inside
                triangle([vertex(corners[one[0]], corners[j]) for j in other])
            else:
                a, b = inside
                d, e = outside
                q = [vertex(corners[a], corners[d]), vertex(corners[a], corners[e]), vertex(corners[b], corners[e]), vertex(corners[b], corners[d])]
                triangle([q[0], q[1], q[2]])
                triangle([q[0], q[2], q[3]])
    return _finish(vertices, triangles, f, leaves, 'marching_tetrahedra', {'pruning': pruning,
                   'note': 'Uniform leaf cells with marching tetrahedra; subcell topology is not certified.'})


def qef_vertex(points, normals, center, cell_lo, cell_hi, regularization):
    """arg min sum (n_i.(x-p_i))^2 + lambda |x-c|^2, solved by SVD with rank truncation, clamped to the cell."""
    A = np.asarray(normals, float)
    b = np.einsum('ij,ij->i', A, np.asarray(points, float))
    ata = A.T @ A + regularization * np.eye(3)
    atb = A.T @ b + regularization * center
    u, s, vt = np.linalg.svd(ata)
    tolerance = max(s[0] * 1e-3, 1e-12)
    inverse = np.array([1 / x if x > tolerance else 0.0 for x in s])
    rank = int((s > tolerance).sum())
    x = vt.T @ (inverse * (u.T @ atb))
    clamped = np.clip(x, cell_lo, cell_hi)
    return clamped, rank, bool(np.any(clamped != x))


def dual_contouring(f, sampled, compiled, pruning, regularization=1e-3):
    leaves = leaf_cells(f, sampled, pruning)
    values = corner_values(leaves, sampled)
    lo, spacing = leaves['lo'], leaves['spacing']
    n = leaves['n']
    cell_index = {tuple(int(x) for x in cell): i for i, cell in enumerate(leaves['cells'])}
    vertices = []
    cell_vertex = {}
    rank_deficient = 0
    clamped_count = 0
    scale = float(np.linalg.norm(spacing))
    for cell in leaves['cells']:
        key = tuple(int(x) for x in cell)
        corners = [tuple(int(x) for x in cell + offset) for offset in OFFSETS]
        vs = [values[k] for k in corners]
        points, normals = [], []
        for a, b in EDGES:
            va, vb = vs[a], vs[b]
            if (va < 0) == (vb < 0):
                continue
            t = va / (va - vb)
            point = lo + spacing * (np.array(corners[a]) + t * (np.array(corners[b]) - np.array(corners[a])))
            gradient = compiled.gradient(point)
            norm = float(np.linalg.norm(gradient))
            if not math.isfinite(norm) or norm < 1e-12:
                continue
            points.append(point)
            normals.append(gradient / norm)
        if not points:
            continue
        cell_lo = lo + spacing * cell
        cell_hi = cell_lo + spacing
        center = (cell_lo + cell_hi) / 2
        # Regularization is relative to the cell size so the tie-break stays scale-free.
        vertex, rank, clamped = qef_vertex(points, normals, center, cell_lo, cell_hi, regularization / scale ** 2 * len(points))
        rank_deficient += rank < 3
        clamped_count += clamped
        cell_vertex[key] = len(vertices)
        vertices.append(vertex.tolist())
    triangles = []
    axes = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    # The four cells around an edge along axis k are offset in the two other axes.
    around = {0: [(0, -1, -1), (0, 0, -1), (0, 0, 0), (0, -1, 0)],
              1: [(-1, 0, -1), (-1, 0, 0), (0, 0, 0), (0, 0, -1)],
              2: [(-1, -1, 0), (0, -1, 0), (0, 0, 0), (-1, 0, 0)]}
    seen = set()
    for key in cell_index:
        cell = np.array(key)
        corners = [tuple(int(x) for x in cell + offset) for offset in OFFSETS]
        for k, axis in enumerate(axes):
            # Edges leaving the cell's minimum corner along each axis are owned by that corner.
            a = corners[0]
            b = tuple(a[i] + axis[i] for i in range(3))
            if (a, b) in seen:
                continue
            seen.add((a, b))
            if any(x <= 0 or x >= n for i, x in enumerate(a) if i != k):
                continue
            va, vb = values.get(a), values.get(b)
            if va is None or vb is None or (va < 0) == (vb < 0):
                continue
            ring = []
            for offset in around[k]:
                neighbour = tuple(a[i] + offset[i] for i in range(3))
                if neighbour not in cell_vertex:
                    ring = None
                    break
                ring.append(cell_vertex[neighbour])
            if ring is None or len(set(ring)) < 3:
                continue
            if va < 0:
                ring = ring[::-1]
            for tri in ([ring[0], ring[1], ring[2]], [ring[0], ring[2], ring[3]]):
                if len(set(tri)) < 3:
                    continue
                p, q, r = (np.asarray(vertices[i]) for i in tri)
                normal = np.cross(q - p, r - p)
                if np.linalg.norm(normal) < 1e-18:
                    continue
                if np.dot(normal, compiled.gradient((p + q + r) / 3)) < 0:
                    tri = [tri[0], tri[2], tri[1]]
                triangles.append(tri)
                require(len(triangles) <= MAX_TRIANGLES, 'Feld-Dreiecksbudget überschritten.', 'BUDGET_EXCEEDED')
    return _finish(vertices, triangles, f, leaves, 'dual_contouring_qef', {'pruning': pruning,
                   'qef_rank_deficient_cells': int(rank_deficient), 'qef_clamped_vertices': int(clamped_count),
                   'qef_regularization': regularization,
                   'note': 'Uniform-level dual contouring with cell-restricted QEF vertices; neither watertightness nor subcell topology is certified.'})


def extract_with_options(f, sampled, compiled, options):
    method = options.get('method', 'marching_tetrahedra')
    pruning = options.get('pruning', 'lipschitz')
    require(method in ('marching_tetrahedra', 'dual_contouring') and pruning in ('lipschitz', 'interval'),
            'Nicht registrierte Extraktionsoption.', 'OUT_OF_SCOPE')
    if method == 'dual_contouring':
        return dual_contouring(f, sampled, compiled, pruning)
    return marching_tetrahedra(f, sampled, compiled, pruning)
