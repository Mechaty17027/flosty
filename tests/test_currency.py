"""Currency units, conversion arithmetic and bounded NBU requests."""
from datetime import date
from decimal import Decimal
from io import BytesIO
import json
import threading
import unittest
from unittest.mock import Mock, patch

from mechahub.core import currency as core


def daily_payload(**changes):
    rows = [dict(cc='USD', txt='Долар США', rate=40, exchangedate='14.10.2026'),
            dict(cc='EUR', txt='Євро', rate=44, exchangedate='14.10.2026'),
            dict(cc='PLN', txt='Злотий', rate=10, exchangedate='14.10.2026')]
    rows[0].update(changes)
    return json.dumps(rows).encode()


class FakeResponse(BytesIO):
    def __init__(self, payload, headers=None, url=core.LATEST_URL):
        super().__init__(payload)
        self.headers = headers or {}
        self.url = url

    def geturl(self):
        return self.url


class CurrencyTests(unittest.TestCase):
    def test_cross_conversion_via_hryvnia_preserves_decimal_precision(self):
        rates = core.parse_rates(daily_payload())
        self.assertEqual(core.convert_amount(Decimal('1000'), 'UAH', 'USD', rates), Decimal('25'))
        self.assertEqual(core.convert_amount(Decimal('25'), 'USD', 'UAH', rates), Decimal('1000'))
        self.assertEqual(core.convert_amount(Decimal('110'), 'USD', 'EUR', rates), Decimal('100'))
        self.assertEqual(core.convert_amount(Decimal('123.45'), 'PLN', 'PLN', rates), Decimal('123.45'))

    def test_comma_decimal_and_grouping_work_without_binary_float_rounding(self):
        self.assertEqual(core.parse_amount(' 1 000,50 '), Decimal('1000.50'))
        self.assertEqual(core.parse_amount('1\u202f000.125'), Decimal('1000.125'))
        self.assertEqual(core.parse_amount('0'), Decimal('0'))
        for amount in ('-1', 'NaN', 'Infinity', '1e3', '1,2,3', '', '1000000000001', '0.123456789'):
            with self.subTest(amount=amount), self.assertRaises(core.CurrencyError):
                core.parse_amount(amount)

    def test_missing_and_invalid_currencies_do_not_produce_a_plausible_total(self):
        rates = core.parse_rates(daily_payload())
        with self.assertRaises(core.CurrencyError):
            core.convert_amount(Decimal('100'), 'ZZZ', 'USD', rates)
        for amount in (Decimal('NaN'), Decimal('Infinity'), Decimal('-1'), 100):
            with self.subTest(amount=amount), self.assertRaises(core.CurrencyError):
                core.convert_amount(amount, 'UAH', 'USD', rates)

    def test_daily_rates_add_exact_hryvnia_and_exclude_metals(self):
        rows = json.loads(daily_payload())
        rows.append(dict(cc='XAU', txt='Золото', rate=150000, exchangedate='14.10.2026'))
        rates = core.parse_rates(json.dumps(rows).encode())
        self.assertEqual(rates['UAH'].uah_per_unit, Decimal('1'))
        self.assertEqual(rates['UAH'].effective_date, date(2026, 10, 14))
        self.assertNotIn('XAU', rates)
        self.assertEqual(rates['PLN'].name, 'Злотий')

    def test_corrupt_daily_data_is_rejected(self):
        for changes in ({'rate': 0}, {'rate': -1}, {'rate': 'NaN'}, {'rate': True},
                        {'cc': '../USD'}, {'txt': None}, {'exchangedate': '31.02.2026'}):
            with self.subTest(changes=changes), self.assertRaises(core.CurrencyError):
                core.parse_rates(daily_payload(**changes))
        for payload in (b'[]', b'{}', b'null', b'[1]', b'not json', b'\xff'):
            with self.subTest(payload=payload), self.assertRaises(core.CurrencyError):
                core.parse_rates(payload)

    def test_duplicate_daily_currency_is_rejected(self):
        rows = json.loads(daily_payload())
        rows.append(rows[0])
        with self.assertRaises(core.CurrencyError):
            core.parse_rates(json.dumps(rows).encode())

    def test_history_uses_per_unit_value_sorts_and_deduplicates(self):
        rows = [dict(cc='USD', exchangedate='14.10.2026', rate=4010, units=100, rate_per_unit=40.10),
                dict(cc='USD', exchangedate='13.10.2026', rate=4000, units=100, rate_per_unit=40),
                dict(cc='USD', exchangedate='13.10.2026', rate=4000, units=100, rate_per_unit=40)]
        points = core.parse_history(json.dumps(rows).encode(), 'USD', date(2026, 10, 13), date(2026, 10, 14))
        self.assertEqual([point.effective_date.day for point in points], [13, 14])
        self.assertEqual(points[-1].uah_per_unit, Decimal('40.1'))

    def test_history_does_not_accept_other_currency_outside_range_or_conflicts(self):
        row = dict(cc='USD', exchangedate='14.10.2026', rate_per_unit=40)
        for changes in ({'cc': 'EUR'}, {'exchangedate': '01.01.2026'}, {'rate_per_unit': -2}):
            with self.subTest(changes=changes), self.assertRaises(core.CurrencyError):
                core.parse_history(json.dumps([dict(row, **changes)]).encode(), 'USD', date(2026, 10, 13), date(2026, 10, 14))
        with self.assertRaises(core.CurrencyError):
            core.parse_history(json.dumps([row, dict(row, rate_per_unit=41)]).encode(), 'USD', date(2026, 10, 13), date(2026, 10, 14))

    def test_hryvnia_history_needs_no_network_and_has_thirty_daily_points(self):
        with patch.object(core, 'build_opener') as opener:
            points = core.fetch_history('UAH', end=date(2026, 10, 14))
        opener.assert_not_called()
        self.assertEqual(len(points), 30)
        self.assertEqual(points[0].effective_date, date(2026, 9, 15))
        self.assertTrue(all(point.uah_per_unit == 1 for point in points))

    def test_history_request_is_a_single_bounded_https_range(self):
        payload = json.dumps([dict(cc='USD', exchangedate='14.10.2026', rate_per_unit=40)]).encode()
        with patch.object(core, 'build_opener') as builder:
            opener = builder.return_value
            opener.open.return_value = FakeResponse(payload, url=core.HISTORY_URL)
            core.fetch_history('USD', end=date(2026, 10, 14))
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertIn('start=20260915&end=20261014&valcode=usd', request.full_url)
        self.assertEqual(opener.open.call_args.kwargs['timeout'], core.REQUEST_TIMEOUT)
        for days in (True, 1, 91, 3.5):
            with self.subTest(days=days), self.assertRaises(core.CurrencyError):
                core.fetch_history('USD', days=days)
        with self.assertRaises(core.CurrencyError):
            core.fetch_history('USD', end=0)

    def test_response_limit_applies_without_content_length(self):
        with patch.object(core, 'build_opener') as builder:
            builder.return_value.open.return_value = FakeResponse(b' ' * (core.MAX_RESPONSE_BYTES + 1))
            with self.assertRaises(core.CurrencyError):
                core.fetch_rates()

    def test_response_limit_applies_before_downloading_declared_oversize(self):
        response = FakeResponse(daily_payload(), {'Content-Length': str(core.MAX_RESPONSE_BYTES + 1)})
        with patch.object(core, 'build_opener') as builder:
            builder.return_value.open.return_value = response
            with self.assertRaises(core.CurrencyError):
                core.fetch_rates()
        self.assertTrue(response.closed)

    def test_cancellation_skips_network_and_checks_after_a_read(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(core, 'build_opener') as builder, self.assertRaises(core.CurrencyCancelled):
            core.fetch_rates(cancelled)
        builder.assert_not_called()
        cancelled.clear()
        response = FakeResponse(daily_payload())
        original_read = response.read
        def read(size):
            cancelled.set()
            return original_read(size)
        response.read = read
        with patch.object(core, 'build_opener') as builder:
            builder.return_value.open.return_value = response
            with self.assertRaises(core.CurrencyCancelled):
                core.fetch_rates(cancelled)
        self.assertTrue(response.closed)

    def test_slow_or_untrusted_response_is_rejected(self):
        with patch.object(core, 'build_opener') as builder:
            builder.return_value.open.return_value = FakeResponse(daily_payload(), url='http://bank.gov.ua/rates')
            with self.assertRaises(core.CurrencyError):
                core.fetch_rates()
        with patch.object(core, 'build_opener') as builder, patch.object(core.time, 'monotonic', side_effect=[0, 16]):
            builder.return_value.open.return_value = FakeResponse(daily_payload())
            with self.assertRaises(core.CurrencyError):
                core.fetch_rates()


if __name__ == '__main__':
    unittest.main()
