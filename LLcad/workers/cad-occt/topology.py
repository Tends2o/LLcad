"""Bounded face provenance from registered primitive roles and OCCT history.

Face ordinals address one exact B-Rep only. They are never used for rebinding.
Unsupported builders leave provenance empty instead of guessing by proximity.
"""
from geometry import *
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Plane
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

VERSION = 3
MAX_FACES = 8192


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def face_list(shape):
    faces = list(explore(shape, TopAbs_FACE))
    require(len(faces) <= MAX_FACES, 'Face budget exceeded.', 'BUDGET_EXCEEDED')
    return faces

def edge_list(shape):
    mapping = TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(shape, TopAbs_EDGE, mapping)
    require(mapping.Extent() <= MAX_FACES * 4, 'Edge budget exceeded.', 'BUDGET_EXCEEDED')
    return [mapping.FindKey(i) for i in range(1, mapping.Extent()+1)]

def successors(maker, method, shape):
    if maker is None or not hasattr(maker, method): return []
    result = getattr(maker, method)(shape)
    if isinstance(result, TopoDS_Shape): return [] if result.IsNull() else [result]
    return list(result)

def adjacent_edges(shape, trace):
    """An edge's or vertex's source is its incident faces, without geometric matching.

    Incidence comes from one native ancestor map (edge/vertex -> faces) instead of
    re-exploring every traced face for every edge: linear instead of quadratic in
    the face count. Traced faces are visited in trace order, exactly as before, so
    the recorded keys and the reported feature are the same."""
    faces=[entry for entry in trace if entry['shape'].ShapeType()==TopAbs_FACE]
    traced=TopTools_IndexedMapOfShape(); positions=[]
    for position,entry in enumerate(faces):
        index=traced.Add(entry['shape'])
        if index>len(positions): positions.append([])
        positions[index-1].append(position)
    entries=[]
    for kind,native,role,items in (('edge',TopAbs_EDGE,'edge_between_source_faces',edge_list(shape)),('vertex',TopAbs_VERTEX,'vertex_between_source_faces',vertex_list(shape))):
        ancestors=TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(shape,native,TopAbs_FACE,ancestors)
        for item in items:
            incident=set()
            slot=ancestors.FindIndex(item)
            if slot:
                for face in ancestors.FindFromIndex(slot):
                    index=traced.FindIndex(face)
                    if index: incident.update(positions[index-1])
            sources={}; unknown=False
            for position in sorted(incident):
                face=faces[position]
                if not face['origins']: unknown=True
                for source in face['origins']: sources[source['key']]=source
            keys=sorted(sources)
            entries.append({'shape':item,'origins': [] if unknown or not keys else [
                {'key':digest(['incident_faces',kind,keys]),'feature_id':next(iter(sources.values()))['feature_id'],'role':role}], 'kind':kind})
    return entries


def vertex_list(shape):
    mapping = TopTools_IndexedMapOfShape(); TopExp.MapShapes_s(shape, TopAbs_VERTEX, mapping)
    require(mapping.Extent() <= MAX_FACES * 4, 'Vertex budget exceeded.', 'BUDGET_EXCEEDED')
    return [mapping.FindKey(i) for i in range(1, mapping.Extent()+1)]


def origin(fid, operator, role):
    return {'key': digest([fid, operator, role]), 'feature_id': fid, 'role': role}


def primitive(shape, fid, operator, roles=None):
    result = []; shape_bounds = []
    for face in face_list(shape):
        role = next((name for name, named in (roles or {}).items() if face.IsSame(named)), None)
        if role is None and operator in ('sphere', 'torus', 'nurbs_surface'):
            role = 'surface'
        if role is None and operator in ('cylinder', 'cone', 'strip'):
            adaptor = BRepAdaptor_Surface(TopoDS.Face_s(face))
            if adaptor.GetType() == GeomAbs_Plane:
                # The shape's box once, not once per planar face.
                if not shape_bounds: shape_bounds.append(bounds(shape))
                fb, sb = bounds(face), shape_bounds[0]
                if abs(fb[2] - sb[2]) < 1e-7 and abs(fb[5] - sb[2]) < 1e-7:
                    role = 'bottom'
                elif abs(fb[2] - sb[5]) < 1e-7 and abs(fb[5] - sb[5]) < 1e-7:
                    role = 'top'
                elif operator == 'strip':
                    role = 'wall'
            else:
                role = 'wall'
        result.append({'shape': face, 'origins': [origin(fid, operator, role)] if role else []})
    return result


def propagate(shape, inputs, prefix, maker=None, owner=None, preserve_origins=False):
    faces = face_list(shape)
    if not faces: faces = edge_list(shape)
    elif preserve_origins and any(any(e['shape'].ShapeType() == TopAbs_EDGE for e in trace) and
                                  any(e['shape'].ShapeType() == TopAbs_FACE for e in trace) for trace in inputs):
        # A sketch region carries its boundary segments next to its face; a moved copy
        # (frame or placement) keeps both, faces first, in the order archive() expects.
        # Builders never do: their results record faces only, as before.
        faces = faces + edge_list(shape)
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
                    item = dict(source) if preserve_origins else dict(source, key=digest([prefix, slot, source['key']]))
                    if owner is not None:
                        item['feature_id'] = owner
                    origins[item['key']] = item
    return [{'shape': face, '_unknown':mapping.FindIndex(face) in unknown, 'origins': [] if mapping.FindIndex(face) in unknown else sorted(found.get(mapping.FindIndex(face), {}).values(), key=lambda x: x['key'])}
            for face in faces]

def builder_trace(shape, deps, traces, prefix, fid, op, maker, named):
    result=propagate(shape,traces,prefix,maker)
    # Result faces by native identity, in result order: a generated successor is
    # looked up once instead of being compared with every result face.
    produced=TopTools_IndexedMapOfShape(); positions=[]
    for position,face in enumerate(result):
        index=produced.Add(face['shape'])
        if index>len(positions): positions.append([])
        positions[index-1].append(position)
    generated=[]
    for slot,(dep,trace) in enumerate(zip(deps,traces)):
        # A sketch face brings its own named boundary segments; faces without them get
        # their edges' provenance from the incident faces.
        own_edges=any(entry['shape'].ShapeType()==TopAbs_EDGE for entry in trace)
        seeds=trace if not face_list(dep) or own_edges else [*trace,*adjacent_edges(dep,trace)]
        for seed in seeds:
            for successor in successors(maker,'Generated',seed['shape']):
                index=produced.FindIndex(successor)
                if not index: continue
                for position in positions[index-1]:
                    face=result[position]
                    keys=sorted(s['key'] for s in seed['origins'])
                    if keys:
                        generated.append((face,{'key':digest([prefix,'generated',slot,keys]),'feature_id':fid,'role':'generated_from_'+seed['origins'][0]['role']}))
                    else: face['_unknown']=True
    for face,source in generated:
        if not any(s['key']==source['key'] for s in face['origins']): face['origins'].append(source)
    named={k:v for k,v in named.items() if not k.startswith('_')}
    def named_as(shape,native):
        return any(shape.IsSame(item) for item in native) if isinstance(native,list) else shape.IsSame(native)
    for face in result:
        if face.get('_unknown'): face['origins']=[]
        role=next((role for role,native in named.items() if named_as(face['shape'],native)),None)
        if role is not None: face['origins']=[origin(fid,op,role)]
    return result


def boolean_trace(op, left, right, left_trace, right_trace, prefix):
    maker = boolean_maker(op, [left], [right])
    shape = maker.Shape()
    return shape, propagate(shape, [left_trace, right_trace], prefix, maker)


def nary_boolean_trace(op, deps, traces, prefix):
    """Union or difference of n inputs as ONE operation (argument deps[0], tools the
    rest), with every input's history taken from that single builder. For two
    inputs the provenance keys equal the former pairwise chain's (step prefix 1,
    slots 0 and 1). Intersection keeps the pairwise chain: a multi-tool Common
    computes (A) ∩ (B ∪ C), not A ∩ B ∩ C."""
    if len(deps) == 1: return deps[0], traces[0]
    if op == 'intersection':
        shape, trace = deps[0], traces[0]
        for index in range(1, len(deps)):
            shape, trace = boolean_trace(op, shape, deps[index], trace, traces[index], [prefix, index])
        return shape, trace
    maker = boolean_maker(op, deps[:1], deps[1:])
    shape = maker.Shape()
    return shape, propagate(shape, traces, [prefix, 1], maker)


def transform_trace(shape, trace, prefix, owner, x=0., y=0., z=0., angle=0., scale=1., mirror=False, construction=None):
    transform = gp_Trsf()
    if mirror:
        transform = mirror_transform(construction)
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


def feature_transform(f):
    """The feature's own construction frame (plane preset or placement) in its local
    frame, or None. Compiled as a proper rotation (columns x, y, z) and a translation."""
    placement = f.get('feature_placement')
    if not placement: return None
    r, t = placement['rotation'], placement['translation']
    transform = gp_Trsf()
    transform.SetValues(r[0][0], r[0][1], r[0][2], t[0], r[1][0], r[1][1], r[1][2], t[1], r[2][0], r[2][1], r[2][2], t[2])
    return transform


def moved(shape, trace, transform):
    maker = BRepBuilderAPI_Transform(shape, transform, False)
    result = maker.Shape()
    return result, propagate(result, [trace], None, maker, preserve_origins=True)


def evaluate_feature(f, deps, traces):
    """Build one feature in its construction frame. A feature with `placement`/`plane`
    sees its inputs in that frame (the inverse placement) and its result is placed back,
    exactly like a structure frame but for this one feature."""
    transform = feature_transform(f)
    if transform is None: return construct(f, deps, traces)
    inverse = transform.Inverted()
    local = [moved(d, t, inverse) for d, t in zip(deps, traces)]
    shape, trace = construct(f, [m[0] for m in local], [m[1] for m in local])
    return moved(shape, trace, transform)


def relief_feature(f, deps, traces, prefix):
    """text / svg_emblem: the relief solid alone, or fused into / cut from the target in
    one Boolean operation. Emboss letters must touch the target and a deboss must remove
    material; neither is left to a later surprise."""
    import lettering, advanced
    fid, op = f['id'], f['construction']['operator']
    tool, roles, report = lettering.build(f, bool(deps))
    tool_trace = lettering.trace_roles(tool, fid, op, roles)
    advanced.CONSTRUCTION_REPORTS.setdefault(fid, {}).update(report)
    if not deps: return tool, tool_trace
    emboss = f['values']['depth'] > 0
    maker = boolean_maker('union' if emboss else 'difference', [deps[0]], [tool])
    shape = maker.Shape()
    before, after = volume_only(deps[0]), volume_only(shape)
    require(before is not None and after is not None, 'A relief needs a closed target solid.')
    if emboss:
        target_solids = sum(1 for _ in explore(deps[0], TopAbs_SOLID))
        loose = sum(1 for _ in explore(shape, TopAbs_SOLID)) - target_solids
        require(loose <= 0, '%d relief parts do not touch the target solid; check the placement, overlap or the target face.' % loose)
        require(after - before > 1e-10, 'The relief adds no material to the target solid.')
    else:
        require(before - after > 1e-10, 'The engraving cuts no material (does it lie on the target face?).')
    return shape, propagate(shape, [traces[0], tool_trace], prefix, maker)


def knurl_feature(f, deps, traces, prefix):
    """Straight knurl about the local Z axis at (x, y), from z to z + height.
    Without input: the knurled ring/cylinder as ONE notched profile extruded once (no
    Boolean). With a target: all grooves cut in ONE multi-tool Boolean."""
    import advanced
    fid, p, c = f['id'], f['values'], f['construction']
    r, h, n, d = p['radius'], p['height'], int(p['count']), p['depth']
    require(3 <= n <= 1000 and n == p['count'], 'Knurling needs 3 to 1000 grooves.')
    require(0 < d < r, 'The groove depth must be smaller than the radius.')
    width = p.get('width', math.pi * r / n)
    half = math.asin(min(1., width / (2 * r)))
    require(2 * half < 2 * math.pi / n - 1e-9, 'The groove width fills the circumference; use fewer or narrower grooves.')
    shape_kind = c.get('profile', 'v')
    cx, cy, z0 = p.get('x', 0.), p.get('y', 0.), p.get('z', 0.)
    def groove_points(theta):
        e = lambda a, rad: (cx + rad * math.cos(a), cy + rad * math.sin(a))
        if shape_kind == 'v': return [e(theta - half, r), e(theta, r - d), e(theta + half, r)]
        inner = math.asin(min(1., width / (2 * (r - d))))
        return [e(theta - half, r), e(theta - inner, r - d), e(theta + inner, r - d), e(theta + half, r)]
    if not deps:
        from OCP.GC import GC_MakeArcOfCircle
        wire = BRepBuilderAPI_MakeWire()
        for i in range(n):
            theta = 2 * math.pi * i / n; nxt = 2 * math.pi * (i + 1) / n
            pts = groove_points(theta)
            for a, b in zip(pts, pts[1:]): wire.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(*a, z0), gp_Pnt(*b, z0)).Edge())
            a0, a1 = theta + half, nxt - half
            mid = (a0 + a1) / 2
            arc = GC_MakeArcOfCircle(gp_Pnt(cx + r * math.cos(a0), cy + r * math.sin(a0), z0), gp_Pnt(cx + r * math.cos(mid), cy + r * math.sin(mid), z0), gp_Pnt(cx + r * math.cos(a1), cy + r * math.sin(a1), z0))
            wire.Add(BRepBuilderAPI_MakeEdge(arc.Value()).Edge())
        require(wire.IsDone(), 'The knurl profile is not closed.')
        face = BRepBuilderAPI_MakeFace(wire.Wire(), True)
        require(face.IsDone(), 'The knurl profile does not yield a face.')
        region = face.Face()
        if p.get('inner_radius'):
            ri = p['inner_radius']
            require(0 < ri < r - d, 'The inner radius must be smaller than the groove bottom.')
            hole = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(cx, cy, z0), gp_Dir(0, 0, 1)), ri)).Edge()).Wire(), True).Face()
            region = TopoDS.Face_s(next(explore(boolean('difference', region, hole), TopAbs_FACE)))
        facing = face_normal(region)
        if facing is not None and facing.Z() < 0: region = TopoDS.Face_s(region.Reversed())
        prism = BRepPrimAPI_MakePrism(region, gp_Vec(0, 0, h))
        require(prism.IsDone(), 'The knurl extrusion failed.')
        shape = prism.Shape()
        advanced.CONSTRUCTION_REPORTS.setdefault(fid, {}).update(method='notched_profile_single_prism', grooves=n)
        role = lambda face_: 'top' if face_.IsSame(prism.LastShape()) else 'bottom' if face_.IsSame(prism.FirstShape()) else 'knurl_wall'
        return shape, [{'shape': face_, 'origins': [origin(fid, 'knurl', role(face_))]} for face_ in face_list(shape)]
    tools, tool_traces = [], []
    for i in range(n):
        pts = groove_points(2 * math.pi * i / n)
        # The groove tool reaches beyond the outer radius so its cut opens cleanly.
        outer = [(cx + (x - cx) * 1.05 + 0., cy + (y - cy) * 1.05) for x, y in (pts[0], pts[-1])]
        poly = BRepBuilderAPI_MakePolygon()
        for q in [outer[0], *pts, outer[1]]: poly.Add(gp_Pnt(q[0], q[1], z0))
        poly.Close()
        face = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
        facing = face_normal(face)
        if facing is not None and facing.Z() < 0: face = TopoDS.Face_s(face.Reversed())
        tool = BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, h)).Shape()
        tools.append(tool); tool_traces.append([{'shape': face_, 'origins': [origin(fid, 'knurl', 'groove_face')]} for face_ in face_list(tool)])
    maker = boolean_maker('difference', [deps[0]], tools)
    shape = maker.Shape()
    before, after = volume_only(deps[0]), volume_only(shape)
    require(before is not None and after is not None and before - after > 1e-10, 'Knurling cuts no material (check radius/position).')
    advanced.CONSTRUCTION_REPORTS.setdefault(fid, {}).update(method='single_multi_tool_cut', grooves=n)
    return shape, propagate(shape, [traces[0], *tool_traces], prefix, maker)


def cut_pattern_feature(f, deps, traces, prefix):
    """Copies of the tool (input 2) on a linear or circular pattern, removed from (or
    added to) the target (input 1) in ONE multi-tool Boolean."""
    import advanced
    fid, p, c = f['id'], f['values'], f['construction']
    count = int(p['count'])
    require(1 <= count <= 10000 and count == p['count'], 'Invalid pattern count.', 'BUDGET_EXCEEDED')
    tools, tool_traces = [], []
    for i in range(count):
        transform = gp_Trsf()
        if c.get('kind', 'linear') == 'circular':
            transform.SetRotation(gp_Ax1(gp_Pnt(*map(float, c.get('origin', ['0', '0', '0']))), unit(c.get('axis', ['0', '0', '1']))), p.get('angle', 2 * math.pi) * i / count)
        else:
            transform.SetTranslation(gp_Vec(*(i * p.get(k, 0.) for k in ('dx', 'dy', 'dz'))))
        maker = BRepBuilderAPI_Transform(deps[1], transform, False)
        tools.append(maker.Shape()); tool_traces.append(propagate(maker.Shape(), [traces[1]], [prefix, i], maker, fid))
    mode = c.get('operation', 'difference')
    maker = boolean_maker(mode, [deps[0]], tools)
    shape = maker.Shape()
    before, after = volume_only(deps[0]), volume_only(shape)
    require(before is not None and after is not None, 'A pattern cut needs closed solids.')
    require(before - after > 1e-10 if mode == 'difference' else after - before > 1e-10,
            'The pattern tool does not change the target solid (check the position).')
    advanced.CONSTRUCTION_REPORTS.setdefault(fid, {}).update(method='single_multi_tool_boolean', tools=count, operation=mode)
    return shape, propagate(shape, [traces[0], *tool_traces], prefix, maker)


def construct(f, deps, traces):
    fid, p, c = f['id'], f['values'], f['construction']
    op = c['operator']
    prefix = [fid, op]
    x, y, z = (p.get(k, 0) for k in ('x', 'y', 'z'))
    from advanced import TRACKED, build
    if op == 'sketch':
        import sketch, advanced
        face, report = sketch.build(f)
        advanced.CONSTRUCTION_REPORTS.setdefault(fid, {}).update(report)
        # Boundary segments are named in native edge order; extruded walls inherit them.
        return face, [{'shape': face, 'origins': [origin(fid, op, 'region')]},
                      *({'shape': edge, 'origins': [origin(fid, op, 'segment_' + str(i))]} for i, edge in enumerate(edge_list(face)))]
    if op in ('text', 'svg_emblem'):
        return relief_feature(f, deps, traces, prefix)
    if op == 'knurl':
        return knurl_feature(f, deps, traces, prefix)
    if op == 'cut_pattern':
        return cut_pattern_feature(f, deps, traces, prefix)
    if op in TRACKED:
        shape,maker,named=build(f,deps,traces)
        if op in ('line','arc'):
            return shape,[{'shape':edge,'origins':[origin(fid,op,'curve')]} for edge in edge_list(shape)]
        return shape,builder_trace(shape,deps,traces,prefix,fid,op,maker,named)
    if op in ('profile','circle','bezier','bspline','nurbs_curve'):
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
    if op in ('sphere', 'cylinder', 'cone', 'torus', 'nurbs_surface', 'strip'):
        shape = make_feature(f, deps)
        return shape, primitive(shape, fid, op)
    if op in ('union', 'difference', 'intersection'):
        return nary_boolean_trace(op, deps, traces, prefix)
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
        before, after = volume_only(deps[0]), volume_only(shape)
        require(before is not None and after is not None, 'A cutting operation needs a closed solid.')
        require(before-after > 1e-10, 'The cutting tool cuts no material.')
        return shape, trace
    if op in ('instance', 'transform', 'mirror'):
        return transform_trace(deps[0], traces[0], prefix, fid, x, y, z, p.get('angle', 0), p.get('scale', 1), op == 'mirror', c)
    if op in ('pattern','circular_pattern'):
        copies, history = [], []
        for i,slot,transform in pattern_placements(f):
            maker=BRepBuilderAPI_Transform(deps[slot],transform,False)
            shape=maker.Shape()
            trace=propagate(shape,[traces[slot]],[prefix,i],maker,fid)
            for entry in trace:
                for source in entry['origins']:
                    source['occurrences']=[*source.get('occurrences',[]),{'feature_id':fid,'index':i}]
            copies.append(shape)
            history.append(trace)
        maker = CompoundBuilder(copies);shape = maker.Shape()
        return shape, propagate(shape, history, prefix, maker)
    if op == 'assembly':
        maker = CompoundBuilder(deps);shape = maker.Shape()
        return shape, propagate(shape, traces, prefix, maker)
    shape = make_feature(f, deps)
    return shape, [{'shape': face, 'origins': []} for face in face_list(shape)]


def serialize(trace, key):
    records = []
    faces=[entry for entry in trace if entry['shape'].ShapeType()==TopAbs_FACE]
    # Face adjacency through shared native edges (same TShape), never by coordinate proximity.
    edge_map=TopTools_IndexedMapOfShape();incident={}
    for index,entry in enumerate(faces):
        for edge in explore(entry['shape'],TopAbs_EDGE):
            slot=edge_map.Add(edge);incident.setdefault(slot,set()).add(index)
    adjacency={index:set() for index in range(len(faces))}
    for members in incident.values():
        for a in members:
            adjacency[a].update(b for b in members if b!=a)
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
        records[-1]['uv_bounds']=list(BRepTools.UVBounds_s(TopoDS.Face_s(face)))
        records[-1]['adjacent_face_ids']=['face_' + digest([key, other])[:32] for other in sorted(adjacency[index])][:256]
    edges=[{'fingerprint':shape_hash(entry['shape']),'origins':entry['origins']} for entry in trace if entry['shape'].ShapeType()==TopAbs_EDGE]
    return {'version': VERSION, 'cache_key': key, 'faces': records, 'edges': edges}


def archive(shape, trace, key, path):
    """Bind history to one immutable file and its deterministic native read.

    OCCT serializes ordered topology references. During the trusted writer /
    reader operation only, this ordering transports native history. It never
    matches entities across revisions. Face fingerprints are computed AFTER
    the read because OCCT normalizes stored transformation matrices on input.
    Retain these exact original file bytes for every subsequent cache read.
    """
    original_faces=face_list(shape)
    traced_faces=[e for e in trace if e['shape'].ShapeType()==TopAbs_FACE]
    require(len(original_faces)==len(traced_faces) and all(a.IsSame(b['shape']) for a,b in zip(original_faces,traced_faces)),
            'The native history is not completely ordered before storage.', 'INTEGRITY_FAILURE')
    traced_edges=[e for e in trace if e['shape'].ShapeType()==TopAbs_EDGE]
    if traced_edges:
        original_edges=edge_list(shape)
        require(len(original_edges)==len(traced_edges) and all(a.IsSame(b['shape']) for a,b in zip(original_edges,traced_edges)),
                'The native edge history is not completely ordered before storage.', 'INTEGRITY_FAILURE')
    write_brep(shape,path)
    restored=read_brep(path);new_faces=face_list(restored)
    require(len(new_faces)==len(traced_faces), 'Native storage changed the face count.', 'INTEGRITY_FAILURE')
    result=[dict(entry,shape=face) for entry,face in zip(traced_faces,new_faces)]
    if traced_edges:
        new_edges=edge_list(restored)
        require(len(new_edges)==len(traced_edges), 'Native storage changed the edge count.', 'INTEGRITY_FAILURE')
        result.extend(dict(entry,shape=edge) for entry,edge in zip(traced_edges,new_edges))
    records=serialize(result,key)
    with open(path,'rb') as handle:records['brep_sha256']=hashlib.sha256(handle.read()).hexdigest()
    return restored,result,records


def restore(shape, data, key, source_hash):
    """Re-attach archived history to the shape read from the archived bytes.

    The record names the sha256 of exactly those bytes, and the archive computed
    its face fingerprints from the deterministic read of the same bytes. With the
    hash bound, the same bytes give the same shape in the same face order, so the
    per-face re-serialisation (shape_hash) proves nothing more and is skipped —
    as the preview worker already does. Counts are still checked."""
    faces = face_list(shape)
    require(data['version'] == VERSION and data['cache_key'] == key and len(faces) == len(data['faces']) and data.get('brep_sha256')==source_hash,
            'The face history does not match the cache.', 'INTEGRITY_FAILURE')
    result = [{'shape': face, 'origins': record['origins']} for face, record in zip(faces, data['faces'])]
    if data.get('edges'):
        edges=edge_list(shape)
        require(len(edges)==len(data['edges']), 'The edge history does not match the cache.', 'INTEGRITY_FAILURE')
        result.extend({'shape':edge,'origins':record['origins']} for edge,record in zip(edges,data['edges']))
    return result
