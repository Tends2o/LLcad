"""Facts without a language model: material, form, tolerances, role, quantity hint and designation of a
part, derived deterministically from its model texts. Every value appears (normalized) verbatim in the text, otherwise None.
Pure functions without OCP: the local runner (verify.py) imports normalize, verbatim and regex_facts.

Sources per part: name = full part name (semantic_name, segments "material #color · ... · [Item n ]designation"),
purpose = part description, texts = names of the part's form features. Material from name, otherwise purpose; role and
designation only from name or purpose (form feature names cite standards of their features, "countersink DIN 74", and
print layouts on manufactured parts); form from name, purpose, texts (first text with a keyword); tolerances from
all texts. Materials: material number 1.xxxx before short name X... before plastic before metal (whole words only,
capitalized as in the benchmark texts; metal optionally with finish "anodized" ...)."""
import re

import plan_roles

FIELDS = ('material', 'form', 'tolerances', 'role', 'quantity_hint', 'designation')
_COLOR = re.compile(r'#[0-9A-Fa-f]{6}\b|\bRAL\s?\d{4}\b')
MATERIALS = (
    re.compile(r'\b1\.\d{4}\b'),                                                       # material number
    re.compile(r'\bX\d+[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*'),                        # short name X5CrNi18-10
    re.compile(r'\b(?:PETG|PLA|PP|PE|POM|PA\d{0,2}|ABS|PC|PMMA|TPU|LCP|PUR)\b'),     # plastics
    re.compile(r'\b(?:Stainless steel|Spring steel|Steel|Brass|Aluminium|Aluminum|Alu|NdFeB)'          # metals (benchmark texts)
               r'(?:\s(?:anodized|nickel-plated|zinc-plated|chrome-plated|blackened))?\b'),
)
# Stock form from keywords (whole words, ranked within one text)
FORM_WORDS = (('3d_print', r'3D[- ]print\w*|print part\w*|printed\w*|FDM|(?<!not )print(?!\s*:|\s+bed|\s+layout)'),
              ('wire', r'(?:spring(?: steel)? )?wire'),
              ('square_bar', r'square bar\w*'),
              ('tube', r'tube|pipe'),
              ('sheet_metal', r'sheet metal(?: part)?'),
              ('flat_bar', r'flat (?:bar|steel|stock)'),
              ('round_bar', r'round (?:bar|steel|stock)'),
              ('plate', r'plate'),
              ('casting', r'casting|cast'))
_FORMS = [(f, re.compile(r'(?<!\w)(?:' + rx + r')(?!\w)', re.I)) for f, rx in FORM_WORDS]
# Form feature keyword directly before a tolerance ("square bar 8 h11"): belongs to the entry (plan_dims assigns by it)
_TOL_KEY = re.compile(r'(?:\b(?:square bar|hole|bore|shaft|spigot|pin|bolt)\s*|[Ø□]\s*)$', re.I)
_QTY = (re.compile(r'(?<![\w.,])(\d+)\s*[xX×](?!\w)'), re.compile(r'(?<![\w.,])[xX×]\s*(\d+)(?![\w.,])'))   # like features.quantity
_QTY_SPAN = re.compile(r'\(\s*\d+\s*[xX×]\s*\)|(?<![\w.,])\d+\s*[xX×](?!\w)|(?<![\w.,])[xX×]\s*\d+(?![\w.,])')
_POS = re.compile(r'^\s*Item\s*\d+\b\.?\s*')
_MARK = r'(?:purchased part\w*|bought-in\w*|standard part|reference|do not print|print|print part|assumption)'
_MARK_PAREN = re.compile(r'\(\s*' + _MARK + r'(?:\s*,\s*' + _MARK + r')*\s*\)', re.I)


def normalize(s):
    """casefold, NBSP/narrow spaces -> space, "×" -> "x", decimal comma = decimal point, whitespace collapsed."""
    s = '' if s is None else str(s)
    s = s.replace(' ', ' ').replace(' ', ' ').replace(' ', ' ').replace('×', 'x')
    s = re.sub(r'(?<=\d),(?=\d)', '.', s)
    return re.sub(r'\s+', ' ', s).strip().casefold()


def verbatim(value, source):
    """Does value appear normalized in source (text or list of texts, each text separately)?"""
    v = normalize(value)
    if not v:
        return False
    texts = [source] if isinstance(source, str) else list(source or [])
    return any(v in normalize(t) for t in texts)


def _material(text):
    for rx in MATERIALS:
        m = rx.search(text or '')
        if m:
            return m.group(0)
    return None


def _form(texts):
    for t in texts:
        for form, rx in _FORMS:
            if rx.search(t or ''):
                return form
    return None


def _tolerances(texts):
    out = []
    for t in texts:
        t = t or ''
        masked = _COLOR.sub(lambda m: ' ' * len(m.group(0)), t)
        for _, _, s, e in plan_roles.tolerance_spans(masked):
            k = _TOL_KEY.search(t[:s])
            val = t[k.start() if k else s:e].strip()
            if val and len(val) <= 40 and val not in out and verbatim(val, t):
                out.append(val)
    return out[:8]


def _quantity(name):
    for rx in _QTY:
        m = rx.search(name or '')
        if m:
            return int(m.group(1))
    return None


def _fragments(seg):
    """Contiguous pieces of a segment without color code, material, quantity, "Item n" and marker parentheses."""
    cut = [False] * len(seg)
    for rx in (_COLOR, _QTY_SPAN, _MARK_PAREN, _POS, *MATERIALS):
        for m in rx.finditer(seg):
            for i in range(m.start(), m.end()):
                cut[i] = True
    out, cur = [], ''
    for ch, c in zip(seg, cut):
        if c:
            out.append(cur)
            cur = ''
        else:
            cur += ch
    out.append(cur)
    clean = []
    for f in out:
        f = f.strip(' -–,;:/')
        if f.startswith('(') and ')' not in f:
            f = f[1:]
        if f.endswith(')') and '(' not in f:
            f = f[:-1]
        f = f.strip(' -–,;:/')
        if f and f not in ('()',) and re.search(r'[^\W\d_]', f):
            clean.append(f)
    return clean


def _designation(name):
    """Designation = longest contiguous remainder of the last segment that still has text after removal
    (pattern of the real part names: "material #color · [material #color · ][Item n ]designation")."""
    for seg in reversed([s for s in re.split(r'\s*·\s*', name or '') if s.strip()]):
        frags = _fragments(seg)
        if frags:
            b = max(frags, key=len)[:120].strip()
            return b if verbatim(b, name) else None
    return None


def regex_facts(name, purpose='', texts=()):
    """{material, form, tolerances, role, quantity_hint, designation} from the model texts of a part (module doc). Every
    value verbatim in the text, otherwise None; tolerances = list (empty if none), quantity_hint = int or None."""
    name, purpose, texts = str(name or ''), str(purpose or ''), [str(t) for t in texts or () if t]
    material = _material(name) or _material(purpose)
    out = {'material': material if material and verbatim(material, [name, purpose]) else None,
           'form': _form([name, purpose, *texts]),
           'tolerances': _tolerances([name, purpose, *texts]),
           'role': plan_roles.regex_role(name) or (plan_roles.regex_role(purpose) if purpose else None),
           'quantity_hint': _quantity(name),
           'designation': _designation(name)}
    return out


# ---------------------------------------------------------------- functional dimensions from the model texts
_NUM = r'(\d+(?:[.,]\d+)?)'
_NORM = re.compile(r'\b(?:ISO|DIN|EN|VDI|VDE|IEC)\s*[\d-]+(?:[-/:]\d+)*|\bM\d+(?:[.,]\d+)?(?:\s*[xX×]\s*\d+(?:[.,]\d+)?)?|\bItem\s*\d+|\d+/\d+\s*(?:["″]|\x27\x27|-\s*\d+\s*UN[CF]?)|\b(?!R\d)[A-Za-z]{1,2}\d+(?:[.,]\d+)?\b'
                   r'|\b[hHjJkKgGfF][1-9]\d?\b|\b\d+(?:\.\d+)?\s*(?:°|%)')
_WORDS = r'(?:\w*(?:height|length|width|depth|thickness|distance|spacing|pitch|gap|offset)|length|height|width|depth|thickness)'
FUNCTION_PATTERNS = (
    ('diameter', re.compile(r'(?:[Ø⌀∅]\s*|Diameter\s*|\bD\s*=?\s*)' + _NUM)),
    ('radius', re.compile(r'(?:(?<![\w])R\s*=?\s*|Radius\s*)' + _NUM)),
    ('length', re.compile(r'(?<![\w.,])' + _WORDS + r'\s*[=:]?\s*' + _NUM, re.I)),
    ('length', re.compile(_NUM + r'\s*mm\b', re.I)),
)
_PAIR = re.compile(r'(?<![\w.,])' + _NUM + r'\s*/\s*' + _NUM + r'(?![\w.,])')
_COUNT = re.compile(r'(?<![\w.,])(\d+)\s*[xX×]\s*' + _NUM + r'(?![\w.,])')
# "4×2" (digit, ×, digit, no space) is a length pair (triangle 4×2 mm), "4 × 2" and "4× Ø5" a count
_LENGTHS = re.compile(r'(?<![\w.,])' + _NUM + r'[xX×]' + _NUM + r'(?![\w.,×xX])')
# "218→56 mm" (height/width from ... to ...): both numbers are lengths, also the first one without a unit of its own
_ARROW = re.compile(r'(?<![\w.,])' + _NUM + r'\s*(?:→|->|⇒|⟶)\s*' + _NUM + r'\s*mm\b', re.I)


def _num(t):
    return float(t.replace(',', '.'))


def function_values(texts):
    """Numbers with a kind from model, part and form feature names: [{value, kind, count, text}], once per (value, kind,
    count), in text order. kind = diameter (Ø7, Diameter 7), radius (R8.6, Radius), length (height/peg height/length/...
    14.9; 12 mm), otherwise any ("218/56": two values; "4 × 2": count 4, value 2). Standard/material/color/thread
    designations (ISO 286, M3, #aabbcc, h11) and percentages do not count. Pure function; plan_dims checks the values
    against the geometry, never against the text."""
    out, seen = [], set()
    for text in texts or []:
        t = str(text or '')
        t = _COLOR.sub(lambda m: ' ' * len(m.group(0)), t)
        t = _NORM.sub(lambda m: ' ' * len(m.group(0)), t.replace(' ', ' '))
        taken = [False] * len(t)

        def take(m, kind, value, count=None):
            if any(taken[m.start():m.end()]):
                return
            for i in range(m.start(), m.end()):
                taken[i] = True
            key = (round(value, 4), kind, count)
            if value > 0 and key not in seen:
                seen.add(key)
                out.append({'value': value, 'kind': kind, 'count': count, 'text': t[m.start():m.end()].strip()})
        for kind, rx in FUNCTION_PATTERNS[:2]:
            for m in rx.finditer(t):
                take(m, kind, _num(m.group(1)))
        for m in _ARROW.finditer(t):
            take(m, 'length', _num(m.group(1)))
            for i in range(m.start(), m.end()):
                taken[i] = False
            take(m, 'length', _num(m.group(2)))
        for m in _LENGTHS.finditer(t):
            if not any(taken[m.start():m.end()]):
                take(m, 'any', _num(m.group(1)))
                for i in range(m.start(), m.end()):
                    taken[i] = False
                take(m, 'any', _num(m.group(2)))
        for m in _COUNT.finditer(t):
            take(m, 'any', _num(m.group(2)), int(m.group(1)))
        for m in _PAIR.finditer(t):
            if not any(taken[m.start():m.end()]):
                take(m, 'any', _num(m.group(1)))
                for i in range(m.start(), m.end()):
                    taken[i] = False
                take(m, 'any', _num(m.group(2)))
        for kind, rx in FUNCTION_PATTERNS[2:]:
            for m in rx.finditer(t):
                take(m, kind, _num(m.group(1)))
    return out

