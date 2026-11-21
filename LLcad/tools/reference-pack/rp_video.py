"""Turntable video: 480p scan, turntable segments, canonical frames, full-res
silhouettes scaled to a datasheet height, colours, optional visual hull."""
import json, math, subprocess, time
from pathlib import Path
import numpy as np
from PIL import Image
from scipy import ndimage
import rp_image


def probe(video):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                          'stream=width,height,r_frame_rate,nb_frames,duration', '-of', 'json', str(video)],
                         capture_output=True, text=True, check=True).stdout
    s = json.loads(out)['streams'][0]
    num, den = (s.get('r_frame_rate') or '25/1').split('/')
    return {'width': int(s['width']), 'height': int(s['height']), 'fps': float(num) / float(den or 1),
            'duration_s': float(s.get('duration') or 0)}


def scan(video, meta, width=480):
    """One pass at low resolution: per frame border background, whiteness and the
    object's bounding box (distance to the border median > 40)."""
    W = width; H = int(round(meta['height'] * W / meta['width'] / 2)) * 2
    t0 = time.time()
    p = subprocess.Popen(['ffmpeg', '-v', 'error', '-i', str(video), '-vf', f'scale={W}:{H}', '-f', 'rawvideo',
                          '-pix_fmt', 'rgb24', '-'], stdout=subprocess.PIPE)
    rows, i, fps = [], 0, meta['fps']
    while True:
        buf = p.stdout.read(W * H * 3)
        if len(buf) < W * H * 3:
            break
        a = np.frombuffer(buf, np.uint8).reshape(H, W, 3).astype(np.int16)
        border = np.concatenate([a[:6].reshape(-1, 3), a[-6:].reshape(-1, 3), a[:, :6].reshape(-1, 3), a[:, -6:].reshape(-1, 3)])
        bg = np.median(border, 0)
        white = bool((bg > 235).all() and border.std(0).max() < 12)
        dark = bool((bg < 25).all() and border.std(0).max() < 12)
        m = np.abs(a - bg).max(-1) > 40
        r = {'i': i, 't': round(i / fps, 3), 'white': white, 'dark': dark, 'area': 0}
        # A dark product on a light background: the grey < 200 mask ignores shadows,
        # plates and light overlays that the background-distance mask picks up.
        if white:
            lum = a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114
            d = lum < 200
            if d.sum() > 50:
                m = d
                r['mask'] = 'dark'
        if m.sum() > 25:
            # The product is the largest component; overlays, text and other
            # products beside it must not widen its box.
            lab, n = ndimage.label(ndimage.binary_closing(m, iterations=2))
            sizes = np.bincount(lab.ravel()); sizes[0] = 0
            big = lab == int(np.argmax(sizes))
            cols = np.where(big.any(0))[0]; rws = np.where(big.any(1))[0]
            if len(cols) > 5 and len(rws) > 5:
                r.update(area=int(sizes.max()), x0=int(cols[0]), x1=int(cols[-1]), y0=int(rws[0]), y1=int(rws[-1]),
                         others=int((sizes > 0.02 * sizes.max()).sum() - 1))
        rows.append(r); i += 1
    p.wait()
    return rows, {'frames': i, 'scan_s': round(time.time() - t0, 2), 'scan_size': [W, H]}


def segments(rows, H, min_frames=10):
    """Runs of white-background frames with one upright object (height > 50 % of the
    frame) that do not touch the border: turntable candidates."""
    segs, cur = [], None
    for r in rows:
        ok = r['white'] and r['area'] > 0 and (r['y1'] - r['y0']) > 0.5 * H and r['y0'] > 1 and r['y1'] < H - 2
        if ok and cur is None:
            cur = [r['i'], r['i']]
        elif ok:
            cur[1] = r['i']
        elif cur:
            segs.append(cur); cur = None
    if cur:
        segs.append(cur)
    out = []
    for s, e in segs:
        if e - s < min_frames:
            continue
        seg = rows[s:e + 1]
        w = np.array([r['x1'] - r['x0'] for r in seg], float); h = np.array([r['y1'] - r['y0'] for r in seg], float)
        # Canonical frames by aspect (width / height) of the product component, only
        # among frames at the segment's zoom (height within 2 % of its median):
        # minimum = frontal, maximum = side. Other objects in the frame (overlays,
        # accessories) do not matter; the product is the largest component.
        stable = np.abs(h - np.median(h)) <= 0.02 * np.median(h)
        aspect = np.where(stable, w / np.maximum(h, 1), np.nan)
        j, k = int(np.nanargmin(aspect)), int(np.nanargmax(aspect))
        out.append({'t0': seg[0]['t'], 't1': seg[-1]['t'], 'frames': len(seg), 'height_px_480_median': float(np.median(h)),
                    'min_width': {'t': seg[j]['t'], 'i': seg[j]['i'], 'w480': int(w[j])},
                    'max_width': {'t': seg[k]['t'], 'i': seg[k]['i'], 'w480': int(w[k])},
                    'stable_frames': int(stable.sum()),
                    'width_range_ratio': round(float(np.nanmax(aspect) / max(np.nanmin(aspect), 1e-6)), 3),
                    'turntable': bool(np.nanmax(aspect) / max(np.nanmin(aspect), 1e-6) > 1.08)})
    return out


def frame(video, t, meta, gray=False):
    fmt, ch = ('gray', 1) if gray else ('rgb24', 3)
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{t:.3f}', '-i', str(video), '-frames:v', '1', '-f', 'rawvideo',
                          '-pix_fmt', fmt, '-'], capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.uint8)
    return a.reshape(meta['height'], meta['width']) if gray else a.reshape(meta['height'], meta['width'], 3)


def dark_mask(gray, thr=200):
    m = gray < thr
    lab, n = ndimage.label(ndimage.binary_opening(m, iterations=2))
    if not n:
        return m
    sizes = ndimage.sum(np.ones_like(gray, float), lab, range(1, n + 1))
    return ndimage.binary_fill_holes(lab == int(np.argmax(sizes)) + 1)


def _window(video, meta, t0, seconds, fmt='gray'):
    """Decoded frames of one window, in order (the same decode for choosing and taking
    a frame, so a frame's index means the same picture both times)."""
    W, H = meta['width'], meta['height']
    ch = 1 if fmt == 'gray' else 3
    p = subprocess.Popen(['ffmpeg', '-v', 'error', '-ss', f'{t0:.3f}', '-i', str(video), '-t', f'{seconds:.3f}',
                          '-f', 'rawvideo', '-pix_fmt', fmt, '-'], stdout=subprocess.PIPE)
    try:
        while True:
            buf = p.stdout.read(W * H * ch)
            if len(buf) < W * H * ch:
                break
            yield np.frombuffer(buf, np.uint8).reshape((H, W) if ch == 1 else (H, W, 3))
    finally:
        p.stdout.close(); p.wait()


def _spread(mask):
    rows = np.where(mask.any(1))[0]
    k = max(1, len(rows) // 20)
    centres = [(np.where(mask[y])[0][0] + np.where(mask[y])[0][-1]) / 2 for y in rows[k:-k:4]]
    return float(np.std(centres)) if centres else 1e9


def refine(video, meta, t, which, window_s=0.6):
    """Full-resolution canonical frame near t. Frames at the window's zoom (component
    height within 3 % of the median) only; front = narrowest component, ties broken
    by the most mirror-symmetric silhouette (smallest spread of the row centres: the
    "axis constant within 0.5 px"); side = widest. Returns (time, rgb)."""
    t0 = max(0.0, t - window_s)
    frames = []
    for n, g in enumerate(_window(video, meta, t0, 2 * window_s)):
        m = dark_mask(g[::2, ::2].astype(float))
        cols = np.where(m.any(0))[0]; rows = np.where(m.any(1))[0]
        if len(cols) > 5:
            frames.append((n, int(cols[-1] - cols[0]), int(rows[-1] - rows[0])))
    if not frames:
        return t, frame(video, t, meta)
    # A cut or zoom inside the window changes the component height by several
    # percent; the threshold edge of light parts moves it by ~1-2 % only.
    hm = float(np.median([f[2] for f in frames]))
    stable = [f for f in frames if abs(f[2] - hm) <= 0.03 * hm] or frames
    if which == 'max_width':
        wmax = max(f[1] for f in stable)
        best = [f for f in stable if f[1] >= wmax]
        cands = {best[len(best) // 2][0]}
    else:
        wmin = min(f[1] for f in stable)
        cands = {f[0] for f in stable if f[1] <= wmin + 1}
    chosen, score = None, None
    for n, rgb in enumerate(_window(video, meta, t0, 2 * window_s, 'rgb24')):
        if n not in cands:
            continue
        sc = 0.0 if which == 'max_width' else _spread(dark_mask(np.asarray(Image.fromarray(rgb).convert('L')).astype(float)))
        if score is None or sc < score:
            chosen, score = (round(t0 + n / meta['fps'], 3), rgb.copy()), sc
    return chosen if chosen else (t, frame(video, t, meta))


def canonical(video, meta, seg, which, height_mm, out_dir, ref_id):
    """Full-resolution canonical frame: subpixel silhouette scaled to height_mm."""
    t, rgb = refine(video, meta, seg[which]['t'], which)
    gray = np.asarray(Image.fromarray(rgb).convert('L')).astype(float)
    mask = dark_mask(gray)
    out_dir = Path(out_dir)
    png = out_dir / f'{ref_id}.png'
    Image.fromarray(rgb).save(png)
    rp_image.save_mask(mask, out_dir / f'{ref_id}-mask.png')
    rec = {'time_s': t, 'path': str(png), 'mask': str(out_dir / f'{ref_id}-mask.png'), 'width': meta['width'], 'height': meta['height']}
    profile = None
    if height_mm:
        stats, prof = rp_image.subpixel_profile(gray, mask, height_mm)
        csv = out_dir / f'{ref_id}-width-per-height.csv'
        rp_image.write_profile_csv(csv, prof)
        profile = {'csv': str(csv), 'rows': len(prof), **stats}
    colours = rp_image.region_colours(Image.fromarray(rgb), mask)
    return rec, profile, colours


def camera_hint(profile, meta, view):
    """Absolute orthographic camera under the pack's frame convention (base centre at
    the origin, product axis +Z, front facing -Y): valid for a long-lens turntable
    frame; the side view's handedness stays unknown until confirmed."""
    s = profile['px_per_mm']
    u_axis, v_base = profile['axis_x_px_mean'], profile['bottom_px']
    W, H = meta['width'], meta['height']
    x_c = (W / 2 - u_axis) / s
    z_c = (v_base - H / 2) / s
    if view == 'front':
        target, position = [x_c, 0.0, z_c], [x_c, -1000.0, z_c]
    else:
        target, position = [0.0, -x_c, z_c], [1000.0, -x_c, z_c]
    return {'mode': 'absolute', 'projection': 'orthographic', 'position_mm': [round(v, 4) for v in position],
            'target_mm': [round(v, 4) for v in target], 'up': [0, 0, 1], 'mm_per_px': round(1 / s, 7),
            'width_px': W, 'height_px': H,
            'convention': 'model base centre at origin, axis +Z up, front faces -Y' + ('' if view == 'front' else '; side assumed from +X (handedness unconfirmed)')}


def visual_hull(video, meta, seg, height_mm, px_per_mm, axis_px, out_dir, views=36, voxel_mm=0.5, max_edge=720):
    """Outer bound only. Turntable angle from constant angular velocity anchored on the
    minimum-width (front, 0 deg) and nearest maximum-width (side, 90 deg) frames; an
    orthographic long lens is assumed. Cannot recover concavities."""
    tf, ts = seg['min_width']['t'], seg['max_width']['t']
    quarter = abs(ts - tf)
    if quarter < 0.2:
        return {'status': 'skipped', 'reason': 'front and side frames too close to estimate the rotation speed'}
    omega = 90.0 / quarter * (1 if ts > tf else -1)
    t_lo, t_hi = seg['t0'], seg['t1']
    ts_list = [t for t in np.linspace(t_lo, t_hi, views) if abs((t - tf) * omega) <= 360]
    scale = min(1.0, max_edge / max(meta['width'], meta['height']))
    k = px_per_mm * scale
    sils = []
    for t in ts_list:
        g = frame(video, float(t), meta, gray=True).astype(float)
        if scale < 1:
            g = np.asarray(Image.fromarray(g.astype(np.uint8)).resize((round(meta['width'] * scale), round(meta['height'] * scale)), Image.Resampling.BILINEAR)).astype(float)
        m = dark_mask(g)
        if m.sum() > 100:
            sils.append((math.radians((t - tf) * omega), m))
    # Only frames at the front frame's zoom (component height within 3 %).
    heights = [np.ptp(np.where(m.any(1))[0]) for _, m in sils]
    if sils:
        href = height_mm * k
        sils = [s for s, hh in zip(sils, heights) if abs(hh - href) <= 0.03 * href]
    if len(sils) < 8:
        return {'status': 'skipped', 'reason': f'only {len(sils)} usable silhouettes at the front frame zoom'}
    Hs, Ws = sils[0][1].shape
    v_base = int(np.median([np.where(m.any(1))[0][-1] for _, m in sils]))
    # Horizontal extent from the widest silhouette, plus margin.
    widest = max(int(np.ptp(np.where(m.any(0))[0])) for _, m in sils) / k
    half = 0.6 * widest + 2 * voxel_mm
    xs = np.arange(-half, half, voxel_mm); zs = np.arange(0, height_mm, voxel_mm)
    X, Y = np.meshgrid(xs, xs, indexing='ij')
    votes = np.zeros((len(zs),) + X.shape, np.uint8)   # (z, x, y)
    u0 = axis_px * scale
    vi = np.clip(np.round(v_base - zs * k).astype(int), 0, Hs - 1)
    for th, m in sils:
        # The object turned by th about +Z: a point (X, Y) projects to X cos th - Y sin th.
        ui = np.clip(np.round(u0 + (X * math.cos(th) - Y * math.sin(th)) * k).astype(int), 0, Ws - 1)
        votes += m[vi][:, ui]
    # A voxel stays when >= 90 % of the views see it inside (angle estimates and
    # compression blur make a strict intersection too brittle); still an outer bound.
    occ = votes >= math.ceil(0.9 * len(sils))
    vol_ml = float(occ.sum() * voxel_mm ** 3 / 1000)
    if not occ.any():
        return {'status': 'inconsistent', 'views': len(sils), 'degrees_per_s': round(omega, 3),
                'reason': 'the silhouettes do not agree on any volume (rotation not steady or not about the product axis)'}
    out_dir = Path(out_dir)
    top = occ.any(0)
    rp_image.save_mask(top.T[::-1], out_dir / 'hull-top.png')
    sections = []
    for zf in (0.1, 0.25, 0.5, 0.75, 0.9):
        iz = min(len(zs) - 1, int(zf * len(zs)))
        sl = occ[iz]
        if sl.any():
            ix = np.where(sl.any(1))[0]; iy = np.where(sl.any(0))[0]
            sections.append({'z_mm': round(float(zs[iz]), 2), 'x_extent_mm': round(float((ix[-1] - ix[0] + 1) * voxel_mm), 2),
                             'y_extent_mm': round(float((iy[-1] - iy[0] + 1) * voxel_mm), 2)})
    return {'status': 'computed', 'views': len(sils), 'vote': 0.9, 'voxel_mm': voxel_mm, 'degrees_per_s': round(omega, 3),
            'volume_ml_outer_bound': round(vol_ml, 1), 'top_silhouette': str(out_dir / 'hull-top.png'),
            'sections': sections, 'note': 'Visual hull = outer bound (no concavities); angles assume constant turntable speed.'}
