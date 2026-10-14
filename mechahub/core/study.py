"""Password-encrypted local study notes; no plaintext or password is saved."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SUBJECTS = {
    'math': 'Математика',
    'history': 'Історія України',
    'english': 'Англійська мова',
    'ukrainian': 'Українська мова',
}
MIN_PASSWORD_LENGTH = 8
KDF_ITERATIONS = 600_000
MAX_NOTES = 200
MAX_BODY_LENGTH = 100_000
MAX_VAULT_BYTES = 8 * 1024 * 1024
_MAGIC = 'MechaHub Study Vault'


class StudyError(ValueError):
    """The vault is locked, invalid, changed externally, or could not be saved."""


class StudyUnlockError(StudyError):
    """Wrong password or failed ciphertext authentication; both are indistinguishable."""


@dataclass(frozen=True)
class StudyNote:
    id: str
    subject: str
    title: str
    body: str
    created_at: str
    updated_at: str


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise StudyError('Пароль має містити щонайменше 8 символів.')
    if len(password) > 1024 or '\0' in password:
        raise StudyError('Пароль надто довгий або містить недопустимий символ.')
    return password.encode('utf-8')


def _derive(password: str, salt: bytes) -> bytearray:
    return bytearray(hashlib.pbkdf2_hmac('sha256', _password_bytes(password), salt, KDF_ITERATIONS, dklen=32))


def _wipe(key: bytearray | None) -> None:
    # Python may retain temporary immutable copies; this clears our session buffer.
    if key is not None:
        key[:] = b'\0' * len(key)


def _text(value: object, label: str, limit: int, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or '\0' in value:
        raise StudyError(f'Поле «{label}» має містити текст до {limit} символів без нульового символу.')
    if nonempty and not value.strip():
        raise StudyError(f'Поле «{label}» не може бути порожнім.')
    return value


def _validate_note(data: object) -> StudyNote:
    if not isinstance(data, dict) or set(data) != set(StudyNote.__dataclass_fields__):
        raise StudyError('Пошкоджена структура конспекту.')
    if data['subject'] not in SUBJECTS:
        raise StudyError('Невідомий предмет конспекту.')
    _text(data['id'], 'ідентифікатор', 64, nonempty=True)
    _text(data['title'], 'назва', 200, nonempty=True)
    _text(data['body'], 'конспект', MAX_BODY_LENGTH)
    for field in ('created_at', 'updated_at'):
        _text(data[field], 'дата', 40, nonempty=True)
        try:
            parsed = datetime.fromisoformat(data[field])
            if parsed.tzinfo is None:
                raise ValueError('missing timezone')
        except ValueError as error:
            raise StudyError('Пошкоджена дата конспекту.') from error
    return StudyNote(**data)


class StudyVault:
    """Owns one unlocked session. Methods serialize access, including atomic writes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._key: bytearray | None = None
        self._salt: bytes | None = None
        self._notes: dict[str, StudyNote] = {}
        self._fingerprint: bytes | None = None
        self._mutex = threading.RLock()

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def unlocked(self) -> bool:
        return self._key is not None

    def lock(self) -> None:
        with self._mutex:
            _wipe(self._key)
            self._key = self._salt = self._fingerprint = None
            self._notes.clear()

    def _require_unlocked(self) -> None:
        if self._key is None:
            raise StudyError('Спершу розблокуй навчання паролем.')

    def _read(self) -> bytes:
        try:
            with self.path.open('rb') as handle:
                payload = handle.read(MAX_VAULT_BYTES + 1)
        except OSError as error:
            raise StudyError('Не вдалося прочитати сховище конспектів.') from error
        if len(payload) > MAX_VAULT_BYTES:
            raise StudyError('Сховище конспектів перевищує 8 МіБ.')
        return payload

    def create(self, password: str) -> None:
        with self._mutex:
            if self.exists:
                raise StudyError('Сховище вже існує. Використай його пароль для входу.')
            salt = os.urandom(16)
            key = _derive(password, salt)
            try:
                payload = self._encode({}, key, salt)
                self._atomic_write(payload, create=True)
            except BaseException:
                _wipe(key)
                raise
            self.lock()
            self._key, self._salt = key, salt
            self._fingerprint = hashlib.sha256(payload).digest()

    def unlock(self, password: str) -> None:
        with self._mutex:
            self.lock()
            payload = self._read()
            try:
                envelope = json.loads(payload.decode('utf-8'))
                if not isinstance(envelope, dict) or set(envelope) != {'format', 'version', 'iterations', 'salt', 'nonce', 'ciphertext'}:
                    raise ValueError('shape')
                if envelope['format'] != _MAGIC or type(envelope['version']) is not int or envelope['version'] != 1:
                    raise ValueError('format')
                if type(envelope['iterations']) is not int or envelope['iterations'] != KDF_ITERATIONS:
                    raise ValueError('KDF')
                salt = base64.b64decode(envelope['salt'], validate=True)
                nonce = base64.b64decode(envelope['nonce'], validate=True)
                ciphertext = base64.b64decode(envelope['ciphertext'], validate=True)
                if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) < 16:
                    raise ValueError('length')
            except (UnicodeError, ValueError, TypeError, KeyError) as error:
                raise StudyError('Файл сховища пошкоджений або має невідомий формат. Він не змінений.') from error
            key = _derive(password, salt)
            try:
                plaintext = AESGCM(key).decrypt(nonce, ciphertext, self._aad(salt))
            except InvalidTag as error:
                _wipe(key)
                raise StudyUnlockError('Неправильний пароль або пошкоджене сховище. Спробуй ще раз; файл не змінений.') from error
            try:
                data = json.loads(plaintext.decode('utf-8'))
                if not isinstance(data, dict) or set(data) != {'notes'} or not isinstance(data['notes'], list) or len(data['notes']) > MAX_NOTES:
                    raise StudyError('Пошкоджена структура сховища.')
                notes = [_validate_note(item) for item in data['notes']]
                by_id = {note.id: note for note in notes}
                if len(by_id) != len(notes):
                    raise StudyError('Повторювані ідентифікатори конспектів.')
            except (UnicodeError, ValueError, TypeError, KeyError) as error:
                _wipe(key)
                raise StudyError('Пошкоджений вміст сховища. Файл не змінений.') from error
            self._key, self._salt, self._notes = key, salt, by_id
            self._fingerprint = hashlib.sha256(payload).digest()

    @staticmethod
    def _aad(salt: bytes) -> bytes:
        return _MAGIC.encode('ascii') + b'\0v1\0' + str(KDF_ITERATIONS).encode('ascii') + b'\0' + salt

    def _encode(self, notes: dict[str, StudyNote], key: bytearray, salt: bytes) -> bytes:
        plaintext = json.dumps({'notes': [asdict(note) for note in notes.values()]}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, self._aad(salt))
        envelope = {'format': _MAGIC, 'version': 1, 'iterations': KDF_ITERATIONS,
                    'salt': base64.b64encode(salt).decode('ascii'), 'nonce': base64.b64encode(nonce).decode('ascii'),
                    'ciphertext': base64.b64encode(ciphertext).decode('ascii')}
        payload = json.dumps(envelope, separators=(',', ':')).encode('ascii')
        if len(payload) > MAX_VAULT_BYTES:
            raise StudyError('Сховище заповнене: його зашифрований розмір має бути до 8 МіБ.')
        return payload

    def _atomic_write(self, payload: bytes, *, create: bool = False) -> None:
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='wb', prefix='.study-', suffix='.tmp', dir=self.path.parent, delete=False) as handle:
                temporary = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if create:
                # Linking creates the destination atomically and refuses an existing file.
                os.link(temporary, self.path)
            else:
                if hashlib.sha256(self._read()).digest() != self._fingerprint:
                    raise StudyError('Сховище змінено іншим процесом. Заблокуй розділ і зайди знову перед збереженням.')
                os.replace(temporary, self.path)
        except OSError as error:
            raise StudyError('Не вдалося зберегти конспекти. Попередній файл збережений.') from error
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    def _save(self, notes: dict[str, StudyNote]) -> None:
        self._require_unlocked()
        payload = self._encode(notes, self._key, self._salt)
        self._atomic_write(payload)
        self._notes = notes
        self._fingerprint = hashlib.sha256(payload).digest()

    def list_notes(self, subject: str | None = None) -> list[StudyNote]:
        with self._mutex:
            self._require_unlocked()
            if subject is not None and subject not in SUBJECTS:
                raise StudyError('Вибери один із чотирьох предметів НМТ.')
            return sorted((note for note in self._notes.values() if subject is None or note.subject == subject), key=lambda note: note.updated_at, reverse=True)

    def get_note(self, note_id: str) -> StudyNote:
        with self._mutex:
            self._require_unlocked()
            try:
                return self._notes[note_id]
            except KeyError as error:
                raise StudyError('Конспект не знайдено.') from error

    def save_note(self, subject: str, title: str, body: str, note_id: str | None = None) -> StudyNote:
        with self._mutex:
            self._require_unlocked()
            if not isinstance(subject, str) or subject not in SUBJECTS:
                raise StudyError('Вибери один із чотирьох предметів НМТ.')
            _text(title, 'назва', 200, nonempty=True)
            _text(body, 'конспект', MAX_BODY_LENGTH)
            now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
            previous = self.get_note(note_id) if note_id is not None else None
            if previous is None and len(self._notes) >= MAX_NOTES:
                raise StudyError('Досягнуто ліміт 200 конспектів.')
            note = StudyNote(previous.id if previous else uuid4().hex, subject, title.strip(), body,
                             previous.created_at if previous else now, now)
            notes = dict(self._notes)
            notes[note.id] = note
            self._save(notes)
            return note

    def delete_note(self, note_id: str) -> None:
        with self._mutex:
            self.get_note(note_id)
            notes = dict(self._notes)
            del notes[note_id]
            self._save(notes)

    def change_password(self, new_password: str) -> None:
        with self._mutex:
            self._require_unlocked()
            salt = os.urandom(16)
            key = _derive(new_password, salt)
            try:
                payload = self._encode(self._notes, key, salt)
                self._atomic_write(payload)
            except BaseException:
                _wipe(key)
                raise
            _wipe(self._key)
            self._key, self._salt = key, salt
            self._fingerprint = hashlib.sha256(payload).digest()
