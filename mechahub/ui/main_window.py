"""Main MechaHub UI. No destructive actions without explicit confirmation."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import time

from psutil import Error as PsutilError

from PySide6.QtCore import QSignalBlocker, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QStackedWidget,
    QTableWidget, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from mechahub import __version__
from mechahub.core.apps import installed_programs
from mechahub.core.launch import open_path, system_action
from mechahub.core.organizer import MovePlan, apply_organization, plan_organization
from mechahub.core.settings import config_file, read_shortcuts, write_shortcuts
from mechahub.core.system import SystemMonitor, format_bytes, memory_heavy_processes
from mechahub.ui.styles import STYLESHEET
from mechahub.ui.network_page import NetworkPage
from mechahub.ui.icons import app_icon, icon
from mechahub.ui.updates_dialog import UpdatesDialog
from mechahub.ui.internet_page import InternetPage
from mechahub.ui.study_page import StudyPage
from mechahub.ui.currency_page import CurrencyPage
from mechahub.ui.system_charts import SystemChartsPage
from mechahub.ui.cash_registers_page import CashRegistersPage
from mechahub.ui.programming_page import ProgrammingPage
from mechahub.core.updates import UpdateError
from mechahub.core.updates_install import (
    HELPER_READY_SECONDS, cancel_installation, discard_prepared,
    installation_ready, prepare_installation, start_installation,
)


class UpdatePreparationThread(QThread):
    received = Signal(object)
    failed = Signal(str)

    def __init__(self, manifest, artifact, parent=None):
        super().__init__(parent)
        self.manifest = manifest
        self.artifact = artifact

    def run(self):
        try:
            self.received.emit(prepare_installation(self.manifest, self.artifact))
        except Exception as error:
            self.failed.emit(str(error))


class ProgramScanThread(QThread):
    programs_found = Signal(list)
    failed = Signal(str)

    def run(self) -> None:
        try:
            self.programs_found.emit(installed_programs())
        except Exception as error:
            self.failed.emit(str(error))


class ProgramPickerDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('Додати програму')
        self.resize(920, 620)
        self.setModal(True)
        self.selected_app: dict[str, str] | None = None
        self.programs: list[dict[str, str]] = []
        self._pending_result: int | None = None
        self._scan_error = ''
        self._scan_thread: ProgramScanThread | None = None
        self._build_ui()
        self._scan_thread = ProgramScanThread(self)
        self._scan_thread.programs_found.connect(self._programs_loaded)
        self._scan_thread.failed.connect(self._scan_failed)
        self._scan_thread.finished.connect(self._scan_finished)
        self.count_label.setText('Шукаємо програми… Можна користуватися пошуком або закрити вікно.')
        self.accept_button.setEnabled(False)
        self._scan_thread.start()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        layout.addWidget(text_label('Додати програму в MechaHub', 'AccentTitle'))
        layout.addWidget(text_label(
            'Тут показані знайдені програми та ярлики з меню Пуск і реєстру Windows. '
            'Обери потрібну програму й натисни «Додати вибрану».',
            'Muted',
            True,
        ))
        self.search = QLineEdit()
        self.search.setPlaceholderText('Пошук за назвою або шляхом…')
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['Програма', 'Джерело', 'Шлях'])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.cellDoubleClicked.connect(self._accept_selected)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.count_label = text_label('', 'Muted')
        footer.addWidget(self.count_label)
        footer.addStretch()
        cancel = button('Скасувати')
        cancel.clicked.connect(self.reject)
        self.accept_button = button('Додати вибрану', True)
        self.accept_button.clicked.connect(self._accept_selected)
        self.table.itemSelectionChanged.connect(self._update_accept_button)
        footer.addWidget(cancel)
        footer.addWidget(self.accept_button)
        layout.addLayout(footer)

    def _programs_loaded(self, programs: list[dict[str, str]]) -> None:
        self.programs = programs
        self._apply_filter()

    def _scan_failed(self, message: str) -> None:
        self._scan_error = message
        self.count_label.setText('Не вдалося знайти програми: ' + message)

    def _scan_finished(self) -> None:
        worker = self._scan_thread
        self._scan_thread = None
        if worker:
            worker.deleteLater()
        if self._pending_result is not None:
            super().done(self._pending_result)
        else:
            self._apply_filter()

    def _update_accept_button(self) -> None:
        self.accept_button.setEnabled(
            self._scan_thread is None and self._pending_result is None
            and self.table.currentRow() >= 0
        )

    def done(self, result: int) -> None:
        # Keep the dialog and its QThread alive until registry scanning finishes.
        if self._scan_thread is not None:
            self._pending_result = result
            self.accept_button.setEnabled(False)
            self.count_label.setText('Завершуємо пошук і закриваємо вікно…')
            return
        super().done(result)

    def _render(self, rows: list[dict[str, str]]) -> None:
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            self.table.setItem(row, 0, QTableWidgetItem(app_icon(item['path']), item['name']))
            self.table.setItem(row, 1, QTableWidgetItem(item.get('source', '—')))
            self.table.setItem(row, 2, QTableWidgetItem(item['path']))
        if self._pending_result is not None:
            self.count_label.setText('Завершуємо пошук і закриваємо вікно…')
        elif self._scan_error:
            self.count_label.setText('Не вдалося знайти програми: ' + self._scan_error)
        elif self._scan_thread is not None:
            self.count_label.setText('Шукаємо програми…')
        elif not self.programs:
            self.count_label.setText('Програм не знайдено. Додай файл вручну у вкладці «Мої програми».')
        else:
            self.count_label.setText(f'Показано: {len(rows)} із {len(self.programs)}')
        if rows:
            self.table.selectRow(0)
        self._update_accept_button()

    def _apply_filter(self) -> None:
        query = self.search.text().strip().casefold()
        if not query:
            filtered = self.programs
        else:
            filtered = [
                item for item in self.programs
                if query in item['name'].casefold() or query in item['path'].casefold() or query in item.get('source', '').casefold()
            ]
        self._render(filtered)

    def _accept_selected(self) -> None:
        if self._scan_thread is not None or self._pending_result is not None:
            return
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, 'Нічого не вибрано', 'Спочатку вибери програму зі списку.')
            return
        self.selected_app = {
            'name': self.table.item(row, 0).text(),
            'source': self.table.item(row, 1).text(),
            'path': self.table.item(row, 2).text(),
        }
        self.accept()


def text_label(text: str, kind: str | None = None, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if kind:
        label.setObjectName(kind)
    label.setWordWrap(wrap)
    return label


def button(text: str, primary: bool = False) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        btn.setObjectName('Primary')
    return btn


def card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName('Card')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 19, 20, 19)
    layout.setSpacing(10)
    return frame, layout


def stretch_space(layout: QVBoxLayout | QHBoxLayout) -> None:
    layout.addStretch(1)


def icon_button(image, name: str, callback, accent: str = 'cyan') -> QToolButton:
    control = QToolButton()
    control.setObjectName('IconLaunch')
    control.setProperty('accent', accent)
    control.setIcon(image)
    control.setIconSize(QSize(36, 36))
    control.setFixedSize(72, 72)
    control.setToolTip(name)
    control.setAccessibleName(name)
    control.setCursor(Qt.CursorShape.PointingHandCursor)
    control.clicked.connect(callback)
    return control


class MetricTile(QFrame):
    def __init__(self, title: str, accent: str):
        super().__init__()
        self.setObjectName('Card')
        self.setMinimumHeight(138)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(9)
        layout.addWidget(text_label(title.upper(), 'Eyebrow'))
        self.value = text_label('—', 'Metric')
        layout.addWidget(self.value)
        self.detail = text_label('Отримання даних…', 'Muted', True)
        layout.addWidget(self.detail)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setStyleSheet(f'QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}')
        layout.addWidget(self.bar)

    def update_tile(self, value: str, detail: str, percent: float) -> None:
        self.value.setText(value)
        self.detail.setText(detail)
        self.bar.setValue(max(0, min(100, round(percent))))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('MechaHub · Твій ПК, твої правила')
        self.resize(1240, 820)
        self.setMinimumSize(980, 690)
        self.setStyleSheet(STYLESHEET)
        self.monitor = SystemMonitor()
        self.shortcuts = read_shortcuts()
        self.folder = Path.home() / 'Downloads'
        if not self.folder.is_dir():
            self.folder = Path.home()
        self.current_plan: list[MovePlan] = []
        self.nav_buttons: list[QPushButton] = []
        self._ticks = 0
        self._program_picker: ProgramPickerDialog | None = None
        self._close_requested = False
        self._updates_dialog: UpdatesDialog | None = None
        self._automatic_updates_started = False
        self._update_prepare_worker = None
        self._pending_update = None
        self._update_helper_process = None
        self._update_helper_ready = False
        self._update_helper_deadline = 0.0
        self._handoff_timer = QTimer(self)
        self._handoff_timer.setInterval(100)
        self._handoff_timer.timeout.connect(self._poll_update_handoff)
        self._build_ui()
        self._render_shortcuts()
        self._update_stats()
        self._update_processes()
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start(1600)
        self.update_check_timer = QTimer(self)
        self.update_check_timer.setInterval(6 * 60 * 60 * 1000)
        self.update_check_timer.timeout.connect(self._begin_automatic_updates)
        self.update_check_timer.start()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main = QHBoxLayout(central)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName('Sidebar')
        sidebar.setFixedWidth(216)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(17, 27, 17, 23)
        sl.setSpacing(10)
        brand = text_label('MechaHub', 'Brand')
        sl.addWidget(brand)
        sl.addWidget(text_label('ТВОЄ РОБОЧЕ МІСЦЕ', 'Eyebrow'))
        sl.addSpacing(24)
        sl.addWidget(text_label('РОБОЧИЙ ПРОСТІР', 'Eyebrow'))
        navigation = [('Огляд', 'home'), ('Програми', 'apps'), ('Файли', 'files'),
                      ('Мережа', 'network'), ('Інтернет', 'globe'), ('Навчання', 'study'),
                      ('Валюти', 'currency'), ('Графіки ПК', 'chart'),
                      ('Касові апарати', 'cash'), ('Програмування', 'code'), ('Про проєкт', 'info')]
        nav_holder = QWidget()
        nav_layout = QVBoxLayout(nav_holder)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(5)
        for name, symbol in navigation:
            nav = button(name)
            nav.setIcon(icon(symbol))
            nav.setIconSize(QSize(20, 20))
            nav.setObjectName('Nav')
            nav.setProperty('active', False)
            nav.clicked.connect(lambda _checked=False, idx=len(self.nav_buttons): self._navigate(idx))
            nav.setMinimumHeight(43)
            nav_layout.addWidget(nav)
            self.nav_buttons.append(nav)
        nav_layout.addStretch()
        nav_area = self._scrollable(nav_holder)
        self.navigation_area = nav_area
        nav_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sl.addWidget(nav_area, 1)
        self.updates_button = button('Оновлення')
        self.updates_button.setObjectName('Nav')
        self.updates_button.setIcon(icon('updates', '#A6A0FF'))
        self.updates_button.clicked.connect(self._show_updates)
        sl.addWidget(self.updates_button)
        version, vl = card()
        vl.addWidget(text_label(f'MECHAHUB / {__version__}', 'Eyebrow'))
        vl.addWidget(text_label('Все потрібне під рукою', 'Muted', True))
        sl.addWidget(version)
        main.addWidget(sidebar)

        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(26, 23, 26, 24)
        col.setSpacing(18)
        top = QHBoxLayout()
        heading = QVBoxLayout()
        self.page_title = text_label('Огляд', 'Headline')
        self.page_subtitle = text_label('Вся важлива інформація про комп’ютер в одному місці.', 'Muted')
        heading.addWidget(self.page_title)
        self.page_subtitle.setWordWrap(True)
        heading.addWidget(self.page_subtitle)
        top.addLayout(heading, 1)
        status = text_label('●  Готово до роботи', 'Pill')
        top.addWidget(status, alignment=Qt.AlignmentFlag.AlignVCenter)
        col.addLayout(top)
        self.update_banner = QFrame()
        self.update_banner.setObjectName('Card')
        self.update_banner.setProperty('accent', 'green')
        banner_layout = QHBoxLayout(self.update_banner)
        banner_layout.setContentsMargins(15, 12, 15, 12)
        self.update_banner_label = text_label('', 'HeroText', True)
        banner_layout.addWidget(self.update_banner_label, 1)
        self.update_offer_button = button('Оновити', True)
        self.update_offer_button.clicked.connect(self._show_update_offer)
        banner_layout.addWidget(self.update_offer_button)
        self.update_later_button = button('Пізніше')
        self.update_later_button.clicked.connect(self.update_banner.hide)
        banner_layout.addWidget(self.update_later_button)
        self.update_banner.hide()
        col.addWidget(self.update_banner)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._dashboard_page())
        self.stack.addWidget(self._shortcuts_page())
        self.stack.addWidget(self._organizer_page())
        self.network_page = NetworkPage(self)
        self.stack.addWidget(self.network_page)
        self.internet_page = InternetPage(self)
        self.stack.addWidget(self.internet_page)
        self.study_page = StudyPage(self)
        self.study_page.open_source.connect(self._open_study_source)
        self.study_page.ready_to_close.connect(self._retry_close)
        self.stack.addWidget(self.study_page)
        self.currency_page = CurrencyPage(self)
        self.currency_page.shutdown_finished.connect(self._retry_close)
        self.stack.addWidget(self.currency_page)
        self.system_charts = SystemChartsPage(self)
        self.stack.addWidget(self.system_charts)
        self.cash_registers_page = CashRegistersPage(self)
        self.cash_registers_page.open_source.connect(self._open_study_source)
        self.stack.addWidget(self.cash_registers_page)
        self.programming_page = ProgrammingPage(self)
        self.programming_page.ready_to_close.connect(self._retry_close)
        self.stack.addWidget(self.programming_page)
        self.stack.addWidget(self._about_page())
        col.addWidget(self.stack, 1)
        main.addWidget(content, 1)
        self._navigate(0)

    def _navigate(self, index: int):
        titles = [
            ('Огляд', 'Швидкий запуск і коротко про стан комп’ютера.'),
            ('Програми', 'Твої застосунки та папки у зручній сітці іконок.'),
            ('Файли', 'Порядок у файлах без видалення і несподіваних змін.'),
            ('Мережа', 'Speed test, ping, jitter, packet loss і статистика пакетів.'),
            ('Інтернет', 'Сайти, пошук і вкладки прямо всередині MechaHub.'),
            ('Навчання та конспекти', 'Підготовка до НМТ, захищені записи та безоплатний локальний ШІ.'),
            ('Валюти', 'Офіційні курси НБУ, графіки та перерахунок через гривню.'),
            ('Графіки ПК', 'Навантаження процесора й пам’яті та швидкість диска й мережі.'),
            ('Касові апарати', 'Каталог моделей, офіційні джерела та власні сервісні кейси.'),
            ('Програмування', 'Власні проєкти, Python, C++ та налаштування додаткових мов.'),
            ('Про проєкт', 'Наша дорожня карта розвитку MechaHub.'),
        ]
        if self.stack.currentWidget() is self.study_page and index != 5:
            if not self.study_page.can_leave():
                return
        if self.stack.currentWidget() is self.programming_page and index != 9:
            if not self.programming_page.can_leave():
                return
        self.stack.setCurrentIndex(index)
        if index == 4:
            self.internet_page.activate()
        self.page_title.setText(titles[index][0])
        self.page_subtitle.setText(titles[index][1])
        for i, nav in enumerate(self.nav_buttons):
            nav.setProperty('active', i == index)
            nav.style().unpolish(nav)
            nav.style().polish(nav)
            if i == index:
                self.navigation_area.ensureWidgetVisible(nav, 0, 8)

    def _open_study_source(self, url: str):
        self._navigate(4)
        if self.stack.currentWidget() is self.internet_page:
            self.internet_page.open_url(url)

    @staticmethod
    def _scrollable(content: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(content)
        return area

    def _dashboard_page(self) -> QWidget:
        self.dashboard_tabs = QTabWidget()
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 14, 6, 6)
        root.setSpacing(16)

        hero = QFrame()
        hero.setObjectName('Hero')
        self.dashboard_hero = hero
        h = QVBoxLayout(hero)
        h.setContentsMargins(24, 19, 24, 19)
        h.setSpacing(8)
        h.addWidget(text_label('MECHA / WORKSPACE', 'Eyebrow'))
        h.addWidget(text_label('Почни з головного.', 'HeroTitle'))
        h.addWidget(text_label('Запускай улюблене. Решта — у своїх розділах.', 'HeroText', True))
        root.addWidget(hero)

        stats_head = QHBoxLayout()
        stats_head.addWidget(text_label('Стан комп’ютера', 'Section'))
        stretch_space(stats_head)
        self.last_update = text_label('Оновлення кожні 1,6 с', 'Muted')
        stats_head.addWidget(self.last_update)
        root.addLayout(stats_head)
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(13)
        metrics.setVerticalSpacing(13)
        self.cpu = MetricTile('Процесор', '#9F8FFF')
        self.cpu.setProperty('accent', 'purple')
        self.mem = MetricTile('Пам’ять', '#50DAD0')
        self.mem.setProperty('accent', 'cyan')
        self.disk = MetricTile('Диск', '#F4BB75')
        self.disk.setProperty('accent', 'amber')
        self.net = MetricTile('Мережа • завантаження', '#EFBF72')
        self.net.bar.hide()
        for r, c, tile in [(0, 0, self.cpu), (0, 1, self.mem), (0, 2, self.disk)]:
            metrics.addWidget(tile, r, c)
        root.addLayout(metrics)

        launch_card, launch = card()
        launch.setSpacing(12)
        launch_head = QHBoxLayout()
        launch_head.addWidget(text_label('Швидкий запуск', 'Section'))
        launch_head.addStretch()
        all_programs = button('Усі програми')
        all_programs.setObjectName('Subtle')
        all_programs.clicked.connect(lambda: self._navigate(1))
        launch_head.addWidget(all_programs)
        launch.addLayout(launch_head)
        self.quick_app_layout = QHBoxLayout()
        self.quick_app_layout.setSpacing(10)
        launch.addLayout(self.quick_app_layout)
        root.addWidget(launch_card)

        tools = QHBoxLayout()
        tools.setSpacing(12)
        tools.addWidget(text_label('Інструменти', 'Muted'))
        windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        for title, action, image_path, fallback, accent in (
            ('Провідник', 'explorer', windows / 'explorer.exe', 'folder', 'amber'),
            ('Диспетчер задач', 'taskmgr', windows / 'System32' / 'Taskmgr.exe', 'taskmgr', 'cyan'),
            ('Налаштування Windows', 'settings', windows / 'ImmersiveControlPanel' / 'SystemSettings.exe', 'settings', 'purple'),
        ):
            image = app_icon(str(image_path)) if image_path.exists() else icon(fallback)
            tools.addWidget(icon_button(image, title, lambda _checked=False, a=action: self._open_system(a), accent))
        tools.addWidget(icon_button(icon('globe', '#5ADBC9'), 'Відкрити інтернет', lambda: self._navigate(4), 'green'))
        tools.addStretch()
        root.addLayout(tools)
        root.addStretch()
        self.dashboard_tabs.addTab(self._scrollable(page), 'Огляд')

        system = QWidget()
        system_root = QVBoxLayout(system)
        system_root.setContentsMargins(0, 14, 6, 6)
        system_root.setSpacing(16)

        diagnostics = QHBoxLayout()
        diagnostics.setSpacing(13)
        info_frame, info = card()
        info.addWidget(text_label('Інформація про систему', 'Section'))
        self.info_os = text_label('ОС: …', 'Muted')
        self.info_cores = text_label('Ядра / потоки: …', 'Muted')
        self.info_uptime = text_label('Час роботи ПК: …', 'Muted')
        for lbl in (self.info_os, self.info_cores, self.info_uptime):
            lbl.setWordWrap(True)
            info.addWidget(lbl)
        info.addStretch()
        diagnostics.addWidget(info_frame, 1)

        diagnostics.addWidget(self.net, 1)
        system_root.addLayout(diagnostics)

        processes_frame, processes = card()
        processes.addWidget(text_label('Найбільше використовують RAM', 'Section'))
        self.process_rows: list[QLabel] = []
        for _ in range(5):
            row = text_label('—', 'Muted')
            row.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            processes.addWidget(row)
            self.process_rows.append(row)
        system_root.addWidget(processes_frame)
        diagnostic_link = button('Відкрити діагностику мережі')
        diagnostic_link.setIcon(icon('network'))
        diagnostic_link.clicked.connect(lambda: self._navigate(3))
        system_root.addWidget(diagnostic_link, alignment=Qt.AlignmentFlag.AlignLeft)
        system_root.addStretch()
        self.dashboard_tabs.addTab(self._scrollable(system), 'Система та процеси')
        return self.dashboard_tabs

    def _render_quick_launch(self):
        while self.quick_app_layout.count():
            child = self.quick_app_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        for index, entry in enumerate(self.shortcuts[:6]):
            control = icon_button(
                app_icon(entry['path']), entry['name'],
                lambda _checked=False, i=index: self._launch_entry(i),
                ('purple', 'cyan', 'amber', 'green')[index % 4],
            )
            control.setToolTip(entry['name'] + '\n' + entry['path'])
            self.quick_app_layout.addWidget(control)
        if not self.shortcuts:
            self.quick_app_layout.addWidget(text_label('Додай програми у вкладці «Програми», щоб вони з’явилися тут.', 'Muted', True))
        self.quick_app_layout.addStretch()

    def _shortcuts_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        search_line = QHBoxLayout()
        self.shortcut_search = QLineEdit()
        self.shortcut_search.setPlaceholderText('Пошук у моїх програмах за назвою або шляхом…')
        self.shortcut_search.setClearButtonEnabled(True)
        self.shortcut_search.textChanged.connect(self._render_shortcuts)
        search_line.addWidget(self.shortcut_search, 1)
        self.shortcut_count = text_label('', 'Muted')
        search_line.addWidget(self.shortcut_count)
        add_control = button('Додати', True)
        add_control.setIcon(icon('plus', '#FFFFFF'))
        add_menu = QMenu(add_control)
        add_menu.addAction('Знайти встановлену програму', self._add_program)
        add_menu.addAction('Вибрати файл вручну', self._add_file)
        add_menu.addAction('Додати папку', self._add_folder)
        add_control.setMenu(add_menu)
        search_line.addWidget(add_control)
        layout.addLayout(search_line)

        self.program_tabs = QTabWidget()
        launch_page = QWidget()
        launch_layout = QVBoxLayout(launch_page)
        launch_layout.setContentsMargins(0, 12, 0, 0)
        self.shortcut_grid = QListWidget()
        self.shortcut_grid.setObjectName('AppGrid')
        self.shortcut_grid.setViewMode(QListView.ViewMode.IconMode)
        self.shortcut_grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.shortcut_grid.setMovement(QListView.Movement.Static)
        self.shortcut_grid.setWrapping(True)
        self.shortcut_grid.setIconSize(QSize(52, 52))
        self.shortcut_grid.setGridSize(QSize(108, 104))
        self.shortcut_grid.setSpacing(8)
        self.shortcut_grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.shortcut_grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.shortcut_grid.currentRowChanged.connect(self._select_grid_row)
        self.shortcut_grid.itemDoubleClicked.connect(lambda _item: self._run_shortcut(self.shortcut_grid.currentRow()))
        launch_layout.addWidget(self.shortcut_grid, 1)
        self.shortcut_empty = text_label('Додай свою першу програму через кнопку «Додати».', 'Muted', True)
        launch_layout.addWidget(self.shortcut_empty)
        self.program_tabs.addTab(launch_page, 'Запуск')

        manage_page = QWidget()
        manage_layout = QVBoxLayout(manage_page)
        manage_layout.setContentsMargins(0, 12, 0, 0)

        self.shortcut_table = QTableWidget(0, 2)
        self.shortcut_table.setHorizontalHeaderLabels(['Назва', 'Шлях до програми або папки'])
        self.shortcut_table.setAlternatingRowColors(True)
        self.shortcut_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.shortcut_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.shortcut_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.shortcut_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.shortcut_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.shortcut_table.verticalHeader().hide()
        self.shortcut_table.cellDoubleClicked.connect(lambda row, _col: self._run_shortcut(row))
        self.shortcut_table.setIconSize(QSize(24, 24))
        manage_layout.addWidget(self.shortcut_table, 1)
        manage_layout.addWidget(text_label('Прибирання ярлика зі списку не видаляє сам файл.', 'Muted', True))
        self.program_tabs.addTab(manage_page, 'Керування списком')
        layout.addWidget(self.program_tabs, 1)

        detail, detail_layout = card()
        self.selected_shortcut_name = text_label('Вибери програму', 'Section')
        self.selected_shortcut_path = text_label('Назва й шлях вибраної іконки з’являться тут.', 'Muted', True)
        self.selected_shortcut_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.selected_shortcut_name)
        detail_layout.addWidget(self.selected_shortcut_path)

        actions = QHBoxLayout()
        self.launch_button = button('Запустити', True)
        self.launch_button.setIcon(icon('arrow', '#FFFFFF'))
        self.launch_button.clicked.connect(lambda: self._run_shortcut(self.shortcut_table.currentRow()))
        self.rename_button = button('Перейменувати')
        self.rename_button.clicked.connect(self._rename_shortcut)
        self.remove_button = button('Прибрати')
        self.remove_button.clicked.connect(self._remove_shortcut)
        actions.addWidget(self.launch_button)
        actions.addWidget(self.rename_button)
        actions.addWidget(self.remove_button)
        actions.addStretch()
        detail_layout.addLayout(actions)
        layout.addWidget(detail)
        self.shortcut_table.itemSelectionChanged.connect(self._update_shortcut_actions)
        return page

    def _select_grid_row(self, row: int):
        if row >= 0:
            self.shortcut_table.selectRow(row)

    def _shortcut_index(self, row: int | None = None) -> int | None:
        row = self.shortcut_table.currentRow() if row is None else row
        item = self.shortcut_table.item(row, 0) if row >= 0 else None
        index = item.data(Qt.ItemDataRole.UserRole) if item else None
        return index if isinstance(index, int) and 0 <= index < len(self.shortcuts) else None

    def _update_shortcut_actions(self) -> None:
        selected = self._shortcut_index() is not None
        for control in (self.launch_button, self.rename_button, self.remove_button):
            control.setEnabled(selected)
        index = self._shortcut_index()
        blocker = QSignalBlocker(self.shortcut_grid)
        self.shortcut_grid.setCurrentRow(self.shortcut_table.currentRow() if selected else -1)
        del blocker
        if index is not None:
            self.selected_shortcut_name.setText(self.shortcuts[index]['name'])
            self.selected_shortcut_path.setText(self.shortcuts[index]['path'])
        else:
            self.selected_shortcut_name.setText('Нічого не вибрано')
            self.selected_shortcut_path.setText('Обери іконку програми або додай новий ярлик.')

    def _render_shortcuts(self, _query: str = '', selected_path: str | None = None):
        previous = self._shortcut_index()
        if selected_path is None and previous is not None:
            selected_path = self.shortcuts[previous]['path']
        query = self.shortcut_search.text().strip().casefold()
        rows = [(index, entry) for index, entry in enumerate(self.shortcuts)
                if query in entry['name'].casefold() or query in entry['path'].casefold()]
        self.shortcut_table.setRowCount(0)
        self.shortcut_table.setRowCount(len(rows))
        grid_blocker = QSignalBlocker(self.shortcut_grid)
        self.shortcut_grid.clear()
        selected_row = 0
        for row, (index, entry) in enumerate(rows):
            image = app_icon(entry['path'])
            name = QTableWidgetItem(image, entry['name'])
            name.setData(Qt.ItemDataRole.UserRole, index)
            self.shortcut_table.setItem(row, 0, name)
            self.shortcut_table.setItem(row, 1, QTableWidgetItem(entry['path']))
            tile = QListWidgetItem(image, '')
            tile.setData(Qt.ItemDataRole.UserRole, index)
            tile.setData(Qt.ItemDataRole.AccessibleTextRole, entry['name'])
            tile.setData(Qt.ItemDataRole.AccessibleDescriptionRole, entry['path'])
            tile.setToolTip(entry['name'] + '\n' + entry['path'])
            tile.setSizeHint(QSize(100, 96))
            self.shortcut_grid.addItem(tile)
            for column in (0, 1):
                self.shortcut_table.item(row, column).setToolTip(entry['path'])
            if selected_path == entry['path']:
                selected_row = row
        if rows:
            self.shortcut_table.selectRow(selected_row)
        del grid_blocker
        self.shortcut_empty.setVisible(not rows)
        self.shortcut_empty.setText('За цим запитом нічого не знайдено.' if self.shortcuts else 'Додай свою першу програму через кнопку «Додати».')
        self.shortcut_count.setText(f'{len(rows)} із {len(self.shortcuts)}')
        self._update_shortcut_actions()
        self._render_quick_launch()

    def _add_program(self):
        dialog = ProgramPickerDialog(self)
        self._program_picker = dialog
        if dialog._scan_thread is not None:
            dialog._scan_thread.finished.connect(self._retry_close)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_app:
            item = dialog.selected_app
            self._add_shortcut(Path(item['path']), name=item['name'], ask_name=False)
        self._program_picker = None
        dialog.deleteLater()

    def _add_file(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, 'Вибери програму або файл', str(Path.home()),
            'Програми та ярлики (*.exe *.lnk *.bat *.cmd *.url);;Усі файли (*)',
        )
        if path:
            self._add_shortcut(Path(path))

    def _add_folder(self):
        path = QFileDialog.getExistingDirectory(self, 'Вибери папку', str(Path.home()))
        if path:
            self._add_shortcut(Path(path))

    def _add_shortcut(self, path: Path, name: str | None = None, ask_name: bool = True):
        item_name = (name or path.stem or path.name or str(path)).strip()
        if ask_name:
            item_name, ok = QInputDialog.getText(self, 'Назва ярлика', 'Як назвати цей ярлик?', text=item_name)
            if not ok or not item_name.strip():
                return
        if any(entry['path'].lower() == str(path).lower() for entry in self.shortcuts):
            QMessageBox.information(self, 'Уже додано', 'Ця програма або папка вже є в списку.')
            return
        candidate = [*self.shortcuts, {'name': item_name.strip(), 'path': str(path)}]
        if self._save_shortcuts(candidate):
            self.shortcut_search.clear()
            self._render_shortcuts(selected_path=str(path))

    def _save_shortcuts(self, candidate: list[dict[str, str]]) -> bool:
        candidate = sorted(candidate, key=lambda item: item['name'].casefold())
        try:
            write_shortcuts(candidate)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, 'Не вдалося зберегти', str(error))
            return False
        self.shortcuts = candidate
        return True

    def _rename_shortcut(self):
        index = self._shortcut_index()
        if index is None:
            return
        entry = self.shortcuts[index]
        name, ok = QInputDialog.getText(
            self, 'Перейменувати ярлик', 'Нова назва:', text=entry['name'],
        )
        if not ok or not name.strip() or name.strip() == entry['name']:
            return
        candidate = [dict(item) for item in self.shortcuts]
        candidate[index]['name'] = name.strip()
        if self._save_shortcuts(candidate):
            self._render_shortcuts(selected_path=entry['path'])

    def _remove_shortcut(self):
        index = self._shortcut_index()
        if index is None:
            return
        candidate = [item for i, item in enumerate(self.shortcuts) if i != index]
        if self._save_shortcuts(candidate):
            self._render_shortcuts()

    def _run_shortcut(self, row: int):
        index = self._shortcut_index(row)
        if index is None:
            return
        self._launch_entry(index)

    def _launch_entry(self, index: int):
        if not 0 <= index < len(self.shortcuts):
            return
        try:
            open_path(self.shortcuts[index]['path'])
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, 'Не вдалося запустити', str(error))

    def _organizer_page(self) -> QWidget:
        self.file_tabs = QTabWidget()
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(14)
        intro, inner = card()
        intro.setProperty('accent', 'amber')
        inner.addWidget(text_label('Розумне сортування файлів', 'Section'))
        folder_line = QHBoxLayout()
        self.folder_label = text_label(str(self.folder), 'Eyebrow', True)
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        folder_line.addWidget(self.folder_label, 1)
        choose = button('Вибрати папку…')
        choose.clicked.connect(self._choose_folder)
        folder_line.addWidget(choose)
        inner.addLayout(folder_line)
        layout.addWidget(intro)

        controls = QHBoxLayout()
        preview_btn = button('Попередній перегляд', True)
        preview_btn.setIcon(icon('search', '#FFFFFF'))
        preview_btn.clicked.connect(self._preview_folder)
        self.organize_button = button('Розсортувати')
        self.organize_button.setIcon(icon('arrow'))
        self.organize_button.clicked.connect(self._apply_folder)
        self.organize_button.setEnabled(False)
        controls.addWidget(preview_btn)
        controls.addWidget(self.organize_button)
        controls.addStretch()
        layout.addLayout(controls)

        self.preview_label = text_label('Спочатку натисни «Показати попередній перегляд».', 'Muted')
        layout.addWidget(self.preview_label)
        self.preview_table = QTableWidget(0, 2)
        self.preview_table.setHorizontalHeaderLabels(['Файл', 'Куди переміститься'])
        self.preview_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.preview_table.verticalHeader().hide()
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        layout.addWidget(self.preview_table, 1)
        self.file_tabs.addTab(page, 'Сортування')
        help_page = QWidget()
        help_layout = QVBoxLayout(help_page)
        help_layout.setContentsMargins(0, 14, 0, 0)
        help_layout.setSpacing(14)
        for step, title, description in (
            ('01', 'Вибери папку', 'Сортуються лише файли безпосередньо в цій папці. Вкладені папки залишаються на місці.'),
            ('02', 'Переглянь переміщення', 'Побачиш назву файлу та категорію призначення. Якщо файл зміниться, повтори перегляд.'),
            ('03', 'Підтвердь сортування', 'Файли переміщуються в категорійні папки. Зайняті назви отримують числовий суфікс.'),
        ):
            panel, content = card()
            content.addWidget(text_label(step + ' / ' + title, 'Section'))
            content.addWidget(text_label(description, 'Muted', True))
            help_layout.addWidget(panel)
        help_layout.addStretch()
        self.file_tabs.addTab(self._scrollable(help_page), 'Як це працює')
        return self.file_tabs

    def _choose_folder(self):
        selected = QFileDialog.getExistingDirectory(self, 'Папка для сортування', str(self.folder))
        if selected:
            self.folder = Path(selected)
            self.folder_label.setText(str(self.folder))
            self.current_plan = []
            self.preview_table.setRowCount(0)
            self.organize_button.setEnabled(False)
            self.preview_label.setText('Нова папка вибрана. Натисни попередній перегляд.')

    def _preview_folder(self):
        try:
            self.current_plan = plan_organization(self.folder)
        except OSError as error:
            self.current_plan = []
            self.preview_table.setRowCount(0)
            self.organize_button.setEnabled(False)
            self.preview_label.setText('Папку не вдалося перевірити. Повтори попередній перегляд.')
            QMessageBox.warning(self, 'Не вдалося перевірити папку', str(error))
            return
        visible = self.current_plan[:300]
        self.preview_table.setRowCount(len(visible))
        for i, item in enumerate(visible):
            self.preview_table.setItem(i, 0, QTableWidgetItem(item.source.name))
            self.preview_table.setItem(i, 1, QTableWidgetItem(item.category + ' / ' + item.destination.name))
        total = len(self.current_plan)
        suffix = ' Показано перші 300.' if total > 300 else ''
        self.preview_label.setText(f'До сортування: {total} файлів.{suffix}' if total else 'Немає файлів для сортування.')
        self.organize_button.setEnabled(total > 0)

    def _apply_folder(self):
        if not self.current_plan:
            return
        answer = QMessageBox.question(
            self, 'Підтверди сортування',
            f'Перемістити {len(self.current_plan)} файлів у категорійні папки всередині:\n{self.folder}\n\n'
            'Файли не видаляються. Продовжити?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        moved, errors = apply_organization(self.current_plan)
        self.current_plan = []
        self._preview_folder()
        message = f'Переміщено файлів: {moved}.'
        if errors:
            message += '\nНе вдалося перенести деякі файли:\n' + '\n'.join(errors[:8])
            if len(errors) > 8:
                message += f'\n…і ще {len(errors) - 8}.'
        QMessageBox.information(self, 'Результат сортування', message)

    def _about_page(self) -> QScrollArea:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 5, 6)
        layout.setSpacing(16)
        hero = QFrame()
        hero.setObjectName('Hero')
        section = QVBoxLayout(hero)
        section.setContentsMargins(24, 24, 24, 24)
        section.setSpacing(12)
        section.addWidget(text_label(f'MechaHub · {__version__}', 'HeroTitle'))
        section.addWidget(text_label('Персональний центр керування ПК. Без реклами, реєстрації та хмарної синхронізації.', wrap=True))
        layout.addWidget(hero)
        blocks = [
            ('✓ Уже працює', 'Моніторинг системи, персональний лаунчер, сортування файлів, анімований спідометр, ping, jitter, packet loss і мережеві лічильники.'),
            (f'→ Що нове у {__version__}', 'Просте спілкування з ШІ та вибір мови, пароль навчання на один сеанс, довідник касових апаратів і власні проєкти Python та C++.'),
            ('→ Наступна версія', 'Моніторинг відеокарти (за підтримки драйвера), температур, віджети та глибше налаштування дизайну.'),
            ('◈ Приватність', 'Ярлики й зашифровані конспекти залишаються локально. ШІ звертається до Ollama на цьому ПК. Курси валют отримуються від НБУ, SPEED TEST — від Cloudflare, оновлення — з GitHub.'),
            ('▣ Налаштування та файли', 'Конфігурація: ' + str(config_file())),
        ]
        for title, description in blocks:
            frame, frame_layout = card()
            frame_layout.addWidget(text_label(title, 'Section'))
            detail = text_label(description, 'Muted', True)
            detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            frame_layout.addWidget(detail)
            layout.addWidget(frame)
        layout.addStretch()
        return self._scrollable(page)

    def _ensure_updates_dialog(self) -> UpdatesDialog:
        if self._updates_dialog is None:
            self._updates_dialog = UpdatesDialog(self)
            self._updates_dialog.ready_to_close.connect(self._retry_close)
            self._updates_dialog.download_ready.connect(self._update_download_ready)
            self._updates_dialog.update_available.connect(self._update_available)
            self._updates_dialog.install_requested.connect(self._request_update_install)
        return self._updates_dialog

    def _show_updates(self):
        dialog = self._ensure_updates_dialog()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _begin_automatic_updates(self):
        if (not self._close_requested and self._pending_update is None
                and self._update_prepare_worker is None and self.isVisible()):
            self._ensure_updates_dialog().begin_automatic_check()

    def _update_available(self, manifest):
        self.updates_button.setText('Оновлення · ' + manifest.version)
        self.updates_button.setIcon(icon('updates', '#83E1BA'))
        self.update_banner_label.setText(
            f'Доступна MechaHub {manifest.version}. Онови програму зі збереженням конспектів і налаштувань.'
        )
        self.update_banner.show()

    def _show_update_offer(self):
        self._ensure_updates_dialog().show_update_offer()

    def _request_update_install(self, manifest, artifact):
        if self._pending_update is not None or self._update_prepare_worker is not None:
            return
        if not self.study_page.can_leave():
            self._ensure_updates_dialog().installation_failed(
                'Спочатку збережи конспект. Оновлення не почалося.'
            )
            return
        self.update_banner_label.setText('Готуємо оновлення. Конспекти й налаштування залишаться на місці…')
        self.update_banner.show()
        self.update_offer_button.setEnabled(False)
        self.update_later_button.setEnabled(False)
        self._update_prepare_worker = UpdatePreparationThread(manifest, artifact, self)
        self._update_prepare_worker.received.connect(self._update_prepared)
        self._update_prepare_worker.failed.connect(self._update_install_failed)
        self._update_prepare_worker.finished.connect(self._update_preparation_finished)
        self._update_prepare_worker.start()

    def _update_prepared(self, prepared):
        self._pending_update = prepared
        self._close_requested = True

    def _update_preparation_finished(self):
        worker, self._update_prepare_worker = self._update_prepare_worker, None
        if worker:
            worker.deleteLater()
        if self._pending_update is not None or self._close_requested:
            QTimer.singleShot(0, self.close)

    def _update_install_failed(self, message):
        self._handoff_timer.stop()
        prepared, self._pending_update = self._pending_update, None
        try:
            if prepared is not None:
                if self._update_helper_process is not None:
                    cancel_installation(prepared, self._update_helper_process)
                else:
                    discard_prepared(prepared)
        except (OSError, UpdateError):
            pass
        self._update_helper_process = None
        self._update_helper_ready = False
        self._close_requested = False
        self.study_page._close_pending = False
        self.programming_page._close_pending = False
        self.currency_page.close_pending = False
        self.currency_page.refresh_button.setEnabled(self.currency_page.worker is None)
        self.network_page.close_pending = False
        self.network_page.start_button.setEnabled(self.network_page.worker is None)
        if self.isVisible():
            self.network_page.gauge.timer.start()
        self.update_offer_button.setEnabled(True)
        self.update_later_button.setEnabled(True)
        self.update_banner_label.setText('Оновлення не встановлено: ' + message)
        self.update_banner.show()
        dialog = self._ensure_updates_dialog()
        dialog.installation_failed(message)
        dialog.show_update_offer()

    def _begin_update_handoff(self):
        if self._update_helper_process is not None:
            return
        try:
            self._update_helper_process = start_installation(self._pending_update)
        except (OSError, UpdateError) as error:
            self._update_install_failed(str(error))
            return
        self._update_helper_deadline = time.monotonic() + HELPER_READY_SECONDS
        self.update_banner_label.setText('Готуємо перезапуск. Програма закриється, щойно оновлення буде готове…')
        self._handoff_timer.start()

    def _poll_update_handoff(self):
        if self._pending_update is None:
            self._handoff_timer.stop()
            return
        try:
            ready = installation_ready(self._pending_update, self._update_helper_process)
            if not ready and time.monotonic() >= self._update_helper_deadline:
                raise UpdateError('Помічник оновлення не запустився вчасно. Спробуй ще раз.')
        except (OSError, UpdateError) as error:
            self._update_install_failed(str(error))
            return
        if ready:
            self._update_helper_ready = True
            self._handoff_timer.stop()
            QTimer.singleShot(0, self.close)

    def _update_download_ready(self, path: str):
        self.updates_button.setText('Оновлення · готово')
        self.updates_button.setIcon(icon('updates', '#83E1BA'))
        self.updates_button.setToolTip('Нову версію завантажено: ' + path)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._automatic_updates_started:
            self._automatic_updates_started = True
            QTimer.singleShot(1000, self._begin_automatic_updates)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'dashboard_hero'):
            # The introduction yields space to controls on smaller screens.
            self.dashboard_hero.setVisible(self.height() >= 760)

    def _open_system(self, action: str):
        try:
            system_action(action)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, 'Не вдалося відкрити', str(error))

    def closeEvent(self, event):
        if self._update_prepare_worker is not None:
            self._close_requested = True
            event.ignore()
            return
        if not self.cash_registers_page.request_close():
            self._close_requested = False
            if self._pending_update is not None:
                self._update_install_failed('Незбережений кейс залишено відкритим. Збережи його й повтори оновлення.')
            event.ignore()
            return
        if not self.programming_page.can_leave():
            self._close_requested = False
            if self._pending_update is not None:
                self._update_install_failed('Не вдалося зберегти код. Програма залишилася відкритою.')
            event.ignore()
            return
        if not self.study_page.can_leave():
            self._close_requested = False
            if self._pending_update is not None:
                self._update_install_failed('Не вдалося зберегти конспект. Програма залишилася відкритою.')
            event.ignore()
            return
        if not self.programming_page.request_close():
            self._close_requested = True
            event.ignore()
            return
        if not self.currency_page.request_close():
            self._close_requested = True
            event.ignore()
            return
        if self._updates_dialog is not None:
            if not self._updates_dialog.request_close():
                self._close_requested = True
                event.ignore()
                return
            self._updates_dialog.close()
        if self._program_picker and self._program_picker._scan_thread is not None:
            self._close_requested = True
            self._program_picker.reject()
            event.ignore()
            return
        # Never destroy an active QThread: cancel and close once it finishes.
        if hasattr(self, 'network_page') and not self.network_page.request_stop_on_close():
            self._close_requested = True
            event.ignore()
            return
        if self._pending_update is not None and not self._update_helper_ready:
            # Keep the unlocked study session if the updater later fails to start.
            # A running worker must still finish before handing off the executable.
            if self.study_page._worker is not None:
                self.study_page._stop()
                self.study_page._close_pending = True
                self._close_requested = True
                event.ignore()
                return
            self._begin_update_handoff()
            event.ignore()
            return
        if not self.study_page.request_close():
            self._close_requested = True
            event.ignore()
            return
        self.internet_page.shutdown()
        self.system_charts.shutdown()
        self.tick_timer.stop()
        self.update_check_timer.stop()
        self._handoff_timer.stop()
        super().closeEvent(event)

    def _retry_close(self):
        if self._close_requested:
            QTimer.singleShot(0, self.close)

    def _tick(self):
        self._update_stats()
        self._ticks += 1
        if self._ticks % 4 == 0:
            self._update_processes()

    def _update_stats(self):
        try:
            info = self.monitor.snapshot()
            self.cpu.update_tile(f"{info['cpu_percent']:.0f}%", 'Поточне навантаження', info['cpu_percent'])
            self.mem.update_tile(
                f"{info['memory_percent']:.0f}%",
                f"{format_bytes(info['memory_used'])} із {format_bytes(info['memory_total'])}",
                info['memory_percent'],
            )
            self.disk.update_tile(
                f"{info['disk_percent']:.0f}%",
                f"{format_bytes(info['disk_used'])} із {format_bytes(info['disk_total'])} · {info['disk_root']}",
                info['disk_percent'],
            )
            self.net.update_tile(
                f"↓ {format_bytes(info['download_rate'])}/с",
                f"Віддача: ↑ {format_bytes(info['upload_rate'])}/с",
                0,
            )
            self.info_os.setText(f"Операційна система: {info['os_name']}")
            self.info_cores.setText(f"Фізичні ядра / потоки: {info['cpu_cores']} / {info['cpu_threads']}")
            self.info_uptime.setText(f"Час роботи ПК: {info['uptime']}")
            self.last_update.setText('Оновлено ' + datetime.now().strftime('%H:%M:%S'))
        except (OSError, RuntimeError, ValueError, PsutilError) as error:
            self.last_update.setText('Помилка статистики: ' + str(error)[:70])

    def _update_processes(self):
        try:
            data = memory_heavy_processes()
            for i, lbl in enumerate(self.process_rows):
                if i < len(data):
                    name, used = data[i]
                    if len(name) > 29:
                        name = name[:26] + '…'
                    lbl.setText(f'{i + 1}.  {name}  ·  {format_bytes(used)}')
                else:
                    lbl.setText('—')
        except (OSError, RuntimeError, ValueError, PsutilError) as error:
            self.process_rows[0].setText('Не вдалося отримати процеси: ' + str(error)[:55])
