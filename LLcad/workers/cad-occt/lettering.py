"""Relief lettering and emblems as ONE feature each: `text` (glyph faces from an allowed
font through OCCT StdPrs_BRepFont, laid out on a line, a planar arc or wrapped round a
cylinder) and `svg_emblem` (original SVG vector paths as exact line/Bezier wires with
holes). Both are prisms of +depth (emboss) or -depth (deboss) along the construction
+Z (or the radial direction on a cylinder); with a target input they are fused into or
cut from it in one Boolean operation. Input is compiler output, never executable source.
"""
import os, math, hashlib, re
import xml.etree.ElementTree as ET
from geometry import *
from OCP.TopoDS import TopoDS_Compound
from OCP.gp import gp_Pln
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.TopAbs import TopAbs_WIRE

_SHA = {}
MAX_GLYPHS = 512
MAX_CONTOURS = 4096


def _sha256(path):
    if path not in _SHA:
        with open(path, 'rb') as h: _SHA[path] = hashlib.sha256(h.read()).hexdigest()
    return _SHA[path]


# ---------------------------------------------------------------- planar regions

def _wire_points(wire, per_edge=12):
    points = []
    explorer = BRepTools_WireExplorer(TopoDS.Wire_s(wire))
    while explorer.More():
        edge = explorer.Current(); curve = BRepAdaptor_Curve(edge)
        u0, u1 = curve.FirstParameter(), curve.LastParameter()
        us = [u0 + (u1 - u0) * i / per_edge for i in range(per_edge)]
        if edge.Orientation() == TopAbs_REVERSED: us = [u1 - (u - u0) for u in us]
        for u in us:
            p = curve.Value(u); points.append((p.X(), p.Y()))
        explorer.Next()
    return points


def _signed_area(points):
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]))


def _inside(point, polygon):
    x, y = point; inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1): inside = not inside
    return inside


_PLANE = gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))


def _loop_face(wire):
    maker = BRepBuilderAPI_MakeFace(_PLANE, TopoDS.Wire_s(wire), True)
    require(maker.IsDone(), 'The outline does not yield a planar face.')
    return maker.Face()


def _unify(shape):
    unify = ShapeUpgrade_UnifySameDomain(shape, True, True, False); unify.Build()
    return unify.Shape()


def _winding(point, loops):
    """Winding number of `point` about closed oriented polylines."""
    x, y = point; total = 0
    for pts in loops:
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            if y1 <= y < y2 and (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1) > 0: total += 1
            elif y2 <= y < y1 and (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1) < 0: total -= 1
    return total


def _interior_point(face):
    """A point inside a planar face: the centroid of one triangle of its own mesh."""
    b = bounds(face)
    BRepMesh_IncrementalMesh(face, max(1e-4, 0.01 * max(b[3] - b[0], b[4] - b[1])), False, 0.5, False).Perform()
    location = TopLoc_Location(); tri = BRep_Tool.Triangulation_s(TopoDS.Face_s(face), location)
    require(tri is not None and tri.NbTriangles() > 0, 'The outline face cannot be classified.')
    best, area = None, -1.
    for i in range(1, min(tri.NbTriangles(), 64) + 1):
        a, b_, c = (tri.Node(k).Transformed(location.Transformation()) for k in tri.Triangle(i).Get())
        size = abs((b_.X() - a.X()) * (c.Y() - a.Y()) - (c.X() - a.X()) * (b_.Y() - a.Y()))
        if size > area: area, best = size, ((a.X() + b_.X() + c.X()) / 3, (a.Y() + b_.Y() + c.Y()) / 3)
    return best


def region_by_arrangement(wires, rule):
    """Fill of arbitrary closed contours (crossing, self-intersecting, overlapping) by a
    fill rule: the contours' edges are split at every intersection into bounded planar
    faces (OCCT BOPAlgo_Tools), each face is kept when the winding number of the ORIGINAL
    oriented contours at an interior point fills it, and the kept faces are united.
    The boundary curves stay the exact input curves (split, never refitted)."""
    from OCP.BOPAlgo import BOPAlgo_Tools
    loops = [pts for pts in (_wire_points(w, 32) for w in wires) if len(pts) >= 3]
    require(loops, 'No outline with area.')
    edges = plain_compound([e for w in wires for e in explore(w, TopAbs_EDGE)])
    joined, faces = TopoDS_Compound(), TopoDS_Compound()
    require(BOPAlgo_Tools.EdgesToWires_s(edges, joined, False, 1e-8) == 0, 'The outlines cannot be joined.')
    require(BOPAlgo_Tools.WiresToFaces_s(joined, faces, 1e-8), 'The outlines enclose no faces.')
    kept = []
    for face in explore(faces, TopAbs_FACE):
        n = _winding(_interior_point(face), loops)
        if (n != 0) if rule == 'nonzero' else (n % 2 == 1): kept.append(face)
    require(kept, 'The outlines enclose no filled face.')
    region = kept[0] if len(kept) == 1 else boolean_maker('union', kept[:1], kept[1:]).Shape()
    region = _unify(region)
    # Orient every face to +Z (the arrangement may return either side).
    oriented = []
    for face in explore(region, TopAbs_FACE):
        n = face_normal(face)
        oriented.append(face.Reversed() if n is not None and n.Z() < 0 else face)
    return oriented[0] if len(oriented) == 1 else plain_compound(oriented)


def region_by_nesting(wires, rule):
    """Fill of non-crossing contours by the SVG fill rule (nonzero or evenodd): each contour
    whose inside differs in fill from its surroundings is a boundary; filled boundaries
    become faces with their directly nested empty boundaries as holes."""
    loops = []
    for wire in wires:
        pts = _wire_points(wire)
        area = _signed_area(pts)
        if len(pts) >= 3 and abs(area) > 1e-12: loops.append({'wire': wire, 'pts': pts, 'area': area})
    require(loops, 'No outline with area.')
    for i, a in enumerate(loops):
        probe = a['pts'][0]
        a['containers'] = [j for j, b in enumerate(loops) if j != i and abs(b['area']) > abs(a['area']) and _inside(probe, b['pts'])]
    def filled(winding, depth):
        return winding != 0 if rule == 'nonzero' else depth % 2 == 1
    for i, a in enumerate(loops):
        winding = sum(1 if loops[j]['area'] > 0 else -1 for j in a['containers'])
        depth = len(a['containers'])
        a['outside'] = filled(winding, depth)
        a['inside'] = filled(winding + (1 if a['area'] > 0 else -1), depth + 1)
        a['boundary'] = a['inside'] != a['outside']
    faces = []
    for i, a in enumerate(loops):
        if not (a['boundary'] and a['inside']): continue
        holes = []
        for j, b in enumerate(loops):
            if not (b['boundary'] and not b['inside']) or i not in b['containers']: continue
            # Directly nested: no other boundary sits between a and b.
            between = [k for k in b['containers'] if k != i and loops[k]['boundary'] and i in loops[k]['containers']]
            if not between: holes.append(b['wire'])
        face = _loop_face(a['wire'])
        if holes:
            face = boolean_maker('difference', [face], [_loop_face(h) for h in holes]).Shape()
        faces.extend(explore(face, TopAbs_FACE))
    require(faces, 'The selected outlines enclose no filled face.')
    return faces[0] if len(faces) == 1 else plain_compound(faces)


def _valid(shape):
    return BRepCheck_Analyzer(shape).IsValid()


def plain_compound(shapes):
    """Compound of freshly built shapes (no shared inputs to protect)."""
    result = TopoDS_Compound(); builder = BRep_Builder(); builder.MakeCompound(result)
    for shape in shapes: builder.Add(result, shape)
    return result


def merge_regions(shapes):
    """Several planar regions (elements, glyphs) as one set of disjoint faces: faces
    whose boxes overlap are united (one Boolean per cluster), isolated faces stay."""
    faces = [face for shape in shapes for face in explore(shape, TopAbs_FACE)]
    require(faces, 'No filled face.')
    if len(faces) == 1: return faces[0]
    boxes = [bounds(face) for face in faces]
    parent = list(range(len(faces)))
    def root(i):
        while parent[i] != i: parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i in range(len(faces)):
        for j in range(i + 1, len(faces)):
            a, b = boxes[i], boxes[j]
            if all(a[k] <= b[k + 3] + 1e-7 and b[k] <= a[k + 3] + 1e-7 for k in range(2)): parent[root(i)] = root(j)
    clusters = {}
    for i in range(len(faces)): clusters.setdefault(root(i), []).append(faces[i])
    out = []
    for members in clusters.values():
        if len(members) == 1: out.append(members[0]); continue
        fused = boolean_maker('union', members[:1], members[1:]).Shape()
        try: fused = _unify(fused)
        except Exception: pass  # UnifySameDomain can refuse touching regions; the fuse stands.
        out.extend(explore(fused, TopAbs_FACE))
    return out[0] if len(out) == 1 else plain_compound(out)


# ---------------------------------------------------------------- relief solids

def relief(placed, depth, overlap, has_target):
    """Prisms of planar faces. `placed` is a list of (face_shape, normal gp_Dir, inset):
    each face is swept along its normal from -(overlap+inset) to +depth (emboss) or
    from depth to +(overlap+inset) (deboss). Returns (solids, named roles, report)."""
    require(abs(depth) >= 1e-6, 'The relief depth must not be zero.')
    solids, top, bottom = [], [], []
    for face, normal, inset in placed:
        under = (overlap if has_target else 0.) + inset
        start, length = (-under, depth + under) if depth > 0 else (depth, -depth + under)
        n = gp_Vec(normal)
        # Sweep a face whose normal points along the sweep, so every prism has positive
        # volume (a mirrored arc glyph or an inward cylinder glyph faces the other way).
        facing = face_normal(next(explore(face, TopAbs_FACE)))
        if facing is not None and facing.Dot(normal) < 0: face = face.Reversed()
        moved = face
        if abs(start) > 0:
            t = gp_Trsf(); t.SetTranslation(n.Multiplied(start)); moved = BRepBuilderAPI_Transform(face, t, True).Shape()
        prism = BRepPrimAPI_MakePrism(moved, n.Multiplied(length))
        require(prism.IsDone(), 'Relief extrusion failed.')
        solids.append(prism.Shape())
        top.extend(explore(prism.LastShape(), TopAbs_FACE)); bottom.extend(explore(prism.FirstShape(), TopAbs_FACE))
    shape = solids[0] if len(solids) == 1 else plain_compound(solids)
    # A compound of prisms keeps the prisms' own faces, so the caps are found by identity.
    roles = {'top' if depth > 0 else 'opening': top, 'bottom' if depth > 0 else 'floor': bottom}
    return shape, roles


def trace_roles(shape, fid, operator, roles):
    import topology
    result = []
    for face in topology.face_list(shape):
        role = next((name for name, members in roles.items() if any(face.IsSame(m) for m in members)), 'wall')
        result.append({'shape': face, 'origins': [topology.origin(fid, operator, role)]})
    return result


# ---------------------------------------------------------------- text

def _font_file(spec):
    for path in spec.get('files', []):
        if path and os.path.isfile(path):
            require(_sha256(path) == spec['sha256'],
                    'Font file %s differs from the compiled state; regenerate the plan.' % spec['family'], 'INTEGRITY_FAILURE')
            return path
    raise GeometryError('OUT_OF_SCOPE', 'Font %s (%s) is not available in the worker.' % (spec.get('family'), spec.get('style')))


def _open_font(path, size):
    from OCP.StdPrs import StdPrs_BRepFont
    from OCP.NCollection import NCollection_Utf8String
    font = StdPrs_BRepFont()
    require(font.Init(NCollection_Utf8String(path), size, 0), 'The font could not be loaded.')
    return font


def _glyph_region(font, ch, report):
    shape = font.RenderGlyph(ch)
    if shape.IsNull(): return None
    if _valid(shape): return shape
    # Fonts built from variable masters (Inter) keep overlapping contours; OCCT then makes
    # a self-overlapping face. The nonzero fill of the same exact contours repairs it.
    wires = [w for face in explore(shape, TopAbs_FACE) for w in explore(face, TopAbs_WIRE)]
    region = region_by_arrangement(wires, 'nonzero')
    require(_valid(region), 'Glyph %r does not yield a valid face.' % ch)
    report['glyphs_repaired_overlap'] = report.get('glyphs_repaired_overlap', 0) + 1
    return region


def _moved(shape, transform):
    return BRepBuilderAPI_Transform(shape, transform, True).Shape()


def _frame(origin, x_axis, y_axis):
    """gp_Trsf mapping the glyph plane (x right, y up) onto origin + x*X + y*Y (+ X x Y)."""
    z = (x_axis[1]*y_axis[2]-x_axis[2]*y_axis[1], x_axis[2]*y_axis[0]-x_axis[0]*y_axis[2], x_axis[0]*y_axis[1]-x_axis[1]*y_axis[0])
    t = gp_Trsf()
    t.SetValues(x_axis[0], y_axis[0], z[0], origin[0], x_axis[1], y_axis[1], z[1], origin[1], x_axis[2], y_axis[2], z[2], origin[2])
    return t


def text_faces(f, report):
    """[(placed face, normal, inset)] of every glyph in construction coordinates."""
    c, p = f['construction'], f['values']
    path = _font_file(f['font'])
    cap = p['cap_height']
    probe = _open_font(path, 100.0)
    h = bounds(probe.RenderGlyph('H'))
    require(h and h[4] > 0, 'The font provides no cap height.')
    size = 100.0 * cap / h[4]
    font = _open_font(path, size)
    tracking = p.get('tracking', 0.)
    spacing = p.get('line_spacing', font.LineSpacing())
    lines = c['string'].split('\n')
    arc = c.get('arc')
    require(not arc or len(lines) == 1, 'Arc text is single-line.')
    valign = c.get('valign', 'baseline')
    dy = {'baseline': 0., 'center': -cap / 2, 'top': -cap}[valign]
    align = c.get('align', 'left')
    glyphs, missing = [], []
    for row, line in enumerate(lines):
        pen, items = 0., []
        for i, ch in enumerate(line):
            nxt = line[i + 1] if i + 1 < len(line) else ' '
            advance = font.AdvanceX(ch, nxt)
            region = None if ch.isspace() else _glyph_region(font, ch, report)
            if region is None and not ch.isspace(): missing.append(ch)
            items.append((ch, region, pen, advance))
            pen += advance + tracking
        width = pen - tracking if line else 0.
        shift = {'left': 0., 'center': -width / 2, 'right': -width}[align]
        for ch, region, x, advance in items:
            if region is not None: glyphs.append((region, x + shift, advance, -row * spacing + dy))
    require(len(glyphs) <= MAX_GLYPHS, 'Too many glyphs in one text feature.', 'BUDGET_EXCEEDED')
    require(glyphs, 'The text contains no renderable glyph.')
    if missing: report['glyphs_skipped_missing'] = sorted(set(missing))
    report.update(font=f['font']['family'], style=f['font']['style'], font_size_em_mm=size, cap_height_mm=cap, glyphs=len(glyphs))
    z = gp_Dir(0, 0, 1)
    if not arc:
        placed = []
        for region, x, advance, y in glyphs:
            t = gp_Trsf(); t.SetTranslation(gp_Vec(x, y, 0.)); placed.append((_moved(region, t), z, 0.))
        return placed
    radius = float(arc['radius']); center = math.radians(float(arc['center_angle_deg']))
    require(radius > 0, 'The arc radius must be positive.')
    side = arc.get('side', 'outside'); surface = arc.get('surface', 'plane')
    direction = arc.get('direction') or (('cw' if side == 'outside' else 'ccw') if surface == 'plane' else ('ccw' if side == 'outside' else 'cw'))
    sense = 1. if direction == 'ccw' else -1.
    placed, sagitta = [], 0.
    for region, x, advance, y in glyphs:
        s = x + advance / 2            # arc length of the glyph's advance centre from the anchor
        theta = center + sense * s / radius
        er = (math.cos(theta), math.sin(theta), 0.)
        tangent = (-sense * math.sin(theta), sense * math.cos(theta), 0.)
        local = gp_Trsf(); local.SetTranslation(gp_Vec(-advance / 2, 0., 0.))
        g = _moved(region, local)
        if surface == 'plane':
            up = er if side == 'outside' else (-er[0], -er[1], 0.)
            origin = (radius * er[0] + y * up[0], radius * er[1] + y * up[1], 0.)
            placed.append((_moved(g, _frame(origin, tangent, up)), z, 0.))
        else:
            # Wrapped round the local Z axis: glyph x along the circumference, y along +Z,
            # extruded radially. Each glyph lies flat on its tangent plane; the inset below
            # covers the gap between that plane and the cylinder under the glyph's edges.
            b = bounds(g); half = max(abs(b[0]), abs(b[3]))
            inset = half * half / (2 * radius)
            sagitta = max(sagitta, inset)
            normal = er if side == 'outside' else (-er[0], -er[1], 0.)
            origin = (radius * er[0], radius * er[1], y)
            placed.append((_moved(g, _frame(origin, tangent, (0., 0., 1.))), gp_Dir(*normal), inset))
    if surface == 'cylinder':
        report['wrap_method'] = 'tangent_plane_per_glyph'; report['max_sagitta_mm'] = sagitta
    report['arc'] = {'radius_mm': radius, 'direction': direction, 'side': side, 'surface': surface}
    return placed


# ---------------------------------------------------------------- SVG emblem

_UNITS = {'mm': 1., 'cm': 10., 'in': 25.4, 'pt': 25.4 / 72, 'pc': 25.4 / 6, 'px': 25.4 / 96, 'q': 0.25}
_SKIP = {'defs', 'clipPath', 'mask', 'symbol', 'marker', 'pattern', 'style', 'script', 'title', 'desc', 'metadata', 'linearGradient', 'radialGradient', 'filter', 'text'}


def _tag(el):
    return el.tag.split('}', 1)[-1] if isinstance(el.tag, str) else ''


def _floats(text):
    return [float(x) for x in re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', text)]


def _parse_transform(text):
    from fontTools.misc.transform import Transform
    result = Transform()
    for name, args in re.findall(r'(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)', text or ''):
        v = _floats(args)
        if name == 'matrix' and len(v) == 6: t = Transform(*v)
        elif name == 'translate': t = Transform().translate(v[0], v[1] if len(v) > 1 else 0.)
        elif name == 'scale': t = Transform().scale(v[0], v[1] if len(v) > 1 else v[0])
        elif name == 'rotate':
            t = Transform()
            if len(v) == 3: t = t.translate(v[1], v[2]).rotate(math.radians(v[0])).translate(-v[1], -v[2])
            else: t = t.rotate(math.radians(v[0]))
        elif name == 'skewX': t = Transform().skew(math.radians(v[0]), 0)
        elif name == 'skewY': t = Transform().skew(0, math.radians(v[0]))
        else: raise GeometryError('INVALID_SCHEMA', 'Invalid SVG transform.')
        result = result.transform(t)
    return result


def _style(el):
    values = dict(el.attrib)
    for item in (el.attrib.get('style') or '').split(';'):
        if ':' in item:
            k, v = item.split(':', 1); values[k.strip()] = v.strip()
    return values


def _colour(value):
    v = (value or '').strip().lower()
    named = {'white': '#ffffff', 'black': '#000000', 'red': '#ff0000', 'none': 'none'}
    v = named.get(v, v)
    if re.fullmatch(r'#[0-9a-f]{3}', v): v = '#' + ''.join(ch * 2 for ch in v[1:])
    return v


class _Recorder:
    """fontTools pen capturing contours as segment lists in SVG user units."""
    def __init__(self):
        from fontTools.pens.basePen import BasePen
        outer = self
        class Pen(BasePen):
            def _moveTo(self, pt): outer.contours.append([('M', pt)])
            def _lineTo(self, pt): outer.contours[-1].append(('L', pt))
            def _curveToOne(self, a, b, c): outer.contours[-1].append(('C', a, b, c))
            def _qCurveToOne(self, a, b): outer.contours[-1].append(('Q', a, b))
            def _closePath(self): pass
            def _endPath(self): pass
        self.contours = []
        self.pen = Pen(None)


def svg_elements(text, element_filter):
    """Filled elements of the document as contour lists (after their transforms)."""
    from fontTools.svgLib.path import parse_path
    from fontTools.svgLib.path.shapes import PathBuilder
    from fontTools.pens.transformPen import TransformPen
    require(len(text) <= 1024 * 1024, 'The SVG file is too large (at most 1 MiB).', 'BUDGET_EXCEEDED')
    require('<!ENTITY' not in text, 'SVG with entity declarations is not read.', 'OUT_OF_SCOPE')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise GeometryError('INVALID_SCHEMA', 'The SVG is not valid XML: %s' % e)
    require(_tag(root) == 'svg', 'The root element is not <svg>.', 'INVALID_SCHEMA')
    flt = element_filter or {}
    ids, classes, fills = set(flt.get('ids') or []), set(flt.get('classes') or []), {_colour(x) for x in flt.get('fills') or []}
    exclude = set(flt.get('exclude_ids') or [])
    found, skipped = [], {}
    def walk(el, transform, fill, rule, cls, chain):
        tag = _tag(el)
        if tag in _SKIP: return
        st = _style(el)
        if st.get('display') == 'none' or st.get('visibility') == 'hidden': return
        if 'transform' in el.attrib: transform = transform.transform(_parse_transform(el.attrib['transform']))
        fill = _colour(st['fill']) if 'fill' in st else fill
        rule = st.get('fill-rule', rule)
        cls = cls | set((el.attrib.get('class') or '').split())
        chain = chain + ([el.attrib['id']] if 'id' in el.attrib else [])
        if tag in ('path', 'polygon', 'polyline', 'rect', 'circle', 'ellipse'):
            if fill == 'none' or tag == 'line':
                skipped['unfilled'] = skipped.get('unfilled', 0) + 1; return
            if ids and not ids.intersection(chain): return
            if exclude.intersection(chain): return
            if classes and not classes.intersection(cls): return
            if fills and fill not in fills: return
            builder = PathBuilder()
            if not builder.add_path_from_element(el) or not builder.paths[-1]: return
            local = transform.transform(builder.transforms[-1]) if builder.transforms[-1] is not None else transform
            rec = _Recorder()
            parse_path(builder.paths[-1], TransformPen(rec.pen, tuple(local)))
            found.append({'tag': tag, 'id': chain[-1] if chain else None, 'fill': fill, 'rule': rule, 'contours': [k for k in rec.contours if len(k) > 1]})
            return
        if tag == 'use':
            skipped['use_elements'] = skipped.get('use_elements', 0) + 1
        for child in el: walk(child, transform, fill, rule, cls, chain)
    from fontTools.misc.transform import Transform
    walk(root, Transform(), '#000000', 'nonzero', set(), [])
    return root, found, skipped


def _document_box(root):
    view = _floats(root.attrib.get('viewBox', ''))
    box = view if len(view) == 4 and view[2] > 0 and view[3] > 0 else None
    size = {}
    for key in ('width', 'height'):
        m = re.fullmatch(r'\s*([-+]?(?:\d+\.?\d*|\.\d+))\s*([a-zA-Z]*)\s*', root.attrib.get(key, ''))
        if m: size[key] = (float(m.group(1)), m.group(2).lower() or 'px')
    if box is None and 'width' in size and 'height' in size: box = [0., 0., size['width'][0], size['height'][0]]
    return box, size


def emblem_faces(f, report):
    c, p = f['construction'], f['values']
    if f.get('svg_text') is not None:
        text = f['svg_text']
        require(hashlib.sha256(text.encode('utf-8')).hexdigest() == f.get('svg_sha256'), 'The SVG source does not match the compiled state.', 'INTEGRITY_FAILURE')
    else:
        path = 'input-' + c['artifact_id'] + '.svg'
        require(os.path.isfile(path), 'The SVG artifact is missing from the job.', 'INTEGRITY_FAILURE')
        with open(path, 'rb') as h: text = h.read().decode('utf-8', 'replace')
    root, elements, skipped = svg_elements(text, c.get('element_filter'))
    require(elements, 'No filled SVG element matches element_filter.', 'AMBIGUOUS_SELECTION')
    contours = sum(len(e['contours']) for e in elements)
    require(contours <= MAX_CONTOURS, 'The SVG has too many outlines.', 'BUDGET_EXCEEDED')
    box, size = _document_box(root)
    fit = c.get('fit_to', 'viewbox' if box else 'selection')
    if fit == 'selection' or box is None:
        xs = [pt[0] for e in elements for k in e['contours'] for seg in k for pt in seg[1:]]
        ys = [pt[1] for e in elements for k in e['contours'] for seg in k for pt in seg[1:]]
        box = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
        fit = 'selection'
    require(box[2] > 0 and box[3] > 0, 'The SVG reference frame has no extent.')
    if c.get('width_mm') is not None: scale = float(c['width_mm']) / box[2]
    elif c.get('height_mm') is not None: scale = float(c['height_mm']) / box[3]
    else:
        require('width' in size and size['width'][1] in _UNITS and fit == 'viewbox',
                'svg_emblem needs width_mm or height_mm (the file gives no physical width).', 'INVALID_SCHEMA')
        scale = size['width'][0] * _UNITS[size['width'][1]] / box[2]
    anchor = c.get('anchor', 'center')
    ax = {'center': box[0] + box[2] / 2, 'bottom_left': box[0], 'bottom_center': box[0] + box[2] / 2, 'top_left': box[0], 'svg_origin': 0.}[anchor]
    ay = {'center': box[1] + box[3] / 2, 'bottom_left': box[1] + box[3], 'bottom_center': box[1] + box[3], 'top_left': box[1], 'svg_origin': 0.}[anchor]
    def mm(pt):
        # SVG y points down; the emblem is read from +Z with y up.
        return gp_Pnt((pt[0] - ax) * scale, -(pt[1] - ay) * scale, 0.)
    from OCP.Geom import Geom_BezierCurve
    regions = []
    for element in elements:
        wires = []
        for k in element['contours']:
            start = k[0][1]; current = start; maker = BRepBuilderAPI_MakeWire(); count = 0
            segments = list(k[1:])
            if math.dist(segments[-1][-1], start) * scale > 1e-9: segments.append(('L', start))
            for seg in segments:
                end = seg[-1]
                if seg[0] == 'L':
                    if math.dist(current, end) * scale <= 1e-9: continue
                    edge = BRepBuilderAPI_MakeEdge(mm(current), mm(end))
                else:
                    poles = [current, *seg[1:]]
                    if all(math.dist(a, b) * scale <= 1e-9 for a, b in zip(poles, poles[1:])): continue
                    array = TColgp_Array1OfPnt(1, len(poles))
                    for i, q in enumerate(poles, 1): array.SetValue(i, mm(q))
                    edge = BRepBuilderAPI_MakeEdge(Geom_BezierCurve(array))
                require(edge.IsDone(), 'The SVG segment does not yield an edge.')
                maker.Add(edge.Edge()); count += 1; current = end
            if count and maker.IsDone(): wires.append(maker.Wire())
        if not wires: continue
        region = region_by_nesting(wires, element['rule'])
        if not _valid(region):
            region = region_by_arrangement(wires, element['rule']); report['elements_filled_by_arrangement'] = report.get('elements_filled_by_arrangement', 0) + 1
        regions.append(region)
    require(regions, 'The selected SVG elements enclose no face.')
    region = merge_regions(regions)
    require(_valid(region), 'The SVG emblem does not yield a valid face.')
    faces = list(explore(region, TopAbs_FACE))
    b = bounds(region)
    report.update(elements=len(elements), contours=contours, faces_2d=len(faces), fit_to=fit, anchor=anchor,
                  scale_mm_per_unit=scale, size_mm=[b[3] - b[0], b[4] - b[1]], fills=sorted({e['fill'] for e in elements}))
    if skipped: report['elements_skipped_unsupported'] = skipped
    return [(face, gp_Dir(0, 0, 1), 0.) for face in faces]


def build(f, has_target):
    """(relief solid, named roles, report) of a text or svg_emblem feature."""
    report = {}
    placed = text_faces(f, report) if f['construction']['operator'] == 'text' else emblem_faces(f, report)
    p = f['values']
    depth = p['depth']; overlap = p.get('overlap', 0.01 if has_target else 0.)
    shape, roles = relief(placed, depth, overlap, has_target)
    report.update(depth_mm=depth, mode='emboss' if depth > 0 else 'deboss', overlap_mm=overlap if has_target else 0.)
    return shape, roles, report
