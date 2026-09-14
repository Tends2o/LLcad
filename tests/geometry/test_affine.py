import sys,unittest,math,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from geometry import *
from fields import evaluate,gradient,rotation_matrix
from field_cache import Sampler
import topology
from analysis import sample
import numpy as np

class AffineTests(unittest.TestCase):
    def test_native_shear_reflection_normals_winding_and_inverse(self):
        base={'id':'box','values':{'width':2,'depth':3,'height':4},'construction':{'operator':'box'}}
        shape,trace=topology.evaluate_feature(base,[],[])
        matrix=np.array([[-2,1,.4],[0,1,.3],[0,0,1]])
        c={'operator':'affine_transform','matrix':[[str(x) for x in row] for row in matrix],'translation':['10','0','0']}
        transformed,history=topology.evaluate_feature({'id':'changed','values':{},'construction':c},[shape],[trace])
        self.assertAlmostEqual(properties(transformed)['volume'],48,places=10)
        self.assertEqual(sum(bool(f['origins']) for f in history),6)
        side=next(i for i,f in enumerate(history) if any(o['role']=='x_max' for o in f['origins']))
        normal=np.array(sample(transformed,{'face_index':side})['direction'])
        expected=np.linalg.inv(matrix).T@np.array([1,0,0]);expected/=np.linalg.norm(expected)
        self.assertLess(float(np.linalg.norm(normal-expected)),1e-12)
        triangulation=mesh(transformed,.05)
        center=matrix@np.array([1,1.5,2])+[10,0,0]
        for indices in triangulation['triangles']:
            a,b,c=(np.array(triangulation['vertices'][i]) for i in indices)
            self.assertGreater(np.dot(np.cross(b-a,c-a),(a+b+c)/3-center),0)
        inv=np.linalg.inv(matrix);translation=-inv@np.array([10,0,0])
        c={'operator':'affine_transform','matrix':[[str(x) for x in row] for row in inv],'translation':[str(x) for x in translation]}
        source_hash=shape_hash(transformed)
        restored=make_feature({'values':{},'construction':c},[transformed])
        self.assertEqual(shape_hash(transformed),source_hash)
        self.assertAlmostEqual(properties(restored)['volume'],24,places=10)
        self.assertLess(max(abs(a-b) for a,b in zip(bounds(shape),bounds(restored))),1e-6)

    def test_axis_angle_roundtrip_and_oriented_plane(self):
        base=make_feature({'values':{'width':2,'depth':3,'height':4},'construction':{'operator':'box'}},[])
        c={'operator':'rotate','axis':['1','1','1'],'origin':['3','-2','5']}
        rotated=make_feature({'values':{'angle':math.pi/3},'construction':c},[base])
        restored=make_feature({'values':{'angle':-math.pi/3},'construction':c},[rotated])
        self.assertAlmostEqual(properties(rotated)['volume'],24,places=10)
        self.assertLess(max(abs(a-b) for a,b in zip(bounds(base),bounds(restored))),1e-12)
        r=rotation_matrix(('1','1','1'),'60','deg')
        self.assertLess(float(np.linalg.norm(r.T@r-np.eye(3))),1e-14)
        self.assertAlmostEqual(float(np.linalg.det(r)),1,places=14)
        plane={'op':'plane','normal':['1','0','0'],'offset':'2'}
        node={'op':'rotate','source':plane,'axis':['0','0','1'],'origin':['0','0','0'],'angle':{'value':'90','unit':'deg'}}
        self.assertAlmostEqual(evaluate(node,[0,3,0]),1,places=14)
        self.assertLess(float(np.linalg.norm(gradient(node,[0,3,0])-[0,1,0])),1e-9)

    def test_affine_field_is_conservative_and_cache_support_uses_inverse_coordinates(self):
        source={'op':'sphere','center':['0','0','0'],'radius':'2'}
        matrix=np.array([[2,1,0],[0,1,0],[0,0,1]])
        node={'op':'affine_transform','source':source,'matrix':[[str(x) for x in row] for row in matrix],'translation':['1000','0','0']}
        self.assertAlmostEqual(evaluate(node,matrix@np.array([2,0,0])+[1000,0,0]),0,places=14)
        for point in [[1005,1,1],[998,-3,2],[1001,4,-2]]:
            self.assertLessEqual(float(np.linalg.norm(gradient(node,point))),1+1e-8)
        delta={'op':'local_field_delta','source':source,'center':['2','0','0'],'radius':'1','amplitude':'0.1'}
        for transform,inside,outside in [
            (dict(node,source=delta),[1004,0,0],[1000,3,0]),
            ({'op':'rotate','source':delta,'axis':['0','0','1'],'origin':['0','0','0'],'angle':{'value':'90','unit':'deg'}},[0,2,0],[0,-3,0])]:
            first=Sampler(transform,'test');a=first(inside);outside_value=first(outside)
            changed=copy.deepcopy(transform);changed['source']['amplitude']='0.2'
            second=Sampler(changed,'test',first.serialize())
            self.assertNotEqual(second(inside),a);self.assertEqual(second(inside),evaluate(changed,inside))
            self.assertEqual(second(outside),outside_value)
            self.assertEqual(second.misses,1);self.assertEqual(second.hits,2)

if __name__=='__main__':unittest.main()
