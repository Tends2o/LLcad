#!/usr/bin/env python3
"""LLcad Reference Pack: measured references for one product before modelling starts.

  ingest  --out DIR [options] SOURCE...   URLs and/or local files (images, mp4, PDF, SVG)
  load    --pack DIR/pack.json --model ID  bind the pack to a model (cad_reference_setup)

Only the given URLs and files are used: a page URL is fetched once and the media it
links directly are downloaded (largest srcset size); nothing else is crawled.
Run with the LLcad venv: /root/projects/LLcad/.venv/bin/python tools/reference-pack/ingest.py
See tools/reference-pack/README.md."""
import argparse, csv, hashlib, json, os, re, sys, time, urllib.request, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from PIL import Image
import rp_image, rp_pdf, rp_sources, rp_video, rp_sam3

SCHEMA = 'llcad-reference-pack-1'
TOOL_VERSION = '1.0.0'


def log(msg):
    print(f'[pack {time.strftime("%H:%M:%S")}] {msg}', file=sys.stderr, flush=True)


def parse_dims(text):
    out = {}
    for part in (text or '').split(','):
        if '=' in part:
            k, v = part.split('=', 1)
            out[k.strip()] = float(v)
    return out


def nearest(value, candidates):
    if not candidates:
        return None
    best = min(candidates, key=lambda d: abs(d['value_mm'] - value))
    return {'dimension': best['id'], 'stated_mm': best['value_mm'], 'measured_mm': round(value, 4),
            'residual_mm': round(value - best['value_mm'], 4), 'residual_pct': round(100 * (value - best['value_mm']) / best['value_mm'], 3)}


def compare_csv(profile_csv, spec):
    """Cross-check a width table against a supplied one: 'path[:column]' (';' or ',' CSV,
    first column = height in mm)."""
    path, _, col = spec.partition(':')
    ref = {}
    with open(path) as f:
        dialect = csv.Sniffer().sniff(f.read(2048), delimiters=';,'); f.seek(0)
        rows = list(csv.reader(f, dialect))
    header = rows[0]
    ci = header.index(col) if col in header else len(header) - 1
    for r in rows[1:]:
        try:
            ref[round(float(r[0]), 1)] = float(r[ci])
        except (ValueError, IndexError):
            pass
    mine = {}
    with open(profile_csv) as f:
        for r in list(csv.reader(f, delimiter=';'))[1:]:
            mine[round(float(r[0]), 1)] = float(r[1])
    zs = sorted(set(ref) & set(mine))
    if not zs:
        return None
    zmax = max(zs)
    d = np.array([mine[z] - ref[z] for z in zs if 2 <= z <= zmax - 2])
    if not len(d):
        return None
    return {'against': path, 'column': header[ci], 'rows': int(len(d)), 'median_mm': round(float(np.median(d)), 4),
            'mean_abs_mm': round(float(np.abs(d).mean()), 4), 'p95_abs_mm': round(float(np.percentile(np.abs(d), 95)), 4),
            'max_abs_mm': round(float(np.abs(d).max()), 4)}


def contact_sheet(paths, out, cols=4, thumb=360):
    ims = []
    for p in paths[:16]:
        try:
            im = Image.open(p).convert('RGB'); im.thumbnail((thumb, thumb)); ims.append(im)
        except Exception:
            pass
    if not ims:
        return None
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * thumb, rows * thumb), 'white')
    for i, im in enumerate(ims):
        sheet.paste(im, ((i % cols) * thumb, (i // cols) * thumb))
    sheet.save(out)
    return str(out)


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def ingest(a):
    t0 = time.time()
    out = Path(a.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    for sub in ('sources', 'references', 'contours', 'logos', 'masks'):
        (out / sub).mkdir(exist_ok=True)
    warnings = []
    sources = rp_sources.collect(a.inputs, out / 'sources', log, a.max_page_items)
    log(f'{len(sources)} sources')
    dims, weights = [], []
    for s in sources:
        if s['kind'] == 'pdf':
            try:
                d, w = rp_pdf.dimensions(rp_pdf.text_of(s['path']), s['id'])
                dims += d; weights += w
            except Exception as e:
                warnings.append(f'{s["id"]}: pdftotext failed: {e}')
    for name, value in parse_dims(a.dims).items():
        dims.append({'name': name, 'role': name, 'value_mm': value, 'tolerance_mm': 0.5, 'class': 'A',
                     'source': 'user', 'evidence': f'--dims {name}={value}'})
    # Roles: a stated product triple is read as length x width x depth (common datasheet order).
    for d in dims:
        if 'role' not in d and d.get('triple_size') == 3:
            d['role'] = ['length', 'width', 'depth'][d['order_in_triple']]
    for i, d in enumerate(dims[:32]):
        d['id'] = f'dim-{i + 1:02d}' + (f'-{d["role"]}' if d.get('role') else '')
    if len(dims) > 32:
        warnings.append(f'{len(dims) - 32} dimensions beyond the 32-dimension budget dropped')
        dims = dims[:32]
    height = a.height_mm or next((d['value_mm'] for d in dims if d.get('role') in ('height', 'length') and d['source'] == 'user'), None) \
        or next((d['value_mm'] for d in dims if d.get('role') == 'length'), None)
    if height:
        log(f'scale reference: height {height} mm')
    else:
        warnings.append('No height/length dimension: silhouettes stay in pixels (pass --height-mm or --dims length=...).')
    others = [d for d in dims if d.get('role') in ('width', 'depth')] or [d for d in dims if d['value_mm'] != height]

    references, contours, colours, markings, landmarks = [], [], [], [], []
    hull, video_meta = None, []

    def add_ref(source, source_type, path, width, height_px, mask=None, view=None, extra=None):
        rid = f'ref-{len(references) + 1:02d}-{source_type.replace("_", "-")}' + (f'-{view}' if view else '')
        references.append({'id': rid, 'source_id': source['id'], 'source_type': source_type, 'path': str(path),
                           'width': width, 'height': height_px, 'mask': mask, 'view': view, **(extra or {})})
        return rid

    def add_profile(rid, profile, view):
        if not profile:
            return
        # Turntable views know their orientation; a render of unknown view is
        # checked against the nearest horizontal dimension.
        wanted = {'front': ('width',), 'side': ('depth',)}.get(view, ('width', 'depth'))
        cross = nearest(profile['max_width_mm'], [d for d in others if (d.get('role') or 'width') in wanted] or others)
        entry = {'id': f'contour-{len(contours) + 1:02d}', 'reference_id': rid, 'kind': 'width_per_height',
                 'orientation': 'z up from the product base, CCW table rows', 'scaled_to_mm': height, 'class': 'B', **profile,
                 'cross_check': cross}
        if a.compare_csv and view == 'front':
            entry['compare'] = compare_csv(profile['csv'], a.compare_csv)
        contours.append(entry)

    def add_colours(rid, cols):
        for c in cols:
            colours.append({'id': f'colour-{len(colours) + 1:02d}', 'reference_id': rid, **c,
                            'note': 'lit-pixel median of a k-means region; photo lighting biases L*'})

    for s in sources:
        try:
            if s['kind'] == 'pdf':
                for r in rp_pdf.alpha_renders(s['path'], out / 'references', s['id']):
                    rid = add_ref(s, 'datasheet_alpha_render', r['path'], r['width'], r['height'],
                                  extra={'page': r['page'], 'coverage': r['coverage']})
                    info, profile, cols = rp_image.measure_still(r['path'], out / 'masks', rid, height)
                    references[-1]['mask'] = info['mask']
                    add_profile(rid, profile, None)
                    add_colours(rid, cols)
                for page in rp_pdf.svg_pages(s['path'], out / 'logos', s['id']):
                    for m in rp_pdf.vector_logos(page, out / 'logos', s['id']):
                        markings.append({'id': f'marking-{len(markings) + 1:02d}', 'kind': 'vector_logo_candidate', **m})
            elif s['kind'] == 'svg':
                for m in rp_pdf.vector_logos(s['path'], out / 'logos', s['id']):
                    markings.append({'id': f'marking-{len(markings) + 1:02d}', 'kind': 'vector_logo_candidate', **m})
            elif s['kind'] == 'image':
                im = Image.open(s['path'])
                rid = add_ref(s, 'closeup' if max(im.size) >= 1800 else 'photo', s['path'], im.width, im.height)
                cut_out = 'A' in im.getbands()
                info, profile, cols = rp_image.measure_still(s['path'], out / 'masks', rid, height if (cut_out and a.scale_cutouts) else None)
                references[-1]['mask'] = info['mask']; references[-1]['mask_method'] = info['mask_method']
                add_profile(rid, profile, 'front')
                add_colours(rid, cols)
            elif s['kind'] == 'video':
                meta = rp_video.probe(s['path'])
                rows, stats = rp_video.scan(s['path'], meta)
                segs = rp_video.segments(rows, stats['scan_size'][1])
                turn = [g for g in segs if g['turntable']]
                video_meta.append({'source_id': s['id'], **meta, **stats, 'segments': segs,
                                   'backgrounds': {'white': sum(r['white'] for r in rows), 'dark': sum(r['dark'] for r in rows)}})
                log(f'{s["id"]}: {stats["frames"]} frames scanned in {stats["scan_s"]} s, {len(segs)} segments, {len(turn)} turntable')
                if not turn and segs:
                    warnings.append(f'{s["id"]}: white-background shots without rotation only')
                if not segs:
                    warnings.append(f'{s["id"]}: no white-background turntable segment (dark/galaxy backgrounds need hosted matting, e.g. --sam3)')
                # The zoom differs between shots: every segment is scaled on its own.
                for n, g in enumerate(turn[:a.max_segments], 1):
                    for view, which in (('front', 'min_width'), ('side', 'max_width')):
                        rid = f'ref-{len(references) + 1:02d}-turntable-{n}-{view}'
                        rec, profile, cols = rp_video.canonical(s['path'], meta, g, which, height, out / 'references', rid)
                        references.append({'id': rid, 'source_id': s['id'], 'source_type': 'turntable_frame', 'view': view,
                                           'segment': n, **rec})
                        if profile:
                            references[-1]['px_per_mm'] = profile['px_per_mm']
                            references[-1]['camera_hint'] = rp_video.camera_hint(profile, meta, view)
                            landmarks.append({'id': f'lm-{rid}-base', 'reference_id': rid, 'name': 'base centre',
                                              'px': [profile['axis_x_px_mean'], profile['bottom_px']], 'point_mm': [0, 0, 0], 'signed': False})
                            landmarks.append({'id': f'lm-{rid}-top', 'reference_id': rid, 'name': 'top on axis',
                                              'px': [profile['axis_x_px_mean'], profile['top_px']], 'point_mm': [0, 0, height], 'signed': False})
                        add_profile(rid, profile, view)
                        add_colours(rid, cols)
                if a.hull and turn and height:
                    # The segment that turns the most: duration over the front-to-side
                    # quarter turn (a near-static shot has a long "quarter").
                    def coverage(g):
                        q = abs(g['max_width']['t'] - g['min_width']['t'])
                        return (g['t1'] - g['t0']) / q * 90 if q >= 0.3 else 0
                    ranked = sorted(((coverage(g), n) for n, g in enumerate(turn[:a.max_segments], 1)), reverse=True)
                    if ranked and ranked[0][0] >= 120:
                        n = ranked[0][1]
                        front = next((c for c in contours if c['reference_id'].endswith(f'turntable-{n}-front')), None)
                        if front:
                            hull = rp_video.visual_hull(s['path'], meta, turn[n - 1], height, front['px_per_mm'], front['axis_x_px_mean'],
                                                        out / 'masks', views=a.hull_views, voxel_mm=a.hull_voxel_mm)
                            hull.update(source_id=s['id'], segment=n, estimated_rotation_deg=round(ranked[0][0], 1))
                    else:
                        hull = {'status': 'skipped', 'reason': 'no segment turns at least 120 degrees at a steady speed'}
        except Exception as e:
            warnings.append(f'{s["id"]} ({s["kind"]}): {type(e).__name__}: {e}')
            log(f'error on {s["id"]}: {e}')
    # Asymmetric (signed) landmarks cannot be found without knowing the product: placeholders.
    for r in references:
        if r['source_type'] in ('turntable_frame', 'photo', 'datasheet_alpha_render'):
            for k in (1, 2):
                landmarks.append({'id': f'lm-{r["id"]}-asym-{k}', 'reference_id': r['id'], 'name': f'asymmetric landmark {k} (e.g. button side, lens facing, text start)',
                                  'px': None, 'point_mm': None, 'feature_hint': None, 'signed': True, 'placeholder': True})
    segmentation = {'provider': 'fal-sam3', 'enabled': bool(a.sam3), 'results': []}
    if a.sam3:
        concepts = [c.strip() for c in (a.concepts or ','.join(rp_sam3.CONCEPTS)).split(',') if c.strip()]
        for r in references[:a.sam3_max_images]:
            segmentation['results'].append({'reference_id': r['id'], **rp_sam3.segment(r['path'], concepts, out / 'masks', r['id'], log)})
    sheet = contact_sheet([r['path'] for r in references], out / 'contact-sheet.jpg')
    pack = {
        'schema': SCHEMA, 'tool_version': TOOL_VERSION, 'name': a.name or out.name,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'elapsed_s': round(time.time() - t0, 2),
        'scope': {'in': ['visible exterior', 'main internals'], 'out': ['variants', 'accessories', 'states'],
                  'note': 'Variants, states and accessories only on explicit request, then as IR configurations. Every class C item goes on the assumptions list.'},
        'tolerance_classes': {'A': 'stated by the manufacturer or measured on a scaled orthographic source',
                              'B': 'derived (silhouette scaled to a class-A dimension)', 'C': 'estimated'},
        'sources': sources, 'dimensions': dims, 'weights': weights, 'references': references, 'contours': contours,
        'colours': colours, 'markings': markings, 'landmarks': landmarks, 'videos': video_meta, 'hull': hull,
        'segmentation': segmentation, 'contact_sheet': sheet, 'warnings': warnings,
    }
    pack['hash'] = hashlib.sha256(canonical_json({k: v for k, v in pack.items() if k not in ('created_at', 'elapsed_s')}).encode()).hexdigest()
    (out / 'pack.json').write_text(json.dumps(pack, indent=1, ensure_ascii=False))
    log(f'pack written: {out / "pack.json"} ({len(references)} references, {len(dims)} dimensions, {len(contours)} contours, '
        f'{len(colours)} colours, {len(markings)} markings) in {pack["elapsed_s"]} s')
    print(json.dumps({'pack': str(out / 'pack.json'), 'hash': pack['hash'], 'references': len(references), 'dimensions': len(dims),
                      'contours': [{k: c.get(k) for k in ('reference_id', 'px_per_mm', 'max_width_mm', 'cross_check', 'compare')} for c in contours],
                      'warnings': warnings, 'elapsed_s': pack['elapsed_s']}, indent=1))


# --- load: bind a pack to a model ----------------------------------------------------
def http(url, token, tool, body, timeout=300):
    req = urllib.request.Request(url.rstrip('/') + '/api/tools/' + tool, data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + token, 'content-type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def upload(url, token, data):
    req = urllib.request.Request(url.rstrip('/') + '/api/uploads', data=data,
                                 headers={'Authorization': 'Bearer ' + token, 'content-type': 'application/octet-stream'})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def load(a):
    pack = json.loads(Path(a.pack).read_text())
    assert pack.get('schema') == SCHEMA, 'not a reference pack'
    url = a.url or os.environ.get('LLCAD_URL', 'http://127.0.0.1:4310')
    token = Path(a.token_file or os.environ.get('LLCAD_TOKEN_FILE', '/root/projects/LLcad/data/local-token')).read_text().strip()
    axis = {k: v for k, v in (p.split('=') for p in a.axis.split(','))}
    chosen = [r for r in pack['references'] if not a.refs or r['id'] in a.refs.split(',')][:12]
    refs = []
    for r in chosen:
        src = next((s for s in pack['sources'] if s['id'] == r['source_id']), {})
        data = Path(r['path']).read_bytes()
        if len(data) > 16 * 1024 * 1024:
            log(f'skip {r["id"]}: larger than 16 MiB'); continue
        entry = {'id': r['id'][:80], 'label': f'{r["source_type"]}{" " + r["view"] if r.get("view") else ""} ({Path(r["path"]).name})'[:200],
                 'source': (f'{r["source_type"]} | {src.get("url") or src.get("path")} | sha256 {src.get("sha256", "")[:16]} | '
                            f'pack {pack["hash"][:12]} | {src.get("licence_note", "")}')[:1000]}
        cam = r.get('camera_hint')
        if cam and a.cameras:
            entry['camera'] = {k: v for k, v in cam.items() if k != 'convention'}
            # Registered only on explicit request: the camera assumes the pack's frame
            # convention (base centre at the origin, +Z up, front -Y).
            entry['registered'] = bool(a.register and r.get('view') == 'front' or (a.register_side and r.get('view') == 'side'))
        if not a.dry_run:
            entry['artifact_id'] = upload(url, token, data)['artifact_id']
        else:
            entry['artifact_id'] = 'art-dry-run'
        refs.append(entry)
    dims = []
    for d in pack['dimensions']:
        role = d.get('role')
        if role in axis and axis[role] in ('x', 'y', 'z') and not any(x['axis'] == axis[role] for x in dims):
            tol = max(a.dim_tolerance_min, a.dim_tolerance_pct / 100 * d['value_mm'])
            dims.append({'id': d['id'][:80], 'axis': axis[role], 'value_mm': d['value_mm'], 'tolerance_mm': round(tol, 3),
                         'source': f'{d["class"]} {d.get("evidence", "")} ({d["source"]}; pack {pack["hash"][:12]})'[:500]})
    requirements = json.loads(Path(a.requirements).read_text()) if a.requirements else []
    body = {'references': refs, 'dimensions': dims, 'requirements': requirements, 'acceptance': a.acceptance}
    if a.dry_run:
        print(json.dumps(body, indent=1)); return
    head = http(url, token, 'cad_get_model', {'model_id': a.model})
    report = http(url, token, 'cad_reference_report', {'model_id': a.model})
    body.update({'model_id': a.model, 'base_revision': head['revision'], 'idempotency_key': 'pack-' + uuid.uuid4().hex,
                 'expected_contract_hash': report.get('contract_hash') if report.get('reference', {}).get('status') != 'not_configured' else None})
    result = http(url, token, 'cad_reference_setup', body)
    print(json.dumps({k: result.get(k) for k in ('status', 'model_id', 'contract_hash', 'errors')}, indent=1))
    if result.get('status') == 'failed':
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    i = sub.add_parser('ingest', help='build a pack from URLs and files')
    i.add_argument('inputs', nargs='+')
    i.add_argument('--out', required=True)
    i.add_argument('--name')
    i.add_argument('--height-mm', type=float, help='product height/length the silhouettes are scaled to (default: datasheet length)')
    i.add_argument('--dims', help='user-declared dimensions, e.g. length=180,width=39,depth=53 (class A)')
    i.add_argument('--compare-csv', help='cross-check the front width table against PATH[:COLUMN]')
    i.add_argument('--max-segments', type=int, default=3)
    i.add_argument('--max-page-items', type=int, default=60)
    i.add_argument('--scale-cutouts', action='store_true', help='also scale cut-out (alpha) images to the height')
    i.add_argument('--hull', action='store_true', help='visual hull from the longest turntable segment (outer bound only)')
    i.add_argument('--hull-views', type=int, default=36)
    i.add_argument('--hull-voxel-mm', type=float, default=0.5)
    i.add_argument('--sam3', action='store_true', help='hosted SAM 3 concept masks (needs LLCAD_FAL_KEY; images leave the machine)')
    i.add_argument('--concepts', help='comma-separated SAM 3 concept prompts')
    i.add_argument('--sam3-max-images', type=int, default=12)
    l = sub.add_parser('load', help='bind a pack to a model with cad_reference_setup')
    l.add_argument('--pack', required=True)
    l.add_argument('--model', required=True)
    l.add_argument('--url'); l.add_argument('--token-file')
    l.add_argument('--refs', help='comma-separated reference ids (default: first 12)')
    l.add_argument('--axis', default='length=z,width=x,depth=y', help='dimension role -> model axis')
    l.add_argument('--cameras', action='store_true', help='attach the absolute camera hints (pack frame convention)')
    l.add_argument('--register', action='store_true', help='mark front turntable cameras registered (requires --cameras)')
    l.add_argument('--register-side', action='store_true', help='also side cameras (handedness must be confirmed first)')
    l.add_argument('--dim-tolerance-pct', type=float, default=2.0)
    l.add_argument('--dim-tolerance-min', type=float, default=1.0)
    l.add_argument('--requirements', help='JSON file with contract requirements (ledger items)')
    l.add_argument('--acceptance', default='gates_judge', choices=['gates_judge', 'two_vlm'])
    l.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    (ingest if a.cmd == 'ingest' else load)(a)


if __name__ == '__main__':
    main()
