import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton

from mechahub.core.cash_registers import CaseStore, MODELS
from mechahub.ui.cash_registers_page import CaseEditor, CashRegistersPage


class CashRegisterUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'cases.json'
        self.page = CashRegistersPage(cases_path=self.path)
        self.addCleanup(self.page.deleteLater)

    def test_catalogue_filters_and_empty_state(self):
        self.assertEqual(self.page.models.count(), len(MODELS))
        self.page.manufacturer.setCurrentIndex(1)
        self.assertEqual(self.page.models.count(), 3)
        self.page.search.setText('Ethernet')
        self.assertEqual(self.page.models.count(), 2)
        self.page.search.setText('неіснуюча модель')
        self.assertEqual(self.page.models.count(), 0)
        self.assertIsNone(self.page._current_model)

    def test_source_button_emits_the_selected_official_url(self):
        urls = []
        self.page.open_source.connect(urls.append)
        self.page.search.setText('IKC-М510')
        buttons = self.page.details.findChildren(QPushButton)
        source = next(control for control in buttons if control.text() == 'Офіційна сторінка ІКС-МАРКЕТ')
        source.click()
        self.assertEqual(urls, ['https://www.ics-market.com.ua/obladnannya/ikc-m510-01/'])

    def test_cases_start_empty_and_render_saved_text_without_html(self):
        self.assertIn('Кейсів ще немає', self.page.case_text.toPlainText())
        self.assertFalse(self.page.edit_case_button.isEnabled())
        saved = self.page.store.save('<b>Назва</b>', 'Модель', '<script>текст</script>')
        self.page._reload_cases(saved.id)
        self.assertTrue(self.page.edit_case_button.isEnabled())
        self.assertIn('<script>текст</script>', self.page.case_text.toPlainText())
        self.assertTrue(self.page.case_text.isReadOnly())

    def test_case_search_matches_body_and_clears_selection(self):
        self.page.store.save('Зв’язок', '', 'Перевірено Ethernet')
        self.page.store.save('Стрічка', '', 'Завантаження паперу')
        self.page._reload_cases()
        self.page.case_search.setText('ethernet')
        self.assertEqual(self.page.case_list.count(), 1)
        self.page.case_search.setText('жоден')
        self.assertFalse(self.page.delete_case_button.isEnabled())

    def test_editor_failed_save_stays_open_and_preserves_text(self):
        editor = CaseEditor(CaseStore(self.path))
        self.addCleanup(editor.deleteLater)
        editor.body.setPlainText('Не втратити текст')
        editor._save()
        self.assertIsNone(editor.saved_case)
        self.assertEqual(editor.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(editor.body.toPlainText(), 'Не втратити текст')
        self.assertIn('Назва', editor.status.text())

    def test_editor_saves_successful_case(self):
        editor = CaseEditor(CaseStore(self.path))
        self.addCleanup(editor.deleteLater)
        editor.title.setText('Мій випадок')
        editor.body.setPlainText('Симптом і результат')
        editor._save()
        self.assertEqual(editor.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(editor.saved_case.title, 'Мій випадок')
        self.assertEqual(CaseStore(self.path).load(), [editor.saved_case])

    def test_dirty_editor_cancel_requires_discard(self):
        editor = CaseEditor(CaseStore(self.path))
        self.addCleanup(editor.deleteLater)
        editor.show()
        editor.title.setText('Незбережене')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel):
            editor.reject()
        self.assertTrue(editor.isVisible())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Discard):
            editor.reject()
        self.assertFalse(editor.isVisible())

    def test_corrupt_store_is_reported_and_preserved(self):
        self.path.write_text('broken', encoding='utf-8')
        self.page._reload_cases()
        self.assertIn('Не вдалося прочитати', self.page.case_status.text())
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'broken')

    def _active_editor(self):
        editor = CaseEditor(self.page.store, self.page)
        self.addCleanup(editor.deleteLater)
        self.page._editor = editor
        editor.show()
        editor.title.setText('Чернетка кейсу')
        editor.body.setPlainText('Результати перевірок')
        return editor

    def test_host_close_cancel_preserves_visible_draft(self):
        editor = self._active_editor()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(self.page.request_close())
        self.assertTrue(editor.isVisible())
        self.assertEqual(editor.body.toPlainText(), 'Результати перевірок')
        self.assertFalse(self.path.exists())

    def test_host_leave_discard_closes_editor(self):
        editor = self._active_editor()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Discard):
            self.assertTrue(self.page.can_leave())
        self.assertFalse(editor.isVisible())
        self.assertFalse(self.path.exists())

    def test_host_close_after_failed_save_keeps_draft(self):
        editor = self._active_editor()
        with patch.object(self.page.store, 'save', side_effect=OSError('Немає місця')):
            editor._save()
        self.assertIsNone(editor.saved_case)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel):
            self.assertFalse(self.page.request_close())
        self.assertTrue(editor.isVisible())
        self.assertEqual(editor.title.text(), 'Чернетка кейсу')
        self.assertEqual(editor.body.toPlainText(), 'Результати перевірок')
        self.assertEqual(editor.status.text(), 'Немає місця')

    def test_host_close_after_successful_save_is_allowed(self):
        editor = self._active_editor()
        editor._save()
        self.assertTrue(self.page.request_close())
        self.assertFalse(editor.isVisible())
        self.assertEqual(self.page.store.load(), [editor.saved_case])


if __name__ == '__main__':
    unittest.main()
