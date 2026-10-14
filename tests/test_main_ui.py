"""Qt regressions with isolated settings, launch calls, and discovery."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from mechahub.ui.main_window import MainWindow, ProgramPickerDialog


class MainUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.environment = patch.dict(os.environ, {'APPDATA': self.folder.name, 'LOCALAPPDATA': self.folder.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.items = [
            {'name': 'Alpha', 'path': 'C:/Apps/alpha.exe'},
            {'name': 'Beta', 'path': 'C:/Apps/beta.exe'},
        ]
        with patch('mechahub.ui.main_window.read_shortcuts', return_value=self.items.copy()), \
             patch.object(MainWindow, '_update_stats'), \
             patch.object(MainWindow, '_update_processes'):
            self.window = MainWindow()
        self.window.tick_timer.stop()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_filtered_row_launches_and_removes_correct_shortcut(self):
        self.window.shortcut_search.setText('beta')
        self.assertEqual(self.window.shortcut_table.rowCount(), 1)
        with patch('mechahub.ui.main_window.open_path') as launch:
            self.window._run_shortcut(0)
        launch.assert_called_once_with('C:/Apps/beta.exe')
        with patch('mechahub.ui.main_window.write_shortcuts') as save:
            self.window._remove_shortcut()
        save.assert_called_once_with([self.items[0]])
        self.assertEqual(self.window.shortcuts, [self.items[0]])
        self.assertEqual(self.window.shortcut_table.rowCount(), 0)
        self.assertFalse(self.window.launch_button.isEnabled())

    def test_icon_grid_has_names_for_accessibility_and_selects_correct_entry(self):
        self.window.shortcut_grid.setCurrentRow(1)
        self.assertEqual(self.window.selected_shortcut_name.text(), 'Beta')
        self.assertEqual(self.window.shortcut_table.currentRow(), 1)
        self.assertEqual(self.window.shortcut_grid.item(1).text(), '')
        self.assertEqual(self.window.shortcut_grid.item(1).data(Qt.ItemDataRole.AccessibleTextRole), 'Beta')
        with patch('mechahub.ui.main_window.open_path') as launch:
            self.window.launch_button.click()
        launch.assert_called_once_with('C:/Apps/beta.exe')

    def test_quick_launch_uses_full_list_when_program_search_hides_entry(self):
        self.window.shortcut_search.setText('beta')
        with patch('mechahub.ui.main_window.open_path') as launch:
            self.window.quick_app_layout.itemAt(0).widget().click()
        launch.assert_called_once_with('C:/Apps/alpha.exe')

    def test_new_sections_keep_overview_and_details_separate(self):
        self.assertEqual(self.window.stack.count(), 11)
        self.assertEqual(self.window.dashboard_tabs.count(), 2)
        self.assertEqual(self.window.program_tabs.count(), 2)
        self.assertEqual(self.window.file_tabs.count(), 2)
        with patch.object(self.window.internet_page, 'activate') as activate:
            self.window._navigate(4)
        activate.assert_called_once()
        self.assertIs(self.window.stack.currentWidget(), self.window.internet_page)

    def test_catalogue_and_coding_are_independent_navigation_sections(self):
        self.window._navigate(8)
        self.assertIs(self.window.stack.currentWidget(), self.window.cash_registers_page)
        self.window._navigate(9)
        self.assertIs(self.window.stack.currentWidget(), self.window.programming_page)
        self.assertIn('Програмування', self.window.page_title.text())

    def test_failed_code_save_prevents_navigation(self):
        self.window._navigate(9)
        with patch.object(self.window.programming_page, 'can_leave', return_value=False):
            self.window._navigate(0)
        self.assertIs(self.window.stack.currentWidget(), self.window.programming_page)

    def test_failed_save_preserves_memory_and_visible_list(self):
        with patch('mechahub.ui.main_window.write_shortcuts', side_effect=OSError('read only')), \
             patch('mechahub.ui.main_window.QMessageBox.warning') as warning:
            self.window._add_shortcut(Path('C:/Apps/gamma.exe'), name='Gamma', ask_name=False)
            self.window._remove_shortcut()
        self.assertEqual(self.window.shortcuts, self.items)
        self.assertEqual(self.window.shortcut_table.rowCount(), 2)
        self.assertEqual(warning.call_count, 2)

    def test_rename_sorts_and_keeps_correct_selection(self):
        self.window.shortcut_table.selectRow(1)
        with patch('mechahub.ui.main_window.QInputDialog.getText', return_value=('Aardvark', True)), \
             patch('mechahub.ui.main_window.write_shortcuts'):
            self.window._rename_shortcut()
        self.assertEqual(self.window.shortcuts[0]['name'], 'Aardvark')
        index = self.window.shortcut_table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        self.assertEqual(self.window.shortcuts[index]['path'], 'C:/Apps/beta.exe')
        self.assertEqual(self.window.shortcut_table.currentRow(), 0)

    def test_failed_preview_clears_old_plan_and_disables_apply(self):
        self.window.current_plan = ['old plan']
        self.window.preview_table.setRowCount(1)
        self.window.organize_button.setEnabled(True)
        with patch('mechahub.ui.main_window.plan_organization', side_effect=PermissionError('denied')), \
             patch('mechahub.ui.main_window.QMessageBox.warning'):
            self.window._preview_folder()
        self.assertEqual(self.window.current_plan, [])
        self.assertEqual(self.window.preview_table.rowCount(), 0)
        self.assertFalse(self.window.organize_button.isEnabled())

    def test_discovery_does_not_block_ui_and_close_waits_for_thread(self):
        started = threading.Event()
        release = threading.Event()
        def discover():
            started.set()
            release.wait(3)
            return self.items

        with patch('mechahub.ui.main_window.installed_programs', side_effect=discover):
            dialog = ProgramPickerDialog(self.window)
            try:
                self.assertTrue(started.wait(1))
                dialog.show()
                self.app.processEvents()
                dialog.search.setText('beta')
                self.assertFalse(dialog.accept_button.isEnabled())
                dialog.reject()
                self.assertIsNotNone(dialog._scan_thread)
                self.assertEqual(dialog._pending_result, QDialog.DialogCode.Rejected)
                release.set()
                deadline = time.monotonic() + 3
                while dialog._scan_thread is not None and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertIsNone(dialog._scan_thread)
                self.assertFalse(dialog.isVisible())
                self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            finally:
                release.set()
                if dialog._scan_thread is not None:
                    dialog._scan_thread.wait(3000)
                    self.app.processEvents()
                dialog.deleteLater()


if __name__ == '__main__':
    unittest.main()
