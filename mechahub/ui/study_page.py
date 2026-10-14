"""Password-gated notes and a free, explicitly configured local study assistant."""
from __future__ import annotations

from pathlib import Path
import threading

from PySide6.QtCore import QSettings, QSignalBlocker, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
    QSplitter, QStackedWidget, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from mechahub.core.settings import config_file
from mechahub.core.study import SUBJECTS, StudyVault
from mechahub.core.study_ai import AISettings, StudyAIClient
from mechahub.core.study_sources import sources_for_subject


def label(text, kind='Muted'):
    control = QLabel(text)
    control.setObjectName(kind)
    control.setTextFormat(Qt.TextFormat.PlainText)
    control.setWordWrap(True)
    return control


def button(text, callback, primary=False):
    control = QPushButton(text)
    if primary:
        control.setObjectName('Primary')
    control.clicked.connect(callback)
    return control


class StudyWorker(QThread):
    received = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.cancel = threading.Event()

    def run(self):
        try:
            result = self.operation(self.cancel)
            if not self.cancel.is_set():
                self.received.emit(result)
        except Exception as error:
            if not self.cancel.is_set():
                self.failed.emit(str(error))
        finally:
            self.operation = None


class StudyPage(QWidget):
    open_source = Signal(str)
    ready_to_close = Signal()

    def __init__(self, parent=None, *, vault_path: Path | None = None, settings=None):
        super().__init__(parent)
        self.vault = StudyVault(vault_path or config_file().with_name('study.vault'))
        self.settings = settings if settings is not None else QSettings('MechaHub', 'MechaHub')
        self._worker = None
        self._close_pending = False
        self._note_id = None
        self._dirty = False
        self._history = []
        self._last_answer = ''
        self._answer_target = None
        self._generation = 0
        self._subject_key = next(iter(SUBJECTS))
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)
        self.stack.addWidget(self._gate())
        self.stack.addWidget(self._workspace())
        self._refresh_gate()

    def _gate(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.addStretch()
        card = QFrame()
        card.setObjectName('Card')
        card.setProperty('accent', 'purple')
        card.setMinimumSize(480, 440)
        card.setMaximumWidth(540)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.addWidget(label('ТВОЄ НАВЧАННЯ', 'Eyebrow'))
        self.gate_title = label('Навчання та конспекти', 'AccentTitle')
        layout.addWidget(self.gate_title)
        self.gate_hint = label('')
        layout.addWidget(self.gate_hint)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('Пароль — щонайменше 8 символів')
        self.password.setMaxLength(1024)
        self.password.returnPressed.connect(self._unlock)
        layout.addWidget(self.password)
        self.password_confirm = QLineEdit()
        self.password_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_confirm.setPlaceholderText('Повтори пароль')
        self.password_confirm.setMaxLength(1024)
        self.password_confirm.returnPressed.connect(self._unlock)
        layout.addWidget(self.password_confirm)
        self.unlock_button = button('Відкрити', self._unlock, True)
        layout.addWidget(self.unlock_button)
        self.gate_status = label('')
        layout.addWidget(self.gate_status)
        layout.addWidget(label('Конспекти шифруються на цьому ПК. Збережи пароль: без нього відновити тексти неможливо.'))
        root.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addStretch()
        return page

    def _refresh_gate(self):
        exists = self.vault.exists
        self.gate_title.setText('Вхід до навчання' if exists else 'Створи пароль для навчання')
        self.gate_hint.setText('Введи пароль, щоб відкрити конспекти та помічника.' if exists else
                               'Один пароль захищає конспекти з усіх чотирьох предметів.')
        self.password_confirm.setVisible(not exists)
        self.unlock_button.setText('Відкрити' if exists else 'Створити та відкрити')

    def _workspace(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.subject = QComboBox()
        for key, name in SUBJECTS.items():
            self.subject.addItem(name, key)
        self.subject.currentIndexChanged.connect(self._change_subject)
        header.addWidget(self.subject, 1)
        header.addWidget(button('Заблокувати', self.lock))
        root.addLayout(header)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._notes_page(), 'Конспекти')
        self.tabs.addTab(self._chat_page(), 'ШІ-помічник')
        self.tabs.addTab(self._sources_page(), 'Джерела НМТ')
        self.tabs.addTab(self._setup_page(), 'Безоплатний ШІ')
        root.addWidget(self.tabs, 1)
        self.status = label('Тексти зберігаються тільки на цьому ПК.')
        root.addWidget(self.status)
        return page

    def _notes_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        controls = QHBoxLayout()
        controls.addWidget(button('Новий конспект', self._new_note))
        controls.addWidget(button('Зберегти', self.save_note, True))
        controls.addWidget(button('Видалити', self._delete_note))
        controls.addStretch()
        root.addLayout(controls)
        split = QSplitter()
        self.notes = QListWidget()
        self.notes.setMinimumWidth(160)
        self.notes.currentItemChanged.connect(self._select_note)
        split.addWidget(self.notes)
        editor = QWidget()
        col = QVBoxLayout(editor)
        col.setContentsMargins(0, 0, 0, 0)
        self.note_title = QLineEdit()
        self.note_title.setPlaceholderText('Назва конспекту')
        self.note_title.setMaxLength(200)
        self.note_title.textChanged.connect(self._mark_dirty)
        col.addWidget(self.note_title)
        self.note_body = QTextEdit()
        self.note_body.setAcceptRichText(False)
        self.note_body.setPlaceholderText('Записуй тему, правила, приклади та власні пояснення…')
        self.note_body.textChanged.connect(self._mark_dirty)
        col.addWidget(self.note_body, 1)
        actions = QHBoxLayout()
        self.structure_button = button('ШІ: структурувати', lambda: self._ask('structure'))
        self.explain_button = button('ШІ: пояснити й доповнити', lambda: self._ask('explain'))
        actions.addWidget(self.structure_button)
        actions.addWidget(self.explain_button)
        col.addLayout(actions)
        split.addWidget(editor)
        split.setSizes([190, 570])
        root.addWidget(split, 1)
        return page

    def _chat_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.addWidget(label('Став запитання, тренуй завдання НМТ або розбирай помилки. Звіряй пояснення з навчальними джерелами.'))
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        root.addWidget(self.transcript, 1)
        self.question = QTextEdit()
        self.question.setAcceptRichText(False)
        self.question.setMaximumHeight(105)
        self.question.setPlaceholderText('Наприклад: поясни квадратні рівняння й дай 3 тренувальні завдання.')
        root.addWidget(self.question)
        line = QHBoxLayout()
        self.send_button = button('Надіслати', lambda: self._ask('chat'), True)
        self.stop_button = button('Зупинити', self._stop)
        self.stop_button.setEnabled(False)
        self.apply_button = button('Застосувати до конспекту', self._apply_answer)
        self.apply_button.setEnabled(False)
        line.addWidget(self.send_button)
        line.addWidget(self.stop_button)
        line.addWidget(self.apply_button)
        line.addWidget(button('Очистити чат', self._clear_chat))
        root.addLayout(line)
        root.addWidget(label('Правки застосовуються після твого натискання. Ctrl+Z у редакторі скасовує заміну.'))
        return page

    def _sources_page(self):
        self.source_container = QWidget()
        self.source_layout = QVBoxLayout(self.source_container)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(self.source_container)
        return area

    def _render_sources(self):
        while self.source_layout.count():
            item = self.source_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.source_layout.addWidget(label('Перевірені навчальні джерела', 'Section'))
        self.source_layout.addWidget(label('iLearn має безоплатні курси; для частини вправ потрібна безоплатна реєстрація на платформі.'))
        for source in sources_for_subject(self._subject_key):
            frame = QFrame()
            frame.setObjectName('Card')
            col = QVBoxLayout(frame)
            col.addWidget(label(source.title, 'Section'))
            col.addWidget(label(source.description))
            col.addWidget(label(source.url))
            col.addWidget(button('Відкрити у MechaHub', lambda _checked=False, url=source.url: self.open_source.emit(url)))
            self.source_layout.addWidget(frame)
        self.source_layout.addStretch()

    def _setup_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.addWidget(label('Локальний ШІ без оплати API', 'AccentTitle'))
        root.addWidget(label('1. Встанови Ollama для Windows.\n2. Завантаж модель командою нижче в PowerShell.\n3. Залиш Ollama запущеною та натисни «Перевірити підключення».'))
        root.addWidget(button('Відкрити завантаження Ollama', lambda: self.open_source.emit('https://ollama.com/download/windows')))
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems(['qwen3:4b', 'qwen3:1.7b', 'qwen3:8b'])
        self.model.setCurrentText(str(self.settings.value('study/model', 'qwen3:4b')))
        root.addWidget(label('Модель для помічника:'))
        root.addWidget(self.model)
        self.model_command = QLineEdit()
        self.model_command.setReadOnly(True)
        self._model_changed(self.model.currentText(), persist=False)
        self.model.currentTextChanged.connect(self._model_changed)
        root.addWidget(self.model_command)
        root.addWidget(button('Скопіювати команду завантаження', lambda: QApplication.clipboard().setText(self.model_command.text())))
        root.addWidget(label('qwen3:4b займає приблизно 2,5 ГБ для завантаження; робота потребує додаткової RAM. Для слабшого ПК можна вибрати 1.7b. Модель завантажується один раз, після цього чат працює локально.'))
        self.probe_button = button('Перевірити підключення', self._probe, True)
        root.addWidget(self.probe_button)
        self.ai_status = label('Ollama: ще не перевірено. Адреса: 127.0.0.1:11434.')
        root.addWidget(self.ai_status)
        root.addStretch()
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        return area

    def _model_changed(self, model, persist=True):
        # Display only validated model names; never execute user text as a shell command.
        import re
        valid = bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', model)) and not model.lower().endswith((':cloud', '-cloud'))
        if hasattr(self, 'model_command'):
            self.model_command.setText('ollama pull ' + model if valid else 'Введи коректну назву локальної моделі')
        if valid and persist:
            self.settings.setValue('study/model', model)

    def _unlock(self):
        password = self.password.text()
        try:
            if self.vault.exists:
                self.vault.unlock(password)
            else:
                if password != self.password_confirm.text():
                    raise ValueError('Паролі не збігаються.')
                self.vault.create(password)
        except Exception as error:
            self.gate_status.setText(str(error))
            return
        self.password.clear()
        self.password_confirm.clear()
        self.gate_status.clear()
        self.stack.setCurrentIndex(1)
        self._load_notes()
        self._render_sources()

    def _mark_dirty(self):
        if self.vault.unlocked:
            self._dirty = True
            if self._answer_target is not None:
                self.apply_button.setEnabled(self._answer_matches_editor())
            self.status.setText('Є незбережені зміни. Збереження також виконується при виході з розділу.')

    def _set_editor(self, note=None):
        with QSignalBlocker(self.note_title), QSignalBlocker(self.note_body):
            self.note_title.setText(note.title if note else '')
            self.note_body.setPlainText(note.body if note else '')
        self._note_id = note.id if note else None
        self._dirty = False
        if self._answer_target is not None:
            self.apply_button.setEnabled(self._answer_matches_editor())

    def _load_notes(self, selected=None):
        with QSignalBlocker(self.notes):
            self.notes.clear()
            for note in self.vault.list_notes(self._subject_key):
                item = QListWidgetItem(note.title)
                item.setData(Qt.ItemDataRole.UserRole, note.id)
                self.notes.addItem(item)
                if note.id == selected:
                    self.notes.setCurrentItem(item)
        if not selected:
            self._set_editor()

    def save_note(self, _checked=False):
        if not self.vault.unlocked or not self._dirty:
            return True
        if self._note_id is None and not self.note_title.text().strip() and not self.note_body.toPlainText().strip():
            self._dirty = False
            return True
        try:
            note = self.vault.save_note(self._subject_key, self.note_title.text().strip() or 'Без назви', self.note_body.toPlainText(), self._note_id)
        except Exception as error:
            self.status.setText('Не вдалося зберегти: ' + str(error))
            QMessageBox.warning(self, 'Конспект не збережено', str(error))
            return False
        self._note_id = note.id
        self._dirty = False
        if self._answer_target is not None:
            self.apply_button.setEnabled(self._answer_matches_editor())
        self._load_notes(note.id)
        self.status.setText('Конспект збережено та зашифровано.')
        return True

    def _select_note(self, current, previous):
        if not self.vault.unlocked:
            return
        selected_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        previous_id = self._note_id
        if not self.save_note():
            self._highlight_note(previous_id)
            return
        if selected_id:
            self._set_editor(self.vault.get_note(selected_id))
            self._highlight_note(selected_id)

    def _highlight_note(self, note_id):
        # Saving can rebuild the list; retain the editor and select by stable id.
        with QSignalBlocker(self.notes):
            for row in range(self.notes.count()):
                if self.notes.item(row).data(Qt.ItemDataRole.UserRole) == note_id:
                    self.notes.setCurrentRow(row)
                    return
            self.notes.setCurrentRow(-1)

    def _new_note(self):
        if self.save_note():
            self._load_notes()
            self.note_title.setFocus()

    def _delete_note(self):
        if not self._note_id:
            return
        if QMessageBox.question(self, 'Видалити конспект?', 'Конспект буде видалено з локального сховища.') != QMessageBox.StandardButton.Yes:
            return
        try:
            self.vault.delete_note(self._note_id)
        except Exception as error:
            QMessageBox.warning(self, 'Не вдалося видалити', str(error))
            return
        self._load_notes()

    def _change_subject(self):
        selected = self.subject.currentData()
        if selected == self._subject_key:
            return
        if self.vault.unlocked and not self.save_note():
            with QSignalBlocker(self.subject):
                self.subject.setCurrentIndex(self.subject.findData(self._subject_key))
            return
        self._stop()
        self._generation += 1
        self._subject_key = selected
        self._clear_chat()
        if self.vault.unlocked:
            self._load_notes()
            self._render_sources()

    def _client(self):
        return StudyAIClient(AISettings(model=self.model.currentText().strip()))

    def _start_worker(self, operation, callback):
        if self._worker is not None or not self.vault.unlocked:
            return False
        generation = self._generation
        worker = StudyWorker(operation, self)
        self._worker = worker
        worker.received.connect(lambda result: callback(result) if generation == self._generation and self.vault.unlocked else None)
        worker.failed.connect(lambda message: self._ai_failed(message) if generation == self._generation and self.vault.unlocked else None)
        worker.finished.connect(self._worker_finished)
        self._set_busy(True)
        worker.start()
        return True

    def _probe(self):
        try:
            client = self._client()
        except Exception as error:
            self._ai_failed(str(error))
            return
        def received(models):
            current = self.model.currentText()
            for model in models:
                if self.model.findText(model) < 0:
                    self.model.addItem(model)
            self.ai_status.setText('Ollama працює. Модель готова.' if current in models else 'Ollama працює. Завантаж вибрану модель командою вище.')
            self.status.setText(self.ai_status.text())
        if self._start_worker(lambda cancel: client.list_models(cancel=cancel), received):
            self.ai_status.setText('Перевірка Ollama…')

    def _ask(self, task):
        if self._worker is not None or not self.vault.unlocked:
            return
        message = self.question.toPlainText().strip() if task == 'chat' else ('Структуруй конспект і поясни правки.' if task == 'structure' else 'Поясни тему конспекту, перевір помилки й доповни потрібними прикладами.')
        note = self.note_body.toPlainText() if task != 'chat' else ''
        if not message or (task != 'chat' and not note.strip()):
            self.status.setText('Спочатку напиши запитання або текст конспекту.')
            return
        try:
            client = self._client()
        except Exception as error:
            self._ai_failed(str(error))
            return
        history = list(self._history)
        sources = tuple((source.title, source.url) for source in sources_for_subject(self._subject_key))
        subject = self._subject_key
        target = (self._note_id, note) if task != 'chat' else None
        if self._start_worker(lambda cancel: client.ask(subject, message, history=history, note_text=note, task=task, sources=sources, cancel=cancel),
                              lambda answer: self._answer_received(message, answer, task, target)):
            self._append_chat('Ти', message)
            if task == 'chat':
                self.question.clear()
            self.tabs.setCurrentIndex(1)
            self.status.setText('ШІ готує відповідь на твоєму ПК…')

    def _append_chat(self, name, text):
        cursor = self.transcript.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(name + '\n' + text + '\n\n')
        self.transcript.setTextCursor(cursor)
        self.transcript.ensureCursorVisible()

    def _answer_received(self, message, answer, task, target=None):
        self._append_chat('ШІ', answer)
        self._history.extend([{'role': 'user', 'content': message}, {'role': 'assistant', 'content': answer}])
        self._history = self._history[-12:]
        self._last_answer = answer
        self._answer_target = target if task in ('structure', 'explain') else None
        self.apply_button.setEnabled(self._answer_matches_editor())
        self.status.setText('Відповідь готова. Перевір її за джерелами та застосуй правки за потреби.')

    def _apply_answer(self):
        if not self._last_answer or not self.vault.unlocked:
            return
        if not self._answer_matches_editor():
            self.apply_button.setEnabled(False)
            self.status.setText('Конспект змінився після запиту. Переглянь пропозицію у чаті або надішли його актуальний текст ще раз.')
            return
        cursor = self.note_body.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(self._last_answer)
        self.tabs.setCurrentIndex(0)
        self.status.setText('Пропозицію перенесено у редактор. Ctrl+Z скасовує заміну; «Зберегти» записує конспект.')
        self.apply_button.setEnabled(False)

    def _answer_matches_editor(self):
        return self._answer_target is not None and self._answer_target == (self._note_id, self.note_body.toPlainText())

    def _ai_failed(self, message):
        self.status.setText(message)
        self.ai_status.setText(message + ' Відкрий вкладку «Безоплатний ШІ» для налаштування.')

    def _set_busy(self, busy):
        for control in (self.send_button, self.structure_button, self.explain_button, self.probe_button):
            control.setEnabled(not busy)
        self.stop_button.setEnabled(busy)

    def _stop(self):
        if self._worker:
            self._worker.cancel.set()
            self.status.setText('Зупиняємо запит…')
            self.stop_button.setEnabled(False)

    def _worker_finished(self):
        worker, self._worker = self._worker, None
        if worker:
            worker.deleteLater()
        self._set_busy(False)
        if self._close_pending:
            self.ready_to_close.emit()

    def _clear_chat(self):
        self._generation += 1
        self._stop()
        self.transcript.clear()
        self.question.clear()
        self._history.clear()
        self._last_answer = ''
        self._answer_target = None
        self.apply_button.setEnabled(False)

    def can_leave(self):
        return self.save_note()

    def lock(self):
        if not self.save_note():
            return False
        self._clear_chat()
        self.vault.lock()
        self._set_editor()
        with QSignalBlocker(self.notes):
            self.notes.clear()
        self.password.clear()
        self.password_confirm.clear()
        self.stack.setCurrentIndex(0)
        self._refresh_gate()
        return True

    def hideEvent(self, event):
        if self.vault.unlocked:
            self.lock()
        super().hideEvent(event)

    def request_close(self):
        if not self.lock():
            return False
        if self._worker is not None:
            self._close_pending = True
            return False
        return True
