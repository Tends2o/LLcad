"""Deterministic layout variants after autofix: pure block displacement on the rendered spec.

Input: (spec, view_frames) of a single-sheet rendered drawing after autofix.
Produces at most MAX_VARIANTS (4) admissible arrangement variants:
  - original: unchanged position after autofix
  - mirror_lr: left-right mirroring of the blocks in the drawing area (with sub-box-exact zone resolution)
  - swap_row / swap_pair: ordered exchange of places of neighbouring blocks or main/secondary part groups
  - asm_corner / corner_alt: assembly in another free corner (or main block corner change for single parts)
  - shelf_grouped / vertical_swap: shelf packing with host proximity or exchange of upper/lower block row

All variants use sub-box-exact collision checking (including headings of detail views/sections/isometric views),
N1.8 axis alignment (aligning the geom_bbox centres of neighbouring blocks) and are filtered strictly:
drawing area, title block/parts list zone, minimum distance >= GAP_MM, N1.9 (assembly_between == 0 via
plan_layout.zone_blocked) and unchanged projection alignment (place stays untouched).
The variant with the highest layout score is at position 0 (defending champion).
"""
from __future__ import annotations

import copy
import math
import sys
from pathlib import Path

_DRAWING_DIR = str(Path(__file__).resolve().parents[1])
if _DRAWING_DIR not in sys.path:
    sys.path.insert(0, _DRAWING_DIR)

import checks
import plan_layout as pl
from sheet import parts_list_height, sheet_size

MAX_VARIANTS = 4
MIN_DIFF_MM = 15.0          # minimum displacement of a block for a variant to count as distinct
VIEW_GAP_MM = 6.5           # safe minimum distance between view geometries (> checks.VIEW_GAP_MM = 6 mm)
SUBBOX_GAP_MM = 2.0         # minimum distance between measured ann_bbox_mm / titles / notes of different blocks
BLOCK_PAD_MM = 0.5          # small safety margin around measured view boxes
STACK_H = 46.0              # title block height (plan.STACK)
ROW_H = 5.0                 # row height of the parts list (plan.ROW_H)
NOTE_STEP = 5.0             # line spacing of notes (plan.NOTE_STEP)
ALIGN_TOL_MM = 3.0          # N1.8 grid tolerance for the score (checks.GRID_TOL_MM)
EDGE_TOL_MM = 5.0           # edge tolerance for the score
ALIGN_MAX_SHIFT = 40.0      # maximum shift distance for N1.8 axis alignment


def sheet_field(sd: dict, gap: float = VIEW_GAP_MM) -> tuple[tuple[float, float, float, float], tuple[float, float]]:
    """(area=(left, bottom, right, top), zone=(zone_x, zone_y)) matching sheet.py / plan.py."""
    fmt = sd.get('format', 'A2')
    orient = sd.get('orientation', 'portrait' if fmt == 'A4' else 'landscape')
    w_sh, h_sh = sheet_size(fmt, orient)
    kw = min(190.0, w_sh - 30.0) / 190.0
    rows = sd.get('parts_list') or []
    stack_h = STACK_H + (parts_list_height(rows, kw, ROW_H) if rows else 0.0)
    # margin of the drawing sheet per ISO 5457: left 20 mm, otherwise 10 mm; small margin buffer 2 mm
    area = (22.0, 12.0, w_sh - 12.0, h_sh - 12.0)
    zone = (min(w_sh - 12.0, w_sh - 10.0 - min(190.0, w_sh - 30.0) - 4.0), 10.0 + stack_h + 4.0)
    return area, zone


def _view_part_root(sd: dict) -> dict[str, str]:
    """Assigns to each view id its root view (with 'at') via place.relative_to."""
    views = {v['id']: v for v in sd.get('views') or [] if isinstance(v, dict) and v.get('id')}
    out = {}
    for vid in views:
        cur, seen = vid, set()
        while cur not in seen and cur in views:
            seen.add(cur)
            pl_d = views[cur].get('place')
            if isinstance(pl_d, dict) and pl_d.get('relative_to') in views:
                cur = pl_d['relative_to']
            else:
                break
        out[vid] = cur
    return out


def _box_union(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], gap: float = 0.0) -> bool:
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def _notes_box(notes: dict) -> tuple[float, float, float, float]:
    lines = notes.get('lines') or []
    h = float(notes.get('h') or 3.5)
    at = notes.get('at') or [25.0, 40.0]
    nw = max((pl.text_width(checks.note_text(t), h) for t in lines), default=40.0) + 4.0
    nh = max(1, len(lines) - 1) * NOTE_STEP + 6.0
    x, y_top = float(at[0]), float(at[1]) + 4.5
    return (x - 1.0, y_top - nh - 1.0, x + nw, y_top + 1.0)


def _table_box(t: dict) -> tuple[float, float, float, float]:
    at = t.get('at') or [0.0, 0.0]
    tx, ty = float(at[0]), float(at[1])
    rows = len(t.get('rows') or [])
    h_down = ROW_H * (rows + 2) if rows else 48.0
    h_up = 12.0 if t.get('bend_table') and t.get('labels_in') else 7.0
    return (tx - 1.0, ty - h_down, tx + 90.0, ty + h_up)


def _title_box(a: dict) -> tuple[float, float, float, float]:
    at = a.get('at_sheet') or [0.0, 0.0]
    tx, ty = float(at[0]), float(at[1])
    h = float(a.get('h') or 5.0)
    w = min(float(a.get('max_width') or 1e9), pl.text_width(str(a.get('text') or ''), h) + 2.0)
    return (tx - 0.5, ty - 0.5, tx + w + 0.5, ty + h + 1.5)


def _view_own_box(v: dict, vf: dict) -> tuple[float, float, float, float] | None:
    """Measured view box including its own heading (Detail X, A-A, ISO, View X) and detail circle."""
    b = vf.get('ann_bbox_mm') or vf.get('bbox_mm') or vf.get('geom_bbox_mm')
    if not b or len(b) != 4:
        return None
    x0, y0, x1, y1 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
    gb = vf.get('geom_bbox_mm') or vf.get('bbox_mm') or b
    gx0, gy0, gx1, gy1 = float(gb[0]), float(gb[1]), float(gb[2]), float(gb[3])
    x0, y0, x1, y1 = min(x0, gx0), min(y0, gy0), max(x1, gx1), max(y1, gy1)
    # the detail circle can be larger than the drawn lines; the heading "Detail X (s:t)" stands above it
    det = v.get('detail')
    if isinstance(det, dict) and det.get('radius'):
        sc = float(vf.get('scale') or 1.0)
        r_mm = float(det['radius']) * sc
        cx, cy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
        x0, x1 = min(x0, cx - r_mm), max(x1, cx + r_mm)
        y0, y1 = min(y0, cy - r_mm), max(y1, cy + r_mm)
    lbl = None
    if isinstance(det, dict):
        lbl = f"Detail {det.get('label', 'X')} ({v.get('scale') or ''})"
    elif isinstance(v.get('section'), dict) and v['section'].get('label'):      # an axial section without a label letter has no heading
        lbl = f"{v['section'].get('label', 'A')}-{v['section'].get('label', 'A')} ({v.get('scale') or ''})"
    elif v.get('label'):
        lbl = str(v['label'])
    elif v.get('preset') in ('iso', 'iso_back') and v.get('scale'):
        lbl = f"ISO ({v['scale']})"
    if lbl:
        lw = pl.text_width(lbl, 5.0) + 4.0
        cx = (gx0 + gx1) / 2
        x0 = min(x0, cx - lw / 2)
        x1 = max(x1, cx + lw / 2)
        y1 = max(y1, gy1 + 10.0)
    return (x0 - BLOCK_PAD_MM, y0 - BLOCK_PAD_MM, x1 + BLOCK_PAD_MM, y1 + BLOCK_PAD_MM)


def extract_blocks(sd: dict, view_frames: dict) -> list[dict]:
    """Extracts the rigid and movable blocks of a sheet from spec and measured view_frames."""
    views = [v for v in sd.get('views') or [] if isinstance(v, dict) and v.get('id')]
    anns = [a for a in sd.get('annotations') or [] if isinstance(a, dict)]
    tables = [t for t in sd.get('tables') or [] if isinstance(t, dict)]

    root_of = _view_part_root(sd)
    balloon_views = {a.get('view') for a in anns if a.get('type') == 'balloon'}
    dim_views = {a.get('view') for a in anns if a.get('type') in pl.SIZE_TYPES}

    def is_asm_view(v: dict) -> bool:
        vid = v['id']
        vf = view_frames.get(vid) or {}
        ps = v.get('parts') or []
        return vf.get('grid_block') == 'asm' or vid.startswith('asm-') or len(ps) > 1 or vid in balloon_views

    # 1. view boxes per single view
    vboxes: dict[str, tuple[float, float, float, float]] = {}
    for v in views:
        vb = _view_own_box(v, view_frames.get(v['id']) or {})
        if vb is not None:
            vboxes[v['id']] = vb

    # 2. First grouping: asm together; otherwise root projection chain
    groups: dict[str, list[dict]] = {}
    view_to_gid: dict[str, str] = {}
    for v in views:
        vid = v['id']
        if is_asm_view(v):
            gid = 'asm'
        else:
            r = root_of[vid]
            vf_r = view_frames.get(r) or {}
            gb = vf_r.get('grid_block')
            if gb and gb != 'asm' and not v.get('detail') and not (v.get('section') and r == vid and len(v.get('parts') or []) == 1 and any(
                    u['id'] != vid and (u.get('parts') or []) == (v.get('parts') or []) and 'at' in u and not u.get('detail') and not u.get('section') for u in views)):
                gid = gb
            else:
                gid = r
        groups.setdefault(gid, []).append(v)
        view_to_gid[vid] = gid

    # 3. Details that lie in the original in (or directly below the common block_title of) the main block of their part
    #    belong to it rigidly (e.g. pt-bar-detail-C/D; NOT relocated ones such as a detail-D of another block).
    def grp_box(gid: str):
        bs = [vboxes[v['id']] for v in groups[gid] if v['id'] in vboxes]
        return _box_union(bs) if bs else None

    part_mains: dict[str, str] = {}
    for gid, vs in groups.items():
        if gid == 'asm' or all(v.get('detail') for v in vs):
            continue
        ps = sorted({p for v in vs for p in (v.get('parts') or [])})
        if len(ps) == 1:
            b = grp_box(gid)
            if b is not None:
                prev = part_mains.get(ps[0])
                if prev is None:
                    part_mains[ps[0]] = gid
                else:
                    bp = grp_box(prev)
                    if bp is not None and (b[2] - b[0]) * (b[3] - b[1]) > (bp[2] - bp[0]) * (bp[3] - bp[1]):
                        part_mains[ps[0]] = gid

    # title span over the whole block (at_sheet[0] .. at_sheet[0] + max_width), to bind internal details
    # such as pt-screw-detail-C reliably to their parent part block (but not relocated pattern_details).
    title_spans = [
        (float(a['at_sheet'][0]) - 2.0, float(a['at_sheet'][1]) - 1.0,
         float(a['at_sheet'][0]) + float(a.get('max_width') or pl.text_width(str(a.get('text') or ''), float(a.get('h') or 5.0))) + 2.0,
         float(a['at_sheet'][1]) + float(a.get('h') or 5.0) + 2.0)
        for a in anns if a.get('block_title') and 'at_sheet' in a
    ]

    def shares_block_title(hb, db):
        for tb in title_spans:
            if tb[1] >= hb[3] - 15.0 and tb[1] <= hb[3] + 40.0 and tb[1] >= db[3] - 15.0 and tb[1] <= db[3] + 55.0:
                if min(hb[2], tb[2]) - max(hb[0], tb[0]) > 0 and min(db[2], tb[2]) - max(db[0], tb[0]) > 0:
                    return True
        return False

    for gid in list(groups):
        vs = groups[gid]
        if gid == 'asm' or not all(v.get('detail') for v in vs):
            continue
        ps = sorted({p for v in vs for p in (v.get('parts') or [])})
        if len(ps) == 1 and ps[0] in part_mains and part_mains[ps[0]] != gid:
            host_gid = part_mains[ps[0]]
            db, hb = grp_box(gid), grp_box(host_gid)
            if db is not None and hb is not None and (_boxes_overlap(db, hb, gap=0.0) or shares_block_title(hb, db)):
                groups[host_gid].extend(vs)
                for v in vs:
                    view_to_gid[v['id']] = host_gid
                del groups[gid]

    raw_boxes: dict[str, tuple[float, float, float, float]] = {}
    for gid in groups:
        b = grp_box(gid)
        if b is not None:
            raw_boxes[gid] = b

    # 4. Assign tables to groups (via labels_in)
    tab_map: dict[str, list[int]] = {gid: [] for gid in groups}
    for ti, t in enumerate(tables):
        lbl = t.get('labels_in')
        gid = view_to_gid.get(lbl) if lbl else None
        if gid is None and raw_boxes:
            tb = _table_box(t)
            cx, cy = (tb[0] + tb[2]) / 2, (tb[1] + tb[3]) / 2
            gid = min(raw_boxes, key=lambda g: math.hypot((raw_boxes[g][0] + raw_boxes[g][2]) / 2 - cx,
                                                          (raw_boxes[g][1] + raw_boxes[g][3]) / 2 - cy))
        if gid is not None:
            tab_map[gid].append(ti)

    # 5. assign block_title annotations 1:1 to groups (never two titles on the same block!)
    title_map: dict[str, list[int]] = {gid: [] for gid in groups}
    used_gids: set[str] = set()
    title_anns = [(ai, a) for ai, a in enumerate(anns) if a.get('block_title') and 'at_sheet' in a]
    for ai, a in title_anns:
        txt = str(a.get('text') or '')
        if txt.startswith('Assembly') and 'asm' in groups:
            title_map['asm'].append(ai)
            used_gids.add('asm')
    for ai, a in title_anns:
        if ai in title_map.get('asm', []):
            continue
        tb = _title_box(a)
        tx = float(a['at_sheet'][0])
        ty = float(a['at_sheet'][1])
        cands = [g for g in raw_boxes if g != 'asm' and g not in used_gids]
        if not cands:
            cands = [g for g in raw_boxes if g != 'asm'] or list(raw_boxes)
        if not cands:
            continue

        def score_title(g):
            b = raw_boxes[g]
            # block title starts left-aligned at the block (x ~ b[0]) and stands directly above b[3]
            h_ov = max(0.0, min(b[2], tb[2]) - max(b[0], tb[0]))
            dy = ty - b[3]
            above = 0 if -15.0 <= dy <= 35.0 else 1
            return (above, 0 if h_ov > 0 else 1, abs(b[0] - tx) + 2.0 * abs(dy))

        best_g = min(cands, key=score_title)
        title_map[best_g].append(ai)
        used_gids.add(best_g)

    blocks = []
    for gid, vs in groups.items():
        if gid not in raw_boxes:
            continue
        sub = [vboxes[v['id']] for v in vs if v['id'] in vboxes]
        for ai in title_map[gid]:
            sub.append(_title_box(anns[ai]))
        for ti in tab_map[gid]:
            sub.append(_table_box(tables[ti]))
        box = _box_union(sub)
        x0, y0, x1, y1 = box
        rel_sub = [(b[0] - x0, b[1] - y0, b[2] - x0, b[3] - y0) for b in sub]
        rel_rects, rel_centers = [], []
        for v in vs:
            vf = view_frames.get(v['id']) or {}
            gb = vf.get('geom_bbox_mm') or vf.get('bbox_mm') or vf.get('ann_bbox_mm')
            if gb and len(gb) == 4:
                rel_rects.append((float(gb[0]) - x0, float(gb[1]) - y0, float(gb[2]) - x0, float(gb[3]) - y0))
                rel_centers.append(((float(gb[0]) + float(gb[2])) / 2 - x0, (float(gb[1]) + float(gb[3])) / 2 - y0))
        vids = [v['id'] for v in vs]
        roots = [v['id'] for v in vs if 'at' in v]
        ps = sorted({p for v in vs for p in (v.get('parts') or [])})
        part_id = ps[0] if len(ps) == 1 else None
        is_detail = all(bool(v.get('detail')) for v in vs)
        is_section = len(vs) == 1 and bool(vs[0].get('section')) and not is_detail
        is_iso_only = len(vs) == 1 and (vs[0].get('preset') in ('iso', 'iso_back') or vs[0]['id'].endswith('-iso')) and gid != 'asm'
        blocks.append({
            'id': gid,
            'root_views': roots,
            'all_views': vids,
            'title_indices': title_map[gid],
            'table_indices': tab_map[gid],
            'is_notes': False,
            'is_asm': gid == 'asm',
            'is_detail': is_detail,
            'is_section': is_section,
            'is_iso_only': is_iso_only,
            'part_id': part_id,
            'host_part': part_id if (is_detail or is_section or is_iso_only) else None,
            'box': box,
            'w': round(x1 - x0, 3),
            'h': round(y1 - y0, 3),
            'rel_subboxes': rel_sub,
            'rel_view_rects': rel_rects,
            'rel_geom_centers': rel_centers,
            'has_balloons': any(vid in balloon_views for vid in vids),
            'has_dims': any(vid in dim_views for vid in vids),
            'balloon_rects': [rel_rects[i] for i, v in enumerate(vs) if v['id'] in balloon_views and i < len(rel_rects)],
            'dim_rects': [rel_rects[i] for i, v in enumerate(vs) if v['id'] in dim_views and i < len(rel_rects)],
        })

    if isinstance(sd.get('notes'), dict) and sd['notes'].get('lines'):
        nb = _notes_box(sd['notes'])
        nw, nh = round(nb[2] - nb[0], 3), round(nb[3] - nb[1], 3)
        blocks.append({
            'id': 'notes',
            'root_views': [], 'all_views': [], 'title_indices': [], 'table_indices': [],
            'is_notes': True, 'is_asm': False, 'is_detail': False, 'is_section': False, 'is_iso_only': False,
            'part_id': None, 'host_part': None, 'box': nb, 'w': nw, 'h': nh,
            'rel_subboxes': [(0.0, 0.0, nw, nh)], 'rel_view_rects': [], 'rel_geom_centers': [],
            'has_balloons': False, 'has_dims': False, 'balloon_rects': [], 'dim_rects': [],
        })

    return blocks


def _boxes_from_at(blocks: list[dict], at: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float, float, float]]:
    return {b['id']: (at[b['id']][0], at[b['id']][1], at[b['id']][0] + b['w'], at[b['id']][1] + b['h'])
            for b in blocks if b['id'] in at}


def _subboxes_from_at(blocks: list[dict], at: dict[str, tuple[float, float]]) -> dict[str, list[tuple[float, float, float, float]]]:
    out = {}
    for b in blocks:
        if b['id'] not in at:
            continue
        x0, y0 = at[b['id']]
        out[b['id']] = [(x0 + s[0], y0 + s[1], x0 + s[2], y0 + s[3]) for s in b['rel_subboxes']]
    return out


def validate_placement(boxes: dict, sd: dict, gap: float = VIEW_GAP_MM) -> bool:
    """Checks hard geometry limits (drawing area, title block/parts list zone, distance >= gap).
    boxes: {block_id: (x0,y0,x1,y1)} OR {block_id: [(x0,y0,x1,y1), ...]} (sub-boxes per block)."""
    area, zone = sheet_field(sd, gap)
    left, bottom, right, top = area
    zx, zy = zone
    eps = 0.6
    grouped = []
    for bid, val in boxes.items():
        subs = [val] if (isinstance(val, tuple) and len(val) == 4 and isinstance(val[0], (int, float))) else list(val)
        for (x0, y0, x1, y1) in subs:
            if x0 < left - eps or y0 < bottom - eps or x1 > right + eps or y1 > top + eps:
                return False
            if x1 > zx + eps and y0 < zy - eps:
                return False
        grouped.append((bid, subs))
    min_gap = gap - 0.5
    for i in range(len(grouped)):
        _, sa = grouped[i]
        for j in range(i + 1, len(grouped)):
            _, sb = grouped[j]
            for a in sa:
                for b in sb:
                    sep_x = max(b[0] - a[2], a[0] - b[2])
                    sep_y = max(b[1] - a[3], a[1] - b[3])
                    if max(sep_x, sep_y) < min_gap:
                        return False
    return True


def is_n19_blocked(blocks: list[dict], at: dict[str, tuple[float, float]]) -> bool:
    """N1.9 (assembly_between): True if dimensioned part views lie on opposite sides of the assembly."""
    asm = next((b for b in blocks if b['is_asm']), None)
    if asm is None or asm['id'] not in at:
        return False
    sizes = {b['id']: (b['w'], b['h']) for b in blocks}
    rects = {}
    for b in blocks:
        if b['is_asm']:
            rects[b['id']] = b['balloon_rects'] or b['rel_view_rects']
        elif not b['is_notes']:
            rects[b['id']] = b['dim_rects']
    return pl.zone_blocked(at, sizes, rects=rects, asm=asm['id'])


def _geom_boxes_from_at(blocks: list[dict], at: dict[str, tuple[float, float]]) -> dict[str, list[tuple[float, float, float, float]]]:
    out = {}
    for b in blocks:
        if b['id'] not in at:
            continue
        x0, y0 = at[b['id']]
        rects = b['rel_view_rects'] or b['rel_subboxes']
        out[b['id']] = [(x0 + s[0], y0 + s[1], x0 + s[2], y0 + s[3]) for s in rects]
    return out


def _pair_min_sep(sa: list[tuple[float, float, float, float]], sb: list[tuple[float, float, float, float]]) -> float:
    return min(max(max(b[0] - a[2], a[0] - b[2]), max(b[1] - a[3], a[1] - b[3])) for a in sa for b in sb)


def _valid_at(blocks: list[dict], at: dict[str, tuple[float, float]], sd: dict, gap: float = VIEW_GAP_MM,
              orig_at: dict[str, tuple[float, float]] | None = None) -> bool:
    """Checks all hard rules for a block position:
    1. All sub-boxes inside the drawing area and outside the title block/parts list zone.
    2. View geometries (geom_bbox_mm) of different blocks keep a distance >= gap (>= 6.5 mm).
    3. Measured ann_bbox_mm / titles / notes keep a distance >= SUBBOX_GAP_MM (2 mm)
       (for two blocks that are not moved relative to each other their already valid original distance suffices).
    4. N1.9 (assembly_between == 0).
    """
    area, zone = sheet_field(sd, gap)
    left, bottom, right, top = area
    zx, zy = zone
    eps = 0.6
    subs = _subboxes_from_at(blocks, at)
    geoms = _geom_boxes_from_at(blocks, at)
    orig_subs = _subboxes_from_at(blocks, orig_at) if orig_at else None

    for bid, sa in subs.items():
        for (x0, y0, x1, y1) in sa:
            if x0 < left - eps or y0 < bottom - eps or x1 > right + eps or y1 > top + eps:
                return False
            if x1 > zx + eps and y0 < zy - eps:
                return False

    ids = [b['id'] for b in blocks if b['id'] in at]
    by = {b['id']: b for b in blocks}
    for i in range(len(ids)):
        ia = ids[i]
        for j in range(i + 1, len(ids)):
            ib = ids[j]
            unchanged_rel = (
                orig_at is not None
                and abs((at[ia][0] - orig_at[ia][0]) - (at[ib][0] - orig_at[ib][0])) < 0.2
                and abs((at[ia][1] - orig_at[ia][1]) - (at[ib][1] - orig_at[ib][1])) < 0.2
            )
            if unchanged_rel and orig_subs is not None:
                continue
            # geometry distance between view blocks (>= 6.5 mm -> 0 views_too_close)
            if not by[ia]['is_notes'] and not by[ib]['is_notes']:
                if _pair_min_sep(geoms[ia], geoms[ib]) < gap - 0.3:
                    return False
            # annotation/title/note distance (>= 2 mm -> 0 text_on_line / text_overlap / overlaps_block)
            if _pair_min_sep(subs[ia], subs[ib]) < SUBBOX_GAP_MM - 0.3:
                return False
    return not is_n19_blocked(blocks, at)


def _align_axes(blocks: list[dict], at: dict[str, tuple[float, float]], sd: dict, gap: float = VIEW_GAP_MM,
                orig_at: dict[str, tuple[float, float]] | None = None) -> dict[str, tuple[float, float]]:
    """N1.8 axis alignment: moves blocks within their free margin (up to ALIGN_MAX_SHIFT)
    so that the view geometry centres of neighbouring blocks line up on common horizontal or vertical
    grid axes - only if all hard limits stay valid."""
    cur = dict(at)
    movable = [b for b in blocks if not b['is_notes'] and b['rel_geom_centers']]
    order = sorted(movable, key=lambda b: b['w'] * b['h'])
    for _ in range(2):
        for b in order:
            bid = b['id']
            x0, y0 = cur[bid]
            my_cy = [y0 + cy for _, cy in b['rel_geom_centers']]
            my_cx = [x0 + cx for cx, _ in b['rel_geom_centers']]
            other_cy = [cur[o['id']][1] + cy for o in movable if o['id'] != bid for _, cy in o['rel_geom_centers']]
            other_cx = [cur[o['id']][0] + cx for o in movable if o['id'] != bid for cx, _ in o['rel_geom_centers']]
            if not any(abs(a - t) <= 0.2 for a in my_cy for t in other_cy):
                dys = sorted({round(t - a, 3) for a in my_cy for t in other_cy if abs(t - a) <= ALIGN_MAX_SHIFT}, key=abs)
                for dy in dys:
                    trial = {**cur, bid: (round(x0, 3), round(y0 + dy, 3))}
                    if _valid_at(blocks, trial, sd, gap, orig_at):
                        cur = trial
                        y0 = cur[bid][1]
                        break
            if not any(abs(a - t) <= 0.2 for a in my_cx for t in other_cx):
                dxs = sorted({round(t - a, 3) for a in my_cx for t in other_cx if abs(t - a) <= ALIGN_MAX_SHIFT}, key=abs)
                for dx in dxs:
                    trial = {**cur, bid: (round(x0 + dx, 3), round(y0, 3))}
                    if _valid_at(blocks, trial, sd, gap, orig_at):
                        cur = trial
                        break
    return cur


def _center_at(blocks: list[dict], at: dict[str, tuple[float, float]], sd: dict, gap: float = VIEW_GAP_MM,
               orig_at: dict[str, tuple[float, float]] | None = None) -> dict[str, tuple[float, float]]:
    area, _ = sheet_field(sd, gap)
    left, bottom, right, top = area
    boxes = _boxes_from_at(blocks, at)
    min_x = min(b[0] for b in boxes.values())
    max_x = max(b[2] for b in boxes.values())
    min_y = min(b[1] for b in boxes.values())
    max_y = max(b[3] for b in boxes.values())
    dx = ((left + right) - (min_x + max_x)) / 2
    dy = ((bottom + top) - (min_y + max_y)) / 2
    for fx, fy in ((dx, dy), (dx, 0.0), (0.0, dy)):
        trial = {k: (round(x + fx, 3), round(y + fy, 3)) for k, (x, y) in at.items()}
        if _valid_at(blocks, trial, sd, gap, orig_at):
            return trial
    return at


def _subtract_block(free: list, b: dict, x0: float, y0: float, gap: float) -> list:
    """Subtracts all real sub-boxes of a block (with gap) from the free rectangles (keeps L-shaped corners!)."""
    out = free
    for s in b['rel_subboxes']:
        out = pl._remaining_rectangles(out, (x0 + s[0], y0 + s[1], x0 + s[2] + gap, y0 + s[3] + gap))
    return out


def _pack_with_order(blocks: list[dict], order_ids: list[str], sd: dict, gap: float = VIEW_GAP_MM,
                     pin_corner: tuple[str, str] | None = None) -> dict[str, tuple[float, float]] | None:
    """Packs blocks sub-box-exactly (uses the L-shaped corners of large blocks)."""
    area, zone = sheet_field(sd, gap)
    by = {b['id']: b for b in blocks}
    free = pl._free_field(area, zone, gap)
    out: dict[str, tuple[float, float]] = {}
    remaining_ids = list(order_ids)

    def place_one(bid: str, corner: str | None = None) -> tuple[float, float] | None:
        b = by[bid]
        W, H = b['w'] + gap, b['h'] + gap
        fits = [r for r in free if W <= r[2] - r[0] + 1e-6 and H <= r[3] - r[1] + 1e-6]
        if not fits:
            return None
        if corner is not None:
            pts = {
                'tl': [(r[0], r[3] - H) for r in fits],
                'tr': [(r[2] - W, r[3] - H) for r in fits],
                'bl': [(r[0], r[1]) for r in fits],
                'br': [(r[2] - W, r[1]) for r in fits],
            }[corner]
            key_fn = {
                'tl': lambda p: (-p[1], p[0]),
                'tr': lambda p: (-p[1], -p[0]),
                'bl': lambda p: (p[1], p[0]),
                'br': lambda p: (p[1], -p[0]),
            }[corner]
            ax, ay = min(pts, key=key_fn)
            return (round(ax, 3), round(ay, 3))
        r = min(fits, key=lambda r: (min(r[2] - r[0] - W, r[3] - r[1] - H), -r[3], r[0]))
        return (round(r[0], 3), round(r[3] - H, 3))

    if pin_corner is not None:
        pid, corner = pin_corner
        pos = place_one(pid, corner)
        if pos is None:
            return None
        out[pid] = pos
        free = _subtract_block(free, by[pid], pos[0], pos[1], gap)
        remaining_ids = [i for i in remaining_ids if i != pid]

    for bid in remaining_ids:
        pos = place_one(bid)
        if pos is None:
            rest_tuples = [(i, by[i]['w'], by[i]['h']) for i in remaining_ids if i not in out]
            rest_at = pl._place_in_free(rest_tuples, free, gap)
            if rest_at is None:
                return None
            out.update({k: (round(v[0], 3), round(v[1], 3)) for k, v in rest_at.items()})
            break
        out[bid] = pos
        free = _subtract_block(free, by[bid], pos[0], pos[1], gap)
    return out


def _mirror_lr(blocks: list[dict], orig_at: dict[str, tuple[float, float]], sd: dict, gap: float = VIEW_GAP_MM) -> dict[str, tuple[float, float]] | None:
    """Mirrors the blocks left/right in the drawing area (sub-box-exact, L-shaped corners are kept!)."""
    area, zone = sheet_field(sd, gap)
    left, bottom, right, top = area
    zx, zy = zone
    mirrored = {b['id']: (round(left + right - (orig_at[b['id']][0] + b['w']), 3), round(orig_at[b['id']][1], 3))
                for b in blocks}
    if _valid_at(blocks, mirrored, sd, gap):
        return mirrored
    # resolve collision with the title block zone (bottom right) or internal L-shaped sub-boxes:
    # fix the blocks that stay valid and pack colliding blocks cleanly
    by = {b['id']: b for b in blocks}
    ok_ids, bad_ids = [], []
    for b in blocks:
        x0, y0 = mirrored[b['id']]
        subs = [(x0 + s[0], y0 + s[1], x0 + s[2], y0 + s[3]) for s in b['rel_subboxes']]
        in_zone = any(s[2] > zx + 0.5 and s[1] < zy - 0.5 for s in subs)
        if in_zone:
            bad_ids.append(b['id'])
        else:
            ok_ids.append(b['id'])
    # also check pairwise overlaps within ok_ids (they arise when an asymmetric L-shaped block is mirrored)
    clean_ok: list[str] = []
    for bid in sorted(ok_ids, key=lambda i: -(by[i]['w'] * by[i]['h'])):
        trial_map = {k: mirrored[k] for k in clean_ok + [bid]}
        sub_blocks = [by[k] for k in trial_map]
        if validate_placement(_subboxes_from_at(sub_blocks, trial_map), sd, gap):
            clean_ok.append(bid)
        else:
            bad_ids.append(bid)
    if bad_ids:
        free = pl._free_field(area, zone, gap)
        fixed = {}
        for bid in clean_ok:
            x0, y0 = mirrored[bid]
            fixed[bid] = (x0, y0)
            free = _subtract_block(free, by[bid], x0, y0, gap)
        rest_tuples = [(bid, by[bid]['w'], by[bid]['h']) for bid in sorted(bad_ids, key=lambda i: -(by[i]['w'] * by[i]['h']))]
        placed = pl._place_in_free(rest_tuples, free, gap)
        if placed is not None:
            trial = {**fixed, **{k: (round(v[0], 3), round(v[1], 3)) for k, v in placed.items()}}
            if _valid_at(blocks, trial, sd, gap):
                return trial
    return None


def _nudge_notes(blocks: list[dict], cand: dict[str, tuple[float, float]], sd: dict, gap: float,
                 orig_at: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]] | None:
    """If only the notes block collides with the moved blocks, move notes into a free gap."""
    if _valid_at(blocks, cand, sd, gap, orig_at):
        return cand
    nb = next((b for b in blocks if b['is_notes']), None)
    if nb is None or nb['id'] not in cand:
        return None
    nx, ny = cand[nb['id']]
    for dy in (-6.0, -12.0, -18.0, 6.0, 12.0, 18.0, -25.0, 25.0):
        trial = {**cand, nb['id']: (nx, round(ny + dy, 3))}
        if _valid_at(blocks, trial, sd, gap, orig_at):
            return trial
    # Alternatively: pack notes freely into the remaining drawing area
    area, zone = sheet_field(sd, gap)
    free = pl._free_field(area, zone, gap)
    for b in blocks:
        if b['id'] != nb['id'] and b['id'] in cand:
            free = _subtract_block(free, b, cand[b['id']][0], cand[b['id']][1], SUBBOX_GAP_MM)
    placed = pl._place_in_free([(nb['id'], nb['w'], nb['h'])], free, SUBBOX_GAP_MM)
    if placed and nb['id'] in placed:
        trial = {**cand, nb['id']: (round(placed[nb['id']][0], 3), round(placed[nb['id']][1], 3))}
        if _valid_at(blocks, trial, sd, gap, orig_at):
            return trial
    return None


def _swap_two_blocks(blocks: list[dict], orig_at: dict[str, tuple[float, float]], a_id: str, b_id: str,
                     sd: dict, gap: float = VIEW_GAP_MM) -> dict[str, tuple[float, float]] | None:
    """Swaps two blocks horizontally (together with directly attached detail blocks) without disturbing other blocks."""
    by = {b['id']: b for b in blocks}
    ba, bb = by[a_id], by[b_id]
    ax, ay = orig_at[a_id]
    bx, by_ = orig_at[b_id]
    if ax > bx:
        a_id, b_id = b_id, a_id
        ba, bb = bb, ba
        ax, ay, bx, by_ = bx, by_, ax, ay
    span_l = ax
    span_r = max(bx + bb['w'], ax + ba['w'])
    new_b = (round(span_l, 3), round(ay + ba['h'] - bb['h'], 3))
    new_a = (round(span_r - ba['w'], 3), round(by_ + bb['h'] - ba['h'], 3))
    att_a = [x['id'] for x in blocks if x['is_detail'] and x['part_id'] == ba['part_id'] and x['id'] not in (a_id, b_id)]
    att_b = [x['id'] for x in blocks if x['is_detail'] and x['part_id'] == bb['part_id'] and x['id'] not in (a_id, b_id)]
    # Try exactly swapped as well as slightly offset to the right/up (for tight neighbourhoods such as asm/nut/ball)
    shifts = ((0.0, 0.0), (5.0, 0.0), (15.0, 0.0), (-5.0, 0.0), (0.0, 3.0))
    for sdx, sdy in shifts:
        for na, nb in (
            ((round(new_a[0] + sdx, 3), round(new_a[1] + sdy, 3)), (round(new_b[0] + sdx, 3), round(new_b[1] + sdy, 3))),
            ((round(new_a[0] + sdx, 3), round(ay + sdy, 3)), (round(new_b[0] + sdx, 3), round(by_ + sdy, 3))),
            ((round(bx + sdx, 3), round(ay + sdy, 3)), (round(ax + sdx, 3), round(by_ + sdy, 3))),
        ):
            for move_att in (True, False):
                cand = {**orig_at, a_id: na, b_id: nb}
                if move_att:
                    dxa, dya = na[0] - ax, na[1] - ay
                    dxb, dyb = nb[0] - bx, nb[1] - by_
                    for cid in att_a:
                        cand[cid] = (round(orig_at[cid][0] + dxa, 3), round(orig_at[cid][1] + dya, 3))
                    for cid in att_b:
                        cand[cid] = (round(orig_at[cid][0] + dxb, 3), round(orig_at[cid][1] + dyb, 3))
                ok = _nudge_notes(blocks, cand, sd, gap, orig_at)
                if ok is not None:
                    return ok
                # If a block below (e.g. asm below a housing) only brushes slightly:
                # move it down (and small blocks beside it on the right slightly to the right into free space if needed).
                asm_b = next((x for x in blocks if x['is_asm'] and x['id'] not in (a_id, b_id)), None)
                if asm_b is not None:
                    ax0, ay0 = cand[asm_b['id']]
                    below_rest = [x for x in blocks if not x['is_notes'] and x['id'] not in (a_id, b_id, asm_b['id'])
                                  and orig_at[x['id']][1] + x['h'] <= ay0 + asm_b['h'] + 5.0
                                  and orig_at[x['id']][0] >= ax0 + asm_b['w'] - 5.0]
                    for ady in (-10.0, -15.0, -22.0):
                        for bdx in (0.0, 12.0, 25.0):
                            t2 = {**cand, asm_b['id']: (ax0, round(ay0 + ady, 3))}
                            if bdx > 0.0:
                                for br in below_rest:
                                    t2[br['id']] = (round(orig_at[br['id']][0] + bdx, 3), orig_at[br['id']][1])
                            ok2 = _nudge_notes(blocks, t2, sd, gap, orig_at)
                            if ok2 is not None:
                                return ok2
    return None


def _swap_columns(blocks: list[dict], orig_at: dict[str, tuple[float, float]], sd: dict, gap: float = VIEW_GAP_MM) -> dict[str, tuple[float, float]] | None:
    """Swaps the left against the right column at a clear vertical dividing line (ideal for a main part left <-> right!)."""
    area, _ = sheet_field(sd, gap)
    left, _, right, _ = area
    vblocks = [b for b in blocks if not b['is_notes']]
    # Search an x dividing line between two groups that do not overlap horizontally
    xs = sorted({round(orig_at[b['id']][0] + b['w'], 2) for b in vblocks})
    for cut in xs:
        L = [b for b in blocks if orig_at[b['id']][0] + b['w'] <= cut + 1.0]
        R = [b for b in blocks if orig_at[b['id']][0] >= cut - 1.0]
        if not L or not R or len(L) + len(R) != len(blocks):
            continue
        lx0 = min(orig_at[b['id']][0] for b in L)
        lx1 = max(orig_at[b['id']][0] + b['w'] for b in L)
        rx0 = min(orig_at[b['id']][0] for b in R)
        rx1 = max(orig_at[b['id']][0] + b['w'] for b in R)
        sep = max(gap, rx0 - lx1)
        dx_r = lx0 - rx0
        dx_l = (lx0 + (rx1 - rx0) + sep) - lx0
        cand = dict(orig_at)
        for b in R:
            cand[b['id']] = (round(orig_at[b['id']][0] + dx_r, 3), round(orig_at[b['id']][1], 3))
        for b in L:
            cand[b['id']] = (round(orig_at[b['id']][0] + dx_l, 3), round(orig_at[b['id']][1], 3))
        if _valid_at(blocks, cand, sd, gap, orig_at):
            return cand
    return None


def _row_permutations(blocks: list[dict], orig_at: dict[str, tuple[float, float]], sd: dict, gap: float = VIEW_GAP_MM) -> list[tuple[str, dict[str, tuple[float, float]]]]:
    """Produces clean local block swaps within horizontal/vertical groups."""
    out = []
    col_sw = _swap_columns(blocks, orig_at, sd, gap)
    if col_sw is not None:
        out.append(('swap_columns', col_sw))
    vblocks = [b for b in blocks if not b['is_notes']]
    pairs = []
    for i in range(len(vblocks)):
        for j in range(i + 1, len(vblocks)):
            a, b = vblocks[i], vblocks[j]
            ay0, ay1 = orig_at[a['id']][1], orig_at[a['id']][1] + a['h']
            by0, by1 = orig_at[b['id']][1], orig_at[b['id']][1] + b['h']
            ov = min(ay1, by1) - max(ay0, by0)
            if ov > 0.25 * min(a['h'], b['h']):
                pairs.append((a['id'], b['id'], a['w'] * a['h'] + b['w'] * b['h']))
    pairs.sort(key=lambda t: (-t[2], t[0], t[1]))
    for a_id, b_id, _ in pairs:
        sw = _swap_two_blocks(blocks, orig_at, a_id, b_id, sd, gap)
        if sw is not None:
            out.append((f'swap_{a_id}_{b_id}', sw))
    # In addition: vertical swap of a stack in the same column (e.g. on the right: asm above <-> cap+screw below!)
    area, _ = sheet_field(sd, gap)
    mid_y = (area[1] + area[3]) / 2
    asm = next((b for b in vblocks if b['is_asm']), None)
    if asm is not None:
        ax0, ay0 = orig_at[asm['id']]
        below = [b for b in vblocks if b['id'] != asm['id'] and orig_at[b['id']][0] + b['w'] > ax0 - 5.0
                 and orig_at[b['id']][0] < ax0 + asm['w'] + 5.0 and orig_at[b['id']][1] + b['h'] <= ay0 + 2.0]
        if below:
            b_top = max(orig_at[b['id']][1] + b['h'] for b in below)
            b_bot = min(orig_at[b['id']][1] for b in below)
            a_top = ay0 + asm['h']
            sep = max(SUBBOX_GAP_MM + 2.0, ay0 - b_top)
            cand = dict(orig_at)
            dy_up = a_top - b_top
            for b in below:
                cand[b['id']] = (orig_at[b['id']][0], round(orig_at[b['id']][1] + dy_up, 3))
            cand[asm['id']] = (ax0, round(b_bot + (a_top - b_top) - (b_top - b_bot) - sep - asm['h'] + (b_top - b_bot), 3))
            # Simple and exact: the below group moves all the way up (top edge = a_top), asm moves directly below it!
            cand[asm['id']] = (ax0, round((b_bot + dy_up) - sep - asm['h'], 3))
            if _valid_at(blocks, cand, sd, gap, orig_at):
                out.append(('asm_below_stack', cand))
    top_row = sorted([b for b in vblocks if orig_at[b['id']][1] + b['h'] / 2 >= mid_y], key=lambda b: orig_at[b['id']][0])
    if len(top_row) >= 3:
        x_left = min(orig_at[b['id']][0] for b in top_row)
        x_right = max(orig_at[b['id']][0] + b['w'] for b in top_row)
        y_top = max(orig_at[b['id']][1] + b['h'] for b in top_row)
        total_w = sum(b['w'] for b in top_row)
        avail = x_right - x_left - total_w
        step = max(gap, avail / (len(top_row) - 1)) if len(top_row) > 1 else gap
        cur_x = x_left
        rev_at = dict(orig_at)
        for b in reversed(top_row):
            rev_at[b['id']] = (round(cur_x, 3), round(y_top - b['h'], 3))
            cur_x += b['w'] + step
        if _valid_at(blocks, rev_at, sd, gap, orig_at):
            out.append(('reverse_top_row', rev_at))
    bot_row = sorted([b for b in vblocks if orig_at[b['id']][1] + b['h'] / 2 < mid_y], key=lambda b: orig_at[b['id']][0])
    if len(bot_row) >= 2:
        area_, zone_ = sheet_field(sd, gap)
        x_left = min(orig_at[b['id']][0] for b in bot_row)
        x_right = max(max(orig_at[b['id']][0] + b['w'] for b in bot_row), zone_[0] - 4.0)
        total_w = sum(b['w'] for b in bot_row)
        if total_w + gap * (len(bot_row) - 1) <= x_right - x_left + 1.0:
            avail = x_right - x_left - total_w
            step = max(gap, min(25.0, avail / (len(bot_row) - 1)))
            cur_x = x_left
            rev_b = dict(orig_at)
            for b in reversed(bot_row):
                rev_b[b['id']] = (round(cur_x, 3), round(orig_at[b['id']][1], 3))
                cur_x += b['w'] + step
            ok_rb = _nudge_notes(blocks, rev_b, sd, gap, orig_at)
            if ok_rb is not None:
                out.append(('reverse_bottom_row', ok_rb))
    # Combine upper swap + lower swap if applicable (e.g. above lid<->housing AND below nut<->ball!)
    top_swaps = [(n, a) for n, a in out if n.startswith('swap_') and n != 'swap_columns']
    if len(top_swaps) >= 2:
        n1, a1 = top_swaps[0]
        for n2, a2 in top_swaps[1:]:
            changed1 = {k for k in a1 if a1[k] != orig_at[k] and k != 'notes'}
            changed2 = {k for k in a2 if a2[k] != orig_at[k] and k != 'notes'}
            if not (changed1 & changed2):
                comb = {k: (a1[k] if k in changed1 else a2[k]) for k in orig_at}
                ok_c = _nudge_notes(blocks, comb, sd, gap, orig_at)
                if ok_c is not None:
                    out.append((f'combo_{n1}_{n2}', ok_c))
                    break
    # Even distribution of a loose row (e.g. nut + ball below, into the free space up to zone_x)
    if len(bot_row) >= 2:
        area_, zone_ = sheet_field(sd, gap)
        x_left = min(orig_at[b['id']][0] for b in bot_row)
        x_right = zone_[0] - 6.0
        cur_right = max(orig_at[b['id']][0] + b['w'] for b in bot_row)
        slack = x_right - cur_right
        if slack >= 25.0:
            # Distribute free gaps evenly over the blocks of the lower row (left stays anchored)
            total_w = sum(b['w'] for b in bot_row)
            step = (x_right - x_left - total_w) / (len(bot_row) - 1)
            if step >= gap:
                sp_at = dict(orig_at)
                cx_ = x_left
                for b in bot_row:
                    sp_at[b['id']] = (round(cx_, 3), orig_at[b['id']][1])
                    cx_ += b['w'] + step
                ok_sp = _nudge_notes(blocks, sp_at, sd, gap, orig_at)
                if ok_sp is not None:
                    out.append(('spread_bottom_row', ok_sp))
    return out


def _shelf_grouped_order(blocks: list[dict], reverse_parts: bool = False) -> list[str]:
    asm_ids = [b['id'] for b in blocks if b['is_asm']]
    notes_ids = [b['id'] for b in blocks if b['is_notes']]
    part_blocks = [b for b in blocks if not b['is_asm'] and not b['is_notes']]
    by_part: dict[str, list[dict]] = {}
    for b in part_blocks:
        by_part.setdefault(b['part_id'] or b['id'], []).append(b)
    parts_sorted = sorted(by_part.items(), key=lambda kv: -sum(x['w'] * x['h'] for x in kv[1]))
    if reverse_parts:
        parts_sorted = list(reversed(parts_sorted))
    out = []
    for _, pbs in parts_sorted:
        pbs_sorted = sorted(pbs, key=lambda b: (b['is_detail'] or b['is_section'] or b['is_iso_only'], -(b['w'] * b['h'])))
        out.extend(b['id'] for b in pbs_sorted)
    return out + asm_ids + notes_ids


def _vertical_swap(blocks: list[dict], orig_at: dict[str, tuple[float, float]], sd: dict, gap: float = VIEW_GAP_MM) -> dict[str, tuple[float, float]] | None:
    """Swaps the upper and lower block position in the drawing area (e.g. main part below, details/assembly above)."""
    area, _ = sheet_field(sd, gap)
    _, bottom, _, top = area
    by_y_asc = [b['id'] for b in sorted(blocks, key=lambda b: (orig_at[b['id']][1], orig_at[b['id']][0]))]
    asm = next((b for b in blocks if b['is_asm']), None)
    if asm is not None:
        orig_top = orig_at[asm['id']][1] + asm['h'] / 2 > (bottom + top) / 2
        for corner in (('bl', 'tl', 'tr') if orig_top else ('tr', 'tl', 'bl')):
            got = _pack_with_order(blocks, by_y_asc, sd, gap, pin_corner=(asm['id'], corner))
            if got and _valid_at(blocks, got, sd, gap):
                return got
        return None
    main_b = max((b for b in blocks if not b['is_notes']), key=lambda b: b['w'] * b['h'])
    orig_top = orig_at[main_b['id']][1] + main_b['h'] / 2 > (bottom + top) / 2
    for corner in (('bl', 'tr', 'tl') if orig_top else ('tl', 'tr', 'bl')):
        got = _pack_with_order(blocks, by_y_asc, sd, gap, pin_corner=(main_b['id'], corner))
        if got and _valid_at(blocks, got, sd, gap):
            return got
    return None


def layout_score(blocks: list[dict], at: dict[str, tuple[float, float]], sd: dict, gap: float = VIEW_GAP_MM) -> dict:
    """Deterministic layout score of a block position (N1.8 axis alignment, edge alignment, host proximity, balance)."""
    area, _ = sheet_field(sd, gap)
    left, bottom, right, top = area
    diag = math.hypot(right - left, top - bottom) or 1.0
    vblocks = [b for b in blocks if not b['is_notes'] and b['rel_geom_centers']]

    aligned = 0
    for b in vblocks:
        bx, by_ = at[b['id']]
        my_cx = [bx + cx for cx, _ in b['rel_geom_centers']]
        my_cy = [by_ + cy for _, cy in b['rel_geom_centers']]
        o_cx = [at[o['id']][0] + cx for o in vblocks if o['id'] != b['id'] for cx, _ in o['rel_geom_centers']]
        o_cy = [at[o['id']][1] + cy for o in vblocks if o['id'] != b['id'] for _, cy in o['rel_geom_centers']]
        if any(abs(a - t) <= ALIGN_TOL_MM for a in my_cy for t in o_cy) or any(abs(a - t) <= ALIGN_TOL_MM for a in my_cx for t in o_cx):
            aligned += 1
    align_share = aligned / len(vblocks) if len(vblocks) >= 2 else 1.0
    grid_lonely = round(1.0 - align_share, 4)

    edges_ok = 0
    boxes = _boxes_from_at(blocks, at)
    for b in vblocks:
        ba = boxes[b['id']]
        if any(abs(ba[k] - boxes[o['id']][k]) <= EDGE_TOL_MM for o in vblocks if o['id'] != b['id'] for k in range(4)):
            edges_ok += 1
    edge_share = edges_ok / len(vblocks) if len(vblocks) >= 2 else 1.0

    main_by_part = {}
    for b in vblocks:
        if b['part_id'] and not b['is_detail'] and not b['is_section'] and not b['is_iso_only']:
            prev = main_by_part.get(b['part_id'])
            if prev is None or b['w'] * b['h'] > prev['w'] * prev['h']:
                main_by_part[b['part_id']] = b
    prox_vals = []
    for b in vblocks:
        host = main_by_part.get(b['host_part']) if b['host_part'] else None
        if host and host['id'] != b['id']:
            ba, bh = boxes[b['id']], boxes[host['id']]
            d = math.hypot((ba[0] + ba[2] - bh[0] - bh[2]) / 2, (ba[1] + ba[3] - bh[1] - bh[3]) / 2)
            prox_vals.append(max(0.0, 1.0 - d / (0.8 * diag)))
    host_prox = sum(prox_vals) / len(prox_vals) if prox_vals else 1.0

    cx_sh, cy_sh = (left + right) / 2, (bottom + top) / 2
    tot_a = sum(b['w'] * b['h'] for b in vblocks) or 1.0
    gx = sum(((boxes[b['id']][0] + boxes[b['id']][2]) / 2) * b['w'] * b['h'] for b in vblocks) / tot_a
    gy = sum(((boxes[b['id']][1] + boxes[b['id']][3]) / 2) * b['w'] * b['h'] for b in vblocks) / tot_a
    balance = max(0.0, 1.0 - math.hypot(gx - cx_sh, gy - cy_sh) / (0.5 * diag))

    score = round(0.40 * align_share + 0.25 * edge_share + 0.20 * host_prox + 0.15 * balance, 4)
    return {
        'score': score,
        'grid_lonely_share': grid_lonely,
        'edge_aligned_share': round(edge_share, 4),
        'host_proximity': round(host_prox, 4),
        'balance': round(balance, 4),
    }


def apply_placement(spec: dict, blocks: list[dict], orig_at: dict[str, tuple[float, float]],
                    new_at: dict[str, tuple[float, float]]) -> dict:
    """Creates a new spec copy with moved blocks (pure function, input stays untouched)."""
    out = copy.deepcopy(spec)
    sd = out['sheets'][0]
    by_vid = {v['id']: v for v in sd.get('views') or [] if isinstance(v, dict) and v.get('id')}
    anns = sd.get('annotations') or []
    tables = sd.get('tables') or []

    for b in blocks:
        bid = b['id']
        dx = new_at[bid][0] - orig_at[bid][0]
        dy = new_at[bid][1] - orig_at[bid][1]
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            continue
        if b['is_notes']:
            at_n = sd['notes']['at']
            sd['notes']['at'] = [round(float(at_n[0]) + dx, 2), round(float(at_n[1]) + dy, 2)]
            continue
        for vid in b['root_views']:
            v = by_vid[vid]
            v['at'] = [round(float(v['at'][0]) + dx, 2), round(float(v['at'][1]) + dy, 2)]
        for ai in b['title_indices']:
            p = anns[ai]['at_sheet']
            anns[ai]['at_sheet'] = [round(float(p[0]) + dx, 2), round(float(p[1]) + dy, 2)]
        for ti in b['table_indices']:
            p = tables[ti]['at']
            tables[ti]['at'] = [round(float(p[0]) + dx, 2), round(float(p[1]) + dy, 2)]
    return out


def _max_shift(a: dict[str, tuple[float, float]], b: dict[str, tuple[float, float]], blocks: list[dict]) -> float:
    return max((math.hypot(a[x['id']][0] - b[x['id']][0], a[x['id']][1] - b[x['id']][1])
                for x in blocks if not x['is_notes']), default=0.0)


def generate(spec: dict, view_frames: dict, max_variants: int = MAX_VARIANTS, gap: float = VIEW_GAP_MM) -> list[dict]:
    """Pure, deterministic variant generation after autofix."""
    sheets = spec.get('sheets') or []
    if len(sheets) != 1:
        return [{
            'id': 'v0', 'strategy': 'original', 'spec': copy.deepcopy(spec), 'score': 1.0,
            'score_parts': {}, 'n19_blocked': False, 'max_shift_mm': 0.0, 'boxes': {},
        }]
    sd = sheets[0]
    blocks = extract_blocks(sd, view_frames or {})
    if len([b for b in blocks if not b['is_notes']]) < 2:
        orig_at = {b['id']: (b['box'][0], b['box'][1]) for b in blocks}
        sp = layout_score(blocks, orig_at, sd, gap)
        return [{
            'id': 'v0', 'strategy': 'original', 'spec': copy.deepcopy(spec), 'score': sp['score'],
            'score_parts': sp, 'n19_blocked': is_n19_blocked(blocks, orig_at), 'max_shift_mm': 0.0,
            'boxes': _boxes_from_at(blocks, orig_at),
        }]

    orig_at = {b['id']: (round(b['box'][0], 3), round(b['box'][1], 3)) for b in blocks}
    area, _ = sheet_field(sd, gap)
    left, bottom, right, top = area

    candidates: list[tuple[str, dict[str, tuple[float, float]] | None]] = [('original', orig_at)]

    def refine(at_c):
        if at_c is None or not _valid_at(blocks, at_c, sd, gap, orig_at):
            return None
        return _center_at(blocks, _align_axes(blocks, at_c, sd, gap, orig_at), sd, gap, orig_at)

    # 1. Left-right mirroring
    candidates.append(('mirror_lr', refine(_mirror_lr(blocks, orig_at, sd, gap))))

    # 2. Other corner for the assembly (or main block for single parts) + sub-box packing
    asm = next((b for b in blocks if b['is_asm']), None)
    grouped = _shelf_grouped_order(blocks, reverse_parts=False)
    grouped_rev = _shelf_grouped_order(blocks, reverse_parts=True)
    if asm is not None:
        ax, ay = orig_at[asm['id']]
        cx, cy = (left + right) / 2, (bottom + top) / 2
        orig_corner = ('t' if ay + asm['h'] / 2 >= cy else 'b') + ('l' if ax + asm['w'] / 2 < cx else 'r')
        other_corners = [c for c in ('tl', 'tr', 'bl', 'br') if c != orig_corner]
        for corner in other_corners:
            candidates.append((f'asm_corner_{corner}', refine(_pack_with_order(blocks, grouped, sd, gap, pin_corner=(asm['id'], corner)))))
        for corner in (orig_corner, *other_corners):
            got = refine(_pack_with_order(blocks, grouped_rev, sd, gap, pin_corner=(asm['id'], corner)))
            if got is not None:
                candidates.append((f'shelf_grouped_{corner}', got))
                break
    else:
        main_b = max((b for b in blocks if not b['is_notes']), key=lambda b: b['w'] * b['h'])
        mx, my = orig_at[main_b['id']]
        cx, cy = (left + right) / 2, (bottom + top) / 2
        orig_corner = ('t' if my + main_b['h'] / 2 >= cy else 'b') + ('l' if mx + main_b['w'] / 2 < cx else 'r')
        for corner in [c for c in ('tr', 'bl', 'tl', 'br') if c != orig_corner]:
            candidates.append((f'corner_{corner}', refine(_pack_with_order(blocks, grouped, sd, gap, pin_corner=(main_b['id'], corner)))))
        candidates.append(('shelf_grouped', refine(_pack_with_order(blocks, grouped_rev, sd, gap))))

    # 3. Targeted row/pair swaps (very effective on tightly packed A1/A2 sheets)
    for name, sw_at in _row_permutations(blocks, orig_at, sd, gap):
        candidates.append((name, refine(sw_at)))

    # 4. Vertical swap + pure N1.8 realignment of the original
    candidates.append(('vertical_swap', refine(_vertical_swap(blocks, orig_at, sd, gap))))
    if _valid_at(blocks, orig_at, sd, gap):
        candidates.append(('aligned_orig', _center_at(blocks, _align_axes(blocks, orig_at, sd, gap), sd, gap)))

    kept: list[tuple[str, dict[str, tuple[float, float]]]] = []
    for strat, at_cand in candidates:
        if at_cand is None:
            continue
        if strat != 'original':
            if not _valid_at(blocks, at_cand, sd, gap, orig_at):
                continue
            if any(_max_shift(at_cand, prev_at, blocks) < MIN_DIFF_MM for _, prev_at in kept):
                continue
        kept.append((strat, at_cand))
        if len(kept) >= max_variants:
            break

    out = []
    for strat, at_cand in kept:
        sp = layout_score(blocks, at_cand, sd, gap)
        out.append({
            'id': '',
            'strategy': strat,
            'spec': apply_placement(spec, blocks, orig_at, at_cand) if strat != 'original' else copy.deepcopy(spec),
            'score': sp['score'],
            'score_parts': sp,
            'n19_blocked': is_n19_blocked(blocks, at_cand),
            'max_shift_mm': round(_max_shift(at_cand, orig_at, blocks), 2),
            'boxes': _boxes_from_at(blocks, at_cand),
            'subboxes': _subboxes_from_at(blocks, at_cand),
        })

    out.sort(key=lambda v: (-v['score'], 0 if v['strategy'] == 'original' else 1, v['strategy']))
    for i, v in enumerate(out):
        v['id'] = f'v{i}'
    return out


# ---------------------------------------------------------------- variant obligation against the original

def spec_key(sheets) -> str:
    """Identifier of a run like drawing.spec_key (hash of the sheets of the input spec)."""
    import hashlib
    import json

    def norm(o):   # the service (JS) writes 10.0 as 10: hash integral floats as int like there
        if isinstance(o, float) and o.is_integer():
            return int(o)
        if isinstance(o, dict):
            return {k: norm(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [norm(v) for v in o]
        return o
    return hashlib.sha1(json.dumps(norm(sheets), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()[:16]


def finding_counts(findings) -> dict:
    """Number of findings per code (all severities)."""
    out: dict = {}
    for f in findings or []:
        code = f.get('code')
        out[code] = out.get(code, 0) + 1
    return out


def worse_codes(orig_counts: dict, var_counts: dict) -> dict:
    """Codes for which the variant has more findings than the original: {code: [original, variant]}."""
    return {c: [int(orig_counts.get(c, 0)), int(n)] for c, n in sorted(var_counts.items(), key=lambda kv: str(kv[0]))
            if n > int(orig_counts.get(c, 0))}


def findings_of(store_root, spec, rendered, action='render'):
    """(findings list, complete?) of a render. The service truncates `findings` to 40 (service.ts) but reports `findings_total`;
    if the response is truncated, the untruncated file last-findings-render-<spec_key>.json from <store>/drawing-work/render applies,
    provided it matches the response (number of findings, blocking) and the input spec. Otherwise incomplete (the variant is then not checkable).
    action='autofix' reads last-findings-autofix-<spec_key of the input>.json (render with layout repair; `spec` is then the
    input spec before the repair): saves the additional comparison render of the original."""
    import json
    resp = list(rendered.get('findings') or [])
    total = rendered.get('findings_total')
    if total is None or len(resp) >= int(total):
        return resp, True
    if store_root is None:
        return resp, False
    key = spec_key(spec.get('sheets'))
    path = Path(store_root) / 'drawing-work' / 'render' / f'last-findings-{action}-{key}.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return resp, False
    full = raw.get('findings') or []
    blocking = sum(1 for f in full if f.get('severity') == 'block')
    ok = (raw.get('spec_key') == key and raw.get('action') == action and len(full) == int(total)
          and (rendered.get('blocking') is None or blocking == int(rendered['blocking'])))
    return (full, True) if ok else (resp, False)
