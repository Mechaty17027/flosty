"""Study UI regressions: real encrypted persistence and cancellable AI proposals."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mechahub.core.study import StudyVault
from mechahub.ui.study_page import StudyPage


class StudyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'study.vault'
        settings = QSettings(str(Path(self.folder.name) / 'settings.ini'), QSettings.Format.IniFormat)
        self.page = StudyPage(vault_path=self.path, settings=settings)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.request_close()
        if self.page._worker:
            self.page._worker.cancel.set()
            self.page._worker.wait(3000)
        self.app.processEvents()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.folder.cleanup()

    def unlock(self):
        self.page.password.setText('test-password-123')
        self.page.password_confirm.setText('test-password-123')
        self.page._unlock()
        self.assertTrue(self.page.vault.unlocked)

    def wait_worker(self):
        deadline = time.monotonic() + 3
        while self.page._worker is not None and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertIsNone(self.page._worker)

    def test_setup_requires_matching_password_and_gate_clears_secrets(self):
        self.page.password.setText('test-password-123')
        self.page.password_confirm.setText('different-password')
        self.page._unlock()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.page.stack.currentIndex(), 0)
        self.unlock()
        self.assertEqual(self.page.password.text(), '')
        self.assertEqual(self.page.password_confirm.text(), '')

    def test_switching_subject_saves_encrypted_note_and_keeps_lists_separate(self):
        self.unlock()
        self.page.note_title.setText('Математика')
        self.page.note_body.setPlainText('Секретний конспект: квадратні рівняння')
        self.page.subject.setCurrentIndex(self.page.subject.findData('history'))
        self.assertEqual(self.page.notes.count(), 0)
        self.page.subject.setCurrentIndex(self.page.subject.findData('math'))
        self.assertEqual(self.page.notes.count(), 1)
        self.page.notes.setCurrentRow(0)
        self.assertIn('квадратні рівняння', self.page.note_body.toPlainText())
        self.assertNotIn('Секретний'.encode(), self.path.read_bytes())

    def test_hide_saves_then_relocks_and_clears_editors_and_chat(self):
        self.unlock()
        self.page.note_title.setText('Нова тема')
        self.page.note_body.setPlainText('Захищені дані')
        self.page._append_chat('Ти', 'Особисте запитання')
        self.page.hide()
        self.app.processEvents()
        self.assertFalse(self.page.vault.unlocked)
        self.assertEqual(self.page.note_body.toPlainText(), '')
        self.assertEqual(self.page.transcript.toPlainText(), '')
        vault = StudyVault(self.path)
        vault.unlock('test-password-123')
        self.assertEqual(vault.list_notes()[0].body, 'Захищені дані')
        vault.lock()

    def test_ai_proposal_requires_apply_and_can_be_undone(self):
        self.unlock()
        original = 'Мій початковий конспект'
        self.page.note_body.setPlainText(original)
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='Структурована пропозиція'):
            self.page._ask('structure')
            self.wait_worker()
        self.assertEqual(self.page.note_body.toPlainText(), original)
        self.assertTrue(self.page.apply_button.isEnabled())
        self.page._apply_answer()
        self.assertEqual(self.page.note_body.toPlainText(), 'Структурована пропозиція')
        self.page.note_body.undo()
        self.assertEqual(self.page.note_body.toPlainText(), original)

    def test_close_cancels_worker_without_showing_late_response(self):
        self.unlock()
        entered = threading.Event()
        def ask(*args, cancel=None, **kwargs):
            entered.set()
            cancel.wait(2)
            return 'Ця відповідь не має відобразитися'
        self.page.question.setPlainText('Поясни правило')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', side_effect=ask):
            self.page._ask('chat')
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.page.request_close())
            self.wait_worker()
        self.assertFalse(self.page.vault.unlocked)
        self.assertEqual(self.page.transcript.toPlainText(), '')
        self.assertTrue(self.page.request_close())

    def test_save_failure_prevents_navigation_and_preserves_editor(self):
        self.unlock()
        self.page.note_body.setPlainText('Не можна загубити')
        with patch.object(self.page.vault, 'save_note', side_effect=OSError('disk full')), \
             patch('mechahub.ui.study_page.QMessageBox.warning'):
            self.assertFalse(self.page.can_leave())
            self.assertFalse(self.page.lock())
        self.assertTrue(self.page.vault.unlocked)
        self.assertEqual(self.page.note_body.toPlainText(), 'Не можна загубити')

    def test_failed_save_during_note_selection_preserves_unsaved_draft(self):
        self.unlock()
        saved = self.page.vault.save_note('math', 'Збережений', 'Попередній текст')
        self.page._load_notes()
        self.page.note_body.setPlainText('Нова незбережена чернетка')
        with patch.object(self.page.vault, 'save_note', side_effect=OSError('disk full')), \
             patch('mechahub.ui.study_page.QMessageBox.warning'):
            self.page.notes.setCurrentRow(0)
        self.assertEqual(self.page.note_body.toPlainText(), 'Нова незбережена чернетка')
        self.assertTrue(self.page._dirty)
        self.assertIsNone(self.page._note_id)
        self.assertEqual(self.page.notes.currentRow(), -1)
        self.assertEqual(self.page.vault.get_note(saved.id).body, 'Попередній текст')

    def test_select_after_autosave_keeps_list_and_editor_on_same_note(self):
        self.unlock()
        first = self.page.vault.save_note('math', 'Перший', 'Перший текст')
        second = self.page.vault.save_note('math', 'Другий', 'Другий текст')
        self.page._load_notes()
        self.page._highlight_note(first.id)
        self.page._set_editor(first)
        self.page.note_body.setPlainText('Змінений перший')
        target = next(self.page.notes.item(row) for row in range(self.page.notes.count())
                      if self.page.notes.item(row).data(Qt.ItemDataRole.UserRole) == second.id)
        self.page.notes.setCurrentItem(target)
        self.assertEqual(self.page._note_id, second.id)
        self.assertEqual(self.page.note_body.toPlainText(), second.body)
        self.assertEqual(self.page.notes.currentItem().data(Qt.ItemDataRole.UserRole), second.id)
        self.assertEqual(self.page.vault.get_note(first.id).body, 'Змінений перший')

    def test_clearing_an_existing_note_saves_the_empty_body(self):
        self.unlock()
        first = self.page.vault.save_note('math', 'Назва', 'Старий текст')
        self.page._load_notes()
        self.page._set_editor(first)
        self.page.note_title.clear()
        self.page.note_body.clear()
        self.assertTrue(self.page.save_note())
        self.assertEqual(self.page.vault.get_note(first.id).body, '')
        self.assertEqual(self.page.vault.get_note(first.id).title, 'Без назви')

    def test_ai_proposal_cannot_overwrite_a_different_note_or_new_user_edits(self):
        self.unlock()
        self.page.note_body.setPlainText('Початкова чернетка')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='Пропозиція'):
            self.page._ask('structure')
            self.wait_worker()
        self.page.note_body.setPlainText('Власні нові правки')
        self.assertFalse(self.page.apply_button.isEnabled())
        self.page._apply_answer()
        self.assertEqual(self.page.note_body.toPlainText(), 'Власні нові правки')
        self.page._new_note()
        self.page.note_body.setPlainText('Інший конспект')
        self.page._apply_answer()
        self.assertEqual(self.page.note_body.toPlainText(), 'Інший конспект')

    def test_cloud_model_is_not_suggested_as_a_download_command(self):
        self.page.model.setCurrentText('gpt-oss:120b-cloud')
        self.assertNotIn('ollama pull', self.page.model_command.text())
