"""HLR cache per view: raw data of section and projection (before clean_hidden, scale independent); plus the
form feature cache per part (JSON, key feature_key)."""
import hashlib, json, os, pickle
import numpy as np

_VERSION = 'hlr2'      # hlr2: the stored lines depend on the sampler (projection.sample_curve), the key does not know it
_digests = {}


def brep_digest(path):
    """SHA-256 of the file content; remembered in the process per (path, mtime, size)."""
    st = os.stat(path)
    sig = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    if sig not in _digests:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        _digests[sig] = h.hexdigest()
    return _digests[sig]


def view_key(digest, part_ids, frame, section_point, hidden, hlr_mode, selection=None):
    payload = {'v': _VERSION, 'brep': digest, 'parts': list(part_ids),
               'frame': np.round(np.asarray(frame, float), 9).tolist(),
               'section': None if section_point is None else [round(float(x), 9) for x in section_point],
               'hidden': bool(hidden), 'hlr': str(hlr_mode), 'sel': selection}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def _path(cache_dir, key):
    return os.path.join(cache_dir, key + '.pkl')


def load(cache_dir, key):
    """(vis, hid_raw, sec_faces) or None."""
    if not cache_dir:
        return None
    try:
        with open(_path(cache_dir, key), 'rb') as f:
            vis, hid, wkb = pickle.load(f)
        from shapely import wkb as swkb
        os.utime(_path(cache_dir, key))
        return vis, hid, [swkb.loads(b) for b in wkb]
    except Exception:
        return None


def store(cache_dir, key, vis, hid, sec_faces):
    if not cache_dir:
        return
    try:
        os.makedirs(cache_dir, exist_ok=True)
        final = _path(cache_dir, key)
        tmp = final + f'.{os.getpid()}.tmp'
        with open(tmp, 'wb') as f:
            pickle.dump((vis, hid, [g.wkb for g in sec_faces]), f, protocol=5)
        os.replace(tmp, final)
    except Exception:
        pass


def feature_key(digest, part_id, selection, version):
    """Form feature cache: B-Rep digest, part ID (part of the feature IDs), part selection {single_root, solids} as
    in the HLR key and version of the detection."""
    payload = {'v': 'feat1', 'brep': digest, 'part': str(part_id), 'sel': selection, 'version': version}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def load_json(cache_dir, key):
    """Stored JSON object or None (no directory, missing, unreadable)."""
    if not cache_dir:
        return None
    path = os.path.join(cache_dir, key + '.json')
    try:
        with open(path, encoding='utf-8') as f:
            obj = json.load(f)
        os.utime(path)
        return obj
    except Exception:
        return None


def store_json(cache_dir, key, obj):
    """Atomic like store: first into a temporary file of the process, then os.replace."""
    if not cache_dir:
        return
    tmp = None
    try:
        os.makedirs(cache_dir, exist_ok=True)
        final = os.path.join(cache_dir, key + '.json')
        tmp = final + f'.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, final)
    except Exception:
        try:
            if tmp:
                os.remove(tmp)
        except OSError:
            pass


def prune(cache_dir, max_files=4000, suffix='.pkl'):
    if not cache_dir or not os.path.isdir(cache_dir):
        return
    try:
        files = [os.path.join(cache_dir, n) for n in os.listdir(cache_dir) if n.endswith(suffix)]
        if len(files) <= max_files:
            return
        files.sort(key=os.path.getmtime)
        for p in files[:len(files) - max_files]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass
