"""Modeless update UI tests with isolated QSettings and mocked networking."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import hashlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from mechahub.core.updates import DEFAULT_MANIFEST_URL, ReleaseManifest, UpdateCancelled, UpdateError
from mechahub.core.updates_download import ArtifactDownloadResult
from mechahub.ui import updates_dialog


class UpdatesUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / 'updates.ini'), QSettings.Format.IniFormat)
        # Existing tests deliberately cover the user-disabled/source-empty case.
        self.settings.setValue(updates_dialog.MANIFEST_SETTING, '')
        self.settings_patch = patch.object(updates_dialog, 'QSettings', wraps=QSettings)
        settings_type = self.settings_patch.start()
        settings_type.return_value = self.settings
        settings_type.Status = QSettings.Status
        self.dialog = updates_dialog.UpdatesDialog()

    def tearDown(self):
        if self.dialog.worker is not None:
            self.dialog.worker.stop()
            self.dialog.worker.wait(3000)
            self.app.processEvents()
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        self.settings_patch.stop()
        self.temp.cleanup()

    def wait_for_worker(self):
        deadline = time.monotonic() + 3
        while self.dialog.worker is not None and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertIsNone(self.dialog.worker)

    def test_initial_window_has_history_without_network_or_default_source(self):
        with patch.object(updates_dialog, 'fetch_manifest') as fetch:
            self.dialog.show()
            self.app.processEvents()
            self.assertFalse(self.dialog.isModal())
            self.assertEqual(self.dialog.source_input.text(), '')
            self.dialog._check()
            fetch.assert_not_called()
        text = '\n'.join(label.text() for label in self.dialog.findChildren(QLabel))
        for version in ('0.4.0', '0.3.1', '0.3.0'):
            self.assertIn(version, text)
        self.assertIn('не задане', self.dialog.status_label.text())
        self.assertTrue(self.dialog.open_button.isHidden())

    def test_history_is_default_tab_and_source_controls_are_on_online_tab(self):
        self.dialog.show()
        self.app.processEvents()
        self.assertEqual(self.dialog.tabs.count(), 2)
        self.assertEqual(self.dialog.tabs.currentIndex(), 0)
        self.assertEqual(self.dialog.tabs.tabText(0), 'Історія змін')
        self.assertEqual(self.dialog.tabs.tabText(1), 'Перевірка онлайн')
        self.assertFalse(self.dialog.source_input.isVisible())
        self.dialog.tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.assertTrue(self.dialog.source_input.isVisible())
        self.assertIn('download_url', self.dialog.source_input.toolTip())
        self.assertEqual(self.dialog.minimumWidth(), 580)
        self.assertEqual(self.dialog.minimumHeight(), 520)

    def test_automatic_checks_require_configured_source_and_can_be_disabled(self):
        self.assertFalse(self.dialog.auto_checkbox.isEnabled())
        self.assertFalse(self.dialog.auto_checkbox.isChecked())
        with patch.object(updates_dialog, 'fetch_manifest') as fetch:
            self.assertFalse(self.dialog.begin_automatic_check())
            fetch.assert_not_called()
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        self.assertTrue(self.dialog.auto_checkbox.isEnabled())
        self.assertTrue(self.dialog.auto_checkbox.isChecked())
        self.dialog.auto_checkbox.setChecked(False)
        self.assertFalse(self.settings.value(updates_dialog.AUTOMATIC_SETTING, type=bool))
        with patch.object(updates_dialog, 'fetch_manifest') as fetch:
            self.assertFalse(self.dialog.begin_automatic_check())
            fetch.assert_not_called()

    def test_published_default_source_enables_mocked_automatic_startup_check(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.settings.remove(updates_dialog.MANIFEST_SETTING)
        with patch.object(updates_dialog, 'fetch_manifest',
                          return_value=ReleaseManifest('0.1.0', 'https://releases.example/old')) as fetch:
            self.dialog = updates_dialog.UpdatesDialog()
            self.assertEqual(self.dialog.source_input.text(), DEFAULT_MANIFEST_URL)
            self.assertTrue(self.dialog.auto_checkbox.isEnabled())
            self.assertTrue(self.dialog.auto_checkbox.isChecked())
            fetch.assert_not_called()
            self.assertTrue(self.dialog.begin_automatic_check())
            self.wait_for_worker()
            self.assertEqual(fetch.call_args.args[0], DEFAULT_MANIFEST_URL)

    def test_explicitly_empty_source_overrides_default_and_disables_startup(self):
        self.assertEqual(self.dialog.source_input.text(), '')
        self.assertTrue(self.settings.contains(updates_dialog.MANIFEST_SETTING))
        with patch.object(updates_dialog, 'fetch_manifest') as fetch:
            self.assertFalse(self.dialog.begin_automatic_check())
            fetch.assert_not_called()

    def test_automatic_startup_check_uses_configured_enabled_source(self):
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        self.assertTrue(self.dialog._save_source())
        with patch.object(updates_dialog, 'fetch_manifest',
                          return_value=ReleaseManifest('0.1.0', 'https://releases.example/old')) as fetch:
            self.assertTrue(self.dialog.begin_automatic_check())
            self.assertFalse(self.dialog.begin_automatic_check())
            self.wait_for_worker()
            fetch.assert_called_once()

    def test_https_source_is_saved_and_reloaded_without_checking(self):
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        self.assertTrue(self.dialog._save_source())
        self.assertEqual(self.settings.value(updates_dialog.MANIFEST_SETTING),
                         'https://updates.example/manifest.json')
        with patch.object(updates_dialog, 'fetch_manifest') as fetch:
            second = updates_dialog.UpdatesDialog()
            self.assertEqual(second.source_input.text(), self.dialog.source_input.text())
            fetch.assert_not_called()
            second.deleteLater()

    def test_invalid_source_does_not_start_worker_or_replace_saved_source(self):
        self.settings.setValue(updates_dialog.MANIFEST_SETTING, 'https://valid.example/manifest.json')
        self.dialog.source_input.setText('http://insecure.example/manifest.json')
        with patch.object(updates_dialog, 'fetch_manifest') as fetch:
            self.dialog._check()
            fetch.assert_not_called()
        self.assertIsNone(self.dialog.worker)
        self.assertIn('HTTPS', self.dialog.status_label.text())
        self.assertEqual(self.settings.value(updates_dialog.MANIFEST_SETTING),
                         'https://valid.example/manifest.json')

    def test_new_version_exposes_only_manual_browser_action_and_plain_text_notes(self):
        manifest = ReleaseManifest('99.0.0', 'https://releases.example/v99', '<b>Опис</b>')
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        with patch.object(updates_dialog, 'fetch_manifest', return_value=manifest), \
                patch.object(updates_dialog.QDesktopServices, 'openUrl', return_value=True) as open_url:
            self.dialog._check()
            self.wait_for_worker()
            open_url.assert_not_called()
            self.assertIn('Доступна версія 99.0.0', self.dialog.status_label.text())
            self.assertFalse(self.dialog.open_button.isHidden())
            self.assertEqual(self.dialog.remote_notes.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(self.dialog.remote_notes.text(), '<b>Опис</b>')
            self.dialog._open_release()
            self.assertEqual(open_url.call_args.args[0].toString(), manifest.download_url)

    def test_verified_new_release_is_downloaded_automatically_without_running_it(self):
        data = b'verified release'
        checksum = hashlib.sha256(data).hexdigest()
        manifest = ReleaseManifest('99.0.0', 'https://releases.example/MechaHub.exe',
                                   'Опис', checksum, len(data))
        path = Path(self.temp.name) / 'MechaHub-99.0.0.exe'
        path.write_bytes(data)
        result = ArtifactDownloadResult(path, len(data), checksum)
        announced = []
        self.dialog.download_ready.connect(announced.append)
        self.dialog.source_input.setText('https://updates.example/manifest.json')

        def download_with_progress(release, folder, cancel_event, progress):
            progress(4, len(data))
            progress(len(data), len(data))
            return result

        with patch.object(updates_dialog, 'fetch_manifest', return_value=manifest), \
                patch.object(updates_dialog, 'download_release', side_effect=download_with_progress) as download, \
                patch.object(updates_dialog.QDesktopServices, 'openUrl', return_value=True) as open_url:
            self.dialog._check()
            self.wait_for_worker()
            download.assert_called_once()
            self.assertEqual(download.call_args.args[0], manifest)
            self.assertEqual(download.call_args.args[1].name, 'MechaHub')
            self.assertEqual(self.dialog.download_result, result)
            self.assertEqual(self.dialog.download_progress.value(), 100)
            self.assertEqual(announced, [str(path)])
            self.assertFalse(self.dialog.open_folder_button.isHidden())
            self.assertTrue(self.dialog.open_button.isHidden())
            open_url.assert_not_called()
            self.dialog._open_download_folder()
            self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), path.parent)

    def test_failed_artifact_verification_keeps_manual_retry_and_no_ready_file(self):
        manifest = ReleaseManifest('99.0.0', 'https://releases.example/MechaHub.exe',
                                   '', 'a' * 64, 10)
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        with patch.object(updates_dialog, 'fetch_manifest', return_value=manifest), \
                patch.object(updates_dialog, 'download_release', side_effect=UpdateError('checksum mismatch')):
            self.dialog._check()
            self.wait_for_worker()
        self.assertIn('checksum mismatch', self.dialog.status_label.text())
        self.assertIsNone(self.dialog.download_result)
        self.assertTrue(self.dialog.check_button.isEnabled())
        self.assertTrue(self.dialog.open_folder_button.isHidden())

    def test_close_waits_for_active_artifact_download_and_discards_cancelled_result(self):
        entered = threading.Event()
        release = threading.Event()
        manifest = ReleaseManifest('99.0.0', 'https://releases.example/MechaHub.exe',
                                   '', 'a' * 64, 10)
        announced = []
        observed_cancellation = []
        self.dialog.download_ready.connect(announced.append)
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        self.dialog.show()

        def pending_download(release_manifest, folder, cancel_event, progress):
            observed_cancellation.append(cancel_event)
            entered.set()
            release.wait(2)
            raise UpdateCancelled()

        with patch.object(updates_dialog, 'fetch_manifest', return_value=manifest), \
                patch.object(updates_dialog, 'download_release', side_effect=pending_download):
            try:
                self.dialog._check()
                deadline = time.monotonic() + 2
                while not entered.is_set() and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertTrue(entered.is_set())
                self.assertIsInstance(self.dialog.worker, updates_dialog.ArtifactDownloadThread)
                self.assertFalse(self.dialog.request_close())
                self.dialog.close()
                self.assertTrue(self.dialog.isVisible())
                self.assertTrue(observed_cancellation[0].is_set())
                release.set()
                self.wait_for_worker()
                self.assertFalse(self.dialog.isVisible())
                self.assertIsNone(self.dialog.download_result)
                self.assertEqual(announced, [])
            finally:
                release.set()
                self.wait_for_worker()

    def test_older_version_is_reported_as_source_result_without_download_action(self):
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        with patch.object(updates_dialog, 'fetch_manifest', return_value=ReleaseManifest('0.1.0', 'https://release.example/old')):
            self.dialog._check()
            self.wait_for_worker()
        self.assertIn('Джерело повідомило', self.dialog.status_label.text())
        self.assertIn('не новіша', self.dialog.status_label.text())
        self.assertTrue(self.dialog.open_button.isHidden())

    def test_failed_check_reports_error_and_can_be_retried(self):
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        with patch.object(updates_dialog, 'fetch_manifest', side_effect=UpdateError('offline')):
            self.dialog._check()
            self.wait_for_worker()
        self.assertIn('offline', self.dialog.status_label.text())
        self.assertTrue(self.dialog.check_button.isEnabled())
        self.assertTrue(self.dialog.open_button.isHidden())

    def test_editing_source_invalidates_previously_found_release(self):
        self.dialog._received(ReleaseManifest('99.0.0', 'https://release.example/new', 'Опис'))
        self.dialog.source_input.setText('https://other.example/manifest.json')
        self.assertIsNone(self.dialog.manifest)
        self.assertTrue(self.dialog.open_button.isHidden())
        self.assertTrue(self.dialog.remote_notes.isHidden())

    def test_close_cancels_worker_and_keeps_dialog_alive_until_finished(self):
        entered = threading.Event()
        release = threading.Event()
        observed = []

        def pending_fetch(url, cancel_event):
            observed.append(cancel_event)
            entered.set()
            release.wait(2)
            if cancel_event.is_set():
                raise UpdateCancelled()
            return ReleaseManifest('99.0.0', 'https://release.example/new')

        self.dialog.source_input.setText('https://updates.example/manifest.json')
        self.dialog.show()
        with patch.object(updates_dialog, 'fetch_manifest', side_effect=pending_fetch):
            try:
                self.dialog._check()
                self.assertTrue(entered.wait(1))
                notifications = []
                self.dialog.ready_to_close.connect(lambda: notifications.append(True))
                self.assertFalse(self.dialog.request_close())
                self.dialog.close()
                self.assertIsNotNone(self.dialog.worker)
                self.assertTrue(self.dialog.isVisible())
                self.assertTrue(observed[0].is_set())
                release.set()
                self.wait_for_worker()
                self.assertFalse(self.dialog.isVisible())
                self.assertEqual(notifications, [True])
                self.assertIsNone(self.dialog.manifest)
                self.assertTrue(self.dialog.request_close())
            finally:
                release.set()
                self.wait_for_worker()

    def test_reopened_dialog_can_start_another_check(self):
        self.dialog.show()
        self.dialog.close()
        self.dialog.show()
        self.app.processEvents()
        self.assertTrue(self.dialog.check_button.isEnabled())
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        with patch.object(updates_dialog, 'fetch_manifest', return_value=ReleaseManifest('99.0.0', 'https://release.example/new')):
            self.dialog._check()
            self.wait_for_worker()
        self.assertIn('Доступна версія', self.dialog.status_label.text())

    def test_stop_preserves_window_and_prevents_overlapping_checks(self):
        entered = threading.Event()
        release = threading.Event()

        def pending_fetch(url, cancel_event):
            entered.set()
            release.wait(2)
            raise UpdateCancelled()

        self.dialog.source_input.setText('https://updates.example/manifest.json')
        self.dialog.show()
        with patch.object(updates_dialog, 'fetch_manifest', side_effect=pending_fetch) as fetch:
            try:
                self.dialog._check()
                self.assertTrue(entered.wait(1))
                owned_worker = self.dialog.worker
                self.dialog._check()
                self.dialog._stop()
                self.dialog._check()
                self.assertIs(self.dialog.worker, owned_worker)
                self.assertEqual(fetch.call_count, 1)
                self.assertFalse(self.dialog.check_button.isEnabled())
                release.set()
                self.wait_for_worker()
                self.assertTrue(self.dialog.isVisible())
                self.assertTrue(self.dialog.check_button.isEnabled())
                self.assertIn('зупинено', self.dialog.status_label.text())
            finally:
                release.set()
                self.wait_for_worker()

    def test_host_close_waits_for_queued_thread_finish_then_retries_via_signal(self):
        dialog = self.dialog

        class HostWindow(QWidget):
            def __init__(self):
                super().__init__()
                self.close_attempts = 0

            def closeEvent(self, event):
                self.close_attempts += 1
                if not dialog.request_close():
                    event.ignore()
                    return
                event.accept()

        host = HostWindow()
        host.show()
        self.dialog.show()
        self.dialog.ready_to_close.connect(host.close)
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        with patch.object(updates_dialog, 'fetch_manifest',
                          return_value=ReleaseManifest('99.0.0', 'https://release.example/new')):
            try:
                self.dialog._check()
                worker = self.dialog.worker
                self.assertTrue(worker.wait(1000))
                # The native thread ended, but its queued cleanup still owns it.
                self.assertFalse(worker.isRunning())
                self.assertIs(self.dialog.worker, worker)
                host.close()
                self.assertEqual(host.close_attempts, 1)
                self.assertTrue(host.isVisible())
                self.wait_for_worker()
                self.assertGreaterEqual(host.close_attempts, 2)
                self.assertFalse(host.isVisible())
                self.assertFalse(self.dialog.isVisible())
            finally:
                self.dialog.ready_to_close.disconnect(host.close)
                self.wait_for_worker()
                host.close()
                host.deleteLater()


if __name__ == '__main__':
    unittest.main()
