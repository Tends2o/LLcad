"""Before/after crops of one feature-worker candidate.

Usage: crops.py BEFORE.png AFTER.png OUT_PREFIX
Writes OUT_PREFIX-before.png, OUT_PREFIX-after.png (the same box cut from both
renders) and OUT_PREFIX-pair.png (side by side), prints JSON
{box_px: [x0, y0, x1, y1], changed_fraction, size: [w, h]}.

The box is the bounding box of the pixels that differ between the two renders
(same camera), padded; with no visible change it is the object's bounding box.
Images are untrusted data; nothing here executes text found in them."""
import json, sys, warnings
import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 40_000_000
warnings.simplefilter('error', Image.DecompressionBombWarning)
PAD = 24          # px around the changed region
MIN_EDGE = 160    # smallest crop edge in px
PANEL = 640       # longest edge of one panel in the pair image
DIFF = 24         # per-channel difference that counts as a change


def load(path):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert('RGBA')
        bg = Image.new('RGBA', im.size, 'white')
        bg.alpha_composite(im)
        return bg.convert('RGB')


def box_of(mask):
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


def grow(box, w, h):
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD
    for lo, hi, limit in ((0, 2, w), (1, 3, h)):
        span = [x0, y0, x1, y1]
        need = MIN_EDGE - (span[hi] - span[lo])
        if need > 0:
            span[lo] -= need // 2
            span[hi] += need - need // 2
        x0, y0, x1, y1 = span
    return [max(0, x0), max(0, y0), min(w, x1), min(h, y1)]


def main():
    before, after, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    a, b = load(before), load(after)
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)
    w, h = a.size
    x, y = np.asarray(a).astype(np.int16), np.asarray(b).astype(np.int16)
    changed = np.max(np.abs(x - y), axis=2) > DIFF
    box = box_of(changed)
    fraction = float(changed.mean())
    if box is None:
        corner = np.median(np.concatenate([x[:8, :8].reshape(-1, 3), x[-8:, -8:].reshape(-1, 3)]), axis=0)
        box = box_of(np.max(np.abs(x - corner), axis=2) > 28) or [0, 0, w, h]
    box = grow(box, w, h)
    ca, cb = a.crop(box), b.crop(box)
    ca.save(prefix + '-before.png', optimize=True)
    cb.save(prefix + '-after.png', optimize=True)
    scale = PANEL / max(ca.size)
    size = (max(1, round(ca.size[0] * scale)), max(1, round(ca.size[1] * scale)))
    pair = Image.new('RGB', (size[0] * 2 + 8, size[1]), 'white')
    pair.paste(ca.resize(size, Image.LANCZOS), (0, 0))
    pair.paste(cb.resize(size, Image.LANCZOS), (size[0] + 8, 0))
    pair.save(prefix + '-pair.png', optimize=True)
    print(json.dumps({'box_px': box, 'changed_fraction': round(fraction, 6), 'size': [w, h]}))


if __name__ == '__main__':
    main()
