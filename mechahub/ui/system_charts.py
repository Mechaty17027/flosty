"""Small live charts with independent, nonblocking system counter sampling."""
from __future__ import annotations

from collections import deque
import math
import time
from typing import Callable, Sequence

import psutil
from PySide6.QtCore import QEvent, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
    QToolTip, QVBoxLayout, QWidget,
)

from mechahub.core.system import format_bytes


MAX_SAMPLES = 120


class SystemLoadSampler:
    """Own CPU and I/O baselines, independent of other psutil consumers.

    CPU is calculated from cumulative cpu_times instead of cpu_percent's shared
    per-thread baseline. No call sleeps, scans processes or touches the disk.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, provider=psutil):
        self.clock = clock
        self.provider = provider
        self._previous: dict | None = None

    def _read(self) -> dict:
        data: dict = {'time': self.clock()}
        for key, read in (
            ('cpu', self.provider.cpu_times),
            ('memory', self.provider.virtual_memory),
            ('disk', self.provider.disk_io_counters),
            ('network', self.provider.net_io_counters),
        ):
            try:
                data[key] = read()
            except (psutil.Error, OSError, AttributeError):
                data[key] = None
        return data

    def prime(self) -> None:
        """Reset all baselines after a pause; never include hidden intervals."""
        self._previous = self._read()

    @staticmethod
    def _cpu_totals(counter) -> tuple[float, float]:
        total = sum(counter) - getattr(counter, 'guest', 0) - getattr(counter, 'guest_nice', 0)
        idle = counter.idle + getattr(counter, 'iowait', 0)
        return total, idle

    def snapshot(self) -> dict[str, float | None]:
        current = self._read()
        previous, self._previous = self._previous, current
        result: dict[str, float | None] = dict.fromkeys(
            ('cpu', 'memory', 'disk_read', 'disk_write', 'download', 'upload')
        )
        if current['memory'] is not None:
            result['memory'] = max(0.0, min(100.0, float(current['memory'].percent)))
        if previous is None:
            return result
        elapsed = current['time'] - previous['time']
        if elapsed <= 0:
            return result
        if current['cpu'] is not None and previous['cpu'] is not None:
            total, idle = self._cpu_totals(current['cpu'])
            old_total, old_idle = self._cpu_totals(previous['cpu'])
            delta = total - old_total
            if delta > 0:
                result['cpu'] = max(0.0, min(100.0, 100 * (delta - (idle - old_idle)) / delta))
        for kind, fields in (
            ('disk', (('disk_read', 'read_bytes'), ('disk_write', 'write_bytes'))),
            ('network', (('download', 'bytes_recv'), ('upload', 'bytes_sent'))),
        ):
            if current[kind] is None or previous[kind] is None:
                continue
            for key, field in fields:
                delta = getattr(current[kind], field) - getattr(previous[kind], field)
                # Counter resets are a gap, not fictitious negative traffic.
                if delta >= 0:
                    result[key] = delta / elapsed
        return result


class TimeSeriesChart(QWidget):
    """Reusable one/multiple series chart; None values represent missing samples."""

    def __init__(
        self, parent: QWidget | None = None, *,
        colors: Sequence[str] = ('#A98AF8',),
        fixed_range: tuple[float, float] | None = None,
        value_formatter: Callable[[float], str] | None = None,
        x_caption: str = '', capacity: int | None = None, zero_baseline: bool = False,
    ):
        super().__init__(parent)
        self.colors = tuple(QColor(color) for color in colors) or (QColor('#A98AF8'),)
        self.fixed_range = fixed_range
        self.value_formatter = value_formatter or (lambda value: f'{value:g}')
        self.x_caption = x_caption
        self.capacity = capacity
        self.zero_baseline = zero_baseline
        self.series: list[list[float | None]] = []
        self.labels: list[str] = []
        self.setMinimumSize(200, 155)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setAccessibleName('Графік ' + x_caption)

    def set_series(self, series, labels: Sequence[str] | None = None) -> None:
        values = list(series)
        nested = bool(values) and isinstance(values[0], (list, tuple, deque))
        rows = values if nested else [values]
        self.series = [
            [float(value) if value is not None and math.isfinite(float(value)) else None for value in row]
            for row in rows
        ]
        self.labels = list(labels or ())
        latest = [self.value_formatter(row[-1]) for row in self.series if row and row[-1] is not None]
        self.setAccessibleDescription('Поточні значення: ' + ', '.join(latest))
        self.update()

    def _range(self) -> tuple[float, float]:
        if self.fixed_range is not None:
            return self.fixed_range
        values = [value for row in self.series for value in row if value is not None]
        if not values:
            return 0.0, 1.0
        low, high = min(values), max(values)
        spread = high - low
        padding = spread * 0.12 if spread else max(abs(high) * 0.015, 0.01)
        return 0.0 if self.zero_baseline else max(0.0, low - padding), high + padding

    def _plot_rect(self) -> QRectF:
        low, high = self._range()
        widest = max(self.fontMetrics().horizontalAdvance(self.value_formatter(v)) for v in (low, high))
        left = min(max(45, widest + 12), 135)
        return QRectF(left, 12, max(1, self.width() - left - 14), max(1, self.height() - 43))

    def _point(self, index: int, count: int, value: float, rect: QRectF, bounds: tuple[float, float]) -> QPointF:
        low, high = bounds
        slots = max(count, self.capacity or count, 2)
        offset = slots - count if self.capacity else 0
        x = rect.left() + (index + offset) * rect.width() / (slots - 1)
        relative = max(0.0, min(1.0, (value - low) / max(high - low, 1e-12)))
        return QPointF(x, rect.bottom() - relative * rect.height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._plot_rect()
        low, high = self._range()
        painter.setPen(QPen(QColor('#343D53'), 1))
        for step in range(5):
            y = rect.top() + step * rect.height() / 4
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        painter.setPen(QColor('#A6B1CB'))
        painter.drawText(QRectF(0, rect.top() - 7, rect.left() - 9, 18), Qt.AlignmentFlag.AlignRight, self.value_formatter(high))
        painter.drawText(QRectF(0, rect.bottom() - 9, rect.left() - 9, 18), Qt.AlignmentFlag.AlignRight, self.value_formatter(low))
        if not any(value is not None for row in self.series for value in row):
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, 'Збираємо дані…')
        for number, values in enumerate(self.series):
            color = self.colors[number % len(self.colors)]
            path = QPainterPath()
            segment: list[QPointF] = []
            segments: list[list[QPointF]] = []
            for index, value in enumerate(values):
                if value is None:
                    if segment:
                        segments.append(segment)
                    segment = []
                    continue
                point = self._point(index, len(values), value, rect, (low, high))
                if not segment:
                    path.moveTo(point)
                else:
                    path.lineTo(point)
                segment.append(point)
            if segment:
                segments.append(segment)
            fill = QPainterPath()
            for points in segments:
                if len(points) < 2:
                    continue
                fill.moveTo(points[0].x(), rect.bottom())
                for point in points:
                    fill.lineTo(point)
                fill.lineTo(points[-1].x(), rect.bottom())
                fill.closeSubpath()
            gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            top = QColor(color)
            top.setAlpha(42)
            bottom = QColor(color)
            bottom.setAlpha(3)
            gradient.setColorAt(0, top)
            gradient.setColorAt(1, bottom)
            painter.fillPath(fill, gradient)
            painter.setPen(QPen(color, 2.1))
            painter.drawPath(path)
            for points in segments:
                if len(points) == 1:
                    painter.drawEllipse(points[0], 2, 2)
        painter.setPen(QColor('#A6B1CB'))
        first = self.labels[0] if self.labels else self.x_caption
        last = self.labels[-1] if self.labels else ''
        painter.drawText(QRectF(rect.left(), rect.bottom() + 9, rect.width() * .65, 18), Qt.AlignmentFlag.AlignLeft, first)
        painter.drawText(QRectF(rect.left(), rect.bottom() + 9, rect.width(), 18), Qt.AlignmentFlag.AlignRight, last)

    def mouseMoveEvent(self, event) -> None:
        rect = self._plot_rect()
        count = max((len(row) for row in self.series), default=0)
        if not count or not rect.contains(event.position()):
            QToolTip.hideText()
            return
        slots = max(count, self.capacity or count, 2)
        offset = slots - count if self.capacity else 0
        index = round((event.position().x() - rect.left()) / rect.width() * (slots - 1)) - offset
        if index < 0 or index >= count:
            QToolTip.hideText()
            return
        values = [self.value_formatter(row[index]) for row in self.series if index < len(row) and row[index] is not None]
        label = self.labels[index] if index < len(self.labels) else f'Точка {index + 1}'
        QToolTip.showText(event.globalPosition().toPoint(), label + '\n' + ' / '.join(values or ['Немає даних']), self)

    def leaveEvent(self, event) -> None:
        QToolTip.hideText()
        super().leaveEvent(event)


def _label(text: str, name: str = 'Muted') -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setObjectName(name)
    return label


class SystemChartsPage(QWidget):
    """Live CPU/RAM/I/O page that samples only while visible and not minimized."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.sampler = SystemLoadSampler()
        self.history = {key: deque(maxlen=MAX_SAMPLES) for key in (
            'cpu', 'memory', 'disk_read', 'disk_write', 'download', 'upload'
        )}
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._poll)
        self._watched_window: QWidget | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label('Навантаження ПК', 'Section'))
        layout.addWidget(_label(
            'CPU й оперативна пам’ять — у відсотках. Диск і мережа — фактичний '
            'обсяг переданих даних за секунду. Графіки оновлюються щосекунди.'
        ))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 4, 0, 4)
        grid.setSpacing(14)
        self.charts: dict[str, TimeSeriesChart] = {}
        self.values: dict[str, QLabel] = {}
        specs = (
            ('cpu', 'Процесор', 'purple', ('#BA9BFF',), ('Навантаження',), ('cpu',), True),
            ('memory', 'Оперативна пам’ять', 'cyan', ('#83E3E0',), ('Використано',), ('memory',), True),
            ('disk', 'Диск · читання та запис', 'amber', ('#F8C88F', '#EE92AD'), ('Читання', 'Запис'), ('disk_read', 'disk_write'), False),
            ('network', 'Мережа · вхід та вихід', 'green', ('#8FE5C4', '#88B8FA'), ('Вхід', 'Вихід'), ('download', 'upload'), False),
        )
        self._specs = specs
        for index, (key, title, accent, colors, legends, series_keys, percent) in enumerate(specs):
            frame = QFrame()
            frame.setObjectName('Card')
            frame.setProperty('accent', accent)
            card = QVBoxLayout(frame)
            card.setContentsMargins(16, 13, 16, 13)
            card.addWidget(_label(title, 'Section'))
            value = _label('Збираємо дані…')
            self.values[key] = value
            card.addWidget(value)
            formatter = (lambda v: f'{v:.0f}%') if percent else (lambda v: format_bytes(v) + '/с')
            chart = TimeSeriesChart(
                colors=colors, fixed_range=(0, 100) if percent else None,
                value_formatter=formatter, capacity=MAX_SAMPLES, zero_baseline=not percent,
            )
            chart.setAccessibleName(title + ' · останні 120 секунд')
            self.charts[key] = chart
            card.addWidget(chart, 1)
            legend = QHBoxLayout()
            for color, text in zip(colors, legends):
                item = _label('● ' + text)
                item.setStyleSheet(f'color: {color}; background: transparent;')
                legend.addWidget(item)
            legend.addStretch()
            card.addLayout(legend)
            grid.addWidget(frame, index // 2, index % 2)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        layout.addWidget(_label(
            'Показано до 120 секунд. Коли розділ приховано або вікно згорнуто, '
            'збір призупиняється; після повернення починається новий графік.'
        ))

    def _update_running(self) -> None:
        visible = self.isVisible() and not self.window().isMinimized()
        if visible and not self.timer.isActive():
            for values in self.history.values():
                values.clear()
            for chart in self.charts.values():
                chart.set_series([])
            for value in self.values.values():
                value.setText('Збираємо дані…')
            self.sampler.prime()
            self.timer.start()
        elif not visible:
            self.timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        window = self.window()
        if self._watched_window is not window:
            if self._watched_window is not None:
                self._watched_window.removeEventFilter(self)
            window.installEventFilter(self)
            self._watched_window = window
        self._update_running()

    def hideEvent(self, event) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if watched is self._watched_window and event.type() == QEvent.Type.WindowStateChange:
            self._update_running()
        return super().eventFilter(watched, event)

    def _poll(self) -> None:
        if not self.isVisible() or self.window().isMinimized():
            self.timer.stop()
            return
        snapshot = self.sampler.snapshot()
        for key, value in snapshot.items():
            self.history[key].append(value)
        count = len(self.history['cpu'])
        labels = [f'{count - 1 - index} с тому' for index in range(count)]
        if labels:
            labels[-1] = 'Зараз'
        for key, title, accent, colors, legends, series_keys, percent in self._specs:
            chart = self.charts[key]
            chart.set_series([self.history[name] for name in series_keys], labels=labels)
            parts = []
            for legend, name in zip(legends, series_keys):
                value = snapshot[name]
                formatted = 'недоступно' if value is None else chart.value_formatter(value)
                parts.append(f'{legend}: {formatted}')
            self.values[key].setText('  ·  '.join(parts))

    def shutdown(self) -> None:
        self.timer.stop()
