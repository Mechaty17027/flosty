"""Launch user-selected apps or folders using the Windows shell."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def open_path(path: str) -> None:
    clean_path = path.strip().strip('"').strip()
    if not clean_path:
        raise ValueError('Шлях до програми або папки порожній.')
    target = Path(os.path.expandvars(clean_path)).expanduser().absolute()
    if not target.exists():
        raise FileNotFoundError(f'Не знайдено: {target}')
    if sys.platform == 'win32':
        # Some portable apps load resources relative to their working directory.
        os.startfile(str(target), cwd=str(target.parent))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(target)])
    else:
        subprocess.Popen(['xdg-open', str(target)])


def system_action(action: str) -> None:
    if sys.platform != 'win32':
        raise OSError('Ця команда доступна у Windows.')
    windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    if action == 'explorer':
        subprocess.Popen([str(windows / 'explorer.exe')])
    elif action == 'taskmgr':
        subprocess.Popen([str(windows / 'System32' / 'taskmgr.exe')])
    elif action == 'settings':
        os.startfile('ms-settings:')
    else:
        raise ValueError('Невідома команда')
