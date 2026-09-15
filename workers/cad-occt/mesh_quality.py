"""Indexed mesh authority and exact-predicate checks; no proximity repair."""
import hashlib, io, json, math, re, struct, subprocess
from collections import defaultdict, Counter
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from geometry import require, GeometryError

MAX_TRIANGLES = 100000
MAX_VERTICES = 300000
HERE = Path(__file__).resolve().parent

def digest(mesh):
    return hashlib.sha256(json.dumps({k:mesh[k] for k in ('vertices','triangles')},sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def validate_data(mesh):
    vertices, triangles = mesh['vertices'], mesh['triangles']
    require(0 < len(vertices) <= MAX_VERTICES and 0 < len(triangles) <= MAX_TRIANGLES,
            'Mesh überschreitet das Vertex-/Dreiecksbudget.','BUDGET_EXCEEDED')
    require(all(len(p)==3 and all(type(x) in (int,float) and math.isfinite(x) and abs(x)<=1e12 for x in p) for p in vertices),
            'Mesh benötigt endliche begrenzte 3D-Koordinaten.','INVALID_SCHEMA')
    require(all(len(t)==3 and all(type(i) is int and 0<=i<len(vertices) for i in t) for t in triangles),
            'Ungültige Mesh-Indizes.','INVALID_SCHEMA')

def read_stl(path, unit):
    data=Path(path).read_bytes()
    require(len(data)<=33554432,'STL-Dateibudget überschritten.','BUDGET_EXCEEDED')
    scale={'mm':Fraction(1),'m':Fraction(1000),'um':Fraction(1,1000)}.get(unit)
    require(scale is not None,'Unbekannte STL-Einheit.','UNIT_MISMATCH')
    vertices=[];triangles=[];indexed={};rounding=Fraction(0);corners=0
    def convert(value):
        nonlocal rounding
        if isinstance(value,bytes):
            require(len(value)<=96 and re.fullmatch(rb'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',value),
                    'Ungültige STL-Zahl.','INVALID_SCHEMA')
            try: decimal=Decimal(value.decode('ascii'))
            except InvalidOperation: raise GeometryError('INVALID_SCHEMA','Ungültige STL-Zahl.')
            require(not decimal or -300<=decimal.adjusted()<=12,'STL-Zahlenbereich nicht unterstützt.','PRECISION_UNSUPPORTED')
            exact=Fraction(decimal)*scale
        else:
            require(math.isfinite(value),'Nichtendliche STL-Koordinate.','INVALID_SCHEMA')
            exact=Fraction(value)*scale
        result=float(exact)
        require(math.isfinite(result) and abs(result)<=1e12,'STL-Koordinate außerhalb des Zahlenbereichs.','PRECISION_UNSUPPORTED')
        rounding=max(rounding,abs(exact-Fraction(result)))
        return result
    def facet(points):
        nonlocal corners
        face=[]
        for p in points:
            point=tuple(convert(x) for x in p);corners+=1
            # STL has no shared indices. Index only mathematically identical
            # decoded coordinates; nearby points remain distinct.
            if point not in indexed:indexed[point]=len(vertices);vertices.append(list(point))
            face.append(indexed[point])
        triangles.append(face)
        require(len(triangles)<=MAX_TRIANGLES,'STL-Dreiecksbudget überschritten.','BUDGET_EXCEEDED')
    count=struct.unpack_from('<I',data,80)[0] if len(data)>=84 else -1
    if count>=0 and 84+50*count==len(data):
        require(0<count<=MAX_TRIANGLES,'STL-Dreiecksbudget überschritten.','BUDGET_EXCEEDED')
        for i in range(count):
            values=struct.unpack_from('<12fH',data,84+50*i)
            require(all(math.isfinite(x) for x in values[:12]),'Nichtendliche STL-Daten.','INVALID_SCHEMA')
            facet([values[j:j+3] for j in (3,6,9)])
        encoding='binary_float32'
    else:
        stream=io.BytesIO(data)
        def line():
            while True:
                value=stream.readline(4098)
                require(len(value)<=4096,'STL-Zeile zu lang.','BUDGET_EXCEEDED')
                if not value:return []
                if value.strip():return value.strip().split()
        first=line();require(first and first[0]==b'solid','ASCII-STL-Kopf fehlt.','INVALID_SCHEMA')
        while True:
            record=line()
            require(record,'ASCII-STL-Ende fehlt.','INVALID_SCHEMA')
            if record[0]==b'endsolid':break
            require(len(record)==5 and record[:2]==[b'facet',b'normal'],'Ungültige STL-Facette.','INVALID_SCHEMA')
            # Normals do not determine winding, but their input must be finite.
            try:normal_valid=all(len(v)<=96 and math.isfinite(float(v)) for v in record[2:])
            except (ValueError,OverflowError):normal_valid=False
            require(normal_valid,'Ungültige STL-Normale.','INVALID_SCHEMA')
            require(line()==[b'outer',b'loop'],'STL-Schleife fehlt.','INVALID_SCHEMA')
            points=[]
            for _ in range(3):
                point=line();require(len(point)==4 and point[0]==b'vertex','STL benötigt Dreiecke.','INVALID_SCHEMA');points.append(point[1:])
            require(line()==[b'endloop'] and line()==[b'endfacet'],'STL-Facettenende fehlt.','INVALID_SCHEMA')
            facet(points)
        require(not line(),'Zusätzliche Daten nach STL-Ende.','INVALID_SCHEMA')
        encoding='ascii_decimal'
    result={'vertices':vertices,'triangles':triangles}
    validate_data(result)
    error=0.0 if rounding==0 else math.nextafter(math.nextafter(float(rounding),math.inf)*math.nextafter(math.sqrt(3),math.inf),math.inf)
    result['source_conversion']={'source_unit':unit,'target_unit':'mm','encoding':encoding,
        'coordinate_error_bound_mm':error,'coincident_corners_indexed':corners-len(vertices),
        'proximity_welding':False,'normal_source':'triangle_winding','source_sha256':hashlib.sha256(data).hexdigest()}
    return result

def topology(mesh):
    validate_data(mesh)
    vertices,triangles=mesh['vertices'],mesh['triangles']
    edges={};links=[defaultdict(list) for _ in vertices];faces_at=[[] for _ in vertices]
    parents=list(range(len(triangles)))
    def root(i):
        while parents[i]!=i:parents[i]=parents[parents[i]];i=parents[i]
        return i
    duplicates=0;seen=set()
    for i,t in enumerate(triangles):
        key=tuple(sorted(t));duplicates+=key in seen;seen.add(key)
        for j in range(3):
            a,b,c=t[j],t[(j+1)%3],t[(j+2)%3]
            faces_at[a].append(i);links[a][b].append(c);links[a][c].append(b)
            key=(min(a,b),max(a,b))
            if key not in edges:edges[key]=[]
            edge=edges[key]
            if edge:parents[root(i)]=root(edge[0][0])
            edge.append((i,1 if a<b else -1))
    boundary=[edge for edge,uses in edges.items() if len(uses)==1]
    nonmanifold_edges=[edge for edge,uses in edges.items() if len(uses)>2]
    inconsistent=[edge for edge,uses in edges.items() if len(uses)==2 and sum(s for _,s in uses)!=0]
    boundary_degree=Counter(v for edge in boundary for v in edge)
    nonmanifold_vertices=[];isolated=[]
    for v,adj in enumerate(links):
        if not adj:isolated.append(v);continue
        start=next(iter(adj));visited=set();pending=[start]
        while pending:
            k=pending.pop()
            if k in visited:continue
            visited.add(k);pending.extend(x for x in adj[k] if x not in visited)
        degrees=[len(x) for x in adj.values()]
        cycle=all(d==2 for d in degrees) and boundary_degree[v]==0
        path=degrees.count(1)==2 and all(d in (1,2) for d in degrees) and boundary_degree[v]==2
        if len(visited)!=len(adj) or not (cycle or path):nonmanifold_vertices.append(v)
    groups={}
    for i,t in enumerate(triangles):
        k=root(i)
        if k not in groups:groups[k]={'first_triangle':i,'vertices':set(),'edges':set(),'faces':0}
        item=groups[k];item['faces']+=1;item['vertices'].update(t)
        item['edges'].update(tuple(sorted((t[j],t[(j+1)%3]))) for j in range(3))
    closed=not boundary and not nonmanifold_edges and not nonmanifold_vertices and not isolated and not duplicates
    components=[]
    for item in sorted(groups.values(),key=lambda c:c['first_triangle']):
        chi=len(item['vertices'])-len(item['edges'])+item['faces']
        genus=(2-chi)//2 if closed and not inconsistent and chi<=2 and chi%2==0 else None
        components.append({'first_triangle':item['first_triangle'],'vertices':len(item['vertices']),'edges':len(item['edges']),
                           'triangles':item['faces'],'euler_characteristic':chi,'genus_if_closed_orientable':genus})
    return {'vertices':len(vertices),'edges':len(edges),'triangles':len(triangles),'surface_components':len(components),
        'boundary_edges':len(boundary),'nonmanifold_edges':len(nonmanifold_edges),'inconsistently_oriented_edges':len(inconsistent),
        'nonmanifold_vertices':len(nonmanifold_vertices),'isolated_vertices':len(isolated),'duplicate_triangles':duplicates,
        'closed_vertex_manifold':closed,'consistent_orientation':not inconsistent,
        'euler_characteristic':len(vertices)-len(edges)+len(triangles),'component_examples':components[:16],
        'examples':{'boundary_edges':boundary[:16],'nonmanifold_edges':nonmanifold_edges[:16],
                    'nonmanifold_vertices':nonmanifold_vertices[:16],'inconsistently_oriented_edges':inconsistent[:16]}}

def inspect_mesh(mesh):
    top=topology(mesh)
    native_input=f"{len(mesh['vertices'])} {len(mesh['triangles'])}\n"+'\n'.join(' '.join(map(str,x)) for x in [*mesh['vertices'],*mesh['triangles']])+'\n'
    process=subprocess.run([str(HERE/'meshcheck')],input=native_input.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if process.returncode:
        code=process.stderr.decode('ascii','ignore').strip()
        raise GeometryError(code if code in ('BUDGET_EXCEEDED','INVALID_SCHEMA') else 'GEOMETRY_INVALID','Native Meshprüfung konnte nicht vollständig ausgeführt werden.')
    require(len(process.stdout)<=32768,'Meshprüfbericht zu groß.','BUDGET_EXCEEDED')
    native=json.loads(process.stdout)
    expected=json.loads((HERE/'.meshcheck-build.json').read_text())
    require(native['schema_version']=='1' and native['source_hash']==expected['source_hash'],'Meshprüfer passt nicht zum Build.','BUILD_MISMATCH')
    checks={
      'nondegenerate':native['degenerate_triangles']==0,
      'unique_triangles':top['duplicate_triangles']==0,
      'closed_vertex_manifold':top['closed_vertex_manifold'],
      'consistent_orientation':top['consistent_orientation'],
      'no_self_intersections':not native['self_intersections_found'],
      'nested_shell_orientation':native['volume_checked'] and native['nested_orientation_valid'],
    }
    return {'schema_version':'1','geometry_hash':digest(mesh),'engine':native['engine'],
            'domain':'entire_indexed_mesh_at_supplied_binary64_coordinates','guarantee':'exact_predicates_for_declared_mesh',
            'topology':top,'native':native,'checks':checks,'watertight_solid':all(checks.values()),
            'manufacturing_status':'not_certified','source_surface_error_bound_mm':None}

def mesh_facts(mesh, feature, quality=None):
    report=quality or inspect_mesh(mesh);valid=report['checks']['nondegenerate'] and report['checks']['unique_triangles']
    vertices=mesh['vertices'];bounds=[*[min(p[i] for p in vertices) for i in range(3)],*[max(p[i] for p in vertices) for i in range(3)]]
    volume=report['native']['signed_volume_interval_mm3'];area=report['native']['area_interval_mm2']
    return {'valid':valid,'bounds':bounds,'area':sum(area)/2,'volume':sum(volume)/2 if report['watertight_solid'] else None,
            'solids':report['native']['bounded_solids'] if report['watertight_solid'] else 0,'precision_solid_only':False,
            'dimensions':{},'geometry_hash':report['geometry_hash'],'engine_build':report['engine'],
            'cache_key':feature['cache_key'],'local_frame':feature.get('local_frame','world'),
            'coordinate_frame':'world','dimension_frame':feature.get('local_frame','world'),
            'mesh_quality':report,'source_conversion':mesh.get('source_conversion'),
            'coverage':'entire_authoritative_indexed_mesh','manufacturing_status':'not_certified'}

def combine(meshes):
    vertices=[];triangles=[]
    for mesh in meshes:
        offset=len(vertices);vertices.extend(mesh['vertices']);triangles.extend([[i+offset for i in t] for t in mesh['triangles']])
    result={'vertices':vertices,'triangles':triangles};validate_data(result);return result

def correspondence_bound(original, restored):
    validate_data(original);validate_data(restored)
    require(len(original['vertices'])==len(restored['vertices']) and original['triangles']==restored['triangles'],
            'Exportierte Dreieckszuordnung stimmt nicht überein.','INTEGRITY_FAILURE')
    error=0.0
    for a,b in zip(original['vertices'],restored['vertices']):
        squared=sum((Fraction(x)-Fraction(y))**2 for x,y in zip(a,b))
        bound=0.0 if squared==0 else math.nextafter(math.sqrt(math.nextafter(float(squared),math.inf)),math.inf)
        error=max(error,bound)
    return error

def export_stl(mesh, path, tolerance):
    """Check the actual binary32 output again, including topology after rounding."""
    validate_data(mesh);error=0.0
    with open(path,'wb') as output:
        output.write(b'LLcad authoritative indexed mesh'.ljust(80,b'\0'));output.write(struct.pack('<I',len(mesh['triangles'])))
        for t in mesh['triangles']:
            values=[x for i in t for x in mesh['vertices'][i]]
            try:packed=struct.pack('<9f',*values)
            except (OverflowError,struct.error):raise GeometryError('PRECISION_UNSUPPORTED','STL-Koordinaten passen nicht in binary32.')
            rounded=struct.unpack('<9f',packed)
            for i in range(3):
                squared=sum((Fraction(values[3*i+j])-Fraction(rounded[3*i+j]))**2 for j in range(3))
                bound=0.0 if squared==0 else math.nextafter(math.sqrt(math.nextafter(float(squared),math.inf)),math.inf)
                error=max(error,bound)
            output.write(struct.pack('<3f',0,0,0)+packed+struct.pack('<H',0))
    require(error<=tolerance,'STL-Quantisierung überschreitet die verlangte Geometriegenauigkeit.','PRECISION_UNSUPPORTED')
    restored=read_stl(path,'mm');quality=inspect_mesh(restored)
    return restored,quality,{'status':'checks_passed_within_profile','method':'binary32_roundtrip_and_exact_mesh_predicates',
        'unit':'mm','measured_vertex_error_bound_mm':error,'certified_surface_bound':error,
        'bound_domain':'corresponding_original_and_serialized_triangles_before_exact_coordinate_indexing',
        'restored_geometry_hash':quality['geometry_hash'],'restored_mesh_quality':quality,'lost_semantics':['feature_history']}
