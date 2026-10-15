import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QPushButton

from mechahub.core.cash_registers import CaseStore, MODELS, ProductImage, RegisterIssue, Source, search_models
from mechahub.ui.cash_registers_page import CaseEditor, CashRegistersPage, _asset_path


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
        manufacturer = self.page.manufacturer.currentData()
        self.assertEqual(self.page.models.count(), len(search_models(manufacturer=manufacturer)))
        self.page.search.setText('Ethernet')
        self.assertEqual(self.page.models.count(), len(search_models('Ethernet', manufacturer)))
        self.page.search.setText('неіснуюча модель')
        self.assertEqual(self.page.models.count(), 0)
        self.assertIsNone(self.page._current_model)
        self.assertIsNone(self.page.detail_tabs)
        self.assertIsNone(self.page.photo)
        self.assertIn('Моделей не знайдено', self._labels(self.page.details))

    def test_source_button_emits_the_selected_official_url(self):
        urls = []
        self.page.open_source.connect(urls.append)
        self.page.search.setText('IKC-М510')
        buttons = self.page.details.findChildren(QPushButton)
        url = self.page._current_model.sources[0].url
        source = next(control for control in buttons if control.property('source_url') == url)
        source.click()
        self.assertEqual(urls, [url])

    @staticmethod
    def _labels(widget):
        return '\n'.join(label.text() for label in widget.findChildren(QLabel))

    def _model(self, *, key='test-register', filename='missing-register-photo.jpg'):
        manual = Source('Настанова для тестової моделі', 'https://example.com/manual.pdf')
        photo_source = Source('Фото виробника', 'https://example.com/device')
        return replace(MODELS[0], key=key, name='Тестовий апарат',
                       image=ProductImage(filename, photo_source, 'Фото: виробник тестового апарата'),
                       issue_note='', reference_notes=(),
                       issues=(
                           RegisterIssue('Немає паперу', '<b>Стрічка закінчилася</b>',
                                         ('Завантажте сумісну стрічку.', 'Закрийте кришку.'),
                                         'Якщо датчик не бачить стрічку — зверніться до сервісу.',
                                         (manual,), code='P01'),
                           RegisterIssue('Зв’язок перервано', 'Не вдалося передати дані.',
                                         ('Перевірте кабель мережі.',), 'За повторення повідомлення — сервіс.',
                                         (manual,)),
                       ))

    def _show_model(self, model):
        with patch('mechahub.ui.cash_registers_page.search_models', return_value=(model,)):
            self.page._filter_models()

    def test_model_details_are_separated_into_facts_issues_and_sources(self):
        model = self._model()
        self._show_model(model)
        self.assertEqual([self.page.detail_tabs.tabText(index) for index in range(3)],
                         ['Характеристики', 'Помилки та рішення', 'Джерела'])
        facts = self._labels(self.page.detail_tabs.widget(0))
        self.assertIn(model.purpose, facts)
        for name, value in model.facts:
            self.assertIn(name, facts)
            self.assertIn(value, facts)
        self.assertNotIn(model.issues[0].meaning, facts)
        sources = self._labels(self.page.detail_tabs.widget(2))
        self.assertIn(model.checked_date, sources)
        self.assertIn(model.image.credit, sources)
        self.assertIn(model.issues[0].sources[0].title, sources)

    def test_issues_have_clear_sections_and_switch_without_stale_text(self):
        model = self._model()
        self._show_model(model)
        self.assertEqual(self.page.issue_selector.count(), len(model.issues))
        self.assertIn('P01', self.page.issue_selector.itemText(0))
        self.assertIn('не статистика', self.page.issue_notice.text())
        text = self._labels(self.page.issue_details)
        self.assertIn('Що означає', text)
        self.assertIn(model.issues[0].meaning, text)
        self.assertIn('1. Завантажте сумісну стрічку.', text)
        self.assertIn('2. Закрийте кришку.', text)
        self.assertIn('Коли потрібен сервіс', text)
        self.assertIn(model.issues[0].service, text)
        meaning_label = next(label for label in self.page.issue_details.findChildren(QLabel)
                             if label.text() == model.issues[0].meaning)
        self.assertEqual(meaning_label.textFormat(), Qt.TextFormat.PlainText)
        self.page.issue_selector.setCurrentIndex(1)
        changed = self._labels(self.page.issue_details)
        self.assertIn(model.issues[1].meaning, changed)
        self.assertNotIn(model.issues[0].meaning, changed)
        self.assertNotIn(model.issues[0].service, changed)

    def test_issue_and_photo_sources_emit_their_own_urls(self):
        model = self._model()
        self._show_model(model)
        urls = []
        self.page.open_source.connect(urls.append)
        issue_button = next(control for control in self.page.issue_details.findChildren(QPushButton)
                            if control.property('source_url') == model.issues[0].sources[0].url)
        issue_button.click()
        photo_button = next(control for control in self.page.detail_tabs.widget(2).findChildren(QPushButton)
                            if control.property('source_url') == model.image.source.url)
        photo_button.click()
        self.assertEqual(urls, [model.issues[0].sources[0].url, model.image.source.url])

    def test_missing_local_photo_keeps_catalogue_and_attribution_available(self):
        model = self._model()
        self._show_model(model)
        self.assertFalse(self.page.photo.property('image_available'))
        self.assertIn('Фото недоступне', self.page.photo.text())
        self.assertIn(model.image.credit, self.page.photo.toolTip())
        self.assertIn(model.image.source.url, self.page.photo.toolTip())
        self.assertFalse(self.page.models.item(0).icon().isNull())
        self.assertEqual(self.page.issue_selector.count(), len(model.issues))

    def test_local_photo_and_thumbnail_preserve_aspect_ratio_with_bounded_size(self):
        filename = Path(self.temp.name) / 'register.png'
        picture = QPixmap(400, 100)
        picture.fill(QColor('#EAA02B'))
        self.assertTrue(picture.save(str(filename)))
        with patch('mechahub.ui.cash_registers_page._asset_path', return_value=filename):
            self._show_model(self._model(filename=filename.name))
        self.assertTrue(self.page.photo.property('image_available'))
        pixmap = self.page.photo.pixmap()
        self.assertLessEqual(pixmap.width(), 146)
        self.assertLessEqual(pixmap.height(), 98)
        self.assertAlmostEqual(pixmap.width() / pixmap.height(), 4, delta=0.1)
        self.assertFalse(self.page.models.item(0).icon().isNull())

    def test_photo_paths_stay_inside_bundled_assets(self):
        for filename in ('', '..', '../photo.jpg', 'folder/photo.jpg', r'folder\photo.jpg',
                         'https://example.com/photo.jpg', 'C:photo.jpg'):
            with self.subTest(filename=filename):
                self.assertIsNone(_asset_path(filename))
        with patch('sys._MEIPASS', self.temp.name, create=True):
            self.assertEqual(_asset_path('device.jpg'),
                             Path(self.temp.name) / 'assets' / 'cash_registers' / 'device.jpg')

    def test_switching_models_resets_detail_selection_and_clears_old_issues(self):
        first = self._model()
        second = replace(self._model(key='another'), name='Інший апарат',
                         issues=(RegisterIssue('Інший симптом', 'Окреме пояснення.', ('Інша перевірка.',),
                                               'Інша порада сервісу.', first.issues[0].sources),))
        with patch('mechahub.ui.cash_registers_page.search_models', return_value=(first, second)):
            self.page._filter_models()
        self.page.detail_tabs.setCurrentIndex(1)
        self.page.issue_selector.setCurrentIndex(1)
        self.page.models.setCurrentRow(1)
        self.assertEqual(self.page._current_model, second)
        self.assertEqual(self.page.detail_tabs.currentIndex(), 0)
        self.assertEqual(self.page.issue_selector.currentIndex(), 0)
        self.assertIn('Окреме пояснення.', self._labels(self.page.issue_details))
        self.assertNotIn(first.issues[1].meaning, self._labels(self.page.issue_details))

    def test_model_specific_issue_scope_and_reference_notes_are_visible(self):
        model = replace(self._model(),
                        issue_note='Описано HTTP/JSON API. Позначення на дисплеї можуть відрізнятися.',
                        reference_notes=('Звіряйте версію ПЗ за паспортом.',
                                         'Папір: уточнення для конкретної модифікації.'))
        self._show_model(model)
        self.assertEqual(self.page.issue_notice.text(), model.issue_note)
        sources = self._labels(self.page.detail_tabs.widget(2))
        self.assertIn('Модифікація та уточнення', sources)
        for note in model.reference_notes:
            self.assertIn(f'• {note}', sources)
        self.assertLess(sources.index('Модифікація та уточнення'), sources.index('Документація моделі'))

    def test_model_without_reference_notes_uses_general_issue_scope(self):
        self._show_model(self._model())
        self.assertIn('не статистика', self.page.issue_notice.text())
        self.assertNotIn('Модифікація та уточнення', self._labels(self.page.detail_tabs.widget(2)))

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
