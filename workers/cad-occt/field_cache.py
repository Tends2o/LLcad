"""Private bounded samples reusable across resolutions and compact local edits."""
import hashlib, json, math
import numpy as np
from fields import evaluate
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
    if 'source' in node: return dict(node,source=relevant(node['source'],point))
    if 'a' in node: return dict(node,a=relevant(node['a'],point),b=relevant(node['b'],point))
    return node

class Sampler:
    def __init__(self,node,registry,previous=None):
        self.node=node;self.registry=registry
        self.values=dict(previous.get('values',{})) if previous and previous.get('version')==1 and previous.get('registry')==registry else {}
        require(len(self.values)<=MAX_SAMPLES,'Feldcache überschreitet das Budget.','BUDGET_EXCEEDED')
        self.hits=0;self.misses=0;self.used={}
    def __call__(self,point):
        p=[float(x) for x in point]
        expression=relevant(self.node,p)
        key=hashlib.sha256(json.dumps([self.registry,expression,p],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if key in self.used: value=self.used[key];self.hits+=1
        elif key in self.values: value=self.values[key];self.hits+=1
        else:
            value=evaluate(self.node,p);self.misses+=1
            require(math.isfinite(value),'Nichtendliche Feldprobe.')
        self.used[key]=value
        require(len(self.used)<=MAX_SAMPLES,'Feldprobenbudget überschritten.','BUDGET_EXCEEDED')
        return value
    def serialize(self):
        remaining=MAX_SAMPLES-len(self.used)
        old={k:v for k,v in self.values.items() if k not in self.used}
        return {'version':1,'registry':self.registry,'values':{**dict(list(old.items())[:remaining]),**self.used}}
    def metrics(self):return {'sample_cache_hits':self.hits,'sample_cache_misses':self.misses,'retained_samples':len(self.used)}
