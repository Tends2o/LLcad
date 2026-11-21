"""Place dimension texts and item numbers in the fully packed sheet with the drawing primitives of the renderer."""
import math

import numpy as np
from shapely.geometry import LineString, box
from shapely.strtree import STRtree

from sheet import Sheet
import checks


ARC_OK_REACH_MM = 12.0      # an arc that is free in the checker's view counts as a choice only up to this far (mm radius) from the starting radius; radii pulled further are no longer reliable in the final sheet (probe <-> final sheet)
TITLE_RISE_MAX = 60      # mm: highest rise of the title above its base (label_dy)
_OPPOSITE = {'above': 'below', 'below': 'above', 'left': 'right', 'right': 'left'}


def _positions(a, v, items):
    """Position fields only. Datum point, dimension direction, numeric value and assigned features are kept."""
    kind = a['type']
    x0, y0, x1, y1 = v['geom_bbox']
    span = math.hypot(x1 - x0, y1 - y0)
    if kind in ('leader', 'balloon'):
        for length in (10, 15, 22, 30, 42, 60, 80):
            for angle in (30, 150, -30, -150, 45, 135, -45, -135, 60, 120, -60, -120, 15, 165, -15, -165, 90, -90):
                t = math.radians(angle)
                yield {'to': [round(length * math.cos(t), 3), round(length * math.sin(t), 3)]}
    elif kind == 'angle':
        # The extension of the arc keeps the text at the dimensioned angle.
        for r in sorted(range(6, max(16, math.ceil(span + 15)), 3), key=lambda r: abs(r-a.get('radius', 12))):
            yield {'radius': r, 'arc': 'fixed'} if a.get('arc') in ('free', 'fixed') else {'radius': r}
        if a.get('arc') in ('free', 'fixed'):      # if every radius lies on a line (text on the bisector, e.g. a section line through the centre), the text moves sideways away from the arc
            for sh_ in (3, -3, 6, -6, 9, -9, 12, -12):
                for r in sorted(range(6, max(16, math.ceil(span + 15)), 3), key=lambda r: abs(r - a.get('radius', 12))):
                    yield {'radius': r, 'arc': 'fixed', 'text_shift': sh_}
    elif kind in ('section_line', 'detail_circle', 'view_arrow'):
        for dist in (2, 4, 6, 9, 12, 15):
            for dx, dy in ((0,1), (1,0), (-1,0), (0,-1), (1,1), (-1,1), (1,-1), (-1,-1)):
                yield {'label_shift': [dist*dx, dist*dy]}
    elif kind == 'radius':
        # Both ends of the angle range still belong to the modelled arc.
        half = min(180., float(a.get('span', 360)) / 2)
        angle = float(a.get('angle', 45))
        turns = [0.] + [d * t for t in range(10, math.ceil(half), 10) for d in (1, -1)]
        for length in (4, 7, 10, 15, 22, 30, 42, 60):
            for turn in turns:
                yield {'angle': (angle + turn + 180) % 360 - 180, 'text_out': length}
    elif kind == 'diameter' and a.get('axis') is not None and abs(np.dot(a['axis'], v['frame'][0])) < 0.99:
        # An axis-parallel Ø dimension has two surface points and behaves like a length dimension.
        line = next((it for it in items if it.get('role') == 'dim' and 'offset' in it), None)
        if line:
            p, q = np.asarray(line['pts'])[[0, -1]]
            normal = np.array([p[1]-q[1], q[0]-p[0]])
            if np.linalg.norm(normal) < 1e-9:
                return
            normal /= np.linalg.norm(normal)
            corners = np.array([[x,y] for x in (x0,x1) for y in (y0,y1)])
            distances = (corners-p) @ normal + line['offset']
            sign = -np.sign(a['half']) if a.get('half') else 1
            for gap in (7, 10, 14, 21, 28, 35):
                for offset in sorted((distances.min()-gap, distances.max()+gap), key=abs):
                    yield {'offset': round(float(sign * offset), 3)}
            for gap in (7, 10, 14):      # if there is no room outside the view (neighbouring block, title), a dimension line close to the element (like the offsets of autofix)
                for offset in (gap, -gap):
                    yield {'offset': round(float(sign * offset), 3)}
    elif kind == 'diameter':
        for length in (0, 8, 14, 22, 32, 45):
            for deg in (45, 135, 225, 315, 30, 150, 210, 330, 60, 120, 240, 300, 0, 180, 90, 270):
                yield {'angle': deg, 'leader': length}
    elif kind == 'linear':
        line = next((i for i in items if i.get('role') == 'dim'), None)
        if line:
            # Stay inside the dimension line, except for short dimensions with the figure outside.
            length = LineString(line['pts']).length
            for step in range(3, min(30, int(length / 2)) + 1, 3):
                for sign in (1, -1):
                    yield {'text_shift': sign * step}
            p = np.asarray(line['pts'][0])
            if length > 1e-6 and 'offset' in line and a.get('axis') in ('h', 'v'):
                u = np.array([1., 0.]) if a['axis'] == 'h' else np.array([0., 1.])
                normal = np.array([-u[1], u[0]])
                bounds = (np.array([[x,y] for x in (x0,x1) for y in (y0,y1)]) - p) @ normal + line['offset']
                for gap in (7, 10, 14, 21, 28, 35):
                    for offset in sorted((bounds.min()-gap, bounds.max()+gap), key=lambda x: abs(x-line['offset'])):
                        yield {'offset': round(float(offset), 3)}


def _arc_owners(items, view_lines, facade):
    """Owners of angle arcs that the checker (checks.angle_arc_findings, the same function) reports on this inventory. facade: a real Sheet (draw=False) whose items are set."""
    facade.items = list(items)
    return {f.get('owner') for f in checks.angle_arc_findings(facade, 0, view_lines)}


def owner_counts(items, facade, views, view_lines, strict):
    """(block, warn) per owner in the verdict of the checker on this inventory - checks.sheet_findings, the same function as in the render (including text_on_line, text_on_geometry,
    text_overlap and angle_arc_findings). A finding counts for every owner involved (owner, other, owners). facade: a real Sheet (draw=False) with area, blocks and
    label letters of the sheet; views {id: {'bbox'}} as in the render."""
    facade.items = list(items)
    out = {}
    for f in checks.sheet_findings(facade, 0, views, view_lines, strict):
        sev = f.get('severity')
        if sev not in ('block', 'warn'):
            continue
        for o in dict.fromkeys([f.get('owner'), f.get('other'), *(f.get('owners') or ())]):
            if o:
                b, w = out.get(o, (0, 0))
                out[o] = (b + 1, w) if sev == 'block' else (b, w + 1)
    return out


def worse(before, after):
    """Worsening of an owner, lexicographic by (block, warn): one block outweighs any number of warn. A move that removes a block is never worse."""
    return tuple(after) > tuple(before)


def _arc_ok(own_items, other_items, view_lines, facade, owner):
    """Angle arc of a candidate free in the checker's sense (other owners + candidate + body edges of the views)."""
    return owner not in _arc_owners(list(other_items) + list(own_items), view_lines, facade)


def _shape(item):
    if item['kind'] == 'line':
        return LineString(item['pts'])
    return box(*item['bbox'])


LETTER_TYPES = ('section_line', 'detail_circle', 'view_arrow')      # annotations with label letters (checks: letter_owners)


def _clear(items, bounds, blocks, edges, lines, texts, letter=False):
    """letter: the text is a label letter - free then also means: no foreign dimension/leader line in its check zone checks.letter_zone (the same zone as
    section_letter_on_line in the checker)."""
    for item in items:
        g = _shape(item)
        if not bounds.covers(g) or blocks.query(g, predicate='intersects').size:
            return False
        if texts.query(g, predicate='intersects').size:
            return False
        if item['kind'] == 'text':
            ink = g.buffer(0.45, join_style='mitre')
            if edges.query(ink, predicate='intersects').size or lines.query(ink, predicate='intersects').size:
                return False
            if letter and lines.query(checks.letter_zone(item['bbox']), predicate='intersects').size:
                return False
    return True


def place(dr, sd, sh, views, findings):
    """Try only affected annotations. Every accepted position is checked against the already updated sheet occupancy."""
    anns = sd.get('annotations') or []
    indices, captions = set(), set()
    for f in findings:
        if f['code'] == 'section_letter_on_line':      # the label letter moves (label_shift), the foreign line stays
            owner = f.get('owner')
            if owner and owner.startswith('ann:') and owner[4:].isdigit():
                indices.add(int(owner[4:]))
        if f['code'] in ('text_on_geometry', 'text_on_line', 'text_overlap'):
            for owner in [f.get('owner'), *(f.get('owners') or ()), f.get('other')]:
                if owner and owner.startswith('ann:') and owner[4:].isdigit():
                    indices.add(int(owner[4:]))
                if owner and owner.startswith('view:') and owner[5:] in views:
                    captions.add(owner)
    if not indices and not captions:
        return False
    bounds = box(*sh.area)
    edges = STRtree([line for v in views.values() for line in v['lines']])
    tmp = Sheet(sh.format, sh.orientation, draw=False)
    chk = Sheet(sh.format, sh.orientation, draw=False)      # facade for the checker (angle_arc_findings reads only .items)
    view_lines = [(vid, g) for vid, u in views.items() for g in u['lines']]
    moved = {}
    occupied = {}
    for it in sh.items:
        occupied.setdefault(it['owner'], []).append(it)
    changed = False      # title and side moves; moves on annotations are in moved (a reverted move is no change)
    for owner in sorted(captions):
        v = views[owner[5:]]
        current = occupied.get(owner, [])
        if len(current) != 1 or current[0]['kind'] != 'text':
            continue
        other = [it for key, group in occupied.items() if key != owner for it in group]
        lines = STRtree([_shape(it) for it in other if it['kind'] == 'line'])
        texts = STRtree([_shape(it).buffer(.45) for it in other if it['kind'] == 'text'])
        blocks = STRtree([box(*b) for b in sh.blocks.values()] + [box(*u['bbox']) for u in views.values() if u is not v])
        limit = max(8, min(40, int((v['geom_bbox'][2]-v['geom_bbox'][0])/2)))
        old = float(v['vd'].get('label_dx', 0))
        cleared = False
        for shift in [0] + [sign*i for i in range(2,limit+1,2) for sign in (1,-1)]:
            trial = {**current[0], 'bbox': (np.asarray(current[0]['bbox']) + [shift,0,shift,0]).tolist()}
            if abs(old+shift) <= limit and _clear([trial],bounds,blocks,edges,lines,texts):
                if shift:
                    v['vd']['label_dx'] = old+shift
                    occupied[owner] = [trial]
                    changed = True
                cleared = True
                break
        if cleared:
            continue
        # The title does not give up as long as the interfering dimension line can change sides (like the autofix move
        # text_on_line -> side). The title stays, the dimension line yields; if that does not help, everything stays as it was.
        for f in findings:
            if f['code'] not in ('text_on_geometry', 'text_on_line', 'text_overlap'):
                continue
            group = [f.get('owner'), f.get('other'), *(f.get('owners') or ())]
            if owner not in group:
                continue
            for o in dict.fromkeys(group):
                if not (o and o.startswith('ann:') and o[4:].isdigit() and int(o[4:]) < len(anns) and o in occupied):
                    continue
                a = anns[int(o[4:])]
                av = views.get(a.get('view'))
                side = a.get('side') or {'h': 'above', 'v': 'left'}.get(a.get('axis'), 'left')
                if a['type'] != 'linear' or not av or 'offset' in a or side not in _OPPOSITE:
                    continue
                others = [it for key, group_ in occupied.items() if key != o for it in group_]
                a_lines = STRtree([_shape(it) for it in others if it['kind'] == 'line'])
                a_texts = STRtree([_shape(it).buffer(0.45, join_style='mitre') for it in others if it['kind'] == 'text'])
                a_blocks = STRtree([box(*b) for b in sh.blocks.values()] +
                                   [box(*u['bbox']) for u in views.values() if u is not av])
                tmp.items = []
                tmp.owner = o
                dr.annotate(tmp, {**a, 'side': _OPPOSITE[side]}, views, {'dims_parts': set(), 'balloons': set()})
                if _clear(tmp.items, bounds, a_blocks, edges, a_lines, a_texts):
                    a['side'] = _OPPOSITE[side]
                    occupied[o] = tmp.items
                    changed = True
        # Title above the dimension lines of the block. If after the sideways move and side change of the dimension line it still stands on lines/text/edge (profile detail view: the
        # dimension lines above the view run through the row of the title), it rises (label_dy) in steps of 2 mm to the first free position; the same free check (_clear) as above.
        current = occupied.get(owner, [])
        if len(current) == 1 and current[0]['kind'] == 'text':
            other = [it for key, group in occupied.items() if key != owner for it in group]
            lines = STRtree([_shape(it) for it in other if it['kind'] == 'line'])
            texts = STRtree([_shape(it).buffer(.45) for it in other if it['kind'] == 'text'])
            blocks = STRtree([box(*b) for b in sh.blocks.values()] + [box(*u['bbox']) for u in views.values() if u is not v])
            if not _clear(current, bounds, blocks, edges, lines, texts):
                for rise in range(2, TITLE_RISE_MAX + 1, 2):
                    trial = {**current[0], 'bbox': (np.asarray(current[0]['bbox']) + [0, rise, 0, rise]).tolist()}
                    if _clear([trial], bounds, blocks, edges, lines, texts):
                        v['vd']['label_dy'] = float(v['vd'].get('label_dy', 0.0)) + rise
                        occupied[owner] = [trial]
                        changed = True
                        break
    for i in sorted(indices):
        owner = f'ann:{i}'
        if i >= len(anns) or owner not in occupied:
            continue
        a = anns[i]
        v = views.get(a.get('view'))
        if not v or a['type'] not in ('linear', 'angle', 'leader', 'balloon', 'diameter', 'radius',
                                      'section_line', 'detail_circle', 'view_arrow'):
            continue
        current = occupied[owner]
        dim = next((it for it in current if it.get('role') == 'dim' and 'offset' in it), None)
        other = [it for key, group in occupied.items() if key != owner for it in group]
        lines = STRtree([_shape(it) for it in other if it['kind'] == 'line'])
        texts = STRtree([_shape(it).buffer(0.45, join_style='mitre') for it in other if it['kind'] == 'text'])
        blocks = STRtree([box(*b) for b in sh.blocks.values()] +
                         [box(*u['bbox']) for u in views.values() if u is not v])
        letter = a['type'] in LETTER_TYPES
        if letter:      # as the checker: only dimension/leader lines of other annotations (not title block, revision block, parts list)
            lines = STRtree([_shape(it) for key, group in occupied.items() if key != owner and key not in checks.BLOCKS for it in group if it['kind'] == 'line'])
        mine = (lambda its: [it for it in its if it['kind'] == 'text']) if letter else (lambda its: its)      # label letter: move only the letters, the line stays where it is
        if _clear(mine(current), bounds, blocks, edges, lines, texts, letter):
            continue
        fallback = None
        old_state = ({k: a[k] for k in ('radius', 'arc', 'text_shift') if k in a}, current)
        for patch in _positions(a, v, current):
            trial = {**a, **patch}
            if 'angle' in patch and a.get('radius') and (a['type'] == 'radius' or trial.get('leader')):
                # The datum tip of an outer dimension must also lie on the visible circle after rotating.
                t = math.radians(trial['angle'])
                c = np.array([np.dot(a['center'], u) for u in v['frame'][1:]])
                q = c + a['radius'] * np.array([math.cos(t), math.sin(t)])
                if not dr.visible_points(v['vd'], sd['scale'], [q])[0]:
                    continue
            if a['type'] == 'linear' and dim and 'offset' not in patch:
                trial['offset'] = dim['offset']   # probe in the same row, do not touch the counter of the next dimension
            tmp.items = []
            tmp.owner = owner
            dr.annotate(tmp, trial, views, {'dims_parts': set(), 'balloons': set()})
            if _clear(mine(tmp.items), bounds, blocks, edges, lines, texts, letter):
                if a['type'] == 'angle' and a.get('arc') in ('free', 'fixed'):
                    # Text-free before checker-free before closeness to the starting radius. The candidates come by closeness; the first checker-free position (checks.angle_arc_findings) wins, unless it lies further
                    # than ARC_OK_REACH_MM from the starting radius, otherwise the first text-free one (fallback). The counter-check on the finished inventory follows after the loop.
                    near = abs(float(patch.get('radius', a.get('radius', 12))) - float(a.get('radius', 12))) <= ARC_OK_REACH_MM
                    if not (near and _arc_ok(tmp.items, other, view_lines, chk, owner)):
                        fallback = fallback or (patch, tmp.items)
                        continue
                a.update(patch)
                occupied[owner] = tmp.items
                moved.setdefault(owner, (a, old_state))
                fallback = None
                break
        else:
            if fallback:
                a.update(fallback[0])
                occupied[owner] = fallback[1]
                moved.setdefault(owner, (a, old_state))
    reverted = set()
    angles = {owner: m for owner, m in moved.items() if m[0].get('type') == 'angle'}
    if angles:
        # Counter-check of the angle moves on the inventory after all moves with the overall verdict of the checker (checks.sheet_findings, as in the render) per owner, before against after
        # lexicographic by (block, warn). Only a worse move is reverted to radius, arc and text_shift before it; a fallback that removes a text finding (block) and brings
        # an arc finding (warn) in return stays.
        fin = Sheet(sh.format, sh.orientation, draw=False)
        fin.area, fin.blocks, fin.letter_owners = sh.area, dict(sh.blocks), set(getattr(sh, 'letter_owners', ()))
        boxes = {vid: {'bbox': u['bbox']} for vid, u in views.items()}
        strict = bool((getattr(dr, 'spec', None) or {}).get('strict'))
        before = owner_counts(sh.items, fin, boxes, view_lines, strict)
        after = owner_counts([it for group in occupied.values() for it in group], fin, boxes, view_lines, strict)
        for owner, (ann, (old, old_items)) in angles.items():
            if worse(before.get(owner, (0, 0)), after.get(owner, (0, 0))):
                for k in ('radius', 'arc', 'text_shift'):
                    if k in old:
                        ann[k] = old[k]
                    else:
                        ann.pop(k, None)
                occupied[owner] = old_items
                reverted.add(owner)
    tmp.close()
    return changed or any(owner not in reverted for owner in moved)
