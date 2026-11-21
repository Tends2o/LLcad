"""Optional hosted SAM 3 concept segmentation (fal.ai). OFF by default.

Enabled only with --sam3 AND a key in LLCAD_FAL_KEY (or FAL_KEY). Images leave the
machine: do not use it for confidential or unreleased products. SAM License: commercial
use allowed, gated access, military use excluded. Endpoint and payload follow fal's
sam-3/image model page; LLCAD_SAM3_ENDPOINT overrides the URL. The response is parsed
defensively (mask URLs or base64 PNGs); anything unexpected is reported, not guessed."""
import base64, json, os, urllib.request
from pathlib import Path

DEFAULT_ENDPOINT = 'https://fal.run/fal-ai/sam-3/image'
CONCEPTS = ['button', 'logo', 'lens', 'LED', 'printed text', 'wheel', 'slider', 'seam']


def key():
    return os.environ.get('LLCAD_FAL_KEY') or os.environ.get('FAL_KEY')


def segment(image_path, concepts, out_dir, ref_id, log, timeout=120):
    k = key()
    if not k:
        return {'status': 'skipped', 'reason': 'no LLCAD_FAL_KEY/FAL_KEY set'}
    endpoint = os.environ.get('LLCAD_SAM3_ENDPOINT', DEFAULT_ENDPOINT)
    data = Path(image_path).read_bytes()
    uri = 'data:image/png;base64,' + base64.b64encode(data).decode()
    out_dir = Path(out_dir); results = []
    for concept in concepts:
        body = json.dumps({'image_url': uri, 'prompt': concept}).encode()
        req = urllib.request.Request(endpoint, data=body, headers={'Authorization': 'Key ' + k, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                answer = json.loads(r.read())
        except Exception as e:
            results.append({'concept': concept, 'status': 'error', 'error': str(e)[:200]})
            continue
        masks = answer.get('masks') or answer.get('instances') or []
        saved = []
        for i, m in enumerate(masks if isinstance(masks, list) else []):
            url = (m.get('url') if isinstance(m, dict) else None) or (m.get('mask', {}).get('url') if isinstance(m, dict) and isinstance(m.get('mask'), dict) else None)
            path = out_dir / f'{ref_id}-sam3-{concept.replace(" ", "_")}-{i}.png'
            try:
                if url and url.startswith('data:'):
                    path.write_bytes(base64.b64decode(url.split(',', 1)[1]))
                elif url:
                    with urllib.request.urlopen(url, timeout=timeout) as r:
                        path.write_bytes(r.read())
                else:
                    continue
                saved.append({'mask': str(path), 'score': m.get('score') if isinstance(m, dict) else None})
            except Exception as e:
                log(f'sam3 mask download failed: {e}')
        results.append({'concept': concept, 'status': 'ok', 'instances': len(saved), 'masks': saved,
                        'raw_keys': sorted(answer.keys())[:12]})
    return {'status': 'computed', 'provider': 'fal-sam3', 'endpoint': endpoint, 'concepts': results,
            'note': 'Instance counts are measured by SAM 3; label them once (judge or user) before using them as the part inventory.'}
