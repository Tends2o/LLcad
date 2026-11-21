"""cad_reference_register: fit an absolute camera to one reference image.

1. Reference mask: the image's alpha channel when it has one (or a supplied mask),
   else pixels that differ from the corner median; declared exclusion boxes (badges,
   hands, stands) are removed, then the largest component (plus components of at
   least 20 % of it) is kept.
2. Initial pose: with landmarks (image pixel <-> model point) a pose + focal solve
   (scipy least_squares over a focal grid and 104 rotation starts; orthographic:
   scaled orthographic solve for turntable frames and datasheet renders); without
   landmarks the stored/declared camera or a coarse direction grid.
3. Refinement: Powell on (1 - silhouette IoU) + chamfer (mm, relative to the model
   size) + landmark reprojection (mm, relative) + a part-class term (dark declared
   parts vs dark photo pixels; glass and emissive parts ignored), coarse to fine.
   Multistart includes the mirrored azimuths (camera direction reflected across the
   model's X and Y planes and both), because silhouettes alone cannot tell front
   from back.
4. Handedness: the mirror image of the model (reflected across its X mid-plane,
   landmarks mirrored with it) is registered the same way; whichever explains the
   image better is reported. A model that its own mirror image beats is suspect.

Output: camera, residuals (IoU, chamfer mm, p95 mm, landmark RMS px/mm/% of height,
part-class IoU), starts, facing and handedness checks, registered (only when every
threshold holds) with reasons, and a residual overlay PNG.

CLI: register.py register SPEC.json -> JSON on stdout."""
import json, math, sys, time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from scipy import ndimage, optimize

import render_vtk as rv

Image.MAX_IMAGE_PIXELS = 80_000_000
COARSE_EDGE = 280
FINE_EDGE = 560
DARK_L = 35.0
# Perspective field of view kept in a physical range: below ~3 degrees a photo is
# indistinguishable from an orthographic one and the distance runs away.
FOV_MIN, FOV_MAX = 3.0, 50.0


class Budget(Exception):
    pass


# --- reference image ----------------------------------------------------------------
def load_reference(path, mask_path=None):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        rgba = im.convert("RGBA")
    alpha = np.asarray(rgba.getchannel("A"))
    # transparent pixels carry arbitrary RGB: composite over white for colours/overlays
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    white.alpha_composite(rgba)
    rgb = np.asarray(white.convert("RGB"))
    has_alpha = alpha.min() < 250 and alpha.max() > 5
    ext = None
    if mask_path:
        with Image.open(mask_path) as m:
            m = ImageOps.exif_transpose(m)
            if m.size != (rgb.shape[1], rgb.shape[0]):
                m = m.resize((rgb.shape[1], rgb.shape[0]), Image.Resampling.NEAREST)
            a = np.asarray(m.convert("RGBA"))
            ext = (a[:, :, 3] > 127) if a[:, :, 3].min() < 250 else (a[:, :, :3].max(2) > 127)
    return rgb, (alpha if has_alpha else None), ext


def object_mask(rgb, alpha=None, exclusions=(), external=None, keep_fraction=0.2):
    H, W = rgb.shape[:2]
    if external is not None:
        m = external.copy()
    elif alpha is not None:
        m = alpha > 127
    else:
        a = rgb.astype(float)
        k = max(4, min(H, W) // 80)
        corners = np.concatenate([a[:k, :k].reshape(-1, 3), a[-k:, :k].reshape(-1, 3),
                                  a[:k, -k:].reshape(-1, 3), a[-k:, -k:].reshape(-1, 3)])
        m = np.max(np.abs(a - np.median(corners, 0)), 2) > 28
    for b in exclusions or ():
        m[int(b[1] * H):int(math.ceil(b[3] * H)), int(b[0] * W):int(math.ceil(b[2] * W))] = False
    m = ndimage.binary_fill_holes(m)
    lab, n = ndimage.label(m)
    if n:
        sizes = np.bincount(lab.ravel())
        sizes[0] = 0
        m = sizes[lab] >= keep_fraction * sizes.max()
    return m


def resize_mask(m, W, H):
    if m.shape == (H, W):
        return m
    return np.asarray(Image.fromarray(m.astype(np.uint8) * 255).resize((W, H), Image.Resampling.BILINEAR)) > 127


def resize_rgb(rgb, W, H):
    if rgb.shape[:2] == (H, W):
        return rgb
    return np.asarray(Image.fromarray(rgb).resize((W, H), Image.Resampling.BILINEAR))


def edges(m):
    return m ^ ndimage.binary_erosion(m)


# --- camera parameterisation -----------------------------------------------------------
def rodrigues(w):
    th = float(np.linalg.norm(w))
    if th < 1e-12:
        return np.eye(3)
    k = np.asarray(w, float) / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * K @ K


class Param:
    """x = [w (3, rotation about the target in camera axes), sx, sy (target shift in
    frame heights), log size (frame height at the target), log fov (perspective)]."""

    def __init__(self, cam):
        self.c0 = cam
        self.ortho = cam.ortho
        self.S0 = cam.mm_per_px * cam.H  # frame height in mm at the target
        self.n = 6 if self.ortho else 7
        self.scale = np.array([0.08, 0.08, 0.08, 0.02, 0.02, 0.04] + ([] if self.ortho else [0.15]))

    def camera(self, x, W=None, H=None):
        c0 = self.c0
        W, H = W or c0.W, H or c0.H
        w_world = c0.R.T @ np.asarray(x[:3], float)
        Q = rodrigues(w_world)
        R = c0.R @ Q.T
        S = self.S0 * math.exp(float(np.clip(x[5], -3, 3)))
        tgt = c0.tgt + (x[3] * c0.R[0] + x[4] * c0.R[1]) * self.S0
        d = {"mode": "absolute", "projection": "orthographic" if self.ortho else "perspective",
             "target_mm": tgt.tolist(), "up": R[1].tolist(), "width_px": int(W), "height_px": int(H)}
        if self.ortho:
            d["mm_per_px"] = S / H
            d["position_mm"] = (tgt - R[2] * max(c0.depth, 1.0)).tolist()
        else:
            fov = float(np.clip(c0.fov * math.exp(float(np.clip(x[6], -3, 3))), FOV_MIN, FOV_MAX))
            dist = S / (2 * math.tan(math.radians(fov) / 2))
            d["fov_deg"] = fov
            d["position_mm"] = (tgt - R[2] * dist).tolist()
        return rv.Camera(d)


def reflect_camera(cam, axis, center):
    """The camera mirrored across the plane axis=center through the target: position and
    target reflected, up re-derived from the reflected up (a proper camera again)."""
    def refl(v, is_point=True):
        v = np.array(v, float)
        v[axis] = (2 * center[axis] - v[axis]) if is_point else -v[axis]
        return v
    d = cam.to_dict()
    d["position_mm"] = refl(cam.pos).tolist()
    d["target_mm"] = refl(cam.tgt).tolist()
    d["up"] = refl(cam.R[1], False).tolist()
    return rv.Camera(d)


# --- landmark pose solve -----------------------------------------------------------------
def _dirs():
    out = []
    for x in (-1, 0, 1):
        for y in (-1, 0, 1):
            for z in (-1, 0, 1):
                if x or y or z:
                    out.append(np.array([x, y, z], float) / math.sqrt(x * x + y * y + z * z))
    return out


def _look(direction, up, roll):
    """Proper rotation rows (right, up, back) of a camera placed along `direction`
    (target -> camera) with the image up near `up`, rolled by `roll`."""
    b = rv._unit(direction)
    f = -b
    r = np.cross(f, up)
    if np.linalg.norm(r) < 1e-6:
        r = np.cross(f, [1.0, 0, 0])
    r = rv._unit(r)
    u = np.cross(r, f)
    c, s = math.cos(roll), math.sin(roll)
    return np.stack([c * r + s * u, -s * r + c * u, b])


def _rot_to_vec(R):
    # rotation matrix (rows = camera axes) -> rotation vector of R^T
    M = R.T
    th = math.acos(max(-1.0, min(1.0, (np.trace(M) - 1) / 2)))
    if th < 1e-9:
        return np.zeros(3)
    if abs(th - math.pi) < 1e-6:
        w, v = np.linalg.eigh((M + np.eye(3)) / 2)
        return v[:, np.argmax(w)] * th
    return th / (2 * math.sin(th)) * np.array([M[2, 1] - M[1, 2], M[0, 2] - M[2, 0], M[1, 0] - M[0, 1]])


def solve_landmarks(P, uv, W, H, ortho, up_hint=(0, 0, 1), init=None):
    """Pose from landmarks. Returns a list of (rms_px, camera) sorted best first."""
    P = np.asarray(P, float)
    uv = np.asarray(uv, float)
    n = len(P)
    cx, cy = W / 2, H / 2
    c3 = P.mean(0)
    ext3 = float(np.sqrt(((P - c3) ** 2).sum(1).mean())) or 1.0
    ext2 = float(np.sqrt(((uv - uv.mean(0)) ** 2).sum(1).mean())) or 1.0
    results = []

    # Rotations are proper: rows (right, up, back); the camera looks along -back.
    def res_persp(z, f):
        R = rodrigues(z[:3]).T
        q = (P - z[3:6]) @ R.T
        depth = -q[:, 2]
        zz = np.where(depth < 1e-6, 1e-6, depth)
        return np.concatenate([cx + f * q[:, 0] / zz - uv[:, 0], cy - f * q[:, 1] / zz - uv[:, 1],
                               10 * np.minimum(depth - 1e-3, 0)])

    def res_ortho(z):
        R = rodrigues(z[:3]).T
        q = (P - c3) @ R.T
        s = math.exp(z[3])
        return np.concatenate([cx + z[4] + q[:, 0] * s - uv[:, 0], cy + z[5] - q[:, 1] * s - uv[:, 1]])

    starts = []
    for d in _dirs():
        for roll in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
            starts.append(_look(d, np.asarray(up_hint, float), roll))
    if init is not None:
        starts.insert(0, np.stack([init.R[0], init.R[1], -init.R[2]]))
    fovs = [5, 9, 14, 20, 28, 40, 55]
    for R0 in starts:
        w0 = _rot_to_vec(R0)
        if ortho:
            s0 = ext2 / ext3
            z0 = np.concatenate([w0, [math.log(s0)], uv.mean(0) - [cx, cy]])
            try:
                r = optimize.least_squares(res_ortho, z0, method="lm" if 2 * n >= 6 else "trf", max_nfev=200)
            except Exception:
                continue
            rms = float(np.sqrt((r.fun ** 2).mean() * 2))
            results.append((rms, ("ortho", r.x)))
        else:
            for fov in fovs:
                f = (H / 2) / math.tan(math.radians(fov) / 2)
                dist = f * ext3 / ext2
                # centre the landmark centroid: shift the camera so c3 lands at the uv centroid
                off = (uv.mean(0) - [cx, cy]) / f * dist
                C0 = c3 + R0[2] * dist - R0[0] * off[0] + R0[1] * off[1]
                z0 = np.concatenate([w0, C0])
                try:
                    r = optimize.least_squares(res_persp, z0, args=(f,), method="lm" if 2 * n >= 6 else "trf", max_nfev=200)
                except Exception:
                    continue
                rms = float(np.sqrt((r.fun[:2 * n] ** 2).mean() * 2))
                results.append((rms, ("persp", r.x, f)))
    results.sort(key=lambda t: t[0])
    out = []
    for rms, sol in results[:40]:
        if sol[0] == "ortho":
            z = sol[1]
            R = rodrigues(z[:3]).T
            s = math.exp(z[3])
            # target: the 3D point on the axis through c3 that projects to the principal point
            tgt = c3 - (z[4] / s) * R[0] + (z[5] / s) * R[1]
            cam = rv.Camera({"mode": "absolute", "projection": "orthographic", "target_mm": tgt.tolist(),
                             "position_mm": (tgt + R[2] * ext3 * 10).tolist(), "up": R[1].tolist(),
                             "mm_per_px": 1 / s, "width_px": W, "height_px": H})
        else:
            z, f = sol[1], sol[2]
            # refine the focal length jointly; a focal outside the physical range is
            # clamped and the pose solved again with it fixed
            def res_f(y):
                return res_persp(y[:6], math.exp(y[6]))
            try:
                r2 = optimize.least_squares(res_f, np.concatenate([z, [math.log(f)]]), max_nfev=300)
                fov = math.degrees(2 * math.atan((H / 2) / math.exp(r2.x[6])))
                if FOV_MIN <= fov <= FOV_MAX:
                    z, f = r2.x[:6], math.exp(r2.x[6])
                else:
                    f = (H / 2) / math.tan(math.radians(float(np.clip(fov, FOV_MIN, FOV_MAX))) / 2)
                    z = optimize.least_squares(res_persp, r2.x[:6], args=(f,), max_nfev=300).x
            except Exception:
                pass
            R = rodrigues(z[:3]).T
            C = z[3:6]
            if ((P - C) @ -R[2]).min() <= 0:
                continue
            fov = float(np.clip(math.degrees(2 * math.atan((H / 2) / f)), FOV_MIN, FOV_MAX))
            depth = float((c3 - C) @ -R[2])
            tgt = C - R[2] * max(depth, 1.0)
            cam = rv.Camera({"mode": "absolute", "projection": "perspective", "position_mm": C.tolist(),
                             "target_mm": tgt.tolist(), "up": R[1].tolist(), "fov_deg": fov,
                             "width_px": W, "height_px": H})
        uvp, _ = cam.project(P)
        rms = float(np.sqrt(((uvp - uv) ** 2).sum(1).mean()))
        out.append((rms, cam))
    out.sort(key=lambda t: t[0])
    return out


# --- scoring -----------------------------------------------------------------------------
class Scorer:
    """Objective of one camera for one scene against the reference at one resolution."""

    def __init__(self, scene, classes, ref_mask, photo_dark, landmarks, span, W, H, weights):
        self.scene, self.classes = scene, classes
        self.W, self.H = W, H
        self.T = resize_mask(ref_mask, W, H)
        self.Tdark = resize_mask(photo_dark, W, H) & self.T
        self.Te = edges(self.T)
        self.Tdist = ndimage.distance_transform_edt(~self.Te)
        self.landmarks = landmarks
        self.span = span
        self.w = weights
        self.use_classes = bool(self.Tdark.sum() > 0.01 * max(1, self.T.sum())) and (np.asarray(classes) == 1).any()

    def evaluate(self, cam, full=False):
        self.scene.set_camera(cam)
        cls = self.scene.classes(self.classes)
        m = cls >= 0
        inter = (m & self.T).sum()
        union = (m | self.T).sum()
        iou = float(inter / max(union, 1))
        me = edges(m)
        mmpp = cam.mm_per_px
        if me.any() and self.Te.any():
            d1 = self.Tdist[me]
            d2 = ndimage.distance_transform_edt(~me)[self.Te]
            ch_px = 0.5 * (float(d1.mean()) + float(d2.mean()))
            p95_px = float(np.percentile(np.concatenate([d1, d2]), 95))
        else:
            ch_px, p95_px = float(max(self.W, self.H)), float(max(self.W, self.H))
        pc = None
        if self.use_classes:
            valid = cls != 2
            md = (cls == 1) & valid
            td = self.Tdark & valid
            pc = float((md & td).sum() / max((md | td).sum(), 1))
        lm_px = lm_mm = None
        if self.landmarks is not None and len(self.landmarks[0]):
            P, uv = self.landmarks
            s = np.array([self.W, self.H], float) / self.full_size
            uvp, depth = cam.project(P)
            err = np.sqrt(((uvp - uv * s) ** 2).sum(1))
            err = np.where(depth > 0, err, 10 * max(self.W, self.H))
            lm_px = float(np.sqrt((err ** 2).mean())) / s[1]
            per = np.array([cam.mm_per_px_at(p) for p in P])
            lm_mm = float(np.sqrt(((err * per) ** 2).mean()))
        J = (1 - iou) + self.w["chamfer"] * ch_px * mmpp / self.span
        if lm_mm is not None:
            J += self.w["landmarks"] * lm_mm / self.span
        if pc is not None:
            J += self.w["classes"] * (1 - pc)
        out = {"objective": J, "iou": iou, "chamfer_mm": ch_px * mmpp, "p95_mm": p95_px * mmpp,
               "part_class_iou": pc, "landmark_rms_px": lm_px, "landmark_rms_mm": lm_mm}
        if full:
            out["mask"] = m
            out["cls"] = cls
        return out


def refine(scorer, param, x0, maxfev, deadline, counter):
    def f(z):
        if time.monotonic() > deadline:
            raise Budget()
        counter[0] += 1
        cam = param.camera(z * param.scale, scorer.W, scorer.H)
        return scorer.evaluate(cam)["objective"]
    z0 = np.asarray(x0, float) / param.scale
    counter[0] += 1
    best = [scorer.evaluate(param.camera(x0, scorer.W, scorer.H))["objective"], z0]

    def wrapped(z):
        v = f(z)
        if v < best[0]:
            best[0], best[1] = v, np.array(z)
        return v
    try:
        optimize.minimize(wrapped, z0, method="Powell",
                          options=dict(maxfev=maxfev, xtol=1e-3, ftol=1e-5, direc=np.eye(len(z0))))
    except Budget:
        pass
    return best[0], best[1] * param.scale


def fit_scene(scene, classes, ref, photo_dark, landmarks_full, span, starts, W, H, weights, deadline, counter,
              coarse_fev=110, fine_fev=220):
    """Coarse Powell from every start, fine Powell from the best. Returns (best, all)."""
    sc_c = COARSE_EDGE / max(W, H)
    Wc, Hc = max(16, round(W * sc_c)), max(16, round(H * sc_c))
    sf = min(1.0, FINE_EDGE / max(W, H))
    Wf, Hf = max(16, round(W * sf)), max(16, round(H * sf))
    lm = None
    if landmarks_full is not None:
        lm = landmarks_full
    coarse = Scorer(scene, classes, ref, photo_dark, lm, span, Wc, Hc, weights)
    fine = Scorer(scene, classes, ref, photo_dark, lm, span, Wf, Hf, weights)
    coarse.full_size = fine.full_size = np.array([W, H], float)
    results = []
    for name, cam in starts:
        param = Param(cam)
        if time.monotonic() > deadline:
            break
        J, x = refine(coarse, param, np.zeros(param.n), coarse_fev, deadline, counter)
        cam_c = param.camera(x, W, H)
        results.append({"start": name, "camera": cam_c, "coarse": J})
    results.sort(key=lambda r: r["coarse"])
    for r in results:
        r["metrics"] = fine.evaluate(r["camera"].scaled(Wf / W))
    best = results[0]
    param = Param(best["camera"])
    if time.monotonic() < deadline:
        J, x = refine(fine, param, np.zeros(param.n), fine_fev, deadline, counter)
        best["camera"] = param.camera(x, W, H)
    best["metrics"] = fine.evaluate(best["camera"].scaled(Wf / W), full=True)
    return best, results, fine


def mirror_starts(cam, center):
    return [("mirror_x", reflect_camera(cam, 0, center)), ("mirror_y", reflect_camera(cam, 1, center)),
            ("mirror_xy", reflect_camera(reflect_camera(cam, 0, center), 1, center))]


def coarse_grid(scene, classes, ref, photo_dark, span, W, H, projection, weights, meshes):
    """Starts without landmarks or a declared camera: 26 directions framed onto the mask."""
    sc = 160 / max(W, H)
    Wg, Hg = max(16, round(W * sc)), max(16, round(H * sc))
    scorer = Scorer(scene, classes, ref, photo_dark, None, span, Wg, Hg, weights)
    scorer.full_size = np.array([W, H], float)
    ys, xs = np.nonzero(ref)
    box = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
    out = []
    for d in _dirs():
        if abs(d[2]) > 0.95:
            up = (0, 1, 0)
        else:
            up = (0, 0, 1)
        cam = rv.Camera(rv.framed_camera(d, up, meshes, W, H, projection, 20.0, 1.0))
        scene.set_camera(cam.scaled(Wg / W))
        m = scene.mask()
        my, mx = np.nonzero(m)
        if not len(mx):
            continue
        mb = np.array([mx.min(), my.min(), mx.max() + 1, my.max() + 1]) / sc
        # zoom about the principal point so the heights match, then shift the centres
        k = (box[3] - box[1]) / max(mb[3] - mb[1], 1)
        c = np.array([cam.cx, cam.cy])
        mc = np.array([(mb[0] + mb[2]) / 2, (mb[1] + mb[3]) / 2])
        o = np.array([(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]) - (c + k * (mc - c))
        mmpp = cam.mm_per_px / k
        shift = (-o[0] * cam.R[0] + o[1] * cam.R[1]) * mmpp
        d0 = cam.to_dict()
        pos = cam.tgt + (cam.pos - cam.tgt) / (1 if cam.ortho else k)
        if cam.ortho:
            d0["mm_per_px"] = mmpp
        d0["target_mm"], d0["position_mm"] = (cam.tgt + shift).tolist(), (pos + shift).tolist()
        c2 = rv.Camera(d0)
        out.append((scorer.evaluate(c2.scaled(Wg / W))["objective"], c2))
    out.sort(key=lambda t: t[0])
    return [c for _, c in out[:3]]


def overlay(rgb, ref, mask, lm_obs, lm_proj, path):
    img = rgb.astype(float) * 0.35 + 255 * 0.65
    img[ref & mask] = (150, 150, 150)
    img[ref & ~mask] = (40, 110, 230)
    img[mask & ~ref] = (235, 40, 70)
    im = Image.fromarray(img.astype(np.uint8))
    d = ImageDraw.Draw(im)
    for (u, v), (pu, pv) in zip(lm_obs, lm_proj):
        d.ellipse([u - 5, v - 5, u + 5, v + 5], outline=(0, 160, 0), width=2)
        d.line([pu - 5, pv - 5, pu + 5, pv + 5], fill=(200, 0, 0), width=2)
        d.line([pu - 5, pv + 5, pu + 5, pv - 5], fill=(200, 0, 0), width=2)
        d.line([u, v, pu, pv], fill=(200, 120, 0), width=1)
    im.save(path)


def triple_agreement(P2_obs, P2_proj, min_area):
    n = len(P2_obs)
    agree = total = 0
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                a = np.cross(P2_obs[j] - P2_obs[i], P2_obs[k] - P2_obs[i])[2]
                if abs(a) < min_area:
                    continue
                b = np.cross(P2_proj[j] - P2_proj[i], P2_proj[k] - P2_proj[i])[2]
                total += 1
                agree += int(np.sign(a) == np.sign(b))
    return agree, total


# --- main ----------------------------------------------------------------------------------
def register(spec):
    t0 = time.monotonic()
    deadline = t0 + float(spec.get("max_seconds", 95))
    counter = [0]
    meshes = rv.load_preview(spec["preview"])
    info = spec.get("info") or {}
    rgb, alpha, ext = load_reference(spec["image"], spec.get("mask"))
    H, W = rgb.shape[:2]
    ref = object_mask(rgb, alpha, spec.get("exclusions") or (), ext)
    if ref.sum() < 50:
        raise ValueError("reference_mask_empty")
    lab = rv.srgb_to_lab(rgb.astype(float))
    photo_dark = (lab[..., 0] < DARK_L) & ref
    classes = []
    for m in meshes:
        inf = info.get(m["feature_id"], {})
        if inf.get("glass") or inf.get("emissive"):
            classes.append(2)
        else:
            L = float(rv.srgb_to_lab(np.array(rv.hex_rgb(inf.get("colour")), float))[0])
            classes.append(1 if L < DARK_L else 0)
    classes = np.array(classes)
    bounds = rv.mesh_bounds(meshes)
    center = (bounds[:3] + bounds[3:]) / 2
    span = float(max(bounds[3:] - bounds[:3]))
    projection = spec.get("mode") or "perspective"
    ortho = projection == "orthographic"
    all_lms = spec.get("landmarks") or []
    # role "check": reported, never fitted (a feature suspected to be misplaced)
    lms = [l for l in all_lms if l.get("role", "fit") != "check"]
    P = np.array([l["point_mm"] for l in lms], float).reshape(-1, 3)
    uv = np.array([l["px"] for l in lms], float).reshape(-1, 2)
    weights = {"chamfer": 2.0, "landmarks": 8.0 if len(lms) else 0.0, "classes": float(spec.get("class_weight", 0.3))}
    min_lm = int(spec.get("min_landmarks", 3 if ortho else 4))
    notes = []
    init = rv.Camera(spec["init_camera"]) if spec.get("init_camera") else None
    if init is None and spec.get("init_relative"):
        # the old bbox-relative camera of the contract, framed exactly like the viewer
        init = rv.Camera(rv.relative_to_absolute(spec["init_relative"], rv.mesh_bounds(meshes), W, H))
    if init is not None and (init.W, init.H) != (W, H):
        d = init.to_dict()
        d["width_px"], d["height_px"] = W, H
        if init.ortho:
            d["mm_per_px"] = init.mm_per_px * init.H / H
        d.pop("principal_px", None)
        init = rv.Camera(d)
    if init is not None and init.ortho != ortho:
        init = None
        notes.append("declared camera has the other projection; ignored as a start")
    scene = rv.Scene(meshes, info)
    starts = []
    lm_solutions = []
    if len(lms) >= min_lm:
        lm_solutions = solve_landmarks(P, uv, W, H, ortho, init=init)
        seen = []
        for rms, cam in lm_solutions:
            if any(np.dot(cam.R[2], s.R[2]) > 0.97 for s in seen):
                continue
            seen.append(cam)
            starts.append((f"landmarks#{len(seen)}", cam))
            if len(seen) >= 2:
                break
    elif lms:
        notes.append(f"{len(lms)} landmarks < {min_lm}: pose from silhouette only")
    if init is not None:
        starts.append(("declared_camera", init))
    if not starts:
        starts = [(f"grid#{i + 1}", c) for i, c in enumerate(
            coarse_grid(scene, classes, ref, photo_dark, span, W, H, projection, weights, meshes))]
    if not starts:
        raise ValueError("no_start_pose")
    base = starts[0][1]
    starts += mirror_starts(base, center)
    lm_full = (P, uv) if len(lms) else None
    best, allres, fine = fit_scene(scene, classes, ref, photo_dark, lm_full, span, starts, W, H, weights,
                                   deadline, counter)
    cam = best["camera"]
    met = best["metrics"]
    # --- mirrored model: the same fit on the model's mirror image ---
    handed = {"method": "mirror_image_registration", "axis": "x", "plane_mm": round(float(center[0]), 4)}
    if spec.get("mirror_test", True) and time.monotonic() < deadline - 5:
        mscene = rv.Scene(meshes, info, mirror=(0, float(center[0])))
        Pm = P.copy()
        if len(Pm):
            Pm[:, 0] = 2 * center[0] - Pm[:, 0]
        mstarts = [("model_pose_reflected", reflect_camera(cam, 0, center)), ("model_pose", cam)]
        if len(lms) >= min_lm:
            sol = solve_landmarks(Pm, uv, W, H, ortho)
            if sol:
                mstarts.insert(0, ("landmarks#1", sol[0][1]))
        mbest, _, _ = fit_scene(mscene, classes, ref, photo_dark, (Pm, uv) if len(lms) else None, span, mstarts,
                                W, H, weights, deadline, counter, coarse_fev=70, fine_fev=140)
        mm = mbest["metrics"]
        margin = mm["objective"] - met["objective"]
        handed.update({"model_objective": round(met["objective"], 4), "mirror_objective": round(mm["objective"], 4),
                       "model_iou": round(met["iou"], 4), "mirror_iou": round(mm["iou"], 4),
                       "model_part_class_iou": None if met["part_class_iou"] is None else round(met["part_class_iou"], 4),
                       "mirror_part_class_iou": None if mm["part_class_iou"] is None else round(mm["part_class_iou"], 4),
                       "model_landmark_rms_px": None if met["landmark_rms_px"] is None else round(met["landmark_rms_px"], 2),
                       "mirror_landmark_rms_px": None if mm["landmark_rms_px"] is None else round(mm["landmark_rms_px"], 2),
                       "margin": round(margin, 4)})
        tol = float(spec.get("handedness_margin", 0.01))
        handed["verdict"] = ("model_fits_better" if margin > tol else
                             "mirror_fits_better" if margin < -tol else "ambiguous")
    else:
        handed["verdict"] = "not_checked"
    # --- landmarks, facing and thresholds ---
    lm_rows = []
    agree = total = 0
    if all_lms:
        Pa = np.array([l["point_mm"] for l in all_lms], float)
        uva = np.array([l["px"] for l in all_lms], float)
        uvp, depth = cam.project(Pa)
        for l, o, pr in zip(all_lms, uva, uvp):
            e = float(np.hypot(*(pr - o)))
            lm_rows.append({"id": l.get("id"), "role": l.get("role", "fit"), "px": [round(float(v), 1) for v in o],
                            "projected_px": [round(float(v), 1) for v in pr], "error_px": round(e, 2),
                            "error_mm": round(e * cam.mm_per_px_at(np.asarray(l["point_mm"], float)), 3)})
        if len(all_lms) >= 3:
            agree, total = triple_agreement(np.c_[uva, np.zeros(len(uva))], np.c_[uvp, np.zeros(len(uvp))], (0.02 * H) ** 2)
    ys, xs = np.nonzero(ref)
    height_mm = float((ys.max() - ys.min() + 1) * cam.mm_per_px)
    fit_rows = [r for r in lm_rows if r["role"] != "check"]
    lm_rms_mm = float(np.sqrt(np.mean([r["error_mm"] ** 2 for r in fit_rows]))) if fit_rows else None
    alts = []
    for r in allres:
        alts.append({"start": r["start"], "objective": round(r["metrics"]["objective"], 4),
                     "iou": round(r["metrics"]["iou"], 4),
                     "part_class_iou": None if r["metrics"]["part_class_iou"] is None else round(r["metrics"]["part_class_iou"], 4),
                     "landmark_rms_px": None if r["metrics"]["landmark_rms_px"] is None else round(r["metrics"]["landmark_rms_px"], 2)})
    other = [a for a in alts if a["start"] != best["start"]]
    fmargin = (min(a["objective"] for a in other) - met["objective"]) if other else None
    source_type = spec.get("source_type") or ("orthographic_render" if ortho else "perspective_photo")
    iou_min = float(spec.get("iou_min", {"orthographic_turntable": 0.97, "orthographic_render": 0.97,
                                          "datasheet_render": 0.97, "perspective_photo": 0.92,
                                          "closeup": 0.85}.get(source_type, 0.92)))
    lm_pct_max = float(spec.get("landmark_rms_pct_max", 1.0))
    reasons = []
    if len(lms) < min_lm:
        reasons.append(f"needs >= {min_lm} landmarks for a registration ({len(lms)} given)")
    if lm_rms_mm is not None and lm_rms_mm > lm_pct_max / 100 * height_mm:
        reasons.append(f"landmark RMS {lm_rms_mm:.2f} mm > {lm_pct_max}% of height ({lm_pct_max / 100 * height_mm:.2f} mm)")
    if met["iou"] < iou_min:
        reasons.append(f"silhouette IoU {met['iou']:.3f} < {iou_min} ({source_type})")
    if handed["verdict"] == "mirror_fits_better":
        reasons.append("the model's mirror image explains the image better (handedness)")
    if total and agree < total:
        reasons.append(f"landmark orientation disagrees in {total - agree} of {total} triples (handedness)")
    if not len(lms) and fmargin is not None and fmargin < 0.01:
        reasons.append("facing ambiguous: a mirrored start fits almost as well; add asymmetric landmarks")
    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    full_mask = resize_mask(met["mask"], W, H)
    uv_all = np.array([l["px"] for l in all_lms], float).reshape(-1, 2)
    overlay(rgb, ref, full_mask, uv_all, cam.project(np.array([l["point_mm"] for l in all_lms], float).reshape(-1, 3))[0]
            if all_lms else [], out_dir / "registration.png")
    return {
        "camera": cam.to_dict(),
        "registered": not reasons,
        "reasons": reasons,
        "source_type": source_type,
        "residuals": {
            "silhouette_iou": round(met["iou"], 4), "chamfer_mm": round(met["chamfer_mm"], 3),
            "p95_mm": round(met["p95_mm"], 3),
            "part_class_iou": None if met["part_class_iou"] is None else round(met["part_class_iou"], 4),
            "landmark_rms_px": None if met["landmark_rms_px"] is None else round(met["landmark_rms_px"], 2),
            "landmark_rms_mm": None if lm_rms_mm is None else round(lm_rms_mm, 3),
            "landmark_rms_pct_height": None if lm_rms_mm is None else round(100 * lm_rms_mm / height_mm, 3),
            "height_mm": round(height_mm, 2), "mm_per_px": round(cam.mm_per_px, 5),
            "objective": round(met["objective"], 4)},
        "thresholds": {"silhouette_iou_min": iou_min, "landmark_rms_pct_max": lm_pct_max, "min_landmarks": min_lm},
        "landmarks": lm_rows,
        "facing": {"best_start": best["start"], "alternatives": alts,
                   "margin": None if fmargin is None else round(fmargin, 4),
                   "triples_agree": agree, "triples_checked": total},
        "handedness": handed,
        "image_px": [W, H],
        "mask_px": int(ref.sum()),
        "notes": notes,
        "overlay": str(out_dir / "registration.png"),
        "evaluations": counter[0],
        "seconds": round(time.monotonic() - t0, 2),
    }


if __name__ == "__main__":
    action, path = sys.argv[1], sys.argv[2]
    spec = json.loads(Path(path).read_text())
    if action != "register":
        raise SystemExit("unknown action " + action)
    sys.stdout.write(json.dumps(register(spec)))
