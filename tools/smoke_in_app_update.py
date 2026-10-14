"""CI-only packaged Windows update smoke test; retain all QA files and logs.

Use a fresh Windows GitHub runner after building the actual EXE and installer:

    python tools/smoke_in_app_update.py --exe dist/MechaHub.exe \
        --installer dist/MechaHubSetup.exe --workspace build/update-smoke

The synthetic next version uses the SAME release binaries. It exercises the
real frozen helper and registered installer without publishing a fake release.
Byte changes and rollback behavior are tested separately in unit tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Callable

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from mechahub import __version__
from mechahub.core.updates import MAX_ARTIFACT_BYTES, ReleaseManifest
from mechahub.core.updates_download import ArtifactDownloadResult
from mechahub.core.updates_install import (
    InstallContext, PreparedUpdate, UNINSTALL_KEY, cancel_installation, detect_installation,
    installation_ready, prepare_installation, start_installation,
)

STEP_TIMEOUT = 90


def _require_ci() -> None:
    if os.name != 'nt' or os.environ.get('CI', '').lower() != 'true' or os.environ.get('GITHUB_ACTIONS', '').lower() != 'true':
        raise RuntimeError('This smoke test runs only on a Windows GitHub Actions runner.')


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    if path.stat().st_size > 64 * 1024:
        raise RuntimeError(f'Oversized smoke record: {path}')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise RuntimeError(f'Invalid smoke record: {path}')
    return value


def _write_json(path: Path, value: dict) -> None:
    # The parent can inspect this record on timeout while its child is writing.
    # Replace only our own record after the entire new value has been flushed.
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('x', encoding='utf-8') as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _inside(path: Path, root: Path) -> Path:
    path = path.resolve(strict=False)
    if not path.is_relative_to(root) or path == root:
        raise RuntimeError(f'Smoke path must be inside its owned workspace: {path}')
    return path


def _registration() -> Path | None:
    """Treat a malformed registration as occupied rather than overwriting it."""
    import winreg
    found = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_READ | view) as key:
                value, kind = winreg.QueryValueEx(key, 'InstallLocation')
                if kind != winreg.REG_SZ or not isinstance(value, str) or not value:
                    raise RuntimeError('Existing MechaHub registration has an invalid installation path.')
                found.append(Path(value).resolve(strict=False))
        except FileNotFoundError:
            # QueryValueEx can also report a missing value in an existing key.
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_READ | view):
                    raise RuntimeError('Existing MechaHub registration has no installation path.')
            except FileNotFoundError:
                pass
    if not found:
        return None
    if any(os.path.normcase(str(path)) != os.path.normcase(str(found[0])) for path in found):
        raise RuntimeError('Conflicting MechaHub installation registrations exist.')
    return found[0]


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(str(right.resolve(strict=False)))


def _owned_processes(executables: list[Path], workspace: Path):
    import psutil
    names = {os.path.normcase(str(_inside(path, workspace))) for path in executables}
    owned = []
    for process in psutil.process_iter(['pid', 'exe', 'create_time']):
        try:
            exe = process.info.get('exe')
            if exe and os.path.normcase(str(Path(exe).resolve(strict=False))) in names:
                owned.append((process, process.info['create_time']))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return owned


def _stop_owned(executables: list[Path], workspace: Path) -> None:
    """Stop only exact executable paths created in this fresh owned workspace."""
    import psutil
    for process, created in _owned_processes(executables, workspace):
        try:
            if process.create_time() != created or not any(_same_path(Path(process.exe()), path) for path in executables):
                continue
            process.terminate()
            try:
                process.wait(timeout=10)
            except psutil.TimeoutExpired:
                if process.create_time() == created and any(_same_path(Path(process.exe()), path) for path in executables):
                    process.kill()
                    process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
    if _owned_processes(executables, workspace):
        raise RuntimeError('An owned smoke executable did not stop.')


def _run(command: list[str], *, env: dict[str, str], log: Path, cwd: Path,
         on_timeout: Callable[[], None] | None = None) -> None:
    with log.open('w', encoding='utf-8') as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, shell=False,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            code = process.wait(timeout=STEP_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Revoke a staged handoff BEFORE the parent harness is terminated;
            # a helper still extracting itself must never apply a late update.
            try:
                if on_timeout is not None:
                    on_timeout()
            finally:
                process.terminate()
                process.wait(timeout=10)
            raise RuntimeError(f'Timed out after {STEP_TIMEOUT}s; log: {log}')
    if code != 0:
        raise RuntimeError(f'Smoke subprocess failed with code {code}; log: {log}')


def _prepare_child(job_path: Path) -> int:
    """Keep the real parent process alive until the frozen helper owns a handle."""
    job = _json(job_path)
    root = Path(job['workspace']).resolve(strict=True)
    target = _inside(Path(job['target']), root)
    payload = Path(job['payload']).resolve(strict=True)
    record = _inside(Path(job['handoff_record']), root)
    manifest = ReleaseManifest(**job['manifest'])
    artifact = ArtifactDownloadResult(payload, payload.stat().st_size, _hash(payload))
    context = InstallContext(job['mode'], target, target.parent if job['mode'] == 'installed' else None)
    prepared = prepare_installation(manifest, artifact, context=context)
    _inside(prepared.plan_path, root)
    _inside(prepared.helper_path, root)
    _write_json(record, {'plan_path': str(prepared.plan_path), 'helper_path': str(prepared.helper_path),
                         'target_path': str(prepared.target_path), 'ready': False})
    process = None
    try:
        process = start_installation(prepared)
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            if installation_ready(prepared, process):
                _write_json(record, {'plan_path': str(prepared.plan_path), 'helper_path': str(prepared.helper_path),
                                     'target_path': str(prepared.target_path), 'helper_pid': process.pid, 'ready': True})
                # Returning lets this exact parent exit; only then can the helper
                # replace the application or start its registered installer.
                return 0
            time.sleep(0.2)
        raise RuntimeError('Frozen helper did not acknowledge the parent process within 70s.')
    except Exception:
        cancel_installation(prepared, process)
        raise


def _wait_result(plan: Path) -> dict:
    deadline = time.monotonic() + STEP_TIMEOUT
    result = plan.parent / 'result.json'
    while time.monotonic() < deadline:
        if result.exists():
            try:
                value = _json(result)
            except (OSError, ValueError, UnicodeError):
                # The helper writes its exclusive result file directly. A poll
                # may observe its creation before the complete JSON is flushed.
                time.sleep(0.2)
                continue
            if value != {'success': True}:
                raise RuntimeError(f'Packaged helper failed: {value}')
            return value
        time.sleep(0.2)
    raise RuntimeError(f'Packaged helper did not finish within {STEP_TIMEOUT}s: {plan}')


def _wait_restart(target: Path, workspace: Path) -> list[int]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        processes = _owned_processes([target], workspace)
        if processes:
            return [process.pid for process, _ in processes]
        time.sleep(0.2)
    raise RuntimeError('The helper reported success but its owned application did not restart.')


def _profile(folder: Path) -> tuple[dict[str, str], dict[Path, str]]:
    local, roaming = folder / 'local-app-data', folder / 'roaming-app-data'
    local.mkdir()
    notes = roaming / 'MechaHub'
    notes.mkdir(parents=True)
    config = notes / 'config.json'
    _write_json(config, {'smoke_sentinel': 'preserve-config', 'shortcuts': [
        {'name': 'Smoke preserved shortcut', 'path': r'C:\Windows\notepad.exe'},
    ]})
    vault = notes / 'study.vault'
    vault.write_bytes(b'Smoke vault sentinel: do not open, edit, or remove this local note file.\n')
    env = os.environ.copy()
    env.update(LOCALAPPDATA=str(local), APPDATA=str(roaming), QT_QPA_PLATFORM='offscreen')
    return env, {path: _hash(path) for path in (config, vault)}


def _assert_preserved(sentinels: dict[Path, str]) -> dict[str, str]:
    for path, checksum in sentinels.items():
        if not path.is_file() or _hash(path) != checksum:
            raise RuntimeError(f'User-data sentinel was changed or removed: {path}')
    return {path.name: checksum for path, checksum in sentinels.items()}


def _mode_test(mode: str, workspace: Path, exe: Path, installer: Path,
               manifest: dict) -> dict:
    folder = workspace / mode
    folder.mkdir()
    env, sentinels = _profile(folder)
    app = folder / 'application'
    app.mkdir()
    target = app / ('MechaHub.exe' if mode == 'installed' else 'Portable MechaHub.exe')
    handoff = folder / 'handoff.json'
    registered_here = False
    helper = None
    result = {}
    try:
        if mode == 'installed':
            if _registration() is not None:
                raise RuntimeError('Refusing to modify an existing MechaHub installation.')
            _run([str(installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                  '/NOCLOSEAPPLICATIONS', '/NORESTARTAPPLICATIONS', '/NOICONS', '/TASKS=',
                  f'/DIR={app}', f'/LOG={folder / "install.log"}'],
                 env=env, log=folder / 'install-process.log', cwd=folder)
            registered = _registration()
            if registered is None or not _same_path(registered, app):
                raise RuntimeError('Smoke installer did not register the exact owned application directory.')
            registered_here = True
            if detect_installation(target, frozen=True).mode != 'installed':
                raise RuntimeError('Packaged update could not detect the owned registered installation.')
        else:
            shutil.copyfile(exe, target)
        expected_hash = _hash(exe)
        if _hash(target) != expected_hash:
            raise RuntimeError('Initial smoke target does not match the actual built EXE.')
        job = folder / 'job.json'
        _write_json(job, {'workspace': str(workspace), 'mode': mode, 'target': str(target),
                          'payload': str(installer if mode == 'installed' else exe),
                          'handoff_record': str(handoff), 'manifest': manifest})

        def revoke_handoff():
            if handoff.is_file():
                record = _json(handoff)
                plan = _inside(Path(record['plan_path']), workspace)
                staged_helper = _inside(Path(record['helper_path']), workspace)
                if not _same_path(Path(record['target_path']), target):
                    raise RuntimeError('Cannot revoke a changed smoke target.')
                cancel_installation(PreparedUpdate(mode, manifest['version'], plan, staged_helper, target))

        _run([sys.executable, str(Path(__file__).resolve()), '--_prepare', str(job)],
             env=env, log=folder / 'handoff-process.log', cwd=PROJECT, on_timeout=revoke_handoff)
        record = _json(handoff)
        if record.get('ready') is not True:
            raise RuntimeError('Child harness exited without a verified readiness acknowledgement.')
        plan = _inside(Path(record['plan_path']), workspace)
        helper = _inside(Path(record['helper_path']), workspace)
        if not _same_path(Path(record['target_path']), target):
            raise RuntimeError('Child harness changed the owned smoke target path.')
        _wait_result(plan)
        restarted = _wait_restart(target, workspace)
        _stop_owned([target, helper], workspace)
        if not target.is_file() or _hash(target) != expected_hash:
            raise RuntimeError('Updated target does not match the expected built EXE.')
        if mode == 'installed' and (_registration() is None or not _same_path(_registration(), app)):
            raise RuntimeError('Installed update changed or removed the owned registration.')
        result = {'mode': mode, 'synthetic_version': manifest['version'], 'same_release_binary': True,
                  'ready_handshake': True, 'helper_success': True, 'target_filename': target.name,
                  'target_sha256': expected_hash, 'restart_pids': restarted,
                  'user_data_sha256': _assert_preserved(sentinels)}
    finally:
        if handoff.is_file():
            record = _json(handoff)
            if record.get('helper_path'):
                helper = _inside(Path(record['helper_path']), workspace)
        _stop_owned([target] + ([helper] if helper else []), workspace)
        if mode == 'installed':
            # Even if initial verification failed, uninstall only when the
            # registration belongs to this exact fresh, owned directory.
            registered = _registration()
            if registered is not None and _same_path(registered, app):
                registered_here = True
                uninstall = _inside(app / 'unins000.exe', workspace)
                if not uninstall.is_file():
                    raise RuntimeError('Owned smoke installer has no uninstaller; QA files retained.')
                _run([str(uninstall), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                      f'/LOG={folder / "uninstall.log"}'],
                     env=env, log=folder / 'uninstall-process.log', cwd=folder)
                if _registration() is not None:
                    raise RuntimeError('Owned uninstall left a MechaHub registration behind.')
                if target.exists():
                    raise RuntimeError('Owned uninstall did not remove the installed application.')
                _assert_preserved(sentinels)
            elif registered_here:
                raise RuntimeError('Owned registration changed unexpectedly; refusing to uninstall another directory.')
    if mode == 'installed':
        result['registration_removed_after_uninstall'] = True
        result['user_data_preserved_after_uninstall'] = True
    else:
        result['original_filename_retained'] = target.name == 'Portable MechaHub.exe'
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--installer', type=Path)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--_prepare', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    _require_ci()
    if args._prepare is not None:
        return _prepare_child(args._prepare.resolve(strict=True))
    if args.exe is None or args.installer is None or args.workspace is None:
        parser.error('--exe, --installer and --workspace are required.')
    if _registration() is not None:
        raise RuntimeError('Existing MechaHub installation detected; this test will not run.')
    exe, installer = args.exe.resolve(strict=True), args.installer.resolve(strict=True)
    if exe.name != 'MechaHub.exe' or installer.name != 'MechaHubSetup.exe' or not exe.is_file() or not installer.is_file():
        raise RuntimeError('Provide the actual built MechaHub.exe and MechaHubSetup.exe.')
    for artifact in (exe, installer):
        if not 0 < artifact.stat().st_size <= MAX_ARTIFACT_BYTES:
            raise RuntimeError('Smoke artifacts must be nonempty and no larger than 512 MiB.')
        with artifact.open('rb') as source:
            if source.read(2) != b'MZ':
                raise RuntimeError('Smoke artifacts must be actual Windows executable binaries.')
    build = (PROJECT / 'build').resolve(strict=False)
    workspace = _inside(args.workspace.resolve(strict=False), build)
    if workspace.exists():
        raise RuntimeError('Smoke workspace must be fresh; existing QA files will not be deleted.')
    workspace.mkdir(parents=True, exist_ok=False)
    parts = __version__.split('.')
    parts[-1] = str(int(parts[-1]) + 1)
    synthetic_version = '.'.join(parts)
    # No HTTP request is made. These URLs describe the current release payload;
    # the synthetic version exists only in this local, isolated test plan.
    base = f'https://github.com/Mechaty17027/flosty/releases/download/v{__version__}'
    manifest = dict(version=synthetic_version, download_url=base + '/MechaHub.exe',
                    sha256=_hash(exe), size=exe.stat().st_size, notes='CI-only synthetic update',
                    installer_download_url=base + '/MechaHubSetup.exe',
                    installer_sha256=_hash(installer), installer_size=installer.stat().st_size)
    proof = {'ci_only': True, 'current_version': __version__, 'synthetic_version': synthetic_version,
             'workspace': str(workspace), 'tests': []}
    for mode in ('portable', 'installed'):
        proof['tests'].append(_mode_test(mode, workspace, exe, installer, manifest))
        _write_json(workspace / 'proof.json', proof)
    print(json.dumps(proof, ensure_ascii=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'Packaged in-app update smoke failed: {error}', file=sys.stderr)
        raise SystemExit(1)
