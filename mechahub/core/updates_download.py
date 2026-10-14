"""Download verified release artifacts without installing or executing them."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from http.client import HTTPException
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
import time
from typing import Callable
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from mechahub import __version__
from mechahub.core.updates import (
    MAX_ARTIFACT_BYTES, ReleaseManifest, REQUEST_TIMEOUT, UpdateCancelled, UpdateError,
    _HttpsRedirectHandler, validate_manifest_url, version_key,
)

DOWNLOAD_TIMEOUT = 5 * 60.0
CHUNK_BYTES = 64 * 1024
ARTIFACT_SUFFIXES = {'.exe', '.msi', '.msix', '.zip'}
MAX_CACHE_CANDIDATES = 32
MAX_CACHE_VERIFY_BYTES = 2 * MAX_ARTIFACT_BYTES


@dataclass(frozen=True)
class ArtifactDownloadResult:
    path: Path
    bytes_received: int
    sha256: str


def _check_pending(cancel_event: threading.Event | None, deadline: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise UpdateCancelled()
    if time.monotonic() >= deadline:
        raise UpdateError('Завантаження триває надто довго. Спробуй пізніше.')


def _artifact_details(manifest: ReleaseManifest) -> tuple[str, str, str, int | None]:
    # Validate here as well because callers may construct ReleaseManifest directly.
    version_key(manifest.version)
    url = validate_manifest_url(manifest.download_url)
    checksum = manifest.sha256
    if not isinstance(checksum, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', checksum):
        raise UpdateError('Для завантаження потрібна контрольна сума SHA-256 із 64 символів.')
    size = manifest.size
    if size is not None and (type(size) is not int or not 0 <= size <= MAX_ARTIFACT_BYTES):
        raise UpdateError('Розмір оновлення має бути цілим числом від 0 до 512 МіБ.')
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    if suffix not in ARTIFACT_SUFFIXES:
        raise UpdateError('Посилання має вести на файл EXE, MSI, MSIX або ZIP.')
    return url, checksum.lower(), f'MechaHub-{manifest.version}{suffix}', size


def _content_length(response) -> int | None:
    value = response.headers.get('Content-Length')
    if value is None:
        return None
    try:
        size = int(value)
    except (TypeError, ValueError) as error:
        raise UpdateError('Сервер повернув некоректний розмір оновлення.') from error
    if not 0 <= size <= MAX_ARTIFACT_BYTES:
        raise UpdateError('Файл оновлення перевищує дозволені 512 МіБ.')
    return size


def _candidate_path(destination: Path, index: int) -> Path:
    return destination if index == 1 else destination.with_name(
        f'{destination.stem} ({index}){destination.suffix}')


def _regular_file(info: os.stat_result) -> bool:
    # A Windows reparse point can be a regular-looking file as well as a link.
    return stat.S_ISREG(info.st_mode) and not (
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _file_signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _cached_release(
    folder: Path, filename: str, expected_hash: str, expected_size: int | None,
    cancel_event: threading.Event | None, deadline: float,
    progress: Callable[[int, int | None], None] | None,
) -> ArtifactDownloadResult | None:
    """Reuse only an unchanged, regular file under an exact generated name.

    Limit both the number of candidate names and total bytes hashed. A corrupt
    or unrelated file is preserved, and does not prevent a fresh download.
    """
    try:
        target_folder = Path(folder).expanduser().resolve(strict=True)
    except OSError:
        return None
    destination = target_folder / filename
    budget = MAX_CACHE_VERIFY_BYTES
    for index in range(1, MAX_CACHE_CANDIDATES + 1):
        _check_pending(cancel_event, deadline)
        candidate = _candidate_path(destination, index)
        try:
            before = candidate.lstat()
            if (not _regular_file(before) or not 0 <= before.st_size <= MAX_ARTIFACT_BYTES
                    or before.st_size > budget
                    or (expected_size is not None and before.st_size != expected_size)):
                continue
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            with os.fdopen(os.open(candidate, flags), 'rb') as source:
                opened = os.fstat(source.fileno())
                # On Windows, where O_NOFOLLOW is unavailable, compare the open
                # handle with lstat before reading any bytes, and check again
                # after hashing. Replaced files and reparse points are rejected.
                if not _regular_file(opened) or _file_signature(opened) != _file_signature(before):
                    continue
                hasher = hashlib.sha256()
                received = 0
                while True:
                    _check_pending(cancel_event, deadline)
                    chunk = source.read(min(CHUNK_BYTES, before.st_size + 1 - received))
                    _check_pending(cancel_event, deadline)
                    if not chunk:
                        break
                    received += len(chunk)
                    budget -= len(chunk)
                    if received > before.st_size or budget < 0:
                        break
                    hasher.update(chunk)
                after = os.fstat(source.fileno())
            final = candidate.lstat()
            actual_hash = hasher.hexdigest()
            if (received != before.st_size or not _regular_file(final)
                    or _file_signature(after) != _file_signature(before)
                    or _file_signature(final) != _file_signature(before)
                    or not hmac.compare_digest(actual_hash, expected_hash)):
                continue
            _check_pending(cancel_event, deadline)
            if progress is not None:
                progress(received, received)
            _check_pending(cancel_event, deadline)
            return ArtifactDownloadResult(candidate, received, actual_hash)
        except OSError:
            # Missing, locked, or unreadable candidates are not a valid cache.
            continue
    return None


def _publish_exclusive(temporary: Path, destination: Path,
                       cancel_event: threading.Event | None, deadline: float) -> Path:
    # Windows rename and POSIX link both fail if a name is already occupied.
    # Never use replace() or POSIX rename(), which could overwrite another file.
    for index in range(1, 1001):
        _check_pending(cancel_event, deadline)
        candidate = _candidate_path(destination, index)
        if os.path.lexists(candidate):
            continue
        try:
            if os.name == 'nt':
                os.rename(temporary, candidate)
            else:
                os.link(temporary, candidate, follow_symlinks=False)
            return candidate
        except FileExistsError:
            # Even an immediately disappearing collision should try the next
            # name, instead of retrying the same one indefinitely.
            continue
        except OSError:
            # A different writer may have claimed the name after lexists().
            if not os.path.lexists(candidate):
                raise
    raise UpdateError('Не вдалося знайти вільну назву для файлу оновлення.')


def download_release(
    manifest: ReleaseManifest,
    folder: Path,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, int | None], None] | None = None,
) -> ArtifactDownloadResult:
    """Stream an HTTPS artifact into *folder*, verify it, and publish exclusively.

    Socket operations use a four-second timeout. Cancellation and a five-minute
    deadline are checked between chunks. All downloaded bytes are bounded by
    512 MiB even when the server omits or understates Content-Length. A result
    contains a saved file only; this function never starts an installer. An
    already saved artifact is reused after streaming checksum verification,
    before opening a network connection.
    """
    url, expected_hash, filename, expected_size = _artifact_details(manifest)
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    _check_pending(cancel_event, deadline)
    request = Request(url, headers={
        'Accept': 'application/octet-stream',
        'Accept-Encoding': 'identity',
        'User-Agent': f'MechaHub/{__version__}',
    })
    temporary: Path | None = None
    try:
        cached = _cached_release(folder, filename, expected_hash, expected_size,
                                 cancel_event, deadline, progress)
        if cached is not None:
            return cached
        with build_opener(_HttpsRedirectHandler()).open(request, timeout=REQUEST_TIMEOUT) as response:
            validate_manifest_url(response.geturl())
            _check_pending(cancel_event, deadline)
            declared_size = _content_length(response)
            if expected_size is not None and declared_size is not None and declared_size != expected_size:
                raise UpdateError('Розмір файлу на сервері не відповідає інформації про оновлення.')
            total = expected_size if expected_size is not None else declared_size
            target_folder = Path(folder).expanduser()
            target_folder.mkdir(parents=True, exist_ok=True)
            target_folder = target_folder.resolve(strict=True)
            hasher = hashlib.sha256()
            received = 0
            with tempfile.NamedTemporaryFile(
                mode='wb', prefix=f'.MechaHub-{manifest.version}-', suffix='.part',
                dir=target_folder, delete=False,
            ) as output:
                temporary = Path(output.name)
                if progress is not None:
                    progress(0, total)
                read_chunk = getattr(response, 'read1', response.read)
                while True:
                    _check_pending(cancel_event, deadline)
                    chunk = read_chunk(min(CHUNK_BYTES, MAX_ARTIFACT_BYTES + 1 - received))
                    _check_pending(cancel_event, deadline)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_ARTIFACT_BYTES:
                        raise UpdateError('Файл оновлення перевищує дозволені 512 МіБ.')
                    if total is not None and received > total:
                        raise UpdateError('Розмір завантаженого файлу не відповідає очікуваному.')
                    hasher.update(chunk)
                    output.write(chunk)
                    if progress is not None:
                        progress(received, total)
                if total is not None and received != total:
                    raise UpdateError('Файл оновлення завантажено не повністю: розмір не відповідає очікуваному.')
                actual_hash = hasher.hexdigest()
                if not hmac.compare_digest(actual_hash, expected_hash):
                    raise UpdateError('Контрольна сума SHA-256 не збігається. Файл оновлення відхилено.')
                _check_pending(cancel_event, deadline)
                output.flush()
                os.fsync(output.fileno())
            _check_pending(cancel_event, deadline)
            path = _publish_exclusive(temporary, target_folder / filename, cancel_event, deadline)
            return ArtifactDownloadResult(path, received, actual_hash)
    except (URLError, OSError, HTTPException) as error:
        if cancel_event is not None and cancel_event.is_set():
            raise UpdateCancelled() from error
        raise UpdateError(f'Не вдалося завантажити оновлення: {error}') from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                raise UpdateError(f'Не вдалося прибрати тимчасовий файл оновлення: {temporary}') from error
