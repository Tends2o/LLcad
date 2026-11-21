"""Drawing core of LLcad: technical drawings (DIN/ISO) from the B-Rep of a revision.

CLI:  python drawing.py analyze|render|crop|plan|autofix <spec.json>   → JSON on stdout
API:  analyze(spec), render(spec); plan: plan.plan_spec(spec) (drawing candidates); autofix: autofix.repair(spec)
      (render with layout repair); layout_probe(spec, session) (lean probe of the repair: sheet findings only)

Dimension values are computed from 3D points of the geometry (projection into the view plane); texts are
only prefix/suffix/tolerance or an explicit override ('text')."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from shapely.geometry import LineString, Point, Polygon as SPolygon, box as sbox
from shapely.strtree import STRtree
import projection as prj
import analyze as ana
import checks
import cache
import features as ft
from sheet import Sheet, fmt, unit as u2

SURE_IN, SURE_OUT = 0.98, 1.02   # shares of tol: this close a point is certainly inside the shapely buffer, this far certainly outside
LOCAL = 3.0                      # in between: buffer only from the lines within LOCAL * tol of the point
HALF_OVER = 4.0                  # mm on paper: a half Ø dimension line ends this far beyond the axis
HALF_AXIS = 8.0                  # mm: half length of the axis centre line there
THREAD_CREST = 1.4                # crest radius of a modelled thread at most THREAD_CREST × core radius (hidden_reference: thread core)
FOOT_AXIS_MM = 0.05               # mm (model): axis distance and axis range when assigning the crest cylinder (thread_crest)
FOOT_MM = 0.3                    # mm on paper: the reference point of a dimension lies on a line (foot points, hidden edges)
RING_N, RING_MM = 72, 0.1        # reference circle of a Ø/R/thread in circle view: probe points, distance to the line on paper
RING_VIS, RING_HID = 0.1, 0.2    # share of probe points: drawn visible from RING_VIS; below that and from RING_HID hidden


class Zone:
    """Point-in-buffer test with the result of unary_union(lines).buffer(tol).contains_xy, without building the buffer of all
    lines (seconds per view for large parts). The shapely/GEOS buffer (round ends and corners, 16 segments per
    quarter circle, input simplified with tol/100) deviates from the distance region tol by less than 2 % of tol: a point
    at a distance <= SURE_IN * tol from the lines is surely inside, one at a distance > SURE_OUT * tol surely outside.
    For points in between, the same buffer decides, built from the lines within LOCAL * tol: lines farther away
    have their buffer more than 2 tol from the point and change nothing there. self.band counts these points."""

    def __init__(self, lines, tol):
        import shapely
        self.lines, self.tol = list(lines), float(tol)
        self.tree = shapely.STRtree(self.lines) if self.lines else None
        self.band = 0

    def contains_xy(self, x, y):
        import shapely
        from shapely.ops import unary_union
        x, y = np.asarray(x, float).ravel(), np.asarray(y, float).ravel()
        if self.tree is None:
            return np.zeros(len(x), bool)
        pts = shapely.points(x, y)
        d = np.full(len(x), np.inf)
        idx, dist = self.tree.query_nearest(pts, max_distance=SURE_OUT * self.tol, return_distance=True, all_matches=False)
        d[idx[0]] = dist
        out = d <= SURE_IN * self.tol
        for i in np.nonzero(~out & (d <= SURE_OUT * self.tol))[0]:
            near = np.sort(self.tree.query(pts[i], predicate='dwithin', distance=LOCAL * self.tol))
            out[i] = shapely.contains_xy(unary_union([self.lines[j] for j in near]).buffer(self.tol), x[i], y[i])
            self.band += 1
        return out


def analyze(spec):
    return ana.analyze_spec(spec)


class AnnotationError(Exception):
    pass


ARROW_MM = 3.0           # arrow length (sheet.Sheet.arrow)
CIRCLE_PAD_MM = 6.0      # clearance between text and arrows of a dimension line in a circle (text in a circle would otherwise touch the circle edge)


def circle_fits(text_w, rp):
    """Do the dimension value (width text_w) and two arrows fit into a circle with the paper radius rp (dimension line through the centre)?"""
    return 2 * rp >= text_w + 2 * ARROW_MM + CIRCLE_PAD_MM


class Drawing:
    def __init__(self, spec):
        self.spec = spec
        self.parts, self.shape = ana.part_shapes(spec)
        self.pos_of = {}
        for s in spec.get('sheets') or []:
            for row in s.get('parts_list') or []:
                if row.get('part'):
                    self.pos_of[row['part']] = str(row.get('pos', ''))
        self._cyl, self._tor, self._bend, self._feat = {}, {}, {}, {}
        self._sym = {}                      # symmetry planes per part (measurement N2)
        self._at, self._content = {}, {}    # parts_at per point; view content per view without position fields (view_content)
        self._refs = {}                     # line zones of the reference point check per view content (reference_state)
        self.slim = False                   # lean probe (layout_probe): without foot point check of the dimensions
        self.cache_stats = {'hits': 0, 'misses': 0}
        self.feature_stats = {'hits': 0, 'misses': 0}

    def planes_of(self, pid, mid):
        """{world axis: coordinate of the symmetry plane} of the whole part pid (symmetry.body_planes, also large parts), once per part per Drawing. Measurement (symmetry_planes) and
        generation (symmetry.add_axes) read the same answer."""
        import symmetry
        if pid not in self._sym:
            mid = np.asarray(mid, float)
            self._sym[pid] = {k: float(mid[k]) for k in symmetry.body_planes(self.shape[pid], mid)}
        return self._sym[pid]

    def symmetry_planes(self, fbp):
        """(Measurement N2): {part: {world axis: coordinate of the symmetry plane}} of the manufactured parts, the same detection as the generation (planes_of)."""
        out = {}
        for pid, pf in fbp.items():
            bb = pf.get('bbox') if isinstance(pf, dict) else None
            if bb is None or pid not in self.shape:
                continue
            out[pid] = self.planes_of(pid, (np.asarray(bb['min'], float) + np.asarray(bb['max'], float)) / 2)
        return out

    # ------------------------------------------------------------------ Geometry queries (cached)
    def cyl(self, pid):
        if pid not in self._cyl:
            self._cyl[pid] = ana.cylinders(self.shape[pid])
        return self._cyl[pid]

    def tor(self, pid):
        if pid not in self._tor:
            self._tor[pid] = ana.tori(self.shape[pid])
        return self._tor[pid]

    def bend(self, pid):
        if pid not in self._bend:
            self._bend[pid] = ana.bend_table(self.shape[pid])
        return self._bend[pid]

    def features(self, pid):
        """Form features for the coverage check: part_features + patterns + match_hints with the IR hints
        of the part (spec['ir_hints'], supplied by the service, as in the planner), freeform, bounding box; via the
        feature cache spec['feature_cache_dir'] (the same as in the planner, the hints are part of the key)."""
        if pid not in self._feat:
            fc = self.spec.get('feature_cache_dir')
            part = next(p for p in self.parts if p['id'] == pid)
            hints = [h for h in self.spec.get('ir_hints') or [] if isinstance(h, dict) and h.get('part') == pid]
            names = checks.spec_thread_designations(self.spec, pid)
            if names is not None:      # thread note without designation - filled like in the planner from the designations it passes along in the sheet
                hints = ft.named_thread_hints(hints, names)[0]
            r, hit = ft.features_of(self.shape[pid], pid, fc, self.spec.get('brep'), ana.part_selection(self.spec, part), hints)
            if fc:
                self.feature_stats['hits' if hit else 'misses'] += 1
            self._feat[pid] = {'features': r['features'] + r['patterns'] + r['hints'], 'freeform': r['freeform'], 'regions': r.get('regions') or [], 'profiles': r.get('profiles') or [],
                               'bbox': ana.bbox(self.shape[pid]), **({'profiles_error': r['profiles_error']} if r.get('profiles_error') else {})}      # render reports coverage_error
        return self._feat[pid]

    def balloon_foreign(self, v, pid, p3):
        """Foreign part that the view in v hits first around the tip p3 of an item number at distance BALLOON_CLEAR_MM (paper), otherwise None
        (the tip lies inside its own part, never at a foreign one)."""
        ids = [i for i in v['parts'] if i in self.shape]
        if pid not in ids:
            return None
        if not hasattr(self, '_ixs'):
            self._ixs, self._far, self._bbs = ft.LazyRays(self.shape), 0.0, {}
        for i in ids:
            if i not in self._bbs:
                bb = self._bbs[i] = ana.bbox(self.shape[i])           # once per part (AddOptimal is expensive)
                self._far = max(self._far, 2.0 * float(np.linalg.norm(bb['size'])) + 10.0)
        bx = {i: (self._bbs[i]['min'], self._bbs[i]['max']) for i in ids}
        c, foreign = ft.view_clearance(ft.LazyView(self._ixs, ids), pid, p3, v['frame'], ft.balloon_rings(float(v['scale'])), self._far, boxes=bx)
        return foreign

    def parts_at(self, p, tol=0.02):
        """Parts that touch the 3D point p (up to tol); computed once per point."""
        key = (tuple(float(x) for x in p), float(tol))
        if key not in self._at:
            from OCP.BRepExtrema import BRepExtrema_DistShapeShape
            from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
            from OCP.gp import gp_Pnt
            v = BRepBuilderAPI_MakeVertex(gp_Pnt(*key[0])).Vertex()
            out = []
            for pid, s in self.shape.items():
                d = BRepExtrema_DistShapeShape(v, s)
                if d.IsDone() and d.Value() <= tol:
                    out.append(pid)
            self._at[key] = out
        return list(self._at[key])

    # ------------------------------------------------------------------ Views
    def selection(self, pids):
        """Which B-Rep children are used per part (part of the cache key identity)."""
        by_id = {p['id']: p for p in self.parts}
        idx = {pid: [int(i) for i in (by_id[pid].get('solid_indices') or [by_id[pid].get('solid_index', 0)])] if pid in by_id else None for pid in pids}
        return {'single_root': bool(self.spec.get('single_root')), 'solids': idx}

    def raw_view(self, vd, pids, frame, hidden, mode=None):
        """(vis, hid_raw, sec_faces) of the parts pids in image coordinates, with section, via the HLR cache."""
        cache_dir = self.spec.get('cache_dir')
        sec_pt = vd['section']['point'] if vd.get('section') else None
        key = got = None
        if cache_dir:
            key = cache.view_key(cache.brep_digest(self.spec['brep']), pids, frame, sec_pt, hidden, mode or vd.get('hlr', 'auto'), self.selection(pids))
            got = cache.load(cache_dir, key)
        if got is not None:
            self.cache_stats['hits'] += 1
            return got
        shapes, sec_faces = [self.shape[pid] for pid in pids], []
        if sec_pt is not None:
            shapes, sec_faces = prj.section(shapes, frame, sec_pt)
        vis, hid = prj.hlr(shapes, frame, hidden=hidden, mode=mode or vd.get('hlr', 'auto'))
        if cache_dir:
            self.cache_stats['misses'] += 1
            cache.store(cache_dir, key, vis, hid, sec_faces)
        return vis, hid, sec_faces

    def thin_edges(self, vd, pids, thin, frame, vis, mode, tol=0.01):
        """Indices of the visible edges vis that belong to the parts thin: on an edge of the view of only the thin parts
        and not entirely on one of the other parts (coincident edges stay thick)."""
        def zone(sub):
            lines = [LineString(p) for p in self.raw_view(vd, sub, frame, False, mode)[0] if len(p) > 1] if sub else []
            return Zone(lines, tol) if lines else None

        def on(buf, pts):
            if buf is None:
                return False
            a = np.asarray(pts, float)
            q = np.vstack([a, (a[:-1] + a[1:]) / 2]) if len(a) > 1 else a
            return bool(buf.contains_xy(q[:, 0], q[:, 1]).all())
        zt, zr = zone(thin), zone([p for p in pids if p not in thin])
        return {i for i, pts in enumerate(vis) if on(zt, pts) and not on(zr, pts)}

    def view_content(self, vd, sheet_scale):
        """Content of a view in image coordinates, independent of its position on the sheet (at, place): edges (HLR,
        section; via the HLR cache), cleaned hidden edges, centre lines, detail circle, thin edges,
        section faces. Built once per Drawing per view definition without position fields; the layout repair keeps a
        Drawing across its probes (moves change only at and place.gap of views). Read only."""
        key = self.content_key(vd, sheet_scale)
        if key not in self._content:
            self._content[key] = self._view_content(vd, sheet_scale)
        return self._content[key]

    @staticmethod
    def content_key(vd, sheet_scale):
        return json.dumps([{k: v for k, v in vd.items() if k not in ('at', 'place')}, sheet_scale], sort_keys=True, default=str)

    def _view_content(self, vd, sheet_scale):
        vid = vd['id']
        pids = vd.get('parts') or [p['id'] for p in self.parts]
        for pid in pids:
            if pid not in self.shape:
                raise AnnotationError(f'View {vid}: unknown part {pid}')
        if vd.get('preset') and vd['preset'] not in prj.PRESETS:
            raise AnnotationError(f"View {vid}: unknown preset {vd['preset']!r} (allowed: {', '.join(prj.PRESETS)})")
        frame = prj.frame_of(vd)
        k = ana.scale_value(vd.get('scale', sheet_scale))
        sec_pt = vd['section']['point'] if vd.get('section') else None
        mode = None
        if vd.get('thin_parts') and vd.get('hlr', 'auto') == 'auto':
            # Whole-part and per-part runs must use the same HLR mode (mesh chord error vs. exact curves > tolerance)
            mode = 'poly' if prj.face_count([self.shape[pid] for pid in pids]) > 2500 else 'exact'
        vis, hid, sec_faces = self.raw_view(vd, pids, frame, bool(vd.get('hidden', False)), mode)
        hid = prj.clean_hidden(vis, hid, 0.15 / k) if hid else []
        axis_aligned = np.isclose(np.abs(frame[0]).max(), 1.0) or bool(vd.get('auxiliary'))   # auxiliary view: like a normal view
        centers_on = bool(vd.get('centerlines', bool(axis_aligned)))
        centers = self.centerlines(vd, pids, frame, k, vis, hid) if centers_on else []
        det = vd.get('detail')
        if det:
            c2 = prj.project([det['center']], frame)[0]
            vis = prj.clip_circle(vis, c2, det['radius']); hid = prj.clip_circle(hid, c2, det['radius'])
            centers = [(prj.clip_circle([pts], c2, det['radius']), kind) for pts, kind in centers]
            centers = [(p, kind) for ps, kind in centers for p in ps]
            circle = Point(c2).buffer(float(det['radius']), 128)          # like clip_circle; after the cache, the key stays
            sec_faces = [poly.intersection(circle) for poly in sec_faces]   # hatching only inside the detail circle
        thin_idx = set()
        thin = [p for p in vd.get('thin_parts') or [] if p in pids]
        for pid in vd.get('thin_parts') or []:
            if pid not in self.shape:
                raise AnnotationError(f'View {vid}: thin_parts: unknown part {pid}')
        if thin and vis:       # style after the cache: visible edges produced only by the thin parts (projection stays the same)
            thin_idx = self.thin_edges(vd, pids, thin, frame, vis, mode)
        allpts = np.vstack(vis + hid) if (vis or hid) else np.zeros((1, 2))
        mn, mx = (c2 - det['radius'], c2 + det['radius']) if det else (allpts.min(0), allpts.max(0))
        return {'pids': pids, 'frame': frame, 'k': k, 'vis': vis, 'hid': hid, 'centers': centers, 'sec_faces': sec_faces,
                'thin_idx': thin_idx, 'mn': mn, 'mx': mx, 'is_section': sec_pt is not None, 'mode': mode,
                'centers_on': centers_on}

    def _reference_lines(self, vd, sheet_scale, hidden):
        """Reference lines of the view in image coordinates (once per view content, details clipped to the circle):
        hidden False -> visible edges, section contours and centre lines of visible form features; hidden True -> hidden
        edges and centre lines of hidden form features (centerlines as with hidden: true; what of them is visible is already
        in the first group). Hidden edges also if the view does not draw them: then a separate HLR run with
        hidden edges (HLR cache). Centre lines only where the view draws some (centerlines)."""
        c = self.view_content(vd, sheet_scale)
        ref = self._refs.setdefault(self.content_key(vd, sheet_scale), {False: None, True: None, 'zones': {}})
        if ref[hidden] is None:
            det = vd.get('detail')
            c2 = prj.project([det['center']], c['frame'])[0] if det else None
            clip = (lambda ls: prj.clip_circle(ls, c2, det['radius'])) if det else (lambda ls: ls)
            img = lambda cs: [q if kind == 'model' else q / c['k'] for q, kind in cs]
            if hidden or vd.get('hidden'):           # raw edges with hidden ones (with hidden: true the same run as the view)
                vis_raw, hid_raw, _ = self.raw_view(vd, c['pids'], c['frame'], True, c['mode'])
            if not hidden:
                rings = [np.asarray(r.coords) for poly in c['sec_faces'] for g in getattr(poly, 'geoms', [poly])
                         if g.geom_type == 'Polygon' and not g.is_empty for r in [g.exterior, *g.interiors]]
                if not c['centers_on']:
                    cen = []
                elif vd.get('hidden'):               # drawn centre lines include those of hidden form features
                    cen = clip(img(self.centerlines({**vd, 'hidden': False}, c['pids'], c['frame'], c['k'], vis_raw, hid_raw)))
                else:
                    cen = img(c['centers'])
                lines = c['vis'] + cen + rings
            else:
                cen = img(self.centerlines({**vd, 'hidden': True}, c['pids'], c['frame'], c['k'], vis_raw, hid_raw)) if c['centers_on'] else []
                lines = clip(hid_raw) + clip(cen)
            ref[hidden] = [LineString(p) for p in lines if len(p) > 1]
        return ref

    def visible_points(self, vd, sheet_scale, pts2, tol_mm=FOOT_MM):
        """Reference on a visible outline or a drawn axis, without computing hidden edges."""
        k = self.view_content(vd, sheet_scale)['k']
        ref = self._reference_lines(vd, sheet_scale, False)
        zone = ref['zones'].setdefault(tol_mm, {})
        if False not in zone:
            zone[False] = Zone(ref[False], tol_mm / k)
        pts = np.atleast_2d(np.asarray(pts2, float))
        return zone[False].contains_xy(pts[:, 0], pts[:, 1])

    def reference_state(self, vd, sheet_scale, pts2, tol_mm=FOOT_MM):
        """Position of reference points (image coordinates of the view vd, n x 2) relative to the geometry, up to tol_mm on paper: per point
        'visible' (on a visible edge, section contour or centre line of a visible form feature), 'hidden' (otherwise on
        a hidden edge or the centre line of a hidden form feature) or 'off' (_reference_lines; the
        hidden ones only if a point is not visible). Image coordinates: independent of the position of the view.
        -> (states, hidden edges drawn?)"""
        k = self.view_content(vd, sheet_scale)['k']
        ref = self._reference_lines(vd, sheet_scale, False)
        zone = ref['zones'].setdefault(tol_mm, {})
        pts2 = np.atleast_2d(np.asarray(pts2, float))
        state = np.where(self.visible_points(vd, sheet_scale, pts2, tol_mm), 'visible', 'off').astype(object)
        off = state == 'off'
        if off.any():
            if True not in zone:
                zone[True] = Zone(self._reference_lines(vd, sheet_scale, True)[True], tol_mm / k)
            state[off] = np.where(zone[True].contains_xy(pts2[off, 0], pts2[off, 1]), 'hidden', 'off')
        return list(state), bool(vd.get('hidden', False))

    def thread_crest(self, v, ref):
        """Radius of the crest cylinder if the Ø reference ref = (centre, radius, axis) means the core of a modelled thread: a coaxial shaft cylinder of the same part with a
        larger radius (up to THREAD_CREST × r) whose axis range contains the centre (1/4"-20 UNC: core 5.0, crest 6.3). The core lies behind the crest; the visible
        thread contour alternates between the two radii per pitch, a point on the core radius only hits it by chance (the tolerance FOOT_MM / k falls with the
        scale). None if there is none."""
        c3, r, axis = ref
        if axis is None or np.linalg.norm(axis) < 1e-9:
            return None
        ax = prj.unit(axis)
        best = None
        for pid in v.get('parts') or []:
            if pid not in self.shape:
                continue
            for f in self.features(pid)['features']:
                if f.get('kind') != 'shaft' or f.get('axis') is None or f.get('start') is None or f.get('end') is None:
                    continue
                rf = float(f['diameter']) / 2
                if not (r * (1 + 1e-6) < rf <= r * THREAD_CREST):
                    continue
                fa, s0, e0 = prj.unit(np.asarray(f['axis'], float)), np.asarray(f['start'], float), np.asarray(f['end'], float)
                if abs(float(fa @ ax)) < 1 - 1e-6 or float(np.linalg.norm(np.cross(c3 - s0, fa))) > FOOT_AXIS_MM:
                    continue
                t0, t1 = sorted((float((s0 - c3) @ ax), float((e0 - c3) @ ax)))
                if t0 - FOOT_AXIS_MM <= 0 <= t1 + FOOT_AXIS_MM and (best is None or rf < best):
                    best = rf
        return best

    def hidden_reference(self, info, v, t, a, rec, ref):
        """dim_on_hidden_edge (block if strict, else warn): a reference of the dimension lies only on hidden edges or
        centre lines of hidden form features of the view (reference_state), no matter who set the dimension and whether the view draws
        hidden edges. Reference: linear p1 and p2 individually; Ø/R/thread with the axis in
        the image plane the outline points c ± w·r across the axis (thread: centre, radius of the modelled cylinder); in the
        circular view (axis ∥ viewing direction or unknown) the circle r around c, RING_N sample points up to RING_MM: hidden if
        fewer than RING_VIS lie on visible and at least RING_HID on hidden edges (independent of the angle at
        which the dimension attaches - the layout repair changes that). ref: None (linear) or (c, r, axis|None).
        -> indices of the hidden end points of a linear dimension (dim_point_off_geometry does not apply to them)."""
        vd, frame = v['vd'], v['frame']
        ring = None
        if t == 'linear':
            pts3 = [np.asarray(a['p1'], float), np.asarray(a['p2'], float)]
        else:
            c3, r, axis = ref
            if axis is None or np.linalg.norm(axis) < 1e-9 or abs(float(prj.unit(axis) @ frame[0])) > 0.99:
                ring = (c3, r)
            else:
                w = prj.unit(np.cross(frame[0], axis))
                pts3 = [c3 - w * r, c3 + w * r] if not a.get('half') else [c3 + int(np.sign(a['half'])) * w * r]      # half dimension line: only the real point
        if ring is None:
            states, drawn = self.reference_state(vd, v['sheet_scale'], prj.project(pts3, frame))
            crest = self.thread_crest(v, ref) if t != 'linear' and any(s == 'hidden' for s in states) else None
            if crest is not None:                    # core of a modelled thread: visible if the outer contour (crest) is visible at the same place
                w_ = prj.unit(np.cross(frame[0], ref[2]))
                cs, _ = self.reference_state(vd, v['sheet_scale'], prj.project([ref[0] - w_ * crest, ref[0] + w_ * crest] if not a.get('half') else [ref[0] + int(np.sign(a['half'])) * w_ * crest], frame))
                states = ['visible' if s == 'hidden' and c == 'visible' else s for s, c in zip(states, cs)]
            bad = [j for j, s in enumerate(states) if s == 'hidden' and not self.at_mark(v, pts3[j])]
            if not bad:
                return set()
            what = f'Reference point {np.round(pts3[bad[0]], 3).tolist()} lies only on a hidden edge'
        else:
            tt = np.linspace(0, 2 * np.pi, RING_N, endpoint=False)
            q = prj.project([ring[0]], frame)[0] + ring[1] * np.c_[np.cos(tt), np.sin(tt)]
            states, drawn = self.reference_state(vd, v['sheet_scale'], q, RING_MM)
            if states.count('visible') >= RING_VIS * RING_N or states.count('hidden') < RING_HID * RING_N:
                return set()
            bad = []
            what = f'Reference circle R{fmt(ring[1])} around {np.round(ring[0], 3).tolist()} is hidden only'
        self.note(info, 'dim_on_hidden_edge', 'block' if self.spec.get('strict') else 'warn',
                  f"{t} dimension {rec.get('text', '')} ({a.get('view')}): {what}" + ('' if drawn else ' (hidden edges not drawn here)')
                  + ': dimension it in a view where it is visible (opposite view, section)')
        return set(bad)

    def at_mark(self, v, p3):
        """Does the model point p3 lie on the centre mark (annotation centermark) of an auxiliary view? The cross is there
        the drawn reference line of an arc centre (auxiliary views only; otherwise as before only edges and centre lines)."""
        q = self.P(v, np.asarray(p3, float))
        return any(np.linalg.norm(q - self.P(v, m)) <= FOOT_MM for m in v.get('marks') or [])

    @staticmethod
    def line_up(ref, frame, k, axis, fallback):
        """(N1.7): origin (sheet mm, axis 0 = x, 1 = y) of a neighbouring view across the stacking direction such that the same model point has the same sheet coordinate in both views
        (projection line per method 1, ISO 5456-2), regardless of how wide the geometry shown is (section) and at which scale. Prerequisite: the image axis (axis 0: image x,
        axis 1: image y) of both views lies on the same model axis and runs in the same direction; otherwise (oblique auxiliary view, mirrored frame) fallback applies (bounding rectangle centres
        centred). Common model point: the centre of the geometry of the reference view; at equal scale the origin of the reference view is the point independent of that."""
        u_ref, u_new = ref['frame'][1 + axis], frame[1 + axis]
        if float(u_ref @ u_new) < 1 - 1e-9:
            return fallback
        k_ref, o_ref = float(ref['scale']), float(ref['origin'][axis])
        if abs(k_ref - k) < 1e-12:
            return o_ref
        rb = ref['geom_bbox']
        return o_ref + (1.0 - k / k_ref) * ((rb[axis] + rb[axis + 2]) / 2 - o_ref)

    def build_view(self, sh, vd, sheet_scale, placed):
        """Place view vd on sheet sh (content from view_content, position from at or place relative to placed)."""
        c = self.view_content(vd, sheet_scale)
        vid, pids, frame, k, mn, mx = vd['id'], c['pids'], c['frame'], c['k'], c['mn'], c['mx']
        vis, hid, centers, sec_faces, thin_idx = c['vis'], c['hid'], c['centers'], c['sec_faces'], c['thin_idx']
        # Position on the sheet
        if 'at' in vd:
            origin = np.asarray(vd['at'], float) - k * (mn + mx) / 2
        elif 'place' in vd:
            pl = vd['place']; ref = placed.get(pl['relative_to'])
            if ref is None:
                raise AnnotationError(f"View {vid}: reference view {pl['relative_to']} is missing or comes later")
            side, gap = pl.get('side', 'below'), float(pl.get('gap', 25))
            rb = ref['geom_bbox']
            if side in ('below', 'above'):
                ox = self.line_up(ref, frame, k, 0, (rb[0] + rb[2]) / 2 - k * (mn[0] + mx[0]) / 2)
                oy = rb[1] - gap - k * mx[1] if side == 'below' else rb[3] + gap - k * mn[1]
            else:
                oy = self.line_up(ref, frame, k, 1, (rb[1] + rb[3]) / 2 - k * (mn[1] + mx[1]) / 2)
                ox = rb[2] + gap - k * mn[0] if side == 'right' else rb[0] - gap - k * mx[0]
            origin = np.array([ox, oy])
        else:
            raise AnnotationError(f'View {vid}: neither at nor place given')
        T = lambda pts: origin + k * np.asarray(pts, float)
        lines, drawn = [], []
        for i, pts in enumerate(vis):
            q = T(pts); sh.line(q, 'thin' if i in thin_idx else 'thick', reg=None, z=2); lines.append(LineString(q))
        if not self.slim:      # hidden edges and centre lines: image and foot point check (neither in the lean probe)
            for pts in hid:
                q = T(pts); sh.line(q, 'thin', 'hidden', reg=None, z=1); drawn.append(LineString(q))
            for pts, kind in centers:
                q = T(pts) if kind == 'model' else pts + origin
                sh.line(q, 'thin', 'center', reg=None, z=1); drawn.append(LineString(q))
        if vd.get('detail') and not self.slim:       # the excerpt is bounded by a thin continuous line - edges end cleanly at the circle, no loose stubs
            det = vd['detail']
            ring = np.array(Point(prj.project([det['center']], frame)[0]).buffer(float(det['radius']), 128).exterior.coords)
            q = T(ring); sh.line(q, 'thin', reg=None, z=1); drawn.append(LineString(q))
        hatch_lines = 0
        for i, poly in enumerate(sec_faces):
            for g in getattr(poly, 'geoms', [poly]):                   # after clipping also empty/collection
                if g.geom_type != 'Polygon' or g.is_empty:
                    continue
                pp = SPolygon(T(np.array(g.exterior.coords)), [T(np.array(r.coords)) for r in g.interiors])
                for h in prj.hatch(pp, 2.5 if i % 2 == 0 else 3.5, 45 if i % 2 == 0 else 135):
                    sh.line(h, 'thin', reg=None, z=1); hatch_lines += 1
        gb = [float(origin[0] + k * mn[0]), float(origin[1] + k * mn[1]), float(origin[0] + k * mx[0]), float(origin[1] + k * mx[1])]
        if vd.get('detail'):                          # the boundary line belongs to the view (distances, arrangement)
            c2, r2 = prj.project([vd['detail']['center']], frame)[0], float(vd['detail']['radius'])
            gb = [min(gb[0], float(origin[0] + k * (c2[0] - r2))), min(gb[1], float(origin[1] + k * (c2[1] - r2))),
                  max(gb[2], float(origin[0] + k * (c2[0] + r2))), max(gb[3], float(origin[1] + k * (c2[1] + r2)))]
        bb = list(gb)
        for pts, kind in centers:
            q = T(pts) if kind == 'model' else pts + origin
            bb = [min(bb[0], q[:, 0].min()), min(bb[1], q[:, 1].min()), max(bb[2], q[:, 0].max()), max(bb[3], q[:, 1].max())]
        if vd.get('label'):
            sh.owner = f'view:{vid}'
            sh.text((gb[0] + gb[2]) / 2 + float(vd.get('label_dx', 0.0)), bb[3] + 5 + float(vd.get('label_dy', 0.0)), vd['label'], 5.0, ha='center')      # label_dy (label_layout.place): title above the dimension lines of the block
            bb[3] += 5 + 5.0
            sh.owner = None
        return {'id': vid, 'frame': frame, 'scale': k, 'origin': origin, 'geom_bbox': gb, 'bbox': [float(v) for v in bb],
                'lines': lines, 'drawn': lines + drawn, 'parts': pids, 'preset': vd.get('preset'), 'counters': {}, 'centerline_count': len(centers),
                'hatch_lines': hatch_lines, 'is_section': c['is_section'],
                'axis_view': bool(np.isclose(np.abs(frame[0]).max(), 1.0)) or bool(vd.get('auxiliary')),   # auxiliary view: lengths in the image plane are true
                'vd': vd, 'sheet_scale': sheet_scale}

    def centerlines(self, vd, pids, frame, k, vis, hid):
        """Centre lines (ISO 128-24, 04.1) only where the form feature is visible (or drawn
        hidden): cylinder axes in the image plane, centre crosses for plan views of circles,
        centre line arcs of bends. Visibility = outline lines lie on drawn edges."""
        N = frame[0]
        tol = max(0.01, 0.2 / k)
        buf_v = Zone([LineString(p) for p in vis], tol) if vis else None
        buf_h = Zone([LineString(p) for p in vis + hid], tol) if (vis or hid) else None

        def near(pts3, use_hidden):
            g = buf_h if use_hidden else buf_v
            if g is None:
                return np.zeros(len(pts3), bool)
            q = prj.project(pts3, frame)
            return g.contains_xy(q[:, 0], q[:, 1])

        def runs(P, w, r, use_hidden):
            """Sections of the centre line P (n x 3) whose outline (P ± w r) is visible; with overshoot."""
            m = near(P + w * r, use_hidden) | near(P - w * r, use_hidden)
            out, i = [], 0
            while i < len(m):
                if not m[i]:
                    i += 1; continue
                j = i
                while j + 1 < len(m) and m[j + 1]:
                    j += 1
                if j > i:
                    q = prj.project(P[i:j + 1], frame)
                    u0, u1 = u2(q[0] - q[1]), u2(q[-1] - q[-2])
                    out.append(np.vstack([q[0] + u0 * 2.0 / k, q, q[-1] + u1 * 2.0 / k]))
                i = j + 1
            return out

        out, crosses = [], []
        show_hidden = bool(vd.get('hidden', False))
        for pid in pids:
            for c in self.cyl(pid):
                r = c['radius']
                if k * r < 0.75:
                    continue
                d = np.asarray(c['axis']); a, b = np.asarray(c['start']), np.asarray(c['end'])
                use_h = show_hidden
                dn = float(d @ N)
                if abs(dn) > 0.99:
                    for e in (a, b):
                        cc = prj.project([e], frame)[0]
                        if any(np.linalg.norm(cc - q) < 1e-3 for q in crosses):
                            continue
                        X = prj.unit(np.cross(d, [1, 0, 0]) if abs(d[0]) < 0.9 else np.cross(d, [0, 1, 0])); Y = np.cross(d, X)
                        tt = np.linspace(0, 2 * np.pi, 24, endpoint=False)
                        ring = e + r * (np.cos(tt)[:, None] * X + np.sin(tt)[:, None] * Y)
                        if near(ring, use_h).mean() < 0.6:
                            continue
                        crosses.append(cc)
                        arm = r + 2.0 / k
                        out.append((np.array([cc - [arm, 0], cc + [arm, 0]]), 'model'))
                        out.append((np.array([cc - [0, arm], cc + [0, arm]]), 'model'))
                        break
                else:                       # axis in or oblique to the image plane: projected axis
                    w = prj.unit(np.cross(N, d))
                    P = a + np.linspace(0, 1, 60)[:, None] * (b - a)
                    out += [(q, 'model') for q in runs(P, w, r, use_h)]
            for t in self.tor(pid):
                A = np.asarray(t['axis'])
                if k * t['minor'] < 0.75:
                    continue
                C = np.asarray(t['center'])
                X = prj.unit(np.cross(A, [1, 0, 0]) if abs(A[0]) < 0.9 else np.cross(A, [0, 1, 0])); Y = np.cross(A, X)
                ang = []
                for f in ana.faces(self.shape[pid]):
                    kind, g = ana.analytic(f)
                    if kind != 'torus' or np.linalg.norm(ana.v3(g.Position().Location()) - C) > 1e-3:
                        continue
                    bp = ana.boundary_points(f)
                    ang += list(np.arctan2((bp - C) @ Y, (bp - C) @ X))
                if not ang:
                    continue
                a0, span = ana._arc_range(np.array(ang))
                tt = np.linspace(a0, a0 + span, 40)
                radial = np.cos(tt)[:, None] * X + np.sin(tt)[:, None] * Y
                P = C + t['major'] * radial
                tang = np.cross(A, radial)                         # tangent of the centre line
                w = np.cross(N, tang)
                nw = np.linalg.norm(w, axis=1)
                if (nw < 0.2).any():                               # arc runs towards the camera: no line
                    continue
                w = w / nw[:, None]
                m = near(P + w * t['minor'], show_hidden) | near(P - w * t['minor'], show_hidden)
                if m.mean() > 0.5:
                    out.append((prj.project(P, frame), 'model'))
        return attached_centerlines(out, vis, k)

    # ------------------------------------------------------------------ Annotations
    def P(self, v, p):
        return v['origin'] + v['scale'] * prj.project([p], v['frame'])[0]

    ARC_FREE_STEP, ARC_FREE_REACH = 1.0, 120.0

    def angle_free_radius(self, sh, v, views, qv, d1, d2, text, h, r0, shift=0.0):
        """Radius of the angle arc at which arc, extension lines and text are free. Arc and extension lines: checks.arc_clear (the same function the checker uses,
        checks.angle_arc_findings; thresholds there) against the visible body edges of the view (v['lines']), dimension/leader lines and texts drawn so far; text: checks.text_core as text_on_line/
        text_on_geometry, plus distance to the centre lines of the view (sh.aux_lines, not registered), drawing area and other views. Order: first between the legs (per range first the whole arc, then without ends:
        legs lie on the contour, e.g. dovetail) (radius <= shortest leg, nearest to r0 first, no extension lines needed), then beyond the
        leg end points (ascending, up to ARC_FREE_REACH, with extension lines). Nothing free: r0 (the checker reports angle_arc_unplaced). Draft only, nothing is drawn or registered."""
        own = sh.owner
        free_items = [it for it in sh.items if it['owner'] not in checks.BLOCKS and not str(it['owner'] or '').startswith('table:') and it['owner'] != own]
        lines_all = [g for g in v['lines']] + [LineString(it['pts']) for it in free_items if it['kind'] == 'line' and it.get('pts') and len(it['pts']) >= 2]
        others = [g for g in v['lines']] + [LineString(it['pts']) for it in free_items if it['kind'] == 'line' and it.get('pts') and len(it['pts']) >= 2 and it.get('role') != 'ext']      # arc: as in checks.angle_arc_findings extension lines may cross it
        det = v['vd'].get('detail')
        if det:      # the detail circle (not registered) is an obstacle for the arc (renderer only, stricter than the checker)
            c2 = v['origin'] + v['scale'] * prj.project([det['center']], v['frame'])[0]
            tt = np.linspace(0, 2 * np.pi, 120)
            others.append(LineString(c2 + v['scale'] * float(det['radius']) * np.c_[np.cos(tt), np.sin(tt)]))
        text_boxes = [it['bbox'] for it in free_items if it['kind'] == 'text']
        texts = [sbox(*b).buffer(0.45, join_style='mitre') for b in text_boxes]
        tree = STRtree(lines_all) if lines_all else None      # text: all lines as in text_on_line/text_on_geometry
        arc_tree = STRtree(others) if others else None
        views_other = [sbox(*u['bbox']) for u in (views or {}).values() if u is not v]
        L, B, R, T = sh.area
        area = sbox(L, B, R, T)
        aux = [g for vid, g in getattr(sh, 'aux_lines', []) if vid == v['id']]
        aux_tree = STRtree(aux) if aux else None

        def free(r, trim):
            pts, am, pos, rot = sh.angle_layout(qv, d1, d2, r, shift)
            corners = sh.text_corners(pos[0], pos[1], text, h, rot, 'center', 'bottom')
            bx = [float(corners[:, 0].min()), float(corners[:, 1].min()), float(corners[:, 0].max()), float(corners[:, 1].max())]      # the registered envelope (axis box) as the checker sees it
            ink = sbox(*bx).buffer(0.45, join_style='mitre')
            if not area.covers(LineString(pts)) or not area.covers(ink):
                return False
            if any(ink.intersects(o) or LineString(pts).intersects(o) for o in views_other):
                return False
            if any(ink.intersects(t) for t in texts):
                return False
            if tree is not None and len(tree.query(checks.text_core(bx), predicate='intersects')):
                return False      # same function as text_on_line/text_on_geometry
            if aux_tree is not None and len(aux_tree.query(checks.text_core(bx), predicate='intersects')):
                return False
            return checks.arc_clear(pts, others, text_boxes, trim, arc_tree)
        leg_min = min(float(np.linalg.norm(d1)), float(np.linalg.norm(d2)))
        inner_top = leg_min - checks.ARC_LEG_MIN_MM
        inner = sorted([float(r) for r in np.arange(4.0, max(4.0, inner_top) + 1e-9, self.ARC_FREE_STEP) if r <= inner_top + 1e-9], key=lambda r: abs(r - r0))
        outer = [max(r0, inner_top + self.ARC_FREE_STEP) + i * self.ARC_FREE_STEP for i in range(int(self.ARC_FREE_REACH / self.ARC_FREE_STEP) + 1)]
        for radii in (inner, outer):      # between the legs takes precedence over beyond, per range first the whole arc, then without ends
            for trim in (False, True):
                for r in radii:
                    if free(r, trim):
                        return float(r)
        return r0

    def side_offset(self, v, side, step=None):
        c = v['counters'].get(side, 0)
        v['counters'][side] = c + 1
        return 10.0 + 7.0 * c

    def annotate(self, sh, a, views, info):
        t = a.get('type')
        v = views.get(a.get('view')) if a.get('view') else None
        if a.get('view') and v is None:
            raise AnnotationError(f"Annotation {t}: view {a.get('view')} does not exist on this sheet")
        h = float(a.get('h', 3.5))
        claims = {k: a[k] for k in ('features', 'model_ref') if a.get(k) is not None}      # collective statement, reference
        for k, ids in claims.items():
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
                raise AnnotationError(f'{t}: {k} must be a non-empty list of form feature ids')
        rec = {'type': t, 'view': a.get('view')}
        pts3 = []
        geo = None                  # internal 3D dimension geometry for the coverage check (rec['_geo'])
        ref = None                  # reference of a Ø/R/thread (centre, radius, axis|None) for hidden_reference
        if t == 'linear':
            p1, p2 = np.asarray(a['p1'], float), np.asarray(a['p2'], float); pts3 = [p1, p2]
            q1, q2 = self.P(v, p1), self.P(v, p2)
            d2 = prj.project([p2], v['frame'])[0] - prj.project([p1], v['frame'])[0]
            axis = a.get('axis', 'aligned')
            if axis == 'h':
                value, u = abs(d2[0]), np.array([1.0, 0.0])
            elif axis == 'v':
                value, u = abs(d2[1]), np.array([0.0, 1.0])
            elif axis == 'along' and a.get('dir') is not None and np.linalg.norm(prj.project([np.asarray(a['dir'], float)], v['frame'])[0]) > 1e-9:
                u = u2(prj.project([np.asarray(a['dir'], float)], v['frame'])[0])      # dimension of the projection onto direction dir (cone length along an oblique axis)
                value = abs(float(d2 @ u))
            else:
                value, u = float(np.linalg.norm(d2)), u2(q2 - q1)
            text = self.dim_text(a, value)
            off = self.linear_offset(a, v, q1, u, axis)
            sh.dim(q1, q2, off, text, u=u, h=h, shift=float(a.get('text_shift', 0.0)))
            rec.update(text=text, value=round(float(value), 4), p1_sheet=q1.round(2).tolist(), p2_sheet=q2.round(2).tolist())
            w = {'h': v['frame'][1], 'v': v['frame'][2]}.get(axis, d2[0] * v['frame'][1] + d2[1] * v['frame'][2])
            if axis == 'along' and a.get('dir') is not None:
                w = np.asarray(a['dir'], float)
            geo = {'p1': p1.tolist(), 'p2': p2.tolist(), 'dir3': (w / np.linalg.norm(w)).tolist() if np.linalg.norm(w) > 1e-12 else None}
        elif t in ('diameter', 'radius'):
            c3 = np.asarray(a['center'], float); pts3 = [c3]
            r, axis_dir = self.lookup_radius(v, c3, a, t)
            value = 2 * r if t == 'diameter' else r
            text = self.dim_text(a, value, default_prefix='Ø' if t == 'diameter' else 'R')
            c = self.P(v, c3); rp = v['scale'] * r
            ang = float(a.get('angle', 45))
            dvec = np.array([np.cos(np.radians(ang)), np.sin(np.radians(ang))])
            N = v['frame'][0]
            if t == 'radius':
                sh.radius(c, rp, ang, text, h=h, outside=float(a.get('text_out', 4.0)))      # text_out pushes the text outward on the extension of the radius line (autofix)
            elif axis_dir is None or abs(axis_dir @ N) > 0.99:
                # (ISO 129-1): the dimension line stands inside the circle if text and both arrows fit; otherwise outside as a leader line
                # (the limit follows from text width and arrow length, not from a fixed radius; also applies in detail views)
                if circle_fits(sh.text_width(text, h), rp) and not a.get('leader'):
                    sh.dim(c - dvec * rp, c + dvec * rp, 0.0, text, u=dvec, h=h)
                else:
                    tip = c + dvec * rp
                    sh.leader(tip, tip + dvec * float(a.get('leader', 8)), text, h=h)
                    rec['outside'] = True
            else:                   # axis in the image plane: linear dimension across the axis (position only via offset)
                w = prj.unit(np.cross(N, axis_dir))
                half = int(np.sign(a.get('half') or 0))
                if half:               # half dimension line (ISO 129-1, half representation): one arrowhead at the real surface point c + half · w · r, the line ends
                    # HALF_OVER beyond the axis, one extension line (at the real point), the axis as centre line across the outline
                    q1, qc = self.P(v, c3 + half * w * r), self.P(v, c3)
                    d = u2(qc - q1)
                    q2 = qc + d * HALF_OVER
                    ax2 = u2(self.P(v, c3 + axis_dir) - qc)
                    sh.line([qc - ax2 * HALF_AXIS, qc + ax2 * HALF_AXIS], 'thin', 'center', reg=None)
                    sh.dim(q1, q2, float(a.get('offset', 0.0)) * (-half), text, u=d, h=h, arrows=(True, False), ext=(True, False))
                    rec.update(p1_sheet=q1.round(2).tolist(), p2_sheet=q2.round(2).tolist(), half=half)
                else:
                    q1, q2 = self.P(v, c3 - w * r), self.P(v, c3 + w * r)
                    u = u2(q2 - q1)
                    sh.dim(q1, q2, float(a.get('offset', 0.0)), text, u=u, h=h, shift=float(a.get('text_shift', 0.0)))
                    rec.update(p1_sheet=q1.round(2).tolist(), p2_sheet=q2.round(2).tolist())   # drawn linearly (autofix)
            rec.update(text=text, value=round(float(value), 4))
            geo = {'center': c3.tolist(), 'radius': float(r), 'normal': N.tolist()}
            ref = (c3, float(r), axis_dir)
        elif t == 'thread':
            # ISO 6410 simplified: thread root (outside minor_d) or crest (inside major_d) as a narrow line; the body edge
            # (outside major_d) comes from the model. Axis in the image plane: two lines, axis ⟂ image plane: three-quarter circle.
            s3, e3 = np.asarray(a['start'], float), np.asarray(a['end'], float); pts3 = [s3, e3]
            major, minor, internal = float(a['major_d']), float(a['minor_d']), bool(a.get('internal', False))
            text = str(a['designation'])
            ax, N, mid = e3 - s3, v['frame'][0], (s3 + e3) / 2
            if np.linalg.norm(ax) < 1e-9:
                raise AnnotationError('thread: start and end coincide')
            ax = ax / np.linalg.norm(ax)
            r_thin = (major if internal else minor) / 2
            ang = float(a.get('angle', 45))
            dvec = np.array([np.cos(np.radians(ang)), np.sin(np.radians(ang))])
            if abs(ax @ N) > 0.99:
                c = self.P(v, mid)
                tt = np.linspace(0, 1.5 * np.pi, 48)
                sh.line(c + v['scale'] * r_thin * np.c_[np.cos(tt), np.sin(tt)], 'thin', reg=None)
                tip = c + dvec * v['scale'] * major / 2
            else:
                w = prj.unit(np.cross(N, ax))
                for sg in (-1, 1):
                    sh.line([self.P(v, s3 + sg * w * r_thin), self.P(v, e3 + sg * w * r_thin)], 'thin', reg=None)
                c = self.P(v, mid)
                tips = [self.P(v, mid + sg * w * major / 2) for sg in (-1, 1)]
                tip = max(tips, key=lambda q: float((q - c) @ dvec))
            sh.leader(tip, tip + dvec * float(a.get('leader', 8)), text, h=h)
            rec.update(text=text, value=round(major, 4))
            geo = {'center': mid.tolist(), 'radius': self.thread_radius(v, mid, major, minor, internal), 'normal': N.tolist()}
            ref = (mid, geo['radius'], ax)
        elif t == 'angle':
            vx, p1, p2 = (np.asarray(a[k_], float) for k_ in ('vertex', 'p1', 'p2')); pts3 = [vx, p1, p2]
            qv, q1, q2 = self.P(v, vx), self.P(v, p1), self.P(v, p2)
            d1, d2 = q1 - qv, q2 - qv
            nrm = np.cross(p1 - vx, p2 - vx)
            if np.linalg.norm(nrm) > 1e-9 and abs(prj.unit(nrm) @ v['frame'][0]) < 1 - 1e-4:
                self.note(info, 'dim_not_true_length', 'block', f"Angle at {np.round(vx, 3).tolist()} does not lie in the image plane of view {a.get('view')}: the displayed angle is distorted; dimension it in a view perpendicular to the angle plane")
            value = float(np.degrees(np.arccos(np.clip(u2(d1) @ u2(d2), -1, 1))))
            text = a.get('text') or (a.get('prefix', '') + fmt(value, int(a.get('decimals', 1))) + '°' + a.get('suffix', ''))
            radius = float(a.get('radius', 12))
            if a.get('arc') == 'free':          # arc='fixed' (label_layout.place, text pull with a fixed radius): no search, but extension lines. Arc and text free of contour and foreign lines/texts, nearest free radius from `radius`
                radius = self.angle_free_radius(sh, v, views, qv, d1, d2, text, h, radius, float(a.get('text_shift', 0.0)))
            sh.angle_dim(qv, d1, d2, radius, text, h=h, legs=a.get('arc') in ('free', 'fixed'), shift=float(a.get('text_shift', 0.0)))
            rec.update(text=text, value=round(value, 4), arc_radius=round(radius, 3))
            geo = {'value': round(value, 4)}                   # the angle belongs to the coverage of a slant (checks)
        elif t == 'leader':
            q = self.P(v, a['p']) if v else np.asarray(a['at_sheet'], float)
            if v:
                pts3 = [np.asarray(a['p'], float)]
            dx, dy = a.get('to', [8, 8])
            sh.leader(q, q + [dx, dy], a['text'], h=h, end=a.get('end', 'arrow'))
            rec.update(text=a['text'], p_sheet=q.round(2).tolist())          # tip on the sheet (autofix: position of the text against the view envelope)
        elif t == 'balloon':
            p3 = np.asarray(a['p'], float)
            pid = a.get('part') or next(iter(self.parts_at(p3)), None)
            num = a.get('pos') or self.pos_of.get(pid)
            if num is None:
                raise AnnotationError(f'Item number: part {pid} is not in the parts list and no pos given')
            q = self.P(v, p3)
            dx, dy = a.get('to', [12, 12])
            sh.balloon(q, q + [dx, dy], num, r=float(a.get('r', 4.5)), h=float(a.get('h', 5)))
            info['balloons'].add(pid)
            fp = None if a.get('hidden') or self.slim else self.balloon_foreign(v, pid, p3)      # the planner's lean layout probe does not check this (planning time)
            if fp:
                self.note(info, 'balloon_on_foreign_part', 'warn',
                          f"Item number {num} (part {pid}) ends at a point closer than {ft.BALLOON_CLEAR_MM:g} mm to part {fp} (edge or face of a foreign part)")
            rec.update(text=str(num), part=pid)
        elif t == 'point_label':
            q = self.P(v, a['p'])
            dx, dy = a.get('to', [2.5, 2.5])
            sh.circle(q, 0.45, fill='k', reg=None)
            sh.text(q[0] + dx, q[1] + dy, a['label'], float(a.get('h', 2.5)))
            rec.update(text=a['label'])
        elif t == 'polyline':
            q = np.array([self.P(v, p) for p in a['points']])
            st = a.get('style', 'thin')
            if st == 'center' and a.get('view'):
                sh.__dict__.setdefault('aux_lines', []).append((a['view'], LineString(q)))      # not registered (reg=None), but an obstacle for the free angle arc (angle_free_radius)
            sh.line(q, 'thick' if st == 'thick' else 'thin', st if st in ('center', 'hidden') else None,
                    reg='line' if a.get('symmetry_axis') else None, role='center' if a.get('symmetry_axis') else None)
        elif t == 'centermark':
            c = self.P(v, a['center']); s = float(a.get('size', 3))
            sh.line([c - [s, 0], c + [s, 0]], 'thin', 'center', reg=None); sh.line([c - [0, s], c + [0, s]], 'thin', 'center', reg=None)
        elif t == 'text':
            if 'at_sheet' in a:
                x, y = a['at_sheet']
            else:
                b = v['bbox']; dx, dy = a.get('at_view', [0, 0])
                x, y = (b[0] + b[2]) / 2 + dx, (b[1] + b[3]) / 2 + dy
            for i, line in enumerate(str(a['text']).split('\n')):
                if a.get('block_title') and a.get('max_width') and a.get('ha', 'left') == 'left':
                    # Block title: fit into the block width (shrink, then truncate) and check as a cell; single-line
                    yy = y - i * 1.6 * h
                    width = float(a['max_width'])
                    bottom, top = yy - 0.4*h, yy + 1.1*h
                    if line:
                        _, ink_height, descent = sh.text_size(line, h)
                        if ink_height <= top - bottom:
                            yy = float(np.clip(yy, bottom - descent, top - descent - ink_height))
                    sh.field(x, yy, line, h, width, h * 1.1, 'Block title', [x, bottom, x + width, top])
                else:
                    sh.text(x, y - i * 1.6 * h, line, h, ha=a.get('ha', 'left'))
            rec.update(text=a['text'])
            if a.get('block_title'):            # heading of a part block (plan.py): not a note text (checks.notes_text)
                rec['_block_title'] = True
        elif t == 'section_line':
            q1, q2 = self.P(v, a['p1']), self.P(v, a['p2'])
            e = u2(q2 - q1)
            look = prj.project([a['look']], v['frame'])[0] if 'look' in a else np.array([-e[1], e[0]])
            look = u2(look)
            sh.line([q1 - e * 2, q2 + e * 2], 'thin', 'center', reg='line')
            for q, s in ((q1, -1), (q2, 1)):
                sh.line([q + e * s * 2, q - e * s * 5], 'thick', reg='line')
                base = q + e * s * 2
                sh.line([base, base + look * 7], 'thin'); sh.arrow(base + look * 7, look)
                lp = base + look * 9 + e * s * 2 + _label_shift(a)
                sh.text(lp[0], lp[1], a.get('label', 'A'), 5.0, ha='center', va='center')
            rec.update(text=a.get('label', 'A'))
        elif t == 'detail_circle':
            c = self.P(v, a['center']); r = v['scale'] * float(a['radius'])
            sh.circle(c, r, 'thin', reg='line')
            lp = c + np.array([r, r]) * 0.75 + 2.5 + _label_shift(a)
            sh.text(lp[0], lp[1], a.get('label', 'Z'), 5.0)
            rec.update(text=a.get('label', 'Z'))
        elif t == 'view_arrow':     # view arrow of an auxiliary view (ISO 128-3): tip at p, points in viewing direction look
            tip = self.P(v, a['p'])
            look = prj.project([a['look']], v['frame'])[0]
            if np.linalg.norm(look) < 0.2 * np.linalg.norm(a['look']):
                raise AnnotationError(f"view_arrow {a.get('label', 'X')}: viewing direction is almost perpendicular to the image plane in view {a.get('view')}")
            e = u2(look)
            sh.line([tip - e * 10, tip], 'thin', reg='line'); sh.arrow(tip, e)
            lp = tip - e * 13 + _label_shift(a)
            sh.text(lp[0], lp[1], a.get('label', 'X'), 5.0, ha='center', va='center')
            rec.update(text=a.get('label', 'X'))
        else:
            raise AnnotationError(f'Unknown annotation {t}')
        if t in ('linear', 'diameter', 'radius', 'angle', 'thread') and v is not None:
            if not v['axis_view']:
                self.note(info, 'dim_not_true_length', 'block', f"{t} dimension in the axonometric view {a.get('view')}: lengths and angles are distorted there; dimension only in normal views")
            if t == 'linear' and a.get('axis', 'aligned') == 'aligned':
                d3 = np.asarray(a['p2'], float) - np.asarray(a['p1'], float)
                if abs(d3 @ v['frame'][0]) > 1e-3 * np.linalg.norm(d3) + 1e-6:
                    self.note(info, 'dim_not_true_length', 'block', f"Dimension {rec.get('text')} ({a.get('view')}): p1-p2 is oblique to the image plane, the projection is shown instead of the true length")
            hidden = set()
            if t != 'angle' and not self.slim:                  # hidden references (position independent): not in the lean probe
                try:
                    hidden = self.hidden_reference(info, v, t, a, rec, ref)
                except Exception as e:                          # exception of the check = visible finding, the dimension stays
                    self.note(info, 'hidden_check_failed', 'block' if self.spec.get('strict') else 'warn',
                              f"{t} dimension {rec.get('text', '')} ({a.get('view')}): hidden-edge check failed ({type(e).__name__}: {e}), "
                              'visibility of the reference unchecked')
            if t in ('linear', 'angle') and not self.slim:      # foot points (warn): not in the lean probe
                if 'foot_zone' not in v:
                    v['foot_zone'] = Zone(v['drawn'], FOOT_MM) if v['drawn'] else None
                for j, p in enumerate((a['p1'], a['p2'])):
                    if j in hidden or self.at_mark(v, p):       # lies on (hidden) geometry or the centre mark of the auxiliary view
                        continue
                    q = self.P(v, np.asarray(p, float))
                    if v['foot_zone'] is None or not v['foot_zone'].contains_xy([q[0]], [q[1]])[0]:
                        self.note(info, 'dim_point_off_geometry', 'warn', f"{t} dimension {rec.get('text', '')} ({a.get('view')}): point {np.round(p, 3).tolist()} lies on no drawn edge/centre line")
                        break
        for p3 in pts3:
            if t in ('linear', 'diameter', 'radius', 'angle', 'thread'):
                info['dims_parts'].update(self.parts_at(p3, 0.05))
        if geo is None and (t in ('angle', 'leader') or a.get('feature') or claims):
            geo = {}
        if geo is not None:
            rec['_geo'] = {'kind': t, 'view': a.get('view'), 'text': rec.get('text'), 'parts': list(v['parts']) if v else [], **geo}
            if a.get('feature'):
                rec['_geo']['feature'] = str(a['feature'])
                if a.get('also'):                       # merged twin dimension: also covers these form features
                    rec['_geo']['also'] = [str(i) for i in a['also']]
            rec['_geo'].update({k: list(ids) for k, ids in claims.items()})
        if claims:
            rec['_claim'] = True                # not a free note text (checks.notes_text)
        return rec

    def thread_radius(self, v, c3, major, minor, internal):
        """Radius of the cylinder that the thread designates, as modelled: a cylinder of the view parts through c3
        with Ø between minor and nominal Ø (features.match_hints) wins; otherwise nominal Ø (outside) or minor Ø (inside, core hole)."""
        found = set()
        for pid in v['parts']:
            for c in self.cyl(pid):
                d, o = np.asarray(c['axis']), np.asarray(c['start'])
                if (np.linalg.norm(np.cross(c3 - o, d)) < 0.02 and -0.02 <= float((c3 - o) @ d) <= c['depth'] + 0.02
                        and minor / 2 - 0.05 <= c['radius'] <= major / 2 + 0.05):
                    found.add(round(c['radius'], 4))
        return float(found.pop()) if len(found) == 1 else (minor if internal else major) / 2

    @staticmethod
    def note(info, code, severity, message):
        info.setdefault('notes', []).append((code, severity, message))

    def dim_text(self, a, value, default_prefix=''):
        if a.get('text'):
            return a['text']
        s = a.get('prefix', default_prefix) + fmt(value, int(a.get('decimals', 2))) + a.get('suffix', '')
        if a.get('tolerance'):
            s += ' ' + a['tolerance']
        return s

    def linear_offset(self, a, v, q1, u, axis):
        if 'offset' in a:
            return float(a['offset'])
        b = v['bbox']
        side = a.get('side') or {'h': 'above', 'v': 'left'}.get(axis, 'left')
        o = self.side_offset(v, (axis, side))
        if axis == 'h':
            yt = b[3] + o if side == 'above' else b[1] - o
            return yt - q1[1]
        if axis == 'v':
            xt = b[0] - o if side == 'left' else b[2] + o
            return q1[0] - xt
        n = np.array([-u[1], u[0]])
        ctr = np.array([(b[0] + b[2]) / 2, (b[1] + b[3]) / 2])
        sgn = 1.0 if (q1 - ctr) @ n >= 0 else -1.0
        if side in ('left', 'right'):
            sgn = 1.0 if side == 'left' else -1.0
        ext = max([0.0] + [float((np.array([x, y]) - q1) @ n) * sgn for x in (b[0], b[2]) for y in (b[1], b[3])])
        return sgn * (ext + o)

    def lookup_radius(self, v, c3, a, kind):
        if a.get('radius') is not None:
            return float(a['radius']), (np.asarray(a['axis'], float) if a.get('axis') else None)
        # Cylinders: point on the axis AND within its axial range (shoulders,
        # countersinks, pin heads are coaxial); tori: centre of the bend.
        found = []
        tol = 0.02
        for pid in v['parts']:
            for c in self.cyl(pid):
                d = np.asarray(c['axis']); o = np.asarray(c['start'])
                dist = np.linalg.norm(np.cross(c3 - o, d))
                t = float((c3 - o) @ d)
                if dist < tol and -tol <= t <= c['depth'] + tol:
                    found.append((c['radius'], d, 'Cylinder'))
            for tr in self.tor(pid):
                if np.linalg.norm(c3 - np.asarray(tr['center'])) < tol:
                    which = a.get('which', 'center')
                    R = tr['major'] + {'center': 0, 'inner': -tr['minor'], 'outer': tr['minor']}.get(which, 0)
                    found.append((R, np.asarray(tr['axis']), 'Torus'))
        if not found:
            raise AnnotationError(f"{kind}: no cylinder/torus with an axis through {np.round(c3, 3).tolist()} found; put the point on the axis (inside the form feature) or give radius")
        radii = sorted({round(r, 4) for r, _, _ in found})
        if len(radii) > 1:
            raise AnnotationError(f"{kind}: several form features meet at {np.round(c3, 3).tolist()} (R {radii}); put the point into the intended form feature or give radius/which")
        return found[0][0], found[0][1]

    AXIS_MM = 8.0          # length of the axes of the axis cross on paper
    CLEAR = 0.6            # clearance between label and body edge (mm)
    LABEL_H = 2.5          # text height of the bend point labels
    NAMES = {'front': 'Front view', 'back': 'Rear view', 'left': 'Left side view', 'right': 'Right side view',
             'top': 'Top view', 'bottom': 'Bottom view', 'iso': 'ISO', 'iso_back': 'ISO'}

    def view_name(self, v):
        vd = v['vd']
        if vd.get('label') or self.NAMES.get(vd.get('preset')):
            return vd.get('label') or self.NAMES[vd['preset']]
        if not v['axis_view']:
            return 'ISO'
        return self.NAMES[max(('front', 'back', 'left', 'right', 'top', 'bottom'), key=lambda p: float(prj.unit(prj.PRESETS[p][0]) @ v['frame'][0]))]

    def axis_cross(self, sh, v, o3, pid):
        """Axis cross of the table coordinate system at the origin o3 in view v: per axis X, Y, Z a line with an arrowhead in
        + direction (length AXIS_MM on paper) and letter; if an axis is almost perpendicular to the image plane, a circle
        (dot: towards the viewer, cross: away) shows the origin and the letter stands beside it. Returns: occupancy (envelopes) for the labels."""
        q0 = self.P(v, o3)
        boxes, lines = [], []
        sh.owner = f'axes:{pid}'
        for k, letter in enumerate('XYZ'):
            e = np.eye(3)[k]
            d2 = prj.project([e], v['frame'])[0]
            if np.linalg.norm(d2) < 0.3:
                toward = float(e @ v['frame'][0]) > 0
                sh.circle(q0, 1.2, 'thin', reg='line')
                if toward:
                    sh.circle(q0, 0.35, fill='k', reg=None)
                else:
                    sh.line([q0 + [-0.85, -0.85], q0 + [0.85, 0.85]], 'thin', reg=None); sh.line([q0 + [-0.85, 0.85], q0 + [0.85, -0.85]], 'thin', reg=None)
                tp = q0 + np.array([1.8, -4.0])
                w, hh, y0 = sh.text_size(letter, self.LABEL_H)
                for off in ((1.8, -4.0), (-3.5, -4.0), (1.8, 2.2), (-3.5, 2.2), (3.0, -1.0), (-5.0, -1.0), (4.0, -6.0), (-6.0, -6.0)):
                    t2 = q0 + np.array(off)
                    b2 = sbox(t2[0] - 0.3, t2[1] + y0 - 0.3, t2[0] + w + 0.3, t2[1] + y0 + hh + 0.3)
                    if not any(g.intersects(b2) for g in v['lines']) and not any(checks._inter(b, [t2[0], t2[1] + y0, t2[0] + w, t2[1] + y0 + hh], 0.2) for b in boxes):
                        tp = t2
                        break
            else:
                u = u2(d2)
                tip = q0 + u * self.AXIS_MM
                sh.line([q0, tip], 'thin'); sh.arrow(tip, u, L=2.0)
                lines.append(LineString([q0, tip]))
                tp = tip + u * 1.2 + np.array([-0.6, -1.0 + 1.2 * u[1]])
                n = np.array([-u[1], u[0]])
                w, hh, y0 = sh.text_size(letter, self.LABEL_H)
                bx = lambda t: sbox(t[0] - 0.3, t[1] + y0 - 0.3, t[0] + w + 0.3, t[1] + y0 + hh + 0.3)
                # letter not on a body edge, another axis or another axis letter (first free spot);
                # in oblique views (isometric) two axes often point almost the same way, otherwise their letters would overlap
                axes_other = [g for g in lines[:-1]]
                taken = lambda t: (any(g.intersects(bx(t)) for g in v['lines']) or any(g.intersects(bx(t)) for g in axes_other)
                                   or any(checks._inter(b, [t[0], t[1] + y0, t[0] + w, t[1] + y0 + hh], 0.2) for b in boxes))
                body = [g for g in v['lines'] if g.distance(bx(tp)) < 1.0]
                if body or taken(tp):
                    for dist, side in ((1.2, 0), (1.2, 2.5), (1.2, -2.5), (3.5, 0), (3.5, 3.0), (3.5, -3.0), (6.0, 0),
                                       (6.0, 3.5), (6.0, -3.5), (8.5, 0), (8.5, 4.0), (8.5, -4.0)):
                        t2 = tip + u * dist + n * side + np.array([-0.6, -1.0])
                        if not taken(t2):
                            tp = t2
                            break
            w, hh, y0 = sh.text_size(letter, self.LABEL_H)
            sh.text(tp[0], tp[1], letter, self.LABEL_H)
            boxes.append([tp[0], tp[1] + y0, tp[0] + w, tp[1] + y0 + hh])
        sh.owner = None
        return boxes, lines

    def bend_labels(self, sh, pid, bt, v, o3, moves, all_views=None):
        """Point marks P0.. of the bend table in view v with the axis cross at the origin o3. Each label stands at the point (default
        +2/+2 mm) or, if another label, another point, a body edge or the axis cross is there, at the first free
        spot of a fixed search grid (radius 3..14 mm, 16 directions) with a leader line; order of the points, hence
        deterministic. moves (label_to) pins a label as long as there is free space there (otherwise search as without)."""
        boxes, axlines = self.axis_cross(sh, v, o3, pid)
        pts = [(p['name'], self.P(v, np.asarray(p['xyz']))) for p in bt['points']]
        edges = [g for w in (all_views or {v['id']: v}).values() for g in w['drawn']] + axlines   # contour, centre lines, hidden edges of all views of the sheet
        edges.extend(LineString(it['pts']) for it in sh.items if it['kind'] == 'line' and
                     str(it['owner']).startswith('ann:') and len(it['pts']) >= 2)
        tree = STRtree(edges) if edges else None
        taken = list(boxes) + [it['bbox'] for it in sh.items if it['kind'] == 'text' and
                               str(it['owner']).startswith('ann:')]
        for name, q in pts:
            w, hh, y0 = sh.text_size(name, self.LABEL_H)
            sh.owner = f'label:{pid}:{name}'

            def box(d):
                return [q[0] + d[0], q[1] + d[1] + y0, q[0] + d[0] + w, q[1] + d[1] + y0 + hh]

            def free(d):
                b = box(d)
                pad = sbox(b[0] - 0.3, b[1] - 0.3, b[2] + 0.3, b[3] + 0.3)
                if any(checks._inter(b, t, 0.3) for t in taken):
                    return False
                if any(r is not q and pad.contains(Point(r)) for _, r in pts):
                    return False
                wide = sbox(b[0] - self.CLEAR, b[1] - self.CLEAR, b[2] + self.CLEAR + 0.05 * w, b[3] + self.CLEAR)   # whole text box with margin (Agg: text up to 5 % wider)
                return not (tree is not None and any(edges[int(i)].intersects(wide) for i in tree.query(wide)))
            if name in moves and free(np.asarray(moves[name], float)):         # fixed request (label_to, also from autofix) only if there is free space there
                d = np.asarray(moves[name], float)
            else:
                ring = [r * np.array([np.cos(a), np.sin(a)]) for r in (3.0, 4.5, 6.0, 8.0, 11.0, 14.0) for a in np.radians(45 + 22.5 * np.arange(16))]
                # left of the point the label stands right-aligned in front of it (text start shifted left by the width), not above it
                cands = [np.array([2.0, 2.0])] + [c - np.array([w, 0.0]) if c[0] < -0.5 else c for c in ring]
                d = next((c for c in cands if free(c)), cands[0])
            sh.circle(q, 0.45, fill='k', reg=None)
            sh.text(q[0] + d[0], q[1] + d[1], name, self.LABEL_H)
            taken.append(box(d))
            if np.hypot(*d) > 2.9 or d[0] < 0:            # not at the default (+2/+2): leader line to the nearest point of the text box
                bx = box(d)
                tgt = np.array([min(max(q[0], bx[0]), bx[2]), min(max(q[1], bx[1]), bx[3])])
                n = np.hypot(*(tgt - q))
                if n > 0.8:
                    sh.line([q, q + (tgt - q) * (1 - 0.5 / n)], 'thin')
        sh.owner = None

    # ------------------------------------------------------------------ Tables
    def table(self, sh, t, views, info):
        sh_name = f"table:{t.get('name', t.get('title', 'Table'))}"
        if t.get('bend_table'):
            pid = t['bend_table']
            bt = self.bend(pid)
            if not bt:
                raise AnnotationError(f'Bend table: part {pid} has no wire/tube sweep (no torus faces)')
            o = np.asarray(t.get('origin', [0, 0, 0]), float)
            nd = int(t.get('decimals', 2))
            rows = []
            for p in bt['points']:
                x, y, z = np.asarray(p['xyz']) - o
                rows.append([p['name'], fmt(x, nd), fmt(y, nd), fmt(z, nd), fmt(p['radius'], nd) if p['radius'] else '-'])
            cols = [{'title': 'Point', 'width': 14}, {'title': 'X', 'width': 20}, {'title': 'Y', 'width': 20}, {'title': 'Z', 'width': 20}, {'title': 'R', 'width': 14}]
            title = t.get('title', f"Bend table wire Ø{fmt(bt['wire_diameter'])} (centre line)")
            subtitle = t.get('subtitle')
            if t.get('labels_in'):
                v = views.get(t['labels_in'])
                if v is None:
                    raise AnnotationError(f"Bend table: view {t['labels_in']} does not exist on this sheet")
                self.bend_labels(sh, pid, bt, v, o, t.get('label_to') or {}, views)
                subtitle = subtitle or f"Coordinates relative to the axis cross in view {self.view_name(v)}"
            sh.table(t['at'][0], t['at'][1], cols, rows, title=title, subtitle=subtitle, name=sh_name)
            info['dims_parts'].add(pid)
            return {'type': 'bend_table', 'part': pid, 'rows': rows, 'centerline_length': bt['centerline_length'], 'complete': bt['complete']}
        sh.table(t['at'][0], t['at'][1], t['columns'], t['rows'], title=t.get('title'), subtitle=t.get('subtitle'), name=sh_name)
        return {'type': 'table', 'title': t.get('title')}


def _png_dpi(w, h, long_px=1600):
    return float(min(220.0, long_px * 25.4 / max(w, h)))


def _compose(dr, spec, si, sd, acc):
    """Build sheet si (frame, title block, change block, parts list, views, annotations, tables, notes) and
    check it (checks.sheet_findings); info receives format and content_mm2 (checks.drawing_findings). Findings,
    annotation records, view frames, valid tables and note lines go to acc. render and layout_probe build every sheet with this, so that their sheet findings are equal; dr.slim:
    sheet without matplotlib, without hidden edges/centre lines as lines and without foot point check. -> (sh, views, info)"""
    from sheet import FORMATS
    findings, n, strict = acc['findings'], len(spec['sheets']), bool(spec.get('strict'))
    if sd.get('format', 'A3') not in FORMATS or sd.get('orientation', 'landscape') not in ('landscape', 'portrait'):
        findings.append(checks._f('sheet_error', 'block', si, None, f"Sheet {si + 1}: format {sd.get('format')!r}/orientation {sd.get('orientation')!r} invalid (A4..A0, landscape|portrait)"))
        sd = {**sd, 'format': sd['format'] if sd.get('format') in FORMATS else 'A3', 'orientation': 'landscape'}
    sh = Sheet(sd.get('format', 'A3'), sd.get('orientation', 'landscape'), draw=not dr.slim)
    sh.frame()
    title = dict(spec.get('title') or {})
    title.update(sd.get('title') or {})
    title.setdefault('scale', sd.get('scale', '1:1'))
    title.setdefault('sheet', f'{si + 1}/{n}')
    ytop, _, _ = sh.title_block(title, spec.get('company') or ['', '', ''])
    changes = sd.get('changes', spec.get('changes', []))
    ytop = sh.change_block([[c.get('index', ''), c.get('date', ''), c.get('name', ''), c.get('text', '')] if isinstance(c, dict) else c for c in changes], ytop, n=max(1, len(changes)))
    info = {'parts_shown': set(), 'balloons': set(), 'dims_parts': set(), 'has_parts_list': bool(sd.get('parts_list'))}
    if sd.get('parts_list'):
        ytop = sh.parts_list(sd['parts_list'], ytop)
    views = {}
    for vd in sd.get('views') or []:
        try:
            v = dr.build_view(sh, vd, sd.get('scale', '1:1'), views)
        except Exception as e:
            msg = str(e) if isinstance(e, AnnotationError) else f"View {vd.get('id')}: {type(e).__name__}: {e}"
            findings.append(checks._f('view_error', 'block', si, None, msg[:300], view=vd.get('id'))); continue
        views[v['id']] = v
        info['parts_shown'].update(v['parts'])
        pl = vd.get('place') or {}
        ref = views.get(pl.get('relative_to'))
        if ref and set(ref['parts']) == set(v['parts']) and not any(vd.get(t) for t in ('section','detail','auxiliary','label')):
            expected = checks.projection_sides(vd, ref['vd'])
            if expected and pl.get('side') not in expected:
                findings.append(checks._f('projection_arrangement', 'warn', si, v['bbox'],
                                          f"View {v['id']}: projection method 1 requires {'/'.join(sorted(expected))} relative to {ref['id']}", view=v['id']))
        acc['view_frames'][v['id']] = {'sheet': si, 'origin_mm': [round(float(x), 3) for x in v['origin']], 'scale': v['scale'],
                                       'axes': {'x': np.round(v['frame'][1], 6).tolist(), 'y': np.round(v['frame'][2], 6).tolist(), 'toward_camera': np.round(v['frame'][0], 6).tolist()},
                                       'bbox_mm': [round(x, 2) for x in v['bbox']], 'geom_bbox_mm': [round(x, 2) for x in v['geom_bbox']],      # (N1.3/N1.4): geometry without centre lines and heading
                                       'centerline_count': v['centerline_count'],
                                       'lines': len(v['lines']), 'hatch_lines': v['hatch_lines'], 'is_section': v['is_section']}
    findings += checks.view_empty_findings(si, [{'id': vid, 'label': v['vd'].get('label'), 'ink_mm': sum(g.length for g in v['lines']),
                                                 'hatch_lines': v['hatch_lines'], 'is_section': v['is_section'],
                                                 'is_detail': bool(v['vd'].get('detail')), 'bbox': v['bbox']}
                                                for vid, v in views.items()], strict)
    for a in sd.get('annotations') or []:            # centre marks of auxiliary views are drawn references (at_mark)
        if isinstance(a, dict) and a.get('type') == 'centermark' and a.get('view') in views and views[a['view']]['vd'].get('auxiliary'):
            try:
                views[a['view']].setdefault('marks', []).append(np.asarray(a['center'], float))
            except (KeyError, TypeError, ValueError):
                pass                     # missing/invalid center: annotate() reports it below as annotation_error
    for i, a in enumerate(list(sd.get('annotations') or []) + list(sd.get('dims') or [])):
        sh.owner = f'ann:{i}'
        info['notes'] = []
        if isinstance(a, dict) and a.get('type') in ('section_line', 'detail_circle', 'view_arrow'):
            sh.letter_owners.add(sh.owner)          # their label letters keep more distance to foreign lines (checks.sheet_findings)
        try:
            rec = dr.annotate(sh, a, views, info)
            rec['sheet'] = si; rec['index'] = i
            acc['annotations'].append(rec)
        except Exception as e:
            msg = str(e) if isinstance(e, AnnotationError) else (f"Field {e} missing" if isinstance(e, KeyError) else f"{type(e).__name__}: {e}")
            kind = a.get('type') if isinstance(a, dict) else '?'
            findings.append(checks._f('annotation_error', 'block', si, None, f"annotations[{i}] ({kind}): {msg}"[:300], index=i))
        for code, sev, msg in info.pop('notes', []):
            findings.append(checks._f(code, sev, si, None, f'annotations[{i}]: {msg}'[:300], index=i))
        sh.owner = None
    for t in sd.get('tables') or []:
        try:
            acc['annotations'].append({**dr.table(sh, t, views, info), 'sheet': si})
            acc['tables_ok'].append(t)
        except Exception as e:
            title = t.get('title', '') if isinstance(t, dict) else ''
            findings.append(checks._f('annotation_error', 'block', si, None, f"Table {title}: {type(e).__name__}: {e}"[:300]))
        sh.owner = None
    notes = sd.get('notes')
    if notes:
        if isinstance(notes, dict):
            lines, (nx, ny), nh = notes.get('lines', []), notes.get('at', [sh.area[0] + 5, sh.area[1] + 5]), float(notes.get('h', 3.5))
        else:
            lines, nh = list(notes), 3.5
            nx, ny = sh.area[0] + 5, sh.area[1] + 5 + (len(lines) - 1) * 1.6 * nh
        sh.owner = 'notes'
        for j, line in enumerate(lines):
            text = checks.note_text(line)               # line: text or object {text?, general?, model_ref?}
            sh.text(nx, ny - j * 1.6 * nh, text, nh)
            claims = {k: line[k] for k in ('general', 'model_ref', 'draft', 'minor') if isinstance(line, dict) and line.get(k) is not None}
            if claims:                                  # general statement/reference: checks the coverage, not a free note
                acc.setdefault('note_geos', []).append({'kind': 'note', 'view': None, 'parts': [], 'text': text, 'sheet': si,
                                                        'index': None, 'line': j, **claims})
            else:
                acc['note_lines'].append(text)
        sh.owner = None
    for vid, (inside, n) in checks.dims_inside(sh, {vid: {'bbox': v['geom_bbox']} for vid, v in views.items()}, _owner_view(sd, views)).items():
        acc['view_frames'][vid].update(dims_inside=inside, dims_n=n)         # (N3.6): dimension texts inside the view; probe and render compute the same
    ann_box = {}                                                             # (N1.3): envelope of all drawing objects of the annotations of a view (dimension space of the block)
    owner_view = _owner_view(sd, views)
    for it in sh.items:
        vid = owner_view.get(it.get('owner'))
        if vid in acc['view_frames']:
            b = it['bbox']
            ann_box[vid] = [min(ann_box[vid][0], b[0]), min(ann_box[vid][1], b[1]), max(ann_box[vid][2], b[2]), max(ann_box[vid][3], b[3])] if vid in ann_box else [float(x) for x in b]
    for vid, b in ann_box.items():
        acc['view_frames'][vid]['ann_bbox_mm'] = [round(x, 2) for x in b]
    view_lines = [(vid, g) for vid, v in views.items() for g in v['lines']]
    table_views = {f"table:{t.get('name', t.get('title', 'Table'))}": t['labels_in'] for t in sd.get('tables') or [] if t.get('labels_in')}
    findings += checks.sheet_findings(sh, si, {vid: {'bbox': v['bbox']} for vid, v in views.items()}, view_lines, strict, table_views)
    info.update(format=sh.format, content_mm2=checks.content_area(sh, views))
    return sh, views, info


MISSING_CAP = 40                 # coverage.missing in the response: otherwise the service exceeds the 32 KB limit for large gaps (about 2300 entries, BUDGET_EXCEEDED)


def cap_missing(coverage, cap=MISSING_CAP):
    """coverage with at most cap entries in missing and the total number in missing_total (without coverage still None)."""
    if not coverage:
        return coverage
    miss = list(coverage.get('missing') or [])
    return {**coverage, 'missing': miss[:cap], 'missing_total': len(miss)}


def _owner_view(sd, views):
    """{'ann:i': view} of the annotations of a sheet (owners as in _compose: annotations, then dims) with a built view."""
    anns = list(sd.get('annotations') or []) + list(sd.get('dims') or [])
    return {f'ann:{i}': a['view'] for i, a in enumerate(anns) if isinstance(a, dict) and a.get('view') in views}


def _placement_findings(sh, si, sd, views):
    """Placement measurement codes (warn only): crossings by line type, detached dimension text, dimension line spacing (full render only;
    dims_inside and dims_n per view are already in view_frames from _compose)."""
    owner_view = _owner_view(sd, views)
    view_lines = [(vid, g) for vid, v in views.items() for g in v['lines']]
    return (checks.crossing_kinds_findings(sh, si) + checks.dim_text_detached_findings(sh, si)
            + checks.dim_spacing_findings(sh, si, view_lines, owner_view))


def _blocks(sh):
    return {name: [round(float(x), 2) for x in bb] for name, bb in sh.blocks.items()}


def _measured(sh, views):
    """Measured extent for the planner (layout_probe only): ann_boxes = {annotation index: envelope of all its
    drawing objects}, view_boxes = {view: geom (geometry), box (with centre lines, without heading)} in sheet mm."""
    ann = {}
    for it in sh.items:
        o = str(it.get('owner') or '')
        if o.startswith('ann:'):
            b, i = it['bbox'], o[4:]
            ann[i] = [min(ann[i][0], b[0]), min(ann[i][1], b[1]), max(ann[i][2], b[2]), max(ann[i][3], b[3])] if i in ann else [float(x) for x in b]
    vb = {}
    for vid, v in views.items():
        box = [float(x) for x in v['bbox']]
        if v['vd'].get('label'):
            box[3] -= 10.0                      # heading (5 mm distance + 5 mm lettering, build_view): the planner adds it itself
        geom = np.asarray(v['geom_bbox'])
        model_bounds = (geom.reshape(2, 2) - v['origin']) / v['scale']
        vb[vid] = {'geom': geom.tolist(), 'box': box, 'model_bounds': model_bounds.ravel().tolist()}
    return {'ann_boxes': {i: [round(x, 2) for x in b] for i, b in ann.items()}, 'view_boxes': vb}


def render(spec, session=None):
    """Render the drawing set: PDF, PNG per sheet, crops, all findings. session: Drawing of an earlier render or
    of a probe of the same B-Rep, parts and parts list (see layout_probe), otherwise a new one."""
    dr = session if session is not None else Drawing(spec)
    dr.slim = False
    out_dir = spec.get('out_dir') or os.path.join(os.path.dirname(spec['brep']), 'drawing')
    os.makedirs(out_dir, exist_ok=True)
    pdf_path = os.path.join(out_dir, 'drawing.pdf')
    acc = {'findings': [], 'annotations': [], 'view_frames': {}, 'tables_ok': [], 'note_lines': []}   # tables, notes: coverage
    sheets_out, crops_out, placement = [], [], []
    rendered = {'sheets': []}
    strict = bool(spec.get('strict'))       # local path: new findings block
    title_meta = (spec.get('title') or {}).get('title', 'Drawing')
    with PdfPages(pdf_path, metadata={'Title': title_meta, 'Creator': 'LLcad drawing'}) as pdf:
        for si, sd in enumerate(spec['sheets']):
            sh, views, info = _compose(dr, spec, si, sd, acc)
            acc['findings'] += checks.leader_crossing_findings(sh, si)
            placement += _placement_findings(sh, si, sd, views)           # at the end of the findings list (after feature, coverage and sheet findings)
            rendered['sheets'].append(info)
            pdf.savefig(sh.fig)
            dpi = _png_dpi(sh.w, sh.h)
            png = os.path.join(out_dir, f'sheet-{si + 1}.png')
            sh.save(png_path=png, dpi=dpi)
            sheets_out.append({'index': si, 'png': png, 'format': sh.format, 'orientation': sh.orientation,
                               'width_px': int(round(sh.w / 25.4 * dpi)), 'height_px': int(round(sh.h / 25.4 * dpi)), 'size_mm': [sh.w, sh.h],
                               'blocks': _blocks(sh)})
            for ci, c in enumerate(spec.get('crops') or []):
                if int(c.get('sheet', 0)) != si:
                    continue
                crops_out.append(_crop(sh, c, os.path.join(out_dir, f'crop-{si + 1}-{ci + 1}.png')))
            sh.close()
    findings, annotations, tables_ok = acc['findings'], acc['annotations'], acc['tables_ok']
    findings += checks.drawing_findings(spec, rendered)
    findings += checks.section_findings(spec, strict, dr.features) + checks.view_information_findings(spec, strict) + checks.budget_findings(spec, strict) + checks.section_mark_findings(spec, strict) + checks.view_unnamed_findings(spec, strict)
    geos = [{**a['_geo'], 'sheet': a['sheet'], 'index': a['index']} for a in annotations if '_geo' in a] + acc.get('note_geos', [])
    notes_text = checks.notes_text(acc['note_lines'], annotations)          # without block titles and coverage claims
    fbp = {}
    try:
        fbp = {p['id']: dr.features(p['id']) for p in dr.parts if checks.part_role(p) == 'manufactured'}
        sym, sym_error = None, None
        try:                                # symmetry planes before the evaluation (virtual mirror dimensions of the coverage); an error there only costs the mirror dimensions and the N2/N5 measurement
            sym = dr.symmetry_planes(fbp)
        except Exception as e:
            sym_error = e
        ev = checks._evaluate(spec, fbp, geos, notes_text, tables_ok, strict, sym, acc['view_frames'])      # one run for coverage, feature findings and N2/N5 measurement (strict only changes status/problems)
        coverage = cap_missing(checks.coverage(spec, fbp, geos, notes_text, tables_ok, strict, ev=ev))
        ff = checks.feature_findings(spec, fbp, geos, notes_text, tables_ok, strict, ev=ev)
        findings += ff
        findings += checks.small_feature_findings(spec, fbp)
        try:                                # (N4.10, warn only): profile shapes; metrics profiles/profiles_dimensioned in coverage (coverage and blocking stay)
            pf_, pm_ = checks.profile_findings(spec, fbp, geos, ev, acc['view_frames'], strict)
            findings += pf_
            if coverage is not None:
                coverage = {**coverage, **pm_}
        except Exception as e:
            findings.append(checks._f('coverage_error', 'warn', 0, None, f'Profile check: {type(e).__name__}: {e}'[:300]))
        for pid_, pf_ in fbp.items():      # if the profile detection of a part failed (features.profiles_error, not cached), N4.10 is unchecked there - reported like a check error, not silently
            if pf_.get('profiles_error'):
                findings.append(checks._f('coverage_error', 'warn', 0, None, f"Profile detection {pid_}: {pf_['profiles_error']} - profile shapes (N4.10) unchecked"[:300], part=pid_))
        try:                                # (N2, N5): measurement warn only; an error here does not take the other checks down with it
            if sym_error is not None:
                raise sym_error
            findings += checks.n2_n5_findings(spec, fbp, geos, notes_text, tables_ok, frames=acc['view_frames'], sym=sym, prior=ff, ev=ev)
        except Exception as e:
            findings.append(checks._f('coverage_error', 'warn', 0, None, f'N2/N5 measurement: {type(e).__name__}: {e}'[:300]))
        try:                                # (N4.9, N4.4): pattern excerpt (warn) and metric detail_scale_steps per detail view (in view_frames); an error here only costs this measurement
            findings += checks.pattern_findings(ev, spec, acc['view_frames'])
            for vid_, n_ in checks.detail_scale_steps(ev, spec, acc['view_frames']).items():
                if vid_ in acc['view_frames']:
                    acc['view_frames'][vid_]['detail_scale_steps'] = n_
        except Exception as e:
            findings.append(checks._f('coverage_error', 'warn', 0, None, f'Pattern excerpt measurement: {type(e).__name__}: {e}'[:300]))
        try:                                # (N4.8, N4.3): replaceable view and foreshortened dimension (warn, without assignment); an error here only costs this measurement
            findings += checks.replaceable_findings(ev, spec, acc['view_frames'])
            findings += checks.foreshortened_findings(spec, acc['view_frames'], ev['items'])
        except Exception as e:
            findings.append(checks._f('coverage_error', 'warn', 0, None, f'View replacement/foreshortening measurement: {type(e).__name__}: {e}'[:300]))
        # tags on other shown parts (not a manufactured part): their form features only if a tag carries their ID; if
        # the detection fails there, their tags stay unchecked (None) - the coverage does not depend on these parts
        every, other = [p['id'] for p in dr.parts], {}
        for pid in sorted({pid for g in geos if g.get('feature') for pid in g.get('parts') or every
                           if pid not in fbp and str(g['feature']).startswith(pid + '#')}):
            try:
                other[pid] = dr.features(pid)
            except Exception:
                other[pid] = None
        findings += checks.feature_tag_findings(spec, {**fbp, **other}, geos, strict)
    except Exception as e:                  # form feature detection must not abort the render
        coverage = None
        findings.append(checks._f('coverage_error', 'block' if strict else 'warn', 0, None, f'Coverage check: {type(e).__name__}: {e}'[:300]))
    try:                                    # (group N1): scales, main part, part scale; warn only, must not abort the render
        wires = {pid for pid in fbp if checks.wire_part(dr.bend(pid))}           # same definition as the planner
        findings += checks.n1_findings(spec, acc['view_frames'], coverage, fbp, wires)
    except Exception as e:
        findings.append(checks._f('coverage_error', 'warn', 0, None, f'N1 check: {type(e).__name__}: {e}'[:300]))
    try:                                    # (N1.8): calm grid; also marks the view_frames (grid_block, grid_lonely), from which the bench computes the metrics
        findings += checks.grid_findings(spec, acc['view_frames'])
    except Exception as e:
        findings.append(checks._f('coverage_error', 'warn', 0, None, f'Grid check: {type(e).__name__}: {e}'[:300]))
    try:                                    # (N1.9): the assembly does not stand between dimensioned part views
        findings += checks.assembly_zone_findings(spec, acc['view_frames'])
    except Exception as e:
        findings.append(checks._f('coverage_error', 'warn', 0, None, f'Assembly zone check: {type(e).__name__}: {e}'[:300]))
    try:                                    # (N4.7): the part title stands at the block of the main view
        findings += checks.part_title_findings(spec, acc['view_frames'])
    except Exception as e:
        findings.append(checks._f('coverage_error', 'warn', 0, None, f'Part title check: {type(e).__name__}: {e}'[:300]))
    try:                                    # (N1.7): views of a part are aligned on their projection lines
        findings += checks.projection_findings(spec, acc['view_frames'])
    except Exception as e:
        findings.append(checks._f('coverage_error', 'warn', 0, None, f'Projection check: {type(e).__name__}: {e}'[:300]))
    findings += placement
    annotations = [{k: v for k, v in a.items() if not k.startswith('_')} for a in annotations]
    cache.prune(spec.get('cache_dir'))
    cache.prune(spec.get('feature_cache_dir'), 1000, '.json')
    return {'cache': dict(dr.cache_stats), 'feature_cache': dict(dr.feature_stats), 'pdf': pdf_path, 'sheets': sheets_out,
            'findings': findings, 'annotations': annotations, 'view_frames': acc['view_frames'], 'crops': crops_out,
            'coverage': coverage, 'blocking': sum(1 for f in findings if f['severity'] == 'block')}


def layout_probe(spec, session=None, place_labels=False):
    """Lean probe of the layout repair: builds the sheets like render (_compose), but without image (no matplotlib, PDF,
    PNG, crop) and without crossing, placement, N1, foot point and coverage checks (leader_crossing, checks.PLACEMENT_CODES, scale_not_standard,
    main_part_small, part_scale_not_max, grid_misaligned, assembly_between, part_title_misplaced, pattern_not_detailed, view_replaceable, dim_foreshortened, projection_misaligned, dim_point_off_geometry,
    dim_on_hidden_edge, feature_findings, small_feature_findings, feature_tag_unknown; the last four do not depend on the
    position). findings are the remaining findings in render order: sheet findings (including all
    layout findings, autofix.LAYOUT, and view_empty), drawing (also sheets_mergeable), section and view findings;
    blocking counts only these findings;
    annotations, view_frames and sheets (index, format, orientation, blocks) as in render. session: a Drawing of the same
    B-Rep, parts and parts list (from render or layout_probe); it holds the built views (content without position: HLR,
    sections, centre lines) and parts_at, valid for specs that differ only in position fields (moved
    views are re-placed with the same content). Without session a new Drawing.
    The probe does not compute dim_on_hidden_edge: the occlusion check needs the hidden edges per view (securing the part scales with two
    such probes: planning time 26 s -> 336 s); it is reserved for the full render and `score`."""
    dr = session if session is not None else Drawing(spec)
    dr.slim = True
    acc = {'findings': [], 'annotations': [], 'view_frames': {}, 'tables_ok': [], 'note_lines': []}
    sheets_out, rendered = [], {'sheets': []}
    try:
        for si, sd in enumerate(spec['sheets']):
            page = {'findings': [], 'annotations': [], 'view_frames': {}, 'tables_ok': [], 'note_lines': []}
            sh, views, info = _compose(dr, spec, si, sd, page)
            if place_labels:
                import label_layout
                if label_layout.place(dr, sd, sh, views, page['findings']):
                    sh.close()
                    page = {'findings': [], 'annotations': [], 'view_frames': {}, 'tables_ok': [], 'note_lines': []}
                    sh, views, info = _compose(dr, spec, si, sd, page)
            for key, values in page.items():
                if key == 'view_frames':
                    acc[key].update(values)
                else:
                    acc.setdefault(key, []).extend(values)
            rendered['sheets'].append(info)
            sheets_out.append({'index': si, 'format': sh.format, 'orientation': sh.orientation, 'blocks': _blocks(sh), **_measured(sh, views)})
            sh.close()
    finally:
        dr.slim = False
    acc['findings'] += checks.drawing_findings(spec, rendered)
    acc['findings'] += checks.section_findings(spec, bool(spec.get('strict')), dr.features) + checks.view_information_findings(spec, bool(spec.get('strict'))) + checks.budget_findings(spec, bool(spec.get('strict'))) + checks.section_mark_findings(spec, bool(spec.get('strict')))
    return {'findings': acc['findings'], 'blocking': sum(1 for f in acc['findings'] if f['severity'] == 'block'),
            'annotations': [{k: v for k, v in a.items() if not k.startswith('_')} for a in acc['annotations']],
            'view_frames': acc['view_frames'], 'sheets': sheets_out}


def _crop(sh, c, path, long_px=1600):
    x0, y0, x1, y1 = map(float, c['box_mm'])
    x0, x1 = sorted((max(0, x0), min(sh.w, x1))); y0, y1 = sorted((max(0, y0), min(sh.h, y1)))
    bw, bh = max(x1 - x0, 1), max(y1 - y0, 1)
    size = sh.fig.get_size_inches().copy()
    sh.ax.set_xlim(x0, x1); sh.ax.set_ylim(y0, y1)
    sh.fig.set_size_inches(bw / 25.4, bh / 25.4)
    dpi = min(1200.0, long_px * 25.4 / max(bw, bh))
    sh.fig.savefig(path, dpi=dpi)
    sh.fig.set_size_inches(*size); sh.ax.set_xlim(0, sh.w); sh.ax.set_ylim(0, sh.h)
    return {'sheet': int(c.get('sheet', 0)), 'box_mm': [x0, y0, x1, y1], 'png': path}


def attached_centerlines(centers, vis, k):
    """A centre line entirely outside the visible geometry (envelope of the visible edges + 0.5 mm paper) has no reference and is dropped
    (a 4 mm dash to the right of the section face; the dash length also stretched the envelope of the view). centers = [(points n x 2 in image coordinates, kind)]."""
    if not vis:
        return centers
    allv = np.vstack([np.asarray(p, float) for p in vis])
    lo, hi = allv.min(0) - 0.5 / k, allv.max(0) + 0.5 / k
    return [(q, kind) for q, kind in centers if (np.asarray(q, float).max(0) >= lo).all() and (np.asarray(q, float).min(0) <= hi).all()]


def _label_shift(a):
    """Offset [dx, dy] (mm) of the identifier letter of a section line, detail or view arrow; autofix moves it out of collisions."""
    try:
        dx, dy = (float(v) for v in a.get('label_shift', (0.0, 0.0)))
    except (TypeError, ValueError):
        return np.zeros(2)
    return np.array([dx, dy])


def spec_key(sheets):
    """Identifier of a run: hash of the sheets of the input spec (canonical JSON). tools/drawing_bench.py computes the same function (spec_key)."""
    import hashlib
    return hashlib.sha1(json.dumps(sheets, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()[:16]


def dump_findings(out_dir, res, action, key, brep=None):
    """Complete findings and annotations of a render next to the working folder (the service caps to 40/60 and cleans up the folder):
    <out_dir>/../last-findings-<action>-<key>.json, for the bench only (findings.json). Every run has its own file (key = spec_key of the
    input, action = render|autofix), parallel runs share none; the file names key, action and the B-Rep file (model state). A
    write error never disturbs the render."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(out_dir)), f'last-findings-{action}-{key}.json'), 'w', encoding='utf-8') as f:
            json.dump({'spec_key': key, 'action': action, 'brep': os.path.basename(brep) if brep else None,
                       'findings': res.get('findings') or [], 'annotations': res.get('annotations') or []}, f, ensure_ascii=False,
                      default=lambda o: o.tolist() if hasattr(o, 'tolist') else str(o))
    except Exception:
        pass


def main(argv):
    if len(argv) != 3 or argv[1] not in ('analyze', 'render', 'crop', 'plan', 'autofix'):
        print(json.dumps({'error': 'usage: drawing.py analyze|render|crop|plan|autofix <spec.json>'})); return 2
    try:
        with open(argv[2], encoding='utf-8') as f:
            spec = json.load(f)
        key = spec_key(spec.get('sheets'))                  # before the repair: identifier of the input
        if argv[1] == 'plan':
            sys.modules.setdefault('drawing', sys.modules[__name__])     # the planner measures with this module (layout_probe)
            import plan
            res = plan.plan_spec(spec)
        elif argv[1] == 'autofix':
            sys.modules.setdefault('drawing', sys.modules[__name__])     # autofix renders with this module
            import autofix
            res = autofix.repair(spec)
        else:
            res = analyze(spec) if argv[1] == 'analyze' else render(spec)
        if argv[1] in ('render', 'autofix') and spec.get('out_dir'):
            dump_findings(spec['out_dir'], res, argv[1], key, spec.get('brep'))
    except Exception as e:  # errors as JSON so that the service can report them cleanly
        import traceback
        print(json.dumps({'error': f'{type(e).__name__}: {e}', 'trace': traceback.format_exc()[-2000:]})); return 1
    sys.stdout.write(json.dumps(res, default=lambda o: o.tolist() if hasattr(o, 'tolist') else str(o)))   # ASCII-safe
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
