"""N5.2: rank test for derivable dimensions. No OCP imports: the check (checks.n2_n5_findings) and the planner use the same computation.

For each part and world axis there is a linear system of equations over the end coordinates of the length dimensions:
- every dimension (a, b) is the row x_b - x_a = b - a,
- every geometrically backed mirror pair (c, c', m) the row x_c + x_c' = 2 m (c = c' means: the coordinate lies in the center plane),
- every pattern pitch (c_i, c_j, t) the row x_j - x_i = t,
- every twin pair (c_i, c'_i, c_0, c'_0) the row x'_i - x_i - x'_0 + x_0 = 0.
Coordinates are merged into columns with TOL. A dimension is derivable if its row lies in the row space of those taken up before (the rank
does not increase) and the value fits (otherwise conflict, no finding). Rows are taken up in this order: facts (mirror, pattern rows), locked dimensions
(auxiliary dimensions in parentheses do not belong here, toleranced ones and dimensions from assembly views always stay), then the others by `rank` (total dimensions, function dimensions,
pattern, rest), for equal rank in input order. The number of derivable dimensions is the number of dimensions minus the rank increase and then does not depend on the order if there are no locked
or contradictory dimensions (otherwise the order of intake decides whether a dimension still adds rank or already lies in the row space); only which dimensions they are (who stays) follows the rank."""
import numpy as np

TOL = 0.01              # mm: coordinates of the same column
VALUE_TOL = 0.05        # mm: value of a derived dimension against the measured segment (rounding of the dimension texts, sum of several members)
SPAN_EPS = 1e-7         # residual norm of the row combination below which a row counts as a combination


class Rank:
    """N5.2: the rank oracle in incremental form - columns = coordinates (equal up to tol), rows taken up as an orthonormal basis of the row space (Gram-Schmidt with
    re-orthogonalization) together with the carried right-hand sides. classify asks the same as the least-squares solution (residual norm of the projection, implied value), add takes up only
    rows that increase the rank. The same computation for the check (derivable) and the planner tree (plan_dims._Tree): every decision "set/omit dimension" is a call of this."""

    def __init__(self, tol=TOL, value_tol=VALUE_TOL):
        self.tol, self.value_tol = tol, value_tol
        self.nodes = []
        self.Q = np.zeros((0, 0))        # rows: orthonormal basis of the row space
        self.r = np.zeros(0)             # right-hand side per basis row (the same combination as the row)

    def node(self, c):
        """Column of the coordinate c (new if none is equal up to tol)."""
        for n, v in enumerate(self.nodes):
            if abs(v - c) <= self.tol:
                return n
        self.nodes.append(float(c))
        self.Q = np.hstack([self.Q, np.zeros((self.Q.shape[0], 1))])
        return len(self.nodes) - 1

    def _split(self, coef):
        v = np.zeros(len(self.nodes))
        for k, x in coef.items():
            v[k] += x
        if not len(self.r):
            return v, np.zeros(0), v
        c = self.Q @ v
        w = v - self.Q.T @ c
        c2 = self.Q @ w
        return v, c + c2, w - self.Q.T @ c2

    def classify(self, coef, b):
        """'new' (rank increases), 'derived' (combination, value fits up to value_tol), 'conflict' (combination, value does not fit)."""
        v, c, w = self._split(coef)
        if np.linalg.norm(w) > SPAN_EPS * (1.0 + np.linalg.norm(v)):
            return 'new'
        return 'derived' if abs(float(c @ self.r) - b) <= self.value_tol else 'conflict'

    def add(self, coef, b):
        """Take up the row if it increases the rank; True then, otherwise False (nothing taken up)."""
        v, c, w = self._split(coef)
        nw = float(np.linalg.norm(w))
        if nw <= SPAN_EPS * (1.0 + np.linalg.norm(v)):
            return False
        self.Q = np.vstack([self.Q, w / nw])
        self.r = np.append(self.r, (float(b) - float(c @ self.r)) / nw)
        return True


def rank_order(measures):
    """Order of intake of the rank test (indices into measures): locked dimensions in input order, then the others by rank (total dimension, function dimension, pattern, rest), for equal rank
    in input order. The same order for the check (derivable) and the planner tree (plan_dims.dimension sorts its segments with it) - both
    then let the same dimension drop out."""
    locked = [n for n, m in enumerate(measures) if m.get('locked')]
    return locked + sorted((n for n, m in enumerate(measures) if not m.get('locked')), key=lambda n: (measures[n].get('rank', 4), n))


def derivable(measures, mirrors=(), steps=(), twins=(), tol=TOL, value_tol=VALUE_TOL):
    """measures: [{'key', 'a', 'b', 'rank', 'locked', 'steps'}] (coordinates along the same axis; steps = [(ci, cj, t)] of a pattern statement "n× t (=L)": the pitch rows belong to
    this dimension and come with it, otherwise the dimension would become derivable through its own pitch), mirrors: [(c, c2, m)], steps: [(ci, cj, t)] as facts, twins: [(ci, cpi, c0, cp0)] twin rows (two sequences of the same pitch are shifted against each other, c'_i - c_i = c'_0 - c_0; homogeneous, rhs 0).
    -> {'derivable': [key, ...] (in order of intake), 'conflicts': [key, ...] (row lies in the row space, the value contradicts)}. Computed with Rank."""
    R = Rank(tol, value_tol)
    node = R.node

    facts, rows = [], []                 # (coefficients as {column: value}, rhs)
    for c, c2, m in mirrors:
        i, j = node(c), node(c2)
        facts.append(({i: 2.0} if i == j else {i: 1.0, j: 1.0}, 2.0 * float(m)))
    for ci, cj, t in steps:
        i, j = node(ci), node(cj)
        if i != j:
            facts.append(({j: 1.0, i: -1.0}, float(t)))
    for ci, cpi, c0, cp0 in twins:
        i, ip, i0, ip0 = node(ci), node(cpi), node(c0), node(cp0)
        coef = {}
        for col, x in ((ip, 1.0), (i, -1.0), (ip0, -1.0), (i0, 1.0)):
            coef[col] = coef.get(col, 0.0) + x
        if any(abs(x) > 1e-12 for x in coef.values()):
            facts.append((coef, 0.0))
    items = []
    for n, mm in enumerate(measures):
        i, j = node(mm['a']), node(mm['b'])
        items.append((n, mm, i, j))
    bundle = {n: [(node(ci), node(cj), float(t)) for ci, cj, t in mm.get('steps') or ()] for n, mm, _, _ in items}
    add, classify = R.add, R.classify

    for coef, b in facts:                # facts: take up independent rows, skip dependent (also contradictory) ones
        if classify(coef, b) == 'new':
            add(coef, b)
    order = [items[n] for n in rank_order(measures)]
    out, conflicts = [], []
    for n, mm, i, j in order:
        if i == j:
            continue                     # segment of length 0 (within the tolerance): no dimension
        coef, b = {j: 1.0, i: -1.0}, float(mm['b']) - float(mm['a'])
        state = classify(coef, b)
        if state == 'new':
            add(coef, b)
            for si, sj, t in bundle[n]:
                if si != sj and classify({sj: 1.0, si: -1.0}, t) == 'new':
                    add({sj: 1.0, si: -1.0}, t)
        elif mm.get('locked'):
            continue
        elif state == 'derived':
            out.append(mm['key'])
        else:
            conflicts.append(mm['key'])
    return {'derivable': out, 'conflicts': conflicts}


def triangle_derivable(items):
    """Slanted edge in the image plane: length (len), angle (angle) and the two legs (leg) are related via Pythagoras and trigonometry, any two determine the rest.
    items: [{'key', 'role': 'len'|'angle'|'leg'}] of the same edge (angle also of parallel slanted faces). The first two of length, angle,
    first leg, second leg are kept (the desired form length + angle first, N5.3); the others are derivable. Further dimensions of the same role (second length, second angle,
    third leg) are duplicates (N5.1) and do not count here. -> keys of the derivable dimensions."""
    first = {}
    legs = []
    for it in items:
        if it['role'] == 'leg':
            if len(legs) < 2:
                legs.append(it['key'])
        else:
            first.setdefault(it['role'], it['key'])
    ranked = [k for k in (first.get('len'), first.get('angle'), *legs) if k is not None]
    return ranked[2:]
