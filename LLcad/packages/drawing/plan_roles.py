"""Planner I: roles, part classes, datum, semi-finished text and tolerances. Pure functions without OCP import
(the local runner imports the module without OCP)."""
import re

ROLES = ('manufactured', 'standard', 'purchased', 'reference', 'duplicate')
FORMS = ('wire', 'square_bar', 'round_bar', 'flat_bar', 'sheet_metal', 'plate', 'tube', '3d_print', 'casting', 'other')

_NORM = re.compile(r'\b(ISO|DIN|EN)\s?(\d{3,5})\b', re.I)
_NOT = r'(?<!no )(?<!not )(?<!non-)'      # a negated marker ("no purchased part") does not count
# Markers in the part name; "Print:" at the start of the name or of a segment "material · Print: ..."
_RULES = (('purchased', re.compile(_NOT + r'(?:purchased part|bought-in)', re.I)),
          ('standard', re.compile(_NOT + r'\bstandard part', re.I)),
          ('reference', re.compile(r'reference|do not print', re.I)),
          ('duplicate', re.compile(r'(?:^|·)\s*Print\s*:|print bed|print layout', re.I)))
# ISO 286: number not directly after a letter/digit/period/comma, no x/X directly after it (size 4x12), grade 1-18
_FIELDS = 'za|zb|zc|cd|ef|fg|js|[abcdefghjkmnprstuvxyz]'
_NORM_REF = re.compile(r'\b(?:ISO|DIN|EN)\s*\d+(?:[-:.]\d+)*', re.I)   # standard designation including its number
_TOL = re.compile(r'(?<![A-Za-z0-9.,])(\d+(?:[.,]\d+)?)(?![xX])\s?((?:%s)(?:1[0-8]|[1-9]))\b' % _FIELDS, re.I)


def _listed(text, words):
    """Hit of a single word or word group of the keyword list: whole word, case-insensitive, negated
    ("no PN-1001") does not count. Not a regex: the entry is taken literally."""
    for w in words or []:
        w = ' '.join(str(w).split())
        if w and re.search(_NOT + r'(?<!\w)' + r'\s+'.join(re.escape(t) for t in w.split()) + r'(?!\w)', text or '', re.I):
            return w
    return None


def listed_role(name, description, part_roles):
    """(role, entry) from the maintainable keyword list {"purchased": [...], "standard": [...]} or (None, None)."""
    if isinstance(part_roles, dict):
        for role in ('purchased', 'standard'):
            for text in (name, description):
                w = _listed(text, part_roles.get(role) if isinstance(part_roles.get(role), list) else None)
                if w:
                    return role, w
    return None, None


def regex_role(name, description=None, part_roles=None):
    """Role from the part name or None. 'EN 10xxx' is a material standard, not a standard part. Only markers of the
    part itself (name, description) - form feature names cite standards of their features (countersink DIN 74) and
    print layouts ("Print: ..." on printed manufactured parts)."""
    name = name or ''
    for m in _NORM.finditer(name):
        if not (m.group(1).upper() == 'EN' and re.fullmatch(r'10\d{3}', m.group(2))):
            return 'standard'
    for role, rx in _RULES:
        if rx.search(name):
            return role
    for role, rx in _RULES[:2]:      # description is free text: word markers only, no standard numbers, no "reference"
        if rx.search(description or ''):
            return role
    return listed_role(name, description, part_roles)[0]


def congruent(a, b, rel=1e-3):
    """Like features.congruent (repeated here so the module stays importable without OCP)."""
    return len(a) == len(b) and all(abs(x - y) <= rel * max(abs(x), abs(y)) + 1e-9 for x, y in zip(a, b))


def roles(parts, facts_roles, fingerprints, part_roles=None):
    """({part id: role}, [note]): name rules, keyword list (part_roles), facts_roles before 'manufactured'. 'duplicate' only
    applies with a congruent non-duplicate (fingerprint), otherwise 'manufactured' with a note."""
    out = {}
    for p in parts:
        r = regex_role(p.get('name'), p.get('description'), part_roles) or (facts_roles or {}).get(p['id'])
        out[p['id']] = r if r in ROLES else 'manufactured'
    notes, demote = [], []
    for p in parts:
        pid = p['id']
        if out[pid] != 'duplicate':
            continue
        fp = fingerprints.get(pid)
        if not (fp and any(o != pid and out[o] != 'duplicate' and fingerprints.get(o) and congruent(fp, fingerprints[o])
                           for o in out)):
            demote.append(pid)
            notes.append(f"Part \"{p.get('name') or pid}\" named as duplicate, but no congruent part: will be manufactured")
    for pid in demote:      # switch only after the check so duplicates do not support each other
        out[pid] = 'manufactured'
    return out, notes


def part_class(part):
    """wire_bend | turned | plate | prismatic from an analyze part dict."""
    if any(b.get('complete') for b in part.get('bend_tables') or []):
        return 'wire_bend'
    size = sorted(part['bbox']['size'])
    cyls = (part.get('holes') or []) + (part.get('shafts') or [])
    shafts = part.get('shafts') or []
    if cyls and shafts:
        a = cyls[0]['axis']
        coaxial = all(abs(abs(sum(x * y for x, y in zip(a, c['axis']))) - 1) < 1e-4 and _off_axis(cyls[0], c) < 1e-3
                      for c in cyls)
        if coaxial:
            k = max(range(3), key=lambda i: abs(a[i]))
            trans = max(s for i, s in enumerate(part['bbox']['size']) if i != k)
            if max(c['diameter'] for c in shafts) >= 0.98 * trans:
                return 'turned'
    if size[0] <= 0.2 * (size[1] + size[2]) / 2:
        return 'plate'
    return 'prismatic'


def _off_axis(c0, c):
    """Distance of the axis of c from the axis of c0 (axes assumed parallel)."""
    d = [x - y for x, y in zip(c['start'], c0['start'])]
    t = sum(x * y for x, y in zip(d, c0['axis']))
    return sum((x - t * y) ** 2 for x, y in zip(d, c0['axis'])) ** 0.5


def datum(bbox, features):
    """Datum per axis x/y/z: origin on a bounding-box face, otherwise the face (min/max) with the most
    feature coordinates within <= 10 % of the edge length, otherwise min."""
    out = {}
    for i, ax in enumerate('xyz'):
        lo, hi, size = bbox['min'][i], bbox['max'][i], bbox['size'][i]
        if abs(lo) < 1e-6:
            out[ax] = lo
        elif abs(hi) < 1e-6:
            out[ax] = hi
        else:
            cs = []
            for f in features:
                if f.get('kind') == 'plane' and f.get('axis_name') == ax:
                    cs.append(f['coord'])
                elif f.get('kind') in ('hole', 'shaft') and f.get('start') and f.get('end'):
                    cs += [f['start'][i], f['end'][i]]
            n_lo = sum(1 for c in cs if abs(c - lo) <= 0.1 * size)
            n_hi = sum(1 for c in cs if abs(c - hi) <= 0.1 * size)
            out[ax] = hi if n_hi > n_lo else lo
    return out


def _num(v):
    s = f'{float(v):.4f}'.rstrip('0').rstrip('.')
    return s.replace('.', ',')


def semi_finished(material, form, dims, table):
    """Semi-finished text from the table entry {material, form, text}; placeholders {d} {a} {t} with decimal comma.
    No hit or missing dimension: None (never invent a standard)."""
    if not material or not form:
        return None
    norm = lambda s: str(s).strip().lower()
    for e in table or []:
        if norm(e.get('material', '')) == norm(material) and norm(e.get('form', '')) == norm(form):
            text, ok = e.get('text') or '', True
            for k in set(re.findall(r'\{(\w+)\}', text)):
                if dims.get(k) is None:
                    ok = False
                    break
                text = text.replace('{' + k + '}', _num(dims[k]))
            if ok and text:
                return text
    return None


def stock_text(material, form, dims, table):
    """Semi-finished text of the parts list column "Semi-finished product / standard": like semi_finished; if the model text does not name the form
    (no "3D print", "square bar" ...), the material alone leads to the entry if the table knows exactly one form for it and
    that is a printing material (PETG, PLA: entry of the form 3d_print). No material, no entry: None (invent nothing)."""
    got = semi_finished(material, form, dims, table)
    if got or form or not material:
        return got
    return semi_finished(material, '3d_print', dims, table)


def tolerance_spans(text):
    """[(nominal size, 'h11', start, end)] in the text; standard designations are blanked out beforehand at equal length so
    that start/end point into the original text."""
    t = _NORM_REF.sub(lambda m: ' ' * len(m.group(0)), text or '')
    return [(float(m.group(1).replace(',', '.')), m.group(2), m.start(), m.end()) for m in _TOL.finditer(t)]


def parse_tolerances(texts):
    """[(nominal size, 'h11')] from free texts."""
    return [(v, f) for t in texts for v, f, _, _ in tolerance_spans(t)]
