import sys,unittest,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from geometry import *
from OCP.BRepAdaptor import BRepAdaptor_Curve

class NurbsTests(unittest.TestCase):
    def test_explicit_rational_quarter_circle_uses_weights_and_clamped_basis(self):
        c={'operator':'nurbs_curve','poles':[['1','0','0'],['1','1','0'],['0','1','0']],
           'weights':['1',str(math.sqrt(.5)),'1'],'basis':{'degree':2,'knots':['0','1'],'multiplicities':[3,3]}}
        wire=make_feature({'construction':c,'values':{}},[])
        curve=BRepAdaptor_Curve(TopoDS.Edge_s(next(explore(wire,TopAbs_EDGE))))
        for i in range(21):
            p=curve.Value(i/20);self.assertAlmostEqual(math.hypot(p.X(),p.Y()),1,places=14)
        self.assertAlmostEqual(curve.Value(.5).X(),math.sqrt(.5),places=14)

    def test_surface_control_point_support_is_local_in_the_parameter_domain(self):
        basis={'degree':2,'knots':['0','.25','.75','1'],'multiplicities':[3,1,1,3]}
        poles=[[[str(u),str(v),'0'] for v in range(3)] for u in range(5)]
        def build(points):
            c={'operator':'nurbs_surface','poles':points,'weights':[['1']*3 for _ in range(5)],'u_basis':basis}
            return BRep_Tool.Surface_s(TopoDS.Face_s(make_feature({'construction':c,'values':{}},[])))
        original=build(poles);poles[0][1][2]='1';modified=build(poles)
        self.assertGreater(original.Value(.1,.5).Distance(modified.Value(.1,.5)),.1)
        for u in [.25,.5,.75,1]:
            for v in [0,.1,.5,.9,1]:self.assertEqual(original.Value(u,v).Distance(modified.Value(u,v)),0)

if __name__=='__main__':unittest.main()
