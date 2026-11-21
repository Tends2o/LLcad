"""Sheet of a technical drawing: size and frame per DIN EN ISO 5457, lines per ISO 128,
dimensioning per ISO 129-1, lettering ISO 3098 type B (osifont), title block layout per DIN EN ISO 7200
(field arrangement after a company template), change block, parts list (DIN EN ISO 7573), tables.

Coordinates in mm on the sheet (origin bottom left). Every element is registered with its bounding box
(self.items) so that checks.py finds overlaps and area violations. Sheet(..., draw=False)
registers the same way but draws nothing (lean probe of the layout repair)."""
import functools
import os
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Polygon, Circle
from matplotlib.textpath import TextPath
from matplotlib import rcParams

rcParams['lines.scale_dashes'] = False
rcParams['pdf.fonttype'] = 42
FONT = FontProperties(fname=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts', 'osifont-lgpl3fe.ttf'))
PT = 72 / 25.4
CAP = 0.7373                      # cap height / font size for osifont
LW = {'thick': 0.5, 'thin': 0.25, 'frame': 0.7, 'xthin': 0.18}
DASH = {'hidden': (0, (3.0 * PT, 1.0 * PT)), 'center': (0, (12 * PT, 1.5 * PT, 1.0 * PT, 1.5 * PT))}
PARTS_COLS = [0, 10, 21, 72, 133, 190]       # parts list columns (item, quantity, designation, stock / standard, material)
FORMATS = {'A4': (210, 297), 'A3': (297, 420), 'A2': (420, 594), 'A1': (594, 841), 'A0': (841, 1189)}
FIT_MIN = 1.8                     # mm, smallest text height that field texts (title block, tables) are shrunk to
WRAP_MIN = 2.5                    # mm, smallest text height of wrapped lines (ISO 3098: minimum height 2.5 mm; FIT_MIN applies to single lines only)
LINE_PITCH = 1.45                 # line spacing of wrapped cell texts in text heights
FIELD_LABEL_GAP = 0.2             # mm, minimum distance between a title-block value and the label above it
CELL_SAFETY = 1.05                # Agg renders text with rounded glyph advances up to ~5 % wider than TextPath: reserve when fitting
ELLIPSIS = '...'                  # truncation marker (osifont has no U+2026)


def wrap_words(s, h, width):
    """Lines of s (wrapping at word boundaries only, greedy) at text height h, each at most width wide measured with CELL_SAFETY;
    a single word that does not fit stays alone on its line (callers then shrink or truncate)."""
    lines, cur = [], ''
    for w in str(s).split():
        t = (cur + ' ' + w) if cur else w
        if not cur or _text_extent(t, float(h))[0] * CELL_SAFETY <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or ['']


def _line_fits(line, h, width):
    return _text_extent(line, float(h))[0] * CELL_SAFETY <= width + 1e-9 if line else True


def _shorten(s, h, width):
    """Start of s with ELLIPSIS, at most width wide."""
    t = str(s)
    while t and not _line_fits(t.rstrip() + ELLIPSIS, h, width):
        t = t[:-1]
    return t.rstrip() + ELLIPSIS


def fit_text(s, h, width, height=None):
    """Fit text s into a cell of width (x height; None = free height, the line grows). Order:
    1. line wrap at word boundaries at text height h if the line height allows it (free height: always),
    2. then shrink the text in 0.1 mm steps (wrapped not below WRAP_MIN = 2.5 mm per ISO 3098, single line not
       below FIT_MIN), each step first as a single line, then wrapped,
    3. only then truncate with ELLIPSIS (at FIT_MIN, single line). The caller puts the full text into a finding (truncated).
    -> {'lines', 'h', 'truncated', 'need': required height in mm}."""
    s = '' if s is None else str(s)
    h = float(h)
    pitch = LINE_PITCH
    need = lambda n, hh: hh + (n - 1) * pitch * hh
    hh = h
    while True:
        if not s or _line_fits(s, hh, width):
            return {'lines': [s], 'h': hh, 'truncated': False, 'need': need(1, hh)}
        if hh >= WRAP_MIN - 1e-9:
            lines = wrap_words(s, hh, width)
            if all(_line_fits(l, hh, width) for l in lines) and (height is None or need(len(lines), hh) <= height + 1e-9):
                return {'lines': lines, 'h': hh, 'truncated': False, 'need': need(len(lines), hh)}
        if round(hh - 0.1, 3) < FIT_MIN - 1e-9:
            break
        hh = round(hh - 0.1, 3)
    return {'lines': [_shorten(s, hh, width)], 'h': hh, 'truncated': True, 'need': need(1, hh)}


def parts_list_rows(items, k=1.0, rh=6.0):
    """Row heights and fitted cells of the parts list (Sheet.parts_list and the planner: same computation). Columns as there;
    cells grow in height (fit_text with free height). -> [(height, {key: fit_text result})] per entry."""
    cols = PARTS_COLS
    out = []
    for it in items:
        cells = {}
        for (a, b_), key in zip(zip(cols[2:5], cols[3:6]), ('name', 'norm', 'material')):
            cells[key] = fit_text(str(it.get(key, '') or ''), 3.0 * k, (b_ - a - 2) * k)
        n = max(len(c['lines']) for c in cells.values())
        hh = min(c['h'] for c in cells.values() if len(c['lines']) == n)
        out.append((max(rh, 1.6 + (n - 1) * LINE_PITCH * hh + hh + 1.4) if n > 1 else rh, cells))
    return out


def parts_list_height(items, k=1.0, rh=6.0):
    """Total height of the parts list (header row + entries) in mm."""
    return rh + sum(h for h, _ in parts_list_rows(items, k, rh))


def fmt(v, nd=2, strip=True):
    """Number with decimal comma; ASCII minus (osifont has no U+2212)."""
    s = f'{v:.{nd}f}'
    if strip and '.' in s:
        s = s.rstrip('0').rstrip('.')
    if s in ('-0', ''):
        s = '0'
    return s.replace('.', ',')


def unit(v):
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def sheet_size(fmt_name, orientation):
    w, h = FORMATS[fmt_name]
    if orientation == 'landscape':
        w, h = h, w
    return float(w), float(h)


def usable_area(fmt_name, orientation, n_parts=1):
    """Free drawing area in mm²: inside the frame, without title block, change block (1 row) and parts list."""
    W, H = sheet_size(fmt_name, orientation)
    aw, ah = W - 30, H - 20
    band = 39 + 6 + (6 * (n_parts + 1) if n_parts > 1 else 0)
    return aw * ah - min(190.0, aw) * band


@functools.lru_cache(maxsize=8192)
def _text_extent(s, h):
    """(width, height, y0) of s at text height h (osifont); depends only on s and h."""
    # A dollar sign would be interpreted by matplotlib as math text; a character of equal width suffices for the width.
    e = TextPath((0, 0), s.replace(chr(36), 'S'), size=h / CAP, prop=FONT).get_extents()
    return e.width, e.height, e.y0


class _NoDraw:
    """Drawing surface of a sheet without matplotlib: every call does nothing."""
    def __getattr__(self, name):
        return lambda *a, **k: None


class Sheet:
    def __init__(self, fmt_name='A3', orientation='landscape', draw=True):
        self.format, self.orientation = fmt_name, orientation
        self.w, self.h = sheet_size(fmt_name, orientation)
        if draw:
            self.fig = plt.figure(figsize=(self.w / 25.4, self.h / 25.4))
            self.ax = self.fig.add_axes([0, 0, 1, 1])
            self.ax.set_xlim(0, self.w); self.ax.set_ylim(0, self.h)
            self.ax.set_aspect('equal'); self.ax.axis('off')
        else:                     # register only (findings), no image
            self.fig, self.ax = None, _NoDraw()
        self.items = []           # {'kind','bbox','owner','text'?}
        self.blocks = {}          # name -> bbox of the table/title block areas
        self.cells = []           # {'name','box','bbox','text'}: fitted cell texts (checks.cell_overflow_findings)
        self.truncated = []       # {'name','full','shown'}: shortened cell texts (full text for the finding)
        self.owner = None         # current annotation (for findings)
        self.letter_owners = set()   # owners of label letters (section line, detail circle, view arrow): checks.sheet_findings checks them with clearance
        L, R, B, T = 20.0, 10.0, 10.0, 10.0
        self.area = (L, B, self.w - R, self.h - T)

    # ------------------------------------------------------------------ Registration
    def _reg(self, kind, pts, **extra):
        pts = np.asarray(pts, float).reshape(-1, 2)
        if not len(pts):
            return
        item = {'kind': kind, 'bbox': [float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max())],
                'owner': self.owner}
        if kind == 'line':
            item['pts'] = pts.tolist()
        item.update(extra)
        self.items.append(item)

    # ------------------------------------------------------------------ Basic elements
    def line(self, pts, kind='thick', style=None, reg='line', z=2, role=None, **extra):
        """role (placement measurement codes): 'dim' dimension line, 'ext' extension line, 'arrow' arrow, 'leader' leader line; without role None.
        extra (e.g. offset) ends up in the registered element."""
        pts = np.asarray(pts, float)
        if len(pts) < 2:
            return
        ls = DASH.get(style, '-') if style else '-'
        self.ax.plot(pts[:, 0], pts[:, 1], color='k', lw=LW.get(kind, 0.25) * PT, linestyle=ls, solid_capstyle='round',
                     dash_capstyle='butt', solid_joinstyle='round', zorder=z)
        if reg:
            self._reg(reg, pts, role=role, **extra)

    def rect(self, x0, y0, x1, y1, kind='thin', reg=None):
        self.line([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], kind, reg=reg)

    def circle(self, c, r, kind='thin', fill=None, z=3, reg='line'):
        self.ax.add_patch(Circle(c, r, fill=fill is not None, facecolor=fill or 'none', edgecolor='k', lw=LW.get(kind, 0.25) * PT, zorder=z))
        if reg:
            t = np.linspace(0, 2 * np.pi, 33)          # circle as a closed polygon (crossing/text checks)
            self._reg(reg, np.c_[c[0] + r * np.cos(t), c[1] + r * np.sin(t)])

    def text_size(self, s, h):
        return _text_extent(s, float(h))

    def text(self, x, y, s, h=3.5, rot=0, ha='left', va='baseline', reg=True, z=5):
        if not s:
            return
        self.ax.text(x, y, s, fontproperties=FONT, fontsize=h * PT / CAP, rotation=rot, rotation_mode='anchor',
                     ha=ha, va=va, color='k', zorder=z, parse_math=False)
        if reg:
            self._reg('text', self.text_corners(x, y, s, h, rot, ha, va), text=s, height=h)

    def text_corners(self, x, y, s, h=3.5, rot=0, ha='left', va='baseline'):
        """Corners of the text envelope (sheet mm) as text() registers them (the angle arc checks its text position before drawing)."""
        w, hh, y0 = self.text_size(s, h)
        dx = {'left': 0, 'center': -w / 2, 'right': -w}[ha]
        dy = {'baseline': 0, 'bottom': -y0, 'center': -(hh / 2 + y0), 'top': -(hh + y0)}.get(va, 0)
        corners = np.array([[dx, dy + y0], [dx + w, dy + y0], [dx + w, dy + y0 + hh], [dx, dy + y0 + hh]])
        a = np.radians(rot); R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
        return corners @ R.T + [x, y]

    def text_width(self, s, h=3.5):
        return self.text_size(s, h)[0]

    def arrow(self, tip, direction, L=3.0, half_angle=15, role='arrow'):
        d = unit(direction); n = np.array([-d[1], d[0]])
        w = L * np.tan(np.radians(half_angle)); base = np.asarray(tip) - d * L
        self.ax.add_patch(Polygon([tip, base + n * w, base - n * w], closed=True, facecolor='k', edgecolor='k', lw=0.1, zorder=4))
        self._reg('line', [tip, base + n * w, base - n * w], role=role)

    # ------------------------------------------------------------------ Dimensioning (ISO 129-1)
    def dim(self, p1, p2, offset, text, u=None, h=3.5, over=2.0, arrows=(True, True), ext=(True, True), shift=0.0):
        """Linear dimension between the sheet points p1, p2; dimension line at distance offset (positive to the left of u). arrows/ext (half dimension line
        of a Ø on a cut cone): switch the arrowhead or extension line at each end on/off. shift (ISO 129-1: dimension value beside the centre):
        moves the dimension value by shift mm along the dimension line (in reading direction); autofix uses it to move it out of a collision."""
        p1 = np.asarray(p1, float); p2 = np.asarray(p2, float)
        u = unit(p2 - p1) if u is None else unit(u)
        n = np.array([-u[1], u[0]])
        a = p1 + n * offset
        b = a + u * ((p2 - p1) @ u)
        for p, q, on in [(p1, a, ext[0]), (p2, b, ext[1])]:
            dv = q - p
            if on and np.linalg.norm(dv) > 0.3:
                self.line([p, q + unit(dv) * over], 'thin', role='ext')
        L = np.linalg.norm(b - a)
        tw = self.text_width(text, h)
        outside = L < 2 * 3.0 + 1.0
        if not outside:
            self.line([a, b], 'thin', role='dim', offset=float(offset))
            if arrows[0]:
                self.arrow(a, a - b)
            if arrows[1]:
                self.arrow(b, b - a)
        else:
            e = unit(b - a)
            self.line([a - e * 7, b + e * 7], 'thin', role='dim', offset=float(offset))
            if arrows[0]:
                self.arrow(a, b - a)
            if arrows[1]:
                self.arrow(b, a - b)
        ang = np.degrees(np.arctan2(u[1], u[0]))
        if ang > 90.01:
            ang -= 180
        if ang <= -89.99:
            ang += 180
        ur = np.array([np.cos(np.radians(ang)), np.sin(np.radians(ang))]); nr = np.array([-ur[1], ur[0]])
        mid = (a + b) / 2
        if not outside and tw + 2 > L:
            mid = (b if (b - a) @ ur > 0 else a) + ur * (tw / 2 + 4)
        if outside:
            mid = (b if (b - a) @ ur > 0 else a) + ur * (tw / 2 + 8)
        pos = mid + ur * shift + nr * 1.0
        self.text(pos[0], pos[1], text, h, rot=ang, ha='center')
        return a, b

    def leader(self, p, knee, text, h=3.5, end='arrow', right=None):
        p = np.asarray(p, float); knee = np.asarray(knee, float)
        tw = self.text_width(text, h)
        right = (knee[0] >= p[0]) if right is None else right
        L = tw + 2
        end_pt = knee + np.array([L if right else -L, 0])
        self.line([p, knee, end_pt], 'thin', role='leader')
        if end == 'arrow':
            self.arrow(p, p - knee)
        elif end == 'dot':
            self.circle(p, 0.5, fill='k')
        tx = knee[0] + (1 if right else -1) * (L / 2)
        self.text(tx, knee[1] + 1.0, text, h, ha='center')

    def radius(self, center, r, ang_deg, text, h=3.5, outside=4.0):
        c = np.asarray(center, float)
        d = np.array([np.cos(np.radians(ang_deg)), np.sin(np.radians(ang_deg))])
        tw = self.text_width(text, h)
        self.line([c, c + d * (r + outside + tw)], 'thin', role='dim'); self.arrow(c + d * r, d)
        ang = ang_deg if -90 < ang_deg <= 90 else ang_deg - 180
        ur = np.array([np.cos(np.radians(ang)), np.sin(np.radians(ang))]); nr = np.array([-ur[1], ur[0]])
        pos = c + d * (r + 2.0 + (outside - 4.0) + tw / 2) + nr * 1.0        # outside > 4 (autofix text_out) pushes the text out 1:1, 4 as before
        self.text(pos[0], pos[1], text, h, rot=ang, ha='center')

    @staticmethod
    def angle_layout(vertex, d1, d2, radius, shift=0.0):
        """(arc points, center angle, text center, text rotation) of an angle dimension (angle_dim)."""
        v = np.asarray(vertex, float)
        a1 = np.arctan2(d1[1], d1[0]); a2 = np.arctan2(d2[1], d2[0])
        da = (a2 - a1) % (2 * np.pi)
        if da > np.pi:
            a1, da = a2, 2 * np.pi - da
        t = np.linspace(a1, a1 + da, 60)
        pts = v + radius * np.c_[np.cos(t), np.sin(t)]
        am = a1 + da / 2
        pos = v + (radius + 1.5) * np.array([np.cos(am), np.sin(am)]) + shift * np.array([-np.sin(am), np.cos(am)])      # shift: text beside the arc midpoint (label_layout.place)
        rot = np.degrees(am) - 90
        rot = rot + 180 if rot < -90 else rot - 180 if rot > 90 else rot
        return pts, am, pos, rot

    def angle_dim(self, vertex, d1, d2, radius, text, h=3.5, legs=False, shift=0.0):
        """legs (ISO 129-1): if the arc lies beyond a leg end point (vertex + d), a thin extension line is drawn from the end point in leg direction to slightly beyond the arc
        (checks.arc_leg_lines). The arc is registered with arc/legs/vertex (checks.angle_arc_findings)."""
        import checks
        pts, am, pos, rot = self.angle_layout(vertex, d1, d2, radius, shift)
        self.line(pts, 'thin', role='dim', arc=True, vertex=[float(x) for x in vertex], legs=[float(np.linalg.norm(d1)), float(np.linalg.norm(d2))])
        self.arrow(pts[0], pts[0] - pts[3]); self.arrow(pts[-1], pts[-1] - pts[-4])
        if legs:
            for g in checks.arc_leg_lines(vertex, d1, d2, radius):
                self.line(g, 'thin', role='ext', leg=True)
        self.text(pos[0], pos[1], text, h, rot=rot, ha='center', va='bottom')

    def balloon(self, p, c, num, r=4.5, h=5):
        p = np.asarray(p, float); c = np.asarray(c, float)
        d = unit(p - c)
        self.line([c + d * r, p], 'thin'); self.circle(p, 0.6, fill='k')
        self.circle(c, r, 'thin', z=4)
        self.text(c[0], c[1] - h / 2, str(num), h, ha='center')

    # ------------------------------------------------------------------ Frame (DIN EN ISO 5457)
    def frame(self):
        w, h = self.w, self.h
        L, B, R_, T_ = self.area
        self.rect(0, 0, w, h, 'xthin')
        self.rect(L, B, R_, T_, 'frame')
        cx, cy = w / 2, h / 2
        xs = sorted({cx + 50 * k for k in range(-30, 31) if L < cx + 50 * k < R_})
        ys = sorted({cy + 50 * k for k in range(-30, 31) if B < cy + 50 * k < T_})
        xb = [L] + xs + [R_]; yb = [B] + ys + [T_]
        for x in xs:
            for y0, y1 in [(B - 5, B), (T_, T_ + 5)]:
                self.line([[x, y0], [x, y1]], 'thin', reg=None)
        for y in ys:
            for x0, x1 in [(L - 5, L), (R_, R_ + 5)]:
                self.line([[x0, y], [x1, y]], 'thin', reg=None)
        self.rect(L - 5, B - 5, R_ + 5, T_ + 5, 'thin')
        for i in range(len(xb) - 1):
            xm = (xb[i] + xb[i + 1]) / 2
            for yy in (B - 4.2, T_ + 0.8):
                self.text(xm, yy, str(i + 1), 3.5, ha='center', reg=False)
        letters = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
        for j in range(len(yb) - 1):
            ym = (yb[-1 - j] + yb[-2 - j]) / 2
            for xx in (L - 2.5, R_ + 2.5):
                self.text(xx, ym - 1.75, letters[j], 3.5, ha='center', reg=False)
        for (x0, y0, x1, y1) in [(cx, 0, cx, B + 5), (cx, h, cx, T_ - 5), (0, cy, L + 5, cy), (w, cy, R_ - 5, cy)]:
            self.line([[x0, y0], [x1, y1]], 'frame', reg=None)

    def projection_symbol(self, x, y, s=1.0):
        """Projection method 1 (ISO 5456-2): truncated cone with the narrow side on the left, circles on the right."""
        d1, d2, L = 3.5 * s, 7 * s, 7 * s
        self.line([[x, y - d1 / 2], [x + L, y - d2 / 2], [x + L, y + d2 / 2], [x, y + d1 / 2], [x, y - d1 / 2]], 'thin', reg=None)
        self.line([[x - 1.5 * s, y], [x + L + 1.5 * s, y]], 'thin', 'center', reg=None)
        cxx = x + L + 7 * s
        self.circle((cxx, y), d2 / 2, 'thin', reg=None); self.circle((cxx, y), d1 / 2, 'thin', reg=None)
        self.line([[cxx - d2 / 2 - 1.5 * s, y], [cxx + d2 / 2 + 1.5 * s, y]], 'thin', 'center', reg=None)
        self.line([[cxx, y - d2 / 2 - 1.5 * s], [cxx, y + d2 / 2 + 1.5 * s]], 'thin', 'center', reg=None)

    def field(self, x, y, s, h, width, height=None, name='', box=None, ha='left', label=None):
        """Fit cell text (fit_text: wrap, then shrink, then truncate) and draw it; y = baseline of the last line
        (wrapped lines stand above it), x left edge (ha 'center': middle), width/height space of the cell; box = cell rectangle
        for the overflow check. label = (x, y, text, h) of the field label: all lines stay below its text envelope (the one that
        checks.text_overlap uses, text_corners) - the available height shrinks until the top line clears it. -> fitted text height."""
        r = fit_text(s, h, width, height)
        if label is not None:
            ceiling = float(self.text_corners(*label)[:, 1].min()) - FIELD_LABEL_GAP
            hgt = height if height is not None else r['need'] + 1.0
            while True:
                n, hh = len(r['lines']), r['h']
                top = max(y + (n - 1 - i) * LINE_PITCH * hh + (lambda e: e[2] + e[1])(self.text_size(line, hh)) for i, line in enumerate(r['lines']) if line) if s else y
                if top <= ceiling + 1e-9 or hgt <= 0.3:
                    break
                hgt -= 0.1
                r = fit_text(s, h, width, hgt)
        hh, n = r['h'], len(r['lines'])
        for i, line in enumerate(r['lines']):
            yy = y + (n - 1 - i) * LINE_PITCH * hh
            self.text(x, yy, line, hh, ha=ha)
            if line and box is not None and self.items and self.items[-1].get('text') == line:
                self.cells.append({'name': name or self.owner, 'box': [float(v) for v in box], 'bbox': list(self.items[-1]['bbox']), 'text': line})
        if r['truncated']:
            self.truncated.append({'name': name or self.owner, 'full': str(s), 'shown': r['lines'][0]})
        return hh

    # ------------------------------------------------------------------ Title block (layout per DIN EN ISO 7200, company template)
    def title_block(self, d, company=('', '', '')):
        L, B, R_, T_ = self.area
        W = min(190.0, R_ - L)
        k = W / 190.0
        x0 = R_ - W
        X = lambda v: x0 + v * k
        yb = B
        hb, hm, ht = 6.0, 17.0, 16.0
        y1, y2, y3 = yb + hb, yb + hb + hm, yb + hb + hm + ht
        lab = 1.8
        self.owner = 'title_block'
        self.rect(x0, yb, R_, y3, 'thick')
        for y in (y1, y2):
            self.line([[x0, y], [R_, y]], 'thick', reg=None)
        cols = [0, 37, 65.8, 107.7, 142.3, 190]
        for c in cols[1:-1]:
            self.line([[X(c), yb], [X(c), y1]], 'thin', reg=None)
        vals = [('Creation date', d.get('created_date', '')), ('Created by', d.get('created_by', '')), ('Approval date', d.get('approval_date', '')),
                ('Approved by', d.get('approved_by', '')), ('Replaces', d.get('replaces', ''))]
        for a, b_, (lbl, v) in zip(cols[:-1], cols[1:], vals):
            s = f'{lbl} {v}'.strip()
            self.field(X(a) + 1, yb + 1.6, s, 2.5 * min(1, k + 0.05), (b_ - a - 2) * k, y1 - yb - 1.9, f'Title block {lbl}', [X(a), yb, X(b_), y1])
        ym = y1 + hm / 2
        for c in (37, 134.0, 179.9):
            self.line([[X(c), y1], [X(c), y2]], 'thin', reg=None)
        self.line([[X(0), ym], [X(134.0), ym]], 'thin', reg=None)
        self.line([[X(179.9), ym], [X(190), ym]], 'thin', reg=None)
        self.text(X(1), ym + hm / 2 - 2.2, 'General tolerance', lab)
        self.field(X(1), ym + 1.2, d.get('general_tolerance', ''), 3.5 * k, 35 * k, hm / 2 - 3.5, 'General tolerance', [X(0), ym, X(37), y2], label=(X(1), ym + hm / 2 - 2.2, 'General tolerance', lab))
        self.text(X(1), y1 + hm / 2 - 2.2, 'Tolerancing', lab)
        self.field(X(1), y1 + 1.2, d.get('tolerancing', ''), 3.5 * k, 35 * k, hm / 2 - 3.5, 'Tolerancing', [X(0), y1, X(37), ym], label=(X(1), y1 + hm / 2 - 2.2, 'Tolerancing', lab))
        self.text(X(38), ym + hm / 2 - 2.2, 'Title', lab)
        self.field(X(38), ym + 1.6, d.get('title', ''), 4.5, 94 * k, hm / 2 - 3.3, 'Title', [X(37), ym, X(134), y2], label=(X(38), ym + hm / 2 - 2.2, 'Title', lab))
        self.text(X(38), y1 + hm / 2 - 2.2, 'Additional title', lab)
        self.field(X(38), y1 + 1.6, d.get('supplementary_title', ''), 4.5, 94 * k, hm / 2 - 3.3, 'Additional title', [X(37), y1, X(134), ym], label=(X(38), y1 + hm / 2 - 2.2, 'Additional title', lab))
        self.text(X(135), y2 - 2.2, 'Drawing no.', lab)
        self.field(X(135), y1 + 4.0, d.get('drawing_number', ''), 5.0, 43 * k, hm - 4.0 - 2.4, 'Drawing no.', [X(134), y1, X(179.9), y2], label=(X(135), y2 - 2.2, 'Drawing no.', lab))
        self.text(X(180.6), y2 - 2.2, 'Revision', 1.6)
        self.field(X(185), ym + 1.2, d.get('revision', ''), 5 * k, 9.1 * k, hm / 2 - 3.5, 'Revision', [X(179.9), ym, X(190), y2], ha='center')
        self.text(X(180.6), ym - 2.2, 'Sheet', 1.6)
        self.field(X(185), y1 + 1.2, d.get('sheet', ''), 4.5 * k, 9.1 * k, hm / 2 - 3.5, 'Sheet', [X(179.9), y1, X(190), ym], ha='center')
        for c in (16.3, 37.0, 111.7, 151.1):
            self.line([[X(c), y2], [X(c), y3]], 'thin', reg=None)
        self.text(X(1), y3 - 2.2, 'Scale', lab)
        self.field(X(8.15), y2 + 3.5, d.get('scale', ''), 7.0, 15 * k, ht - 3.5 - 2.4, 'Scale', [X(0), y2, X(16.3), y3], ha='center')
        ys = y2 + 4.6
        self.line([[X(16.3), ys], [X(37.0), ys]], 'thin', reg=None)
        self.line([[X(31.7), y2], [X(31.7), ys]], 'thin', reg=None)
        self.text(X(16.8), y2 + 1.3, 'Units', 1.5)
        self.text(X(31.2), y2 + 1.2, 'mm', 2.2, ha='right')
        self.text(X(34.35), y2 + 1.2, self.format, 2.5, ha='center')
        self.projection_symbol(X(19.0), ys + (y3 - ys) / 2, 0.85 * k)
        self.text(X(38), y3 - 2.2, 'Material', lab)
        self.field(X(38), y3 - 7.5, d.get('material', ''), 3.5, 72 * k, 5.0, 'Material', [X(37.0), y2, X(111.7), y3], label=(X(38), y3 - 2.2, 'Material', lab))
        yo = y2 + 5.3
        self.line([[X(111.7), yo], [X(151.1), yo]], 'thin', reg=None)
        self.text(X(112.4), y3 - 2.2, 'Surface finish', lab)
        self.field(X(112.4), y3 - 7.0, d.get('surface', ''), 3.5, 37 * k, 4.5, 'Surface finish', [X(111.7), yo, X(151.1), y3], label=(X(112.4), y3 - 2.2, 'Surface finish', lab))
        self.text(X(112.4), yo - 1.8, 'Document type', lab)
        self.field(X(150.4), y2 + 0.7, d.get('document_type', ''), 2.2, 37 * k, 3.0, 'Document type', [X(111.7), y2, X(151.1), yo], ha='right')
        c1, c2, c3 = (list(company) + ['', '', ''])[:3]
        hf = min(fit_text(c1, 3.6, 36 * k)['h'], fit_text(c2, 3.6, 36 * k)['h'])
        self.field(X(152.5), y3 - 1.5 - hf, c1, hf, 36 * k, hf, 'Company', [X(151.1), y2, X(190), y3])
        self.field(X(152.5), y3 - 2.8 - 2 * hf, c2, hf, 36 * k, hf, 'Company', [X(151.1), y2, X(190), y3])
        self.field(X(152.5), y2 + 1.3, c3, 2.5, 36 * k, 3.0, 'Company', [X(151.1), y2, X(190), y3])
        self.owner = None
        self.blocks['title_block'] = [x0, yb, R_, y3]
        return y3, x0, W

    def change_block(self, rows, yb, n=None, rh=6.0):
        L, B, R_, T_ = self.area
        W = min(190.0, R_ - L); k = W / 190.0; x0 = R_ - W
        X = lambda v: x0 + v * k
        n = max(n or 0, len(rows), 1)
        yt = yb + n * rh
        self.owner = 'change_block'
        self.rect(x0, yb, R_, yt, 'thick')
        for i in range(1, n):
            self.line([[X(7.0), yb + i * rh], [R_, yb + i * rh]], 'thin', reg=None)
        for c in (7.0, 16.6, 34.4, 45.5):
            self.line([[X(c), yb], [X(c), yt]], 'thin', reg=None)
        if n * rh >= 18:
            self.text(X(5.0), yb + 1.5, 'Revision', 3.5, rot=90)
        else:
            self.text(X(4.6), yb + 0.8, 'Rev.', 2.0, rot=90)
        for i, r in enumerate(rows):
            y = yb + i * rh + 1.6
            r = (list(r) + ['', '', '', ''])[:4]
            self.text(X(11.8), y, r[0], 3.5 * k, ha='center')
            self.field(X(25.5), y, r[1], 3.5 * k, 16 * k, rh - 2.0, 'Change block', [X(16.6), yb + i * rh, X(34.4), yb + (i + 1) * rh], ha='center')
            self.text(X(40.0), y, r[2], 3.5 * k, ha='center')
            self.field(X(46.5), y, r[3], 3.5 * k, 140 * k, rh - 2.0, 'Change block', [X(45.5), yb + i * rh, R_, yb + (i + 1) * rh])
        self.owner = None
        self.blocks['change_block'] = [x0, yb, R_, yt]
        return yt

    def table(self, x0, y_top, columns, rows, title=None, subtitle=None, rh=5.0, h=3.0, name='table', head_h=2.5):
        """General table with header row; columns [{'title','width'}]; top left corner (x0, y_top)."""
        W = sum(c['width'] for c in columns)
        n = len(rows) + 1
        yb = y_top - n * rh
        self.owner = name
        y_tt = y_top
        if subtitle:
            self.field(x0, y_tt + 2.0, subtitle, 2.5, W - 0.5, 2.6, name + ' Subtitle', [x0, y_tt + 1.0, x0 + W, y_tt + 5.0]); y_tt += 4.0
        if title:
            self.field(x0, y_tt + 2.0, title, 3.0 if not subtitle else 2.5, W - 0.5, 3.1, name + ' Title', [x0, y_tt + 1.0, x0 + W, y_tt + 6.0])
        self.rect(x0, yb, x0 + W, y_top, 'thick')
        self.line([[x0, y_top - rh], [x0 + W, y_top - rh]], 'thick', reg=None)
        for i in range(1, n - 1):
            yy = y_top - rh - i * rh
            self.line([[x0, yy], [x0 + W, yy]], 'thin', reg=None)
        xs = np.cumsum([0] + [c['width'] for c in columns])
        for c in xs[1:-1]:
            self.line([[x0 + c, yb], [x0 + c, y_top]], 'thin', reg=None)
        for (c0, c1), col in zip(zip(xs[:-1], xs[1:]), columns):
            self.field(x0 + (c0 + c1) / 2, y_top - rh + 1.4, col['title'], head_h, c1 - c0 - 2, rh - 2.0, name + ' header',
                       [x0 + c0, y_top - rh, x0 + c1, y_top], ha='center')
        for i, row in enumerate(rows):
            yy = y_top - rh - (i + 1) * rh + 1.4
            for (c0, c1), col, val in zip(zip(xs[:-1], xs[1:]), columns, row):
                val = '' if val is None else str(val)
                cell = [x0 + c0, y_top - rh - (i + 1) * rh, x0 + c1, y_top - rh - i * rh]
                if col.get('align', 'center') == 'left':
                    self.field(x0 + c0 + 1, yy, val, h, c1 - c0 - 2, rh - 2.0, name, cell)
                else:
                    self.field(x0 + (c0 + c1) / 2, yy, val, h, c1 - c0 - 2, rh - 2.0, name, cell, ha='center')
        self.owner = None
        top = y_tt + (5.0 if title else 0)
        self.blocks[name] = [x0, yb, x0 + W, top]
        return yb, top

    def parts_list(self, items, yb, rh=6.0):
        """Parts list, header row at the bottom (adjoining the change block). Cell texts are wrapped (the row grows), only then
        shrunk, finally truncated (fit_text, parts_list_rows); columns PARTS_COLS."""
        L, B, R_, T_ = self.area
        W = min(190.0, R_ - L); k = W / 190.0; x0 = R_ - W
        X = lambda v: x0 + v * k
        cols, heads = PARTS_COLS, ['Item', 'Qty', 'Designation', 'Stock / standard', 'Material']
        rows = parts_list_rows(items, k, rh)
        yt = yb + rh + sum(h for h, _ in rows)
        self.owner = 'parts_list'
        self.rect(x0, yb, R_, yt, 'thick')
        self.line([[x0, yb + rh], [R_, yb + rh]], 'thick', reg=None)
        edges = [yb + rh]
        for h, _ in rows:
            edges.append(edges[-1] + h)
        for e in edges[1:-1]:
            self.line([[x0, e], [R_, e]], 'thin', reg=None)
        for c in cols[1:-1]:
            self.line([[X(c), yb], [X(c), yt]], 'thin', reg=None)
        for a, hd in zip(cols[:-1], heads):
            self.text(X(a) + 1, yb + 1.8, hd, 2.5)
        for i, (it, (h, cells)) in enumerate(zip(items, rows)):
            y = edges[i] + 1.6
            self.text(X(5), y, str(it.get('pos', '')), 3.5 * k, ha='center')
            self.text(X(15.5), y, str(it.get('qty', '')), 3.5 * k, ha='center')
            for (a, b_), key in zip(zip(cols[2:5], cols[3:6]), ('name', 'norm', 'material')):
                r = cells[key]
                for j, line in enumerate(r['lines']):
                    self.text(X(a) + 1, y + (len(r['lines']) - 1 - j) * LINE_PITCH * r['h'], line, r['h'])
                    if line:
                        self.cells.append({'name': f'Parts list {key}', 'box': [X(a), edges[i], X(b_), edges[i + 1]],
                                           'bbox': list(self.items[-1]['bbox']), 'text': line})
                if r['truncated']:
                    self.truncated.append({'name': f'Parts list {key}', 'full': str(it.get(key, '') or ''), 'shown': r['lines'][0]})
        self.owner = None
        self.blocks['parts_list'] = [x0, yb, R_, yt]
        return yt

    def save(self, pdf_path=None, png_path=None, dpi=110):
        if pdf_path:
            self.fig.savefig(pdf_path)
        if png_path:
            self.fig.savefig(png_path, dpi=dpi)

    def close(self):
        if self.fig is not None:
            plt.close(self.fig)
