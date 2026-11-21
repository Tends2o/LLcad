"""cad_gates: deterministic mesh and image checks of one revision.

Native (tessellation, no camera):
  visibility    per part: seen in any of six axis views (which floating parts matter)
  controls      rays along each declared control's outward direction from >= 200
                area-weighted samples on its outward faces; any hit on other
                geometry is an occluded sample (a covered key)
  sightline     a ray bundle along a declared optical axis; hits on geometry that is
                not allowed on the axis (lenses, windows) obstruct it
  stations      width of the model's cross-section at a declared station
  sides         sign of a feature's position along an axis (handedness)
  counts        separate items (touching mesh pieces merged) of a declared class
  envelope      parts that push the model beyond a declared overall dimension

Image, per reference with an absolute camera (registered = hard gate, otherwise
advisory):
  silhouette IoU, mean chamfer and p95 in mm; landmark residuals in mm (fit and check
  landmarks) and landmark-triple orientation (handedness); marking centroid in mm,
  size ratio and visible fraction; must-be-visible fractions; per-part colour against
  the declared hex on masked, white-balanced photo patches (glass and emissive parts
  excluded; Delta E00 for orthographic sources and close-ups, hue family for
  perspective photos); visible item counts.

Rows: {check, target, measured, unit, severity: fail|warn|pass|info, where, excess}.
Thresholds by source class (lens-quality 3.2) are defaults; the contract overrides.

CLI: gates.py gates SPEC.json -> JSON on stdout."""
import json, math, sys, time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

import render_vtk as rv
import register as rg

THRESHOLDS = {
    "orthographic": {"iou_min": 0.97, "chamfer_mm_max": 0.5, "p95_mm_max": 1.0, "landmark_pct_max": 1.0,
                     "marking_mm_max": 1.0, "marking_ratio_min": 0.9, "marking_ratio_max": 1.1,
                     "delta_e_max": 6.0, "colour": "delta_e", "visible_fraction_min": 0.8},
    "perspective": {"iou_min": 0.92, "chamfer_mm_max": 1.5, "p95_mm_max": 5.0, "landmark_pct_max": 1.5,
                    "marking_pct_max": 1.5, "marking_ratio_min": 0.85, "marking_ratio_max": 1.15,
                    "delta_e_max": 10.0, "colour": "hue_family", "visible_fraction_min": 0.8},
    "closeup": {"iou_min": None, "chamfer_mm_max": None, "p95_mm_max": None, "landmark_pct_max": 1.5,
                "marking_mm_max": 1.0, "marking_ratio_min": 0.85, "marking_ratio_max": 1.15,
                "delta_e_max": 10.0, "colour": "delta_e", "visible_fraction_min": 0.8},
}
SOURCE_CLASS = {"orthographic_turntable": "orthographic", "orthographic_render": "orthographic",
                "datasheet_render": "orthographic", "perspective_photo": "perspective", "closeup": "closeup"}


def row(check, target, measured, unit, severity, where, excess=0.0, **extra):
    r = {"check": check, "target": target, "measured": measured, "unit": unit, "severity": severity,
         "where": where, "excess": round(float(excess), 4)}
    r.update(extra)
    return r


def rnd(v, d=3):
    return None if v is None else round(float(v), d)


# --- native mesh checks --------------------------------------------------------------
class Geometry:
    def __init__(self, meshes, info):
        self.meshes = meshes
        self.info = info
        self.index = {m["feature_id"]: i for i, m in enumerate(meshes)}
        self._obb = {}

    def points(self, fids):
        ps = [self.meshes[self.index[f]]["points"] for f in fids if f in self.index]
        return np.concatenate(ps) if ps else np.zeros((0, 3))

    def bounds(self, fids):
        P = self.points(fids)
        return None if not len(P) else np.concatenate([P.min(0), P.max(0)])

    def locator(self, exclude=()):
        """OBB tree over every mesh except `exclude` (feature ids)."""
        key = tuple(sorted(exclude))
        if key in self._obb:
            return self._obb[key]
        app = rv.vtk.vtkAppendPolyData()
        n = 0
        for i, m in enumerate(self.meshes):
            if m["feature_id"] in exclude:
                continue
            app.AddInputData(rv._polydata(m["points"], m["tris"]))
            n += 1
        if not n:
            self._obb[key] = None
            return None
        app.Update()
        tree = rv.vtk.vtkOBBTree()
        tree.SetDataSet(app.GetOutput())
        tree.BuildLocator()
        self._obb[key] = (tree, app.GetOutput())
        return self._obb[key]

    def per_mesh_locators(self, fids):
        return {f: self.locator([x for x in self.index if x != f]) for f in fids}


def first_hit(tree, p0, p1):
    pts = rv.vtk.vtkPoints()
    ids = rv.vtk.vtkIdList()
    code = tree.IntersectWithLine(list(p0), list(p1), pts, ids)
    if code == 0 or pts.GetNumberOfPoints() == 0:
        return None, None
    d = [np.linalg.norm(np.array(pts.GetPoint(k)) - p0) for k in range(pts.GetNumberOfPoints())]
    k = int(np.argmin(d))
    return float(d[k]), int(ids.GetId(k))


def sample_faces(P, T, direction=None, n=240, min_cos=0.5, seed=0):
    """Area-weighted samples on triangles facing `direction` (or all, with their
    normals). Returns points and the ray directions."""
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    nrm = np.cross(b - a, c - a)
    area = np.linalg.norm(nrm, axis=1) / 2
    ok = area > 1e-9
    nrm = np.where(ok[:, None], nrm / np.maximum(area[:, None] * 2, 1e-12), 0)
    if direction is not None:
        d = rv._unit(direction)
        # outward faces: triangle winding may be either way, accept both orientations
        cosv = nrm @ d
        # preview triangles are wound outward; a mesh wound the other way falls back
        sel = ok & (cosv >= min_cos)
        if not sel.any():
            sel = ok & (np.abs(cosv) >= min_cos)
    else:
        sel = ok
    if not sel.any():
        return np.zeros((0, 3)), np.zeros((0, 3))
    idx = np.nonzero(sel)[0]
    w = area[idx] / area[idx].sum()
    rng = np.random.default_rng(seed)
    pick = rng.choice(idx, size=n, p=w)
    r1, r2 = rng.random(n), rng.random(n)
    s = np.sqrt(r1)
    pts = (1 - s)[:, None] * a[pick] + (s * (1 - r2))[:, None] * b[pick] + (s * r2)[:, None] * c[pick]
    if direction is not None:
        dirs = np.repeat(rv._unit(direction)[None, :], n, 0)
    else:
        dirs = nrm[pick]
    return pts, dirs


def control_rows(geo, controls, span):
    rows = []
    for c in controls:
        fids = [f for f in c["feature_ids"] if f in geo.index]
        if not fids:
            rows.append(row("control_visibility", "0 occluded", "missing geometry", "", "fail", c["id"], 1.0))
            continue
        P = geo.points(fids)
        tris = []
        off = 0
        for f in fids:
            m = geo.meshes[geo.index[f]]
            tris.append(m["tris"] + off)
            off += len(m["points"])
        T = np.concatenate(tris)
        direction = c.get("direction")
        if direction is None:
            # outward: from the model's centre through the control's centre, snapped to
            # the dominant axis of its face normals
            b = geo.bounds(list(geo.index))
            centre = (b[:3] + b[3:]) / 2
            direction = rv._unit(P.mean(0) - centre)
        n = int(max(200, c.get("samples", 240)))
        pts, dirs = sample_faces(P, T, direction, n)
        if not len(pts):
            rows.append(row("control_visibility", "0 occluded", "no face toward direction", "", "fail", c["id"], 1.0))
            continue
        loc = geo.locator(fids)
        occluded, blockers = 0, {}
        if loc is not None:
            tree, poly = loc
            for p, d in zip(pts, dirs):
                p0 = p + d * 0.05
                dist, cell = first_hit(tree, p0, p0 + d * span * 2)
                if dist is not None:
                    occluded += 1
                    # which feature: map the hit cell back through the append order
                    blockers[cell] = blockers.get(cell, 0) + 1
        frac = occluded / len(pts)
        by = cell_features(geo, fids, list(blockers))
        rows.append(row("control_visibility", "0 occluded", f"{occluded}/{len(pts)}", "samples",
                        "fail" if occluded else "pass", c["id"], frac,
                        **({"blocked_by": by[:6]} if by else {})))
    return rows


def cell_features(geo, excluded, cells):
    """Feature ids of appended-polydata cells (in append order, excluded meshes skipped)."""
    if not cells:
        return []
    starts, names, off = [], [], 0
    for m in geo.meshes:
        if m["feature_id"] in excluded:
            continue
        starts.append(off)
        names.append(m["feature_id"])
        off += len(m["tris"])
    starts = np.array(starts)
    out = []
    for c in cells:
        k = int(np.searchsorted(starts, c, side="right") - 1)
        if 0 <= k < len(names) and names[k] not in out:
            out.append(names[k])
    return out


def sightline_rows(geo, axes):
    rows = []
    for a in axes:
        o = np.asarray(a["origin_mm"], float)
        d = rv._unit(a["direction"])
        L = float(a["length_mm"])
        r = float(a.get("radius_mm", 0) or 0)
        allowed = set(a.get("allowed_feature_ids") or [])
        # glass is transparent on an optical axis unless declared otherwise
        allowed |= {f for f, inf in geo.info.items() if inf.get("glass")}
        loc = geo.locator(allowed)
        u = np.cross(d, [1.0, 0, 0] if abs(d[0]) < 0.9 else [0, 1.0, 0])
        u = rv._unit(u)
        v = np.cross(d, u)
        offsets = [(0.0, 0.0)]
        if r > 0:
            for ring, k in ((0.5, 6), (1.0, 12)):
                for j in range(k):
                    t = 2 * math.pi * j / k
                    offsets.append((ring * r * math.cos(t), ring * r * math.sin(t)))
        blocked, by = 0, set()
        if loc is not None:
            tree, poly = loc
            for du, dv in offsets:
                p0 = o + u * du + v * dv
                dist, cell = first_hit(tree, p0, p0 + d * L)
                if dist is not None:
                    blocked += 1
                    by.update(cell_features(geo, allowed, [cell]))
        rows.append(row("sightline", "0 blocked", f"{blocked}/{len(offsets)}", "rays", "fail" if blocked else "pass",
                        a["id"], blocked / len(offsets), **({"blocked_by": sorted(by)[:6]} if by else {})))
    return rows


def station_rows(geo, stations):
    ax = {"x": 0, "y": 1, "z": 2}
    rows = []
    for s in stations:
        fids = s.get("feature_ids") or list(geo.index)
        k, j = ax[s["at_axis"]], ax[s["axis"]]
        c = float(s["at_mm"])
        pts = []
        for f in fids:
            if f not in geo.index:
                continue
            m = geo.meshes[geo.index[f]]
            P, T = m["points"], m["tris"]
            dv = P[:, k] - c
            for e0, e1 in ((0, 1), (1, 2), (2, 0)):
                a, b = T[:, e0], T[:, e1]
                cross = (dv[a] * dv[b]) < 0
                if cross.any():
                    t = dv[a[cross]] / (dv[a[cross]] - dv[b[cross]])
                    pts.append(P[a[cross]] + t[:, None] * (P[b[cross]] - P[a[cross]]))
        if not pts:
            rows.append(row("station_width", f"{s['value_mm']} ± {s['tolerance_mm']}", "no section", "mm", "fail", s["id"], 1.0))
            continue
        Q = np.concatenate(pts)
        w = float(Q[:, j].max() - Q[:, j].min())
        err = abs(w - float(s["value_mm"]))
        tol = float(s["tolerance_mm"])
        rows.append(row("station_width", f"{s['value_mm']} ± {tol}", round(w, 3), "mm",
                        "fail" if err > tol else "pass", s["id"], err / max(tol, 1e-6) - 1 if err > tol else 0))
    return rows


def side_rows(geo, sides, bounds):
    ax = {"x": 0, "y": 1, "z": 2}
    rows = []
    for s in sides:
        b = geo.bounds(s["feature_ids"])
        if b is None:
            rows.append(row("handedness_side", ("+" if s["sign"] > 0 else "-") + s["axis"], "missing geometry", "", "fail", s["id"], 1.0))
            continue
        k = ax[s["axis"]]
        plane = float(s["plane_mm"]) if s.get("plane_mm") is not None else float((bounds[k] + bounds[k + 3]) / 2)
        pos = float((b[k] + b[k + 3]) / 2) - plane
        ok = np.sign(pos) == np.sign(s["sign"]) and abs(pos) >= 0.5
        sev = "pass" if ok else ("warn" if abs(pos) < 0.5 else "fail")
        rows.append(row("handedness_side", ("+" if s["sign"] > 0 else "-") + s["axis"], round(pos, 2), "mm",
                        sev, s["id"], 0 if ok else 1.0))
    return rows


def item_count(geo, fids, gap=0.1):
    boxes = []
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    for f in fids:
        if f not in geo.index:
            continue
        m = geo.meshes[geo.index[f]]
        T, n = m["tris"], len(m["points"])
        r = np.concatenate([T[:, 0], T[:, 1], T[:, 2]])
        c = np.concatenate([T[:, 1], T[:, 2], T[:, 0]])
        k, lab = connected_components(coo_matrix((np.ones(len(r)), (r, c)), shape=(n, n)))
        for i in range(k):
            P = m["points"][lab == i]
            if len(P):
                boxes.append(np.concatenate([P.min(0), P.max(0)]))
    if not boxes:
        return 0
    B = np.array(boxes)
    parent = list(range(len(B)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(B)):
        lo = B[i, :3] - gap
        hi = B[i, 3:] + gap
        near = np.nonzero(np.all(B[:, :3] <= hi, 1) & np.all(B[:, 3:] >= lo, 1))[0]
        for j in near:
            parent[find(j)] = find(i)
    return len({find(i) for i in range(len(B))})


def count_rows(geo, counts):
    rows = []
    for c in counts:
        n = item_count(geo, c["feature_ids"])
        rows.append(row("count", str(c["expected"]), n, "items", "pass" if n == c["expected"] else "fail",
                        f"{c['id']} ({c.get('label', '')})".strip(), abs(n - c["expected"])))
    return rows


def envelope_rows(geo, envelope, parts_of):
    """Parts that push the model beyond a declared overall dimension: removed greedily
    (most extreme first) until the extent fits; each removed part is a row."""
    ax = {"x": 0, "y": 1, "z": 2}
    rows = []
    pb = {}
    for pid, fids in parts_of.items():
        b = geo.bounds(fids)
        if b is not None:
            pb[pid] = b
    if not pb:
        return rows
    for e in envelope:
        k = ax[e["axis"]]
        limit = float(e["value_mm"]) + float(e["tolerance_mm"])
        active = dict(pb)
        removed = []
        for _ in range(6):
            lo = min(b[k] for b in active.values())
            hi = max(b[k + 3] for b in active.values())
            if hi - lo <= limit or len(active) <= 1:
                break
            # the part whose removal shrinks the extent most
            best, gain = None, 0.0
            for pid in active:
                rest = [b for q, b in active.items() if q != pid]
                ext = max(b[k + 3] for b in rest) - min(b[k] for b in rest)
                if (hi - lo) - ext > gain:
                    best, gain = pid, (hi - lo) - ext
            if best is None or gain <= 1e-6:
                break
            removed.append((best, gain))
            del active[best]
        lo = min(b[k] for b in pb.values())
        hi = max(b[k + 3] for b in pb.values())
        for pid, gain in removed:
            rows.append(row("outside_envelope", f"<= {e['value_mm']} + {e['tolerance_mm']} along {e['axis']}",
                            round(gain, 2), "mm beyond", "fail", pid, gain / max(float(e["value_mm"]), 1)))
        if not removed:
            rows.append(row("outside_envelope", f"<= {e['value_mm']} + {e['tolerance_mm']} along {e['axis']}",
                            round(hi - lo, 2), "mm extent", "pass", e["id"], 0))
    return rows


def visible_parts(scene, parts_of, bounds):
    """Parts seen in any of the six axis views (orthographic, 480 px)."""
    feat_part = {f: p for p, fs in parts_of.items() for f in fs}
    seen = set()
    for name in ("front", "back", "left", "right", "top", "bottom"):
        cam = rv.Camera(rv.preset_camera(name, bounds, 480, 480, "orthographic"))
        scene.set_camera(cam)
        ids = scene.ids()
        for code in np.unique(ids):
            if 0 < code <= len(scene.meshes):
                f = scene.feature(code - 1)
                seen.add(feat_part.get(f, f))
    return seen


# --- image checks ----------------------------------------------------------------------
def lab_patch(photo, m):
    px = photo[m].reshape(-1, 3).astype(float)
    if len(px) < 20:
        return None
    L = rv.srgb_to_lab(px)[:, 0]
    lo, hi = np.percentile(L, 20), np.percentile(L, 90)
    sel = px[(L >= lo) & (L <= hi)]
    if len(sel) < 10:
        sel = px
    return rv.srgb_to_lab(np.median(sel, 0))


def family_mismatch(decl, obs):
    Cd, Co = math.hypot(decl[1], decl[2]), math.hypot(obs[1], obs[2])
    if Cd < 8 and Co < 12:
        # neutrals: only dark against light is a different family (lighting moves L*)
        return (decl[0] < 35 and obs[0] > 65) or (decl[0] > 65 and obs[0] < 35)
    if Cd < 8 or Co < 8:
        return (Cd < 8) != (Co < 8) and max(Cd, Co) > 20
    hd = math.degrees(math.atan2(decl[2], decl[1])) % 360
    ho = math.degrees(math.atan2(obs[2], obs[1])) % 360
    dh = abs((hd - ho + 180) % 360 - 180)
    return dh > 45


def view_rows(v, scene, geo, parts, th, feat_part):
    """All image checks of one reference view."""
    rows = []
    hard = bool(v.get("registered"))
    fail = "fail" if hard else "warn"
    where = v["reference_id"]
    rgb, alpha, ext = rg.load_reference(v["image"], v.get("mask"))
    H, W = rgb.shape[:2]
    H0, W0 = H, W  # landmark pixels are in the original image
    cam = rv.Camera(v["camera"])
    if (cam.W, cam.H) != (W, H):
        cam = cam.scaled(W / cam.W) if abs(W / cam.W - H / cam.H) < 1e-3 else cam
    if (cam.W, cam.H) != (W, H):
        rgb = rg.resize_rgb(rgb, cam.W, cam.H)
        alpha = None if alpha is None else np.asarray(Image.fromarray(alpha).resize((cam.W, cam.H)))
        ext = None if ext is None else rg.resize_mask(ext, cam.W, cam.H)
        H, W = cam.H, cam.W
    ref = rg.object_mask(rgb, alpha, v.get("exclusions") or (), ext)
    scene.set_camera(cam)
    ids = scene.ids()
    model = ids > 0
    ys, xs = np.nonzero(ref)
    height_mm = float((ys.max() - ys.min() + 1) * cam.mm_per_px) if len(ys) else 1.0
    metrics = {"reference_id": where, "registered": hard, "source_class": v["source_class"],
               "mm_per_px": round(cam.mm_per_px, 5), "height_mm": round(height_mm, 2)}
    # silhouette
    if th.get("iou_min") is not None and ref.any():
        iou = float((model & ref).sum() / max((model | ref).sum(), 1))
        me, te = rg.edges(model), rg.edges(ref)
        if me.any() and te.any():
            d1 = ndimage.distance_transform_edt(~te)[me]
            d2 = ndimage.distance_transform_edt(~me)[te]
            ch = 0.5 * (d1.mean() + d2.mean()) * cam.mm_per_px
            p95 = float(np.percentile(np.concatenate([d1, d2]), 95)) * cam.mm_per_px
        else:
            ch = p95 = float("inf")
        metrics.update(silhouette_iou=round(iou, 4), chamfer_mm=rnd(ch), p95_mm=rnd(p95))
        rows.append(row("silhouette_iou", f">= {th['iou_min']}", round(iou, 4), "", fail if iou < th["iou_min"] else "pass",
                        where, (th["iou_min"] - iou) / max(1 - th["iou_min"], 0.01) if iou < th["iou_min"] else 0))
        if th.get("chamfer_mm_max") is not None:
            rows.append(row("silhouette_chamfer", f"<= {th['chamfer_mm_max']}", rnd(ch), "mm",
                            fail if ch > th["chamfer_mm_max"] else "pass", where, ch / th["chamfer_mm_max"] - 1 if ch > th["chamfer_mm_max"] else 0))
        if th.get("p95_mm_max") is not None:
            rows.append(row("silhouette_p95", f"<= {th['p95_mm_max']}", rnd(p95), "mm",
                            fail if p95 > th["p95_mm_max"] else "pass", where, p95 / th["p95_mm_max"] - 1 if p95 > th["p95_mm_max"] else 0))
    # landmarks
    lms = v.get("landmarks") or []
    if lms:
        P = np.array([l["point_mm"] for l in lms], float)
        uv = np.array([l["px"] for l in lms], float) * np.array([W / W0, H / H0])
        uvp, depth = cam.project(P)
        lim = th["landmark_pct_max"] / 100 * height_mm
        errs = []
        for l, o, pr, p3 in zip(lms, uv, uvp, P):
            e = float(np.hypot(*(pr - o))) * cam.mm_per_px_at(p3)
            errs.append(e)
            rows.append(row("landmark", f"<= {round(lim, 2)}", round(e, 2), "mm", fail if e > lim else "pass",
                            f"{where}:{l.get('id') or l.get('feature_id') or '?'}", e / lim - 1 if e > lim else 0,
                            **({"role": "check"} if l.get("role") == "check" else {})))
        metrics["landmark_rms_mm"] = rnd(math.sqrt(np.mean(np.square(errs))))
        if len(lms) >= 3:
            agree, total = rg.triple_agreement(np.c_[uv, np.zeros(len(uv))], np.c_[uvp, np.zeros(len(uvp))], (0.02 * H) ** 2)
            if total:
                rows.append(row("handedness_landmarks", f"{total}/{total} triples agree", f"{agree}/{total}", "triples",
                                fail if agree < total else "pass", where, (total - agree) / total))
    reg = v.get("registration") or {}
    verdict = (reg.get("handedness") or {}).get("verdict")
    if verdict in ("mirror_fits_better", "model_fits_better", "ambiguous"):
        same = reg.get("revision") == v.get("revision")
        rows.append(row("handedness_mirror_test", "model_fits_better", verdict, "", "pass" if verdict == "model_fits_better"
                        else ((fail if same else "warn") if verdict == "mirror_fits_better" else "warn"),
                        f"{where} (registration of {reg.get('revision', '?')})", 1.0 if verdict == "mirror_fits_better" else 0))
    # per-feature masks
    code_of = {scene.feature(i): i + 1 for i in range(len(scene.meshes))}

    def proj_mask(fids):
        codes = [code_of[f] - 1 for f in fids if f in code_of]
        if not codes:
            return None
        return scene.ids(only=set(codes)) > 0

    def vis_mask(fids):
        codes = [code_of[f] for f in fids if f in code_of]
        return np.isin(ids, codes) if codes else None
    for mk in v.get("markings") or []:
        vm, pm = vis_mask(mk["feature_ids"]), proj_mask(mk["feature_ids"])
        scene.set_camera(cam)
        if pm is None or not pm.any():
            rows.append(row("marking", "in view", "not in view", "", fail, f"{where}:{mk['id']}", 1.0))
            continue
        bx = np.array(mk["box"], float) * np.array([W, H, W, H])
        src_c = np.array([(bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2])
        py, px = np.nonzero(pm)
        mod_c = np.array([px.mean() + 0.5, py.mean() + 0.5])
        b3 = geo.bounds(mk["feature_ids"])
        mmpp = cam.mm_per_px_at((b3[:3] + b3[3:]) / 2) if b3 is not None else cam.mm_per_px
        off = float(np.hypot(*(mod_c - src_c))) * mmpp
        lim = th["marking_mm_max"] if th.get("marking_mm_max") is not None else th["marking_pct_max"] / 100 * height_mm
        rows.append(row("marking_centroid", f"<= {round(lim, 2)}", round(off, 2), "mm", fail if off > lim else "pass",
                        f"{where}:{mk['id']}", off / lim - 1 if off > lim else 0))
        mb = (px.max() - px.min() + 1) * (py.max() - py.min() + 1)
        sb = max((bx[2] - bx[0]) * (bx[3] - bx[1]), 1)
        ratio = math.sqrt(mb / sb)
        bad = ratio < th["marking_ratio_min"] or ratio > th["marking_ratio_max"]
        rows.append(row("marking_size_ratio", f"{th['marking_ratio_min']}..{th['marking_ratio_max']}", round(ratio, 3), "",
                        fail if bad else "pass", f"{where}:{mk['id']}", abs(math.log(ratio)) if bad else 0))
        frac = float((vm & pm).sum() / max(pm.sum(), 1)) if vm is not None else 0.0
        vmin = th.get("visible_fraction_min", 0.8)
        rows.append(row("marking_visible", f">= {vmin}", round(frac, 3), "fraction", fail if frac < vmin else "pass",
                        f"{where}:{mk['id']}", (vmin - frac) / vmin if frac < vmin else 0))
    for mv in v.get("must_be_visible") or []:
        vm, pm = vis_mask(mv["feature_ids"]), proj_mask(mv["feature_ids"])
        scene.set_camera(cam)
        if pm is None or not pm.any():
            continue
        frac = float((vm & pm).sum() / max(pm.sum(), 1)) if vm is not None else 0.0
        vmin = float(mv.get("min_fraction", th.get("visible_fraction_min", 0.8)))
        rows.append(row("visible_fraction", f">= {vmin}", round(frac, 3), "fraction", fail if frac < vmin else "pass",
                        f"{where}:{mv['id']}", (vmin - frac) / vmin if frac < vmin else 0))
    for c in v.get("counts") or []:
        vm = vis_mask(c["feature_ids"])
        n = 0
        if vm is not None and vm.any():
            lab, n = ndimage.label(ndimage.binary_dilation(vm, iterations=1))
            sizes = np.bincount(lab.ravel())[1:]
            n = int((sizes >= 6).sum())
        exp = c["expected_visible"]
        rows.append(row("visible_count", str(exp), n, "items", fail if n != exp else "pass", f"{where}:{c['id']}", abs(n - exp)))
    # colour: per part, masked + eroded photo patches, white-balanced on a neutral part
    obj_px = max(int(ref.sum()), 1)
    photo = rgb.astype(float)
    part_px = {}
    for code in np.unique(ids):
        if 0 < code <= len(scene.meshes):
            f = scene.feature(code - 1)
            part_px.setdefault(feat_part.get(f, f), []).append(code)
    stats = []
    for pid, codes in part_px.items():
        info = parts.get(pid, {})
        if info.get("glass") or info.get("emissive"):
            continue
        m = np.isin(ids, codes) & ref
        m = ndimage.binary_erosion(m, iterations=2)
        n = int(m.sum())
        if n < max(150, 0.002 * obj_px):
            continue
        stats.append((pid, m, n, rv.srgb_to_lab(np.array(rv.hex_rgb(info.get("colour")), float))))
    gains = np.ones(3)
    neutral = [s for s in stats if math.hypot(s[3][1], s[3][2]) < 6 and s[3][0] > 50]
    if neutral:
        pid, m, n, lab = max(neutral, key=lambda s: s[2])
        med = np.median(photo[m].reshape(-1, 3), 0)
        decl = np.array(rv.hex_rgb(parts.get(pid, {}).get("colour")), float)
        gains = np.clip(rv.srgb_to_linear(decl) / np.maximum(rv.srgb_to_linear(med), 1e-4), 0.6, 1.6)
        metrics["white_balance"] = {"part": pid, "gains": [round(float(g), 3) for g in gains]}
    balanced = rv.linear_to_srgb(rv.srgb_to_linear(photo) * gains)
    colour_mode = th.get("colour", "hue_family")
    for pid, m, n, decl in stats:
        obs = lab_patch(balanced, m)
        if obs is None:
            continue
        de = float(rv.delta_e00(decl, obs))
        share = n / obj_px
        name = parts.get(pid, {}).get("name", pid)
        if colour_mode == "delta_e" and share >= 0.02:
            lim = th["delta_e_max"]
            rows.append(row("colour_delta_e00", f"<= {lim}", round(de, 1), "dE00", fail if de > lim else "pass",
                            f"{where}:{pid} ({name[:40]})", de / lim - 1 if de > lim else 0))
        else:
            mism = family_mismatch(decl, obs)
            rows.append(row("colour_family", rv.hue_family(decl), rv.hue_family(obs), "", fail if mism else "pass",
                            f"{where}:{pid} ({name[:40]})", 1.0 if mism else 0, delta_e00=round(de, 1)))
            if not mism and de > th["delta_e_max"]:
                rows.append(row("colour_delta_e00", f"<= {th['delta_e_max']} (advisory)", round(de, 1), "dE00", "warn",
                                f"{where}:{pid} ({name[:40]})", de / th["delta_e_max"] - 1))
    return rows, metrics


# --- main ------------------------------------------------------------------------------
def gates(spec):
    t0 = time.monotonic()
    meshes = rv.load_preview(spec["preview"])
    info = spec.get("info") or {}
    parts = spec.get("parts") or {}
    geo = Geometry(meshes, info)
    bounds = rv.mesh_bounds(meshes)
    span = float(max(bounds[3:] - bounds[:3]))
    parts_of = {}
    for f, inf in info.items():
        if f in geo.index:
            parts_of.setdefault(inf.get("part") or f, []).append(f)
    for m in meshes:
        if m["feature_id"] not in info:
            parts_of.setdefault(m["feature_id"], []).append(m["feature_id"])
    feat_part = {f: p for p, fs in parts_of.items() for f in fs}
    scene = rv.Scene(meshes, info)
    rows = []
    timings = {}
    want = set(spec.get("checks") or [])

    def on(name):
        return not want or name in want
    t = time.monotonic()
    seen = visible_parts(scene, parts_of, bounds)
    timings["visibility"] = round(time.monotonic() - t, 2)
    if on("controls") and spec.get("controls"):
        t = time.monotonic()
        rows += control_rows(geo, spec["controls"], span)
        timings["controls"] = round(time.monotonic() - t, 2)
    if on("sightline") and spec.get("optical_axes"):
        rows += sightline_rows(geo, spec["optical_axes"])
    if on("stations") and spec.get("stations"):
        rows += station_rows(geo, spec["stations"])
    if on("sides") and spec.get("sides"):
        rows += side_rows(geo, spec["sides"], bounds)
    if on("counts") and spec.get("counts"):
        rows += count_rows(geo, spec["counts"])
    if on("envelope") and spec.get("envelope"):
        rows += envelope_rows(geo, spec["envelope"], parts_of)
    views = []
    if on("images"):
        for v in spec.get("views") or []:
            t = time.monotonic()
            cls = SOURCE_CLASS.get(v.get("source_type") or "", "orthographic" if v["camera"].get("projection") == "orthographic" else "perspective")
            th = dict(THRESHOLDS[cls])
            th.update({k: val for k, val in ((spec.get("thresholds") or {}).get(cls) or {}).items() if val is not None})
            v["source_class"] = cls
            try:
                r, met = view_rows(v, scene, geo, parts, th, feat_part)
            except Exception as e:  # one broken view never hides the others
                r, met = [row("image_view", "evaluated", f"error: {str(e)[:120]}", "", "warn", v["reference_id"], 0)], {
                    "reference_id": v["reference_id"], "error": str(e)[:200]}
            met["seconds"] = round(time.monotonic() - t, 2)
            rows += r
            views.append(met)
    return {"rows": rows, "views": views, "visible_parts": sorted(seen), "parts_total": len(parts_of),
            "timings": timings, "seconds": round(time.monotonic() - t0, 2)}


if __name__ == "__main__":
    action, path = sys.argv[1], sys.argv[2]
    spec = json.loads(Path(path).read_text())
    if action != "gates":
        raise SystemExit("unknown action " + action)
    sys.stdout.write(json.dumps(gates(spec)))
