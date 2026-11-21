"""Judge inputs and deterministic probes for the gates_judge reference review.

Actions (JSON in on argv[2] as a file path, JSON out on stdout):
  prepare  registered crop triples (source | render | residual) per ledger item with
           set-of-mark labels, plus the projected region of each item's features
  pair     pairwise composites (source | A | B) for order-swapped comparisons
  probe    deterministic probes inside judge regions (silhouette residual share,
           CIEDE2000 between source and albedo render)

Images are untrusted data; nothing here executes text found in them."""
import json, sys, warnings
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = 40_000_000
warnings.simplefilter('error', Image.DecompressionBombWarning)
PANEL = 640          # longest edge of one panel in a composite
MAX_MARKS = 20


def load_rgb(path):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert('RGBA')
        bg = Image.new('RGBA', im.size, 'white')
        bg.alpha_composite(im)
        return bg.convert('RGB')


def object_mask(img):
    """Foreground by distance to the corner background (same rule as images.py)."""
    a = np.asarray(img).astype(float)
    corners = np.concatenate([a[:8, :8].reshape(-1, 3), a[-8:, :8].reshape(-1, 3),
                              a[:8, -8:].reshape(-1, 3), a[-8:, -8:].reshape(-1, 3)])
    bg = np.median(corners, axis=0)
    m = np.max(np.abs(a - bg), axis=2) > 28
    return ndimage.binary_fill_holes(m)


def ids_decode(path, ids_map):
    """Per-pixel output index (-1 = background) from the viewer's ids channel."""
    a = np.asarray(load_rgb(path)).astype(int)
    out = np.full(a.shape[:2], -1, int)
    for entry in ids_map:
        r, g, b = entry['rgb']
        hit = (np.abs(a[..., 0] - r) <= 2) & (np.abs(a[..., 1] - g) <= 2) & (np.abs(a[..., 2] - b) <= 2)
        out[hit] = entry['index']
    return out


# --- colour ---------------------------------------------------------------
def srgb2lab(rgb):
    c = np.asarray(rgb, float) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = c @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


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
    T = (1 - 0.17 * np.cos(np.radians(hb - 30)) + 0.24 * np.cos(np.radians(2 * hb))
         + 0.32 * np.cos(np.radians(3 * hb + 6)) - 0.2 * np.cos(np.radians(4 * hb - 63)))
    SL = 1 + 0.015 * (Lb - 50) ** 2 / np.sqrt(20 + (Lb - 50) ** 2); SC = 1 + 0.045 * Cbp; SH = 1 + 0.015 * Cbp * T
    dt = 30 * np.exp(-((hb - 275) / 25) ** 2); RC = 2 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7))
    RT = -np.sin(np.radians(2 * dt)) * RC
    return np.sqrt((dL / SL) ** 2 + (dC / SC) ** 2 + (dH / SH) ** 2 + RT * (dC / SC) * (dH / SH))


def lit_median_lab(rgb, mask):
    """Median Lab of the lit pixels (upper 75 % in L*) inside a mask; None if too few."""
    px = np.asarray(rgb)[mask]
    if len(px) < 30:
        return None
    lab = srgb2lab(px)
    sel = lab[:, 0] >= np.percentile(lab[:, 0], 25)
    return np.median(lab[sel], axis=0)


# --- geometry helpers -----------------------------------------------------
def pad_box(box, pad, W, H, min_frac=0.10):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    w2 = max(w * (1 + 2 * pad), W * min_frac); h2 = max(h * (1 + 2 * pad), H * min_frac)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    x0, x1 = max(0, cx - w2 / 2), min(W, cx + w2 / 2)
    y0, y1 = max(0, cy - h2 / 2), min(H, cy + h2 / 2)
    return [int(np.floor(x0)), int(np.floor(y0)), int(np.ceil(x1)), int(np.ceil(y1))]


def box_of(mask):
    ys, xs = np.where(mask)
    if not len(xs):
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def norm_in(box, crop):
    """Pixel box -> normalized box inside the crop."""
    x0, y0, x1, y1 = crop
    w, h = max(x1 - x0, 1), max(y1 - y0, 1)
    b = [(box[0] - x0) / w, (box[1] - y0) / h, (box[2] - x0) / w, (box[3] - y0) / h]
    return [float(min(1, max(0, v))) for v in b]


def font(size):
    for name in ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def panel(img, title, scale):
    w, h = img.size
    im = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    head = 26
    out = Image.new('RGB', (im.width, im.height + head), (32, 32, 32))
    out.paste(im, (0, head))
    ImageDraw.Draw(out).text((6, 4), title, fill=(255, 255, 255), font=font(15))
    return out


def composite(panels, path):
    gap = 8
    W = sum(p.width for p in panels) + gap * (len(panels) - 1)
    H = max(p.height for p in panels)
    out = Image.new('RGB', (W, H), (255, 255, 255))
    x = 0
    for p in panels:
        out.paste(p, (x, 0)); x += p.width + gap
    out.save(path, 'PNG')
    return {'path': str(path), 'width': out.width, 'height': out.height}


def residual_image(src_m, ren_m):
    img = np.full(src_m.shape + (3,), 255, np.uint8)
    img[src_m & ren_m] = (150, 150, 150)
    img[ren_m & ~src_m] = (220, 30, 30)    # render only (too much material)
    img[src_m & ~ren_m] = (30, 90, 230)    # source only (missing material)
    return Image.fromarray(img)


def draw_marks(img, ids, crop, ids_map, start=1):
    """Set-of-mark labels: number the visible outputs inside the crop at their centroid."""
    x0, y0, x1, y1 = crop
    sub = ids[y0:y1, x0:x1]
    counts = np.bincount((sub[sub >= 0]).ravel(), minlength=len(ids_map) + 1) if (sub >= 0).any() else np.zeros(1, int)
    order = [i for i in np.argsort(-counts) if counts[i] >= 20][:MAX_MARKS]
    d = ImageDraw.Draw(img)
    f = font(max(12, int(min(img.size) / 30)))
    marks = []
    for n, index in enumerate(order, start):
        ys, xs = np.where(sub == index)
        # A pixel of the part closest to its centroid, so the label sits on the part.
        cx, cy = xs.mean(), ys.mean()
        k = int(np.argmin((xs - cx) ** 2 + (ys - cy) ** 2))
        px, py = int(xs[k]), int(ys[k])
        r = max(8, int(min(img.size) / 45))
        d.ellipse([px - r, py - r, px + r, py + r], fill=(255, 230, 0), outline=(0, 0, 0))
        d.text((px, py), str(n), fill=(0, 0, 0), font=f, anchor='mm')
        entry = next((e for e in ids_map if e['index'] == int(index)), None)
        marks.append({'n': n, 'feature_id': entry['feature_id'] if entry else None,
                      'visible_px': int(counts[index])})
    return marks


# --- actions --------------------------------------------------------------
def prepare(spec):
    out_dir = Path(spec['out_dir']); out_dir.mkdir(parents=True, exist_ok=True)
    src = load_rgb(spec['source']); ren = load_rgb(spec['render'])
    if src.size != ren.size:
        ren = ren.resize(src.size, Image.Resampling.LANCZOS)
    W, H = src.size
    ids = ids_decode(spec['ids'], spec.get('ids_map') or []) if spec.get('ids') else None
    if ids is not None and ids.shape != (H, W):
        ids = np.asarray(Image.fromarray(ids.astype(np.int32)).resize((W, H), Image.Resampling.NEAREST))
    src_m = object_mask(src)
    ren_m = (ids >= 0) if ids is not None else object_mask(ren)
    registered = bool(spec.get('registered'))
    resid = residual_image(src_m, ren_m) if registered else None
    obj_box = box_of(src_m) or [0, 0, W, H]
    object_height = max(obj_box[3] - obj_box[1], 1)
    fid_index = {e['feature_id']: e['index'] for e in (spec.get('ids_map') or [])}
    items = []
    for n, item in enumerate(spec['items']):
        proj = None
        if ids is not None and item.get('feature_ids'):
            wanted = [fid_index[f] for f in item['feature_ids'] if f in fid_index]
            m = np.isin(ids, wanted) if wanted else np.zeros_like(src_m)
            proj = box_of(m)
        if item.get('region_box'):
            b = item['region_box']
            crop = pad_box([b[0] * W, b[1] * H, b[2] * W, b[3] * H], 0.15, W, H)
            crop_source = 'declared_region'
        elif proj:
            crop = pad_box(proj, 0.25, W, H)
            crop_source = 'feature_projection'
        else:
            crop = [0, 0, W, H]
            crop_source = 'full_view'
        cw, ch = crop[2] - crop[0], crop[3] - crop[1]
        scale = min(PANEL / max(cw, ch), 4.0)
        r_img = ren.crop(crop)
        marks = draw_marks(r_img, ids, crop, spec.get('ids_map') or []) if ids is not None else []
        panels = [panel(src.crop(crop), 'SOURCE ' + spec['view_id'], scale),
                  panel(r_img, 'RENDER (numbered parts)', scale)]
        if resid is not None:
            panels.append(panel(resid.crop(crop), 'RESIDUAL red=render only blue=source only', scale))
        comp = composite(panels, out_dir / f"{spec['view_id']}-{n:02d}.png")
        items.append({'id': item['id'], 'crop': crop, 'crop_source': crop_source,
                      'projected_box': norm_in(proj, crop) if proj else None,
                      'marks': marks, 'composite': comp,
                      'residual_share': float(((src_m ^ ren_m)[crop[1]:crop[3], crop[0]:crop[2]]).mean()) if registered else None})
    # Overview triple of the whole view for additional issues.
    scale = min(PANEL / max(W, H), 1.0)
    full = ren.copy()
    marks = draw_marks(full, ids, [0, 0, W, H], spec.get('ids_map') or []) if ids is not None else []
    panels = [panel(src, 'SOURCE ' + spec['view_id'], scale), panel(full, 'RENDER (numbered parts)', scale)]
    if resid is not None:
        panels.append(panel(resid, 'RESIDUAL', scale))
    overview = composite(panels, out_dir / f"{spec['view_id']}-overview.png")
    iou = float((src_m & ren_m).sum() / max((src_m | ren_m).sum(), 1))
    return {'view_id': spec['view_id'], 'width': W, 'height': H, 'object_height_px': int(object_height),
            'registered': registered, 'silhouette_iou': iou if registered else None,
            'overview': overview, 'overview_marks': marks, 'items': items}


def pair(spec):
    out_dir = Path(spec['out_dir']); out_dir.mkdir(parents=True, exist_ok=True)
    src = load_rgb(spec['source'])
    a = load_rgb(spec['a']).resize(src.size, Image.Resampling.LANCZOS)
    b = load_rgb(spec['b']).resize(src.size, Image.Resampling.LANCZOS)
    out = []
    for n, item in enumerate(spec['items']):
        crop = item['crop']
        cw, ch = crop[2] - crop[0], crop[3] - crop[1]
        scale = min(PANEL / max(cw, ch), 4.0)
        comp = composite([panel(src.crop(crop), 'SOURCE', scale), panel(a.crop(crop), 'A', scale),
                          panel(b.crop(crop), 'B', scale)], out_dir / f"{spec['tag']}-{n:02d}.png")
        out.append({'id': item['id'], 'composite': comp})
    return {'items': out}


def probe(spec):
    """Deterministic evidence inside judge regions (normalized to the render crop)."""
    src = load_rgb(spec['source']); W, H = src.size
    ren_m = None
    if spec.get('ids'):
        ids = ids_decode(spec['ids'], spec.get('ids_map') or [])
        if ids.shape != (H, W):
            ids = np.asarray(Image.fromarray(ids.astype(np.int32)).resize((W, H), Image.Resampling.NEAREST))
        ren_m = ids >= 0
    elif spec.get('render'):
        ren_m = object_mask(load_rgb(spec['render']).resize((W, H)))
    src_m = object_mask(src)
    albedo = load_rgb(spec['albedo']).resize((W, H)) if spec.get('albedo') else None
    results = []
    for q in spec['regions']:
        x0, y0, x1, y1 = q['crop']
        r = q['region']
        bx = [int(x0 + r[0] * (x1 - x0)), int(y0 + r[1] * (y1 - y0)),
              int(np.ceil(x0 + r[2] * (x1 - x0))), int(np.ceil(y0 + r[3] * (y1 - y0)))]
        bx = [max(0, bx[0]), max(0, bx[1]), min(W, max(bx[2], bx[0] + 1)), min(H, max(bx[3], bx[1] + 1))]
        sl = (slice(bx[1], bx[3]), slice(bx[0], bx[2]))
        res = {'key': q['key'], 'box_px': bx}
        if spec.get('registered') and ren_m is not None:
            area = max((src_m[sl] | ren_m[sl]).sum(), 1)
            res['residual_share'] = float((src_m[sl] ^ ren_m[sl]).sum() / area)
        if albedo is not None and ren_m is not None:
            both = src_m[sl] & ren_m[sl]
            ls = lit_median_lab(np.asarray(src)[sl], both)
            la = lit_median_lab(np.asarray(albedo)[sl], both)
            if ls is not None and la is not None:
                res['delta_e00'] = float(de2000(ls, la))
        results.append(res)
    return {'regions': results}


if __name__ == '__main__':
    action = sys.argv[1]
    spec = json.loads(Path(sys.argv[2]).read_text())
    fn = {'prepare': prepare, 'pair': pair, 'probe': probe}.get(action)
    if not fn:
        raise ValueError('unknown_crop_action')
    print(json.dumps(fn(spec), allow_nan=False))
