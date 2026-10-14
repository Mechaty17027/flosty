"""Modeless update UI tests with isolated QSettings and mocked networking."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import hashlib
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QRect, QSettings, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QScrollArea, QWidget

from mechahub.core.updates import DEFAULT_MANIFEST_URL, ReleaseManifest, UpdateCancelled, UpdateError
from mechahub.core.updates_download import ArtifactDownloadResult
from mechahub.ui import updates_dialog


class UpdatesUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # Qt's offscreen Windows backend does not discover native fonts. Load
        # the app's real font explicitly for meaningful layout assertions.
        font_path = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Fonts' / 'segoeui.ttf'
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / 'updates.ini'), QSettings.Format.IniFormat)
        # Existing tests deliberately cover the user-disabled/source-empty case.
        self.settings.setValue(updates_dialog.MANIFEST_SETTING, '')
        # Most cases cover explicit downloads; automatic prefetch is exercised
        # separately without real networking or application execution.
        self.settings.setValue(updates_dialog.AUTOMATIC_DOWNLOAD_SETTING, False)
        self.settings_patch = patch.object(updates_dialog, 'QSettings', wraps=QSettings)
        settings_type = self.settings_patch.start()
        settings_type.return_value = self.settings
        settings_type.Status = QSettings.Status
        self.context_patch = patch.object(updates_dialog, 'detect_installation',
                                          return_value=SimpleNamespace(mode='portable'))
        self.context_patch.start()
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
        self.context_patch.stop()
        self.temp.cleanup()

    def wait_for_worker(self):
        deadline = time.monotonic() + 3
        while self.dialog.worker is not None and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertIsNone(self.dialog.worker)

    def ready_release(self, *, installed=False, notes='Нові можливості'):
        """Seed the UI with a genuinely saved, hash-matching mock artifact."""
        portable = b'portable update'
        setup = b'installer update'
        manifest = ReleaseManifest(
            '99.0.0', 'https://releases.example/MechaHub.exe', notes,
            hashlib.sha256(portable).hexdigest(), len(portable),
            'https://releases.example/MechaHubSetup.exe', hashlib.sha256(setup).hexdigest(), len(setup),
        )
        self.dialog.installation_context = SimpleNamespace(mode='installed' if installed else 'portable')
        self.dialog._received(manifest)
        selected = self.dialog._selected_artifact
        data = setup if installed else portable
        path = Path(self.temp.name) / ('setup.exe' if installed else 'portable.exe')
        path.write_bytes(data)
        result = ArtifactDownloadResult(path, len(data), hashlib.sha256(data).hexdigest())
        self.dialog._download_manifest = selected
        self.dialog._downloaded(result)
        return manifest, result

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

    def test_verified_new_release_downloads_after_user_action_without_running_it(self):
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
            download.assert_not_called()
            self.assertIsNone(self.dialog.download_result)
            self.assertTrue(self.dialog.download_button.isEnabled())
            self.assertTrue(self.dialog.begin_update())
            self.wait_for_worker()
            download.assert_called_once()
            self.assertEqual(download.call_args.args[0], manifest)
            self.assertEqual(download.call_args.args[1].name, 'MechaHub')
            self.assertEqual(self.dialog.download_result, result)
            self.assertEqual(self.dialog.download_progress.value(), 100)
            self.assertEqual(announced, [str(path)])
            self.assertFalse(self.dialog.open_folder_button.isHidden())
            self.assertTrue(self.dialog.open_button.isHidden())
            self.assertTrue(self.dialog.install_button.isEnabled())
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
            self.assertTrue(self.dialog.begin_update())
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
        self.dialog.auto_download_checkbox.setChecked(True)
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
        self.assertFalse(self.dialog.download_button.isEnabled())
        self.assertFalse(self.dialog.install_button.isEnabled())

    def test_startup_announces_each_new_version_once_without_download(self):
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        manifest = ReleaseManifest('99.0.0', 'https://releases.example/MechaHub.exe', '', 'a' * 64, 10)
        offers = []
        self.dialog.update_available.connect(offers.append)
        with patch.object(updates_dialog, 'fetch_manifest', return_value=manifest), \
                patch.object(updates_dialog, 'download_release') as download:
            self.assertTrue(self.dialog.begin_automatic_check())
            self.wait_for_worker()
            self.assertTrue(self.dialog.begin_automatic_check())
            self.wait_for_worker()
            self.assertEqual(offers, [manifest])
            download.assert_not_called()
        next_release = ReleaseManifest('100.0.0', 'https://releases.example/MechaHub.exe', '', 'b' * 64, 10)
        self.dialog._received(next_release)
        self.assertEqual(offers, [manifest, next_release])

    def test_automatic_download_preference_defaults_to_existing_auto_check_choice(self):
        self.settings.remove(updates_dialog.AUTOMATIC_DOWNLOAD_SETTING)
        self.settings.setValue(updates_dialog.MANIFEST_SETTING, DEFAULT_MANIFEST_URL)
        for old_choice in (True, False):
            with self.subTest(previous_automatic=old_choice):
                self.settings.setValue(updates_dialog.AUTOMATIC_SETTING, old_choice)
                with patch.object(updates_dialog, 'fetch_manifest') as fetch, \
                        patch.object(updates_dialog, 'download_release') as download:
                    second = updates_dialog.UpdatesDialog()
                    self.assertEqual(second.auto_download_checkbox.isChecked(), old_choice)
                    fetch.assert_not_called()
                    download.assert_not_called()
                    second.deleteLater()

    def test_automatic_download_choice_is_saved_separately_from_check_choice(self):
        self.assertFalse(self.dialog.auto_download_checkbox.isEnabled())
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        self.dialog.auto_download_checkbox.setChecked(True)
        self.assertTrue(self.settings.value(updates_dialog.AUTOMATIC_DOWNLOAD_SETTING, type=bool))
        self.assertTrue(self.settings.value(updates_dialog.AUTOMATIC_SETTING, type=bool))
        self.dialog.auto_download_checkbox.setChecked(False)
        self.assertFalse(self.settings.value(updates_dialog.AUTOMATIC_DOWNLOAD_SETTING, type=bool))
        self.assertTrue(self.settings.value(updates_dialog.AUTOMATIC_SETTING, type=bool))

    def test_automatic_prefetch_selects_correct_artifact_without_installing(self):
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        for installed in (False, True):
            with self.subTest(installed=installed):
                self.dialog._automatic_download_preference = False
                manifest, result = self.ready_release(installed=installed)
                self.dialog._automatic_download_preference = True
                requested = []
                self.dialog.install_requested.connect(lambda *args: requested.append(args))
                with patch.object(updates_dialog, 'fetch_manifest', return_value=manifest), \
                        patch.object(updates_dialog, 'download_release', return_value=result) as download, \
                        patch.object(updates_dialog.QMessageBox, 'question') as prompt:
                    self.assertTrue(self.dialog.begin_automatic_check())
                    self.wait_for_worker()
                    download.assert_called_once()
                    chosen = download.call_args.args[0]
                    self.assertEqual(chosen.download_url,
                                     manifest.installer_download_url if installed else manifest.download_url)
                    self.assertEqual(chosen.sha256,
                                     manifest.installer_sha256 if installed else manifest.sha256)
                    prompt.assert_not_called()
                self.assertEqual(requested, [])
                self.assertTrue(self.dialog.install_button.isEnabled())
                self.assertEqual(self.dialog.download_result, result)

    def test_source_execution_or_legacy_page_never_starts_automatic_download(self):
        self.dialog._automatic_download_preference = True
        for mode, checksum, url in (
            ('source', 'a' * 64, 'https://releases.example/MechaHub.exe'),
            ('portable', '', 'https://releases.example/v99'),
            ('installed', 'a' * 64, 'https://releases.example/MechaHub.exe'),
        ):
            with self.subTest(mode=mode, checksum=checksum):
                self.dialog.installation_context = SimpleNamespace(mode=mode)
                with patch.object(updates_dialog, 'download_release') as download:
                    self.dialog._received(ReleaseManifest('99.0.0', url, '', checksum, 10))
                    self.app.processEvents()
                    self.assertIsNone(self.dialog.worker)
                    self.assertIsNone(self.dialog._pending_download)
                    download.assert_not_called()

    def test_dismissing_dialog_keeps_periodic_automatic_checks_available(self):
        self.dialog.source_input.setText('https://updates.example/manifest.json')
        self.dialog.show()
        self.dialog.close()
        self.assertTrue(self.dialog._close_pending)
        with patch.object(updates_dialog, 'fetch_manifest',
                          return_value=ReleaseManifest('0.1.0', 'https://releases.example/old')):
            self.assertTrue(self.dialog.begin_automatic_check())
            self.wait_for_worker()
        self.assertFalse(self.dialog.isVisible())

    def test_install_requests_host_only_after_explicit_confirmation(self):
        manifest, result = self.ready_release()
        requested = []
        self.dialog.install_requested.connect(lambda release, artifact: requested.append((release, artifact)))
        with patch.object(updates_dialog.QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            self.dialog._request_install()
        self.assertEqual(requested, [])
        self.assertTrue(self.dialog.install_button.isEnabled())
        with patch.object(updates_dialog.QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes) as prompt, \
                patch.object(updates_dialog.QDesktopServices, 'openUrl') as external:
            self.dialog._request_install()
            self.dialog._request_install()
            prompt.assert_called_once()
            external.assert_not_called()
        self.assertEqual(requested, [(manifest, result)])
        self.assertFalse(self.dialog.install_button.isEnabled())
        self.assertFalse(self.dialog.check_button.isEnabled())
        self.dialog.installation_failed('Не вдалося зберегти конспект. Спробуй пізніше.')
        self.assertTrue(self.dialog.install_button.isEnabled())
        self.assertIn('конспект', self.dialog.status_label.text())

    def test_installed_app_downloads_installer_and_not_portable_executable(self):
        manifest, result = self.ready_release(installed=True)
        self.dialog.download_result = None
        self.dialog._verified_manifest = None
        self.assertNotEqual(self.dialog._selected_artifact.download_url, manifest.download_url)
        with patch.object(updates_dialog, 'download_release', return_value=result) as download:
            self.assertTrue(self.dialog.begin_update())
            self.wait_for_worker()
        selected = download.call_args.args[0]
        self.assertEqual(selected.download_url, manifest.installer_download_url)
        self.assertEqual(selected.sha256, manifest.installer_sha256)
        self.assertEqual(selected.size, manifest.installer_size)
        self.assertTrue(self.dialog.install_button.isEnabled())

    def test_installed_app_without_installer_metadata_has_only_manual_fallback(self):
        self.dialog.installation_context = SimpleNamespace(mode='installed')
        manifest = ReleaseManifest('99.0.0', 'https://releases.example/MechaHub.exe', '', 'a' * 64, 10)
        self.dialog._received(manifest)
        with patch.object(updates_dialog, 'download_release') as download, \
                patch.object(updates_dialog.QMessageBox, 'question') as prompt:
            self.assertFalse(self.dialog.begin_update())
            self.dialog._request_install()
            download.assert_not_called()
            prompt.assert_not_called()
        self.assertFalse(self.dialog.open_button.isHidden())
        self.assertTrue(self.dialog.download_button.isHidden())
        self.assertFalse(self.dialog.install_button.isEnabled())
        self.assertIn('інсталятора', self.dialog.status_label.text())

    def test_source_execution_does_not_offer_installation(self):
        manifest, result = self.ready_release()
        self.dialog.installation_context = SimpleNamespace(mode='source')
        self.dialog._update_controls()
        with patch.object(updates_dialog.QMessageBox, 'question') as prompt:
            self.dialog._request_install()
            prompt.assert_not_called()
        self.assertFalse(self.dialog.install_button.isEnabled())

    def test_source_change_invalidates_previously_verified_download(self):
        self.ready_release()
        self.dialog.source_input.setText('https://new.example/update.json')
        self.assertIsNone(self.dialog.download_result)
        self.assertTrue(self.dialog.install_button.isHidden())
        self.assertTrue(self.dialog.open_folder_button.isHidden())
        with patch.object(updates_dialog.QMessageBox, 'question') as prompt:
            self.dialog._request_install()
            prompt.assert_not_called()

    def test_source_change_during_install_prompt_cannot_install_stale_file(self):
        self.ready_release()
        requested = []
        self.dialog.install_requested.connect(lambda *args: requested.append(args))

        def edit_source(*args):
            self.dialog.source_input.setText('https://new.example/update.json')
            return QMessageBox.StandardButton.Yes

        with patch.object(updates_dialog.QMessageBox, 'question', side_effect=edit_source):
            self.dialog._request_install()
        self.assertEqual(requested, [])

    def test_missing_or_mismatched_saved_file_disables_install(self):
        manifest, result = self.ready_release()
        result.path.unlink()
        self.dialog._update_controls()
        self.assertFalse(self.dialog.install_button.isEnabled())
        with patch.object(updates_dialog.QMessageBox, 'question') as prompt:
            self.dialog._request_install()
            prompt.assert_not_called()
        self.assertTrue(self.dialog.download_button.isEnabled())
        self.assertFalse(self.dialog.download_button.isHidden())
        self.assertIn('повторно', self.dialog.status_label.text())
        result.path.write_bytes(b'bad bytes')
        bad_result = ArtifactDownloadResult(result.path, 9, '0' * 64)
        self.dialog._downloaded(bad_result)
        self.assertIsNone(self.dialog.download_result)
        self.assertIn('не відповідає', self.dialog.status_label.text())

    def test_update_offer_opens_online_tab_without_starting_network(self):
        self.ready_release()
        with patch.object(updates_dialog, 'fetch_manifest') as fetch, \
                patch.object(updates_dialog, 'download_release') as download:
            self.dialog.show_update_offer()
            self.app.processEvents()
            self.assertTrue(self.dialog.isVisible())
            self.assertEqual(self.dialog.tabs.currentIndex(), 1)
            fetch.assert_not_called()
            download.assert_not_called()

    def test_minimum_window_keeps_install_and_check_controls_visible_with_long_notes(self):
        from mechahub.ui.styles import STYLESHEET
        self.dialog.setStyleSheet(STYLESHEET)
        long_notes = ('• Покращено навчання, роботу конспектів і перевірку оновлень.\n' * 100)
        self.ready_release(installed=True, notes=long_notes)
        self.dialog.show_update_offer()
        self.dialog.resize(580, 520)
        self.app.processEvents()
        outer_scroll = next(scroll for scroll in self.dialog.findChildren(QScrollArea)
                            if scroll.isVisible() and scroll is not self.dialog.notes_scroll)
        viewport = outer_scroll.viewport()
        action_rect = QRect(self.dialog.install_button.mapTo(viewport, QPoint(0, 0)),
                            self.dialog.install_button.size())
        self.assertTrue(viewport.rect().contains(action_rect),
                        f'Installation action {action_rect} is outside visible viewport {viewport.rect()}')
        self.assertTrue(self.dialog.install_button.isVisible())
        self.assertTrue(self.dialog.install_button.isEnabled())
        self.assertTrue(self.dialog.download_button.isHidden())
        for control in (self.dialog.check_button, self.dialog.cancel_button):
            self.assertTrue(control.isVisible())
            control_rect = QRect(control.mapTo(self.dialog, QPoint(0, 0)), control.size())
            self.assertTrue(self.dialog.rect().contains(control_rect))
        self.assertEqual(self.dialog.remote_notes.text(), long_notes)
        self.assertEqual(self.dialog.remote_notes.textFormat(), Qt.TextFormat.PlainText)
        self.assertLessEqual(self.dialog.notes_scroll.height(), 120)
        self.assertGreater(self.dialog.notes_scroll.verticalScrollBar().maximum(), 0)

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
