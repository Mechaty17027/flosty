"""Configured release-manifest checks without running an installer."""
from __future__ import annotations

from dataclasses import dataclass, replace
from http.client import HTTPException
import json
import re
import threading
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mechahub import __version__

MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
REQUEST_TIMEOUT = 4.0
DEFAULT_MANIFEST_URL = 'https://github.com/Mechaty17027/flosty/releases/latest/download/update.json'

LOCAL_HISTORY = (
    ('0.6.0', (
        'Автоматична перевірка нових версій при запуску й кожні 6 годин.',
        'Пропозиція оновлення та встановлення зсередини програми.',
        'Оновлення наявної встановленої або переносної копії з перезапуском.',
        'Конспекти й налаштування зберігаються; ручне видалення не потрібне.',
    )),
    ('0.5.0', (
        'Навчання з чотирьох предметів НМТ та джерела iLearn і УЦОЯО.',
        'Конспекти з паролем і локальним шифруванням.',
        'Безоплатний локальний ШІ через Ollama для пояснень і правок.',
        'Курси НБУ, валютний калькулятор і графіки у гривнях.',
        'Живі графіки процесора, пам’яті, диска та мережі.',
    )),
    ('0.4.0', (
        'Новий дизайн із чіткою навігацією та узгодженими іконками.',
        'Структурований огляд стану ПК, програм, файлів та інтернету.',
        'Окреме вікно історії змін і перевірки оновлень.',
        'Автоматичне завантаження нових версій із перевіркою цілісності файла.',
    )),
    ('0.3.1', (
        'Пошук і перейменування ярликів, додавання власного файлу.',
        'Пошук установлених програм у фоновому потоці.',
        'Надійніше збереження, сортування файлів і завершення мережевого тесту.',
    )),
    ('0.3.0', (
        'Вкладка «Мережа» з анімованим спідометром.',
        'Download, upload, ping, jitter, packet loss і лічильники пакетів.',
    )),
)


class UpdateError(ValueError):
    """An invalid source, failed request, or invalid release manifest."""


class UpdateCancelled(Exception):
    """The user cancelled a pending check."""


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    download_url: str
    notes: str = ''
    sha256: str = ''
    size: int | None = None
    installer_download_url: str = ''
    installer_sha256: str = ''
    installer_size: int | None = None


def version_key(version: str) -> tuple[int, int, int, int]:
    """Compare numeric versions, padding omitted parts with zero."""
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,9}){0,3}', version):
        raise UpdateError('Версія має містити від одного до чотирьох чисел, розділених крапками.')
    parts = tuple(int(part) for part in version.split('.'))
    return parts + (0,) * (4 - len(parts))


def is_newer_version(version: str, current: str = __version__) -> bool:
    return version_key(version) > version_key(current)


def _web_url(value: object, *, https_only: bool) -> str:
    if not isinstance(value, str):
        raise UpdateError('Адреса має бути текстом.')
    value = value.strip()
    if not value or any(ord(character) <= 32 or ord(character) == 127 for character in value):
        raise UpdateError('Вкажи повну вебадресу без пробілів і керівних символів.')
    try:
        parsed = urlsplit(value)
        schemes = {'https'} if https_only else {'http', 'https'}
        if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('unsupported URL')
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError('invalid port')
    except ValueError as error:
        protocol = 'HTTPS' if https_only else 'HTTP або HTTPS'
        raise UpdateError(f'Потрібна повна адреса {protocol} без логіна й пароля.') from error
    return value


def validate_manifest_url(url: str) -> str:
    return _web_url(url, https_only=True)


def _installer_details(url: object, checksum: object, size: object) -> tuple[str, str, int]:
    """Require a complete installer artifact, including an exact bounded size."""
    url = _web_url(url, https_only=True)
    if not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', checksum):
        raise UpdateError('Контрольна сума installer_sha256 має містити 64 шістнадцяткові символи.')
    if type(size) is not int or not 1 <= size <= MAX_ARTIFACT_BYTES:
        raise UpdateError('Розмір installer_size має бути цілим числом від 1 байта до 512 МіБ.')
    return url, checksum.lower(), size


def select_artifact(manifest: ReleaseManifest, installed: bool) -> ReleaseManifest:
    """Select the verified installer for an installed app, otherwise the portable EXE.

    Older manifests remain readable. Page-only manifests cannot be selected
    without a checksum, and installed apps require complete installer metadata.
    Do not mutate the original manifest: callers may still need its portable
    artifact as a separately verified download.
    """
    version_key(manifest.version)
    if not installed:
        url = _web_url(manifest.download_url, https_only=True)
        checksum = manifest.sha256
        if not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', checksum):
            raise UpdateError('Для оновлення потрібна контрольна сума SHA-256 із 64 символів.')
        if manifest.size is not None and (type(manifest.size) is not int
                                         or not 1 <= manifest.size <= MAX_ARTIFACT_BYTES):
            raise UpdateError('Розмір оновлення має бути цілим числом від 1 байта до 512 МіБ.')
        if url != manifest.download_url or checksum.lower() != checksum:
            return replace(manifest, download_url=url, sha256=checksum.lower())
        return manifest
    if not manifest.installer_download_url and not manifest.installer_sha256 and manifest.installer_size is None:
        raise UpdateError('Цей реліз не містить інсталятора для оновлення встановленої програми.')
    url, checksum, size = _installer_details(
        manifest.installer_download_url, manifest.installer_sha256, manifest.installer_size,
    )
    return replace(manifest, download_url=url, sha256=checksum, size=size)


def parse_manifest(payload: bytes) -> ReleaseManifest:
    if len(payload) > MAX_MANIFEST_BYTES:
        raise UpdateError('Файл інформації про версію перевищує 64 КіБ.')
    try:
        data = json.loads(payload.decode('utf-8-sig'))
    except (UnicodeError, ValueError) as error:
        raise UpdateError('Сервер повернув некоректний JSON у кодуванні UTF-8.') from error
    if not isinstance(data, dict):
        raise UpdateError('Інформація про версію має бути JSON-об’єктом.')
    version = data.get('version')
    version_key(version)
    checksum = data.get('sha256', '')
    if not isinstance(checksum, str) or (checksum and not re.fullmatch(r'[0-9a-fA-F]{64}', checksum)):
        raise UpdateError('Контрольна сума sha256 має містити 64 шістнадцяткові символи.')
    # Older page-only manifests remain readable. A downloadable artifact must
    # use HTTPS and include a checksum before it can be saved automatically.
    download_url = _web_url(data.get('download_url'), https_only=bool(checksum))
    size = data.get('size')
    if size is not None and (not isinstance(size, int) or isinstance(size, bool)
                             or not 0 <= size <= MAX_ARTIFACT_BYTES):
        raise UpdateError('Розмір файла size має бути цілим числом від 0 до 512 МіБ.')
    notes = data.get('notes', '')
    if not isinstance(notes, str) or '\0' in notes:
        raise UpdateError('Опис змін notes має бути текстом.')
    installer_url, installer_checksum, installer_size = '', '', None
    installer_fields = ('installer_download_url', 'installer_sha256', 'installer_size')
    if any(field in data for field in installer_fields):
        if not all(field in data for field in installer_fields):
            raise UpdateError('Інсталятор потребує всіх трьох полів: адреси, SHA-256 та розміру.')
        installer_url, installer_checksum, installer_size = _installer_details(
            *(data[field] for field in installer_fields),
        )
    return ReleaseManifest(version, download_url, notes.strip(), checksum.lower(), size,
                           installer_url, installer_checksum, installer_size)


class _HttpsRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        validate_manifest_url(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise UpdateCancelled()


def fetch_manifest(url: str, cancel_event: threading.Event | None = None) -> ReleaseManifest:
    """Read only a small HTTPS manifest; never fetch release binaries.

    Socket operations have a four-second timeout. A deadline and cancellation
    are checked between response chunks, including for a slowly streaming body.
    """
    url = validate_manifest_url(url)
    _check_cancel(cancel_event)
    request = Request(url, headers={
        'Accept': 'application/json',
        'User-Agent': f'MechaHub/{__version__}',
    })
    deadline = time.monotonic() + REQUEST_TIMEOUT
    try:
        with build_opener(_HttpsRedirectHandler()).open(request, timeout=REQUEST_TIMEOUT) as response:
            validate_manifest_url(response.geturl())
            _check_cancel(cancel_event)
            size_header = response.headers.get('Content-Length')
            if size_header is not None:
                try:
                    declared_size = int(size_header)
                except (TypeError, ValueError) as error:
                    raise UpdateError('Сервер повернув некоректний розмір файлу.') from error
                if not 0 <= declared_size <= MAX_MANIFEST_BYTES:
                    raise UpdateError('Файл інформації про версію перевищує 64 КіБ.')
            chunks = []
            received = 0
            read_chunk = getattr(response, 'read1', response.read)
            while True:
                _check_cancel(cancel_event)
                if time.monotonic() >= deadline:
                    raise UpdateError('Сервер відповідає надто повільно. Спробуй пізніше.')
                chunk = read_chunk(min(4096, MAX_MANIFEST_BYTES + 1 - received))
                _check_cancel(cancel_event)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_MANIFEST_BYTES:
                    raise UpdateError('Файл інформації про версію перевищує 64 КіБ.')
                chunks.append(chunk)
            return parse_manifest(b''.join(chunks))
    except (URLError, OSError, HTTPException) as error:
        _check_cancel(cancel_event)
        raise UpdateError(f'Не вдалося отримати інформацію про версію: {error}') from error
