"""Offline hardware reference and a separate notebook for personal support cases."""
from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from mechahub.core.cash_registers import MANUFACTURERS, CaseStore, TechnicalCase, read_case_text, search_models
from mechahub.core.settings import config_file
from mechahub.ui.icons import icon


def _label(text: str, kind: str = 'Muted') -> QLabel:
    control = QLabel(text)
    control.setObjectName(kind)
    control.setTextFormat(Qt.TextFormat.PlainText)
    control.setWordWrap(True)
    control.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return control


def _button(text, callback, *, primary=False):
    control = QPushButton(text)
    if primary:
        control.setObjectName('Primary')
    control.clicked.connect(callback)
    return control


def _asset_path(filename: str) -> Path | None:
    """Only bundled catalogue photos are read; the GUI never requests an image URL."""
    if not filename or filename in ('.', '..') or any(char in filename for char in '/\\:'):
        return None
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
    return root / 'assets' / 'cash_registers' / filename


def _model_image(model) -> QPixmap:
    image = getattr(model, 'image', None)
    path = _asset_path(image.filename) if image is not None else None
    return QPixmap(str(path)) if path is not None else QPixmap()


def _thumbnail(picture: QPixmap) -> QIcon:
    canvas = QPixmap(48, 48)
    canvas.fill(QColor('#FFFFFF'))
    painter = QPainter(canvas)
    if picture.isNull():
        scaled = icon('cash', '#64748B').pixmap(28, 28)
    else:
        scaled = picture.scaled(42, 42, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
    painter.drawPixmap((48 - scaled.width()) // 2, (48 - scaled.height()) // 2, scaled)
    painter.end()
    return QIcon(canvas)


class _ProductPhoto(QLabel):
    """A bounded, neutral photo surface with an offline placeholder."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.setObjectName('RegisterPhoto')
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setFixedSize(160, 112)
        self.setStyleSheet('QLabel#RegisterPhoto { background: #FFFFFF; color: #64748B; '
                           'border: 1px solid #D6DBE5; border-radius: 10px; padding: 6px; }')
        self.setAccessibleName(f'Фото {model.name}')
        image = getattr(model, 'image', None)
        tooltip = f'{image.credit}\n{image.source.title}\n{image.source.url}' if image else model.name
        picture = _model_image(model)
        self.setProperty('image_available', not picture.isNull())
        if picture.isNull():
            self.setText('Фото недоступне\nОфлайн-довідник працює')
            tooltip = f'Локальне фото не знайдено.\n{tooltip}'
        else:
            self.setPixmap(picture.scaled(146, 98, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
        self.setToolTip(tooltip)


class CaseEditor(QDialog):
    def __init__(self, store: CaseStore, parent=None, *, case: TechnicalCase | None = None,
                 initial_title='', initial_body=''):
        super().__init__(parent)
        self.store = store
        self.case = case
        self.saved_case = None
        self.setWindowTitle('Редагувати кейс' if case else 'Новий технічний кейс')
        self.resize(650, 540)
        root = QVBoxLayout(self)
        root.addWidget(_label('ВЛАСНІ МАТЕРІАЛИ', 'Eyebrow'))
        root.addWidget(_label('Запиши симптом, перевірки, рішення та результат.'))
        self.title = QLineEdit(case.title if case else initial_title)
        self.title.setMaxLength(160)
        self.title.setPlaceholderText('Назва проблеми або інструкції')
        root.addWidget(self.title)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItem('')
        self.model.addItems([entry.name for entry in search_models()])
        self.model.lineEdit().setMaxLength(120)
        self.model.lineEdit().setPlaceholderText('Модель апарата (необов’язково)')
        self.model.setCurrentText(case.model if case else '')
        root.addWidget(self.model)
        self.body = QTextEdit()
        self.body.setAcceptRichText(False)
        self.body.setPlaceholderText('Симптом:\n\nЩо перевірив:\n\nРішення:\n\nРезультат:\n\nДжерело:')
        self.body.setPlainText(case.body if case else initial_body)
        root.addWidget(self.body, 1)
        self.status = _label('Зберігається локально, як звичайний текст.')
        root.addWidget(self.status)
        self._baseline = self._values()
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(_button('Скасувати', self.reject))
        actions.addWidget(_button('Зберегти кейс', self._save, primary=True))
        root.addLayout(actions)

    def _values(self):
        return self.title.text(), self.model.currentText(), self.body.toPlainText()

    def _save(self):
        try:
            self.saved_case = self.store.save(*self._values(), identifier=self.case.id if self.case else None)
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self._baseline = self._values()
        self.accept()

    def reject(self):
        if self._values() != self._baseline:
            choice = QMessageBox.question(self, 'Незбережений кейс', 'Відкинути незбережені зміни?',
                                          QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                          QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Discard:
                return
        super().reject()


class CashRegistersPage(QWidget):
    open_source = Signal(str)

    def __init__(self, parent=None, *, cases_path: Path | None = None):
        super().__init__(parent)
        self.store = CaseStore(cases_path or config_file().with_name('cash-register-cases.json'))
        self._current_model = None
        self._cases = []
        self._editor = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        hero = QFrame()
        hero.setObjectName('Card')
        hero.setProperty('accent', 'amber')
        header = QVBoxLayout(hero)
        header.setContentsMargins(12, 8, 12, 8)
        header.setSpacing(3)
        header.addWidget(_label('Касові апарати', 'Headline'))
        header.addWidget(_label('Фото моделей, характеристики, повідомлення про помилки та власні технічні кейси.'))
        root.addWidget(hero)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._catalog_page(), 'Каталог моделей')
        self.tabs.addTab(self._cases_page(), 'Кейси й інструкції')
        root.addWidget(self.tabs, 1)
        self._filter_models()
        self._reload_cases()

    def _catalog_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Пошук моделі, інтерфейсу або призначення…')
        self.search.setMaxLength(200)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_models)
        tools.addWidget(self.search, 1)
        self.manufacturer = QComboBox()
        self.manufacturer.addItem('Усі виробники', '')
        for name in MANUFACTURERS:
            self.manufacturer.addItem(name, name)
        self.manufacturer.currentIndexChanged.connect(self._filter_models)
        tools.addWidget(self.manufacturer)
        self.result_count = _label('')
        tools.addWidget(self.result_count)
        root.addLayout(tools)
        split = QSplitter()
        split.setChildrenCollapsible(False)
        self.models = QListWidget()
        self.models.setObjectName('RegisterModels')
        self.models.setMinimumWidth(165)
        self.models.setIconSize(QSize(48, 48))
        self.models.setWordWrap(True)
        self.models.setSpacing(3)
        self.models.setStyleSheet('QListWidget#RegisterModels::item { padding: 7px 5px; }')
        self.models.currentItemChanged.connect(self._select_model)
        split.addWidget(self.models)
        self.details = QWidget()
        self.details.setMinimumWidth(320)
        self.details_layout = QVBoxLayout(self.details)
        self.details_layout.setContentsMargins(10, 0, 0, 0)
        split.addWidget(self.details)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 570])
        root.addWidget(split, 1)
        root.addWidget(_label('Комплектація, повідомлення й параметри залежать від модифікації та версії ПЗ. '
                              'Дата перевірки та оригінали — у вкладці «Джерела».'))
        return page

    def _filter_models(self, *_args):
        selected = self._current_model.key if self._current_model else None
        entries = search_models(self.search.text(), self.manufacturer.currentData())
        self.models.clear()
        selected_row = 0
        for index, model in enumerate(entries):
            item = QListWidgetItem(_thumbnail(_model_image(model)), f'{model.name}\n{model.manufacturer}')
            item.setData(Qt.ItemDataRole.UserRole, model)
            image = getattr(model, 'image', None)
            credit = f'\n{image.credit}\n{image.source.url}' if image else ''
            item.setToolTip(f'{model.name}\n{model.kind}{credit}')
            self.models.addItem(item)
            if model.key == selected:
                selected_row = index
        self.result_count.setText(f'Моделей: {len(entries)}')
        if entries:
            self.models.setCurrentRow(selected_row)
        else:
            self._select_model(None)

    def _clear_details(self):
        while self.details_layout.count():
            item = self.details_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.detail_tabs = None
        self.issue_selector = None
        self.issue_details = None
        self.photo = None

    @staticmethod
    def _scroll_content():
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setMinimumWidth(0)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 12, 10, 8)
        layout.setSpacing(12)
        scroll.setWidget(content)
        return scroll, content, layout

    def _add_source(self, layout, source):
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        title = _label(source.title, 'HeroText')
        title.setMinimumWidth(0)
        line.addWidget(title, 1)
        button = _button('Відкрити', lambda _checked=False, url=source.url: self.open_source.emit(url))
        button.setObjectName('Subtle')
        button.setProperty('source_url', source.url)
        button.setAccessibleName(f'Відкрити: {source.title}')
        button.setToolTip(source.url)
        line.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(row)

    def _select_model(self, current, _previous=None):
        self._clear_details()
        self._current_model = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        model = self._current_model
        if model is None:
            self.details_layout.addWidget(_label('Моделей не знайдено. Спробуй коротшу назву або інший фільтр.', 'Section'))
            self.details_layout.addStretch()
            return
        heading = QWidget()
        heading_layout = QHBoxLayout(heading)
        heading_layout.setContentsMargins(0, 0, 0, 4)
        text = QVBoxLayout()
        text.setSpacing(5)
        text.addWidget(_label(model.name, 'AccentTitle'))
        text.addWidget(_label(f'{model.manufacturer} · {model.kind}'))
        heading_layout.addLayout(text, 1)
        self.details_layout.addWidget(heading)
        self.detail_tabs = QTabWidget()
        self.detail_tabs.setObjectName('RegisterDetailTabs')
        self.detail_tabs.setStyleSheet('QTabWidget#RegisterDetailTabs QTabBar::tab '
                                      '{ padding: 8px 9px; margin-right: 3px; font-size: 12px; }')
        self.detail_tabs.addTab(self._facts_page(model), 'Характеристики')
        self.detail_tabs.addTab(self._issues_page(model), 'Помилки та рішення')
        self.detail_tabs.addTab(self._sources_page(model), 'Джерела')
        self.details_layout.addWidget(self.detail_tabs, 1)

    def _facts_page(self, model):
        scroll, _content, layout = self._scroll_content()
        introduction = QHBoxLayout()
        purpose = QVBoxLayout()
        purpose.addWidget(_label('Призначення', 'Section'))
        purpose.addWidget(_label(model.purpose, 'HeroText'))
        purpose.addStretch()
        introduction.addLayout(purpose, 1)
        self.photo = _ProductPhoto(model)
        introduction.addWidget(self.photo, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(introduction)
        card = QFrame()
        card.setObjectName('Card')
        facts = QGridLayout(card)
        facts.setContentsMargins(12, 12, 12, 12)
        facts.setHorizontalSpacing(12)
        facts.setVerticalSpacing(12)
        for row, (name, value) in enumerate(model.facts):
            name_label = _label(name)
            name_label.setMaximumWidth(135)
            facts.addWidget(name_label, row, 0, Qt.AlignmentFlag.AlignTop)
            facts.addWidget(_label(value, 'HeroText'), row, 1, Qt.AlignmentFlag.AlignTop)
        facts.setColumnStretch(1, 1)
        layout.addWidget(card)
        layout.addWidget(_button('Копіювати назву моделі',
                                 lambda: QApplication.clipboard().setText(model.name)))
        layout.addStretch()
        return scroll

    def _issues_page(self, model):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(4, 10, 0, 0)
        root.setSpacing(8)
        self.issue_notice = _label(model.issue_note or
                                   'Типові повідомлення з настанов виробника, не статистика частоти поломок.')
        root.addWidget(self.issue_notice)
        self.issue_selector = QComboBox()
        self.issue_selector.setAccessibleName('Оберіть повідомлення або проблему')
        self.issue_selector.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.issue_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.issue_selector.setMinimumContentsLength(12)
        for issue in model.issues:
            name = f'{issue.code} — {issue.title}' if issue.code else issue.title
            self.issue_selector.addItem(name, issue)
            self.issue_selector.setItemData(self.issue_selector.count() - 1, name, Qt.ItemDataRole.ToolTipRole)
        self.issue_selector.setEnabled(bool(model.issues))
        self.issue_selector.currentIndexChanged.connect(self._select_issue)
        root.addWidget(self.issue_selector)
        self.issue_scroll, self.issue_details, self.issue_layout = self._scroll_content()
        root.addWidget(self.issue_scroll, 1)
        self._select_issue()
        return page

    def _select_issue(self, *_args):
        while self.issue_layout.count():
            item = self.issue_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        issue = self.issue_selector.currentData()
        if issue is None:
            self.issue_layout.addWidget(_label('Для цієї моделі перевірених повідомлень ще немає.'))
        else:
            self.issue_layout.addWidget(_label(issue.title, 'Section'))
            if issue.code:
                self.issue_layout.addWidget(_label(f'Позначення в настанові: {issue.code}', 'Eyebrow'))
            self.issue_layout.addWidget(_label('Що означає', 'Section'))
            self.issue_layout.addWidget(_label(issue.meaning, 'HeroText'))
            self.issue_layout.addWidget(_label('Що перевірити оператору', 'Section'))
            for number, check in enumerate(issue.checks, 1):
                self.issue_layout.addWidget(_label(f'{number}. {check}', 'HeroText'))
            self.issue_layout.addWidget(_label('Коли потрібен сервіс', 'Section'))
            self.issue_layout.addWidget(_label(issue.service, 'HeroText'))
            self.issue_layout.addWidget(_label('Джерела повідомлення', 'Eyebrow'))
            for source in issue.sources:
                self._add_source(self.issue_layout, source)
        self.issue_layout.addStretch()
        self.issue_scroll.verticalScrollBar().setValue(0)

    def _sources_page(self, model):
        scroll, _content, layout = self._scroll_content()
        layout.addWidget(_label(f'Перевірено: {model.checked_date}', 'Eyebrow'))
        if model.reference_notes:
            layout.addWidget(_label('Модифікація та уточнення', 'Section'))
            for note in model.reference_notes:
                layout.addWidget(_label(f'• {note}', 'HeroText'))
        layout.addWidget(_label('Документація моделі', 'Section'))
        for source in model.sources:
            self._add_source(layout, source)
        image = getattr(model, 'image', None)
        if image is not None:
            layout.addWidget(_label('Фото апарата', 'Section'))
            layout.addWidget(_label(image.credit))
            self._add_source(layout, image.source)
        seen = {source.url for source in model.sources}
        extras = []
        for issue in model.issues:
            for source in issue.sources:
                if source.url not in seen:
                    seen.add(source.url)
                    extras.append(source)
        if extras:
            layout.addWidget(_label('Повідомлення та перевірки', 'Section'))
            for source in extras:
                self._add_source(layout, source)
        layout.addStretch()
        return scroll

    def _cases_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.addWidget(_label('Власні кейси та інструкції', 'Section'))
        root.addWidget(_label('Збирай практичні випадки окремо від каталогу виробників. '
                              'Нові матеріали можна написати тут або імпортувати з TXT.'))
        actions = QHBoxLayout()
        actions.addWidget(_button('Новий кейс', self._new_case, primary=True))
        actions.addWidget(_button('Імпортувати TXT', self._import_case))
        self.edit_case_button = _button('Редагувати', self._edit_case)
        self.delete_case_button = _button('Видалити', self._delete_case)
        actions.addWidget(self.edit_case_button)
        actions.addWidget(self.delete_case_button)
        actions.addStretch()
        root.addLayout(actions)
        self.case_search = QLineEdit()
        self.case_search.setMaxLength(200)
        self.case_search.setPlaceholderText('Пошук у власних кейсах…')
        self.case_search.setClearButtonEnabled(True)
        self.case_search.textChanged.connect(self._filter_cases)
        root.addWidget(self.case_search)
        split = QSplitter()
        self.case_list = QListWidget()
        self.case_list.setMinimumWidth(190)
        self.case_list.currentItemChanged.connect(self._select_case)
        self.case_list.itemDoubleClicked.connect(lambda _item: self._edit_case())
        split.addWidget(self.case_list)
        self.case_text = QTextEdit()
        self.case_text.setReadOnly(True)
        self.case_text.setAcceptRichText(False)
        split.addWidget(self.case_text)
        split.setSizes([250, 540])
        root.addWidget(split, 1)
        self.case_status = _label('')
        root.addWidget(self.case_status)
        return page

    def _reload_cases(self, selected_id=None):
        try:
            self._cases = self.store.load()
        except (OSError, ValueError) as error:
            self._cases = []
            self.case_status.setText(str(error))
            self._filter_cases()
            return
        self.case_status.setText('Зберігається на цьому ПК. Власні матеріали не є інструкціями виробника.')
        self._filter_cases(selected_id=selected_id)

    def _filter_cases(self, *_args, selected_id=None):
        current = self._selected_case()
        selected_id = selected_id or (current.id if current else None)
        terms = self.case_search.text().casefold().split()
        self.case_list.clear()
        for case in self._cases:
            text = f'{case.title} {case.model} {case.body}'.casefold()
            if not all(term in text for term in terms):
                continue
            item = QListWidgetItem(case.title + (f'\n{case.model}' if case.model else ''))
            item.setData(Qt.ItemDataRole.UserRole, case)
            self.case_list.addItem(item)
            if case.id == selected_id:
                self.case_list.setCurrentItem(item)
        if self.case_list.currentItem() is None and self.case_list.count():
            self.case_list.setCurrentRow(0)
        if not self.case_list.count():
            self._select_case(None)

    def _selected_case(self):
        item = self.case_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _select_case(self, current, _previous=None):
        case = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self.edit_case_button.setEnabled(case is not None)
        self.delete_case_button.setEnabled(case is not None)
        if case is None:
            text = ('За цим запитом кейсів не знайдено.' if self.case_search.text() else
                    'Кейсів ще немає.\n\nДодай власний випадок або імпортуй текстовий файл. '
                    'Тут будуть твої симптоми, перевірки, рішення та пояснення.')
        else:
            text = f'{case.title}\n{case.model or "Модель не вказано"}\n\n{case.body}\n\nОновлено: {case.updated_at[:10]}'
        self.case_text.setPlainText(text)

    def _open_editor(self, *, case=None, initial_title='', initial_body=''):
        editor = CaseEditor(self.store, self, case=case, initial_title=initial_title, initial_body=initial_body)
        self._editor = editor
        try:
            if editor.exec() == QDialog.DialogCode.Accepted:
                self._reload_cases(editor.saved_case.id)
        finally:
            self._editor = None
            editor.deleteLater()

    def can_leave(self) -> bool:
        """Protect a modal draft when the host closes or updates programmatically."""
        editor = self._editor
        if editor is None or not editor.isVisible():
            return True
        editor.reject()
        return not editor.isVisible()

    def request_close(self) -> bool:
        return self.can_leave()

    def _new_case(self):
        self._open_editor()

    def _edit_case(self):
        case = self._selected_case()
        if case is not None:
            self._open_editor(case=case)

    def _import_case(self):
        filename, _filter = QFileDialog.getOpenFileName(self, 'Імпортувати технічний кейс', '', 'Текстові файли UTF-8 (*.txt)')
        if not filename:
            return
        try:
            body = read_case_text(Path(filename))
        except (OSError, ValueError) as error:
            self.case_status.setText(str(error))
            return
        self._open_editor(initial_title=Path(filename).stem[:160], initial_body=body)

    def _delete_case(self):
        case = self._selected_case()
        if case is None:
            return
        choice = QMessageBox.question(self, 'Видалити кейс', f'Видалити «{case.title}»?',
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if choice != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete(case.id)
        except (OSError, ValueError) as error:
            self.case_status.setText(str(error))
            return
        self._reload_cases()
