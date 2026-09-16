import sys, unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'workers/cad-occt'))
from field_analysis import implicit_curvature
from fields import compile_field
from solver import solve
from geometry import GeometryError


class ImplicitCurvatureTests(unittest.TestCase):
    def test_sphere_and_torus_level_set_curvatures_follow_the_outward_convention(self):
        sphere = {'op': 'sphere', 'center': ['0', '0', '0'], 'radius': '5'}
        report = implicit_curvature(sphere, compile_field(sphere), [3, 4, 0], 1e-3)
        self.assertAlmostEqual(report['mean_curvature_per_mm'], -0.2, places=6)
        self.assertAlmostEqual(report['gaussian_curvature_per_mm2'], 0.04, places=6)
        self.assertAlmostEqual(report['regularity']['certified_gradient_lower_bound'], 1.0, places=12)
        self.assertAlmostEqual(report['direction'][0], 0.6, places=6)
        torus = {'op': 'torus', 'center': ['0', '0', '0'], 'major': '2', 'minor': '0.5'}
        report = implicit_curvature(torus, compile_field(torus), [2.5, 0, 0], 1e-3)
        np.testing.assert_allclose(report['principal_curvatures_per_mm'], [-2, -0.4], atol=1e-5)
        self.assertAlmostEqual(report['gaussian_curvature_per_mm2'], 0.8, places=5)
        # Away from the zero set the level set through the point is reported, never a made-up surface.
        report = implicit_curvature(sphere, compile_field(sphere), [0, 0, 2.5], 1e-3)
        self.assertAlmostEqual(report['field_value'], -2.5, places=12)
        self.assertAlmostEqual(report['mean_curvature_per_mm'], -0.4, places=6)

    def test_creases_and_singular_gradients_are_refused(self):
        boxes = {'op': 'union', 'a': {'op': 'box', 'center': ['0', '0', '0'], 'half_size': ['2', '2', '2']},
                 'b': {'op': 'box', 'center': ['2', '0', '0'], 'half_size': ['2', '1', '1']}}
        compiled = compile_field(boxes)
        with self.assertRaises(GeometryError) as caught:
            implicit_curvature(boxes, compiled, [2, 1, 1], 1e-3)
        self.assertEqual(caught.exception.code, 'PRECISION_UNSUPPORTED')
        report = implicit_curvature(boxes, compiled, [0, 0, 2], 1e-3)
        self.assertAlmostEqual(report['mean_curvature_per_mm'], 0, places=6)
        sphere = {'op': 'sphere', 'center': ['0', '0', '0'], 'radius': '5'}
        with self.assertRaises(GeometryError) as caught:
            implicit_curvature(sphere, compile_field(sphere), [0, 0, 0], 1e-3)
        self.assertEqual(caught.exception.code, 'PRECISION_UNSUPPORTED')


def c(value): return {'constant': value}
def p(name): return {'parameter': name}
def f(name, *args): return {'fn': name, 'args': list(args)}


class SolverDiagnosticsTests(unittest.TestCase):
    def problem(self, equations, objectives=None, regularization=1.0):
        return {'variables': [{'name': 'w', 'initial': 10, 'lower_value': 7, 'upper_value': 12},
                              {'name': 'd', 'initial': 5, 'lower_value': 3, 'upper_value': 7}],
                'equations': equations, 'objectives': objectives or [], 'regularization': regularization, 'max_iterations': 100}

    def test_rank_redundancy_and_target_sensitivity_are_reported(self):
        area = {'id': 'area', 'relation': 'eq', 'tolerance': 1e-8, 'expression': f('-', f('/', f('*', p('w'), p('d')), c(48)), c(1))}
        twice = {'id': 'area-twice', 'relation': 'eq', 'tolerance': 1e-8, 'expression': f('-', f('/', f('*', p('w'), p('d')), c(48)), c(1))}
        result = solve(self.problem([area, twice]))
        d = result['diagnostics']
        self.assertEqual(d['jacobian_rank'], 1)
        self.assertEqual(len(d['redundant_constraints']), 1)
        self.assertEqual(set(d['redundant_constraints'][0]['combination']), {'area', 'area-twice'})
        self.assertEqual(len(d['free_directions']), 1)
        self.assertEqual(result['global_optimum_claimed'], False)
        self.assertAlmostEqual(result['values']['w'] * result['values']['d'], 48, places=6)
        sensitivity = d['target_sensitivity'][0]['parameter_change_per_unit_target']
        self.assertTrue(all(np.isfinite(v) for v in sensitivity.values()))

    def test_soft_objectives_with_robust_losses_are_optional_and_reported(self):
        area = {'id': 'area', 'relation': 'eq', 'tolerance': 1e-8, 'expression': f('-', f('/', f('*', p('w'), p('d')), c(48)), c(1))}
        prefer = {'id': 'prefer-square', 'expression': f('-', f('/', p('w'), p('d')), c(1)), 'weight': 5, 'scale': 1, 'loss': 'huber'}
        result = solve(self.problem([area], [prefer], regularization=0.0))
        self.assertEqual(result['objective_terms'][0]['loss'], 'huber')
        self.assertLess(abs(result['values']['w'] - result['values']['d']), 0.6)
        with self.assertRaises(GeometryError):
            solve(self.problem([area], [dict(prefer, loss='python')]))


if __name__ == '__main__':
    unittest.main()
