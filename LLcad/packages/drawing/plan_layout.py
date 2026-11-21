"""Planner IV: layout - block sizes, shelf packing, candidates and metrics.

A block is the view group of one part or of the assembly including its estimated dimension space, titles and
tables; the note lines form a block of their own. Rules:
- The first view is the anchor (position via 'at'). Neighbors at the sheet scale are placed by first-angle projection via
  'place' at the anchor: viewing direction -Vx right, +Vx left, +Vy below, -Vy above (as in plan_views). Detail views,
  views with their own scale, oblique or doubled directions and tables are placed in turn to the right of
  (top-aligned) or below (left-aligned) an already placed occupancy, where the hull stays smallest
  (so the isometric view ends up in the free quadrant at the bottom right).
- Occupancy of a view: geometry (bounding box in the view frame times scale, detail view: circle) plus OVER
  centerline overshoot, per side SIDE + STEP · (linear dimensions on that side; the core stacks them at a spacing of
  10 + 7 · n), at the top TITLE for a title (at least as wide as it), with item numbers per side BALLOON.
  The title line of the block (TITLE) sits left-aligned above everything.
- Sheet-aware: arrange(..., sheet=(B, H)) chooses among the placements right/below by (1) fits on the sheet, (2) smallest
  max(b/B, h/H), (3) smallest area; so a wide placement that fits beats the narrow column that forces a larger size.
- Measured occupancy: arrange(..., measured={view: (left, bottom, right, top)}) replaces the estimate of a
  view by the hull measured from a probe (geometry, centerlines and all their annotations from the
  geometry center, without title) plus MEASURE_PAD; the title is added as in the estimate.
- pick: choice of size and scale among the admissible candidates (hard limits X, Y, fill; see violations/pick).
- pack: shelf packing (first fit, by descending height) from the top left, gap between blocks, gap/2 to the frame and
  to the zone at the bottom right (min(190, width) x stack_h: title block, revision block, parts list); afterwards the
  content is centered in the free area as far as it does not touch the zone."""
import functools
import json
import math
from dataclasses import dataclass, replace
import numpy as np
from matplotlib.textpath import TextPath
import analyze as an
import checks
import projection as prj
from sheet import CAP, CELL_SAFETY, FONT, sheet_size, usable_area

OVER = checks.OVER_MM   # mm, overshoot of the centerlines beyond the geometry
SIDE, STEP = 10.0, 7.0  # dimension space per side: SIDE + STEP · n
TITLE, TITLE_H = checks.BLOCK_TITLE, 5.0   # title of a view or title line of a block: space (one value with check N1.3, 10 mm), text height
TITLE_KEY = '#title'
FOOTPRINT_KEY = '#footprint'
RECTS_KEY = '#rects'          # {view: (l, b, r, t)} occupancy (including dimension space) per view of the block relative to the block corner; the zone checker asks views instead of the block box
AXES_KEY = '#axes'          # {'ys': [...]} heights of the geometry centres of all views above the block bottom edge (first = anchor); pack aligns blocks on these axes
TITLE_MAX_W = 150.0          # mm: a block title does not make its block wider than this; beyond it the text is shrunk/shortened
BALLOON = 22.0         # item number: leader line 14 mm + circle
MAIN_INFO = 0.85       # alternative main view at most 15 % less information value
FILL_BAND = (0.3, 0.9)     # a measured variant that fits may occupy up to 90 % of the free area
MEASURE_PAD = 1.0      # mm safety margin around the measured occupancy of a view
INNER = checks.VIEW_GAP_MM - 2 * MEASURE_PAD   # spacing between occupancies inside a block: the hulls of two views are at least VIEW_GAP_MM (6 mm) apart (one value for planner and check)
MIN_FEATURE_MM = 5.0   # smallest size dimension (Ø, R, thread, □, length) on paper: from 5 mm arrowheads and text stay legible
MIN_FILL = 0.35        # block areas / free area; white space at most 65 % (reference drawings are at 52-69 %)
BLOCK_UP_STEPS = 2     # at most this many standard steps above the sheet scale for a part block with a too small mandatory dimension
UP_STEPS = 3           # standard scales above the proposal that count as candidates
DOWN_STEPS = 2         # smaller scales tried for a single sheet before follow-up sheets are added
UP_FORMAT_MAX = 'A1'   # maximum size of a single sheet; A0 only if no candidate is admissible on A1 (plan._search measures again: a candidate lifted above A1 only by the measurement does not count, A0 only if nothing fits on A1)
X_MIN_MM = 2.5         # every dimension of a normal view is at least this long on paper
DETAIL_MIN_MM = 2.0    # ... in a detail view at least this long (plan_views.checks.MIN_FEATURE_MM)
Y_MAX_DM2, Y_MIN_ANNS = checks.Y_MAX_DM2, checks.Y_MIN_ANNS   # at most this many annotations per dm² of view area, only for views with at least Y_MIN_ANNS annotations
FILL_MAX = 0.9         # fill of the free area at most; below MIN_FILL only a warning
ISO_WINDOW = (60.0, 120.0)   # longest side of the part isometric view on paper
CLOSE_MM, SHORT_MM, GOOD_FEATURE_MM = 6.0, 8.0, 5.0
W_FILL, W_FEATURE, W_SHEET, W_COLLISION, W_SCALE = 2.0, 1.0, 0.5, 0.05, 0.1
SIZE_TYPES = ('linear', 'diameter', 'radius', 'angle', 'thread')
FREE_TYPES = ('diameter', 'radius', 'thread', 'leader', 'balloon')


@functools.lru_cache(maxsize=4096)
def text_width(s, h):
    """Width of an osifont text of text height h in mm (like Sheet.text_size)."""
    return float(TextPath((0, 0), str(s).replace('$', 'S'), size=h / CAP, prop=FONT).get_extents().width) if s else 0.0


def view_scale(v, k):
    """Scale of a view (detail view, own scale, otherwise k of the sheet)."""
    det = v.get('detail') or {}
    s = det.get('scale') or v.get('scale')
    return an.scale_value(s) if s else k


def _view_key(v):
    """Geometry identity of a view across several scale attempts of the same planner run."""
    detail = v.get('detail') or {}
    return (tuple(v.get('parts') or ([v['part']] if v.get('part') else [])),
            v.get('preset'), tuple(v.get('direction') or ()), tuple(v.get('up') or ()),
            tuple((v.get('section') or {}).get('point') or ()),
            tuple(detail.get('center') or ()), detail.get('radius'), bool(v.get('hidden')))


@dataclass(frozen=True)
class ViewMeasure:
    key: tuple
    bounds: tuple       # visible geometry in the model coordinates of the image frame
    margins: tuple      # distance of the four hull edges from the geometry centre on paper
    scale: float

    def on_paper(self, k):
        x0, y0, x1, y1 = self.bounds
        w, h = x1 - x0, y1 - y0
        delta = (k - self.scale) / 2
        occ = tuple(m + delta * span for m, span in zip(self.margins, (w, h, w, h)))
        return (w * k, h * k), occ, ((x0 + x1) * k / 2, (y0 + y1) * k / 2)


def _measurement(v, measured):
    m = (measured or {}).get(v['id'])
    return None if isinstance(m, ViewMeasure) and m.key != _view_key(v) else m


def _occupancy(v, anns, extents, k, measured=None):
    """(geometry w, h on paper, occupancy (left, bottom, right, top) from the geometry center)."""
    kv = view_scale(v, k)
    if v.get('detail'):
        gw = gh = 2 * float(v['detail']['radius']) * kv
    else:
        gw, gh = extents[v['id']]['w'] * kv, extents[v['id']]['h'] * kv
    m = _measurement(v, measured)
    center = None
    if isinstance(m, ViewMeasure):
        (gw, gh), m, center = m.on_paper(kv)
    if m is not None:
        return (*_titled(v, (gw, gh), tuple(float(x) + MEASURE_PAD for x in m)), center)
    n = dict.fromkeys(('left', 'below', 'right', 'above'), 0)
    balloon = False
    for a in anns:
        if a.get('view') != v['id']:
            continue
        if a.get('type') == 'linear':
            side = a.get('side') or {'h': 'above', 'v': 'left'}.get(a.get('axis'), 'left')
            n[side if side in n else 'left'] += 1
        balloon |= a.get('type') == 'balloon'
    m = {s: max(SIDE + STEP * c, BALLOON if balloon else 0.0) for s, c in n.items()}
    return (*_titled(v, (gw, gh), (gw / 2 + OVER + m['left'], gh / 2 + OVER + m['below'], gw / 2 + OVER + m['right'], gh / 2 + OVER + m['above'])), center)


def _titled(v, g, occ):
    """Occupancy plus title (space at the top, at least as wide as the title)."""
    L, D, R, U = occ
    if v.get('label'):
        U += TITLE
        half = text_width(v['label'], TITLE_H) / 2 + 1.0
        L, R = max(L, half), max(R, half)
    return g, (L, D, R, U)


_proj_side = prj.first_angle_side


def _axis_sides(fv, fr):
    """Sides of the reference view (frame fr) on which a view with frame fv can line up on a common image axis of the same sense: right/left at equal image y, below/above at equal image x."""
    out = []
    if np.allclose(fv[2], fr[2], atol=1e-6):
        out += ['right', 'left']
    if np.allclose(fv[1], fr[1], atol=1e-6):
        out += ['below', 'above']
    return out


def places(views, k):
    """Which views arrange places at a reference view with place: {view: {'ref': reference view, 'sides': [sides], 'proj': bool}} in the order of assignment. views[0] is the anchor.
    1. Neighbours of the anchor after method 1 (one per side, proj True).
    2. Further views at the sheet scale (no detail view), one after the other, until nothing more is added: (a) same frame as an already placed view (second section of the same direction): beside
          or below it, on its image axis (proj False); (b) otherwise projection position after method 1 to any already placed view whose side is still free (proj True, the label letter is dropped);
          (c) otherwise a common image axis of the same sense with an already placed view (proj False, the label letter stays). Views without a relation stay free (no entry).
    From frames and scales alone, without geometry; arrange chooses among several sides by hull area, plan._name_free names what does not stand in projection position."""
    out, taken = {}, {}
    if not views:
        return out
    fr = {v['id']: prj.frame_of(v) for v in views}
    ok = lambda v: not v.get('detail') and abs(view_scale(v, k) - k) < 1e-12
    anchor = views[0]['id']
    placed, taken[anchor] = [anchor], set()
    for v in views[1:]:
        side = _proj_side(fr[v['id']], fr[anchor]) if ok(v) else None
        if side and side not in taken[anchor]:
            taken[anchor].add(side)
            out[v['id']] = {'ref': anchor, 'sides': [side], 'proj': True}
            placed.append(v['id'])
            taken[v['id']] = set()
    pending = [v for v in views[1:] if v['id'] not in out and ok(v)]
    progress = True
    while pending and progress:
        progress = False
        for v in list(pending):
            f, hit = fr[v['id']], None
            same = next((r for r in reversed(placed) if np.allclose(f[0], fr[r][0], atol=1e-6) and np.allclose(f[1], fr[r][1], atol=1e-6)), None)      # the last placed one: sections of the same direction line up
            if same is not None:
                hit = (same, ['right', 'below', 'left', 'above'], False)
            if hit is None:
                for r in placed:
                    side = _proj_side(f, fr[r])
                    if side and side not in taken[r]:
                        taken[r].add(side)
                        hit = (r, [side], True)
                        break
            if hit is None:
                for r in placed:
                    sides = _axis_sides(f, fr[r])
                    if sides:
                        hit = (r, sides, False)
                        break
            if hit is not None:
                out[v['id']] = {'ref': hit[0], 'sides': hit[1], 'proj': hit[2]}
                placed.append(v['id'])
                taken[v['id']] = set()
                pending.remove(v)
                progress = True
    return out


def ordered(views, lay):
    """views ordered so that every view with place stands after its reference view (drawing.build_view places views in sequence and needs the reference view already); otherwise unchanged
    order (stable: a view only moves behind its reference view, the anchor stays first)."""
    ids = {v['id'] for v in views}
    ref = {v['id']: (lay.get(v['id']) or {}).get('place', {}).get('relative_to') for v in views}
    out, done, todo = [], set(), list(views)
    while todo:
        for v in todo:
            if ref[v['id']] is None or ref[v['id']] not in ids or ref[v['id']] in done:
                out.append(v)
                done.add(v['id'])
                todo.remove(v)
                break
        else:
            out += todo
            break
    return out


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _fit_key(w, h, sheet):
    """Sort key of an arrangement of size w x h: (not on the sheet, largest share of sheet width/height, area); without sheet
    only the area."""
    if not sheet:
        return (0, 0.0, w * h)
    ratio = max(w / sheet[0], h / sheet[1])
    return (ratio > 1 + 1e-9, round(ratio, 6), w * h)


def _outer_bands(rects):
    """Horizontal strips of the group hull. Between two views of a row the space stays reserved for the part."""
    edges = sorted({r[i] for r in rects for i in (1, 3)})
    bands = []
    for bottom, top in zip(edges, edges[1:]):
        row = [r for r in rects if r[1] < top and r[3] > bottom]
        if not row:  # keep the passage for the part free between two separate rows of views.
            row = [r for r in rects if r[1] == top or r[3] == bottom]
        left, right = min(r[0] for r in row), max(r[2] for r in row)
        if bands and (left, right) == (bands[-1][0], bands[-1][2]):
            bands[-1] = (left, bands[-1][1], right, top)
        else:
            bands.append((left, bottom, right, top))
    return bands


def arrange(views, anns, extents, k, tables=(), title=None, measured=None, sheet=None, search_width=1, variants=None):
    """(w, h, placements) of a block. views: ViewPlans (optional 'label'), extents: {view: {'w', 'h'}} in model mm,
    tables: [(name, w, h)] or [(name, w, h, reference view)] (the table is then placed next to its view), title: title line of the block (above everything, left-aligned). Placements relative to the lower left
    block corner: anchor and free views {'at': center of the geometry}, neighbors {'place': {relative_to, side, gap}},
    tables {'at': upper left corner}, TITLE_KEY {'at': baseline left}. sheet = (B, H): free sheet area of the candidate
    size; the placement right of or below an occupancy is then chosen by (1) fits on the sheet, (2) smallest
    max(b/B, h/H), (3) smallest hull area, without sheet by the hull area alone."""
    occ = {v['id']: _occupancy(v, anns, extents, k, measured) for v in views}
    anchor = views[0]
    center, hung = {anchor['id']: (0.0, 0.0)}, places(views, k)
    extras = [v['id'] for v in views[1:] if v['id'] not in hung]
    hull = lambda rs: (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs))

    def rect(vid):
        (L, D, R, U), (x, y) = occ[vid][1], center[vid]
        return (x - L, y - D, x + R, y + U)

    def hang(vid, ref, side):
        """Across the shift the model origin stays on the projection line.
        In stacking direction the complete occupancy keeps the distance to the neighbour."""
        ax, sg = {'right': (0, 1), 'left': (0, -1), 'below': (1, -1), 'above': (1, 1)}[side]
        L, D, R, U = occ[vid][1]
        a = occ[ref][1]
        reach = {'right': a[2] + L, 'left': a[0] + R, 'below': a[1] + U, 'above': a[3] + D}[side] + INNER      # minimum distance of the occupancies
        cx, cy = center[ref]
        here, there = occ[vid][2], occ[ref][2]
        cross = here[1-ax] - there[1-ax] if here is not None and there is not None else 0.0
        c = (cx + sg * reach, cy + cross) if ax == 0 else (cx + cross, cy + sg * reach)
        moved, clean = 0.0, True
        while True:                                          # every step passes at least one obstacle edge
            center[vid] = c
            r = rect(vid)
            clearance = (r[0] - INNER + 1e-8, r[1] - INNER + 1e-8, r[2] + INNER - 1e-8, r[3] + INNER - 1e-8)
            hit = [o for o in center if o != vid and _overlap(clearance, rect(o))]
            if not hit:
                break
            clean &= all(chosen.get(o) == (ref, side) for o in hit)      # only past siblings (same reference view and side); otherwise the view would stand beyond a foreign view
            o = rect(hit[0])
            push = {'right': o[2] - r[0], 'left': r[2] - o[0], 'below': r[3] - o[1], 'above': o[3] - r[1]}[side] + INNER
            c = (c[0] + sg * push, c[1]) if ax == 0 else (c[0], c[1] + sg * push)
            moved += push
        out = center.pop(vid)
        return out, moved, clean

    frames = {v['id']: prj.frame_of(v) for v in views}
    cutting_lines = {(a.get('view'), a.get('label')) for a in anns if a.get('type') == 'section_line'}
    marked = {v['id'] for v in views if v.get('section') and v.get('section_line_in')
              and (v['section_line_in'], v['section'].get('label')) in cutting_lines}
    rank = {'right': 0, 'left': 1, 'below': 2, 'above': 3}

    def options(vid, p):
        neighbours = [(p['ref'], side) for side in p['sides']]
        if vid in marked and search_width > 1:
            # The section trace and its letter fix the viewing direction unambiguously. An offset position
            # is only possible on an image axis of the same sense, also to the reference view itself.
            for ref in center:
                neighbours.extend((ref, side) for side in _axis_sides(frames[vid], frames[ref]))
        elif p['proj'] and search_width > 1:
            for ref in center:
                neighbours.extend((ref, side) for side in prj.first_angle_sides(frames[vid], frames[ref]))
        elif not p['proj']:
            # Named sections may also use the rail of another already placed section.
            # For parallel views the alignment with an equally oriented section keeps priority.
            parallel = len(p['sides']) == 4
            for ref in center:
                if ref == p['ref'] or (parallel and not np.allclose(frames[vid], frames[ref])):
                    continue
                neighbours.extend((ref, side) for side in _axis_sides(frames[vid], frames[ref]))
        for n, (ref, side) in enumerate(dict.fromkeys(neighbours)):
            c, moved, clean = hang(vid, ref, side)
            center[vid] = c
            b = hull([rect(o) for o in center])
            del center[vid]
            size = _fit_key(b[2] - b[0], b[3] - b[1] + (TITLE if title else 0.0), sheet)
            yield ref, side, c, int(not clean), size + (round(moved, 3), n)

    states = [(center, {}, 0)]
    for vid, p in sorted(hung.items(), key=lambda item: (0, rank[item[1]['sides'][0]])
                         if item[1]['ref'] == anchor['id'] and item[1]['proj'] else (1, 0)):
        branches = []
        for center, chosen, crossed in states:
            for ref, side, c, penalty, size in options(vid, p):
                branches.append(((crossed + penalty, *size), {**center, vid:c}, {**chosen, vid:(ref,side)}))
        states, signatures = [], set()
        for score, centers, links in sorted(branches, key=lambda branch: branch[0]):
            signature = tuple(centers.values())
            if signature not in signatures:
                states.append((centers, links, score[0]))
                signatures.add(signature)
            if len(states) >= search_width:
                break
    measured_group = len(views) >= 2 and measured and all(_measurement(v, measured) is not None for v in views)

    def finish():
        sides_of = {vid: side for vid, (_, side) in chosen.items()}
        placed = [rect(vid) for vid in center]
        near = {t[0]: t[3] for t in tables if len(t) > 3 and t[3]}       # table -> reference view (stands next to it)
        items = [(vid, *occ[vid][1]) for vid in extras] + [(t[0], 0.0, 0.0, t[1], t[2]) for t in tables]
        pos, at, cc = {}, {vid: rect(vid) for vid in center}, dict(center)       # cc: geometry centres of the views already placed (axes of freely placed views)
        for name, L, D, R, U in items:     # right of (top aligned) or below (left aligned) an occupancy, smallest hull
            w, h = L + R, D + U
            best = aligned = None
            base = [at[near[name]]] if near.get(name) in at else placed          # first at the reference view, if nothing is free there: anywhere
            for rs in (base, placed) if base is not placed else (placed,):
                cands = [(r[2] + INNER, r[3], False) for r in rs] + [(r[0], r[1] - INNER, False) for r in rs]
                if name in occ:             # a freely placed view shares at least one axis (row or column) with a view of the part: equal geometry centres
                    cands += [(r[2] + INNER, cc[o][1] + U, True) for o, r in ((o, at[o]) for o in cc) if r in rs]
                    cands += [(cc[o][0] - L, r[1] - INNER, True) for o, r in ((o, at[o]) for o in cc) if r in rs]
                for x, y, share in cands:
                    q = (x, y - h, x + w, y)
                    if not any(_overlap(q, r) for r in placed):
                        b = hull(placed + [q])
                        key = _fit_key(b[2] - b[0], b[3] - b[1] + (TITLE if title else 0.0), sheet) + (-y, x)
                        if best is None or key < best[0]:
                            best = (key, q)
                        if share and (aligned is None or key < aligned[0]):
                            aligned = (key, q)
                if best is not None:
                    break
            if aligned is not None and best is not None and aligned[0][:2] <= (best[0][0], best[0][1] * 1.03 + 1e-9) and aligned[0][2] <= best[0][2] * 1.05 + 1e-9:
                best = aligned          # the axis costs at most 3 % of the sheet share and 5 % of the hull area
            placed.append(best[1])         # below the lowest occupancy it is always free
            if name in occ:
                cc[name] = (best[1][0] + L, best[1][1] + D)
            pos[name] = best[1][:2]
            at[name] = best[1]
        x0, y0, x1, y1 = hull(placed)
        lay_out = {anchor['id']: {'at': [round(-x0, 3), round(-y0, 3)]}}
        for vid, side in sides_of.items():
            ref = chosen[vid][0]
            (gw, gh), (cx, cy), (rw, rh), (rx, ry) = occ[vid][0], center[vid], occ[ref][0], center[ref]
            gap = abs(cx - rx) - (rw + gw) / 2 if side in ('right', 'left') else abs(cy - ry) - (rh + gh) / 2
            lay_out[vid] = {'place': {'relative_to': ref, 'side': side, 'gap': round(gap, 3)}}
        for name, L, D, R, U in items:
            px, py = pos[name]
            if name in occ:
                lay_out[name] = {'at': [round(px + L - x0, 3), round(py + D - y0, 3)]}
            else:
                lay_out[name] = {'at': [round(px - x0, 3), round(py + U - y0, 3)]}
        lay_out[AXES_KEY] = {'ys': [round(c[1] - y0, 3) for c in cc.values()]}
        lay_out[RECTS_KEY] = {vid: (r[0] - x0, r[1] - y0, r[2] - x0, r[3] - y0) for vid in occ for r in [at[vid]]}
        if title:
            top_row = [r for r in placed if r[3] >= y1 - 1e-8]
            title_left = min(r[0] for r in top_row)
            title_end = title_left + min(text_width(title, TITLE_H) * CELL_SAFETY, TITLE_MAX_W) + 2.0
            lay_out[TITLE_KEY] = {'at': [round(title_left - x0, 3), round(y1 - y0 + TITLE - TITLE_H - 2.0, 3)]}
            placed.append((title_left, y1, max(title_end, *(r[2] for r in top_row)), y1 + TITLE))
            y1, x1 = y1 + TITLE, max(x1, title_end)
        if measured_group:
            for vid, (ref, side) in chosen.items():
                if side in ('above', 'below'):
                    lo, hi = sorted((at[ref], at[vid]), key=lambda r: r[1])
                    if hi[1] > lo[3]:
                        placed.append((min(lo[0], hi[0]), lo[3], max(lo[2], hi[2]), hi[1]))
            lay_out[FOOTPRINT_KEY] = [(l-x0, b-y0, r-x0, t-y0) for l, b, r, t in _outer_bands(placed)]
        return x1 - x0, y1 - y0, lay_out

    center, chosen, best_penalty = states[0]
    best = finish()
    if variants is not None:
        for center, chosen, penalty in states[1:]:
            if penalty == best_penalty:
                variants.append(finish())
    return best


def block_size(views, anns, extents, k, tables=(), title=None, measured=None, sheet=None):
    """(w, h) of a block: view group, dimension space per side 10 + 7 · (dimensions on the side), titles of the
    views and title line of the block (arrange)."""
    w, h, _ = arrange(views, anns, extents, k, tables, title, measured, sheet)
    return w, h


def block_scale(feature_mm, scale, limit=None, steps=BLOCK_UP_STEPS):
    """Standard scale (ISO 5455) of a part block whose smallest mandatory dimension (size dimension Ø, R, thread, □; feature_mm in model mm)
    is below the legibility limit MIN_FEATURE_MM at the sheet scale: the smallest of the next steps standard steps at which it
    reaches the limit. None if it is already legible, there is no dimension or even steps steps are not enough (nothing gained)."""
    limit = MIN_FEATURE_MM if limit is None else limit
    if feature_mm is None or feature_mm * an.scale_value(scale) >= limit - 1e-9:
        return None
    names = [an.scale_str(x) for x in an.SCALES]
    i = names.index(scale)
    return next((names[j] for j in range(i - 1, max(0, i - steps) - 1, -1) if feature_mm * an.scale_value(names[j]) >= limit - 1e-9), None)


def min_dim(anns, views, k):
    """Smallest size dimension (Ø, R, thread, □) of a normal view on paper in mm (limit X_MIN_MM) in views that are not detail views,
    at the scale of their view; None without size dimension. Length dimensions do not count: steps of tenths of a millimeter (0.48 mm) cannot be
    made legible by any standard scale, they belong to the view/dimension selection, not to the scale selection."""
    vs = {v['id']: v for v in views}
    got = [x for a in anns if (vd := vs.get(a.get('view'))) is not None and not vd.get('detail') for x in paper(a, vd, k)[:1] if x is not None]
    return min(got) if got else None


def min_len(anns, views, k):
    """Smallest length dimension of a normal view on paper in mm (soft ranking criterion below X_MIN_MM, no hard limit:
    steps of tenths of a millimeter belong to the dimension selection); detail views do not count; None without length dimension."""
    vs = {v['id']: v for v in views}
    got = [paper(a, vd, k)[1] for a in anns if (vd := vs.get(a.get('view'))) is not None and not vd.get('detail') and a.get('type') == 'linear']
    return min(got) if got else None


def min_detail(anns, views, k):
    """Smallest size dimension in a detail view on paper (limit DETAIL_MIN_MM); None without."""
    vs = {v['id']: v for v in views}
    got = [x for a in anns if (vd := vs.get(a.get('view'))) is not None and vd.get('detail') for x in paper(a, vd, k)[:1] if x is not None]
    return min(got) if got else None


def collisions(anns, views, k):
    """Estimated collision load: like metrics.est_collisions, but for annotations and views of a part without spec: pairs of free
    annotations (Ø, R, thread, note, item number) of the same view with anchors closer than CLOSE_MM on paper plus linear dimensions below SHORT_MM
    (text outside). Scale of a view: view_scale. The quantity is the planner precursor of text_overlap/text_on_line (estimable without rendering)."""
    vs = {v['id']: v for v in views}
    n, anchors = 0, {}
    for a in anns:
        vd = vs.get(a.get('view'))
        if vd is None:
            continue
        length = paper(a, vd, k)[1] if a.get('type') == 'linear' and a.get('p1') is not None else None
        if length is not None and length < SHORT_MM:
            n += 1
        p = _anchor(a) if a.get('type') in FREE_TYPES else None
        if p is not None:
            anchors.setdefault(vd['id'], []).append(prj.project([p], prj.frame_of(vd))[0] * view_scale(vd, k))
    for pts in anchors.values():
        n += sum(1 for i in range(len(pts)) for j in range(i) if np.linalg.norm(pts[i] - pts[j]) < CLOSE_MM)
    return n


def density(anns, views, extents, k):
    """Largest annotation density (annotations per dm² of view area, like the benchmark metric) among the views with at least Y_MIN_ANNS
    annotations (limit Y_MAX_DM2); the area is the geometry on paper plus OVER all around. 0.0 without such a view."""
    out = 0.0
    for v in views:
        n = sum(1 for a in anns if a.get('view') == v['id'])
        if n < Y_MIN_ANNS:
            continue
        kv = view_scale(v, k)
        gw, gh = (2 * float(v['detail']['radius']) * kv,) * 2 if v.get('detail') else (extents[v['id']]['w'] * kv, extents[v['id']]['h'] * kv)
        out = max(out, n / ((gw + 2 * OVER) * (gh + 2 * OVER) / 1e4))
    return out


def violations(min_dim_mm, min_detail_mm, dens, fill=None):
    """Violated hard limits as texts: X_MIN_MM, DETAIL_MIN_MM, Y_MAX_DM2, FILL_MAX; empty = admissible. None values do not count."""
    out = []
    if min_dim_mm is not None and min_dim_mm < X_MIN_MM - 1e-9:
        out.append(f'smallest dimension {min_dim_mm:.1f} mm < {X_MIN_MM:g} mm')
    if min_detail_mm is not None and min_detail_mm < DETAIL_MIN_MM - 1e-9:
        out.append(f'smallest dimension in detail view {min_detail_mm:.1f} mm < {DETAIL_MIN_MM:g} mm')
    if dens is not None and dens > Y_MAX_DM2 + 1e-9:
        out.append(f'density {dens:.0f}/dm² > {Y_MAX_DM2:g}/dm²')
    if fill is not None and fill > FILL_MAX + 1e-9:
        out.append(f'fill {fill:.2f} > {FILL_MAX:g}')
    return out


def pick(cands, order=an.ORDER):
    """Choice among the admissible candidates {format, index (scale, 0 = largest), ok}: the smallest size with an admissible candidate (sizes up to
    UP_FORMAT_MAX; A0 and larger only if none is admissible there), within it the largest sheet scale (smallest index). None if none is admissible. This is the preselection after
    the estimate; plan._search caps the result after the measurement at UP_FORMAT_MAX and otherwise falls back to the largest scale there."""
    ok = [c for c in cands if c['ok']]
    small = [c for c in ok if order.index(c['format']) <= order.index(UP_FORMAT_MAX)]
    return min(small or ok, key=lambda c: (order.index(c['format']), c['index']), default=None)


def spread(measured, findings, gap_min):
    """Measured occupancies {view: (left, bottom, right, top)} enlarged after views_too_close findings (views, gap_mm): each view involved
    all around by half of the missing gap (gap_min + 1 mm reserve - gap_mm), so that the re-arrangement keeps the minimum spacing. New dict."""
    out = dict(measured)
    for f in findings:
        if f.get('code') != 'views_too_close':
            continue
        d = max(0.0, gap_min + 1.0 - float(f.get('gap_mm') or 0.0)) / 2
        for vid in f.get('views') or []:
            if vid in out:
                m = out[vid]
                if isinstance(m, ViewMeasure):
                    out[vid] = replace(m, margins=tuple(x + d for x in m.margins))
                else:
                    out[vid] = tuple(x + d for x in m)
    return out


def iso_scale(ext_mm, scale, names=None):
    """Scale of the part isometric view: one to two standard steps below the sheet scale, the longest side (ext_mm, model mm) 60-120 mm on paper
    (ISO_WINDOW); the first step in the window, otherwise the one nearest to the window. The sheet scale itself if no smaller step exists."""
    names = names or [an.scale_str(x) for x in an.SCALES]
    i = names.index(scale)
    cands = [names[j] for j in (i + 1, i + 2) if j < len(names)] or [scale]
    lo, hi = ISO_WINDOW
    dist = lambda sc: max(lo - ext_mm * an.scale_value(sc), ext_mm * an.scale_value(sc) - hi, 0.0)
    return min(cands, key=lambda sc: (dist(sc), cands.index(sc)))


def _axes(a):
    """Axis heights of a block for pack: None -> [], number -> [number], sequence -> list."""
    return [] if a is None else [float(a)] if isinstance(a, (int, float)) else [float(x) for x in a]


def _remaining_rectangles(free, occupied):
    a, b, c, t = occupied
    remaining = []
    for x, y, r, u in free:
        if not _overlap(occupied, (x, y, r, u)):
            remaining.append((x, y, r, u))
            continue
        if x < a:
            remaining.append((x, y, a, u))
        if c < r:
            remaining.append((c, y, r, u))
        if y < b:
            remaining.append((x, y, r, b))
        if t < u:
            remaining.append((x, t, r, u))
    remaining = sorted(set(remaining))
    return [r for r in remaining if not any(s != r and s[0] <= r[0] and s[1] <= r[1]
            and s[2] >= r[2] and s[3] >= r[3] for s in remaining)]


def _place_in_free(blocks, initial, gap):
    orders = (lambda b: -b[2], lambda b: -b[1], lambda b: -b[1]*b[2])
    tried = set()
    for key in orders:
        order = sorted(blocks, key=key)
        ids = tuple(b[0] for b in order)
        if ids in tried:
            continue
        tried.add(ids)
        free, out = initial, {}
        for name, w, h, *_ in order:
            W, H = w + gap, h + gap
            fits = [r for r in free if W <= r[2]-r[0]+1e-9 and H <= r[3]-r[1]+1e-9]
            if not fits:
                break
            # Shortest remaining edge first, then top left. No rotation of the view frame.
            a, _, _, t = min(fits, key=lambda r: (min(r[2]-r[0]-W, r[3]-r[1]-H), -r[3], r[0]))
            b, c = t-H, a+W
            out[name] = (a, b)
            free = _remaining_rectangles(free, (a, b, c, t))
        else:
            return out
    return None


def _packing_field(fmt, orient, stack_h, gap):
    W, H = sheet_size(fmt, orient)
    right, bottom = W - 10 - gap / 2, 10 + gap / 2
    area = (20 + gap / 2, bottom, right, H - 10 - gap / 2)
    zone = (min(right, W - 10 - min(190.0, W - 30) - gap / 2), bottom + stack_h)
    return area, zone


def _free_field(area, zone, gap):
    left, bottom, right, top = area
    zx, zy = zone
    return [(left, bottom, min(right,zx) + gap, top + gap), (left, max(bottom,zy), right + gap, top + gap)]


def _pack_rectangles(blocks, area, zone, gap):
    """Use free rectangles beside the title block and below short blocks. Upper and right allowance secure the spacing."""
    left, bottom, right, top = area
    zx, zy = zone
    capacity = (right-left)*(top-bottom) - (right-max(left,zx))*max(0, min(zy,top)-bottom)
    if sum(b[1]*b[2] for b in blocks) > capacity + 1e-8:
        return None
    return _place_in_free(blocks, _free_field(area, zone, gap), gap)


def pack_in_corners(primary, bands, others, fmt, orient, stack_h, gap=10, zone=None):
    """Use free outer corners of a measured view group for complete other blocks. zone = (asm, accept) (soft): accept(at) checks the position including the main block;
    if the usual position fails the check, all further outer corners of the main block with the usual packing apply first, only then the assembly goes into a corner of the free rectangles for each outer corner (_place_zone).
    If none finds a position that passes, the first usual one stays (never None where there is a position today)."""
    area, zone_ = _packing_field(fmt, orient, stack_h, gap)
    left, bottom, right, top = area
    zx, zy = zone_
    name, w, h = primary[:3]
    if w > right-left+1e-8 or h > top-bottom+1e-8:
        return None
    used = sum((r-l)*(t-b) for l,b,r,t in bands) + sum(b[1]*b[2] for b in others)
    if used > (right-left)*(top-bottom) - (right-zx)*(min(zy,top)-bottom) + 1e-8:
        return None
    # First at the sheet corners, then with an outer edge of the part at the title block.
    xs = [left, right-w, *(zx-r for l,b,r,t in bands)]
    ys = [top-h, bottom, *(zy-b for l,b,r,t in bands)]
    fallback, spots = None, []
    for x in dict.fromkeys(xs):
        if not left-1e-8 <= x <= right-w+1e-8:
            continue
        for y in dict.fromkeys(ys):
            if not bottom-1e-8 <= y <= top-h+1e-8:
                continue
            placed = [(x+l,y+b,x+r,y+t) for l,b,r,t in bands]
            if any(r > zx+1e-8 and b < zy-1e-8 for l,b,r,t in placed):
                continue
            free = _free_field(area, zone_, gap)
            for l,b,r,t in placed:
                free = _remaining_rectangles(free, (l,b,r+gap,t+gap))
            at = _place_in_free(others, free, gap)
            if zone is None:
                if at is not None:
                    return {name:(x,y), **at}
                continue
            spots.append((x, y, free))
            if at is not None:
                full = {name:(x,y), **at}
                if zone[1](full):
                    return full
                fallback = fallback or full
    # only if none of the previous positions passes (the previous position without violation therefore stays unchanged), the assembly goes into a corner first for each outer corner
    if zone is not None and any(o[0] == zone[0] for o in others):
        for x, y, free in spots:
            got = _place_zone(others, free, gap, zone[0], lambda a, xy=(x, y): zone[1]({name: xy, **a}))
            if got is not None:
                return {name:(x,y), **got}
    return fallback


ASM_BLOCK = 'asm'      # name of the assembly block (plan._content)
NOTES_BLOCK = 'notes'  # note block: no part block


def zone_blocked(at, sizes, rects=None, asm=ASM_BLOCK, overlap=None):
    """True if part views lie on opposite sides of the assembly (left and right or above and below; the same side logic as the check
    assembly_between: checks.zone_side, overlap at least ZONE_OVERLAP). at {block: (x, y) lower left corner}, sizes {block: (w, h)}; rects {block: [(l, b, r, t)]} relative to the block corner
    (plan._zone_rects: view rectangles, RECTS_KEY): for the assembly the views with item numbers, for part blocks the dimensioned views (an empty list: nothing dimensioned);
    without an entry the block box applies. The note block is no part block. False without an assembly or without a part block."""
    if asm not in at or asm not in sizes:
        return False
    rects = rects or {}

    def boxes(name):
        x, y = at[name]
        if rects.get(name) is not None:
            return [(x + l, y + b, x + r, y + t) for l, b, r, t in rects[name]]
        return [(x, y, x + sizes[name][0], y + sizes[name][1])]
    parts = [r for name in at if name not in (asm, NOTES_BLOCK) and name in sizes for r in boxes(name)]
    return any(checks.zone_between([side for rect in parts if (side := checks.zone_side(rect, z, overlap))]) for z in boxes(asm))


def _place_zone(blocks, free0, gap, asm, accept):
    """The assembly asm first into a corner of the free rectangles free0 (top left, bottom left, top right, bottom right), then the remaining blocks as in _place_in_free;
    the first position that accept(at) (at only with the blocks placed here) passes applies. None without such a position."""
    ablock = next(b for b in blocks if b[0] == asm)
    rest = [b for b in blocks if b[0] != asm]
    W, H = ablock[1] + gap, ablock[2] + gap
    spots = {}
    for r in free0:
        if W <= r[2] - r[0] + 1e-9 and H <= r[3] - r[1] + 1e-9:
            for rank, spot in enumerate(((r[0], r[3] - H), (r[0], r[1]), (r[2] - W, r[3] - H), (r[2] - W, r[1]))):
                spots.setdefault((round(spot[0], 6), round(spot[1], 6)), rank)
    for (a, b), _ in sorted(spots.items(), key=lambda kv: (kv[1], -kv[0][1], kv[0][0])):
        free = _remaining_rectangles(free0, (a, b, a + W, b + H))
        at = _place_in_free(rest, free, gap)
        if at is None:
            continue
        out = {asm: (a, b), **at}
        if accept(out):
            return out
    return None


def pack_zone(blocks, fmt, orient, stack_h, gap=10, asm=ASM_BLOCK, rects=None):
    """{block: (x, y)} like pack, with the assembly first in a corner of the free area and the remaining blocks after it (_place_zone); the first position without a violation of N1.9
    (zone_blocked, with the view rectangles rects) applies, the content is centered as in pack. None if the assembly is missing or no corner position takes all blocks."""
    if asm not in [b[0] for b in blocks]:
        return None
    area, zone = _packing_field(fmt, orient, stack_h, gap)
    (left, bottom, right, top), (zone_x, zone_y) = area, zone
    sizes = {b[0]: (b[1], b[2]) for b in blocks}
    got = _place_zone(blocks, _free_field(area, zone, gap), gap, asm, lambda out: not zone_blocked(out, sizes, rects, asm))
    return None if got is None else _centered(got, sizes, right, bottom, lambda y: right if y >= zone_y - 1e-9 else zone_x)      # like pack: margins for axis labels


def pack(blocks, fmt, orient, stack_h, gap=10):
    """{block: (x, y) lower left corner} in shelf packing (first fit, by descending height) into the drawing area without
    the zone at the bottom right; None if a block does not fit. Afterwards the content is centered in the free area
    (both directions, otherwise horizontally only, otherwise vertically only) as far as it does not touch the zone.
    blocks: (name, w, h) or (name, w, h, cy) with cy = height of the geometry centre of the first view above the block bottom edge, or a sequence of such heights (all views, first first).
    Row axis: blocks of a shelf that are lower than the shelf move vertically so that the centre of one of their views lies on the centre of a view of another block
    of the shelf, as far as the shelf height and the zone permit: first first view on first view of the highest block (which opens the shelf), otherwise the smallest shift among all
    pairs of view axes. Blocks that already have an axis partner on the shelf (±GRID_TOL_MM) do not move (the common top edge of the titles stays); otherwise the
    top edge stays. Blocks without cy (note block) stay on top. Size, sizes and order never change as a result."""
    (left, bottom, right, top), (zone_x, zone_y) = _packing_field(fmt, orient, stack_h, gap)
    limit = lambda y: right if y >= zone_y - 1e-9 else zone_x
    shelves, out, size = [], {}, {b[0]: (b[1], b[2]) for b in blocks}   # shelf: [top edge, height, next x, names]
    axis = {b[0]: _axes(b[3] if len(b) > 3 else None) for b in blocks}
    for name, w, h, *_ in sorted(blocks, key=lambda b: -b[2]):
        for sh in shelves:
            if h <= sh[1] + 1e-9 and sh[2] + w <= limit(sh[0] - h) + 1e-9:
                out[name] = (sh[2], sh[0] - h)
                sh[2] += w + gap
                sh[3].append(name)
                break
        else:
            y_top = shelves[-1][0] - shelves[-1][1] - gap if shelves else top
            if y_top - h < bottom - 1e-9 or left + w > limit(y_top - h) + 1e-9:
                return _pack_rectangles(blocks, (left, bottom, right, top), (min(right, zone_x), max(bottom, zone_y)), gap)
            shelves.append([y_top, h, left + w + gap, [name]])
            out[name] = (left, y_top - h)
    tol = checks.GRID_TOL_MM
    for y_top, sh_h, _, names in shelves:                   # row axis
        first = names[0]
        if not axis[first]:
            continue
        for n in names[1:]:
            w, h = size[n]
            if not axis[n] or h >= sh_h - 1e-9:
                continue
            others = [out[o][1] + a for o in names if o != n for a in axis[o]]
            if any(abs(out[n][1] + a - t) <= tol - 0.2 for a in axis[n] for t in others):       # already an axis partner (with reserve to the check tolerance): the top edge stays
                continue
            fits = lambda y: y_top - sh_h - 1e-9 <= y <= y_top - h + 1e-9 and out[n][0] + w <= limit(y) + 1e-9
            legacy = out[first][1] + axis[first][0] - axis[n][0]
            if fits(legacy):
                out[n] = (out[n][0], legacy)
                continue
            ys = [t - a for a in axis[n] for t in others if fits(t - a)]
            if ys:
                out[n] = (out[n][0], min(ys, key=lambda y: abs(y - out[n][1])))
    return _centered(out, size, right, bottom, limit)


def _centered(out, size, right, bottom, limit):
    """Centre the content in the free area (both directions, otherwise horizontally only, otherwise vertically only) as far as it does not touch the zone (limit(y) = right limit at height y)."""
    if not out:
        return out
    dx = (right - max(x + size[n][0] for n, (x, y) in out.items())) / 2
    dy = -(min(y for x, y in out.values()) - bottom) / 2
    for fx, fy in ((dx, dy), (dx, 0.0), (0.0, dy)):
        moved = {n: (round(x + fx, 3), round(y + fy, 3)) for n, (x, y) in out.items()}
        if all(x + size[n][0] <= limit(y) + 1e-9 for n, (x, y) in moved.items()):
            return moved
    return out


def _signature(spec):
    """Sheets without placements: same signature = same drawing in a different arrangement."""
    return json.dumps([[sd.get('format'), sd.get('orientation'), sd.get('scale'),
                        [{k: v for k, v in vd.items() if k not in ('at', 'place', 'label')} for vd in sd.get('views') or []]]
                       for sd in spec['sheets']], sort_keys=True)


def candidate_order(m):
    """Sort key of the candidates from their metrics - score descending, at equal score the smaller size (analyze.ORDER) first; the size step is
    only a tie-break, not part of the score. Without a score the candidate counts last. One rule for the planner (candidates) and the choice in the runner (runner.pick)."""
    s = m.get('score')
    fmt = m.get('format')
    return (-(s if s is not None else float('-inf')), an.ORDER.index(fmt) if fmt in an.ORDER else len(an.ORDER))


def candidates(base, alternatives, max_n):
    """[{candidate_id, spec, metrics}] from variants {kind, spec, packed ({block: (x, y, w, h)}, None = does not fit),
    info?}: base = proposal, alternatives e.g. next larger size ('format'), next smaller scale ('scale'),
    alternative main view ('main', info = information value relative to the best, at least MAIN_INFO). Variants that cannot be packed
    and duplicates are dropped; by candidate_order (score descending, tie: smaller size, then input order), at most max_n."""
    seen, kept = set(), []
    for var in [base] + list(alternatives):
        if not var or var.get('packed') is None:
            continue
        if var.get('kind') == 'main' and var.get('info', 0.0) < MAIN_INFO - 1e-9:
            continue
        sig = _signature(var['spec'])
        if sig not in seen:
            seen.add(sig)
            kept.append((var['spec'], {**metrics(var['spec'], var['packed']), **(var.get('extra') or {})}))      # extra = metrics from the dimensioning (derivable_left), not in the score
    kept.sort(key=lambda sm: candidate_order(sm[1]))
    return [{'candidate_id': f'c{i + 1}', 'spec': s, 'metrics': m} for i, (s, m) in enumerate(kept[:max(1, int(max_n))])]


def paper(a, vd, k):
    """(form feature size, measured length) of an annotation on paper, each None if not applicable."""
    kv = view_scale(vd, k)
    t = a.get('type')
    if t == 'diameter':
        return 2 * float(a['radius']) * kv, None
    if t == 'radius':
        return float(a['radius']) * kv, None
    if t == 'thread':
        return float(a['major_d']) * kv, None
    if t == 'linear':
        d = prj.project([a['p2']], prj.frame_of(vd))[0] - prj.project([a['p1']], prj.frame_of(vd))[0]
        L = abs(d[0]) if a.get('axis') == 'h' else abs(d[1]) if a.get('axis') == 'v' else float(np.linalg.norm(d))
        return (L * kv if a.get('prefix') == '□' else None), L * kv
    return None, None


def _anchor(a):
    if a.get('type') == 'thread':
        return (np.asarray(a['start'], float) + np.asarray(a['end'], float)) / 2
    p = a.get('center') if a.get('type') in ('diameter', 'radius') else a.get('p')
    return None if p is None else np.asarray(p, float)


def metrics(spec, packed):
    """Metrics of a candidate; packed = {block: (x, y, w, h[, occupied strip area])} over all sheets.
    fill = block areas / free drawing area (sheet.usable_area); min_feature_mm = smallest size dimension (Ø, R, thread,
    □) on paper at the scale of its view; est_collisions = pairs of free annotations (Ø, R, thread, note,
    item number) of the same view with anchors closer than CLOSE_MM plus linear dimensions below SHORT_MM (text outside).
    score = W_FILL · (1 within the band FILL_BAND, otherwise linear down to 0 at a distance of 0.3) + W_FEATURE · min(1, min_feature_mm /
    GOOD_FEATURE_MM) - W_SHEET per follow-up sheet - W_COLLISION per collision (at most 0.5) + W_SCALE · log2(sheet scale).
    derivable_left (rank test as a report, not in the score): 0 here, the dimensioning (plan_dims.derivable_left) supplies it via the variant (extra) in candidates.
    slant_merged (metric only, not in the score): number of slant entries in also (per "n× ..." angle or length dimension, slant dimensions of the group without the carrier)."""
    sheets = spec['sheets']
    s0 = sheets[0]
    cap = sum(usable_area(sd['format'], sd['orientation'], max(1, len(sd.get('parts_list') or []))) for sd in sheets)
    fill = sum(float(b[4]) if len(b) > 4 else float(b[2])*float(b[3]) for b in packed.values()) / cap
    sizes, collisions, n_views, n_dims, slant_merged = [], 0, 0, 0, 0
    for sd in sheets:
        k = an.scale_value(sd['scale'])
        views = {vd['id']: vd for vd in sd.get('views') or []}
        n_views += len(views)
        anchors = {}
        for a in sd.get('annotations') or []:
            vd = views.get(a.get('view'))
            if vd is None:
                continue
            n_dims += a.get('type') in SIZE_TYPES
            slant_merged += len(a.get('also') or []) if a.get('type') in ('angle', 'linear') and '#slant' in str(a.get('feature') or '') else 0      # slant dimensions moved into also
            size, length = paper(a, vd, k)
            if size is not None:
                sizes.append(size)
            if length is not None and length < SHORT_MM:
                collisions += 1
            p = _anchor(a) if a.get('type') in FREE_TYPES else None
            if p is not None:
                anchors.setdefault(vd['id'], []).append(prj.project([p], prj.frame_of(vd))[0] * view_scale(vd, k))
        for pts in anchors.values():
            collisions += sum(1 for i in range(len(pts)) for j in range(i) if np.linalg.norm(pts[i] - pts[j]) < CLOSE_MM)
    lo, hi = FILL_BAND
    off = lo - fill if fill < lo else fill - hi if fill > hi else 0.0
    min_feature = round(min(sizes), 3) if sizes else None
    score = (W_FILL * (1 - min(1.0, off / 0.3)) + W_FEATURE * (1.0 if min_feature is None else min(1.0, min_feature / GOOD_FEATURE_MM))
             - W_SHEET * (len(sheets) - 1) - min(0.5, W_COLLISION * collisions) + W_SCALE * math.log2(an.scale_value(s0['scale'])))
    return {'format': s0['format'], 'orientation': s0['orientation'], 'scale': s0['scale'], 'sheets': len(sheets),
            'fill': round(fill, 3), 'min_feature_mm': min_feature, 'est_collisions': collisions, 'views': n_views,
            'dims': n_dims, 'slant_merged': slant_merged, 'derivable_left': 0, 'score': round(score, 4)}
