import sys, unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'workers/cad-occt'))
from mesh_ops import repair, remesh_region, arap_deform, fit_primitives
from mesh_quality import inspect_mesh, index_exact
from geometry import make_feature, mesh, GeometryError
from fields import extract

CUBE_V = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]]
CUBE_T = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]


def sphere_mesh(cell=1.0):
    f = {'id': 's', 'construction': {'expression': {'op': 'sphere', 'center': ['0', '0', '0'], 'radius': '5'}, 'domain': {'min': ['-6'] * 3, 'max': ['6'] * 3}},
         'field': {'cell_size': cell, 'lipschitz': 1, 'semantics': 'exact_sdf'}}
    return extract(f)


class MeshOperatorTests(unittest.TestCase):
    def test_repair_reports_every_change_and_yields_a_watertight_body(self):
        fixed, report = repair({'vertices': CUBE_V, 'triangles': CUBE_T[:-1]}, {'fill_holes_max_edges': 8})
        self.assertEqual(report['filled_holes'], 1)
        self.assertAlmostEqual(report['filled_hole_area_mm2'], 0.5)
        self.assertEqual(report['added_vertices'], 1)
        self.assertTrue(inspect_mesh(fixed)['watertight_solid'])
        messy = {'vertices': CUBE_V + [[0, 0, 0]], 'triangles': CUBE_T + [CUBE_T[0]] + [[0, 0, 1]] + [[1, 2, 0]]}
        fixed2, report2 = repair(messy, {'weld_tolerance': 0.001})
        self.assertEqual((report2['welded_vertices'], report2['removed_degenerate_triangles'], report2['removed_duplicate_triangles']), (1, 1, 2))
        self.assertTrue(inspect_mesh(fixed2)['watertight_solid'])
        reversed_cube = {'vertices': CUBE_V, 'triangles': [t[::-1] for t in CUBE_T]}
        fixed3, report3 = repair(reversed_cube, {})
        self.assertEqual(report3['flipped_triangles'], 12)
        self.assertTrue(inspect_mesh(fixed3)['native']['nested_orientation_valid'])
        skipped, report4 = repair({'vertices': CUBE_V, 'triangles': CUBE_T[:-1]}, {'fill_holes_max_edges': 2})
        self.assertEqual((report4['filled_holes'], report4['skipped_holes']), (0, 1))
        self.assertFalse(inspect_mesh(skipped)['watertight_solid'])

    def test_region_remeshing_changes_only_the_region_and_stays_valid(self):
        sphere = sphere_mesh()
        result, report = remesh_region(sphere, [5, 0, 0], 2.5, 0.5, 3)
        quality = inspect_mesh(result)
        self.assertTrue(quality['watertight_solid'], quality['checks'])
        self.assertGreater(report['splits'] + report['collapses'], 0)
        self.assertLess(report['region_edge_length_mm']['max'], 1.2)
        original = np.array(sphere['vertices'])
        outside = original[np.linalg.norm(original - [5, 0, 0], axis=1) >= 2.5]
        kept = np.array(result['vertices'])
        for p in outside[::7]:
            self.assertLess(np.min(np.linalg.norm(kept - p, axis=1)), 1e-12)
        with self.assertRaises(GeometryError):
            remesh_region(sphere, [5, 0, 0], 2.5, 0.5, 0)

    def test_arap_pins_handles_fixes_the_exterior_and_reports_energy(self):
        sphere = sphere_mesh()
        result, report = arap_deform(sphere, [5, 0, 0], 3.0, [{'point': [5, 0, 0], 'displacement': [0.5, 0, 0]}], 5)
        self.assertAlmostEqual(report['max_displacement_mm'], 0.5, places=9)
        self.assertEqual(report['fixed_vertices'] + report['free_vertices'] + report['handles'], len(sphere['vertices']))
        self.assertLess(report['energy_final'], report['energy_initial'])
        self.assertIsNone(report['collision_certificate'])
        original = np.array(sphere['vertices'])
        moved = np.array(result['vertices'])
        far = np.linalg.norm(original - [5, 0, 0], axis=1) >= 3.0
        np.testing.assert_array_equal(moved[far], original[far])
        self.assertTrue(inspect_mesh(result)['watertight_solid'])
        with self.assertRaises(GeometryError) as caught:
            arap_deform(sphere, [5, 0, 0], 1.0, [{'point': [-5, 0, 0], 'displacement': [0.1, 0, 0]}], 2)
        self.assertEqual(caught.exception.code, 'OUT_OF_SCOPE')

    def test_primitive_and_symmetry_hypotheses_carry_residuals_and_coverage(self):
        cylinder = make_feature({'id': 'c', 'values': {'radius': 3, 'height': 8}, 'construction': {'operator': 'cylinder'}}, [])
        report = fit_primitives(index_exact(mesh(cylinder, 0.02)), min_triangles=2)
        kinds = [h['kind'] for h in report['hypotheses'][:3]]
        self.assertEqual(kinds.count('plane'), 2)
        cyl = next(h for h in report['hypotheses'] if h['kind'] == 'cylinder')
        self.assertAlmostEqual(cyl['radius_mm'], 3.0, places=6)
        self.assertLess(cyl['rms_residual_mm'], 1e-6)
        self.assertEqual(cyl['status'], 'hypothesis')
        self.assertEqual(cyl['confidence'], 'not_calibrated')
        self.assertAlmostEqual(report['coverage_area_fraction'], 1.0, places=6)
        self.assertEqual(report['construction_history'], 'unknown_not_reconstructed')
        self.assertTrue(all(s['sampled_max_mm'] < 1e-9 for s in report['symmetry_hypotheses']))
        box = make_feature({'id': 'b', 'values': {'width': 2, 'depth': 3, 'height': 4}, 'construction': {'operator': 'box'}}, [])
        boxed = index_exact(mesh(box, 0.1))
        self.assertTrue(inspect_mesh(boxed)['watertight_solid'])
        planes = fit_primitives(boxed, min_triangles=2)['hypotheses']
        self.assertEqual([h['kind'] for h in planes], ['plane'] * 6)


if __name__ == '__main__':
    unittest.main()
