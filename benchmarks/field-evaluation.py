"""Same-point scalar microbenchmark; excludes worker startup, mesh and disk I/O."""
import json, sys, time, platform
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workers/cad-occt'))
from fields import compile_field,evaluate
from field_cache import Sampler

def stats(values):
    values=sorted(values)
    return {'samples':len(values),'p50_ms':values[len(values)//2],'p95_ms':values[-1],'min_ms':values[0],'max_ms':values[-1]}

def measure(function,points):
    started=time.perf_counter()
    values=[function(point) for point in points]
    return (time.perf_counter()-started)*1000,np.asarray(values)

sphere={'op':'sphere','center':['0','0','0'],'radius':'2'}
gyroid={'op':'gyroid','period':'3','origin':['0.1','0.2','0.3'],'threshold':'0.2'}
delta={'op':'local_field_delta','source':sphere,'center':['1','0','0'],'radius':'2','amplitude':'0.1'}
transform={'op':'affine_transform','source':delta,'matrix':[['1','0.2','0'],['0','-1','0.1'],['0','0','1']],'translation':['1','0','0']}
complex_field={'op':'smooth_union','a':transform,'b':{'op':'convert_field_unit','source':gyroid,'to':'length','reference_length':{'value':'0.5','unit':'mm'}},'k':'0.2'}
deformed={'op':'local_deform','source':complex_field,'center':['1','0','0'],'radius':'2','displacement':['0.1','0','0']}
points=np.random.default_rng(186).uniform(-3,3,(2048,3))
cases=[]
for name,node in [('sphere',sphere),('mixed_fields',complex_field),('local_deformation',deformed)]:
    compiled=compile_field(node);reference=lambda point:evaluate(node,point)
    for point in points[:32]:compiled(point);reference(point)
    times={'recursive':[],'compiled':[]};max_error=0.
    for run in range(5):
        order=['recursive','compiled'] if run%2==0 else ['compiled','recursive']
        values={}
        for kind in order:
            elapsed,values[kind]=measure(reference if kind=='recursive' else compiled,points)
            times[kind].append(elapsed)
        max_error=max(max_error,float(np.max(np.abs(values['recursive']-values['compiled']))))
        np.testing.assert_allclose(values['compiled'],values['recursive'],rtol=1e-12,atol=1e-12)
    cold=[];warm=[];cached=[]
    for _ in range(5):
        sampler=Sampler(node,'microbenchmark')
        cold.append(measure(sampler,points)[0])
        warm.append(measure(sampler,points)[0])
        restored=Sampler(node,'microbenchmark',sampler.serialize())
        elapsed,restored_values=measure(restored,points);cached.append(elapsed)
        np.testing.assert_allclose(restored_values,values['recursive'],rtol=1e-12,atol=1e-12)
    cases.append({'name':name,'points_per_batch':len(points),'compiled_nodes':compiled.node_count,'compile_ms':compiled.compile_seconds*1000,
                  'recursive_evaluation':stats(times['recursive']),'compiled_evaluation':stats(times['compiled']),
                  'compiled_p50_speedup':stats(times['recursive'])['p50_ms']/stats(times['compiled'])['p50_ms'],
                  'sampler_cold_values':stats(cold),'sampler_same_worker_repeated_points':stats(warm),
                  'sampler_restored_private_cache':stats(cached),'max_reference_difference':max_error})
print(json.dumps({'python':platform.python_version(),'numpy':np.__version__,'cases':cases,
                  'limitations':['Five batches per case, alternated evaluator order; exploratory microbenchmark, no latency promise.',
                                 'Scalar CPU evaluation only; excludes process startup, geometry validation, triangulation, serialization and LLM latency.',
                                 'Same-worker repeated points and restored-cache hits are measured separately from cold values.']},allow_nan=False))
