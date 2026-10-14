"""Discover installed apps for the add-program dialog."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


APP_EXTENSIONS = {'.exe', '.lnk', '.bat', '.cmd', '.url'}
AUXILIARY_EXECUTABLE_MARKERS = ('unins', 'setup', 'update', 'helper')


def _clean_display_icon(raw: str) -> str:
    value = (raw or '').strip()
    if not value:
        return ''
    # DisplayIcon is a path followed by an optional signed resource index.
    # Only remove that final index: commas can also be part of the filename.
    value = re.sub(r',\s*-?\d+\s*$', '', value).strip().strip('"').strip()
    return os.path.expandvars(value)


def _is_app_file(path: str) -> bool:
    if not path or Path(path).suffix.lower() not in APP_EXTENSIONS:
        return False
    try:
        return Path(path).is_file()
    except (OSError, ValueError):
        return False


def _best_install_executable(folder: Path) -> str:
    candidates = []
    try:
        if not folder.is_dir():
            return ''
        for item in folder.iterdir():
            if item.is_file() and item.suffix.lower() == '.exe':
                name = item.name.lower()
                if any(blocked in name for blocked in AUXILIARY_EXECUTABLE_MARKERS):
                    continue
                candidates.append(item)
    except OSError:
        return ''
    if not candidates:
        return ''
    candidates.sort(key=lambda p: (len(p.name), p.name.lower()))
    return str(candidates[0])


def _push_unique(target: list[dict[str, str]], seen: set[tuple[str, str]], name: str, path: str, source: str) -> None:
    clean_name = (name or '').strip()
    clean_path = os.path.expandvars((path or '').strip().strip('"').strip())
    if not clean_name or not clean_path:
        return
    if not _is_app_file(clean_path):
        return
    key = (clean_name.casefold(), os.path.normpath(clean_path).casefold())
    if key in seen:
        return
    seen.add(key)
    target.append({'name': clean_name, 'path': clean_path, 'source': source})


def _registry_programs() -> list[dict[str, str]]:
    if sys.platform != 'win32':
        return []
    try:
        import winreg
    except ImportError:
        return []

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    root = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    # Explicit views also find 64-bit apps when MechaHub uses 32-bit Python.
    hives = [
        (hive, view)
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    ]

    def read_value(key, name: str) -> str:
        try:
            value, _kind = winreg.QueryValueEx(key, name)
            return value if isinstance(value, str) else ''
        except OSError:
            return ''

    for hive, view in hives:
        try:
            key = winreg.OpenKey(hive, root, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                try:
                    subkey = winreg.OpenKey(key, sub_name, 0, winreg.KEY_READ | view)
                except OSError:
                    continue
                with subkey:
                    display_name = read_value(subkey, 'DisplayName')
                    if not display_name:
                        continue
                    path = _clean_display_icon(read_value(subkey, 'DisplayIcon'))
                    auxiliary_icon = any(marker in Path(path).name.lower() for marker in AUXILIARY_EXECUTABLE_MARKERS)
                    if not _is_app_file(path) or auxiliary_icon:
                        # Inno Setup often stores the app icon in unins000.exe.
                        # It is an icon source, not the executable users should run.
                        path = ''
                        install_location = os.path.expandvars(read_value(subkey, 'InstallLocation').strip().strip('"'))
                        if install_location:
                            path = _best_install_executable(Path(install_location))
                    _push_unique(result, seen, display_name, path, 'Реєстр Windows')
    return result


def _start_menu_programs() -> list[dict[str, str]]:
    if sys.platform != 'win32':
        return []
    roots = []
    appdata = os.environ.get('APPDATA')
    programdata = os.environ.get('PROGRAMDATA')
    if appdata:
        roots.append(Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs')
    if programdata:
        roots.append(Path(programdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs')
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        # os.walk skips inaccessible directories and keeps scanning siblings.
        # It also avoids following directory symlinks into another file tree.
        for folder, _directories, filenames in os.walk(root):
            for filename in filenames:
                item = Path(folder) / filename
                if item.suffix.lower() in {'.lnk', '.url', '.exe'}:
                    rel = item.relative_to(root)
                    _push_unique(result, seen, item.stem.replace(' ', ' '), str(item), f'Пуск · {rel.parent}')
    return result


def installed_programs(limit: int = 600) -> list[dict[str, str]]:
    """Return discovered apps sorted by name for the picker dialog."""
    if limit <= 0:
        return []
    result = _start_menu_programs() + _registry_programs()
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for item in result:
        key = (item['name'].casefold(), os.path.normpath(item['path']).casefold())
        unique.setdefault(key, item)
    ordered = sorted(unique.values(), key=lambda item: item['name'].casefold())
    return ordered[:limit]
