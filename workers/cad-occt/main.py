"""One bounded job per process. JSON is carried via files to isolate kernel stdout."""
import json, os, sys, time, resource, traceback
from geometry import *
import topology

def run(request):
    if request.get('action')=='solve_constraints':
        from solver import solve
        return dict(status='succeeded',facts={},aggregate=solve(request['solver']),files=[],engine_build=BUILD,metrics={})
    shapes={}; facts={}; histories={}; face_records={}; samplers={}; hits=0; started=time.monotonic()
    plan=request['plan'];features=plan['features'];field_results={}
    for f in features:
        fid=f['id'];op=f['construction']['operator']
        if op=='field':
            from fields import extract, field_facts
            from field_cache import Sampler
            cached='cache/'+f['cache_key']+'.field.json';previous=None
            if os.path.isfile(cached):
                with open(cached) as h:previous=json.load(h)
            samplers[fid]=Sampler(f['construction']['expression'],plan['registry_hash'],previous)
            field_results[fid]=f
            facts[fid]=field_facts(f)
            continue
        deps=[shapes[d] for d in f['depends_on']]
        key=f['cache_key'];cache='cache/'+key+'.brep'
        history_cache='cache/'+key+'.topology.json'
        if os.path.isfile(cache) and os.path.isfile(history_cache):
            shape=read_brep(cache)
            with open(history_cache) as h: records=json.load(h)
            trace=topology.restore(shape,records,key);hits+=1
        else:
            shape,trace=topology.evaluate_feature(f,deps,[histories[d] for d in f['depends_on']])
            records=topology.serialize(trace,key)
        histories[fid]=trace;face_records[fid]=records
        with open('out/'+key+'.topology.json','w') as h:json.dump(records,h,allow_nan=False)
        require(not shape.IsNull(),'Leeres Operatorergebnis.')
        props=properties(shape);require(props['valid'],'OCCT meldet eine ungültige Geometrie.')
        if f['depends_on']:
            f=dict(f,base_operator=next(x['construction']['operator'] for x in features if x['id']==f['depends_on'][0]))
        props['dimensions']=dimensions(f,shape,deps);props['geometry_hash']=shape_hash(shape)
        props['engine_build']=BUILD;props['cache_key']=key
        props['topology']={'version':topology.VERSION,'face_count':len(records['faces']),'tracked_faces':sum(bool(x['origins']) for x in records['faces'])}
        write_brep(shape,'out/'+key+'.brep');shapes[fid]=shape;facts[fid]=props
    output_shapes=[shapes[fid] for fid in plan['outputs'] if fid in shapes]
    root=compound(output_shapes) if len(output_shapes)>1 else output_shapes[0] if output_shapes else None
    aggregate=properties(root) if root is not None else None
    if plan['profile']=='precision_cad' and plan['outputs']:
        require(root is not None and len(output_shapes)==len(plan['outputs']),'CAD-Profil benötigt native Körper.')
        require(all(facts[fid]['precision_solid_only'] and facts[fid]['volume']>0 for fid in plan['outputs']),'Jede Ausgabe des CAD-Profils muss ausschließlich aus Volumenkörpern bestehen.')
        require(aggregate['solids']>0 and aggregate['volume']>0,'CAD-Profil benötigt nichtleere Volumenkörper.')
    files=[];action=request.get('action','evaluate')
    if root is not None:
        write_brep(root,'out/model.brep');files.append('model.brep')
    if action=='render':
        selected=request.get('feature_id');targets=[selected] if selected else plan['outputs'];meshes=[]
        for fid in targets:
            if fid in shapes:meshes.append(mesh(shapes[fid],request['deflection'],fid,face_records[fid]['faces']))
            elif fid in field_results:
                from fields import extract
                meshes.append(extract(field_results[fid],samplers[fid]))
        with open('out/preview.json','w') as h:json.dump({'meshes':meshes,'unit':'mm','quality':'preview_only'},h,allow_nan=False)
        files.append('preview.json')
    if action=='export' and request['format']=='vdb':
        from volume_io import export_vdb
        require(len(plan['outputs'])==1 and plan['outputs'][0] in field_results,'OpenVDB-Ausgabe benötigt genau ein analytisches Feld.','OUT_OF_SCOPE')
        fid=plan['outputs'][0];report=export_vdb(field_results[fid],'out/model.vdb',samplers[fid]);files.append('model.vdb')
        with open('out/roundtrip.json','w') as h:json.dump(report,h,allow_nan=False)
        files.append('roundtrip.json')
    elif action=='export':
        require(root is not None,'Dieses Format benötigt B-Rep-Geometrie.','OUT_OF_SCOPE')
        fmt=request['format'];name='model.'+fmt
        report=export_shape(root,fmt,'out/'+name,request['deflection']);files.append(name)
        with open('out/roundtrip.json','w') as h:json.dump(report,h,allow_nan=False)
        files.append('roundtrip.json')
    if action=='distance':
        evaluator=BRepExtrema_DistShapeShape(shapes[request['feature_id']],shapes[request['other_feature_id']]);evaluator.Perform()
        require(evaluator.IsDone(),'Abstandsauswertung fehlgeschlagen.');aggregate={'distance':evaluator.Value()}
    if action=='analysis':
        from analysis import measure
        aggregate=measure(request,shapes)
    for fid,sampler in samplers.items():
        if sampler.used:
            key=field_results[fid]['cache_key'];name=key+'.field.json'
            with open('out/'+name,'w') as h:json.dump(sampler.serialize(),h,allow_nan=False,separators=(',',':'))
            files.append(name)
    return dict(status='candidate_ready',facts=facts,aggregate=aggregate,files=files,engine_build=BUILD,
                metrics={'seconds':time.monotonic()-started,'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'cache_hits':hits,'total_features':len(features),'field_samples':{fid:s.metrics() for fid,s in samplers.items()}})

if __name__=='__main__':
    resource.setrlimit(resource.RLIMIT_CPU,(40,42))
    resource.setrlimit(resource.RLIMIT_AS,(1536*1024**2,1536*1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE,(34*1024**2,34*1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
    try:
        with open('request.json') as h:request=json.load(h)
        result=run(request)
    except GeometryError as e:result={'status':'failed','error':{'code':e.code,'message':e.message}}
    except MemoryError:result={'status':'failed','error':{'code':'BUDGET_EXCEEDED','message':'Worker-Speicherbudget erschöpft.'}}
    except Exception:
        traceback.print_exc(file=sys.stderr)
        result={'status':'failed','error':{'code':'KERNEL_FAILURE','message':'Native Geometrieoperation fehlgeschlagen.'}}
    with open('result.json','w') as h:json.dump(result,h,allow_nan=False)
