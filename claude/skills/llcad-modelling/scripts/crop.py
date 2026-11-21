#!/usr/bin/env python3
"""Reference | render | diff crop triples with part-ID labels, for the visual judge.

  crop.py triple --ref ref.png --render render.png [--parts view.json] [--box x,y,w,h | --part PID] \
                 [--margin 16] [--label "logo"] [--scale 2] -o out.png
  crop.py sheet spec.json -o sheet.png        spec: [{"ref":..,"render":..,"box":[x,y,w,h]|null,"part":..,"label":..,"parts":..}]

The render must come from the same (registered) camera as the reference; it is resized to the
reference size when they differ. `--parts` takes the per-part JSON of cad_view (part_id, name,
bbox_px, centroid_px) or a list of such records: numbered set-of-mark labels are drawn on the
render tile and listed in a legend strip. The diff tile is a heat map of |ref - render| over a
dimmed reference. Output long edge is capped at 2576 px (image limit). Prints a one-line JSON.
"""
import argparse
import json
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

MAX_EDGE = 2576


def load_parts(path):
    if not path:
        return []
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict):
        for key in ("parts", "per_part", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            views = data.get("views")
            data = views[0].get("parts", []) if isinstance(views, list) and views else []
    return [p for p in data if isinstance(p, dict)]


def font(size):
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            pass
    return ImageFont.load_default()


def crop_box(img_size, box, part, parts, margin):
    W, H = img_size
    if box:
        x, y, w, h = box
    elif part:
        rec = next((p for p in parts if p.get("part_id") == part or p.get("name") == part), None)
        if not rec or not rec.get("bbox_px"):
            raise SystemExit("part %s has no bbox_px in --parts" % part)
        x0, y0, x1, y1 = rec["bbox_px"]
        x, y, w, h = x0, y0, x1 - x0, y1 - y0
    else:
        x, y, w, h = 0, 0, W, H
    x0, y0 = max(0, int(x - margin)), max(0, int(y - margin))
    x1, y1 = min(W, int(x + w + margin)), min(H, int(y + h + margin))
    if x1 - x0 < 4 or y1 - y0 < 4:
        raise SystemExit("crop box empty")
    return x0, y0, x1, y1


def triple(ref_p, render_p, parts=None, box=None, part=None, margin=16, label=None, scale=None):
    ref = Image.open(ref_p).convert("RGB")
    ren = Image.open(render_p).convert("RGB")
    sx = sy = 1.0
    if ren.size != ref.size:
        sx, sy = ref.size[0] / ren.size[0], ref.size[1] / ren.size[1]
        ren = ren.resize(ref.size, Image.LANCZOS)
    parts = parts or []
    x0, y0, x1, y1 = crop_box(ref.size, box, part, [{**p, "bbox_px": [p["bbox_px"][0] * sx, p["bbox_px"][1] * sy, p["bbox_px"][2] * sx, p["bbox_px"][3] * sy]} if p.get("bbox_px") else p for p in parts], margin)
    a = np.asarray(ref.crop((x0, y0, x1, y1)), dtype=np.float32)
    b = np.asarray(ren.crop((x0, y0, x1, y1)), dtype=np.float32)
    d = np.abs(a - b).mean(axis=2)
    heat = np.zeros_like(a)
    gray = a.mean(axis=2, keepdims=True) * 0.35
    heat[:] = gray
    heat[..., 0] = np.clip(gray[..., 0] + d * 2.2, 0, 255)
    heat[..., 1] = np.clip(gray[..., 0] + np.maximum(0, d * 2.2 - 255) * 0.8, 0, 255)
    tiles = [Image.fromarray(a.astype(np.uint8)), Image.fromarray(b.astype(np.uint8)), Image.fromarray(heat.astype(np.uint8))]
    w, h = tiles[0].size
    k = scale or max(1.0, min(4.0, 420 / max(1, h)))
    k = min(k, MAX_EDGE / (3 * w + 8))
    tw, th = max(1, int(w * k)), max(1, int(h * k))
    tiles = [t.resize((tw, th), Image.LANCZOS if k < 1 else Image.NEAREST if k >= 3 else Image.BICUBIC) for t in tiles]
    draw = ImageDraw.Draw(tiles[1])
    legend = []
    fnt = font(max(11, int(th / 22)))
    n = 0
    for p in parts:
        c = p.get("centroid_px")
        if not c:
            continue
        u, v = c[0] * sx, c[1] * sy
        if not (x0 <= u < x1 and y0 <= v < y1) or p.get("visible_px", 1) == 0:
            continue
        n += 1
        px, py = (u - x0) * k, (v - y0) * k
        r = max(7, int(th / 40))
        draw.ellipse((px - r, py - r, px + r, py + r), fill=(255, 255, 0), outline=(0, 0, 0))
        draw.text((px - r / 2, py - r * 0.9), str(n), fill=(0, 0, 0), font=font(max(9, r)))
        legend.append("%d %s" % (n, p.get("part_id") or p.get("name")))
    head = 26
    legend_h = (16 * ((len(legend) + 3) // 4) + 6) if legend else 0
    out = Image.new("RGB", (3 * tw + 8, th + head + legend_h), (24, 24, 24))
    dr = ImageDraw.Draw(out)
    f2 = font(14)
    for i, (t, name) in enumerate(zip(tiles, ("reference", "render", "|diff|"))):
        out.paste(t, (i * (tw + 4), head))
        dr.text((i * (tw + 4) + 4, 5), name + ((" · " + label) if (label and i == 0) else ""), fill=(230, 230, 230), font=f2)
    for i, item in enumerate(legend):
        dr.text((6 + (i % 4) * (out.size[0] // 4), head + th + 3 + 16 * (i // 4)), item[:40], fill=(255, 255, 120), font=font(12))
    return out, {"box": [x0, y0, x1 - x0, y1 - y0], "mean_abs_diff": round(float(d.mean()), 2), "p95_abs_diff": round(float(np.percentile(d, 95)), 2),
                 "labels": legend[:24]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("triple")
    t.add_argument("--ref", required=True)
    t.add_argument("--render", required=True)
    t.add_argument("--parts")
    t.add_argument("--box", help="x,y,w,h in reference pixels")
    t.add_argument("--part")
    t.add_argument("--margin", type=int, default=16)
    t.add_argument("--label")
    t.add_argument("--scale", type=float)
    t.add_argument("-o", "--out", required=True)
    s = sub.add_parser("sheet")
    s.add_argument("spec")
    s.add_argument("-o", "--out", required=True)
    a = ap.parse_args()
    if a.cmd == "triple":
        box = [float(v) for v in a.box.split(",")] if a.box else None
        img, info = triple(a.ref, a.render, load_parts(a.parts), box, a.part, a.margin, a.label, a.scale)
        img.save(a.out)
        print(json.dumps({"out": a.out, "size": img.size, **info}, ensure_ascii=False))
        return
    spec = json.load(open(a.spec, encoding="utf-8"))
    rows, infos = [], []
    for e in spec:
        img, info = triple(e["ref"], e["render"], load_parts(e.get("parts")), e.get("box"), e.get("part"), e.get("margin", 16), e.get("label"))
        rows.append(img)
        infos.append({"label": e.get("label"), **info})
    W = max(r.size[0] for r in rows)
    H = sum(r.size[1] + 6 for r in rows)
    k = min(1.0, MAX_EDGE / max(W, H))
    sheet = Image.new("RGB", (W, H), (0, 0, 0))
    y = 0
    for r in rows:
        sheet.paste(r, (0, y))
        y += r.size[1] + 6
    if k < 1:
        sheet = sheet.resize((int(W * k), int(H * k)), Image.LANCZOS)
    sheet.save(a.out)
    print(json.dumps({"out": a.out, "size": sheet.size, "items": infos}, ensure_ascii=False)[:1500])


if __name__ == "__main__":
    sys.exit(main())
