"""Currency page lifecycle and useful calculations without live networking."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date
from decimal import Decimal
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mechahub.core.currency import CurrencyCancelled, CurrencyRate, HistoryPoint
from mechahub.ui import currency_page as ui
from mechahub.ui.styles import STYLESHEET


def sample_rates():
    return {code: CurrencyRate(code, code, Decimal(value), date(2026, 10, 14))
            for code, value in (('UAH', '1'), ('USD', '40'), ('EUR', '44'), ('GBP', '50'),
                                ('PLN', '10'), ('CHF', '46'), ('AUD', '27'))}


POINTS = (HistoryPoint(date(2026, 10, 13), Decimal('39.5')),
          HistoryPoint(date(2026, 10, 14), Decimal('40')))


class CurrencyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        # Windows' offscreen plugin has no system fonts until explicitly added.
        # Real glyph widths matter to the small-window layout regression.
        for filename in ('segoeui.ttf', 'segoeuib.ttf'):
            QFontDatabase.addApplicationFont(os.path.join(os.environ.get('SystemRoot', 'C:/Windows'), 'Fonts', filename))

    def setUp(self):
        self.page = ui.CurrencyPage()
        self.page._initial_requested = True

    def tearDown(self):
        self.page.shutdown()
        if self.page.worker is not None:
            self.page.worker.wait(2500)
            self.app.processEvents()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(condition(), 'Currency worker did not reach expected state')

    def test_hidden_page_does_not_contact_nbu_and_first_show_fetches_once(self):
        self.page._initial_requested = False
        with patch.object(ui, 'fetch_rates', return_value=sample_rates()) as request:
            self.app.processEvents()
            request.assert_not_called()
            self.page.show()
            self.wait_for(lambda: request.called and self.page.worker is None)
            self.page.hide()
            self.page.show()
            QTest.qWait(20)
        request.assert_called_once()
        self.assertIn('14.10.2026', self.page.status.text())
        self.assertEqual(self.page.rate_values['USD'].text(), '40,0000 ₴')

    def test_calculator_converts_any_received_currency_and_handles_invalid_input(self):
        self.page._loaded('rates', sample_rates())
        self.assertEqual(self.page.calc_result.text(), '1 000,00 UAH = 25,00 USD')
        self.page.source_currency.setCurrentText('USD')
        self.page.target_currency.setCurrentText('EUR')
        self.page.amount.setText('110')
        self.assertEqual(self.page.calc_result.text(), '110,00 USD = 100,00 EUR')
        self.page.source_currency.setCurrentText('AUD')
        self.page.target_currency.setCurrentText('UAH')
        self.page.amount.setText('1 000,50')
        self.assertEqual(self.page.calc_result.text(), '1 000,50 AUD = 27 013,50 UAH')
        self.page.amount.setText('-12')
        self.assertIn('невід’ємну', self.page.calc_result.text())
        self.assertEqual(self.page.calc_detail.text(), '')

    def test_swap_and_refresh_preserve_selection_and_amount(self):
        self.page._loaded('rates', sample_rates())
        self.page.amount.setText('250')
        self.page._swap()
        self.assertEqual(self.page.calc_result.text(), '250,00 USD = 10 000,00 UAH')
        self.page._loaded('rates', sample_rates())
        self.assertEqual(self.page.source_currency.currentText(), 'USD')
        self.assertEqual(self.page.target_currency.currentText(), 'UAH')
        self.assertEqual(self.page.amount.text(), '250')

    def test_offline_refresh_retains_last_successful_rate_and_calculation(self):
        self.page._loaded('rates', sample_rates())
        old_calculation = self.page.calc_result.text()
        with patch.object(ui, 'fetch_rates', side_effect=ValueError('Інтернет недоступний')):
            self.page.refresh()
            self.wait_for(lambda: self.page.worker is None)
        self.assertIn('Попередні курси збережено', self.page.status.text())
        self.assertEqual(self.page.calc_result.text(), old_calculation)
        self.assertEqual(self.page.rate_values['USD'].text(), '40,0000 ₴')
        self.assertTrue(self.page.refresh_button.isEnabled())

    def test_history_fetches_only_when_selected_and_reuses_cached_currency(self):
        self.page._loaded('rates', sample_rates())
        with patch.object(ui, 'fetch_history', return_value=POINTS) as request:
            request.assert_not_called()
            self.page.tabs.setCurrentIndex(2)
            self.wait_for(lambda: request.called and self.page.worker is None)
            self.page.tabs.setCurrentIndex(0)
            self.page.tabs.setCurrentIndex(2)
            QTest.qWait(20)
        request.assert_called_once()
        self.assertEqual(self.page.chart.labels, ['13.10.2026', '14.10.2026'])
        self.assertEqual(self.page.chart.series, [[39.5, 40.0]])
        self.assertIn('1 USD = 40,0000 UAH', self.page.chart_summary.text())

    def test_selecting_another_currency_cancels_old_request_and_replaces_graph(self):
        self.page._loaded('rates', sample_rates())
        entered = threading.Event()
        calls = []
        def history(code, *, end, cancel_event):
            calls.append(code)
            if code == 'USD':
                entered.set()
                cancel_event.wait(2)
                raise CurrencyCancelled()
            return POINTS
        with patch.object(ui, 'fetch_history', side_effect=history):
            self.page.tabs.setCurrentIndex(2)
            self.wait_for(entered.is_set)
            self.page.chart_currency.setCurrentText('EUR')
            self.wait_for(lambda: 'EUR' in self.page.history_cache and self.page.worker is None)
        self.assertEqual(calls, ['USD', 'EUR'])
        self.assertNotIn('USD', self.page.history_cache)
        self.assertIn('1 EUR', self.page.chart_summary.text())

    def test_finished_signal_ownership_prevents_replacing_an_old_worker(self):
        previous = Mock()
        previous.isRunning.return_value = False
        self.page.worker = previous
        try:
            with patch.object(ui, 'CurrencyThread') as worker_type:
                self.page.refresh()
            worker_type.assert_not_called()
            self.assertIs(self.page.worker, previous)
        finally:
            self.page.worker = None

    def test_close_is_nonblocking_and_waits_for_cancelled_worker_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        notifications = []
        self.page.shutdown_finished.connect(lambda: notifications.append('done'))
        self.page.show()
        def rates(cancel_event):
            entered.set()
            cancel_event.wait(2)
            release.wait(2)
            raise CurrencyCancelled()
        try:
            with patch.object(ui, 'fetch_rates', side_effect=rates):
                self.page.refresh()
                self.wait_for(entered.is_set)
                start = time.monotonic()
                self.assertFalse(self.page.request_close())
                self.assertLess(time.monotonic() - start, .2)
                self.assertFalse(self.page.close())
                self.assertTrue(self.page.isVisible())
                release.set()
                self.wait_for(lambda: self.page.worker is None and not self.page.isVisible())
            self.assertEqual(notifications, ['done'])
            self.assertTrue(self.page.request_close())
        finally:
            release.set()

    def test_tabs_are_reachable_at_small_host_content_size(self):
        self.page._loaded('rates', sample_rates())
        self.page.setStyleSheet(STYLESHEET)
        self.page.resize(680, 535)
        self.page.show()
        self.app.processEvents()
        self.assertLessEqual(self.page.height(), 535)
        self.page.tabs.setCurrentIndex(1)
        self.app.processEvents()
        for control in (self.page.refresh_button, self.page.amount, self.page.source_currency,
                        self.page.target_currency, self.page.swap_button, self.page.calc_result):
            self.assertTrue(control.isVisible())
            bounds = QRect(control.mapTo(self.page, QPoint(0, 0)), control.size())
            self.assertTrue(self.page.rect().contains(bounds), f'Control clipped: {control.accessibleName()}')
        self.page.history_cache['USD'] = POINTS
        self.page.tabs.setCurrentIndex(2)
        self.app.processEvents()
        self.assertGreaterEqual(self.page.chart.height(), 215)
        self.assertFalse(self.page.grab().isNull())


if __name__ == '__main__':
    unittest.main()
