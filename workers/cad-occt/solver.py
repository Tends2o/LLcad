"""Bounded smooth SLSQP with analytic forward derivatives, never source eval.

Hard equations and inequalities are constraints (Bauplan eq. 42), never small
weights. Optional soft objectives carry weights, scales and a robust loss.
After convergence the constraint Jacobian is factorised by SVD for rank,
conditioning and redundancy diagnostics, and the KKT system (eq. 46) yields the
local sensitivity of the solution to each constraint target. Nothing here
claims a global optimum.
"""
import math
import numpy as np
import scipy
from scipy.optimize import minimize
from geometry import require, GeometryError

LOSSES = {'none': (lambda z: z, lambda z: 1.0),
          'huber': (lambda z: z if z <= 1 else 2 * math.sqrt(z) - 1, lambda z: 1.0 if z <= 1 else 1 / math.sqrt(z)),
          'cauchy': (lambda z: math.log1p(z), lambda z: 1 / (1 + z))}


def solve(problem):
    variables=problem['variables']; count=len(variables)
    require(0<count<=12 and 0<len(problem['equations'])<=32,'Solverbudget überschritten.','BUDGET_EXCEEDED')
    objectives=problem.get('objectives',[])
    require(len(objectives)<=32,'Zu viele Zielterme.','BUDGET_EXCEEDED')
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
    # Linearly dependent equalities at the start point would make the SQP subproblem singular.
    # They are detected here by pivoted QR, excluded from the solver and still checked afterwards.
    equalities=[e for e in problem['equations'] if e['relation']=='eq'];dropped=[]
    if len(equalities)>1:
        J0=np.array([evaluated(e,start)[1] for e in equalities])
        singular=np.linalg.svd(J0,compute_uv=False)
        rank=int((singular>max(singular[0]*1e-9,1e-12)).sum()) if singular.size else 0
        if rank<len(equalities):
            from scipy.linalg import qr
            _,_,pivot=qr(J0.T,pivoting=True)
            independent=set(int(i) for i in pivot[:rank])
            dropped=[e['id'] for i,e in enumerate(equalities) if i not in independent]
    constraints=[{'type':'eq' if e['relation']=='eq' else 'ineq','fun':lambda y,e=e:evaluated(e,y)[0],
                  'jac':lambda y,e=e:evaluated(e,y)[1]} for e in problem['equations'] if e['id'] not in dropped]
    regularization=float(problem.get('regularization',1.0))
    require(math.isfinite(regularization) and 0<=regularization<=1e6,'Ungültige Dämpfung.','INVALID_SCHEMA')
    terms=[]
    for o in objectives:
        weight=float(o.get('weight',1));scale=float(o.get('scale',1));loss=o.get('loss','none')
        require(math.isfinite(weight) and 0<weight<=1e6 and math.isfinite(scale) and 1e-9<=scale<=1e9 and loss in LOSSES,
                'Ungültiger Zielterm.','INVALID_SCHEMA')
        terms.append((o,weight,scale,LOSSES[loss]))
    def objective(y):
        total=regularization*float(np.dot(y-start,y-start));gradient=regularization*2*(y-start)
        for o,weight,scale,(rho,drho) in terms:
            value,grad=expression(o['expression'],lower+y*span)
            require(np.ndim(value)==0,'Zielterm benötigt einen Skalar.','CONSTRAINT_CONFLICT')
            r=value/scale;z=r*r
            total+=weight*rho(z);gradient=gradient+weight*drho(z)*2*r*grad*span/scale
        return total,gradient
    result=minimize(lambda y:objective(y)[0],start,jac=lambda y:objective(y)[1],
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
            'engine':'SciPy-'+scipy.__version__+'/SLSQP','global_optimum_claimed':False,
            'objective_terms':[{'id':o['id'],'value':float(expression(o['expression'],lower+result.x*span)[0]),'loss':o.get('loss','none')} for o in objectives],
            'diagnostics':dict(diagnostics(problem,variables,expression,lower,span,result.x,regularization),
                               dependent_equalities_excluded_from_sqp=dropped)}


def diagnostics(problem,variables,expression,lower,span,y,regularization):
    """SVD rank/conditioning of the constraint Jacobian and KKT target sensitivity at the solution."""
    equations=problem['equations'];x=lower+y*span
    rows=[];active=[]
    for e in equations:
        value,grad=expression(e['expression'],x)
        if e['relation']=='eq' or abs(value)<=float(e['tolerance']):
            rows.append(grad*span);active.append(e['id'])
    names=[v['name'] for v in variables]
    if not rows:
        return {'active_constraints':[],'jacobian_rank':0,'condition_number':None,'redundant_constraints':[],
                'free_directions':[],'scaling':'normalized_unit_box_parameters','target_sensitivity':[]}
    J=np.array(rows)
    u,s,vt=np.linalg.svd(J,full_matrices=True)
    tolerance=max(s[0]*1e-9,1e-12) if len(s) else 0.
    rank=int((s>tolerance).sum())
    condition=float(s[0]/s[rank-1]) if rank else None
    redundant=[]
    if rank<len(rows):
        # Left singular vectors of vanishing singular values combine dependent constraint gradients.
        for k in range(rank,len(rows)):
            vector=u[:,k];involved=[active[i] for i in range(len(rows)) if abs(vector[i])>1e-6]
            if involved:redundant.append({'combination':involved,'coefficients':[float(vector[i]) for i in range(len(rows)) if abs(vector[i])>1e-6]})
    free=[]
    for k in range(rank,len(names)):
        direction=vt[k];free.append({name:float(direction[i]) for i,name in enumerate(names) if abs(direction[i])>1e-9})
    # KKT (eq. 46) for a unit change of each active target with the damped objective Hessian 2*lambda*I.
    n=len(names);m=len(rows);H=2*max(regularization,1e-6)*np.eye(n)
    kkt=np.block([[H,J.T],[J,np.zeros((m,m))]])
    sensitivity=[]
    for i,eid in enumerate(active):
        rhs=np.zeros(n+m);rhs[n+i]=1.0
        solution,*_=np.linalg.lstsq(kkt,rhs,rcond=None)
        step=solution[:n]*span
        sensitivity.append({'constraint':eid,'parameter_change_per_unit_target':{name:float(step[j]) for j,name in enumerate(names)}})
    return {'active_constraints':active,'jacobian_rank':rank,'singular_values':[float(v) for v in s],'condition_number':condition,
            'redundant_constraints':redundant,'free_directions':free,'scaling':'normalized_unit_box_parameters',
            'factorization':'numpy_SVD_with_relative_tolerance_1e-9','target_sensitivity':sensitivity,
            'note':'Local linearization at the solution; not valid across topology changes or feature jumps.'}
