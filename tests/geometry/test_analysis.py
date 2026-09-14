import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from geometry import *
from analysis import sample
from OCP.Geom import Geom_BSplineCurve

class AnalysisTests(unittest.TestCase):
    def test_piecewise_curve_does_not_report_a_one_sided_derivative_as_global(self):
        poles=TColgp_Array1OfPnt(1,3)
        for i,point in enumerate([(0,0,0),(1,0,0),(1,1,0)],1):poles.SetValue(i,gp_Pnt(*point))
        knots=TColStd_Array1OfReal(1,3);multiplicities=TColStd_Array1OfInteger(1,3)
        for i,(value,multiplicity) in enumerate([(0,2),(.5,1),(1,2)],1):knots.SetValue(i,value);multiplicities.SetValue(i,multiplicity)
        curve=Geom_BSplineCurve(poles,knots,multiplicities,1)
        shape=BRepBuilderAPI_MakeEdge(curve).Shape()
        for curvature in [False,True]:
            with self.assertRaises(GeometryError) as caught:sample(shape,{'curve_parameter':'.5'},curvature)
            self.assertEqual(caught.exception.code,'PRECISION_UNSUPPORTED')
        left=sample(shape,{'curve_parameter':'.25'},True)
        right=sample(shape,{'curve_parameter':'.75'},True)
        self.assertEqual(left['curvature_per_mm'],0);self.assertEqual(left['direction'],[1,0,0])
        self.assertEqual(right['direction'],[0,1,0])
