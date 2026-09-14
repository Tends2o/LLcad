import sys,unittest,math,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from fields import evaluate,gradient
from geometry import GeometryError
import numpy as np

class GyroidTests(unittest.TestCase):
    def test_vdb_checks_finite_storage_and_reports_actual_sampling_losses(self):
        from volume_io import export_vdb
        f={'id':'test','construction':{'domain':{'min':['0','0','0'],'max':['1','1','1']}},'field':{'cell_size':1,'lipschitz':1e40,'semantics':'general_implicit','value_unit':'dimensionless'}}
        with tempfile.TemporaryDirectory() as directory:
            path=directory+'/test.vdb'
            with self.assertRaises(GeometryError):export_vdb(f,path,lambda p:1.)
            f['field']['lipschitz']=1
            with self.assertRaises(GeometryError):export_vdb(f,path,lambda p:float('inf'))
            report=export_vdb(f,path,lambda p:10. if p[0]==0 else 1/3)
            self.assertEqual(report['truncated_samples'],4)
            self.assertEqual(report['sample_truncation_error_max'],6)
            self.assertGreater(report['sample_storage_error_max'],0)
            self.assertEqual(report['sample_roundtrip_error'],0)
    def test_periodicity_reference_values_analytic_gradient_and_value_conversion(self):
        node={'op':'gyroid','period':'4','origin':['0','0','0'],'threshold':'0.1'}
        self.assertEqual(evaluate(node,[0,0,0]),-.1)
        self.assertAlmostEqual(evaluate(node,[1,0,0]),.9,places=14)
        for point in [[.1,.4,1.2],[1.1,2.3,3.7],[-2,-1,0]]:
            k=2*math.pi/4;x,y,z=np.array(point)*k
            analytic=k*np.array([math.cos(x)*math.cos(y)-math.sin(z)*math.sin(x),-math.sin(x)*math.sin(y)+math.cos(y)*math.cos(z),-math.sin(y)*math.sin(z)+math.cos(z)*math.cos(x)])
            self.assertLess(float(np.linalg.norm(gradient(node,point)-analytic)),1e-8)
            self.assertLess(float(np.linalg.norm(analytic)),22/4)
            for axis in np.eye(3):self.assertAlmostEqual(evaluate(node,np.array(point)+axis*4),evaluate(node,point),places=14)
        converted={'op':'convert_field_unit','source':node,'to':'length','reference_length':{'value':'500','unit':'um'}}
        self.assertAlmostEqual(evaluate(converted,[1,0,0]),.45,places=14)
        inverse={'op':'convert_field_unit','source':converted,'to':'dimensionless','reference_length':{'value':'0.0005','unit':'m'}}
        self.assertAlmostEqual(evaluate(inverse,[1,0,0]),.9,places=14)

if __name__=='__main__':unittest.main()
