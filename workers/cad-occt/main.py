"""One bounded job per process. JSON is carried via files to isolate kernel stdout."""
import json, os, sys, time, resource, traceback, shutil, math
import numpy as np
from geometry import *
import topology
import frames
import mesh_quality

def conversion_report(shape,source,deflection,tolerance):
    """B-Rep to indexed mesh: sampled centroid distances to the native surface (Bauplan 5.5)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    import numpy as np
    vertices=np.array(source['vertices']);triangles=source['triangles']
    step=max(1,len(triangles)//256);measured=0.
    for t in triangles[::step]:
        centroid=vertices[t].mean(axis=0)
        evaluator=BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float,centroid))).Shape(),shape);evaluator.Perform()
        require(evaluator.IsDone(),'Konvertierungsmessung fehlgeschlagen.')
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

def run(request):
    if request.get('action')=='mesh_check':
        restored=mesh_quality.combine(request['meshes'])
        report={'mesh_quality':mesh_quality.inspect_mesh(restored)}
        if 'original_meshes' in request:
            report['measured_vertex_error_bound_mm']=mesh_quality.correspondence_bound(mesh_quality.combine(request['original_meshes']),restored)
            require(report['measured_vertex_error_bound_mm']<=request['tolerance'],
                    'GLB-Quantisierung überschreitet die verlangte Geometriegenauigkeit.','PRECISION_UNSUPPORTED')
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
    definitions={f['id']:f for f in features}
    # What is already in the cache is read, never written back: a job that
    # re-emits the whole model spends its budget copying bytes that the store
    # already has, which is what made a one-parameter change cost half a minute.
    def cached_shape(key):
        cache='cache/'+key+'.brep';history_cache='cache/'+key+'.topology.json'
        if not (os.path.isfile(cache) and os.path.isfile(history_cache)):return None
        shape=read_brep(cache)
        with open(history_cache) as h:records=json.load(h)
        with open(cache,'rb') as h:source_hash=hashlib.sha256(h.read()).hexdigest()
        trace=topology.restore(shape,records,key,source_hash)
        return shape,trace,records
    def history_file(key,records,fresh):
        if fresh:
            with open('out/'+key+'.topology.json','w') as h:json.dump(records,h,allow_nan=False)
    # The measured facts of a feature belong to its cache key just as much as its
    # shape does, so an unchanged feature is never measured twice.
    def cached_facts(key):
        path='cache/'+key+'.facts.json'
        if not os.path.isfile(path):return None
        try:
            with open(path) as h:return json.load(h)
        except Exception:return None
    def facts_file(key,props):
        with open('out/'+key+'.facts.json','w') as h:json.dump(props,h,allow_nan=False)
    # Which shapes this run has to hold. The gateway staged the cache and knows
    # exactly which features it left out, so it says so; without that list every
    # feature is rebuilt, which is what any older caller expects.
    needed=set(request['shapes_needed']) if request.get('shapes_needed') is not None else {f['id'] for f in features}
    for f in features:
        fid=f['id'];op=f['construction']['operator']
        if op=='imported' and f['construction']['format']=='stl':
            source=mesh_quality.read_stl('input-'+f['construction']['artifact_id']+'.stl',f['construction']['source_unit'])
            require(source['source_conversion']['coordinate_error_bound_mm']<=plan['tolerance'],
                    'STL-Einheitenkonvertierung überschreitet die Modellgenauigkeit.','PRECISION_UNSUPPORTED')
            local_bounds=[*[min(p[i] for p in source['vertices']) for i in range(3)],*[max(p[i] for p in source['vertices']) for i in range(3)]]
            source=frames.world_mesh(source,f.get('placement'))
            source.update(feature_id=fid,quality='preview_only',deflection=0.,certified_bound=None)
            mesh_results[fid]=source;facts[fid]=mesh_quality.mesh_facts(source,f)
            facts[fid]['local_bounds']=local_bounds
            require(facts[fid]['valid'],'Mesh enthält degenerierte oder doppelte Dreiecke.')
            with open('out/'+f['cache_key']+'.mesh.json','w') as h:json.dump(source,h,allow_nan=False,separators=(',',':'))
            continue
        if op in ('mesh_repair','remesh_region','local_mesh_deform','tessellate','extract_isosurface'):
            import mesh_ops
            dep=f['depends_on'][0];p=f['values'];c=f['construction'];report=None
            if op=='tessellate':
                require(dep in shapes,'Tessellierung benötigt einen B-Rep-Eingang.','OUT_OF_SCOPE')
                source=mesh_quality.index_exact(mesh(shapes[dep],p['deflection'],fid,face_records[dep]['faces']))
                report=conversion_report(shapes[dep],source,p['deflection'],plan['tolerance'])
            elif op=='extract_isosurface':
                from fields import extract
                require(dep in field_results,'Isoflächenextraktion benötigt ein analytisches Feld.','OUT_OF_SCOPE')
                source=frames.world_mesh(extract(field_results[dep],samplers[dep]),field_results[dep].get('placement'))
                report=field_conversion_report(field_results[dep],samplers[dep],source,plan['tolerance'])
            else:
                require(dep in mesh_results,'Netzoperator benötigt ein maßgebliches Netz.','OUT_OF_SCOPE')
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
            require(facts[fid]['valid'],'Netzoperation erzeugte degenerierte oder doppelte Dreiecke.')
            with open('out/'+f['cache_key']+'.mesh.json','w') as h:json.dump(source,h,allow_nan=False,separators=(',',':'))
            continue
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
            continue
        key=f['cache_key'];local_key=f.get('local_cache_key',key)
        if fid not in needed:
            # Nothing in this run builds on this feature, and what it measures is
            # already known: it is reported from its facts and never reopened.
            stored=cached_facts(key)
            if stored is not None:
                facts[fid]=stored;hits+=1
                continue
        # The inputs are only placed when something is actually built from them:
        # a feature that comes back from the cache must not force the run to hold
        # everything underneath it.
        def inputs(f=f):
            converted=[frames.place(local_shapes[d],local_histories[d],definitions[d].get('placement'),f.get('placement')) for d in f['depends_on']]
            return [item[0] for item in converted],[item[1] for item in converted]
        deps=None
        cached=cached_shape(local_key)
        if cached:
            local_shape,local_trace,local_records=cached;hits+=1
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
        require(not shape.IsNull(),'Leeres Operatorergebnis.')
        stored=cached_facts(key)
        if stored is not None:
            # The shape was needed for something else, its measurements were not.
            shapes[fid]=shape;facts[fid]=stored
            continue
        props=properties(shape);require(props['valid'],'OCCT meldet eine ungültige Geometrie.')
        if plan['profile'] in ('precision_cad','manufacturing_candidate'):
            require(max(props['native_tolerances_mm'].values())<=plan['tolerance'],
                    'Native Randtoleranzen überschreiten die verlangte Modellgenauigkeit.','PRECISION_UNSUPPORTED')
        if f['depends_on']:
            base=definitions[f['depends_on'][0]]
            f=dict(f,base_operator=base['construction']['operator'] if base.get('local_frame','world')==f.get('local_frame','world') else 'transformed_source')
        if deps is None:deps,_=inputs()
        props['dimensions']=dimensions(f,local_shape,deps);props['geometry_hash']=records['brep_sha256']
        props['local_frame']=f.get('local_frame','world');props['local_bounds']=bounds(local_shape)
        props['coordinate_frame']='world';props['dimension_frame']=f.get('local_frame','world')
        props['engine_build']=BUILD;props['cache_key']=key
        import advanced
        if fid in advanced.CONSTRUCTION_REPORTS:props['construction_report']=advanced.CONSTRUCTION_REPORTS[fid]
        props['topology']={'version':topology.VERSION,'face_count':len(records['faces']),'tracked_faces':sum(bool(x['origins']) for x in records['faces'])}
        facts_file(key,props)
        shapes[fid]=shape;facts[fid]=props
    output_shapes=[shapes[fid] for fid in plan['outputs'] if fid in shapes]
    root=compound(output_shapes) if len(output_shapes)>1 else output_shapes[0] if output_shapes else None
    aggregate=properties(root) if root is not None else None
    if any(fid in field_results or fid in mesh_results for fid in plan['outputs']):
        declared=[facts[fid]['bounds'] for fid in plan['outputs']]
        aggregate={'valid':all(facts[fid]['valid'] for fid in plan['outputs']),
                   'bounds':[*(min(b[i] for b in declared) for i in range(3)),*(max(b[i+3] for b in declared) for i in range(3))],
                   'volume':None,'area':None,'solids':None,'precision_solid_only':False,
                   'coverage':'declared_world_bounds_without_implicit_volume_certificate'}
    if plan['outputs'] and all(fid in mesh_results for fid in plan['outputs']):
        combined=mesh_quality.combine([mesh_results[fid] for fid in plan['outputs']])
        aggregate=mesh_quality.mesh_facts(combined,{'cache_key':'aggregate'})
    if plan['profile']=='manufacturing_candidate' and plan['outputs']:
        import distances
        rules=plan.get('manufacturing') or {}
        require(root is not None and len(output_shapes)==len(plan['outputs']),'Das Fertigungskandidatenprofil benötigt native Körper.','OUT_OF_SCOPE')
        build=np.array(rules.get('build_direction',[0,0,1]),float);build=build/np.linalg.norm(build)
        for fid in plan['outputs']:
            report={'process':rules.get('process'),'wall_thickness':distances.wall_thickness(shapes[fid]),'guarantee':'sampled','certified':False}
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
    if plan['profile'] in ('precision_cad','manufacturing_candidate') and plan['outputs']:
        require(root is not None and len(output_shapes)==len(plan['outputs']),'CAD-Profil benötigt native Körper.')
        require(all(facts[fid]['precision_solid_only'] and facts[fid]['volume']>0 for fid in plan['outputs']),'Jede Ausgabe des CAD-Profils muss ausschließlich aus Volumenkörpern bestehen.')
        require(aggregate['solids']>0 and aggregate['volume']>0,'CAD-Profil benötigt nichtleere Volumenkörper.')
    files=[definitions[fid]['cache_key']+'.mesh.json' for fid in mesh_results];action=request.get('action','evaluate')
    if root is not None:
        write_brep(root,'out/model.brep');files.append('model.brep')
    if action=='render' and request.get('view'):
        import views
        view=request['view'];selected=request.get('feature_id');targets=[selected] if selected else plan['outputs']
        require(all(fid in shapes for fid in targets),'Diagnoseansichten benötigen native B-Rep-Geometrie.','OUT_OF_SCOPE')
        names={fid:definitions[fid].get('semantic_name',fid) for fid in targets}
        pairs=[(fid,shapes[fid]) for fid in targets]
        if view['kind']=='section':svg,report=views.section_view(pairs,view['origin'],view['normal'],view['discretization'],names)
        else:svg,report=views.orthographic_view(pairs,view['direction'],view['discretization'],names,view.get('hidden_lines',True))
        with open('out/view.svg','w') as h:h.write(svg)
        with open('out/view.json','w') as h:json.dump(report,h,allow_nan=False)
        files.extend(['view.svg','view.json'])
    elif action=='render':
        selected=request.get('feature_id');targets=[selected] if selected else plan['outputs'];meshes=[];adaptive=request.get('adaptive')
        for fid in targets:
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
        with open('out/preview.json','w') as h:json.dump(summary,h,allow_nan=False)
        files.append('preview.json')
    if action=='export' and request['format']=='vdb':
        from volume_io import export_vdb
        require(len(plan['outputs'])==1 and plan['outputs'][0] in field_results,'OpenVDB-Ausgabe benötigt genau ein analytisches Feld.','OUT_OF_SCOPE')
        fid=plan['outputs'][0];report=export_vdb(field_results[fid],'out/model.vdb',samplers[fid]);files.append('model.vdb')
        with open('out/roundtrip.json','w') as h:json.dump(report,h,allow_nan=False)
        files.append('roundtrip.json')
    elif action=='export' and mesh_results:
        require(all(fid in mesh_results for fid in plan['outputs']),
                'Gemischte Geometrie darf beim Meshexport nicht still weggelassen werden.','OUT_OF_SCOPE')
        require(request['format']=='stl','Ein maßgebliches Mesh benötigt STL, GLB oder IR; B-Rep-Rekonstruktion ist eine eigene Konvertierung.','OUT_OF_SCOPE')
        source=mesh_quality.combine([mesh_results[fid] for fid in plan['outputs']])
        restored,quality,report=mesh_quality.export_stl(source,'out/model.stl',plan['tolerance'])
        if plan['profile']=='watertight_solid':require(quality['watertight_solid'],'Exportiertes STL erfüllt das Meshkörperprofil nicht.')
        report.update(before=aggregate,after=mesh_quality.mesh_facts(restored,{'cache_key':'roundtrip'},quality))
        with open('out/roundtrip.json','w') as h:json.dump(report,h,allow_nan=False)
        files.extend(['model.stl','roundtrip.json'])
    elif action=='export':
        require(root is not None,'Dieses Format benötigt B-Rep-Geometrie.','OUT_OF_SCOPE')
        require(len(output_shapes)==len(plan['outputs']),'Dieses native Exportformat darf implizite Ausgaben nicht still weglassen. GLB/IR verwenden oder Ausgaben ausdrücklich auswählen.','OUT_OF_SCOPE')
        fmt=request['format'];name='model.'+fmt
        report=export_shape(root,fmt,'out/'+name,request['deflection']);files.append(name)
        with open('out/roundtrip.json','w') as h:json.dump(report,h,allow_nan=False)
        files.append('roundtrip.json')
    if action=='distance':
        evaluator=BRepExtrema_DistShapeShape(shapes[request['feature_id']],shapes[request['other_feature_id']]);evaluator.Perform()
        require(evaluator.IsDone(),'Abstandsauswertung fehlgeschlagen.');aggregate={'distance':evaluator.Value()}
    if action=='analysis' and request['metric']=='surface_deviation':
        from intervals import certify_deviation
        fid=request['feature_id'];require(fid in field_results,'Oberflächenabweichung benötigt ein analytisches Feld.','OUT_OF_SCOPE')
        c=field_results[fid]['construction']
        report=certify_deviation(request['reference_expression'],c['expression'],c['domain'],field_results[fid]['field']['cell_size'],request['epsilon'])
        aggregate={'measurements':report,'unit':'mm','method':report['method'],'coverage':report['coverage'],
                   'certified_error_bound':report['certified_hausdorff_bound_mm'],'guarantee':'bounded' if report['status']=='certified' else 'not_certified',
                   'domain_frame':field_results[fid].get('local_frame','world')}
    elif action=='analysis' and request['metric']=='fit_primitives':
        import mesh_ops
        fid=request['feature_id'];require(fid in mesh_results,'Primitivhypothesen benötigen ein maßgebliches Netz.','OUT_OF_SCOPE')
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
        require(a in shapes and b in shapes,'Oberflächenabstand benötigt zwei native B-Rep-Features.','OUT_OF_SCOPE')
        report=distances.surface_distance(shapes[a],shapes[b])
        aggregate={'measurements':report,'unit':'mm','method':'sampled_chamfer_hausdorff_exact_extrema_and_boolean_iou',
                   'coverage':'area_weighted_surface_samples_plus_exact_kernel_extrema','certified_error_bound':None,'guarantee':'sampled'}
    elif action=='analysis' and request['metric']=='wall_thickness':
        import distances
        fid=request['feature_id'];require(fid in shapes,'Wandstärke benötigt native B-Rep-Geometrie.','OUT_OF_SCOPE')
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
        fid=request['feature_id'];require(fid in field_results,'Passregionen werden für analytische Felder zertifiziert.','OUT_OF_SCOPE')
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
            with open('out/'+name,'w') as h:json.dump(sampler.serialize(),h,allow_nan=False,separators=(',',':'))
            files.append(name)
    return dict(status='candidate_ready',facts=facts,aggregate=aggregate,files=files,engine_build=BUILD,
                metrics={'seconds':time.monotonic()-started,'peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'cache_hits':hits,'total_features':len(features),'field_samples':{fid:s.metrics() for fid,s in samplers.items()}})

if __name__=='__main__':
    resource.setrlimit(resource.RLIMIT_CPU,(40,42))
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
        result=run(request)
        if isinstance(result.get('metrics'),dict):result['metrics'].update(warm_start='--wait' in sys.argv,idle_wait_seconds=waited)
    except GeometryError as e:result={'status':'failed','error':{'code':e.code,'message':e.message}}
    except MemoryError:result={'status':'failed','error':{'code':'BUDGET_EXCEEDED','message':'Worker-Speicherbudget erschöpft.'}}
    except Exception:
        traceback.print_exc(file=sys.stderr)
        result={'status':'failed','error':{'code':'KERNEL_FAILURE','message':'Native Geometrieoperation fehlgeschlagen.'}}
    with open('result.json','w') as h:json.dump(result,h,allow_nan=False)
