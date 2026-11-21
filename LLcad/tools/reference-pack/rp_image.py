"""Image measurements: object masks, subpixel silhouettes, width-per-height tables,
and region colours in CIELAB (lit pixels only, CIEDE2000 spread)."""
import csv, json
from pathlib import Path
import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = 80_000_000


def load(path):
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).copy()


def object_mask(im, threshold=None):
    """Foreground: alpha if the image has a real one, else distance to the border
    background (median of the outer 6 px). Largest component, holes filled."""
    if 'A' in im.getbands():
        a = np.asarray(im.getchannel('A'))
        if a.min() < 250:
            m = a > 127
            return _largest(m), 'alpha'
    rgb = np.asarray(im.convert('RGB')).astype(np.int16)
    border = np.concatenate([rgb[:6].reshape(-1, 3), rgb[-6:].reshape(-1, 3), rgb[:, :6].reshape(-1, 3), rgb[:, -6:].reshape(-1, 3)])
    bg = np.median(border, 0)
    m = np.abs(rgb - bg).max(-1) > (threshold or 40)
    return _largest(ndimage.binary_opening(m, iterations=2)), 'border_background'


def _largest(m):
    lab, n = ndimage.label(m)
    if not n:
        return m
    sizes = np.bincount(lab.ravel()); sizes[0] = 0
    return ndimage.binary_fill_holes(lab == int(np.argmax(sizes)))


def subpixel_profile(gray, mask, height_mm, thr=200.0, step_mm=0.5, axis_hint=None):
    """Width per height of a dark object on a light background:
    rows from the bottom, subpixel left/right edges by linear interpolation of the
    grey level across the threshold. Scale = object height px / height_mm."""
    g = gray.astype(float)
    rows = np.where(mask.any(1))[0]
    if not len(rows):
        raise ValueError('empty_mask')
    y0, y1 = int(rows[0]), int(rows[-1])
    cx = int(np.median(np.where(mask[(y0 + y1) // 2])[0])) if axis_hint is None else int(axis_hint)
    band = g[:, max(cx - 3, 0):cx + 4].mean(1)

    def cross(a, b, i_out):
        return i_out + (a - thr) / (a - b) if a != b else float(i_out)
    top = cross(band[y0 - 1], band[y0], y0 - 1) if y0 > 0 else float(y0)
    bot = y1 + (band[y1] - thr) / (band[y1] - band[y1 + 1]) if y1 + 1 < len(band) and band[y1] != band[y1 + 1] else float(y1)
    # Top/bottom edges are found on a band at the axis; a light background above the
    # object crosses the threshold between y0-1 and y0.
    h_px = bot - top
    s = h_px / float(height_mm)
    prof = []
    W = g.shape[1]
    for z in np.arange(step_mm, height_mm, step_mm):
        y = int(round(bot - z * s))
        if y < 0 or y >= g.shape[0]:
            continue
        c = np.where(mask[y])[0]
        if len(c) < 2:
            continue
        l, r = int(c[0]), int(c[-1])
        row = g[y]
        le = l - 1 + (row[l - 1] - thr) / (row[l - 1] - row[l]) if l > 0 and row[l - 1] != row[l] else float(l)
        re_ = r + (row[r] - thr) / (row[r] - row[r + 1]) if r + 1 < W and row[r] != row[r + 1] else float(r)
        prof.append((round(float(z), 3), round(float((re_ - le) / s), 4), round(float((le + re_) / 2), 2)))
    axis = np.array([p[2] for p in prof]) if prof else np.array([cx])
    return {'height_px': round(float(h_px), 3), 'px_per_mm': round(float(s), 5), 'top_px': round(float(top), 3),
            'bottom_px': round(float(bot), 3), 'axis_x_px_mean': round(float(axis.mean()), 3),
            'axis_x_px_std': round(float(axis.std()), 3), 'max_width_mm': round(max((p[1] for p in prof), default=0.0), 4)}, prof


def write_profile_csv(path, prof):
    with open(path, 'w', newline='') as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(['z_mm', 'width_mm', 'center_x_px'])
        w.writerows(prof)


# --- colour ---------------------------------------------------------------
def srgb2lab(rgb):
    c = np.asarray(rgb, float) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
    xyz = c @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def lab2srgb(lab):
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16) / 116; fx = fy + a / 500; fz = fy - b / 200
    def inv(t):
        return np.where(t ** 3 > 0.008856, t ** 3, (t - 16 / 116) / 7.787)
    xyz = np.stack([inv(fx), inv(fy), inv(fz)], -1) * np.array([0.95047, 1.0, 1.08883])
    M = np.linalg.inv(np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]]))
    c = np.clip(xyz @ M.T, 0, 1)
    c = np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)
    return np.clip(np.round(c * 255), 0, 255).astype(int)


def de2000(l1, l2):
    L1, a1, b1 = l1[..., 0], l1[..., 1], l1[..., 2]
    L2, a2, b2 = l2[..., 0], l2[..., 1], l2[..., 2]
    C1 = np.hypot(a1, b1); C2 = np.hypot(a2, b2); Cb = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
    a1p = (1 + G) * a1; a2p = (1 + G) * a2
    C1p = np.hypot(a1p, b1); C2p = np.hypot(a2p, b2)
    h1 = np.degrees(np.arctan2(b1, a1p)) % 360; h2 = np.degrees(np.arctan2(b2, a2p)) % 360
    dL = L2 - L1; dC = C2p - C1p; dh = h2 - h1
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dH = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lb = (L1 + L2) / 2; Cbp = (C1p + C2p) / 2
    hb = np.where(np.abs(h1 - h2) > 180, (h1 + h2 + 360) / 2, (h1 + h2) / 2)
    T = (1 - 0.17 * np.cos(np.radians(hb - 30)) + 0.24 * np.cos(np.radians(2 * hb)) + 0.32 * np.cos(np.radians(3 * hb + 6))
         - 0.2 * np.cos(np.radians(4 * hb - 63)))
    SL = 1 + 0.015 * (Lb - 50) ** 2 / np.sqrt(20 + (Lb - 50) ** 2); SC = 1 + 0.045 * Cbp; SH = 1 + 0.015 * Cbp * T
    dt = 30 * np.exp(-((hb - 275) / 25) ** 2); RC = 2 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7))
    RT = -np.sin(np.radians(2 * dt)) * RC
    return np.sqrt((dL / SL) ** 2 + (dC / SC) ** 2 + (dH / SH) ** 2 + RT * (dC / SC) * (dH / SH))


def hexof(rgb):
    return '#%02x%02x%02x' % tuple(int(v) for v in rgb)


def region_colours(im, mask, k=5, min_fraction=0.01, erode=2, max_pixels=200_000, seed=7):
    """Colour regions of the object: k-means in Lab on eroded object pixels, then per
    cluster the median of its lit pixels (upper 75 % in L*, no specular top 2 %),
    with hex, CIEDE2000 spread (p90 of the lit pixels to the median) and area share."""
    rgb = np.asarray(im.convert('RGB'))
    m = ndimage.binary_erosion(mask, iterations=erode) if erode else mask
    px = rgb[m]
    if len(px) < 50:
        return []
    rng = np.random.default_rng(seed)
    if len(px) > max_pixels:
        px = px[rng.choice(len(px), max_pixels, replace=False)]
    lab = srgb2lab(px)
    k = max(1, min(k, len(lab) // 50))
    centres = lab[rng.choice(len(lab), k, replace=False)]
    for _ in range(25):
        d = ((lab[:, None, :] - centres[None]) ** 2).sum(-1)
        lbl = d.argmin(1)
        new = np.array([lab[lbl == i].mean(0) if (lbl == i).any() else centres[i] for i in range(k)])
        if np.allclose(new, centres, atol=0.05):
            break
        centres = new
    out = []
    for i in range(k):
        sel = lab[lbl == i]
        frac = len(sel) / len(lab)
        if frac < min_fraction or len(sel) < 30:
            continue
        L = sel[:, 0]
        lit = sel[(L >= np.percentile(L, 25)) & (L <= np.percentile(L, 98))]
        if len(lit) < 10:
            lit = sel
        med = np.median(lit, 0)
        spread = float(np.percentile(de2000(lit, np.broadcast_to(med, lit.shape)), 90))
        out.append({'hex': hexof(lab2srgb(med[None])[0]), 'lab': [round(float(v), 2) for v in med],
                    'delta_e00_p90': round(spread, 2), 'area_fraction': round(frac, 4), 'lit_pixels': int(len(lit))})
    return sorted(out, key=lambda c: -c['area_fraction'])


def save_mask(mask, path):
    Image.fromarray((mask * 255).astype(np.uint8)).save(path)


def measure_still(path, out_dir, ref_id, height_mm=None):
    """Mask, optional scaled width table and colours of one still image."""
    im = load(path)
    mask, method = object_mask(im)
    out_dir = Path(out_dir)
    mpath = out_dir / f'{ref_id}-mask.png'
    save_mask(mask, mpath)
    rows = np.where(mask.any(1))[0]; cols = np.where(mask.any(0))[0]
    info = {'width': im.width, 'height': im.height, 'mask': str(mpath), 'mask_method': method,
            'object_bbox_px': [int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1] if len(rows) else None}
    profile = None
    if height_mm and len(rows):
        # Alpha renders: the grey level is the alpha itself (object dark = opaque).
        gray = 255 - np.asarray(im.getchannel('A')).astype(float) if method == 'alpha' else np.asarray(im.convert('L')).astype(float)
        thr = 127.0 if method == 'alpha' else 200.0
        stats, prof = subpixel_profile(gray, mask, height_mm, thr=thr)
        csv_path = out_dir / f'{ref_id}-width-per-height.csv'
        write_profile_csv(csv_path, prof)
        profile = {'csv': str(csv_path), 'rows': len(prof), **stats}
    return info, profile, region_colours(im, mask)
