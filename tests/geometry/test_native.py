import unittest,sys,math,tempfile,random
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from geometry import *
from fields import evaluate,wendland,gradient,extract
import numpy as np

def feature(op,p=None,extra=None):
    return {'id':op,'values':p or {},'construction':{'operator':op,**(extra or {})}}
def make(op,p=None,deps=None,extra=None):return make_feature(feature(op,p,extra),deps or [])

class NativeGeometryTests(unittest.TestCase):
    def test_analytic_primitives(self):
        cases=[('box',{'width':2,'depth':3,'height':4},24),('sphere',{'radius':2},4/3*math.pi*8),('cylinder',{'radius':2,'height':3},12*math.pi),('cone',{'radius':2,'top_radius':0,'height':3},4*math.pi),('torus',{'major_radius':4,'minor_radius':1},8*math.pi**2)]
        for op,p,volume in cases:
            with self.subTest(op=op):
                props=properties(make(op,p));self.assertTrue(props['valid']);self.assertAlmostEqual(props['volume'],volume,places=6)
    def test_boolean_identities(self):
        s=make('box',{'width':2,'depth':3,'height':4})
        self.assertAlmostEqual(properties(boolean('union',s,s))['volume'],24)
        self.assertAlmostEqual(properties(boolean('intersection',s,s))['volume'],24)
        self.assertEqual(sum(1 for _ in explore(boolean('difference',s,s),TopAbs_SOLID)),0)
    def test_profiles_extrusion_revolution_loft_sweep(self):
        square=make('profile',extra={'points':[['0','0','0'],['2','0','0'],['2','3','0'],['0','3','0']]})
        self.assertAlmostEqual(properties(make('extrude',{'height':4},[square]))['volume'],24)
        radial=make('profile',extra={'points':[['2','0','0'],['4','0','0'],['4','0','3'],['2','0','3']]})
        self.assertAlmostEqual(properties(make('revolve',{'angle':2*math.pi},[radial]))['volume'],36*math.pi,places=6)
        a=make('circle',{'radius':2});b=make('circle',{'radius':1,'z':4})
        self.assertTrue(properties(make('loft',deps=[a,b]))['valid'])
        path=make('bspline',extra={'points':[['0','0','0'],['0','0','10']]})
        self.assertAlmostEqual(properties(make('sweep',deps=[a,path]))['volume'],40*math.pi,places=5)
    def test_precise_groove_and_local_rest_wall(self):
        base=make('box',{'width':40,'depth':40,'height':3,'x':-20,'y':-20})
        f=feature('groove',{'radius':10,'width':1.2,'depth':.82,'z':3});f['base_operator']='box'
        result=make_feature(f,[base]);d=dimensions(f,result,[base]);self.assertAlmostEqual(d['depth'],.82,places=7);self.assertAlmostEqual(d['width'],1.2,places=7);self.assertAlmostEqual(d['remaining_wall'],2.18,places=7)
    def test_fillet_chamfer_shell(self):
        base=make('box',{'width':10,'depth':10,'height':10})
        for op,p,c in [('fillet',{'radius':1},{'edge_selector':'vertical'}),('chamfer',{'distance':1},{'edge_selector':'vertical'}),('shell',{'thickness':1},{'opening':'top'})]:
            with self.subTest(op=op):
                shape=make(op,p,[base],c);props=properties(shape);self.assertTrue(props['valid']);self.assertGreater(props['volume'],0);self.assertLess(props['volume'],1000)
    def test_rational_surface_and_knot_insertion(self):
        surface=make('nurbs_surface',extra={'poles':[[['0','0','0'],['0','2','0']],[['2','0','0'],['2','2','1']]],'weights':[['1','1'],['1','2']]})
        self.assertTrue(properties(surface)['valid'])
        points=TColgp_Array1OfPnt(1,4)
        for i,p in enumerate([(0,0,0),(1,2,0),(2,-1,0),(3,0,0)],1):points.SetValue(i,gp_Pnt(*p))
        curve=GeomAPI_PointsToBSpline(points).Curve();before=[curve.Value(i/20) for i in range(21)]
        curve.InsertKnot(.5)
        self.assertLess(max(a.Distance(curve.Value(i/20)) for i,a in enumerate(before)),1e-10)
    def test_transform_inverse_and_instance_volume(self):
        base=make('box',{'width':2,'depth':3,'height':4});moved=translated(base,10,-5,2)
        restored=translated(moved,-10,5,-2);self.assertEqual(bounds(base),bounds(restored))
        self.assertAlmostEqual(properties(make('pattern',{'count':20,'dx':5},[base]))['volume'],480)
        mirror=make('mirror',deps=[base]);self.assertAlmostEqual(properties(mirror)['volume'],24)
    def test_step_brep_stl_roundtrip(self):
        base=make('sphere',{'radius':2})
        with tempfile.TemporaryDirectory() as d:
            for fmt in ['step','brep','stl']:
                with self.subTest(format=fmt):
                    result=export_shape(base,fmt,str(Path(d)/('shape.'+fmt)),.05);self.assertEqual(result['status'],'checks_passed_within_profile')
    def test_hash_determinism_and_meshing(self):
        a=make('sphere',{'radius':3});b=make('sphere',{'radius':3});self.assertEqual(shape_hash(a),shape_hash(b))
        coarse=mesh(a,.2);fine=mesh(b,.002);self.assertGreaterEqual(len(fine['triangles']),len(coarse['triangles']))
    def test_invalid_native_operation_diagnoses(self):
        base=make('box',{'width':2,'depth':2,'height':2})
        with self.assertRaises(Exception):make('fillet',{'radius':20},[base],{'edge_selector':'all'})
        with self.assertRaises(GeometryError):make('hole',{'radius':1,'depth':1,'x':100,'z':2},[base])
    def test_open_surface_never_claims_a_volume(self):
        wire=make('circle',{'radius':1})
        face=as_face(wire)
        self.assertIsNone(properties(face)['volume'])
        solid=make('box',{'width':2,'depth':2,'height':2})
        self.assertFalse(properties(compound([solid,translated(face,10,0,0)]))['precision_solid_only'])

class FieldTests(unittest.TestCase):
    source={'op':'sphere','center':['0','0','0'],'radius':'2'}
    def test_sdf_reference_points_and_gradient(self):
        self.assertEqual(evaluate(self.source,[0,0,0]),-2);self.assertEqual(evaluate(self.source,[2,0,0]),0)
        self.assertAlmostEqual(float(np.linalg.norm(gradient(self.source,[3,1,0]))),1,places=7)
    def test_compact_support_far_region_unchanged(self):
        delta={'op':'local_field_delta','source':self.source,'center':['2','0','0'],'radius':'1','amplitude':'0.2'}
        random.seed(2036)
        for _ in range(100):
            point=[-random.uniform(1,10),random.uniform(-10,10),random.uniform(-10,10)]
            self.assertEqual(evaluate(self.source,point),evaluate(delta,point))
        self.assertAlmostEqual(evaluate(delta,[2,0,0]),.2);self.assertEqual(wendland(1),0);self.assertEqual(wendland(2),0)
    def test_csg_sign_and_anisotropic_field(self):
        moved={'op':'transform','source':self.source,'translation':['0','0','0'],'scale':['2','1','1']}
        self.assertEqual(evaluate(moved,[4,0,0]),0)
        for op in ['union','intersection']:self.assertEqual(evaluate({'op':op,'a':self.source,'b':self.source},[3,0,0]),1)
        self.assertGreater(evaluate({'op':'difference','a':self.source,'b':self.source},[0,0,0]),0)
    def test_sparse_extraction_and_vertex_error(self):
        f={'id':'sphere','construction':{'expression':self.source,'domain':{'min':['-3','-3','-3'],'max':['3','3','3']}},'field':{'cell_size':.6,'lipschitz':1,'semantics':'exact_sdf'}}
        result=extract(f);self.assertGreater(result['pruned_cells'],0);self.assertGreater(len(result['triangles']),100)
        self.assertLess(max(abs(evaluate(self.source,v)) for v in result['vertices']),.06)
        self.assertIsNone(result['certified_bound'])
    def test_local_deformation_inverse_moves_only_the_support(self):
        node={'op':'local_deform','source':self.source,'center':['2','0','0'],'radius':'2','displacement':['0.1','0','0']}
        self.assertAlmostEqual(evaluate(node,[2.1,0,0]),0,places=8)
        self.assertEqual(evaluate(node,[-3,0,0]),evaluate(self.source,[-3,0,0]))

if __name__=='__main__':unittest.main()
