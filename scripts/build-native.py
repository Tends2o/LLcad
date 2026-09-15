"""Build the pinned isolated mesh checker; never compile user-provided input."""
import hashlib, json, os, subprocess, tempfile, resource
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ['workers/mesh-cgal/check.cpp', 'scripts/build-native.py', 'deployment/native-packages.json']
FLAGS = ['-std=c++17', '-O1', '-frounding-math', '--param=ggc-min-expand=5', '--param=ggc-min-heapsize=16384']

def compiler_limits():
    # Template-heavy compilation must not exhaust the shared host's memory.
    resource.setrlimit(resource.RLIMIT_AS, (1536*1024**2,1536*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (240,250))

def fingerprint():
    # The same canonical byte sequence is checked by the gateway before mounting.
    return hashlib.sha256(b''.join(name.encode()+b'\0'+(ROOT/name).read_bytes()+b'\0' for name in SOURCES)).hexdigest()

def main():
    digest=fingerprint(); target=ROOT/'workers/cad-occt/meshcheck'; manifest=target.with_name('.meshcheck-build.json')
    expected=json.loads((ROOT/'deployment/native-packages.json').read_text())
    for package in ['libcgal-dev','libgmp-dev','libmpfr-dev','libboost1.83-dev','g++-14']:
        version=subprocess.check_output(['dpkg-query','-W','-f=${Version}',package],text=True).strip()
        if version != expected[package]: raise RuntimeError('Pinned mesh build dependency differs: '+package)
    if target.is_file() and manifest.is_file():
        stored=json.loads(manifest.read_text())
        if stored['source_hash']==digest and stored['binary_sha256']==hashlib.sha256(target.read_bytes()).hexdigest():
            print('Pinned native mesh checker is current.'); return
    with tempfile.TemporaryDirectory(prefix='llcad-native-build-') as tmp:
        binary=Path(tmp)/'meshcheck'
        subprocess.run(['g++-14', *FLAGS, '-DLLCAD_SOURCE_HASH="'+digest+'"', str(ROOT/SOURCES[0]), '-o', str(binary), '-lgmp', '-lmpfr'],check=True,preexec_fn=compiler_limits)
        info=json.loads(subprocess.check_output([str(binary),'--build-info'],text=True))
        if info != {'source_hash':digest,'cgal':'6.0.1','kernel':'EPECK'}: raise RuntimeError('Unexpected native checker identity')
        data=binary.read_bytes(); staging=target.with_name('.meshcheck-stage')
        staging.write_bytes(data); staging.chmod(0o755); os.replace(staging,target)
        value={**info,'binary_sha256':hashlib.sha256(data).hexdigest(),'compiler':'g++-14','flags':FLAGS}
        staging=manifest.with_name('.meshcheck-build-stage'); staging.write_text(json.dumps(value,indent=2)+'\n');os.replace(staging,manifest)
    print('Pinned native mesh checker built:',digest)

if __name__=='__main__': main()
