"""Preview worker: tessellates shapes the evaluated build has already cached.

It draws, and only draws. No geometry facts, caches or model files come out of
it, which is why it stands beside main.py instead of inside the checked build:
the shapes it reads were produced and verified by that build, and a preview
stays preview_only. Targets are spread over the cores; each one becomes its
own out/<cache_key>.mesh.json, which the gateway assembles and keeps.
One bounded job per process; request and result travel as files, like main.py."""
import json, os, sys, time, resource, traceback, hashlib, multiprocessing
from geometry import *
import topology

BUDGET_MESSAGE='Dreiecksbudget überschritten; gröbere Vorschauabweichung, kleinerer Ausschnitt (region) oder einzelnes Feature wählen.'
SETTINGS={}

def load(key):
    brep='cache/'+key+'.brep';history='cache/'+key+'.topology.json'
    require(os.path.isfile(brep) and os.path.isfile(history),'Vorschau benötigt zwischengespeicherte Geometrie.','OUT_OF_SCOPE')
    with open(brep,'rb') as h:source=h.read()
    with open(history) as h:records=json.load(h)
    # Same bytes, same shape, same face order: the archive's per-face fingerprints need no second check here.
    require(records.get('version')==topology.VERSION and records.get('cache_key')==key and records.get('brep_sha256')==hashlib.sha256(source).hexdigest(),
            'Flächenhistorie passt nicht zum Cache.','INTEGRITY_FAILURE')
    shape=read_brep(brep)
    require(not shape.IsNull() and len(topology.face_list(shape))==len(records['faces']),'Flächenhistorie passt nicht zum Cache.','INTEGRITY_FAILURE')
    return shape,records

def tessellate(shape,deflection,feature_id,faces):
    """geometry.mesh, with the per-node placement skipped for faces at the identity."""
    require(deflection>=1e-5,'Angeforderte Tessellierung zu fein.','PRECISION_UNSUPPORTED')
    BRepMesh_IncrementalMesh(shape,deflection,False,0.15,False).Perform()
    vertices,triangles,face_ranges=[],[],[]
    for face_index,face in enumerate(explore(shape,TopAbs_FACE)):
        location=TopLoc_Location();tri=BRep_Tool.Triangulation_s(TopoDS.Face_s(face),location)
        if tri is None:continue
        base=len(vertices);node=tri.Node
        if location.IsIdentity():
            for i in range(1,tri.NbNodes()+1):
                p=node(i);vertices.append([p.X(),p.Y(),p.Z()])
        else:
            trsf=location.Transformation()
            for i in range(1,tri.NbNodes()+1):
                p=node(i).Transformed(trsf);vertices.append([p.X(),p.Y(),p.Z()])
        first_triangle=len(triangles);flipped=face.Orientation()==TopAbs_REVERSED;triangle=tri.Triangle
        for i in range(1,tri.NbTriangles()+1):
            a,b,c=triangle(i).Get()
            triangles.append([base+a-1,base+c-1,base+b-1] if flipped else [base+a-1,base+b-1,base+c-1])
        if len(triangles)>500000:raise GeometryError('BUDGET_EXCEEDED',BUDGET_MESSAGE)
        face_ranges.append({'face_id':faces[face_index]['face_id'],'first_triangle':first_triangle,'triangle_count':len(triangles)-first_triangle})
    require(triangles,'Drahtgeometrie benötigt einen eigenen Kurvenviewer.','OUT_OF_SCOPE')
    return dict(vertices=vertices,triangles=triangles,face_ranges=face_ranges,feature_id=feature_id,deflection=deflection,quality='preview_only',certified_bound=None)

def configure(settings):
    SETTINGS.update(settings)

def work(item):
    """One target in a forked worker: exceptions travel back as plain values."""
    fid,key=item
    try:
        shape,records=load(key)
        m=adaptive_mesh(shape,SETTINGS['adaptive'],fid,records['faces']) if SETTINGS['adaptive'] else tessellate(shape,SETTINGS['deflection'],fid,records['faces'])
        clip=SETTINGS['clip']
        if clip:m=clip_mesh(m,clip['center'],clip['radius'])
        m['geometry_hash']=records['brep_sha256'];m['engine_build']=BUILD
        with open('out/'+key+'.mesh.json','w') as h:json.dump(m,h,allow_nan=False,separators=(',',':'))
        return dict(feature_id=fid,cache_key=key,geometry_hash=records['brep_sha256'],triangles=len(m['triangles']))
    except GeometryError as e:return dict(feature_id=fid,error=[e.code,e.message])
    except MemoryError:return dict(feature_id=fid,error=['BUDGET_EXCEEDED','Worker-Speicherbudget erschöpft.'])
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return dict(feature_id=fid,error=['KERNEL_FAILURE','Native Geometrieoperation fehlgeschlagen.'])

def run(request):
    started=time.monotonic();plan=request['plan'];definitions={f['id']:f for f in plan['features']}
    selected=request.get('feature_id');targets=[selected] if selected else plan['outputs'];items=[]
    for fid in targets:
        f=definitions.get(fid);require(f is not None,'Vorschauziel fehlt im Plan.','OUT_OF_SCOPE')
        items.append((fid,f['cache_key']))
    settings=dict(deflection=request['deflection'],adaptive=request.get('adaptive'),clip=request.get('clip_region'))
    workers=max(1,min(4,os.cpu_count() or 1,len(items)))
    if workers>1:
        with multiprocessing.get_context('fork').Pool(workers,initializer=configure,initargs=(settings,)) as pool:
            results=list(pool.imap_unordered(work,items,chunksize=1))
    else:
        configure(settings);results=[work(item) for item in items]
    facts={};files=[];triangles=0
    for r in results:
        if 'error' in r:raise GeometryError(*r['error'])
        facts[r['feature_id']]={'geometry_hash':r['geometry_hash'],'cache_key':r['cache_key']}
        files.append(r['cache_key']+'.mesh.json');triangles+=r['triangles']
    peak=max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    return dict(status='succeeded',facts=facts,aggregate=None,files=sorted(set(files)),engine_build=BUILD,
                metrics={'seconds':time.monotonic()-started,'peak_rss_kib':peak,'cache_hits':len(targets),'total_features':len(targets),'preview_workers':workers,'preview_triangles':triangles})

if __name__=='__main__':
    resource.setrlimit(resource.RLIMIT_CPU,(40,42))
    resource.setrlimit(resource.RLIMIT_AS,(1536*1024**2,1536*1024**2))
    resource.setrlimit(resource.RLIMIT_FSIZE,(34*1024**2,34*1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
    imported_at=time.monotonic();waited=0.
    if '--wait' in sys.argv:
        # Pre-warmed single-use sandbox, as for main.py: the request is written last.
        deadline=time.monotonic()+600
        while not os.path.exists('request.json'):
            if time.monotonic()>deadline:sys.exit(0)
            time.sleep(0.02)
        waited=time.monotonic()-imported_at
    try:
        with open('request.json') as h:request=json.load(h)
        result=run(request)
        result['metrics'].update(warm_start='--wait' in sys.argv,idle_wait_seconds=waited)
    except GeometryError as e:result={'status':'failed','error':{'code':e.code,'message':e.message}}
    except MemoryError:result={'status':'failed','error':{'code':'BUDGET_EXCEEDED','message':'Worker-Speicherbudget erschöpft.'}}
    except Exception:
        traceback.print_exc(file=sys.stderr)
        result={'status':'failed','error':{'code':'KERNEL_FAILURE','message':'Native Geometrieoperation fehlgeschlagen.'}}
    with open('result.json','w') as h:json.dump(result,h,allow_nan=False)
