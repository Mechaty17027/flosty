"""Network diagnostics page: animated speedometer + bandwidth/ping/packet stats."""
from __future__ import annotations

from math import cos, sin, radians
import threading

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QVBoxLayout, QWidget,
)

from mechahub.core.speedtest import SpeedTestResult, TestCancelled, speed_test


def label(text: str, kind: str | None = None, *, wrap: bool = False):
    item = QLabel(text)
    if kind:
        item.setObjectName(kind)
    item.setWordWrap(wrap)
    return item


def panel(accent: str | None = None):
    widget = QFrame()
    widget.setObjectName('Card')
    if accent:
        widget.setProperty('accent', accent)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(8)
    return widget, layout


class Speedometer(QWidget):
    """Custom-drawn semicircle with moving needle, like a network speed-test dial."""
    def __init__(self):
        super().__init__()
        self.setMinimumSize(240, 205)
        self.setMaximumHeight(270)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.target_speed = 0.0
        self.display_speed = 0.0
        self.mode = 'idle'
        self.scale = 100.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(30)

    def set_speed(self, speed: float, stage: str = 'download'):
        self.mode = stage
        self.target_speed = max(0.0, float(speed))
        thresholds = (10,25,50,100,250,500,1000,2500,5000,10000,25000)
        if self.target_speed > self.scale * .93:
            self.scale = next((x for x in thresholds if x >= self.target_speed * 1.15),
                              max(25000, ((int(self.target_speed)//10000)+2)*10000))
        self.update()

    def set_mode(self, mode: str):
        self.mode = mode
        if mode in {'idle','ping'}:
            self.target_speed = 0
            self.scale = 100
        elif mode in {'download','upload'}:
            self.target_speed = 0
            self.display_speed = 0
            self.scale = 100
        self.update()

    def _animate(self):
        difference = self.target_speed - self.display_speed
        if abs(difference) < .08:
            if self.display_speed != self.target_speed:
                self.display_speed = self.target_speed
                self.update()
            return
        self.display_speed += difference * .19
        self.update()

    def _point(self, cx: float, cy: float, radius: float, ratio: float) -> QPointF:
        angle = radians(210 - 240 * ratio)
        return QPointF(cx + radius*cos(angle), cy - radius*sin(angle))

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()
        cx, cy = width/2, min(height*.61, height-90)
        radius = min(width*.39, height*.49)
        accent = QColor('#60A5FA') if self.mode != 'upload' else QColor('#A78BFA')
        # The arc is made of small vector segments, not a prerendered image.
        p.setPen(QPen(QColor('#283952'), 13, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for i in range(120):
            p.drawLine(self._point(cx,cy,radius,i/120), self._point(cx,cy,radius,(i+1)/120))
        fraction = min(1.0, self.display_speed/max(1,self.scale))
        for i in range(max(0,round(fraction*120))):
            col = accent.lighter(int(110 + 45*i/120))
            p.setPen(QPen(col, 13, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(self._point(cx,cy,radius,i/120), self._point(cx,cy,radius,(i+1)/120))
        p.setPen(QPen(QColor('#58718F'), 2))
        for i in range(0,13):
            fraction_tick = i/12
            start = self._point(cx,cy,radius-23,fraction_tick)
            end = self._point(cx,cy,radius-33 if i%3==0 else radius-29,fraction_tick)
            p.drawLine(start,end)
        p.setPen(QColor('#8AA5C5'))
        font = QFont('Segoe UI', 10)
        p.setFont(font)
        for index in (0,3,6,9,12):
            pos = self._point(cx,cy,radius-53,index/12)
            p.drawText(QRectF(pos.x()-30,pos.y()-12,60,24),Qt.AlignmentFlag.AlignCenter,str(round(self.scale*index/12)))

        needle = self._point(cx,cy,radius-56,fraction)
        p.setPen(QPen(QColor('#EAF2FF'),3,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx,cy),needle)
        p.setBrush(QBrush(accent))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx,cy),9,9)
        p.setPen(QColor('#F1F6FF'))
        value_font = QFont('Segoe UI', 27)
        value_font.setBold(True)
        p.setFont(value_font)
        p.drawText(QRectF(0,cy+14,width,48),Qt.AlignmentFlag.AlignHCenter|Qt.AlignmentFlag.AlignTop,
                   f'{self.display_speed:,.1f}')
        p.setPen(QColor('#A8BBD8'))
        p.setFont(QFont('Segoe UI', 11))
        p.drawText(QRectF(0,cy+61,width,26),Qt.AlignmentFlag.AlignCenter,'Мбіт/с')
        p.end()


class SpeedTestThread(QThread):
    stage_changed = Signal(str)
    speed_updated = Signal(str,float)
    measured = Signal(object)
    failed = Signal(str)
    stopped = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancel_event = threading.Event()

    def stop(self):
        self.cancel_event.set()

    def run(self):
        try:
            value = speed_test(self.stage_changed.emit, self.speed_updated.emit, self.cancel_event)
        except TestCancelled as exc:
            self.stopped.emit(exc.result)
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.measured.emit(value)


class NetworkPage(QWidget):
    shutdown_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker: SpeedTestThread | None = None
        self.close_pending = False
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0,0,0,0)
        outer.setSpacing(6)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setObjectName('NetworkTabs')
        outer.addWidget(self.tabs, 1)
        self.values: dict[str, QLabel] = {}
        self.metric_labels: dict[str, list[QLabel]] = {}

        speed_page = QWidget()
        self.speed_page = speed_page
        speed_layout = QVBoxLayout(speed_page)
        speed_layout.setContentsMargins(12, 12, 12, 10)
        speed_layout.setSpacing(8)
        speed_top = QHBoxLayout()
        self.status_title = label('Готово до тесту', 'AccentTitle')
        speed_top.addWidget(self.status_title)
        speed_top.addStretch()
        self.server_label = label('Cloudflare', 'Pill')
        speed_top.addWidget(self.server_label)
        speed_layout.addLayout(speed_top)

        # A short horizontal dashboard keeps the action visible in the main
        # window's smallest supported size instead of placing it below a scroll.
        readings = QHBoxLayout()
        readings.setSpacing(12)
        dial_box, dial_layout = panel('cyan')
        dial_layout.setContentsMargins(10, 10, 10, 10)
        self.gauge = Speedometer()
        dial_layout.addWidget(self.gauge)
        readings.addWidget(dial_box, 1)
        summary = QVBoxLayout()
        summary.setSpacing(8)
        for key, title, unit, accent in (
            ('download', '↓ Завантаження', 'Мбіт/с', 'cyan'),
            ('upload', '↑ Віддача', 'Мбіт/с', 'purple'),
            ('ping', 'Ping', 'мс', 'green'),
        ):
            summary.addWidget(self._metric_card(key, title, unit, accent, compact=True))
        readings.addLayout(summary, 1)
        speed_layout.addLayout(readings, 1)

        actions = QHBoxLayout()
        actions.setSpacing(12)
        self.description = label('Натисни «Розпочати». Перевірка: 15–35 с.', 'Muted', wrap=True)
        actions.addWidget(self.description, 1)
        self.start_button = QPushButton('▶  Розпочати SPEED TEST')
        self.start_button.setObjectName('Primary')
        self.start_button.setMinimumHeight(40)
        self.start_button.clicked.connect(self.toggle)
        actions.addWidget(self.start_button)
        speed_layout.addLayout(actions)
        speed_layout.addWidget(label('Тест використовує інтернет: до 64 МіБ ↓ та 24 МіБ ↑.', 'Muted', wrap=True))
        self.tabs.addTab(speed_page, 'Швидкість')

        quality_page, quality_layout = self._details_page()
        quality_header, quality_header_layout = panel('green')
        quality_header_layout.addWidget(label('Стабільність підключення', 'Section'))
        quality_header_layout.addWidget(label('Затримка, її коливання та втрати пакетів до 1.1.1.1.', 'Muted', wrap=True))
        quality_layout.addWidget(quality_header)
        quality_metrics = QHBoxLayout()
        quality_metrics.setSpacing(10)
        for key, title, unit, accent in (
            ('ping', 'Ping', 'мс · затримка', 'green'),
            ('jitter', 'Jitter', 'мс · коливання', 'purple'),
            ('loss', 'Втрати пакетів', '% · без відповіді', 'cyan'),
        ):
            quality_metrics.addWidget(self._metric_card(key, title, unit, accent), 1)
        quality_layout.addLayout(quality_metrics)
        ping_panel, ping_layout = panel()
        ping_layout.addWidget(label('Відповіді ICMP', 'Section'))
        self.icmp_label = label('Відправлені запити: —   •   Отримані відповіді: —   •   Втрачені: —', 'Muted', wrap=True)
        ping_layout.addWidget(self.icmp_label)
        ping_layout.addWidget(label('Втрата означає, що відповідь не отримана. Ping не розрізняє втрати в напрямку до сервера й назад.', 'Muted', wrap=True))
        quality_layout.addWidget(ping_panel)
        quality_layout.addWidget(label('Усі показники оновляться після тесту у вкладці «Швидкість».', 'Muted', wrap=True))
        quality_layout.addStretch()
        self.tabs.addTab(quality_page, 'Якість звʼязку')

        packets_page, packets_layout = self._details_page()
        packet_panel, packet_layout = panel('cyan')
        packet_layout.addWidget(label('Трафік адаптера', 'Section'))
        packet_layout.addWidget(label('Вхідні та вихідні пакети за час тесту. Лічильники враховують весь трафік комп’ютера.', 'Muted', wrap=True))
        packet_grid = QGridLayout()
        packet_grid.setHorizontalSpacing(20)
        packet_grid.setVerticalSpacing(8)
        self.packet_values = {}
        rows = [
            ('packets_sent','↑ Вихідні пакети'),
            ('packets_recv','↓ Вхідні пакети'),
            ('errout','↑ Помилки відправлення'),
            ('errin','↓ Помилки отримання'),
            ('dropout','↑ Відкинуті вихідні'),
            ('dropin','↓ Відкинуті вхідні'),
        ]
        for i,(key,title) in enumerate(rows):
            side = i%2
            line = i//2
            item = QHBoxLayout()
            item.addWidget(label(title, 'Muted', wrap=True))
            item.addStretch()
            value = label('—','PacketValue')
            item.addWidget(value)
            self.packet_values[key] = value
            packet_grid.addLayout(item,line,side)
        packet_layout.addLayout(packet_grid)
        self.interface_label = label('Адаптер: ще не визначено', 'Muted')
        packet_layout.addWidget(self.interface_label)
        packets_layout.addWidget(packet_panel)
        packets_layout.addWidget(label('Помилки та відкинуті пакети — показники адаптера; вони відрізняються від втрат ICMP у вкладці «Якість звʼязку».', 'Muted', wrap=True))
        packets_layout.addStretch()
        self.tabs.addTab(packets_page, 'Пакети')

        self.errors_label = label('', 'Muted',wrap=True)
        self.errors_label.setVisible(False)
        outer.addWidget(self.errors_label)

    @staticmethod
    def _details_page():
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        return scroll, layout

    def _metric_card(self, key: str, title: str, unit: str, accent: str, *, compact=False):
        frame, layout = panel(accent)
        result = label('—', 'Metric')
        result.setAlignment(Qt.AlignmentFlag.AlignRight if compact else Qt.AlignmentFlag.AlignLeft)
        if compact:
            layout.setContentsMargins(12, 8, 12, 8)
            row = QHBoxLayout()
            column = QVBoxLayout()
            column.setSpacing(2)
            column.addWidget(label(title, 'Eyebrow'))
            column.addWidget(label(unit, 'Muted'))
            row.addLayout(column)
            row.addStretch()
            row.addWidget(result)
            layout.addLayout(row)
        else:
            layout.addWidget(label(title, 'Eyebrow', wrap=True))
            layout.addWidget(result)
            layout.addWidget(label(unit, 'Muted', wrap=True))
        self.values.setdefault(key, result)
        self.metric_labels.setdefault(key, []).append(result)
        return frame

    def _set_metric(self, key: str, text: str, tooltip: str = ''):
        # Ping appears in both overview and details; keep both views consistent.
        for item in self.metric_labels[key]:
            item.setText(text)
            item.setToolTip(tooltip)

    def toggle(self):
        if self.close_pending:
            return
        if self.worker is not None:
            self.stop()
            return
        self._start()

    def _start(self):
        # A result signal can arrive just before QThread.finished. Do not replace
        # its owner until the finished slot has released the previous worker.
        if self.worker is not None or self.close_pending:
            return
        for key in self.values:
            self._set_metric(key, '—')
        for item in self.packet_values.values():
            item.setText('—')
        self.interface_label.setText('Адаптер: визначається…')
        self.icmp_label.setText('Виконуємо перевірку ICMP…')
        self.errors_label.clear()
        self.errors_label.setVisible(False)
        self.start_button.setText('■  Зупинити тест')
        self.start_button.setEnabled(True)
        self.status_title.setText('Вимірювання…')
        self.gauge.set_mode('ping')
        self.worker = SpeedTestThread(self)
        self.worker.stage_changed.connect(self._stage)
        self.worker.speed_updated.connect(self._speed)
        self.worker.measured.connect(self._results)
        self.worker.failed.connect(self._failed)
        self.worker.stopped.connect(self._cancelled)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def stop(self):
        if self.worker is not None:
            self.worker.stop()
            self.start_button.setText('Зупиняємо…')
            self.start_button.setEnabled(False)
            self.description.setText('Завершуємо поточний мережевий запит…')

    def _stage(self, stage: str):
        if self.worker is not None and self.worker.cancel_event.is_set():
            return
        if stage == 'ping':
            self.status_title.setText('Крок 1/3 · Ping та втрати')
            self.description.setText('Надсилаємо 10 ICMP-запитів до 1.1.1.1…')
            self.gauge.set_mode('ping')
        elif stage == 'download':
            self.status_title.setText('Крок 2/3 · DOWNLOAD')
            self.description.setText('Отримуємо тестові дані від Cloudflare…')
            self.gauge.set_mode('download')
        elif stage == 'upload':
            self.status_title.setText('Крок 3/3 · UPLOAD')
            self.description.setText('Передаємо тестові дані до Cloudflare…')
            self.gauge.set_mode('upload')
        elif stage == 'done':
            self.status_title.setText('Обробка результатів…')

    def _speed(self, stage: str, speed: float):
        if self.worker is not None and self.worker.cancel_event.is_set():
            return
        if stage in {'download','upload'} and speed >= 0:
            self.gauge.set_speed(speed,stage)
            self._set_metric(stage, f'{speed:.1f}')

    @staticmethod
    def _number(value, decimals=1):
        return '—' if value is None else f'{float(value):.{decimals}f}'

    def _results(self, result: SpeedTestResult):
        incomplete = any((result.error_download, result.error_upload, result.ping.get('error')))
        self.status_title.setText('Тест зупинено' if result.cancelled else
                                  'Тест завершено частково' if incomplete else 'Тест завершено')
        self.description.setText('Збережено заміри. ≈ — незавершена оцінка.'
                                 if result.cancelled or incomplete else
                                 'Наближена швидкість до Cloudflare.')
        for stage, speed, partial, error in (
            ('download', result.download_mbps, result.partial_download, result.error_download),
            ('upload', result.upload_mbps, result.partial_upload, result.error_upload),
        ):
            text = self._number(speed)
            self._set_metric(stage, '≈ '+text if speed is not None and (partial or error) else text,
                             'Швидкість за частиною переданих даних' if partial or error else '')
        self._set_metric('ping', self._number(result.ping.get('ping_ms')))
        self._set_metric('jitter', self._number(result.ping.get('jitter_ms')))
        self._set_metric('loss', self._number(result.ping.get('lost_percent')))
        for key,value in result.counters.items():
            if key in self.packet_values:
                self.packet_values[key].setText(f'{value:,}'.replace(',',' ') if value is not None else '—')
        self.interface_label.setText('Адаптер: ' + (result.interface or 'не визначений'))
        ping = result.ping
        recv = ping.get('received')
        sent = ping.get('sent')
        lost = sent-recv if isinstance(sent,int) and isinstance(recv,int) else None
        fmt = lambda value: '—' if value is None else str(value)
        self.icmp_label.setText(f'Відправлені запити: {fmt(sent)}   •   Отримані відповіді: {fmt(recv)}   •   Втрачені: {fmt(lost)}')
        errors = [f'{stage}: {message}' for stage, message in (
            ('Ping', ping.get('error')), ('Download', result.error_download), ('Upload', result.error_upload)
        ) if message]
        self.errors_label.setText('Зауваження: ' + ' | '.join(errors) if errors else '')
        self.errors_label.setVisible(bool(errors))
        traffic = f'Тестові дані: ↓ {result.downloaded_bytes / (1024*1024):.2f} МіБ • ↑ {result.uploaded_bytes / (1024*1024):.2f} МіБ'
        self.description.setText(self.description.text()+'\n'+traffic)
        if result.upload_mbps is not None:
            self.gauge.set_speed(result.upload_mbps,'upload')
        elif result.download_mbps is not None:
            self.gauge.set_speed(result.download_mbps,'download')
        else:
            self.gauge.set_mode('idle')

    def _failed(self, message: str):
        self.status_title.setText('Сталася помилка')
        self.description.setText('Перевір підключення до інтернету та спробуй знову.')
        self.errors_label.setText('Помилка: '+message)
        self.errors_label.setVisible(True)
        for stage in ('download', 'upload'):
            self._set_metric(stage, '—')
        self.gauge.set_mode('idle')

    def _cancelled(self, result: SpeedTestResult | None = None):
        if result is not None:
            self._results(result)
            self.gauge.set_mode('idle')
            return
        self.status_title.setText('Тест зупинено')
        self.description.setText('Запусти ще раз, коли буде зручно.')
        for stage in ('download', 'upload'):
            self._set_metric(stage, '—')
        self.icmp_label.setText('Перевірку ICMP зупинено. Кількість запитів невідома.')
        self.gauge.set_mode('idle')

    def _worker_finished(self):
        self.start_button.setEnabled(not self.close_pending)
        self.start_button.setText('▶  Повторити SPEED TEST')
        old = self.worker
        self.worker = None
        if old:
            old.deleteLater()
        if self.close_pending:
            self.gauge.timer.stop()
            self.shutdown_finished.emit()
            QTimer.singleShot(0, self.window().close)

    def shutdown(self) -> bool:
        """Cancel without blocking the UI; close again after worker cleanup.

        Return True only once no worker is owned, including queued finished signals.
        Call from a host closeEvent and ignore that event when this returns False.
        """
        self.close_pending = True
        if self.worker is not None:
            self.stop()
            return False
        self.gauge.timer.stop()
        return True

    def request_stop_on_close(self) -> bool:
        return self.shutdown()

    def closeEvent(self, event):
        if not self.shutdown():
            event.ignore()
            return
        super().closeEvent(event)
