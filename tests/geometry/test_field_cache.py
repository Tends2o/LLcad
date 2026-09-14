import sys, unittest, tempfile, copy
import numpy as np
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from field_cache import Sampler
from fields import evaluate
from geometry import GeometryError
from volume_io import export_vdb

class FieldCacheTests(unittest.TestCase):
    def test_cross_resolution_and_compact_support_and_registry_binding(self):
        source={'op':'sphere','center':['0','0','0'],'radius':'2'}
        old=Sampler(source,'build-a')
        for x in [-3,-2,-1,0,1,2,3]:old([x,0,0])
        changed={'op':'local_field_delta','source':source,'center':['2','0','0'],'radius':'1','amplitude':'0.2'}
        new=Sampler(changed,'build-a',old.serialize())
        for x in [-3,-2.5,-2,-1,0,1,2,3]:self.assertEqual(new([x,0,0]),evaluate(changed,[x,0,0]))
        self.assertEqual((new.hits,new.misses),(6,2))
        invalid=Sampler(source,'build-b',old.serialize());invalid([2,0,0]);self.assertEqual(invalid.hits,0)
        deformation={'op':'local_deform','source':changed,'center':['2','0','0'],'radius':'2','displacement':['0.1','0','0']}
        transformed=Sampler(deformation,'build-a',new.serialize())
        for x in [1,2,2.9,3.1,4]:self.assertEqual(transformed([x,0,0]),evaluate(deformation,[x,0,0]))

    def test_vdb_grid_budget_rejects_before_allocation(self):
        f={'id':'large','construction':{'domain':{'min':['0','0','0'],'max':['100','100','100']}},'field':{'cell_size':.1,'lipschitz':1,'semantics':'exact_sdf'}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(GeometryError) as caught:export_vdb(f,str(Path(directory)/'bad.vdb'),lambda _:0)
            self.assertEqual(caught.exception.code,'BUDGET_EXCEEDED')

    def test_nested_transforms_compact_edits_and_repeated_samples(self):
        sphere={'op':'sphere','center':['0','0','0'],'radius':'2'}
        delta={'op':'local_field_delta','source':sphere,'center':['1','0','0'],'radius':'2','amplitude':'0.2'}
        transform={'op':'affine_transform','source':delta,'matrix':[['1','0.2','0'],['0','-1','0'],['0','0','1']],'translation':['2','0','0']}
        rotate={'op':'rotate','source':transform,'axis':['0','0','1'],'origin':['1','0','0'],'angle':{'value':'90','unit':'deg'}}
        shape={'op':'smooth_union','a':rotate,'b':{'op':'transform','source':delta,'translation':['-2','0','0'],'scale':['1','2','1']},'k':'0.5'}
        changed=copy.deepcopy(shape)
        changed['a']['source']['source']['amplitude']='-0.1'
        changed['b']['source']['amplitude']='0.4'
        points=[*np.random.default_rng(752).uniform(-5,5,(400,3)),[1,1,0],[-1,0,0]]
        previous=Sampler(shape,'build')
        for point in points:previous(point)
        current=Sampler(changed,'build',previous.serialize())
        for point in points:
            value=current(point)
            self.assertAlmostEqual(value,evaluate(changed,point),places=12)
            # Same-worker repeated samples must avoid both formula and key work.
            self.assertEqual(value,current(point))
        self.assertGreater(current.misses,0)
        self.assertGreater(current.hits,len(points))
        self.assertEqual(len(current.used),len(points))
        # Older key formats must never be silently interpreted as current entries.
        old=current.serialize();old['version']=1
        cold=Sampler(changed,'build',old);cold(points[0]);self.assertEqual(cold.hits,0)
        bad=current.serialize();bad['values']={key:float('nan') for key in bad['values']}
        with self.assertRaises(GeometryError):Sampler(changed,'build',bad)(points[0])

    def test_compact_deformation_retains_relevance_of_shifted_source(self):
        sphere={'op':'sphere','center':['0','0','0'],'radius':'1'}
        delta={'op':'local_field_delta','source':sphere,'center':['1','0','0'],'radius':'0.1','amplitude':'0.01'}
        node={'op':'local_deform','source':delta,'center':['1','0','0'],'radius':'3','displacement':['0.2','0','0']}
        before=Sampler(node,'build');first=before([1.18,0,0]);far=before([5,0,0])
        changed=copy.deepcopy(node);changed['source']['amplitude']='0.02'
        after=Sampler(changed,'build',before.serialize())
        self.assertNotEqual(after([1.18,0,0]),first)
        self.assertAlmostEqual(after([1.18,0,0]),evaluate(changed,[1.18,0,0]),places=12)
        self.assertEqual(after([5,0,0]),far)
        self.assertEqual(after.misses,1)
