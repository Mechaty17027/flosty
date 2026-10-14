"""Bundle a pinned, verified official Windows Python for user projects.

The runtime is private to MechaHub; it neither installs nor changes system Python.
Upstream URL/hash: https://www.python.org/downloads/release/python-3148/
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
from urllib.request import Request, urlopen
import zipfile

VERSION = '3.14.8'
URL = f'https://www.python.org/ftp/python/{VERSION}/python-{VERSION}-embed-amd64.zip'
SHA256 = 'a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310'
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_EXPANDED = 96 * 1024 * 1024
LAUNCHER = '''"""Execute an explicitly selected user script with sibling module imports."""
from pathlib import Path
import runpy
import sys
if len(sys.argv) < 2:
    raise SystemExit('A user script path is required.')
target = Path(sys.argv[1]).resolve(strict=True)
if not target.is_file():
    raise SystemExit('The selected script is not a file.')
sys.argv = [str(target)] + sys.argv[2:]
sys.path.insert(0, str(target.parent))
runpy.run_path(str(target), run_name='__main__')
'''


def verified_members(payload: bytes, expected_sha: str = SHA256) -> dict[str, bytes]:
    if not payload or len(payload) > MAX_DOWNLOAD or hashlib.sha256(payload).hexdigest() != expected_sha:
        raise ValueError('Python runtime archive checksum/size does not match the pinned release.')
    result = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            # Official embedded packages are flat. Do not extract directory paths,
            # symlinks, alternate data streams, case duplicates, or executable links.
            if (len(path.parts) != 1 or path.is_absolute() or entry.filename in {'.', '..'}
                    or '\\' in entry.filename or ':' in entry.filename or entry.is_dir()
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('Unexpected path in Python runtime archive.')
            if any(name.casefold() == entry.filename.casefold() for name in result):
                raise ValueError('Duplicate runtime member.')
            total += entry.file_size
            if entry.file_size < 0 or total > MAX_EXPANDED:
                raise ValueError('Python runtime archive is too large after extraction.')
            result[entry.filename] = archive.read(entry)
    if not {'python.exe', 'python314.dll', 'python314.zip', 'python314._pth', 'LICENSE.txt'} <= result.keys():
        raise ValueError('Python runtime archive lacks required files.')
    return result


def prepare(directory: Path) -> Path:
    directory = Path(directory).absolute()
    if directory.is_symlink():
        raise ValueError('Runtime output must not be a symbolic link.')
    request = Request(URL, headers={'User-Agent': 'MechaHub-build-runtime/0.7'})
    with urlopen(request, timeout=30) as response:
        payload = response.read(MAX_DOWNLOAD + 1)
    members = verified_members(payload)
    members['run_user_code.py'] = LAUNCHER.encode('utf-8')
    members['runtime.json'] = json.dumps({'version': VERSION, 'url': URL, 'sha256': SHA256}, indent=2).encode('utf-8')
    directory.mkdir(parents=True, exist_ok=True)
    unknown = {path.name for path in directory.iterdir()} - members.keys()
    if unknown:
        raise ValueError('Runtime output contains files from another build; choose a fresh output directory.')
    for name, data in members.items():
        target = directory / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError('Unexpected runtime output entry.')
        target.write_bytes(data)
    return directory / 'python.exe'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('build/python_runtime'))
    args = parser.parse_args()
    executable = prepare(args.output)
    print(json.dumps({'version': VERSION, 'executable': str(executable), 'sha256': SHA256}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
