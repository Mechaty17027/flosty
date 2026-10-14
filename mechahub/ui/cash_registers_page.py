"""Offline hardware reference and a separate notebook for personal support cases."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from mechahub.core.cash_registers import MANUFACTURERS, CaseStore, TechnicalCase, read_case_text, search_models
from mechahub.core.settings import config_file


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
        header.addWidget(_label('РОБОЧИЙ ДОВІДНИК', 'Eyebrow'))
        header.addWidget(_label('Касові апарати', 'Headline'))
        header.addWidget(_label('Моделі, основні характеристики та твої власні технічні кейси.'))
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
        root.addLayout(tools)
        self.result_count = _label('')
        root.addWidget(self.result_count)
        split = QSplitter()
        self.models = QListWidget()
        self.models.setMinimumWidth(190)
        self.models.currentItemChanged.connect(self._select_model)
        split.addWidget(self.models)
        self.details_scroll = QScrollArea()
        self.details_scroll.setWidgetResizable(True)
        self.details = QWidget()
        self.details_layout = QVBoxLayout(self.details)
        self.details_layout.setContentsMargins(12, 0, 0, 0)
        self.details_scroll.setWidget(self.details)
        split.addWidget(self.details_scroll)
        split.setSizes([250, 540])
        root.addWidget(split, 1)
        root.addWidget(_label('Характеристики звірено з офіційними сторінками 14.10.2026. '
                              'Комплектація й параметри залежать від модифікації та версії ПЗ.'))
        return page

    def _filter_models(self, *_args):
        selected = self._current_model.key if self._current_model else None
        entries = search_models(self.search.text(), self.manufacturer.currentData())
        self.models.clear()
        selected_row = 0
        for index, model in enumerate(entries):
            item = QListWidgetItem(f'{model.name}\n{model.manufacturer} · {model.kind}')
            item.setData(Qt.ItemDataRole.UserRole, model)
            self.models.addItem(item)
            if model.key == selected:
                selected_row = index
        self.result_count.setText(f'Знайдено моделей: {len(entries)}')
        if entries:
            self.models.setCurrentRow(selected_row)
        else:
            self._select_model(None)

    def _clear_details(self):
        while self.details_layout.count():
            item = self.details_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _select_model(self, current, _previous=None):
        self._clear_details()
        self._current_model = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        model = self._current_model
        if model is None:
            self.details_layout.addWidget(_label('Моделей не знайдено. Спробуй коротшу назву або інший фільтр.', 'Section'))
            self.details_layout.addStretch()
            return
        self.details_layout.addWidget(_label(model.manufacturer.upper(), 'Eyebrow'))
        self.details_layout.addWidget(_label(model.name, 'AccentTitle'))
        self.details_layout.addWidget(_label(model.kind, 'Section'))
        self.details_layout.addWidget(_label(model.purpose))
        card = QFrame()
        card.setObjectName('Card')
        facts = QGridLayout(card)
        facts.setContentsMargins(16, 16, 16, 16)
        facts.setHorizontalSpacing(20)
        facts.setVerticalSpacing(14)
        for row, (name, value) in enumerate(model.facts):
            facts.addWidget(_label(name), row, 0, Qt.AlignmentFlag.AlignTop)
            facts.addWidget(_label(value, 'HeroText'), row, 1)
        facts.setColumnStretch(1, 1)
        self.details_layout.addWidget(card)
        self.details_layout.addWidget(_label('ДЖЕРЕЛА', 'Eyebrow'))
        for source in model.sources:
            control = _button(source.title, lambda _checked=False, url=source.url: self.open_source.emit(url))
            control.setToolTip(source.url)
            self.details_layout.addWidget(control)
        self.details_layout.addWidget(_button('Копіювати назву моделі',
                                              lambda: QApplication.clipboard().setText(model.name)))
        self.details_layout.addWidget(_label(f'Дата перевірки джерел: {model.checked_date}'))
        self.details_layout.addStretch()

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
