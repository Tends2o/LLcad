"""CSG regularisation, empty results and low-dimensional leftovers (Bauplan 6.7)."""
import sys, unittest, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'workers/cad-occt'))
from geometry import *
import topology


def feature(fid, op, p=None, **c):
    return {'id': fid, 'values': p or {}, 'construction': {'operator': op, **c}}


def box(fid, w, d, h, **pos):
    return make_feature(feature(fid, 'box', {'width': w, 'depth': d, 'height': h, **pos}), [])


class RegularizationTests(unittest.TestCase):
    def test_difference_removing_everything_is_an_explicit_null_result(self):
        a = box('a', 4, 4, 4)
        empty = boolean('difference', a, box('b', 6, 6, 6, x=-1, y=-1, z=-1))
        self.assertEqual(sum(1 for _ in explore(empty, TopAbs_SOLID)), 0)
        with self.assertRaises(GeometryError) as caught:
            properties(empty)
        self.assertEqual(caught.exception.code, 'GEOMETRY_INVALID')

    def test_face_contact_intersection_is_not_a_solid(self):
        a = box('a', 4, 4, 4)
        b = box('b', 4, 4, 4, x=4)
        contact = boolean('intersection', a, b)
        self.assertEqual(sum(1 for _ in explore(contact, TopAbs_SOLID)), 0)
        # Low-dimensional leftovers (a shared face or nothing) never pass as volume.
        with self.assertRaises(GeometryError):
            properties(contact)
        thin = boolean('intersection', a, box('c', 4, 4, 4, x=3.999999))
        self.assertLess(properties(thin)['volume'], 1e-3)

    def test_union_of_touching_boxes_regularizes_into_one_solid_with_merged_faces(self):
        a = box('a', 4, 4, 4)
        b = box('b', 4, 4, 4, x=4)
        fused = boolean('union', a, b)
        before = len(list(explore(fused, TopAbs_FACE)))
        regular = make_feature(feature('reg', 'regularize'), [fused])
        after = len(list(explore(regular, TopAbs_FACE)))
        self.assertAlmostEqual(properties(regular)['volume'], 128)
        self.assertEqual(properties(regular)['solids'], 1)
        self.assertLess(after, before)
        self.assertEqual(after, 6)

    def test_csg_identities_hold_only_for_regular_solids(self):
        a = box('a', 4, 4, 4)
        b = box('b', 4, 4, 4, x=2)
        union = properties(boolean('union', a, b))['volume']
        common = properties(boolean('intersection', a, b))['volume']
        self.assertAlmostEqual(union + common, 64 + 64, places=9)
        self.assertAlmostEqual(properties(boolean('difference', a, b))['volume'] + common, 64, places=9)
        self.assertAlmostEqual(properties(boolean('union', a, a))['volume'], 64, places=9)


if __name__ == '__main__':
    unittest.main()
