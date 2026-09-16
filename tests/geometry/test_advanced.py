import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from geometry import *
import topology as t

def feature(fid,op,p=None,**c): return {'id':fid,'values':p or {},'construction':{'operator':op,**c}}

class AdvancedTests(unittest.TestCase):
    def test_curves_and_planar_caps(self):
        line=make_feature(feature('line','line',start=['0','0','0'],end=['0','0','3']),[])
        self.assertEqual(bounds(line),[0,0,0,0,0,3])
        arc=make_feature(feature('arc','arc',points=[['0','0','0'],['1','1','0'],['2','0','0']]),[])
        self.assertTrue(properties(arc)['valid'])
        wire=make_feature(feature('p','profile',points=[['0','0','0'],['3','0','0'],['3','2','0'],['0','2','0']]),[])
        face=make_feature(feature('cap','cap'),[wire]); self.assertAlmostEqual(properties(face)['area'],6)
        point=make_feature(feature('pt','point',{'x':1,'y':2,'z':3}),[]); self.assertEqual(bounds(point),[1,2,3,1,2,3])

    def test_trim_and_sew_native_surfaces(self):
        surface=make_feature(feature('patch','nurbs_surface',poles=[[['0','0','0'],['0','10','0']],[['10','0','0'],['10','10','0']]],weights=[['1','1'],['1','1']]),[])
        trimmed=make_feature(feature('trim','trim_surface',{'u_min':.25,'u_max':.75,'v_min':.1,'v_max':.9}),[surface])
        self.assertAlmostEqual(properties(trimmed)['area'],40)
        box=make_feature(feature('box','box',{'width':3,'depth':4,'height':5}),[])
        faces=list(explore(box,TopAbs_FACE))
        sewn=make_feature(feature('sew','sew',{'tolerance':1e-7},make_solid=True),faces)
        self.assertAlmostEqual(properties(sewn)['volume'],60)
        with self.assertRaises(GeometryError):make_feature(feature('bad','sew',{'tolerance':1e-7},make_solid=True),faces[:-1])
        regular=make_feature(feature('reg','regularize'),[box]); self.assertAlmostEqual(properties(regular)['volume'],60)

    def test_custom_helical_thread_is_a_real_valid_solid(self):
        parameters={'root_radius':3,'height':4,'pitch':2,'tooth_depth':.5,'tooth_width':1}
        for hand in ['left','right']:
            shape=make_feature(feature('thread','thread',parameters,mode='external',handedness=hand),[])
            props=properties(shape)
            self.assertTrue(props['valid']); self.assertEqual(props['solids'],1)
            self.assertGreater(props['volume'],math.pi*3**2*4)
            self.assertLess(abs(props['bounds'][5]-props['bounds'][2]-4),.001)
        base=make_feature(feature('base','box',{'width':10,'depth':10,'height':4,'x':-5,'y':-5}),[])
        cut=make_feature(feature('internal','thread',parameters,mode='internal',handedness='right'),[base])
        self.assertTrue(properties(cut)['valid']); self.assertLess(properties(cut)['volume'],400)

    def test_profile_history_survives_cache_and_drives_loft_faces(self):
        profile=feature('profile','profile',points=[['0','0','0'],['4','0','0'],['4','4','0'],['0','4','0']])
        shape,trace=t.evaluate_feature(profile,[],[])
        records=t.serialize(trace,'profile-key'); self.assertEqual(len(records['edges']),4)
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'profile.brep');_,_,records=t.archive(shape,trace,'profile-key',path)
            checksum=hashlib.sha256(Path(path).read_bytes()).hexdigest()
            shape=read_brep(path);trace=t.restore(shape,records,'profile-key',checksum)
            other,history=t.evaluate_feature(feature('top','transform',{'z':4,'scale':.8}),[shape],[trace])
            result,history=t.evaluate_feature(feature('loft','loft'),[shape,other],[trace,history])
            self.assertTrue(properties(result)['valid'])
            self.assertTrue(all(x['origins'] for x in history))

    def test_fillet_generated_faces_have_stable_native_origins(self):
        base,trace=t.evaluate_feature(feature('base','box',{'width':10,'depth':10,'height':10}),[],[])
        histories=[]
        for radius in [1,1.02]:
            shape,history=t.evaluate_feature(feature('round','fillet',{'radius':radius},edge_selector='vertical'),[base],[trace])
            records=t.serialize(history,str(radius));self.assertTrue(all(x['origins'] for x in records['faces']))
            histories.append(sorted(x['origins'][0]['key'] for x in records['faces']))
        self.assertEqual(histories[0],histories[1])

    def test_sweep_frames_twist_scale_and_self_contact(self):
        import advanced
        circle=make_feature(feature('c','circle',{'radius':1}),[])
        path=make_feature(feature('p','bspline',points=[['0','0','0'],['0','0','5'],['3','0','10']]),[])
        plain=make_feature(feature('s','sweep'),[circle,path])
        self.assertTrue(properties(plain)['valid']); self.assertEqual(advanced.CONSTRUCTION_REPORTS['s']['frame'],'occt_pipe_corrected_frenet')
        twisted=make_feature(feature('t','sweep',{'twist':math.pi/2,'scale_end':.5,'sections':12},frame='rotation_minimizing'),[circle,path])
        props=properties(twisted); self.assertTrue(props['valid']); self.assertEqual(props['solids'],1)
        self.assertEqual(advanced.CONSTRUCTION_REPORTS['t']['frame'],'rotation_minimizing_double_reflection')
        self.assertLess(props['volume'],properties(plain)['volume'])
        straight=make_feature(feature('line','line',start=['0','0','0'],end=['0','0','6']),[])
        rmf=make_feature(feature('r','sweep',{'scale_end':2,'sections':4}),[circle,straight])
        self.assertAlmostEqual(properties(rmf)['volume'],math.pi*6*(1+2+4)/3,delta=.3)
        points,tangents,references=advanced.rotation_minimizing_frames(straight,4)
        for t,r in zip(tangents,references):self.assertAlmostEqual(float(abs(t@r)),0,places=12)
        tight=make_feature(feature('arc','arc',points=[['0','0','0'],['0.5','0.5','0'],['1','0','0']]),[])
        big=make_feature(feature('big','circle',{'radius':2}),[])
        with self.assertRaises(GeometryError) as caught:make_feature(feature('bad','sweep'),[big,tight])
        self.assertEqual(caught.exception.code,'GEOMETRY_INVALID')

    def test_loft_compatibility_offset_and_iso_thread(self):
        square=make_feature(feature('sq','profile',points=[['0','0','0'],['2','0','0'],['2','2','0'],['0','2','0']]),[])
        triangle=make_feature(feature('tri','profile',points=[['0','0','3'],['2','0','3'],['1','2','3']]),[])
        with self.assertRaises(GeometryError):make_feature(feature('l','loft'),[square,triangle])
        forced=make_feature(feature('l2','loft',check_compatibility=False),[square,triangle])
        self.assertTrue(properties(forced)['valid'])
        box=make_feature(feature('box','box',{'width':4,'depth':4,'height':4}),[])
        grown=make_feature(feature('off','offset_solid',{'distance':1}),[box])
        self.assertTrue(properties(grown)['valid']); self.assertGreater(properties(grown)['volume'],64)
        shrunk=make_feature(feature('in','offset_solid',{'distance':-1}),[box]); self.assertAlmostEqual(properties(shrunk)['volume'],8,places=6)
        with self.assertRaises(GeometryError):make_feature(feature('collapse','offset_solid',{'distance':-2.5}),[box])
        iso={'root_radius':(6-1.082532)/2,'pitch':1,'height':6,'tooth_depth':5*math.sqrt(3)/16,'tooth_width':.75,'crest_width':.125,'runout':1}
        thread=make_feature(feature('m6','thread',iso,mode='external',handedness='right',standard='iso_metric_basic',designation='M6'),[])
        props=properties(thread); self.assertTrue(props['valid']); self.assertEqual(props['solids'],1)
        self.assertAlmostEqual(props['bounds'][3]-props['bounds'][0],6,delta=.02)
        core=math.pi*iso['root_radius']**2*6
        self.assertGreater(props['volume'],core); self.assertLess(props['volume'],math.pi*9*6)

if __name__=='__main__':unittest.main()
