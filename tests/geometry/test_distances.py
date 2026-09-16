"""Sampled and exact distance measures, wall thickness and motion-sampled clearance."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'workers/cad-occt'))
from geometry import *
import distances


def feature(fid, op, p=None, **c):
    return {'id': fid, 'values': p or {}, 'construction': {'operator': op, **c}}


class DistanceTests(unittest.TestCase):
    def test_surface_distance_reports_strengths_separately(self):
        a = make_feature(feature('a', 'box', {'width': 10, 'depth': 10, 'height': 4}), [])
        b = make_feature(feature('b', 'box', {'width': 10, 'depth': 10, 'height': 4, 'x': 0.5}), [])
        report = distances.surface_distance(a, b)
        self.assertEqual(report['sampled_chamfer_mm']['guarantee'], 'sampled')
        self.assertEqual(report['sampled_hausdorff_mm']['guarantee'], 'sampled')
        self.assertAlmostEqual(report['sampled_hausdorff_mm']['value'], 0.5, places=9)
        self.assertLess(report['sampled_chamfer_mm']['value'], 0.5)
        self.assertEqual(report['exact_minimum_distance_mm']['guarantee'], 'exact_for_declared_domain')
        self.assertEqual(report['exact_minimum_distance_mm']['value'], 0.0)
        self.assertAlmostEqual(report['volume_iou']['value'], 9.5 / 10.5, places=9)
        far = make_feature(feature('c', 'box', {'width': 2, 'depth': 2, 'height': 2, 'x': 20}), [])
        apart = distances.surface_distance(a, far)
        self.assertAlmostEqual(apart['exact_minimum_distance_mm']['value'], 10.0, places=9)
        self.assertEqual(apart['volume_iou']['value'], 0.0)

    def test_wall_thickness_is_sampled_and_finds_thin_walls(self):
        outer = make_feature(feature('o', 'box', {'width': 10, 'depth': 10, 'height': 4}), [])
        inner = make_feature(feature('i', 'box', {'width': 8, 'depth': 8, 'height': 2, 'x': 1, 'y': 1, 'z': 1}), [])
        hollow = boolean('difference', outer, inner)
        report = distances.wall_thickness(hollow)
        self.assertEqual(report['guarantee'], 'sampled')
        self.assertAlmostEqual(report['minimum_mm'], 1.0, places=6)
        self.assertGreater(report['samples'], 100)
        solid = distances.wall_thickness(outer)
        self.assertAlmostEqual(solid['minimum_mm'], 4.0, places=6)
        with self.assertRaises(GeometryError):
            distances.wall_thickness(make_feature(feature('p', 'profile', points=[['0', '0', '0'], ['1', '0', '0'], ['1', '1', '0']]), []))

    def test_motion_clearance_samples_a_linear_path_without_sweep_claims(self):
        base = make_feature(feature('a', 'box', {'width': 10, 'depth': 10, 'height': 4}), [])
        mover = make_feature(feature('m', 'box', {'width': 2, 'depth': 2, 'height': 2, 'x': 12, 'z': 1}), [])
        safe = distances.motion_clearance(base, mover, [0, 0, 5], 4, 1.0)
        self.assertEqual(safe['guarantee'], 'sampled')
        self.assertTrue(safe['minimum_satisfied_at_samples'])
        self.assertAlmostEqual(safe['minimum_sampled_clearance_mm'], 2.0, places=9)
        collide = distances.motion_clearance(base, mover, [-4, 0, 0], 8, 0.5)
        self.assertTrue(collide['collision_sampled'])
        self.assertFalse(collide['minimum_satisfied_at_samples'])
        self.assertEqual(len(collide['samples']), 9)
        with self.assertRaises(GeometryError):
            distances.motion_clearance(base, mover, [1, 0, 0], 65)


if __name__ == '__main__':
    unittest.main()
