"""The host preserves notes and stays open until the update helper is ready."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mechahub.core.updates import ReleaseManifest, UpdateError
from mechahub.ui.main_window import MainWindow


class HostUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(MainWindow, '_update_stats'), patch.object(MainWindow, '_update_processes'), \
             patch('mechahub.ui.main_window.read_shortcuts', return_value=[]):
            self.window = MainWindow()
        self.window._automatic_updates_started = True
        self.window.tick_timer.stop()
        self.window.update_check_timer.stop()

    def tearDown(self):
        self.window._handoff_timer.stop()
        self.window._pending_update = None
        self.window._update_helper_process = None
        self.window._close_requested = False
        if self.window._update_prepare_worker:
            self.window._update_prepare_worker.wait(3000)
        self.app.processEvents()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_background_offer_shows_app_banner_without_downloading(self):
        dialog = self.window._ensure_updates_dialog()
        with patch.object(dialog, 'begin_update') as download, patch.object(dialog, 'show_update_offer') as offer:
            self.window._update_available(ReleaseManifest('0.9.0', 'https://example.com/update.exe'))
            self.assertFalse(self.window.update_banner.isHidden())
            self.assertIn('0.9.0', self.window.update_banner_label.text())
            self.window.update_offer_button.click()
        offer.assert_called_once()
        download.assert_not_called()

    def test_failed_note_save_prevents_preparation_and_execution(self):
        dialog = self.window._ensure_updates_dialog()
        with patch.object(self.window.study_page, 'can_leave', return_value=False), \
             patch('mechahub.ui.main_window.prepare_installation') as prepare, \
             patch('mechahub.ui.main_window.start_installation') as start, \
             patch.object(dialog, 'installation_failed') as failed:
            self.window._request_update_install(Mock(), Mock())
        prepare.assert_not_called()
        start.assert_not_called()
        failed.assert_called_once()
        self.assertIsNone(self.window._update_prepare_worker)

    def test_preparation_failure_keeps_host_open_and_allows_retry(self):
        dialog = self.window._ensure_updates_dialog()
        with patch('mechahub.ui.main_window.prepare_installation', side_effect=UpdateError('bad checksum')), \
             patch('mechahub.ui.main_window.start_installation') as start, \
             patch.object(dialog, 'show_update_offer'), patch.object(self.window, 'close') as close:
            self.window._request_update_install(Mock(), Mock())
            deadline = time.monotonic() + 3
            while self.window._update_prepare_worker is not None and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertIsNone(self.window._update_prepare_worker)
        start.assert_not_called()
        close.assert_not_called()
        self.assertFalse(self.window._close_requested)
        self.assertTrue(self.window.update_offer_button.isEnabled())

    def test_safe_close_starts_helper_and_waits_for_readiness(self):
        self.window._pending_update = Mock()
        process = Mock()
        with patch('mechahub.ui.main_window.start_installation', return_value=process) as start, \
             patch.object(self.window.study_page, 'can_leave', return_value=True), \
             patch.object(self.window.study_page, 'request_close', return_value=True), \
             patch.object(self.window.currency_page, 'request_close', return_value=True), \
             patch.object(self.window.network_page, 'request_stop_on_close', return_value=True), \
             patch.object(self.window.internet_page, 'shutdown') as browser_close:
            event = QCloseEvent()
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertIs(self.window._update_helper_process, process)
        start.assert_called_once()
        browser_close.assert_not_called()
        self.window._handoff_timer.stop()
        with patch('mechahub.ui.main_window.installation_ready', return_value=True), \
             patch.object(self.window, 'close') as close:
            self.window._poll_update_handoff()
            self.app.processEvents()
        self.assertTrue(self.window._update_helper_ready)
        close.assert_called_once()

    def test_start_failure_restores_controls_and_leaves_app_open(self):
        self.window._pending_update = Mock()
        dialog = self.window._ensure_updates_dialog()
        with patch('mechahub.ui.main_window.start_installation', side_effect=OSError('cannot launch')), \
             patch('mechahub.ui.main_window.discard_prepared') as discard, \
             patch.object(dialog, 'show_update_offer'):
            self.window._begin_update_handoff()
        discard.assert_called_once()
        self.assertIsNone(self.window._pending_update)
        self.assertFalse(self.window._close_requested)
        self.assertFalse(self.window.currency_page.close_pending)
        self.assertTrue(self.window.currency_page.refresh_button.isEnabled())

    def test_readiness_timeout_cancels_helper_before_gui_can_exit(self):
        self.window._pending_update = Mock()
        self.window._update_helper_process = Mock()
        self.window._update_helper_deadline = time.monotonic() - 1
        dialog = self.window._ensure_updates_dialog()
        with patch('mechahub.ui.main_window.installation_ready', return_value=False), \
             patch('mechahub.ui.main_window.cancel_installation') as cancel, \
             patch.object(dialog, 'show_update_offer'), patch.object(self.window, 'close') as close:
            self.window._poll_update_handoff()
        cancel.assert_called_once()
        close.assert_not_called()
        self.assertIsNone(self.window._pending_update)
        self.assertTrue(self.window.update_offer_button.isEnabled())

    def test_failed_handoff_restores_speed_test_after_cancelled_scan(self):
        self.window.network_page.close_pending = True
        self.window.network_page.start_button.setEnabled(False)
        dialog = self.window._ensure_updates_dialog()
        with patch.object(dialog, 'show_update_offer'):
            self.window._update_install_failed('helper stopped')
        self.assertFalse(self.window.network_page.close_pending)
        self.assertTrue(self.window.network_page.start_button.isEnabled())
