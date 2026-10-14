"""Generate a release manifest from an actual binary and a verified version tag."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

PROJECT = Path(__file__).resolve().parents[1]
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024


def current_version() -> str:
    module = ast.parse((PROJECT / 'mechahub' / '__init__.py').read_text(encoding='utf-8-sig'))
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == '__version__' for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+', value):
                return value
    raise ValueError('Cannot read MechaHub version.')


def validate_tag(tag: str, version: str | None = None) -> None:
    expected = 'v' + (version or current_version())
    if tag != expected:
        raise ValueError(f'Release tag must be {expected}; received {tag}.')


def _artifact_info(artifact: Path, name: str) -> tuple[int, str]:
    artifact = Path(artifact)
    if artifact.name != name or not artifact.is_file():
        raise ValueError(f'Artifact must be an existing {name}.')
    size = artifact.stat().st_size
    if not 0 < size <= MAX_ARTIFACT_BYTES:
        raise ValueError('Artifact must be nonempty and no larger than 512 MiB.')
    digest = hashlib.sha256()
    with artifact.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return size, digest.hexdigest()


def make_manifest(repository: str, tag: str, artifact: Path, notes: str = '',
                  installer_artifact: Path | None = None) -> dict:
    version = current_version()
    validate_tag(tag, version)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Repository must be OWNER/REPO.')
    size, checksum = _artifact_info(artifact, 'MechaHub.exe')
    if '\0' in notes:
        raise ValueError('Release notes cannot contain NUL characters.')
    payload = {
        'version': version,
        'download_url': f'https://github.com/{repository}/releases/download/{tag}/MechaHub.exe',
        'sha256': checksum,
        'size': size,
        'notes': notes.strip(),
    }
    if installer_artifact is not None:
        installer_size, installer_checksum = _artifact_info(installer_artifact, 'MechaHubSetup.exe')
        payload.update({
            'installer_download_url': f'https://github.com/{repository}/releases/download/{tag}/MechaHubSetup.exe',
            'installer_sha256': installer_checksum,
            'installer_size': installer_size,
        })
    if len((json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode('utf-8')) > MAX_MANIFEST_BYTES:
        raise ValueError('Update manifest must be no larger than 64 KiB.')
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-tag')
    parser.add_argument('--repository')
    parser.add_argument('--tag')
    parser.add_argument('--artifact', type=Path)
    parser.add_argument('--installer-artifact', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--notes-file', type=Path)
    args = parser.parse_args()
    try:
        if args.check_tag is not None:
            validate_tag(args.check_tag)
            return 0
        if not all((args.repository, args.tag, args.artifact, args.output)):
            parser.error('--repository, --tag, --artifact, and --output are required.')
        notes = args.notes_file.read_text(encoding='utf-8-sig') if args.notes_file else ''
        payload = make_manifest(args.repository, args.tag, args.artifact, notes, args.installer_artifact)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
