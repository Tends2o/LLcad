"""Bernstein-basis Bézier evaluation as an exact oracle against the native kernel (Bauplan 6.11)."""
import sys, unittest
from fractions import Fraction
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'workers/cad-occt'))
import bezier
from geometry import gp_Pnt, gp_Vec
from OCP.Geom import Geom_BezierCurve, Geom_BezierSurface
from OCP.TColgp import TColgp_Array1OfPnt, TColgp_Array2OfPnt

POLES = [['0', '0', '0'], ['1', '2', '0'], ['3', '2', '1'], ['4', '0', '0']]
NET = [[['0', '0', '0'], ['0', '1', '0'], ['0', '2', '0']],
       [['1', '0', '1'], ['1', '1', '2'], ['1', '2', '1']],
       [['2', '0', '0'], ['2', '1', '0'], ['2', '2', '0']]]


class BezierTests(unittest.TestCase):
    def test_partition_of_unity_and_endpoint_interpolation(self):
        for n in range(1, 8):
            for t in ('0', '0.25', '0.5', '0.9', '1'):
                self.assertEqual(bezier.partition_of_unity(n, Fraction(t)), 1)
        self.assertEqual(bezier.curve_point(POLES, '0'), [Fraction(0)] * 3)
        self.assertEqual(bezier.curve_point(POLES, '1'), [Fraction(4), Fraction(0), Fraction(0)])
        # End tangents follow the first and last control legs.
        self.assertEqual(bezier.curve_derivative(POLES, '0'), [Fraction(3), Fraction(6), Fraction(0)])

    def test_matches_native_bezier_curve_and_derivatives(self):
        array = TColgp_Array1OfPnt(1, 4)
        for i, p in enumerate(POLES):
            array.SetValue(i + 1, gp_Pnt(*map(float, p)))
        native = Geom_BezierCurve(array)
        for t in ('0.1', '0.3', '0.5', '0.77', '0.9'):
            point = bezier.curve_point(POLES, t)
            first = bezier.curve_derivative(POLES, t)
            second = bezier.curve_derivative(POLES, t, 2)
            p, d1, d2 = gp_Pnt(), gp_Vec(), gp_Vec()
            native.D2(float(t), p, d1, d2)
            for exact, value in zip(point, (p.X(), p.Y(), p.Z())):
                self.assertAlmostEqual(float(exact), value, places=12)
            for exact, value in zip(first, (d1.X(), d1.Y(), d1.Z())):
                self.assertAlmostEqual(float(exact), value, places=11)
            for exact, value in zip(second, (d2.X(), d2.Y(), d2.Z())):
                self.assertAlmostEqual(float(exact), value, places=10)

    def test_subdivision_and_degree_elevation_preserve_the_curve(self):
        left, right = bezier.de_casteljau(POLES, '0.4')
        for s in ('0', '0.5', '1'):
            t = Fraction('0.4') * Fraction(s)
            self.assertEqual(bezier.curve_point(left, s), bezier.curve_point(POLES, t))
            u = Fraction('0.4') + Fraction('0.6') * Fraction(s)
            self.assertEqual(bezier.curve_point(right, s), bezier.curve_point(POLES, u))
        elevated = bezier.degree_elevate(POLES)
        self.assertEqual(len(elevated), 5)
        for t in ('0.2', '0.5', '0.8'):
            self.assertEqual(bezier.curve_point(elevated, t), bezier.curve_point(POLES, t))

    def test_tensor_surface_matches_native_surface_and_normal(self):
        array = TColgp_Array2OfPnt(1, 3, 1, 3)
        for i, row in enumerate(NET):
            for j, p in enumerate(row):
                array.SetValue(i + 1, j + 1, gp_Pnt(*map(float, p)))
        native = Geom_BezierSurface(array)
        for u, v in (('0.5', '0.5'), ('0.2', '0.7'), ('0.9', '0.1')):
            point = bezier.surface_point(NET, u, v)
            s_u, s_v, normal = bezier.surface_partials(NET, u, v)
            p, d1u, d1v = gp_Pnt(), gp_Vec(), gp_Vec()
            native.D1(float(u), float(v), p, d1u, d1v)
            for exact, value in zip(point, (p.X(), p.Y(), p.Z())):
                self.assertAlmostEqual(float(exact), value, places=12)
            for exact, value in zip(s_u, (d1u.X(), d1u.Y(), d1u.Z())):
                self.assertAlmostEqual(float(exact), value, places=11)
            for exact, value in zip(s_v, (d1v.X(), d1v.Y(), d1v.Z())):
                self.assertAlmostEqual(float(exact), value, places=11)
            cross = d1u.Crossed(d1v)
            for exact, value in zip(normal, (cross.X(), cross.Y(), cross.Z())):
                self.assertAlmostEqual(float(exact), value, places=10)


if __name__ == '__main__':
    unittest.main()
