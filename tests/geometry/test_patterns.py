import sys,math,copy,tempfile,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from geometry import *
import topology

def center(shape):
    p=GProp_GProps();BRepGProp.VolumeProperties_s(shape,p)
    v=p.CentreOfMass();return np.array([v.X(),v.Y(),v.Z()])

class PatternTests(unittest.TestCase):
    def test_compound_keeps_root_face_and_curve_history_without_mutating_sources(self):
        fixtures=[
            {'id':'plane','values':{'width':2,'height':3},'construction':{'operator':'plane'}},
            {'id':'line','values':{},'construction':{'operator':'line','start':['0','0','0'],'end':['2','1','3']}},
            {'id':'box','values':{'width':2,'depth':3,'height':4},'construction':{'operator':'box'}},
        ]
        for f in fixtures:
            source,trace=topology.evaluate_feature(f,[],[])
            if f['id']=='box':
                source.Reverse()
                # Rebuild the oriented native entries without guessing owners.
                trace=[dict(e,shape=face) for e,face in zip(trace,topology.face_list(source))]
            old=shape_hash(source)
            pattern={'id':'many','depends_on':[f['id']],'values':{'count':3,'dx':5},'construction':{'operator':'pattern'}}
            output,history=topology.evaluate_feature(pattern,[source],[trace])
            self.assertEqual(shape_hash(source),old)
            self.assertTrue(history and all(e['origins'] for e in history))
            with tempfile.TemporaryDirectory() as directory:
                path=str(Path(directory)/'shape.brep')
                restored,_,records=topology.archive(output,history,'key',path)
                topology.restore(restored,records,'key',hashlib.sha256(Path(path).read_bytes()).hexdigest())

    def test_circular_placements_against_rodrigues_and_shared_native_geometry(self):
        shape,trace=topology.evaluate_feature({'id':'source','values':{'width':2,'depth':3,'height':4,'x':10},'construction':{'operator':'box'}},[],[])
        mesh(shape,.05);source_hash=shape_hash(shape)
        rng=np.random.default_rng(519)
        for count,span in [(1,2*math.pi),(4,2*math.pi),(7,-math.pi),(3,math.pi/2)]:
            axis=rng.normal(size=3);origin=rng.normal(size=3);unit=axis/np.linalg.norm(axis)
            f={'id':'pattern','depends_on':['source'],'values':{'count':count,'angle':span},'construction':{
                'operator':'circular_pattern','axis':list(map(str,axis)),'origin':list(map(str,origin))}}
            result,history=topology.evaluate_feature(f,[shape],[trace])
            solids=list(explore(result,TopAbs_SOLID))
            self.assertEqual(len(solids),count)
            self.assertAlmostEqual(properties(result)['volume'],24*count,places=9)
            for i,solid in enumerate(solids):
                v=center(shape)-origin;a=span*i/count
                expected=origin+v*math.cos(a)+np.cross(unit,v)*math.sin(a)+unit*np.dot(unit,v)*(1-math.cos(a))
                self.assertLess(float(np.linalg.norm(center(solid)-expected)),1e-10)
                # Only the outer occurrence container owns its own lock;
                # native shells, faces, surfaces and triangulation stay shared.
                self.assertTrue(TopoDS_Iterator(solid,False,False).Value().IsPartner(TopoDS_Iterator(shape,False,False).Value()))
            records=topology.serialize(history,'test')
            self.assertEqual(len({o['key'] for face in records['faces'] for o in face['origins']}),6*count)
            self.assertEqual(shape_hash(shape),source_hash)
            with tempfile.TemporaryDirectory() as directory:
                path=str(Path(directory)/'pattern.brep')
                _,_,records=topology.archive(result,history,'test',path)
                checksum=hashlib.sha256(Path(path).read_bytes()).hexdigest()
                for _ in range(3):
                    self.assertEqual(len(topology.restore(read_brep(path),records,'test',checksum)),6*count)
                with self.assertRaises(GeometryError):topology.restore(read_brep(path),records,'test','0'*64)

    def test_one_variant_retains_other_occurrence_fingerprints_and_history(self):
        base={'id':'source','values':{'width':2,'depth':3,'height':4,'x':10},'construction':{'operator':'box'}}
        shape,trace=topology.evaluate_feature(base,[],[])
        variant=copy.deepcopy(base);variant['id']='variant';variant['values']['width']=3
        other,other_trace=topology.evaluate_feature(variant,[],[])
        for op in ['pattern','circular_pattern']:
            f={'id':'many','depends_on':['source'],'values':{'count':4,'dx':5,'angle':2*math.pi},'construction':{'operator':op,'axis':['0','0','1'],'origin':['0','0','0']}}
            before,history=topology.evaluate_feature(f,[shape],[trace])
            changed=copy.deepcopy(f);changed['depends_on'].append('variant')
            changed['construction']['overrides']=[{'index':2,'source':'variant','translation':['0','0','1']}]
            after,new_history=topology.evaluate_feature(changed,[shape,other],[trace,other_trace])
            old_solids=list(explore(before,TopAbs_SOLID));new_solids=list(explore(after,TopAbs_SOLID))
            for index in [0,1,3]:
                self.assertEqual(shape_hash(old_solids[index]),shape_hash(new_solids[index]))
                self.assertEqual([f['origins'] for f in history[index*6:(index+1)*6]],
                                 [f['origins'] for f in new_history[index*6:(index+1)*6]])
            self.assertAlmostEqual(properties(after)['volume'],3*24+36,places=9)
            shell=TopoDS_Iterator(new_solids[2],False,False).Value()
            self.assertTrue(shell.IsPartner(TopoDS_Iterator(other,False,False).Value()))
            self.assertFalse(shell.IsPartner(TopoDS_Iterator(shape,False,False).Value()))

if __name__=='__main__':unittest.main()
