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
        if fn=='vec3':
            require(len(a)==3 and all(np.ndim(v)==0 for v,g in a),'Drei Skalare für Vektoraufbau erforderlich.','CONSTRAINT_CONFLICT')
            value,grad=np.asarray([v for v,g in a]),np.stack([g for v,g in a])
        elif fn=='dot':
            require(len(a)==2 and all(np.shape(v)==(3,) for v,g in a),'Skalarprodukt benötigt zwei 3D-Vektoren.','CONSTRAINT_CONFLICT')
            value,grad=float(np.dot(a[0][0],a[1][0])),a[0][0]@a[1][1]+a[1][0]@a[0][1]
        elif fn=='norm':
            require(len(a)==1 and np.shape(a[0][0])==(3,),'Norm benötigt einen 3D-Vektor.','CONSTRAINT_CONFLICT')
            value=float(np.linalg.norm(a[0][0]))
            require(value>1e-15,'Norm außerhalb des glatten Solverbereichs.','CONSTRAINT_CONFLICT')
            grad=a[0][0]@a[0][1]/value
        elif fn in ('+','-'):
            require(np.shape(a[0][0])==np.shape(a[1][0]),'Addition benötigt gleiche Werttypen.','CONSTRAINT_CONFLICT')
            sign=1 if fn=='+' else -1
            value,grad=a[0][0]+sign*a[1][0],a[0][1]+sign*a[1][1]
        elif fn=='*':
            require(np.ndim(a[0][0])==0 or np.ndim(a[1][0])==0,'Vektormultiplikation benötigt einen Skalar.','CONSTRAINT_CONFLICT')
            value=a[0][0]*a[1][0]
            grad=a[0][1]*np.asarray(a[1][0])[...,None]+a[1][1]*np.asarray(a[0][0])[...,None]
        elif fn=='/':
            require(np.ndim(a[1][0])==0 and abs(a[1][0])>1e-15,'Division außerhalb des glatten Solverbereichs.','CONSTRAINT_CONFLICT')
            value,grad=a[0][0]/a[1][0],(a[0][1]*a[1][0]-a[1][1]*np.asarray(a[0][0])[...,None])/a[1][0]**2
        elif fn=='sqrt':
            require(np.ndim(a[0][0])==0 and a[0][0]>1e-15,'Wurzel außerhalb des glatten Solverbereichs.','CONSTRAINT_CONFLICT')
            value=math.sqrt(a[0][0]);grad=a[0][1]/(2*value)
        elif fn in ('sin','cos'):
            require(np.ndim(a[0][0])==0,'Trigonometrie benötigt einen Skalar.','CONSTRAINT_CONFLICT')
            value,grad=(math.sin(a[0][0]),math.cos(a[0][0])*a[0][1]) if fn=='sin' else (math.cos(a[0][0]),-math.sin(a[0][0])*a[0][1])
        else: raise GeometryError('OUT_OF_SCOPE','Nicht registrierte Solverfunktion.')
        require(np.all(np.isfinite(value)) and np.max(np.abs(value))<=1e12 and np.all(np.isfinite(grad)),'Nichtendliche Solverauswertung.','CONSTRAINT_CONFLICT')
        return value,grad
    def evaluated(equation,y):
        value,gradient=expression(equation['expression'],lower+y*span)
        require(np.ndim(value)==0,'Solvergleichung benötigt einen Skalar.','CONSTRAINT_CONFLICT')
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
