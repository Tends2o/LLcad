import copy, math, sys, unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from fields import compile_field, evaluate, gradient, extract
from field_cache import Sampler
from geometry import GeometryError

class CompiledFieldTests(unittest.TestCase):
    def test_every_registered_operator_and_nested_combinations_match_reference(self):
        sphere={'op':'sphere','center':['0.1','-0.2','0.3'],'radius':'2'}
        primitives=[sphere,
            {'op':'plane','normal':['1','2','-1'],'offset':'0.4'},
            {'op':'box','center':['1','0','0'],'half_size':['1','2','3']},
            {'op':'torus','center':['0','1','0'],'major':'2','minor':'0.5'},
            {'op':'cylinder','center':['0','0','1'],'radius':'1','half_height':'2'},
            {'op':'capsule','start':['-1','0','0'],'end':['1','2','0'],'radius':'0.7'},
            {'op':'capsule','start':['1','0','0'],'end':['1','0','0'],'radius':'0.7'},
            {'op':'gyroid','period':'3','origin':['0.1','0.2','-0.3'],'threshold':'0.2'}]
        expressions=primitives+[
            {'op':op,'a':sphere,'b':primitives[3],**({'k':'0.6'} if op=='smooth_union' else {})}
            for op in ['union','intersection','difference','smooth_union']]
        expressions += [
            {'op':'transform','source':expressions[9],'translation':['1','-2','0.1'],'scale':['2','0.5','1.5']},
            {'op':'affine_transform','source':expressions[10],'translation':['2','1','0'],'matrix':[['-2','0.2','0'],['0','1','0.3'],['0','0','1.5']]},
            {'op':'rotate','source':primitives[2],'axis':['1','2','3'],'origin':['0.3','0.4','0.5'],'angle':{'value':'32.4','unit':'deg'}},
            {'op':'local_deform','source':sphere,'center':['1','0','0'],'radius':'2','displacement':['0.1','0.05','-0.04']},
            {'op':'offset','source':sphere,'distance':'0.1'},
            {'op':'shell','source':sphere,'thickness':'0.4','variations':[{'center':['2','0','0'],'radius':'1','amplitude':'0.1'}]},
            {'op':'local_field_delta','source':sphere,'center':['2','0','0'],'radius':'1','amplitude':'-0.05'},
            {'op':'convert_field_unit','source':primitives[-1],'to':'length','reference_length':{'value':'0.0005','unit':'m'}},
            {'op':'convert_field_unit','source':sphere,'to':'dimensionless','reference_length':{'value':'500','unit':'um'}}]
        # Fixed seed and points on either side of compact-support and CSG boundaries.
        points=[*np.random.default_rng(411).uniform(-4,4,(80,3)),[1,0,0],[3,0,0],[3-1e-9,0,0],[3+1e-9,0,0],[0,0,0]]
        for node in expressions:
            with self.subTest(op=node['op']):
                compiled=compile_field(node)
                for point in points:
                    self.assertAlmostEqual(compiled(point),evaluate(node,point),places=12)
                for point in points[:3]:np.testing.assert_allclose(compiled.gradient(point),gradient(node,point),rtol=1e-8,atol=1e-9)
        combined={'op':'smooth_union','a':expressions[13],'b':expressions[14],'k':'0.3'}
        compiled=compile_field(combined)
        for point in points:self.assertAlmostEqual(compiled(point),evaluate(combined,point),places=12)

    def test_compilation_snapshot_and_sampler_do_not_follow_mutable_input(self):
        node={'op':'sphere','center':['0','0','0'],'radius':'2'}
        compiled=compile_field(node);sampler=Sampler(node,'build')
        node['center'][0]='100';node['radius']='200'
        self.assertEqual(compiled([3,0,0]),1)
        self.assertEqual(sampler([3,0,0]),1)
        self.assertEqual(sampler([4,0,0]),2)
        self.assertEqual(sampler.metrics()['compiled_nodes'],1)
        self.assertGreaterEqual(sampler.metrics()['compile_seconds'],0)
        # No recursive evaluator may be invoked in the production compiled path.
        with patch('fields.evaluate',side_effect=AssertionError('reparsed AST')):
            self.assertEqual(compiled([5,0,0]),3)
            np.testing.assert_allclose(compiled.gradient([5,0,0]),[1,0,0],atol=1e-9)

    def test_depth_node_and_unknown_operator_guards(self):
        sphere={'op':'sphere','center':['0','0','0'],'radius':'2'}
        node=sphere
        for _ in range(32):node={'op':'offset','source':node,'distance':'0.1'}
        self.assertEqual(compile_field(node).max_depth,32)
        for invalid in [{'op':'offset','source':node,'distance':'0.1'}]:
            with self.assertRaises(GeometryError) as caught:compile_field(invalid)
            self.assertEqual(caught.exception.code,'BUDGET_EXCEEDED')
        node=sphere
        for _ in range(12):node={'op':'union','a':node,'b':node}
        with self.assertRaises(GeometryError) as caught:compile_field(node)
        self.assertEqual(caught.exception.code,'BUDGET_EXCEEDED')
        with self.assertRaises(GeometryError) as caught:compile_field({'op':'python','source':sphere,'code':'raise Exception()'})
        self.assertEqual(caught.exception.code,'OUT_OF_SCOPE')

    def test_extracted_mesh_uses_compiled_gradients_and_outward_normals(self):
        node={'op':'sphere','center':['0','0','0'],'radius':'1'}
        feature={'id':'f','construction':{'expression':node,'domain':{'min':['-1.5']*3,'max':['1.5']*3}},'field':{'cell_size':.5,'lipschitz':1,'semantics':'exact_sdf'}}
        with patch('fields.evaluate',side_effect=AssertionError('reparsed AST')):
            result=extract(feature,Sampler(node,'build'))
        vertices=np.array(result['vertices']);triangles=result['triangles']
        self.assertGreater(len(triangles),0)
        for indices in triangles:
            a,b,c=vertices[indices]
            self.assertGreater(float(np.dot(np.cross(b-a,c-a),(a+b+c)/3)),0)
        self.assertLess(max(abs(float(np.linalg.norm(v))-1) for v in vertices),.08)

if __name__=='__main__':unittest.main()
