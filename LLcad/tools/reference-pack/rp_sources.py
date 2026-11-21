"""Sources of a Reference Pack: the URLs and files the caller passes, nothing else.

A page URL is fetched once and only the media it links directly (images at their
largest srcset size, videos, PDFs, SVGs) are downloaded. There is no crawling beyond
that page and no search. Every source is recorded with URL, hash and a licence note."""
import hashlib, html.parser, json, mimetypes, os, re, shutil, subprocess, time, urllib.parse, urllib.request
from pathlib import Path

UA = 'LLcad-reference-pack/1 (+local reconstruction reference; contact the operator)'
MAX_BYTES = int(os.environ.get('LLCAD_PACK_MAX_BYTES', str(300 * 1024 * 1024)))
MEDIA = {'.mp4': 'video', '.m4v': 'video', '.mov': 'video', '.webm': 'video', '.pdf': 'pdf', '.svg': 'svg',
         '.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.webp': 'image'}
LICENCE = ('Third-party material fetched from the given URL for use as a reconstruction reference only. '
           'Rights stay with the owner; check them before redistributing or publishing derived images.')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def kind_of(name, mime=None):
    ext = Path(urllib.parse.urlparse(name).path).suffix.lower()
    if ext in MEDIA:
        return MEDIA[ext]
    if mime:
        if mime.startswith('video/'):
            return 'video'
        if mime == 'application/pdf':
            return 'pdf'
        if mime == 'image/svg+xml':
            return 'svg'
        if mime.startswith('image/'):
            return 'image'
        if mime.startswith('text/html'):
            return 'page'
    return 'unknown'


def fetch(url, folder, log):
    """Download one URL into folder (cached by URL hash). Returns (path, mime)."""
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    tag = hashlib.sha256(url.encode()).hexdigest()[:10]
    base = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name or 'index.html'
    base = re.sub(r'[^A-Za-z0-9._-]', '_', base)[-80:]
    target = folder / f'{tag}-{base}'
    meta = target.with_suffix(target.suffix + '.meta.json')
    if target.exists() and meta.exists():
        return target, json.loads(meta.read_text()).get('mime')
    t0 = time.time()
    if shutil.which('curl'):
        # curl speaks HTTP/2 and retries; some manufacturer CDNs stall HTTP/1.1 clients.
        tmp = target.with_suffix(target.suffix + '.part')
        res = subprocess.run(['curl', '-sS', '-L', '--fail', '--retry', '3', '--retry-delay', '2', '--connect-timeout', '30',
                              '--max-time', '1800', '--max-filesize', str(MAX_BYTES), '-A', UA, '-o', str(tmp),
                              '-w', '%{content_type}\t%{size_download}', url], capture_output=True, text=True)
        if res.returncode != 0:
            raise OSError(f'download failed ({res.returncode}): {res.stderr.strip()[:200]}')
        ctype, _, size = res.stdout.partition('\t')
        mime = ctype.split(';')[0].strip() or mimetypes.guess_type(base)[0]
        shutil.move(tmp, target)
        meta.write_text(json.dumps({'url': url, 'mime': mime, 'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}))
        log(f'fetched {url} ({float(size or 0) / 1e6:.1f} MB, {time.time() - t0:.1f} s)')
        return target, mime
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        mime = (r.headers.get('content-type') or '').split(';')[0].strip() or mimetypes.guess_type(base)[0]
        size = int(r.headers.get('content-length') or 0)
        if size > MAX_BYTES:
            raise ValueError(f'source larger than {MAX_BYTES} bytes: {url}')
        tmp = target.with_suffix(target.suffix + '.part')
        n = 0
        with open(tmp, 'wb') as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                n += len(chunk)
                if n > MAX_BYTES:
                    raise ValueError(f'source larger than {MAX_BYTES} bytes: {url}')
                f.write(chunk)
        shutil.move(tmp, target)
    meta.write_text(json.dumps({'url': url, 'mime': mime, 'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}))
    log(f'fetched {url} ({n / 1e6:.1f} MB, {time.time() - t0:.1f} s)')
    return target, mime


class _Links(html.parser.HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.found = base, []

    def add(self, url, width=0):
        if url and not url.startswith('data:'):
            self.found.append((urllib.parse.urljoin(self.base, url.strip()), width))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ('img', 'source'):
            best = None
            for key in ('srcset', 'data-srcset'):
                for part in (a.get(key) or '').split(','):
                    bits = part.strip().split()
                    if not bits:
                        continue
                    w = int(re.sub(r'\D', '', bits[1]) or 0) if len(bits) > 1 and bits[1].endswith('w') else 0
                    if best is None or w > best[1]:
                        best = (bits[0], w)
            if best:
                self.add(*best)
            self.add(a.get('src') or a.get('data-src'))
        elif tag in ('a', 'link'):
            self.add(a.get('href'))
        elif tag == 'video':
            self.add(a.get('src')); self.add(a.get('poster'))
        elif tag == 'meta' and a.get('property') in ('og:image', 'og:video'):
            self.add(a.get('content'))


def page_media(page_path, page_url, kinds=('video', 'pdf', 'svg', 'image')):
    """Media URLs linked directly from one page; one entry per URL (largest srcset)."""
    text = Path(page_path).read_text(errors='replace')
    p = _Links(page_url); p.feed(text)
    # Plain URLs in inline JSON/JS (video players often keep them there).
    for m in re.finditer(r'["\'](https?:[^"\'\s]+?\.(?:mp4|pdf|svg|png|jpe?g|webp))["\']', text, re.I):
        p.add(m.group(1).replace('\\/', '/'))
    seen, out = set(), []
    for url, _ in p.found:
        u = url.split('#')[0]
        if u in seen or kind_of(u) not in kinds:
            continue
        seen.add(u); out.append(u)
    return out


def collect(inputs, folder, log, max_page_items=60):
    """Resolve CLI inputs (URLs, files) into source records with local paths."""
    sources = []

    def record(path, kind, url=None, mime=None, parent=None):
        sid = f'src-{len(sources) + 1:02d}'
        sources.append({'id': sid, 'kind': kind, 'url': url, 'path': str(path), 'sha256': sha256(path),
                        'bytes': Path(path).stat().st_size, 'mime': mime or mimetypes.guess_type(str(path))[0],
                        'parent': parent, 'licence_note': LICENCE if url else 'Local file supplied by the user.',
                        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        return sid

    for item in inputs:
        if re.match(r'^https?://', item):
            path, mime = fetch(item, folder, log)
            kind = kind_of(item, mime)
            if kind == 'page':
                pid = record(path, 'page', item, mime)
                links = page_media(path, item)[:max_page_items]
                log(f'page {item}: {len(links)} linked media (downloading those only)')
                for link in links:
                    try:
                        lp, lm = fetch(link, folder, log)
                        record(lp, kind_of(link, lm), link, lm, parent=pid)
                    except Exception as e:  # one broken link does not stop the pack
                        log(f'skip {link}: {e}')
            else:
                record(path, kind, item, mime)
        else:
            path = Path(item).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(item)
            record(path, kind_of(path.name), None, mimetypes.guess_type(path.name)[0])
    return sources
