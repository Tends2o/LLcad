"""Bounded smooth SLSQP with analytic forward derivatives, never source eval."""
import math
import numpy as np
import scipy
from scipy.optimize import minimize
from geometry import require, GeometryError

def solve(problem):
    variables=problem['variables']; count=len(variables)
    require(0<count<=12 and 0<len(problem['equations'])<=32,'Solverbudget überschritten.','BUDGET_EXCEEDED')
    lower=np.array([v['lower_value'] for v in variables]); span=np.array([v['upper_value']-v['lower_value'] for v in variables])
    start=(np.array([v['initial'] for v in variables])-lower)/span
    names={v['name']:i for i,v in enumerate(variables)}
    evaluations=0
    def expression(node,x,depth=0):
        nonlocal evaluations
        evaluations+=1
        require(depth<=32 and evaluations<=2000000,'Solver-Auswertungsbudget überschritten.','BUDGET_EXCEEDED')
        if 'constant' in node: return float(node['constant']),np.zeros(count)
        if 'parameter' in node:
            i=names[node['parameter']]; gradient=np.zeros(count);gradient[i]=1
            return x[i],gradient
        a=[expression(n,x,depth+1) for n in node['args']]; fn=node['fn']
        if fn=='+': value,grad=a[0][0]+a[1][0],a[0][1]+a[1][1]
        elif fn=='-': value,grad=a[0][0]-a[1][0],a[0][1]-a[1][1]
        elif fn=='*': value,grad=a[0][0]*a[1][0],a[0][1]*a[1][0]+a[1][1]*a[0][0]
        elif fn=='/':
            require(abs(a[1][0])>1e-15,'Division außerhalb des glatten Solverbereichs.','CONSTRAINT_CONFLICT')
            value,grad=a[0][0]/a[1][0],(a[0][1]*a[1][0]-a[1][1]*a[0][0])/a[1][0]**2
        elif fn=='sqrt':
            require(a[0][0]>1e-15,'Wurzel außerhalb des glatten Solverbereichs.','CONSTRAINT_CONFLICT')
            value=math.sqrt(a[0][0]);grad=a[0][1]/(2*value)
        elif fn=='sin':value,grad=math.sin(a[0][0]),math.cos(a[0][0])*a[0][1]
        elif fn=='cos':value,grad=math.cos(a[0][0]),-math.sin(a[0][0])*a[0][1]
        else: raise GeometryError('OUT_OF_SCOPE','Nicht registrierte Solverfunktion.')
        require(math.isfinite(value) and abs(value)<=1e12 and np.all(np.isfinite(grad)),'Nichtendliche Solverauswertung.','CONSTRAINT_CONFLICT')
        return value,grad
    def evaluated(equation,y):
        value,gradient=expression(equation['expression'],lower+y*span)
        sign=-1 if equation['relation']=='le' else 1
        return value*sign,gradient*span*sign
    constraints=[{'type':'eq' if e['relation']=='eq' else 'ineq','fun':lambda y,e=e:evaluated(e,y)[0],
                  'jac':lambda y,e=e:evaluated(e,y)[1]} for e in problem['equations']]
    result=minimize(lambda y:float(np.dot(y-start,y-start)),start,jac=lambda y:2*(y-start),
                    method='SLSQP',bounds=[(0.,1.)]*count,constraints=constraints,
                    options={'maxiter':problem['max_iterations'],'ftol':min(float(e['tolerance']) for e in problem['equations'])*.1,'disp':False})
    residuals=[]
    for e in problem['equations']:
        value,_=expression(e['expression'],lower+result.x*span)
        violation=abs(value) if e['relation']=='eq' else max(0,-value) if e['relation']=='ge' else max(0,value)
        residuals.append({'id':e['id'],'residual':value,'violation':violation,'tolerance':float(e['tolerance'])})
    require(result.success and all(r['violation']<=r['tolerance'] for r in residuals),
            'Keine nachgewiesene lokale Lösung innerhalb der Grenzen und Iterationsbudgets.','CONSTRAINT_CONFLICT')
    return {'status':'converged','values':dict(zip(names,map(float,lower+result.x*span))),
            'residuals':residuals,'iterations':int(result.nit),'expression_evaluations':evaluations,
            'engine':'SciPy-'+scipy.__version__+'/SLSQP','global_optimum_claimed':False}
