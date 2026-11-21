"""Planner V: plan_spec - complete drawing candidates from B-rep, facts and IR hints.

Input {brep, parts: [{id, name, solid_indices, semantic_name?, description?, feature_names?}], single_root, model:
{id, name, purpose, revision}, facts: {title?, parts?: [{id, designation?, material?, form?, role?, tolerances?}]},
ir_hints, semi_finished, title_defaults, company, max_candidates}; output {candidates: [{candidate_id, spec, metrics}],
parts, notes, open}. Rules:
- Facts: per part plan_facts.regex_facts from semantic_name (otherwise name), description and feature_names; facts supplied
    by the caller take precedence, regex_facts only fills gaps (role: marker in the part name before facts).
- Roles by plan_roles.roles, quantity = congruent single bodies (features.quantity), class and datum per part;
    form features including patterns and IR hints for the drawn parts (part['freeform'] for plan_dims).
- Identical parts: a part with an item number that is congruent to an earlier part of the same role, the same material and the same
    form (same_part: fingerprint, surface, face signature, proper motion - mirror images
    not), is a further instance: spec role duplicate with same_as, no item number or view of its own, quantity at the
    first part; in the assembly it stays visible and gets the same item number.
- Assembly = at least 2 parts without duplicate: assembly with item numbers and parts list, plus every
    manufactured part with part_views + dimension; otherwise the single part. Item 1... first manufactured (volume descending), then
    standard, then purchased (each in input order); reference and duplicate get no item number.
- Texts only from facts (verbatim from model texts), algorithms and tables; if something is missing, the field stays empty and
    is listed under open. Title block see _title (revision index only from title_defaults, the model revision appears as
    note line "3D model <name>, revision <rev8>"); parts list: designation from the facts, otherwise part name without color code,
    material and quantity; semi-finished product/standard only from semi_finished (single part: note line "Semi-finished product: ..."); a
    tolerance that cannot be assigned unambiguously to a dimension appears as a note and under open.
- Titles: title line per block as text above the block ("Assembly", in assemblies "Item n designation
    (scale)" - a view title would sit fixed above the geometry and collide with the labels of
    section lines and detail circles), marked block_title: true (no note text for the coverage,
    checks.notes_text), on views "A-A" (own scale: "A-A (5:1)") and "Detail B (5:1)";
    notes of the dimensioning in assemblies with "Item n: ".
- Part scales (N1.3/N1.4): _search chooses the format and sheet scale (one scale for all blocks); then _part_scales gives each
    manufactured part (not wire parts) its own standard scale up to PART_UP_STEPS steps above, the main part (most required elements, checks.main_part) first;
    the assembly keeps the sheet scale unless N1.4 demands a smaller one (_main_large). The part cache (_part_content) computes views and
    dimensioning once per (part, scale, main view), _variant arranges each block in up to three ways (sheet-aware, tall, wide).
- Variants: proposal (analyze.suggestion; if it does not fit on a sheet, first the larger sizes up to A0, then
    follow-up sheets, then the next smaller scale), next larger size, next smaller scale in the smallest
    fitting size, alternative main view of the largest drawn part; selection in plan_layout.candidates."""
import copy
import itertools
import json
import re
import sys
import time
from collections import defaultdict
import numpy as np
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepTools import BRepTools
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepTopAdaptor import BRepTopAdaptor_FClass2d
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_IN, TopAbs_OUT, TopAbs_REVERSED, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Dir, gp_Pnt, gp_Pnt2d
import analyze as an
import checks
import features as ft
import plan_dims as pd
import plan_facts as pfx
import plan_layout as pl
import plan_roles as pr
import plan_views as pv
from sheet import fmt, parts_list_height, sheet_size, usable_area
import projection as prj

LETTERS = 'ABCDEFGHJKLMNPRSTUVWXYZ'      # label letters without I, O, Q
NOTE_H, NOTE_STEP = 3.5, 5.6             # note lines: text height, line spacing (core: 1.6 · h)
SECTION_LABEL_GAP, SECTION_STEP = 9.0, 5.0     # letters of two section lines at the same end at least this far apart; offset per step (mm on paper)
SECTION_OVER = 3.0                       # mm on paper: section line beyond the outline
NAMED_ARROW_GAP = 1.0                    # mm on paper: viewing arrow of a freely placed view sits close to the outline (room in the guest block)
ARROW_GAP = 6.0                          # mm on paper: tip of the viewing arrow of an auxiliary view in front of the outline
LABEL_NAME = 60                          # at most this many characters of the designation in a title
STACK, ROW_H = 39.0 + 6.0, 6.0           # title block + revision block (one row); row of the parts list
BEND_SUBTITLE = 4.0                        # mm: line "coordinates relative to axis cross in view ..." above the bend table (drawing.table)
EXTRA_BALLOON_VIEWS = ('back', 'right', 'bottom')   # further assembly views for parts that are hidden in the four views
PROBES = 48                              # probe points per part for item numbers
NAMED_ARROWS = True                      # freely placed views carry a name and viewing arrow (ISO 128-3)
LISTED = ('manufactured', 'standard', 'purchased')
_COLOR = re.compile(r'#[0-9A-Fa-f]{3,8}\b|\bRAL\s?\d{4}\b', re.I)
_QTY = re.compile(r'(?<![\w.,])\d+\s*[xX×](?!\w)|(?<![\w.,])[xX×]\s*\d+(?![\w.,])')   # like features.quantity
_NUMBER = re.compile(r'(?<!\d)\d{6,7}(?!\d)')
SAME_REL = 1e-3         # identical parts: volume, surface, faces per kind relatively equal (like features.congruent)
PROBE_EPS = 0.02        # mm, check points this far inside/outside next to the surface (same_part)
PROBE_FACES = 48        # until here a sample was used; now all faces carry check points
BUDGET_VIEWS = 2         # at most this many additional normal views per part for the overflow of the stack budget
FREE_GAP = checks.BLOCK_GAP   # block spacing when packing (pl.pack gap), margin of the free area for the sheet-aware arrangement (10 mm; one value with check N1.3)
SPREAD_TRIES = 2         # rounds in which views that are too close are moved apart after the measurement
MEASURED_MAX = 3         # this many of the best candidates (by the estimate) are measured
GUARD_GAIN = 2           # format guard only switches at this much fewer blocking findings (or to 0); autofix noise +-3
MAX_MERGE_FACES = 300   # more faces: do not merge (full check on a part with 3262 faces: 640 s)


FEATURE_RUN_MIN = 40      # runs from this length on are written as a range
FEATURE_LIST_MAX = 200     # longer form feature lists in the plan result are summarized per kind (response budget)


def _feature_ids(ids):
    """Ids of the form features of a part for the plan result. Up to FEATURE_LIST_MAX all. Above that, for kinds with more than
    FEATURE_RUN_MIN members, gaplessly numbered runs (consecutive numbers of the same kind) are written as one entry
    "<part>#<kind><first>..<last> (<count>)"; gaps (e.g. slant faces that fell into a facet region)
    end the run, runs below FEATURE_RUN_MIN and all other kinds stay single ids. The list can thus be reconstructed
    losslessly (range = every number from first to last), the response budget is kept (hundreds of cones)."""
    if len(ids) <= FEATURE_LIST_MAX:
        return ids
    kinds = {}
    for fid in ids:
        kinds.setdefault(fid.rstrip('0123456789'), []).append(fid)
    out = []
    for stem, mem in kinds.items():
        if len(mem) <= FEATURE_RUN_MIN or not all(m[len(stem):].isdigit() for m in mem):
            out += mem
            continue
        run = [mem[0]]
        for m in mem[1:] + [None]:
            if m is not None and int(m[len(stem):]) == int(run[-1][len(stem):]) + 1:
                run.append(m)
                continue
            out += run if len(run) < FEATURE_RUN_MIN else [f"{run[0]}..{run[-1].rsplit('#', 1)[-1]} ({len(run)})"]
            run = [m]
    return out


def _r(p):
    return [round(float(x), 4) + 0.0 for x in p]


def _letters():
    for n in range(100):
        for c in LETTERS:
            yield c if n == 0 else f'{c}{n}'


def _solids(shape):
    out, ex = [], TopExp_Explorer(shape, TopAbs_SOLID)
    while ex.More():
        out.append(ex.Current())
        ex.Next()
    return out or [shape]


# ------------------------------------------------------------------ identical parts

def _rel_eq(a, b, rel=SAME_REL):
    return abs(a - b) <= rel * max(abs(a), abs(b)) + 1e-9


def _signature(shape):
    """(surface, {face kind: (count, area)}): form feature signature at face level."""
    sig = defaultdict(lambda: [0, 0.0])
    total = 0.0
    for f in an.faces(shape):
        g = GProp_GProps(); BRepGProp.SurfaceProperties_s(f, g)
        k = int(BRepAdaptor_Surface(f).GetType())
        sig[k][0] += 1
        sig[k][1] += g.Mass()
        total += g.Mass()
    return total, dict(sig)


def _frame(shape):
    """(center of mass, principal axes as rows, principal moments) like features.fingerprint (equal moments: axes chosen fixed)."""
    g = GProp_GProps(); BRepGProp.VolumeProperties_s(shape, g)
    pp = g.PrincipalProperties()
    lam = np.array(pp.Moments(), float)
    E = np.array([an.v3(a) for a in (pp.FirstAxisOfInertia(), pp.SecondAxisOfInertia(), pp.ThirdAxisOfInertia())])
    c = an.v3(g.CentreOfMass())
    tol = 1e-6 * max(np.abs(lam).max(), 1e-12)
    same = [(i, j) for i, j in ((0, 1), (1, 2), (0, 2)) if abs(lam[i] - lam[j]) <= tol]
    if len(same) > 1:
        planes = [(x['area'], x['face']) for x in ft._face_table(shape) if x['kind'] == 'plane']
        if planes:
            _, n = an.face_point_normal(max(planes, key=lambda a: a[0])[1])
            E = np.array([*ft._perp(prj.unit(n)), prj.unit(n)])
            E = ft._axes_in_degenerate_plane(shape, E, c, 0, 1)
    elif same:
        E = ft._axes_in_degenerate_plane(shape, E, c, *same[0])
    return c, E, lam


def _surface_probes(shape):
    """[(point, outward normal)] on all faces, per face a 2 x 2 grid (otherwise 5 x 5) in the interior."""
    faces = list(an.faces(shape))
    pick = range(len(faces))        # all faces: a sample would overlook a small offset feature
    out = []
    for f in (faces[i] for i in pick):
        u0, u1, v0, v1 = BRepTools.UVBounds_s(f)
        cls = BRepTopAdaptor_FClass2d(f, 1e-7)
        s = BRepAdaptor_Surface(f)
        for n in (2, 5):
            got = 0
            for i, j in itertools.product(range(n), range(n)):
                u, v = u0 + (u1 - u0) * (i + 0.5) / n, v0 + (v1 - v0) * (j + 0.5) / n
                if cls.Perform(gp_Pnt2d(u, v)) != TopAbs_IN:
                    continue
                p = BRepLProp_SLProps(s, u, v, 1, 1e-7)
                if not p.IsNormalDefined():
                    continue
                nn = an.v3(p.Normal())
                out.append((an.v3(p.Value()), -nn if f.Orientation() == TopAbs_REVERSED else nn))
                got += 1
            if got:
                break
    return out


def _covers(probes, R, t, cls):
    """Do all check points (point -/+ PROBE_EPS along the normal), moved by x -> R x + t, lie inside or outside of the
    target (cls: one classifier per body)?"""
    def state(q):
        st = []
        for c in cls:
            c.Perform(gp_Pnt(*map(float, q)), 1e-7)
            st.append(c.State())
        return 'in' if TopAbs_IN in st else 'out' if all(s == TopAbs_OUT for s in st) else 'on'
    return all(state(R @ (p - PROBE_EPS * n) + t) == 'in' and state(R @ (p + PROBE_EPS * n) + t) == 'out' for p, n in probes)


def same_part(a, b):
    """Are a and b the same part at a different place (congruent by a proper motion)? Volume, principal
    moments and extents (features.fingerprint), surface and face signature equal up to SAME_REL, and a
    rotation (det +1) that aligns the principal axis systems (signs, with equal moments also permutation)
    maps the check points of a into b and those of b into a. Mirror images (other handedness) are not the same part."""
    if not ft.congruent(ft.fingerprint(a), ft.fingerprint(b), SAME_REL):
        return False
    if max(len(list(an.faces(a))), len(list(an.faces(b)))) > MAX_MERGE_FACES:
        return False        # when in doubt do not merge: every face needs check points, which gets too expensive for large parts
    (sa, ka), (sb, kb) = _signature(a), _signature(b)
    if not _rel_eq(sa, sb) or set(ka) != set(kb) or any(ka[k][0] != kb[k][0] or not _rel_eq(ka[k][1], kb[k][1]) for k in ka):
        return False
    (ca, Ea, la), (cb, Eb, lb) = _frame(a), _frame(b)
    pa, pb = _surface_probes(a), _surface_probes(b)
    if not pa or not pb:
        return False
    in_a, in_b = ([BRepClass3d_SolidClassifier(s) for s in _solids(x)] for x in (a, b))
    for perm in itertools.permutations(range(3)):
        if not all(_rel_eq(la[i], lb[perm[i]]) for i in range(3)):
            continue
        for signs in itertools.product((1.0, -1.0), repeat=3):
            P = np.zeros((3, 3))
            for i in range(3):
                P[perm[i], i] = signs[i]
            R = Eb.T @ P @ Ea
            if np.linalg.det(R) < 0:
                continue
            t = cb - R @ ca
            if _covers(pa, R, t, in_b) and _covers(pb, R.T, -R.T @ t, in_a):
                return True
    return False


def _instances(parts, roles, shapes, key):
    """{part id: id of the first identical part} for parts with an item number (LISTED), the same role and the same key
    (material, form); the first part of a group stays, the others are instances."""
    leaders, out = [], {}
    for p in parts:
        pid = p['id']
        if roles[pid] not in LISTED:
            continue
        hit = next((q for q in leaders if roles[q] == roles[pid] and key[q] == key[pid] and same_part(shapes[q], shapes[pid])), None)
        if hit:
            out[pid] = hit
        else:
            leaders.append(pid)
    return out


def _common_name(names):
    """Common designation of a group of identical parts: all equal -> that one; otherwise the common leading words
    ("Countersunk screw right/left" -> "Countersunk screw"), verbatim in the first name; otherwise the first name."""
    if len(set(names)) == 1:
        return names[0]
    words = [n.split() for n in names]
    common = []
    for ws in zip(*words):
        if len(set(ws)) != 1:
            break
        common.append(ws[0])
    head = ' '.join(common).strip(' -–,;:/(')
    return head if re.search(r'[^\W\d_]', head) and pfx.verbatim(head, names[0]) else names[0]


def _clean_name(name, material):
    """Part name without color code (#rrggbb, RAL), material (verbatim from the facts) and quantity (4x, x4)."""
    s = _COLOR.sub(' ', name or '')
    if material:
        s = re.sub(r'(?<!\w)' + re.escape(material) + r'(?!\w)', ' ', s, flags=re.I)
    s = re.sub(r'\(\s*\)|\[\s*\]', ' ', _QTY.sub(' ', s))
    s = re.sub(r'\s+', ' ', s).strip(' -–,;:/')
    return s or _COLOR.sub(' ', name or '').strip() or (name or '')


def _label_name(text, n=LABEL_NAME):
    """Designation for a title: up to n characters unchanged, longer cut at the last word boundary (never in the middle of a word,
    "Housi (2:1)"), without trailing punctuation and without an open parenthesis (the started parenthetical part is dropped entirely)."""
    text = str(text or '')
    if len(text) <= n:
        return text
    cut = text[:n]
    if text[n] != ' ' and cut.rfind(' ') > 0:      # the cut lies inside a word: back to the word boundary
        cut = cut[:cut.rfind(' ')]
    if cut.count('(') > cut.count(')'):
        cut = cut[:cut.rfind('(')]
    return cut.rstrip(' ,;:-–/') or text[:n]


def _short(text, prefix, n=8):
    text = str(text or '')
    body = text[len(prefix):] if text.startswith(prefix) else text
    return body[:n]


# ------------------------------------------------------------------ parts, roles, texts

def _analyzed(p, s):
    """Part dict like analyze.analyze_spec (without plane distances)."""
    cyl, bt = an.cylinders(s), an.bend_table(s)
    return {'id': p['id'], 'name': p.get('name') or p['id'], 'bbox': an.bbox(s), 'volume': an.volume(s),
            'holes': [c for c in cyl if c['hole']], 'shafts': [c for c in cyl if not c['hole']], 'bend_tables': [bt] if bt else []}


def _profile_spans(face, normal, tol=1e-4):
    """Positions along the extrusion axis at which the full profile of a non-convex slant (face with cut-outs) lies on the face: both
    profile ends (the edges at the smallest and the largest profile coordinate) are edges of the face there. Only for normals perpendicular to exactly one world axis a (extrusion along a).
    -> (a, [[from, to], ...]) along a or None. The edges are sampled like analyze.boundary_points; an edge belongs to a profile end if all its points lie there."""
    n = prj.unit(normal)
    axes = [k for k in range(3) if abs(float(n[k])) <= 1e-6]
    if len(axes) != 1:
        return None
    a = axes[0]
    p = prj.unit(np.cross(np.eye(3)[a], n))
    edges = []
    ex = TopExp_Explorer(face, TopAbs_EDGE)
    while ex.More():
        pts = np.array([an.v3(q) for q in prj.sample_curve(BRepAdaptor_Curve(TopoDS.Edge_s(ex.Current())), 0.01)])
        if len(pts):
            edges.append(pts)
        ex.Next()
    if not edges:
        return None
    st = np.concatenate([e @ p for e in edges])
    ends = []
    for s in (st.min(), st.max()):
        cover = sorted((float(e[:, a].min()), float(e[:, a].max())) for e in edges if np.all(np.abs(e @ p - s) <= tol))
        merged = []
        for lo, hi in cover:
            if merged and lo <= merged[-1][1] + tol:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        ends.append(merged)
    spans = [[max(l1, l2), min(h1, h2)] for l1, h1 in ends[0] for l2, h2 in ends[1] if min(h1, h2) - max(l1, l2) > 2 * pv.TOL]
    return (a, [[round(lo, 6), round(hi, 6)] for lo, hi in spans]) if spans else None


def _features(shape, part, hints, spec):
    """(form features with patterns and IR hints, freeform, note texts, freeform regions, profiles, error of the profile detection or None) of a drawn part; via the
    form feature cache spec['feature_cache_dir'] with the same face assignment to the hints as in the render."""
    r, _ = ft.features_of(shape, part['id'], spec.get('feature_cache_dir'), spec.get('brep'), an.part_selection(spec, part),
                          hints)
    spheres = [f for f in r['features'] if f['kind'] == 'sphere']
    slants, unseen_planes = {}, {}
    for f in r['features']:
        if f['kind'] == 'slant' and not f.get('minor') and not f.get('via_member'):
            bounds = np.r_[f['bbox']['min'], f['bbox']['max']]
            slants.setdefault(tuple(np.round(bounds, 3)), []).append(f)
        if f['kind'] == 'plane' and f.get('visible_from') == [] and f.get('bbox') and not f.get('minor'):
            bounds = np.r_[f['bbox']['min'], f['bbox']['max']]
            unseen_planes.setdefault(tuple(np.round(bounds, 3)), []).append(f)
    intersector = ft._intersector(shape) if unseen_planes else None
    if spheres or slants or unseen_planes:
        for face in an.faces(shape):
            kind, sphere = ft._surface(face)
            if kind == 'plane' and (slants or unseen_planes):
                bb = an.bbox(face)
                key = tuple(np.round(np.r_[bb['min'], bb['max']], 3))
                for f in slants.get(key, ()):
                    _, normal = an.face_point_normal(face)
                    if normal is not None and normal @ f['normal'] > 1 - 1e-6:
                        outline = an.boundary_points(face)
                        f['_outline'] = outline.tolist()
                        lateral, vertical = ft._perp(normal)
                        area = ft.MultiPoint(np.column_stack((outline@lateral, outline@vertical))).convex_hull.area
                        f['_convex_profile'] = abs(area-f['area']) < max(0.002, 1e-6*f['area'])
                        if not f['_convex_profile']:
                            spans = _profile_spans(face, normal)
                            if spans:
                                f['_profile_axis'], f['_profile_spans'] = spans
                for f in unseen_planes.get(key, ()):
                    probes = ft._probes([face])
                    if len(probes):
                        # An unsuccessful ray test on existing, trimmed face points is a
                        # proven occlusion. Without a probe point the visibility stays unknown.
                        seen = ft._visible(intersector, probes, pv.VIEWS)
                        f['_edge_views'] = [p for p in pv.VIEWS if seen[p]]
            if kind != 'sphere':
                continue
            c = an.v3(sphere.Location())
            p, n = an.face_point_normal(face)
            if p is None or (p-c) @ n >= 0:
                continue
            for f in spheres:
                if abs(2*sphere.Radius()-f['diameter']) < 1e-3 and np.linalg.norm(c-f['center']) < 1e-3:
                    f['internal'] = True
                    g = GProp_GProps()
                    BRepGProp.SurfaceProperties_s(face, g)
                    radial = an.v3(g.CentreOfMass()) - c
                    if np.linalg.norm(radial) > 1e-4:
                        f['profile_axis'] = prj.unit(radial).tolist()
    return r['features'] + r['patterns'] + r['hints'], r['freeform'], r['hint_notes'], r.get('regions') or [], r.get('profiles') or [], r.get('profiles_error')


def _profile_rest(feats, regions, profiles, freeform):
    """Profiles with residual members (slants, cones, planes, freeform regions that are minor faces) and a non-empty mandatory list (checks.profile_need, the same computation as the check):
    [{'profile', 'need', 'residual'}] and the profiles without frame (whether a view along d can be built is decided by `plan_views.part_views` with the anchor via `checks.profile_candidates`, mark `profile_unbuilt`). The residual members carry `profile` = profile id (minor stays: view and dimension planning as before): they no longer appear in the row “Undimensioned forms” (plan_dims),
    the profile covers them. Regions only for a freeform part or facets (like checks._required)."""
    shown = list(regions) if freeform else [r for r in regions if r.get('facet')]
    items = {f['id']: f for f in list(feats) + shown}
    minor = {i for i, f in items.items() if f.get('minor')}
    plans, mark, unbuilt = [], {}, []
    for P in profiles:
        res, need = checks.profile_need(P, items, minor)
        if need and P.get('frame') is None:      # without a profile frame not buildable (the candidate choice along d is made by part_views with the anchor, checks.profile_candidates)
            unbuilt.append(P)
        elif need:
            plans.append({'profile': P, 'need': need, 'residual': res})
            mark.update({m: P['id'] for m in res})
    if mark:
        feats = [dict(f, profile=mark[f['id']]) if f['id'] in mark else f for f in feats]
        regions = [dict(r, profile=mark[r['id']]) if r['id'] in mark else r for r in regions]
    return feats, regions, plans, unbuilt


def _context(spec):
    """Everything that does not depend on size, scale and main view."""
    model, facts = spec.get('model') or {}, spec.get('facts') or {}
    fparts = {p['id']: p for p in facts.get('parts') or [] if p.get('id')}
    parts, shapes = an.part_shapes(spec)
    A = {p['id']: _analyzed(p, shapes[p['id']]) for p in parts}
    fps = {pid: ft.fingerprint(shapes[pid]) for pid in A}
    # Facts from all texts of the part; role from the full part name, from the description only word markers (not from
    # form feature names, plan_roles.regex_role)
    full = {p['id']: str(p.get('semantic_name') or p.get('name') or p['id']) for p in parts}
    rf = {p['id']: pfx.regex_facts(full[p['id']], p.get('description') or '', p.get('feature_names') or []) for p in parts}
    named = [{'id': p['id'], 'name': full[p['id']], 'description': str(p.get('description') or '')} for p in parts]
    roles, notes = pr.roles(named, {pid: f['role'] for pid, f in fparts.items() if f.get('role')}, fps, spec.get('part_roles'))
    given = lambda pid, k: fparts.get(pid, {}).get(k)                       # facts supplied by the caller take precedence
    material = {pid: str(given(pid, 'material') or rf[pid]['material'] or '').strip() for pid in A}
    form = {pid: given(pid, 'form') or rf[pid]['form'] for pid in A}
    tolerances = {pid: list(given(pid, 'tolerances') or rf[pid]['tolerances'] or []) for pid in A}
    same_as = _instances(parts, roles, shapes, {pid: (pfx.normalize(material[pid]), form[pid]) for pid in A})
    opn = []
    shown = [p['id'] for p in parts if roles[p['id']] != 'duplicate']
    assembly = len(shown) >= 2
    rank = {'manufactured': 0, 'standard': 1, 'purchased': 2}
    order = sorted((pid for pid in shown if roles[pid] in LISTED and pid not in same_as),
                   key=lambda pid: (rank[roles[pid]], -A[pid]['volume'] if roles[pid] == 'manufactured' else 0))
    pos = {pid: i + 1 for i, pid in enumerate(order)}
    drawn = [pid for pid in order if roles[pid] == 'manufactured'] or ([] if assembly else shown[:1])
    display = {pid: str(given(pid, 'designation') or '').strip() or rf[pid]['designation'] or _clean_name(A[pid]['name'], material[pid])
               or pid for pid in A}
    group = {pid: [pid] + [i for i, lead in same_as.items() if lead == pid] for pid in pos}
    for pid, g in group.items():
        if len(g) > 1 and not given(pid, 'designation'):
            display[pid] = _common_name([display[i] for i in g])
    who = lambda pid: (f'Item {pos[pid]} "{display[pid]}"' if assembly and pid in pos else f'"{display[pid]}"')

    P, out_parts, lines, rows, qty, qnotes, thread_names = {}, [], [], [], {}, {}, {}
    for p in parts:
        pid, name = p['id'], A[p['id']]['name']
        sol = _solids(shapes[pid])
        qty[pid], qnotes[pid] = ft.quantity([ft.fingerprint(s) for s in sol] if len(sol) > 1 else [fps[pid]], name)
        if qnotes[pid]:
            notes.append(f'{who(pid)}: {qnotes[pid]}')
    for p in parts:
        pid, name = p['id'], A[p['id']]['name']
        total = sum(qty[i] for i in group.get(pid, [pid]))
        feats, freeform = [], False
        cls = pr.part_class(A[pid]) if roles[pid] == 'manufactured' else 'standard_part' if roles[pid] == 'standard' else None
        if pid in drawn:
            texts = [str(model.get('name') or ''), str(model.get('purpose') or ''), full[pid], str(p.get('description') or ''),
                     *[str(t) for t in p.get('feature_names') or []]]
            # Thread hint without designation - the designation verbatim from the model texts if exactly one matches pitch and nominal Ø (features.named_thread_hints);
            # the designations read go into the sheet (checks.THREAD_NAMES_KEY) so that the render forms the same note
            hints, named = ft.named_thread_hints([h for h in spec.get('ir_hints') or [] if h.get('part') == pid], texts)
            if named:
                thread_names[pid] = ft.text_designations(texts)
            for _, des in named:
                if des:
                    notes.append(f'Thread at {pid}: designation {des} verbatim from the model text (pitch and nominal Ø match the IR hint without designation)')
                else:
                    opn.append(f'Thread designation of {who(pid)} missing (IR thread hint without designation, no model text names one matching pitch and nominal Ø)')
            feats, freeform, hnotes, regions, profs, profs_error = _features(shapes[pid], p, hints, spec)
            notes += hnotes
            plans = []
            if roles[pid] == 'manufactured' and profs_error:      # profile detection failed (not cached) - open, as the render (coverage_error) reports it
                opn.append(f'Profile detection of {who(pid)} failed ({profs_error}): profile shapes (N4.10) unchecked and not dimensioned')
            if roles[pid] == 'manufactured':      # minor faces (neither in the mandatory list nor in a higher class) carry minor and appear in one row per part
                feats, regions = checks.mark_minor(texts, feats, regions)
                feats, regions, plans, unbuilt = _profile_rest(feats, regions, profs, freeform)
                opn += [f"Profile {P['id']} of {who(pid)}: no profile frame, no view along the generatrix buildable - residual faces stay in the row “Undimensioned forms according to 3D model” (N4.10)" for P in unbuilt]
            part = {**A[pid], 'freeform': freeform, 'regions': regions, 'profiles': plans}
            P[pid] = {'part': part, 'cls': cls or pr.part_class(A[pid]), 'feats': feats, 'role': roles[pid], 'texts': texts,
                      'datum': pr.datum(part['bbox'], feats), 'tolerances': tolerances[pid]}
            if freeform:
                opn.append(f'Shape of {who(pid)} according to 3D model (freeform surfaces, main dimensions only)')
        norm = ''
        if roles[pid] == 'manufactured' and pid in pos:
            norm = pr.stock_text(material[pid], form[pid], _stock(A[pid], feats, cls), spec.get('semi_finished')) or ''
            if not norm:
                opn.append(f'Semi-finished product/standard of {who(pid)} missing (no entry in drawing-config.json → semi_finished)')
            elif not assembly:
                lines.append(f'Semi-finished product: {norm}')
        if roles[pid] == 'standard' and pid in pos and not pr._NORM.search(full[pid]):
            opn.append(f'Standard designation of {who(pid)} missing (standard not in the part name)')
        if pid in pos:
            if not material[pid]:
                opn.append(f'Material of {who(pid)} missing (not in the model text)')
            rows.append({'pos': pos[pid], 'qty': total, 'name': display[pid], 'part': pid, 'norm': norm, 'material': material[pid]})
            if len(group[pid]) > 1:
                notes.append(f"{who(pid)}: {len(group[pid])} identical parts ({', '.join(group[pid])}) – one item, quantity {total}")
        out_parts.append({'id': pid, 'name': name, 'role': 'duplicate' if pid in same_as else roles[pid], 'class': cls,
                          'quantity': total if pid in pos else qty[pid], **({'same_as': same_as[pid]} if pid in same_as else {}),
                          **({'quantity_note': qnotes[pid]} if qnotes[pid] else {}),
                          'datum': P[pid]['datum'] if pid in P else pr.datum(A[pid]['bbox'], []),
                          'features': _feature_ids([f['id'] for f in feats]), 'freeform': bool(freeform)})
    rows.sort(key=lambda r: r['pos'])
    title, topen, tnotes = _title(model, facts.get('title') or {}, spec.get('title_defaults') or {},
                                  'see parts list' if assembly else material[shown[0]] if shown else '')
    notes += tnotes
    mid, rev = _model_id(model), _short(model.get('revision'), 'rev-')
    if tnotes:                                  # drawing number from the model ID: the full ID leads back to the model
        lines.append(f"Drawing no. = LLcad model ID {_model_id(model, True)}")
    if rev:                                     # the model revision as a note line, not in the revision field
        lines.append(f"3D model {(str(model.get('name') or '').strip().partition(' – ')[0].strip() or mid)[:80]}, revision {rev}")
    ctx = {'source': spec, 'A': A, 'shapes': shapes, 'roles': roles, 'pos': pos, 'order': order, 'drawn': drawn, 'P': P, 'assembly': assembly,
           'display': display, 'rows': rows if assembly else [], 'lines': lines, 'title': title, 'notes': notes,
           'open': topen + opn, 'model_ref': f'{mid}, revision {rev}' if rev else mid,
           'company': [str(c)[:80] for c in (spec.get('company') or [])[:3]],
           'spec_parts': [{'id': x['id'], 'name': x['name'][:120], 'role': x['role'], 'quantity': max(1, min(999, int(x['quantity'])))}
                          for x in out_parts],
           'out_parts': out_parts, 'who': who, 'function_dims': [],
           # numbers and angles of the full model texts per manufactured part for the checker (checks.spec_function_numbers), whose render does not see the texts
           'function_values': {pid: checks.function_values_entry(P[pid]['texts']) for pid in P if roles[pid] == 'manufactured'},
           'thread_names': thread_names,      # {part: thread designations of the model texts}, only parts with a thread hint without designation (checks.spec_thread_designations)
           # item numbers: every part with an item number and every further instance with the number of its first part
           'balloon_pos': {**pos, **{i: pos[lead] for i, lead in same_as.items() if lead in pos}}}
    ctx['suggestion'] = an.suggestion(an.extents([shapes[pid] for pid in shown]),
                                      [{'id': pid, 'ext': an.extents([shapes[pid]]), 'holes': len(A[pid]['holes']),
                                        'bend_rows': max((len(b.get('points') or []) for b in A[pid]['bend_tables']), default=0)}
                                       for pid in shown]) if shown else None
    ctx['asm_views'], ctx['balloons'] = ([], [])
    if assembly:
        _asm_setup(ctx)
    return ctx


def _asm_setup(ctx, single=False):
    """Assembly views and item numbers (ctx['asm_views'], ctx['balloons']): all four views, numbers via _balloons, afterwards only carriers and isometric view (_carrier_views). Repeatable
    (single: fallback from plan_spec): the open items that _balloons itself created (ctx['_asm_open_items']) are dropped; foreign entries (e.g. tolerance notes from
    _build_part_content) stay. ctx['_balloon_stats']['seconds'] counts the time of all runs."""
    _asm_drop_open(ctx)
    ctx.pop('_bplan', None)
    ctx['asm_views'] = pv.assembly_views(list(ctx['A'].values()), ctx['roles'])
    t0 = time.perf_counter()
    n_open = len(ctx['open'])
    ctx['balloons'] = _balloons(ctx, single)
    ctx['_asm_open_items'] = list(ctx['open'][n_open:])
    stats = ctx.setdefault('_balloon_stats', {})
    stats['seconds'] = round(stats.get('seconds', 0.0) + time.perf_counter() - t0, 3)         # time of _balloons (metric balloons_s in candidates[].metrics)
    stats['single'] = single
    ctx['asm_views'] = _carrier_views(ctx['asm_views'], ctx['balloons'])


def _asm_drop_open(ctx):
    """Removes exactly the open items that the last _balloons run created (each entry once, from the back)."""
    for item in ctx.pop('_asm_open_items', []):
        for i in range(len(ctx['open']) - 1, -1, -1):
            if ctx['open'][i] == item:
                del ctx['open'][i]
                break


def _carrier_views(views, balloons):
    """Assembly views that stay: the carriers of the item numbers and the isometric view (for orientation); every other view without an item number
    is dropped (a view without information is not information, view_without_information). The front view no longer stays automatically either."""
    carriers = {b['view'] for b in balloons if b.get('view')}
    return [v for v in views if v['role'] == 'iso' or v['id'] in carriers]


def _stock(part, feats, cls):
    """Dimensions for the semi-finished product texts: d = wire diameter or largest shaft Ø, a = square bar, t = plate thickness."""
    bt = next((b for b in part['bend_tables'] if b.get('complete')), None)
    shafts = [s['diameter'] for s in part['shafts']]
    squares = [f['size'] for f in feats if f['kind'] == 'square']
    return {'d': bt['wire_diameter'] if bt else max(shafts) if shafts else None, 'a': max(squares) if squares else None,
            't': min(part['bbox']['size']) if cls == 'plate' else None}


def _model_id(model, full=False):
    """LLcad model id (model.id = model id of the service/database, e.g. "model-00000000-0000-4000-8000-000000000001"); short form "model-" + 8 characters for
    note lines, full = whole id (title block: leads back to the model)."""
    mid = str(model.get('id') or '').strip()
    return mid if full or not mid.startswith('model-') else mid[:len('model-') + 8]


CREATED_BY_FALLBACK = 'LLcad'


def _title(model, ft_title, defaults, material):
    """Title block -> (title, open, notes): title = facts or model name up to the first " – ", supplementary_title = facts or the rest, drawing_number =
    facts, otherwise first 6–7-digit number in the model name, otherwise title_defaults, otherwise "LLcad <id>" with the short form of the LLcad model id (never
    empty, never an invented company number; plus a planner note), material as passed, revision (revision index) only from
    title_defaults (never the model revision); the rest from title_defaults. Empty fields are missing (the service then adds its
    defaults); if title, drawing number (without model id) or revision index are missing, they are listed under open."""
    name = str(model.get('name') or '').strip()
    head, sep, rest = name.partition(' – ')
    nr = _NUMBER.search(name)
    got = {'title': (ft_title.get('title') or head).strip(), 'supplementary_title': (ft_title.get('supplementary_title') or (rest if sep else '')).strip(),
           'drawing_number': (ft_title.get('drawing_number') or (nr.group(0) if nr else '')).strip(), 'material': material}
    title = {str(k)[:40]: str(v)[:200] for k, v in defaults.items() if isinstance(v, str) and v.strip()}
    mid, notes = _model_id(model, True), []
    if not got['drawing_number'] and not title.get('drawing_number') and mid:
        # Short form in the field: the full id (48 characters) does not fit into the field (wrapping only at spaces; shortened until FIT_MIN,
        # wrapped it overlaps the field label - pin text_overlap); the full id is in the note line (plan._context) and in the planner note
        got['drawing_number'] = 'LLcad ' + _model_id(model)[len('model-'):] if mid.startswith('model-') else f'LLcad {mid}'
        notes.append(f'Drawing no. from LLcad model ID (no company number in the model); full ID {mid}')
    title.update({k: v[:200] for k, v in got.items() if v})
    opn = []
    if not title.get('created_by'):
        # A configured creator always wins; without one the title block would block publication (checks.REQUIRED_TITLE), so use a visible fallback and an open item
        title['created_by'] = CREATED_BY_FALLBACK
        opn.append('Creator not configured: set title_defaults.created_by in drawing-config.json')
    if not got['title']:
        opn.append('Title missing (no model name, no title in the facts)')
    if not title.get('drawing_number'):
        opn.append('Drawing number missing (not in the facts, no 6–7-digit number in the model name, no model ID)')
    if not title.get('revision'):
        opn.append('Revision index missing (no revision entry in drawing-config.json → title_defaults)')
    return title, opn, notes


def _ray(ctx, pid):
    """Ray test of the part body for dimension anchors (plan_dims.dimension ray): ray(p, N, n[, tmax]) -> True if the ray from anchor p to the camera (direction N)
    is free (with tmax only up to there: in a section up to the cutting plane); n (face normal/dimension direction) offsets p by ft.LIFT to both sides - free if one of the two points is free (edge of a
    face that the view only meets from the side). Built once per part, results remembered per (point, direction) (plan runs repeat them)."""
    cache = ctx.setdefault('rays', {})
    if pid not in cache:
        ix, memo = ft._intersector(ctx['shapes'][pid]), {}

        def free(p, N, n, tmax=1e9):
            key = (tuple(np.round(np.asarray(p, float), 3)), tuple(np.round(np.asarray(N, float), 6)), tuple(np.round(np.asarray(n, float), 3)), round(float(tmax), 3))
            if key not in memo:
                p_, d = np.asarray(p, float), gp_Dir(*map(float, N))
                memo[key] = any(ix.free(p_ + sg * ft.LIFT * np.asarray(n, float), d, tmax) for sg in (1, -1))
            return memo[key]
        def on_face(p, n):
            """Does p lie on a face of the part (normal n)? A ray from 0.03 next to p to the face hits it within 0.06 (one side suffices)."""
            key = ('f', tuple(np.round(np.asarray(p, float), 3)), tuple(np.round(np.asarray(n, float), 3)))
            if key not in memo:
                p_, n_ = np.asarray(p, float), np.asarray(n, float)
                memo[key] = any(not ix.free(p_ + sg * 0.03 * n_, gp_Dir(*map(float, -sg * n_)), 0.06) for sg in (1, -1))
            return memo[key]
        planes = []
        def boundaries(p, normal, direction, feature=None, section=None):
            """Points of the original trimmed face on the same dimension coordinate."""
            from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
            from OCP.BRepClass import BRepClass_FaceClassifier
            from OCP.BRepAdaptor import BRepAdaptor_Curve
            from OCP.GeomAbs import GeomAbs_Plane
            from OCP.TopAbs import TopAbs_EDGE, TopAbs_ON
            from OCP.TopoDS import TopoDS
            from OCP.gp import gp_Pln
            section_key = tuple(tuple(v) for v in section) if section is not None else None
            key = ('boundary', tuple(p), tuple(normal), tuple(direction), feature['id'] if feature else None, section_key)
            if key in memo:
                return memo[key]
            if not planes:
                for face in an.faces(ctx['shapes'][pid]):
                    surface = BRepAdaptor_Surface(face)
                    if surface.GetType() == GeomAbs_Plane:
                        plane = surface.Plane()
                        planes.append((face, plane, an.v3(plane.Axis().Direction())))
            point = gp_Pnt(*p)
            candidates = []
            for face, plane, axis in planes:
                if abs(axis @ normal) < 1 - 1e-6 or plane.Distance(point) > 1e-3:
                    continue
                if feature:
                    bounds = an.bbox(face)
                    boxes = feature.get('patch_bounds') or [feature['bbox']['min'] + feature['bbox']['max']]
                    if not any(np.allclose(bounds['min']+bounds['max'], b, atol=1e-3, rtol=0) for b in boxes):
                        continue
                elif BRepClass_FaceClassifier(face, point, 1e-3).State() not in (TopAbs_IN, TopAbs_ON):
                    continue
                shapes = [face]
                if abs(axis @ direction) < 1 - 1e-9:
                    cut = BRepAlgoAPI_Section(face, gp_Pln(point, gp_Dir(*direction)))
                    if cut.IsDone():
                        shapes.append(cut.Shape())
                if section is not None and abs(axis @ section[1]) < 1 - 1e-9:
                    cut = BRepAlgoAPI_Section(face, gp_Pln(gp_Pnt(*section[0]), gp_Dir(*section[1])))
                    if cut.IsDone():
                        shapes.append(cut.Shape())
                for witness in shapes:
                    ex = TopExp_Explorer(witness, TopAbs_EDGE)
                    while ex.More():
                        curve = BRepAdaptor_Curve(TopoDS.Edge_s(ex.Current()))
                        for u in np.linspace(curve.FirstParameter(), curve.LastParameter(), 9):
                            q = an.v3(curve.Value(u))
                            if abs((q-p) @ direction) < 1e-4:
                                candidates.append(q)
                        ex.Next()
            memo[key] = sorted(candidates, key=lambda q: float(np.linalg.norm(q-p)))
            return memo[key]
        def contour(w, points):
            if ctx.get('_contours') is None:
                ctx['_contours'] = _drawing().Drawing(ctx['source'])
            v = {'id': pid, 'parts': [pid], 'direction': w['fr'][0].tolist(), 'up': w['fr'][2].tolist(),
                 'centerlines': False}
            if w['sect'] is not None:
                v['section'] = {'point': w['sect'].tolist()}
            return ctx['_contours'].visible_points(v, '1:1', prj.project(points, w['fr']), tol_mm=0.06)

        def extreme_vertices(axis, coord):
            """Actual body points on an envelope coordinate, also for slanted or stepped faces."""
            from OCP.BRep import BRep_Tool
            from OCP.TopAbs import TopAbs_VERTEX
            from OCP.TopExp import TopExp
            from OCP.TopTools import TopTools_IndexedMapOfShape
            from OCP.TopoDS import TopoDS
            if 'vertices' not in memo:
                vertices = TopTools_IndexedMapOfShape()
                TopExp.MapShapes_s(ctx['shapes'][pid], TopAbs_VERTEX, vertices)
                memo['vertices'] = [an.v3(BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertices.FindKey(i))))
                                    for i in range(1, vertices.Extent()+1)]
            return [p for p in memo['vertices'] if abs(p[axis]-coord) <= 0.0001]

        free.contour = contour
        free.on_face = on_face
        free.boundaries = boundaries
        free.extreme_vertices = extreme_vertices
        cache[pid] = free
    return cache[pid]


def _balloons_at(ctx, k):
    """Item numbers for the assembly block at the actual scale k: the view per part stays, the tip is chosen anew
    with the distance of the checker (ft.balloon_rings(k): BALLOON_CLEAR_MM paper), computed once per scale."""
    plan = ctx.get('_bplan')
    if plan is None:
        return list(ctx['balloons'])
    key = round(k, 9)
    if key not in plan['cache']:
        out = []
        for v, pids in plan['groups'].values():
            seen = {pid: plan['seen'][(v['id'], pid)] for pid in pids}
            got = pd.balloons(v, plan['parts'], {pid: plan['pos'][pid] for pid in pids}, seen, lambda pid, p, v=v: plan['clear'](v, k)(pid, p),
                              ft.balloon_rings(k)[-1])
            for b in got:
                b.pop('_c', None)
            out += got
        order = {pid: i for i, pid in enumerate(plan['pos'])}
        plan['cache'][key] = sorted(out, key=lambda b: order.get(b['part'], 0)) + [dict(b) for b in plan['hidden']]
    return [dict(b) for b in plan['cache'][key]]


def _balloons(ctx, single=False):
    """Item numbers: exactly one per item (N4.7, DIN ISO 6433: identical parts carry one number, stated once, the quantity is in the parts list), at the most suitable instance
    (same choice as elsewhere for the part: visible with enough distance, otherwise greatest distance, tie the earlier one), at the visible
    point (probe point of the surface, ray to the camera free through the whole assembly) in the first assembly
    view in which the part is visible (chosen by coverage). If it is visible in none, a further view from the back, right or bottom is added
    (EXTRA_BALLOON_VIEWS) in which it is free. Otherwise it gets its number at a hidden point in the first view that already carries numbers
    (entry under open with reason).
    N4.7: which view carries the number is no longer decided by the first one that shows the part, but by a coverage over all views (see below);
    single = fallback for N1.9 (the set of views with the fewest numbers on foreign parts plus views)."""
    views = ctx['asm_views']
    ids = views[0]['parts']
    ix = ft._intersector(prj.compound([ctx['shapes'][i] for i in ids]))
    # distance of the number tip to foreign parts; target BALLOON_CLEAR_MM on paper at the estimated assembly scale
    ixs = ctx.setdefault('ixs', {})
    for i in ids:
        if i not in ixs:
            ixs[i] = ft._intersector(ctx['shapes'][i])
    boxes = [ctx['A'][i]['bbox'] for i in ids if ctx['A'][i].get('bbox')]
    far = 2.0 * max((float(np.linalg.norm(np.asarray(b['size'], float))) for b in boxes), default=100.0) + 10.0
    k_est = an.scale_value(((ctx.get('suggestion') or {}).get('scale')) or '1:1') or 1.0
    rings = ft.balloon_rings(k_est)             # same criterion as the checker (Drawing.balloon_foreign): BALLOON_CLEAR_MM paper at the scale
    want = rings[-1]
    bxs = {i: (ctx['A'][i]['bbox']['min'], ctx['A'][i]['bbox']['max']) for i in ids if ctx['A'][i].get('bbox')}

    memo = ctx.setdefault('_clear_memo', {})         # each (view, part, point, rings) computed only once (estimate and final scale share the rays)

    def clear_for(v, rg):
        fr = prj.frame_of(v)

        def f(pid, p):
            key = (v['id'], v['preset'], pid, tuple(round(float(x), 4) for x in p), tuple(rg))
            if key not in memo:
                memo[key] = ft.view_clearance(ixs, pid, p, fr, rg, far, boxes=bxs)[0]
            return memo[key]
        return f
    clear_in = lambda v: clear_for(v, rings)
    todo = {pid: num for pid, num in ctx['balloon_pos'].items() if pid in ids}
    num_of = dict(todo)
    members = checks.balloon_groups([(pid, num) for pid, num in todo.items()])      # number -> instances (same grouping as checks.balloon_repeat_findings)
    probes, best, seen_all = {}, {}, {}  # best: number -> (distance, satisfies want, view, item number, instance)
    evaluated = {}                       # view -> {part: (distance, satisfies want, item number)}, computed once per view
    stats = ctx.setdefault('_balloon_stats', {})
    stats['views_evaluated'] = 0
    parts = [ctx['A'][i] for i in ids]

    def candidates(v, pids):
        toward = gp_Dir(*map(float, prj.frame_of(v)[0]))
        seen = {}
        for pid in pids:
            if pid not in probes:
                probes[pid] = ft._probes(list(an.faces(ctx['shapes'][pid])), PROBES)
            seen[pid] = [p.tolist() for p in probes[pid] if ix.free(p, toward)]
        balloons = pd.balloons(v, parts, {pid: todo[pid] for pid in pids}, seen, clear_in(v), want)
        return balloons, seen

    def evaluate(v, pids, known=None):
        """Candidates of the view for pids: per part the distance of the tip to foreign parts and the item number; known = distances from the front view (only for the choice of the isometric octant)."""
        got, seen = candidates(v, pids)
        stats['views_evaluated'] += 1
        if v.get('role') == 'iso':
            # Opposite upper octant: hidden positions can become recognizable in the same isometric view.
            # Visibility and distance of the number tip decide together.
            def quality(items):
                visible = {}                    # per number the best instance (one number per item)
                for pid, c in (known or {}).items():
                    if pid in pids:
                        visible[str(num_of[pid])] = max(visible.get(str(num_of[pid]), 0.0), c)
                for item in items:
                    key = str(num_of[item['part']])
                    visible[key] = max(visible.get(key, 0.0), item.get('_c', 0.0))
                return sum(c >= want-1e-9 for c in visible.values()), len(visible), sum(visible.values())
            if quality(got)[0] < len({str(num_of[pid]) for pid in pids}):
                other = 'iso_back' if v['preset'] == 'iso' else 'iso'
                alt, alt_seen = candidates({**v, 'preset': other}, pids)
                if quality(alt) > quality(got):
                    v['preset'], got, seen = other, alt, alt_seen
        for pid, pts in seen.items():
            seen_all[(v['id'], pid)] = pts
        res = {}
        for b in got:
            c = b.pop('_c', 0.0)
            res[b['part']] = (c, c >= want - 1e-9, b)
        evaluated[v['id']] = res
        return res

    # N4.7: which views carry the numbers? First the front view; if it does not show all parts with enough distance, the other views are computed for all parts
    # (_clear_memo shares the rays). Default: greedy coverage - each time the view that shows the most still open parts with enough distance, on a tie front, top,
    # side view, isometric view (order of the views). single (fallback from plan_spec when N1.9 cannot be met otherwise): the set S of views that minimizes the number of numbers with too
    # small a distance (number on a foreign part) plus the number of assembly views (S and the isometric view, which stays for orientation); tie: fewer views,
    # then the order of the views - usually the isometric view alone then carries all numbers.
    def top(res, num):
        """Best instance of the number in a view result: (satisfies want, distance, number, instance); visible with enough distance first, otherwise the greatest distance, tie the earlier instance."""
        opts = [(res[p][1], 0.0 if res[p][1] else res[p][0], -i, p) for i, p in enumerate(members[num]) if p in res]
        if not opts:
            return None
        ok, _, _, p = max(opts, key=lambda o: o[:3])
        return ok, res[p][0], res[p][2], p
    ok_in = lambda v, num: (t := top(evaluated[v['id']], num)) is not None and t[0]
    front_res = evaluate(views[0], list(todo))
    if not single and all(ok_in(views[0], num) for num in members):          # in the fallback, "the front view shows everything" also counts only via the costs (isometric view alone)
        chosen = [views[0]]
    else:
        known = {pid: r[0] for pid, r in front_res.items()}
        for v in views[1:]:
            evaluate(v, list(todo), known)
        iso_ids = {v['id'] for v in views if v.get('role') == 'iso'}

        def cost(subset):
            bad = 0
            for num in members:
                opts = [(t[0], t[1]) for v in subset if (t := top(evaluated[v['id']], num))]
                bad += 1 if not opts or not max(opts)[0] else 0
            return bad + len({v['id'] for v in subset} | iso_ids), len(subset), [i for i, v in enumerate(views) if v in subset]
        subsets = [[v for j, v in enumerate(views) if mask >> j & 1] for mask in range(1, 1 << len(views))]
        if single:
            chosen = min(subsets, key=cost)
            stats['carrier_cost'] = cost(chosen)[0]
        else:
            chosen, pend = [], list(members)
            while pend:
                gains = [(sum(1 for num in pend if ok_in(v, num)), -i, v) for i, v in enumerate(views) if v['id'] in evaluated]
                gain, _, v = max(gains, key=lambda g: (g[0], g[1]))
                if gain == 0:
                    break
                chosen.append(v)
                pend = [num for num in pend if not ok_in(v, num)]
            chosen = chosen or [views[0]]
            # N4.1: if the isometric view, which stays for orientation anyway, is already a carrier next to further views, the same cost rule applies as in the N1.9 fallback
            # (numbers without enough distance + number of assembly views, tie: fewer views): fewer views at costs that are at most equal (one instead of two)
            if len(chosen) > 1 and any(v['id'] in iso_ids for v in chosen):
                fewer = min(subsets, key=cost)
                if len(fewer) < len(chosen) and cost(fewer)[0] <= cost(chosen)[0]:
                    chosen = fewer
                    stats['carrier_cost'] = cost(chosen)[0]
    for num in members:
        # per number the first chosen view in which an instance with enough distance stands; otherwise the chosen one with the greatest distance (tie: earlier)
        opts = [(t[0], 0.0 if t[0] else t[1], -j, v, t) for j, v in enumerate(chosen) if (t := top(evaluated[v['id']], num))]
        if opts:
            ok, c, _, v, t = max(opts, key=lambda o: o[:3])
            best[num] = (t[1], ok, v, t[2], t[3])
    for num in members:
        # no instance with enough distance in any view: the view with the greatest distance (tie: chosen view, then order of the views)
        if num in best:
            continue
        opts = [(t[1], v in chosen, -i, v, t) for i, v in enumerate(views) if (r := evaluated.get(v['id'])) and (t := top(r, num))]
        if opts:
            c, _, _, v, t = max(opts, key=lambda o: o[:3])
            best[num] = (c, False, v, t[2], t[3])
    extras = []
    for preset in (p for p in EXTRA_BALLOON_VIEWS if p not in {v.get('preset') for v in views}):
        # not freely visible from any assembly view (or only with the number on a foreign part) -> a further view from another normal direction
        pend = [num for num in members if not any((t := top(r, num)) is not None and t[0] for r in evaluated.values())]      # only numbers that no view shows with enough distance (a part that stands free elsewhere gets no additional view of its own)
        if not pend:
            break
        v = {'id': f'asm-{preset}', 'part': None, 'preset': preset, 'role': 'side', 'parts': list(ids), 'features': [],
             **({'thin_parts': views[0]['thin_parts']} if views[0].get('thin_parts') else {})}
        res = evaluate(v, [pid for num in pend for pid in members[num]])
        for num in pend:
            t = top(res, num)
            cur = best.get(num)
            if t is not None and (cur is None or (t[0] and not cur[1])):
                best[num] = (t[1], t[0], v, t[2], t[3])           # an additional view only if it establishes the distance (or the part is visible nowhere else)
        extras.append(v)
    stats['covers'] = {vid: {pid: round(r[0], 3) for pid, r in res.items()} for vid, res in evaluated.items()}      # distance of the tip per view and part (model mm; satisfies from want), evidence for N4.7
    stats['want'] = want
    views += [v for v in extras if any(x[2] is v for x in best.values())]
    out = [best[num][3] for num in members if num in best]
    groups, lead = {}, {}
    for num in members:
        if num in best:
            lead[best[num][4]] = num_of[best[num][4]]
            groups.setdefault(best[num][2]['id'], (best[num][2], []))[1].append(best[num][4])
    plan = ctx['_bplan'] = {'groups': groups, 'seen': seen_all, 'pos': lead, 'parts': [ctx['A'][i] for i in ids], 'hidden': [], 'cache': {},
                            'clear': lambda v, k: clear_for(v, ft.balloon_rings(k))}
    todo = {members[num][0]: num_of[members[num][0]] for num in members if num not in best}      # hidden in all instances: the first instance carries the number
    primary = next((v for v in chosen if any(x[2] is v for x in best.values())), views[0])          # hidden numbers stand in the first view that already carries numbers (no further view with numbers)
    hidden = pd.balloons(primary, [ctx['A'][i] for i in ids], todo, {pid: [p.tolist() for p in probes[pid]] for pid in todo}) if todo else []
    for b in hidden:
        b['hidden'] = True              # hidden part: the number deliberately points to another visible part (balloon_on_foreign_part does not check it)
    out += hidden
    plan['hidden'] = hidden
    placed = {b['part'] for b in hidden}
    for pid in todo:
        what = ('points to a hidden part (not freely visible in any of the six normal directions, it lies inside other parts; '
                'an assembly section is not planned)') if pid in placed else 'is missing (not visible in any assembly view)'
        ctx['open'].append(f"Item number of item {ctx['balloon_pos'][pid]} \"{ctx['display'][pid]}\" {what}")
    return out


# ------------------------------------------------------------------ content of a variant

def _extents(views, A):
    out = {}
    for v in views:
        ids = [v['part']] if v.get('part') else list(v.get('parts') or [])
        lo = np.min([A[i]['bbox']['min'] for i in ids], 0)
        hi = np.max([A[i]['bbox']['max'] for i in ids], 0)
        q = prj.project([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])], prj.frame_of(v))
        w, h = q.max(0) - q.min(0)
        out[v['id']] = {'w': float(w), 'h': float(h)}
    return out


def _marks(views, bb, k):
    """Section lines (views with role 'section') and detail circles in their host views."""
    by = {v['id']: v for v in views}
    corners = np.array([[x, y, z] for x in (bb['min'][0], bb['max'][0]) for y in (bb['min'][1], bb['max'][1])
                        for z in (bb['min'][2], bb['max'][2])], float)
    out, placed = [], []                      # placed: (host view, side, sheet position of the letter end)
    for v in views:
        host = by.get(v.get('section_line_in'))
        if (v.get('role') == 'section' or v.get('section')) and host:      # also auxiliary view as a section
            Ns = prj.frame_of(v)[0]
            d = prj.unit(np.cross(Ns, prj.frame_of(host)[0]))
            P = np.asarray(v['section']['point'], float)
            t, e = (corners - P) @ d, SECTION_OVER / pl.view_scale(host, k)
            hs, hf = pl.view_scale(host, k), prj.frame_of(host)
            ends = []
            for sg, base in ((-1, float(t.min())), (1, float(t.max()))):       # the letter stands at the end of the section line; if a letter of another mark is already there
                x = e                                                          # (< SECTION_LABEL_GAP), the line moves further out (offset along the section line)
                while True:
                    q = prj.project([P + (base + sg * x) * d], hf)[0] * hs
                    if not any(h == host['id'] and s_ == sg and float(np.linalg.norm(q - q_)) < SECTION_LABEL_GAP for h, s_, q_ in placed) or x > e + 12 * SECTION_STEP / hs:
                        break
                    x += SECTION_STEP / hs
                placed.append((host['id'], sg, q))
                ends.append(P + (base + sg * x) * d)
            out.append({'type': 'section_line', 'view': host['id'], 'p1': _r(ends[0]), 'p2': _r(ends[1]),
                        'label': v['section']['label'], 'look': _r(-Ns)})
        if v.get('role') == 'detail' and v.get('detail_circle_in') in by:
            det = v['detail']
            out.append({'type': 'detail_circle', 'view': v['detail_circle_in'], 'center': list(det['center']),
                        'radius': det['radius'], 'label': det['label']})
        if (v.get('auxiliary') or v.get('named')) and v.get('arrow_in') in by:      # viewing arrow to the middle of the elements, tip in front of the bounding box
            N, c = prj.frame_of(v)[0], np.asarray(v['arrow_at'], float)
            t = float(((corners - c) @ N).max()) + (ARROW_GAP if v.get('auxiliary') else NAMED_ARROW_GAP) / pl.view_scale(by[v['arrow_in']], k)
            out.append({'type': 'view_arrow', 'view': v['arrow_in'], 'p': _r(c + t * N), 'look': _r(-N),
                        'label': (v.get('auxiliary') or v['named'])['label'], **({'compact': True} if v.get('named') and not v.get('auxiliary') else {})})
    return out


def _union_box(boxes):
    return {'min': np.min([b['min'] for b in boxes], 0).tolist(), 'max': np.max([b['max'] for b in boxes], 0).tolist()}


def _name_free(views, k, letters, bb):
    """Names for views that plan_layout.arrange does not place in projection position (plan_layout.places, not proj: room already taken, own scale,
    rear view, only a common axis), ISO 128-3: “View X” (v['named']) with viewing arrow in a view whose image plane contains the viewing direction
    (otherwise only the name). Sections, detail views, auxiliary views and isometric views already carry their designation. letters: generator of free
    label letters, bb: bounding box of the block (arrow middle)."""
    if len(views) < 2:
        return
    hung = pl.places(views, k)
    for v in views[1:]:
        if hung.get(v['id'], {}).get('proj'):            # projection position by method 1 (to the anchor or to another neighbouring view): no label letter
            continue
        N = prj.frame_of(v)[0]
        if v.get('detail') or v.get('section') or v.get('auxiliary') or v.get('named') or v.get('label') or float(np.abs(N).max()) < 1 - 1e-9:
            continue
        v['named'] = {'label': next(letters)}
        host = next((h for h in views if h is not v and not h.get('detail') and not h.get('section') and not h.get('auxiliary')
                     and abs(float(prj.frame_of(h)[0] @ N)) < 1e-3), None)
        if NAMED_ARROWS and host is not None:
            v['arrow_in'], v['arrow_at'] = host['id'], _r((np.asarray(bb['min'], float) + np.asarray(bb['max'], float)) / 2)


def _used_letters(views):
    return {str(v[key]['label']) for v in views for key in ('section', 'detail', 'auxiliary', 'named') if v.get(key) and v[key].get('label')}


def _titles(views):
    """Titles of sections ("A-A", with own scale "A-A (5:1)"), detail views ("Detail B (5:1)") and
    auxiliary views ("View C", with own scale "View C (2:1)")."""
    for v in views:
        if v.get('auxiliary') and not v.get('section'):
            v['label'] = f"View {v['auxiliary']['label']}" + (f" ({v['scale']})" if v.get('scale') else '')
        elif v.get('named') and not v.get('detail') and not v.get('section'):
            v['label'] = f"View {v['named']['label']}" + (f" ({v['scale']})" if v.get('scale') else '')
        elif v.get('detail'):
            v['label'] = f"Detail {v['detail']['label']} ({v['detail']['scale']})"
        elif v.get('section') and v['section'].get('label'):      # an axial section without a label letter (checks.axis_section) carries no title
            L = v['section']['label']
            v['label'] = f'{L}-{L}' + (f" ({v['scale']})" if v.get('scale') else '')


def _arrange(ctx, b, k, measured=None, sheet=None):
    """Size and positions of the block b (b['w'], b['h'], b['lay']); measured: measured occupancy per view (plan_layout), sheet: free
    sheet area (B, H) of the format (sheet-aware arrangement). The inputs stay in b['_arr'] so that _variant can arrange the block anew for
    each candidate format (_fitted)."""
    b['_arr'], b['_fit'], b['_alt'] = (ctx, k, measured), {}, {}
    b['w'], b['h'], b['lay'] = pl.arrange(b['views'], b['anns'], _extents(b['views'], ctx['A']), k,
                                          [(t['_name'], 88.0, t['_h'], t.get('labels_in')) for t in b['tables']], b['title'], measured, sheet)


free_area = checks.free_area      # free sheet area (B, H) of a block on the format (drawing area without margin and half block spacing all around, like pl.pack): one definition with check N1.3


def _fitted(b, fmt, orient):
    """Block b, arranged for the free area of the size (sheet-aware); result remembered per size."""
    if '_arr' not in b:
        return b
    if fmt not in b['_fit']:
        ctx, k, measured = b['_arr']
        c = dict(b)
        c['w'], c['h'], c['lay'] = pl.arrange(b['views'], b['anns'], _extents(b['views'], ctx['A']), k,
                                              [(t['_name'], 88.0, t['_h'], t.get('labels_in')) for t in b['tables']], b['title'], measured,
                                              free_area(fmt, orient))
        b['_fit'][fmt] = c
    return b['_fit'][fmt]


ARRANGEMENTS = {'tall': (0.42, 1.0), 'wide': (1.0, 0.45)}   # arrangement variants of a block (besides the sheet-aware one): arranged for this share of the free sheet width/height
MAX_COMBOS = 1500                                              # at most this many packing attempts per variant and format (order: as many blocks as possible in the current arrangement)


def _alternatives(b, fmt, orient):
    """Further arrangements of the block b for the format: tall and wide (pl.arrange with a narrow or flat sheet area); arrangements of the same size and
    position as existing ones are dropped. Result remembered per format; empty without remembered inputs."""
    if '_arr' not in b:
        return []
    if fmt not in b['_alt']:
        ctx, k, measured = b['_arr']
        W, H = free_area(fmt, orient)
        have, out = [_fitted(b, fmt, orient)], []
        efforts = [(fw, fh, 1) for fw, fh in ARRANGEMENTS.values()]
        if measured and (len(b['views']) >= 4 or any(v.get('section_line_in') for v in b['views'])):
            efforts.extend((fw, fh, 10) for fw, fh in [(1., 1.), *ARRANGEMENTS.values()])
        extents = _extents(b['views'], ctx['A'])
        tables = [(t['_name'], 88.0, t['_h'], t.get('labels_in')) for t in b['tables']]
        for fw, fh, width in efforts:
            more = []
            best = pl.arrange(b['views'], b['anns'], extents, k, tables, b['title'], measured,
                              (W * fw, H * fh), search_width=width, variants=more)
            for w, h, lay in [best, *more]:
                if not any(abs(w-x['w']) < 1e-6 and abs(h-x['h']) < 1e-6 and lay == x['lay'] for x in have):
                    c = {**b, 'w': w, 'h': h, 'lay': lay}
                    have.append(c)
                    out.append(c)
        b['_alt'][fmt] = out
    return b['_alt'][fmt]


def _sized(b):
    """(name, b, h, cy) for pl.pack: cy = height of the geometry centre of the first view above the block lower edge (row axis, N1.8); None without a view with a fixed position."""
    first = ((b.get('lay') or {}).get(b['views'][0]['id']) or {}).get('at') if b.get('views') else None
    ys = ((b.get('lay') or {}).get(pl.AXES_KEY) or {}).get('ys')          # axes of all views, the first (anchor) in front
    return (b['name'], b['w'], b['h'], tuple(float(y) for y in ys) if ys else float(first[1]) if first else None)


def _sized_blocks(blocks, lines, ctx):
    sized = [_sized(b) for b in blocks]
    if lines:
        sized.insert(1 if ctx['assembly'] else 0, ('notes', *_notes_size(lines)))
    return sized


def _pack_corners(fitting, lines, ctx, fmt, orient, first):
    """A measured main group may accept other complete blocks at its free outer corners. N1.9 (soft): if the assembly stands between part blocks in doing so,
    further positions are tried (ctx['_zone_tries'] counts them); if none finds the rule, the first position stays."""
    owners = []
    for i, choices in enumerate(fitting):
        for choice in choices:
            bands = choice.get('lay', {}).get(pl.FOOTPRINT_KEY)
            if bands:
                area = sum((r-l)*(t-b) for l,b,r,t in bands)
                saving = choice['w']*choice['h'] - area
                if saving > 1e-6:
                    owners.append((saving, i, choice, bands, area))
    keep = None
    for _, i, owner, bands, area in sorted(owners, key=lambda entry: -entry[0]):
        firsts = [cs[0] for cs in fitting]
        smallest = [min(cs, key=lambda c: c['w']*c['h']) for cs in fitting]
        variants = [firsts]
        if any(a is not b for j,(a,b) in enumerate(zip(firsts,smallest)) if j != i):
            variants.append(smallest)
        for cur in variants:
            cur[i] = owner
            sized = _sized_blocks(cur, lines, ctx)
            others = [item for item in sized if item[0] != owner['name']]
            zone = None
            if ctx.get('assembly') and any(b['name'] == pl.ASM_BLOCK for b in cur):
                sizes, rects = {n: (w, h) for n, w, h, *_ in sized}, _zone_rects(cur)
                zone = (pl.ASM_BLOCK, lambda full, sizes=sizes, rects=rects: not pl.zone_blocked(full, sizes, rects))
            if keep is not None:
                ctx['_zone_tries'] = ctx.get('_zone_tries', 0) + 1
            at = pl.pack_in_corners(_sized(owner), bands, others, fmt, orient, first, zone=zone)
            if at is not None:
                cur[i] = {**owner, '_occupied_area': area}
                if zone is None or zone[1](at):
                    return cur, at
                keep = keep or (list(cur), at)
    return keep


def _zone_rects(blocks):
    """{block: [(l, b, r, t)]} view rectangles for pl.zone_blocked from RECTS_KEY of the arrangement: for the assembly the views with item numbers, for part blocks the views with
    dimensions (annotations of type pl.SIZE_TYPES). Blocks without RECTS_KEY (older arrangements) are missing: the block box applies there."""
    out = {}
    for b in blocks:
        rects = (b.get('lay') or {}).get(pl.RECTS_KEY)
        if rects is None:
            continue
        kinds = ('balloon',) if b['name'] == pl.ASM_BLOCK else pl.SIZE_TYPES
        carrying = {a.get('view') for a in b.get('anns') or [] if a.get('type') in kinds}
        out[b['name']] = [tuple(r) for vid, r in rects.items() if vid in carrying]
    return out


def _zone_fix(ctx, sized, rects, fmt, orient, stack_h, at):
    """N1.9 (soft): if the assembly lies between part blocks in the packing at (pl.zone_blocked), it first goes into a corner instead, the remaining blocks afterwards (pl.pack_zone);
    without a violation at stays unchanged (byte-identical), without a corner position likewise (never None where a position exists). ctx['_zone_tries'] counts the additional packing attempts."""
    if not ctx.get('assembly') or pl.ASM_BLOCK not in at:
        return at
    sizes = {n: (w, h) for n, w, h, *_ in sized}
    if not pl.zone_blocked(at, sizes, rects):
        return at
    ctx['_zone_tries'] = ctx.get('_zone_tries', 0) + 1
    got = pl.pack_zone(sized, fmt, orient, stack_h, rects=rects)
    return got if got is not None else at


def _zoned(ctx, blocks, lines, fmt, orient, first, at):
    """_zone_fix for blocks of the arrangement (view rectangles from RECTS_KEY)."""
    return _zone_fix(ctx, _sized_blocks(blocks, lines, ctx), _zone_rects(blocks), fmt, orient, first, at)


def _zone_note(packed, blocks=None):
    """Planner note if the final state places the assembly between part blocks (N1.9, no position without a violation found); packed {block: (x, y, b, h, …)} of a sheet, blocks the blocks
    with their view rectangles (otherwise block boxes). None without a violation."""
    at = {n: tuple(p[:2]) for n, p in packed.items()}
    sizes = {n: tuple(p[2:4]) for n, p in packed.items()}
    if not pl.zone_blocked(at, sizes, _zone_rects(blocks or [])):
        return None
    return 'Assembly stands between dimensioned part views (N1.9): no position without a violation found, the previous position stays'


def _arranged(ctx, blocks, lines, fmt, orient, first):
    """(blocks, positions) of the first combination of arrangements that packs: first all blocks sheet-aware (as before), then with as few
    deviating blocks as possible (tall, wide); None if no combination packs. N1.9: if the assembly stands between part blocks in the packed position, it is
    put into a corner (_zoned); the combination and thus format and scale stay as they were."""
    base = [_fitted(b, fmt, orient) for b in blocks]
    at = pl.pack(_sized_blocks(base, lines, ctx), fmt, orient, first)
    if at is not None:
        return base, _zoned(ctx, base, lines, fmt, orient, first, at)
    W, H = free_area(fmt, orient)
    alts = [_alternatives(b, fmt, orient) for b in blocks]
    fitting = [[c for c in [b] + alt if c['w'] <= W + 1e-6 and c['h'] <= H + 1e-6] for b, alt in zip(base, alts)]
    notes = _notes_size(lines)[0] * _notes_size(lines)[1] if lines else 0.0
    if not all(fitting):
        return None

    def corners():
        got = _pack_corners(fitting, lines, ctx, fmt, orient, first)
        return None if got is None else (got[0], _zoned(ctx, got[0], lines, fmt, orient, first, got[1]))
    if sum(min(c['w'] * c['h'] for c in opts) for opts in fitting) + notes > W * H:
        return corners()
    idx = [i for i, a_ in enumerate(alts) if a_]
    choices = (dict(zip(indices, picks)) for n in range(1, len(idx)+1)
               for indices in itertools.combinations(idx, n)
               for picks in itertools.product(*(alts[i] for i in indices)))
    for selection in itertools.islice(choices, MAX_COMBOS):
        cur = [selection.get(i, b) for i,b in enumerate(base)]
        at = pl.pack(_sized_blocks(cur, lines, ctx), fmt, orient, first)
        if at is not None:
            return cur, _zoned(ctx, cur, lines, fmt, orient, first, at)
    return corners()


def _remeasured(ctx, content, scale, measured):
    """Content with blocks rearranged from the measured occupancy (views, dimensions and notes stay)."""
    blocks, lines = content
    out = [dict(b) for b in blocks]
    for b in out:
        _arrange(ctx, b, b['_arr'][1] if '_arr' in b else an.scale_value(scale), measured)
    return out, lines


def _relabel(items, labels):
    """Reassign the label letters of sections, detail views and auxiliary views without gaps (after dropping), in the
    order of the original assignment; view ids, references (..._in), annotations and tables follow. items:
    [(views, annotations, tables)] of all parts; without gaps nothing changes."""
    order = {l: i for i, l in enumerate(itertools.islice(_letters(), 100 * len(LETTERS)))}
    own = lambda v: (v.get('detail') or v.get('auxiliary') or (v.get('section') if not v.get('detail') else None) or {}).get('label')
    olds = sorted({own(v) for views, _, _ in items for v in views if own(v)}, key=lambda l: order.get(l, len(order)))
    new = {old: next(labels) for old in olds}
    ids = {}
    for views, _, _ in items:
        for v in views:
            old = own(v)
            if old and new[old] != old and v['id'].endswith('-' + old):
                ids[v['id']] = v['id'][:-len(old)] + new[old]
    for views, anns, tables in items:
        for v in views:
            for key in ('detail', 'auxiliary', 'section'):
                if isinstance(v.get(key), dict) and v[key].get('label') in new:
                    v[key] = {**v[key], 'label': new[v[key]['label']]}
            for key in ('id', 'section_line_in', 'detail_circle_in', 'arrow_in'):
                if v.get(key) in ids:
                    v[key] = ids[v[key]]
        for a in anns:
            if a.get('view') in ids:
                a['view'] = ids[a['view']]
        for t in tables:
            if t.get('labels_in') in ids:
                t['labels_in'] = ids[t['labels_in']]
    return ids


def _rename_ids(text, ids):
    """Carry view IDs in planner notes forward to the assigned label letters after _relabel (whole IDs only)."""
    if not ids:
        return text
    pat = re.compile(r'(?<![A-Za-z0-9_-])(?:' + '|'.join(re.escape(k) for k in sorted(ids, key=len, reverse=True)) + r')(?![A-Za-z0-9_-])')
    return pat.sub(lambda m: ids[m.group(0)], text)


PART_LETTERS = 30        # label letter reserve per part in the part cache (part j draws from position j · PART_LETTERS; _relabel then reassigns without gaps)


def _part_content(ctx, pid, j, kp, main_pid):
    """(views, dimensions, notes, tables, dropped views, planner notes) of the drawn part pid at scale kp (value) with the main view
    main_pid, from the part cache: computed once per (part, scale, main view) (views, dimensioning), reused in every combination. The return values
    are copies (_relabel, title and arrangement change them). j = position of the part in ctx['drawn']: its label letter reserve starts at j · PART_LETTERS,
    independent of the other parts; only _relabel assigns the letters of the sheet without gaps in the order of the parts (as before)."""
    cache = ctx.setdefault('_part_cache', {})
    key = (pid, kp, json.dumps(main_pid, sort_keys=True))
    if key not in cache:
        cache[key] = _build_part_content(ctx, pid, j, kp, main_pid)
    return copy.deepcopy(cache[key])


def _section_oblique_profiles(ctx, pid, views):
    """Show a hidden slant elevation at the same place as a designated auxiliary section."""
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree
    by_id = {f['id']: f for f in ctx['P'][pid]['feats']}
    for v in views:
        if not v.get('auxiliary') or v.get('section') or not v.get('arrow_in'):
            continue
        members = [by_id[i] for i in v.get('aux_members', [])]
        if not members or any(f['kind'] != 'slant' or not f.get('_outline') for f in members):
            continue
        frame = prj.frame_of(v)
        N = frame[0]
        host = next((u for u in views if not u.get('auxiliary') and not u.get('detail') and
                     abs(prj.frame_of(u)[0] @ N) < 1e-6), None)
        if host is None:
            continue
        cache = ctx.setdefault('_profile_sections', {})
        key = (tuple(f['id'] for f in members), tuple(N.round(8)))
        if key not in cache:
            shapes = [ctx['shapes'][pid]]
            ends = np.vstack([pd._slant_segment(f, N) for f in members])
            projected = [Point(p) for p in prj.project(ends, frame)]
            def exposed(shapes):
                lines = STRtree([LineString(p) for p in prj.hlr(shapes, frame, hidden=False)[0] if len(p) >= 2])
                return all(lines.query(p, predicate='dwithin', distance=0.04).size for p in projected)
            cut = None
            if not exposed(shapes):
                front = float(np.max(ends @ N))
                limit = min(float(np.max(np.asarray(f['_outline']) @ N)) for f in members)
                for depth in (front + 0.05, (front+limit)/2, front):
                    if depth >= limit - 1e-4:
                        continue
                    pt = ends[0] + (depth - ends[0] @ N) * N
                    kept, faces = prj.section(shapes, frame, pt)
                    if faces and exposed(kept):
                        cut = _r(pt)
                        break
            cache[key] = cut
        if cache[key] is not None:
            v['section'] = {'point': cache[key], 'label': v['auxiliary']['label']}
            v['section_line_in'] = host['id']
            del v['arrow_in'], v['arrow_at']


def _part_planes(ctx, pid):
    """Whole-part symmetry planes {world axis: coordinate} of the part pid for the dimensioning: the same answer as the symmetry lines (symmetry.part_symmetry, cache ctx['symmetry']). Empty without a plane or on an
    error of the detection (OCP): then the planner dimensions as before."""
    try:
        import symmetry
        return dict(symmetry.part_symmetry(ctx, pid)[1])
    except Exception:
        return {}


def _build_part_content(ctx, pid, j, kp, main_pid):
    letters = itertools.islice(_letters(), j * PART_LETTERS, None)
    P = ctx['P'][pid]
    views = pv.part_views(P['part'], P['cls'], P['feats'], kp, letters, main=main_pid, ray=lambda p, N, n, tmax=None: _ray(ctx, pid)(p, N, n), sym=_part_planes(ctx, pid) if P['role'] == 'manufactured' else None)      # ray test only built for a pattern detail (N4.9)
    _section_oblique_profiles(ctx, pid, views)
    for pr_id in dict.fromkeys(i for v in views for i in v.get('profile_unbuilt') or []):      # no view along d buildable: residual members stay in the row “Undimensioned forms”
        item = f"Profile {pr_id} of {ctx['who'](pid)}: generatrix {np.round(next(p_['profile']['axis'] for p_ in P['part']['profiles'] if p_['profile']['id'] == pr_id), 3).tolist()} lies in no viewing direction of the views of the part, no view along the generatrix buildable - residual faces stay in the row “Undimensioned forms according to 3D model” (N4.10)"
        if item not in ctx['open']:
            ctx['open'].append(item)
    pruned, added = [], []
    if P['role'] == 'manufactured':
        dim = lambda vs, P=P, pid=pid: pd.dimension(P['part'], P['cls'], P['feats'], vs, P['datum'], P['tolerances'], ctx['model_ref'],
                                                                   P['texts'], ctx['function_dims'], ray=_ray(ctx, pid), k=kp,
                                                                   sym=_part_planes(ctx, pid))
        unchecked = []
        views, (anns, dnotes, tables), pruned = pv.prune_views(views, P['feats'], kp, dim, unchecked, lambda vs, P=P: _extents(vs, {P['part']['id']: P['part']}))
        added += [f'{pid}: {u}' for u in unchecked]
        excess = lambda ns: sum(n.get('excess', 0) for n in ns if isinstance(n, dict) and pd.BUDGET_NOTE in n)
        needs_pview = lambda ns: sum(1 for n in ns if isinstance(n, dict) and n.get('needs_profile_view'))      # envelope dimension without a view with two visible ends
        if needs_pview(dnotes):         # at most one profile view per part (side view in whose image plane the axis of the dimension lies); only afterwards the stack budget
            need = next((n.get('axis') for n in dnotes if isinstance(n, dict) and n.get('needs_profile_view') and n.get('axis') is not None), None)
            extra = pv.extra_view(P['part'], P['feats'], views, need)
            got = dim(views + [extra]) if extra else None
            if got is not None and needs_pview(got[1]) < needs_pview(dnotes) and excess(got[1]) <= excess(dnotes) + 1:
                views, (anns, dnotes, tables) = views + [extra], got
                added.append(f"{pid}: profile view {extra['id']} added (envelope/range dimension without a view with two visible ends, fallback dropped; one view more)")
        for _ in range(BUDGET_VIEWS if not any('profile view' in a for a in added) else 1):     # stack budget cannot be kept and no existing view shows the elements -> further normal view (after a profile view at most one, total view count ≤ +2)
            if not excess(dnotes):
                break
            extra = pv.extra_view(P['part'], P['feats'], views)
            if extra is not None and kp >= 2.0:      # (only with an enlarged main scale ≥ 2:1, where the additional view costs sheet area; at 1:1 it only led to more views and text collisions): the budget additional view carries only surplus dimensions, not the main representation: one standard step smaller (block area) so that the sheet does not grow
                half = next((an.scale_str(sc) for sc in an.SCALES if abs(an.scale_value(an.scale_str(sc)) - kp / 2) < 1e-9), None)
                if half:
                    extra['scale'] = half
            got = dim(views + [extra]) if extra else None
            if got is None or excess(got[1]) >= excess(dnotes):
                break
            views, (anns, dnotes, tables) = views + [extra], got
            added.append(f"{pid}: view {extra['id']} added (stack budget {checks.BUDGET_VIEW} per view: no existing view shows the surplus entries)")
        for g in pv.undetailed_patterns(P['feats'], views, pid, kp, sym=_part_planes(ctx, pid)):        # pattern group without detail (mirrored groups excluded via sym): dimensions in the normal views, N4.9 open
            added.append(f"{pid}: plane pattern {', '.join(g['ids'])} ({g['count']}× {pd.fmt(g['pitch'])}, characteristic length {pd.fmt(g['char_len'])} mm) without detail (quota {pv.PATTERN_DETAIL_MAX} or cap {pv.PATTERN_DETAIL_MAX_R:g} mm reached): dimensions in the normal views (N4.9 open)")
    else:
        anns, dnotes, tables = [], [], []
    pnotes = [n[pd.BUDGET_NOTE] for n in dnotes if isinstance(n, dict) and n.get(pd.BUDGET_NOTE)]       # planner notes of the stack budget, not sheet text
    ctx.setdefault('_dleft', {})[(pid, kp, json.dumps(main_pid, sort_keys=True))] = sum(n.get('derivable_left', 0) for n in dnotes if isinstance(n, dict))      # rank test as a message, candidate metric derivable_left
    dnotes = [n for n in dnotes if not (isinstance(n, dict) and pd.BUDGET_NOTE in n)]
    for n in dnotes:            # tolerance without an unambiguous dimension: note line and open item (do not guess); once per cache entry, identical lines are dropped
        if isinstance(n, str) and n.startswith(pd.TOL_NOTE):
            item = f"Tolerance {n[len(pd.TOL_NOTE):]} of {ctx['who'](pid)} cannot be assigned unambiguously to a dimension (note only)"
            if item not in ctx['open']:
                ctx['open'].append(item)
    return views, anns, dnotes, tables, pruned, added + pnotes


def _content(ctx, scale, main=None, bscales=None):
    """(blocks, note lines) of a variant; block = {name, title, views, anns, tables, w, h, lay} (positions from
    arrange). Title line: “Assembly” or in assemblies always “Item n designation (a:b)” with the scale of the block (without “M ”). Detail views and
    sections without an element of their own that they cover are dropped (plan_views.prune_views), the label letters are then reassigned without gaps.
    The part cache (_part_content) holds views and dimensioning per (part, scale, main view).
    bscales {part: scale}: part block with its own standard scale - views, dimensions and arrangement of the part at this scale, all views without
    their own scale get it (sections carry no scale of their own, deviating detail views do), assembly with its own scale “Assembly (a:b)”;
    plus, only if a block deviates, a note line “Scale …, unless stated otherwise”."""
    k = an.scale_value(scale)
    bscales = bscales or {}
    letters, blocks, lines = _letters(), [], []
    if ctx['assembly']:
        ba = bscales.get('asm')                 # assembly at a smaller block scale (main part large, _main_large)
        blocks.append({'name': 'asm', 'title': 'Assembly' + (f' ({ba})' if ba else ''), 'bscale': ba,
                       'views': [dict(v, scale=ba) for v in ctx['asm_views']] if ba else ctx['asm_views'], 'anns': _balloons_at(ctx, an.scale_value(ba or scale)), 'tables': []})
    per_part, budget_notes, kp_of = [], {}, {}
    for j, pid in enumerate(ctx['drawn']):
        kp = kp_of[pid] = an.scale_value(bscales.get(pid, scale))
        views, anns, dnotes, tables, pruned, added = _part_content(ctx, pid, j, kp, (main or {}).get(pid))
        budget_notes[pid] = added
        per_part.append((pid, views, anns, dnotes, tables, pruned))
    ids = _relabel([(v, a, t) for _, v, a, _, t, _ in per_part], _letters())
    if ids:
        for pid in budget_notes:
            budget_notes[pid] = [_rename_ids(n, ids) for n in budget_notes[pid]]
    used = set().union(*[_used_letters(v) for _, v, _, _, _, _ in per_part]) if per_part else set()
    free_letters = (c for c in _letters() if c not in used)         # names of freely placed views without duplicate assignment on the sheet
    if ctx['assembly'] and blocks:
        asm, ka = blocks[0], an.scale_value(blocks[0].get('bscale') or scale)
        abb = _union_box([ctx['A'][i]['bbox'] for i in ctx['asm_views'][0]['parts']])
        asm['views'] = [{k_: x for k_, x in v.items() if k_ not in ('named', 'arrow_in', 'arrow_at', 'label')} for v in asm['views']]    # fresh per variant (letters)
        _name_free(asm['views'], ka, free_letters, abb)
        _titles(asm['views'])
        asm['anns'] = list(asm['anns']) + _marks(asm['views'], abb, ka)
    for pid, views, anns, dnotes, tables, pruned in per_part:
        P = ctx['P'][pid]
        _name_free(views, an.scale_value(bscales.get(pid, scale)), free_letters, P['part']['bbox'])
        _titles(views)
        rows = len(next((b['points'] for b in P['part']['bend_tables'] if b.get('complete')), []))
        tabs = [{**t, '_name': f'{pid}#table{i + 1}', '_h': 5.0 * (rows + 2) + (BEND_SUBTITLE if t.get('bend_table') and t.get('labels_in') else 0.0)}
                for i, t in enumerate(tables)]
        views = pd.label_bend_views(views, tabs)      # the reference view of the bend table carries its name as a title
        bs = bscales.get(pid)
        name = _label_name(ctx['display'][pid])
        title = (f"Item {ctx['pos'][pid]} {name} ({bs or scale})" if ctx['assembly'] else f"{name} ({bs})" if bs else None)
        marks = _marks(views, P['part']['bbox'], an.scale_value(bs or scale))
        pd.stagger_leaders(anns + marks, pd._views(views, pid), an.scale_value(bs or scale))      # against all texts of the view, also label letters
        if bs:                                     # views of the block at the block scale (detail views, sections with own scale keep it)
            for v in views:
                if not v.get('detail') and not v.get('scale'):
                    v['scale'] = bs
        all_anns = anns + marks
        details = [v for v in views if (v.get('pattern_detail') and v.get('detail')) or v.get('profile_own')]       # the profile view (detail/section, N4.10) is also a block of its own; the pattern detail is a block of its own (no bounding box around block and circle)
        dids = {v['id'] for v in details}
        owner = _title_block(pid, views, details)               # N4.7: the title stands at the block with the main view (checks.part_main_view on the spec views; the check part_title_misplaced only requires any view of the part under the title)
        blocks.append({'name': pid, 'title': title if owner == pid else None, 'bscale': bs,
                       'derivable_left': ctx.get('_dleft', {}).get((pid, kp_of[pid], json.dumps((main or {}).get(pid), sort_keys=True)), 0),
                       'views': [v for v in views if v['id'] not in dids], 'anns': [a for a in all_anns if a.get('view') not in dids], 'tables': tabs,
                       'pruned': [f'{pid}: {i} dropped ({why})' for i, why in pruned] + budget_notes.get(pid, [])})
        for v in details:           # the circle (detail_circle in marks) stays with the host view, the dimensions follow their view; title: the view carries “Detail X (a:b)” itself
            blocks.append({'name': v['id'], 'detail_of': pid, 'title': title if owner == v['id'] else None, 'bscale': None, 'views': [v], 'anns': [a for a in all_anns if a.get('view') == v['id']], 'tables': [], 'pruned': []})
        pre = f"Item {ctx['pos'][pid]}: " if ctx['assembly'] else ''
        lines += [{**n, 'text': pre + n['text']} if isinstance(n, dict) else pre + n for n in dnotes]   # object: general note
    if any(b.get('bscale') for b in blocks):
        lines.append(f"Scale {scale}, unless stated otherwise; deviating: " + ', '.join(
            f"{('Item ' + str(ctx['pos'][b['name']]) + ' ') if ctx['assembly'] and b['name'] in ctx['pos'] else ''}{_label_name(ctx['display'].get(b['name'], 'Assembly'))} {b['bscale']}"
            for b in blocks if b.get('bscale')))
    for b in blocks:
        _arrange(ctx, b, an.scale_value(b.get('bscale') or scale))
    seen, out = set(), []                       # identical texts only once, first line wins
    for n in lines + ctx['lines']:
        if checks.note_text(n) not in seen:
            seen.add(checks.note_text(n))
            out.append(n)
    return blocks, out


def _title_block(pid, views, details):
    """Name of the block that carries the part title “Item n designation (scale)” of the part pid (N4.7): the part block (pid) or, if the main view of the part (checks.part_main_view on the
    spec views; the check part_title_misplaced does not call it) is split off into a block of its own (profile view, pattern detail), the ID of this view (name of its block)."""
    main = checks.part_main_view([_spec_view(v, {}) for v in views])        # on the spec views, i.e. the fields the check reads in the spec (named views without viewing arrow are not an auxiliary view there)
    return main['id'] if main is not None and any(main['id'] == d['id'] for d in details) else pid


def _spec_view(v, place):
    """ViewPlan -> spec view: only fields the core understands (no role/features/..._in)."""
    out = {'id': v['id'], 'parts': [v['part']] if v.get('part') else list(v.get('parts') or [])}
    out.update({key: v[key] for key in ('preset', 'direction', 'up', 'thin_parts', 'label') if key in v})
    if v.get('section'):
        out['section'] = {'point': list(v['section']['point']), **({'label': v['section']['label']} if v['section'].get('label') else {})}      # axial section without a label letter
    if v.get('auxiliary') or (v.get('named') and v.get('arrow_in')):      # freely placed view “View X” with viewing arrow: label letter as for the auxiliary view (viewing arrow check); without a host only the title
        out['auxiliary'] = {'label': (v.get('auxiliary') or v['named'])['label']}
    if v.get('detail'):
        d = v['detail']
        out['detail'] = {'center': list(d['center']), 'radius': d['radius'], 'label': d['label']}
        out['scale'] = d['scale']
    elif v.get('scale'):
        out['scale'] = v['scale']
    return {**out, **place}


def _notes_size(lines):
    return max(pl.text_width(checks.note_text(t), NOTE_H) for t in lines) + 2.0, (len(lines) - 1) * NOTE_STEP + 6.0


def _variant(ctx, content, scale, fmt, multi=False):
    """{spec, packed} of the variant on one sheet fmt or (multi) on follow-up sheets of the same size; None if
    it does not fit. Follow-up sheets: assembly and notes first, then the parts, as many per sheet as fit."""
    blocks, lines = content
    extra = {'derivable_left': sum(int(b.get('derivable_left') or 0) for b in blocks)}      # rank test as a message (metric only, not in the score)
    orient = 'portrait' if fmt == 'A4' else 'landscape'
    kw = min(190.0, sheet_size(fmt, orient)[0] - 30.0) / 190.0        # parts list: rows grow with wrapped texts (sheet.parts_list_rows)
    first = STACK + (parts_list_height(ctx['rows'], kw, ROW_H) if ctx['rows'] else 0.0)
    if not multi:                       # per block up to three arrangements; the first combination that packs applies (the current one first)
        got = _arranged(ctx, blocks, lines, fmt, orient, first)
        if got is None:
            return None
        blocks, at = got
    else:
        blocks = [_fitted(b, fmt, orient) for b in blocks]
    sized = _sized_blocks(blocks, lines, ctx)
    if not multi:
        places = [at]
    else:
        groups = [[]]
        for item in sized:
            if pl.pack(groups[-1] + [item], fmt, orient, first if len(groups) == 1 else STACK) is None and groups[-1]:
                groups.append([])
            groups[-1].append(item)
        places = [pl.pack(g, fmt, orient, first if i == 0 else STACK) for i, g in enumerate(groups)]
        rects = _zone_rects(blocks)
        places = [p if p is None else _zone_fix(ctx, g, rects, fmt, orient, first if i == 0 else STACK, p) for i, (g, p) in enumerate(zip(groups, places))]       # N1.9 also on follow-up sheets
    if any(p is None for p in places):
        return None
    sizes = {name: (w, h) for name, w, h, *_ in sized}
    occupied = {b['name']: b['_occupied_area'] for b in blocks if '_occupied_area' in b}
    sheets, packed = [], {}
    for si, at in enumerate(places):
        sd = {'format': fmt, 'orientation': orient, 'scale': scale, 'views': [], 'annotations': [], 'tables': []}
        if si == 0 and ctx['rows']:
            sd['parts_list'] = [dict(r) for r in ctx['rows']]
        if si == 0 and ctx.get('function_values'):      # (missing in hand-built test contexts: then no key)
            sd[checks.FUNCTION_VALUES_KEY] = {pid: {k: list(v) for k, v in e.items()} for pid, e in ctx['function_values'].items()}
        if si == 0 and ctx.get('thread_names'):         # thread designations of the model texts for parts with a thread hint without designation (checks.spec_thread_designations)
            sd[checks.THREAD_NAMES_KEY] = {pid: list(v) for pid, v in ctx['thread_names'].items()}
        for b in blocks:
            if b['name'] not in at:
                continue
            x, y = at[b['name']]
            vds = {}
            for v in b['views']:
                lay = b['lay'][v['id']]
                vds[v['id']] = _spec_view(v, {'at': [round(x + lay['at'][0], 2), round(y + lay['at'][1], 2)]} if 'at' in lay else {'place': dict(lay['place'])})
            sd['views'] += [vds[v['id']] for v in pl.ordered(b['views'], b['lay'])]       # reference views before their neighbours
            sd['annotations'] += b['anns']
            if b['title']:
                tx, ty = b['lay'][pl.TITLE_KEY]['at']
                sd['annotations'].append({'type': 'text', 'at_sheet': [round(x + tx, 2), round(y + ty, 2)], 'text': b['title'],
                                          'h': pl.TITLE_H, 'block_title': True, 'max_width': round(sizes[b['name']][0] - 2.0, 2)})
            for t in b['tables']:
                tx, ty = b['lay'][t['_name']]['at']
                sd['tables'].append({**{key: val for key, val in t.items() if not key.startswith('_')},
                                     'at': [round(x + tx, 2), round(y + ty - 5.0 - (BEND_SUBTITLE if t.get('bend_table') and t.get('labels_in') else 0.0), 2)]})   # title (and reference line) above the header row
        if 'notes' in at:
            x, y = at['notes']
            sd['notes'] = {'lines': list(lines), 'at': [round(x, 2), round(y + sizes['notes'][1] - 4.5, 2)], 'h': NOTE_H}
        if not sd['tables']:
            del sd['tables']
        sheets.append(sd)
        for name, xy in at.items():
            packed[name] = (*xy, *sizes[name], occupied[name]) if name in occupied else (*xy, *sizes[name])
    spec = {'title': dict(ctx['title']), 'company': list(ctx['company']), 'parts': [dict(p) for p in ctx['spec_parts']], 'sheets': sheets}
    return {'spec': json.loads(json.dumps(spec)), 'packed': packed, 'extra': extra}


# ------------------------------------------------------------------ size and scale from measured block sizes

def _drawing():
    """The core module (probe). If the planner runs via drawing.py plan, it is already loaded."""
    import importlib
    return sys.modules.get('drawing') or importlib.import_module('drawing')


def truncation_notes(findings):
    """Planner notes on shortened texts (finding text_truncated, checks.cell_overflow_findings): full wording and place."""
    out = []
    for f in findings:
        if f.get('code') == 'text_truncated':
            note = f"Text in {f.get('cell')} shortened (shown with \"...\"); full text: \"{f.get('text')}\""
            if note not in out:
                out.append(note)
    return out


class _Prober:
    """Lean probe (drawing.layout_probe, without image) for measured block sizes; one Drawing for all probes. If it
    fails, the estimate stays (measured False, error with reason)."""

    def __init__(self, ctx, spec):
        self.ctx, self.spec, self.dr, self.n, self.measured, self.error = ctx, spec, None, 0, True, None
        self.geom = {}          # {view: (b, h)} geometry in model mm (bounding rectangle of the drawn edges), from the view_frames of the last probe per view (N1.4 computes with real areas)
        self.parts = {p['id']: p for p in spec.get('parts') or []}

    def drawing(self, vspec):
        """The one Drawing of the probe (built from the first spec, parts with their outputs)."""
        if self.dr is None:
            self.dr = _drawing().Drawing({**self.spec, **vspec, 'strict': True, 'parts': [{**self.parts.get(p['id'], {}), **p} for p in vspec['parts']]})
        return self.dr

    def axes(self, vspec):
        """Planner step symmetry axes (N2.2, symmetry.add_axes): adds the lines to vspec; idempotent. If it failed (measurement not possible), vspec stays unchanged."""
        if not self.measured:
            return False
        try:
            import symmetry
            symmetry.add_axes(vspec, self.ctx, self.drawing(vspec))
            return True
        except Exception as e:
            self.measured, self.error = False, f'{type(e).__name__}: {e}'[:160]
            return False

    def probe(self, vspec, place_labels=False):
        if not self.measured:
            return None
        try:
            full = {**self.spec, **vspec, 'strict': True,
                    'parts': [{**self.parts.get(p['id'], {}), **p} for p in vspec['parts']]}
            mod = _drawing()
            self.drawing(vspec)
            if place_labels:
                self.axes(vspec)             # the probe measures with the axes (they occupy room); the same idempotent step as the explicit planner step
            res = mod.layout_probe(full, self.dr, place_labels=True) if place_labels else mod.layout_probe(full, self.dr)
            self.n += 1
            for vid, fr in (res.get('view_frames') or {}).items():
                g, k = fr.get('geom_bbox_mm'), float(fr.get('scale') or 0.0)
                if g and k > 0:
                    self.geom[vid] = ((g[2] - g[0]) / k, (g[3] - g[1]) / k)
            return res
        except Exception as e:           # measurement failed: estimate (noted in the result)
            self.measured, self.error = False, f'{type(e).__name__}: {e}'[:160]
            return None


TEXT_CODES = ('text_on_line', 'text_on_geometry', 'text_overlap')      # the findings that label_layout.place (title and dimension line move) addresses
SETTLE_ROUNDS = 3


def _probe_settled(prober, vspec, rounds=SETTLE_ROUNDS):
    """Probe with label placement (place_labels) that does not count a solvable title/label conflict as blocking. label_layout.place writes its moves back into vspec
    but does not manage everything in every move (moving a title first clears the room on which the dimension line of the next may change sides, and the title fallback needs
    the cleaned state). If a finding from TEXT_CODES remains after the probe, the same move is measured again as long as the blocking count falls (at most `rounds` repetitions). The finding
    is not hidden: whoever cannot be resolved stays in the blocking count. Not measurable: None like prober.probe."""
    res = prober.probe(vspec, place_labels=True)
    for _ in range(rounds):
        if not res or res.get('blocking') is None or not any(f['severity'] == 'block' and f['code'] in TEXT_CODES for f in res.get('findings') or []):
            break
        nxt = prober.probe(vspec, place_labels=True)
        if not nxt or nxt.get('blocking') is None:
            break
        gain = res['blocking'] - nxt['blocking']
        res = nxt
        if gain <= 0:
            break
    return res


def _occupancy_of(vspec, probe):
    """{view: (left, bottom, right, top)} from the geometry center, from the hull of geometry, centerlines and all
    annotations of the view in the probe (without title)."""
    out = {}
    for si, sd in enumerate(vspec['sheets']):
        ps = probe['sheets'][si]
        for vd in sd.get('views') or []:
            vb = ps['view_boxes'].get(vd['id'])
            if not vb:
                continue
            g = vb['geom']
            cx, cy, hull = (g[0] + g[2]) / 2, (g[1] + g[3]) / 2, list(vb['box'])
            for i, a in enumerate(sd.get('annotations') or []):
                bx = ps['ann_boxes'].get(str(i)) if a.get('view') == vd['id'] else None
                if bx and not a.get('compact'):         # the viewing arrow of a freely placed view does not count towards the occupancy (stands in the gap, costs no sheet scale)
                    hull = [min(hull[0], bx[0]), min(hull[1], bx[1]), max(hull[2], bx[2]), max(hull[3], bx[3])]
            margins = (cx - hull[0], cy - hull[1], hull[2] - cx, hull[3] - cy)
            out[vd['id']] = pl.ViewMeasure(pl._view_key(vd), tuple(vb['model_bounds']), margins,
                                           pl.view_scale(vd, an.scale_value(sd.get('scale', '1:1'))))
    return out


def _smallest(ctx, content, scale, order):
    """Variant on the smallest size on which the content fits on one sheet, otherwise None."""
    return next((v for fmt in order for v in [_variant(ctx, content, scale, fmt)] if v), None)


def _fmt_of(v):
    return v['spec']['sheets'][0]['format']


def _limits(ctx, c, scale):
    """Metrics of the hard limits of a content c at sheet scale scale: {min_dim, min_detail, density, bad} - smallest dimension of a
    normal view, smallest dimension in a detail view, largest annotation density and the blocks (names) that violate a limit."""
    k, out = an.scale_value(scale), {'min_dim': None, 'min_detail': None, 'min_len': None, 'density': 0.0, 'bad': []}
    lower = lambda a, b: b if a is None or (b is not None and b < a) else a
    for b in c[0]:
        kb = b['_arr'][1] if '_arr' in b else k
        d, dd = pl.min_dim(b['anns'], b['views'], kb), pl.min_detail(b['anns'], b['views'], kb)
        y = pl.density(b['anns'], b['views'], _extents(b['views'], ctx['A']), kb)
        if pl.violations(d, dd, y):
            out['bad'].append(b['name'])
        out['min_dim'], out['min_detail'], out['density'] = lower(out['min_dim'], d), lower(out['min_detail'], dd), max(out['density'], y)
        out['min_len'] = lower(out['min_len'], pl.min_len(b['anns'], b['views'], kb))
    return out


def _too_short(lim):
    """Soft criterion: a length dimension of a normal view is shorter than X_MIN_MM on paper."""
    return lim.get('min_len') is not None and lim['min_len'] < pl.X_MIN_MM - 1e-9


def _fit_text(blocks, fmt_):
    """Why the content does not fit the size: block area against free sheet area (blocks arranged sheet-aware per size) and
    largest block against the free width/height."""
    orient = 'portrait' if fmt_ == 'A4' else 'landscape'
    fb = [x for x in (_fitted(b, fmt_, orient) for b in blocks) if 'w' in x and 'h' in x]
    W, H = free_area(fmt_, orient)
    if not fb:
        return f'no content (free area {W:.0f} × {H:.0f} mm)'
    area = sum(b['w'] * b['h'] for b in fb)
    single = all(b['w'] <= W + 1e-6 and b['h'] <= H + 1e-6 for b in fb)
    return (f"Blocks {area:.0f} mm² against free area {W * H:.0f} mm² (fill {area / (W * H):.2f}), largest block {max(b['w'] for b in fb):.0f} × "
            f"{max(b['h'] for b in fb):.0f} mm against {W:.0f} × {H:.0f} mm" + ('; each block fits on its own, but not together with the note block and title block zone'
                                                                         if single and area <= W * H else ''))


def _viol(lim, fill=None):
    return pl.violations(lim['min_dim'], lim['min_detail'], lim['density'], fill)


def _search(ctx, scales, order, sug, content, prober):
    """{variant, content, est, est_content, bscales, notes} of the base variant: candidates are all sizes with all standard scales
    around the proposal (UP_STEPS larger, DOWN_STEPS smaller); the sheet scale applies to all blocks (the former common step attempt for violating
    manufactured parts is dropped, _part_scales assigns own part scales). A candidate is admissible if the content fits and the limits pl.violations (smallest dimension X,
    detail view, density Y, fill) are kept. The smallest size with an admissible candidate is chosen (pl.pick), within it the largest
    sheet scale. First the estimate checks all candidates, only the best are measured (probes, MEASURED_MAX). Then the guards
    (estimate against measurement, format against the next larger one). If nothing fits on a sheet: follow-up sheets at the proposal scale as
    before; if no candidate is admissible, the fit decides (note with the violated limits)."""
    start = scales.index(sug['scale'])
    why, screened = {}, {}                # (index, size) -> violated limits or ['does not fit']; index -> candidate

    def screen(i):
        """Candidate scale i: per size the content at the sheet scale (no common step attempt any more; part scales only arise in _part_scales)."""
        scale, found = scales[i], None
        c0 = content(scale)
        lim0 = _limits(ctx, c0, scale)
        for fmt_ in order:
            v = _variant(ctx, c0, scale, fmt_)
            if v is None:
                why.setdefault((i, fmt_), ['does not fit: ' + _fit_text(c0[0], fmt_)])
                continue
            viol = _viol(lim0, pl.metrics(v['spec'], v['packed'])['fill'])
            why[(i, fmt_)] = viol
            if not viol:
                found = {'format': fmt_, 'index': i, 'ok': True, 'bs': None, 'short': _too_short(lim0)}
                break
        screened[i] = found or {'format': order[-1], 'index': i, 'ok': False, 'bs': None}

    def refine(cand):
        """Candidate measured: estimate -> probe -> measured blocks -> possibly larger size ...; None if a limit is violated afterwards."""
        scale = scales[cand['index']]
        mk = lambda: content(scale)
        c = mk()
        v = est = _smallest(ctx, c, scale, order)
        for _ in range(3):
            if v is None:
                return None
            pr = prober.probe(v['spec'])
            if pr is None:
                break
            c = _remeasured(ctx, mk(), scale, _occupancy_of(v['spec'], pr))
            w = _smallest(ctx, c, scale, order)
            if w is None:
                return None
            same = _fmt_of(w) == _fmt_of(v)
            v = w
            if same:
                break
        else:
            v = _variant(ctx, c, scale, order[min(len(order) - 1, order.index(_fmt_of(v)) + 1)]) or v
        lim = _limits(ctx, c, scale)
        for f_ in order[:order.index(_fmt_of(v))]:               # what the measurement says against the smaller sizes (the estimate allowed them)
            if _variant(ctx, c, scale, f_) is None:
                why[(cand['index'], f_)] = ['measured does not fit: ' + _fit_text(c[0], f_)]
        while cand.get('ok', True) and _viol(lim, pl.metrics(v['spec'], v['packed'])['fill']):
            why[(cand['index'], _fmt_of(v))] = ['measured: ' + '; '.join(_viol(lim, pl.metrics(v['spec'], v['packed'])['fill']))]
            nxt = order.index(_fmt_of(v)) + 1                     # fill too high after the measurement: next size
            v = _variant(ctx, c, scale, order[nxt]) if nxt < len(order) else None
            if v is None:
                return None
        return {'variant': v, 'content': c, 'est': est, 'est_content': mk(), 'bscales': {}, 'format': _fmt_of(v), 'index': cand['index'],
                'short': _too_short(lim), 'min_len': lim.get('min_len')}

    hi = min(len(scales), start + 1 + pl.DOWN_STEPS)
    for i in range(max(0, start - pl.UP_STEPS), hi):
        screen(i)
    while hi < len(scales) and pl.pick(list(screened.values()), order) is None:
        screen(hi)
        hi += 1
    big = order.index(pl.UP_FORMAT_MAX)      # maximum size of a single sheet; after the measurement no candidate above it merely because the limits (density) are kept there; above only if nothing fits on UP_FORMAT_MAX at all (fallback below)
    dropped = []                              # admissible candidates that only the measurement lifted above UP_FORMAT_MAX (for the note)
    ok = sorted((c for c in screened.values() if c['ok']), key=lambda c: (order.index(c['format']) > big, order.index(c['format']), c.get('short', False), c['index']))      # in the size first without too short length dimensions (soft), then the largest scale
    res, notes = None, []
    for n, cand in enumerate(ok):             # the best MEASURED_MAX are measured; if all drop out, the next candidate moves up
        if n >= MEASURED_MAX and res is not None:
            break
        got = refine(cand)
        if got is not None and order.index(got['format']) > big:
            dropped.append((got['format'], scales[got['index']]))      # measured above UP_FORMAT_MAX: no candidate (not larger merely because of the limits; fallback below)
            got = None
        if got is not None and (res is None or (order.index(got['format']), got['short'], got['index']) < (order.index(res['format']), res['short'], res['index'])):
            res = got
    if res is None:                  # no admissible candidate: the largest scale that still fits on UP_FORMAT_MAX (most legible remainder), limits as a note
        first = None
        for i in range(max(0, start - pl.UP_STEPS), len(scales)):
            got = refine({'index': i, 'bs': None, 'ok': False})
            first = first or got
            if got and order.index(got['format']) <= big:
                res = got
                break
        res = res or first
        if res and res['index'] + 1 < len(scales):      # one more scale on a larger size violates the same limit (smallest dimension) just the same: then the smaller size applies at the next standard step, the sheet does not grow without gain
            viol = lambda r_: _viol(_limits(ctx, r_['content'], scales[r_['index']]))
            small = lambda vs: bool(vs) and all(str(v_).startswith('smallest dimension') for v_ in vs)
            nxt = refine({'index': res['index'] + 1, 'bs': None, 'ok': False})
            if nxt and order.index(nxt['format']) < order.index(res['format']) and small(viol(res)) and small(viol(nxt)):
                res = nxt
        if res:
            res['fallback'] = order.index(res['format']) > big      # only if nothing really fits on UP_FORMAT_MAX (the fallback itself goes beyond UP_FORMAT_MAX): then the format guard may lift above UP_FORMAT_MAX; if the fallback yields A1, it stays at A1
            if dropped and order.index(res['format']) <= big:
                notes.append("Admissible only on a larger size than " + pl.UP_FORMAT_MAX + " (" + ', '.join(f'{f_} {s_}' for f_, s_ in dict.fromkeys(dropped)) + "): not chosen, maximum size of a single sheet is " + pl.UP_FORMAT_MAX)
            notes.append(f"No admissible candidate (limits {pl.X_MIN_MM:g} mm, {pl.Y_MAX_DM2:g}/dm², fill {pl.FILL_MAX:g}): largest scale on "
                         f"{res['format']} chosen ({scales[res['index']]}), violated: " + '; '.join(_viol(_limits(ctx, res['content'], scales[res['index']]))))
    if res is None:
        for scale in scales[start:]:             # no sheet suffices: follow-up sheets as before
            c = content(scale)
            v = next((v for fmt in order[order.index(sug['format']):] for v in [_variant(ctx, c, scale, fmt, True)] if v), None)
            if v:
                return {'variant': v, 'content': c}
        return None
    res['notes'] = notes + _choice_notes(res, why, scales, order)
    est = res.get('est')
    if prober.measured and est and order.index(_fmt_of(est)) < order.index(_fmt_of(res['variant'])):
        a, b = prober.probe(res['variant']['spec']), prober.probe(est['spec'])      # measurement demands a larger size than the estimate:
        if a and b and b['blocking'] <= a['blocking']:                              # the estimate stays if it no longer blocks
            res = {**res, 'variant': est, 'content': res['est_content'], 'override': res.get('override', []) + ['estimate'],
                   'override_notes': res.get('override_notes', []) + [
                       f"override estimate: probe blocking {a['blocking']} at {_fmt_of(res['variant'])} (measurement) versus "
                       f"{b['blocking']} at {_fmt_of(est)} (estimate): {_fmt_of(est)} chosen"]}
    return _format_guard(ctx, res, prober, order)


def _format_guard(ctx, res, prober, order):
    """Format guard against the next larger size: only lifts with a clear gain; above UP_FORMAT_MAX only if the fallback itself went beyond it (res['fallback'])."""
    fmt = _fmt_of(res['variant'])
    scale = res['variant']['spec']['sheets'][0]['scale']
    if prober.measured and order.index(fmt) + 1 < len(order):       # ... and not against the next larger size
        big = _variant(ctx, res['content'], scale, order[order.index(fmt) + 1])
        a = prober.probe(res['variant']['spec'])
        b = prober.probe(big['spec']) if big else None
        if a and b:
            gain, fill = a['blocking'] - b['blocking'], pl.metrics(big['spec'], big['packed'])['fill']
            clear = gain >= GUARD_GAIN or (b['blocking'] == 0 and gain > 0)      # clear gain (autofix noise +-3)
            full = fill >= pl.MIN_FILL - 1e-9                                    # and the larger size stays filled (white space <= 65 %)
            lift_ok = _may_lift(_fmt_of(big), bool(res.get('fallback')), order)           # above UP_FORMAT_MAX only if no candidate was admissible on UP_FORMAT_MAX
            if clear and full and lift_ok:
                res = {**res, 'variant': big, 'content': res['content'], 'override': res.get('override', []) + ['format'],
                       'override_notes': res.get('override_notes', []) + [
                           f"override format: probe blocking {a['blocking']} at {fmt} versus {b['blocking']} at {_fmt_of(big)} "
                           f"(−{gain}, fill {fill:.2f}): {_fmt_of(big)} chosen"]}
            elif gain > 0 and lift_ok:
                res = {**res, 'guard_notes': res.get('guard_notes', []) + [
                    f"Format guard: probe blocking {a['blocking']} at {fmt} versus {b['blocking']} at {_fmt_of(big)} "
                    f"(−{gain}, fill {fill:.2f}) is no clear gain (at least −{GUARD_GAIN} or to 0, fill "
                    f"at least {pl.MIN_FILL}): {fmt} stays"]}
    return res


def _may_lift(fmt_, fallback, order=an.ORDER):
    """May the format guard lift to fmt_? Up to UP_FORMAT_MAX always; above it (A0) only in the fallback (no admissible candidate on UP_FORMAT_MAX)."""
    return order.index(fmt_) <= order.index(pl.UP_FORMAT_MAX) or fallback


def _choice_notes(res, why, scales, order):
    """Planner notes on the size and scale choice, each with the reason: for every size below the chosen one (a) why the
    chosen scale fails there (limit or block area against free area, measured or estimated) and (b) what the largest scale that fits there
    violates; for the next larger scale on the chosen size (c) why it fails. Empty if there is nothing to explain."""
    fmt, i, out = res['format'], res['index'], []
    scale = scales[i]
    text = lambda w: '; '.join(x for x in w if x) or 'no limit violated, but not chosen by measurement'
    for f in order[:order.index(fmt)]:
        mine = why.get((i, f))
        fits = sorted(j for (j, g), w in why.items() if g == f and w and not w[0].startswith(('does not fit', 'measured does not fit')))
        part = []
        if mine:
            part.append(f"scale {scale} fails: {text(mine)}")
        if fits and fits[0] != i:
            part.append(f"the largest fitting scale {scales[fits[0]]} violates {text(why[(fits[0], f)])}")
        if part or any(g == f for (_, g) in why):
            out.append(f"Size {f} not chosen: " + ('; '.join(part) or 'no checked scale fits') + f" (chosen: {fmt}, {scale})")
    if i > 0 and (i - 1, fmt) in why:
        out.append(f"Scale {scales[i - 1]} on {fmt} not chosen: {text(why[(i - 1, fmt)])}")
    return out


# ------------------------------------------------------------------ part scales (N1.3/N1.4) and assembly fallback

MAIN_STEPS = 2           # fallback (_main_large): the assembly stands at most this many standard steps smaller than the sheet scale
PART_UP_STEPS = 3        # N1.3: a manufactured part gets at most this many standard steps above the sheet scale
PART_PROBES = 4          # at most this many probes (measurements) of the chosen part scales; if the combination does not pack, the last lifted step is undone
PART_PROBES_MAX = PART_PROBES + 2      # maximum number of all probes of _part_scales including the guard (starting point and result)


def _counts(ctx):
    """{manufactured part: number of mandatory elements} of the drawn parts in the order of the spec parts (counted like coverage.parts[part].required)."""
    if '_counts' not in ctx:
        out = {}
        for sp in ctx['spec_parts']:
            pid = sp['id']
            if pid in ctx['drawn'] and pid in ctx['P'] and ctx['roles'].get(pid) == 'manufactured':
                P = ctx['P'][pid]
                out[pid] = checks.required_count(pid, {'features': P['feats'], 'freeform': P['part'].get('freeform'), 'bbox': P['part']['bbox'],
                                                       'regions': P['part'].get('regions') or []})
        ctx['_counts'] = out
    return ctx['_counts']


def _is_wire(ctx, pid):
    """Wire part (bend table only): checks.wire_part on the bend table of the part, the same definition as the check."""
    return any(checks.wire_part(bt) for bt in ctx['P'][pid]['part'].get('bend_tables') or [])


def _n14_counts(ctx):
    """_counts without wire parts: N1.4 does not apply to them."""
    return checks.non_wire_counts(_counts(ctx), [pid for pid in _counts(ctx) if _is_wire(ctx, pid)])


def _main_of(ctx):
    """Main part (N1.4): checks.main_part over the mandatory elements (without wire parts), tie the larger envelope volume; the same function as in the check."""
    counts = _n14_counts(ctx)
    return checks.main_part(counts, {pid: float(np.prod(ctx['P'][pid]['part']['bbox']['size'])) for pid in counts})


def _paper_area(ctx, b, scale, geom=None):
    """Paper area of the geometry of the block b (mm²): sum of the envelope areas of its normal views, sections and auxiliary views at the block scale, without isometric view and
    detail view. geom {view: (b, h) in model mm} from the measurement (prober.geom) takes precedence over the estimate from the bounding box of the part (a section shows less than the envelope)."""
    kb = an.scale_value(b.get('bscale') or scale)
    ext = _extents(b['views'], ctx['A'])
    geom = geom or {}
    return sum((geom[v['id']][0] * geom[v['id']][1] if v['id'] in geom else ext[v['id']]['w'] * ext[v['id']]['h']) * pl.view_scale(v, kb) ** 2
               for v in b['views'] if not v.get('detail') and v.get('role') != 'iso')


def _n14(ctx, blocks, scale, geom=None):
    """{(case, part): ratio} of the N1.4 violations of the blocks (checks.main_ratio_violations): planner areas (measured geometry before estimate)
    only for small secondary parts, block scale for comparable parts and the assembly, wire parts excepted. Ratio = area or scale relative to the main part."""
    areas = {b['name']: _paper_area(ctx, b, scale, geom) for b in blocks if b['name'] == 'asm' or b['name'] in ctx['P']}
    scales = {b['name']: an.scale_value(b.get('bscale') or scale) for b in blocks}
    return {(v['case'], v['part']): v['ratio'] for v in checks.main_ratio_violations(areas, _counts(ctx), _main_of(ctx), scales, [p for p in _counts(ctx) if _is_wire(ctx, p)])}


def _n14_text(key, ratio):
    """Report text of an N1.4 case: area or scale in relation to the main part."""
    case, part = key
    if case == 'minor':
        return f'Secondary part {part} {ratio:.2f}× the area of the main part (limit {checks.MAIN_RATIO:g}×)'
    return f"{'Assembly' if case == 'assembly' else 'Part ' + str(part)} at {ratio:.2f} times the scale of the main part (at most 1×)"


def _not_worse(new, old):
    """Are the limit violations of the metrics new (from _limits) not worse than those of old: no new kind, no larger amount."""
    if new['min_dim'] is not None and new['min_dim'] < pl.X_MIN_MM - 1e-9 and (old['min_dim'] is None or new['min_dim'] < old['min_dim'] - 1e-9):
        return False
    if new['min_detail'] is not None and new['min_detail'] < pl.DETAIL_MIN_MM - 1e-9 and (old['min_detail'] is None or new['min_detail'] < old['min_detail'] - 1e-9):
        return False
    return not (new['density'] > pl.Y_MAX_DM2 + 1e-9 and new['density'] > old['density'] + 1e-9)


def _part_scales(ctx, res, scales, order, content, prober):
    """(res, notes): own standard scales per manufactured part on the size and the sheet scale of _search (N1.3). Applies to sheets with at least
    two blocks (single parts run unchanged); never for wire parts; the assembly keeps the sheet scale. Main part = manufactured part with the most mandatory elements
    (checks.main_part, like the check). Per round each part gets at most one step more (up to PART_UP_STEPS above the sheet scale), in this order: parts that violate the
    reading limits X/detail (N1.6), the main part, the others by descending number of mandatory elements. A step is admissible if (1) the content packs on the
    size (it never grows) and the fill is at most pl.FILL_MAX, (2) N1.4 does not get worse (no new violation, no larger ratio) and (3) the
    limit violations of the part do not get worse (no new kind, no larger amount; “no violation” is not a condition). The estimate is made from the measured
    occupancy of the starting point (only the geometry of the lifted part grows, _grown), afterwards the combination is measured once, rearranged and packed again; if it
    does not pack, the last lifted step is undone (at most PART_PROBES probes). Guard: probe blocking of the solution at most that of the starting point plus
    GUARD_GAIN, otherwise the starting point stays; it needs two more probes (in total at most PART_PROBES_MAX). The probe does not see dim_on_hidden_edge (too expensive, see layout_probe). One planner note per manufactured part with the scale and the reason why the next step fails."""
    counts = _counts(ctx)
    if not ctx['assembly'] or 'index' not in res or len(res['variant']['spec']['sheets']) != 1 or len(counts) < 1 or len(res['content'][0]) < 2:
        return res, []
    scale, fmt_ = res['variant']['spec']['sheets'][0]['scale'], _fmt_of(res['variant'])
    i0, main = scales.index(scale), _main_of(ctx)
    blocks0 = res['content'][0]
    k0 = an.scale_value(scale)
    bs0 = dict(res.get('bscales') or {})
    mfg = [p for p in counts if any(b['name'] == p for b in blocks0)]
    wires = [p for p in mfg if _is_wire(ctx, p)]
    eligible = [p for p in mfg if p not in wires]
    idx = {p: i0 for p in eligible}
    lim = lambda b, bl: _limits(ctx, ([b] + [x for x in bl if x.get('detail_of') == b['name']], []), scale)      # the pattern detail belongs to the limits of its part
    state = {'c': res['content'], 'meas': next((b['_arr'][2] for b in blocks0 if '_arr' in b), None), 'v': res['variant'],
             'n14': _n14(ctx, blocks0, scale, prober.geom), 'lim': {b['name']: lim(b, blocks0) for b in blocks0 if b['name'] in eligible}}
    measure_fail, history = {}, []

    def scale_map(ix):
        return {**bs0, **{p: scales[i] for p, i in ix.items() if i != i0}}

    def attempt(p):
        """(admissible, reason, state) of the step above the current one of part p."""
        ni = idx[p] - 1
        if ni < 0 or ni < i0 - PART_UP_STEPS:
            return False, f'already {i0 - idx[p]} standard steps above the sheet scale {scale} (at most {PART_UP_STEPS})', None
        c = content(scale, None, scale_map({**idx, p: ni}))
        meas = state['meas']
        blocks = [dict(b) for b in c[0]]
        for b in blocks:
            _arrange(ctx, b, an.scale_value(b.get('bscale') or scale), meas)
        n14 = _n14(ctx, blocks, scale, prober.geom)
        bad = [_n14_text(key, r) for key, r in n14.items() if key not in state['n14'] or r > state['n14'][key] + 1e-9]
        if bad:
            return False, 'N1.4 would be violated (' + '; '.join(bad) + ')', None
        nlim = lim(next(b for b in blocks if b['name'] == p), blocks)
        if not _not_worse(nlim, state['lim'][p]):
            return False, 'limits would get worse (' + '; '.join(_viol(nlim)) + ')', None
        cc = (blocks, c[1])
        v = _variant(ctx, cc, scale, fmt_)
        if v is None:
            return False, 'does not fit: ' + _fit_text(blocks, fmt_), None
        fill = pl.metrics(v['spec'], v['packed'])['fill']
        if fill > pl.FILL_MAX + 1e-9:
            return False, f'fill {fill:.2f} > {pl.FILL_MAX:g}', None
        return True, '', {'c': cc, 'meas': meas, 'v': v, 'n14': n14, 'lim': {**state['lim'], p: nlim}}

    def rank():
        heal = [p for p in eligible if state['lim'][p]['min_dim'] is not None and state['lim'][p]['min_dim'] < pl.X_MIN_MM - 1e-9
                or state['lim'][p]['min_detail'] is not None and state['lim'][p]['min_detail'] < pl.DETAIL_MIN_MM - 1e-9]
        rest = [p for p in eligible if p not in heal]
        return (sorted(heal, key=lambda p: (p != main, -counts[p])) + [p for p in rest if p == main] + sorted((p for p in rest if p != main), key=lambda p: -counts[p]))

    banned, unmeasured, verified = set(), not prober.measured, False

    def lift_all():
        changed = True
        while changed:
            changed = False
            for p in rank():
                if p in banned:
                    continue
                ok, _, new = attempt(p)
                if ok:
                    history.append((p, dict(state)))
                    idx[p] -= 1
                    state.update(new)
                    changed = True

    notes, out = [], res
    for _ in range(PART_PROBES):             # estimate, measure, if “does not pack when measured” undo the last lifted step and continue with the other parts
        lift_all()
        if not history or unmeasured:
            break
        pr = prober.probe(state['v']['spec'])
        if pr is None:
            unmeasured = True
            break
        c = _remeasured(ctx, content(scale, None, scale_map(idx)), scale, _occupancy_of(state['v']['spec'], pr))
        v = _variant(ctx, c, scale, fmt_)
        fill = pl.metrics(v['spec'], v['packed'])['fill'] if v is not None else None
        if v is not None and fill <= pl.FILL_MAX + 1e-9:
            state.update({'c': c, 'v': v})
            verified = True
            break
        p, before = history.pop()
        measure_fail[p] = ('measured does not fit: ' + _fit_text(c[0], fmt_)) if v is None else f'measured fill {fill:.2f} > {pl.FILL_MAX:g}'
        banned.add(p)
        idx[p] += 1
        state.update(before)
    if history and not verified and not unmeasured:
        while history:                      # probes used up without a combination that packs when measured: starting point
            p, before = history.pop()
            measure_fail.setdefault(p, f'no combination that packs after {PART_PROBES} probes')
            idx[p] += 1
            state.update(before)
    if history:
        base_b = prober.probe(res['variant']['spec']) if prober.measured and not unmeasured else None
        fin_b = prober.probe(state['v']['spec']) if prober.measured and not unmeasured else None
        if base_b is not None and fin_b is not None and fin_b['blocking'] > base_b['blocking'] + GUARD_GAIN:
            notes.append(f"Part scales discarded: probe blocking {fin_b['blocking']} versus {base_b['blocking']} at the sheet scale {scale} (more than +{GUARD_GAIN})")
            while history:
                p, before = history.pop()
                measure_fail[p] = f"probe blocking {fin_b['blocking']} versus {base_b['blocking']} (at most +{GUARD_GAIN})"
                idx[p] += 1
                state.update(before)
        else:
            out = {**res, 'variant': state['v'], 'content': state['c'], 'bscales': scale_map(idx)}
            if 'notes' in res:                      # the planner note of the search (e.g. density 246/dm²) describes the state before the part scales
                out['notes'] = [('Before the part scales: ' + n) if str(n).startswith('No admissible candidate') else n for n in res['notes']]
            if unmeasured:
                notes.append('Part scales after the estimate (measurement not possible)')
    # planner notes per manufactured part: the reason why the next step fails from the final state
    state.update({'meas': next((b['_arr'][2] for b in out['content'][0] if '_arr' in b), None), 'n14': _n14(ctx, out['content'][0], scale, prober.geom),
                  'lim': {b['name']: lim(b, out['content'][0]) for b in out['content'][0] if b['name'] in mfg}})
    for p in mfg:
        who = ctx['who'](p)
        if p in wires:
            notes.append(f'{who}: sheet scale {scale} (wire part, no own scale)')
            continue
        cur = scales[idx[p]]
        reason = measure_fail.get(p) or attempt(p)[1] or 'admissible after the estimate, but not lifted'
        left = _viol(state['lim'][p]) if p in state['lim'] else []
        notes.append(f"{who}{' (main part)' if p == main else ''}: scale {cur}" + (f' (sheet scale {scale})' if cur != scale else '')
                     + f"; next step {scales[idx[p] - 1] if idx[p] > 0 else '–'} not chosen: {reason}"
                     + (f"; limits remain violated: {'; '.join(left)} (priority: N1.4 and N1.3 before density)" if left else ''))
    return out, notes


def _main_large(ctx, res, scales, order, content, prober):
    """(res, notes): fallback “assembly smaller” (N1.4). If the assembly stands at a larger scale than the main part,
    its block gets up to MAIN_STEPS smaller standard steps as block scale; the first solution that fits on the size of the sheet (measured, fill at most pl.FILL_MAX, limits
    not worse) applies. If there is none, the reason is in a planner note. The main part is no longer lifted here (_part_scales does that)."""
    if not ctx['assembly'] or 'index' not in res or len(res['variant']['spec']['sheets']) != 1 or len(_counts(ctx)) < 2:
        return res, []
    scale, fmt_ = res['variant']['spec']['sheets'][0]['scale'], _fmt_of(res['variant'])
    blocks = res['content'][0]
    asm = next((b for b in blocks if b['name'] == 'asm'), None)
    viol = _n14(ctx, blocks, scale, prober.geom)
    if asm is None or ('assembly', None) not in viol:
        return res, []
    main = _main_of(ctx)
    who = ctx['who'](main) if 'who' in ctx else main
    bs0 = dict(res.get('bscales') or {})
    cur = scales.index(bs0.get('asm') or scale)
    old = _limits(ctx, res['content'], scale)
    for j in range(1, MAIN_STEPS + 1):
        if cur + j >= len(scales):
            break
        bs = {**bs0, 'asm': scales[cur + j]}
        c = content(scale, None, bs)
        v = _variant(ctx, c, scale, fmt_)
        if v is None:
            continue
        pr = prober.probe(v['spec']) if prober.measured else None
        if pr is not None:
            c = _remeasured(ctx, content(scale, None, bs), scale, _occupancy_of(v['spec'], pr))
            v = _variant(ctx, c, scale, fmt_)
            if v is None:
                continue
        if ('assembly', None) in _n14(ctx, c[0], scale, prober.geom) or pl.metrics(v['spec'], v['packed'])['fill'] > pl.FILL_MAX + 1e-9 or not _not_worse(_limits(ctx, c, scale), old):
            continue
        return {**res, 'variant': v, 'content': c, 'bscales': bs}, [
            f"Assembly at block scale {bs['asm']}: it stood at {viol[('assembly', None)]:.2f} times the scale of the main part {who} (N1.4, at most 1×)"]
    return res, [f"Assembly at {viol[('assembly', None)]:.2f} times the scale of the main part {who}: no block scale up to {MAIN_STEPS} standard steps smaller "
                 f"fits on {fmt_} without making the limits worse"]


# ------------------------------------------------------------------ isometric view per sheet

def _feats_of(ctx, pid):
    return (ctx.get('P') or {}).get(pid, {}).get('feats') or []


def _isoed(ctx, content, scale, iso_scales, measured=None):
    """Content with an undimensioned isometric view (preset iso, title "ISO", with a deviating scale "ISO (1:2)") per manufactured part
    in iso_scales {part: scale}; the assembly already has its isometric view. Blocks rearranged."""
    blocks, lines = content
    out = []
    for b in blocks:
        b = dict(b)
        sc = iso_scales.get(b['name'])
        own = b.get('bscale') or scale                  # scale in which the block is arranged (block scale)
        if sc and not any(v.get('role') == 'iso' for v in b['views']):
            iso = {'id': f"{b['name']}-iso", 'part': b['name'], **pv.iso_view(_feats_of(ctx, b['name'])), 'role': 'iso', 'features': [],
                   'label': 'ISO' + (f' ({sc})' if sc != own else '')}
            if sc != own:
                iso['scale'] = sc
            b['views'] = list(b['views']) + [iso]
            bt = next((x for x in ((ctx.get('P') or {}).get(b['name']) or {}).get('part', {}).get('bend_tables', []) if x.get('points')), None)
            if bt:                       # point labels and axis cross where the bend points are best separated on the sheet (also isometric view)
                b['tables'] = [{**t, 'labels_in': pd.bend_label_view(bt, b['views'], b['name'], an.scale_value(own)) or t.get('labels_in')}
                               if t.get('bend_table') else t for t in b['tables']]
                b['views'] = pd.label_bend_views(b['views'], b['tables'])
            _arrange(ctx, b, an.scale_value(own), measured)
        out.append(b)
    return out, lines


def _iso_measured(ctx, content, scale, fmt, order, iso_scales, prober):
    """(content, variant or None) with the isometric view, after the block including the isometric view has been measured. The new isometric view has no measurement yet; without a measurement
    of all views a block gets no footprint (plan_layout.arrange, FOOTPRINT_KEY), other blocks do not find its free corners, and the size appears too small
    (the own block of the profile detail then fitted next to no isometric step). As with the N1.9 fallback (_zone_fallback): measurement is made on the first larger
    size on which the probe packs; arrangement and packing are made afterwards on fmt."""
    trial = _isoed(ctx, content, scale, iso_scales)
    paper = next((p_ for f in order[order.index(fmt) + 1:] if (p_ := _variant(ctx, trial, scale, f))), None)
    pr = prober.probe(paper['spec']) if paper is not None else None
    if pr is None:
        return trial, None
    trial = _isoed(ctx, content, scale, iso_scales, _occupancy_of(paper['spec'], pr))
    return trial, _variant(ctx, trial, scale, fmt)


def _with_iso(ctx, res, scales, order, prober):
    """res (result of _search) with exactly one isometric view per sheet: assembly sheet only the assembly isometric view (no part isometric view,
    nothing to do); single-part sheet an isometric view of the first manufactured part, mandatory, at scale pl.iso_scale (one to two standard steps below the
    sheet scale, longest side 60-120 mm); if it does not fit on the size, the next smaller step until it fits (noted). No probe on
    blocking. (res, notes)"""
    variant = res['variant']
    if ctx['assembly'] or len(variant['spec']['sheets']) != 1:
        return res, []
    todo = [b['name'] for b in res['content'][0] if b['name'] in ctx['drawn'] and ctx['roles'].get(b['name']) == 'manufactured'][:1]
    if not todo:
        return res, []
    pid = todo[0]
    fmt, scale = _fmt_of(variant), variant['spec']['sheets'][0]['scale']
    base = prober.probe(variant['spec']) if prober.measured else None
    measured = _occupancy_of(variant['spec'], base) if base else None
    probe_view = {'id': f'{pid}-iso', 'part': pid, **pv.iso_view(_feats_of(ctx, pid))}
    ext = max(_extents([probe_view], ctx['A'])[probe_view['id']].values())
    sc = pl.iso_scale(ext, scale)
    for cand in scales[scales.index(sc):]:
        trial = _isoed(ctx, res['content'], scale, {pid: cand}, measured)
        v = _variant(ctx, trial, scale, fmt)
        if v is None and measured is not None:
            trial, v = _iso_measured(ctx, res['content'], scale, fmt, order, {pid: cand}, prober)
        if v is not None:
            who = ctx['who'](pid) if 'who' in ctx else pid
            return {**res, 'variant': v, 'content': trial, 'iso_scales': {pid: cand}}, [
                f"Isometric view of {who} at scale {cand} on {fmt} (longest side {ext * an.scale_value(cand):.0f} mm on paper)"]
    return res, [f'No isometric view of {pid}: no step fits on {fmt}']


def _section_at_scale(v, scale, position):
    letter = v['section']['label']
    return {**v, 'scale': scale, 'label': f"{letter}-{letter} ({scale}) · Pos. {position}"}


def _removed_sections(ctx, block, previous, sheet_scale):
    """Sections with their own section trace and a known sheet scale as separately placeable views."""
    views = block['views']
    marks = {(a.get('view'), a.get('label')) for a in block['anns'] if a.get('type') == 'section_line'}
    normal_ids = {v['id'] for v in views if not any(v.get(k) for k in ('section', 'detail', 'auxiliary'))
                  and np.max(np.abs(prj.frame_of(v)[0])) >= 1 - 1e-9}
    if len(normal_ids) < 2:
        return None
    hosts = {v.get(field) for v in views for field in ('section_line_in','detail_circle_in','arrow_in')}
    hosts.update(t.get('labels_in') for t in block['tables'])
    old = {pl._view_key(v) for v in previous['views'] if v.get('section') and not v.get('detail')
           and pl.view_scale(v,an.scale_value(sheet_scale)) == an.scale_value(sheet_scale)}
    cuts = {}
    for v in views:
        letter = (v.get('section') or {}).get('label')
        host = v.get('section_line_in')
        if (letter and not v.get('detail') and host in normal_ids and (host,letter) in marks
                and v['id'] not in hosts and pl._view_key(v) in old):
            cuts[v['id']] = _section_at_scale(v, sheet_scale, ctx['pos'][block['name']])
    if not cuts:
        return None
    parent = {**block, 'views':[v for v in views if v['id'] not in cuts],
              'anns':[a for a in block['anns'] if a.get('view') not in cuts]}
    _arrange(ctx, parent, an.scale_value(block['bscale']))
    result = [parent]
    for vid, v in cuts.items():
        cut = {'name':vid, 'part_id':block['name'], 'views':[v], 'bscale':sheet_scale, 'title':None, 'tables':[],
               'anns':[a for a in block['anns'] if a.get('view') == vid]}
        _arrange(ctx, cut, an.scale_value(sheet_scale))
        result.append(cut)
    return result


def _enlarge_normal_views(ctx, res, scales, order, content, prober):
    """With a reduced main part, lift the normal views together, then the designated sections according to their dimensioning need."""
    sheets = res['variant']['spec']['sheets']
    if not ctx['assembly'] or not prober.measured or len(sheets) != 1 or 'index' not in res:
        return res, None
    scale, fmt_ = sheets[0]['scale'], sheets[0]['format']
    if an.scale_value(scale) >= 1 or fmt_ not in order or scale not in scales[1:]:
        return res, None
    pid = _main_of(ctx)
    previous = next((b for b in res['content'][0] if b['name'] == pid), None)
    if not previous or previous.get('bscale') or not any(v.get('section_line_in') for v in previous['views']):
        return res, None
    enlarged = scales[scales.index(scale)-1]
    block_scales = {**(res.get('bscales') or {}), pid:enlarged}
    blocks, lines = content(scale, None, block_scales)
    primary = next(b for b in blocks if b['name'] == pid)
    split = _removed_sections(ctx, primary, previous, scale)
    if not split:
        return res, None
    baseline = _probe_settled(prober, res['variant']['spec'])
    if not baseline:
        return res, None
    limits = _limits(ctx, res['content'], scale)

    def fit(trial_blocks, max_blocking):
        trial = (trial_blocks, lines)
        if not _not_worse(_limits(ctx, trial, scale), limits):
            return None
        # The probe sheet supplies the envelopes at the actual scale. The starting format is published.
        paper = next((v for f in order[order.index(fmt_):] if (v := _variant(ctx, trial, scale, f))), None)
        measured = _probe_settled(prober, paper['spec']) if paper else None
        if not measured:
            return None
        trial = _remeasured(ctx, trial, scale, _occupancy_of(paper['spec'], measured))
        placed = _variant(ctx, trial, scale, fmt_)
        if not placed or pl.metrics(placed['spec'], placed['packed'])['fill'] > pl.FILL_MAX:
            return None
        final = _probe_settled(prober, placed['spec'])
        if not final or final['blocking'] > max_blocking:
            return None
        if any(f['code'] in ('out_of_area', 'view_overlap', 'overlaps_block') and f['severity'] == 'block' for f in final['findings']):
            return None
        if checks.projection_findings(placed['spec'], final['view_frames']):
            return None
        return trial, placed, final['blocking']

    kept = [x for b in blocks for x in (split if b['name'] == pid else [b])]
    best = fit(kept, baseline['blocking'])
    if not best:
        return res, None
    by_need = sorted(split[1:], key=lambda b: -sum(a['type'] in pl.SIZE_TYPES for a in b['anns']))
    for cut in by_need:
        larger = {**cut, 'views': [_section_at_scale(cut['views'][0], enlarged, ctx['pos'][pid])], 'bscale': enlarged}
        _arrange(ctx, larger, an.scale_value(enlarged))
        attempt = [larger if b is cut else b for b in kept]
        choice = fit(attempt, best[2])
        if choice:
            kept, best = attempt, choice
    trial, placed, _ = best
    labels = ', '.join(f"{b['views'][0]['section']['label']}-{b['views'][0]['section']['label']} {b['bscale']}"
                       for b in kept if b.get('part_id') == pid)
    return {**res, 'content':trial, 'variant':placed, 'bscales':block_scales}, (
        f"{ctx['who'](pid)}: normal views {enlarged}, sections {labels} on {fmt_}")


# ------------------------------------------------------------------ variants and candidates

def _spread_views(ctx, variant, content, prober):
    """Minimum spacing of the views after the measurement: if the probe reports views_too_close, the occupancies of the views involved
    are enlarged by the missing gap and rearranged (at most SPREAD_TRIES rounds, only on the same size). (variant, content, notes)"""
    notes = []
    if not prober.measured or len(variant['spec']['sheets']) != 1:
        return variant, content, notes
    fmt_, scale = _fmt_of(variant), variant['spec']['sheets'][0]['scale']
    for n in range(SPREAD_TRIES):
        pr = prober.probe(variant['spec'])
        close = [f for f in (pr or {}).get('findings') or [] if f.get('code') == 'views_too_close']
        if not close:
            break
        c = _remeasured(ctx, content, scale, pl.spread(_occupancy_of(variant['spec'], pr), close, checks.VIEW_GAP_MM))
        v = _variant(ctx, c, scale, fmt_)
        if v is None:
            notes.append(f"Views too close ({len(close)} findings): increased spacing does not fit on {fmt_}")
            break
        variant, content = v, c
        notes.append(f"Views too close ({len(close)} findings, smallest gap {min(float(f.get('gap_mm') or 0) for f in close):.1f} mm): rearranged (round {n + 1})")
    return variant, content, notes


def _visible_dimensions(ctx, variant, content, prober):
    """Check the datum points of the chosen scale against the actual HLR contour and then repack the dimension space."""
    if prober.dr is None or len(variant['spec']['sheets']) != 1:
        return variant, content
    sd = variant['spec']['sheets'][0]
    vds = {v['id']: v for v in sd['views']}
    blocks, lines = content
    revised, changed = [], False
    for block in blocks:
        pid = block.get('part_id') or block.get('detail_of') or block['name']
        if pid not in ctx['P']:
            revised.append(block)
            continue
        feats = ctx['P'][pid]['feats']
        by_id = {f['id']: f for f in feats}
        ws = pd._views(block['views'], pid)
        by_view = {w['id']: w for w in ws}
        anns = [dict(a) for a in block['anns']]
        load = {w['id']: sum(a.get('view') == w['id'] and a['type'] in checks.BUDGET_TYPES for a in anns) for w in ws}
        ray = _ray(ctx, pid)
        sym = _part_planes(ctx, pid)
        block_k = block['_arr'][1]

        def visible(w, points):
            return prober.dr.visible_points(vds[w['id']], sd['scale'], prj.project(points, w['fr']))

        for a in anns:
            old = by_view.get(a.get('view'))
            if old is None or a['type'] != 'linear' or a.get('axis') not in ('h', 'v'):
                continue
            feature = by_id.get(a.get('feature')) or {}
            axial = feature.get('kind') in ('hole', 'shaft') and feature.get('start') is not None
            if not axial and not pd._surface_anchor(a, by_id):
                continue
            feet = [np.asarray(a[key], float) for key in ('p1', 'p2')]
            if visible(old, feet).all():
                continue
            direction = pd._meas_dir(old, a['axis'])
            wk = pd._world(direction)
            gate = (lambda w: pd._axis_view_ok(w, wk, ctx['P'][pid]['part']['bbox'], block_k)) if wk in sym else (lambda w: True)      # a dimension that replaces a mirror dimension only moves to views in which the symmetry line arises (no detail view, no stub)
            others = sorted((w for w in ws if w is not old and (old['aux'] or not w['aux']) and load[w['id']] < checks.BUDGET_VIEW and gate(w)),
                            key=lambda w: (a['feature'] not in w['shows'], load[w['id']]))
            for w in [old] + others:
                axis = pd._orient(w, direction)
                if axis not in ('h', 'v') or not all(pd._inside(w, p) for p in feet):
                    continue
                trial = {**a, 'axis': axis, 'view': w['id']}
                on_contour = lambda p: visible(w, [p])[0]
                if axial:
                    ok = pd._axial_datum(trial, w, feature, ctx['A'][pid]['bbox'], ray, on_contour)
                else:
                    ok = pd._reanchor(trial, w, feats, ray, on_contour)
                if not ok:
                    continue
                if w is not old:
                    trial['side'] = 'above' if axis == 'h' else 'left'
                    trial.pop('offset', None)
                    load[old['id']] -= 1
                    load[w['id']] += 1
                a.clear()
                a.update(trial)
                changed = True
                break
        if anns != block['anns']:
            block = {**block, 'anns': anns}
            _, k, previous = block['_arr']
            _arrange(ctx, block, k, previous)
        revised.append(block)
    if changed:
        trial = _variant(ctx, (revised, lines), sd['scale'], sd['format'])
        if trial:
            probe = prober.probe(trial['spec'])
            if probe:
                c = _remeasured(ctx, (revised, lines), sd['scale'], _occupancy_of(trial['spec'], probe))
                packed = _variant(ctx, c, sd['scale'], sd['format'])
                if packed:
                    return packed, c
    return variant, content


def function_note(e, who):
    """(list, text) of a recognized functional dimension e (plan_dims._function_dims) for planner notes ('notes') or open points ('open'):
    dimensioned directly, already present, ambiguous (nothing dimensioned) or not found in the geometry (the geometry does not carry the
    stated dimension; nothing invented). ('notes', '') without statement."""
    what = f"{e['text']} = {fmt(e['value'])} ({e['kind']}{', ' + str(e['count']) + '×' if e.get('count') else ''})"
    if e['status'] == 'direct':
        return 'notes', f"Functional dimension {what} of {who} dimensioned directly: {e['measure']}"
    if e['status'] == 'existing':
        return 'notes', f"Functional dimension {what} of {who} already has a dimension of its own: {e['measure']}"
    if e['status'] == 'ambiguous':
        return 'open', f"Functional dimension {what} of {who} matches several geometry dimensions, not dimensioned (no guessing)"
    if e['status'] == 'none':
        return 'notes', f"Functional dimension {what} not found in any part of the geometry (nothing dimensioned, no guessing)"
    return 'notes', ''


def _opening_facts(spec, entries):
    """Dimension values such as 218→56 mm are only assigned to an opening if both stations lie on the same face pair."""
    openings = defaultdict(lambda: defaultdict(set))
    for sheet in spec['sheets']:
        parts = {v['id']: v.get('parts') or [] for v in sheet['views']}
        for a in sheet.get('annotations') or []:
            if a.get('opening_axis') and len(parts[a['view']]) == 1:
                k = 'xyz'.index(a['opening_axis'])
                pair = (k, tuple(sorted([a['feature'], *a['also']])))
                openings[parts[a['view']][0]][pair].add(abs(a['p1'][k]-a['p2'][k]))
    for entry in entries:
        if entry['status'] not in ('none','ambiguous') or entry['kind'] != 'length':
            continue
        targets = {entry['value']}
        if pfx._ARROW.search(entry['text']):
            targets.update(e['value'] for e in entries if e['part']==entry['part'] and e['text']==entry['text'])
        groups = [widths for widths in openings[entry['part']].values() if
                  all(any(abs(w-value) <= pd.VALUE_TOL for w in widths) for value in targets)]
        if len(groups)==1 and (entry['status']=='none' or len(targets)>1):
            value = next(w for w in sorted(groups[0]) if abs(w-entry['value']) <= pd.VALUE_TOL)
            entry.update(status='existing', measure=f'Opening width {pd.fmt(value)}')


def plan_spec(spec):
    """Candidate specs (without brep/out_dir/cache_dir), parts overview, notes and open items (module doc).
    Optional spec['feature_cache_dir']: form feature cache that the render also reads (features.features_of).
    N1.9: if the assembly stands between part views in the final state and the assembly views can be reduced (_balloons single), only the block of the
    assembly is rebuilt and the planning continues from there (_zone_fallback: prober, scales and enlarged part views stay, no second full run); otherwise the result
    of the first run stays with a planner note."""
    ctx = _context(spec)
    max_n = max(1, min(4, int(spec.get('max_candidates') or 3)))
    out = {'candidates': [], 'parts': ctx['out_parts'], 'notes': ctx['notes'], 'open': ctx['open'],
           'function_dims': ctx['function_dims']}     # recognized text values per part (direct/existing/ambiguous/none)
    if not ctx['drawn'] and not ctx['assembly']:
        ctx['open'].append('No drawable part')
        return out
    _plan_pass(ctx, spec, out, max_n)
    return out


def _accept_zone_fallback(note, blocking, first_blocking):
    """The result of the fallback only applies if it meets N1.9 (note None) and has no more blocking findings than the first run (None = not measurable: then only the zone counts)."""
    return note is None and (blocking is None or first_blocking is None or blocking <= first_blocking)


def _asm_snapshot(ctx):
    """State of the assembly setup (views, numbers, plan, own open items, metrics without time) for the fallback of _zone_fallback."""
    stats = ctx.get('_balloon_stats') or {}
    return {'asm_views': list(ctx['asm_views']), 'balloons': list(ctx['balloons']), 'bplan': ctx.get('_bplan'), 'items': list(ctx.get('_asm_open_items') or []),
            'stats': {k: v for k, v in stats.items() if k != 'seconds'}}


def _asm_restore(ctx, snap):
    """Restores the state from _asm_snapshot; the time in _balloon_stats['seconds'] keeps counting all runs."""
    _asm_drop_open(ctx)
    ctx['asm_views'], ctx['balloons'] = list(snap['asm_views']), list(snap['balloons'])
    ctx['_bplan'] = snap['bplan']
    ctx['open'] += snap['items']
    ctx['_asm_open_items'] = list(snap['items'])
    stats = ctx.setdefault('_balloon_stats', {})
    seconds = stats.get('seconds', 0.0)
    stats.clear()
    stats.update(snap['stats'], seconds=seconds)


def _zone_fallback(ctx, found, order, prober):
    """N1.9 fallback without a second full run: if the assembly stands between part views in the final state, _asm_setup(single) is computed (usually the isometric view
    alone carries all numbers) and only the block 'asm' of found['content'] is replaced by the newly built one; all other blocks (scales, enlarged part views, measured occupancies)
    and the prober stay. Measured, rearranged and packed on the same size and at the same scale. (found with new variant/content, snapshot) or (None, snapshot) if the
    fallback changes nothing or does not pack; then ctx stands as before. The result is checked by the caller (_accept_zone_fallback), who otherwise resets with _asm_restore."""
    snap = _asm_snapshot(ctx)
    old_ids = [v['id'] for v in ctx['asm_views']]
    _asm_setup(ctx, single=True)
    sheet = found['variant']['spec']['sheets'][0]
    scale, fmt_ = sheet['scale'], sheet['format']
    old_blocks = found['content'][0]
    fresh = _content(ctx, scale, None, found.get('bscales')) if [v['id'] for v in ctx['asm_views']] != old_ids else None
    new_asm = next((b for b in fresh[0] if b['name'] == pl.ASM_BLOCK), None) if fresh else None
    if new_asm is None or not any(b['name'] == pl.ASM_BLOCK for b in old_blocks):
        _asm_restore(ctx, snap)
        return None, snap
    others = [b for b in old_blocks if b['name'] != pl.ASM_BLOCK]
    if _used_letters(new_asm['views']) & set().union(*[_used_letters(b['views']) for b in others]):          # the label letters of the assembly views must not be assigned twice
        _asm_restore(ctx, snap)
        return None, snap
    trial = ([new_asm if b['name'] == pl.ASM_BLOCK else b for b in old_blocks], found['content'][1])
    # The new block is still estimated: it is measured on the first size from fmt_ on on which the probe packs (as in _enlarge_normal_views), fmt_ is published
    paper = next((p_ for f in order[order.index(fmt_):] if (p_ := _variant(ctx, trial, scale, f))), None) if prober.measured else _variant(ctx, trial, scale, fmt_)
    pr = prober.probe(paper['spec']) if paper is not None and prober.measured else None
    if pr is not None:
        trial = _remeasured(ctx, trial, scale, _occupancy_of(paper['spec'], pr))
    v = _variant(ctx, trial, scale, fmt_)
    if v is None:
        _asm_restore(ctx, snap)
        return None, snap
    return {**found, 'variant': v, 'content': trial}, snap


def _plan_pass(ctx, spec, out, max_n):
    """The planning run on ctx (see plan_spec); out is filled."""
    sug = ctx['suggestion'] or {'format': 'A3', 'scale': '1:1'}
    order, scales = an.ORDER, [an.scale_str(s) for s in an.SCALES]
    cache = {}

    def content(scale, main=None, bscales=None):
        key = (scale, json.dumps(main, sort_keys=True), json.dumps(bscales, sort_keys=True))
        if key not in cache:
            cache[key] = _content(ctx, scale, main, bscales)
        return cache[key]
    prober = _Prober(ctx, spec)
    found = _search(ctx, scales, order, sug, content, prober)
    if found is None:
        ctx['open'].append('No layout found: the content fits at no scale on sheets up to A0')
        return out
    found, part_notes = _part_scales(ctx, found, scales, order, content, prober)       # own part scales (N1.3), afterwards the fallback “assembly smaller” (N1.4)
    found, main_notes = _main_large(ctx, found, scales, order, content, prober)
    found, section_note = _enlarge_normal_views(ctx, found, scales, order, content, prober)
    if section_note:
        part_notes = ['Before the arrangement of the designated sections: ' + n for n in part_notes] + [section_note]
    found, iso_notes = _with_iso(ctx, found, scales, order, prober)

    def finish(f):
        b_, c_ = _visible_dimensions(ctx, f['variant'], f['content'], prober)
        return _spread_views(ctx, b_, c_, prober)
    base, mcontent, spread_notes = finish(found)
    if ctx['assembly'] and len(base['spec']['sheets']) == 1 and _zone_note(base['packed'], mcontent[0]):
        # N1.9: no position without a violation - reduce the assembly views (isometric view as carrier of all numbers) if that changes anything: only the block 'asm' is rebuilt, the rest
        # of the planning stays; the result only applies if it meets N1.9 and has no more blocking findings, otherwise the first run stays
        second, snap = _zone_fallback(ctx, found, order, prober)
        if second is not None:
            b2, c2, spread2 = finish(second)
            ok = len(b2['spec']['sheets']) == 1 and not _zone_note(b2['packed'], c2[0])
            first_blocking = blocking2 = None
            if ok and prober.measured:
                p1, p2 = _probe_settled(prober, base['spec']), _probe_settled(prober, b2['spec'])
                first_blocking, blocking2 = (p1 or {}).get('blocking'), (p2 or {}).get('blocking')
            if ok and _accept_zone_fallback(None, blocking2, first_blocking):
                found, base, mcontent, spread_notes = second, b2, c2, spread2
                cache.clear()                    # the assembly blocks of the other variants (alternatives) come from the new assembly
            else:
                _asm_restore(ctx, snap)
    s0 = base['spec']['sheets'][0]
    bfmt, bscale, multi = s0['format'], s0['scale'], len(base['spec']['sheets']) > 1
    if multi:
        ctx['notes'].append(f"No size up to A0 holds everything on one sheet: {len(base['spec']['sheets'])} sheets {bfmt}")
    small = next((f for f in order if _variant(ctx, mcontent, bscale, f)), bfmt) if not multi else bfmt       # smallest size on which the content packs at the chosen scales
    steps = order.index(bfmt) - order.index(small)
    out['layout'] = {'measured': prober.measured, 'probes': prober.n, 'format': bfmt, 'scale': bscale,
                     'override': found.get('override', []),   # a guard overrode the measurement: scale | estimate | format
                     'min_format': small, 'format_steps': steps, 'part_scales': dict(found.get('bscales') or {})}
    ctx['notes'].append(f"Sheet size {bfmt}, scale {bscale}: " + (f"from measured block sizes ({prober.n} probes)" if prober.measured
                        else f"block sizes estimated (estimate, measurement not possible: {prober.error})"))
    ctx['notes'] += found.get('override_notes', []) + found.get('guard_notes', []) + found.get('notes', []) + part_notes + main_notes + iso_notes + spread_notes
    ctx['notes'].append(f"Smallest size at the chosen scales: {small}, chosen: {bfmt} (size steps difference {steps})")       # format_steps metric
    ctx['notes'] += sorted({n for b in mcontent[0] for n in b.get('pruned', [])})       # dropped detail views/sections; planner notes of the stack budget
    iso_scales = found.get('iso_scales') or {}
    isoed = (lambda c, sc: _isoed(ctx, c, sc, {p: (x if an.scale_value(x) <= an.scale_value(sc) + 1e-12 else sc) for p, x in iso_scales.items()}))         if iso_scales else (lambda c, sc: c)
    alts = []
    if order.index(bfmt) + 1 < len(order):
        alts.append({**(_variant(ctx, mcontent, bscale, order[order.index(bfmt) + 1], multi) or {'packed': None}),
                     'kind': 'format'})
    if scales.index(bscale) + 1 < len(scales):
        smaller = scales[scales.index(bscale) + 1]
        alts.append({**(next((v for fmt in order for v in [_variant(ctx, isoed(content(smaller), smaller), smaller, fmt)] if v), None)
                        or {'packed': None}), 'kind': 'scale'})
    big = ctx['drawn'][0] if ctx['drawn'] else None
    opts = pv.main_options(ctx['P'][big]['part'], ctx['P'][big]['cls'], ctx['P'][big]['feats']) if big else []
    if len(opts) > 1 and opts[0][1] > 0:
        main = {big: opts[1][0]}
        alts.append({**(_variant(ctx, isoed(content(bscale, main), bscale), bscale, bfmt, multi) or {'packed': None}),
                     'kind': 'main', 'info': opts[1][1] / opts[0][1]})
    out['candidates'] = pl.candidates({**base, 'kind': 'base'}, alts, max_n)
    bstats = ctx.get('_balloon_stats') or {}
    for c in out['candidates'] if ctx['assembly'] else []:                    # time of _balloons, computed views and additional packing attempts of the zone rule (N1.9) as metrics (assemblies only), not in the score
        c['metrics'].update({'balloons_s': bstats.get('seconds', 0.0), 'balloon_views_evaluated': bstats.get('views_evaluated', 0), 'zone_tries': ctx.get('_zone_tries', 0)})
    if not multi and (znote := _zone_note(base['packed'], mcontent[0])):
        ctx['notes'].append(znote)                 # N1.9: no position without a violation, the previous one stays (once)
    for c in out['candidates']:                    # symmetry axes as a separate step after the views are fixed and before label layout and shortening probe
        prober.axes(c['spec'])
    for c in out['candidates']:                    # every shortening of a cell/block title text as a planner note with the full wording
        pr_ = prober.probe(c['spec'], place_labels=True)
        for note in truncation_notes((pr_ or {}).get('findings') or []):
            if note not in ctx['notes']:
                ctx['notes'].append(note)
    _opening_facts(base['spec'], ctx['function_dims'])
    carried = {round(float(e['value']), 4) for e in ctx['function_dims'] if e['status'] != 'none'}      # another part carries the value
    told = set()
    for e in ctx['function_dims']:         # functional dimensions from the model texts - what was dimensioned, what was not unambiguous
        who = ctx['who'](e['part']) if e['part'] in ctx['display'] else e['part']
        if e['status'] == 'none':          # "not found" only if no part of the model carries the value, and once per model
            if round(float(e['value']), 4) in carried or (round(float(e['value']), 4), e['text']) in told:
                continue
            told.add((round(float(e['value']), 4), e['text']))
        target, text = function_note(e, who)
        if text:
            ctx[target].append(text)
    return out
