"""Bézier curves and tensor-product surfaces in the Bernstein basis (Bauplan 6.11).

Exact rational evaluation (fractions) of points and derivatives, used as an independent oracle
against the native kernel's B-spline evaluation. Degree elevation and de Casteljau subdivision
give the local refinement path of a compact patch without changing its shape.
"""
from fractions import Fraction
from math import comb


def _fractions(values):
    return [Fraction(str(v)) if not isinstance(v, Fraction) else v for v in values]


def bernstein(n, i, t):
    return comb(n, i) * t ** i * (1 - t) ** (n - i)


def curve_point(poles, t):
    """Point of the Bézier curve with control points `poles` at parameter t in [0, 1]."""
    n = len(poles) - 1
    t = Fraction(str(t)) if not isinstance(t, Fraction) else t
    dim = len(poles[0])
    return [sum(bernstein(n, i, t) * _fractions(poles[i])[k] for i in range(n + 1)) for k in range(dim)]


def curve_derivative(poles, t, order=1):
    """Derivative via the hodograph: C'(t) = n * Bézier(P[i+1] - P[i])."""
    if order == 0:
        return curve_point(poles, t)
    n = len(poles) - 1
    if n == 0:
        return [Fraction(0)] * len(poles[0])
    hodograph = [[n * (b - a) for a, b in zip(_fractions(poles[i]), _fractions(poles[i + 1]))] for i in range(n)]
    return curve_derivative(hodograph, t, order - 1)


def de_casteljau(poles, t):
    """Split at t: control polygons of the left and right halves (shape preserving)."""
    t = Fraction(str(t)) if not isinstance(t, Fraction) else t
    levels = [[_fractions(p) for p in poles]]
    while len(levels[-1]) > 1:
        previous = levels[-1]
        levels.append([[(1 - t) * a + t * b for a, b in zip(previous[i], previous[i + 1])] for i in range(len(previous) - 1)])
    left = [level[0] for level in levels]
    right = [level[-1] for level in reversed(levels)]
    return left, right


def degree_elevate(poles):
    """Same curve with one more control point."""
    n = len(poles) - 1
    p = [_fractions(x) for x in poles]
    result = [p[0]]
    for i in range(1, n + 1):
        a = Fraction(i, n + 1)
        result.append([a * u + (1 - a) * v for u, v in zip(p[i - 1], p[i])])
    result.append(p[n])
    return result


def surface_point(net, u, v):
    """Tensor-product Bézier surface S(u, v) = sum B_i(u) B_j(v) P_ij."""
    rows = [curve_point(row, v) for row in net]
    return curve_point(rows, u)


def surface_partials(net, u, v):
    """First partial derivatives S_u, S_v and the (unnormalised) normal S_u x S_v."""
    rows = [curve_point(row, v) for row in net]
    s_u = curve_derivative(rows, u)
    columns = [curve_point([row[j] for row in net], u) for j in range(len(net[0]))]
    s_v = curve_derivative(columns, v)
    normal = [s_u[1] * s_v[2] - s_u[2] * s_v[1], s_u[2] * s_v[0] - s_u[0] * s_v[2], s_u[0] * s_v[1] - s_u[1] * s_v[0]]
    return s_u, s_v, normal


def partition_of_unity(n, t):
    return sum(bernstein(n, i, t) for i in range(n + 1))
