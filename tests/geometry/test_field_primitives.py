import sys,unittest,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from fields import evaluate,gradient

class FieldPrimitiveTests(unittest.TestCase):
    def test_normalized_plane_and_capped_cylinder(self):
        plane={'op':'plane','normal':['0','0','4'],'offset':'2'}
        for point,expected in [([0,0,0],-2),([3,4,2],0),([1,2,5],3)]:self.assertEqual(evaluate(plane,point),expected)
        cylinder={'op':'cylinder','center':['0','0','0'],'radius':'2','half_height':'3'}
        for point,expected in [([0,0,0],-2),([2,0,0],0),([0,0,3],0),([3,0,4],math.sqrt(2))]:self.assertAlmostEqual(evaluate(cylinder,point),expected)
        self.assertAlmostEqual(float((gradient(cylinder,[3,0,4])**2).sum()),1,places=7)

    def test_capsule_endpoints_and_degenerate_segment(self):
        capsule={'op':'capsule','start':['0','0','0'],'end':['0','0','4'],'radius':'1'}
        for point,expected in [([0,0,2],-1),([1,0,2],0),([0,0,-1],0),([0,0,6],1)]:self.assertEqual(evaluate(capsule,point),expected)
        capsule['end']=capsule['start'];self.assertEqual(evaluate(capsule,[0,0,3]),2)

    def test_constant_and_compact_variable_shell(self):
        source={'op':'sphere','center':['0','0','0'],'radius':'3'}
        shell={'op':'shell','source':source,'thickness':'0.4','variations':[]}
        self.assertAlmostEqual(evaluate(shell,[3.2,0,0]),0)
        self.assertAlmostEqual(evaluate(shell,[2.8,0,0]),0)
        shell['variations']=[{'center':['3','0','0'],'radius':'1','amplitude':'0.2'}]
        self.assertAlmostEqual(evaluate(shell,[3,0,0]),-.3)
        self.assertAlmostEqual(evaluate(shell,[-3,0,0]),-.2)
