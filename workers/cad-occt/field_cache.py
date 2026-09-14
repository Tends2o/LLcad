"""Private bounded samples reusable across resolutions and compact local edits."""
import hashlib, json, math, copy, struct
import numpy as np
from fields import compile_field,affine_constants,rotated_local,rotation_matrix
from geometry import require

MAX_SAMPLES=160000

def relevant(node,point):
    op=node['op']
    if op in ('local_field_delta','local_deform'):
        if np.linalg.norm(np.asarray(point)-np.asarray(node['center'],float)) >= float(node['radius']):
            return relevant(node['source'],point)
        if op=='local_deform': return node
    if op=='transform':
        local=(np.asarray(point)-np.asarray(node['translation'],float))/np.asarray(node['scale'],float)
        return dict(node,source=relevant(node['source'],local))
    if op=='affine_transform':
        inverse,_=affine_constants(tuple(tuple(row) for row in node['matrix']))
        local=inverse@(np.asarray(point)-np.asarray(node['translation'],float))
        return dict(node,source=relevant(node['source'],local))
    if op=='rotate':return dict(node,source=relevant(node['source'],rotated_local(node,point)))
    if 'source' in node: return dict(node,source=relevant(node['source'],point))
    if 'a' in node: return dict(node,a=relevant(node['a'],point),b=relevant(node['b'],point))
    return node

def compile_relevance(node):
    """Merkle projection equivalent to relevant(), with static subtrees hashed once.

    Transforms change the query coordinates, never the cache's world-space point.
    Inside a deformation support the entire source stays relevant: inverse points
    depend on the displacement, so source subtrees must not be pruned there.
    """
    def combine(prefix,children):return hashlib.sha256(prefix+b''.join(children)).digest()
    def visit(n):
        op=n['op'];names=['a','b'] if 'a' in n else ['source'] if 'source' in n else []
        children=[visit(n[name]) for name in names]
        prefix=json.dumps({k:v for k,v in n.items() if k not in names},sort_keys=True,separators=(',',':')).encode()+b'\0'
        full=combine(prefix,[child[1] for child in children])
        dynamic=op in ('local_field_delta','local_deform') or any(child[2] for child in children)
        if not dynamic:return (lambda p:full),full,False
        if op in ('local_field_delta','local_deform'):
            source=children[0][0];center=np.asarray(n['center'],float);radius=float(n['radius'])
            def compact(p):
                if np.linalg.norm(np.asarray(p)-center)>=radius:return source(p)
                return full if op=='local_deform' else combine(prefix,[source(p)])
            return compact,full,True
        if op=='transform':
            source=children[0][0];translation=np.asarray(n['translation'],float);scale=np.asarray(n['scale'],float)
            return (lambda p:combine(prefix,[source((np.asarray(p)-translation)/scale)])),full,True
        if op=='affine_transform':
            source=children[0][0];inverse,_=affine_constants(tuple(tuple(row) for row in n['matrix']));translation=np.asarray(n['translation'],float)
            return (lambda p:combine(prefix,[source(inverse@(np.asarray(p)-translation))])),full,True
        if op=='rotate':
            source=children[0][0];origin=np.asarray(n['origin'],float);angle=n['angle']
            inverse=rotation_matrix(tuple(n['axis']),angle['value'],angle['unit']).T
            return (lambda p:combine(prefix,[source(origin+inverse@(np.asarray(p)-origin))])),full,True
        return (lambda p:combine(prefix,[child[0](p) for child in children])),full,True
    return visit(node)[0]

class Sampler:
    def __init__(self,node,registry,previous=None):
        self.node=copy.deepcopy(node);self.registry=registry
        self.compiled=compile_field(self.node)
        self.project=compile_relevance(self.node)
        self.prefix=hashlib.sha256(registry.encode()).digest()
        self.values=dict(previous.get('values',{})) if previous and previous.get('version')==2 and previous.get('registry')==registry else {}
        require(len(self.values)<=MAX_SAMPLES,'Feldcache überschreitet das Budget.','BUDGET_EXCEEDED')
        self.hits=0;self.misses=0;self.used={};self.points={}
    def __call__(self,point):
        p=tuple(float(x) for x in point)
        require(len(p)==3 and all(math.isfinite(x) for x in p),'Ungültiger Feldprobenpunkt.')
        if p in self.points:
            self.hits+=1
            return self.points[p]
        key=hashlib.sha256(self.prefix+self.project(p)+struct.pack('>ddd',*p)).hexdigest()
        if key in self.used: value=self.used[key];self.hits+=1
        elif key in self.values: value=self.values[key];self.hits+=1
        else:
            value=self.compiled(p);self.misses+=1
        require(isinstance(value,(int,float)) and math.isfinite(value),'Nichtendliche Feldprobe.')
        self.used[key]=value
        self.points[p]=value
        require(len(self.used)<=MAX_SAMPLES,'Feldprobenbudget überschritten.','BUDGET_EXCEEDED')
        return value
    def serialize(self):
        remaining=MAX_SAMPLES-len(self.used)
        old={k:v for k,v in self.values.items() if k not in self.used}
        return {'version':2,'registry':self.registry,'values':{**dict(list(old.items())[:remaining]),**self.used}}
    def metrics(self):return {'sample_cache_hits':self.hits,'sample_cache_misses':self.misses,'retained_samples':len(self.used),
                              'evaluation_backend':'compiled_closures','compiled_nodes':self.compiled.node_count,
                              'compiled_depth':self.compiled.max_depth,'compile_seconds':self.compiled.compile_seconds}
