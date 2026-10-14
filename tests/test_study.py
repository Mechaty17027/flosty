"""Real encrypted storage and failure recovery in a temporary directory."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mechahub.core import study


class StudyVaultTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'study.vault'
        self.vault = study.StudyVault(self.path)
        self.addCleanup(self.vault.lock)

    def create(self):
        self.vault.create('секретний пароль 17027')

    def test_notes_are_encrypted_and_round_trip_after_relocking(self):
        self.create()
        note = self.vault.save_note('history', 'Українська історія', 'Приватний конспект: Гетьманщина, 1648 рік')
        payload = self.path.read_bytes()
        self.assertNotIn('секретний'.encode(), payload)
        self.assertNotIn('Українська історія'.encode(), payload)
        self.assertNotIn('Приватний'.encode(), payload)
        key_buffer = self.vault._key
        self.vault.lock()
        self.assertFalse(self.vault.unlocked)
        self.assertEqual(bytes(key_buffer), b'\0' * 32)
        self.assertEqual(self.vault._notes, {})
        with self.assertRaises(study.StudyError):
            self.vault.list_notes()
        self.vault.unlock('секретний пароль 17027')
        self.assertEqual(self.vault.get_note(note.id), note)
        self.assertEqual(self.vault.list_notes('history'), [note])
        self.assertEqual(self.vault.list_notes('math'), [])

    def test_wrong_password_does_not_modify_file_or_leave_unlocked(self):
        self.create()
        before = self.path.read_bytes()
        with self.assertRaises(study.StudyUnlockError):
            self.vault.unlock('wrong password')
        self.assertFalse(self.vault.unlocked)
        self.assertEqual(self.path.read_bytes(), before)
        self.vault.unlock('секретний пароль 17027')
        self.assertTrue(self.vault.unlocked)

    def test_existing_vault_is_not_overwritten_by_create(self):
        self.create()
        before = self.path.read_bytes()
        other = study.StudyVault(self.path)
        with self.assertRaises(study.StudyError):
            other.create('different password')
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_password_does_not_create_file(self):
        for password in ('', '1234567', None, '12345678\0', 'x' * 1025):
            with self.subTest(password=password), self.assertRaises(study.StudyError):
                self.vault.create(password)
            self.assertFalse(self.path.exists())

    def test_crud_keeps_note_id_and_creation_date_and_randomizes_nonce(self):
        self.create()
        note = self.vault.save_note('math', 'Чернетка', 'x² + 2x + 1')
        first = json.loads(self.path.read_bytes())
        changed = self.vault.save_note('math', 'Квадрат суми', '(x + 1)²', note_id=note.id)
        second = json.loads(self.path.read_bytes())
        self.assertEqual(changed.id, note.id)
        self.assertEqual(changed.created_at, note.created_at)
        self.assertNotEqual(first['nonce'], second['nonce'])
        self.assertEqual(len(self.vault.list_notes()), 1)
        self.vault.delete_note(note.id)
        self.assertEqual(self.vault.list_notes(), [])
        with self.assertRaises(study.StudyError):
            self.vault.get_note(note.id)
        with self.assertRaises(study.StudyError):
            self.vault.save_note('math', 'Orphan', '', note_id=note.id)

    def test_invalid_fields_and_note_limits_do_not_mutate_saved_data(self):
        self.create()
        before = self.path.read_bytes()
        for fields in [('missing', 'Title', ''), ([], 'Title', ''), ('math', '', ''),
                       ('math', 'x' * 201, ''), ('math', 'Title', 'x' * (study.MAX_BODY_LENGTH + 1)),
                       ('math', 'Title', '\0')]:
            with self.subTest(fields=fields), self.assertRaises(study.StudyError):
                self.vault.save_note(*fields)
            self.assertEqual(self.path.read_bytes(), before)
        with patch.object(study, 'MAX_NOTES', 0), self.assertRaises(study.StudyError):
            self.vault.save_note('english', 'Grammar', '')

    def test_corrupt_ciphertext_and_header_are_rejected_without_overwrite(self):
        self.create()
        original = self.path.read_bytes()
        data = json.loads(original)
        cipher = bytearray(base64.b64decode(data['ciphertext']))
        cipher[0] ^= 1
        data['ciphertext'] = base64.b64encode(cipher).decode()
        modified = json.dumps(data).encode()
        self.path.write_bytes(modified)
        with self.assertRaises(study.StudyUnlockError):
            self.vault.unlock('секретний пароль 17027')
        self.assertEqual(self.path.read_bytes(), modified)
        for payload in (b'{', b'[]', b'\xff', original.replace(b'600000', b'999999999'), original.replace(b'"version":1', b'"version":true')):
            with self.subTest(payload=payload[:100]):
                self.path.write_bytes(payload)
                with self.assertRaises(study.StudyError):
                    self.vault.unlock('секретний пароль 17027')
                self.assertFalse(self.vault.unlocked)
                self.assertEqual(self.path.read_bytes(), payload)

    def test_authenticated_but_invalid_plaintext_is_rejected(self):
        self.create()
        envelope = json.loads(self.path.read_bytes())
        nonce = base64.b64decode(envelope['nonce'])
        salt = base64.b64decode(envelope['salt'])
        envelope['ciphertext'] = base64.b64encode(AESGCM(self.vault._key).encrypt(nonce, b'{"notes":[null]}', self.vault._aad(salt))).decode()
        self.path.write_text(json.dumps(envelope))
        with self.assertRaises(study.StudyError):
            self.vault.unlock('секретний пароль 17027')
        self.assertFalse(self.vault.unlocked)

    def test_atomic_save_failure_preserves_both_file_and_session_notes(self):
        self.create()
        saved = self.vault.save_note('ukrainian', 'Мова', 'Правила')
        before = self.path.read_bytes()
        with patch.object(study.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(study.StudyError):
                self.vault.save_note('ukrainian', 'Мова', 'Нові правила', saved.id)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.vault.get_note(saved.id), saved)
        self.assertEqual(list(self.path.parent.glob('.study-*.tmp')), [])

    def test_external_edit_prevents_stale_save(self):
        self.create()
        other = study.StudyVault(self.path)
        self.addCleanup(other.lock)
        other.unlock('секретний пароль 17027')
        other.save_note('english', 'Other process', 'Present Perfect')
        before = self.path.read_bytes()
        with self.assertRaises(study.StudyError):
            self.vault.save_note('math', 'Stale session', '')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.vault.list_notes(), [])

    def test_password_change_preserves_notes_and_invalidates_old_password(self):
        self.create()
        note = self.vault.save_note('math', 'Рівняння', '2x=4')
        old_key = self.vault._key
        self.vault.change_password('новий довгий пароль')
        self.assertEqual(bytes(old_key), b'\0' * 32)
        with self.assertRaises(study.StudyUnlockError):
            self.vault.unlock('секретний пароль 17027')
        self.vault.unlock('новий довгий пароль')
        self.assertEqual(self.vault.get_note(note.id), note)

    def test_oversized_file_is_bounded_and_rejected(self):
        with patch.object(study, 'MAX_VAULT_BYTES', 8):
            self.path.write_bytes(b'x' * 20)
            with self.assertRaises(study.StudyError):
                self.vault.unlock('секретний пароль 17027')


if __name__ == '__main__':
    unittest.main()
