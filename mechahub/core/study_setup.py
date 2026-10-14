"""Discover and start an already installed local Ollama on Windows.

This module does not download, install, stop, or own Ollama. A launched service
belongs to the user and remains available to other applications after MechaHub
closes. No study notes or application settings are read or changed here.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess


def _is_windows() -> bool:
    return os.name == 'nt'


def _installed_executable(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    try:
        candidate = Path(value)
        if (not candidate.is_absolute() or candidate.name.lower() not in {'ollama.exe', 'ollama app.exe'}
                or str(candidate).startswith('\\\\')):
            return None
        info = candidate.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            return None
        return candidate.resolve(strict=True)
    except (OSError, ValueError, TypeError):
        return None


def find_ollama() -> Path | None:
    """Find the normal per-user GUI/CLI installation, then an absolute PATH EXE."""
    if not _is_windows():
        return None
    local = os.environ.get('LOCALAPPDATA')
    if local:
        try:
            folder = Path(local) / 'Programs' / 'Ollama'
            for name in ('ollama app.exe', 'ollama.exe'):
                found = _installed_executable(folder / name)
                if found is not None:
                    return found
        except (ValueError, TypeError):
            pass
    # Do not let Windows which() prepend the current directory to a bare
    # command. Search explicit absolute PATH entries with a directory part.
    # That also excludes empty/relative PATH entries and .cmd/.bat wrappers.
    seen = set()
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        entry = entry.strip().strip('"')
        try:
            folder = Path(entry)
            key = os.path.normcase(str(folder))
            if not entry or not folder.is_absolute() or key in seen or entry.startswith('\\\\'):
                continue
            seen.add(key)
            found = _installed_executable(shutil.which(str(folder / 'ollama.exe')))
            if found is not None:
                # Prefer a companion GUI even when PATH locates only the CLI.
                gui = _installed_executable(found.parent / 'ollama app.exe')
                return gui or found
        except (OSError, ValueError, TypeError):
            continue
    return None


def start_ollama():
    """Start the user's existing Ollama silently; never stop it on app shutdown."""
    if not _is_windows():
        raise ValueError('Запуск Ollama з MechaHub доступний у Windows.')
    executable = find_ollama()
    if executable is None:
        raise ValueError('Ollama не знайдено. Спочатку встанови її через кнопку «Відкрити завантаження Ollama», '
                         'після цього натисни «Запустити Ollama».')
    # Recheck after discovery rather than launching a removed/replaced link.
    executable = _installed_executable(executable)
    if executable is None:
        raise ValueError('Файл Ollama більше недоступний. Встанови або відкрий Ollama й спробуй ще раз.')
    arguments = [str(executable)]
    if executable.name.lower() == 'ollama.exe':
        arguments.append('serve')
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    environment = os.environ.copy()
    environment['OLLAMA_HOST'] = '127.0.0.1:11434'
    try:
        return subprocess.Popen(arguments, cwd=str(executable.parent), shell=False, close_fds=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                                startupinfo=startup, env=environment)
    except OSError as error:
        raise ValueError(f'Не вдалося запустити Ollama: {error}. Відкрий її через меню «Пуск» '
                         'та перевір підключення у MechaHub.') from error
