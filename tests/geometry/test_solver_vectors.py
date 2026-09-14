import sys, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'workers/cad-occt'))
from solver import solve
from geometry import GeometryError

def c(value):return {'constant':value}
def p(name):return {'parameter':name}
def f(name,*args):return {'fn':name,'args':list(args)}

class SolverVectorTests(unittest.TestCase):
    def test_actual_optimizer_callbacks_have_correct_vector_chain_rule(self):
        v=f('vec3',p('x'),p('y'),p('z'));s=p('scale')
        expressions=[
            (f('norm',f('*',s,v)),np.sqrt(56)),
            (f('dot',f('/',v,s),f('vec3',s,p('x'),p('y'))),5),
            (f('norm',f('-',v,f('vec3',c(3),c(1),c(-1)))),np.sqrt(21)),
        ]
        problem={'variables':[{'name':name,'initial':value,'lower_value':value-.5,'upper_value':value+.5} for name,value in zip(['x','y','z','scale'],[1,2,3,2])],
                 'equations':[{'id':str(i),'relation':'eq','tolerance':1e-8,'expression':f('-',f('/',expr,c(reference)),c(1))} for i,(expr,reference) in enumerate(expressions)],'max_iterations':50}
        checks=[]
        def inspect_callbacks(objective,start,**kwargs):
            for constraint in kwargs['constraints']:
                for point in [start,*np.random.default_rng(987).uniform(.1,.9,(12,4))]:
                    step=np.eye(4)*1e-6
                    finite=np.array([(constraint['fun'](point+h)-constraint['fun'](point-h))/(2e-6) for h in step])
                    actual=constraint['jac'](point)
                    np.testing.assert_allclose(actual,finite,rtol=3e-6,atol=1e-8)
                    self.assertEqual(actual.shape,(4,))
                    checks.append(1)
            return SimpleNamespace(success=True,x=start,nit=0)
        # This unit test examines the callbacks, not optimizer convergence. The
        # separate MCP integration test runs the actual four-variable optimizer.
        with patch('solver.minimize',side_effect=inspect_callbacks):result=solve(problem)
        self.assertEqual(len(checks),39)
        self.assertTrue(all(abs(r['residual'])<1e-12 for r in result['residuals']))

    def test_zero_norm_is_not_given_an_invented_smooth_derivative(self):
        problem={'variables':[{'name':'x','initial':0,'lower_value':-1,'upper_value':1}],
                 'equations':[{'id':'zero','relation':'eq','tolerance':1e-8,'expression':f('norm',f('vec3',p('x'),c(0),c(0)))}],'max_iterations':10}
        with self.assertRaises(GeometryError) as caught:solve(problem)
        self.assertEqual(caught.exception.code,'CONSTRAINT_CONFLICT')

if __name__=='__main__':unittest.main()
