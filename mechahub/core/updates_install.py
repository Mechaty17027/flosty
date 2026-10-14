"""Stage and apply a confirmed Windows update without importing Qt.

The application calls ``prepare_installation`` only after an explicit install
action, saves its documents, and starts the staged helper when shutdown is safe.
The helper opens a handle to that exact parent process before acknowledging that
it is ready. It waits for the handle, verifies the payload again, and updates
the existing location. User configuration and study data are never touched.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from mechahub.core.updates import MAX_ARTIFACT_BYTES, ReleaseManifest, UpdateError, version_key
from mechahub.core.updates_download import ArtifactDownloadResult

UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{D426FCA6-8DE0-4A69-8E36-12004679E7D4}_is1'
MAX_PLAN_BYTES = 16 * 1024
PARENT_WAIT_SECONDS = 180
HELPER_READY_SECONDS = 60


class _InstallCancelled(UpdateError):
    pass


class _ApplicationRestarted(UpdateError):
    pass


@dataclass(frozen=True)
class InstallContext:
    mode: str
    executable: Path
    install_directory: Path | None = None


@dataclass(frozen=True)
class PreparedUpdate:
    mode: str
    version: str
    plan_path: Path
    helper_path: Path
    target_path: Path


def _canonical(path: Path | str, *, exists: bool = True) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise UpdateError('Шлях оновлення має бути абсолютним.')
    try:
        # Reject links/reparse points before resolving: resolution hides them.
        for part in (candidate, *candidate.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise UpdateError('Оновлення не підтримує символьні посилання або точки повторного аналізу.')
        return candidate.resolve(strict=exists)
    except OSError as error:
        raise UpdateError(f'Шлях оновлення недоступний: {candidate}') from error


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _registered_directory() -> Path | None:
    if os.name != 'nt':
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            value, kind = winreg.QueryValueEx(key, 'InstallLocation')
        if kind != winreg.REG_SZ or not isinstance(value, str) or not value:
            return None
        return _canonical(value)
    except (OSError, UpdateError):
        return None


def detect_installation(executable: Path | str | None = None,
                        frozen: bool | None = None) -> InstallContext:
    """Distinguish the registered installation from a portable/source copy."""
    exe = _canonical(executable or sys.executable)
    frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else frozen
    if not frozen:
        return InstallContext('source', exe)
    directory = _registered_directory()
    if directory is not None and _same_path(exe.parent, directory) and exe.name.lower() == 'mechahub.exe':
        return InstallContext('installed', exe, directory)
    return InstallContext('portable', exe)


def _fingerprint(path: Path, expected_hash: str | None = None,
                 expected_size: int | None = None) -> tuple[str, int]:
    path = _canonical(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_ARTIFACT_BYTES:
        raise UpdateError('Файл оновлення має бути звичайним непорожнім файлом до 512 МіБ.')
    if expected_size is not None and (type(expected_size) is not int or before.st_size != expected_size):
        raise UpdateError('Розмір файла оновлення змінився.')
    if expected_hash is not None and (not isinstance(expected_hash, str)
                                      or not re.fullmatch(r'[a-fA-F0-9]{64}', expected_hash)):
        raise UpdateError('Для встановлення потрібна контрольна сума SHA-256.')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise UpdateError('Файл оновлення змінився під час відкриття.')
        for chunk in iter(lambda: source.read(64 * 1024), b''):
            digest.update(chunk)
        after = os.fstat(source.fileno())
    final = path.stat()
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if signature(before) != signature(after) or signature(before) != signature(final):
        raise UpdateError('Файл оновлення змінився під час перевірки.')
    checksum = digest.hexdigest()
    if expected_hash is not None and not hmac.compare_digest(checksum, expected_hash.lower()):
        raise UpdateError('Контрольна сума SHA-256 не збігається. Встановлення скасовано.')
    return checksum, before.st_size


def _windows_api():
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                      ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                      ctypes.POINTER(wintypes.FILETIME))
    kernel.GetProcessTimes.restype = wintypes.BOOL
    return kernel, wintypes


def _creation_time(kernel, types, handle) -> int:
    created, ended, kernel_time, user = (types.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended),
                                  ctypes.byref(kernel_time), ctypes.byref(user)):
        raise UpdateError('Не вдалося перевірити процес програми перед оновленням.')
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def _parent_creation_time(pid: int) -> int:
    if os.name != 'nt':
        raise UpdateError('Встановлення оновлень доступне у Windows.')
    kernel, types = _windows_api()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise UpdateError('Не вдалося відкрити процес програми перед оновленням.')
    try:
        return _creation_time(kernel, types, handle)
    finally:
        kernel.CloseHandle(handle)


def _bootstrap_parent(target: Path) -> tuple[int, int]:
    """A one-file bootloader may still hold the target after its child exits."""
    if os.name != 'nt':
        return 0, 0
    kernel, types = _windows_api()
    pid = os.getppid()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return 0, 0
    try:
        kernel.QueryFullProcessImageNameW.argtypes = (types.HANDLE, types.DWORD, types.LPWSTR,
                                                     ctypes.POINTER(types.DWORD))
        kernel.QueryFullProcessImageNameW.restype = types.BOOL
        text = ctypes.create_unicode_buffer(32768)
        length = types.DWORD(len(text))
        if kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(length)):
            if _same_path(_canonical(text.value), target):
                return pid, _creation_time(kernel, types, handle)
        return 0, 0
    except (OSError, UpdateError):
        return 0, 0
    finally:
        kernel.CloseHandle(handle)


def _write_json(path: Path, data: dict) -> None:
    with path.open('x', encoding='utf-8') as output:
        json.dump(data, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())


def _stage_root() -> Path:
    location = os.environ.get('LOCALAPPDATA')
    if not location or not Path(location).is_absolute():
        raise UpdateError('Не вдалося знайти локальну папку даних Windows для оновлення.')
    folder = Path(location) / 'MechaHub' / 'update-stage'
    folder.mkdir(parents=True, exist_ok=True)
    return _canonical(folder)


def _payload_metadata(manifest: ReleaseManifest, mode: str) -> tuple[str, int | None]:
    if mode == 'installed':
        checksum = getattr(manifest, 'installer_sha256', '')
        size = getattr(manifest, 'installer_size', None)
        url = getattr(manifest, 'installer_download_url', '')
    else:
        checksum, size, url = manifest.sha256, manifest.size, manifest.download_url
    from urllib.parse import urlsplit
    from mechahub.core.updates import validate_manifest_url
    validate_manifest_url(url)
    if Path(urlsplit(url).path).suffix.lower() != '.exe':
        raise UpdateError('Для встановлення потрібен файл EXE поточної версії.')
    if not isinstance(checksum, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', checksum):
        raise UpdateError('Для встановлення потрібна контрольна сума SHA-256.')
    if size is not None and (type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES):
        raise UpdateError('Розмір оновлення некоректний.')
    return checksum.lower(), size


def prepare_installation(manifest: ReleaseManifest, artifact: ArtifactDownloadResult, *,
                         helper_artifact: ArtifactDownloadResult | None = None,
                         context: InstallContext | None = None) -> PreparedUpdate:
    """Verify and stage a payload/helper; do not close or execute anything."""
    context = context or detect_installation()
    if context.mode not in {'installed', 'portable'}:
        raise UpdateError('Оновлення з програми доступне для зібраного Windows EXE.')
    version_key(manifest.version)
    target = _canonical(context.executable)
    if target.suffix.lower() != '.exe':
        raise UpdateError('Поточна програма має бути файлом EXE.')
    if context.mode == 'installed' and (context.install_directory is None
                                      or not _same_path(target.parent, _canonical(context.install_directory))):
        raise UpdateError('Папка встановленої програми не збігається з поточним файлом.')
    checksum, expected_size = _payload_metadata(manifest, context.mode)
    payload = _canonical(artifact.path)
    actual_hash, actual_size = _fingerprint(payload, checksum, expected_size)
    if (not isinstance(artifact.sha256, str) or type(artifact.bytes_received) is not int
            or artifact.sha256.lower() != actual_hash or artifact.bytes_received != actual_size):
        raise UpdateError('Завантажений файл більше не відповідає результату перевірки.')
    if _same_path(payload, target):
        raise UpdateError('Оновлення має бути окремим від поточної програми файлом.')
    target_hash, target_size = _fingerprint(target)
    # Check write permission without changing an existing executable.
    try:
        with tempfile.NamedTemporaryFile(prefix='.MechaHub-write-check-', dir=target.parent):
            pass
    except OSError as error:
        raise UpdateError('Немає прав на оновлення цієї папки. Перемісти portable-копію у доступну папку.') from error
    parent_creation = _parent_creation_time(os.getpid())
    bootstrap_pid, bootstrap_creation = _bootstrap_parent(target)
    try:
        stage = Path(tempfile.mkdtemp(prefix='update-', dir=_stage_root()))
    except OSError as error:
        raise UpdateError(f'Не вдалося створити папку оновлення: {error}') from error
    try:
        helper = stage / 'helper.exe'
        staged_payload = stage / 'payload.exe'
        shutil.copyfile(target, helper)
        _fingerprint(helper, target_hash, target_size)
        shutil.copyfile(payload, staged_payload)
        _fingerprint(staged_payload, actual_hash, actual_size)
        new_checksum = manifest.sha256
        new_size = actual_size if context.mode == 'portable' else manifest.size
        if (not isinstance(new_checksum, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', new_checksum)
                or (new_size is not None and (type(new_size) is not int
                                             or not 0 < new_size <= MAX_ARTIFACT_BYTES))):
            raise UpdateError('Для встановлення потрібна контрольна сума нового MechaHub.exe.')
        plan = dict(schema=1, mode=context.mode, version=manifest.version,
                    parent_pid=os.getpid(), parent_creation=parent_creation,
                    bootstrap_pid=bootstrap_pid, bootstrap_creation=bootstrap_creation,
                    target_path=str(target), target_sha256=target_hash, target_size=target_size,
                    helper_path=str(helper), helper_sha256=target_hash, helper_size=target_size,
                    payload_path=str(staged_payload), payload_sha256=actual_hash, payload_size=actual_size,
                    new_target_sha256=new_checksum.lower(), new_target_size=new_size)
        plan_path = stage / 'plan.json'
        _write_json(plan_path, plan)
        return PreparedUpdate(context.mode, manifest.version, plan_path, helper, target)
    except (OSError, UpdateError) as error:
        for name in ('plan.json', 'payload.exe', 'helper.exe'):
            try:
                (stage / name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            stage.rmdir()
        except OSError:
            pass
        if isinstance(error, UpdateError):
            raise
        raise UpdateError(f'Не вдалося підготувати оновлення: {error}') from error


def _load_plan(plan_path: Path | str) -> dict:
    path = _canonical(plan_path)
    if path.name != 'plan.json' or path.stat().st_size > MAX_PLAN_BYTES:
        raise UpdateError('Файл плану оновлення некоректний або завеликий.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as error:
        raise UpdateError('Не вдалося прочитати план оновлення.') from error
    keys = {'schema', 'mode', 'version', 'parent_pid', 'parent_creation', 'bootstrap_pid',
            'bootstrap_creation', 'target_path',
            'target_sha256', 'target_size', 'helper_path', 'helper_sha256', 'helper_size',
            'payload_path', 'payload_sha256', 'payload_size', 'new_target_sha256', 'new_target_size'}
    if not isinstance(data, dict) or set(data) != keys or type(data['schema']) is not int or data['schema'] != 1:
        raise UpdateError('Формат плану оновлення не підтримується.')
    if not isinstance(data['mode'], str) or data['mode'] not in {'installed', 'portable'}:
        raise UpdateError('Режим встановлення не підтримується.')
    version_key(data['version'])
    for name in ('parent_pid', 'parent_creation'):
        if type(data[name]) is not int or not 0 < data[name] < 2 ** (32 if name == 'parent_pid' else 64):
            raise UpdateError('Процес для оновлення вказано некоректно.')
    for name in ('bootstrap_pid', 'bootstrap_creation'):
        if type(data[name]) is not int or not 0 <= data[name] < 2 ** (32 if name.endswith('pid') else 64):
            raise UpdateError('Процес запуску програми вказано некоректно.')
    if bool(data['bootstrap_pid']) != bool(data['bootstrap_creation']):
        raise UpdateError('Процес запуску програми вказано некоректно.')
    if (not isinstance(data['new_target_sha256'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', data['new_target_sha256'])
            or (data['new_target_size'] is not None and (type(data['new_target_size']) is not int
                                                        or not 0 < data['new_target_size'] <= MAX_ARTIFACT_BYTES))):
        raise UpdateError('Інформація про новий MechaHub.exe некоректна.')
    for prefix in ('target', 'helper', 'payload'):
        value = data[f'{prefix}_path']
        if not isinstance(value, str) or '\0' in value:
            raise UpdateError('Шлях у плані оновлення некоректний.')
        candidate = _canonical(value)
        if candidate.suffix.lower() != '.exe':
            raise UpdateError('У плані оновлення має бути файл EXE.')
        checksum, size = data[f'{prefix}_sha256'], data[f'{prefix}_size']
        if not isinstance(checksum, str) or not re.fullmatch(r'[a-f0-9]{64}', checksum):
            raise UpdateError('Контрольна сума в плані оновлення некоректна.')
        if type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES:
            raise UpdateError('Розмір у плані оновлення некоректний.')
        data[f'{prefix}_path'] = candidate
    if (data['helper_path'] != path.parent / 'helper.exe'
            or data['payload_path'] != path.parent / 'payload.exe'
            or data['target_path'].parent == path.parent):
        raise UpdateError('Файли оновлення розташовані поза власною папкою підготовки.')
    if (data['helper_sha256'] != data['target_sha256'] or data['helper_size'] != data['target_size']
            or (data['mode'] == 'portable' and (data['payload_sha256'] != data['new_target_sha256']
                                               or data['payload_size'] != data['new_target_size']))):
        raise UpdateError('Файли у плані оновлення не узгоджені між собою.')
    data['plan_path'] = path
    return data


def _popen(arguments: list[str], *, cwd: Path):
    flags = 0
    if os.name == 'nt':
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    environment = os.environ.copy()
    # A detached child must own its one-file extraction directory. Inheriting
    # the old app's PyInstaller environment can remove resources on shutdown.
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return subprocess.Popen(arguments, cwd=str(cwd), shell=False, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=flags, env=environment)


def start_installation(prepared: PreparedUpdate):
    """Start the helper, then poll installation_ready before closing the GUI."""
    plan = _load_plan(prepared.plan_path)
    if (plan['helper_path'] != prepared.helper_path or plan['target_path'] != prepared.target_path
            or plan['mode'] != prepared.mode or plan['version'] != prepared.version):
        raise UpdateError('Підготовлений план встановлення змінився.')
    _fingerprint(plan['helper_path'], plan['helper_sha256'], plan['helper_size'])
    try:
        return _popen([str(plan['helper_path']), '--apply-update', str(prepared.plan_path)],
                      cwd=prepared.plan_path.parent)
    except OSError as error:
        raise UpdateError(f'Не вдалося запустити оновлення. Програма залишиться відкритою: {error}') from error


def installation_ready(prepared: PreparedUpdate, process) -> bool:
    """A helper acknowledges readiness only after it opens the parent handle."""
    result = prepared.plan_path.parent / 'result.json'
    if result.exists():
        try:
            if result.stat().st_size > MAX_PLAN_BYTES:
                raise ValueError('oversized result')
            message = json.loads(result.read_text(encoding='utf-8')).get('error')
        except (OSError, ValueError, AttributeError):
            message = 'Помічник оновлення повернув некоректний результат.'
        raise UpdateError(str(message or 'Помічник оновлення завершився до закриття програми.'))
    if process.poll() is not None:
        raise UpdateError('Помічник оновлення не запустився. Програма залишиться відкритою.')
    ready = prepared.plan_path.parent / 'ready.json'
    if ready.exists():
        try:
            value = json.loads(ready.read_text(encoding='utf-8')) if ready.stat().st_size < 1024 else None
            return isinstance(value, dict) and set(value) == {'ready'} and value['ready'] is True
        except (OSError, ValueError):
            return False
    return False


def discard_prepared(prepared: PreparedUpdate) -> None:
    """Discard a never-started staging directory after cancelled GUI shutdown."""
    plan = _load_plan(prepared.plan_path)
    folder = prepared.plan_path.parent
    if (folder / 'ready.json').exists() or (folder / 'result.json').exists():
        return
    if plan['helper_path'] != prepared.helper_path or plan['target_path'] != prepared.target_path:
        raise UpdateError('Підготовлений план встановлення змінився.')
    expected = {'plan.json', 'helper.exe', 'payload.exe'}
    if {item.name for item in folder.iterdir()} != expected:
        raise UpdateError('У папці оновлення з’явилися сторонні файли.')
    for name in ('plan.json', 'payload.exe', 'helper.exe'):
        (folder / name).unlink()
    folder.rmdir()


def cancel_installation(prepared: PreparedUpdate, process=None) -> None:
    """Revoke a launched handoff, including a helper still extracting itself."""
    marker = prepared.plan_path.parent / 'cancel.json'
    try:
        _write_json(marker, {'cancelled': True})
    except FileExistsError:
        pass
    except OSError as error:
        raise UpdateError(f'Не вдалося скасувати помічник оновлення: {error}') from error


def cleanup_finished_stages() -> int:
    """Remove at most two old successful stages, keeping the newest and failures.

    Never traverse or recursively remove a directory. Unknown files, reparse
    points, incomplete plans, failed results, and busy helpers are preserved.
    Downloads and files alongside the application are outside this cleanup's
    scope. The newest helper is also the verified previous application backup.
    """
    root = _stage_root()
    candidates = []
    with os.scandir(root) as scan:
        entries = []
        for entry in scan:
            entries.append(entry)
            if len(entries) > 64:
                return 0
    known = {'helper.exe', 'payload.exe', 'plan.json', 'ready.json', 'result.json', 'cancel.json'}
    for entry in entries:
        if not entry.name.startswith('update-') or not entry.is_dir(follow_symlinks=False):
            continue
        folder = root / entry.name
        try:
            folder = _canonical(folder)
            if folder.parent != root:
                continue
            files = list(folder.iterdir())
            names = {file.name for file in files}
            if not {'helper.exe', 'payload.exe', 'plan.json', 'result.json'} <= names or not names <= known:
                continue
            for file in files:
                canonical = _canonical(file)
                if canonical.parent != folder or not stat.S_ISREG(file.stat().st_mode):
                    raise UpdateError('Нестандартний файл у папці оновлення.')
            result = folder / 'result.json'
            if result.stat().st_size > MAX_PLAN_BYTES:
                continue
            value = json.loads(result.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or set(value) != {'success'} or value['success'] is not True:
                continue
            _load_plan(folder / 'plan.json')
            candidates.append((result.stat().st_mtime_ns, folder, files))
        except (OSError, UpdateError, ValueError):
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    deleted = 0
    for _completed, folder, files in candidates[1:3]:
        try:
            # Writing a mapped/running helper is forbidden by Windows. Test all
            # files before deleting any, then delete the helper first.
            for file in files:
                descriptor = os.open(file, os.O_RDWR | getattr(os, 'O_BINARY', 0)
                                      | getattr(os, 'O_NOFOLLOW', 0))
                os.close(descriptor)
            ordered = sorted(files, key=lambda file: file.name != 'helper.exe')
            for file in ordered:
                _canonical(file)
                file.unlink()
            folder.rmdir()
            deleted += 1
        except (OSError, UpdateError):
            continue
    return deleted


def _check_cancelled(folder: Path) -> None:
    if (folder / 'cancel.json').exists():
        raise _InstallCancelled('Оновлення скасовано.')


class _ParentHandle:
    """Keep the original process handle open; never wait on a reused PID."""
    def __init__(self, pid: int, created: int):
        self.kernel, self.types = _windows_api()
        self.handle = self.kernel.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not self.handle:
            raise UpdateError('Програма вже закрилася або її процес недоступний. Запусти оновлення ще раз.')
        try:
            if _creation_time(self.kernel, self.types, self.handle) != created:
                raise UpdateError('Процес програми змінився. Оновлення скасовано.')
        except Exception:
            self.close()
            raise

    def wait(self, folder: Path | None = None):
        deadline = time.monotonic() + PARENT_WAIT_SECONDS
        while True:
            if folder is not None:
                _check_cancelled(folder)
            status = self.kernel.WaitForSingleObject(self.handle, 200)
            if status == 0:
                return
            if status != 258 or time.monotonic() >= deadline:
                raise UpdateError('Програма не завершилася вчасно. Оновлення скасовано.')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _restart(executable: Path):
    return _popen([str(executable)], cwd=executable.parent)


def _apply_portable(plan: dict) -> None:
    target = plan['target_path']
    temporary: Path | None = None
    backup = target.with_name(f'.{target.stem}.previous-{uuid.uuid4().hex}.exe')
    moved = False
    try:
        with tempfile.NamedTemporaryFile(prefix='.MechaHub-new-', suffix='.exe', dir=target.parent,
                                         delete=False) as output:
            temporary = Path(output.name)
            with plan['payload_path'].open('rb') as source:
                shutil.copyfileobj(source, output, 64 * 1024)
            output.flush()
            os.fsync(output.fileno())
        _fingerprint(temporary, plan['payload_sha256'], plan['payload_size'])
        _fingerprint(target, plan['target_sha256'], plan['target_size'])
        os.rename(target, backup)
        moved = True
        os.rename(temporary, target)
        temporary = None
        try:
            _restart(target)
        except OSError:
            # Roll back if the replacement cannot be launched.
            target.unlink()
            os.rename(backup, target)
            moved = False
            _restart(target)
            raise _ApplicationRestarted('Нова версія не запустилася. Попередню версію відновлено.')
        # The staged helper contains the same verified previous executable and
        # is retained with the newest successful stage. Do not accumulate an
        # additional old EXE in the application directory after every update.
        try:
            backup.unlink()
        except OSError:
            # An antivirus may briefly retain the old file; a completed update
            # must not roll back an already running new app just for cleanup.
            pass
    except OSError as error:
        if moved and not target.exists():
            os.rename(backup, target)
            moved = False
        if not moved:
            _fingerprint(target, plan['target_sha256'], plan['target_size'])
            _restart(target)
        raise _ApplicationRestarted(f'Не вдалося замінити програму. Попередню копію збережено: {error}') from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _restore_old_target(plan: dict) -> None:
    """Copy the running helper to a sibling temp file, then replace the target.

    The stage may be on another drive. Never move the running helper or attempt
    a cross-volume rename; only the final sibling-to-target replace is atomic.
    """
    target = plan['target_path']
    _fingerprint(plan['helper_path'], plan['target_sha256'], plan['target_size'])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.MechaHub-restore-', suffix='.exe', dir=target.parent,
                                         delete=False) as output:
            temporary = Path(output.name)
            with plan['helper_path'].open('rb') as source:
                shutil.copyfileobj(source, output, 64 * 1024)
            output.flush()
            os.fsync(output.fileno())
        _fingerprint(temporary, plan['target_sha256'], plan['target_size'])
        os.replace(temporary, target)
        temporary = None
        _fingerprint(target, plan['target_sha256'], plan['target_size'])
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _apply_installed(plan: dict) -> None:
    target = plan['target_path']
    context = detect_installation(target, frozen=True)
    if context.mode != 'installed':
        raise UpdateError('Реєстрація встановленої програми змінилася. Оновлення скасовано.')
    _fingerprint(plan['payload_path'], plan['payload_sha256'], plan['payload_size'])
    backup = plan['helper_path']
    _fingerprint(backup, plan['target_sha256'], plan['target_size'])
    command = [str(plan['payload_path']), '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
               '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS',
               f'/DIR={target.parent}', f'/MECHAHUBUPDATE={plan["parent_pid"]}']
    try:
        process = _popen(command, cwd=plan['payload_path'].parent)
        code = process.wait()
        if code != 0:
            raise UpdateError(f'Інсталятор не завершив оновлення (код {code}).')
        _fingerprint(target, plan['new_target_sha256'], plan['new_target_size'])
        _restart(target)
    except (UpdateError, OSError) as error:
        # Even a partially applied installer cannot remove our verified backup.
        _fingerprint(backup, plan['target_sha256'], plan['target_size'])
        restore = True
        try:
            _fingerprint(target, plan['target_sha256'], plan['target_size'])
            restore = False
        except UpdateError:
            pass
        if restore:
            _restore_old_target(plan)
        _restart(target)
        raise _ApplicationRestarted(f'{error} Попередню версію відновлено й запущено.') from error


def _notify_error(message: str) -> None:
    if os.name == 'nt':
        try:
            ctypes.windll.user32.MessageBoxW(None, message, 'MechaHub — оновлення', 0x10)
        except (AttributeError, OSError):
            pass


def run_update_helper(plan_path: Path | str) -> int:
    """Run from an independent frozen helper, before QApplication is imported."""
    parent = None
    bootstrap = None
    plan = None
    parent_exited = False
    try:
        if os.name != 'nt' or not getattr(sys, 'frozen', False):
            raise UpdateError('Помічник оновлення має запускатися як Windows-програма MechaHub.')
        plan = _load_plan(plan_path)
        _check_cancelled(plan['plan_path'].parent)
        if not _same_path(_canonical(sys.executable), plan['helper_path']):
            raise UpdateError('Помічник запущено не з підготовленої копії програми.')
        _fingerprint(plan['helper_path'], plan['helper_sha256'], plan['helper_size'])
        _fingerprint(plan['payload_path'], plan['payload_sha256'], plan['payload_size'])
        _fingerprint(plan['target_path'], plan['target_sha256'], plan['target_size'])
        parent = _ParentHandle(plan['parent_pid'], plan['parent_creation'])
        if plan['bootstrap_pid']:
            bootstrap = _ParentHandle(plan['bootstrap_pid'], plan['bootstrap_creation'])
        _write_json(plan['plan_path'].parent / 'ready.json', {'ready': True})
        parent.wait(plan['plan_path'].parent)
        parent_exited = True
        if bootstrap is not None:
            bootstrap.wait(plan['plan_path'].parent)
        _check_cancelled(plan['plan_path'].parent)
        # Recheck the exact payload and target after the parent exits.
        _fingerprint(plan['payload_path'], plan['payload_sha256'], plan['payload_size'])
        _fingerprint(plan['target_path'], plan['target_sha256'], plan['target_size'])
        if plan['mode'] == 'installed':
            _apply_installed(plan)
        else:
            _apply_portable(plan)
        _write_json(plan['plan_path'].parent / 'result.json', {'success': True})
        return 0
    except (UpdateError, OSError) as error:
        if plan is not None:
            try:
                _write_json(plan['plan_path'].parent / 'result.json', {'error': str(error)})
            except OSError:
                pass
        # Before shutdown, the GUI receives the error through its readiness poll.
        # Once closed, only this helper owns the error message.
        if parent_exited and not isinstance(error, _InstallCancelled):
            if not isinstance(error, _ApplicationRestarted):
                try:
                    _fingerprint(plan['target_path'], plan['target_sha256'], plan['target_size'])
                    _restart(plan['target_path'])
                except (OSError, UpdateError):
                    pass
            _notify_error(str(error))
        return 1
    finally:
        if parent is not None:
            parent.close()
        if bootstrap is not None:
            bootstrap.close()
