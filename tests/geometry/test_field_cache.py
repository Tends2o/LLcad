import sys, unittest, tempfile
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
