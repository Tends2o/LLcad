"""Analytic implicit fields with conservative Lipschitz pruning and sparse surface cells."""
import math
import time
from dataclasses import dataclass
from typing import Callable
from functools import lru_cache
from fractions import Fraction
import numpy as np
from geometry import require

def wendland(q):
    return max(0.,1.-q)**4*(4.*q+1.) if q>=0 else 0.

@lru_cache(maxsize=128)
def affine_constants(matrix):
    a=[[Fraction(x) for x in row] for row in matrix]
    def cross(v,w):return [v[(i+1)%3]*w[(i+2)%3]-v[(i+2)%3]*w[(i+1)%3] for i in range(3)]
    rows=[cross(a[1],a[2]),cross(a[2],a[0]),cross(a[0],a[1])]
    det=sum(a[0][i]*rows[0][i] for i in range(3));require(det!=0,'Singuläre Feldtransformation.')
    inverse=[[rows[j][i]/det for j in range(3)] for i in range(3)]
    norm=sum(abs(x) for row in inverse for x in row)
    # Round the positive distance multiplier down, never claim an exact SDF.
    scale=math.nextafter(float(1/norm),0.)
    return np.array([[float(x) for x in row] for row in inverse]),scale

@lru_cache(maxsize=128)
def rotation_matrix(axis,value,unit):
    u=np.asarray(axis,float);u=u/np.linalg.norm(u)
    angle=float(value)*(math.pi/180 if unit=='deg' else 1)
    cross=np.array([[0,-u[2],u[1]],[u[2],0,-u[0]],[-u[1],u[0],0]])
    return np.eye(3)*math.cos(angle)+(1-math.cos(angle))*np.outer(u,u)+math.sin(angle)*cross

def rotated_local(node,point):
    origin=np.asarray(node['origin'],float);angle=node['angle']
    rotation=rotation_matrix(tuple(node['axis']),angle['value'],angle['unit'])
    return origin+rotation.T@(np.asarray(point,float)-origin)

def evaluate(node, point):
    p=np.asarray(point,dtype=float);op=node['op']
    if op=='gyroid':
        x,y,z=2*math.pi*(p-np.asarray(node['origin'],float))/float(node['period'])
        return math.sin(x)*math.cos(y)+math.sin(y)*math.cos(z)+math.sin(z)*math.cos(x)-float(node['threshold'])
    if op=='convert_field_unit':
        q=node['reference_length'];reference=float(q['value'])*{'mm':1,'m':1000,'um':.001}[q['unit']]
        return evaluate(node['source'],p)*(reference if node['to']=='length' else 1/reference)
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
    if op=='affine_transform':
        inverse,scale=affine_constants(tuple(tuple(row) for row in node['matrix']))
        return scale*evaluate(node['source'],inverse@(p-np.asarray(node['translation'],float)))
    if op=='rotate':return evaluate(node['source'],rotated_local(node,p))
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

@dataclass(frozen=True)
class CompiledField:
    """A bounded tree of trusted closures; no generated source, eval or executable input."""
    function: Callable
    node_count: int
    max_depth: int
    compile_seconds: float

    def __call__(self,point):
        return float(self.function(np.asarray(point,dtype=float)))

    def gradient(self,point,h=1e-5):
        p=np.asarray(point,dtype=float)
        axes=np.eye(3)*h
        return np.array([(self.function(p+axis)-self.function(p-axis))/(2*h) for axis in axes])

def compile_field(node):
    """Snapshot constants once per job. The recursive evaluator stays an independent oracle."""
    started=time.monotonic();count=0;max_depth=0
    def number(value):
        result=float(value);require(math.isfinite(result),'Nichtendliche Feldkonstante.')
        return result
    def vector(value):
        result=np.array(value,dtype=float,copy=True)
        require(result.shape==(3,) and np.isfinite(result).all(),'Ungültiger Feldvektor.')
        result.flags.writeable=False
        return result
    def visit(n,depth):
        nonlocal count,max_depth
        count+=1;max_depth=max(max_depth,depth)
        require(count<=4096 and depth<=32,'Kompiliertes Feld-AST überschreitet das Budget.','BUDGET_EXCEEDED')
        op=n['op']
        if op=='gyroid':
            origin=vector(n['origin']);period=number(n['period']);threshold=number(n['threshold'])
            def gyroid(p):
                x,y,z=2*math.pi*(p-origin)/period
                return math.sin(x)*math.cos(y)+math.sin(y)*math.cos(z)+math.sin(z)*math.cos(x)-threshold
            return gyroid
        if op=='sphere':
            center=vector(n['center']);radius=number(n['radius'])
            return lambda p:float(np.linalg.norm(p-center)-radius)
        if op=='box':
            center=vector(n['center']);half=vector(n['half_size'])
            def box(p):
                q=np.abs(p-center)-half
                return float(np.linalg.norm(np.maximum(q,0))+min(max(q),0))
            return box
        if op=='torus':
            center=vector(n['center']);major=number(n['major']);minor=number(n['minor'])
            def torus(p):
                q=p-center
                return math.hypot(math.hypot(q[0],q[1])-major,q[2])-minor
            return torus
        if op=='plane':
            normal=vector(n['normal']);normal=normal/np.linalg.norm(normal);offset=number(n['offset'])
            return lambda p:float(np.dot(normal,p)-offset)
        if op=='cylinder':
            center=vector(n['center']);radius=number(n['radius']);half=number(n['half_height'])
            def cylinder(p):
                q=p-center;d=np.array([math.hypot(q[0],q[1])-radius,abs(q[2])-half])
                return float(np.linalg.norm(np.maximum(d,0))+min(max(d),0))
            return cylinder
        if op=='capsule':
            a=vector(n['start']);axis=vector(n['end'])-a;length2=float(np.dot(axis,axis));radius=number(n['radius'])
            def capsule(p):
                t=max(0.,min(1.,float(np.dot(p-a,axis))/length2)) if length2>0 else 0.
                return float(np.linalg.norm(p-a-t*axis)-radius)
            return capsule
        if op in ('union','intersection','difference','smooth_union'):
            a=visit(n['a'],depth+1);b=visit(n['b'],depth+1)
            if op=='union':return lambda p:min(a(p),b(p))
            if op=='intersection':return lambda p:max(a(p),b(p))
            if op=='difference':return lambda p:max(a(p),-b(p))
            k=number(n['k'])
            def smooth(p):
                va,vb=a(p),b(p);h=max(k-abs(va-vb),0)/k
                return min(va,vb)-h*h*k/4
            return smooth
        require(op in ('convert_field_unit','transform','affine_transform','rotate','local_deform','offset','shell','local_field_delta'),
                'Unregistrierter Feldoperator.','OUT_OF_SCOPE')
        source=visit(n['source'],depth+1)
        if op=='convert_field_unit':
            q=n['reference_length'];reference=number(q['value'])*{'mm':1,'m':1000,'um':.001}[q['unit']]
            factor=reference if n['to']=='length' else 1/reference
            return lambda p:source(p)*factor
        if op=='transform':
            scale=vector(n['scale']);translation=vector(n['translation']);distance_scale=float(min(scale))
            return lambda p:distance_scale*source((p-translation)/scale)
        if op=='affine_transform':
            inverse,scale=affine_constants(tuple(tuple(row) for row in n['matrix']));translation=vector(n['translation'])
            return lambda p:scale*source(inverse@(p-translation))
        if op=='rotate':
            origin=vector(n['origin']);angle=n['angle'];inverse=rotation_matrix(tuple(n['axis']),angle['value'],angle['unit']).T
            return lambda p:source(origin+inverse@(p-origin))
        if op=='local_deform':
            center=vector(n['center']);displacement=vector(n['displacement']);radius=number(n['radius'])
            def deform(p):
                q=p.copy()
                for _ in range(128):
                    inverse=p-displacement*wendland(float(np.linalg.norm(q-center))/radius)
                    if np.linalg.norm(inverse-q)<1e-10:return source(inverse)
                    q=inverse
                require(False,'Inverse lokale Deformation konvergiert nicht.','CONSTRAINT_CONFLICT')
            return deform
        if op=='offset':
            distance=number(n['distance'])
            return lambda p:source(p)-distance
        if op=='shell':
            thickness=number(n['thickness'])
            variations=tuple((vector(t['center']),number(t['radius']),number(t['amplitude'])) for t in n.get('variations',[]))
            def shell(p):
                local=thickness
                for center,radius,amplitude in variations:local+=amplitude*wendland(float(np.linalg.norm(p-center))/radius)
                return abs(source(p))-local/2
            return shell
        center=vector(n['center']);radius=number(n['radius']);amplitude=number(n['amplitude'])
        return lambda p:source(p)+amplitude*wendland(float(np.linalg.norm(p-center))/radius)
    function=visit(node,0)
    return CompiledField(function,count,max_depth,time.monotonic()-started)

def field_facts(f,compiled=None):
    c=f['construction'];lo=np.array(c['domain']['min'],dtype=float);hi=np.array(c['domain']['max'],dtype=float)
    evaluated=compiled if compiled is not None else compile_field(c['expression'])
    vals=[evaluated(p) for p in [lo,hi,(lo+hi)/2]]
    require(all(math.isfinite(v) for v in vals),'Nichtendliches Feld.')
    return dict(valid=True,bounds=[*lo,*hi],field_semantics=f['field']['semantics'],value_unit=f['field'].get('value_unit','length'),lipschitz_bound=f['field']['lipschitz'],
                dimensions={},geometry_hash=f['cache_key'],engine_build='mathforge-field-1',
                volume=None,area=None,solids=None,coverage='analytic_contract_and_sampled_finiteness',manufacturing_status='not_certified')

def extract(f,sampler=None):
    c=f['construction'];node=c['expression'];lo=np.array(c['domain']['min'],float);hi=np.array(c['domain']['max'],float)
    step=f['field']['cell_size'];L=f['field']['lipschitz'];depth=max(0,math.ceil(math.log2(max(hi-lo)/step)))
    compiled=sampler.compiled if hasattr(sampler,'compiled') else compile_field(node)
    sampled=sampler if sampler is not None else compiled
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
        if np.dot(normal,compiled.gradient((a+b+d)/3))<0:indices=[indices[0],indices[2],indices[1]]
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
    return dict(vertices=vertices,triangles=triangles,feature_id=f['id'],quality='preview_only',field_semantics=f['field']['semantics'],value_unit=f['field'].get('value_unit','length'),
                deflection=float(np.linalg.norm(spacing)),certified_bound=None,active_cells=cells,pruned_cells=pruned,
                note='Sparse Lipschitz octree with marching tetrahedra; subcell topology is not certified.')
