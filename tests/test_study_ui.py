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
from mechahub.core.study_ai import StudyAIConnectionError, StudyAIError
from mechahub.ui.study_page import StudyPage


class StudyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'study.vault'
        self.settings = QSettings(str(Path(self.folder.name) / 'settings.ini'), QSettings.Format.IniFormat)
        self.page = StudyPage(vault_path=self.path, settings=self.settings)
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

    def test_hide_saves_notes_and_keeps_unlocked_session_for_return_navigation(self):
        self.unlock()
        self.page.note_title.setText('Нова тема')
        self.page.note_body.setPlainText('Захищені дані')
        self.page._append_chat('Ти', 'Особисте запитання')
        self.page.hide()
        self.app.processEvents()
        self.assertTrue(self.page.vault.unlocked)
        self.assertEqual(self.page.note_body.toPlainText(), 'Захищені дані')
        self.assertIn('Особисте запитання', self.page.transcript.toPlainText())
        self.page.show()
        self.app.processEvents()
        self.assertEqual(self.page.stack.currentIndex(), 1)
        self.assertEqual(self.page.password.text(), '')
        self.assertFalse(any('password' in key.lower() for key in self.settings.allKeys()))
        vault = StudyVault(self.path)
        vault.unlock('test-password-123')
        self.assertEqual(vault.list_notes()[0].body, 'Захищені дані')
        vault.lock()

    def test_explicit_lock_clears_all_chat_modes_and_passwords_and_closes_session(self):
        self.unlock()
        self.page.note_body.setPlainText('Приватний конспект')
        self.page.question.setPlainText('Приватне запитання для НМТ')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='Приватна відповідь для НМТ'):
            self.page._ask('chat')
            self.wait_worker()
        self.page.general_chat_button.click()
        self.page.question.setPlainText('Приватна розмова')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='Приватна відповідь у розмові'):
            self.page._ask('chat')
            self.wait_worker()
        self.assertTrue(self.page._conversation_states)
        self.assertTrue(self.page.lock())
        self.assertFalse(self.page.vault.unlocked)
        self.assertEqual(self.page.stack.currentIndex(), 0)
        self.assertEqual(self.page.note_body.toPlainText(), '')
        self.assertEqual(self.page.transcript.toPlainText(), '')
        self.assertEqual(self.page.question.toPlainText(), '')
        self.assertEqual(self.page._history, [])
        self.assertEqual(self.page._conversation_states, {})
        self.assertEqual(self.page.password.text(), '')
        self.assertEqual(self.page.password_confirm.text(), '')
        saved_settings = '\n'.join(str(self.settings.value(key)) for key in self.settings.allKeys())
        self.assertNotIn('test-password-123', saved_settings)
        self.assertNotIn('Приват', saved_settings)
        self.assertNotIn('Приватний'.encode(), self.path.read_bytes())

    def test_hiding_page_does_not_cancel_pending_ai_and_answer_is_ready_on_return(self):
        self.unlock()
        entered, release = threading.Event(), threading.Event()

        def ask(*args, **kwargs):
            entered.set()
            release.wait(2)
            return 'Відповідь підготовлена у фоні'

        self.page.question.setPlainText('Поясни тему')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', side_effect=ask):
            try:
                self.page._ask('chat')
                self.assertTrue(entered.wait(1))
                self.page.hide()
                self.app.processEvents()
                self.assertTrue(self.page.vault.unlocked)
                self.assertFalse(self.page._worker.cancel.is_set())
                release.set()
                self.wait_worker()
            finally:
                release.set()
        self.page.show()
        self.app.processEvents()
        self.assertEqual(self.page.stack.currentIndex(), 1)
        self.assertIn('Відповідь підготовлена у фоні', self.page.transcript.toPlainText())

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

    def test_failed_ai_request_preserves_question_and_shows_error_in_chat(self):
        self.unlock()
        question = 'Поясни квадратні рівняння та наведи приклад'
        error = 'Немає з’єднання з Ollama. Встанови й запусти її.'
        self.page.question.setPlainText(question)
        with patch('mechahub.ui.study_page.StudyAIClient.ask', side_effect=StudyAIConnectionError(error)):
            self.page._ask('chat')
            self.wait_worker()
        self.assertEqual(self.page.question.toPlainText(), question)
        self.assertIn(error, self.page.transcript.toPlainText())
        self.assertIn(error, self.page.chat_ai_status.text())
        self.assertTrue(self.page.chat_ai_status.isVisible())
        self.assertTrue(self.page.send_button.isEnabled())
        self.page.setup_button.click()
        self.assertEqual(self.page.tabs.tabText(self.page.tabs.currentIndex()), 'Безоплатний ШІ')

    def test_success_keeps_new_question_written_while_ai_was_working(self):
        self.unlock()
        entered, release = threading.Event(), threading.Event()

        def ask(*args, **kwargs):
            entered.set()
            release.wait(2)
            return 'Відповідь на перше запитання'

        self.page.question.setPlainText('Перше запитання')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', side_effect=ask):
            try:
                self.page._ask('chat')
                self.assertTrue(entered.wait(1))
                self.page.question.setPlainText('Нова чернетка під час очікування')
                release.set()
                self.wait_worker()
            finally:
                release.set()
        self.assertEqual(self.page.question.toPlainText(), 'Нова чернетка під час очікування')
        self.assertIn('Відповідь на перше запитання', self.page.transcript.toPlainText())

    def test_success_clears_only_the_question_that_received_an_answer(self):
        self.unlock()
        self.page.question.setPlainText('Поясни правило')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='Пояснення правила'):
            self.page._ask('chat')
            self.wait_worker()
        self.assertEqual(self.page.question.toPlainText(), '')
        self.assertIn('Пояснення правила', self.page.transcript.toPlainText())

    def test_cancelled_request_finishes_with_stopped_status_and_preserves_question(self):
        self.unlock()
        entered = threading.Event()

        def ask(*args, cancel=None, **kwargs):
            entered.set()
            cancel.wait(2)
            return 'Скасована відповідь'

        question = 'Поясни дроби'
        self.page.question.setPlainText(question)
        with patch('mechahub.ui.study_page.StudyAIClient.ask', side_effect=ask):
            self.page._ask('chat')
            self.assertTrue(entered.wait(1))
            self.page._stop()
            self.wait_worker()
        self.assertIn('зупинено', self.page.status.text().lower())
        self.assertIn('зупинено', self.page.chat_ai_status.text().lower())
        self.assertEqual(self.page.question.toPlainText(), question)
        self.assertNotIn('Скасована відповідь', self.page.transcript.toPlainText())
        self.assertTrue(self.page.send_button.isEnabled())
        self.assertFalse(self.page.stop_button.isEnabled())

    def test_probe_uses_installed_model_when_default_is_missing_and_no_explicit_choice(self):
        self.unlock()
        self.assertFalse(self.settings.contains('study/model'))
        with patch('mechahub.ui.study_page.StudyAIClient.list_models', return_value=['llama3.2:3b']) as probe:
            self.page._probe()
            self.wait_worker()
        probe.assert_called_once()
        self.assertEqual(self.page.model.currentText(), 'llama3.2:3b')
        self.assertIn('готова', self.page.ai_status.text())
        self.assertIn('llama3.2:3b', self.page.chat_ai_status.text())

    def test_probe_preserves_explicit_saved_model_choice_even_if_not_installed(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.settings.setValue('study/model', 'qwen3:8b')
        self.page = StudyPage(vault_path=self.path, settings=self.settings)
        self.page.show()
        self.app.processEvents()
        self.unlock()
        with patch('mechahub.ui.study_page.StudyAIClient.list_models', return_value=['llama3.2:3b']):
            self.page._probe()
            self.wait_worker()
        self.assertEqual(self.page.model.currentText(), 'qwen3:8b')
        self.assertIn('ще не завантажена', self.page.ai_status.text())
        self.assertIn('Завантажити вибрану модель', self.page.chat_ai_status.text())

    def test_download_model_reports_progress_then_marks_model_ready(self):
        self.unlock()
        self.page.tabs.setCurrentIndex(3)
        entered, release = threading.Event(), threading.Event()
        model = self.page.model.currentText()

        def pull(*args, progress=None, **kwargs):
            progress({'status': 'pulling model', 'total': 100 * 1024 ** 2, 'completed': 50 * 1024 ** 2})
            entered.set()
            release.wait(2)
            progress({'status': 'success', 'total': None, 'completed': None})
            return [model]

        with patch('mechahub.ui.study_page.StudyAIClient.list_models', return_value=[]), \
                patch('mechahub.ui.study_page.StudyAIClient.pull_model', side_effect=pull) as download, \
                patch('mechahub.ui.study_page.start_ollama') as start:
            try:
                self.page._download_model()
                self.assertTrue(entered.wait(1))
                QTest.qWait(20)
                self.assertTrue(self.page.model_progress.isVisible())
                self.assertEqual(self.page.model_progress.value(), 50)
                self.assertFalse(self.page.download_model_button.isEnabled())
                self.assertTrue(self.page.setup_stop_button.isEnabled())
                release.set()
                self.wait_worker()
                download.assert_called_once()
                start.assert_not_called()
            finally:
                release.set()
        self.assertTrue(self.page.model_progress.isHidden())
        self.assertTrue(self.page.download_model_button.isEnabled())
        self.assertIn('готова', self.page.ai_status.text())
        self.assertIn(model, self.page.chat_ai_status.text())

    def test_start_service_when_ollama_is_missing_shows_install_hint_without_external_execution(self):
        self.unlock()
        self.page.tabs.setCurrentIndex(3)
        message = 'Ollama не встановлена. Встанови Ollama для Windows і спробуй знову.'
        with patch('mechahub.ui.study_page.StudyAIClient.list_models',
                   side_effect=StudyAIConnectionError('Немає з’єднання')), \
                patch('mechahub.ui.study_page.start_ollama', side_effect=OSError(message)) as start:
            self.page._start_service()
            self.wait_worker()
            start.assert_called_once()
        self.assertIn(message, self.page.ai_status.text())
        self.assertIn(message, self.page.chat_ai_status.text())
        self.assertTrue(self.page.start_service_button.isEnabled())

    def test_start_service_reuses_already_running_ollama_without_starting_another_process(self):
        self.unlock()
        with patch('mechahub.ui.study_page.StudyAIClient.list_models', return_value=['qwen3:4b']), \
                patch('mechahub.ui.study_page.start_ollama') as start:
            self.page._start_service()
            self.wait_worker()
            start.assert_not_called()
        self.assertIn('готова', self.page.ai_status.text())

    def test_general_chat_is_separate_from_exam_context_and_cannot_apply_notes(self):
        self.unlock()
        self.page.note_body.setPlainText('Мій приватний академічний конспект')
        self.page.question.setPlainText('Академічне запитання')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='Академічна відповідь'):
            self.page._ask('chat')
            self.wait_worker()
        self.page.general_chat_button.click()
        self.assertEqual(self.page.subject.count(), 4)
        self.assertFalse(self.page.subject.isEnabled())
        self.assertEqual(self.page._history, [])
        self.assertNotIn('Академічна відповідь', self.page.transcript.toPlainText())
        self.assertTrue(self.page.apply_button.isHidden())
        self.page.question.setPlainText('Поговорімо про музику')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='Вільна розмова про музику') as ask:
            self.page._ask('chat')
            self.wait_worker()
        self.assertIsNone(ask.call_args.args[0])
        self.assertEqual(ask.call_args.kwargs['mode'], 'general')
        self.assertEqual(ask.call_args.kwargs['history'], [])
        self.assertEqual(ask.call_args.kwargs['note_text'], '')
        self.assertEqual(ask.call_args.kwargs['sources'], ())
        self.assertFalse(self.page.apply_button.isEnabled())
        self.page._apply_answer()
        self.assertEqual(self.page.note_body.toPlainText(), 'Мій приватний академічний конспект')
        self.page.study_chat_button.click()
        self.assertTrue(self.page.subject.isEnabled())
        self.assertIn('Академічна відповідь', self.page.transcript.toPlainText())
        self.assertNotIn('Вільна розмова про музику', self.page.transcript.toPlainText())
        self.page.general_chat_button.click()
        self.assertIn('Вільна розмова про музику', self.page.transcript.toPlainText())
        self.assertNotIn('Академічна відповідь', self.page.transcript.toPlainText())

    def test_reply_language_is_explicitly_passed_and_each_language_has_separate_history(self):
        self.unlock()
        self.page.reply_language.setCurrentIndex(self.page.reply_language.findData('uk'))
        self.page.question.setPlainText('Explain “present perfect”')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='Пояснення українською') as ask:
            self.page._ask('chat')
            self.wait_worker()
        self.assertEqual(ask.call_args.kwargs['language'], 'uk')
        self.assertEqual(self.settings.value('study/reply_language'), 'uk')
        self.page.reply_language.setCurrentIndex(self.page.reply_language.findData('en'))
        self.assertEqual(self.page._history, [])
        self.assertNotIn('Пояснення українською', self.page.transcript.toPlainText())
        self.page.question.setPlainText('English follow-up')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='English explanation') as ask:
            self.page._ask('chat')
            self.wait_worker()
        self.assertEqual(ask.call_args.kwargs['language'], 'en')
        self.assertEqual(ask.call_args.kwargs['history'], [])
        self.page.reply_language.setCurrentIndex(self.page.reply_language.findData('uk'))
        self.assertIn('Пояснення українською', self.page.transcript.toPlainText())
        self.assertNotIn('English explanation', self.page.transcript.toPlainText())

    def test_mode_switch_discards_queued_reply_from_previous_conversation(self):
        self.unlock()
        self.page.question.setPlainText('Запит для НМТ')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='Запізніла академічна відповідь'):
            self.page._ask('chat')
            self.assertTrue(self.page._worker.wait(1000))
            # Native worker completed, but its result has not reached the UI.
            self.page.general_chat_button.click()
            self.wait_worker()
        self.assertNotIn('Запізніла академічна відповідь', self.page.transcript.toPlainText())
        self.assertEqual(self.page._history, [])
        self.assertFalse(self.page.apply_button.isEnabled())
        self.page.study_chat_button.click()
        self.assertEqual(self.page.question.toPlainText(), 'Запит для НМТ')

    def test_language_switch_discards_queued_reply_and_preserves_old_draft_for_return(self):
        self.unlock()
        self.page.question.setPlainText('Поясни українською')
        with patch('mechahub.ui.study_page.StudyAIClient.ask', return_value='Відповідь зі старою мовою'):
            self.page._ask('chat')
            self.assertTrue(self.page._worker.wait(1000))
            self.page.reply_language.setCurrentIndex(self.page.reply_language.findData('en'))
            self.wait_worker()
        self.assertNotIn('Відповідь зі старою мовою', self.page.transcript.toPlainText())
        self.assertEqual(self.page._history, [])
        self.page.reply_language.setCurrentIndex(self.page.reply_language.findData('auto'))
        self.assertEqual(self.page.question.toPlainText(), 'Поясни українською')

    def test_note_proposal_explicitly_returns_to_study_mode_and_survives_navigation(self):
        self.unlock()
        self.page.note_body.setPlainText('Початковий конспект')
        self.page.general_chat_button.click()
        entered, release = threading.Event(), threading.Event()

        def ask(*args, **kwargs):
            entered.set()
            release.wait(2)
            return 'Структурований конспект'

        with patch('mechahub.ui.study_page.StudyAIClient.ask', side_effect=ask) as mock_ask:
            try:
                self.page._ask('structure')
                self.assertTrue(entered.wait(1))
                self.page.hide()
                self.app.processEvents()
                release.set()
                self.wait_worker()
            finally:
                release.set()
        self.page.show()
        self.app.processEvents()
        self.assertEqual(mock_ask.call_args.kwargs['mode'], 'study')
        self.assertEqual(mock_ask.call_args.args[0], 'math')
        self.assertTrue(self.page.apply_button.isEnabled())
        self.page._apply_answer()
        self.assertEqual(self.page.note_body.toPlainText(), 'Структурований конспект')
