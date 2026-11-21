"""Legibility check of the text items of a drawing with RapidOCR.

Cross-check only: an OCR model reads the crop of each text, and the result is compared with the expected text. Only
warnings are produced (text_unreadable, glyph_missing), never a dimension value. Text position and expected text come from the text layer of the PDF (pdfium: one
text object per text of the sheet, i.e. per `Sheet.text` call), so that local runs and the reference run are measured the same way.

Narrow interface to the OCR model: an engine is an object with `read(img: np.ndarray, mode: str) -> (text, confidence, lines)`.
`RapidEngine` loads the det, rec and cls models from explicit local paths (no download, no network)."""
import ctypes
import difflib
import hashlib
import math
import os
import re
import time
import unicodedata
from importlib import metadata
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FONT_PATH = HERE / 'fonts' / 'osifont-lgpl3fe.ttf'
MODEL_FILES = {'det': 'ch_PP-OCRv4_det_infer.onnx', 'rec': 'ch_PP-OCRv4_rec_infer.onnx', 'cls': 'ch_ppocr_mobile_v2.0_cls_infer.onnx'}
MODEL_ENV = 'LLCAD_RAPIDOCR_DIR'
# default model folder: <package root>/models/rapidocr; this file is <package root>/LLcad/packages/drawing/legibility.py
DEFAULT_MODEL_DIR = HERE.parents[2] / 'models' / 'rapidocr'

MARGIN_MM = 3.0                 # margin around the text box
CAP_PX = 32                     # minimum height of the capitals in the crop in pixels
DPI_MIN, DPI_MAX = 150.0, 700.0
CAP = 0.7373                    # cap height / font size for osifont (as sheet.CAP)
TIGHT_MM = 0.6                  # margin of the tight crop for pure line recognition
SIM_MIN = 0.8                   # similarity threshold for texts without digits
SKIP_SYMBOLS = 'Ø⌀□±°øØ∅'       # the model does not know them: removed from both sides
TEXT_SYMBOLS = 'Ø⌀□±°×'         # characters the font must carry (the glyph check applies to all characters of the expected text)
UMLAUT = str.maketrans({'ä': 'a', 'ö': 'o', 'ü': 'u', 'Ä': 'A', 'Ö': 'O', 'Ü': 'U', 'ß': 'ss'})
PTMM = 25.4 / 72


class EngineUnavailable(Exception):
    """Package or model files are missing: the check ends in an orderly way with this message."""


# ---------------------------------------------------------------------------------------------------------- Comparison
def normalize(s, spaces=False):
    """Decimal comma = point, × = x, without spaces (spaces=True: spaces stay as separators, for the number groups), without Ø ⌀ □ ± °, umlauts folded, lower case."""
    s = str(s).replace('\u00a0', ' ')
    s = ''.join(c for c in s if c not in SKIP_SYMBOLS)
    s = s.replace(',', '.').replace('×', 'x').replace('✕', 'x')
    s = s.translate(UMLAUT)
    s = re.sub(r'\s+', ' ' if spaces else '', s)
    return s.lower()


def number_groups(s):
    """Number groups of a normalized text in order (the decimal point counts)."""
    return re.findall(r'\d+(?:\.\d+)?', s)


def _in_order(want, have):
    it = iter(have)
    return all(any(w == h for h in it) for w in want)


def judgeable(expected):
    """A text without letters and digits (only "-", "–", "≤", ⌵ ...) cannot be judged by OCR: it does not count (skipped)."""
    return any(c.isalnum() for c in str(expected))


def has_digits(expected):
    return bool(number_groups(normalize(expected, spaces=True)))


def compare(expected, ocr_lines):
    """-> (readable: bool, similarity|None). ocr_lines: lines of the OCR output. With digits: all number groups of the expected text occur in
    the OCR output in the same order, exactly (extra numbers from the margin do not matter). Without digits: normalized similarity
    >= SIM_MIN (against the whole output and against each line, the best result counts)."""
    ocr_lines = [str(x) for x in (ocr_lines or [])]
    ns = normalize(expected)
    groups = number_groups(normalize(expected, spaces=True))
    if groups:
        ocr_text = normalize(' '.join(ocr_lines), spaces=True)
        if any(c in SKIP_SYMBOLS for c in str(expected)):       # the model reads Ø/⌀ as "0" (or "O") before the number: such a zero does not count
            ocr_alt = re.sub(r'(?<![\d.])0(?=\d)', '', ocr_text)
            return _in_order(groups, number_groups(ocr_text)) or _in_order(groups, number_groups(ocr_alt)), None
        return _in_order(groups, number_groups(ocr_text)), None
    cands = [normalize(''.join(ocr_lines))] + [normalize(x) for x in ocr_lines]
    best = max((difflib.SequenceMatcher(None, ns, c).ratio() for c in cands if c), default=0.0)
    if not ns:
        return True, 1.0
    return best >= SIM_MIN, round(best, 3)


# ---------------------------------------------------------------------------------------------------------- Glyphs
def font_chars(font_path=FONT_PATH):
    """Set of the Unicode characters that the font maps (cmap)."""
    from fontTools.ttLib import TTFont
    f = TTFont(str(font_path), lazy=True)
    try:
        return set(f.getBestCmap().keys())
    finally:
        f.close()


def glyph_findings(texts, have=None):
    """glyph_missing (warn): a character of an expected text is missing in the font. texts: list of {'text', 'bbox'?, 'owner'?}; have: character codes of the
    font (default: osifont of the renderer). One finding per missing character, with count and first text."""
    have = font_chars() if have is None else have
    seen = {}
    for t in texts:
        for ch in t['text']:
            if ch.isspace() or unicodedata.category(ch).startswith('C') or ord(ch) in have:
                continue
            e = seen.setdefault(ch, {'count': 0, 'text': t['text'], 'bbox_mm': t.get('bbox'), 'owner': t.get('owner')})
            e['count'] += 1
    return [{'code': 'glyph_missing', 'severity': 'warn', 'char': ch, 'codepoint': f'U+{ord(ch):04X}', 'count': e['count'],
             'text': e['text'], 'bbox_mm': e['bbox_mm'], 'owner': e['owner'],
             'message': f"The font has no glyph for {ch!r} (U+{ord(ch):04X}), occurring in “{e['text']}” ({e['count']}×)"}
            for ch, e in seen.items()]


# ---------------------------------------------------------------------------------------------------------- Text items from the PDF
def pdf_texts(pdf_path, page_no=0):
    """Text items of the page from the text layer (pdfium): per text object {'text','center','w','h','angle','cap','bbox'} in sheet mm (origin bottom
    left, as Sheet). angle in degrees (reading direction counter-clockwise), w/h = length/height of the box in reading direction, cap = cap height."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        tp = pdf[page_no].get_textpage()
        h = tp.raw
        groups, order = {}, []
        x0, y0, x1, y1 = (ctypes.c_double() for _ in range(4))
        mat = raw.FS_MATRIX()
        for i in range(tp.count_chars()):
            u = raw.FPDFText_GetUnicode(h, i)
            if u < 32:
                continue
            o = raw.FPDFText_GetTextObject(h, i)
            key = ctypes.addressof(o.contents) if o else None
            if key is None:
                continue
            if key not in groups:
                groups[key] = {'chars': [], 'fs': float(raw.FPDFText_GetFontSize(h, i))}
                order.append(key)
                raw.FPDFText_GetMatrix(h, i, ctypes.byref(mat))
                groups[key]['angle'] = math.degrees(math.atan2(mat.b, mat.a))
            box = pdfium_charbox(raw, h, i)
            groups[key]['chars'].append((chr(u), box))
        out = []
        for key in order:
            g = groups[key]
            s = ''.join(c for c, _ in g['chars'])
            if not s.strip():
                continue
            ang = g['angle'] % 360
            for k in (0, 90, 180, 270, 360):                 # snap rounding noise of the matrix to the axes
                if abs(ang - k) < 0.6:
                    ang = float(k % 360)
            a = math.radians(ang)
            ux, uy = math.cos(a), math.sin(a)
            # box in reading direction: corner points of all character boxes in text coordinates (u along, v across)
            us, vs, xs, ys = [], [], [], []
            for _, (l, b, r, t) in g['chars']:
                for px, py in ((l, b), (r, b), (r, t), (l, t)):
                    us.append(px * ux + py * uy)
                    vs.append(-px * uy + py * ux)
                    xs.append(px)
                    ys.append(py)
            u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
            cu, cv = (u0 + u1) / 2, (v0 + v1) / 2
            cx, cy = cu * ux - cv * uy, cu * uy + cv * ux
            cap = g['fs'] * PTMM * CAP
            out.append({'text': s, 'center': [cx * PTMM, cy * PTMM], 'w': (u1 - u0) * PTMM, 'h': (v1 - v0) * PTMM, 'angle': ang, 'cap': cap,
                        'bbox': [min(xs) * PTMM, min(ys) * PTMM, max(xs) * PTMM, max(ys) * PTMM], 'owner': None})
        return out
    finally:
        pdf.close()


def pdfium_charbox(raw, h, i):
    l, r, b, t = (ctypes.c_double() for _ in range(4))
    raw.FPDFText_GetCharBox(h, i, ctypes.byref(l), ctypes.byref(r), ctypes.byref(b), ctypes.byref(t))
    return l.value, b.value, r.value, t.value


# ---------------------------------------------------------------------------------------------------------- Crops
def crop_dpi(cap_mm):
    """Resolution at which the capitals are at least CAP_PX pixels high (limited to DPI_MIN..DPI_MAX)."""
    if cap_mm <= 0:
        return DPI_MAX
    return min(DPI_MAX, max(DPI_MIN, CAP_PX * 25.4 / cap_mm))


def render_crop(page, t, margin_mm=MARGIN_MM, dpi=None):
    """Crop of the text box t (+ margin) from the PDF page as an upright RGB array (numpy). Only the crop is rendered
    (pdfium clipping); when rotated, a square around the box, which is then straightened and cropped to the box + margin."""
    from PIL import Image
    dpi = dpi or crop_dpi(t['cap'])
    cx, cy = t['center']
    w, h = t['w'] + 2 * margin_mm, t['h'] + 2 * margin_mm
    ang = t['angle'] % 360
    if abs(ang) < 1e-6:
        rw, rh = w, h
    else:
        a = math.radians(ang)
        rw = abs(w * math.cos(a)) + abs(h * math.sin(a))
        rh = abs(w * math.sin(a)) + abs(h * math.cos(a))
    pw, ph = page.get_size()                          # pt
    f = 1 / PTMM
    l, r = max(0.0, (cx - rw / 2) * f), min(pw, (cx + rw / 2) * f)
    b, tp = max(0.0, (cy - rh / 2) * f), min(ph, (cy + rh / 2) * f)
    img = page.render(scale=dpi / 72, crop=(l, b, pw - r, ph - tp)).to_pil().convert('RGB')
    if abs(ang) < 1e-6:
        return np.asarray(img)
    # reading direction -> horizontal: PIL rotates counter-clockwise, hence by -ang. The centre of the rendered area (clipped at the page edge,
    # then not the centre of the box) is rotated along; the box centre lies d beside it.
    img = img.rotate(-ang, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
    s = dpi / 25.4
    a = math.radians(ang)
    dx, dy = cx - (l + r) / 2 * PTMM, cy - (b + tp) / 2 * PTMM
    du, dv = dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a)
    W, H = img.size
    cw, ch = int(round(w * s)), int(round(h * s))
    left, top = int(round(W / 2 + du * s - cw / 2)), int(round(H / 2 - dv * s - ch / 2))
    canvas = Image.new('RGB', (cw, ch), (255, 255, 255))
    canvas.paste(img.crop((max(left, 0), max(top, 0), min(left + cw, W), min(top + ch, H))), (max(-left, 0), max(-top, 0)))
    return np.asarray(canvas)


# ---------------------------------------------------------------------------------------------------------- Engine
def model_dir(path=None):
    return Path(path or os.environ.get(MODEL_ENV) or DEFAULT_MODEL_DIR)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def engine_info(path=None):
    d = model_dir(path)
    info = {'package': 'rapidocr', 'models': {k: {'file': str(d / v), 'sha256': sha256_file(d / v) if (d / v).is_file() else None}
                                            for k, v in MODEL_FILES.items()}}
    for pkg in ('rapidocr', 'onnxruntime'):
        try:
            info[pkg + '_version'] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            info[pkg + '_version'] = None
    return info


class RapidEngine:
    """RapidOCR with explicit local model paths (PP-OCRv4 det/rec, cls v2.0). mode 'det': detection + reading on the crop;
    mode 'rec': reading only (the crop is the line)."""

    def __init__(self, path=None):
        d = model_dir(path)
        missing = [str(d / v) for v in MODEL_FILES.values() if not (d / v).is_file()]
        if missing:
            raise EngineUnavailable('RapidOCR model files missing: ' + ', '.join(missing) + f' (folder via {MODEL_ENV} or --models)')
        try:
            from rapidocr import OCRVersion, RapidOCR
        except ImportError as e:
            raise EngineUnavailable('Package rapidocr missing (pip install -r requirements-tools.lock): ' + str(e))
        self.info = engine_info(d)
        self.ocr = RapidOCR(params={
            'Det.model_path': str(d / MODEL_FILES['det']), 'Det.ocr_version': OCRVersion.PPOCRV4,
            'Rec.model_path': str(d / MODEL_FILES['rec']), 'Rec.ocr_version': OCRVersion.PPOCRV4,
            'Cls.model_path': str(d / MODEL_FILES['cls']), 'Cls.ocr_version': OCRVersion.PPOCRV4,
            'Det.limit_type': 'max', 'Det.limit_side_len': 960, 'Global.log_level': 'warning'})

    def read(self, img, mode='det'):
        """-> (text, confidence, lines). Lines in reading order (top to bottom, left to right)."""
        if mode == 'rec':
            r = self.ocr(img, use_det=False, use_cls=False, use_rec=True)
            txts, scores = list(r.txts or ()), list(r.scores or ())
            return ' '.join(txts), (float(np.mean(scores)) if scores else 0.0), txts
        r = self.ocr(img, use_det=True, use_cls=False, use_rec=True)
        if r.txts is None or not len(r.txts):
            return '', 0.0, []
        items = sorted(zip(r.boxes, r.txts, r.scores), key=lambda z: (round(float(np.min(z[0][:, 1])) / 20), float(np.min(z[0][:, 0]))))
        lines = [t for _, t, _ in items]
        return ' '.join(lines), float(np.mean([float(s) for _, _, s in items])), lines


# ---------------------------------------------------------------------------------------------------------- Measurement
def read_text(engine, page, t, mode):
    """Read the crop of text t. mode 'rec': tight crop (margin TIGHT_MM), reading only (line known); 'det': box + 3 mm margin
    with a white border, detection + reading; 'auto' starts with 'rec'. -> (image, text, confidence, lines, path)."""
    if mode == 'det':
        img = np.pad(render_crop(page, t, margin_mm=MARGIN_MM), ((24, 24), (24, 24), (0, 0)), constant_values=255)   # border for the box finder
        return (img, *engine.read(img, 'det'), 'det')
    img = render_crop(page, t, margin_mm=TIGHT_MM)
    return (img, *engine.read(img, 'rec'), 'rec')


def measure(pdf_path, engine, mode='auto', texts=None, crops_dir=None, glyph_have=None, limit=None):
    """Measure a run (PDF): -> result for legibility.json (without changing run files). crops_dir: folder for the crops of the
    unreadable texts (new files only) or None."""
    import pypdfium2 as pdfium
    from PIL import Image
    t0 = time.time()
    texts = pdf_texts(pdf_path) if texts is None else texts
    if limit:
        texts = texts[:limit]
    skipped = [{'text': t['text'], 'bbox_mm': [round(v, 2) for v in t['bbox']]} for t in texts if not judgeable(t['text'])]
    texts = [t for t in texts if judgeable(t['text'])]
    pdf = pdfium.PdfDocument(str(pdf_path))
    page = pdf[0]
    findings, rows = glyph_findings(texts, glyph_have), []
    n_ok = n_dig = n_dig_ok = 0
    try:
        for i, t in enumerate(texts):
            img, text, conf, lines, via = read_text(engine, page, t, mode)
            ok, sim = compare(t['text'], lines)
            if not ok and mode == 'auto':       # second chance with the context of the given text: box + 3 mm margin, detection + reading
                img, text, conf, lines, via = read_text(engine, page, t, 'det')
                ok, sim = compare(t['text'], lines)
            dig = has_digits(t['text'])
            n_ok += ok
            n_dig += dig
            n_dig_ok += (ok and dig)
            row = {'text': t['text'], 'ocr': text, 'readable': bool(ok), 'digits': dig, 'confidence': round(conf, 3), 'similarity': sim,
                   'bbox_mm': [round(v, 2) for v in t['bbox']], 'angle': t['angle'], 'cap_mm': round(t['cap'], 2), 'owner': t.get('owner'), 'via': via}
            rows.append(row)
            if not ok:
                f = {'code': 'text_unreadable', 'severity': 'warn', 'expected': t['text'], 'ocr': text, 'bbox_mm': row['bbox_mm'],
                     'owner': t.get('owner'), 'confidence': row['confidence'], 'angle': t['angle'], 'cap_mm': row['cap_mm'],
                     'message': f"Text “{t['text']}” is not legible in the crop (OCR: “{text}”)"}
                if crops_dir is not None:
                    Path(crops_dir).mkdir(parents=True, exist_ok=True)
                    cf = Path(crops_dir) / f"text-{i:04d}.png"
                    Image.fromarray(img).save(cf)
                    f['crop'] = cf.name
                findings.append(f)
    finally:
        pdf.close()
    n = len(texts)
    return {'mode': mode, 'texts': n, 'readable': n_ok,
            'rate': {'all': (n_ok / n if n else 1.0), 'digits': (n_dig_ok / n_dig if n_dig else 1.0)},
            'skipped': skipped, 'texts_digits': n_dig, 'readable_digits': n_dig_ok,
            'seconds': round(time.time() - t0, 1), 'engine': getattr(engine, 'info', None),
            'findings': findings, 'rows': rows}
