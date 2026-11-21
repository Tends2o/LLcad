"""Layout repair: moves annotations and views until the layout findings (LAYOUT) disappear.

repair(spec) renders once in full; in each round every owner with a layout finding simultaneously gets its next
untried move (moves), then the state is checked once. Per owner: keep the move if its layout findings
decrease and the findings in which it takes part as a line (other) do not increase, otherwise
revert it; if the number of blocking findings rises, only the moves with an improvement remain. The next round
checks the cleaned state as well; interactions between the moves of one round are accepted. Findings of the round probe with
a reverted owner stem from its discarded position: they are replaced by its findings from the state
before the round (it is back there; so it and its partners stay candidates), except those in which an owner kept in the
round takes part or something that moves along with a kept view (stale). Owners
that share a finding (text overlap, view overlap) do not move in the same round - identical moves
(both 5 mm up, both to the same angle) would never separate them; whoever has tried fewer moves has priority, otherwise
the order. If no owner has a move left but the spec differs from the last probe, the cleaned
state is checked (counts as a round) and the loop continues: findings that only a reverted move had hidden come
back this way. End (autofix.ended_by) with no open moves (no_moves: also with 0 layout findings), at max_rounds (60,
deterministic; the finite move lists end the loop anyway) or at the budget (safeguard; afterwards a probe
of the cleaned state, if necessary). A probe that throws counts as a failed attempt of the moves of its round (reverted,
ticked off, in autofix.errors). Best state: fewest blocking, then fewest layout findings; on a
tie a state of the spec before a probe with reverted moves, otherwise the later one. Other findings
(coverage, title block, ...) remain; strict acts as in the render.

Lean probes: rounds check with drawing.layout_probe in the session (Drawing) of the initial render -
views (HLR, sections, centre lines) are built once and only repositioned, for each probe annotations are
placed and the sheet, drawing, section and view findings are computed (without PDF/PNG, without coverage). The
blocking findings that the probe does not check (coverage, form features) are added from the initial render (they
do not depend on the position). Cross-check: the probe of the initial state must show its layout findings. The best
state gets a full render at the end (independent, without a session), which must show the same layout findings as its
probe. If the cross-check or this full render deviates, repair renders in full each round for the rest of the run
(autofix.fallback), starting from the fully rendered best state. The best full render is returned; if the full render
of the best probe state ranks behind an earlier full render, this is noted in autofix.dropped.

Owners (sheet.items 'owner', finding fields owner/owners/view/views) and their spec entry on the finding's sheet:
- 'ann:i'            entry i of annotations + dims (order as in drawing.render)
- 'label:<part>:<P>' point label P of the bend table with bend_table <part> and labels_in (field label_to[P])
- 'view:<id>'        view <id> including its title
- 'notes'            note block
Title block, revision block, parts list and tables are fixed. For text_on_line the text is the owner, the
line (other) only if the text cannot be moved. Order: annotations, labels and notes before views,
otherwise as in the first finding.

Moves change only position fields (side, offset, angle, leader, to, label_to, at_view/at_sheet, place.gap, at of a view,
notes.at), never dimension points, values, texts or tags. The list per owner is derived from its position at the first finding
(spec and render of that moment):
- linear: switch side (offset is dropped, the core restacks), then offset = previous distance + 7 * n (n = 1..3;
  without offset the distance the core chose)
- diameter/radius/thread: angle 45, 135, 225, 315, 0, 90, 180, 270 (without the previous one), then leader 12 (not for
  radius: the R dimension has no leader line)
- Ø that the core draws linearly across the axis (axis in the image plane; recognisable by the sheet points p1_sheet/p2_sheet
  in the render's annotation record): offset to the other side (-offset), then offset +- 7 * n (n = 1..3);
  angle/leader have no effect there. Without a render the circle view applies.
- angle (angular dimension): radius of the arc + 6, + 12, - 5 (at least 4 mm)
- balloon/leader/point_label: to rotated by +30, -30, +60, -60, +90, -90 degrees, then the current length * 1.5; leader afterwards LEADER_FREE
  (fixed directions 16/12 mm). A move counts only with fewer findings of the owner (box check: the same findings text_overlap/text_on_line/text_on_geometry
  as checks), so a collision-free planner position keeps its to
- bend table label: label_to[P] in 8 directions (45, 135, ..., 270 degrees) times length 2.5 or 4
- text: at_view or at_sheet by 5 mm up, down, left, right
- view with place: gap + 10 * n (n = 1..3); with at: away from the partner by the overlap + 5 mm (partner: the other view,
  for overlaps_block the block, for out_of_area the edge of the drawing area), not if the partner depends on it
- leader first with the text outside the view envelope (to, shortest position first, '(outside)'); linear and linearly
  drawn Ø: text_shift (dimension number +-6 ... 30 mm along the dimension line, before the stack for text_on_line/text_on_geometry, otherwise after it);
  radius: text_out (text on the outward extension), with span (arc angle from the planner) only small rotations within the arc;
  angle: radius also far out; section_line/detail_circle/view_arrow: label_shift of the identifying letter; view with a title:
  label_dx for text findings. The line of a text finding (other) moves only when the owner has no move left (line_movers); its
  move counts if the findings in which it takes part as a line decrease
- note block: notes.at by n lines (1.6 * h) upwards (n = 1..3)
Every full render writes into its own folder; the files of the returned render end up in out_dir. Without
cache_dir/feature_cache_dir the loop uses temporary caches (full renders hit the HLR cache, whose key
does not contain the position)."""
import copy
import math
import os
import shutil
import tempfile
import time
import numpy as np
import checks
import drawing
from sheet import FORMATS, Sheet, unit

LAYOUT = ('out_of_area', 'overlaps_block', 'view_overlap', 'views_too_close', 'text_overlap', 'text_on_line', 'text_on_geometry')
OFFSET_STEP, OFFSET_TRIES = 7.0, 3          # linear: offset + 7 · n
ANGLES = (45, 135, 225, 315, 0, 90, 180, 270)   # diameter/radius/thread; also the directions of the bend table labels
LEADER = 12                                 # then leader 12
TURNS = (30, -30, 60, -60, 90, -90)         # rotation of to in degrees
STRETCH = 1.5                               # then length * 1.5
LEADER_FREE = ((16.0, 0.0), (-16.0, 0.0), (0.0, 16.0), (0.0, -16.0), (12.0, 12.0), (-12.0, 12.0), (12.0, -12.0), (-12.0, -12.0))     # leader lines: fixed evasion directions after the rotations
LABEL_LENGTHS = (2.5, 4.0)
TEXT_STEP = 5.0
GAP_STEP, GAP_TRIES = 10.0, 3
CLEAR = 5.0                                 # view with at: overlap + 5 mm
NOTE_TRIES = 3
TO_DEFAULT = {'balloon': [12.0, 12.0], 'leader': [8.0, 8.0], 'point_label': [2.5, 2.5]}   # as in drawing.annotate
LABEL_DEFAULT = [2.0, 2.0]                  # as in drawing.table
GAP_DEFAULT, NOTE_H = 25.0, 3.5
SIDE_DEFAULT = {'h': 'above', 'v': 'left'}
OPPOSITE = {'above': 'below', 'below': 'above', 'left': 'right', 'right': 'left'}
MOVABLE = ('linear', 'diameter', 'radius', 'thread', 'angle', 'balloon', 'leader', 'point_label', 'text', 'section_line', 'detail_circle', 'view_arrow')
TEXT_CODES = ('text_overlap', 'text_on_line', 'text_on_geometry')
SHIFT_FIRST = ('text_on_line', 'text_on_geometry')       # text on line/edge: move the dimension number first; two dimensions on top of each other (text_overlap) are separated by the stack first
TEXT_SHIFTS = (6.0, -6.0, 12.0, -12.0, 20.0, -20.0, 30.0, -30.0)    # dimension number along the dimension line (text_shift, mm)
LABEL_SHIFTS = (4.0, 8.0, 14.0)                                      # identifying letter/title: distance from the planner's place (label_shift or label_dx)
LABEL_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1))
TEXT_OUT = (10.0, 18.0, 28.0, 40.0)          # R dimension: distance of the text from the arc (default 4)
ARC_TURNS = (0.2, 0.4, 0.5)                # R dimension with span: rotation of the tip by this fraction of the arc angle (per direction)
ANGLE_FAR = (20.0, 30.0, 45.0)        # angular arc: far out, the text then stands outside the body


def _r(x):
    return round(float(x), 2) + 0.0


def _num(x):
    s = ('%.2f' % _r(x)).rstrip('0').rstrip('.')
    return '0' if s in ('', '-0') else s


def _vec(v):
    return '[' + ', '.join(_num(c) for c in v) + ']'


def _pair(v):
    try:
        x, y = (float(c) for c in v)
    except (TypeError, ValueError):
        return None
    return [x, y]


# ------------------------------------------------------------------ Owner -> spec entry

def _sheet(spec, si):
    sheets = spec.get('sheets') or []
    return sheets[si] if 0 <= si < len(sheets) and isinstance(sheets[si], dict) else None


def _anns(spec, si):
    sd = _sheet(spec, si) or {}
    return list(sd.get('annotations') or []) + list(sd.get('dims') or [])


def _ann(spec, si, i):
    anns = _anns(spec, si)
    a = anns[i] if 0 <= i < len(anns) else None
    return a if isinstance(a, dict) else None


def _view(spec, si, vid):
    return next((vd for vd in (_sheet(spec, si) or {}).get('views') or [] if isinstance(vd, dict) and vd.get('id') == vid), None)


def _bend_table(spec, si, key):
    """(table, point name) of a label 'label:<part>:<P>'."""
    pid, _, name = key[len('label:'):].rpartition(':')
    t = next((t for t in (_sheet(spec, si) or {}).get('tables') or []
              if isinstance(t, dict) and t.get('bend_table') == pid and t.get('labels_in')), None)
    return t, name


def _movable(spec, si, key):
    key = str(key or '')
    if key.startswith('ann:'):
        a = _ann(spec, si, int(key[4:])) if key[4:].isdigit() else None
        return a is not None and a.get('type') in MOVABLE
    if key.startswith('label:'):
        t, _ = _bend_table(spec, si, key)
        return t is not None and isinstance(t.get('label_to') or {}, dict)
    if key.startswith('view:'):
        return _view(spec, si, key[5:]) is not None
    return key == 'notes' and bool((_sheet(spec, si) or {}).get('notes'))


def owners_of(finding, spec):
    """Owner of a finding in field order (owner, owners, view, views as 'view:<id>'); for text_on_line the
    line (other) only if none of them can be moved."""
    si = int(finding.get('sheet') or 0)
    views = ([finding['view']] if finding.get('view') else []) + list(finding.get('views') or [])
    keys = []
    for k in [finding.get('owner'), *(finding.get('owners') or []), *(f'view:{v}' for v in views)]:
        if k and k not in keys:
            keys.append(k)
    other = finding.get('other')
    if other and other not in keys and not any(_movable(spec, si, k) for k in keys):
        keys.append(other)
    return keys


def line_movers(findings, spec):
    """Movable lines (field other) of text findings that are not owners already: they move only when no owner of the finding
    has a move left (titles and identifying letters whose move alone is not enough), [(sheet, owner)]."""
    out = []
    for f in findings:
        si = int(f.get('sheet') or 0)
        other = f.get('other')
        if other and other not in owners_of(f, spec) and _movable(spec, si, other) and (si, other) not in out:
            out.append((si, other))
    return out


# ------------------------------------------------------------------ Moves

def moves(spec, finding, tried, result=None):
    """[(name, move)] of the first movable owner of finding (owners_of) in fixed order, without the names
    in tried. A move changes a spec with the same entries (spec or a copy) in place; spec itself stays
    unchanged. result = render of spec: distance of linear dimensions without offset, envelopes of the views with at, whether the core
    drew a Ø linearly (without result the first two moves are missing, Ø count as a circle view)."""
    si = int(finding.get('sheet') or 0)
    key = next((k for k in owners_of(finding, spec) if _movable(spec, si, k)), None)
    if key is None:
        out = []
    elif key.startswith('ann:'):
        out = _annotation_moves(spec, si, int(key[4:]), result, finding.get('code'))
    elif key.startswith('label:'):
        out = _label_moves(spec, si, key)
    elif key.startswith('view:'):
        out = _view_moves(spec, si, key[5:], finding, result)
    else:
        out = _notes_moves(spec, si)
    return [(name, fn) for name, fn in out if name not in tried]


def _annotation_moves(spec, si, i, result, code=None):
    a = _ann(spec, si, i)
    t = a.get('type')

    def put(**fields):                                  # None removes the field
        def move(s):
            b = _ann(s, si, i)
            for k, v in fields.items():
                if v is None:
                    b.pop(k, None)
                else:
                    b[k] = copy.deepcopy(v)
        return move
    out = []
    shifts = []
    if t == 'radius':                    # the text moves outward on the extension of the radius line (text_out), the tip stays on the arc; neighbours start with different distances (index) so their texts do not collide
        far = TEXT_OUT[i % len(TEXT_OUT):] + TEXT_OUT[:i % len(TEXT_OUT)]
        out += [(f'text_out {_num(d)}', put(text_out=d)) for d in far if d > float(a.get('text_out', 4.0))]
    if t == 'linear' or (t == 'diameter' and 'p1_sheet' in (_record(result, si, i) or {})):     # move the dimension number along the dimension line (ISO 129-1), for text findings before everything else
        s0 = float(a.get('text_shift', 0.0))
        shifts = [(f'text_shift {_num(s0 + d)}', put(text_shift=_r(s0 + d))) for d in TEXT_SHIFTS]
        if code in SHIFT_FIRST:
            out += shifts
    if t == 'linear':
        axis = a.get('axis', 'aligned')
        side = a.get('side') or SIDE_DEFAULT.get(axis, 'left')
        if side in OPPOSITE:
            out.append((f'side {OPPOSITE[side]}', put(side=OPPOSITE[side], offset=None)))
        o0 = _offset(spec, si, i, a, result)
        if o0 is not None:
            sg = 1.0 if o0 >= 0 else -1.0
            for n in range(1, OFFSET_TRIES + 1):
                o = _r(o0 + sg * OFFSET_STEP * n)
                out.append((f'offset {_num(o)}', put(offset=o, side=a.get('side'))))
    elif t == 'diameter' and 'p1_sheet' in (_record(result, si, i) or {}):    # drawn linearly by the core
        o0 = _r(a.get('offset', 0.0))
        s = 1.0 if o0 >= 0 else -1.0
        offs = ([-o0] if o0 else []) + [_r(o0 + sg * OFFSET_STEP * n) for n in range(1, OFFSET_TRIES + 1) for sg in (s, -s)]
        out += [(f'offset {_num(o)}', put(offset=o)) for o in dict.fromkeys(offs) if o != o0]
    elif t == 'radius' and a.get('span') is not None:        # the tip stays on the arc (span = its angle range in degrees, from the planner): only small rotations within it
        a0, span = float(a.get('angle', 45)), float(a['span'])
        out += [(f'angle {_num(a0 + sg * f * span)}', put(angle=_r(a0 + sg * f * span))) for f in ARC_TURNS for sg in (1, -1)]
    elif t in ('diameter', 'radius', 'thread'):
        a0 = float(a.get('angle', 45))
        out += [(f'angle {ang}', put(angle=ang)) for ang in ANGLES if abs((ang - a0 + 180) % 360 - 180) > 1e-6]
        if t != 'radius' and a.get('leader') != LEADER:
            out.append((f'leader {LEADER}', put(leader=LEADER)))
    elif t == 'angle':       # angular arc: its radius on paper (position field, not the value); the vertex stays
        r0 = float(a.get('radius', 12.0))
        out += [(f'radius {_num(r)}', put(radius=r)) for r in dict.fromkeys(_r(x) for x in (r0 + 6.0, r0 + 12.0, max(4.0, r0 - 5.0), *(r0 + d for d in ANGLE_FAR))) if r != r0]
    elif t in TO_DEFAULT:
        t0 = _pair(a.get('to', TO_DEFAULT[t]))
        if t == 'leader':                   # first the text outside the view envelope, shortest leader line first
            out += [(f'to {_vec(v)} (outside)', put(to=v)) for v in _outward(a, _record(result, si, i), result)]
        if t0 is not None:
            for d in TURNS:
                c, s_ = math.cos(math.radians(d)), math.sin(math.radians(d))
                v = [_r(t0[0] * c - t0[1] * s_), _r(t0[0] * s_ + t0[1] * c)]
                out.append((f'to {_vec(v)}', put(to=v)))

            def stretch(s):
                b = _ann(s, si, i)
                b['to'] = [_r(x * STRETCH) for x in _pair(b.get('to', TO_DEFAULT[t]))]
            out.append((f'to x{_num(STRETCH)}', stretch))
            if t == 'leader':           # leader lines then try fixed directions in 8 corners/axes (LEADER_FREE), each counts only with fewer findings
                tried = {tuple(_pair(a.get('to', TO_DEFAULT[t])))}
                out += [(f'to {_vec(list(v))}', put(to=list(v))) for v in LEADER_FREE if v not in tried]
    elif t == 'text':
        k = 'at_sheet' if 'at_sheet' in a else 'at_view'
        p0 = _pair(a.get(k, [0, 0]))
        if p0 is not None:
            for dx, dy in ((0, 1), (0, -1), (-1, 0), (1, 0)):
                v = [_r(p0[0] + dx * TEXT_STEP), _r(p0[1] + dy * TEXT_STEP)]
                out.append((f'{k} {_vec(v)}', put(**{k: v})))
    elif t in ('section_line', 'detail_circle', 'view_arrow'):     # only the identifying letter moves (label_shift), the line stays
        s0 = _pair(a.get('label_shift', [0, 0])) or [0.0, 0.0]
        for d in LABEL_SHIFTS:
            for dx, dy in LABEL_DIRS:
                v = [_r(s0[0] + dx * d / math.hypot(dx, dy)), _r(s0[1] + dy * d / math.hypot(dx, dy))]
                out.append((f'label_shift {_vec(v)}', put(label_shift=v)))
    if code not in SHIFT_FIRST:
        out += shifts
    return out


OUT_GAP = 1.5          # mm distance of the note text from the view envelope
OUT_LATERAL = (0.0, 12.0, -12.0)           # offset along the envelope (anchor line, then right/left of it)
OUT_STEPS = (0.0, 7.0, 14.0)               # further out in dimension line steps (side_offset): between and behind the dimension line stack
OUT_TRIES = 20         # at most this many positions per leader line (shortest first)
OUT_MAX = 80.0         # longer leader lines are not tried by the autofix


def _outward(a, rec, result):
    """Leader line 'to' with the text outside the view envelope (view_frames bbox_mm), three positions along the edge per side of the envelope,
    shortest leader line first (at most 15 mm outside, unless a collision requires more: the probe accepts only moves with
    fewer findings). The text stands at the knee (sheet.leader): to the right of it for to[0] >= 0, otherwise to the left, on the line above. [] without a
    record with a sheet tip (p_sheet) and the view envelope."""
    frame = ((result or {}).get('view_frames') or {}).get(a.get('view'))
    q = _pair((rec or {}).get('p_sheet'))
    if q is None or not frame or not a.get('text'):
        return []
    x0, y0, x1, y1 = frame['bbox_mm']
    h = float(a.get('h', 3.5))
    cands = []
    for lat in OUT_LATERAL:
        for d in OUT_STEPS:
            cands += [(q[0] + lat, y1 + OUT_GAP + d), (q[0] + lat, y0 - OUT_GAP - d - h - 1.0), (x1 + OUT_GAP + d, q[1] + lat), (x0 - OUT_GAP - d, q[1] + lat)]
    out = []
    for kx, ky in cands:
        v = [_r(kx - q[0]), _r(ky - q[1])]
        if math.hypot(*v) <= OUT_MAX and v not in out:
            out.append(v)
    return sorted(out, key=lambda v: math.hypot(*v))[:OUT_TRIES]


def _record(result, si, i):
    """Annotation record of the render for annotations + dims[i] on sheet si (or None)."""
    return next((r for r in (result or {}).get('annotations') or [] if r.get('sheet') == si and r.get('index') == i), None)


def _offset(spec, si, i, a, result):
    """Previous distance of a linear dimension: offset, otherwise the distance that drawing.Drawing.linear_offset chose
    in the render (view envelope, sheet points and stack counter as in the core); None if result is missing."""
    if 'offset' in a:
        return float(a['offset'])
    rec = _record(result, si, i)
    frame = ((result or {}).get('view_frames') or {}).get(a.get('view'))
    if not rec or 'p1_sheet' not in rec or not frame or frame.get('sheet') != si:
        return None
    axis = a.get('axis', 'aligned')
    stack = lambda b: (b.get('axis', 'aligned'), b.get('side') or SIDE_DEFAULT.get(b.get('axis', 'aligned'), 'left'))
    n = sum(1 for b in _anns(spec, si)[:i] if isinstance(b, dict) and b.get('type') == 'linear' and b.get('view') == a.get('view')
            and 'offset' not in b and stack(b) == stack(a))
    q1, q2 = np.asarray(rec['p1_sheet'], float), np.asarray(rec['p2_sheet'], float)
    u = {'h': np.array([1.0, 0.0]), 'v': np.array([0.0, 1.0])}.get(axis)
    v = {'bbox': list(frame['bbox_mm']), 'counters': {stack(a): n}}
    core = drawing.Drawing.__new__(drawing.Drawing)          # linear_offset needs only v
    return float(drawing.Drawing.linear_offset(core, a, v, q1, unit(q2 - q1) if u is None else u, axis))


def _label_moves(spec, si, key):
    t, name = _bend_table(spec, si, key)
    cur = _pair((t.get('label_to') or {}).get(name, LABEL_DEFAULT)) or LABEL_DEFAULT
    out = []
    for length in LABEL_LENGTHS:
        for ang in ANGLES:
            v = [_r(length * math.cos(math.radians(ang))), _r(length * math.sin(math.radians(ang)))]
            if np.allclose(v, cur, atol=1e-6):
                continue

            def move(s, v=v):
                tt, _ = _bend_table(s, si, key)
                tt['label_to'] = {**(tt.get('label_to') or {}), name: list(v)}
            out.append((f'label_to {name} {_vec(v)}', move))
    return out


def _view_moves(spec, si, vid, finding, result):
    vd = _view(spec, si, vid)
    out = []
    if finding.get('code') in TEXT_CODES and vd.get('label'):      # the title yields sideways, without moving the view
        d0 = float(vd.get('label_dx', 0.0))
        out += [(f'label_dx {_num(d0 + sg * d)}', (lambda s, v=_r(d0 + sg * d): _view(s, si, vid).__setitem__('label_dx', v)))
                for d in (6.0, 14.0, 24.0) for sg in (1, -1)]
    if 'at' not in vd and isinstance(vd.get('place'), dict):
        g0 = float(vd['place'].get('gap', GAP_DEFAULT))
        for n in range(1, GAP_TRIES + 1):
            g = _r(g0 + GAP_STEP * n)

            def move(s, g=g):
                _view(s, si, vid)['place']['gap'] = g
            out.append((f'gap {_num(g)}', move))
    elif _pair(vd.get('at')) is not None and result:
        d = _shift(spec, si, vid, finding, result)
        if d is not None:
            at = [_r(float(vd['at'][0]) + d[0]), _r(float(vd['at'][1]) + d[1])]

            def move(s):
                _view(s, si, vid)['at'] = list(at)
            out.append((f'at {_vec(at)}', move))
    return out


def _bbox(result, si, vid):
    f = (result.get('view_frames') or {}).get(vid)
    return f['bbox_mm'] if f and f.get('sheet') == si else None


def _hangs_on(spec, si, vid, anchor):
    """Does view vid depend on anchor (via place.relative_to, also indirectly)?"""
    seen = set()
    while vid not in seen:
        seen.add(vid)
        vd = _view(spec, si, vid)
        if vd is None or 'at' in vd or not isinstance(vd.get('place'), dict):
            return False
        vid = vd['place'].get('relative_to')
        if vid == anchor:
            return True
    return False


def _area(sd):
    fmt, orient = sd.get('format', 'A3'), sd.get('orientation', 'landscape')
    sh = Sheet(fmt if fmt in FORMATS else 'A3', orient if orient in ('landscape', 'portrait') else 'landscape', draw=False)
    area = sh.area
    sh.close()
    return area


def _shift(spec, si, vid, finding, result):
    """(dx, dy) of a view with at: away from the finding's partner by overlap + CLEAR along the axis with the
    smaller overlap; for out_of_area back into the drawing area (excess + CLEAR). None without a way out."""
    own = _bbox(result, si, vid)
    code = finding.get('code')
    if own is None:
        return None
    if code == 'out_of_area':
        L, B, R, T = _area(_sheet(spec, si))
        dx = L - own[0] + CLEAR if own[0] < L else R - own[2] - CLEAR if own[2] > R else 0.0
        dy = B - own[1] + CLEAR if own[1] < B else T - own[3] - CLEAR if own[3] > T else 0.0
        return (dx, dy) if dx or dy else None
    if code == 'view_overlap':
        other = next((v for v in finding.get('views') or [] if v != vid), None)
        if other is None or _hangs_on(spec, si, other, vid):
            return None                                  # the partner moved along
        part = _bbox(result, si, other)
    elif code == 'views_too_close':                     # move apart to the minimum distance + CLEAR/5
        other = next((v for v in finding.get('views') or [] if v != vid), None)
        part = _bbox(result, si, other) if other and not _hangs_on(spec, si, other, vid) else None
        if not part:
            return None
        gx = max(own[0] - part[2], part[0] - own[2])         # gap per axis (negative: overlaps in this axis)
        gy = max(own[1] - part[3], part[1] - own[3])
        need = checks.VIEW_GAP_MM + 1.0
        if gx >= gy:                                         # move away along the axis with the larger gap (they stand side by side there)
            return ((1.0 if own[0] + own[2] >= part[0] + part[2] else -1.0) * max(0.0, need - gx), 0.0)
        return (0.0, (1.0 if own[1] + own[3] >= part[1] + part[3] else -1.0) * max(0.0, need - gy))
    elif code == 'overlaps_block':
        sheet = next((s for s in result.get('sheets') or [] if s.get('index') == si), {})
        part = (sheet.get('blocks') or {}).get(finding.get('block'))
    else:
        return None
    if not part:
        return None
    ox = min(own[2], part[2]) - max(own[0], part[0])
    oy = min(own[3], part[3]) - max(own[1], part[1])
    if ox <= 0 or oy <= 0:
        return None
    if ox <= oy:
        return ((1.0 if own[0] + own[2] >= part[0] + part[2] else -1.0) * (ox + CLEAR), 0.0)
    return (0.0, (1.0 if own[1] + own[3] >= part[1] + part[3] else -1.0) * (oy + CLEAR))


def _notes_moves(spec, si):
    sd = _sheet(spec, si)
    notes = sd['notes']
    L, B, _, _ = _area(sd)
    if isinstance(notes, dict):                         # as in drawing.render
        h = float(notes.get('h', NOTE_H))
        p0 = _pair(notes.get('at', [L + 5, B + 5]))
    else:
        h = NOTE_H
        p0 = [L + 5, B + 5 + (len(notes) - 1) * 1.6 * h]
    out = []
    for n in range(1, NOTE_TRIES + 1) if p0 is not None else ():
        at = [_r(p0[0]), _r(p0[1] + n * 1.6 * h)]

        def move(s, at=at):
            sd_ = _sheet(s, si)
            nt = sd_['notes']
            sd_['notes'] = {**nt, 'at': list(at)} if isinstance(nt, dict) else {'lines': list(nt), 'at': list(at), 'h': NOTE_H}
        out.append((f'notes.at {_vec(at)}', move))
    return out


# ------------------------------------------------------------------ Loop

def _layout(result):
    return [f for f in result['findings'] if f['code'] in LAYOUT]


def _mine(findings, spec, key):
    """Findings from findings whose owner is key = (sheet, owner)."""
    return [f for f in findings if int(f.get('sheet') or 0) == key[0] and key[1] in owners_of(f, spec)]


def _lines(findings, key):
    """Number of findings in which key takes part with its line (field other)."""
    return sum(1 for f in findings if int(f.get('sheet') or 0) == key[0] and f.get('other') == key[1])


def _involved(finding, spec):
    """All participants (sheet, owner) of a finding: owner and line."""
    si = int(finding.get('sheet') or 0)
    return {(si, k) for k in owners_of(finding, spec) + ([finding['other']] if finding.get('other') else [])}


def _carried(spec, keys):
    """Participants (sheet, owner) that move along with the owners keys: these themselves and, for views, everything attached to
    them - views attached via place.relative_to (also indirectly), annotations with view (except texts with
    at_sheet) and bend table labels with labels_in (as 'label:<part>:*')."""
    out = set(keys)
    for si, k in keys:
        if not str(k).startswith('view:'):
            continue
        sd = _sheet(spec, si) or {}
        ids = [vd.get('id') for vd in sd.get('views') or [] if isinstance(vd, dict) and vd.get('id')]
        moved = {k[5:]} | {w for w in ids if _hangs_on(spec, si, w, k[5:])}
        out |= {(si, f'view:{w}') for w in moved}
        out |= {(si, f'ann:{i}') for i, a in enumerate(_anns(spec, si)) if isinstance(a, dict) and a.get('view') in moved
                and not (a.get('type') == 'text' and 'at_sheet' in a)}
        out |= {(si, f"label:{t['bend_table']}:*") for t in sd.get('tables') or []
                if isinstance(t, dict) and t.get('bend_table') and t.get('labels_in') in moved}
    return out


def _rides(key, carried):
    """Does participant key belong to carried (_carried)?"""
    si, k = key
    return key in carried or (str(k).startswith('label:') and (si, k.rpartition(':')[0] + ':*') in carried)


def _order(findings, spec):
    keys = []
    for f in findings:
        si = int(f.get('sheet') or 0)
        for k in owners_of(f, spec):
            if (si, k) not in keys and _movable(spec, si, k):
                keys.append((si, k))
    keys += [k for k in line_movers(findings, spec) if k not in keys]
    return [k for k in keys if not k[1].startswith('view:')] + [k for k in keys if k[1].startswith('view:')]


def _counts(findings, spec):
    """{owner: (own findings, findings as line)} of the owners in findings."""
    return {k: (len(_mine(findings, spec, k)), _lines(findings, k)) for k in _order(findings, spec)}


def _partners(findings, spec):
    """Pairs of movable owners (sheet, owner) that share a finding."""
    out = set()
    for f in findings:
        si = int(f.get('sheet') or 0)
        keys = [(si, k) for k in owners_of(f, spec) if _movable(spec, si, k)]
        out.update(frozenset((a, b)) for a in keys for b in keys if a != b)
    return out


def _adopt(result, src, out_dir):
    """Files of the render from src to out_dir; src is removed."""
    def move(p):
        q = os.path.join(out_dir, os.path.basename(p))
        os.replace(p, q)
        return q
    result['pdf'] = move(result['pdf'])
    for item in list(result.get('sheets') or []) + list(result.get('crops') or []):
        item['png'] = move(item['png'])
    shutil.rmtree(src, ignore_errors=True)


def repair(spec, max_rounds=60, budget_s=600):
    """Render result of the best state + autofix {rounds, ended_by, changes [{round, sheet, owner, code, move}] (the
    moves in this state), spec (input spec with its sheets), errors [{round, moves, error}] (renders/probes that
    threw; round 0 = cross-check of the initial state, None = final render or probe), fallback (None or
    {round, slim, full}: layout findings of probe and full render deviated, from there a full render per round), dropped (None
    or {blocking, layout, changes}: the full render of the best probe state ranked behind an earlier full render,
    that one is returned), probes {slim, full} (number of probes and full renders)}. rounds = round probes including probes of the cleaned state; ended_by =
    no_moves | max_rounds | budget. The budget is checked before each round (elapsed time + duration of the last
    probe, lean twice, because a probe of the cleaned state may still follow + reserve = duration of the
    initial render including B-rep loading for the final full render); without a budget limit the loop is
    deterministic. If the initial render itself throws, repair throws."""
    started = time.monotonic()
    out_dir = spec.get('out_dir') or os.path.join(os.path.dirname(spec['brep']), 'drawing')
    os.makedirs(out_dir, exist_ok=True)
    work = copy.deepcopy(spec)
    temps = []
    for k in ('cache_dir', 'feature_cache_dir'):
        if not work.get(k):
            work[k] = tempfile.mkdtemp(prefix='llcad-autofix-')
            temps.append(work[k])
    runs, seq, best, best_full, errors = [0], [0], [], [], []
    st = {'slim': True, 'offset': 0, 'last': 0.0, 'reserve': 0.0, 'n_slim': 0, 'n_full': 0, 'fallback': None, 'session': None,
          'dropped': None}

    def run(s, session=None):
        """Full render into its own folder -> (result, folder)."""
        d = os.path.join(out_dir, f'autofix-{runs[0]}')
        runs[0] += 1
        st['n_full'] += 1
        t = time.monotonic()
        try:
            return (drawing.render({**s, 'out_dir': d}) if session is None else drawing.render({**s, 'out_dir': d}, session)), d
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        finally:
            st['last'] = time.monotonic() - t

    def slim(s):
        """Lean probe in the session of the initial render; blocking adds the findings that the probe does not check
        (coverage, form features: position independent) from the initial render, so that probes and full renders
        are comparable."""
        st['n_slim'] += 1
        t = time.monotonic()
        try:
            res = drawing.layout_probe(s, st['session'])
        finally:
            st['last'] = time.monotonic() - t
        return {**res, 'blocking': res['blocking'] + st['offset']}

    def probe(s):
        """Probe of a round or of the cleaned state: lean, after a fallback full render. -> (result, folder|None)"""
        return (slim(s), None) if st['slim'] else run(s)

    def rendered(res, d, s, applied, back):
        """Best state by (blocking, layout findings, state before a probe with reverted moves, later before
        earlier), among all states (best) and among the full renders (best_full, only its folder remains)."""
        seq[0] += 1
        rank = (res['blocking'], len(_layout(res)), back, -seq[0])
        if d is not None:
            if not best_full or rank < best_full[0]:
                if best_full:
                    shutil.rmtree(best_full[2], ignore_errors=True)
                best_full[:] = [rank, res, d, s, list(applied)]
            else:
                shutil.rmtree(d, ignore_errors=True)
        if not best or rank < best[0]:
            best[:] = [rank, res, d, s, list(applied)]

    def failed(rnd, pick, e):
        errors.append({'round': rnd, 'moves': [{'sheet': c[1][0], 'owner': c[1][1], 'move': c[2]} for c in pick],
                       'error': f'{type(e).__name__}: {e}'[:300]})

    def fall_back(rnd, slim_res, full_res):
        """Probe and full render of the same state show different layout findings: from now on a full render per round."""
        st['fallback'] = {'round': rnd, 'slim': None if slim_res is None else len(_layout(slim_res)), 'full': len(_layout(full_res))}
        st['slim'], st['last'] = False, st['reserve']
        best[:] = list(best_full)                    # probe states no longer count

    def over_budget():
        return time.monotonic() - started + st['last'] * (2 if st['slim'] else 1) + st['reserve'] > budget_s
    try:
        t = time.monotonic()
        st['session'] = drawing.Drawing(work)        # views and parts_at for all probes (one process)
        ref, d = run(work, st['session'])
        st['reserve'] = time.monotonic() - t         # duration of a full render including B-rep loading (like the final one)
        rendered(ref, d, work, [], False)
        try:                                         # cross-check: the lean probe of the initial state = its render
            chk = slim(work)
        except Exception as e:
            failed(0, [], e)
            chk = None
        if chk is None or _layout(chk) != _layout(ref):
            fall_back(0, chk, ref)
        else:
            st['offset'] = ref['blocking'] - chk['blocking']
        ref_spec, fresh = work, _layout(ref)         # last probe, its spec, its valid layout findings
        base = _counts(fresh, work)                  # per owner (own findings, findings as line) in its position
        changes, tried, anchors, rounds, final_failed = [], {}, {}, 0, False
        while True:
            ended_by = 'max_rounds'
            while rounds < max_rounds:
                owners = _order(fresh, work)         # finding order, views last
                cands = []                           # (rank, owner, move name, move, finding code)
                for n, key in enumerate(owners):
                    mine = _mine(fresh, work, key) or [f for f in fresh if int(f.get('sheet') or 0) == key[0] and f.get('other') == key[1]]
                    if key not in anchors:
                        anchors[key] = (ref_spec, ref, mine[0])
                    a_spec, a_result, a_finding = anchors[key]
                    for name, move in moves(a_spec, {**a_finding, 'owner': key[1]}, tried.setdefault(key, set()), a_result):
                        trial = copy.deepcopy(work)
                        move(trial)
                        if trial != work:
                            cands.append((n, key, name, move, (mine or [a_finding])[0]['code']))
                            break
                        tried[key].add(name)         # changes nothing in the current position
                if not cands:                        # no move left: check the cleaned state (counts as a round)
                    if work == ref_spec or over_budget():
                        ended_by = 'no_moves' if work == ref_spec else 'budget'
                        break
                    if work == best[3]:
                        ref = best[1]                # already checked
                    else:
                        rounds += 1
                        try:
                            ref, d = probe(work)
                        except Exception as e:
                            failed(rounds, [], e)
                            final_failed, ended_by = True, 'no_moves'
                            break
                        rendered(ref, d, work, changes, False)
                    ref_spec, fresh = work, _layout(ref)
                    base = _counts(fresh, work)
                    continue
                pairs, pick = _partners(fresh, work), []
                is_line = lambda c: not _mine(fresh, work, c[1])      # takes part only as the line of a text finding
                texts = {c[1] for c in cands if not is_line(c)}
                for c in sorted(cands, key=lambda c: (is_line(c), len(tried[c[1]]), c[0])):
                    if is_line(c) and any(f.get('other') == c[1][1] and any((c[1][0], k) in texts for k in owners_of(f, work))
                                          for f in fresh if int(f.get('sheet') or 0) == c[1][0]):
                        continue                     # the finding's text still has a move: it moves first
                    if not any(frozenset((c[1], p[1])) in pairs for p in pick):
                        pick.append(c)               # partners of a finding move in turns, not simultaneously
                pick.sort(key=lambda c: c[0])
                if over_budget():
                    ended_by = 'budget'
                    break
                rounds += 1
                trial = copy.deepcopy(work)
                for _, key, name, move, _ in pick:
                    tried[key].add(name)
                    move(trial)
                try:
                    res, d = probe(trial)
                except Exception as e:               # failed attempt: moves of the round reverted and ticked off
                    failed(rounds, pick, e)
                    continue
                found = _layout(res)
                kept = [c for c in pick if (len(_mine(found, trial, c[1])) < base[c[1]][0] and _lines(found, c[1]) <= base[c[1]][1])
                        or (base[c[1]][0] == 0 and len(_mine(found, trial, c[1])) == 0 and _lines(found, c[1]) < base[c[1]][1])]
                back = [c[1] for c in pick if c not in kept]     # list in pick order (not a set: the order matters)

                def entry(c):
                    return {'round': rounds, 'sheet': c[1][0], 'owner': c[1][1], 'code': c[4], 'move': c[2]}
                rendered(res, d, trial, changes + [entry(c) for c in pick], bool(back))
                changes += [entry(c) for c in kept]
                nxt = trial
                if back:                             # revert; the next round checks the cleaned state as well
                    nxt = copy.deepcopy(work)
                    for c in kept:
                        c[3](nxt)
                # findings with a reverted owner (or something that moved along with a reverted view)
                # stem from its discarded position: they do not count, its findings from the state before
                # the round take their place (it is back there) - except those in which a kept owner or something that moves with a
                # kept view takes part (stale)
                gone, moved = _carried(work, back), _carried(work, [c[1] for c in kept])
                fresh = ([f for f in found if not any(_rides(x, gone) for x in _involved(f, trial))]
                         + [f for f in fresh if any(_rides(x, gone) for x in _involved(f, work))
                            and not any(_rides(x, moved) for x in _involved(f, work))])
                base = _counts(fresh, nxt)
                ref, ref_spec, work = res, trial, nxt
            if work != ref_spec and work != best[3] and not final_failed:     # cleaned state
                try:
                    res, d = probe(work)
                    rendered(res, d, work, changes, False)
                except Exception as e:
                    failed(None, [], e)
            if best[2] is not None:                  # the best state is fully rendered
                break
            _, b_res, _, b_spec, b_applied = best
            try:                                     # full render of the best state, the same layout findings as its probe?
                res, d = run(b_spec)
            except Exception as e:
                failed(None, [], e)
                break
            rendered(res, d, b_spec, b_applied, False)
            if _layout(res) == _layout(b_res):
                if best_full[3] is not b_spec:       # ranks behind an earlier full render: note it
                    st['dropped'] = {'blocking': res['blocking'], 'layout': len(_layout(res)), 'changes': len(b_applied)}
                break
            fall_back(rounds, b_res, res)            # rest of the run with full renders, from the rendered best state
            ref, ref_spec, work, changes, final_failed = res, b_spec, b_spec, list(b_applied), False
            fresh = _layout(res)
            base = _counts(fresh, work)
        _, res, d, s, applied = best_full
        fixed = copy.deepcopy(spec)
        fixed['sheets'] = copy.deepcopy(s['sheets'])
        _adopt(res, d, out_dir)
        return {**res, 'autofix': {'rounds': rounds, 'ended_by': ended_by, 'changes': applied, 'spec': fixed, 'errors': errors,
                                   'fallback': st['fallback'], 'dropped': st['dropped'],
                                   'probes': {'slim': st['n_slim'], 'full': st['n_full']}}}
    finally:
        for d in temps:
            shutil.rmtree(d, ignore_errors=True)
