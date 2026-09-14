"""Bounded face provenance from registered primitive roles and OCCT history.

Face ordinals address one exact B-Rep only. They are never used for rebinding.
Unsupported builders leave provenance empty instead of guessing by proximity.
"""
from geometry import *
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Plane

VERSION = 2
MAX_FACES = 8192


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def face_list(shape):
    faces = list(explore(shape, TopAbs_FACE))
    require(len(faces) <= MAX_FACES, 'Flächenbudget überschritten.', 'BUDGET_EXCEEDED')
    return faces

def edge_list(shape):
    mapping = TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(shape, TopAbs_EDGE, mapping)
    require(mapping.Extent() <= MAX_FACES * 4, 'Kantenbudget überschritten.', 'BUDGET_EXCEEDED')
    return [mapping.FindKey(i) for i in range(1, mapping.Extent()+1)]

def successors(maker, method, shape):
    if maker is None or not hasattr(maker, method): return []
    result = getattr(maker, method)(shape)
    if isinstance(result, TopoDS_Shape): return [] if result.IsNull() else [result]
    return list(result)

def adjacent_edges(shape, trace):
    """An edge's source is its incident faces, without geometric matching."""
    entries=[]
    for edge in edge_list(shape):
        sources={}; unknown=False
        for face in trace:
            if face['shape'].ShapeType()!=TopAbs_FACE: continue
            if any(edge.IsSame(candidate) for candidate in explore(face['shape'],TopAbs_EDGE)):
                if not face['origins']: unknown=True
                for source in face['origins']: sources[source['key']]=source
        keys=sorted(sources)
        entries.append({'shape':edge,'origins': [] if unknown or not keys else [
            {'key':digest(['incident_faces',keys]),'feature_id':next(iter(sources.values()))['feature_id'],'role':'edge_between_source_faces'}], 'kind':'edge'})
    return entries


def origin(fid, operator, role):
    return {'key': digest([fid, operator, role]), 'feature_id': fid, 'role': role}


def primitive(shape, fid, operator, roles=None):
    result = []
    for face in face_list(shape):
        role = next((name for name, named in (roles or {}).items() if face.IsSame(named)), None)
        if role is None and operator in ('sphere', 'torus', 'nurbs_surface'):
            role = 'surface'
        if role is None and operator in ('cylinder', 'cone'):
            adaptor = BRepAdaptor_Surface(TopoDS.Face_s(face))
            if adaptor.GetType() == GeomAbs_Plane:
                fb, sb = bounds(face), bounds(shape)
                if abs(fb[2] - sb[2]) < 1e-7 and abs(fb[5] - sb[2]) < 1e-7:
                    role = 'bottom'
                elif abs(fb[2] - sb[5]) < 1e-7 and abs(fb[5] - sb[5]) < 1e-7:
                    role = 'top'
            else:
                role = 'wall'
        result.append({'shape': face, 'origins': [origin(fid, operator, role)] if role else []})
    return result


def propagate(shape, inputs, prefix, maker=None, owner=None):
    faces = face_list(shape)
    if not faces: faces = edge_list(shape)
    mapping = TopTools_IndexedMapOfShape()
    for face in faces:
        mapping.Add(face)
    found = {}
    unknown = set()
    for slot, trace in enumerate(inputs):
        for entry in trace:
            candidates = [entry['shape']]
            if maker is not None:
                candidates.extend(successors(maker, 'Modified', entry['shape']))
            for successor in candidates:
                index = mapping.FindIndex(successor)
                if not index:
                    continue
                origins = found.setdefault(index, {})
                if not entry['origins']:
                    unknown.add(index)
                for source in entry['origins']:
                    item = dict(source, key=digest([prefix, slot, source['key']]))
                    if owner is not None:
                        item['feature_id'] = owner
                    origins[item['key']] = item
    return [{'shape': face, '_unknown':mapping.FindIndex(face) in unknown, 'origins': [] if mapping.FindIndex(face) in unknown else sorted(found.get(mapping.FindIndex(face), {}).values(), key=lambda x: x['key'])}
            for face in faces]

def builder_trace(shape, deps, traces, prefix, fid, op, maker, named):
    result=propagate(shape,traces,prefix,maker)
    generated=[]
    for slot,(dep,trace) in enumerate(zip(deps,traces)):
        seeds=trace if not face_list(dep) else [*trace,*adjacent_edges(dep,trace)]
        for seed in seeds:
            for successor in successors(maker,'Generated',seed['shape']):
                for face in result:
                    if face['shape'].IsSame(successor):
                        keys=sorted(s['key'] for s in seed['origins'])
                        if keys:
                            generated.append((face,{'key':digest([prefix,'generated',slot,keys]),'feature_id':fid,'role':'generated_from_'+seed['origins'][0]['role']}))
                        else: face['_unknown']=True
    for face,source in generated:
        if not any(s['key']==source['key'] for s in face['origins']): face['origins'].append(source)
    for face in result:
        if face.get('_unknown'): face['origins']=[]
        role=next((role for role,native in named.items() if face['shape'].IsSame(native)),None)
        if role is not None: face['origins']=[origin(fid,op,role)]
    return result


def boolean_trace(op, left, right, left_trace, right_trace, prefix):
    maker = {'union': BRepAlgoAPI_Fuse, 'difference': BRepAlgoAPI_Cut, 'intersection': BRepAlgoAPI_Common}[op](left, right)
    maker.SetRunParallel(False)
    maker.Build()
    require(maker.IsDone(), 'Boolesche Operation fehlgeschlagen.')
    shape = maker.Shape()
    return shape, propagate(shape, [left_trace, right_trace], prefix, maker)


def transform_trace(shape, trace, prefix, owner, x=0., y=0., z=0., angle=0., scale=1., mirror=False):
    transform = gp_Trsf()
    if mirror:
        transform.SetMirror(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)))
    else:
        transform.SetScale(gp_Pnt(0, 0, 0), scale)
        if angle:
            rotation = gp_Trsf()
            rotation.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), angle)
            transform = rotation.Multiplied(transform)
        translation = gp_Trsf()
        translation.SetTranslation(gp_Vec(x, y, z))
        transform = translation.Multiplied(transform)
    maker = BRepBuilderAPI_Transform(shape, transform, mirror)
    result = maker.Shape()
    return result, propagate(result, [trace], prefix, maker, owner)


def evaluate_feature(f, deps, traces):
    fid, p, c = f['id'], f['values'], f['construction']
    op = c['operator']
    prefix = [fid, op]
    x, y, z = (p.get(k, 0) for k in ('x', 'y', 'z'))
    from advanced import TRACKED, build
    if op in TRACKED:
        shape,maker,named=build(f,deps)
        if op in ('line','arc'):
            return shape,[{'shape':edge,'origins':[origin(fid,op,'curve')]} for edge in edge_list(shape)]
        return shape,builder_trace(shape,deps,traces,prefix,fid,op,maker,named)
    if op in ('profile','circle','bezier','bspline'):
        shape=make_feature(f,deps)
        # Profile segment ordinals belong to the explicit ordered construction,
        # and are persisted with native fingerprints before any kernel edit.
        return shape,[{'shape':edge,'origins':[origin(fid,op,'segment_'+str(i))]} for i,edge in enumerate(edge_list(shape))]
    if op == 'box':
        maker = BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), p['width'], p['depth'], p['height'])
        shape = maker.Shape()
        roles = {name: getattr(maker, method)() for name, method in {
            'bottom': 'BottomFace', 'top': 'TopFace', 'x_min': 'BackFace',
            'x_max': 'FrontFace', 'y_min': 'LeftFace', 'y_max': 'RightFace'}.items()}
        return shape, primitive(shape, fid, op, roles)
    if op in ('sphere', 'cylinder', 'cone', 'torus', 'nurbs_surface'):
        shape = make_feature(f, deps)
        return shape, primitive(shape, fid, op)
    if op in ('union', 'difference', 'intersection'):
        shape, trace = deps[0], traces[0]
        for index in range(1, len(deps)):
            shape, trace = boolean_trace(op, shape, deps[index], trace, traces[index], [prefix, index])
        return shape, trace
    if op in ('hole', 'groove', 'pocket'):
        depth = p['depth']
        ax = gp_Ax2(gp_Pnt(x, y, z-depth), gp_Dir(0, 0, 1))
        if op == 'pocket':
            maker = BRepPrimAPI_MakeBox(gp_Pnt(x-p['width']/2, y-p['length']/2, z-depth), p['width'], p['length'], depth+1e-6)
            cutter = maker.Shape()
            cutter_trace = primitive(cutter, fid, op, {name: getattr(maker, method)() for name, method in {
                'floor': 'BottomFace', 'opening': 'TopFace', 'x_min_wall': 'BackFace',
                'x_max_wall': 'FrontFace', 'y_min_wall': 'LeftFace', 'y_max_wall': 'RightFace'}.items()})
        else:
            cutter = BRepPrimAPI_MakeCylinder(ax, p['radius'] + (p['width']/2 if op == 'groove' else 0), depth+1e-6).Shape()
            cutter_trace = primitive(cutter, fid, 'cylinder')
            for entry in cutter_trace:
                role = entry['origins'][0]['role']
                entry['origins'] = [origin(fid, op, {'bottom': 'floor', 'top': 'opening', 'wall': 'outer_wall' if op == 'groove' else 'wall'}[role])]
            if op == 'groove':
                inner = BRepPrimAPI_MakeCylinder(ax, p['radius']-p['width']/2, depth+1e-6).Shape()
                inner_trace = primitive(inner, fid, 'cylinder')
                for entry in inner_trace:
                    role = entry['origins'][0]['role']
                    entry['origins'] = [origin(fid, op, 'inner_wall' if role == 'wall' else 'inner_' + role)]
                cutter, cutter_trace = boolean_trace('difference', cutter, inner, cutter_trace, inner_trace, [prefix, 'cutter'])
        shape, trace = boolean_trace('difference', deps[0], cutter, traces[0], cutter_trace, prefix)
        before, after = properties(deps[0])['volume'], properties(shape)['volume']
        require(before is not None and after is not None, 'Schneidoperation benötigt einen geschlossenen Körper.')
        require(before-after > 1e-10, 'Schneidwerkzeug schneidet kein Material.')
        return shape, trace
    if op in ('instance', 'transform', 'mirror'):
        return transform_trace(deps[0], traces[0], prefix, fid, x, y, z, p.get('angle', 0), p.get('scale', 1), op == 'mirror')
    if op == 'pattern':
        copies, history = [], []
        for i in range(int(p['count'])):
            shape, trace = transform_trace(deps[0], traces[0], [prefix, i], fid, i*p.get('dx', 0), i*p.get('dy', 0), i*p.get('dz', 0))
            copies.append(shape)
            history.append(trace)
        shape = compound(copies)
        return shape, propagate(shape, history, prefix)
    if op == 'assembly':
        shape = compound(deps)
        return shape, propagate(shape, traces, prefix)
    shape = make_feature(f, deps)
    return shape, [{'shape': face, 'origins': []} for face in face_list(shape)]


def serialize(trace, key):
    records = []
    faces=[entry for entry in trace if entry['shape'].ShapeType()==TopAbs_FACE]
    for index, entry in enumerate(faces):
        face = entry['shape']
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        center = props.CentreOfMass()
        sources = entry['origins'] if len(entry['origins']) <= 16 else []
        records.append({'face_id': 'face_' + digest([key, index])[:32],
                        'fingerprint': shape_hash(face), 'origins': sources,
                        'area': props.Mass(), 'center': [center.X(), center.Y(), center.Z()],
                        'bounds': bounds(face), 'surface': BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType().name})
    edges=[{'fingerprint':shape_hash(entry['shape']),'origins':entry['origins']} for entry in trace if entry['shape'].ShapeType()==TopAbs_EDGE]
    return {'version': VERSION, 'cache_key': key, 'faces': records, 'edges': edges}


def restore(shape, data, key):
    faces = face_list(shape)
    require(data['version'] == VERSION and data['cache_key'] == key and len(faces) == len(data['faces']),
            'Flächenhistorie passt nicht zum Cache.', 'INTEGRITY_FAILURE')
    result = []
    for face, record in zip(faces, data['faces']):
        require(shape_hash(face) == record['fingerprint'], 'Flächenreihenfolge im Cache ist nicht belegt.', 'INTEGRITY_FAILURE')
        result.append({'shape': face, 'origins': record['origins']})
    if data.get('edges'):
        edges=edge_list(shape)
        require(len(edges)==len(data['edges']), 'Kantenhistorie passt nicht zum Cache.', 'INTEGRITY_FAILURE')
        for edge,record in zip(edges,data['edges']):
            require(shape_hash(edge)==record['fingerprint'], 'Kantenreihenfolge im Cache ist nicht belegt.', 'INTEGRITY_FAILURE')
            result.append({'shape':edge,'origins':record['origins']})
    return result
