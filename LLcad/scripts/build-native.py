"""Build the pinned isolated mesh checker; never compile user-provided input."""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ['workers/mesh-cgal/check.cpp', 'scripts/build-native.py', 'deployment/native-packages.json']
FLAGS = ['-std=c++17', '-O1', '-frounding-math', '--param=ggc-min-expand=5', '--param=ggc-min-heapsize=16384']
WINDOWS = os.name == 'nt'
# Windows: zig c++ (clang) for x86_64-windows-gnu against the CGAL release's GMP/MPFR DLLs.
WINDOWS_FLAGS = ['-target', 'x86_64-windows-gnu', '-std=c++17', '-O1', '-frounding-math']
BINARY = 'meshcheck.exe' if WINDOWS else 'meshcheck'

def compiler_limits():
    # Template-heavy compilation must not exhaust the shared host's memory.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (1536*1024**2,1536*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (240,250))

def fingerprint():
    # The same canonical byte sequence is checked by the gateway before mounting.
    return hashlib.sha256(b''.join(name.encode()+b'\0'+(ROOT/name).read_bytes()+b'\0' for name in SOURCES)).hexdigest()

def check_linux_packages(expected):
    for package in ['libcgal-dev','libgmp-dev','libmpfr-dev','libboost1.83-dev','g++-14']:
        version=subprocess.check_output(['dpkg-query','-W','-f=${Version}',package],text=True).strip()
        if version != expected[package]: raise RuntimeError('Pinned mesh build dependency differs: '+package)

def windows_toolchain(expected):
    """The pinned Windows toolchain that install.ps1 unpacks under tools/native (LLCAD_NATIVE_TOOLS);
    install.ps1 checks the archives against pins['archives'] before unpacking."""
    pins = expected['windows']
    tools = Path(os.environ.get('LLCAD_NATIVE_TOOLS') or ROOT.parent/'tools'/'native')
    cgal = tools/('CGAL-'+pins['cgal'])/'include'
    boost = tools/('boost_'+pins['boost'].replace('.','_'))
    gmp = tools/'gmp-mpfr'/'auxiliary'/'gmp'
    version = lambda path, pattern: re.search(pattern, path.read_text(encoding='utf-8', errors='replace'), re.M).group(1)
    found = {
        'cgal': version(cgal/'CGAL'/'version.h', r'^#define CGAL_VERSION (\S+)'),
        'boost': version(boost/'boost'/'version.hpp', r'^#define BOOST_LIB_VERSION "(\S+)"').replace('_','.')+'.0',
        'gmp': '.'.join(version(gmp/'include'/'gmp.h', r'^#define __GNU_MP_VERSION%s\s+(\d+)' % s) for s in ('', '_MINOR', '_PATCHLEVEL')),
        'mpfr': version(gmp/'include'/'mpfr.h', r'^#define MPFR_VERSION_STRING "(\S+)"'),
    }
    for key, value in found.items():
        if value != pins[key]: raise RuntimeError('Pinned mesh build dependency differs: %s %s' % (key, value))
    zig = [os.environ['LLCAD_ZIG']] if os.environ.get('LLCAD_ZIG') else [str(tools.parent/'zigenv'/'Scripts'/'python.exe'), '-m', 'ziglang']
    if subprocess.check_output([*zig, 'version'], text=True).strip() != pins['zig']: raise RuntimeError('Pinned mesh build dependency differs: zig')
    return zig, cgal, boost, gmp, pins['runtime_dlls']

def main():
    digest=fingerprint(); target=ROOT/'workers/cad-occt'/BINARY; manifest=target.with_name('.meshcheck-build.json')
    expected=json.loads((ROOT/'deployment/native-packages.json').read_text())
    if WINDOWS: zig, cgal, boost, gmp, dlls = windows_toolchain(expected)
    else: check_linux_packages(expected)
    if target.is_file() and manifest.is_file():
        stored=json.loads(manifest.read_text())
        if stored['source_hash']==digest and stored['binary_sha256']==hashlib.sha256(target.read_bytes()).hexdigest():
            print('Pinned native mesh checker is current.'); return
    with tempfile.TemporaryDirectory(prefix='llcad-native-build-') as tmp:
        binary=Path(tmp)/BINARY
        if WINDOWS:
            compiler, flags = 'zig c++', WINDOWS_FLAGS
            subprocess.run([*zig, 'c++', *flags, '-DLLCAD_SOURCE_HASH="'+digest+'"', '-I', str(cgal), '-I', str(boost), '-I', str(gmp/'include'),
                            str(ROOT/SOURCES[0]), '-o', str(binary), str(gmp/'lib'/'mpfr.lib'), str(gmp/'lib'/'gmp.lib')], check=True)
            # The checker loads GMP and MPFR from its own folder.
            for name in dlls:
                shutil.copyfile(gmp/'bin'/name, Path(tmp)/name); shutil.copyfile(gmp/'bin'/name, target.with_name(name))
        else:
            compiler, flags = 'g++-14', FLAGS
            subprocess.run(['g++-14', *FLAGS, '-DLLCAD_SOURCE_HASH="'+digest+'"', str(ROOT/SOURCES[0]), '-o', str(binary), '-lgmp', '-lmpfr'],check=True,preexec_fn=compiler_limits)
        info=json.loads(subprocess.check_output([str(binary),'--build-info'],text=True))
        if info != {'source_hash':digest,'cgal':'6.0.1','kernel':'EPECK'}: raise RuntimeError('Unexpected native checker identity')
        data=binary.read_bytes(); staging=target.with_name('.meshcheck-stage')
        staging.write_bytes(data); staging.chmod(0o755); os.replace(staging,target)
        value={**info,'binary_sha256':hashlib.sha256(data).hexdigest(),'compiler':compiler,'flags':flags}
        staging=manifest.with_name('.meshcheck-build-stage'); staging.write_text(json.dumps(value,indent=2)+'\n');os.replace(staging,manifest)
    print('Pinned native mesh checker built:',digest)

if __name__=='__main__': main()
