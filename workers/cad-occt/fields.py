"""Analytic implicit fields with conservative Lipschitz pruning and sparse surface cells."""
import math
import numpy as np
from geometry import require

def wendland(q):
    return max(0.,1.-q)**4*(4.*q+1.) if q>=0 else 0.

def evaluate(node, point):
    p=np.asarray(point,dtype=float);op=node['op']
    if op in ('sphere','box','torus','cylinder'):p=p-np.asarray(node['center'],dtype=float)
    if op=='sphere':return float(np.linalg.norm(p)-float(node['radius']))
    if op=='box':
        q=np.abs(p)-np.asarray(node['half_size'],dtype=float)
        return float(np.linalg.norm(np.maximum(q,0))+min(max(q),0))
    if op=='torus':return math.hypot(math.hypot(p[0],p[1])-float(node['major']),p[2])-float(node['minor'])
    if op=='plane':
        normal=np.asarray(node['normal'],float);return float(np.dot(normal/np.linalg.norm(normal),p)-float(node['offset']))
    if op=='cylinder':
        q=np.array([math.hypot(p[0],p[1])-float(node['radius']),abs(p[2])-float(node['half_height'])])
        return float(np.linalg.norm(np.maximum(q,0))+min(max(q),0))
    if op=='capsule':
        a=np.asarray(node['start'],float);b=np.asarray(node['end'],float);axis=b-a;length2=float(np.dot(axis,axis))
        t=max(0.,min(1.,float(np.dot(p-a,axis))/length2)) if length2>0 else 0.
        return float(np.linalg.norm(p-a-t*axis)-float(node['radius']))
    if op in ('union','intersection','difference','smooth_union'):
        a,b=evaluate(node['a'],p),evaluate(node['b'],p)
        if op=='union':return min(a,b)
        if op=='intersection':return max(a,b)
        if op=='difference':return max(a,-b)
        k=float(node['k']);h=max(k-abs(a-b),0)/k;return min(a,b)-h*h*k/4
    if op=='transform':
        scale=np.asarray(node['scale'],dtype=float)
        return float(min(scale))*evaluate(node['source'],(p-np.asarray(node['translation'],dtype=float))/scale)
    if op=='local_deform':
        center=np.asarray(node['center'],float);displacement=np.asarray(node['displacement'],float);radius=float(node['radius']);q=p.copy()
        for _ in range(128):
            inverse=p-displacement*wendland(float(np.linalg.norm(q-center))/radius)
            if np.linalg.norm(inverse-q)<1e-10:return evaluate(node['source'],inverse)
            q=inverse
        require(False,'Inverse lokale Deformation konvergiert nicht.','CONSTRAINT_CONFLICT')
    base=evaluate(node['source'],p)
    if op=='offset':return base-float(node['distance'])
    if op=='shell':
        thickness=float(node['thickness'])
        for term in node.get('variations',[]):thickness+=float(term['amplitude'])*wendland(float(np.linalg.norm(p-np.asarray(term['center'],float)))/float(term['radius']))
        return abs(base)-thickness/2
    if op=='local_field_delta':
        q=float(np.linalg.norm(p-np.asarray(node['center'],dtype=float)))/float(node['radius'])
        return base+float(node['amplitude'])*wendland(q)
    raise ValueError('Unknown field node')

def gradient(node, point, h=1e-5):
    p=np.asarray(point,dtype=float)
    return np.array([(evaluate(node,p+np.eye(3)[i]*h)-evaluate(node,p-np.eye(3)[i]*h))/(2*h) for i in range(3)])

def field_facts(f):
    c=f['construction'];lo=np.array(c['domain']['min'],dtype=float);hi=np.array(c['domain']['max'],dtype=float)
    vals=[evaluate(c['expression'],p) for p in [lo,hi,(lo+hi)/2]]
    require(all(math.isfinite(v) for v in vals),'Nichtendliches Feld.')
    return dict(valid=True,bounds=[*lo,*hi],field_semantics=f['field']['semantics'],lipschitz_bound=f['field']['lipschitz'],
                dimensions={},geometry_hash=f['cache_key'],engine_build='mathforge-field-1',
                volume=None,area=None,solids=None,coverage='analytic_contract_and_sampled_finiteness',manufacturing_status='not_certified')

def extract(f,sampler=None):
    c=f['construction'];node=c['expression'];lo=np.array(c['domain']['min'],float);hi=np.array(c['domain']['max'],float)
    step=f['field']['cell_size'];L=f['field']['lipschitz'];depth=max(0,math.ceil(math.log2(max(hi-lo)/step)))
    sampled=sampler if sampler is not None else lambda point:evaluate(node,point)
    require(depth<=10,'Feldtiefe überschreitet das Budget.','BUDGET_EXCEEDED')
    n=2**depth;spacing=(hi-lo)/n;values={};vertices=[];triangles=[];vertex_map={};cells=0;pruned=0
    offsets=np.array([[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]])
    tets=((0,1,2,6),(0,2,3,6),(0,3,7,6),(0,7,4,6),(0,4,5,6),(0,5,1,6))
    def sample(index):
        key=tuple(index)
        if key not in values:values[key]=sampled(lo+spacing*index)
        return values[key]
    def vertex(a,b,va,vb):
        ka,kb=tuple(a),tuple(b);key=tuple(sorted((ka,kb)))
        if abs(va)<1e-14:key=('point',ka)
        elif abs(vb)<1e-14:key=('point',kb)
        if key not in vertex_map:
            t=va/(va-vb);point=lo+spacing*(a+t*(b-a));vertex_map[key]=len(vertices);vertices.append(point.tolist())
        return vertex_map[key]
    def triangle(indices):
        if len(set(indices))<3:return
        a,b,d=(np.asarray(vertices[i]) for i in indices);normal=np.cross(b-a,d-a)
        if np.linalg.norm(normal)<1e-18:return
        if np.dot(normal,gradient(node,(a+b+d)/3))<0:indices=[indices[0],indices[2],indices[1]]
        triangles.append(indices);require(len(triangles)<=500000,'Feld-Dreiecksbudget überschritten.','BUDGET_EXCEEDED')
    def recurse(origin,size):
        nonlocal cells,pruned
        cells+=1;require(cells<=250000,'Aktive Feldzellen überschreiten das Budget.','BUDGET_EXCEEDED')
        center=lo+spacing*(origin+size/2);radius=float(np.linalg.norm(spacing*size/2));v=sampled(center)
        if abs(v)>L*radius+1e-12:pruned+=1;return
        if size>1:
            for offset in offsets:recurse(origin+offset*(size//2),size//2)
            return
        points=origin+offsets;vs=[sample(p) for p in points]
        for tet in tets:
            inside=[i for i in tet if vs[i]<0];outside=[i for i in tet if vs[i]>=0]
            if not inside or not outside:continue
            if len(inside) in (1,3):
                one=inside if len(inside)==1 else outside;other=outside if len(inside)==1 else inside
                triangle([vertex(points[one[0]],points[j],vs[one[0]],vs[j]) for j in other])
            else:
                a,b=inside;d,e=outside
                q=[vertex(points[a],points[d],vs[a],vs[d]),vertex(points[a],points[e],vs[a],vs[e]),vertex(points[b],points[e],vs[b],vs[e]),vertex(points[b],points[d],vs[b],vs[d])]
                triangle([q[0],q[1],q[2]]);triangle([q[0],q[2],q[3]])
    recurse(np.zeros(3,dtype=int),n)
    require(triangles,'Keine Oberfläche in der Domäne gefunden.')
    return dict(vertices=vertices,triangles=triangles,feature_id=f['id'],quality='preview_only',field_semantics=f['field']['semantics'],
                deflection=float(np.linalg.norm(spacing)),certified_bound=None,active_cells=cells,pruned_cells=pruned,
                note='Sparse Lipschitz octree with marching tetrahedra; subcell topology is not certified.')
