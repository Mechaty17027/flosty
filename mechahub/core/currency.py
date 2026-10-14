"""Official NBU exchange rates, bounded historical requests and UAH conversions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from http.client import HTTPException
import json
import re
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

NBU_SOURCE_URL = 'https://bank.gov.ua/ua/markets/exchangerates'
NBU_OPEN_DATA_URL = 'https://bank.gov.ua/ua/open-data'
LATEST_URL = 'https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json'
HISTORY_URL = 'https://bank.gov.ua/NBU_Exchange/exchange_site'
POPULAR_CURRENCIES = ('USD', 'EUR', 'GBP', 'PLN', 'CHF')
MAX_RESPONSE_BYTES = 256 * 1024
REQUEST_TIMEOUT = 4.0
REQUEST_DEADLINE = 15.0
MAX_AMOUNT = Decimal('1000000000000')
_METALS = {'XAU', 'XAG', 'XPT', 'XPD'}


class CurrencyError(ValueError):
    """The NBU response or requested calculation cannot be used."""


class CurrencyCancelled(Exception):
    """The page is closing or the user cancelled a request."""


@dataclass(frozen=True)
class CurrencyRate:
    code: str
    name: str
    uah_per_unit: Decimal
    effective_date: date


@dataclass(frozen=True)
class HistoryPoint:
    effective_date: date
    uah_per_unit: Decimal


def _code(code: object) -> str:
    if not isinstance(code, str) or not re.fullmatch(r'[A-Z]{3}', code):
        raise CurrencyError('Некоректний код валюти у відповіді НБУ.')
    return code


def _rate(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise CurrencyError('НБУ повернув некоректний курс валюти.')
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise CurrencyError('НБУ повернув некоректний курс валюти.') from error
    if not result.is_finite() or not Decimal('0') < result <= Decimal('1000000000'):
        raise CurrencyError('Курс має бути додатним скінченним числом.')
    return result


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise CurrencyError('НБУ не вказав дату курсу.')
    try:
        return datetime.strptime(value, '%d.%m.%Y').date()
    except ValueError as error:
        raise CurrencyError('НБУ повернув некоректну дату курсу.') from error


def _rows(payload: bytes) -> list[dict]:
    if len(payload) > MAX_RESPONSE_BYTES:
        raise CurrencyError('Відповідь НБУ перевищує дозволений розмір.')
    try:
        data = json.loads(payload.decode('utf-8-sig'), parse_float=Decimal)
    except (UnicodeError, ValueError) as error:
        raise CurrencyError('НБУ повернув некоректний JSON.') from error
    if not isinstance(data, list) or not data or len(data) > 500:
        raise CurrencyError('НБУ не повернув список курсів валют.')
    if not all(isinstance(row, dict) for row in data):
        raise CurrencyError('НБУ повернув некоректний список курсів.')
    return data


def parse_rates(payload: bytes) -> dict[str, CurrencyRate]:
    """Legacy daily endpoint expresses rates per one currency unit.

    Banking metals are excluded from the money calculator. UAH is supplied
    locally because its hryvnia equivalent is always exactly one.
    """
    rates: dict[str, CurrencyRate] = {}
    for row in _rows(payload):
        code = _code(row.get('cc'))
        if code in _METALS or code == 'UAH':
            continue
        if code in rates:
            raise CurrencyError('НБУ повернув дубльовані курси однієї валюти.')
        name = row.get('txt')
        if not isinstance(name, str) or not name.strip() or len(name) > 150:
            raise CurrencyError('НБУ повернув некоректну назву валюти.')
        rates[code] = CurrencyRate(code, name.strip(), _rate(row.get('rate')), _date(row.get('exchangedate')))
    if not all(code in rates for code in ('USD', 'EUR')):
        raise CurrencyError('У відповіді НБУ відсутні курси USD або EUR.')
    effective_date = rates['USD'].effective_date
    rates['UAH'] = CurrencyRate('UAH', 'Українська гривня', Decimal('1'), effective_date)
    return rates


def parse_history(payload: bytes, code: str, start: date, end: date) -> tuple[HistoryPoint, ...]:
    _code(code)
    points: dict[date, HistoryPoint] = {}
    for row in _rows(payload):
        if row.get('cc') != code:
            raise CurrencyError('НБУ повернув історію іншої валюти.')
        effective_date = _date(row.get('exchangedate'))
        if not start <= effective_date <= end:
            raise CurrencyError('Дата курсу виходить за межі запитаного періоду.')
        # The history API's rate can be for 10/100 units. Its explicit
        # rate_per_unit field prevents plotting/using that value as one unit.
        point = HistoryPoint(effective_date, _rate(row.get('rate_per_unit')))
        if effective_date in points and points[effective_date] != point:
            raise CurrencyError('НБУ повернув суперечливі курси на одну дату.')
        points[effective_date] = point
    return tuple(points[day] for day in sorted(points))


def parse_amount(text: str) -> Decimal:
    if not isinstance(text, str) or len(text) > 80:
        raise CurrencyError('Введи суму цифрами, наприклад 1000 або 1000,50.')
    cleaned = text.strip().replace(' ', '').replace('\u00a0', '').replace('\u202f', '').replace(',', '.')
    if not re.fullmatch(r'[0-9]{1,13}(?:\.[0-9]{1,8})?', cleaned):
        raise CurrencyError('Введи невід’ємну суму, наприклад 1000 або 1000,50.')
    amount = Decimal(cleaned)
    if amount > MAX_AMOUNT:
        raise CurrencyError('Максимальна сума — 1 000 000 000 000.')
    return amount


def convert_amount(amount: Decimal, source: str, target: str,
                   rates: dict[str, CurrencyRate]) -> Decimal:
    if not isinstance(amount, Decimal) or not amount.is_finite() or not Decimal('0') <= amount <= MAX_AMOUNT:
        raise CurrencyError('Сума має бути невід’ємним скінченним числом.')
    if source not in rates or target not in rates:
        raise CurrencyError('Для вибраної валюти ще немає завантаженого курсу.')
    with localcontext() as context:
        context.prec = 40
        return amount * rates[source].uah_per_unit / rates[target].uah_per_unit


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CurrencyCancelled()


class _NbuRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlsplit(new_url)
        if parsed.scheme != 'https' or parsed.hostname != 'bank.gov.ua' or parsed.username or parsed.password:
            raise CurrencyError('НБУ перенаправив запит на непідтримувану адресу.')
        return super().redirect_request(request, response, code, message, headers, new_url)


def _fetch(url: str, cancel_event: threading.Event | None) -> bytes:
    _check_cancel(cancel_event)
    deadline = time.monotonic() + REQUEST_DEADLINE
    request = Request(url, headers={'User-Agent': 'MechaHub currency reader', 'Accept': 'application/json'})
    try:
        with build_opener(_NbuRedirectHandler()).open(request, timeout=REQUEST_TIMEOUT) as response:
            final_url = urlsplit(response.geturl())
            if final_url.scheme != 'https' or final_url.hostname != 'bank.gov.ua':
                raise CurrencyError('Курси можна завантажувати лише з офіційного HTTPS-сервера НБУ.')
            length = response.headers.get('Content-Length')
            if length and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
                raise CurrencyError('Відповідь НБУ перевищує дозволений розмір.')
            pieces: list[bytes] = []
            size = 0
            while True:
                _check_cancel(cancel_event)
                if time.monotonic() >= deadline:
                    raise CurrencyError('НБУ відповідає занадто довго. Спробуй ще раз.')
                block = response.read(min(8192, MAX_RESPONSE_BYTES + 1 - size))
                _check_cancel(cancel_event)
                if not block:
                    break
                size += len(block)
                if size > MAX_RESPONSE_BYTES:
                    raise CurrencyError('Відповідь НБУ перевищує дозволений розмір.')
                pieces.append(block)
            return b''.join(pieces)
    except (URLError, OSError, HTTPException) as error:
        _check_cancel(cancel_event)
        raise CurrencyError('Не вдалося отримати курси НБУ. Перевір інтернет і натисни «Оновити».') from error


def fetch_rates(cancel_event: threading.Event | None = None) -> dict[str, CurrencyRate]:
    return parse_rates(_fetch(LATEST_URL, cancel_event))


def fetch_history(code: str, *, end: date | None = None, days: int = 30,
                  cancel_event: threading.Event | None = None) -> tuple[HistoryPoint, ...]:
    _code(code)
    if isinstance(days, bool) or not isinstance(days, int) or not 2 <= days <= 90:
        raise CurrencyError('Період графіка має бути від 2 до 90 днів.')
    end = date.today() if end is None else end
    if not isinstance(end, date) or isinstance(end, datetime):
        raise CurrencyError('Потрібна коректна кінцева дата графіка.')
    start = end - timedelta(days=days - 1)
    _check_cancel(cancel_event)
    if code == 'UAH':
        return tuple(HistoryPoint(start + timedelta(days=offset), Decimal('1')) for offset in range(days))
    query = urlencode({'start': start.strftime('%Y%m%d'), 'end': end.strftime('%Y%m%d'),
                       'valcode': code.lower(), 'sort': 'exchangedate', 'order': 'asc'})
    return parse_history(_fetch(f'{HISTORY_URL}?{query}&json', cancel_event), code, start, end)
