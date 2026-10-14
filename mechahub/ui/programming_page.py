"""A small code workspace with asynchronous user-owned processes."""
from __future__ import annotations

import codecs
import json
import os
from pathlib import Path
import re

import psutil
from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton,
    QTabWidget, QVBoxLayout, QWidget,
)

from mechahub.core.code_workspace import (
    CodeWorkspace, CodeWorkspaceError, LanguageProfile,
    command_working_directory, find_compiler, find_python, validate_profile,
)

MAX_OUTPUT_CHARS = 200_000


def _label(text, name='Muted'):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def _button(text, callback):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    return widget


class CodeHighlighter(QSyntaxHighlighter):
    PYTHON = 'False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield'.split()
    CPP = 'alignas alignof auto bool break case catch char class const constexpr continue default delete do double else enum explicit false float for friend if inline int long namespace new nullptr private protected public return short signed sizeof static struct switch template this throw true try typedef typename union unsigned using virtual void volatile while'.split()

    def __init__(self, document):
        super().__init__(document)
        self.language = 'Python'
        self.formats = {}
        for name, color in [('keyword', '#C9A7FF'), ('string', '#9BDCB0'), ('number', '#F4C781'), ('comment', '#7F91AB')]:
            form = QTextCharFormat()
            form.setForeground(QColor(color))
            if name == 'keyword':
                form.setFontWeight(QFont.Weight.Bold)
            self.formats[name] = form

    def set_language(self, language):
        self.language = language
        self.rehighlight()

    def highlightBlock(self, text):
        self.setCurrentBlockState(0)
        words = self.PYTHON if self.language == 'Python' else self.CPP if self.language == 'C++' else []
        if words:
            for match in re.finditer(r'\b(?:' + '|'.join(words) + r')\b', text):
                self.setFormat(match.start(), len(match.group()), self.formats['keyword'])
        for match in re.finditer(r'\b(?:0[xX][\da-fA-F]+|\d+(?:\.\d+)?)\b', text):
            self.setFormat(match.start(), len(match.group()), self.formats['number'])
        spans = []
        for match in re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', text):
            spans.append((match.start(), match.end()))
            self.setFormat(match.start(), len(match.group()), self.formats['string'])
        marker = '#' if self.language != 'C++' else '//'
        position = text.find(marker)
        while position >= 0 and any(start <= position < end for start, end in spans):
            position = text.find(marker, position + len(marker))
        if position >= 0:
            self.setFormat(position, len(text) - position, self.formats['comment'])
        delimiters = [('/*', '*/', 1, 'comment')] if self.language == 'C++' else [("'''", "'''", 2, 'string'), ('"""', '"""', 3, 'string')]
        for start_marker, end_marker, state, style in delimiters:
            start = 0 if self.previousBlockState() == state else text.find(start_marker)
            while start >= 0:
                search = start if self.previousBlockState() == state and start == 0 else start + len(start_marker)
                end = text.find(end_marker, search)
                if end < 0:
                    self.setCurrentBlockState(state)
                    length = len(text) - start
                else:
                    length = end + len(end_marker) - start
                self.setFormat(start, length, self.formats[style])
                start = -1 if end < 0 else text.find(start_marker, end + len(end_marker))


class LanguageDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Додати мову програмування')
        self.resize(640, 400)
        root = QVBoxLayout(self)
        root.addWidget(_label('Вибери встановлений інтерпретатор. Команди — JSON-масиви аргументів; доступні {file}, {folder}, {output}.'))
        form = QFormLayout()
        self.name = QLineEdit()
        self.extension = QLineEdit('.js')
        self.executable = QLineEdit()
        self.executable.setPlaceholderText('Шлях інтерпретатора або {output} після компіляції')
        row = QHBoxLayout()
        row.addWidget(self.executable)
        row.addWidget(_button('Файл…', self.choose_executable))
        self.arguments = QLineEdit('["{file}"]')
        self.compile = QLineEdit()
        self.compile.setPlaceholderText('Необов’язково: ["C:/compiler.exe", "{file}", "-o", "{output}"]')
        form.addRow('Назва', self.name)
        form.addRow('Розширення', self.extension)
        form.addRow('Програма запуску', row)
        form.addRow('Аргументи запуску', self.arguments)
        form.addRow('Команда компіляції', self.compile)
        root.addLayout(form)
        self.error = _label('')
        root.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Додати')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Скасувати')
        buttons.accepted.connect(self.check)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.profile = None

    def choose_executable(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Встановлений інтерпретатор', '', 'Програми (*.exe);;Усі файли (*)')
        if path:
            self.executable.setText(path)

    def check(self):
        try:
            arguments = json.loads(self.arguments.text())
            if not isinstance(arguments, list):
                raise CodeWorkspaceError('Аргументи запуску мають бути JSON-масивом.')
            compile_command = json.loads(self.compile.text()) if self.compile.text().strip() else None
            self.profile = validate_profile(LanguageProfile(self.name.text(), self.extension.text(),
                                                            [self.executable.text(), *arguments], compile_command))
            if self.profile.run[0] != '{output}' and not Path(self.executable.text()).is_file():
                raise CodeWorkspaceError('Вибери встановлений виконуваний файл.')
            self.accept()
        except (ValueError, OSError) as error:
            self.error.setText(str(error))


class ProgrammingPage(QWidget):
    ready_to_close = Signal()
    open_source = Signal(str)

    def __init__(self, parent=None, *, workspace_dir: Path | None = None, settings_path: Path | None = None):
        super().__init__(parent)
        self.workspace = CodeWorkspace(workspace_dir, settings_path)
        self.current_path: Path | None = None
        self.process: QProcess | None = None
        self._phase = ''
        self._run_command = None
        self._tool_directories = []
        self._stopping = False
        self._close_pending = False
        self._owned_identity = None
        self._output_chars = 0
        self._output_truncated = False
        self._decoders = {}
        self._build_ui()
        self._refresh_languages()
        if self.workspace.load_error:
            self.status.setText(self.workspace.load_error)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_label('Програмування · твої ідеї', 'AccentTitle'))
        root.addWidget(_label('Власний код запускається з твоїми правами Windows. Зберігай і запускай код, якому довіряєш.'))
        toolbar = QHBoxLayout()
        self.language = QComboBox()
        self.language.currentIndexChanged.connect(self._language_changed)
        self.new_button = _button('Новий проєкт', self.new_project)
        self.open_button = _button('Відкрити', self.open_file)
        self.save_button = _button('Зберегти', self.save_code)
        self.save_as_button = _button('Зберегти як…', self.save_as)
        self.run_button = _button('▶ Запустити', self.run_code)
        self.run_button.setObjectName('Primary')
        self.run_button.setStyleSheet('QPushButton#Primary:disabled { background: #1C2130; border-color: #30374B; color: #748099; }')
        self.stop_button = _button('■ Зупинити', self.stop_code)
        self.stop_button.setEnabled(False)
        for widget in (self.language, self.new_button, self.open_button, self.save_button):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        toolbar.addWidget(self.run_button)
        toolbar.addWidget(self.stop_button)
        root.addLayout(toolbar)
        tools = QHBoxLayout()
        self.tool_status = _label('')
        tools.addWidget(self.tool_status, 1)
        self.tool_button = QPushButton('Інструмент…')
        menu = QMenu(self.tool_button)
        menu.addAction('Вибрати виконуваний файл…', self.choose_tool)
        menu.addAction('Автоматичний вибір Python / C++', self.reset_tool)
        self.tool_button.setMenu(menu)
        tools.addWidget(self.tool_button)
        tools.addWidget(self.save_as_button)
        self.add_language_button = _button('+ Мова', self.add_language)
        tools.addWidget(self.add_language_button)
        root.addLayout(tools)
        self.path_label = _label('Нова ідея · код збережеться у локальній папці проєктів')
        root.addWidget(self.path_label)
        self.tabs = QTabWidget()
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont('Consolas', 11))
        self.editor.setStyleSheet('QPlainTextEdit { font-family: "Consolas"; font-size: 14px; }')
        self.editor.setTabStopDistance(self.editor.fontMetrics().horizontalAdvance(' ') * 4)
        self.editor.setPlaceholderText('Напиши ідею на Python або C++…')
        self.editor.document().modificationChanged.connect(self._modified)
        self.highlighter = CodeHighlighter(self.editor.document())
        self.tabs.addTab(self.editor, 'Редактор')
        output_page = QWidget()
        output_layout = QVBoxLayout(output_page)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont('Consolas', 10))
        self.output.setStyleSheet('QPlainTextEdit { font-family: "Consolas"; font-size: 13px; }')
        self.output.document().setMaximumBlockCount(3000)
        output_layout.addWidget(self.output)
        input_row = QHBoxLayout()
        self.stdin_input = QLineEdit()
        self.stdin_input.setPlaceholderText('Ввід для input() / std::cin; Enter надсилає рядок')
        self.stdin_input.returnPressed.connect(self.send_input)
        self.send_input_button = _button('Надіслати', self.send_input)
        self.stdin_input.setEnabled(False)
        self.send_input_button.setEnabled(False)
        input_row.addWidget(self.stdin_input, 1)
        input_row.addWidget(self.send_input_button)
        output_layout.addLayout(input_row)
        self.tabs.addTab(output_page, 'Вивід і введення')
        root.addWidget(self.tabs, 1)
        self.status = _label('Новий проєкт або відкрий UTF-8 файл. Код зберігається перед запуском.')
        root.addWidget(self.status)

    @property
    def profile(self):
        return self.language.currentData()

    def _refresh_languages(self, selected=None):
        self.language.blockSignals(True)
        self.language.clear()
        for profile in self.workspace.profiles:
            self.language.addItem(profile.name, profile)
        index = self.language.findText(selected or 'Python')
        self.language.setCurrentIndex(max(0, index))
        self.language.blockSignals(False)
        self._language_changed()

    def _language_changed(self, _index=0):
        if self.profile is None:
            return
        self.highlighter.set_language(self.profile.name)
        try:
            if self.profile.name == 'Python':
                tool = find_python(self.workspace.python_executable or None)
                text = (f'{tool.label}: {tool.executable}' if tool else 'Python не знайдено — вибери інтерпретатор.')
                if tool and tool.label == 'Вбудований Python':
                    text = 'Вбудований Python · готовий до роботи.\nСтандартна бібліотека доступна. Для pip, Tk та сторонніх пакетів вибери власний Python.'
            elif self.profile.name == 'C++':
                tool = find_compiler(self.workspace.compiler_executable or None)
                text = f'Компілятор: {tool.executable}' if tool else 'C++: g++ / clang++ не знайдено. Установи компілятор окремо й вибери його.'
            else:
                text = 'Програма мови: ' + self.profile.run[0]
            self.tool_status.setText(text)
        except (ValueError, OSError) as error:
            self.tool_status.setText(str(error))

    def _modified(self, modified):
        text = str(self.current_path) if self.current_path else 'Нова ідея · локальна папка проєктів'
        self.path_label.setText(('● ' if modified else '') + text)

    def save_code(self, _checked=False, *, force=False) -> bool:
        if not force and not self.editor.document().isModified() and self.current_path is not None:
            return True
        if self.current_path is None and not self.editor.toPlainText():
            self.editor.document().setModified(False)
            return True
        try:
            if self.current_path is None:
                self.current_path = self.workspace.create_project(self.profile, 'Ідея')
            self.workspace.save_file(self.current_path, self.editor.toPlainText())
            self.editor.document().setModified(False)
            self._modified(False)
            self.status.setText('Код збережено.')
            return True
        except (OSError, ValueError) as error:
            self.editor.document().setModified(True)
            self.status.setText('Код не збережено; чернетка залишається у редакторі. ' + str(error))
            return False

    def can_leave(self) -> bool:
        return self.save_code()

    def new_project(self, name=None):
        if self.process is not None or not self.can_leave():
            return False
        if not isinstance(name, str):
            name, ok = QInputDialog.getText(self, 'Новий проєкт', 'Назва проєкту:', text='Моя ідея')
            if not ok:
                return False
        try:
            path = self.workspace.create_project(self.profile, name)
            self._load_path(path)
            return True
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return False

    def _load_path(self, path: Path):
        content = self.workspace.read_file(path)
        self.current_path = path.resolve()
        self.editor.setPlainText(content)
        self.editor.document().setModified(False)
        self._modified(False)
        self.tabs.setCurrentIndex(0)
        self.status.setText('Файл відкрито. Код збережеться перед запуском.')

    def open_file(self, path=None):
        if self.process is not None or not self.can_leave():
            return False
        if not isinstance(path, (str, Path)):
            value, _ = QFileDialog.getOpenFileName(self, 'Відкрити код UTF-8', str(self.workspace.root), 'Код (*.py *.cpp *.cxx *.cc *.h *.js);;Усі файли (*)')
            if not value:
                return False
            path = value
        path = Path(path)
        try:
            self._load_path(path)
            for profile in self.workspace.profiles:
                if profile.extension == path.suffix.lower() or (profile.name == 'C++' and path.suffix.lower() in {'.cxx', '.cc', '.h', '.hpp'}):
                    self.language.setCurrentIndex(self.language.findText(profile.name))
                    break
            return True
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return False

    def save_as(self):
        value, _ = QFileDialog.getSaveFileName(self, 'Зберегти код', str(self.current_path or self.workspace.root / ('main' + self.profile.extension)), 'Усі файли (*)')
        if not value:
            return False
        previous = self.current_path
        self.current_path = Path(value)
        self.editor.document().setModified(True)
        if self.save_code():
            return True
        self.current_path = previous
        self._modified(True)
        return False

    def choose_tool(self):
        value, _ = QFileDialog.getOpenFileName(self, 'Виконуваний файл інтерпретатора / компілятора', '', 'Програми (*.exe);;Усі файли (*)')
        if not value:
            return
        old_python, old_compiler = self.workspace.python_executable, self.workspace.compiler_executable
        old_profiles = list(self.workspace.profiles)
        try:
            if self.profile.name == 'Python':
                find_python(value)
                self.workspace.python_executable = value
            elif self.profile.name == 'C++':
                find_compiler(value)
                self.workspace.compiler_executable = value
            else:
                profile = validate_profile(LanguageProfile(self.profile.name, self.profile.extension,
                                                            (value, *self.profile.run[1:]), self.profile.compile))
                self.workspace.profiles[self.language.currentIndex()] = profile
            self.workspace.save_settings()
            self._refresh_languages(self.profile.name)
        except (OSError, ValueError) as error:
            self.workspace.python_executable, self.workspace.compiler_executable = old_python, old_compiler
            self.workspace.profiles = old_profiles
            self.status.setText(str(error))

    def reset_tool(self):
        if self.profile.name not in {'Python', 'C++'}:
            return
        field = 'python_executable' if self.profile.name == 'Python' else 'compiler_executable'
        previous = getattr(self.workspace, field)
        setattr(self.workspace, field, '')
        try:
            self.workspace.save_settings()
            self._language_changed()
        except (OSError, ValueError) as error:
            setattr(self.workspace, field, previous)
            self.status.setText(str(error))

    def add_language(self):
        dialog = LanguageDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.workspace.add_language(dialog.profile)
                self._refresh_languages(dialog.profile.name)
            except (OSError, ValueError) as error:
                self.status.setText(str(error))
        dialog.deleteLater()

    def _set_busy(self, busy):
        for control in (self.run_button, self.new_button, self.open_button, self.save_button,
                        self.save_as_button, self.language, self.tool_button, self.add_language_button):
            control.setEnabled(not busy)
        self.stop_button.setEnabled(busy and not self._stopping)
        self.stdin_input.setEnabled(busy and self._phase == 'run' and not self._stopping)
        self.send_input_button.setEnabled(self.stdin_input.isEnabled())

    def run_code(self):
        if self.process is not None or self._close_pending or not self.save_code(force=True) or self.current_path is None:
            return
        try:
            compile_command, self._run_command = self.workspace.build_commands(self.profile, self.current_path)
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            self.tabs.setCurrentIndex(1)
            return
        self._tool_directories = []
        for command in (compile_command, self._run_command):
            if command:
                folder = str(Path(command[0]).parent)
                if folder not in self._tool_directories:
                    self._tool_directories.append(folder)
        self.output.clear()
        self._output_chars = 0
        self._output_truncated = False
        self._stopping = False
        self.tabs.setCurrentIndex(1)
        self._launch(compile_command or self._run_command, 'compile' if compile_command else 'run')

    def _launch(self, command, phase):
        self._phase = phase
        self._owned_identity = None
        self._decoders = {name: codecs.getincrementaldecoder('utf-8')(errors='replace') for name in ('stdout', 'stderr')}
        process = QProcess(self)
        self.process = process
        process.setWorkingDirectory(str(command_working_directory(self.profile, self.current_path,
                                                                  self._run_command, phase)))
        environment = QProcessEnvironment.systemEnvironment()
        path = environment.value('PATH')
        environment.insert('PATH', os.pathsep.join([*self._tool_directories, path]))
        environment.insert('PYTHONIOENCODING', 'utf-8')
        process.setProcessEnvironment(environment)
        process.started.connect(self._started)
        process.readyReadStandardOutput.connect(lambda: self._read_output(process, 'stdout'))
        process.readyReadStandardError.connect(lambda: self._read_output(process, 'stderr'))
        process.finished.connect(lambda code, status: self._finished(process, code, status))
        process.errorOccurred.connect(lambda error: self._process_error(process, error))
        self.status.setText('Компілюємо C++…' if phase == 'compile' else 'Виконуємо збережений код. За потреби введи дані на вкладці виводу.')
        self._set_busy(True)
        process.start(command[0], command[1:])

    def _started(self):
        if self.process is None:
            return
        try:
            owned = psutil.Process(int(self.process.processId()))
            self._owned_identity = owned.pid, owned.create_time()
        except psutil.Error:
            self._owned_identity = None
        if self._stopping:
            self.stop_code()

    def _append_output(self, text):
        remaining = MAX_OUTPUT_CHARS - self._output_chars
        if remaining > 0:
            text = text[:remaining]
            cursor = self.output.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(text)
            self.output.setTextCursor(cursor)
            self.output.ensureCursorVisible()
            self._output_chars += len(text)
        if self._output_chars >= MAX_OUTPUT_CHARS and not self._output_truncated:
            self._output_truncated = True
            cursor = self.output.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText('\n[Вивід обмежено 200 000 символами; процес можна зупинити.]\n')

    def _read_output(self, process, channel):
        if self.process is not process:
            return
        old = process.readChannel()
        process.setReadChannel(QProcess.ProcessChannel.StandardOutput if channel == 'stdout' else QProcess.ProcessChannel.StandardError)
        for _chunk in range(16):
            data = bytes(process.read(64 * 1024))
            if not data:
                break
            if not self._output_truncated:
                self._append_output(self._decoders[channel].decode(data))
        pending = process.bytesAvailable() > 0
        process.setReadChannel(old)
        if pending:
            QTimer.singleShot(0, lambda: self._read_output(process, channel))

    def send_input(self):
        if self.process is None or self._phase != 'run' or self._stopping:
            return
        value = self.stdin_input.text()
        if len(value) > 16_000:
            self.status.setText('Один рядок введення має містити до 16 000 символів.')
            return
        if self.process.write((value + '\n').encode('utf-8')) < 0:
            self.status.setText('Програма вже не приймає введення.')
            return
        self.stdin_input.clear()

    def _process_error(self, process, error):
        if self.process is not process:
            return
        if error == QProcess.ProcessError.FailedToStart:
            self._append_output('Не вдалося запустити процес: ' + process.errorString() + '\n')
            self._release_process(process)
            self.status.setText('Інструмент не запустився. Перевір виконуваний файл і налаштування мови.')

    def _finished(self, process, code, exit_status):
        if self.process is not process:
            return
        self._read_output(process, 'stdout')
        self._read_output(process, 'stderr')
        for decoder in self._decoders.values():
            self._append_output(decoder.decode(b'', final=True))
        phase = self._phase
        success = code == 0 and exit_status == QProcess.ExitStatus.NormalExit and not self._stopping
        self._release_process(process, notify=False)
        if phase == 'compile' and success and not self._close_pending:
            self._launch(self._run_command, 'run')
            return
        self.status.setText('Виконання зупинено.' if self._stopping else
                            f'Компіляція завершилася з кодом {code}.' if phase == 'compile' else
                            f'Виконання завершилося з кодом {code}.')
        if self._close_pending:
            self.ready_to_close.emit()

    def _release_process(self, process, *, notify=True):
        self.process = None
        self._owned_identity = None
        process.deleteLater()
        self._set_busy(False)
        if notify and self._close_pending:
            self.ready_to_close.emit()

    def stop_code(self):
        if self.process is None:
            return
        self._stopping = True
        self.status.setText('Зупиняємо власний процес…')
        self._set_busy(True)
        if self._owned_identity:
            try:
                parent = psutil.Process(self._owned_identity[0])
                if parent.create_time() == self._owned_identity[1]:
                    children = [(child, child.create_time()) for child in parent.children(recursive=True)]
                    for child, created in reversed(children):
                        try:
                            if child.create_time() == created:
                                child.kill()
                        except psutil.Error:
                            pass
            except psutil.Error:
                pass
        process = self.process
        process.terminate()
        QTimer.singleShot(1500, lambda: process.kill() if self.process is process and process.state() != QProcess.ProcessState.NotRunning else None)

    def request_close(self) -> bool:
        if not self.can_leave():
            return False
        if self.process is not None:
            self._close_pending = True
            self.stop_code()
            return False
        self._close_pending = False
        return True

    def closeEvent(self, event):
        if not self.request_close():
            event.ignore()
            return
        super().closeEvent(event)
