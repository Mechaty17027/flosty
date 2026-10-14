"""Counter semantics and visible-only chart collection regressions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from collections import namedtuple
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import psutil
from PySide6.QtWidgets import QApplication, QStackedWidget

from mechahub.ui.system_charts import SystemChartsPage, SystemLoadSampler, TimeSeriesChart


Cpu = namedtuple('Cpu', 'user system idle guest guest_nice')


def counters(cpu=(20, 10, 70, 0, 0), disk=(1000, 2000), net=(3000, 4000)):
    return SimpleNamespace(
        cpu_times=Mock(return_value=Cpu(*cpu)),
        virtual_memory=Mock(return_value=SimpleNamespace(percent=62)),
        disk_io_counters=Mock(return_value=SimpleNamespace(read_bytes=disk[0], write_bytes=disk[1])),
        net_io_counters=Mock(return_value=SimpleNamespace(bytes_recv=net[0], bytes_sent=net[1])),
    )


class SamplingTests(unittest.TestCase):
    def test_rates_use_elapsed_seconds_and_cpu_has_own_baseline(self):
        provider = counters()
        sampler = SystemLoadSampler(provider=provider, clock=Mock(side_effect=(10, 12)))
        sampler.prime()
        provider.cpu_times.return_value = Cpu(40, 20, 140, 0, 0)
        provider.disk_io_counters.return_value = SimpleNamespace(read_bytes=3048, write_bytes=3024)
        provider.net_io_counters.return_value = SimpleNamespace(bytes_recv=9144, bytes_sent=6048)
        sample = sampler.snapshot()
        self.assertEqual(sample, dict(cpu=30.0, memory=62.0, disk_read=1024.0, disk_write=512.0, download=3072.0, upload=1024.0))

    def test_cpu_guest_time_is_not_double_counted(self):
        provider = counters(cpu=(20, 10, 70, 5, 0))
        sampler = SystemLoadSampler(provider=provider, clock=Mock(side_effect=(10, 11)))
        sampler.prime()
        provider.cpu_times.return_value = Cpu(40, 20, 140, 10, 0)
        self.assertEqual(sampler.snapshot()['cpu'], 30)

    def test_counter_reset_and_unavailable_disk_do_not_fake_negative_rates(self):
        provider = counters()
        sampler = SystemLoadSampler(provider=provider, clock=Mock(side_effect=(10, 11)))
        sampler.prime()
        provider.disk_io_counters.side_effect = psutil.AccessDenied()
        provider.net_io_counters.return_value = SimpleNamespace(bytes_recv=0, bytes_sent=0)
        sample = sampler.snapshot()
        self.assertIsNone(sample['disk_read'])
        self.assertIsNone(sample['download'])
        self.assertEqual(sample['memory'], 62)

    def test_resume_primes_baselines_instead_of_including_hidden_traffic(self):
        provider = counters()
        sampler = SystemLoadSampler(provider=provider, clock=Mock(side_effect=(10, 100, 101)))
        sampler.prime()
        provider.net_io_counters.return_value = SimpleNamespace(bytes_recv=1000000, bytes_sent=2000000)
        sampler.prime()
        provider.net_io_counters.return_value = SimpleNamespace(bytes_recv=1000200, bytes_sent=2000100)
        sample = sampler.snapshot()
        self.assertEqual(sample['download'], 200)
        self.assertEqual(sample['upload'], 100)


class ChartUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_tab_switch_stops_timer_and_return_starts_fresh_history(self):
        stack = QStackedWidget()
        page = SystemChartsPage()
        other = QStackedWidget()
        stack.addWidget(page)
        stack.addWidget(other)
        try:
            self.assertFalse(page.timer.isActive())
            stack.show()
            self.app.processEvents()
            self.assertTrue(page.timer.isActive())
            page._poll()
            self.assertEqual(len(page.history['memory']), 1)
            stack.setCurrentWidget(other)
            self.app.processEvents()
            self.assertFalse(page.timer.isActive())
            stack.setCurrentWidget(page)
            self.app.processEvents()
            self.assertTrue(page.timer.isActive())
            self.assertEqual(len(page.history['memory']), 0)
            stack.showMinimized()
            self.app.processEvents()
            self.assertFalse(page.timer.isActive())
            stack.showNormal()
            self.app.processEvents()
            self.assertTrue(page.timer.isActive())
        finally:
            stack.close()
            stack.deleteLater()
            self.app.processEvents()

    def test_chart_handles_flat_values_and_missing_intervals(self):
        chart = TimeSeriesChart(value_formatter=lambda value: f'{value:.4f}')
        chart.resize(500, 170)
        chart.set_series([41.5, 41.5, None, 41.5], labels=['01.10', '02.10', '03.10', '04.10'])
        low, high = chart._range()
        self.assertLess(low, 41.5)
        self.assertGreater(high, 41.5)
        self.assertFalse(chart.grab().isNull())
        chart.deleteLater()
        self.app.processEvents()

    def test_live_history_is_bounded(self):
        page = SystemChartsPage()
        for _ in range(200):
            page.history['cpu'].append(10)
        self.assertEqual(len(page.history['cpu']), 120)
        page.deleteLater()
        self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
