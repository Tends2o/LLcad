"""Sparse field cost scales with the surface band (Bauplan 15.4), not with the dense volume."""
import sys, unittest, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'workers/cad-occt'))
from fields import extract
from field_cache import Sampler

SPHERE = {'op': 'sphere', 'center': ['0', '0', '0'], 'radius': '5'}


def field(cell):
    return {'id': 'f', 'cache_key': 'k', 'construction': {'operator': 'field', 'expression': SPHERE, 'domain': {'min': [-8, -8, -8], 'max': [8, 8, 8]}, 'cell_size': cell},
            'field': {'semantics': 'exact_sdf', 'lipschitz': 1.0, 'cell_size': cell, 'value_unit': 'length'}}


class ScalingTests(unittest.TestCase):
    def test_active_cells_grow_like_the_band_area_not_the_volume(self):
        counts = {}
        for cell in (1.0, 0.5, 0.25):
            result = extract(field(cell), Sampler(SPHERE, 'registry'))
            counts[cell] = result['active_cells']
            self.assertGreater(result['pruned_cells'], 0)
        ratio_coarse = counts[0.5] / counts[1.0]
        ratio_fine = counts[0.25] / counts[0.5]
        # Halving h multiplies dense grid cells by 8; a surface band multiplies by about 4.
        self.assertLess(ratio_fine, 6.0, counts)
        self.assertGreater(ratio_fine, 2.5, counts)
        self.assertLess(ratio_coarse, 7.0, counts)

    def test_physical_band_versus_cell_relative_band(self):
        # A band of fixed physical width w costs ~ A*w/h^3 cells; a band of k cells costs ~ k*A/h^2.
        area = 4 * math.pi * 25
        for h in (1.0, 0.5, 0.25):
            physical = area * 2.0 / h ** 3
            relative = 2 * area / h ** 2
            self.assertGreater(physical / relative, 1.0 / h - 1e-9)


if __name__ == '__main__':
    unittest.main()
