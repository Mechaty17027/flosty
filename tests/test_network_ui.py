"""Offscreen network page regressions; every worker uses a mocked speed test."""
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mechahub.core.speedtest import SpeedTestResult, TestCancelled
from mechahub.ui import network_page as ui
from mechahub.ui.styles import STYLESHEET


class NetworkPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.page = ui.NetworkPage()

    def tearDown(self):
        self.page.shutdown()
        if self.page.worker is not None:
            self.page.worker.wait(2000)
            self.app.processEvents()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def wait_for(self, condition):
        deadline = time.monotonic()+2
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(condition(), 'Worker did not reach the expected state')

    def test_start_cannot_replace_worker_awaiting_finished_signal(self):
        # isRunning() can already be False while its finished slot is queued.
        old = Mock()
        old.isRunning.return_value = False
        self.page.worker = old
        try:
            with patch.object(ui, 'SpeedTestThread') as new_worker:
                self.page._start()
                self.page.toggle()
            new_worker.assert_not_called()
            self.assertIs(self.page.worker, old)
            old.stop.assert_called_once()
        finally:
            self.page.worker = None

    def test_shutdown_is_nonblocking_and_page_closes_only_after_worker_finish(self):
        entered = threading.Event()
        release = threading.Event()
        def measured(stage, speed, cancelled):
            entered.set()
            cancelled.wait(2)
            release.wait(2)
            raise TestCancelled()
        self.page.show()
        notifications = []
        self.page.shutdown_finished.connect(lambda: notifications.append('finished'))
        try:
            with patch.object(ui, 'speed_test', side_effect=measured):
                self.page._start()
                self.wait_for(entered.is_set)
                start = time.monotonic()
                self.assertFalse(self.page.shutdown())
                self.assertLess(time.monotonic()-start, .2)
                self.assertTrue(self.page.worker.cancel_event.is_set())
                self.assertFalse(self.page.start_button.isEnabled())
                self.assertFalse(self.page.close())
                self.assertTrue(self.page.isVisible())
                self.assertIsNotNone(self.page.worker)
                release.set()
                self.wait_for(lambda: self.page.worker is None)
                self.wait_for(lambda: not self.page.isVisible())
                self.assertEqual(notifications, ['finished'])
                self.assertTrue(self.page.shutdown())
        finally:
            release.set()

    def test_cancelled_partial_result_marks_estimate_and_unknown_upload(self):
        result = SpeedTestResult(
            ping={'sent': 10, 'received': 9, 'ping_ms': 15, 'jitter_ms': 2, 'lost_percent': 10},
            download_mbps=42.3, upload_mbps=None, interface=None, counters={},
            downloaded_bytes=7*1024*1024, partial_download=True, cancelled=True)
        self.page._cancelled(result)
        self.assertEqual(self.page.status_title.text(), 'Тест зупинено')
        self.assertEqual(self.page.values['download'].text(), '≈ 42.3')
        self.assertEqual(self.page.values['upload'].text(), '—')
        self.assertEqual(self.page.values['ping'].text(), '15.0')
        self.assertIn('7.00 МіБ', self.page.description.text())

    def test_failed_request_is_labelled_partial_and_error_names_phase(self):
        result = SpeedTestResult(ping={}, download_mbps=10, upload_mbps=None,
                                 interface=None, counters={}, error_download='timeout')
        self.page._results(result)
        self.assertEqual(self.page.status_title.text(), 'Тест завершено частково')
        self.assertEqual(self.page.values['download'].text(), '≈ 10.0')
        self.assertIn('Download: timeout', self.page.errors_label.text())

    def test_unexpected_error_does_not_leave_live_reading_as_final_result(self):
        self.page._speed('download', 125)
        self.page._failed('unexpected error')
        self.assertEqual(self.page.values['download'].text(), '—')
        self.assertEqual(self.page.values['upload'].text(), '—')

    def test_speed_controls_fit_small_content_area_and_can_start_test(self):
        # A 980x690 host also needs space for its sidebar, heading and margins.
        # Exercise the smaller content area using real styles and hit testing.
        self.page.setStyleSheet(STYLESHEET)
        self.page.resize(680, 535)
        self.page.show()
        self.app.processEvents()
        self.assertLessEqual(self.page.height(), 535)

        def assert_controls_reachable():
            for control in (self.page.gauge, self.page.start_button,
                            self.page.values['download'], self.page.values['upload'],
                            self.page.values['ping']):
                self.assertTrue(control.isVisible())
                bounds = QRect(control.mapTo(self.page, QPoint(0, 0)), control.size())
                self.assertTrue(self.page.rect().contains(bounds), f'Control clipped: {control.objectName()}')
            self.assertIs(self.page.childAt(self.page.start_button.mapTo(self.page,
                              self.page.start_button.rect().center())), self.page.start_button)

        assert_controls_reachable()
        result = SpeedTestResult(ping={'ping_ms': 17}, download_mbps=100,
                                 upload_mbps=45, interface=None, counters={})
        with patch.object(ui, 'speed_test', return_value=result) as measured:
            QTest.mouseClick(self.page.start_button, Qt.MouseButton.LeftButton)
            self.wait_for(lambda: measured.called and self.page.worker is None)
        measured.assert_called_once()
        self.assertEqual(self.page.values['download'].text(), '100.0')
        assert_controls_reachable()

    def test_quality_tab_shows_same_ping_result_without_speed_controls(self):
        result = SpeedTestResult(ping={'ping_ms': 17, 'jitter_ms': 3, 'lost_percent': 0},
                                 download_mbps=100, upload_mbps=45,
                                 interface=None, counters={})
        self.page._results(result)
        self.page.resize(680, 535)
        self.page.show()
        self.page.tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.assertFalse(self.page.start_button.isVisible())
        self.assertTrue(self.page.values['jitter'].isVisible())
        self.assertTrue(self.page.values['loss'].isVisible())
        visible_ping = [item for item in self.page.metric_labels['ping'] if item.isVisible()]
        self.assertEqual(len(visible_ping), 1)
        self.assertEqual(visible_ping[0].text(), '17.0')
        self.assertEqual(self.page.values['ping'].text(), '17.0')


if __name__ == '__main__':
    unittest.main()
