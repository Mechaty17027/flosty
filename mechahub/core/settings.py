"""Local settings for MechaHub. No cloud account or telemetry."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading

APP_NAME = 'MechaHub'
_SAVE_LOCK = threading.Lock()


def config_file() -> Path:
    if os.name == 'nt':
        base = Path(os.environ.get('APPDATA', str(Path.home() / 'AppData' / 'Roaming')))
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    return base / APP_NAME / 'config.json'


def read_shortcuts(path: Path | None = None) -> list[dict[str, str]]:
    path = path or config_file()
    try:
        # Also accept a UTF-8 BOM from editors such as Windows Notepad.
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict):
            return []
        entries = data.get('shortcuts', [])
        if not isinstance(entries, list):
            return []
        return [item for entry in entries if (item := _valid_shortcut(entry)) is not None]
    except (OSError, ValueError, TypeError, KeyError):
        return []


def _valid_shortcut(entry: object) -> dict[str, str] | None:
    if not isinstance(entry, dict):
        return None
    name, target = entry.get('name'), entry.get('path')
    if not isinstance(name, str) or not isinstance(target, str):
        return None
    name, target = name.strip(), target.strip()
    if not name or not target or '\0' in name or '\0' in target:
        return None
    return {'name': name, 'path': target}


def write_shortcuts(shortcuts: list[dict[str, str]], path: Path | None = None) -> None:
    """Validate first, then replace the config with one complete UTF-8 file.

    Concurrent saves use independent temporary files; a failed save preserves
    the previous config. Invalid entries raise ValueError instead of erasing it.
    """
    if not isinstance(shortcuts, list):
        raise ValueError('Список ярликів має бути списком.')
    entries = []
    for entry in shortcuts:
        item = _valid_shortcut(entry)
        if item is None:
            raise ValueError('Кожен ярлик має містити непорожні текстові назву та шлях.')
        entries.append(item)
    payload = json.dumps({'shortcuts': entries}, ensure_ascii=False, indent=2) + '\n'
    path = path or config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', newline='\n',
            dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp', delete=False,
        ) as stream:
            temp = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows can reject simultaneous replacements with sharing errors.
        # Only the short commit step needs serialization within this process.
        with _SAVE_LOCK:
            os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
