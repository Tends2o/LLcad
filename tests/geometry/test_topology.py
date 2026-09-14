import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'workers/cad-occt'))
import topology as t
from geometry import *


def feature(fid, operator, values=None, **construction):
    return {'id': fid, 'values': values or {}, 'construction': {'operator': operator, **construction}}


class TopologyTests(unittest.TestCase):
    def test_registered_history_preserves_the_existing_geometry_contract(self):
        base = feature('base', 'box', {'width': 10, 'depth': 10, 'height': 10})
        shape, trace = t.evaluate_feature(base, [], [])
        cases = [
            (feature('sphere', 'sphere', {'radius': 2}), [], []),
            (feature('cylinder', 'cylinder', {'radius': 2, 'height': 4}), [], []),
            (feature('cone', 'cone', {'radius': 2, 'top_radius': 0, 'height': 4}), [], []),
            (feature('torus', 'torus', {'major_radius': 4, 'minor_radius': 1}), [], []),
            (feature('one', 'instance', {'x': 20, 'angle': .5, 'scale': 2}), [shape], [trace]),
            (feature('mirror', 'mirror'), [shape], [trace]),
            (feature('many', 'pattern', {'count': 4, 'dx': 12}), [shape], [trace]),
            (feature('hole', 'hole', {'radius': 1, 'depth': 3, 'x': 5, 'y': 5, 'z': 10}), [shape], [trace]),
        ]
        for f, deps, histories in cases:
            with self.subTest(operator=f['construction']['operator']):
                traced, history = t.evaluate_feature(f, deps, histories)
                old = make_feature(f, deps)
                self.assertAlmostEqual(properties(traced)['volume'], properties(old)['volume'], places=7)
                self.assertEqual(bounds(traced), bounds(old))
                self.assertTrue(all(x['origins'] for x in history))
                if f['id'] in ('one', 'many', 'mirror'):
                    self.assertTrue(all(o['feature_id'] == f['id'] for x in history for o in x['origins']))

    def test_unknown_history_is_never_replaced_with_a_geometric_guess(self):
        base = feature('base', 'box', {'width': 10, 'depth': 10, 'height': 10})
        shape, trace = t.evaluate_feature(base, [], [])
        f = feature('round', 'fillet', {'radius': 1}, edge_selector='vertical')
        rounded, history = t.evaluate_feature(f, [shape], [trace])
        self.assertTrue(properties(rounded)['valid'])
        self.assertTrue(history)
        self.assertTrue(all(x['origins'] for x in history))
        unknown = [{'shape': x['shape'], 'origins': []} for x in trace]
        result = t.propagate(shape, [trace, unknown], 'mixed')
        self.assertTrue(all(not x['origins'] for x in result))

    def test_cache_checks_each_face_and_patterns_keep_separate_occurrences(self):
        base = feature('base', 'box', {'width': 2, 'depth': 3, 'height': 4})
        shape, trace = t.evaluate_feature(base, [], [])
        shape, trace = t.evaluate_feature(feature('many', 'pattern', {'count': 3, 'dx': 5}), [shape], [trace])
        records = t.serialize(trace, 'key')
        self.assertEqual(len({x['origins'][0]['key'] for x in records['faces']}), 18)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'shape.brep')
            write_brep(shape, path)
            restored = read_brep(path)
            self.assertEqual(len(t.restore(restored, records, 'key')), 18)
            records['faces'][0], records['faces'][1] = records['faces'][1], records['faces'][0]
            with self.assertRaises(GeometryError) as error:
                t.restore(restored, records, 'key')
            self.assertEqual(error.exception.code, 'INTEGRITY_FAILURE')


if __name__ == '__main__':
    unittest.main()
