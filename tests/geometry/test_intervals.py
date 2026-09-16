import json, math, sys, unittest, warnings
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'workers/cad-occt'))
from intervals import evaluate_interval, certify_deviation, certified_empty, value_range, difference_magnitude, _point
from extract import extract_with_options
from fields import evaluate, gradient, compile_field
from field_cache import Sampler
from geometry import GeometryError

SPHERE = {'op': 'sphere', 'center': ['0.1', '-0.2', '0.3'], 'radius': '2'}
PRIMITIVES = [SPHERE,
              {'op': 'plane', 'normal': ['1', '2', '-1'], 'offset': '0.4'},
              {'op': 'box', 'center': ['1', '0', '0'], 'half_size': ['1', '2', '3']},
              {'op': 'torus', 'center': ['0', '1', '0'], 'major': '2', 'minor': '0.5'},
              {'op': 'cylinder', 'center': ['0', '0', '1'], 'radius': '1', 'half_height': '2'},
              {'op': 'capsule', 'start': ['-1', '0', '0'], 'end': ['1', '2', '0'], 'radius': '0.7'},
              {'op': 'capsule', 'start': ['1', '0', '0'], 'end': ['1', '0', '0'], 'radius': '0.7'},
              {'op': 'gyroid', 'period': '3', 'origin': ['0.1', '0.2', '-0.3'], 'threshold': '0.2'}]
EXPRESSIONS = PRIMITIVES + [
    {'op': op, 'a': SPHERE, 'b': PRIMITIVES[3], **({'k': '0.6'} if op == 'smooth_union' else {})}
    for op in ['union', 'intersection', 'difference', 'smooth_union']]
EXPRESSIONS += [
    {'op': 'transform', 'source': EXPRESSIONS[9], 'translation': ['1', '-2', '0.1'], 'scale': ['2', '0.5', '1.5']},
    {'op': 'affine_transform', 'source': EXPRESSIONS[10], 'translation': ['2', '1', '0'], 'matrix': [['-2', '0.2', '0'], ['0', '1', '0.3'], ['0', '0', '1.5']]},
    {'op': 'rotate', 'source': PRIMITIVES[2], 'axis': ['1', '2', '3'], 'origin': ['0.3', '0.4', '0.5'], 'angle': {'value': '32.4', 'unit': 'deg'}},
    {'op': 'local_deform', 'source': SPHERE, 'center': ['1', '0', '0'], 'radius': '2', 'displacement': ['0.1', '0.05', '-0.04']},
    {'op': 'offset', 'source': SPHERE, 'distance': '0.1'},
    {'op': 'shell', 'source': SPHERE, 'thickness': '0.4', 'variations': [{'center': ['2', '0', '0'], 'radius': '1', 'amplitude': '0.1'}]},
    {'op': 'local_field_delta', 'source': SPHERE, 'center': ['2', '0', '0'], 'radius': '1', 'amplitude': '-0.05'},
    {'op': 'convert_field_unit', 'source': PRIMITIVES[-1], 'to': 'length', 'reference_length': {'value': '0.0005', 'unit': 'm'}},
    {'op': 'local_field_delta', 'source': {'op': 'transform', 'source': SPHERE, 'translation': ['0', '0', '0'], 'scale': ['2', '2', '2']},
     'center': ['4', '0', '0'], 'radius': '1', 'amplitude': '0.05'}]
BOXES = {'op': 'union', 'a': {'op': 'box', 'center': ['0', '0', '0'], 'half_size': ['2', '2', '2']},
         'b': {'op': 'box', 'center': ['2', '0', '0'], 'half_size': ['2', '1', '1']}}
DOMAIN = {'min': ['-4', '-4', '-4'], 'max': ['4', '4', '4']}


class IntervalTests(unittest.TestCase):
    def test_values_gradients_and_norm_bounds_contain_reference_evaluations(self):
        rng = np.random.default_rng(11)
        checked = 0
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            for node in EXPRESSIONS:
                lo = rng.uniform(-4, 4, (200, 3))
                hi = lo + rng.uniform(0.01, 1.5, (200, 3))
                gv = evaluate_interval(node, lo, hi)
                nlo, nhi = gv.norm_bounds()
                for i in range(200):
                    for _ in range(4):
                        p = rng.uniform(lo[i], hi[i])
                        v = evaluate(node, p)
                        self.assertLessEqual(gv.v.lo[i] - 1e-12, v, node['op'])
                        self.assertGreaterEqual(gv.v.hi[i] + 1e-12, v, node['op'])
                        if gv.smooth[i] and node['op'] != 'local_deform':
                            g = gradient(node, p, 1e-6)
                            for k in range(3):
                                self.assertLessEqual(gv.g[k].lo[i] - 1e-5, g[k], node['op'])
                                self.assertGreaterEqual(gv.g[k].hi[i] + 1e-5, g[k], node['op'])
                            norm = float(np.linalg.norm(g))
                            self.assertLessEqual(nlo[i] - 1e-5, norm, node['op'])
                            self.assertGreaterEqual(nhi[i] + 1e-5, norm, node['op'])
                            checked += 1
        self.assertGreater(checked, 5000)

    def test_exact_primitives_keep_unit_gradient_bounds_where_smooth(self):
        lo = np.array([[2.5, 0.0, 0.0], [-0.2, 3.0, 0.5]])
        hi = lo + 0.2
        for node in [SPHERE, PRIMITIVES[2], PRIMITIVES[4], EXPRESSIONS[14]]:
            gv = evaluate_interval(node, lo, hi)
            nlo, nhi = gv.norm_bounds()
            for i in range(2):
                if gv.smooth[i]:
                    self.assertAlmostEqual(float(nlo[i]), 1.0, places=9)
                    self.assertAlmostEqual(float(nhi[i]), 1.0, places=9)

    def test_certificate_bounds_a_compact_edit_and_matches_the_measured_shift(self):
        edited = {'op': 'local_field_delta', 'source': SPHERE, 'center': ['2.1', '-0.2', '0.3'], 'radius': '1', 'amplitude': '0.04'}
        report = certify_deviation(SPHERE, edited, DOMAIN, 0.25, 0.05)
        self.assertEqual(report['status'], 'certified')
        self.assertLessEqual(report['certified_hausdorff_bound_mm'], 0.05)
        self.assertGreater(report['identical_cells'], 0)
        self.assertEqual(report['failed_cells'], 0)
        compiled = compile_field(edited)
        centre = np.array([0.1, -0.2, 0.3])
        a, b = 1.5, 2.5
        for _ in range(60):
            m = (a + b) / 2
            if compiled(centre + np.array([m, 0, 0])) < 0:
                a = m
            else:
                b = m
        self.assertLessEqual(abs(2 - (a + b) / 2), report['certified_hausdorff_bound_mm'])
        self.assertGreater(abs(2 - (a + b) / 2), 0.03)

    def test_global_radius_change_is_certified_exactly_and_creases_are_refused(self):
        bigger = dict(SPHERE, radius='2.03')
        report = certify_deviation(SPHERE, bigger, DOMAIN, 0.25, 0.05)
        self.assertEqual(report['status'], 'certified')
        self.assertAlmostEqual(report['certified_hausdorff_bound_mm'], 0.03, places=9)
        self.assertIsNone(report['difference_supports'])
        crease = {'op': 'local_field_delta', 'source': BOXES, 'center': ['2', '1', '1'], 'radius': '0.5', 'amplitude': '0.02'}
        refused = certify_deviation(BOXES, crease, {'min': ['-5'] * 3, 'max': ['5'] * 3}, 0.25, 0.05)
        self.assertEqual(refused['status'], 'not_certified')
        self.assertIn('nonsmooth_or_singular_gradient', refused['failure_reasons'])
        self.assertIsNone(refused['certified_hausdorff_bound_mm'])
        face = {'op': 'local_field_delta', 'source': BOXES, 'center': ['0', '0', '2'], 'radius': '0.5', 'amplitude': '0.02'}
        accepted = certify_deviation(BOXES, face, {'min': ['-5'] * 3, 'max': ['5'] * 3}, 0.25, 0.05)
        self.assertEqual(accepted['status'], 'certified')
        self.assertLessEqual(accepted['certified_hausdorff_bound_mm'], 0.03)
        tight = certify_deviation(SPHERE, bigger, DOMAIN, 0.25, 0.02)
        self.assertEqual(tight['status'], 'not_certified')
        self.assertIn('deviation_exceeds_epsilon', tight['failure_reasons'])
        margin = certify_deviation(SPHERE, bigger, {'min': ['-2.05', '-4', '-4'], 'max': ['4', '4', '4']}, 0.25, 0.1)
        self.assertIn('surface_reaches_domain_margin', margin['failure_reasons'])

    def test_difference_propagation_is_tight_for_compatible_structure(self):
        lo = np.array([[1.5, -0.5, 0.0]])
        hi = lo + 0.5
        point = _point(lo, hi)
        edited = {'op': 'local_field_delta', 'source': SPHERE, 'center': ['2', '0', '0'], 'radius': '1', 'amplitude': '-0.05'}
        self.assertLessEqual(float(difference_magnitude(SPHERE, edited, point)[0]), 0.05 + 1e-12)
        self.assertEqual(float(difference_magnitude(SPHERE, SPHERE, point)[0]), 0.0)
        nested = {'op': 'union', 'a': SPHERE, 'b': PRIMITIVES[3]}
        changed = {'op': 'union', 'a': dict(SPHERE, radius='2.01'), 'b': PRIMITIVES[3]}
        self.assertAlmostEqual(float(difference_magnitude(nested, changed, point)[0]), 0.01, places=12)
        transformed = {'op': 'transform', 'source': SPHERE, 'translation': ['0', '0', '0'], 'scale': ['2', '2', '2']}
        moved = {'op': 'transform', 'source': dict(SPHERE, radius='2.01'), 'translation': ['0', '0', '0'], 'scale': ['2', '2', '2']}
        self.assertAlmostEqual(float(difference_magnitude(transformed, moved, point)[0]), 0.02, places=12)

    def test_value_range_and_certified_pruning(self):
        rng_range = value_range(SPHERE, DOMAIN, 0.5)
        self.assertLessEqual(rng_range['lower'], -2)
        self.assertGreaterEqual(rng_range['upper'], math.sqrt(3) * 4 - 2)
        lo = np.array([[2.5, 2.5, 2.5], [-0.2, -0.2, -0.2], [1.8, -0.2, 0.3]])
        self.assertEqual(certified_empty(SPHERE, lo, lo + 0.5).tolist(), [True, True, False])

    def test_batched_extraction_methods_produce_outward_previews(self):
        node = {'op': 'sphere', 'center': ['0', '0', '0'], 'radius': '1'}
        feature = {'id': 'f', 'construction': {'expression': node, 'domain': {'min': ['-1.5'] * 3, 'max': ['1.5'] * 3}},
                   'field': {'cell_size': .25, 'lipschitz': 1, 'semantics': 'exact_sdf'}}
        for method in ('marching_tetrahedra', 'dual_contouring'):
            for pruning in ('lipschitz', 'interval'):
                sampler = Sampler(node, 'build')
                result = extract_with_options(feature, sampler, sampler.compiled, {'method': method, 'pruning': pruning})
                self.assertEqual(result['method'], method if method == 'marching_tetrahedra' else 'dual_contouring_qef')
                self.assertEqual(result['pruning'], pruning)
                self.assertGreater(result['pruned_cells'], 0)
                vertices = np.array(result['vertices'])
                self.assertGreater(len(result['triangles']), 50)
                for a, b, c in (vertices[t] for t in result['triangles']):
                    self.assertGreater(float(np.dot(np.cross(b - a, c - a), (a + b + c) / 3)), 0)
                self.assertLess(max(abs(float(np.linalg.norm(v)) - 1) for v in vertices), 0.05 if method == 'dual_contouring_qef' else 0.08)
                if method == 'dual_contouring':
                    # Tangent planes of a curved patch may meet outside the cell; clamping is part of the contract.
                    self.assertLessEqual(result['qef_clamped_vertices'], result['leaf_cells'])
                    self.assertIn('qef_rank_deficient_cells', result)
        with self.assertRaises(GeometryError):
            extract_with_options(feature, sampler, sampler.compiled, {'method': 'python', 'pruning': 'interval'})


if __name__ == '__main__':
    unittest.main()
