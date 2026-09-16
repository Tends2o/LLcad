"""OpenVDB import: natural cubic B-spline interpolation, measured Lipschitz bound and rigorous enclosure."""
import os, sys, math, tempfile, unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'workers/cad-occt'))
sys.path.append('/usr/lib/python3/dist-packages')
import pyopenvdb as vdb
from volume_io import load_grid, import_field
from fields import compile_field, evaluate
from intervals import evaluate_interval, certify_deviation
from geometry import GeometryError

PLANE = {'op': 'plane', 'normal': ['1', '2', '-3'], 'offset': '0.5'}
SPHERE = {'op': 'sphere', 'center': ['0', '0', '0'], 'radius': '3'}


def write_grid(path, values, voxel, origin, name='grid', metadata=None):
    grid = vdb.FloatGrid(background=float(values.max()) + 1)
    grid.name = name
    grid.copyFromArray(values.astype(np.float32), ijk=(0, 0, 0))
    grid.transform = vdb.createLinearTransform([[voxel, 0, 0, 0], [0, voxel, 0, 0], [0, 0, voxel, 0], [origin[0], origin[1], origin[2], 1]])
    for key, value in (metadata or {}).items():
        grid[key] = value
    vdb.write(path, grid)


def sampled(expression, n, voxel, origin):
    return np.array([[[evaluate(expression, origin + voxel * np.array([i, j, k])) for k in range(n)] for j in range(n)] for i in range(n)])


class VolumeImportTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.previous = os.getcwd()
        os.chdir(self.directory)
        self.voxel, self.origin, n = 0.5, np.array([-2., -1., -3.]), 12
        write_grid('input-linear.vdb', sampled(PLANE, n, self.voxel, self.origin), self.voxel, self.origin, name='linear',
                   metadata={'value_unit': 'length', 'coordinate_unit': 'mm'})
        self.node = {'op': 'sampled_grid', 'artifact_id': 'linear', 'lipschitz': '1.0001', 'value_unit': 'length', 'source_unit': 'mm'}
        self.extent = self.voxel * (n - 1)

    def tearDown(self):
        os.chdir(self.previous)

    def test_natural_spline_interpolates_samples_and_reproduces_linear_fields(self):
        g = load_grid('input-linear.vdb', None, 'mm')
        self.assertEqual(g['grid_name'], 'linear')
        self.assertGreaterEqual(g['lipschitz'], 1.0)
        self.assertLess(g['lipschitz'], 1.0001)
        self.assertLess(g['interpolation_error_at_probes'], 1e-12)
        rng = np.random.default_rng(3)
        compiled = compile_field(self.node)
        for _ in range(60):
            p = self.origin + rng.random(3) * self.extent
            exact = evaluate(PLANE, p)
            # float32 storage of the samples limits agreement to about 1e-7.
            self.assertLess(abs(compiled(p) - exact), 1e-6)
            self.assertLess(abs(evaluate(self.node, p) - exact), 1e-6)
        for index in [(0, 0, 0), (11, 11, 11), (5, 7, 2)]:
            self.assertLess(abs(compiled(self.origin + self.voxel * np.array(index)) - g['values'][index]), 1e-9)
        self.assertAlmostEqual(compiled(self.origin - 5), compiled(self.origin), places=12)

    def test_interval_enclosure_gradient_and_smoothness(self):
        lo = np.array([[-1.9, -0.9, -2.9], [-1.5, -0.5, -2.5], [-2.2, -1.1, -3.3]])
        hi = np.array([[-1.6, -0.6, -2.6], [-0.5, 0.5, -1.5], [-1.8, -0.7, -2.8]])
        result = evaluate_interval(self.node, lo, hi)
        rng = np.random.default_rng(5)
        compiled = compile_field(self.node)
        for c in range(3):
            for _ in range(60):
                p = lo[c] + rng.random(3) * (hi[c] - lo[c])
                value = compiled(p)
                self.assertLessEqual(result.v.lo[c] - 1e-12, value)
                self.assertGreaterEqual(result.v.hi[c] + 1e-12, value)
        normal = np.array([1., 2., -3.]) / math.sqrt(14)
        for c in range(2):
            for axis in range(3):
                self.assertLessEqual(result.g[axis].lo[c] - 1e-6, normal[axis])
                self.assertGreaterEqual(result.g[axis].hi[c] + 1e-6, normal[axis])
        self.assertTrue(bool(result.smooth[0]))
        self.assertTrue(bool(result.smooth[1]))
        self.assertFalse(bool(result.smooth[2]))
        nlo, nhi = result.norm_bounds()
        self.assertGreater(nlo[0], 0.9999)
        self.assertLess(nhi[1], 1.0001)
        self.assertEqual(nlo[2], 0.0)

    def test_certificate_between_a_sampled_sphere_and_its_analytic_source(self):
        voxel, origin, n = 0.4, np.array([-4.8, -4.8, -4.8]), 25
        write_grid('input-sphere.vdb', sampled(SPHERE, n, voxel, origin), voxel, origin, name='sphere', metadata={'value_unit': 'length'})
        g = load_grid('input-sphere.vdb', None, 'mm')
        # The distance kink at the centre makes the interpolating spline ring there: the measured
        # bound is honestly larger than the 1-Lipschitz source, and it is what the field really has.
        self.assertGreater(g['lipschitz'], 1.0)
        node = {'op': 'sampled_grid', 'artifact_id': 'sphere', 'lipschitz': '%.4f' % (g['lipschitz'] + 1e-4), 'value_unit': 'length'}
        domain = {'min': [-4.4] * 3, 'max': [4.4] * 3}
        report = certify_deviation(SPHERE, node, domain, voxel / 2, 0.05)
        self.assertEqual(report['status'], 'certified', report)
        self.assertGreater(report['certified_hausdorff_bound_mm'], 0)
        self.assertLessEqual(report['certified_hausdorff_bound_mm'], 0.05)
        self.assertEqual(report['failed_cells'], 0)
        tight = certify_deviation(SPHERE, node, domain, voxel / 2, 1e-7)
        self.assertEqual(tight['status'], 'not_certified')
        self.assertIn('deviation_exceeds_epsilon', tight['failure_reasons'])

    def test_claims_units_and_transforms_are_verified(self):
        with self.assertRaises(GeometryError) as caught:
            compile_field(dict(self.node, lipschitz='0.5'))
        self.assertEqual(caught.exception.code, 'CONSTRAINT_CONFLICT')
        with self.assertRaises(GeometryError) as caught:
            compile_field(dict(self.node, value_unit='dimensionless'))
        self.assertEqual(caught.exception.code, 'UNIT_MISMATCH')
        with self.assertRaises(GeometryError) as caught:
            load_grid('input-linear.vdb', None, 'm')
        self.assertEqual(caught.exception.code, 'UNIT_MISMATCH')
        rotated = vdb.FloatGrid(background=1.0)
        rotated.copyFromArray(np.zeros((3, 3, 3), np.float32))
        rotated.transform = vdb.createLinearTransform([[0, 1, 0, 0], [-1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
        vdb.write('input-rotated.vdb', rotated)
        with self.assertRaises(GeometryError) as caught:
            load_grid('input-rotated.vdb', None, 'mm')
        self.assertEqual(caught.exception.code, 'OUT_OF_SCOPE')
        field, report = import_field({'id': 'f', 'cache_key': 'k', 'construction': {'operator': 'imported', 'artifact_id': 'linear', 'format': 'vdb', 'source_unit': 'mm'}})
        self.assertEqual(field['construction']['operator'], 'field')
        self.assertEqual(field['construction']['expression']['op'], 'sampled_grid')
        self.assertEqual(report['voxel_size_mm'], [0.5, 0.5, 0.5])
        self.assertFalse(report['value_unit_assumed'])
        self.assertIsNone(report['continuous_distance_certificate'])
        self.assertEqual(field['field']['semantics'], 'sampled_implicit_cubic_bspline')


if __name__ == '__main__':
    unittest.main()
