"""Build a local Omega candidate; never import add-on/Kodi code."""
import ast
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import platform
import subprocess
import xml.etree.ElementTree as ET
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
ADDON_ID = 'script.channelsdvr.hub'
LANES = ('omega',)


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT).decode().strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_runtime():
    # No unknown worktree files, credentials, tests, caches or evidence enter ZIPs.
    source_commit = git('rev-parse', '--verify', 'HEAD^{commit}')
    if git('status', '--porcelain', '--untracked-files=all', '--', ADDON_ID):
        raise ValueError('Runtime must match its source commit before packaging')
    # Status can hide edits under assume-unchanged/skip-worktree. Bind both the
    # inventory and each byte to one immutable tree, independently of index flags.
    tree = subprocess.check_output(
        ['git', 'ls-tree', '-r', '-z', '--full-tree', source_commit, '--', ADDON_ID],
        cwd=ROOT)
    blobs = {}
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, kind, object_id = metadata.split()
        if kind != b'blob' or mode != b'100644':
            raise ValueError('Expected regular non-executable runtime source')
        blobs[raw_name.decode()] = object_id.decode()
    names = set(git('ls-files', '-z', '--', ADDON_ID).strip('\0').split('\0'))
    if not blobs or names != set(blobs):
        raise ValueError('Runtime inventory differs from source commit')
    files = {}
    for name in sorted(blobs):
        path = ROOT / name
        relative = path.relative_to(ROOT / ADDON_ID).as_posix()
        parts = PurePosixPath(relative).parts
        if (any(p.startswith('.') or p in ('__pycache__', 'tests', 'tools', 'docs', 'profile') for p in parts)
                or (path.suffix not in ('.py', '.xml', '.po', '.png') and relative != 'LICENSE')
                or any(p.is_symlink() for p in (path, *path.parents))):
            raise ValueError('Unreviewed runtime path: ' + relative)
        raw = path.read_bytes()
        committed = subprocess.check_output(['git', 'cat-file', 'blob', blobs[name]], cwd=ROOT)
        if raw != committed:
            raise ValueError('Runtime bytes differ from source commit: ' + relative)
        if path.suffix == '.py':
            ast.parse(raw, filename=relative, feature_version=(3, 10))
        elif path.suffix == '.xml':
            ET.fromstring(raw)
        files[relative] = raw
    manifest = ET.fromstring(files['addon.xml'])
    if manifest.get('id') != ADDON_ID or 'LICENSE' not in files:
        raise ValueError('Unexpected identity or missing license')
    for entry in manifest.findall('extension[@library]'):
        if entry.get('library') not in files:
            raise ValueError('Missing declared entry point')
    for asset in manifest.findall('extension/assets/*'):
        if asset.text not in files:
            raise ValueError('Missing declared asset')
    version = manifest.get('version', '')
    if len(version.split('.')) != 3 or not all(p.isdigit() for p in version.split('.')):
        raise ValueError('Expected numeric x.y.z version')
    return version, files, source_commit


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name, raw in sorted(files.items()):
            entry = zipfile.ZipInfo(ADDON_ID + '/' + name, (1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            output.writestr(entry, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


def retain(path, data):
    # Never replace different bytes under an existing ID/version or evidence name.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Linked destination forbidden: ' + str(path))
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('Refusing to replace existing different bytes: ' + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)


def main():
    version, files, source_commit = read_runtime()
    blob = archive(files)
    if blob != archive(files):
        raise ValueError('Non-deterministic archive')
    with zipfile.ZipFile(io.BytesIO(blob)) as check:
        expected = {ADDON_ID + '/' + n: b for n, b in files.items()}
        if (check.testzip() is not None or len(check.infolist()) != len(expected)
                or {n: check.read(n) for n in check.namelist()} != expected):
            raise ValueError('ZIP contents differ from source')
    filename = f'{ADDON_ID}-{version}.zip'
    checksums = []
    for lane in LANES:
        path = ROOT / 'dist' / lane / filename
        retain(path, blob)
        retain(path.with_suffix('.zip.sha256'), (sha(blob) + '\n').encode())
        checksums.append(f'{sha(blob)}  {lane}/{filename}\n')
    provenance = {
        'id': ADDON_ID, 'version': version, 'proposed_lanes': list(LANES),
        'source_repository': 'https://github.com/kcook98765/script.channelsdvr.hub',
        'source_commit': source_commit, 'runtime_matches_commit': True,
        'runtime_files': {n: sha(b) for n, b in sorted(files.items())},
        'package_sha256': sha(blob), 'deterministic_archive': True,
        'toolchain': {'python': platform.python_version(), 'zlib': zlib.ZLIB_RUNTIME_VERSION,
                      'packager_sha256': sha(Path(__file__).read_bytes())},
        'scope': 'Local candidate packaging only; not release approval',
        'runtime_tested': False, 'publication_performed': False,
    }
    retain(ROOT / 'dist' / f'{ADDON_ID}-{version}-provenance.json',
           (json.dumps(provenance, indent=2, sort_keys=True) + '\n').encode())
    retain(ROOT / 'dist' / f'{ADDON_ID}-{version}-SHA256SUMS', ''.join(checksums).encode())
    print('Local candidate packages (runtime untested):')
    for lane in LANES:
        print(f'  dist/{lane}/{filename}')
    print('SHA-256:', sha(blob))


if __name__ == '__main__':
    main()
