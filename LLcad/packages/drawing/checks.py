"""Automatic findings for a rendered drawing set. Severity 'block' prevents publication,
'warn' is a hint for the visual inspection."""
import math
import re
from collections import defaultdict
import numpy as np
from shapely.geometry import box as sbox, LineString, Point
from shapely.ops import unary_union
from shapely.strtree import STRtree
from sheet import FORMATS, usable_area, fmt, sheet_size
import plan_roles
from rules import GRID_LONELY_TARGET, MIN_AXIS_MM, PATTERN_MIN_MM as _PATTERN_MIN_MM, PROJ_TOL_MM, ZONE_OVERLAP_MM        # thresholds of the rule book are defined once (rules.py)

BLOCKS = ('title_block', 'change_block', 'parts_list')
ORDER = ['A4', 'A3', 'A2', 'A1', 'A0']
MERGE_FILL = 0.5     # if the content of all sheets occupies at most this fraction of one sheet, it belongs on one sheet
REQUIRED_TITLE = ('title', 'created_by', 'created_date', 'scale')
WANTED_TITLE = ('drawing_number', 'material', 'general_tolerance', 'tolerancing')


def part_role(part):
    """Role of a spec part: manufactured|standard|purchased|reference|duplicate. Single place of the default: without
    'role', plan_roles.regex_role(name) applies, otherwise 'manufactured'."""
    return part.get('role') or plan_roles.regex_role(part.get('name')) or 'manufactured'


def _f(code, severity, sheet, bbox, message, **extra):
    d = {'code': code, 'severity': severity, 'sheet': sheet, 'bbox_mm': [round(float(v), 2) for v in bbox] if bbox else None, 'message': message}
    d.update(extra)
    return d


def _inter(a, b, pad=0.0):
    return a[0] < b[2] - pad and b[0] < a[2] - pad and a[1] < b[3] - pad and b[1] < a[3] - pad


MIN_TEXT_H = 2.49    # mm text height (minimum 2.5 with rounding allowance)


VIEW_GAP_MM = 6.0        # minimum distance between the envelopes (geometry, centre lines, title) of two views; below it their dimension zones touch (planner and check share the value: plan_layout.INNER = VIEW_GAP_MM - 2 * MEASURE_PAD; positions are rounded to 0.01 mm, hence the tolerance VIEW_GAP_TOL)
VIEW_GAP_TOL = 0.05      # mm: rounding of the positions in the spec (0.01 mm) and of the envelope size
TABLE_NEAR_MM = 40.0     # largest distance between a table with a reference view (bend table: labels_in) and that view; it stands adjacent, in the view's block


def _rect_gap(a, b):
    """Distance of two rectangles [x0, y0, x1, y1] (0 when touching or overlapping)."""
    return float(np.hypot(max(a[0] - b[2], b[0] - a[2], 0.0), max(a[1] - b[3], b[1] - a[3], 0.0)))


def view_distance_findings(idx, views, strict=False):
    """views_too_close (warn, strict block): two views that do not overlap (view_overlap reports that) are closer than VIEW_GAP_MM."""
    out, ids = [], list(views)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = views[ids[i]]['bbox'], views[ids[j]]['bbox']
            gap = _rect_gap(a, b)
            if not _inter(a, b, 0.1) and gap < VIEW_GAP_MM - VIEW_GAP_TOL:
                out.append(_f('views_too_close', 'block' if strict else 'warn', idx, a,
                              f'Views {ids[i]} and {ids[j]} are only {fmt(gap, 1)} mm apart (minimum {fmt(VIEW_GAP_MM, 0)} mm): their dimension zones touch',
                              views=[ids[i], ids[j]], gap_mm=round(gap, 2)))
    return out


def table_distance_findings(idx, views, blocks, table_views, strict=False):
    """table_far_from_view (warn, strict block): a table with a reference view (table_views {block name 'table:...': view id}) is farther than
    TABLE_NEAR_MM from it (blocks = sh.blocks)."""
    out = []
    for name, vid in (table_views or {}).items():
        if name in blocks and vid in views:
            gap = _rect_gap(blocks[name], views[vid]['bbox'])
            if gap > TABLE_NEAR_MM + 1e-9:
                out.append(_f('table_far_from_view', 'block' if strict else 'warn', idx, blocks[name],
                              f'{name} is {fmt(gap, 0)} mm from its reference view {vid} (maximum {fmt(TABLE_NEAR_MM, 0)} mm): '
                              f'move it into the view block', view=vid, gap_mm=round(gap, 1)))
    return out


TEXT_CORE_MM = 0.3


def text_core(bbox):
    """Core of a text envelope (axis box, shrunk by TEXT_CORE_MM all around): what text_on_line and text_on_geometry check against lines and body edges. The planner (drawing.angle_free_radius)
    chooses the position of the angle text with the same function."""
    x0, y0, x1, y1 = bbox
    return sbox(x0 + TEXT_CORE_MM, y0 + TEXT_CORE_MM, x1 - TEXT_CORE_MM, y1 - TEXT_CORE_MM)


ARC_CLEAR_MM, ARC_END_FRACTION, ARC_LEG_OVER_MM, ARC_LEG_MIN_MM = 1.6, 0.12, 2.0, 0.3


def arc_leg_lines(vertex, d1, d2, radius):
    """Extension lines of an angle arc that lies beyond a leg end point (ISO 129-1; radius > leg length): from the leg end point (vertex + d) in
    leg direction up to ARC_LEG_OVER_MM beyond the arc. [] for legs that reach the arc. Renderer (sheet.angle_dim, drawing.angle_free_radius) and checker share them."""
    v = np.asarray(vertex, float)
    out = []
    for d in (d1, d2):
        d = np.asarray(d, float)
        n = float(np.linalg.norm(d))
        if n > 1e-9 and radius > n + ARC_LEG_MIN_MM:
            out.append([(v + d).tolist(), (v + d / n * (radius + ARC_LEG_OVER_MM)).tolist()])
    return out


def arc_clear(pts, others, texts, trim=False, tree=None):
    """Is the angle arc free? pts = arc points (sheet mm), others = lines (shapely) of body edges and of foreign dimension/leader lines, texts = envelopes (x0, y0, x1, y1) of foreign
    texts. Free means: the arc (trim: without the ends at the legs, ARC_END_FRACTION) keeps ARC_CLEAR_MM distance to all lines and hits no
    text (0.45 mm clearance); extension lines may cross lines (ISO 129-1). The renderer chooses the radius with this function, the checker
    (angle_arc_findings) evaluates the drawn arc with it."""
    pts = np.asarray(pts, float)
    k = max(1, int(len(pts) * ARC_END_FRACTION)) if trim else 0
    arc = LineString(pts[k:len(pts) - k] if len(pts) > 2 * k + 1 else pts)
    if tree is None and len(others):
        tree = STRtree(others)      # the renderer passes its tree through (radius search: many calls with the same lines)
    if tree is not None and len(tree.query(arc.buffer(ARC_CLEAR_MM), predicate='intersects')):
        return False
    for t in texts:
        if arc.intersects(sbox(*t).buffer(0.45, join_style='mitre')):
            return False
    return True


def angle_arc_findings(sh, idx, view_lines, strict=False):
    """angle_arc_unplaced (warn; N3.8): a drawn angle arc (sheet element with arc=True, sheet.angle_dim) that (a) lies on the contour or on foreign lines/texts (arc_clear in
    neither of the two stages: whole, without ends) or (b) lies beyond a leg end point without extension lines (ISO 129-1: no assignment to the vertex)."""
    arcs = [it for it in sh.items if it['kind'] == 'line' and it.get('arc')]
    if not arcs:
        return []
    out = []
    free = [it for it in sh.items if it['owner'] not in BLOCKS and not str(it['owner'] or '').startswith('table:')]
    for it in arcs:
        own = it['owner']
        others = [g for _, g in view_lines] + [LineString(o['pts']) for o in free if o['kind'] == 'line' and o.get('pts') and len(o['pts']) >= 2 and o['owner'] != own and o.get('role') != 'ext']      # extension lines (also the leg lines of other angle dimensions) may cross the arc (ISO 129-1, N3.3: cross_ext_dim only a metric)
        texts = [o['bbox'] for o in free if o['kind'] == 'text' and o['owner'] != own]
        legs = [o['pts'] for o in free if o['kind'] == 'line' and o.get('leg') and o['owner'] == own]
        pts = np.asarray(it['pts'], float)
        radius = float(np.linalg.norm(pts[0] - np.asarray(it['vertex'], float)))
        need = sum(1 for n in it['legs'] if radius > n + ARC_LEG_MIN_MM)
        why = None
        if len(legs) < need:
            why = 'beyond the legs without extension lines'
        elif not (arc_clear(pts, others, texts, False) or arc_clear(pts, others, texts, True)):
            why = 'lies on contour, lines or text'
        if why:
            out.append(_f('angle_arc_unplaced', 'warn', idx, it['bbox'], f'Angle arc {why} (radius {radius:.1f} mm): not clearly assigned to the angle (ISO 129-1)', owner=own))
    return out


def sheet_findings(sh, idx, views, view_lines, strict=False, table_views=None):
    """sh: Sheet; views: {id: {'bbox': [...]}}; view_lines: list of (view_id, shapely line) of visible edges in sheet mm.
    strict: text_too_small blocks (local path), otherwise a hint. table_views: {table block 'table:...': reference view}."""
    out = []
    L, B, R, T = sh.area
    texts = [it for it in sh.items if it['kind'] == 'text']
    free = [it for it in sh.items if it['owner'] not in BLOCKS and not str(it['owner'] or '').startswith('table:')]
    # text below the minimum height (title block, revision block, parts list and tables are exempt)
    for t in free:
        if t['kind'] == 'text' and t.get('height') is not None and t['height'] < MIN_TEXT_H:
            out.append(_f('text_too_small', 'block' if strict else 'warn', idx, t['bbox'],
                          f"Text “{t['text']}” is only {fmt(t['height'], 1)} mm high (minimum 2.5 mm)", owner=t['owner'], height=t['height']))
    # outside the drawing area
    for it in sh.items:
        x0, y0, x1, y1 = it['bbox']
        if x0 < L - 0.05 or y0 < B - 0.05 or x1 > R + 0.05 or y1 > T + 0.05:
            out.append(_f('out_of_area', 'block', idx, it['bbox'], f"{'Text “' + it['text'] + '”' if it['kind'] == 'text' else 'Line'} extends beyond the drawing area", owner=it['owner']))
    for vid, v in views.items():
        x0, y0, x1, y1 = v['bbox']
        if x0 < L or y0 < B or x1 > R or y1 > T:
            out.append(_f('out_of_area', 'block', idx, v['bbox'], f'View {vid} extends beyond the drawing area', view=vid))
    # over title block, revision block, parts list, tables
    for name, bb in sh.blocks.items():
        for it in free:
            if _inter(it['bbox'], bb, 0.1):
                what = f"Text “{it['text']}”" if it['kind'] == 'text' else 'Line'
                out.append(_f('overlaps_block', 'block', idx, it['bbox'], f'{what} overlaps {name}', owner=it['owner'], block=name))
        for vid, v in views.items():
            if _inter(v['bbox'], bb, 0.1):
                out.append(_f('overlaps_block', 'block', idx, v['bbox'], f'View {vid} overlaps {name}', view=vid, block=name))
    names = list(sh.blocks)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if _inter(sh.blocks[names[i]], sh.blocks[names[j]], 0.1):
                out.append(_f('overlaps_block', 'block', idx, sh.blocks[names[i]], f'{names[i]} overlaps {names[j]}', block=names[j]))
    # views overlap
    ids = list(views)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if _inter(views[ids[i]]['bbox'], views[ids[j]]['bbox'], 0.1):
                out.append(_f('view_overlap', 'block', idx, views[ids[i]]['bbox'], f'Views {ids[i]} and {ids[j]} overlap', views=[ids[i], ids[j]]))
    out += view_distance_findings(idx, views, strict)
    out += table_distance_findings(idx, views, sh.blocks, table_views, strict)
    # texts overlap (also block texts among themselves, if the text does not fit)
    boxes = [sbox(*t['bbox']) for t in texts]
    if boxes:
        tree = STRtree(boxes)
        for i, b in enumerate(boxes):
            for j in tree.query(b):
                j = int(j)
                if j <= i:
                    continue
                a = b.intersection(boxes[j]).area
                if a > 0.05 * min(b.area, boxes[j].area) and a > 0.2:
                    out.append(_f('text_overlap', 'block', idx, texts[i]['bbox'],
                                  f"Texts “{texts[i]['text']}” and “{texts[j]['text']}” overlap", owners=[texts[i]['owner'], texts[j]['owner']]))
    # text on a foreign dimension, leader or section line (the annotation's own line is excluded)
    alines = [it for it in sh.items if it['kind'] == 'line' and it.get('pts') and len(it['pts']) >= 2 and it['owner'] not in BLOCKS]
    if alines:
        geoms = [LineString(it['pts']) for it in alines]
        tree = STRtree(geoms)
        for t in texts:
            if t['owner'] in BLOCKS:
                continue
            x0, y0, x1, y1 = t['bbox']
            if x1 - x0 < 0.6 or y1 - y0 < 0.6:
                continue
            letter = t['owner'] in getattr(sh, 'letter_owners', ())        # label letters lie free: on a line like any text (block), with less than LETTER_CLEAR_MM distance: section_letter_on_line (warn)
            core = text_core(t['bbox'])
            wide = letter_zone(t['bbox']) if letter else None
            hit = None
            for k in tree.query(wide if letter else core):
                k = int(k)
                if alines[k]['owner'] == t['owner']:
                    continue
                if geoms[k].intersects(core):
                    hit = (k, True)
                    break
                if letter and geoms[k].intersects(wide) and hit is None:
                    hit = (k, False)
            if hit:
                k, on = hit
                out.append(_f('text_on_line' if on else 'section_letter_on_line', 'block' if on else 'warn', idx, t['bbox'],
                              f"{'Label letter' if letter else 'Text'} “{t['text']}” {'lies on' if on else 'is too close to'} a dimension/leader line ({alines[k]['owner']})",
                              owner=t['owner'], other=alines[k]['owner']))
    # text on a visible body edge
    if view_lines:
        geoms = [g for _, g in view_lines]
        tree = STRtree(geoms)
        for t in texts:
            if t['owner'] in BLOCKS:
                continue
            x0, y0, x1, y1 = t['bbox']
            if x1 - x0 < 0.6 or y1 - y0 < 0.6:
                continue
            core = text_core(t['bbox'])
            hit = [int(k) for k in tree.query(core) if geoms[int(k)].intersects(core)]
            if hit:
                out.append(_f('text_on_geometry', 'block', idx, t['bbox'], f"Text “{t['text']}” lies on a body edge (view {view_lines[hit[0]][0]})", owner=t['owner']))
    out += angle_arc_findings(sh, idx, view_lines, strict)
    out += cell_overflow_findings(sh, idx, strict)
    return out


LETTER_CLEAR_MM = 0.8    # label letters of a section line/detail circle/view arrow: distance to foreign lines


def letter_zone(bbox):
    """Check zone of a label letter: its text envelope enlarged by LETTER_CLEAR_MM all around; a foreign dimension/leader line inside it is section_letter_on_line (sheet_findings).
    label_layout.place chooses the position of a label letter with the same zone."""
    x0, y0, x1, y1 = bbox
    return sbox(x0 - LETTER_CLEAR_MM, y0 - LETTER_CLEAR_MM, x1 + LETTER_CLEAR_MM, y1 + LETTER_CLEAR_MM)
CELL_TOL = 0.05      # mm, tolerance of the text edge to the cell edge (x); y: CELL_TOL_Y (descenders)
CELL_TOL_Y = 0.3


def cell_overflow_findings(sh, idx, strict=False):
    """cell_overflow (block when strict, otherwise warn): text in a cell (title block, revision block, parts list, table cell,
    table and block titles; sh.cells) whose envelope extends beyond the cell edge - measured with the same text width function
    as drawing (sheet.text_size, registered in sh.items). text_truncated (warn): shortened cell text with an ellipsis,
    the full text is in the finding (planner note). Sheet.field fits texts in (wrapping, shrinking, truncating); the finding catches what
    still overflows (fixed cells below the smallest font, self-placed texts)."""
    out = []
    for c in sh.cells:
        x0, y0, x1, y1 = c['bbox']
        b0, c0, b1, c1 = c['box']
        if x0 < b0 - CELL_TOL or x1 > b1 + CELL_TOL or y0 < c0 - CELL_TOL_Y or y1 > c1 + CELL_TOL_Y:
            out.append(_f('cell_overflow', 'block' if strict else 'warn', idx, c['bbox'],
                          f"Text “{c['text']}” overflows its cell ({c['name']})", cell=c['name'], text=c['text']))
    for t in getattr(sh, 'truncated', []):
        out.append(_f('text_truncated', 'warn', idx, None, f"Text in {t['name']} truncated to “{t['shown']}”; full text: “{t['full']}”",
                      cell=t['name'], text=t['full']))
    return out


VIEW_EMPTY_MM = 1.0  # mm of visible edges on paper: below it a view shows no part geometry


def view_empty_findings(si, views, strict=False):
    """view_empty (block when strict, otherwise warn): view without part geometry - normal view without visible edges,
    detail view without a visible edge in the circle (hidden edges do not count), section (also a detail of a section)
    without hatching and without visible edges; "without" means less than VIEW_EMPTY_MM line length on paper.
    views: [{id, label, ink_mm (length of the visible edges in sheet mm), hatch_lines, is_section, is_detail, bbox}].
    Depends only on the view content, not on the position: render and layout_probe check it identically (drawing._compose)."""
    out = []
    for v in views:
        if v['ink_mm'] >= VIEW_EMPTY_MM or (v['is_section'] and v['hatch_lines']):
            continue
        what = ('neither hatching nor visible edges' + (' in the detail circle' if v['is_detail'] else '') if v['is_section']
                else 'no visible edge in the detail circle' if v['is_detail'] else 'no visible edges')
        title = f" “{v['label']}”" if v.get('label') else ''
        out.append(_f('view_empty', 'block' if strict else 'warn', si, v['bbox'],
                      f"View {v['id']}{title} (sheet {si + 1}) shows no part geometry: {what}; omit the view or put a "
                      f"visible form feature into it", view=v['id']))
    return out


CROSS_PAD = 0.3      # mm: intersections near the line end points (connections, arrowheads) do not count


def leader_crossing_findings(sh, idx):
    """leader_crossing (always warn): lines of two different annotations (owner 'ann:i': leader, dimension, extension lines)
    intersect outside CROSS_PAD around their end points."""
    lines = [(it['owner'], LineString(it['pts'])) for it in sh.items
             if it['kind'] == 'line' and it.get('pts') and len(it['pts']) >= 2 and str(it['owner'] or '').startswith('ann:')]
    lines = [(o, g) for o, g in lines if g.length > 1e-6]
    if len(lines) < 2:
        return []
    tree = STRtree([g for _, g in lines])
    ends = [[Point(g.coords[0]), Point(g.coords[-1])] for _, g in lines]
    out, seen = [], set()
    for i, (oi, gi) in enumerate(lines):
        for j in tree.query(gi):
            j = int(j)
            oj = lines[j][0]
            if j <= i or oj == oi or (oi, oj) in seen or (oj, oi) in seen:
                continue
            x = gi.intersection(lines[j][1])
            pts = [x] if x.geom_type == 'Point' else list(x.geoms) if x.geom_type == 'MultiPoint' else []
            hit = next((p for p in pts if all(p.distance(e) > CROSS_PAD for e in ends[i] + ends[j])), None)
            if hit is not None:
                seen.add((oi, oj))
                out.append(_f('leader_crossing', 'warn', idx, [hit.x - 1, hit.y - 1, hit.x + 1, hit.y + 1],
                              f"Leader/dimension lines of {oi} and {oj} cross at ({fmt(hit.x, 0)}, {fmt(hit.y, 0)}) mm: reroute the leader line",
                              owners=[oi, oj]))
    return out


# ---- Placement measurement codes (standards rule set N3.1, N3.3, N3.5, N3.6). All warn, not in autofix.LAYOUT: they only measure.
PLACEMENT_CODES = ('cross_dim_dim', 'cross_leader', 'cross_ext_dim', 'dim_text_detached', 'dim_spacing')
# Thresholds are practice values (drafting practice, not a standard value), calibrated on six reference sheets.
DETACH_MM = 4.5      # Dimension text at most this far from its envelope to the dimension line. Default position centered ≈ 1 mm above the line; if the dimension figure is wider than the dimension line,
#                      it stands 4.0 mm beyond its end (Sheet.dim), rotated texts measure up to 4.12 mm (envelope). Measured: four texts
#                      with 4.00-4.12 mm, all in the default position; hence 4.5 instead of the planned 3.0 (margin 0.4 mm for rounding)
FIRST_ROW_MM = 6.0   # dimension line at least this far from a visible edge of its view; textbook 10 mm (N3.1), warning threshold below
ROW_GAP_MM = 5.0     # two overlapping parallel dimension lines of a view at least this far apart; textbook 7 mm (N3.1), warning threshold below
ROW_SAME_MM = 0.5    # dimension lines closer than this are in the same row (chain dimension), not in two rows
ROW_OVERLAP_MM = 1.0       # dimension lines must overlap this far along their direction for the row spacing to count
PARALLEL_TOL = 0.02        # |sin| of the angle of two parallel dimension lines
MIN_OFFSET_MM = 1.0        # dimensions without offset (Ø drawn into the circle) form no row
DIM_TOL = 0.01             # mm: positions in the spec are rounded to 0.01 mm; rows are placed exactly at 5.0 or 6.0 mm (floating-point noise must not trigger)


def _ann_items(sh):
    return [it for it in sh.items if it['kind'] == 'line' and it.get('pts') and len(it['pts']) >= 2 and str(it['owner'] or '').startswith('ann:')]


def crossing_kinds_findings(sh, idx):
    """Crossings by line type (rule N3.3), all warn: cross_dim_dim (dimension line × dimension line of two annotations), cross_leader (leader line ×
    any line of another annotation) and cross_ext_dim (extension line × foreign dimension line; metric only). Extension line × extension line, lines
    of the same owner and intersections within CROSS_PAD around end points do not count (as in leader_crossing, which stays unchanged). One finding per pair of
    owners and code. Role: Sheet.line(role=...)."""
    lines = [(it['owner'], it.get('role'), LineString(it['pts'])) for it in _ann_items(sh)]
    lines = [(o, r, g) for o, r, g in lines if g.length > 1e-6]
    if len(lines) < 2:
        return []
    tree = STRtree([g for _, _, g in lines])
    ends = [[Point(g.coords[0]), Point(g.coords[-1])] for _, _, g in lines]
    out, seen = [], set()
    for i, (oi, ri, gi) in enumerate(lines):
        for j in tree.query(gi):
            j = int(j)
            oj, rj, gj = lines[j]
            if j <= i or oj == oi:
                continue
            roles = {ri, rj}
            if 'leader' in roles:
                code = 'cross_leader'
            elif ri == 'dim' and rj == 'dim':
                code = 'cross_dim_dim'
            elif roles == {'ext', 'dim'}:
                code = 'cross_ext_dim'
            else:
                continue
            if (code, frozenset((oi, oj))) in seen:
                continue
            x = gi.intersection(gj)
            pts = [x] if x.geom_type == 'Point' else list(x.geoms) if x.geom_type == 'MultiPoint' else []
            hit = next((p for p in pts if all(p.distance(e) > CROSS_PAD for e in ends[i] + ends[j])), None)
            if hit is None:
                continue
            seen.add((code, frozenset((oi, oj))))
            what = {'cross_dim_dim': 'Dimension lines', 'cross_leader': 'Leader line and line', 'cross_ext_dim': 'Extension line and dimension line'}[code]
            out.append(_f(code, 'warn', idx, [hit.x - 1, hit.y - 1, hit.x + 1, hit.y + 1],
                          f"{what} of {oi} and {oj} cross at ({fmt(hit.x, 0)}, {fmt(hit.y, 0)}) mm", owners=[oi, oj]))
    return out


def _dim_lines(sh):
    """{owner: [(shapely line, item)]} of the items with role 'dim'."""
    out = {}
    for it in _ann_items(sh):
        if it.get('role') == 'dim':
            g = LineString(it['pts'])
            if g.length > 1e-6:
                out.setdefault(it['owner'], []).append((g, it))
    return out


def dim_text_detached_findings(sh, idx):
    """dim_text_detached (warn, rule N3.5): text of an owner with dimension lines (role 'dim') whose envelope is farther than DETACH_MM from the
    union of its dimension lines. Short dimensions with text beside the arrows (outside) do not count: their dimension line is extended beyond the
    arrows, the text stands in front of it."""
    dims = _dim_lines(sh)
    if not dims:
        return []
    union = {o: unary_union([g for g, _ in v]) for o, v in dims.items()}
    out = []
    for it in sh.items:
        o = it['owner']
        if it['kind'] != 'text' or o not in union:
            continue
        d = sbox(*it['bbox']).distance(union[o])
        if d > DETACH_MM:
            out.append(_f('dim_text_detached', 'warn', idx, it['bbox'],
                          f"Dimension figure “{it['text']}” ({o}) is {fmt(d, 1)} mm away from its dimension line (at most {fmt(DETACH_MM, 1)} mm): move the text to the dimension",
                          owner=o, distance_mm=round(float(d), 2)))
    return out


def dim_spacing_findings(sh, idx, view_lines, owner_view):
    """dim_spacing (warn, rule N3.1): (a) a dimension line is closer than FIRST_ROW_MM to a visible edge of its view; (b) two overlapping
    parallel dimension lines of the same view are closer than ROW_GAP_MM (same row, distance ≈ 0, does not count). Only
    dimension lines with offset (sh.dim with |offset| ≥ MIN_OFFSET_MM) count; radius and angle dimensions and Ø in the circle form no row.
    view_lines: [(view, shapely line)] of visible edges in sheet mm; owner_view: {'ann:i': view} (built by the caller from the annotations)."""
    rows = []
    for o, v in _dim_lines(sh).items():
        view = owner_view.get(o)
        if view is None:
            continue
        for g, it in v:
            if abs(it.get('offset') or 0.0) >= MIN_OFFSET_MM:
                rows.append((o, view, g))
    if not rows:
        return []
    edges = {}
    for vid, g in view_lines:
        edges.setdefault(vid, []).append(g)
    edges = {vid: unary_union(gs) for vid, gs in edges.items()}
    out, first = [], set()
    for o, view, g in rows:
        if view in edges and o not in first:
            d = g.distance(edges[view])
            if d < FIRST_ROW_MM - DIM_TOL:
                first.add(o)
                out.append(_f('dim_spacing', 'warn', idx, g.bounds,
                              f"Dimension line of {o} is only {fmt(d, 1)} mm from the edge of view {view} (at least {fmt(FIRST_ROW_MM, 0)} mm)",
                              owner=o, view=view, distance_mm=round(float(d), 2)))
    pairs = set()
    for i, (oi, vi, gi) in enumerate(rows):
        pi = np.asarray(gi.coords[0]); ui = np.asarray(gi.coords[-1]) - pi
        ui = ui / np.linalg.norm(ui)
        for oj, vj, gj in rows[i + 1:]:
            if oj == oi or vj != vi or frozenset((oi, oj)) in pairs:
                continue
            pj = np.asarray(gj.coords[0]); uj = np.asarray(gj.coords[-1]) - pj
            uj = uj / np.linalg.norm(uj)
            if abs(ui[0] * uj[1] - ui[1] * uj[0]) > PARALLEL_TOL:
                continue
            n = np.array([-ui[1], ui[0]])
            gap = abs(float((pj - pi) @ n))
            a0, a1 = sorted(float(np.asarray(c) @ ui) for c in (gi.coords[0], gi.coords[-1]))
            b0, b1 = sorted(float(np.asarray(c) @ ui) for c in (gj.coords[0], gj.coords[-1]))
            if ROW_SAME_MM <= gap < ROW_GAP_MM - DIM_TOL and min(a1, b1) - max(a0, b0) > ROW_OVERLAP_MM:
                pairs.add(frozenset((oi, oj)))
                out.append(_f('dim_spacing', 'warn', idx, unary_union([gi, gj]).bounds,
                              f"Dimension lines of {oi} and {oj} (view {vi}) are only {fmt(gap, 1)} mm apart (at least {fmt(ROW_GAP_MM, 0)} mm)",
                              owners=[oi, oj], view=vi, distance_mm=round(gap, 2)))
    return out


def dims_inside(sh, views, owner_view):
    """Rule N3.6: per view (number of dimension texts whose center lies in the geometry envelope of the view, number of dimension texts of the view).
    views: {view: {'bbox': geometry envelope in sheet mm}}; owner_view: {'ann:i': view}. Dimension texts = texts of owners with dimension lines
    (role 'dim'). Empty without dimensions."""
    dim_owners = set(_dim_lines(sh))
    out = {}
    for it in sh.items:
        view = owner_view.get(it['owner'])
        if it['kind'] != 'text' or it['owner'] not in dim_owners or view not in views:
            continue
        x0, y0, x1, y1 = it['bbox']
        bx0, by0, bx1, by1 = views[view]['bbox']
        inside = bx0 <= (x0 + x1) / 2 <= bx1 and by0 <= (y0 + y1) / 2 <= by1
        a, n = out.get(view, (0, 0))
        out[view] = (a + int(inside), n + 1)
    return out


def content_area(sh, views, pad=2.0):
    """Occupied area of a sheet in mm^2: views, dimensions, texts, tables (each with margin pad), without title block,
    revision block and parts list; overlaps count once."""
    boxes = [v['bbox'] for v in views.values()]
    boxes += [it['bbox'] for it in sh.items if it['owner'] is not None and it['owner'] not in BLOCKS]
    if not boxes:
        return 0.0
    return float(unary_union([sbox(b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad) for b in boxes]).area)


def sheet_count_findings(spec, rendered):
    """Several sheets whose content fits on one sheet together (largest used size or one larger)."""
    sheets = rendered['sheets']
    if len(sheets) < 2:
        return []
    n_parts = len(spec.get('parts') or [])
    used = sum(s['content_mm2'] for s in sheets)
    biggest = max(ORDER.index(s['format']) for s in sheets if s['format'] in FORMATS)
    for fmt in ORDER[biggest:biggest + 2]:
        cap = usable_area(fmt, 'portrait' if fmt == 'A4' else 'landscape', n_parts)
        if used <= MERGE_FILL * cap:
            reason = str(spec.get('sheets_reason') or '').strip()
            return [_f('sheets_mergeable', 'warn' if reason else 'block', 0, None,
                       f'The content of the {len(sheets)} sheets together occupies {used / cap:.0%} of the drawing area of one {fmt} sheet: '
                       f'merge onto one {fmt} sheet (assembly, parts list and single parts on one sheet; details in their own scale)'
                       + (f' — kept separate per sheets_reason: {reason[:120]}' if reason else ''),
                       sheets=len(sheets), fill=round(used / cap, 3), format=fmt)]
    return []


def drawing_findings(spec, rendered):
    """rendered: {'sheets':[{'parts_shown':set, 'balloons':set, 'dims_parts':set, 'has_parts_list':bool}]}."""
    out = []
    parts = spec.get('parts') or []
    ids = [p['id'] for p in parts]
    role = {p['id']: part_role(p) for p in parts}
    title = spec.get('title') or {}
    for k in REQUIRED_TITLE:
        if k == 'scale':
            continue
        if not str(title.get(k, '') or '').strip():
            out.append(_f('title_field_empty', 'block', 0, None, f'Title block: {k} missing', field=k))
    for k in WANTED_TITLE:
        if not str(title.get(k, '') or '').strip():
            out.append(_f('title_field_empty', 'warn', 0, None, f'Title block: {k} empty', field=k))
    shown = set().union(*[s['parts_shown'] for s in rendered['sheets']]) if rendered['sheets'] else set()
    for pid in ids:
        if pid not in shown and role[pid] != 'duplicate':
            out.append(_f('part_not_shown', 'block', 0, None, f'Part {pid} appears in no view', part=pid))
    unlisted = ('reference', 'duplicate')         # surrounding contour or print layout: not in the parts list
    listable = [pid for pid in ids if role[pid] not in unlisted]
    if len(listable) > 1:
        pl_sheets = [i for i, s in enumerate(rendered['sheets']) if s['has_parts_list']]
        if not pl_sheets:
            out.append(_f('parts_list_missing', 'block', 0, None, f'{len(listable)} parts, but no parts list', parts=listable))
        else:
            listed = set()
            for s in spec['sheets']:
                for row in s.get('parts_list') or []:
                    if row.get('part'):
                        listed.add(row['part'])
            for pid in listable:
                if pid not in listed:
                    out.append(_f('parts_list_missing', 'block', pl_sheets[0], None, f'Part {pid} is missing in the parts list', part=pid))
            ballooned = set().union(*[s['balloons'] for s in rendered['sheets']])
            numbered = {num for _, num, _ in balloon_numbers(spec)}            # identical parts carry one number (on one instance): the item counts, not the instance
            rows = _pos_rows(spec)
            for pid in sorted(listed):
                if pid not in ballooned and rows.get(pid) not in numbered and role.get(pid) not in unlisted:
                    out.append(_f('balloon_missing', 'block', pl_sheets[0], None, f'Part {pid} has no item number in any view', part=pid))
    dimmed = set().union(*[s['dims_parts'] for s in rendered['sheets']]) if rendered['sheets'] else set()
    for pid in ids:
        if pid in shown and pid not in dimmed and role[pid] == 'manufactured':
            out.append(_f('part_without_dimension', 'warn', 0, None, f'Part {pid} is not dimensioned (may be intended for standard parts/semi-finished products)', part=pid))
    out += sheet_count_findings(spec, rendered)
    out += balloon_repeat_findings(spec)                # N4.7: one item number per item
    return out


# ------------------------------------------------------------------ N1 Sheet and scale
# Rules shared by planner (plan.py: part scales) and checker; everything is computed from spec and render result (view_frames), identical for local and remote.

SCALE_PAIRS = ((50, 1), (20, 1), (10, 1), (5, 1), (2, 1), (1, 1), (1, 2), (1, 5), (1, 10), (1, 20), (1, 50), (1, 100))      # analyze.SCALES (ISO 5455)
SCALE_VALUES = [a / b for a, b in SCALE_PAIRS]
SCALE_TOL = 1e-9
MAIN_RATIO = 0.65            # N1.4: a small secondary part (< COMPARABLE_SHARE of the elements) at most as large (paper area) as MAIN_RATIO × main part; calibration: rules.py N1.4
COMPARABLE_SHARE = 0.5       # N1.4: from this share of the main part's mandatory elements a part counts as comparable (scale ≤ main part, no area)
FIT_FILL = 0.45              # N1.3: sheet fill up to which an enlargement still counts as "still fits". Single-point calibration (smallest value of the reference set is 0.459,
#                              all others ≥ 0.61); recalibration open before the code moves to block: rules.py N1.3
BLOCK_GAP = 10.0             # mm: block gap when packing (plan.FREE_GAP): free area of a block = drawing area without margin and half the block gap all around
BLOCK_TITLE = 10.0           # mm: space of the title line above a block (plan_layout.TITLE)


def scale_ok(text):
    """True if the scale (text "a:b") is in the standard series (N1.1)."""
    v = _scale(text)
    return v is not None and any(abs(v - s) <= SCALE_TOL * max(1.0, s) for s in SCALE_VALUES)


def next_scale(value):
    """Next larger standard scale (value) above value, None for the largest."""
    return next((s for s in reversed(SCALE_VALUES) if s > value * (1 + SCALE_TOL)), None)


def fmt_scale(value):
    """Scale value -> text "a:b" of the standard series (otherwise the value)."""
    for (a, b), s in zip(SCALE_PAIRS, SCALE_VALUES):
        if abs(value - s) <= SCALE_TOL * max(1.0, s):
            return f'{a}:{b}'
    return f'{value:g}'


def scale_findings(spec):
    """scale_not_standard (warn, N1.1): sheet scale, scale of a view or of a detail view is not in the ISO 5455 series (analyze.SCALES).
    One finding per sheet (sheet scale) or view."""
    out = []
    for si, sd in enumerate(spec.get('sheets') or []):
        if sd.get('scale') is not None and not scale_ok(sd['scale']):
            out.append(_f('scale_not_standard', 'warn', si, None, f"Sheet scale {sd['scale']} is not in the standard series (ISO 5455)", scale=str(sd['scale'])))
        for vd in sd.get('views') or []:
            bad = [(what, text) for what, text in (('Scale', vd.get('scale')), ('Scale of the detail view', (vd.get('detail') or {}).get('scale')))
                   if text is not None and not scale_ok(text)]
            if bad:                                       # at most one finding per view
                out.append(_f('scale_not_standard', 'warn', si, None,
                              f"View {vd.get('id')}: " + '; '.join(f'{what} {text}' for what, text in bad) + ' is not in the standard series (ISO 5455)',
                              view=vd.get('id'), scale=str(bad[0][1])))
    return out


def main_part(counts, volumes=None):
    """Main part (N1.4): the manufactured part with the most mandatory elements (counts {part: number}, counted like coverage.parts[part].required); tie:
    larger envelope volume (volumes {part: mm³}), then the order of counts. One function for planner and checker. None without a part."""
    volumes = volumes or {}
    order = list(counts)
    return max(order, key=lambda p: (counts[p], float(volumes.get(p) or 0.0), -order.index(p)), default=None)


def wire_part(bend):
    """Wire part: its mandatory element is only the complete bend table (bend = analyze.bend_table of the part). One definition for planner and checker."""
    return bool(bend and bend.get('complete'))


def non_wire_counts(counts, wires=()):
    """counts without wire parts: N1.4 does not apply to them, they are neither main part nor secondary part."""
    return {pid: n for pid, n in counts.items() if pid not in set(wires)}


def main_ratio_violations(areas, counts, main, scales=None, wires=(), ratio=None):
    """Violations of N1.4 as [{'case', 'part', 'ratio'}]: counts {manufactured part: mandatory elements}, main = main part, areas
    {part or 'asm': paper area}, scales {part or 'asm': scale value}, wires = wire parts (exempt).
    - 'minor': manufactured part with fewer than COMPARABLE_SHARE of the main part's elements, area larger than ratio × main part (ratio = area ratio);
    - 'comparable': manufactured part with at least COMPARABLE_SHARE of the elements at a larger scale than the main part (ratio = scale ratio);
    - 'assembly': assembly at a larger scale than the main part (ratio = scale ratio).
    The area depends on the number of views and therefore applies only to small secondary parts. If the area of the main part or a scale is missing, the case is not measurable."""
    ratio = MAIN_RATIO if ratio is None else ratio
    scales = scales or {}
    counts = non_wire_counts(counts, wires)
    out = []
    if main is None or main not in counts:
        return out
    base, kmain = float(areas.get(main) or 0.0), float(scales.get(main) or 0.0)
    for pid, n in counts.items():
        if pid == main:
            continue
        if n >= COMPARABLE_SHARE * counts[main]:
            k = float(scales.get(pid) or 0.0)
            if kmain > 0 and k > kmain * (1 + SCALE_TOL):
                out.append({'case': 'comparable', 'part': pid, 'ratio': k / kmain})
        else:
            a = float(areas.get(pid) or 0.0)
            if base > 0 and a > ratio * base * (1 + SCALE_TOL):
                out.append({'case': 'minor', 'part': pid, 'ratio': a / base})
    k = float(scales.get('asm') or 0.0)
    if kmain > 0 and k > kmain * (1 + SCALE_TOL):
        out.append({'case': 'assembly', 'part': None, 'ratio': k / kmain})
    return out


def _view_group(vd):
    """Block of a view: part ID, 'asm' (several parts) or None."""
    ps = vd.get('parts') or []
    return ps[0] if len(ps) == 1 else 'asm' if len(ps) > 1 else None


def _geom(fr):
    g = fr.get('geom_bbox_mm') or fr.get('bbox_mm')
    return [float(x) for x in g] if g else None


def _is_normal(vd):
    """Standard view, section or auxiliary view (without isometric and detail view)."""
    return not vd.get('detail') and not is_iso(vd)


def paper_areas(spec, frames):
    """{part or 'asm': paper area (mm²)} of the geometry: sum of the geometry envelopes of its standard views, sections and auxiliary views
    (view_frames), without isometric and detail view. Over all sheets."""
    out = {}
    for sd in spec.get('sheets') or []:
        for vd in sd.get('views') or []:
            fr, grp = frames.get(vd.get('id')), _view_group(vd)
            if fr is None or grp is None or not _is_normal(vd) or _geom(fr) is None:
                continue
            g = _geom(fr)
            out[grp] = out.get(grp, 0.0) + max(0.0, g[2] - g[0]) * max(0.0, g[3] - g[1])
    return out


def part_scale_values(spec, frames):
    """{part or 'asm': scale value} = median of the scales (view_frames) of the standard views, sections and auxiliary views of the block, without isometric and detail view."""
    vals = {}
    for sd in spec.get('sheets') or []:
        for vd in sd.get('views') or []:
            fr, grp = frames.get(vd.get('id')), _view_group(vd)
            if fr is not None and grp is not None and _is_normal(vd) and float(fr.get('scale') or 0.0) > 0:
                vals.setdefault(grp, []).append(float(fr['scale']))
    return {g: sorted(v)[len(v) // 2] for g, v in vals.items()}


def main_part_small_findings(spec, frames, counts, volumes=None, wires=()):
    """main_part_small (warn, N1.4): see main_ratio_violations (area only for small secondary parts, comparable parts and assembly via the scale,
    wire parts exempt); counts {manufactured part: mandatory elements} (coverage.parts), volumes for the tie at the main part.
    Not measurable (no findings): fewer than two manufactured parts without wire parts, sheet without part views with parts, main part without area or scale."""
    counts = non_wire_counts(counts or {}, wires)
    if len(counts) < 2:
        return []
    main = main_part(counts, volumes)
    scales = part_scale_values(spec, frames)
    out = []
    for v in main_ratio_violations(paper_areas(spec, frames), counts, main, scales):
        if v['case'] == 'minor':
            text = (f"Secondary part {v['part']} (fewer than {COMPARABLE_SHARE:.0%} of the form features) has {v['ratio']:.2f} × the paper area of the main part {main} "
                    f"(at most {MAIN_RATIO:g}×): the main part should be the largest")
        else:
            who = 'Assembly' if v['case'] == 'assembly' else f"Part {v['part']} (at least {COMPARABLE_SHARE:.0%} of the form features)"
            text = (f"{who} is at scale {fmt_scale(scales[v['part'] or 'asm'])}, larger than the main part {main} ({fmt_scale(scales[main])}): "
                    'no larger scale than the main part')
        out.append(_f('main_part_small', 'warn', 0, None, text, main=main, part=v['part'] or 'asm', case=v['case'], ratio=round(v['ratio'], 3)))
    return out


def _hull(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def enlarged_size(items, f):
    """Envelope size (w, h) of a block if its growing views are larger by factor f: geometry times f, dimension space (margin from the geometry to the envelope including
    dimensions) unchanged, view positions (geometry centers) stretched along. items: [(geometry box, envelope box, grows)] in sheet mm."""
    c0 = ((items[0][0][0] + items[0][0][2]) / 2, (items[0][0][1] + items[0][0][3]) / 2)
    rects = []
    for g, box, grows in items:
        cx, cy = (g[0] + g[2]) / 2, (g[1] + g[3]) / 2
        ex, ey = ((f - 1) * (g[2] - g[0]) / 2, (f - 1) * (g[3] - g[1]) / 2) if grows else (0.0, 0.0)
        nx, ny = c0[0] + (cx - c0[0]) * f, c0[1] + (cy - c0[1]) * f
        rects.append([nx - (cx - box[0]) - ex, ny - (cy - box[1]) - ey, nx + (box[2] - cx) + ex, ny + (box[3] - cy) + ey])
    h = _hull(rects)
    return h[2] - h[0], h[3] - h[1]


def free_area(format_, orientation):
    """Free area (W, H) of a block on the format: drawing area without margin and without half the block gap all around (as plan.free_area)."""
    W, H = sheet_size(format_, orientation)
    return W - 20 - 10 - BLOCK_GAP, H - 10 - 10 - BLOCK_GAP


def part_scale_findings(spec, frames, counts, volumes=None, wires=(), fit_fill=None):
    """part_scale_not_max (warn, N1.3): area test without layout attempt per manufactured part (without wire parts wires). Block box = envelope of the views of the part
    including dimensions (view_frames bbox_mm, ann_bbox_mm); enlarged at the next standard step (enlarged_size). Violation if the enlarged box fits into the free sheet area
    (width and height), the sheet fill afterwards is at most fit_fill (FIT_FILL) and N1.4 would not be newly violated afterwards. The test is
    necessary, not sufficient: a finding means "looks like a larger scale is possible"."""
    fit_fill = FIT_FILL if fit_fill is None else fit_fill
    counts, out = dict(counts or {}), []
    n14 = non_wire_counts(counts, wires)
    main = main_part(n14, volumes)
    areas, scales = paper_areas(spec, frames), part_scale_values(spec, frames)
    before = {(v['case'], v['part']) for v in main_ratio_violations(areas, n14, main, scales)} if len(n14) > 1 else set()
    for si, sd in enumerate(spec.get('sheets') or []):
        blocks = {}
        for vd in sd.get('views') or []:
            fr, grp = frames.get(vd.get('id')), _view_group(vd)
            if fr is None or grp is None or fr.get('bbox_mm') is None or _geom(fr) is None:
                continue
            box = [float(x) for x in fr['bbox_mm']]
            if fr.get('ann_bbox_mm'):
                box = _hull([box, [float(x) for x in fr['ann_bbox_mm']]])
            blocks.setdefault(grp, []).append((vd, fr, _geom(fr), box))
        if not blocks:
            continue
        orient = sd.get('orientation') or ('portrait' if sd.get('format') == 'A4' else 'landscape')
        fmt_ = sd.get('format') or 'A3'
        Wf, Hf = free_area(fmt_, orient)
        cap = usable_area(fmt_, orient, max(1, len(sd.get('parts_list') or [])))
        hulls = {g: _hull([x[3] for x in items]) for g, items in blocks.items()}
        size = {g: (h[2] - h[0], h[3] - h[1] + BLOCK_TITLE) for g, h in hulls.items()}
        for pid in counts:
            items = blocks.get(pid)
            if not items or pid in wires:
                continue
            normal = sorted(float(x[1].get('scale') or 0.0) for x in items if _is_normal(x[0]))
            if not normal or normal[len(normal) // 2] <= 0:
                continue
            k = normal[len(normal) // 2]
            nxt = next_scale(k)
            if nxt is None:
                continue
            f = nxt / k
            grows = lambda x: _is_normal(x[0]) and abs(float(x[1].get('scale') or 0.0) - k) <= SCALE_TOL * max(1.0, k)
            ordered = [x for x in items if grows(x)] + [x for x in items if not grows(x)]
            w, h = enlarged_size([(x[2], x[3], grows(x)) for x in ordered], f)
            h += BLOCK_TITLE
            if w > Wf + 1e-6 or h > Hf + 1e-6:
                continue
            fill = (sum(a * b for a, b in size.values()) - size[pid][0] * size[pid][1] + w * h) / cap
            if fill > fit_fill + 1e-9:
                continue
            area2, scales2 = dict(areas), dict(scales)
            area2[pid] = sum((x[2][2] - x[2][0]) * (x[2][3] - x[2][1]) * (f * f if grows(x) else 1.0) for x in items if _is_normal(x[0]))
            scales2[pid] = nxt
            after = {(v['case'], v['part']) for v in main_ratio_violations(area2, n14, main, scales2)} if len(n14) > 1 else set()
            if after - before:
                continue
            out.append(_f('part_scale_not_max', 'warn', si, hulls[pid],
                          f"Part {pid} is at scale {fmt_scale(k)}; the next standard scale {fmt_scale(nxt)} still fits on the sheet {fmt_} (block {w:.0f} × {h:.0f} mm against "
                          f"{Wf:.0f} × {Hf:.0f} mm free, fill afterwards {fill:.2f} ≤ {fit_fill:g}) and N1.4 stays satisfied",
                          part=pid, scale=fmt_scale(k), next_scale=fmt_scale(nxt), fill=round(fill, 3)))
    return out


def n1_findings(spec, frames, coverage, features_by_part, wires=()):
    """All findings of group N1: scale_not_standard, main_part_small, part_scale_not_max. coverage: result of coverage() (None: only N1.1);
    features_by_part {part: {'bbox': …}} for the envelope volume (tie at the main part); wires: wire parts (no own scale)."""
    out = scale_findings(spec)
    manuf = list(features_by_part or {})              # manufactured parts of the service/core (also for specs without parts)
    if coverage is None or len(manuf) < 2:
        return out
    cov = coverage.get('parts') or {}
    counts = {pid: int((cov.get(pid) or {}).get('required') or 0) for pid in manuf}
    volumes = {pid: float(np.prod((features_by_part.get(pid) or {}).get('bbox', {}).get('size') or [0.0])) for pid in manuf}
    return out + main_part_small_findings(spec, frames, counts, volumes, wires) + part_scale_findings(spec, frames, counts, volumes, wires)


# ------------------------------------------------------------------ N1.8 calm grid
GRID_CODES = ('grid_misaligned',)
GRID_TOL_MM = 1.0            # N1.8: two geometry centers (x or y) count as a common axis up to this distance
GRID_MIN_BLOCKS = 3          # N1.8: applies to sheets with at least this many blocks (part and assembly blocks; note and table blocks do not count)
GRID_EDGE_TOL_MM = 2.0       # only metric grid_edges_aligned: block edges of the same kind are aligned up to this distance


def _grid_center(fr):
    g = _geom(fr)
    return ((g[0] + g[2]) / 2, (g[1] + g[3]) / 2) if g else None


def _grid_box(items):
    """Block box in sheet mm: envelope of the geometry and annotation boxes of its views (view_frames)."""
    boxes = []
    for fr in items:
        g = _geom(fr)
        if g is not None:
            boxes.append(g)
            if fr.get('ann_bbox_mm'):
                boxes.append([float(x) for x in fr['ann_bbox_mm']])
    return _hull(boxes) if boxes else None


def grid_findings(spec, frames):
    """grid_misaligned (warn, N1.8) and the marking of the view_frames: per block (part or 'asm', as _view_group) of sheets with at least GRID_MIN_BLOCKS blocks one finding
    if none of its views shares a geometry center (x with x or y with y, ±GRID_TOL_MM; view_frames geom_bbox_mm) with a view of another block of the same sheet;
    isometric and detail views do not count as axis partners on either side.
    Views without part assignment (notes, tables, remote assembly without parts) are no blocks. Writes into the frames of these sheets grid_block (block) and grid_lonely
    (bool); grid_metrics computes the metrics from them. Identical for remote specs."""
    out = []
    for si, sd in enumerate(spec.get('sheets') or []):
        blocks, partners = {}, {}
        for vd in sd.get('views') or []:
            fr, grp = frames.get(vd.get('id')), _view_group(vd)
            if fr is None or grp is None or _grid_center(fr) is None:
                continue
            blocks.setdefault(grp, []).append(fr)
            if _is_normal(vd):                    # isometric and detail views are no axis partners
                partners.setdefault(grp, []).append(fr)
        if len(blocks) < GRID_MIN_BLOCKS:
            continue
        centers = {g: [_grid_center(fr) for fr in partners.get(g, [])] for g in blocks}
        for g, items in blocks.items():
            others = [c for o, cs in centers.items() if o != g for c in cs]
            lonely = not any(abs(c[i] - o[i]) <= GRID_TOL_MM + 1e-9 for c in centers[g] for o in others for i in (0, 1))
            for fr in items:
                fr['grid_block'], fr['grid_lonely'] = g, lonely
            if lonely:
                out.append(_f('grid_misaligned', 'warn', si, _grid_box(items),
                              f'Block {g}: no view shares a geometry center (x or y, ±{GRID_TOL_MM:g} mm) with a view of another block; '
                              f'align blocks on a row or column axis', block=g))
    return out


def grid_metrics(frames):
    """Metrics for N1.8 from the view_frames that grid_findings marked (grid_block, grid_lonely; only sheets with at least GRID_MIN_BLOCKS blocks):
    - grid_lonely_share = blocks without axis partner ÷ blocks (target at most GRID_LONELY_TARGET);
    - grid_edges_aligned = share of the blocks of which at least one edge (left, bottom, right, top) is aligned up to GRID_EDGE_TOL_MM with the same edge of another block;
    - grid_gap_spread = standard deviation (mm) of the distances to the nearest neighbor blocks (right with row overlap, below with column overlap); None below two distances.
    Block boxes: envelope of geometry and annotation boxes. Edge alignment and gap spread are only metrics (not scored). None where nothing is measurable."""
    sheets = {}
    for fr in (frames or {}).values():
        if fr.get('grid_block') is not None and 'grid_lonely' in fr:
            sheets.setdefault(fr.get('sheet'), {}).setdefault(fr['grid_block'], []).append(fr)
    n = lonely = aligned = 0
    gaps = []
    for blocks in sheets.values():
        box = {g: _grid_box(items) for g, items in blocks.items()}
        box = {g: b for g, b in box.items() if b is not None}
        n += len(blocks)
        lonely += sum(1 for items in blocks.values() if items[0]['grid_lonely'])
        for g, b in box.items():
            if any(abs(b[i] - o[i]) <= GRID_EDGE_TOL_MM + 1e-9 for h, o in box.items() if h != g for i in range(4)):
                aligned += 1
            right = [o[0] - b[2] for h, o in box.items() if h != g and o[0] >= b[2] - 1e-9 and o[3] > b[1] and b[3] > o[1]]
            below = [b[1] - o[3] for h, o in box.items() if h != g and o[3] <= b[1] + 1e-9 and o[2] > b[0] and b[2] > o[0]]
            gaps += [min(x) for x in (right, below) if x]
    if not n:
        return {'grid_lonely_share': None, 'grid_edges_aligned': None, 'grid_gap_spread': None}
    mean = sum(gaps) / len(gaps) if gaps else 0.0
    return {'grid_lonely_share': round(lonely / n, 4), 'grid_edges_aligned': round(aligned / n, 4),
            'grid_gap_spread': round(math.sqrt(sum((x - mean) ** 2 for x in gaps) / len(gaps)), 3) if len(gaps) >= 2 else None}


# ------------------------------------------------------------------ N1.9 Assembly zone, N4.7 item numbers in few views
ZONE_CODES = ('assembly_between',)
ZONE_OVERLAP = ZONE_OVERLAP_MM    # N1.9: overlap (mm sheet) of the transverse extent from which a part view counts as a neighbor on one side of the assembly view


def _balloon_views(sd):
    return {a.get('view') for a in sd.get('annotations') or [] if a.get('type') == 'balloon' and a.get('view')}


def zone_side(box, z, overlap=None):
    """Side ('left', 'right', 'below', 'above') on which the box of a part view lies next to the box z of the assembly view: entirely beside it (edge ≤ edge) and in the transverse axis
    overlapped by at least `overlap` (default ZONE_OVERLAP); otherwise None (corner position). Pure function of the boxes [x0, y0, x1, y1], also used by the packer."""
    ov = ZONE_OVERLAP if overlap is None else overlap
    ox, oy = min(box[2], z[2]) - max(box[0], z[0]), min(box[3], z[3]) - max(box[1], z[1])
    if oy >= ov - 1e-9:
        if box[2] <= z[0] + 1e-9:
            return 'left'
        if box[0] >= z[2] - 1e-9:
            return 'right'
    if ox >= ov - 1e-9:
        if box[3] <= z[1] + 1e-9:
            return 'below'
        if box[1] >= z[3] - 1e-9:
            return 'above'
    return None


def zone_between(sides):
    """Violation (N1.9): part views on opposite sides (left and right or above and below); sides = sides from zone_side."""
    s = set(sides)
    return ('left' in s and 'right' in s) or ('above' in s and 'below' in s)


def assembly_zone_findings(spec, frames):
    """assembly_between (warn, N1.9): one finding per view Z with at least one balloon if dimensioned part views T (exactly one part in parts, dims_n > 0, view_frames) lie on opposite
    sides of Z (zone_side, boxes as _grid_box: geometry including annotations). Corner position (one side or overlap below ZONE_OVERLAP) is allowed. Identical for remote specs: there
    the assembly views carry no parts, Z is recognized via balloon."""
    out = []
    for si, sd in enumerate(spec.get('sheets') or []):
        zs = _balloon_views(sd)
        boxes = {}
        for vd in sd.get('views') or []:
            fr = frames.get(vd.get('id'))
            if fr is not None and fr.get('sheet', si) == si and _geom(fr) is not None:
                boxes[vd['id']] = (vd, _grid_box([fr]), fr)
        for vid in sorted(zs & set(boxes), key=lambda v: list(boxes).index(v)):
            z = boxes[vid][1]
            near = {}
            for tid, (vd, box, fr) in boxes.items():
                if tid in zs or len(vd.get('parts') or []) != 1 or not int(fr.get('dims_n') or 0):
                    continue
                side = zone_side(box, z)
                if side:
                    near.setdefault(side, []).append(tid)
            if zone_between(near):
                pairs = [(a, b) for a, b in (('left', 'right'), ('above', 'below')) if a in near and b in near]
                out.append(_f('assembly_between', 'warn', si, z,
                              f'View {vid} with item numbers lies between dimensioned part views ({"; ".join(f"{a}: {", ".join(near[a])} / {b}: {", ".join(near[b])}" for a, b in pairs)}): '
                              f'put the assembly in a zone of its own (corner position allowed)', view=vid, sides={k: sorted(v) for k, v in near.items()}))
    return out


def assembly_metrics(spec):
    """N4.7: balloon_views = views with at least one balloon; assembly_views = views with a balloon or with more than one part in parts (assembly, also without item number,
    e.g. the isometric view). Over all sheets; None without spec."""
    if spec is None:
        return {'balloon_views': None, 'assembly_views': None}
    nb = na = 0
    for sd in spec.get('sheets') or []:
        zs = _balloon_views(sd)
        nb += len(zs)
        na += len(zs | {vd.get('id') for vd in sd.get('views') or [] if len(vd.get('parts') or []) > 1})
    return {'balloon_views': nb, 'assembly_views': na}


# ------------------------------------------------------------------ N4.7 one item number per item, part title at the main view
BALLOON_CODES = ('balloon_repeated',)
TITLE_CODES = ('part_title_misplaced',)
PART_TITLE = re.compile(r'^\s*Item\s*(\d+(?:\s*,\s*\d+)*)(?![\d,])')
TITLE_OVERLAP = 0.5            # N4.7: a view stands under the title if the title box overlaps it horizontally by at least this share of the smaller width (title or view) (otherwise a long title reaches over neighbors; the planner sets the title left-aligned above its block)


def _pos_rows(spec):
    """{part: item (text)} from the parts list rows of all sheets."""
    return {r['part']: str(r.get('pos', '')) for sd in spec.get('sheets') or [] for r in sd.get('parts_list') or [] if r.get('part')}


def balloon_numbers(spec):
    """[(sheet, item number, balloon)] of all item numbers of the spec: `pos` of the annotation, otherwise the item of the part from the parts list (as Drawing.annotate);
    numbers without either are ignored."""
    rows, out = _pos_rows(spec), []
    for si, sd in enumerate(spec.get('sheets') or []):
        for a in sd.get('annotations') or []:
            if a.get('type') != 'balloon':
                continue
            num = str(a.get('pos') or rows.get(a.get('part')) or '').strip()
            if num:
                out.append((si, num, a))
    return out


def balloon_groups(numbers):
    """{number: [key, …]} from pairs (key, number) in input order. Pure function: the planner (plan._balloons) sets exactly one item number per number
    (one key per group), the checker (balloon_repeat_findings) reports every number with more than one entry."""
    out = {}
    for key, num in numbers:
        out.setdefault(str(num), []).append(key)
    return out


def balloon_repeat_findings(spec):
    """balloon_repeated (warn, N4.7, DIN ISO 6433): one finding per item number that appears more than once on the sheet (over all sheets); identical parts carry one number,
    the quantity is in the parts list. Identical for remote specs."""
    out = []
    for num, items in balloon_groups([((si, a), num) for si, num, a in balloon_numbers(spec)]).items():
        if len(items) > 1:
            parts = sorted({str(a.get('part')) for _, a in items if a.get('part')})
            out.append(_f('balloon_repeated', 'warn', items[0][0], None,
                          f'Item number {num} appears {len(items)}x on the sheet{(" (parts " + ", ".join(parts) + ")") if parts else ""}: '
                          f'identical parts carry one item number, stated once, the quantity is in the parts list',
                          number=num, count=len(items), **({'part': parts[0]} if parts else {})))
    return out


def part_main_view(views):
    """Main view of a part: the first view in order that is neither a detail view nor a section nor an auxiliary view; if there is none, the first. Views as spec or
    spec dicts (detail, section, auxiliary); None without views. Pure function on spec views: the planner (plan._title_block) passes its views through plan._spec_view beforehand and
    thus decides on the same fields that are in the spec. The check part_title_findings does not call it; it demands less (any view of the part under the title, see there)."""
    views = list(views)
    return next((v for v in views if not v.get('detail') and not v.get('section') and not v.get('auxiliary')), views[0] if views else None)


def part_title_box(a):
    """Box [x0, y0, x1, y1] of a part title (text annotation with at_sheet, baseline left); width from the font, at most max_width."""
    x, y, h = float(a['at_sheet'][0]), float(a['at_sheet'][1]), float(a.get('h') or 5.0)
    try:
        from plan_layout import text_width
        w = text_width(a.get('text', ''), h)
    except Exception:
        w = 0.6 * h * len(str(a.get('text', '')))
    if a.get('max_width'):
        w = min(w, float(a['max_width']))
    return [x, y, x + w, y + h]


def title_under(title, boxes):
    """Keys of the boxes ({key: [x0, y0, x1, y1]}) that stand under the part title: horizontal overlap of the title box at least TITLE_OVERLAP of the smaller width and top edge of the box
    below the baseline of the title (y0 of the box < baseline). A title stands above what it names; boxes beside or above it do not belong to it. Pure function of the boxes."""
    return [k for k, x in boxes.items()
            if min(title[2], x[2]) - max(title[0], x[0]) >= TITLE_OVERLAP * min(title[2] - title[0], x[2] - x[0]) - 1e-9 and x[1] < title[1]]


def title_misplaced(title, own, foreign):
    """The part title does not stand above the part (one decision for planner and checker): no view of the part (own: boxes of its views) stands under it (title_under). Returns: the
    key of the foreign view (foreign: boxes of foreign views) that stands under the title (the one with the smallest vertical distance), '' if nothing at all stands under the title, None if the title is
    fine. Pure function of the boxes."""
    if title_under(title, own):
        return None
    under = title_under(title, foreign)
    return min(under, key=lambda k: title[1] - foreign[k][3]) if under else ''


def part_title_findings(spec, frames):
    """part_title_misplaced (warn, N4.7): per part title "Item n designation (scale)" (text annotation with at_sheet; "Item 2, 3 …" names several items) one finding if no view of the named
    parts (views with exactly this part in parts) stands under the title (title_under: box = geometry including annotations according to view_frames, _grid_box); if a foreign view (other part, assembly)
    stands below it, the finding names it, if nothing stands below, "above no view". The planner sets the title above the block with the main view (plan._title_block, part_main_view on the spec views),
    the check demands less (any view of the part): a title above a split-off detail view of the same part is not objected to, because planner blocks consist of several freely placed
    views and the spec does not carry the block boundary. Identical for remote specs."""
    out, pos2parts = [], {}
    for pid, num in _pos_rows(spec).items():
        pos2parts.setdefault(num, []).append(pid)
    for si, sd in enumerate(spec.get('sheets') or []):
        boxes = {}
        for vd in sd.get('views') or []:
            fr = frames.get(vd.get('id'))
            if fr is not None and fr.get('sheet', si) == si and _geom(fr) is not None:
                boxes[vd['id']] = (vd, _grid_box([fr]))
        for a in sd.get('annotations') or []:
            m = PART_TITLE.match(str(a.get('text') or '')) if a.get('type') == 'text' and a.get('at_sheet') else None
            if not m:
                continue
            pids = {pid for n in re.split(r'\s*,\s*', m.group(1)) for pid in pos2parts.get(n, [])}
            own = {k: b for k, (vd, b) in boxes.items() if (vd.get('parts') or []) and set(vd['parts']) <= pids}
            if not own:
                continue
            t = part_title_box(a)
            f = title_misplaced(t, own, {k: b for k, (vd, b) in boxes.items() if k not in own})
            if f is not None:
                out.append(_f('part_title_misplaced', 'warn', si, t,
                              f'Part title “{a.get("text")}” stands ' + (f'above view {f} of another part or of the assembly' if f else 'above no view')
                              + ', not above a view of the part: put the title above the block of the main view', title=a.get('text'), view=f or None))
    return out


# ------------------------------------------------------------------ N4.9 Pattern detail, N4.4 detail view scale
PATTERN_CODES = ('pattern_not_detailed',)
PATTERN_MIN_MM = _PATTERN_MIN_MM    # N4.9: characteristic distance of a plane pattern at least this large on paper, otherwise the pattern needs a detail view


def pattern_groups(items):
    """Pattern groups of the plane sequences (kind plane_pattern) in items: same part, same axis, same count and pitch (TOL) and interleaved coordinates (the offset of the first members to the
    first member of the group is smaller than the pitch; the two flanks of the same ribs are two sequences). Per group {part, axis_name, k, ids, coords (all members ascending), pitch, count, lo, hi,
    feat_len}: feat_len = characteristic distance = smallest distance of two coordinates modulo pitch (for two sequences min(offset, pitch − offset), for one sequence the pitch). Pure function, used by planner and checker."""
    out = []
    for x in items:
        if x.get('kind') != 'plane_pattern' or not x.get('coords'):
            continue
        g = next((g for g in out if g['part'] == x.get('part') and g['axis_name'] == x['axis_name'] and g['count'] == x['count']
                  and abs(g['pitch'] - x['pitch']) <= TOL and abs(x['first'] - g['first']) < g['pitch'] - TOL), None)
        if g is None:
            g = {'part': x.get('part'), 'axis_name': x['axis_name'], 'k': 'xyz'.index(x['axis_name']), 'ids': [], 'coords': [], 'pitch': float(x['pitch']), 'count': int(x['count']),
                 'first': float(x['first'])}
            out.append(g)
        g['ids'].append(x['id'])
        g['coords'] += [float(c) for c in x['coords']]
    for g in out:
        g['coords'] = sorted(g['coords'])
        g['lo'], g['hi'] = g['coords'][0], g['coords'][-1]
        pos = sorted({round((c - g['lo']) % g['pitch'], 4) for c in g['coords']})
        pos = [p for p in pos if p < g['pitch'] - TOL] or [0.0]
        gaps = [b - a for a, b in zip(pos, pos[1:])] + [pos[0] + g['pitch'] - pos[-1]]
        g['char_len'] = round(min(gaps), 4)
    return out


def chain_twins(items, k, part=None):
    """Twin rows for chains.derivable. If two or more plane sequences belong to one group (pattern_groups: same count and pitch, interleaved, e.g. the two flanks
    of the same ribs), they are shifted against each other: c'_i - c_i = c'_0 - c_0 (i ≥ 1; reference is the first sequence of the group). -> [(c_i, c'_i, c_0, c'_0)] for axis k (and part, if given).
    Shared by the checker (_chain_findings) and the planner (plan_dims.derivable_left)."""
    items = [x for x in items if x.get('kind') == 'plane_pattern' and x.get('coords') and (part is None or x.get('part') == part)]
    by = {x['id']: x for x in items}
    out = []
    for gr in pattern_groups(items):
        if gr['k'] != k or len(gr['ids']) < 2:
            continue
        ref = [float(c) for c in by[gr['ids'][0]]['coords']]
        for sid in gr['ids'][1:]:
            cs = [float(c) for c in by[sid]['coords']]
            if len(cs) != len(ref):
                continue
            out += [(ref[i], cs[i], ref[0], cs[0]) for i in range(1, len(ref))]
    return out


def pattern_findings(ev, spec, frames):
    """pattern_not_detailed (warn, N4.9, without assignment to an annotation): one finding per pattern group and view in which a length dimension along the pattern axis has both ends on
    pattern coordinates (the length "n× t (=L)" or the thickness) and where characteristic distance × view scale is smaller than PATTERN_MIN_MM. Dimensions in assembly views do not count."""
    out, seen = [], set()
    all_parts = [p['id'] for p in spec.get('parts') or []]
    defs = _view_defs(spec)
    for gr in pattern_groups(x for x in ev['items'].values() if x.get('part') in ev['manuf']):
        e = np.eye(3)[gr['k']]
        for L in ev['linear']:
            g = L['g']
            vid = g.get('view')
            if vid is None or gr['part'] not in L['parts'] or not _par(L['d'], e) or _assembly(g, all_parts) or (vid, tuple(gr['ids'])) in seen:
                continue
            pr = _pair(g, e)
            if pr is None or not all(any(abs(v - c) <= TOL for c in gr['coords']) for v in pr):
                continue
            fr = (frames or {}).get(vid) or {}
            k = float(fr['scale']) if fr.get('scale') else _paper_scale(next((sd for sd in spec.get('sheets') or [] if any(v.get('id') == vid for v in sd.get('views') or [])), {}), defs.get(vid))
            if gr['char_len'] * k >= PATTERN_MIN_MM - 1e-9:
                continue
            seen.add((vid, tuple(gr['ids'])))
            out.append(_f('pattern_not_detailed', 'warn', int(fr.get('sheet') or g.get('sheet') or 0), None,
                          f"View {vid} (part {gr['part']}): plane pattern {', '.join(gr['ids'])} ({gr['count']}× {fmt(gr['pitch'])} along {gr['axis_name']}) with characteristic distance {fmt(gr['char_len'])} mm is only "
                          f"{fmt(gr['char_len'] * k, 1)} mm large here (smaller than {fmt(PATTERN_MIN_MM, 1)} mm): dimension pitch and thickness in a detail view over the whole pattern envelope (N4.9)",
                          view=vid, part=gr['part'], features=list(gr['ids'])))
    return out


def detail_scale_steps(ev, spec, frames=None):
    """N4.4 (metric, no finding): per detail view {view: standard steps of the detail scale above the smallest step at which the characteristic distance of its content becomes at least PATTERN_MIN_MM large}.
    Characteristic distance of the content = smallest size of the form features (SMALL_KINDS: size; slanted face: smaller side; pattern group: characteristic distance) that are dimensioned in the detail view (annotations with tag in
    this view); if it carries no tagged annotation, those with position in the circle. Detail views without such content are missing.
    0 = smallest sufficient step, positive = larger than necessary, negative = characteristic distance stays below PATTERN_MIN_MM."""
    import projection as prj
    out = {}
    scales = sorted(SCALE_VALUES)
    items = [x for x in ev['items'].values() if x.get('part') in ev['manuf']]
    groups = pattern_groups(items)
    for sd in spec.get('sheets') or []:
        for vd in sd.get('views') or []:
            det = vd.get('detail')
            if not isinstance(det, dict):
                continue
            frame = prj.frame_of(vd)
            c = prj.project([det['center']], frame)[0]
            inside = lambda pts: bool(pts) and any(np.linalg.norm(q - c) <= float(det['radius']) + TOL for q in prj.project(pts, frame))
            parts = vd.get('parts') or [p['id'] for p in spec.get('parts') or []]
            tagged = {str(t) for a in sd.get('annotations') or [] if a.get('view') == vd.get('id') for t in [a.get('feature'), *(a.get('also') or []), *(a.get('features') or [])] if t}
            char_len = []
            for x in items:
                if x.get('part') not in parts or (tagged and x['id'] not in tagged):
                    continue
                if x['kind'] in SMALL_KINDS and x.get('size') and (x['id'] in tagged or inside([x[k] for k in ('start', 'end', 'center') if x.get(k) is not None])):
                    char_len.append(float(x['size']))
                elif x['kind'] == 'slant' and x.get('width') and x.get('length') and (x['id'] in tagged or (x.get('pt') is not None and inside([x['pt']]))):
                    char_len.append(min(float(x['width']), float(x['length'])))
            for gr in groups:
                bb = next((x['bbox'] for x in items if x['id'] == gr['ids'][0] and x.get('bbox')), None)
                if gr['part'] not in parts or bb is None:
                    continue
                if tagged:
                    if tagged & set(gr['ids']):
                        char_len.append(gr['char_len'])
                    continue
                ends = []
                for v in (gr['lo'], gr['hi']):
                    q = [(bb['min'][m] + bb['max'][m]) / 2 for m in range(3)]
                    q[gr['k']] = v
                    ends.append(q)
                if inside(ends):
                    char_len.append(gr['char_len'])
            if not char_len:
                continue
            k_det = (frames or {}).get(vd.get('id'), {}).get('scale') or _scale(det.get('scale') or vd.get('scale'))
            if not k_det:
                continue
            need = next((s for s in scales if min(char_len) * s >= PATTERN_MIN_MM - 1e-9), scales[-1])
            idx = lambda s: min(range(len(scales)), key=lambda i: abs(scales[i] - s))
            out[vd['id']] = idx(float(k_det)) - idx(need)
    return out


# ------------------------------------------------------------------ N4.8 replaceable view, N4.3 foreshortened dimension
REPLACEABLE_CODES = ('view_replaceable',)
FORESHORT_CODES = ('dim_foreshortened',)
SOFT_MM = 2.5               # N4.8 soft: characteristic distance of the element in the other view below this length on paper (plan_layout.X_MIN_MM)
Y_MAX_DM2, Y_MIN_ANNS = 40.0, 5   # N4.8 soft: density of the target view (annotations per dm²), only from Y_MIN_ANNS annotations (plan_layout.Y_MAX_DM2)
OVER_MM = 2.0               # centerline overhang in the density area (plan_layout.OVER)


def kenn_length(f):
    """Characteristic distance of a form feature (model mm) or None if unknown: size, for slanted faces the smaller side."""
    if f.get('size'):
        return float(f['size'])
    if f.get('kind') == 'slant' and f.get('width') and f.get('length'):
        return float(min(f['width'], f['length']))
    return None


def readable_in(f, N):
    """Does a view with viewing direction N carry the dimension of element f legibly (true length)? Slanted face only with normal perpendicular to N, cone only with the axis in the image plane, plane and plane pattern only as an edge (axis perpendicular to N); otherwise yes."""
    if f is not None and f.get('kind') == 'slant' and f.get('normal') is not None and abs(float(_u(f['normal']) @ N)) > 1e-3:
        return False
    if f is not None and f.get('kind') == 'cone' and f.get('axis') is not None and abs(float(_u(f['axis']) @ N)) > 1e-3:
        return False
    if f is not None and f.get('kind') in ('plane', 'plane_pattern') and f.get('axis_name') in ('x', 'y', 'z') and abs(float(N['xyz'.index(f['axis_name'])])) > 1e-3:
        return False
    return True


def view_weight(own, others, by_id, shows, normal_of, tight, unlinked=0):
    """(hard, soft) of the dimensions of a view (N4.8; shared by the planner, plan_views.prune_views, and the check, view_replaceable; OCP-free). own = {form feature ID: number of its dimensions} in this view,
    others = the other views of the part (without detail views), by_id = {ID: form feature}. shows(o, ID) -> does view o show the element, normal_of(o) -> viewing direction to the camera, tight(o, f, n) -> is o too tight after moving
    n dimensions of element f there (stacking budget, density, characteristic distance). Hard: no other view shows the element legibly (readable_in). Soft: one shows it, but all would be too tight. unlinked = dimensions without reference
    to a form feature: count as hard (they cannot be moved; this avoids false alarms, the measurement undercounts sheets without element reference such as remote ones)."""
    hard, soft = unlinked, 0
    for fid, n in own.items():
        f = by_id.get(fid)
        ok = [o for o in others if shows(o, fid) and readable_in(f, normal_of(o))]
        if not ok:
            hard += 1
        elif all(tight(o, f, n) for o in ok):
            soft += 1
    return hard, soft


def _part_view_sets(spec, ev):
    """Per sheet and manufactured part: [(sheet index, part, [views without detail view, in sheet order], annotations of the sheet)]; assembly views (more than one part) are missing."""
    all_parts = [p['id'] for p in spec.get('parts') or []]
    out = []
    for si, sd in enumerate(spec.get('sheets') or []):
        anns = [a for a in sd.get('annotations') or [] if isinstance(a, dict)]
        by_part = {}
        for v in sd.get('views') or []:
            ps = v.get('parts') or all_parts
            if v.get('id') is not None and len(ps) == 1 and ps[0] in ev['manuf'] and not v.get('detail'):
                by_part.setdefault(ps[0], []).append(v)
        out += [(si, pid, vs, anns) for pid, vs in by_part.items()]
    return out


def _cam(frames, vid):
    n = ((frames or {}).get(vid) or {}).get('axes', {}).get('toward_camera')
    return None if n is None else np.asarray(n, float)


def replaceable_findings(ev, spec, frames):
    """view_replaceable (warn, N4.8): the view of a manufactured part whose dimensions another view of the part also shows legibly, without the target view becoming too tight (view_weight: hard 0 and soft 0).
    Exempt: the first view of the part (anchor), isometric and other oblique views, host views (carry detail circle, section line or view arrow), assembly views, detail views and views without
    dimension (view_without_information). A view shows an element if it is visible from there (visible_from, like the planner layout); a section only the elements it dimensions itself (the checker does not know its
    section image); round elements (axis) only in views with the same position relative to the axis as the checked one (circle or section image), envelope dimensions (extent) and freeform regions not at all (planner as well). Dimensions without reference to a form feature (no feature/features) count as hard: sheets without element reference, above all remote ones, are thus undercounted. Without assignment to an annotation."""
    out = []
    items = ev['items']
    for si, pid, vs, anns in _part_view_sets(spec, ev):
        hosts = {a.get('view') for a in anns if a.get('type') in ('detail_circle', 'section_line', 'view_arrow')}
        info = {v['id']: [a for a in anns if a.get('view') == v['id'] and a.get('type') in BUDGET_TYPES and not bare_polyline(a)] for v in vs}
        own, unlinked, total, load = {}, {}, {}, {}
        for v in vs:
            o, u = {}, 0
            for a in info[v['id']]:
                tags = [a['feature']] if a.get('feature') else list(a.get('features') or ())
                u += 0 if tags else 1
                for t in tags:
                    o[t] = o.get(t, 0) + 1
            own[v['id']], unlinked[v['id']] = o, u
            total[v['id']] = sum(1 for a in anns if a.get('view') == v['id'] and informative_annotation(a))
            load[v['id']] = len(info[v['id']])
        axis_views = [v for v in vs if _cam(frames, v['id']) is not None and (v.get('auxiliary') or abs(_cam(frames, v['id'])).max() > 1 - 1e-6)]
        anchor = vs[0] if vs else None
        for v in vs:
            if v is anchor or v not in axis_views or v['id'] in hosts or (v.get('auxiliary') and not v.get('section')) or (not own[v['id']] and not unlinked[v['id']]):
                continue
            others = [o for o in axis_views if o is not v]

            def shows(o, fid, own=own, v=v):
                x = items.get(fid)
                if x is None or x.get('kind') in ('extent', 'freeform_region', 'freeform_note', 'bend_table'):      # envelope dimension: needs a view with both ends visible (planner); freeform/table: no view choice
                    return False
                if x.get('axis') is not None and x.get('kind') in ('hole', 'shaft', 'arc', 'fillet', 'cone', 'pattern'):      # round elements: same position relative to the viewing direction as in the view that carries them (circle or section image), not the other one
                    a, n0, n1 = _u(x['axis']), _cam(frames, v['id']), _cam(frames, o['id'])
                    cls = lambda n: 0 if abs(float(a @ n)) > 0.99 else 1 if abs(float(a @ n)) < 1e-3 else 2
                    if a is None or cls(n0) != cls(n1):
                        return False
                return fid in own[o['id']] if o.get('section') else _seen_from(x, _cam(frames, o['id']))

            def tight(o, f, n, total=total, load=load):
                fr = (frames or {}).get(o['id']) or {}
                cnt = total[o['id']] + n
                gb = fr.get('geom_bbox_mm')
                dens = 0.0
                if gb and cnt >= Y_MIN_ANNS:
                    area = ((gb[2] - gb[0] + 2 * OVER_MM) * (gb[3] - gb[1] + 2 * OVER_MM)) / 1e4
                    dens = cnt / area if area > 0 else 0.0
                k = float(fr['scale']) if fr.get('scale') else 1.0
                return load[o['id']] + n > BUDGET_VIEW or dens > Y_MAX_DM2 or (f is not None and kenn_length(f) is not None and kenn_length(f) * k < SOFT_MM)
            hard, soft = view_weight(own[v['id']], others, items, shows, lambda o: _cam(frames, o['id']), tight, unlinked[v['id']])
            if hard == 0 and soft == 0 and others:
                out.append(_f('view_replaceable', 'warn', si, None,
                              f"View {v['id']} (part {pid}) is replaceable: another view of the part also shows all {sum(own[v['id']].values())} dimensions legibly without becoming too tight; "
                              f"omit the view and place the dimensions there (N4.8)", view=v['id'], part=pid))
    return out


def _notch_profile_dim(x, a):
    """Is the length dimension a the width or depth of notch x (end points = the two opening points or opening center and tip)? Only then does it stand in the profile of the notch."""
    if not x or x.get('kind') != 'notch' or x.get('axis') is None or not x.get('open') or x.get('apex') is None or a.get('p1') is None or a.get('p2') is None:
        return False
    o0, o1, apex = (np.asarray(q, float) for q in (x['open'][0], x['open'][1], x['apex']))
    p1, p2 = np.asarray(a['p1'], float), np.asarray(a['p2'], float)
    same = lambda u, v: float(np.linalg.norm(u - v)) <= 0.02
    mid = (o0 + o1) / 2
    return (same(p1, o0) and same(p2, o1)) or (same(p1, o1) and same(p2, o0)) or (same(p1, mid) and same(p2, apex)) or (same(p1, apex) and same(p2, mid))


def foreshortened_findings(spec, frames, items=None):
    """dim_foreshortened (warn, N4.3, without assignment to an annotation): a length dimension without axis h/v (exception below: notch, step) whose 3D segment does not lie in the image plane (the core prints the projected length, not the true one), or an
    angle dimension whose plane does not lie in the image plane. Only standard and auxiliary views (dimensions in axonometric views are already reported by the core as dim_not_true_length); in the case aligned and for angles the
    core additionally reports the same cause as dim_not_true_length (block), here without blocking effect of rule N4.3. A dimension with axis h/v does not count (no error of the dimension, the planner sets it deliberately).
    Notches (N4.3, profile) only with feature tag: a length dimension on the element whose end points are the width (the two opening points) or depth (opening center to tip) of the notch stands in a view that does not
    look along the notch (items = ev['items'], without: not applicable). Steps not: their height and depth can also be read in the top view or side view, position dimensions with axis h/v (a point at another depth) are
    intended."""
    import projection as prj    # only in the render (OCP present), pure vector computation
    out = []
    defs = _view_defs(spec)
    for si, sd in enumerate(spec.get('sheets') or []):
        for a in sd.get('annotations') or []:
            if not isinstance(a, dict) or a.get('type') not in ('linear', 'angle'):
                continue
            vid = a.get('view')
            N = _cam(frames, vid) if vid else None
            if N is None or not (bool(np.isclose(np.abs(N).max(), 1.0)) or bool((defs.get(vid) or {}).get('auxiliary'))):
                continue
            if a['type'] == 'linear' and items is not None and _notch_profile_dim(items.get(a.get('feature')), a) and abs(float(_u(items[a['feature']]['axis']) @ N)) < 1 - 1e-3:
                out.append(_f('dim_foreshortened', 'warn', si, None,
                              f"Dimension{' ' + str(a['text']) if a.get('text') else ''} on {ART.get('notch', 'Notch')} {a['feature']} in view {vid}: width or depth of the notch stand in the profile, the view does not look along the notch; "
                              f"dimension in a view along the notch (N4.3)", view=vid))
                continue
            try:
                if a['type'] == 'linear':
                    if a.get('axis') in ('h', 'v'):
                        continue
                    d3 = np.asarray(a['p2'], float) - np.asarray(a['p1'], float)
                    bad = abs(float(d3 @ N)) > 1e-3 * float(np.linalg.norm(d3)) + 1e-6
                else:
                    vx, p1, p2 = (np.asarray(a[k_], float) for k_ in ('vertex', 'p1', 'p2'))
                    nrm = np.cross(p1 - vx, p2 - vx)
                    bad = float(np.linalg.norm(nrm)) > 1e-9 and abs(float(prj.unit(nrm) @ N)) < 1 - 1e-4
            except (KeyError, TypeError, ValueError):
                continue
            if bad:
                what = 'Length dimension' if a['type'] == 'linear' else 'Angle dimension'
                out.append(_f('dim_foreshortened', 'warn', si, None,
                              f"{what} in view {vid}{' (' + str(a['text']) + ')' if a.get('text') else ''}: the segment does not lie in the image plane, the projected length or a distorted angle is printed; "
                              f"dimension in a view in which it appears true (N4.3)", view=vid))
    return out


# ------------------------------------------------------------------ N1.7 projection relation exact
PROJECTION_CODES = ('projection_arrangement', 'projection_misaligned')
PROJ_NORMAL_TOL = 1e-6       # viewing direction is a normal direction (±X, ±Y, ±Z) up to this deviation


def _proj_axes(fr):
    ax = fr.get('axes') or {}
    if not all(k in ax for k in ('x', 'y', 'toward_camera')) or not fr.get('origin_mm') or not fr.get('scale'):
        return None
    return np.array(ax['toward_camera'], float), np.array(ax['x'], float), np.array(ax['y'], float)


def _proj_related(a, b):
    """Projection-related (N1.7): same parts, same scale, orthogonal viewing directions (view_frames axes). -> (Na, Vxa, Vya, Nb, Vxb, Vyb) or None."""
    if tuple(sorted(a['parts'])) != tuple(sorted(b['parts'])) or a['fr'].get('sheet') != b['fr'].get('sheet'):
        return None
    ka, kb = float(a['fr']['scale']), float(b['fr']['scale'])
    if abs(ka - kb) > 1e-6 * max(ka, kb):
        return None
    if abs(float(a['ax'][0] @ b['ax'][0])) > 1e-3:
        return None
    return a['ax'] + b['ax']


def _proj_pair(a, b, axis):
    """Offset of two neighboring views on the axis axis (0 = side by side measured in x, stacked; 1 = y): (kind, offset_mm) with kind 'ok'|'offset'|'mirrored'|'rotated'.
    The common model axis c = Na x Nb lies in both image planes; it must lie on the stacking-direction axis in both views (image x when stacked,
    image y when side by side) and run in the same direction. What is measured is the sheet coordinate transverse to the stacking direction of the same model point (center of the geometry of a)."""
    rel = _proj_related(a, b)
    if rel is None:
        return None
    Na, Vxa, Vya, Nb, Vxb, Vyb = rel
    c = np.cross(Na, Nb)
    c = c / np.linalg.norm(c)
    ua, ub = (Vxa, Vxb) if axis == 0 else (Vya, Vyb)
    sa, sb = float(c @ ua), float(c @ ub)
    if abs(abs(sa) - 1) > 1e-3 or abs(abs(sb) - 1) > 1e-3:
        return 'rotated', None
    k = float(a['fr']['scale'])
    oa, ob = float(a['fr']['origin_mm'][axis]), float(b['fr']['origin_mm'][axis])
    ga = _geom(a['fr'])
    t = (((ga[axis] + ga[axis + 2]) / 2) - oa) / (k * sa)            # coordinate of the geometry center of a along c (model mm)
    if sa * sb < 0:                        # opposite direction: only the geometry center of a is aligned by chance; the ends of the part are apart by its extent
        return 'mirrored', ga[axis + 2] - ga[axis]
    off = abs((ob + k * sb * t) - (oa + k * sa * t))
    return ('offset' if off > PROJ_TOL_MM + 1e-9 else 'ok'), off


def _proj_views(spec, frames):
    """[(sheet, [view])] with frames and axes {id, parts, fr, ax, vd}, without detail views (own scale); isometric views are excluded by the orthogonality in _proj_related."""
    out = []
    for si, sd in enumerate(spec.get('sheets') or []):
        views = []
        for vd in sd.get('views') or []:
            fr = frames.get(vd.get('id'))
            ax = _proj_axes(fr) if fr else None
            if fr is None or ax is None or _geom(fr) is None or vd.get('detail'):
                continue
            views.append({'id': vd.get('id'), 'parts': list(vd.get('parts') or []), 'fr': fr, 'ax': ax, 'vd': vd})
        out.append((si, views))
    return out


def projection_pairs(spec, frames):
    """[(sheet, a, b, axis, kind, offset_mm)] of the neighbor pairs of projection-related views: per view the nearest projection-related one below (overlapping
    x ranges of the geometry, axis 0) and the nearest one to the right (overlapping y ranges, axis 1)."""
    out = []
    for si, views in _proj_views(spec, frames):
        by_id = {v['id']: v for v in views}
        checked = set()
        # A narrow, eccentric section can belong on the rail in the model,
        # even if the visible outlines of the two views do not overlap.
        for b in views:
            place = b['vd'].get('place') or {}
            a = by_id.get(place.get('relative_to'))
            if a is not None:
                axis = 1 if place.get('side', 'below') in ('left', 'right') else 0
                pair = _proj_pair(a, b, axis)
                if pair is not None:
                    out.append((si, a, b, axis, *pair))
                    checked.add((frozenset((a['id'], b['id'])), axis))
        for a in views:
            ga = _geom(a['fr'])
            for axis, below in ((0, True), (1, False)):
                best = None
                for b in views:
                    if b is a or _proj_related(a, b) is None:
                        continue
                    gb = _geom(b['fr'])
                    if axis == 0:
                        if not (ga[0] < gb[2] and gb[0] < ga[2]) or not gb[3] <= ga[1] + 1e-6:
                            continue
                        gap = ga[1] - gb[3]
                    else:
                        if not (ga[1] < gb[3] and gb[1] < ga[3]) or not gb[0] >= ga[2] - 1e-6:
                            continue
                        gap = gb[0] - ga[2]
                    if best is None or gap < best[0]:
                        best = (gap, b)
                if best is not None and (frozenset((a['id'], best[1]['id'])), axis) not in checked:
                    r = _proj_pair(a, best[1], axis)
                    if r is not None:
                        out.append((si, a, best[1], axis, r[0], r[1]))
    return out


def projection_findings(spec, frames):
    """projection_misaligned (warn, N1.7): two views of the same part with the same scale and orthogonal viewing directions that lie as neighbors (stacked with overlapping
    x ranges, side by side with overlapping y ranges) are not aligned: the same model point lies apart transverse to the stacking direction by more than PROJ_TOL_MM (kind 'offset'),
    the common axis runs in the opposite direction (kind 'mirrored', offset_mm = extent of the part on this axis). Neighbors without a common stacking axis (kind 'rotated', a section beside a view from another direction) are
    no projection neighbors: no finding, only the metric projection_rotated. From spec and view_frames (origin_mm, scale, axes), identical for remote specs."""
    out = []
    for si, a, b, axis, kind, off in projection_pairs(spec, frames):
        if kind in ('ok', 'rotated'):          # rotated: the common axis does not lie on the stacking axis, there is nothing to align (metric projection_rotated)
            continue
        what = {'offset': f'offset by {off or 0.0:.1f} mm (tolerance {PROJ_TOL_MM:g} mm)', 'mirrored': 'mirrored (the common axis runs in the opposite direction)',
                'rotated': 'rotated (the common axis does not lie on the stacking axis)'}[kind]
        where = 'stacked' if axis == 0 else 'side by side'
        out.append(_f('projection_misaligned', 'warn', si, _hull([_geom(a['fr']), _geom(b['fr'])]),
                      f"Views {a['id']} and {b['id']} are {where}, but not in projection relation: {what}; place the view on the projection line (method 1, ISO 5456-2)",
                      view=a['id'], views=[a['id'], b['id']], kind=kind, offset_mm=None if off is None else round(off, 3)))
    return out


def projection_metrics(spec, frames):
    """Metrics for N1.7: view_not_in_projection = views of a part (parts as a group) whose viewing direction is a normal direction (±X, ±Y, ±Z) orthogonal to that of the main view
    (first view of the parts) and that are placed freely (at, no place) instead of in projection position; projection_max_offset_mm = largest measured offset (None without a neighbor pair with a value);
    projection_rotated = neighbor pairs without a common stacking axis (no finding)."""
    import projection as prj
    n = 0
    for sd in spec.get('sheets') or []:
        main = {}
        for vd in sd.get('views') or []:
            if not isinstance(vd, dict) or vd.get('detail'):
                continue
            key = tuple(sorted(vd.get('parts') or []))
            try:
                N = prj.frame_of(vd)[0]
            except (KeyError, ValueError, TypeError):
                continue
            if key not in main:
                main[key] = N
                continue
            if 'place' in vd or 'at' not in vd or abs(float(np.abs(N).max()) - 1.0) > PROJ_NORMAL_TOL or abs(float(N @ main[key])) > PROJ_NORMAL_TOL:
                continue
            n += 1
    pairs = projection_pairs(spec, frames)
    offs = [off for _, _, _, _, _, off in pairs if off is not None]
    return {'view_not_in_projection': n, 'projection_max_offset_mm': round(max(offs), 3) if offs else None,
            'projection_rotated': sum(1 for p in pairs if p[4] == 'rotated')}


# ------------------------------------------------------------------ Form features: assignment, coverage, redundancy
# geo = internal 3D dimension geometry of an annotation (drawing.Drawing.annotate -> rec['_geo']):
#   {kind, view, text, parts (parts of the view), feature? (tag), sheet?, index?}
#   + linear {p1, p2, dir3 (measuring direction)} | diameter/radius {center, radius, normal (viewing direction of the view)}
# features_by_part = {part ID: {'features': [Feature], 'freeform': bool, 'bbox': analyze.bbox, 'regions': [freeform region]}}

TOL = 0.01                                       # mm: coordinates, positions, center plane
R_TOL = 0.005                                    # mm: radius of a size dimension against the form feature
SIZE_KINDS = ('diameter', 'radius', 'thread')    # size dimensions
LOCATED = ('hole', 'shaft', 'sphere', 'arc')     # need size and position (blind holes additionally depth)
CYLINDERS = ('hole', 'shaft', 'arc')             # reference for "coaxial to a placed cylinder"
ART = {'cone': 'Cone', 'slant': 'Slanted face', 'hole': 'Hole', 'shaft': 'Shaft', 'plane': 'Plane', 'square': 'Square bar', 'sphere': 'Sphere', 'chamfer': 'Chamfer',
       'fillet': 'Fillet', 'arc': 'Arc', 'bend_table': 'Bend table', 'pattern': 'Pattern', 'thread': 'Thread',
       'knurl': 'Knurl', 'engraving': 'Engraving', 'extent': 'Bounding box dimension', 'freeform_note': 'Note', 'pocket': 'Pocket',
       'step': 'Step', 'slot': 'Slot', 'plane_pattern': 'Plane series', 'freeform_region': 'Freeform region', 'notch': 'Notch'}
ELEMENTS = ('pocket', 'step', 'slot', 'plane_pattern')   # composite form features (features.ELEMENTS), members: plane


def _u(v):
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else None


def _par(a, b):
    return abs(float(a @ b)) >= 1 - 1e-6


def _perp(a, b):
    return abs(float(a @ b)) <= 1e-3


def _axis(f):
    """(point, direction) of the axis of a hole/shaft/arc/fillet, (centre, None) of a sphere, otherwise (None, None)."""
    if f['kind'] in ('hole', 'shaft'):
        return np.asarray(f['start'], float), _u(f['axis'])
    if f['kind'] in ('arc', 'fillet'):
        return np.asarray(f['center'], float), _u(f['axis'])
    if f['kind'] == 'sphere':
        return np.asarray(f['center'], float), None
    return None, None


def mirrored_positions(members, k, mid):
    """Check the mirror position of the positional features, including the two coordinates in the center plane."""
    rows = defaultdict(list)
    for f in members:
        if f.get('start') is not None and f.get('end') is not None:
            c = (np.asarray(f['start']) + f['end']) / 2
        elif f.get('center') is not None:
            c = np.asarray(f['center'])
        else:
            return False
        rows[tuple(np.round(np.delete(c, k), 3))].append(c[k])
    if not any(any(abs(c-mid) > TOL for c in row) for row in rows.values()):
        return False
    for row in rows.values():
        ordered = sorted(row)
        if any(abs(a+b-2*mid) > TOL for a, b in zip(ordered, reversed(ordered))):
            return False
    return True


def _hits(geo, features, tol=TOL):
    """For each end point of a linear dimension the features on its coordinate along dir3: plane with normal parallel to dir3,
    axes perpendicular to dir3, sphere centres."""
    d = _u(geo['dir3']) if geo.get('dir3') is not None else None
    if d is None:
        return [[], []]
    out = []
    for p in (geo['p1'], geo['p2']):
        c = float(np.asarray(p, float) @ d)
        ids = []
        for f in features:
            if f['kind'] == 'plane':
                ok = _par(_u(f['normal']), d) and abs(float(np.asarray(f['pt'], float) @ d) - c) <= tol
            elif f['kind'] == 'slant':     # position of the slant = coordinate of its point along an axis to which its normal is inclined
                ok = abs(float(_u(f['normal']) @ d)) >= 0.5 and abs(float(np.asarray(f['pt'], float) @ d) - c) <= tol
            elif f['kind'] in ('hole', 'shaft', 'arc', 'fillet', 'sphere'):
                o, ax = _axis(f)
                ok = (ax is None or _perp(ax, d)) and abs(float(o @ d) - c) <= tol
            else:
                ok = False
            if ok:
                ids.append(f['id'])
        out.append(ids)
    return out


def assign(geo, features, tol=TOL):
    """IDs of the form features a dimension geometrically belongs to. Diameter/radius/thread: centre on the axis of a
    hole/shaft/arc/fillet or at the sphere centre, radius +-0.005; for hole/shaft additionally within [start, end]
    +- tol, so coaxial sections of equal diameter stay separate (if no section fits: the axis line). If the
    view looks along the axis (geo['normal']), the depth of the centre is undetermined in the drawing: then
    the dimension applies to all aligned sections (the planner tags). Linear: for each end point all features on its
    coordinate along dir3 (_hits)."""
    if geo.get('kind') == 'linear':
        return list(dict.fromkeys(i for ids in _hits(geo, features, tol) for i in ids))
    if geo.get('kind') not in SIZE_KINDS or geo.get('center') is None or geo.get('radius') is None:
        return []
    c, r = np.asarray(geo['center'], float), float(geo['radius'])
    n = _u(geo['normal']) if geo.get('normal') is not None else None
    on_line, inside = [], []
    for f in features:
        if f['kind'] not in ('hole', 'shaft', 'sphere', 'arc', 'fillet'):
            continue
        if abs((f['diameter'] / 2 if f['kind'] in ('hole', 'shaft', 'sphere') else f['radius']) - r) > R_TOL:
            continue
        o, ax = _axis(f)
        if (np.linalg.norm(c - o) if ax is None else np.linalg.norm(np.cross(c - o, ax))) > tol:
            continue
        on_line.append(f['id'])
        if f['kind'] in ('hole', 'shaft') and not (n is not None and abs(float(ax @ n)) > 0.99):
            t, length = float((c - o) @ ax), float((np.asarray(f['end'], float) - o) @ ax)
            if not min(0.0, length) - tol <= t <= max(0.0, length) + tol:
                continue
        inside.append(f['id'])
    return inside or on_line


def _aux(geo):
    """Auxiliary dimension: dimension text entirely in parentheses "(...)"; counts neither for coverage nor for redundancy and chains."""
    s = str(geo.get('text') or '').strip()
    return s.startswith('(') and s.endswith(')')


def _pair(g, d):
    """Coordinates of the end points along d, ascending; None for length 0."""
    a, b = sorted((float(np.asarray(g['p1'], float) @ d), float(np.asarray(g['p2'], float) @ d)))
    return (a, b) if b - a > TOL else None


class Chain:
    """N5.2: chain of the length dimensions of one direction - union-find over their end coordinates (equal up to tol). The one chain decision: chain_closed takes up the dimensions
    in order (link: True if the dimension closes a chain, i.e. follows from the previous ones), profile_stated (planner and checker) asks joined."""

    def __init__(self, pairs=(), tol=TOL):
        self.tol, self.nodes, self.parent = tol, [], []
        for a, b in pairs:
            self.link(a, b)

    def _node(self, c, add=True):
        for n, v in enumerate(self.nodes):
            if abs(v - c) <= self.tol:
                return n
        if not add:
            return None
        self.nodes.append(c); self.parent.append(len(self.parent))
        return len(self.nodes) - 1

    def _find(self, n):
        while self.parent[n] != n:
            self.parent[n] = self.parent[self.parent[n]]; n = self.parent[n]
        return n

    def link(self, a, b):
        """Take up segment a..b; True if a and b were already connected (the dimension follows from those taken up)."""
        x, y = self._find(self._node(a)), self._find(self._node(b))
        if x == y:
            return True
        self.parent[x] = y
        return False

    def joined(self, a, b):
        x, y = self._node(a, False), self._node(b, False)
        return x is not None and y is not None and self._find(x) == self._find(y)


def chain_determined(pairs, lo, hi, tol=TOL):
    """Is the segment lo..hi of a direction already fixed by the segments pairs [(a, b)] of the same direction (the same segment or a chain over shared
    end coordinates)? Pure function; the same computation as chain_closed (Chain)."""
    return Chain(pairs, tol).joined(lo, hi)


def sequence_arc(feats, po):
    """Rounding/arc (kind 'arc') of the same part whose R already dimensions the pitch circle of the sequence: axis ∥ rotation axis through the same center, radius equal (±0.011), single dimension (no
    collective note). Shared rule of planner (no second R) and checker (the radius is backed by this dimension). None if there is none (no second R)."""
    A, C = np.asarray(po['axis'], float), np.asarray(po['center'], float)
    for f in feats:
        if f['kind'] == 'arc' and f.get('axis') is not None and f.get('center') is not None and abs(float(np.asarray(f['axis'], float) @ A)) > 1 - 1e-6                 and abs(float(f['radius']) - float(po['radius'])) <= 0.011 and float(np.linalg.norm(np.cross(np.asarray(f['center'], float) - C, A))) <= 0.05                 and collective_family(f) is None and not f.get('major_radius'):
            return f
    return None


def _required(pid, pf):
    """Mandatory elements of a manufactured part: all form features, also cones/countersinks and slanted faces. A part with a freeform share
    stays mandatory for all analytic elements; in addition, for each freeform region (pf['regions']) a
    reference to the 3D model with position/envelope dimensions, the three bounding-box extents of the part and the note
    "Shape according to 3D model"."""
    feats = [f for f in pf['features'] if not f.get('drill_tip')]      # the drill tip belongs to the hole, not independently mandatory
    regions = [dict(r) for r in pf.get('regions') or []]     # facet regions (facet) exist even without a freeform share
    if not pf.get('freeform'):
        return feats + [r for r in regions if r.get('facet')]
    bb = pf['bbox']
    return (feats + regions
            + [{'id': f'{pid}#extent_{a}', 'part': pid, 'kind': 'extent', 'axis_name': a, 'size': bb['size'][k],
                'lo': bb['min'][k], 'hi': bb['max'][k]} for k, a in enumerate('xyz')]
            + [{'id': f'{pid}#freeform_note', 'part': pid, 'kind': 'freeform_note', 'size': None}])


def center_plane(f, feats, bb=None, tol=TOL):
    """The rotation element through whose center plane f passes and which thereby fixes it, or None. Sphere: the center lies in the plane. Hole/shaft
    (dimensioned body of revolution): the axis lies in the plane and the plane is the center plane of the part (bounding box center bb). Pure function for the planner (plan_dims: no position dimension of the plane)
    and checker (_evaluate: covered if the element is dimensioned and, for the sphere, a center cross stands at its center)."""
    if f.get('kind') != 'plane' or f.get('axis_name') not in ('x', 'y', 'z') or f.get('coord') is None:
        return None
    k, c = 'xyz'.index(f['axis_name']), float(f['coord'])
    same = [g for g in feats if g.get('part') == f.get('part') and g is not f]
    for g in same:
        if g.get('kind') == 'sphere' and g.get('center') is not None and abs(float(g['center'][k]) - c) <= tol:
            return g
    if bb is not None and abs((float(bb['min'][k]) + float(bb['max'][k])) / 2 - c) <= tol:
        for g in same:
            if g.get('kind') in ('hole', 'shaft') and g.get('axis') is not None and g.get('start') is not None:
                o, a = _axis(g)
                if a is not None and abs(float(a[k])) < 1e-6 and abs(float(o[k]) - c) <= tol:
                    return g
    return None


def required_count(pid, pf):
    """Number of mandatory elements of a manufactured part (= coverage.parts[pid].required); one count for planner (main part, N1.4) and checker."""
    return len(_required(pid, pf))


def _has_designation(text, designation):
    """Thread designation in the text, not as part of a longer number (M12 not in M120), decimal comma equal to point."""
    designation = str(designation or '').replace(',', '.').strip()
    return bool(designation) and re.search(r'(?<![\w.])' + re.escape(designation) + r'(?![\d.])', text.replace(',', '.')) is not None


def _spans(dirs, ax):
    """Do the directions fix the position: rank 2 perpendicular to the axis, rank 3 for spheres."""
    if not dirs:
        return False
    M = np.array(dirs, float)
    if ax is not None:
        M = M - np.outer(M @ ax, ax)
    return int(np.linalg.matrix_rank(M, tol=1e-3)) >= (2 if ax is not None else 3)


def part_axis(feats):
    """Common axis (point, direction) of a turned part, otherwise None. All axial mandatory elements (hole, shaft, arc)
    are coaxial (tolerance TOL), spheres lie on the axis, there are no compound form features and every plane transverse to the axis is an end face
    or part of a pair symmetric to the axis (width across flats); at least one shaft or two elements. Conservative: otherwise None."""
    ax_f = [f for f in feats if f['kind'] in ('hole', 'shaft', 'arc') and _axis(f)[1] is not None]
    if not ax_f or any(f['kind'] in ELEMENTS or f['kind'] == 'pattern' for f in feats):
        return None
    if len(ax_f) < 2 and ax_f[0]['kind'] != 'shaft':
        return None
    o, a = _axis(ax_f[0])
    for f in ax_f[1:]:
        of, af = _axis(f)
        if not _par(a, af) or np.linalg.norm(np.cross(of - o, a)) > TOL:
            return None
    for f in feats:
        if f['kind'] == 'sphere' and np.linalg.norm(np.cross(np.asarray(f['center'], float) - o, a)) > TOL:
            return None
        if f['kind'] == 'plane' and not _par(_u(f['normal']), a):
            n = _u(f['normal'])                  # side face (width across flats of a nut): only symmetric in pairs about the axis
            dist = lambda g: float((np.asarray(g['pt'], float) - o) @ _u(g['normal']))
            if not _perp(n, a) or not any(g['kind'] == 'plane' and g is not f and float(_u(g['normal']) @ n) <= -1 + 1e-6
                                          and abs(dist(g) - dist(f)) <= TOL for g in feats):
                return None
    return o, a


def coaxial(f, axis):
    """N4.1: does the form feature lie rotationally symmetric on the axis axis = (point, direction) from part_axis? Hole, shaft, arc, rounding (axis), sphere (center), cone
    (tip and axis), chamfer (circular edge: at), thread (start and end). A section through this axis exposes its profile (plan_views.part_views shows it there)."""
    if axis is None:
        return False
    o, a = np.asarray(axis[0], float), _u(axis[1])

    def on(p, d=None):
        if p is None or np.linalg.norm(np.cross(np.asarray(p, float) - o, a)) > TOL:
            return False
        return d is None or (_u(d) is not None and _par(_u(d), a))
    k = f.get('kind')
    if k in ('hole', 'shaft', 'arc', 'fillet', 'sphere') and (f.get('start') is not None or f.get('center') is not None):
        p, d = _axis(f)
        return on(p, d)
    if k == 'cone' and f.get('apex') is not None and f.get('axis') is not None:
        return on(f['apex'], f['axis'])
    if k == 'thread' and f.get('start') is not None and f.get('end') is not None:
        return on(f['start']) and on(f['end'])
    if isinstance(f.get('at'), dict) and f['at'].get('center') is not None and f['at'].get('axis') is not None:
        return on(f['at']['center'], f['at']['axis'])
    return False


def axis_section(point, normal, feats):
    """N4.1 (ISO 128-3): section through the rotation axis of a turned part - the cutting plane (point, normal = viewing direction of the section) contains the common axis from
    part_axis (freeform parts: pass feats empty). Its course is unambiguous: the section needs neither a section line nor label letters. One decision for the planner
    (plan_views.part_views: no label letter, no host) and checker (section_findings: no section line required)."""
    ax = part_axis(feats) if feats else None
    if ax is None or point is None or normal is None or _u(normal) is None:
        return False
    o, a = ax
    n = _u(normal)
    return abs(float(a @ n)) <= 1e-4 and abs(float((np.asarray(point, float) - o) @ n)) <= TOL      # axis in the plane (angle below 0.006°), axis point at most TOL beside it


def axis_section_view(vd, part_ids, feats_of):
    """Is the spec view vd an axial section (axis_section) of its only part? feats_of(part ID) -> {features, freeform} (drawing.Drawing.features) or None;
    without feats_of or on an error no (then the section line obligation applies as before)."""
    import projection as prj
    sec = vd.get('section')
    ps = vd.get('parts') or part_ids
    if feats_of is None or not isinstance(sec, dict) or len(ps) != 1:
        return False
    try:
        fe = feats_of(ps[0])
        if not fe:
            return False
        return axis_section(sec.get('point'), prj.frame_of(vd)[0], [] if fe.get('freeform') else fe.get('features') or [])
    except Exception:
        return False


def notes_text(note_lines, annotations):
    """Note text of the coverage (chamfers/radii, thread, knurl, engraving, freeform note): note lines and
    text annotations without block titles. Block title = text annotation with block_title (in the annotation record of the core
    '_block_title'), the heading "Item n designation (scale)" of a part block (plan.py): it names a part and
    is not a note - "knurled nut M16" would otherwise cover knurl and thread M16 via substrings. Likewise not notes
    are annotations with a coverage claim (features, model_ref): the coverage checks them by their form."""
    return '\n'.join([str(x) for x in note_lines] + [str(a['text']) for a in annotations if a.get('type') == 'text'
                                                     and not (a.get('_block_title') or a.get('block_title') or a.get('_claim'))
                                                     and a.get('features') is None and a.get('model_ref') is None])


# ------------------------------------------------------------------ Coverage kinds A/B/C
# A individually dimensioned; B collective note for a class of equal fillets/corner radii or chamfers: counted on an
# annotation with features [IDs] ("4x R2", "6x 0,5x45°") or general note as a note line object
# {text, general: {kind radius|chamfer, size R | [width, angle], part?}}, which covers all otherwise uncovered elements of exactly
# this size; C reference to the 3D model (model_ref [IDs] on an annotation or note line) only for
# freeform regions. The core passes note line objects in as geo {kind 'note', text, general?, model_ref?, line}.
ANG_TOL = 0.05                                   # degrees: chamfer angle of a collective note
COUNT_RX = re.compile(r'^\s*(\d+)\s*[×x]\s*')
CHAMFER_RX = re.compile(r'(\d+(?:[.,]\d+)?)\s*[×x]\s*(\d+(?:[.,]\d+)?)\s*°')
RADIUS_RX = re.compile(r'\bR\s*(\d+(?:[.,]\d+)?)')
BUDGET_VIEW = 17                                # at most this many entries per view (dimensions, leader lines; view labels and markings do not count)
BUDGET_TYPES = ('linear', 'diameter', 'radius', 'angle', 'thread', 'leader')
FREEFORM_RX = re.compile(r'shape(?: and undimensioned forms)? according to 3d model')      # matched against the casefolded text; one line may carry both notes


def _canon_axis(a):
    a = _u(a)
    i = int(np.argmax(np.abs(a) > 1e-9))
    return a if a[i] > 0 else -a


def equal_key(f, kind=None):
    """Key under which two form features are "equal" (n x): hole/shaft - kind, diameter, axis direction (up to sign), depth and
    blind; arc - radius and major radius; slant - inclination (kind 'angle': only that) or inclination, width and length (congruent). None otherwise."""
    k = f.get('kind')
    r3 = lambda v: round(float(v), 3) + 0.0
    if k in ('hole', 'shaft') and f.get('axis') is not None and f.get('start') is not None:
        depth = float(np.linalg.norm(np.asarray(f['end'], float) - np.asarray(f['start'], float)))
        return (k, r3(f['diameter']), *(r3(x) for x in _canon_axis(f['axis'])), round(depth, 2) + 0.0, bool(f.get('blind')))
    if k in ('arc', 'fillet') and f.get('radius') is not None:
        return (k, r3(f['radius']), r3(f.get('major_radius') or 0))
    if k == 'cone' and f.get('d1') is not None and f.get('d2') is not None and f.get('angle_deg') is not None and f.get('length') is not None:      # congruent cones
        return (k, r3(f['d1']), r3(f['d2']), round(float(f['angle_deg']), 1) + 0.0, round(float(f['length']), 2) + 0.0)
    if k == 'notch' and f.get('width') is not None and f.get('depth') is not None:      # congruent notches
        return (k, round(float(f['width']), 2) + 0.0, round(float(f['depth']), 2) + 0.0, round(float(f.get('angle_deg') or 0), 1) + 0.0)
    if k == 'slant' and f.get('angle_deg') is not None:
        a = round(float(f['angle_deg']), 1) + 0.0
        return (k, a) if kind == 'angle' else (k, a, round(float(f.get('width') or 0), 2) + 0.0, round(float(f.get('length') or 0), 2) + 0.0)
    return None


def _dim_value(g):
    """True length of a length dimension (distance of its end points), None without end points."""
    if g.get('p1') is None or g.get('p2') is None:
        return None
    return float(np.linalg.norm(np.asarray(g['p2'], float) - np.asarray(g['p1'], float)))


def slant_angle_key(x):
    """Shared key of the angle dimension of a slanted face: inclination and the magnitudes of the face normal (|n_x|, |n_y|, |n_z|). Thus parallel slanted faces of equal
    inclination and ones mirrored at world axis planes (ISO practice "2× 45°" on symmetric chamfers) are equal; width and length may differ. Normals rotated about an axis (72°: (-0.31; 0.95; 0) against
    (0; 0.95; 0.31)) have different keys. Width only as a fallback without normal. None if x is no slanted face with inclination."""
    if x.get('kind') != 'slant' or x.get('angle_deg') is None:
        return None
    a = round(float(x['angle_deg']), 1) + 0.0
    nrm = _u(x['normal']) if x.get('normal') is not None else None
    return ('slant', a, tuple(round(abs(float(v)), 2) + 0.0 for v in nrm)) if nrm is not None else ('slant', a, round(float(x.get('width') or 0), 2) + 0.0)


def slant_profile_length(x):
    """N5.1: true profile length of a slanted face whose normal is perpendicular to exactly one world axis a (extrusion along a): extent of the face transverse to a in its plane. Seen along
    a the face is a segment, its length is the diagonal of the bounding box without the a component - also for non-rectangular faces (pentagon with width
    43.25 and length 89.24, profile 50 like the opposite side). None without normal/bbox or for a doubly inclined face."""
    if x.get('kind') != 'slant' or x.get('normal') is None or not x.get('bbox'):
        return None
    n = _u(x['normal'])
    if n is None:
        return None
    axes = [k for k in range(3) if abs(float(n[k])) <= 1e-6]
    if len(axes) != 1:
        return None
    lo, hi = x['bbox']['min'], x['bbox']['max']
    return float(np.hypot(*[float(hi[k]) - float(lo[k]) for k in range(3) if k != axes[0]]))


def slant_profile_key(x, value):
    """Key of a length statement on a slanted face, from the element value: features.py sets width = min, length = max, the profile length is in one or the other depending on the slanted face;
    the dimension value only chooses which (tolerance 0.011, for two hits the nearer, for a tie the width), the key carries the value of the element: slanted faces with the same profile also have it
    in common when the dimension values differ by rounding noise (49.994 / 49.996). First the true profile length (slant_profile_length), then width/length as before.
    None if the value is neither profile length nor width nor length."""
    if x.get('kind') != 'slant' or x.get('angle_deg') is None or value is None:
        return None
    prof = slant_profile_length(x)
    if prof is not None and abs(prof - float(value)) <= 0.011:
        return ('slant', round(float(x['angle_deg']), 1) + 0.0, 'profile', round(prof, 2) + 0.0)
    near = [(abs(float(x[k]) - float(value)), n, float(x[k])) for n, k in enumerate(('width', 'length')) if x.get(k) is not None and abs(float(x[k]) - float(value)) <= 0.011]
    if not near:
        return None
    return ('slant', round(float(x['angle_deg']), 1) + 0.0, 'profile', round(min(near)[2], 2) + 0.0)


def copy_key(x, g):
    """Key under which dimension g on the form features x is "equal" (n× on congruent copies): angle on slanted faces - parallel with equal inclination; length dimension on slanted faces - same profile
    (from the dimension value), otherwise, and for all other elements, equal_key. Shared function of _evaluate (thus, collective_size_mismatch) and dimension_duplicate_copy."""
    kind = g.get('kind')
    if x.get('kind') == 'slant':
        if kind == 'angle':
            return slant_angle_key(x)
        if kind == 'linear' and (key := slant_profile_key(x, _dim_value(g))) is not None:
            return key
    return equal_key(x, kind)


FAMILY_WORD = {'radius': 'radii', 'chamfer': 'chamfers', 'cone': 'countersinks'}      # only countersinks (⌵Ø…×…°) without hole statement; shaft cones are dimensioned in the trapezoid, small ones in the minor-face row
FAMILIES = tuple(FAMILY_WORD)
# Cones/countersinks: one general note per size (its size is entirely in the text). Slanted faces (slant) can never be collected
PER_SIZE = ('cone',)
# Inclined plane faces with inclination < DRAFT_DEG to the nearest main direction (draft/print angles) are draft angles:
# one note line per part with the claim `draft` covers them, and only them (verification: inclination and part). Small threshold: a
# functional slant below 3° would otherwise appear only in the note.
DRAFT_DEG = 3.0
DRAFT_TEXT = 'Draft angles ≤ 3° according to 3D model'
# Minor faces = slanted faces (slant), free-standing cones (cone with free) and freeform/facet regions that are not functional
# faces: F1 carries an A element (carries: a hole opens into it), F2 angle/size hits a functional dimension of the model text, F3 area share
# (share) >= MINOR_SHARE. All others: exactly one note line per part MINOR_TEXT with the IDs (claim `minor`, IDs in model_ref).
# When in doubt A: if the area share (share) is missing, the face is never a minor face. Threshold F3 provisional (2 %).
# F2 for narrow faces below MINOR_SHARE/4 requires angle AND size from the text (a single match is coincidence there).
MINOR_SHARE = 0.02
MINOR_TEXT = 'Undimensioned forms according to 3D model'
MINOR_KINDS = ('slant', 'cone', 'freeform_region')
# A face (plane) is a minor face (row MINOR_TEXT) if it is no envelope face, no member of an element (member_of) and no square-bar side,
# carries no A element (carries), no model text names its dimension (partner distance, extents), its area share is < MINOR_PLANE_SHARE and a parallel, overlapping
# partner plane of the same part is at most MINOR_PLANE_PAIR x largest envelope edge away (slot, thin web, small step: features._plane_fields). If a statement is missing: A.
MINOR_PLANE_SHARE = 0.005   # measured basis (six models): the 24 planes of the mandatory list have 0.040-0.477 % (slot/notch flanks 0.5-1.5 mm); 0.6 % (test) stays A
MINOR_PLANE_PAIR = 0.03     # measured basis: the same 24 planes lie 0.3-1.5 mm from the partner (0.20-2.94 % of the largest envelope edge, one just below)
FUNC_TOL = 0.01          # mm: dimension of a face against a number of the model text
FUNC_ANG = 0.15          # degrees: angle of a face against a degree statement of the model text
ANGLE_RX = re.compile(r'(?<![\w.,])(\d+(?:[.,]\d+)?)\s*°')
CONE_RX = re.compile(r'(?:⌵|Cone)Ø(\d+(?:[.,]\d+)?)(?:→Ø(\d+(?:[.,]\d+)?))?[×x](\d+(?:[.,]\d+)?)°(?:/L(\d+(?:[.,]\d+)?))?')


def corner_arc(f):
    """Arc whose tangent planes lie transverse to the axis in both directions (corner radius)."""
    if f.get('kind') != 'arc' or f.get('axis') is None:
        return False
    return _spans([np.eye(3)['xyz'.index(a)] for a in f.get('tangent') or {} if a in ('x', 'y', 'z')], _u(f['axis']))


def function_numbers(texts):
    """(sizes, angles) from the model texts: all functional values (plan_facts.function_values) and all degree values "30°"."""
    import plan_facts
    nums = [float(i['value']) for i in plan_facts.function_values(texts)]
    angs = [float(m.group(1).replace(',', '.')) for t in texts or [] for m in ANGLE_RX.finditer(str(t or ''))]
    return nums, angs


FUNCTION_VALUES_KEY = 'function_values'       # sheet key (sheets[0]) {part ID: {nums, angles}} - the planner writes, the checker reads (spec_function_numbers)


def function_values_entry(texts):
    """Entry of a part for sheets[0][FUNCTION_VALUES_KEY] from its model texts: only numbers and angles (function_numbers), no texts; sorted, without duplicates."""
    nums, angs = function_numbers(texts)
    return {'nums': sorted({round(float(v), 4) for v in nums}), 'angles': sorted({round(float(v), 4) for v in angs})}


def spec_function_numbers(spec, pid):
    """The ONE reader of the function values (dimensions, angles) of a part for F2 (minor_ids, N4.10) and the N5.2 rank: the numbers of the spec texts (spec_texts)
    plus the numbers the planner passes along in the sheet (sheets[0].function_values[pid], its full model texts). If the key is missing (Claude path, old specs),
    the spec text number alone applies. Additional values only make fewer minor faces (minor_ids is monotonic)."""
    nums, angs = function_numbers(spec_texts(spec, pid))
    sheets = spec.get('sheets') or []
    entry = ((sheets[0].get(FUNCTION_VALUES_KEY) if sheets and isinstance(sheets[0], dict) else None) or {}).get(pid)
    if isinstance(entry, dict):
        num = lambda xs: [float(v) for v in xs or [] if isinstance(v, (int, float)) and not isinstance(v, bool)]
        nums, angs = nums + num(entry.get('nums')), angs + num(entry.get('angles'))
    return nums, angs


THREAD_NAMES_KEY = 'thread_designations'     # sheet key (sheets[0]) {part ID: [thread designations verbatim from the model texts]} - the planner writes, the render reads (spec_thread_designations)


def spec_thread_designations(spec, pid):
    """The ONE reader of the thread designations that the planner found in the full model texts of a part (features.text_designations); the render uses them to fill
    thread notes without designation with the same function as the planner (features.named_thread_hints), so the same notes, the same form feature cache key and
    the same form features arise. None without key or entry (Claude path, old specs): then every note stays as it is."""
    sheets = spec.get('sheets') or []
    entry = sheets[0].get(THREAD_NAMES_KEY) if sheets and isinstance(sheets[0], dict) else None
    got = entry.get(pid) if isinstance(entry, dict) else None
    if not isinstance(got, list):
        return None
    return [str(x) for x in got if isinstance(x, str)]


def minor_plane(f, elements, named):
    """Is face f a minor face? elements: IDs of the members of elements; named(v): does a model text name the dimension v (F2). In case of doubt (missing statement) A."""
    if f['id'] in elements or f.get('member_of') or f.get('square') or f.get('hull') is not False or f.get('carries') is not False:
        return False
    share, pair, rel = f.get('share'), f.get('pair'), f.get('pair_rel')
    if share is None or pair is None or rel is None or float(share) >= MINOR_PLANE_SHARE or float(rel) > MINOR_PLANE_PAIR + 1e-9:
        return False
    box = f.get('bbox') or {}
    ext = [float(h) - float(l) for l, h in zip(box.get('min') or [], box.get('max') or [])]
    return not any(named(v) for v in [pair, *ext] if v is not None and v > 0)


def minor_ids(texts, pf, values=None):
    """IDs of the minor faces of a manufactured part (pf: features + regions; texts: model texts for F2) - the faces that are neither
    A positive list nor F1/F2/F3 and may get a row MINOR_TEXT. Slanted faces with inclination/width/length,
    cones with Ø/length/angle that hit a function dimension of the text are F2; without area share (share) the face stays A.
    values: additional (dimensions, angles) beside the texts (spec_function_numbers); more values never give more minor faces."""
    nums, angs = function_numbers(texts)
    if values:
        nums, angs = nums + list(values[0]), angs + list(values[1])
    near = lambda v, vals, tol: v is not None and any(abs(float(v) - c) <= tol for c in vals)
    out = set()
    elements = {m for f in pf.get('features') or [] for m in f.get('members') or []}      # members of pockets, steps, grooves, plane sequences
    for f in list(pf.get('features') or []) + list(pf.get('regions') or []):
        k = f.get('kind')
        if k == 'plane':
            if minor_plane(f, elements, lambda v: near(v, nums, FUNC_TOL)):
                out.add(f['id'])
            continue
        if k not in MINOR_KINDS or (k == 'cone' and (f.get('drill_tip') or f.get('countersink') or (
                not f.get('free') and (f.get('pattern_member') or f.get('via_member') or f.get('share') is None
                                       or float(f['share']) >= MINOR_SHARE / 4)))):      # shaft cones below a quarter of the threshold are minor faces, not the members of a pattern (instance or chamfer); F2 always applies
            continue
        if f.get('share') is None or f.get('carries') or float(f['share']) >= MINOR_SHARE:      # F3, F1, missing value: A
            continue
        if k == 'slant':
            a = float(f.get('angle_deg') or 0.0)
            by_size = near(f.get('width'), nums, FUNC_TOL) or near(f.get('length'), nums, FUNC_TOL)
            by_angle = near(a, angs, FUNC_ANG) or near(90 - a, angs, FUNC_ANG)
            weak = bool(f.get('narrow')) and float(f['share']) < MINOR_SHARE / 4      # narrow and tiny: equal angle or size alone is no evidence of function (e.g. 26 facets 4x5/4x6 mm with the 35° inclination of a funnel and round sizes)
            f2 = (by_size and by_angle) if weak else (by_size or by_angle)
        elif k == 'cone':
            f2 = any(near(f.get(key), nums, FUNC_TOL) for key in ('d1', 'd2', 'length')) or near(f.get('angle_deg'), angs, FUNC_ANG)                 or near(f.get('half_angle_deg'), angs, FUNC_ANG)
        else:
            f2 = False
        if not f2:
            out.add(f['id'])
    return out


def spec_texts(spec, pid):
    """Model texts of a part for F2 (like the planner, plan.py): model name/purpose, part name, description, form feature names - as far as the
    spec carries them (the finished drawing spec often carries only the part name: then F2 checks less than the planner, never more)."""
    m = spec.get('model') or {}
    p = next((q for q in spec.get('parts') or [] if q.get('id') == pid), {})
    return [str(m.get('name') or ''), str(m.get('purpose') or ''), str(p.get('semantic_name') or p.get('name') or pid),
            str(p.get('description') or ''), *[str(t) for t in p.get('feature_names') or []]]


def countersinks_of(h, cones):
    """Countersinks (cone with countersink) of a hole h: same part, coaxial, hole diameter = narrow end of the countersink."""
    o, a = _axis(h)
    return [c for c in cones if c.get('kind') == 'cone' and c.get('countersink') and c.get('axis') is not None and c['part'] == h['part']
            and a is not None and abs(float(h['diameter']) - float(c['d1'])) <= 0.02 and _par(_u(c['axis']), a)
            and np.linalg.norm(np.cross(np.asarray(c['apex'], float) - o, a)) <= 0.05]


def transition_arc(f, minor):
    """A transition arc (features: trans) is only placed if none of its planar-face neighbors is a minor face (minor, no dimension of its own) - otherwise
    nothing determines the position (a corner slant in the row "Undimensioned forms" whose R would need its position)."""
    return bool(f.get('trans')) and not set(f.get('trans_nb') or []) & set(minor)


def mark_minor(texts, feats, regions, part=None):
    """(features, regions) with minor True on the minor faces (planner); the check computes minor_ids itself. Transition arcs at a minor face lose trans (transition_arc)."""
    ids = minor_ids(texts, {'features': feats, 'regions': regions})
    feats = [dict(f, minor=True) if f['id'] in ids else f for f in feats]
    return ([dict(f, trans=False) if f.get('trans') and not transition_arc(f, ids) else f for f in feats], [dict(r, minor=True) if r['id'] in ids else r for r in regions])


def is_draft(f):
    """Draft angle: slanted face with inclination below DRAFT_DEG."""
    return f.get('kind') == 'slant' and float(f.get('angle_deg') or 0.0) < DRAFT_DEG


END_CHAMFER_DEG = (15.0, 75.0)       # half angle of a short end cone that is geometrically a chamfer (like the chamfer window)


def end_chamfer(f):
    """Short end cone of a pattern member (peg tip/foot), geometrically a chamfer: cone with host body (not free-standing, no
    countersink, no drill tip), half angle in the chamfer window, axial length at most half the wide Ø, member of a pattern.
    Likewise every cone with chamfer_cone (features._mark_chamfer_cones: length <= 4 mm, on a plane or hole/shaft, also inner cones/nests).
    It stands as chamfer "a×α" (a axial length, α half angle to the axis) and follows the chamfer rule (collective statement/general statement)."""
    return (f.get('kind') == 'cone' and not f.get('countersink') and not f.get('head') and not f.get('drill_tip') and f.get('half_angle_deg') is not None
            and f.get('length') is not None and END_CHAMFER_DEG[0] <= float(f['half_angle_deg']) <= END_CHAMFER_DEG[1]
            and float(f['length']) <= float(f['d2']) / 2 + 1e-9
            and (bool(f.get('chamfer_cone')) or (not f.get('free') and bool(f.get('pattern_member') or f.get('via_member')))))      # also every short cone on a plane/hole (features.chamfer_cone)


OUTLINE_TOL = 0.05       # mm: axis point in the plane or in its face bbox


def axis_on_outline(f, feats, tol=OUTLINE_TOL):
    """(ISO 129-1): does the axis of a cone (tip + axis, point at mid-length) or of a partial cylinder (hole/shaft with angle range <= 200°) lie in a
    planar face of the part (plane/slant: normal ⟂ axis, axis point in the plane and in the face bbox)? -> outer normal of this face (unit vector), otherwise None. A Ø would have
    only half its dimension line in the material there (nest, channel): instead R or depth from the face. Axis in the material or in the cavity is no outline edge."""
    if f.get('kind') == 'cone' and f.get('apex') is not None and f.get('axis') is not None and f.get('half_angle_deg'):
        e = _u(f['axis'])
        t = np.tan(np.radians(float(f['half_angle_deg'])))
        P = np.asarray(f['apex'], float) + e * (float(f['d1']) + float(f['d2'])) / 4 / t
    elif f.get('kind') in ('hole', 'shaft') and f.get('start') is not None and f.get('axis') is not None and float(f.get('span_deg') or 360.0) <= 200.0:
        e = _u(f['axis'])
        P = (np.asarray(f['start'], float) + np.asarray(f['end'], float)) / 2
    else:
        return None
    if e is None:
        return None
    for g in feats:
        if g.get('kind') not in ('plane', 'slant') or g.get('normal') is None or g.get('pt') is None or not g.get('bbox'):
            continue
        n = _u(g['normal'])
        if n is None or abs(float(n @ e)) > 1e-3 or abs(float(n @ (P - np.asarray(g['pt'], float)))) > tol:
            continue
        lo, hi = np.asarray(g['bbox']['min'], float), np.asarray(g['bbox']['max'], float)
        if np.all(P >= lo - tol) and np.all(P <= hi + tol):
            return n
    return None


def collective_family(f):
    """'radius' (fillet, corner radius), 'chamfer' (chamfer, also the short end cone of a pattern member), 'cone' (cone, countersink) or
    None: A element, never a collective note (also the slanted face slant)."""
    k = f.get('kind')
    if end_chamfer(f):
        return 'chamfer'
    return 'radius' if k == 'fillet' or corner_arc(f) else k if k in ('chamfer', 'cone') and not f.get('drill_tip') else None


def collective_size(f):
    """Size of the class: radius; chamfer (width, angle); cone (wide diameter, cone angle, narrow diameter, length - None for a countersink, the
    hole diameter dimensions it); slanted face, individually (width, inclination, length)."""
    k = collective_family(f) or f.get('kind')
    if k == 'radius':
        return float(f['radius'])
    if k == 'chamfer' and f.get('kind') == 'cone':          # short end cone as chamfer (axial length, half angle)
        return (float(f['length']), float(f['half_angle_deg']))
    if k == 'cone':
        cs = f.get('countersink')
        return (float(f['d2']), float(f['angle_deg']), None if cs else float(f['d1']),
                None if cs or f.get('length') is None else float(f['length']))
    if k == 'slant':
        return (float(f['width']), float(f['angle_deg']), float(f['length']))
    return (float(f['width']), float(f['angle_deg']))


def norm_size(family, size):
    """Size of a class rounded (spec and text): radius, [width, angle], [diameter, angle, narrow diameter|None, length|None]."""
    if family == 'radius':
        return round(float(size), 4)
    out = [round(float(size[0]), 4), round(float(size[1]), 3)]
    if family == 'cone':
        rd = lambda v: None if v is None else round(float(v), 4)
        return out + [rd(size[2]), rd(size[3]) if len(size) > 3 else None]
    return out


def same_size(family, a, b):
    """Equal size of a class: radius or chamfer width +-R_TOL, chamfer angle +-ANG_TOL; cones additionally equal narrow diameter
    (both None = countersink); the cone length only if both sides state it (a spec without length stays valid)."""
    tol = R_TOL + 1e-6                   # text rounds to 2 places: 0,575 -> 0,57 lies exactly on R_TOL
    if family == 'radius':
        return abs(float(a) - float(b)) <= tol
    ok = abs(float(a[0]) - float(b[0])) <= tol and abs(float(a[1]) - float(b[1])) <= ANG_TOL + 1e-6
    if family == 'cone':
        ok = ok and (a[2] is None) == (b[2] is None) and (a[2] is None or abs(float(a[2]) - float(b[2])) <= tol)
        la, lb = (a[3] if len(a) > 3 else None), (b[3] if len(b) > 3 else None)
        ok = ok and (la is None or lb is None or abs(float(la) - float(lb)) <= tol)
    return ok


def _valid_size(family, size):
    try:
        if family == 'radius':
            return float(size) > 0
        if family == 'cone':
            return (len(size) in (3, 4) and float(size[0]) > 0 and float(size[1]) > 0 and (size[2] is None or float(size[2]) >= 0)
                    and (len(size) < 4 or size[3] is None or float(size[3]) >= 0))
        return len(size) == 2 and float(size[0]) > 0 and float(size[1]) > 0
    except (TypeError, ValueError):
        return False


def size_text(family, size):
    """"R2", "0,5×45°", "⌵Ø8×90°" (countersink), "ConeØ10→Ø5×30°/L12" (cone: wide diameter → narrow diameter x cone angle / length),
    "∠15°/B0,65/L12" (single slanted face: inclination/width/length); decimal comma, without spaces (the general note searches the text without spaces)."""
    if family == 'radius':
        return 'R' + fmt(float(size))
    if family == 'cone':
        ln = size[3] if len(size) > 3 else None
        return (f'⌵Ø{fmt(float(size[0]))}×{fmt(float(size[1]), 1)}°' if size[2] is None
                else f'ConeØ{fmt(float(size[0]))}→Ø{fmt(float(size[2]))}×{fmt(float(size[1]), 1)}°'
                + ('' if ln is None else f'/L{fmt(float(ln))}'))
    if family == 'slant':
        return f'∠{fmt(float(size[1]), 1)}°/B{fmt(float(size[0]))}/L{fmt(float(size[2]))}'
    return f'{fmt(float(size[0]))}×{fmt(float(size[1]))}°'


def general_text(family, size):
    """Text of a general note: "Undimensioned radii R2", "Undimensioned chamfers 0,5×45°"; '' without a valid specification."""
    if family not in FAMILY_WORD or not _valid_size(family, size):
        return ''
    return f'Undimensioned {FAMILY_WORD[family]} {size_text(family, size)}'


def note_text(line):
    """Text of a note line: string or object {text?, general?, model_ref?} (without text: general note)."""
    if isinstance(line, dict):
        gs = line.get('general') if isinstance(line.get('general'), list) else [line.get('general')]
        g = gs[0] if gs and isinstance(gs[0], dict) else {}
        return str(line.get('text') or general_text(g.get('kind'), g.get('size')))
    return str(line)


def _order(size):
    """Sort key of a size (None = -1)."""
    return tuple(-1.0 if v is None else v for v in size) if isinstance(size, tuple) else size


def size_classes(feats):
    """Classes of equal size per kind (deterministic): elements sorted by (size, ID), a new class starts
    as soon as the size no longer matches the first member (same_size). Returns [(kind, size of the first member, [elements])]."""
    out = []
    for family in FAMILIES:
        cur = None
        for f in sorted((f for f in feats if collective_family(f) == family), key=lambda f: (_order(collective_size(f)), f['id'])):
            s = collective_size(f)
            if cur is None or not same_size(family, cur[1], s):
                cur = (family, s, [])
                out.append(cur)
            cur[2].append(f)
    return out


def _counted_size(g):
    """(kind, size) of a counted collective note: R dimension from its radius, otherwise from the text (cone, slanted face, chamfer before radius)."""
    if g.get('kind') == 'radius' and g.get('radius') is not None:
        return 'radius', float(g['radius'])
    raw = str(g.get('text') or '')
    text = COUNT_RX.sub('', raw, count=1)
    num = lambda v: float(v.replace(',', '.'))
    m = CONE_RX.search(text.replace(' ', ''))
    if m:
        return 'cone', (num(m.group(1)), num(m.group(3)), num(m.group(2)) if m.group(2) else None, num(m.group(4)) if m.group(4) else None)
    m = CHAMFER_RX.search(text)
    if m:
        return 'chamfer', (num(m.group(1)), num(m.group(2)))
    m = RADIUS_RX.search(raw)
    return ('radius', num(m.group(1))) if m else (None, None)


def _tag_ok(tag, g, items):
    """Tag of a form feature in items that belongs to the parts of the dimension's view (geo['parts'], empty = all)."""
    return tag in items and (not g.get('parts') or items[tag].get('part') in g['parts'])


ASSIGN_KINDS = ('linear', 'diameter', 'radius', 'angle')       # dimension types for assigned_share (rule fulfilment)


def mirror_dimensions(spec, items, fl, manuf, linear, sym, frames=None):
    """Virtual mirror dimensions for the coverage. For every length dimension along world axis k that stands in a single-part view of a part with symmetry plane x_k = m of the whole part (sym),
    and whose symmetry axis is shown in this view (symmetry_axis_shown: the same predicate as N2.2/N5.2), the mirrored dimension (end points 2m - x) arises as {g, ids, hits, d, parts, virtual}:
    ids/hits = form features on the mirrored coordinates (_hits). No feature/also: the mirror dimension carries only position and extent, never a tag. Auxiliary dimensions "(…)" and dimensions in assembly views
    do not mirror (they never stand in linear); a dimension across the plane is its own mirror image and is dropped. Without sym: none."""
    if not sym or not linear:
        return []
    defs, lines = _view_defs(spec), _drawn_lines(spec)
    all_parts = [p['id'] for p in spec.get('parts') or []] or sorted({x['part'] for x in items.values()})
    cores, memo, out = {}, {}, []
    for L in linear:
        g = L['g']
        if g.get('p1') is None or g.get('p2') is None or g.get('view') is None or len(L['parts']) != 1 or _assembly(g, all_parts):
            continue                                                         # a dimension without a view shows no axis
        pid = next(iter(L['parts']))
        planes = _sym_planes(sym, pid)
        if pid not in manuf or not planes:
            continue
        for k, m in planes.items():
            e = np.eye(3)[k]
            pr = _pair(g, e) if _par(L['d'], e) else None
            if pr is None or abs(pr[0] + pr[1] - 2 * m) <= TOL:
                continue
            key = (pid, k, g.get('view'), bool(PATTERN_ID_RX.search(str(g.get('feature') or ''))))
            if key not in memo:
                if (pid, k) not in cores:
                    cores[(pid, k)] = _core_axis(items.values(), pid, k, m)
                memo[key] = symmetry_axis_shown(lines, k, m, {g.get('view')}, frames, cores[(pid, k)], defs, feature=g.get('feature'))
            if not memo[key]:
                continue
            mirror = lambda p: [(2 * m - float(c)) if j == k else float(c) for j, c in enumerate(np.asarray(p, float))]
            vg = {**{q: v for q, v in g.items() if q not in ('feature', 'also')}, 'p1': mirror(g['p1']), 'p2': mirror(g['p2']), '_virtual': True}
            pool = [f for f in fl if not g.get('parts') or f['part'] in g['parts']]
            hits = _hits(vg, pool)
            out.append({'g': vg, 'ids': [i for i in dict.fromkeys(hits[0] + hits[1]) if i in items], 'hits': hits, 'd': L['d'], 'parts': L['parts'], 'virtual': True})
    return out


def _evaluate(spec, features_by_part, geos, notes_text, tables, strict=False, sym=None, frames=None):
    """Assignment of all dimensions and coverage per mandatory element of the manufactured parts.
    Assignment: linear dimensions fix coordinates and apply to every form feature on their end point coordinates,
    a tag 'feature' is added; all other dimensions belong, with a tag, only to the tagged form feature, otherwise assign().
    Geometric assignment goes only to form features of the parts of the view (geo['parts'], empty = all); a tag counts only
    if it names a form feature of these parts (otherwise feature_tag_unknown). Part of a linear dimension (redundancy,
    chains) = parts of the assigned form features, without such the parts of the view.
    sym ({part: {world axis: coordinate}}, symmetry planes of the whole part) and frames (view_frames) enable virtual mirror dimensions (mirror_dimensions): a length dimension along k in a
    single-part view in which the symmetry axis is shown (symmetry_axis_shown) also covers its mirror images (2m - x). They feed only the coverage (by, linear inside), never the
    returned ev['by'] and ev['linear'] (findings dimension_redundant, chains): the findings see only the drawn dimensions."""
    by_id = {p['id']: p for p in spec.get('parts') or []}
    pfs = {pid: (v if isinstance(v, dict) else {'features': v}) for pid, v in features_by_part.items()}
    manuf = [pid for pid in pfs if part_role(by_id.get(pid, {})) == 'manufactured']
    feats = {f['id']: f for pid in manuf for f in pfs[pid]['features']}
    req = [x for pid in manuf for x in _required(pid, pfs[pid])]
    items = {**feats, **{x['id']: x for x in req}}
    fl = list(feats.values())
    minor = set().union(*[minor_ids([], pfs[pid], spec_function_numbers(spec, pid)) for pid in manuf]) & set(items) if manuf else set()    # model texts and sheet values via the one reader
    by, linear, also_problems = defaultdict(list), [], []
    dim_n = dim_assigned = 0                        # dimensions (linear/diameter/radius/angle) and of these those assigned to a form feature
    for g in geos:
        if _aux(g):
            continue
        pool = [f for f in fl if not g.get('parts') or f['part'] in g['parts']]
        tag = g.get('feature')
        claim = g.get('features') is not None or g.get('model_ref') is not None or g.get('kind') == 'note'   # B/C: not A
        hits = _hits(g, pool) if g.get('kind') == 'linear' else [[], []]
        ids = list(dict.fromkeys(hits[0] + hits[1])) if g.get('kind') == 'linear' else ([] if tag or claim else assign(g, pool))
        if tag and tag not in ids and _tag_ok(tag, g, items):
            ids.append(tag)
        also = [t for t in dict.fromkeys(str(x) for x in g.get('also') or []) if t != tag]
        if also:                                    # n x for equal elements: the count must be right, the members must be equal
            text, members = str(g.get('text') or ''), ([tag] if tag else []) + also
            m = COUNT_RX.match(text)
            if m is not None and int(m.group(1)) != len(members):
                also_problems.append(('collective_count_mismatch', g, f"Note “{text}” ({_claim_where(g)}) names {m.group(1)}×, but covers "
                                      f"{len(members)} elements", {}))
                also = []
            else:
                key = copy_key(items.get(tag) or {}, g)      # slanted face - angle parallel with equal inclination, length with the same profile (from the dimension value)
                bad = [t for t in also if key is not None and t in items and copy_key(items[t], g) != key]
                if bad:
                    also_problems.append(('collective_size_mismatch', g, f"Note “{text}” ({_claim_where(g)}) names {tag} and {', '.join(also)}, "
                                          f"but {', '.join(bad)} is not equal", {'features': bad}))
                also = [t for t in also if t not in bad]
        for t in also:                              # twin dimension or n x: also covers these form features
            if t not in ids and _tag_ok(t, g, items):
                ids.append(t)
        ids = [i for i in ids if i in items]
        if g.get('kind') in ASSIGN_KINDS:
            dim_n += 1
            dim_assigned += bool(ids)
        for i in ids:
            by[i].append(g)
        d = _u(g['dir3']) if g.get('kind') == 'linear' and g.get('dir3') is not None else None
        if d is not None:
            parts = ({items[i]['part'] for i in ids} or set(g.get('parts') or [])) & set(manuf)
            linear.append({'g': g, 'ids': ids, 'hits': hits, 'd': d, 'parts': parts})

    texts = '\n'.join([notes_text or ''] + [str(g.get('text') or '') for g in geos])
    notes = (notes_text or '').casefold()
    on_part = lambda g, pid: not g.get('parts') or pid in g['parts']
    shown_by, linear_drawn = by, linear         # findings see only drawn dimensions; the coverage below computes with by/linear including virtual mirror dimensions
    prof_err = None
    try:                                        # profiles with residual members: their residual members are covered via the profile; an error costs only this coverage path (the members stay in the minor-face row)
        prof_state = profile_state(spec, pfs, items, minor, geos, shown_by, frames, manuf)
    except Exception as e:
        prof_state, prof_err = {}, f'Profile check: {type(e).__name__}: {e}'[:300]
    prof_of = {m: pid_ for pid_, st_ in prof_state.items() if st_['buildable'] for m in st_['residual']}      # without a buildable view along d the minor-face path remains
    mirror_notes = [prof_err] if prof_err else []
    try:                                        # an error costs only the mirror dimensions (and gets a note), not the coverage
        mirrored = mirror_dimensions(spec, items, fl, manuf, linear, sym, frames)
    except Exception as e:
        mirrored = []
        mirror_notes.append(f'Mirror dimensions: {type(e).__name__}: {e}'[:300])
    if mirrored:
        by = defaultdict(list, {i: list(gs) for i, gs in by.items()})
        linear = linear + mirrored
        for V in mirrored:
            for i in V['ids']:
                by[i].append(V['g'])


    def sized(fid):
        """Size dimension on the element; for arc/fillet also a tagged leader line "R..." with its radius (fallback without axis view). An "n x R..." covers only with
        `also` (members of equal arcs, count and equality are checked by the n x check above: on deviation `also` is dropped); without `also` it does not count."""
        if any(g['kind'] in SIZE_KINDS for g in by[fid]):
            return True
        x = items.get(fid) or {}
        if x.get('kind') not in ('arc', 'fillet'):
            return False
        return any(g['kind'] == 'leader' and (m := RADIUS_RX.search(str(g.get('text') or ''))) is not None
                   and abs(float(m.group(1).replace(',', '.')) - float(x['radius'])) <= 0.005 and (not COUNT_RX.match(str(g.get('text') or '')) or bool(g.get('also')))
                   for g in by[fid])

    def states(f, text):
        """Text of a tagged dimension names the characteristic values (cone: wide diameter, cone angle, for a cone narrow diameter; slanted face:
        inclination and width); a dimension without text ("Ø" dimension) names only the diameter."""
        nums = [float(x.replace(',', '.')) for x in re.findall(r'\d+(?:[.,]\d+)?', text)]
        has = lambda v, tol=0.006: any(abs(n - v) <= tol for n in nums)       # text rounds to 2 places, (angles) 1 place
        size = collective_size(f)
        if f['kind'] == 'chamfer' or collective_family(f) == 'chamfer':     # size and angle (also the end cone as chamfer)
            return has(size[0]) and has(size[1], 0.06)
        return (has(size[0]) and has(size[1], 0.06) and (size[2] is None or has(size[2]))
                and (len(size) < 4 or size[3] is None or has(size[3])))

    def sunk(f):
        """Countersink (cone at the hole end) is covered by the dimension of its hole or of the hole's pattern if its text names it
        ("Ø5 ⌵Ø10x90°"): coaxial hole of the same part, hole diameter = narrow end of the countersink."""
        if f['kind'] != 'cone' or not f.get('countersink') or f.get('axis') is None:
            return False
        for h in fl:
            if h['kind'] != 'hole' or not countersinks_of(h, [f]):
                continue
            ids = [h['id']] + [q['id'] for q in fl if q['kind'] == 'pattern' and h['id'] in (q.get('members') or [])]
            if any(g['kind'] == 'diameter' and states(f, str(g.get('text') or '')) for i in ids for g in by[i]):
                return True
        return False

    def angle_value(g):
        v = g.get('value')
        if v is None:
            m = re.search(r'\d+(?:[.,]\d+)?', str(g.get('text') or ''))
            v = float(m.group(0).replace(',', '.')) if m else None
        return None if v is None else float(v)

    def angle_is(f, wants):
        """Angle dimension on element f with one of the values wants (tolerance 0.15 degrees: text rounds to one place)."""
        return any(g['kind'] == 'angle' and (v := angle_value(g)) is not None and any(abs(v - w) <= 0.15 for w in wants) for g in by[f['id']])

    def slant_angle_ok(f):
        """Angle dimension of the slant: inclination to the main plane or its supplementary angle (acute/obtuse depending on the reference edge, 90 +- inclination)."""
        a = float(f.get('angle_deg') or 0.0)
        if angle_is(f, (a, 180.0 - a, 90.0 - a, 90.0 + a)):
            return True
        # without a body edge as angle reference, the two extents of the edge (two tagged length dimensions) fix the slant
        return sum(1 for g in by[f['id']] if g['kind'] == 'linear' and (g.get('feature') == f['id'] or f['id'] in (g.get('also') or []))) >= 2      # n x of the twins counts too

    def cone_ok(f):
        """Shaft cone: both diameters (each as a diameter dimension with centre on the cone axis, also the dimension of the coaxial shaft) and the cone length as a length dimension
        between the two cone ends along the axis, or a diameter and the cone angle (full or half) without length."""
        if f.get('apex') is None or f.get('axis') is None or not f.get('half_angle_deg'):
            return False
        apex, e = np.asarray(f['apex'], float), _u(f['axis'])
        nest = axis_on_outline(f, fl) is not None      # axis on an outline edge: the depth from the face (length dimension perpendicular to the axis = R of the end) replaces the Ø
        tag = lambda g: g.get('feature') == f['id'] or f['id'] in (g.get('also') or [])
        dia = lambda d: d <= 1e-6 or any(g['kind'] == 'diameter' and g.get('center') is not None and g.get('radius') is not None
                                         and on_part(g, f['part']) and abs(float(g['radius']) - d / 2) <= R_TOL
                                         and np.linalg.norm(np.cross(np.asarray(g['center'], float) - apex, e)) <= 0.05
                                         for g in geos if not _aux(g)) or (nest and any(
                                         g['kind'] == 'linear' and tag(g) and g.get('p1') is not None and g.get('p2') is not None
                                         and abs(float(np.linalg.norm(np.asarray(g['p2'], float) - np.asarray(g['p1'], float))) - d / 2) <= 2 * R_TOL
                                         and abs(float(_u(np.asarray(g['p2'], float) - np.asarray(g['p1'], float)) @ e)) <= 1e-3
                                         for g in geos if not _aux(g)))
        d1, d2 = dia(float(f['d1'])), dia(float(f['d2']))
        ang = angle_is(f, (float(f['angle_deg']), float(f['half_angle_deg'])))
        t = np.tan(np.radians(float(f['half_angle_deg'])))
        lo, hi = sorted((float(apex @ e) + float(f['d1']) / 2 / t, float(apex @ e) + float(f['d2']) / 2 / t))
        length = any(on_part(L['g'], f['part']) and (
            (_par(L['d'], e) and (pr := _pair(L['g'], e)) is not None and abs(pr[0] - lo) <= TOL and abs(pr[1] - hi) <= TOL)
            or (tag(L['g']) and (own := _pair(L['g'], L['d'])) is not None and abs((own[1] - own[0]) - float(f['length'])) <= 2 * TOL))      # n× of a congruent copy (also) carries the length (in its own dimension direction)
                     for L in linear)
        return bool(d1 and d2 and length or (d1 or d2) and ang)      # Ø + cone angle cover without length (one end has no proven lateral point, no Ø there)

    def own(f):
        """A: own dimension (fillet/chamfer: any non-length dimension or tagged length dimension; arc: size dimension)."""
        if f['kind'] == 'arc':
            return sized(f['id'])
        if f['kind'] == 'slant':         # angle dimension and length dimension (one claim per dimension kind, no text)
            return slant_angle_ok(f) and any(g['kind'] == 'linear' for g in by[f['id']])
        if collective_family(f) == 'chamfer' and f['kind'] == 'cone':      # end cone as chamfer: the note names length and angle
            return any(g['kind'] != 'linear' and states(f, str(g.get('text') or '')) for g in by[f['id']])
        if f['kind'] in PER_SIZE:        # the dimension names the size of the countersink; shaft cone via both diameters (or diameter + angle) and length
            return cone_ok(f) or any(g['kind'] != 'linear' and states(f, str(g.get('text') or '')) for g in by[f['id']]) or sunk(f)
        if f['kind'] == 'chamfer':       # note/dimension names size and angle (a tagged length dimension counts as before)
            return any((g['kind'] == 'linear' and g.get('feature') == f['id']) or (g['kind'] != 'linear' and states(f, str(g.get('text') or '')))
                       for g in by[f['id']])
        return any(g['kind'] != 'linear' or g.get('feature') == f['id'] for g in by[f['id']])

    # counted collective notes (features), general notes (note line with general), references (model_ref)
    via_b, via_c, via_minor, minor_parts, generals, problems, drafts = set(), set(), set(), [], [], [], []
    problems.extend(also_problems)
    for g in geos:
        if _aux(g):
            continue
        text, here = str(g.get('text') or ''), _claim_where(g)
        if g.get('draft') is not None:           # draft angle note: names draft angles and 3D model, covers only inclination < DRAFT_DEG
            part = g['draft'].get('part') if isinstance(g['draft'], dict) else None
            if 'draft angle' in text.casefold() and '3d model' in text.casefold():
                drafts.append(part)
            else:
                problems.append(('collective_not_allowed', g, f"Draft angle note “{text}” ({here}) does not name “draft angles” and the "
                                 f"3D model", {}))
        ids_of = lambda key: list(dict.fromkeys(str(i) for i in (g[key] if isinstance(g[key], list) else [g[key]])))
        if g.get('features') is not None:
            ids = ids_of('features')
            known = [i for i in ids if _tag_ok(i, g, items)]
            family, size = _counted_size(g)
            m = COUNT_RX.match(text)
            if m is None or int(m.group(1)) != len(ids):
                problems.append(('collective_count_mismatch', g, f"Collective note “{text}” ({here}) names "
                                 f"{'no count' if m is None else m.group(1) + '×'}, but covers {len(ids)} elements", {}))
            barred = [i for i in known if collective_family(items[i]) is None or (family and collective_family(items[i]) != family)]
            if barred:
                problems.append(('collective_not_allowed', g, f"Collective note “{text}” ({here}) covers "
                                 f"{', '.join(f'{_label(items[i])} ({i})' for i in barred)}: collective notes only for equal "
                                 f"fillets/corner radii or chamfers, dimension all other form features individually", {'features': barred}))
            cand = [i for i in known if i not in barred]
            wrong = cand if family is None else [i for i in cand if not same_size(family, size, collective_size(items[i]))]
            if wrong:
                problems.append(('collective_size_mismatch', g, f"Collective note “{text}” ({here}) "
                                 f"{'names no size' if family is None else 'names ' + size_text(family, size)}, but "
                                 f"{', '.join(f'{_label(items[i])} ({i})' for i in wrong)}", {'features': wrong}))
            if m is not None and int(m.group(1)) == len(ids):          # a wrongly counting collective note does not cover its members
                via_b.update(i for i in cand if i not in wrong)
        if g.get('minor') is not None:           # one line per part "Undimensioned forms according to 3D model" with the IDs of the minor faces
            part = g['minor'].get('part') if isinstance(g['minor'], dict) else None
            known = [i for i in ids_of('model_ref') if _tag_ok(i, g, items)] if g.get('model_ref') is not None else []
            barred = [i for i in known if i not in minor]
            named = 'undimensioned forms'.casefold() in text.casefold() and '3d model' in text.casefold()
            if barred or not named or not known:
                why = ([f"{', '.join(f'{_label(items[i])} ({i})' for i in barred)} is a functional face (A element, F1 carries an A element, "
                        f"F2 functional dimension, F3 area share ≥ {MINOR_SHARE:.0%}) and must be dimensioned"] if barred else []) + (
                    [] if named else ['the note does not name “Undimensioned forms” and the 3D model']) + (
                    [] if known else ['it names no form feature (model_ref)'])
                problems.append(('model_ref_not_allowed', g, f"Line “{text}” ({here}): {'; '.join(why)}", {'features': barred}))
            dup = any(p is None or part is None or p == part for p in minor_parts)
            if dup:
                problems.append(('collective_not_allowed', g, f"Line “{text}” ({here}): at most one line “Undimensioned forms according to "
                                 f"3D model” per part", {}))
            minor_parts.append(part)
            if named and not (dup and strict):
                via_minor.update(i for i in known if i not in barred)
        elif g.get('model_ref') is not None:
            known = [i for i in ids_of('model_ref') if _tag_ok(i, g, items)]
            barred = [i for i in known if items[i]['kind'] not in ('freeform_note', 'freeform_region')]
            named = '3d model' in text.casefold()
            if barred or not named:
                why = ([f"{', '.join(f'{_label(items[i])} ({i})' for i in barred)} is analytic and must be dimensioned"] if barred else []) + (
                    [] if named else ['the note does not name the 3D model'])
                problems.append(('model_ref_not_allowed', g, f"Reference to the 3D model “{text}” ({here}): {'; '.join(why)}",
                                 {'features': barred}))
            if named:
                via_c.update(i for i in known if i not in barred)
        if g.get('general') is not None:
            # a note line may carry several general notes (list), one per size - cones/slanted faces of many sizes
            for gen in (g['general'] if isinstance(g['general'], list) else [g['general']]):
                gen = gen if isinstance(gen, dict) else {}
                family, size = gen.get('kind'), gen.get('size')
                if family not in FAMILY_WORD:
                    problems.append(('collective_not_allowed', g, f"General note “{text}” ({here}) for “{family}”: only fillets "
                                     f"(radius) and chamfers (chamfer)", {}))
                elif not _valid_size(family, size) or not re.search(r'(?<![\d,])' + re.escape(size_text(family, size)) + r'(?!\d|,\d)',
                                                                      text.replace(' ', '')):
                    problems.append(('collective_size_mismatch', g, f"General note “{text}” ({here}) does not name its size "
                                     f"{size_text(family, size) if _valid_size(family, size) else '(invalid)'}", {}))
                elif any(f == family and (p is None or gen.get('part') is None or p == gen.get('part'))
                         and (family not in PER_SIZE or same_size(family, sz, size)) for f, sz, p in generals):
                    problems.append(('collective_not_allowed', g, f"General note “{text}” ({here}): at most one general note "
                                     f"per kind and part, dimension further sizes individually or counted", {}))
                    if not strict:                                   # Claude path: covers as before, only a hint
                        generals.append((family, size, gen.get('part')))
                else:
                    generals.append((family, size, gen.get('part')))
    for family, size, part in generals:          # general note: all otherwise uncovered elements of exactly this class
        via_b.update(f['id'] for f in fl if collective_family(f) == family and (part is None or f['part'] == part)
                     and f['id'] not in via_b and not own(f) and same_size(family, size, collective_size(f)))

    def drafted(x):
        """Draft angle (inclination < DRAFT_DEG) of a part with a draft angle note."""
        return is_draft(x) and any(p is None or p == x['part'] for p in drafts)

    def mode(x):
        """Coverage kind of a covered mandatory element: C reference/freeform note, B collective or free note, otherwise A."""
        k, fid = x['kind'], x['id']
        if fid in prof_of:
            return 'A'
        if k in ('freeform_note', 'freeform_region') or fid in via_c or (fid in via_minor and not own(x)):
            return 'C'
        if k == 'slant':
            return 'B' if drafted(x) and not own(x) else 'A'
        if k in ('fillet', 'chamfer', 'cone'):
            return 'A' if own(x) else 'B'
        return 'B' if k == 'arc' and fid in via_b and not sized(fid) else 'A'

    def square_ok(sid):
        return any('□' in str(L['g'].get('text') or '') and (sid in L['ids'] or any(items[i].get('square') == sid for i in L['ids']))
                   for L in linear)

    def pattern_size(p):
        return any(sized(m) for m in p['members']) or sized(p['id'])

    def pattern_pitch(p):
        mem = set(p['members'])
        for d in p.get('dirs') or []:
            if d is None:
                continue
            d = _u(d)
            if not any(_par(L['d'], d) and (L['g'].get('feature') == p['id'] or (
                    set(L['hits'][0]) & mem and set(L['hits'][1]) & mem and _pair(L['g'], d) is not None)) for L in linear):
                comps = [k for k in range(3) if abs(float(d[k])) > 1e-6]      # oblique pitch via its axis-parallel components (one tagged length dimension each)
                def comp_ok(k):      # tagged, or the same segment first..last member already stands (same component of a second pattern, one dimension)
                    cs = [float(_axis(items[m])[0][k]) for m in p['members'] if m in items and _axis(items[m])[0] is not None]
                    return any(_par(L['d'], np.eye(3)[k]) and L['g'].get('feature') == p['id'] for L in linear) or (
                        bool(cs) and span_ok(items[p['members'][0]]['part'], k, min(cs), max(cs)))
                if len(comps) < 2 or not all(comp_ok(k) for k in comps):
                    return False
        return True

    def depth_ok(f):
        """Blind hole depth: linear dimension parallel to the axis with an end point on the bottom coordinate (end) or with a tag."""
        o, ax = _axis(f)
        c = float(np.asarray(f['end'], float) @ ax)
        return any(_par(L['d'], ax) and (L['g'].get('feature') == f['id'] or (on_part(L['g'], f['part']) and any(
            abs(float(np.asarray(p, float) @ ax) - c) <= TOL for p in (L['g']['p1'], L['g']['p2'])))) for L in linear)

    seqs = {}

    def polar_seq(p):
        """Recount of a polar/helical sequence from the geometry of its members (features.sequence_check, the same computation as the detection): (sequence, IDs).
        Members that the recount does not find in the sequence do not belong to it and remain individually mandatory (A)."""
        if p['id'] not in seqs:
            import features as ft
            mem = [items[m] for m in p['members'] if m in items and items[m].get('axis') is not None and items[m].get('start') is not None]
            seq, ids = ft.sequence_check(mem)
            seqs[p['id']] = (seq, set(ids))
        return seqs[p['id']]

    def polar_missing(p):
        """(ISO 129-1, equally distributed elements): what the sequence statement must still name after the recount: count n× Ø, pitch "k× step°" (k = number of steps, full circle
        = number of members), pitch circle radius (length center to member), start angle to the reference direction (except 0), for a helical sequence the axial offset first-last member."""
        seq, ids = polar_seq(p)
        if seq is None or len(ids) < 3 or p['members'][0] not in ids:
            return ['Sequence (recount)']
        gs, n_ids = by[p['id']], len(ids)
        want = seq['steps'] + 1 if seq['full'] else seq['steps']

        def pitch_stated(g):
            m = re.match(r'\s*(\d+)\s*[×x]\s*(\d+(?:[.,]\d+)?)\s*°', str(g.get('text') or ''))
            return g['kind'] == 'angle' and m is not None and int(m.group(1)) == want and abs(float(m.group(2).replace(',', '.')) - seq['step']) <= 0.06
        out = []
        if not any(g['kind'] == 'diameter' and (m := COUNT_RX.match(str(g.get('text') or ''))) is not None and int(m.group(1)) == n_ids for g in gs):
            out.append('count n× Ø')
        if not any(pitch_stated(g) for g in gs):
            out.append('pitch')
        arc = sequence_arc([items[m] for m in items if items[m].get('part') == p['part']], {'axis': seq['A'], 'center': seq['center'], 'radius': seq['radius']})
        if not any(g['kind'] == 'linear' and g.get('p1') is not None and abs(float(np.linalg.norm(np.asarray(g['p2'], float) - np.asarray(g['p1'], float))) - seq['radius']) <= 0.011 for g in gs)                 and not (arc is not None and sized(arc['id'])):          # the pitch circle radius stands as R of the equivalent arc
            out.append('pitch circle radius')
        w_ = min(seq['start'], 360.0 - seq['start'])
        if w_ > 0.05 and not any(g['kind'] == 'angle' and not pitch_stated(g) and (v := angle_value(g)) is not None and abs(v - w_) <= 0.15 for g in gs):
            out.append('start angle')
        if seq['rise'] != 0.0 and not any(
                g['kind'] == 'linear' and g.get('p1') is not None and _par(_u(g['dir3']), seq['A']) and abs(abs(float((np.asarray(g['p2'], float) - np.asarray(g['p1'], float)) @ seq['A'])) - abs(seq['rise']) * seq['steps']) <= 0.011
                for g in gs):
            out.append('axial offset')
        return out

    def pattern_depth(p):
        """Patterns of blind holes need a depth; one member suffices (all equally deep), a pattern tag too."""
        blind = [items[m] for m in p['members'] if m in items and items[m]['kind'] == 'hole' and items[m].get('blind')]
        if not blind:
            return True
        ax = _axis(blind[0])[1]
        return any(depth_ok(m) for m in blind) or any(_par(L['d'], ax) and L['g'].get('feature') == p['id'] for L in linear)

    # pocket/step/slot/plane series as a whole (characteristic dimensions + position); then their members (plane) count as covered
    def spans(part, k):
        e = np.eye(3)[k]
        return [pr for L in linear if _par(L['d'], e) and on_part(L['g'], part) for pr in [_pair(L['g'], e)] if pr]

    def span_ok(part, k, a, b):
        lo, hi = sorted((a, b))
        return any(abs(pr[0] - lo) <= TOL and abs(pr[1] - hi) <= TOL for pr in spans(part, k))

    elements = [x for x in req if x['kind'] in ELEMENTS and x.get('members') is not None]

    def pitch_direct(x):
        k, e = 'xyz'.index(x['axis_name']), np.eye(3)['xyz'.index(x['axis_name'])]
        return any(_par(L['d'], e) and on_part(L['g'], x['part']) and L['g'].get('feature') == x['id'] for L in linear) or span_ok(
            x['part'], k, x['first'], x['first'] + x['length'])

    twin_of = {}
    for gr in pattern_groups(x for x in elements if x['kind'] == 'plane_pattern' and x.get('coords')):
        for sid in gr['ids']:
            twin_of[sid] = [t for t in gr['ids'] if t != sid]            # every other sequence of the group can carry it, provided it carries the pitch directly (pitch_direct, no circle); the order of the identifiers says nothing about the side (the mirror dimension hits the later identifier)

    def pitch_ok(x):
        """Pitch of the plane sequence x: dimension with feature = sequence or segment first to last member. A twin sequence (second flank of the same ribs, same count and pitch) is
        covered if another sequence of its group carries its pitch directly and the offset is dimensioned (segment from a boundary coordinate of one to the boundary coordinate of the other sequence: the
        thickness, with known length also to the other end); the rank test (chains.derivable, twin rows) then finds the second length dimension derivable."""
        if pitch_direct(x):
            return True
        k = 'xyz'.index(x['axis_name'])
        for sid in twin_of.get(x['id'], ()):
            y = items.get(sid)
            if y is not None and y['part'] == x['part'] and pitch_direct(y) and any(
                    span_ok(x['part'], k, a, b) for a in (y['first'], y['first'] + y['length']) for b in (x['first'], x['first'] + x['length'])):
                return True
        return False

    def tied(part, k, coords, sym=True):
        """Position: the middle of two coords lies on the median plane of the part (sym), or a chain of length dimensions connects
        one of the coords with the datum (bounding box edge min/max of the part; the planner always sets its datum on such a
        face, plan_roles.datum); a dimension whose other end hangs free (rib thickness) does not count. A plane series with
        a dimensioned pitch that hangs on the datum locates all its members."""
        bb = pfs[part].get('bbox')
        if bb is None:
            return True
        if sym and len(coords) == 2 and abs((min(coords) + max(coords)) / 2 - (bb['min'][k] + bb['max'][k]) / 2) <= TOL:
            return True
        reach, sp, grew = [bb['min'][k], bb['max'][k]], spans(part, k), True
        near = lambda c: any(abs(c - r) <= TOL for r in reach)
        while grew:
            grew = False
            for a, b in sp:
                if near(a) != near(b):
                    reach.append(b if near(a) else a)
                    grew = True
            for x in elements:
                if x['kind'] == 'plane_pattern' and x['part'] == part and x['axis_name'] == 'xyz'[k] and pitch_ok(x) and any(
                        near(c) for c in x['coords']):
                    new = [c for c in x['coords'] if not near(c)]
                    reach += new
                    grew = grew or bool(new)
        return any(near(c) for c in coords)

    def bound_ok(part, k, c):
        """Extent: the boundary c along k carries a length dimension, or there is no plane there (it follows from other elements)."""
        return any(min(abs(pr[0] - c), abs(pr[1] - c)) <= TOL for pr in spans(part, k)) or not any(
            f['kind'] == 'plane' and f['part'] == part and f['axis_name'] == 'xyz'[k] and abs(f['coord'] - c) <= TOL for f in fl)

    def located_ok(x):
        """Position of an element (step: open edge and wall) - also applies to the route via individually dimensioned members."""
        k, part = 'xyz'.index(x['axis_name']), x['part']
        if x['kind'] == 'plane_pattern':
            return tied(part, k, x['coords'])
        if x['kind'] == 'step':
            return tied(part, 'xyz'.index(x['across']), [x['edge'], x['wall']], sym=False)
        return all(tied(part, 'xyz'.index(a), w) for a, w in x['walls'].items())

    def element_missing(x):
        k, part, kind = 'xyz'.index(x['axis_name']), x['part'], x['kind']
        if kind == 'plane_pattern':
            ext = all(bound_ok(part, m, x['bbox'][b][m]) for m in range(3) if m != k and 'xyz'[m] not in x.get('varies', [])
                      for b in ('min', 'max'))          # different heights (varies) keep their own coverage obligation
            return ([] if pitch_ok(x) else ['pitch']) + ([] if located_ok(x) else ['position']) + ([] if ext else ['extent'])
        if kind == 'step':
            m = 'xyz'.index(x['across'])
            return ([] if span_ok(part, k, x['floor'], x['top']) else ['height']) + (
                [] if span_ok(part, m, x['edge'], x['wall']) else ['depth']) + ([] if located_ok(x) else ['position'])
        out, ws = [], x['walls']
        for n, a in enumerate(sorted(ws, key=lambda a: (-(ws[a][1] - ws[a][0]), a))):
            if not span_ok(part, 'xyz'.index(a), *ws[a]):
                out.append('width' if kind == 'slot' or n else 'length')
        out += [] if span_ok(part, k, x['floor'], x['top']) else ['depth']
        return out + ([] if located_ok(x) else ['position'])

    whole = {x['id'] for x in elements if not element_missing(x)}
    via_element = {m for x in elements if x['id'] in whole for m in x['members']}

    def extent_ok(x):
        e = np.eye(3)['xyz'.index(x['axis_name'])]
        for L in linear:
            pr = _pair(L['g'], e) if _par(L['d'], e) and on_part(L['g'], x['part']) else None
            if x['id'] in L['ids'] or (pr and abs(pr[0] - x['lo']) <= TOL and abs(pr[1] - x['hi']) <= TOL):
                return True
        return False

    def hull_ok(x, k):
        """Envelope dimension/position of a freeform region along k: dimension between its boundaries or both boundaries as dimension line end points
        (position) of parallel dimensions of the part; a region without extent along k needs none."""
        lo, hi, e = x['min'][k], x['max'][k], np.eye(3)[k]
        if hi - lo <= TOL:
            return True
        ends = [c for L in linear if _par(L['d'], e) and on_part(L['g'], x['part']) and (pr := _pair(L['g'], e)) for c in pr]
        return any(abs(c - lo) <= TOL for c in ends) and any(abs(c - hi) <= TOL for c in ends)

    # Position: directions from linear dimensions perpendicular to the axis, median planes of the part bounding box, coaxial located
    # cylinders, for arcs the axes of tangent planes (features: arc['tangent']); patterns (size + pitch per direction + position of the
    # first member, blind holes + depth) fix all members. Fixed point.
    loc = [f for f in fl if f['kind'] in LOCATED]
    turned = {pid: ax for pid in manuf if (ax := part_axis(pfs[pid]['features'] if not pfs[pid].get('freeform') else [])) is not None}
    base = {}
    for f in loc:
        o, ax = _axis(f)
        dirs = [L['d'] for L in linear if f['id'] in L['ids'] and (ax is None or _perp(L['d'], ax))]
        bb = pfs[f['part']].get('bbox')
        for k in range(3 if bb else 0):
            e = np.eye(3)[k]
            if (ax is None or _perp(e, ax)) and abs(o[k] - (bb['min'][k] + bb['max'][k]) / 2) <= TOL:
                dirs.append(e)
        if f['kind'] == 'arc':
            dirs += [np.eye(3)['xyz'.index(a)] for a in f.get('tangent') or {}]
            if transition_arc(f, minor) or (bb and any(o[k] < bb['min'][k] - TOL or o[k] > bb['max'][k] + TOL for k in range(3))):
                dirs += list(np.eye(3))        # center outside the part lies on no edge - only R, no center position; transition arc (tangential in two planar faces) likewise
        if f['part'] in turned and (ax is None or _par(ax, turned[f['part']][1])):     # position transverse to the rotation axis through symmetry
            ta = turned[f['part']][1]
            x = _u(np.cross(ta, np.eye(3)[int(np.argmin(np.abs(ta)))]))
            dirs += [x, np.cross(ta, x)]
        base[f['id']] = dirs
    patterns = [f for f in fl if f['kind'] == 'pattern' and f.get('members')]
    for pt in patterns:      # members of a pattern symmetric to the center plane of the part are fixed transverse to it by the symmetry (no position dimension of the first member)
        bbp = pfs[pt['part']].get('bbox')
        mem = [items[m] for m in pt['members'] if m in items and _axis(items[m])[0] is not None]
        for k in range(3 if bbp and len(mem) >= 2 else 0):
            if mirrored_positions(mem, k, (bbp['min'][k] + bbp['max'][k]) / 2):
                for m in mem:
                    ax_ = _axis(m)[1]
                    if ax_ is None or _perp(np.eye(3)[k], ax_):
                        base[m['id']].append(np.eye(3)[k])
    groups_eq = {}
    for f in loc:      # equal elements (equal_key) whose coordinates are symmetric to the center plane are fixed transverse to it by the symmetry (the distances determine the row)
        if equal_key(f) is not None:
            groups_eq.setdefault((f['part'], equal_key(f)), []).append(f)
    for (gpart, _), gm in groups_eq.items():
        bbp = pfs[gpart].get('bbox')
        for k in range(3 if bbp and len(gm) >= 2 else 0):
            if mirrored_positions(gm, k, (bbp['min'][k] + bbp['max'][k]) / 2):
                for m in gm:
                    ax_ = _axis(m)[1]
                    if ax_ is None or _perp(np.eye(3)[k], ax_):
                        base[m['id']].append(np.eye(3)[k])
    located, pat_ok, changed = set(), set(), True
    while changed:
        changed = False
        for f in loc:
            if f['id'] in located:
                continue
            o, ax = _axis(f)
            dirs = list(base[f['id']])
            for g in loc:
                if g['id'] in located and g['kind'] in CYLINDERS and g['part'] == f['part']:
                    og, ag = _axis(g)
                    if (ax is None or _par(ax, ag)) and np.linalg.norm(np.cross(o - og, ag)) <= TOL:
                        x = _u(np.cross(ag, np.eye(3)[int(np.argmin(np.abs(ag)))]))
                        dirs += [x, np.cross(ag, x)]
            if _spans(dirs, ax):
                located.add(f['id']); changed = True
        for p in patterns:
            if p['id'] in pat_ok:
                continue
            if p.get('polar'):      # the sequence statement covers only the members that the recount finds in the sequence
                if p['members'][0] in located and pattern_size(p) and pattern_depth(p) and not polar_missing(p):
                    pat_ok.add(p['id']); located.update(polar_seq(p)[1]); changed = True
            elif p['members'][0] in located and pattern_size(p) and pattern_pitch(p) and pattern_depth(p):
                pat_ok.add(p['id']); located.update(p['members']); changed = True
    via_pattern = {m for pid in pat_ok for m in (polar_seq(items[pid])[1] if items[pid].get('polar') else items[pid]['members'])}

    part_feats = {}
    for f_ in items.values():
        part_feats.setdefault(f_.get('part'), []).append(f_)
    marks = _drawn_lines(spec)      # center crosses (points) and centerlines of the spec

    def missing(x):
        k, fid = x['kind'], x['id']
        if fid in prof_of:      # the residual member of a profile is covered via the profile (mandatory list in true size, profile_state), not via the row "Undimensioned forms"
            return ['Profile dimensions ' + prof_of[fid]] if prof_state[prof_of[fid]]['missing'] else []
        if x.get('via_member'):     # sub-element of a pattern member - covered via the instance and the dimensioned pattern
            ex = items.get(x['via_member'])
            if ex is not None and x.get('via_pattern') in pat_ok and not missing(ex):
                return []
            return [f"instance {x['via_member']}" + ('' if x.get('via_pattern') in pat_ok else ' and pattern ' + str(x.get('via_pattern')))]
        if k in LOCATED:
            if fid in via_pattern:
                return []
            return ([] if sized(fid) or fid in via_b else ['size']) + ([] if fid in located else ['position']) + (
                ['depth'] if k == 'hole' and x.get('blind') and not depth_ok(x) else [])
        if k == 'plane':      # side plane of a square bar is dimensioned with its □ dimension; member of a fully dimensioned element
            ok = any(g['kind'] == 'linear' for g in by[fid]) or (x.get('square') and square_ok(x['square'])) or fid in via_element
            if not ok and (c := center_plane(x, part_feats.get(x['part'], ()), pfs[x['part']].get('bbox'))) is not None:      # plane through the center of a dimensioned body of revolution
                ok = sized(c['id']) and (c['kind'] != 'sphere' or any(np.allclose(q[0], c['center'], atol=TOL) for _, q in marks if len(q) == 1))
            if fid in minor:        # minor plane: own dimension or the row "Undimensioned forms"
                return [] if ok or fid in via_minor else ['dimension or line “Undimensioned forms according to 3D model”']
            return [] if ok else ['position']
        if k in ELEMENTS:     # dimensioned as a whole, or each member individually (baseline per plane, Claude path as before)
            if fid in whole or (located_ok(x) and all(not missing(items[m]) for m in x.get('members') or [] if m in items)):
                return []
            return element_missing(x)
        if k == 'notch':      # width at the opening, depth (length dimensions on the element, also via n× 'also') and position of the opening edges (chain to the reference)
            lens = [float(np.linalg.norm(np.asarray(g['p2'], float) - np.asarray(g['p1'], float))) for g in by[fid]
                    if g['kind'] == 'linear' and g.get('p1') is not None and g.get('p2') is not None]
            o0, o1 = (np.asarray(q, float) for q in x['open'])
            ka = int(np.argmax(np.abs(o1 - o0)))
            return (([] if any(abs(v - float(x['width'])) <= 0.011 for v in lens) else ['width'])
                    + ([] if any(abs(v - float(x['depth'])) <= 0.011 for v in lens) else ['depth'])
                    + ([] if tied(x['part'], ka, [float(o0[ka]), float(o1[ka])]) else ['position']))
        if k == 'square':
            return [] if square_ok(fid) else ['□ dimension']
        if k == 'pattern':
            if fid in pat_ok or all(not missing(items[m]) for m in x['members'] if m in items):
                return []
            if x.get('polar'):
                return ([] if pattern_size(x) else ['size']) + polar_missing(x) + ([] if x['members'][0] in located else ['position']) + ([] if pattern_depth(x) else ['depth'])
            return ([] if pattern_size(x) else ['size']) + ([] if pattern_pitch(x) else ['pitch']) + (
                [] if x['members'][0] in located else ['position']) + ([] if pattern_depth(x) else ['depth'])
        if k == 'bend_table':
            t = next((t for t in tables if t.get('bend_table') == x['part']), None)
            if t is None:
                return ['bend table']
            # the wire diameter is mandatory: Ø dimension on the wire (tag on the bend table) or, alternatively, in the table heading
            # (without its own title the table writes "Wire Ø..." itself); the semi-finished product text of the parts list does not replace the dimension
            wire = 'Ø' + fmt(x['wire_diameter'])
            return [] if any(g['kind'] == 'diameter' for g in by[fid]) or wire in str(t.get('title', wire)) else ['wire Ø']
        if k == 'slant':      # individually mandatory (A): width/inclination/length in the text and position; never a collective/general note
            if drafted(x):          # draft angle: the note per part covers it
                return []
            if fid in minor:        # minor face: own dimension or the line "Undimensioned forms"
                return [] if own(x) or fid in via_minor else ['dimension or line “Undimensioned forms according to 3D model”']
            return ([] if slant_angle_ok(x) else ['angle dimension']) + ([] if any(g['kind'] == 'linear' for g in by[fid]) else ['length dimension'])
        if k == 'cone' and fid in minor and fid not in via_b:       # free-standing cone is a minor face (a collective note covers as before)
            return [] if own(x) or fid in via_minor else ['dimension or line “Undimensioned forms according to 3D model”']
        if k in ('chamfer', 'fillet', 'cone'):
            word = {'chamfer': 'chamfers', 'fillet': 'radii', 'cone': 'cone'}[k]
            return [] if own(x) or fid in via_b or (not strict and (word.casefold() in notes or (k == 'cone' and 'countersink' in notes))) else [
                'dimension or collective note (features/general)' if strict else f'dimension or note “{word}”']
        if k == 'thread':
            return [] if by[fid] or _has_designation(texts, x.get('designation')) else ['designation']
        if k == 'knurl':
            return [] if by[fid] or 'knurl' in texts.casefold() else ['note “Knurl”']
        if k == 'engraving':
            return [] if by[fid] or (x.get('text') or 'Engraving') in texts else ['engraving text']
        if k == 'extent':
            return [] if extent_ok(x) else ['overall dimension']
        if k == 'freeform_note':
            return [] if FREEFORM_RX.search(notes) or any(g.get('kind') == 'note' and FREEFORM_RX.search(str(g.get('text') or '').casefold()) for g in geos) or fid in via_c else ['note “Shape according to 3D model”']
        if k == 'freeform_region' and fid in minor and fid not in via_c:     # region below F3: part of the line "Undimensioned forms"
            return [] if fid in via_minor else ['line “Undimensioned forms according to 3D model”']
        if k == 'freeform_region':      # reference to the 3D model with a leader line to the region and position/envelope dimensions
            return (([] if fid in via_c else ['reference “Contour according to 3D model”'])
                    + [f'envelope dimension {a}' for j, a in enumerate('xyz') if not hull_ok(x, j)])
        return []

    status = [(x, missing(x)) for x in req]
    return {'status': status, 'by': shown_by, 'linear': linear_drawn, 'manuf': manuf, 'mode': mode, 'problems': problems,
            'items': items, 'dim_n': dim_n, 'dim_assigned': dim_assigned, 'notes': mirror_notes, 'minor': minor, 'prof_of': prof_of, 'profile_state': prof_state}


def coverage(spec, features_by_part, geos, notes_text, tables, strict=False, sym=None, frames=None, ev=None):
    """{required, dimensioned, missing: [ID], modes: {A, B, C}, parts: {part: {required, dimensioned, A, B, C}}} over the
    mandatory elements of the parts with role manufactured (rules in _evaluate); modes counts the covered elements per coverage type
    (A individually, B collective statement/note, C reference to the 3D model); assigned_share = share of the dimensions (ASSIGN_KINDS without
    auxiliary dimensions) to which a form feature is assigned (tag or geometric), 3 digits, None without dimensions."""
    ev = ev or _evaluate(spec, features_by_part, geos, notes_text, tables, strict, sym, frames)      # ev = evaluation of the render (one run for coverage and findings)
    st = ev['status']
    miss = [x['id'] for x, m in st if m]
    modes, parts = {'A': 0, 'B': 0, 'C': 0}, {}
    for x, m in st:
        p = parts.setdefault(x['part'], {'required': 0, 'dimensioned': 0, 'A': 0, 'B': 0, 'C': 0})
        p['required'] += 1
        if not m:
            k = ev['mode'](x)
            p['dimensioned'] += 1
            p[k] += 1
            modes[k] += 1
    share = round(ev['dim_assigned'] / ev['dim_n'], 3) if ev['dim_n'] else None
    return {'required': len(st), 'dimensioned': len(st) - len(miss), 'missing': miss, 'modes': modes, 'parts': parts, 'assigned_share': share}


def _label(x):
    """Kind and size of a mandatory element for finding texts."""
    k = x['kind']
    size = {'hole': lambda: 'Ø' + fmt(x['diameter']), 'shaft': lambda: 'Ø' + fmt(x['diameter']),
            'sphere': lambda: 'SØ' + fmt(x['diameter']), 'arc': lambda: 'R' + fmt(x['radius']),
            'fillet': lambda: 'R' + fmt(x['radius']), 'plane': lambda: f"{x['axis_name']} = {fmt(x['coord'])}",
            'square': lambda: '□' + fmt(x['size']), 'chamfer': lambda: f"{fmt(x['width'])}×{fmt(x['angle_deg'], 1)}°",
            'cone': lambda: size_text(collective_family(x) or 'cone', collective_size(x)), 'slant': lambda: size_text('slant', collective_size(x)),
            'pattern': lambda: f"{x['count']}× Ø{fmt(x['size'])}", 'bend_table': lambda: f"Wire Ø{fmt(x['wire_diameter'])}",
            'pocket': lambda: f"{fmt(x['length'])}×{fmt(x['width'])}×{fmt(x['depth'])}",
            'step': lambda: f"{fmt(x['height'])}×{fmt(x['width'])}", 'slot': lambda: f"{fmt(x['width'])}×{fmt(x['depth'])}",
            'notch': lambda: f"{fmt(x['width'])}×{fmt(x['depth'])}",
            'plane_pattern': lambda: f"{x['count']}× {fmt(x['pitch'])} ({x['axis_name']})",
            'thread': lambda: str(x.get('designation') or ''), 'knurl': lambda: str(x.get('profile') or ''),
            'engraving': lambda: f"“{x.get('text') or ''}”", 'extent': lambda: f"{x['axis_name']} {fmt(x['size'])}",
            'freeform_note': lambda: '“Shape according to 3D model”', 'freeform_region': lambda: x['id'].rpartition('#')[2]}.get(k, lambda: '')()
    return f'{ART.get(k, k)} {size}'.strip()


def _where(g):
    return f"Sheet {int(g.get('sheet') or 0) + 1}, view {g.get('view')}"


def _claim_where(g):
    """Location of a collective note/reference/tag: view, note line or sheet."""
    if g.get('kind') == 'note':
        return f"Sheet {int(g.get('sheet') or 0) + 1}, note line {int(g.get('line') or 0) + 1}"
    return _where(g) if g.get('view') else f"Sheet {int(g.get('sheet') or 0) + 1}"


def _dir_name(d):
    k = int(np.argmax(np.abs(d)))
    return 'xyz'[k] if _par(d, np.eye(3)[k]) else str(np.round(d, 3).tolist())


def _own_form_value(x, g):
    """A length dimension g on element x is its own form dimension (notch: width/depth, cone: length, slanted face: width/length), not a position dimension."""
    if g.get('p1') is None or g.get('p2') is None:
        return False
    v = float(np.linalg.norm(np.asarray(g['p2'], float) - np.asarray(g['p1'], float)))
    forms = {'notch': ('width', 'depth'), 'cone': ('length',), 'slant': ('width', 'length')}.get(x.get('kind'), ())
    prof = slant_profile_length(x) if x.get('kind') == 'slant' else None       # the true profile length is also a form dimension of the slanted face
    return any(x.get(k) is not None and abs(float(x[k]) - v) <= 0.011 for k in forms) or (prof is not None and abs(prof - v) <= 0.011)


def _gkey(g):
    """Identifier of an annotation: (sheet, index); without index (tests) the object identity."""
    return (int(g.get('sheet') or 0), g.get('index') if g.get('index') is not None else id(g))


def _assembly(g, all_parts):
    """Dimension in an assembly view: more than one part visible (a view without parts shows all parts of the set). Such dimensions count neither for duplicates (N5.1) nor for derivation (N5.2)."""
    return len(g.get('parts') or all_parts) >= 2


def _leg_key(g, x):
    """Key of a leg of the slanted edge (length dimension with end points A, B whose direction does not lie along A→B): (kind, hypotenuse, inclination, dimension direction) - two slanted faces of equal
    width and inclination carry the same legs, even if their lengths differ. None if the dimension is no leg."""
    if g.get('p1') is None or g.get('p2') is None or g.get('dir3') is None or x.get('angle_deg') is None:
        return None
    v = np.asarray(g['p2'], float) - np.asarray(g['p1'], float)
    n, d = float(np.linalg.norm(v)), _u(g['dir3'])
    if n <= TOL or d is None or abs(float(v @ d)) >= 0.999 * n or abs(float(v @ d)) <= TOL:
        return None
    return ('leg', round(n, 2) + 0.0, round(float(x['angle_deg']), 1) + 0.0, int(np.argmax(np.abs(d))))      # dimension direction (dominant world axis) - at 45° both legs are equally long


def feature_findings(spec, features_by_part, geos, notes_text, tables, strict, sym=None, frames=None, ev=None):
    """feature_not_dimensioned (per missing element), dimension_redundant (same distance twice on the same part,
    two size dimensions on the same form feature), chain_closed (closed dimension chain, always warn). Severity block for strict
    (local path), otherwise warn (Claude path). Auxiliary dimensions "(…)" do not count."""
    ev = ev or _evaluate(spec, features_by_part, geos, notes_text, tables, strict, sym, frames)
    sev = 'block' if strict else 'warn'
    out = [_f('feature_not_dimensioned', sev, 0, None, f"{_label(x)} ({x['id']}) not completely dimensioned, missing: {', '.join(m)}",
              part=x['part'], feature=x['id'], missing=m) for x, m in ev['status'] if m and x['id'] not in (ev.get('prof_of') or {})]      # the residual members of a profile are reported once per profile by profile_not_dimensioned
    # collective_count_mismatch, collective_size_mismatch, collective_not_allowed, model_ref_not_allowed
    out += [_f(code, sev, int(g.get('sheet') or 0), None, msg[:300], index=g.get('index'), **extra) for code, g, msg, extra in ev['problems']]
    out += [_f('coverage_error', 'warn', 0, None, note) for note in ev.get('notes') or []]        # mirror dimensions failed, the coverage continued

    def redundant(g, first, what):
        return _f('dimension_redundant', sev, int(g.get('sheet') or 0), None,
                  f"Dimension “{g.get('text')}” ({_where(g)}) repeats “{first.get('text')}” ({_where(first)}){what}",
                  index=g.get('index'), duplicate_of={'sheet': first.get('sheet'), 'index': first.get('index')})

    all_parts = [p['id'] for p in spec.get('parts') or []] or list(features_by_part)
    lin = [L for L in ev['linear'] if not _assembly(L['g'], all_parts)]      # dimensions in assembly views do not count for duplicates, chains and over-determination
    for j, Lj in enumerate(lin):
        pj = _pair(Lj['g'], Lj['d'])
        for Li in lin[:j]:
            pi = _pair(Li['g'], Lj['d'])
            tags = {str(L['g'].get('feature')) for L in (Li, Lj)}
            two_slants = len(tags) == 2 and all((ev['items'].get(t) or {}).get('kind') == 'slant' for t in tags)    # congruent slants (mirror pair) each carry their own edge length, even where they coincide in the view
            if pj and pi and _par(Li['d'], Lj['d']) and Li['parts'] & Lj['parts'] and not two_slants \
                    and abs(pi[0] - pj[0]) <= TOL and abs(pi[1] - pj[1]) <= TOL:
                out.append(redundant(Lj['g'], Li['g'], f" on part {sorted(Li['parts'] & Lj['parts'])[0]}"))
                break
    firsts = {}              # the same pattern note ("3× 20 (=60)") twice on the same part: dimension_redundant
    for L in lin:
        g, t = L['g'], str(L['g'].get('text') or '')
        if (ev['items'].get(str(g.get('feature'))) or {}).get('kind') == 'plane_pattern' and COUNT_RX.match(t) and '(=' in t:
            key = (t, tuple(sorted(L['parts'])))
            if key in firsts:
                out.append(redundant(g, firsts[key], ' (same pattern note)'))
            else:
                firsts[key] = g
    seen = set()
    for fid, gs in ev['by'].items():
        size = [g for g in gs if g['kind'] in SIZE_KINDS]
        if (ev['items'].get(fid) or {}).get('kind') == 'cone':        # both cone diameters are different dimensions; only the same value twice is a duplicate
            first, dups = {}, []
            for g in size:
                v = round(float(g.get('radius') or 0.0), 3)
                if v in first:
                    dups.append((g, first[v]))
                first.setdefault(v, g)
            for g, orig in dups:
                if id(g) not in seen:
                    seen.add(id(g))
                    out.append(redundant(g, orig, f' on form feature {fid}'))
            continue
        for g in size[1:]:
            if id(g) not in seen:
                seen.add(id(g))
                out.append(redundant(g, size[0], f' on form feature {fid}'))

    # Ø dimension on an element whose axis lies in a planar face of the part (outline edge): only half the dimension line would lie in the material, ISO 129-1 demands R or depth from the face
    feats_of = {}
    for x in ev['items'].values():
        feats_of.setdefault(x['part'], []).append(x)
    for g in geos:
        if _aux(g) or g.get('kind') != 'diameter' or g.get('center') is None or g.get('radius') is None:
            continue
        for fid in dict.fromkeys(([str(g['feature'])] if g.get('feature') else []) + [str(t) for t in g.get('also') or []]):
            x = ev['items'].get(fid)
            if x is None or x['kind'] not in ('cone', 'hole', 'shaft') or axis_on_outline(x, feats_of.get(x['part'], [])) is None:
                continue
            out.append(_f('diameter_on_outline_axis', 'warn', int(g.get('sheet') or 0), None,
                          f"Ø dimension “{g.get('text')}” ({_where(g)}) on {_label(x)} ({fid}): the axis lies in a planar face of the part (outline edge),"
                          f"only half the dimension line lies in the material; dimension R or depth from this face (ISO 129-1)", index=g.get('index'), part=x['part'], feature=fid))
            break

    # the same form dimension (Ø, R, angle, notch/cone/slanted face with own dimension, legs of a slanted edge) on congruent copies (same equal_key) is stated several times instead of once "n× …" (also),
    # over all views, also dimensions with also ("2× 35°" in two views) and angles; dimensions in assembly views do not count
    seen_copy, copies = set(), {}
    for g in geos:
        if _aux(g) or g.get('kind') not in ('diameter', 'radius', 'angle', 'linear') or not g.get('feature') or _assembly(g, all_parts):
            continue
        x = ev['items'].get(str(g['feature']))
        if x is None:
            continue
        key = _leg_key(g, x) if g['kind'] == 'linear' and x.get('kind') == 'slant' else None
        if key is None:
            # full congruence (equal_key); copy_key: angle on slanted faces - inclination and face normal up to the sign ("2× 35°" on slanted faces of different widths;
            # non-parallel slanted faces of equal inclination are no copies); length on slanted faces - same profile from the dimension value (profile length 50 stands as width in one, as length in another)
            key = copy_key(x, g)
            if key is None or (g['kind'] == 'linear' and not _own_form_value(x, g)):
                continue
        copies.setdefault((x['part'], g['kind'], COUNT_RX.sub('', str(g.get('text') or ''), count=1).strip(), key), []).append(g)
    for (part, kind, text, key), gs in copies.items():
        tags = list(dict.fromkeys(str(t) for g in gs for t in [g['feature'], *(g.get('also') or [])]))
        views_of = {(int(g.get('sheet') or 0), g.get('view')) for g in gs}
        if len({_gkey(g) for g in gs}) > 1 and (len(tags) > 1 or (kind == 'angle' and len(views_of) > 1)):      # the same angle on the same element in two views
            for g in gs[1:]:
                if id(g) not in seen_copy:
                    seen_copy.add(id(g))
                    out.append(_f('dimension_duplicate_copy', 'warn', int(g.get('sheet') or 0), None,
                                  f"Dimension “{g.get('text')}” ({_where(g)}) is stated several times for congruent copies ({', '.join(tags)}): state it once as “n× …” with also",
                                  index=g.get('index'), part=part, features=tags))

    # position dimensions determined twice - (a) an element is dimensioned on the same axis from two different envelope faces (opposite view from the opposite edge),
    # (b) a dimension closes a chain in which the n× pitch of a pattern fixes all its members (e.g. 250/235/226.69 next to 6× 36.67)
    over = set()
    for pid in ev['manuf']:
        pf = features_by_part.get(pid)
        bb = pf.get('bbox') if isinstance(pf, dict) else None
        for k in range(3):
            e = np.eye(3)[k]
            Ls = [L for L in lin if pid in L['parts'] and _par(L['d'], e)]
            if bb is not None:
                hull = (float(bb['min'][k]), float(bb['max'][k]))
                by_el = {}
                for L in Ls:
                    pr = _pair(L['g'], e)
                    if pr is None:
                        continue
                    for end, other in ((pr[0], pr[1]), (pr[1], pr[0])):
                        for fid in L['hits'][0] + L['hits'][1]:
                            x = ev['items'].get(fid)
                            if x is None or x['kind'] not in LOCATED + ('notch',) or any(abs(end - r) <= TOL for r in hull) or not any(abs(other - r) <= TOL for r in hull):
                                continue
                            c = _axis(x)[0] if x['kind'] != 'notch' else np.asarray(x['pt'], float)
                            if c is not None and abs(float(c @ e) - end) <= TOL + 1e-9:
                                by_el.setdefault(fid, []).append((L, other))
                for fid, got in by_el.items():
                    far = {round(o, 2) for _, o in got}
                    if len(far) > 1 and len({id(L['g']) for L, _ in got}) > 1:
                        g = got[1][0]['g'] if got[1][0]['g'] is not got[0][0]['g'] else got[-1][0]['g']
                        if id(g) not in over:
                            over.add(id(g))
                            out.append(_f('dimension_overdetermined', 'warn', int(g.get('sheet') or 0), None,
                                          f"Position of {_label(ev['items'][fid])} ({fid}) in direction {_dir_name(e)} is over-determined: dimensioned from both envelope faces (dimension “{g.get('text')}”,"
                                          f"{_where(g)}); one body-fixed datum per axis is enough", index=g.get('index'), part=pid, feature=fid))
            if bb is not None:
                hull = (float(bb['min'][k]), float(bb['max'][k]))
                spans_h = []      # (envelope coordinate, other coordinate, L) of the dimensions that start at an envelope face
                for L in Ls:
                    pr_ = _pair(L['g'], e)
                    if pr_ is not None:
                        for end_, oth_ in ((pr_[0], pr_[1]), (pr_[1], pr_[0])):
                            if any(abs(end_ - h_) <= TOL for h_ in hull) and not any(abs(oth_ - h_) <= TOL for h_ in hull):
                                spans_h.append((end_, oth_, L))
                for x in ev['items'].values():      # (c) pattern symmetric to the center plane: the position of its members follows from symmetry and pitch - a dimension from the envelope face to a member is over-determined (e.g. 235/226.69 next to 6× 36.67)
                    if x['kind'] != 'pattern' or x['part'] != pid or not x.get('members'):
                        continue
                    members = [ev['items'][m] for m in x['members'] if m in ev['items'] and _axis(ev['items'][m])[0] is not None]
                    cs = [float(_axis(m)[0][k]) for m in members]
                    if mirrored_positions(members, k, sum(hull) / 2):
                        for end_, oth_, L in spans_h:
                            if any(abs(oth_ - c_) <= TOL for c_ in cs) and str(L['g'].get('feature')) in list(x['members']) + [x['id']] and id(L['g']) not in over:      # only dimensions that belong to the pattern or its members (a foreign element on the same coordinate needs its own position dimension)
                                over.add(id(L['g']))
                                g = L['g']
                                out.append(_f('dimension_overdetermined', 'warn', int(g.get('sheet') or 0), None,
                                              f"Dimension “{g.get('text')}” ({_where(g)}) fixes in direction {_dir_name(e)} a member of {x['id']} whose position follows from the symmetry about the center plane and the pitch;"
                                              f"omit it or mark it as an auxiliary dimension “(…)”", index=g.get('index'), part=pid, feature=x['id']))
                ta_ = part_axis(features_by_part[pid]['features'] if isinstance(features_by_part.get(pid), dict) and not features_by_part[pid].get('freeform') else [])
                for f_ in ev['items'].values():      # (d) two planes (web edges) symmetric to the axis of a hole/shaft: both dimensioned from the envelope face although axis position and width fix them
                    if f_['kind'] not in ('hole', 'shaft') or f_['part'] != pid or _axis(f_)[1] is None or not _perp(np.eye(3)[k], _axis(f_)[1]) or (ta_ is not None and _perp(e, ta_[1])):
                        continue
                    ca = float(_axis(f_)[0][k])
                    if abs(ca - sum(hull) / 2) <= TOL:      # axis in the center plane: the edges lie symmetric to the center plane (dimensions from both envelope faces are usual there)
                        continue
                    pls = [q for q in ev['items'].values() if q['kind'] == 'plane' and q['part'] == pid and q['axis_name'] == 'xyz'[k] and not q.get('square')]
                    cand_ = []
                    for q1 in pls:      # like the planner only the nearest symmetric pair per axis (pairs further out have their own dimension)
                        q2 = next((q for q in pls if abs(q['coord'] - (2 * ca - q1['coord'])) <= TOL), None)
                        if q2 is not None and q1['coord'] < ca - TOL and not any(abs(q['coord'] - h_) <= TOL for q in (q1, q2) for h_ in hull):
                            cand_.append((ca - q1['coord'], q1, q2))
                    for _, q1, q2 in sorted(cand_, key=lambda t: t[0])[:1]:
                        got = [(L, o_) for end_, o_, L in spans_h if abs(o_ - q1['coord']) <= TOL or abs(o_ - q2['coord']) <= TOL]
                        if len({round(o_, 2) for _, o_ in got}) >= 2:
                            g = got[-1][0]['g']
                            if id(g) not in over:
                                over.add(id(g))
                                out.append(_f('dimension_overdetermined', 'warn', int(g.get('sheet') or 0), None,
                                              f"Dimension “{g.get('text')}” ({_where(g)}): planes {q1['id']} and {q2['id']} lie symmetric about the axis of {f_['id']}; dimensioning both edges from the envelope face"
                                              f"determines them twice (axis position + width are enough)", index=g.get('index'), part=pid, feature=q1['id']))
            nodes, parent = [], []

            def node(c):
                for n, v in enumerate(nodes):
                    if abs(v - c) <= TOL:
                        return n
                nodes.append(c); parent.append(len(parent))
                return len(nodes) - 1

            def find(n):
                while parent[n] != n:
                    parent[n] = parent[parent[n]]; n = parent[n]
                return n
            has_pattern = False
            for L in Ls:                              # nodes: only coordinates that a dimension names as an end point (member coordinates without a dimension connect nothing: two patterns with a member at x = 0 are no circle)
                pr0 = _pair(L['g'], e)
                if pr0 is not None:
                    node(pr0[0]), node(pr0[1])
            known = len(nodes)
            for L in Ls:                              # n× pitch of a pattern: all members lie on the grid first..last
                x = ev['items'].get(str(L['g'].get('feature')))
                pr = _pair(L['g'], e)
                if x is not None and x['kind'] == 'pattern' and pr is not None and COUNT_RX.match(str(L['g'].get('text') or '')):
                    has_pattern = True
                    for m in x['members']:
                        mx = ev['items'].get(m)
                        c = _axis(mx)[0] if mx is not None else None
                        if c is not None and pr[0] - TOL <= float(c @ e) <= pr[1] + TOL and any(abs(float(c @ e) - v) <= TOL for v in nodes[:known]):
                            a, b = find(node(pr[0])), find(node(float(c @ e)))
                            if a != b:
                                parent[a] = b
                    a, b = find(node(pr[0])), find(node(pr[1]))
                    if a != b:
                        parent[a] = b
            done = []
            for L in Ls:
                pr = _pair(L['g'], e)
                x = ev['items'].get(str(L['g'].get('feature')))
                if pr is None or (x is not None and x['kind'] == 'pattern') or any(abs(pr[0] - q[0]) <= TOL and abs(pr[1] - q[1]) <= TOL for q in done):
                    continue
                done.append(pr)
                a, b = find(node(pr[0])), find(node(pr[1]))
                if a == b and has_pattern and id(L['g']) not in over:      # only if the pattern pitch carries the connection (otherwise closed chain: chain_closed)
                    over.add(id(L['g']))
                    g = L['g']
                    out.append(_f('dimension_overdetermined', 'warn', int(g.get('sheet') or 0), None,
                                  f"Dimension “{g.get('text')}” ({_where(g)}) follows in direction {_dir_name(e)} from other dimensions of the same axis (n× pitch of a pattern);"
                                  f"omit it or mark it as an auxiliary dimension “(…)”", index=g.get('index'), part=pid))
                elif a != b:
                    parent[a] = b

    flagged = set()
    for pid in ev['manuf']:
        groups = []                                  # [(direction, [linear])]
        for L in lin:
            if pid in L['parts']:
                grp = next((gr for gr in groups if _par(gr[0], L['d'])), None)
                if grp is None:
                    groups.append((L['d'], [L]))
                else:
                    grp[1].append(L)
        for d, Ls in groups:
            chain, pairs = Chain(), []                # the same chain decision as profile_stated (chain_determined)
            for L in Ls:
                pr = _pair(L['g'], d)
                if pr is None or any(abs(pr[0] - q[0]) <= TOL and abs(pr[1] - q[1]) <= TOL for q in pairs):
                    continue                         # duplicate distance: dimension_redundant
                pairs.append(pr)
                if chain.link(*pr) and id(L['g']) not in flagged:
                    flagged.add(id(L['g']))
                    g = L['g']
                    out.append(_f('chain_closed', 'warn', int(g.get('sheet') or 0), None,
                                  f"Closed dimension chain on part {pid} in direction {_dir_name(d)}: dimension “{g.get('text')}” ({_where(g)}) "
                                  f"follows from other dimensions; mark it as an auxiliary dimension \"(...)\" or omit it", index=g.get('index'), part=pid))
    return out


# ---------------------------------------------------------------------------------------------------------------------------------------------------
# Measurement N2 (symmetry) and N5 (dimensioning without over-determination). Only warn; planner and autofix do not read these codes (autofix.LAYOUT).

TOLERANCE_RX = re.compile(r'±|[+−-]\s*\d+(?:[.,]\d+)?\s*/\s*[+−-]|(?<=\d)\s*[A-Za-z]{1,2}\d{1,2}\b')      # "40 ±0.1", "+0.1/-0", fit "12 H7"
SIDE_OF = {(0, 1): 'left', (0, -1): 'right', (1, 1): 'front', (1, -1): 'back', (2, 1): 'top', (2, -1): 'bottom'}      # viewing side by camera direction (projection.PRESETS)
MIN_CENTER_MM = 0.75         # like the core: centerlines only for cylinders with paper radius from 0.75 mm (Drawing.centerlines)
RANK_TOTAL, RANK_FUNCTION, RANK_PATTERN, RANK_REST = 0, 1, 2, 3


PATTERN_ID_RX = re.compile(r'#plane_pattern\d+$')      # identifier of a plane sequence (features.py: part#plane_patternN)
_ANY = object()                                        # symmetry_axis_shown without statement about the dimension


def _toleranced(text):
    return bool(TOLERANCE_RX.search(str(text or '')))


def _view_parts(spec):
    """{view ID: parts} of all sheets; a view without parts shows all parts of the set."""
    every = [p['id'] for p in spec.get('parts') or []]
    return {v['id']: list(v.get('parts') or every) for sh in spec.get('sheets') or [] for v in sh.get('views') or [] if v.get('id') is not None}


def _view_defs(spec):
    """{view ID: view definition} of all sheets. A pattern detail view (N4.9: detail view that carries the length dimensions of a plane sequence) gets as a copy `_host`: the view in which
    its circle (detail_circle of the same label) stands; symmetry_axis_shown counts the axis of its view there (the spec itself stays unchanged)."""
    out = {v['id']: v for sh in spec.get('sheets') or [] for v in sh.get('views') or [] if v.get('id') is not None}
    for sh in spec.get('sheets') or []:
        anns = [a for a in sh.get('annotations') or [] if isinstance(a, dict)]
        for vid, vd in list(out.items()):
            det = vd.get('detail')
            if not isinstance(det, dict) or not any(a.get('type') == 'linear' and a.get('view') == vid and PATTERN_ID_RX.search(str(a.get('feature') or '')) for a in anns):
                continue
            host = next((a.get('view') for a in anns if a.get('type') == 'detail_circle' and str(a.get('label') or 'Z') == str(det.get('label') or 'Z') and a.get('view') in out), None)
            if host is not None and not out[host].get('detail'):
                out[vid] = {**vd, '_host': host}
    return out


def _paper_scale(sd, vd):
    """Scale of a view (paper per model mm): own or detail scale, otherwise that of the sheet; 1 if unreadable."""
    for text in (vd.get('scale') if vd else None, sd.get('scale')):
        m = re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)\s*', str(text or ''))
        if m and float(m.group(2).replace(',', '.')) > 0:
            return float(m.group(1).replace(',', '.')) / float(m.group(2).replace(',', '.'))
    return 1.0


def _drawn_lines(spec):
    """All drawn centerlines and center crosses: [(view, points n x 3)] from spec annotations (polyline with style center or symmetry_axis, centermark as point). A planner-generated
    symmetry line (`symmetry_axis`) below MIN_AXIS_MM on paper is a stub and does not count."""
    out = []
    for sh in spec.get('sheets') or []:
        views = {v.get('id'): v for v in sh.get('views') or []}
        for a in sh.get('annotations') or []:
            if a.get('type') == 'polyline' and (a.get('style') == 'center' or a.get('symmetry_axis')) and len(a.get('points') or []) >= 2:
                pts = np.asarray(a['points'], float).reshape(-1, 3)
                if a.get('symmetry_axis') and float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) * _paper_scale(sh, views.get(a.get('view'))) < MIN_AXIS_MM - 1e-9:
                    continue
                out.append((a.get('view'), pts))
            elif a.get('type') == 'centermark' and a.get('center') is not None:
                out.append((a.get('view'), np.asarray(a['center'], float).reshape(1, 3)))
    return out


def _plane_drawn(lines, k, m, views=None, frames=None):
    """Does a centerline stand in the plane x_k = m (at least two points, all in it) in one of the views views (None: any)? With frames (view_frames): only in views
    in which the plane appears as a line (viewing direction ⟂ plane normal) and the line lies there as a line, not as a point; views without a frame stay unevaluated (count)."""
    def line_in_view(v, p):
        N = None if not frames or v not in frames else (frames[v].get('axes') or {}).get('toward_camera')
        if N is None:
            return True
        N = _u(N)
        if N is None or abs(float(N[k])) > 1e-3:
            return False
        span = p.max(axis=0) - p.min(axis=0)
        return float(np.linalg.norm(span - (span @ N) * N)) > TOL
    return any((views is None or v in views) and len(p) >= 2 and np.all(np.abs(p[:, k] - m) <= TOL) and line_in_view(v, p) for v, p in lines)


def _core_axis(items, pid, k, m):
    """The axes of holes/shafts of the part pid that lie in the plane x_k = m (⟂ plane, axis point in it): the core draws them there itself as a centerline (N2.2) if they are visible in the view
    (symmetry_axis_shown checks that per view). Empty list = none."""
    return [x for x in items if x['part'] == pid and x['kind'] in ('hole', 'shaft') and x.get('axis') is not None and x.get('start') is not None and abs(float(_u(x['axis'])[k])) <= 1e-6
            and abs(float(np.asarray(x['start'], float)[k]) - m) <= TOL]


SIDE_VEC = {side: np.eye(3)[k] * sign for (k, sign), side in SIDE_OF.items()}


def _seen_from(x, N):
    """Is form feature x visible in a view with viewing direction to the camera N? visible_from missing or empty = unknown, counts as visible (like plan_views, N2.3); otherwise one of its sides
    looks toward the camera (angle between side and N at most 60 degrees)."""
    vf = x.get('visible_from')
    return not vf or any(float(SIDE_VEC[s] @ N) > 0.5 for s in vf if s in SIDE_VEC)


def symmetry_axis_shown(lines, k, m, views=None, frames=None, core=None, defs=None, feature=_ANY):
    """Is the symmetry axis of the plane x_k = m shown in one of the views views (None: any)? A single answer for measurement (N2.2, N5.2) and coverage (virtual mirror dimensions):
    (a) spec polyline `symmetry_axis` or `style: center` in the plane (_plane_drawn, with frames only in views in which the plane appears as a line), or
    (b) the core centerline (core: the axes from _core_axis, True = visibility unknown) in a view in which the plane appears as a line, the centerlines are not switched off
    (defs[view]['centerlines'] is not False) and at least one of these axes is visible (visible_from contains the viewing side; with `hidden: true` also hidden ones).
    Detail views (defs[view]['detail']) never show the axis of the whole part: they count neither for (a) nor for (b). Views without a frame stay unevaluated (count).
    Exception pattern detail view (N4.9): its axis is that of the view of its circle (`_host`); asked for a specific dimension (`feature` = its tag, also None),
    that applies only if the dimension itself carries a plane sequence (PATTERN_ID_RX); without `feature` (part as a whole, N2.2/N5.2) as before."""
    defs = defs or {}
    hosts = {v: d['_host'] for v, d in defs.items() if d.get('detail') and d.get('_host')} if (feature is _ANY or PATTERN_ID_RX.search(str(feature or ''))) else {}      # pattern detail view carries the axis of its view
    if views is not None and hosts:
        views = {hosts.get(v, v) for v in views}
    hidden_detail = {v for v, d in defs.items() if d.get('detail')}
    if hidden_detail:
        lines = [(v, p) for v, p in lines if v not in hidden_detail]
        views = None if views is None else {v for v in views if v not in hidden_detail}
        if views is not None and not views:
            return False
    if _plane_drawn(lines, k, m, views, frames):
        return True
    if not core:
        return False
    for vid in (views if views is not None else [v for v in (frames or {}) if v not in hidden_detail]):
        if defs.get(vid, {}).get('centerlines') is False:
            continue
        N = None if not frames or vid not in frames else (frames[vid].get('axes') or {}).get('toward_camera')
        if N is None:
            return True
        N = _u(N)
        if N is None or abs(float(N[k])) > 1e-3:
            continue
        if core is True or defs.get(vid, {}).get('hidden') or any(_seen_from(x, N) for x in core):
            return True
    return False


def _sym_planes(sym, pid):
    return {int(k): float(m) for k, m in ((sym or {}).get(pid) or {}).items()}


def chain_mirrors(coords, m):
    """Mirror rows (c, c', m) of the rank test: every coordinate in the plane (c = c') and every pair from coords with c + c' = 2m."""
    out = []
    for c in coords:
        if abs(c - m) <= TOL:
            out.append((c, c, m))
        elif c < m - TOL and any(abs(c2 - (2 * m - c)) <= TOL for c2 in coords):
            out.append((c, 2 * m - c, m))
    return out


def chain_measure(key, text, a, b, bb, k, nums, x, items):
    """Input of the rank test (chains.derivable) for a length dimension (a, b) along world axis k of a part: ({key, a, b, rank, locked[, steps]}, coordinates). Rank: total dimension (bounding box), function dimension (number
    from nums), pattern statement "n× …", rest; steps = pitch steps of the pattern members (plane sequence or hole row of element x, if the statement spans them from a to b). Shared function of
    _chain_findings (checker) and plan_dims.derivable_left (planner); items: {ID: form feature} for the members of a pattern."""
    e = np.eye(3)[k]
    if bb is not None and abs(a - float(bb['min'][k])) <= TOL and abs(b - float(bb['max'][k])) <= TOL:
        rank = RANK_TOTAL
    elif any(abs((b - a) - v) <= FUNC_TOL for v in nums):
        rank = RANK_FUNCTION
    elif COUNT_RX.match(text):
        rank = RANK_PATTERN
    else:
        rank = RANK_REST
    mm = {'key': key, 'a': a, 'b': b, 'rank': rank, 'locked': _toleranced(text)}
    coords = [a, b]
    if x is not None and COUNT_RX.match(text):
        cs = []
        if x['kind'] == 'plane_pattern' and x.get('axis_name') == 'xyz'[k] and x.get('coords'):
            cs = sorted(float(c) for c in x['coords'])
        elif x['kind'] == 'pattern':
            cs = sorted(float(_axis(items[q])[0] @ e) for q in x.get('members') or [] if q in items and _axis(items[q])[0] is not None)
        if len(cs) >= 3 and abs(cs[0] - a) <= TOL and abs(cs[-1] - b) <= TOL:
            mm['steps'] = [(cs[i], cs[i + 1], cs[i + 1] - cs[i]) for i in range(len(cs) - 1)]
            coords = cs + coords
    return mm, coords


def _chain_findings(ev, spec, fbp, all_parts, sym, lines, done, frames=None):
    """N5.2 (rank test, chains.derivable): per part and world axis the length dimensions derivable from others."""
    import chains
    out, flagged = [], set(done)
    views = _view_parts(spec)
    defs = _view_defs(spec)
    for pid in ev['manuf']:
        pf = fbp.get(pid) if isinstance(fbp.get(pid), dict) else {}
        bb = pf.get('bbox')
        try:
            nums = spec_function_numbers(spec, pid)[0]      # the same reader as F2
        except Exception:
            nums = []
        planes = _sym_planes(sym, pid)
        for k in range(3):
            e = np.eye(3)[k]
            Ls = [L for L in ev['linear'] if L['parts'] == {pid} and not _assembly(L['g'], all_parts) and _par(L['d'], e) and _pair(L['g'], e) is not None]
            if len(Ls) < 2:
                continue
            measures, coords = [], []
            for n, L in enumerate(Ls):
                g = L['g']
                a, b = _pair(g, e)
                mm, cs = chain_measure(n, str(g.get('text') or ''), a, b, bb, k, nums, ev['items'].get(str(g.get('feature'))), ev['items'])
                measures.append(mm)
                coords += cs
            mirrors = []
            if k in planes and symmetry_axis_shown(lines, k, planes[k], {v for v, ps in views.items() if pid in ps}, frames, _core_axis(ev['items'].values(), pid, k, planes[k]), defs):
                mirrors = chain_mirrors(coords, planes[k])
            twins = chain_twins((x for x in ev['items'].values()), k, pid)
            res = chains.derivable(measures, mirrors=mirrors, twins=twins)
            for n in res['derivable']:
                g = Ls[n]['g']
                if _gkey(g) in flagged:
                    continue
                flagged.add(_gkey(g))
                how = 'chain' + (', symmetry' if mirrors else '') + (', pattern' if twins or any('steps' in mm for mm in measures) else '')
                out.append(_f('dimension_derivable', 'warn', int(g.get('sheet') or 0), None,
                              f"Dimension “{g.get('text')}” ({_where(g)}) follows in direction {_dir_name(e)} on part {pid} from other dimensions ({how}); omit it or mark it as an auxiliary dimension “(…)”",
                              index=g.get('index'), part=pid))
    return out, flagged


def _segments(ev, all_parts):
    """Length dimensions with the same end point pair in the same view: {(sheet, view, A, B): [L]} (part views only)."""
    seg = defaultdict(list)
    for L in ev['linear']:
        g = L['g']
        if _assembly(g, all_parts) or g.get('p1') is None or g.get('p2') is None:
            continue
        a, b = (tuple(np.round(np.asarray(g[k], float), 2) + 0.0) for k in ('p1', 'p2'))
        seg[(int(g.get('sheet') or 0), g.get('view'), *sorted((a, b)))].append(L)
    return seg


def _segment_slant(ev, pid, a, b):
    """Slanted face of the part in whose plane the edge A-B lies (direction ⟂ normal, both ends in the face bbox); also for dimensions without feature (remote)."""
    v = np.asarray(b, float) - np.asarray(a, float)
    n = float(np.linalg.norm(v))
    if n <= TOL:
        return None
    u = v / n
    for x in ev['items'].values():
        if x['kind'] != 'slant' or x['part'] != pid or x.get('normal') is None or x.get('bbox') is None:
            continue
        lo, hi = np.asarray(x['bbox']['min'], float) - 2 * TOL, np.asarray(x['bbox']['max'], float) + 2 * TOL
        if abs(float(u @ _u(x['normal']))) <= 0.02 and all(np.all(p >= lo) and np.all(p <= hi) for p in (np.asarray(a, float), np.asarray(b, float))):
            return x
    return None


def _slant_findings(ev, all_parts, done):
    """N5.3 (slant_by_legs) and the triangle part of N5.2: edge A-B of a slanted face with two legs (two length dimensions with the same end point pair, directions not parallel)."""
    import chains
    out, flagged = [], set(done)
    for (sheet, view, a, b), Ls in _segments(ev, all_parts).items():
        v = np.asarray(b, float) - np.asarray(a, float)
        n = float(np.linalg.norm(v))
        parts = {p for L in Ls for p in L['parts']}
        if n <= TOL or len(parts) != 1:
            continue
        pid = next(iter(parts))
        legs, lens = [], []
        for L in Ls:
            (lens if abs(float(v @ L['d'])) >= 0.999 * n else legs).append(L)
        slant = next((ev['items'][str(L['g']['feature'])] for L in Ls if (ev['items'].get(str(L['g'].get('feature'))) or {}).get('kind') == 'slant'), None) \
            or _segment_slant(ev, pid, a, b)
        if slant is None:
            continue
        dirs = []
        for L in legs:
            if not any(_par(L['d'], q['d']) for q in dirs):
                dirs.append(L)
        if len(dirs) >= 2:
            g = dirs[1]['g']
            out.append(_f('slant_by_legs', 'warn', sheet, None,
                          f"Slanted face {slant['id']} ({_where(g)}): two legs “{dirs[0]['g'].get('text')}” and “{g.get('text')}” instead of length and angle (N5.3);"
                          f"dimension the length along the slanted face and the inclination angle", index=g.get('index'), part=pid, feature=slant['id']))
        items = [{'key': _gkey(L['g']), 'role': 'len', 'g': L['g']} for L in lens] + [{'key': _gkey(L['g']), 'role': 'leg', 'g': L['g']} for L in dirs]
        nrm = _u(slant['normal'])
        for x in ev['items'].values():
            if x['kind'] != 'slant' or x['part'] != pid or x.get('normal') is None or abs(abs(float(_u(x['normal']) @ nrm)) - 1) > 1e-4:
                continue
            items += [{'key': _gkey(g), 'role': 'angle', 'g': g} for g in ev['by'].get(x['id'], []) if g['kind'] == 'angle']
        if len(items) < 3:
            continue
        gof = {i['key']: i['g'] for i in items}
        for key in chains.triangle_derivable(items):
            g = gof[key]
            if key in flagged:
                continue
            flagged.add(key)
            out.append(_f('dimension_derivable', 'warn', int(g.get('sheet') or 0), None,
                          f"Dimension “{g.get('text')}” ({_where(g)}) follows on slanted face {slant['id']} from length, legs and angle (Pythagoras, trigonometry); omit it or mark it as an auxiliary dimension “(…)”",
                          index=g.get('index'), part=pid, feature=slant['id']))
    return out, flagged


def _symmetry_findings(ev, spec, all_parts, frames, sym, lines):
    """N2.4/N2.5 (symmetric_double_dim) and N2.2 (symmetry_line_missing)."""
    out = []
    views = _view_parts(spec)
    defs = _view_defs(spec)
    for pid in ev['manuf']:
        for k, m in _sym_planes(sym, pid).items():
            e = np.eye(3)[k]
            pairs = [(L, _pair(L['g'], e)) for L in ev['linear'] if L['parts'] == {pid} and not _assembly(L['g'], all_parts) and _par(L['d'], e) and _pair(L['g'], e) is not None]
            doubles, mirrored = [], set()
            free = lambda L: not _toleranced(str(L['g'].get('text') or ''))      # toleranced dimensions are spared by N2.4/N2.5 (as N5.2)
            for i, (L, (a, b)) in enumerate(pairs):
                if abs(a + b - 2 * m) <= TOL or not free(L):
                    continue                                         # dimension across the axis: its own mirror image
                for L2, (a2, b2) in pairs[i + 1:]:
                    if free(L2) and _gkey(L2['g']) != _gkey(L['g']) and abs(a2 - (2 * m - b)) <= TOL and abs(b2 - (2 * m - a)) <= TOL:
                        mirrored |= {_gkey(L['g']), _gkey(L2['g'])}
                        doubles.append(L2['g'])
                        break
            for g in doubles:
                out.append(_f('symmetric_double_dim', 'warn', int(g.get('sheet') or 0), None,
                              f"Dimension “{g.get('text')}” ({_where(g)}) on part {pid} mirrors another dimension at the symmetry plane {_dir_name(e)} = {fmt(m)}: two position dimensions of edges instead of one"
                              f"distance across the axis (N2.4/N2.5); draw the symmetry line and omit the dimension", index=g.get('index'), part=pid))
            if not frames:
                continue
            # N2.2: per view of the part in which the plane appears as a line and the dimensioning relies on it
            located = [x for x in ev['items'].values() if x['part'] == pid and x['kind'] in LOCATED and _axis(x)[0] is not None]
            dimmed = set()                                           # form features with their own position dimension along e
            for L, _ in pairs:
                dimmed |= set(L['hits'][0] + L['hits'][1]) | {str(t) for t in [L['g'].get('feature'), *(L['g'].get('also') or [])] if t}
            on_axis = _core_axis(ev['items'].values(), pid, k, m)      # axis of a hole/shaft in the plane: the core draws it as a centerline
            for vid, fr in frames.items():
                if views.get(vid) != [pid] or not fr.get('axes') or defs.get(vid, {}).get('detail'):
                    continue
                N = np.asarray(fr['axes'].get('toward_camera'), float)
                if abs(float(N @ e)) > 1e-3 or symmetry_axis_shown(lines, k, m, {vid}, frames, on_axis, defs):
                    continue
                why = None
                for L, (a, b) in pairs:
                    if L['g'].get('view') != vid:
                        continue
                    if abs(a - m) <= TOL or abs(b - m) <= TOL:
                        why = 'a dimension starts or ends on the plane'
                    elif abs(a + b - 2 * m) <= TOL:
                        why = 'a dimension spans the plane symmetrically'
                    elif _gkey(L['g']) in mirrored:
                        why = 'a mirror pair of equal dimensions'
                    else:
                        for fid in dict.fromkeys(L['hits'][0] + L['hits'][1]):
                            x = ev['items'].get(fid)
                            if x is None or x['kind'] not in LOCATED or _axis(x)[0] is None:
                                continue
                            c = float(_axis(x)[0] @ e)
                            partner = next((q for q in located if q['id'] != fid and abs(c - m) > TOL and abs(float(_axis(q)[0] @ e) - (2 * m - c)) <= TOL
                                            and equal_key(q) == equal_key(x)), None)
                            if partner is not None and partner['id'] not in dimmed:
                                why = f"{partner['id']} as the mirror partner of {fid} carries no position dimension of its own"
                                break
                    if why:
                        break
                if why:
                    out.append(_f('symmetry_line_missing', 'warn', int(fr.get('sheet') or 0), None,
                                  f"View {vid} (part {pid}): the symmetry plane {_dir_name(e)} = {fmt(m)} appears as a line and the dimensioning relies on it ({why}),"
                                  f"but no symmetry line (center) is drawn (N2.2)", view=vid, part=pid))
    return out


def _centerline_findings(ev, spec, frames, lines):
    """N2.3 (centerline_missing): hole/shaft visible in an axis-parallel view without automatic centerlines (centerlines: false), without centerline/center cross on the axis."""
    out = []
    if not frames:
        return out
    views = _view_parts(spec)
    defs = _view_defs(spec)
    for vid, fr in frames.items():
        vd = defs.get(vid)
        if vd is None or vd.get('centerlines') is not False or vd.get('detail') or not fr.get('axes') or len(views.get(vid) or []) != 1 or views[vid][0] not in ev['manuf']:
            continue
        pid = views[vid][0]
        N = np.asarray(fr['axes'].get('toward_camera'), float)
        k = int(np.argmax(np.abs(N)))
        if abs(abs(N[k]) - 1) > 1e-6:
            continue
        side = SIDE_OF[(k, 1 if N[k] > 0 else -1)]
        scale = float(fr.get('scale') or 1.0)
        mine = [p for v, p in lines if v == vid]
        for x in ev['items'].values():
            if x['part'] != pid or x['kind'] not in ('hole', 'shaft') or x.get('axis') is None or x.get('start') is None:
                continue
            if x.get('visible_from') is not None and side not in x['visible_from']:
                continue
            if scale * float(x['diameter']) / 2 < MIN_CENTER_MM:
                continue
            o, d = np.asarray(x['start'], float), _u(x['axis'])
            if any(float(np.max(np.linalg.norm(np.cross(p - o, d), axis=1))) <= 0.05 for p in mine):
                continue
            out.append(_f('centerline_missing', 'warn', int(fr.get('sheet') or 0), None,
                          f"View {vid} (part {pid}): {_label(x)} ({x['id']}) is visible, the view does not draw its centerlines (centerlines: false) and no centerline lies"
                          f"on the axis (N2.3)", view=vid, part=pid, feature=x['id']))
    return out


def n2_n5_findings(spec, features_by_part, geos, notes_text, tables, frames=None, sym=None, prior=(), ev=None):
    """Measurement only (warn, no drawing behavior): N2 (symmetry_line_missing, centerline_missing, symmetric_double_dim) and N5.2/N5.3 (dimension_derivable, slant_by_legs); the duplication
    of dimensions (N5.1) is measured by feature_findings (dimension_duplicate_copy, dimension_redundant). frames = view_frames of the render (axes, scale), sym = {part: {world axis: coordinate of the
    symmetry plane}} (symmetry.mirror_planes, whole part); without frames the view findings (N2.2, N2.3) are dropped, without sym all mirror findings. prior = findings already reported
    (chain_closed, dimension_overdetermined, dimension_redundant, dimension_duplicate_copy): their annotations get no second finding for the same matter. Auxiliary dimensions "(…)", toleranced dimensions and
    dimensions in assembly views are never derivable."""
    ev = ev or _evaluate(spec, features_by_part, geos, notes_text, tables, False, sym, frames)      # the same evaluation as the coverage (strict changes only status/problems)
    all_parts = [p['id'] for p in spec.get('parts') or []] or list(features_by_part)
    lines = _drawn_lines(spec)
    done = {(int(f.get('sheet') or 0), f['index']) for f in prior if f.get('index') is not None and f.get('code') in
            ('chain_closed', 'dimension_overdetermined', 'dimension_redundant', 'dimension_duplicate_copy', 'dimension_derivable')}
    out, done = _chain_findings(ev, spec, features_by_part, all_parts, sym, lines, done, frames)
    legs, done = _slant_findings(ev, all_parts, done)
    return out + legs + _symmetry_findings(ev, spec, all_parts, frames, sym, lines) + _centerline_findings(ev, spec, frames, lines)


def feature_tag_findings(spec, features_by_part, geos, strict):
    """feature_tag_unknown (block when strict, otherwise warn): annotation whose tag 'feature' names no form feature of the parts of its
    view (geo['parts'], without a view all parts) - neither a form feature/pattern from features_by_part nor a
    mandatory element of a freeform part (bounding box dimension, freeform note). The coverage discards such tags (_tag_ok);
    this used to happen silently. features_by_part must contain every part a tag points to (render: the
    manufactured parts and every shown part whose ID precedes the '#' of a tag); None = form features unknown
    (detection failed): tags that begin with "<part>#" then stay unchecked."""
    sev = 'block' if strict else 'warn'
    every = [p['id'] for p in spec.get('parts') or []] or list(features_by_part)
    known = {}
    for pid, pf in features_by_part.items():
        if pf is None:
            continue
        pf = pf if isinstance(pf, dict) else {'features': pf}
        known[pid] = {f['id'] for f in pf['features']} | (
            {f'{pid}#extent_{a}' for a in 'xyz'} | {f'{pid}#freeform_note'} | {r['id'] for r in pf.get('regions') or []}
            if pf.get('freeform') else {r['id'] for r in pf.get('regions') or [] if r.get('facet')}) | {P['id'] for P in pf.get('profiles') or []}      # a profile may stand as a tag (profile dimensions, profile_findings)
    unknown = [pid for pid, pf in features_by_part.items() if pf is None]
    out = []
    for g in geos:
        listed = [t for key in ('features', 'model_ref') if g.get(key) is not None
                  for t in (g[key] if isinstance(g[key], list) else [g[key]])]          # members of a collective note/reference
        for tag in dict.fromkeys(str(t) for t in ([g['feature']] if g.get('feature') else []) + list(g.get('also') or []) + listed):
            if any(tag in known.get(pid, ()) for pid in g.get('parts') or every) or any(tag.startswith(pid + '#') for pid in unknown):
                continue
            out.append(_f('feature_tag_unknown', sev, int(g.get('sheet') or 0), None,
                          f"Annotation “{g.get('text') or g.get('kind')}” ({_claim_where(g)}) names the form feature {tag}, which none "
                          f"of the shown parts has: the tag does not count for coverage", index=g.get('index'), feature=tag))
    return out


# ------------------------------------------------------------------ N4.10: profile shapes in true size
PROFILE_COS = 0.999      # viewing direction of the view ∥ generatrix d: |cos| at least this large
PROFILE_ANGLE_TOL = 0.11  # degrees: angle dimension against the mandatory statement (dimension text with one decimal)
RESIDUAL_KINDS = ('slant', 'cone', 'plane', 'freeform_region')      # kinds of the members that today are covered only via the row "Undimensioned forms according to 3D model"


def _geo_value(g):
    """Value of a dimension for the comparison with profile_dims: Ø, R, angle (degrees) or length along the dimension direction."""
    k = g.get('kind')
    if k == 'diameter' and g.get('radius') is not None:
        return 2.0 * float(g['radius'])
    if k == 'radius' and g.get('radius') is not None:
        return float(g['radius'])
    if k == 'angle' and g.get('value') is not None:
        return float(g['value'])
    if k == 'linear' and g.get('p1') is not None and g.get('p2') is not None:
        v = np.asarray(g['p2'], float) - np.asarray(g['p1'], float)
        d = _u(g['dir3']) if g.get('dir3') is not None else None
        return abs(float(v @ d)) if d is not None else float(np.linalg.norm(v))
    return None


def _profile_text(x):
    n = f"{x['n']}× " if x['n'] > 1 else ''
    return n + {'diameter': 'Ø' + fmt(x['value']), 'radius': 'R' + fmt(x['value']), 'angle': fmt(x['value'], 1) + '°'}.get(x['kind'], fmt(x['value']))


def profile_members(P):
    """All members of the class: instance and copies."""
    return [*(P.get('members') or []), *[m for c in P.get('copy_members') or [] for m in c]]


def profile_residual(P, items, minor):
    """{member ID: kind} of the residual members of the profile (instance and copies): slanted faces, cones, planes and freeform regions that are minor faces (minor) and today would be covered only via the row "Undimensioned
    forms according to 3D model". Arcs, shafts, roundings and holes go via their own coverage path (no double dimension)."""
    return {m: items[m]['kind'] for m in profile_members(P) if m in minor and m in items and items[m].get('kind') in RESIDUAL_KINDS}


def profile_need(P, items, minor):
    """(residual members, mandatory list profiles.required_dims): empty if the profile has no residual members (then everything stays with the previous coverage path)."""
    import profiles as prof
    res = profile_residual(P, items, minor)
    return res, (prof.required_dims(P, set(res.values())) if res else [])


def _profile_view_ok(P, frames, vid):
    """Does view vid show the profile in true size? View ∥ d and either a section transverse to d (frames: is_section) or a standard view from which an end of the instance is visible
    (P['visible_from']; if the key is missing, without restriction)."""
    import projection as prj
    cam, d = _cam(frames, vid), _u(P['axis']) if P.get('axis') is not None else None
    if cam is None or d is None or abs(float(_u(cam) @ d)) < PROFILE_COS:
        return False
    if (frames or {}).get(vid, {}).get('is_section') or P.get('visible_from') is None:
        return True
    return any(float(_u(cam) @ _u(prj.PRESETS[n][0])) > PROFILE_COS for n in P['visible_from'] if n in prj.PRESETS)


def _profile_matches(x, mine):
    """Is the mandatory size x (or one of its equivalent alternatives) among the dimensions mine [(kind, value)]?"""
    for e in [x, *(x.get('alt') or [])]:
        tol = PROFILE_ANGLE_TOL if e['kind'] == 'angle' else FUNC_TOL + 0.001
        if any(k == e['kind'] and abs(v - e['value']) <= tol for k, v in mine):
            return True
    return False


def profile_missing(P, pid, need, geos, assigned, frames, all_parts):
    """The mandatory sizes need of profile P that stand in no view with view ∥ d (list of the missing ones). A dimension belongs to the profile if its tag (feature, also, features, model_ref) names the profile or one of
    its members, it is geometrically assigned to a member (assigned: object identifiers) or it carries no tag at all (sheets without element reference), it does not stand in an assembly view and is not an
    auxiliary dimension, the view shows the profile (_profile_view_ok) and the value (or an equivalent alternative) is equal."""
    ids = {P['id'], *profile_members(P)}
    mine, elsewhere = [], []
    for g in geos:
        if _aux(g) or (g.get('parts') and pid not in g['parts']) or _assembly(g, all_parts):
            continue
        if g.get('kind') == 'linear' and g.get('dir3') is not None and (v := _geo_value(g)) is not None:
            dd = _u(g['dir3'])
            if g.get('p1') is not None:
                elsewhere.append((dd, *sorted(float(np.asarray(g[k], float) @ dd) for k in ('p1', 'p2'))))      # length dimensions of the part in every view (the same segment, profile_stated)
        if not _profile_view_ok(P, frames, g.get('view')):
            continue
        tags = {str(t) for key in ('feature', 'also', 'features', 'model_ref') if g.get(key) is not None for t in (g[key] if isinstance(g[key], list) else [g[key]])}
        if tags & ids or id(g) in assigned or (not tags and g.get('kind') != 'note'):      # without any tag (remote sheets): the value in the view along d is enough
            v = _geo_value(g)
            if v is not None:
                mine.append((g['kind'], v))
    return [x for x in need if not _profile_matches(x, mine) and not profile_stated(P, x, elsewhere)]


def profile_direction(P, x):
    """Direction (world, unit vector) of a length dimension of the mandatory list: the profile axis for coordinate dimensions (geom['axis']), otherwise foot point to foot point; None for angles and without a profile frame."""
    g = x.get('geom') or {}
    if x['kind'] != 'linear' or 'p1' not in g or not P.get('frame'):
        return None
    u, w = (_u(P['frame'][k]) for k in ('u', 'v'))
    if g.get('axis') is not None:
        return (u, w)[int(g['axis'])]
    v = (g['p2'][0] - g['p1'][0]) * u + (g['p2'][1] - g['p1'][1]) * w
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else None


def profile_stated(P, x, elsewhere):
    """Does the length dimension x of the mandatory list already stand on the part? elsewhere = [(direction, smaller, larger end coordinate along the direction)] of the length dimensions of the part (checker: annotations, planner: its
    general dimensions and the already placed profile dimensions). One decision for both (each dimension once): the segment is already fixed by the dimensions of the same direction - the same segment
    (N5.1 `dimension_redundant`) or a chain over shared end coordinates (N5.2 `chain_closed`; chain_determined, the same computation). The planner does not set such a dimension,
    the checker does not demand it and does not report it as derivable."""
    d = profile_direction(P, x)
    if d is None:
        return False
    g, o = x['geom'], np.asarray(P['frame']['origin'], float)
    u, w = (_u(P['frame'][k]) for k in ('u', 'v'))
    lo, hi = sorted(float((o + q[0] * u + q[1] * w) @ d) for q in (g['p1'], g['p2']))
    def along(dd, l, h):
        c = float(dd @ d)
        if abs(c) <= PROFILE_COS:
            return None
        return (-h, -l) if c < 0 else (l, h)     # antiparallel: coordinates along d (negate and swap) - the only place of the sign handling
    return chain_determined([q for q in (along(dd, l, h) for dd, l, h in elsewhere) if q is not None], lo, hi)


def profile_candidates(d, anchor=None):
    """Viewing directions (normals) of the standard views in which a detail view or a section along the generatrix d can arise: the six directions ±N, ±Vx, ±Vy of the anchor (main view,
    frame (N, Vx, Vy)) - anchor, its four neighbor views and the opposite view (plan_views); without anchor the world axes. The only candidate choice: the planner builds the view from it, the checker queries it."""
    N, Vx, Vy = (np.asarray(v, float) for v in anchor) if anchor is not None else tuple(np.eye(3))
    d = _u(d)
    return [n for n in (N, -N, Vx, -Vx, Vy, -Vy) if abs(float(n @ d)) > 1 - 1e-3]


def profile_buildable(P, anchor=None):
    """Can a view along d be built? Profile frame present and a candidate direction (profile_candidates) lies along d. Planner (plan_views.profile_view) and checker ask the same
    candidate choice. Otherwise the profile is not upgraded: the residual members keep the row "Undimensioned forms", the finding stays warn (planner: open)."""
    return P.get('axis') is not None and P.get('frame') is not None and bool(profile_candidates(P['axis'], anchor))


def _part_anchor(spec, pid):
    """Frame (N, Vx, Vy) of the first view of the part in the spec (the anchor of the planner); None if the spec has none."""
    import projection as prj
    for sd in spec.get('sheets') or []:
        for vd in sd.get('views') or []:
            if isinstance(vd, dict) and list(vd.get('parts') or []) == [pid]:
                try:
                    return tuple(prj.frame_of(vd))
                except (KeyError, ValueError, TypeError):
                    return None
    return None


def profile_state(spec, pfs, items, minor, geos, shown_by, frames, manuf):
    """{profile ID: {'part', 'profile', 'residual': {member: kind}, 'need': [...], 'missing': [...]}} of the profiles with residual members and non-empty mandatory list."""
    all_parts = [p['id'] for p in spec.get('parts') or []] or list(pfs)
    out = {}
    for pid in manuf:
        anchor = _part_anchor(spec, pid)
        for P in (pfs.get(pid) or {}).get('profiles') or []:
            res, need = profile_need(P, items, minor)
            if need:
                assigned = {id(g) for m in profile_members(P) for g in shown_by.get(m, [])}
                out[P['id']] = {'part': pid, 'profile': P, 'residual': res, 'need': need, 'buildable': profile_buildable(P, anchor),
                                'missing': profile_missing(P, pid, need, geos, assigned, frames, all_parts)}
    return out


def profile_findings(spec, features_by_part, geos, ev, frames=None, strict=False):
    """N4.10: profile_not_dimensioned (block for strict, otherwise warn) per profile (features.profiles) with residual members whose mandatory list (profiles.required_dims,
    residual geometry only; equivalent alternatives suffice) is not completely present in views with viewing direction ∥ d that show the profile (section transverse to d or standard view with a free
    end of the instance; dimensions in assembly views do not count). Assignment see profile_missing. Returns ([finding], {'profiles': n, 'profiles_dimensioned': m}): n all profiles, m those without
    residual members or completely dimensioned. The residual members (ev['prof_of']; only with a buildable view along d) are covered via the profile and leave the minor-face row: if the profile is missing, their coverage is missing."""
    state = ev.get('profile_state')
    if state is None:
        pfs = {pid: (v if isinstance(v, dict) else {'features': v}) for pid, v in features_by_part.items()}
        by = ev.get('shown_by') or ev.get('by') or {}
        state = profile_state(spec, pfs, ev['items'], ev.get('minor') or set(), geos, by, frames, ev['manuf'])
    out, n = [], 0
    for pid in ev['manuf']:
        pf = features_by_part.get(pid)
        n += len((pf.get('profiles') if isinstance(pf, dict) else None) or [])
    done = n - sum(1 for s in state.values() if s['missing'])
    for P_id, s in state.items():
        if not s['missing']:
            continue
        P, d = s['profile'], _u(s['profile']['axis'])
        sev = 'block' if strict and s['buildable'] else 'warn'      # not buildable -> never upgraded
        out.append(_f('profile_not_dimensioned', sev, 0, None,
                      f"Profile {P_id} ({P['copies']}× lateral faces along {_dir_name(d)}, {fmt(P['share'] * 100, 1)} % of the surface) not completely dimensioned in true size"
                      f"(view looking along the generatrix), missing: {', '.join(_profile_text(x) for x in s['missing'])}", part=s['part'], feature=P_id,
                      missing=[x['key'] for x in s['missing']]))
    return out, {'profiles': n, 'profiles_dimensioned': done}


# ------------------------------------------------------------------ Section consistency, information content, legibility
SECTION_TITLE = re.compile(r'\b([A-Z])\s*[-–]\s*\1\b')
DETAIL_TITLE = re.compile(r'Detail ([A-Z])')
AUX_TITLE = re.compile(r'\bView ([A-Z])\b')
NO_NEW_VIEW = ('iso', 'iso_back')


def is_iso(vd):
    """Isometric: preset iso/iso_back or (planner's octant choice) a viewing direction with three equal components."""
    if vd.get('preset'):
        return vd['preset'] in NO_NEW_VIEW
    d = vd.get('direction')
    return d is not None and bool(np.allclose(np.abs(np.asarray(d, float) / np.linalg.norm(d)), 1 / np.sqrt(3), atol=1e-3))
MARK_TYPES = ('centermark', 'section_line', 'detail_circle', 'view_arrow')   # markings carry no information


def bare_polyline(a):
    """Polyline without dimension, generated by the planner: reference line of an angle dimension (`role: slant_ref`) or symmetry axis (`symmetry_axis`). It shows nothing by itself, it only belongs to a dimension
    or to the partial geometry. Polylines from the spec without these markers (remote) stay information."""
    return a.get('type') == 'polyline' and (a.get('role') == 'slant_ref' or bool(a.get('symmetry_axis')))


def informative_annotation(a):
    """Does the annotation carry information (view_without_information, view without an element of its own)? Markings (MARK_TYPES) and polylines without dimension (bare_polyline) do not."""
    return a.get('type') not in MARK_TYPES and not bare_polyline(a)
MIN_FEATURE_MM = 2.0       # mm on paper: smaller form features need a detail view at a larger scale
SMALL_KINDS = ('hole', 'shaft', 'sphere', 'arc', 'square')       # chamfers/roundings cover notes, no detail view needed


def _sheet_anns(sd):
    return [a for a in list(sd.get('annotations') or []) + list(sd.get('dims') or []) if isinstance(a, dict)]


def _scale(text):
    try:
        a, b = str(text).replace(' ', '').split(':')
        return float(a) / float(b)
    except (ValueError, ZeroDivisionError):
        return None


def section_findings(spec, strict, feats_of=None):
    """section_inconsistent: section view without a section line of the same label on the same sheet (and vice versa),
    likewise detail view and detail_circle as well as auxiliary view (auxiliary) and view_arrow. Letter of the view:
    section.label / detail.label / auxiliary.label, otherwise from the title (view.label "A-A", "Detail X" or "View X").
    Without a determinable letter any line or any arrow on the sheet suffices.
    N4.1 (ISO 128-3): a section without label letter through the rotation axis of a turned part (axis_section_view, form features via feats_of(part ID) -> {features, freeform})
    needs no section line; it also does not count as a view without letter (the check of orphaned lines remains). Without feats_of the obligation applies as before."""
    sev = 'block' if strict else 'warn'
    out = []
    all_parts = [p.get('id') for p in spec.get('parts') or [] if isinstance(p, dict)]
    for si, sd in enumerate(spec.get('sheets') or []):
        anns = _sheet_anns(sd)
        for key, atype, default, rx, word, mark in (('section', 'section_line', 'A', SECTION_TITLE, 'section', 'section line'),
                                                     ('detail', 'detail_circle', 'Z', DETAIL_TITLE, 'detail', 'detail circle'),
                                                     ('auxiliary', 'view_arrow', 'X', AUX_TITLE, 'auxiliary view', 'view arrow mark')):
            marks = [(str(a.get('label') or default), a) for a in anns if a.get('type') == atype]
            vws = []
            for vd in sd.get('views') or []:
                if isinstance(vd.get(key), dict) and not (key == 'auxiliary' and vd.get('section')):   # auxiliary section: section line
                    m = rx.search(str(vd.get('label') or ''))
                    letter = vd[key].get('label') or (m.group(1) if m else None)
                    if key == 'section' and letter is None and axis_section_view(vd, all_parts, feats_of):
                        continue                    # axial section of a turned part, course unambiguous
                    vws.append((vd.get('id'), letter))
            have, shown = {l for l, _ in marks}, {l for _, l in vws if l}
            for vid, letter in vws:
                if (letter is not None and letter not in have) or (letter is None and not marks):
                    out.append(_f('section_inconsistent', sev, si, None,
                                  f"View {vid} ({word}{' ' + letter if letter else ''}, sheet {si + 1}) has no {mark} "
                                  f"{letter or ''} on the same sheet: add a {mark} with the same identifying letter", view=vid))
            if not any(l is None for _, l in vws):
                for letter, a in marks:
                    if letter not in shown:
                        out.append(_f('section_inconsistent', sev, si, None,
                                      f"{mark} {letter} (sheet {si + 1}) has no matching view on the same sheet",
                                      index=next((i for i, x in enumerate(anns) if x is a), None)))
    return out


def view_information_findings(spec, strict=False):
    """view_without_information. (1) Section or detail view (not an auxiliary view without a section) without its own annotation, i.e.
    without its own covered element: block when strict, otherwise warn. (2) View (not iso/iso_back, no section, no
    detail view) without an annotation, without a label table, whose parts all also appear in other views (any sheet):
    always warn. Isometrics (undimensioned pictorial view) are exempt from both; the main view carries its dimensions
    itself (1) or falls under (2)."""
    all_ids = [p['id'] for p in spec.get('parts') or []]
    views = [(si, vd) for si, sd in enumerate(spec.get('sheets') or []) for vd in sd.get('views') or []]
    parts_of = lambda vd: set(vd.get('parts') or all_ids)
    out = []
    for si, vd in views:
        if is_iso(vd):
            continue
        sd = spec['sheets'][si]
        anns = _sheet_anns(sd)
        has_ann = any(a.get('view') == vd.get('id') and informative_annotation(a) for a in anns)
        if vd.get('section') or vd.get('detail'):
            # host: carries the detail circle of a detail view that itself has a dimension (computed from the spec, no planner flag)
            hosts = any(a.get('type') == 'detail_circle' and a.get('view') == vd.get('id') and any(
                isinstance(o.get('detail'), dict) and str(o['detail'].get('label') or 'Z') == str(a.get('label') or 'Z') and any(
                    b.get('view') == o.get('id') and informative_annotation(b) for b in anns) for o in sd.get('views') or [])
                for a in anns)
            if not has_ann and not hosts:
                kind = 'Detail' if vd.get('detail') else 'Section'
                out.append(_f('view_without_information', 'block' if strict else 'warn', si, None,
                              f"{kind} {vd.get('id')} (sheet {si + 1}) covers no element of its own: no dimensions in this view; "
                              f"omit it (content into the parent view) or dimension it", view=vd.get('id')))
            continue
        if any(a.get('view') == vd.get('id') for a in _sheet_anns(sd)) or any(
                isinstance(t, dict) and t.get('labels_in') == vd.get('id') for t in sd.get('tables') or []):
            continue
        others = set().union(*[parts_of(o) for _, o in views if o is not vd]) if len(views) > 1 else set()
        if parts_of(vd) <= others:
            out.append(_f('view_without_information', 'warn', si, None,
                          f"View {vd.get('id')} (sheet {si + 1}) shows nothing new: no dimensions or labels and all parts "
                          f"appear in other views; omit it or dimension it", view=vd.get('id')))
    return out


def projection_sides(vd, ref):
    """Projection sides of two views, including an opposite view with a common vertical axis."""
    import projection as prj
    return set(prj.first_angle_sides(prj.frame_of(vd), prj.frame_of(ref)))


def view_unnamed_findings(spec, strict=False):
    """view_unnamed (warn): a view that does not stand in projection position to its main view (the first view of its parts on the sheet)
    carries a name (view.label: "View X" with view arrow, section "A-A", detail view). In projection position means: place.relative_to
    is the main view (or a view itself in projection position) and place.side is the side on which the viewing direction stands.
    Isometric views (undimensioned visualization) are exempt."""
    out = []
    all_parts = frozenset(p['id'] for p in spec.get('parts', []) if isinstance(p, dict) and 'id' in p)
    def group(vd):
        return frozenset(vd.get('parts') or all_parts)

    for si, sd in enumerate(spec.get('sheets') or []):
        views = [vd for vd in sd.get('views') or [] if isinstance(vd, dict)]
        by = {vd.get('id'): vd for vd in views}
        main = {}
        for vd in views:
            if not vd.get('detail') and not is_iso(vd):
                main.setdefault(group(vd), vd.get('id'))
        for vd in views:
            main.setdefault(group(vd), vd.get('id'))

        def named(vd):
            return bool(vd.get('label') or vd.get('section') or vd.get('detail') or vd.get('auxiliary'))

        def projected(vd):
            seen = set()
            while vd.get('id') not in seen:
                seen.add(vd.get('id'))
                pl = vd.get('place') or {}
                ref = by.get(pl.get('relative_to'))
                if ref is None or group(vd) != group(ref):
                    return False
                try:
                    if pl.get('side') not in projection_sides(vd, ref):
                        return False
                except (KeyError, ValueError, TypeError):
                    return False
                if main.get(group(ref)) == ref.get('id') or named(ref):
                    return True
                vd = ref
            return False
        for vd in views:
            if named(vd) or is_iso(vd) or main.get(group(vd)) == vd.get('id') or projected(vd):
                continue
            out.append(_f('view_unnamed', 'warn', si, None,
                          f"View {vd.get('id')} (sheet {si + 1}) is not in projection position to its main view and has no name:"
                          f"name it “View X” with view arrow, section or detail view", view=vd.get('id')))
    return out


SECTION_MARK_GAP = 8.0       # mm on paper: label letters (5 mm) of two section lines/view arrows of the same view are at least this far apart (planner: 9 mm)


def section_mark_findings(spec, strict=False):
    """section_marks_close (warn, strict block): the ends of two section lines or view arrows of the same view (the
    identifying letter stands there, ISO 128-3) are closer than SECTION_MARK_GAP on paper: the letters touch or overlap. Complements text_overlap, which only reports on
    a real overlap of the text boxes (the letters "D" and "F" side by side would go unnoticed)."""
    import projection as prj
    out = []
    for si, sd in enumerate(spec.get('sheets') or []):
        views = {vd.get('id'): vd for vd in sd.get('views') or []}
        pts = []
        for i, a in enumerate(sd.get('annotations') or []):
            vd = views.get(a.get('view'))
            if vd is None or a.get('type') not in ('section_line', 'view_arrow'):
                continue
            keys = ('p1', 'p2') if a['type'] == 'section_line' else ('p',)
            try:
                q = prj.project([a[x] for x in keys if a.get(x) is not None], prj.frame_of(vd)) * _scale(vd.get('scale', sd.get('scale', '1:1')))
            except (KeyError, ValueError):
                continue
            pts += [(i, vd['id'], str(a.get('label') or ''), p) for p in q]
        reported = set()
        for x in range(len(pts)):
            for y in range(x):
                (i, v1, l1, p1), (j, v2, l2, p2) = pts[x], pts[y]
                if v1 == v2 and i != j and (i, j) not in reported and float(np.linalg.norm(p1 - p2)) < SECTION_MARK_GAP - 1e-6:
                    reported.add((i, j))
                    out.append(_f('section_marks_close', 'block' if strict else 'warn', si, None,
                                  f"Identifying letters “{l1}” and “{l2}” in view {v1} (sheet {si + 1}) are only {fmt(float(np.linalg.norm(p1 - p2)), 1)} mm apart "
                                  f"(minimum {fmt(SECTION_MARK_GAP, 0)} mm): shift the section line along its direction", view=v1))
    return out


def budget_findings(spec, strict=False):
    """view_over_budget: view with more than BUDGET_VIEW entries (BUDGET_TYPES; view labels, section/detail marks,
    centre marks and item numbers do not count): block when strict, otherwise warn."""
    out = []
    for si, sd in enumerate(spec.get('sheets') or []):
        count = {}
        for a in _sheet_anns(sd):
            if a.get('view') and a.get('type') in BUDGET_TYPES:
                count[a['view']] = count.get(a['view'], 0) + 1
        for vid, n in count.items():
            if n > BUDGET_VIEW:
                out.append(_f('view_over_budget', 'block' if strict else 'warn', si, None,
                              f"View {vid} (sheet {si + 1}) carries {n} entries, maximum {BUDGET_VIEW}: move dimensions to another view or "
                              f"a section", view=vid))
    return out


def _in_detail(vd, f):
    """Position of the form feature (axis ends or center) in the circle of detail view vd (in the image plane)."""
    import projection as prj
    det = vd['detail']
    pts = [f[k] for k in ('start', 'end', 'center') if f.get(k) is not None]
    frame = prj.frame_of(vd)
    c = prj.project([det['center']], frame)[0]
    return any(np.linalg.norm(q - c) <= float(det['radius']) + TOL for q in prj.project(pts, frame))


def covered_groups(spec, fby):
    """Elements that an enlarged instance also covers - {element ID: [members of the group]}. A group is a pattern (all members)
    or the equal elements of ONE n× dimension (ISO 129-1: once with n× on the enlarged instance): annotation with feature and also that stands in a view
    in which the element of the dimension (feature) is shown large enough (characteristic distance x scale >= MIN_FEATURE_MM, for a detail view in the circle); only the members are counted
    that are equal under this dimension (copy_key like collective_size_mismatch). The check "too small" (small_feature_findings) then demands the enlargement only on one
    member of the group; the planner (plan_views.prune_views) drops a detail view whose element is covered via an n× dimension, by the same coverage (_coverage: feature +
    also; its n× is set by plan_dims only for equal members)."""
    group = {}
    for p in fby.values():
        if p.get('kind') == 'pattern':
            for m in p.get('members') or []:
                group.setdefault(m, []).extend(fby[x] for x in p['members'] if x in fby)
    for sd in spec.get('sheets') or []:
        views = {vd.get('id'): vd for vd in sd.get('views') or []}
        for a in _sheet_anns(sd):
            vd, anchor = views.get(a.get('view')), fby.get(str(a.get('feature')))
            if not a.get('also') or vd is None or anchor is None:
                continue
            k = _scale(vd.get('scale', sd.get('scale', '1:1')))
            size = anchor.get('size')
            if not k or not size or float(size) * k < MIN_FEATURE_MM - 1e-9:
                continue
            if isinstance(vd.get('detail'), dict) and not _in_detail(vd, anchor):
                continue
            g = dict(a, kind=a.get('type'))
            key = copy_key(anchor, g)
            ids = [str(t) for t in dict.fromkeys([a.get('feature'), *(a.get('also') or [])]) if t and str(t) in fby
                   and (str(t) == str(a.get('feature')) or (key is not None and copy_key(fby[str(t)], g) == key))]
            if len(ids) > 1:
                for m in ids:
                    group.setdefault(m, []).extend(x for x in (fby[t] for t in ids) if x not in group.get(m, []))
    return group


def small_feature_findings(spec, features_by_part):
    """feature_too_small (always warn): form feature of a manufactured part that is smaller than MIN_FEATURE_MM on paper in every view of the part (also sections and
    detail views). Scale of a view: view.scale, otherwise sheet scale."""
    import projection as prj
    by_id = {p['id']: p for p in spec.get('parts') or []}
    out = []

    for pid, pf in features_by_part.items():
        if part_role(by_id.get(pid, {})) != 'manufactured':
            continue
        vds = [(vd, _scale(vd.get('scale', sd.get('scale', '1:1')))) for sd in spec.get('sheets') or [] for vd in sd.get('views') or []
               if pid in (vd.get('parts') or by_id)]
        vds = [(vd, k) for vd, k in vds if k]
        flist = pf['features'] if isinstance(pf, dict) else pf
        fby = {f['id']: f for f in flist}   # not by_id: those are the parts of the spec (role, views without 'parts')
        group = covered_groups(spec, fby)      # member of a pattern or of an n× dimension -> all members (a detail view on one member covers the group)
        for f in flist:
            size = f.get('size')
            if f['kind'] not in SMALL_KINDS or not size:
                continue
            # Detail views count only for elements in their circle (or a member of the same pattern), sections and
            # standard views for the whole part
            ks = [k for vd, k in vds if not isinstance(vd.get('detail'), dict) or any(_in_detail(vd, g) for g in group.get(f['id'], [f]))]
            kmax = max(ks) if ks else None
            if kmax and size * kmax < MIN_FEATURE_MM:
                out.append(_f('feature_too_small', 'warn', 0, None,
                              f"{_label(f)} ({f['id']}) is only {fmt(size * kmax, 1)} mm in size in all views (smaller than {fmt(MIN_FEATURE_MM, 1)} mm): "
                              f"add a detail view at a larger scale", part=pid, feature=f['id']))
    return out
