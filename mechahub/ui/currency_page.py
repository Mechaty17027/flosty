"""NBU currency cards, a UAH-based calculator and thirty-day history."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
import threading

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from mechahub.core.currency import (
    CurrencyCancelled, CurrencyError, CurrencyRate, HistoryPoint,
    NBU_OPEN_DATA_URL, NBU_SOURCE_URL, POPULAR_CURRENCIES,
    convert_amount, fetch_history, fetch_rates, parse_amount,
)
from mechahub.ui.system_charts import TimeSeriesChart
from mechahub.ui.icons import icon


def _label(text: str, kind: str | None = None, *, wrap: bool = False) -> QLabel:
    item = QLabel(text)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(wrap)
    if kind:
        item.setObjectName(kind)
    return item


def _number(value: Decimal, places: int = 4) -> str:
    with localcontext() as context:
        context.prec = 50
        rounded = value.quantize(Decimal(1).scaleb(-places))
    return f'{rounded:,.{places}f}'.replace(',', ' ').replace('.', ',')


def _card(accent: str = 'cyan') -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName('Card')
    frame.setProperty('accent', accent)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 13, 16, 13)
    layout.setSpacing(6)
    return frame, layout


class CurrencyThread(QThread):
    loaded = Signal(str, object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, kind: str, code: str = 'USD', end: date | None = None, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.code = code
        self.end = end
        self.cancel_event = threading.Event()

    def stop(self):
        self.cancel_event.set()

    def run(self):
        try:
            result = (fetch_rates(self.cancel_event) if self.kind == 'rates'
                      else fetch_history(self.code, end=self.end, cancel_event=self.cancel_event))
        except CurrencyCancelled:
            self.cancelled.emit()
        except Exception as error:
            self.failed.emit(str(error))
        else:
            if self.cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.loaded.emit(self.code if self.kind == 'history' else 'rates', result)


class CurrencyPage(QWidget):
    """Fetch lazily, keep successful data, and cancel before the host closes."""
    shutdown_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: CurrencyThread | None = None
        self.close_pending = False
        self.rates: dict[str, CurrencyRate] = {}
        self.history_cache: dict[str, tuple[HistoryPoint, ...]] = {}
        self._initial_requested = False
        self._history_pending = False
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        toolbar = QHBoxLayout()
        self.status = _label('Курси ще не завантажено', 'Muted', wrap=True)
        toolbar.addWidget(self.status, 1)
        self.cancel_button = QPushButton('Зупинити')
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.hide()
        toolbar.addWidget(self.cancel_button)
        self.refresh_button = QPushButton('Оновити')
        self.refresh_button.setObjectName('Primary')
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)
        outer.addLayout(toolbar)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs, 1)
        self.tabs.currentChanged.connect(self._tab_changed)

        rates_page = QWidget()
        rates_layout = QVBoxLayout(rates_page)
        rates_layout.setContentsMargins(0, 8, 0, 0)
        rates_layout.addWidget(_label('Гривень за 1 одиницю валюти', 'Section'))
        grid = QGridLayout()
        grid.setSpacing(10)
        self.rate_values: dict[str, QLabel] = {}
        self.rate_dates: dict[str, QLabel] = {}
        titles = {'USD': 'Долар США', 'EUR': 'Євро', 'GBP': 'Фунт стерлінгів',
                  'PLN': 'Польський злотий', 'CHF': 'Швейцарський франк', 'UAH': 'Українська гривня'}
        accents = ('cyan', 'purple', 'amber', 'green', 'purple', 'cyan')
        for index, code in enumerate((*POPULAR_CURRENCIES, 'UAH')):
            frame, layout = _card(accents[index])
            layout.addWidget(_label(f'{code} · {titles[code]}', 'Muted'))
            value = _label('—', 'Metric')
            value.setAccessibleName(f'{code}: курс у гривнях за одну одиницю')
            layout.addWidget(value)
            rate_date = _label('Очікуємо дані НБУ', 'Muted')
            layout.addWidget(rate_date)
            self.rate_values[code] = value
            self.rate_dates[code] = rate_date
            grid.addWidget(frame, index // 2, index % 2)
        rates_layout.addLayout(grid)
        rates_layout.addStretch()
        self.tabs.addTab(self._scroll(rates_page), 'Курси')

        calc_page = QWidget()
        calc_layout = QVBoxLayout(calc_page)
        calc_layout.setContentsMargins(0, 8, 0, 0)
        calc_layout.addWidget(_label('Перерахувати суму', 'Section'))
        box, form = _card('green')
        form.addWidget(_label('Сума', 'Muted'))
        self.amount = QLineEdit('1000')
        self.amount.setPlaceholderText('Наприклад: 1000,50')
        self.amount.setAccessibleName('Сума для перерахунку')
        self.amount.setMaxLength(80)
        form.addWidget(self.amount)
        row = QHBoxLayout()
        self.source_currency = QComboBox()
        self.target_currency = QComboBox()
        self.source_currency.setAccessibleName('Валюта початкової суми')
        self.target_currency.setAccessibleName('Валюта результату')
        for combo in (self.source_currency, self.target_currency):
            combo.setMinimumHeight(38)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(12)
            combo.addItems(('UAH', *POPULAR_CURRENCIES))
        self.target_currency.setCurrentText('USD')
        row.addWidget(self.source_currency, 1)
        self.swap_button = QPushButton()
        self.swap_button.setIcon(icon('updates', '#B3ECD4'))
        self.swap_button.setAccessibleName('Поміняти валюти місцями')
        self.swap_button.setToolTip('Поміняти валюти місцями')
        self.swap_button.clicked.connect(self._swap)
        row.addWidget(self.swap_button)
        row.addWidget(self.target_currency, 1)
        form.addLayout(row)
        self.calc_result = _label('Завантаж курси для розрахунку', 'Section', wrap=True)
        form.addWidget(self.calc_result)
        self.calc_detail = _label('', 'Muted', wrap=True)
        form.addWidget(self.calc_detail)
        calc_layout.addWidget(box)
        calc_layout.addWidget(_label('Розрахунок за офіційним курсом НБУ через гривню. '
                                    'Курс обміну в банку чи обміннику може відрізнятися.', 'Muted', wrap=True))
        calc_layout.addStretch()
        self.tabs.addTab(self._scroll(calc_page), 'Калькулятор')
        self.amount.textChanged.connect(self._calculate)
        self.source_currency.currentIndexChanged.connect(self._calculate)
        self.target_currency.currentIndexChanged.connect(self._calculate)

        chart_page = QWidget()
        chart_layout = QVBoxLayout(chart_page)
        chart_layout.setContentsMargins(0, 8, 0, 0)
        row = QHBoxLayout()
        row.addWidget(_label('Курс за останні 30 днів', 'Section'), 1)
        self.chart_currency = QComboBox()
        self.chart_currency.addItems(POPULAR_CURRENCIES)
        self.chart_currency.setMinimumHeight(36)
        self.chart_currency.setAccessibleName('Валюта графіка')
        self.chart_currency.currentTextChanged.connect(self._history_changed)
        row.addWidget(self.chart_currency)
        chart_layout.addLayout(row)
        self.chart = TimeSeriesChart(colors=('#83E3E0',), value_formatter=lambda value: f'{value:.4f}', x_caption='Дата')
        self.chart.setMinimumHeight(215)
        self.chart.setAccessibleName('Історія курсу, гривень за одну одиницю валюти')
        chart_layout.addWidget(self.chart, 1)
        self.chart_summary = _label('Вибери вкладку, щоб завантажити історію НБУ.', 'Muted', wrap=True)
        chart_layout.addWidget(self.chart_summary)
        self.tabs.addTab(chart_page, 'Графік')

        source = QLabel(f'Джерело: <a href="{NBU_SOURCE_URL}" style="color:#83E3E0">Національний банк України</a>'
                        f' · <a href="{NBU_OPEN_DATA_URL}" style="color:#C8B8FF">відкриті дані</a>')
        source.setWordWrap(True)
        source.setOpenExternalLinks(True)
        outer.addWidget(source)

    @staticmethod
    def _scroll(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        return scroll

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initial_requested and not self.close_pending:
            self._initial_requested = True
            QTimer.singleShot(0, self.refresh)

    def refresh(self):
        if self.worker is not None or self.close_pending:
            return
        self._initial_requested = True
        self._history_pending = self.tabs.currentIndex() == 2
        self._start('rates')

    def _start(self, kind: str, code: str = 'USD'):
        if self.worker is not None or self.close_pending:
            return
        end = self.rates['USD'].effective_date if 'USD' in self.rates else date.today()
        self.worker = CurrencyThread(kind, code, end, self)
        self.worker.loaded.connect(self._loaded)
        self.worker.failed.connect(self._failed)
        self.worker.cancelled.connect(self._cancelled)
        self.worker.finished.connect(self._finished)
        self.refresh_button.setEnabled(False)
        self.cancel_button.show()
        self.status.setText('Завантажуємо курси НБУ…' if kind == 'rates' else f'Завантажуємо історію {code}…')
        self.worker.start()

    def cancel(self):
        self._history_pending = False
        if self.worker is not None:
            self.worker.stop()
            self.status.setText('Зупиняємо запит…')

    def _loaded(self, code: str, result):
        if self.close_pending or (self.worker is not None and self.worker.cancel_event.is_set()):
            return
        if code == 'rates':
            self.rates = result
            self.history_cache.clear()
            for currency, value in self.rate_values.items():
                rate = self.rates.get(currency)
                value.setText(_number(rate.uah_per_unit) + ' ₴' if rate else '—')
                self.rate_dates[currency].setText(rate.effective_date.strftime('%d.%m.%Y') if rate else 'НБУ не повернув курс')
            available = ['UAH'] + [currency for currency in POPULAR_CURRENCIES if currency in result]
            available += sorted(set(result) - set(available))
            for combo in (self.source_currency, self.target_currency):
                previous = combo.currentText()
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(available)
                combo.setCurrentText(previous if previous in result else 'UAH')
                for index, currency in enumerate(available):
                    combo.setItemData(index, result[currency].name, Qt.ItemDataRole.ToolTipRole)
                combo.blockSignals(False)
            self._calculate()
            self.status.setText(f'Курси НБУ на {self.rates["USD"].effective_date:%d.%m.%Y}')
            self.chart.set_series([])
            self.chart_summary.setText('Відкрий «Графік», щоб завантажити історію НБУ.')
        else:
            self.history_cache[code] = result
            if code == self.chart_currency.currentText():
                self._show_history(code, result)
            self.status.setText(f'Історію {code} завантажено')

    def _calculate(self):
        if not self.rates:
            return
        source, target = self.source_currency.currentText(), self.target_currency.currentText()
        try:
            amount = parse_amount(self.amount.text())
            result = convert_amount(amount, source, target, self.rates)
        except CurrencyError as error:
            self.calc_result.setText(str(error))
            self.calc_detail.setText('')
            return
        self.calc_result.setText(f'{_number(amount, 2)} {source} = {_number(result, 2)} {target}')
        if source == 'UAH':
            self.calc_detail.setText(f'1 {target} = {_number(self.rates[target].uah_per_unit)} UAH'
                                     f' · курс на {self.rates[target].effective_date:%d.%m.%Y}')
        elif target == 'UAH':
            self.calc_detail.setText(f'1 {source} = {_number(self.rates[source].uah_per_unit)} UAH'
                                     f' · курс на {self.rates[source].effective_date:%d.%m.%Y}')
        else:
            self.calc_detail.setText(f'Еквівалент: {_number(amount * self.rates[source].uah_per_unit, 2)} UAH'
                                     f' · {source}: {self.rates[source].effective_date:%d.%m.%Y}'
                                     f' · {target}: {self.rates[target].effective_date:%d.%m.%Y}')

    def _swap(self):
        source, target = self.source_currency.currentText(), self.target_currency.currentText()
        self.source_currency.blockSignals(True)
        self.target_currency.blockSignals(True)
        self.source_currency.setCurrentText(target)
        self.target_currency.setCurrentText(source)
        self.source_currency.blockSignals(False)
        self.target_currency.blockSignals(False)
        self._calculate()

    def _tab_changed(self, index: int):
        if index == 2:
            self._history_changed(self.chart_currency.currentText())

    def _history_changed(self, code: str):
        if self.close_pending or self.tabs.currentIndex() != 2:
            return
        if code in self.history_cache:
            self._show_history(code, self.history_cache[code])
            return
        self.chart.set_series([])
        self.chart_summary.setText(f'Завантаження історії {code}…')
        if self.worker is not None:
            self._history_pending = True
            if self.worker.kind == 'history' and self.worker.code != code:
                self.worker.stop()
            return
        self._history_pending = False
        self._start('history', code)

    def _show_history(self, code: str, points: tuple[HistoryPoint, ...]):
        if not points:
            self.chart.set_series([])
            self.chart_summary.setText(f'НБУ не повернув історію {code} за цей період.')
            return
        self.chart.set_series([float(point.uah_per_unit) for point in points],
                              labels=[point.effective_date.strftime('%d.%m.%Y') for point in points])
        change = (points[-1].uah_per_unit / points[0].uah_per_unit - 1) * 100
        self.chart_summary.setText(f'1 {code} = {_number(points[-1].uah_per_unit)} UAH'
                                   f' · {points[0].effective_date:%d.%m}–{points[-1].effective_date:%d.%m.%Y}'
                                   f' · зміна {change:+.2f}%')

    def _failed(self, message: str):
        if self.close_pending:
            return
        self._history_pending = False
        preserved = ' Попередні курси збережено.' if self.rates else ''
        self.status.setText(message + preserved)
        if self.worker is not None and self.worker.kind == 'history':
            self.chart_summary.setText('Історію не завантажено. Натисни «Оновити», щоб спробувати ще раз.')

    def _cancelled(self):
        if not self.close_pending:
            self.status.setText('Запит зупинено. Завантажені курси залишаються доступними.')

    def _finished(self):
        old = self.worker
        self.worker = None
        if old is not None:
            old.deleteLater()
        self.refresh_button.setEnabled(not self.close_pending)
        self.cancel_button.hide()
        if self.close_pending:
            self.shutdown_finished.emit()
            QTimer.singleShot(0, self.window().close)
        elif self._history_pending:
            self._history_pending = False
            QTimer.singleShot(0, lambda: self._history_changed(self.chart_currency.currentText()))

    def shutdown(self) -> bool:
        self.close_pending = True
        self._history_pending = False
        self.refresh_button.setEnabled(False)
        if self.worker is not None:
            self.worker.stop()
            return False
        return True

    def request_close(self) -> bool:
        return self.shutdown()

    def is_busy(self) -> bool:
        return self.worker is not None

    def closeEvent(self, event):
        if not self.shutdown():
            event.ignore()
            return
        super().closeEvent(event)
