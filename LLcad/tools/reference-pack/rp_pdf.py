"""Datasheets, flyers and vector files: text dimensions, alpha renders, vector logos."""
import hashlib, re, subprocess, xml.etree.ElementTree as ET
from pathlib import Path
import numpy as np
from PIL import Image

NUM = r'(\d+(?:[.,]\d+)?)'
TRIPLE = re.compile(NUM + r'\s*[x×X]\s*' + NUM + r'(?:\s*[x×X]\s*' + NUM + r')?\s*(mm|cm)\b')
SINGLE = re.compile(r'(?P<label>[A-Za-z /()\-]{3,40}?)\s*[:=]?\s+(?:ca\.?\s*|approx\.?\s*|~\s*)?' + NUM + r'\s*(mm|cm)\b')
WEIGHT = re.compile(r'(?i)(weight|mass)[^\n\d]{0,40}?' + NUM + r'\s*(kg|g)\b')
DIM_WORDS = re.compile(r'(?i)dimension|size|length|width|height|depth|diameter|mass')


def text_of(pdf):
    return subprocess.run(['pdftotext', '-layout', str(pdf), '-'], capture_output=True, text=True, check=True).stdout


def _mm(v, unit):
    x = float(v.replace(',', '.'))
    return x * 10 if unit == 'cm' else x


def dimensions(text, source_id):
    """Datasheet dimensions (tolerance class A: stated by the manufacturer). Datasheets
    round, so the default tolerance is half of the last stated digit or 0.5 mm."""
    out, seen = [], set()
    for line_no, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if 'packing' in low or 'packaging' in low:
            continue   # the box, not the product
        for m in TRIPLE.finditer(line):
            if not DIM_WORDS.search(line):
                continue
            vals = [_mm(v, m.group(4)) for v in m.groups()[:3] if v]
            label = line[:m.start()].strip(' :\t') or 'dimensions'
            key = ('triple', tuple(vals))
            if key in seen:
                continue
            seen.add(key)
            for i, v in enumerate(vals):
                digits = len(m.groups()[i].split('.')[-1]) if '.' in m.groups()[i] else 0
                out.append({'name': f'{label} [{i + 1}/{len(vals)}]', 'value_mm': round(v, 4),
                            'tolerance_mm': max(0.5, 0.5 * 10 ** -digits), 'class': 'A',
                            'source': source_id, 'evidence': line.strip()[:200], 'line': line_no,
                            'order_in_triple': i, 'triple_size': len(vals)})
        if TRIPLE.search(line):
            continue
        for m in SINGLE.finditer(line):
            label = m.group('label').strip()
            if not DIM_WORDS.search(label):
                continue
            v = _mm(m.group(2), m.group(3))
            key = ('single', label.lower(), v)
            if key in seen:
                continue
            seen.add(key)
            out.append({'name': label, 'value_mm': round(v, 4), 'tolerance_mm': 0.5, 'class': 'A',
                        'source': source_id, 'evidence': line.strip()[:200], 'line': line_no})
    weights = []
    for m in WEIGHT.finditer(text):
        ctx = text[max(0, m.start() - 20):m.end() + 5].lower()
        if 'packing' in ctx or 'packaging' in ctx:
            continue
        g = float(m.group(2).replace(',', '.')) * (1000 if m.group(3).lower() == 'kg' else 1)
        weights.append({'value_g': g, 'source': source_id, 'evidence': m.group(0).strip()[:120]})
    return out, weights


def alpha_renders(pdf, out_dir, tag, min_px=300):
    """Raster images that carry a soft mask (a cut-out product render): image + smask
    recombined into RGBA. pdfimages lists every image followed by its smask."""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    listing = subprocess.run(['pdfimages', '-list', str(pdf)], capture_output=True, text=True, check=True).stdout.splitlines()[2:]
    rows = []
    for line in listing:
        f = line.split()
        if len(f) >= 5:
            rows.append({'page': int(f[0]), 'num': int(f[1]), 'type': f[2], 'width': int(f[3]), 'height': int(f[4])})
    prefix = out_dir / f'{tag}-img'
    subprocess.run(['pdfimages', '-png', str(pdf), str(prefix)], check=True)
    found, seen = [], set()
    for i, r in enumerate(rows):
        if r['type'] != 'image' or i + 1 >= len(rows) or rows[i + 1]['type'] != 'smask':
            continue
        if max(r['width'], r['height']) < min_px:
            continue
        img = Path(f'{prefix}-{r["num"]:03d}.png'); mask = Path(f'{prefix}-{rows[i + 1]["num"]:03d}.png')
        if not img.exists() or not mask.exists():
            continue
        rgb = Image.open(img).convert('RGB')
        a = Image.open(mask).convert('L')
        if a.size != rgb.size:
            a = a.resize(rgb.size, Image.Resampling.BILINEAR)
        alpha = np.asarray(a)
        if alpha.min() > 250 or (alpha > 127).mean() > 0.95:
            continue   # no real cut-out
        digest = hashlib.sha256(np.asarray(rgb).tobytes() + alpha.tobytes()).hexdigest()
        if digest in seen:
            continue   # the same render repeated on several pages
        seen.add(digest)
        rgba = rgb.copy(); rgba.putalpha(a)
        path = out_dir / f'{tag}-alpha-p{r["page"]}-{r["num"]:03d}.png'
        rgba.save(path)
        found.append({'path': str(path), 'page': r['page'], 'width': r['width'], 'height': r['height'],
                      'coverage': round(float((alpha > 127).mean()), 4)})
    for p in out_dir.glob(f'{tag}-img-*.png'):
        p.unlink()
    return found


FILL = re.compile(r'fill\s*[:=]\s*"?\s*(rgb\([^)]*\)|#[0-9a-fA-F]{3,6})')


def _hex(fill):
    fill = fill.strip()
    if fill.startswith('#'):
        h = fill[1:]
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        return '#' + h.lower()
    vals = [v.strip() for v in fill[4:-1].split(',')]
    rgb = [round(float(v[:-1]) * 2.55) if v.endswith('%') else round(float(v)) for v in vals]
    return '#%02x%02x%02x' % tuple(max(0, min(255, c)) for c in rgb)


def svg_pages(pdf, out_dir, tag, max_pages=4):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    pages = int(re.search(r'Pages:\s+(\d+)', subprocess.run(['pdfinfo', str(pdf)], capture_output=True, text=True).stdout or 'Pages: 1').group(1))
    files = []
    for p in range(1, min(pages, max_pages) + 1):
        target = out_dir / f'{tag}-p{p}.svg'
        subprocess.run(['pdftocairo', '-svg', '-f', str(p), '-l', str(p), str(pdf), str(target)], check=True)
        files.append(target)
    return files


def vector_logos(svg_file, out_dir, source_id, max_paths=400):
    """Cluster the filled paths of a vector page by fill colour. Colours other than
    black/white/grey with a bounded path count are logo/marking candidates; each gets a
    copy of the page with every other path hidden (transforms and viewBox kept)."""
    text = Path(svg_file).read_text(errors='replace')
    counts = {}
    for m in FILL.finditer(text):
        h = _hex(m.group(1))
        counts[h] = counts.get(h, 0) + 1
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    ET.register_namespace('xlink', 'http://www.w3.org/1999/xlink')
    out = []
    for h, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
        chroma = max(r, g, b) - min(r, g, b)
        if chroma < 40 or n > max_paths:
            continue
        tree = ET.parse(svg_file)
        kept = 0
        for el in tree.iter():
            style = (el.get('style') or '') + ' fill="' + (el.get('fill') or '') + '"'
            fm = FILL.search(style)
            if el.tag.endswith('path') or el.tag.endswith('use'):
                if fm and _hex(fm.group(1)) == h:
                    kept += 1
                else:
                    el.set('style', (el.get('style') or '') + ';display:none')
        path = Path(out_dir) / f'{Path(svg_file).stem}-fill-{h[1:]}.svg'
        tree.write(path)
        out.append({'fill': h, 'path_count': n, 'kept_elements': kept, 'svg': str(path), 'source': source_id,
                    'page_svg': str(svg_file),
                    'note': 'Original vector shape; brand print colour differs from the device colour (measure that on product images).'})
    return out
