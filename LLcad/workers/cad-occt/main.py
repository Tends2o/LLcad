"""One bounded job per process. JSON is carried via files to isolate kernel stdout."""
import json, os, sys, time, traceback, shutil, math
import hostlimits
from hostlimits import resource
import numpy as np
from geometry import *
import topology
import frames
import mesh_quality

def conversion_report(shape,source,deflection,tolerance):
    """B-Rep to indexed mesh: sampled centroid distances to the native surface."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    import numpy as np
    vertices=np.array(source['vertices']);triangles=source['triangles']
    step=max(1,len(triangles)//256);measured=0.
    for t in triangles[::step]:
        centroid=vertices[t].mean(axis=0)
        evaluator=BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float,centroid))).Shape(),shape);evaluator.Perform()
        require(evaluator.IsDone(),'Conversion measurement failed.')
        measured=max(measured,evaluator.Value())
    return {'source_representation':'brep','target_representation':'mesh','algorithm_build':BUILD,'unit_transform':'identity_mm',
            'requested_error':deflection,'measured_error':measured,'measured_error_guarantee':'sampled_triangle_centroids','samples':len(triangles[::step]),
            'certified_bound_or_null':None,'topology_changes':['faces_triangulated','exactly_coincident_nodes_indexed'],
            'lost_semantics':['analytic_surfaces','curve_edges'],'unresolved_regions':[],'model_tolerance':tolerance,
            'authority_note':'The mesh feature is a separate explicit authority; the B-Rep source keeps its own precision.'}

def field_conversion_report(f,sampler,source,tolerance):
    """Field to mesh: sampled first-order distance estimate |f|/|grad f| at vertices."""
    import numpy as np
    compiled=sampler.compiled;rotation,translation=frames.arrays(f.get('placement'))
    estimates=[]
    for p in source['vertices'][::max(1,len(source['vertices'])//512)]:
        local=rotation.T@(np.array(p)-translation);value=compiled(local);g=np.linalg.norm(compiled.gradient(local))
        if g>1e-9:estimates.append(abs(value)/g)
    return {'source_representation':'implicit','target_representation':'mesh','algorithm_build':BUILD,'unit_transform':'identity_mm',
            'requested_error':f['field']['cell_size'],'measured_error':max(estimates) if estimates else None,
            'measured_error_guarantee':'sampled_first_order_vertex_distance_estimate','samples':len(estimates),
            'certified_bound_or_null':None,'topology_changes':['isosurface_sampled_on_uniform_leaf_cells'],
            'lost_semantics':['analytic_field_expression','field_semantics_'+f['field']['semantics']],'unresolved_regions':['subcell_topology'],
            'model_tolerance':tolerance,'extraction':{k:source.get(k) for k in ('method','pruning','active_cells','pruned_cells')}}

# Facts every native output carries; the model aggregate is combined from them.
AGGREGATE_KEYS=('volume','area','bounds','native_tolerances_mm','solids','precision_solid_only','faces','valid')
# Operators whose result is a rigid copy (or a compound of rigid copies) of their
# inputs: validity, volume, area, face/solid counts and tolerances do not change.
RIGID_OPERATORS=('rotate','mirror','pattern','circular_pattern','assembly')

def combine_facts(items):
    """Facts of a compound of the given shapes, from their own facts: volume, area,
    faces and solids add up (a compound's integrals and counts run over every
    occurrence), bounds unite, tolerances take the maximum, validity and
    solid-only-ness must hold for every part (BRepCheck of a compound checks its
    children). None when a part lacks one of these facts."""
    if not items or any(item is None or any(k not in item for k in AGGREGATE_KEYS) or not item['bounds'] for item in items):return None
    solid_only=all(item['precision_solid_only'] for item in items)
    volumes=[item['volume'] for item in items]
    estimates=[item.get('volume_integration_relative_error_estimate') for item in items]
    tolerances={name:max(item['native_tolerances_mm'].get(name,0.) for item in items) for name in ('vertex','edge','face')}
    return dict(volume=sum(volumes) if solid_only and all(v is not None for v in volumes) else None,
                area=sum(item['area'] for item in items),
                bounds=[*(min(item['bounds'][i] for item in items) for i in range(3)),*(max(item['bounds'][i+3] for item in items) for i in range(3))],
                volume_integration_relative_error_estimate=max(estimates) if solid_only and all(e is not None for e in estimates) else None,
                native_tolerances_mm=tolerances,solids=sum(item['solids'] for item in items),precision_solid_only=solid_only,
                faces=sum(item['faces'] for item in items),valid=all(item['valid'] for item in items))

# Parallel evaluation of independent subtrees (opt-in). A cold or largely dirty
# build is a forest of part subtrees that share a few inputs. With
# `parallel_eval` in the request (the gateway sets it from LLCAD_PARALLEL_EVAL)
# the run builds the shared inputs first, then forks and lets up to `processes`
# processes claim whole subtrees. Each process writes exactly the content-keyed
# archives and facts a serial run writes; the parent reads back only what it
# still needs and reports every feature in plan order, so the result is the
# serial one. Without the setting, or below the thresholds, nothing changes.
PARALLEL_LIMIT=4
PARALLEL_MIN_FREE_MB=1536
PARALLEL_MIN_FEATURES=24
PARALLEL_MIN_SECONDS=4.0
PARALLEL_ACTIONS=('evaluate','render','export')
MESH_OPERATORS=('mesh_repair','remesh_region','local_mesh_deform','tessellate','extract_isosurface')
# Estimated seconds per feature: base + per-ancestor cost by operator (a result's
# face count grows with what it is built from), fitted to the three instrument
# replays on the current kernel in one process. They only size and order the
# subtrees and decide whether forking pays off, never what is built.
OPERATOR_COST={'difference':(0.21,0.006),'union':(0.2,0.003),'intersection':(0.,0.016),'rotate':(0.,0.017),
               'affine_transform':(0.06,0.011),'transform':(0.,0.008),'assembly':(0.,0.0035),'pattern':(0.,0.01),
               'circular_pattern':(0.07,0.0024),'mirror':(0.,0.0056),'strip':(0.05,0.),'extrude':(0.023,0.),
               'loft':(0.06,0.001),'revolve':(0.018,0.),'box':(0.002,0.),'cylinder':(0.002,0.),'sphere':(0.002,0.),
               'cone':(0.002,0.),'profile':(0.002,0.),'nurbs_curve':(0.002,0.)}
DEFAULT_COST=(0.1,0.005)
PARALLEL_SHARE=1.5  # a merged subtree stays below 1/(SHARE*processes) of the work

def native_brep(f):
    """Whether the evaluation loop builds this feature as a native B-Rep (and so may
    hand it to another process); meshes, fields and their imports stay with the parent."""
    op=f['construction']['operator']
    if op in MESH_OPERATORS or op=='field':return False
    if op=='imported' and f['construction'].get('format') in ('stl','vdb'):return False
    return f.get('authoritative_representation','brep')=='brep'

def parallel_settings(request):
    """The requested process count (1 = serial), or None when nothing was asked for.
    The sandbox gets it in the request; the environment only serves direct runs."""
    value=request.get('parallel_eval')
    if value is None and os.environ.get('LLCAD_PARALLEL_EVAL'):value=os.environ['LLCAD_PARALLEL_EVAL']
    if value is None:return None
    if not isinstance(value,dict):value={'processes':value}
    try:processes=int(value.get('processes',1))
    except (TypeError,ValueError):return None
    return dict(value,processes=max(1,min(PARALLEL_LIMIT,processes)))

available_mb = hostlimits.available_mb

def native_threads():
    """Threads of this process, native ones included. A fork is only safe while the
    kernel's thread pool has not started (geometry.py starts it lazily)."""
    try:
        with open('/proc/self/status') as h:
            for line in h:
                if line.startswith('Threads:'):return int(line.split()[1])
    except OSError:pass
    return None

def parallel_partition(features,needed,processes):
    """Dirty features split into a shared prefix and independent subtrees.

    A feature is dirty when evaluating it writes something (archive or facts); only
    native B-Rep features move, and a dirty feature built on a mesh or field stays
    with the parent ('held'). Every dirty feature is owned by the dirty sinks (part
    outputs and other ends of the graph) that consume it. Sinks that share inputs
    are merged while the merged subtree stays within an even share of the work;
    what is still shared by two subtrees forms the prefix, which is ancestor-closed
    and is built before the fork. Subtrees come largest (estimated) first."""
    exists=os.path.isfile
    def archived(key):return exists('cache/'+key+'.brep') and exists('cache/'+key+'.topology.json')
    order={f['id']:i for i,f in enumerate(features)};by_id={f['id']:f for f in features}
    dirty={};held=set();keys=set();collision=False;ancestors={}
    for f in features:
        fid=f['id'];key=f['cache_key'];local=f.get('local_cache_key',key)
        upstream=set()
        for d in f['depends_on']:
            if d in ancestors:upstream.add(d);upstream|=ancestors[d]
        ancestors[fid]=upstream
        if not native_brep(f):held.add(fid);continue
        facts_ok=exists('cache/'+key+'.facts.json')
        if fid not in needed and facts_ok:continue
        if archived(local) and (local==key or archived(key)) and facts_ok:continue
        if any(d in held for d in f['depends_on']):held.add(fid);continue
        base,per=OPERATOR_COST.get(f['construction']['operator'],DEFAULT_COST)
        dirty[fid]=max(0.002,base+per*len(upstream))
        collision=collision or key in keys or local in keys;keys.update((key,local))
    users={fid:[] for fid in dirty}
    for fid in dirty:
        for d in by_id[fid]['depends_on']:
            if d in dirty and fid not in users[d]:users[d].append(fid)
    owners={}
    for fid in sorted(dirty,key=order.get,reverse=True):
        own=set() if users[fid] else {fid}
        for u in users[fid]:own|=owners[u]
        owners[fid]=frozenset(own)
    parent={s:s for s in owners if not users[s]}
    def find(s):
        while parent[s]!=s:parent[s]=parent[parent[s]];s=parent[s]
        return s
    def roots(fid):return {find(s) for s in owners[fid]}
    shared={}
    for fid,w in dirty.items():
        if len(owners[fid])>1:shared[owners[fid]]=shared.get(owners[fid],0.)+w
    cap=sum(dirty.values())/(PARALLEL_SHARE*max(1,processes))
    for signature,_ in sorted(shared.items(),key=lambda kv:(-kv[1],min(order[s] for s in kv[0]))):
        joined=sorted({find(s) for s in signature},key=order.get)
        if len(joined)<2:continue
        members=set(joined)
        merged=sum(w for fid,w in dirty.items() if roots(fid)<=members)
        if merged>cap:continue
        for r in joined[1:]:parent[r]=joined[0]
    prefix=[];groups={}
    for fid in sorted(dirty,key=order.get):
        r=roots(fid)
        if len(r)>1:prefix.append(fid)
        else:groups.setdefault(next(iter(r)),[]).append(fid)
    ordered=sorted(groups.values(),key=lambda g:(-sum(dirty[x] for x in g),order[g[0]]))
    return dict(prefix=prefix,groups=ordered,dirty=dirty,held=held,collision=collision)

def evaluate_in_parallel(features,needed,settings,process,foreign):
    """Build the prefix here, fork, and let every process claim subtrees.

    Returns (fids processed in this process, fids built by forked processes,
    partition, metrics). Anything neither covers - held features, a subtree whose
    process failed or vanished - is left to the caller's serial pass in plan order,
    which also reproduces the first failure exactly where a serial run meets it."""
    import geometry
    processes=settings['processes'];started=time.monotonic()
    metrics={'requested':settings.get('requested',processes),'processes':1}
    if settings.get('gateway_headroom_mb') is not None:metrics['gateway_headroom_mb']=settings['gateway_headroom_mb']
    part=parallel_partition(features,needed,processes)
    dirty,groups,prefix=part['dirty'],part['groups'],part['prefix']
    grouped=sum(dirty[x] for g in groups for x in g)
    metrics.update(dirty_features=len(dirty),held_features=len(part['held']),prefix_features=len(prefix),subtrees=len(groups),
                   estimated_seconds={'prefix':round(sum(dirty[x] for x in prefix),2),'subtrees':round(grouped,2),
                                      'largest_subtree':round(max((sum(dirty[x] for x in g) for g in groups),default=0.),2)})
    reason=None
    if len(groups)<2:reason='fewer_than_two_subtrees'
    elif len(dirty)<int(settings.get('min_features',PARALLEL_MIN_FEATURES)):reason='small_dirty_set'
    elif grouped<float(settings.get('min_seconds',PARALLEL_MIN_SECONDS)):reason='little_parallel_work'
    elif part['collision']:reason='shared_cache_keys'
    elif not hasattr(os,'fork'):reason='no_fork_on_host'
    free=available_mb();metrics['free_mb']=free
    if reason is None and free is not None and free<int(settings.get('min_free_mb',PARALLEL_MIN_FREE_MB)):reason='memory'
    if reason is not None:
        metrics['skipped']=reason
        return set(),set(),part,metrics
    by_id={f['id']:f for f in features};done=set()
    def build(fid):
        # A feature's inputs are loaded (or reported from their facts) first, exactly
        # as the serial loop has done by the time it reaches the feature.
        for d in by_id[fid]['depends_on']:
            if d not in done:process(by_id[d]);done.add(d)
        process(by_id[fid]);done.add(fid)
    # The prefix runs before the fork without the parallel checker: the verdict is
    # the same, and OCCT's thread pool must not be running when the process forks.
    serial_check=geometry.PARALLEL_CHECK_FACES;geometry.PARALLEL_CHECK_FACES=float('inf')
    try:
        for fid in prefix:build(fid)
    except Exception:
        metrics['skipped']='prefix_failed'
        return done,set(),part,metrics
    finally:geometry.PARALLEL_CHECK_FACES=serial_check
    metrics['prefix_seconds']=round(time.monotonic()-started,3)
    threads=native_threads()
    if threads is not None and threads>1:
        metrics['skipped']='threads_running';metrics['threads']=threads
        return done,set(),part,metrics
    os.makedirs('par/claims',exist_ok=True)
    def claim_and_build(role):
        built=[];failure=None;t0=time.monotonic()
        try:
            for index,group in enumerate(groups):
                if os.path.exists('par/failed'):break
                try:os.close(os.open('par/claims/%d'%index,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600))
                except FileExistsError:continue
                for fid in group:
                    build(fid);built.append(fid)
        # A failure stops this process's claims (and, through the marker, everyone's);
        # the caller's serial pass meets it again in plan order. A budget stop is not
        # caught: in a fork it ends the fork without a report, in the parent the job.
        except GeometryError as e:failure={'code':e.code,'message':e.message}
        except MemoryError:failure={'code':'BUDGET_EXCEEDED','message':'memory'}
        except Exception as e:failure={'code':'KERNEL_FAILURE','message':type(e).__name__}
        if failure is not None:
            try:os.close(os.open('par/failed',os.O_CREAT|os.O_WRONLY,0o600))
            except OSError:pass
        return {'role':role,'built':built,'failure':failure,'seconds':round(time.monotonic()-t0,3),
                'cpu_seconds':round(sum(resource.getrusage(resource.RUSAGE_SELF)[:2]),3),
                'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    children=[];reaped=False
    try:
        for index in range(1,processes):
            pid=os.fork()
            if pid==0:
                code=0
                try:
                    try:
                        import ctypes,signal
                        ctypes.CDLL(None).prctl(1,signal.SIGKILL)  # PR_SET_PDEATHSIG: never outlive the parent
                    except Exception:pass
                    report=claim_and_build('fork-%d'%index)
                    write_json('par/%d.tmp'%index,report);os.replace('par/%d.tmp'%index,'par/%d.json'%index)
                except BaseException:code=1
                finally:os._exit(code)
            children.append((index,pid))
        metrics['processes']=1+len(children)
        own=claim_and_build('parent')
        waited=time.monotonic()
        for _,pid in children:os.waitpid(pid,0)
        reaped=True
    finally:
        # A budget stop (or any other escape) of the parent ends its forks before the
        # result is written, so no archive is still being written while the gateway keeps them.
        if not reaped:
            import signal
            for _,pid in children:
                try:os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
                except OSError:pass
    def archived(key):
        return any(os.path.isfile(base+key+'.brep') and os.path.isfile(base+key+'.topology.json') for base in ('cache/','out/'))
    external=set();reports=[own]
    for index,_ in children:
        try:
            with open('par/%d.json'%index) as h:report=json.load(h)
        except Exception:
            reports.append({'role':'fork-%d'%index,'built':[],'failure':{'code':'KERNEL_FAILURE','message':'no report'}});continue
        reports.append(report)
        for fid in report['built']:
            # Only complete pairs count; a feature without them is rebuilt by the serial pass.
            f=by_id[fid];key=f['cache_key'];local=f.get('local_cache_key',key)
            if archived(key) and archived(local) and (os.path.isfile('out/'+key+'.facts.json') or os.path.isfile('cache/'+key+'.facts.json')):
                external.add(fid);foreign.update((key,local))
    metrics.update(wait_seconds=round(time.monotonic()-waited,3),
                   per_process=[{k:r.get(k) for k in ('role','seconds','cpu_seconds','peak_rss_kib','failure')}|{'features':len(r.get('built',[]))} for r in reports],
                   peak_rss_children_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    return done,external,part,metrics

def run(request):
    if request.get('action')=='mesh_check':
        restored=mesh_quality.combine(request['meshes'])
        report={'mesh_quality':mesh_quality.inspect_mesh(restored)}
        if 'original_meshes' in request:
            report['measured_vertex_error_bound_mm']=mesh_quality.correspondence_bound(mesh_quality.combine(request['original_meshes']),restored)
            require(report['measured_vertex_error_bound_mm']<=request['tolerance'],
                    'GLB quantisation exceeds the required geometry accuracy.','PRECISION_UNSUPPORTED')
        return dict(status='candidate_ready',facts={},aggregate=report,files=[],engine_build=BUILD,metrics={})
    if request.get('action')=='probe_step':
        import step_structure
        report=step_structure.probe('input-'+request['artifact_id']+'.step')
        return dict(status='succeeded',facts={},aggregate=report,files=[],engine_build=BUILD,metrics={})
    if request.get('action')=='solve_constraints':
        from solver import solve
        return dict(status='succeeded',facts={},aggregate=solve(request['solver']),files=[],engine_build=BUILD,metrics={})
    shapes={}; local_shapes={}; local_histories={}; facts={}; histories={}; face_records={}; samplers={}; hits=0; started=time.monotonic()
    plan=request['plan'];features=plan['features'];field_results={};mesh_results={}
    import advanced as _adv; _adv.ACTIVE_PLAN=plan
    definitions={f['id']:f for f in features}
    # What is already in the cache is read, never written back: a job that
    # re-emits the whole model spends its budget copying bytes that the store
    # already has, which is what made a one-parameter change cost half a minute.
    # Keys a forked process of this run built (parallel evaluation) are read from
    # out/, where that process archived them, bound to the same sha256.
    foreign=set()
    def cached_shape(key):
        cache='cache/'+key+'.brep';history_cache='cache/'+key+'.topology.json'
        if not (os.path.isfile(cache) and os.path.isfile(history_cache)):
            if key not in foreign:return None
            cache='out/'+key+'.brep';history_cache='out/'+key+'.topology.json'
        shape=read_brep(cache)
        with open(history_cache) as h:records=json.load(h)
        with open(cache,'rb') as h:source_hash=hashlib.sha256(h.read()).hexdigest()
        trace=topology.restore(shape,records,key,source_hash)
        return shape,trace,records
    def history_file(key,records,fresh):
        if fresh:write_json('out/'+key+'.topology.json',records)
    # The measured facts of a feature belong to its cache key just as much as its
    # shape does, so an unchanged feature is never measured twice.
    def cached_facts(key):
        path='cache/'+key+'.facts.json'
        if not os.path.isfile(path) and key in foreign:path='out/'+key+'.facts.json'
        if not os.path.isfile(path):return None
        try:
            with open(path) as h:return json.load(h)
        except Exception:return None
    def facts_file(key,props):
        write_json('out/'+key+'.facts.json',props)
    def derived_facts(f,world_bounds):
        """A rigid move changes where a shape is, not what it is: validity, volume,
        area, counts and native tolerances come from the source's facts, bounds are
        measured on the moved shape. Marked, so no one mistakes them for a fresh
        BRepCheck of the copy. None when the move is not rigid or a source lacks facts."""
        op=f['construction']['operator']
        rigid=op in RIGID_OPERATORS or op in ('transform','instance') and float(f['values'].get('scale',1))==1 \
              or op=='affine_transform' and f['id'] in _adv.RIGID_COPIES
        if not rigid or not f['depends_on']:return None
        if op in ('pattern','circular_pattern'):sources=[f['depends_on'][slot] for _,slot,_ in pattern_placements(f)]
        elif op=='assembly':sources=list(f['depends_on'])
        else:sources=[f['depends_on'][0]]
        combined=combine_facts([facts.get(d) for d in sources])
        if combined is None:return None
        require(world_bounds and all(math.isfinite(x) for x in world_bounds),'Non-finite or empty geometry.')
        combined['bounds']=world_bounds
        combined['derivation']={'method':'derived_from_source','operator':op,'sources':sorted(set(sources)),
                                'measured':['bounds'],'carried_over':['valid','volume','area','faces','solids','native_tolerances_mm','precision_solid_only']}
        return combined
    def ensure_shape(fid):
        """A native output's shape for the few actions that need it after the loop
        (export, manufacturing checks, render, measurements). The loop only opens
        what it builds or what the gateway listed; the staged cache has the rest."""
        if fid in shapes:return shapes[fid]
        f=definitions[fid];key=f['cache_key'];local_key=f.get('local_cache_key',key)
        world=cached_shape(key)
        if world:shape,trace,records=world
        else:
            local=cached_shape(local_key)
            require(local is not None,'Required geometry is missing from the cache.','INTEGRITY_FAILURE')
            shape,trace=frames.place(local[0],local[1],f.get('placement'))
            shape,trace,records=topology.archive(shape,trace,key,'out/'+key+'.brep')
            history_file(key,records,True)
        shapes[fid]=shape;histories[fid]=trace;face_records[fid]=records
        return shape
    # Which shapes this run has to hold. The gateway staged the cache and knows
    # exactly which features it left out, so it says so; without that list every
    # feature is rebuilt, which is what any older caller expects.
    needed=set(request['shapes_needed']) if request.get('shapes_needed') is not None else {f['id'] for f in features}
    def process(f):
        """One feature: build or read back its shape, measure or read back its facts.
        The body of the serial loop; forked processes run it for their subtrees."""
        nonlocal hits
        fid=f['id'];op=f['construction']['operator']
        if op=='imported' and f['construction']['format']=='stl':
            source=mesh_quality.read_stl('input-'+f['construction']['artifact_id']+'.stl',f['construction']['source_unit'])
            require(source['source_conversion']['coordinate_error_bound_mm']<=plan['tolerance'],
                    'STL unit conversion exceeds the model accuracy.','PRECISION_UNSUPPORTED')
            local_bounds=[*[min(p[i] for p in source['vertices']) for i in range(3)],*[max(p[i] for p in source['vertices']) for i in range(3)]]
            source=frames.world_mesh(source,f.get('placement'))
            source.update(feature_id=fid,quality='preview_only',deflection=0.,certified_bound=None)
            mesh_results[fid]=source;facts[fid]=mesh_quality.mesh_facts(source,f)
            facts[fid]['local_bounds']=local_bounds
            require(facts[fid]['valid'],'The mesh contains degenerate or duplicate triangles.')
            write_json('out/'+f['cache_key']+'.mesh.json',source,compact=True)
            return
        if op in ('mesh_repair','remesh_region','local_mesh_deform','tessellate','extract_isosurface'):
            import mesh_ops
            dep=f['depends_on'][0];p=f['values'];c=f['construction'];report=None
            if op=='tessellate':
                require(dep in shapes,'Tessellation needs a B-Rep input.','OUT_OF_SCOPE')
                source=mesh_quality.index_exact(mesh(shapes[dep],p['deflection'],fid,face_records[dep]['faces']))
                report=conversion_report(shapes[dep],source,p['deflection'],plan['tolerance'])
            elif op=='extract_isosurface':
                from fields import extract
                require(dep in field_results,'Isosurface extraction needs an analytic field.','OUT_OF_SCOPE')
                source=frames.world_mesh(extract(field_results[dep],samplers[dep]),field_results[dep].get('placement'))
                report=field_conversion_report(field_results[dep],samplers[dep],source,plan['tolerance'])
            else:
                require(dep in mesh_results,'A mesh operator needs an authoritative mesh.','OUT_OF_SCOPE')
                base=mesh_results[dep]
                if op=='mesh_repair':
                    options={k:c[k] for k in ('remove_degenerate','remove_duplicates','orient','fill_holes_max_edges') if k in c}
                    if 'weld_tolerance' in p:options['weld_tolerance']=p['weld_tolerance']
                    source,report=mesh_ops.repair(base,options)
                elif op=='remesh_region':
                    source,report=mesh_ops.remesh_region(base,[p.get('x',0),p.get('y',0),p.get('z',0)],p['radius'],p['target_edge'],int(p.get('iterations',3)))
                else:
                    handles=[{'point':[float(x) for x in h['point']],'displacement':[float(x) for x in h['displacement']]} for h in c['handles']]
                    source,report=mesh_ops.arap_deform(base,[p.get('x',0),p.get('y',0),p.get('z',0)],p['radius'],handles,int(p.get('iterations',8)))
            source=dict(source,feature_id=fid,quality='preview_only',deflection=source.get('deflection',0.),certified_bound=None,coordinate_frame='world')
            mesh_results[fid]=source;facts[fid]=mesh_quality.mesh_facts(source,f)
            facts[fid]['local_bounds']=facts[fid]['bounds']
            facts[fid]['conversion_report' if op in ('tessellate','extract_isosurface') else 'operation_report']=report
            require(facts[fid]['valid'],'The mesh operation produced degenerate or duplicate triangles.')
            write_json('out/'+f['cache_key']+'.mesh.json',source,compact=True)
            return
        if op=='field' or (op=='imported' and f['construction']['format']=='vdb'):
            from fields import extract, field_facts
            from field_cache import Sampler
            import_report=None
            if op=='imported':
                from volume_io import import_field
                f,import_report=import_field(f)
            cached='cache/'+f['cache_key']+'.field.json';previous=None
            if os.path.isfile(cached):
                with open(cached) as h:previous=json.load(h)
            samplers[fid]=Sampler(f['construction']['expression'],plan['registry_hash'],previous)
            field_results[fid]=f
            facts[fid]=field_facts(f,samplers[fid].compiled)
            if import_report:facts[fid]['import_report']=import_report;facts[fid]['coverage']='sampled_grid_trilinear_interpolant_without_continuous_certificate'
            if f.get('blend_free_regions'):
                from intervals import blend_activity
                facts[fid]['blend_free_regions']={region['id']:blend_activity(f['construction']['expression'],{'min':region['min'],'max':region['max']},f['field']['cell_size']) for region in f['blend_free_regions']}
            facts[fid]['local_bounds']=facts[fid]['bounds']
            facts[fid]['bounds']=frames.world_bounds(facts[fid]['bounds'],f.get('placement'))
            facts[fid]['local_frame']=f.get('local_frame','world')
            facts[fid]['coordinate_frame']='world'
            return
        key=f['cache_key'];local_key=f.get('local_cache_key',key)
        if fid not in needed:
            # Nothing in this run builds on this feature, and what it measures is
            # already known: it is reported from its facts and never reopened.
            stored=cached_facts(key)
            if stored is not None:
                facts[fid]=stored;hits+=1
                return
        # The inputs are only placed when something is actually built from them:
        # a feature that comes back from the cache must not force the run to hold
        # everything underneath it.
        def inputs(f=f):
            converted=[frames.place(local_shapes[d],local_histories[d],definitions[d].get('placement'),f.get('placement')) for d in f['depends_on']]
            return [item[0] for item in converted],[item[1] for item in converted]
        deps=None
        cached=cached_shape(local_key)
        if cached:
            local_shape,local_trace,local_records=cached;hits+=local_key not in foreign
        else:
            deps,dep_histories=inputs()
            local_shape,local_trace=topology.evaluate_feature(f,deps,dep_histories)
            local_shape,local_trace,local_records=topology.archive(local_shape,local_trace,local_key,'out/'+local_key+'.brep')
        local_shapes[fid]=local_shape;local_histories[fid]=local_trace
        history_file(local_key,local_records,not cached)
        world_cached_used=False
        if local_key==key:shape,trace,records=local_shape,local_trace,local_records
        else:
            world_cached=cached_shape(key)
            if world_cached:shape,trace,records=world_cached;world_cached_used=True
            else:
                shape,trace=frames.place(local_shape,local_trace,f.get('placement'))
                shape,trace,records=topology.archive(shape,trace,key,'out/'+key+'.brep')
        histories[fid]=trace;face_records[fid]=records
        history_file(key,records,not cached or local_key!=key and not world_cached_used)
        require(not shape.IsNull(),'Empty operator result.')
        stored=cached_facts(key)
        if stored is not None:
            # The shape was needed for something else, its measurements were not.
            shapes[fid]=shape;facts[fid]=stored
            return
        # Optimal bounds are computed once per shape: the world box serves the facts
        # and the local box both the dimensions and local_bounds.
        world_box=bounds(shape)
        local_box=world_box if local_shape is shape else bounds(local_shape)
        props=derived_facts(f,world_box) or properties(shape,world_box)
        require(props['valid'],'OCCT reports invalid geometry.')
        if plan['profile'] in ('precision_cad','manufacturing_candidate'):
            require(max(props['native_tolerances_mm'].values())<=plan['tolerance'],
                    'Native boundary tolerances exceed the required model accuracy.','PRECISION_UNSUPPORTED')
        if f['depends_on']:
            base=definitions[f['depends_on'][0]]
            f=dict(f,base_operator=base['construction']['operator'] if base.get('local_frame','world')==f.get('local_frame','world') else 'transformed_source')
        # Only the cut operators measure against their input; every other
        # operator's dimensions come from its own shape.
        if deps is None and op in ('hole','groove','pocket'):deps,_=inputs()
        own=topology.feature_transform(f)
        if own is not None and op in DIMENSIONED:
            # Dimensions are measured in the feature's own construction frame (plane /
            # placement), where width, depth and height are axis-aligned again.
            back=own.Inverted()
            props['dimensions']=dimensions(f,BRepBuilderAPI_Transform(local_shape,back,False).Shape(),
                                           [BRepBuilderAPI_Transform(d,back,False).Shape() for d in deps] if deps else deps)
        else:props['dimensions']=dimensions(f,local_shape,deps,local_box)
        props['geometry_hash']=records['brep_sha256']
        props['local_frame']=f.get('local_frame','world');props['local_bounds']=local_box
        props['coordinate_frame']='world';props['dimension_frame']=f.get('local_frame','world')
        props['engine_build']=BUILD;props['cache_key']=key
        import advanced
        if fid in advanced.CONSTRUCTION_REPORTS:props['construction_report']=advanced.CONSTRUCTION_REPORTS[fid]
        props['topology']={'version':topology.VERSION,'face_count':len(records['faces']),'tracked_faces':sum(bool(x['origins']) for x in records['faces'])}
        facts_file(key,props)
        shapes[fid]=shape;facts[fid]=props
    parallel=None;done=set();external=set();rest=set();keep=set();forked=False
    settings=parallel_settings(request) if request.get('action','evaluate') in PARALLEL_ACTIONS else None
    if settings and settings['processes']>1:
        done,external,part,parallel=evaluate_in_parallel(features,needed,settings,process,foreign)
        forked=parallel['processes']>1
        # What this process still builds itself (held features, subtrees a fork did
        # not finish), and the inputs that reads.
        rest={fid for fid in (*part['dirty'],*part['held']) if fid not in done and fid not in external}
        keep={d for f in features if f['id'] in rest for d in f['depends_on']}
    elif settings and settings.get('skipped'):
        # The gateway kept this job serial (memory headroom, box limit): say so.
        parallel={k:settings.get(k) for k in ('requested','skipped','gateway_headroom_mb')}|{'processes':1}
    for f in features:
        if f['id'] in done:continue
        if forked and f['id'] not in rest and f['id'] not in keep and native_brep(f):
            # Built by a fork, or unchanged and read by nothing still to build here:
            # reported from its facts and never reopened.
            stored=cached_facts(f['cache_key'])
            if stored is not None:
                facts[f['id']]=stored;hits+=f['id'] not in external
                continue
        process(f)
    if parallel is not None:
        # Plan order, as the serial loop reports it.
        ordered={f['id']:facts[f['id']] for f in features if f['id'] in facts}
        facts.clear();facts.update(ordered)
        if 'dirty_features' in parallel:
            parallel.update(serial_pass_built=len(rest),external_features=len(external),seconds=round(time.monotonic()-started,3))
    action=request.get('action','evaluate')
    # Measurements answer about named features of an evaluated revision: they do
    # not re-measure or re-check the whole model (the gateway sends them a plan
    # trimmed to those features and what is missing underneath them).
    measuring=action in ('distance','contact_graph','analysis')
    def native(fid):
        return fid in definitions and fid not in field_results and fid not in mesh_results and definitions[fid].get('authoritative_representation','brep')=='brep'
    native_outputs=[fid for fid in plan['outputs'] if native(fid)]
    root=None
    def output_root():
        shapes_out=[ensure_shape(fid) for fid in native_outputs]
        return compound(shapes_out) if len(shapes_out)>1 else shapes_out[0] if shapes_out else None
    aggregate=None
    if not measuring and plan['outputs']:
        if any(fid in field_results or fid in mesh_results for fid in plan['outputs']):
            declared=[facts[fid]['bounds'] for fid in plan['outputs']]
            aggregate={'valid':all(facts[fid]['valid'] for fid in plan['outputs']),
                       'bounds':[*(min(b[i] for b in declared) for i in range(3)),*(max(b[i+3] for b in declared) for i in range(3))],
                       'volume':None,'area':None,'solids':None,'precision_solid_only':False,
                       'coverage':'declared_world_bounds_without_implicit_volume_certificate'}
        else:
            # The model is the compound of its outputs, and every output's facts are
            # already measured (and cached with its key): the aggregate follows from
            # them without re-checking and re-integrating the whole compound.
            aggregate=combine_facts([facts.get(fid) for fid in plan['outputs']])
            if aggregate is not None:aggregate['aggregation']='combined_from_per_output_facts'
            else:
                root=output_root();aggregate=properties(root) if root is not None else None
    if plan['outputs'] and all(fid in mesh_results for fid in plan['outputs']):
        combined=mesh_quality.combine([mesh_results[fid] for fid in plan['outputs']])
        aggregate=mesh_quality.mesh_facts(combined,{'cache_key':'aggregate'})
    if plan['profile']=='manufacturing_candidate' and plan['outputs'] and not measuring:
        import distances
        rules=plan.get('manufacturing') or {}
        require(len(native_outputs)==len(plan['outputs']),'The manufacturing candidate profile needs native solids.','OUT_OF_SCOPE')
        build=np.array(rules.get('build_direction',[0,0,1]),float);build=build/np.linalg.norm(build)
        for fid in plan['outputs']:
            report={'process':rules.get('process'),'wall_thickness':distances.wall_thickness(ensure_shape(fid)),'guarantee':'sampled','certified':False}
            if rules.get('maximum_overhang_deg') is not None:
                # Overhang = angle of a downward-facing surface against the build direction; faces
                # resting on the build plate (lowest height) are supported and skipped.
                limit=math.radians(float(rules['maximum_overhang_deg']));worst=0.0;count=0
                samples=distances.surface_samples(shapes[fid],200,23)
                floor=min(float(np.dot(np.array(point),build)) for point,_ in samples)
                for point,normal in samples:
                    cosine=float(np.dot(np.array(normal),build))
                    if cosine<-1e-9 and float(np.dot(np.array(point),build))>floor+1e-6:
                        overhang=math.pi/2-math.acos(max(-1.0,min(1.0,-cosine)));worst=max(worst,overhang);count+=1
                report['overhang']={'maximum_sampled_deg':math.degrees(worst),'downward_facing_samples':count,'limit_deg':float(rules['maximum_overhang_deg']),
                                    'build_plate_faces_skipped':True,'guarantee':'sampled'}
            facts[fid]['manufacturing_rules']=report
    if plan['profile'] in ('precision_cad','manufacturing_candidate') and plan['outputs'] and not measuring:
        require(len(native_outputs)==len(plan['outputs']) and aggregate is not None,'The CAD profile needs native solids.')
        require(all(facts[fid]['precision_solid_only'] and facts[fid]['volume']>0 for fid in plan['outputs']),'Every output of the CAD profile must consist of solids only.')
        require(aggregate['solids']>0 and aggregate['volume']>0,'The CAD profile needs non-empty solids.')
    # model.brep is written by the export that asks for it, not by every job:
    # nothing reads a per-evaluate whole-model archive back.
    files=[definitions[fid]['cache_key']+'.mesh.json' for fid in mesh_results]
    if action=='render' and request.get('view'):
        import views
        view=request['view'];selected=request.get('feature_id');targets=[selected] if selected else plan['outputs']
        for fid in targets:
            if native(fid):ensure_shape(fid)
        require(all(fid in shapes for fid in targets),'Diagnostic views need native B-Rep geometry.','OUT_OF_SCOPE')
        names={fid:definitions[fid].get('semantic_name',fid) for fid in targets}
        pairs=[(fid,shapes[fid]) for fid in targets]
        if view['kind']=='section':svg,report=views.section_view(pairs,view['origin'],view['normal'],view['discretization'],names)
        else:svg,report=views.orthographic_view(pairs,view['direction'],view['discretization'],names,view.get('hidden_lines',True))
        with open('out/view.svg','w') as h:h.write(svg)
        write_json('out/view.json',report)
        files.extend(['view.svg','view.json'])
    elif action=='render':
        selected=request.get('feature_id');targets=[selected] if selected else plan['outputs'];meshes=[];adaptive=request.get('adaptive')
        for fid in targets:
            if native(fid):ensure_shape(fid)
            if fid in shapes:
                meshes.append(adaptive_mesh(shapes[fid],adaptive,fid,face_records[fid]['faces']) if adaptive else mesh(shapes[fid],request['deflection'],fid,face_records[fid]['faces']))
            elif fid in mesh_results:meshes.append(mesh_results[fid])
            elif fid in field_results:
                from fields import extract
                meshes.append(frames.world_mesh(extract(field_results[fid],samplers[fid]),field_results[fid].get('placement')))
        clip=request.get('clip_region')
        if clip:meshes=[clip_mesh(m,clip['center'],clip['radius']) for m in meshes]
        summary={'meshes':meshes,'unit':'mm','quality':'preview_only'}
        resolutions=[m.get('resolution') for m in meshes if m.get('resolution')]
        if resolutions:
            summary['resolution']={'absolute_resolution_mm':max(r['absolute_resolution_mm'] for r in resolutions),'finest_deflection_mm':min(r['finest_deflection_mm'] for r in resolutions),
                                   'reference_scale_mm':max(r['reference_scale_mm'] for r in resolutions),'minimum_feature_resolved_mm':max(r['minimum_feature_resolved_mm'] for r in resolutions),
                                   'method':resolutions[0]['method'],'policy':resolutions[0]['policy'],'certified_surface_bound':None}
        if clip:summary['clip']=clip
        write_json('out/preview.json',summary)
        files.append('preview.json')
    if action=='export' and request['format']=='vdb':
        from volume_io import export_vdb
        require(len(plan['outputs'])==1 and plan['outputs'][0] in field_results,'OpenVDB output needs exactly one analytic field.','OUT_OF_SCOPE')
        fid=plan['outputs'][0];report=export_vdb(field_results[fid],'out/model.vdb',samplers[fid]);files.append('model.vdb')
        write_json('out/roundtrip.json',report)
        files.append('roundtrip.json')
    elif action=='export' and mesh_results:
        require(all(fid in mesh_results for fid in plan['outputs']),
                'Mixed geometry must not be silently omitted in a mesh export.','OUT_OF_SCOPE')
        require(request['format']=='stl','An authoritative mesh needs STL, GLB or IR; B-Rep reconstruction is a separate conversion.','OUT_OF_SCOPE')
        source=mesh_quality.combine([mesh_results[fid] for fid in plan['outputs']])
        restored,quality,report=mesh_quality.export_stl(source,'out/model.stl',plan['tolerance'])
        if plan['profile']=='watertight_solid':require(quality['watertight_solid'],'The exported STL does not meet the mesh body profile.')
        report.update(before=aggregate,after=mesh_quality.mesh_facts(restored,{'cache_key':'roundtrip'},quality))
        write_json('out/roundtrip.json',report)
        files.extend(['model.stl','roundtrip.json'])
    elif action=='export':
        if root is None:root=output_root()
        require(root is not None,'This format needs B-Rep geometry.','OUT_OF_SCOPE')
        require(len(native_outputs)==len(plan['outputs']),'This native export format must not silently omit implicit outputs. Use GLB/IR or select the outputs explicitly.','OUT_OF_SCOPE')
        fmt=request['format'];name='model.'+fmt
        report=export_shape(root,fmt,'out/'+name,request['deflection']);files.append(name)
        write_json('out/roundtrip.json',report)
        files.append('roundtrip.json')
    if measuring:
        for fid in (request.get('feature_id'),request.get('other_feature_id')):
            if fid and native(fid):ensure_shape(fid)
    if action=='distance':
        evaluator=BRepExtrema_DistShapeShape(shapes[request['feature_id']],shapes[request['other_feature_id']]);evaluator.Perform()
        require(evaluator.IsDone(),'Distance evaluation failed.');aggregate={'distance':evaluator.Value()}
    if action=='contact_graph':
        import distances
        nodes=request['nodes']
        for node in nodes:
            for fid in node['features']:
                require(native(fid),'The contact graph needs native B-Rep features.','OUT_OF_SCOPE');ensure_shape(fid)
        budget=float(request.get('policy_budget_seconds') or 45)
        aggregate=distances.contact_graph(nodes,shapes,face_records,facts,request.get('pairs'),float(request.get('tolerance_mm',0.05)),
                                          started+0.7*budget)
    if action=='analysis' and request['metric']=='surface_deviation':
        from intervals import certify_deviation
        fid=request['feature_id'];require(fid in field_results,'Surface deviation needs an analytic field.','OUT_OF_SCOPE')
        c=field_results[fid]['construction']
        report=certify_deviation(request['reference_expression'],c['expression'],c['domain'],field_results[fid]['field']['cell_size'],request['epsilon'])
        aggregate={'measurements':report,'unit':'mm','method':report['method'],'coverage':report['coverage'],
                   'certified_error_bound':report['certified_hausdorff_bound_mm'],'guarantee':'bounded' if report['status']=='certified' else 'not_certified',
                   'domain_frame':field_results[fid].get('local_frame','world')}
    elif action=='analysis' and request['metric']=='fit_primitives':
        import mesh_ops
        fid=request['feature_id'];require(fid in mesh_results,'Primitive hypotheses need an authoritative mesh.','OUT_OF_SCOPE')
        report=mesh_ops.fit_primitives(mesh_results[fid],min_triangles=2)
        aggregate={'measurements':report,'unit':'mm','method':report['method'],'coverage':'segments_covering_%.3f_of_mesh_area'%report['coverage_area_fraction'],
                   'certified_error_bound':None,'guarantee':'sampled'}
    elif action=='analysis' and request['metric']=='curvature' and request['feature_id'] in field_results:
        from field_analysis import implicit_curvature
        fid=request['feature_id'];c=field_results[fid]['construction']
        report=implicit_curvature(c['expression'],samplers[fid].compiled,request['point_local'],request['step'])
        aggregate={'measurements':report,'unit':'per_mm_and_per_mm2','method':'central_finite_difference_hessian_with_interval_regularity',
                   'coverage':'single_point_and_its_two_step_cell','certified_error_bound':None,'domain_frame':field_results[fid].get('local_frame','world')}
    elif action=='analysis' and request['metric']=='surface_distance':
        import distances
        a,b=request['feature_id'],request['other_feature_id']
        require(a in shapes and b in shapes,'Surface distance needs two native B-Rep features.','OUT_OF_SCOPE')
        report=distances.surface_distance(shapes[a],shapes[b])
        aggregate={'measurements':report,'unit':'mm','method':'sampled_chamfer_hausdorff_exact_extrema_and_boolean_iou',
                   'coverage':'area_weighted_surface_samples_plus_exact_kernel_extrema','certified_error_bound':None,'guarantee':'sampled'}
    elif action=='analysis' and request['metric']=='wall_thickness':
        import distances
        fid=request['feature_id'];require(fid in shapes,'Wall thickness needs native B-Rep geometry.','OUT_OF_SCOPE')
        report=distances.wall_thickness(shapes[fid])
        aggregate={'measurements':report,'unit':'mm','method':report['method'],'coverage':report['coverage'],'certified_error_bound':None,'guarantee':'sampled'}
    elif action=='analysis' and request['metric']=='clearance' and request.get('motion'):
        import distances
        a,b=shapes[request['feature_id']],shapes[request['other_feature_id']]
        report=distances.motion_clearance(a,b,request['motion']['translation'],int(request['motion']['steps']),request.get('minimum_clearance',0))
        aggregate={'measurements':report,'unit':'mm','method':'OCCT_BRepExtrema_DistShapeShape_along_sampled_linear_motion','coverage':report['coverage'],
                   'certified_error_bound':None,'motion_or_global_wall_certificate':False}
    elif action=='analysis' and request['metric']=='blend_activity':
        from intervals import blend_activity
        fid=request['feature_id'];require(fid in field_results,'Fit regions are certified for analytic fields.','OUT_OF_SCOPE')
        c=field_results[fid]['construction']
        report=blend_activity(c['expression'],request['region'],request['cell_size'])
        aggregate={'measurements':report,'unit':'mm','method':report['method'],'coverage':report['coverage'],'certified_error_bound':None,
                   'guarantee':'bounded' if report['status']=='certified' else 'not_certified','domain_frame':field_results[fid].get('local_frame','world')}
    elif action=='analysis':
        from analysis import measure
        aggregate=measure(request,shapes)
    for fid,sampler in samplers.items():
        if sampler.used:
            key=field_results[fid]['cache_key'];name=key+'.field.json'
            write_json('out/'+name,sampler.serialize(),compact=True)
            files.append(name)
    return dict(status='candidate_ready',facts=facts,aggregate=aggregate,files=files,engine_build=BUILD,
                metrics={'seconds':time.monotonic()-started,'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'cache_hits':hits,'total_features':len(features),'field_samples':{fid:s.metrics() for fid,s in samplers.items()},
                         **({'parallel':parallel} if parallel is not None else {})})

# The gateway enforces the job's wall-clock budget (45 s at most) and kills the
# sandbox when it runs out. The processor limit only guards against runaway work
# and must not be the tighter of the two: it counts the CPU time of every thread,
# so it is set to about four times the wall budget (parallel checks use at most
# three threads). Running into it is budget exhaustion, never a kernel failure.
MAX_WALL_SECONDS=45
CPU_FACTOR=4

def cpu_budget(request=None):
    """Tighten the processor limit to this request's approved wall budget."""
    used=sum(resource.getrusage(resource.RUSAGE_SELF)[:2])
    wall=float((request or {}).get('policy_budget_seconds') or MAX_WALL_SECONDS)
    soft,hard=resource.getrlimit(resource.RLIMIT_CPU)
    limit=int(min(hard-2 if hard!=resource.RLIM_INFINITY else float('inf'),used+CPU_FACTOR*min(wall,MAX_WALL_SECONDS)+5))
    resource.setrlimit(resource.RLIMIT_CPU,(max(limit,int(used)+1),hard))

class CpuBudget(BaseException):
    """Not an Exception: operator code that tries alternatives (blend search) must
    not swallow it and carry on past the limit."""

def on_cpu_limit(signum,frame):
    raise CpuBudget()

if __name__=='__main__':
    import signal
    first=CPU_FACTOR*MAX_WALL_SECONDS+60
    resource.setrlimit(resource.RLIMIT_CPU,(first,first+5))
    signal.signal(hostlimits.CPU_SIGNAL,on_cpu_limit)
    resource.setrlimit(resource.RLIMIT_AS,(1536*1024**2,1536*1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE,(34*1024**2,34*1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
    imported_at=time.monotonic();waited=0.
    if '--wait' in sys.argv:
        # Pre-warmed single-use sandbox: native libraries are already imported. The
        # request is written last by the gateway; an idle sandbox exits without output.
        deadline=time.monotonic()+600
        while not os.path.exists('request.json'):
            if time.monotonic()>deadline:sys.exit(0)
            time.sleep(0.02)
        waited=time.monotonic()-imported_at
    try:
        with open('request.json') as h:request=json.load(h)
        cpu_budget(request)
        # A preview rendered ahead is a convenience: it yields the processor to
        # the job someone is waiting for.
        if request.get('ahead'):
            hostlimits.nice(10)
        result=run(request)
        if isinstance(result.get('metrics'),dict):result['metrics'].update(warm_start='--wait' in sys.argv,idle_wait_seconds=waited)
    except GeometryError as e:result={'status':'failed','error':{'code':e.code,'message':e.message}}
    except MemoryError:result={'status':'failed','error':{'code':'BUDGET_EXCEEDED','message':'Worker memory budget exhausted.'}}
    except CpuBudget:result={'status':'failed','error':{'code':'BUDGET_EXCEEDED','message':'Worker compute time budget exhausted; finished features stay in the cache, the run continues.'}}
    except Exception:
        traceback.print_exc(file=sys.stderr)
        result={'status':'failed','error':{'code':'KERNEL_FAILURE','message':'Native geometry operation failed.'}}
    write_json('result.json',result)
