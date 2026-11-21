"""cad_fit_parameters numerics: silhouettes of preview meshes under absolute cameras,
a cheap 2D stand-in and a bounded Powell search.

Tier 1 (stand-in): every registered view gets a signed distance field (SDF) of the
real model silhouette at the start values x0 and at x0 + delta_i e_i for each declared
variable (each from a real LLcad candidate build). Between them the SDF is linear in
the variables, phi(x) = phi0 + sum_i (x_i - x0_i)/delta_i (phi_i - phi0), and the
predicted silhouette is phi(x) < 0. Dimension changes of profile, extrude, revolve and
primitive features move the outline linearly, so the stand-in is exact at the samples
and accurate nearby; an evaluation costs a few milliseconds, so Powell can afford
hundreds. Landmark anchors (feature bound centres) are linearised the same way.
Tier 3 (confirmation) is a real candidate at the optimum, measured with `measure`.

Actions: standin SPEC | measure SPEC (JSON file) -> JSON on stdout."""
import json, math, struct, sys, time
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageOps
from scipy import ndimage, optimize

Image.MAX_IMAGE_PIXELS = 60_000_000
MAX_EDGE = 480   # optimisation resolution (long edge, px)


# --- preview meshes -----------------------------------------------------------
def load_preview(path):
    """Triangles (N,3,3) in mm from preview.bin (MFPV) or preview.json."""
    data = Path(path).read_bytes()
    tris = []
    if data[:4] == b'MFPV':
        length, pad = struct.unpack('<II', data[8:16])
        header = json.loads(data[16:16 + length])
        base = 16 + length + pad
        for m in header['meshes']:
            n, t = int(m['vertex_count']), int(m['triangle_count'])
            pos = np.frombuffer(data, '<f4', n * 3, base + int(m['positions_offset'])).reshape(n, 3).astype(float)
            idx = np.frombuffer(data, '<u4', t * 3, base + int(m['indices_offset'])).reshape(t, 3)
            pos = pos + np.asarray(m.get('origin', [0, 0, 0]), float)
            tris.append(pos[idx])
    else:
        header = json.loads(data)
        for m in header['meshes']:
            v = np.asarray(m['vertices'], float)
            tris.append(v[np.asarray(m['triangles'], int)])
    if not tris:
        raise ValueError('preview_without_meshes')
    return np.concatenate(tris)


# --- cameras ------------------------------------------------------------------
class Cam:
    """The absolute camera contract (reference-review schema / viewer / VTK)."""
    def __init__(self, c, scale=1.0):
        self.W = max(8, round(c['width_px'] * scale)); self.H = max(8, round(c['height_px'] * scale))
        self.s = self.W / c['width_px']
        pos = np.asarray(c['position_mm'], float); tgt = np.asarray(c['target_mm'], float)
        up = np.asarray(c.get('up') or [0, 0, 1], float)
        f = tgt - pos; f /= np.linalg.norm(f)
        r = np.cross(f, up); r /= np.linalg.norm(r)
        u = np.cross(r, f)
        self.pos, self.R = pos, np.stack([r, u, f])
        self.ortho = c.get('projection') == 'orthographic'
        pp = c.get('principal_px') or [c['width_px'] / 2, c['height_px'] / 2]
        self.cx, self.cy = pp[0] * self.s, pp[1] * self.s
        if self.ortho:
            self.k = self.s / float(c['mm_per_px'])           # px per mm
            self.mm_per_px = float(c['mm_per_px']) / self.s
        else:
            self.k = (c['height_px'] / 2) / math.tan(math.radians(c['fov_deg']) / 2) * self.s
            depth = float(np.linalg.norm(tgt - pos))
            self.mm_per_px = depth / self.k                   # at the target distance

    def project(self, P):
        q = (np.asarray(P, float) - self.pos) @ self.R.T
        if self.ortho:
            return np.stack([self.cx + q[..., 0] * self.k, self.cy - q[..., 1] * self.k], -1), q[..., 2]
        z = np.maximum(q[..., 2], 1e-9)
        return np.stack([self.cx + q[..., 0] / z * self.k, self.cy - q[..., 1] / z * self.k], -1), q[..., 2]


def render_mask(tris, cam):
    uv, z = cam.project(tris.reshape(-1, 3))
    uv = uv.reshape(-1, 3, 2); z = z.reshape(-1, 3)
    keep = (z > 1e-6).all(1) if not cam.ortho else np.ones(len(uv), bool)
    uv = uv[keep]
    lo, hi = uv.min(1), uv.max(1)
    vis = (hi[:, 0] >= 0) & (lo[:, 0] <= cam.W) & (hi[:, 1] >= 0) & (lo[:, 1] <= cam.H)
    img = Image.new('1', (cam.W, cam.H), 0)
    d = ImageDraw.Draw(img)
    for t in uv[vis]:
        d.polygon([(t[0, 0], t[0, 1]), (t[1, 0], t[1, 1]), (t[2, 0], t[2, 1])], fill=1)
    return np.asarray(img, bool)


def reference_mask(path, cam, exclusions=()):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        alpha = im.convert('RGBA').getchannel('A') if 'A' in im.getbands() else None
        rgb = im.convert('RGB')
    rgb = rgb.resize((cam.W, cam.H), Image.Resampling.LANCZOS)
    if alpha is not None and np.asarray(alpha).min() < 250:
        m = np.asarray(alpha.resize((cam.W, cam.H), Image.Resampling.BILINEAR)) > 127
    else:
        a = np.asarray(rgb).astype(float)
        corners = np.concatenate([a[:8, :8].reshape(-1, 3), a[-8:, :8].reshape(-1, 3),
                                  a[:8, -8:].reshape(-1, 3), a[-8:, -8:].reshape(-1, 3)])
        m = np.max(np.abs(a - np.median(corners, 0)), 2) > 28
    m = ndimage.binary_fill_holes(m)
    lab, n = ndimage.label(m)
    if n:
        sizes = np.bincount(lab.ravel()); sizes[0] = 0
        m = sizes[lab] >= 0.005 * sizes.max()
    for b in exclusions:   # declared exclusion boxes (normalized), e.g. badges
        m[int(b[1] * cam.H):int(math.ceil(b[3] * cam.H)), int(b[0] * cam.W):int(math.ceil(b[2] * cam.W))] = False
    return m


def sdf(mask):
    if not mask.any():
        return np.full(mask.shape, 1e4)
    return ndimage.distance_transform_edt(~mask) - ndimage.distance_transform_edt(mask)


def edges(m):
    return m ^ ndimage.binary_erosion(m)


class Scorer:
    def __init__(self, ref, cam):
        self.ref, self.cam = ref, cam
        self.re = edges(ref)
        self.rdist = ndimage.distance_transform_edt(~self.re)
        self.h = max(1, np.ptp(np.where(ref.any(1))[0]) if ref.any() else 1)

    def __call__(self, mask):
        """mask: bool, or float coverage 0..1 (the stand-in's anti-aliased edge,
        which keeps the objective continuous below one pixel)."""
        if mask.dtype == bool:
            inter = (mask & self.ref).sum(); union = (mask | self.ref).sum()
        else:
            inter = np.minimum(mask, self.ref).sum(); union = np.maximum(mask, self.ref).sum()
            mask = mask >= 0.5
        iou = inter / max(union, 1)
        me = edges(mask)
        if not me.any() or not self.re.any():
            return iou, 1e3
        md = ndimage.distance_transform_edt(~me)
        chamfer_px = 0.5 * (self.rdist[me].mean() + md[self.re].mean())
        return float(iou), float(chamfer_px * self.cam.mm_per_px)


def view_setup(spec, scale_to):
    views = []
    for v in spec['views']:
        full = v['camera']
        scale = min(1.0, scale_to / max(full['width_px'], full['height_px'])) if scale_to else 1.0
        cam = Cam(full, scale)
        views.append({'id': v['reference_id'], 'cam': cam,
                      'score': Scorer(reference_mask(v['source'], cam, v.get('exclusion_boxes') or []), cam),
                      'landmarks': [l for l in spec.get('objective', {}).get('landmarks', []) if l['reference_id'] == v['reference_id']]})
    return views


def objective_parts(view, mask, anchors, weights):
    iou, chamfer_mm = view['score'](mask)
    lm = []
    for l in view['landmarks']:
        p = anchors.get(l['feature_id'])
        if p is None:
            continue
        uv, _ = view['cam'].project(np.asarray(p, float)[None])
        px = np.asarray(l['px'], float) * view['cam'].s
        lm.append(float(np.linalg.norm(uv[0] - px) * view['cam'].mm_per_px))
    landmark_mm = float(np.mean(lm)) if lm else None
    total = (weights['silhouette'] * (1 - iou) + weights['chamfer'] * chamfer_mm
             + (weights['landmark'] * landmark_mm if landmark_mm is not None else 0.0))
    return total, {'reference_id': view['id'], 'iou': round(iou, 5), 'chamfer_mm': round(chamfer_mm, 4),
                   'landmark_mm': None if landmark_mm is None else round(landmark_mm, 4)}


def weights_of(spec):
    o = spec.get('objective') or {}
    return {'silhouette': float(o.get('silhouette_weight', 1.0)), 'chamfer': float(o.get('chamfer_weight', 0.05)),
            'landmark': float(o.get('landmark_weight', 0.05))}


def standin(spec):
    t0 = time.perf_counter()
    weights = weights_of(spec)
    variables = spec['variables']
    n = len(variables)
    x0 = np.array([v['x0'] for v in variables], float)
    lo = np.array([v['min'] for v in variables], float); hi = np.array([v['max'] for v in variables], float)
    samples = spec['samples']              # samples[0] = x0, samples[1+i] = x0 + delta_i e_i
    deltas = np.array([samples[1 + i]['x'][i] - x0[i] for i in range(n)], float)
    if np.any(np.abs(deltas) < 1e-12):
        raise ValueError('zero_fd_step')
    views = view_setup(spec, spec.get('resolution', MAX_EDGE))
    trimeshes = [load_preview(s['preview']) for s in samples]
    for v in views:
        phis = [sdf(render_mask(t, v['cam'])) for t in trimeshes]
        v['phi0'] = phis[0]
        v['J'] = np.stack([(phis[1 + i] - phis[0]) / deltas[i] for i in range(n)], 0)
    anchors0 = samples[0].get('anchors') or {}
    anchor_J = {}
    for fid, p0 in anchors0.items():
        cols = []
        for i in range(n):
            pi = (samples[1 + i].get('anchors') or {}).get(fid)
            cols.append((np.asarray(pi, float) - np.asarray(p0, float)) / deltas[i] if pi is not None else np.zeros(3))
        anchor_J[fid] = (np.asarray(p0, float), np.stack(cols, 1))

    def predicted(x):
        dx = x - x0
        anchors = {fid: p0 + J @ dx for fid, (p0, J) in anchor_J.items()}
        return anchors

    evals = [0]

    def f(x):
        evals[0] += 1
        x = np.clip(x, lo, hi)
        anchors = predicted(x)
        total = 0.0
        for v in views:
            phi = v['phi0'] + np.tensordot(x - x0, v['J'], 1)
            total += objective_parts(v, np.clip(0.5 - phi, 0.0, 1.0), anchors, weights)[0]
        return total / len(views)

    def report(x):
        anchors = predicted(x)
        parts = []
        tot = 0.0
        for v in views:
            phi = v['phi0'] + np.tensordot(x - x0, v['J'], 1)
            t, d = objective_parts(v, np.clip(0.5 - phi, 0.0, 1.0), anchors, weights); tot += t; parts.append(d)
        return {'objective': round(tot / len(views), 6), 'views': parts}

    before = report(x0)
    budget = int(spec.get('max_evals', 400))
    best_x, best_f = x0.copy(), f(x0)
    # Coarse start set: x0, the bound centre and a small deterministic design.
    starts = [x0, (lo + hi) / 2]
    rng = np.random.default_rng(12345)
    for _ in range(4 if n <= 4 else 2):
        starts.append(lo + rng.random(n) * (hi - lo))
    scored = sorted(((f(s), i) for i, s in enumerate(starts)))
    for _, i in scored[:2]:
        if evals[0] >= budget:
            break
        res = optimize.minimize(f, starts[i], method='Powell', bounds=list(zip(lo, hi)),
                                options={'maxfev': max(20, (budget - evals[0])), 'xtol': 1e-4 * float(np.max(hi - lo)), 'ftol': 1e-6})
        x = np.clip(res.x, lo, hi)
        fx = f(x)
        if fx < best_f:
            best_x, best_f = x, fx
    after = report(best_x)
    # How far the optimum lies outside the sampled range, in fd steps (trust).
    extrapolation = float(np.max(np.abs((best_x - x0) / deltas))) if n else 0.0
    return {'x_best': [float(v) for v in best_x], 'before': before, 'standin_after': after,
            'evals': evals[0], 'seconds': round(time.perf_counter() - t0, 3),
            'extrapolation_steps': round(extrapolation, 3),
            'resolution_px': [[v['cam'].W, v['cam'].H] for v in views],
            'method': 'powell_on_sdf_linearised_standin'}


def measure(spec):
    weights = weights_of(spec)
    views = view_setup(spec, spec.get('resolution', MAX_EDGE))
    tris = load_preview(spec['preview'])
    anchors = spec.get('anchors') or {}
    parts = []; tot = 0.0
    for v in views:
        t, d = objective_parts(v, render_mask(tris, v['cam']), anchors, weights); tot += t; parts.append(d)
    return {'objective': round(tot / len(views), 6), 'views': parts}


if __name__ == '__main__':
    action = sys.argv[1]
    spec = json.loads(Path(sys.argv[2]).read_text())
    fn = {'standin': standin, 'measure': measure}.get(action)
    if not fn:
        raise ValueError('unknown_fit_action')
    print(json.dumps(fn(spec), allow_nan=False))
