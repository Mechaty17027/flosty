"""Modeless local release history and explicitly configured update checks."""
from __future__ import annotations

import threading
import hmac
import re
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QSettings, QStandardPaths, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QTabWidget, QVBoxLayout, QWidget,
)

from mechahub import __version__
from mechahub.core.updates import (
    DEFAULT_MANIFEST_URL, LOCAL_HISTORY, MAX_ARTIFACT_BYTES, ReleaseManifest, UpdateCancelled, UpdateError,
    fetch_manifest, is_newer_version, select_artifact, validate_manifest_url,
)
from mechahub.core.updates_download import ArtifactDownloadResult, download_release
from mechahub.core.updates_install import detect_installation

MANIFEST_SETTING = 'updates/manifest_url'
AUTOMATIC_SETTING = 'updates/automatic'
AUTOMATIC_DOWNLOAD_SETTING = 'updates/automatic_download'


def _label(text: str, kind: str = 'Muted') -> QLabel:
    item = QLabel(text)
    item.setObjectName(kind)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(True)
    item.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return item


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName('Card')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(10)
    return frame, layout


class UpdateCheckThread(QThread):
    received = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = url
        self.cancel_event = threading.Event()

    def stop(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            manifest = fetch_manifest(self.url, self.cancel_event)
            if self.cancel_event.is_set():
                self.stopped.emit()
            else:
                self.received.emit(manifest)
        except UpdateCancelled:
            self.stopped.emit()
        except Exception as error:
            if self.cancel_event.is_set():
                self.stopped.emit()
            else:
                self.failed.emit(str(error))


class ArtifactDownloadThread(QThread):
    progress_changed = Signal(int, object)
    downloaded = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, manifest: ReleaseManifest, folder: Path, parent=None):
        super().__init__(parent)
        self.manifest = manifest
        self.folder = folder
        self.cancel_event = threading.Event()

    def stop(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            result = download_release(
                self.manifest, self.folder, self.cancel_event, self.progress_changed.emit,
            )
            self.downloaded.emit(result)
        except UpdateCancelled:
            self.stopped.emit()
        except Exception as error:
            if self.cancel_event.is_set():
                self.stopped.emit()
            else:
                self.failed.emit(str(error))


class UpdatesDialog(QDialog):
    ready_to_close = Signal()
    artifact_ready = Signal(object)
    download_ready = Signal(str)
    update_available = Signal(object)
    install_requested = Signal(object, object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle('MechaHub · Оновлення та історія змін')
        self.setModal(False)
        self.resize(780, 650)
        self.setMinimumSize(580, 520)
        self.settings = QSettings('MechaHub', 'MechaHub')
        self._auto_preference = self.settings.value(AUTOMATIC_SETTING, True, type=bool)
        self._automatic_download_preference = self.settings.value(
            AUTOMATIC_DOWNLOAD_SETTING, self._auto_preference, type=bool,
        )
        self.worker: UpdateCheckThread | ArtifactDownloadThread | None = None
        self.manifest: ReleaseManifest | None = None
        self.download_result: ArtifactDownloadResult | None = None
        self.installation_context = detect_installation()
        self._selected_artifact: ReleaseManifest | None = None
        self._download_manifest: ReleaseManifest | None = None
        self._verified_manifest: ReleaseManifest | None = None
        self._pending_download: ReleaseManifest | None = None
        self._announced_versions: set[str] = set()
        self._install_pending = False
        self._close_pending = False
        self._pending_result = QDialog.DialogCode.Rejected
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 18)
        outer.setSpacing(14)
        self.current_version_label = _label(f'Встановлена версія: {__version__}', 'Section')
        outer.addWidget(self.current_version_label)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        history_scroll = QScrollArea()
        history_scroll.setWidgetResizable(True)
        history_scroll.setFrameShape(QFrame.Shape.NoFrame)
        history_content = QWidget()
        history = QVBoxLayout(history_content)
        history.setContentsMargins(12, 12, 16, 12)
        history.setSpacing(12)
        for version, notes in LOCAL_HISTORY:
            card, layout = _card()
            if version == LOCAL_HISTORY[0][0]:
                card.setProperty('accent', 'purple')
            heading = f'MechaHub {version}'
            if version == __version__:
                heading += ' · поточна версія'
            layout.addWidget(_label(heading, 'Section'))
            layout.addWidget(_label('\n'.join(f'• {note}' for note in notes)))
            history.addWidget(card)
        history.addStretch()
        history_scroll.setWidget(history_content)
        self.tabs.addTab(history_scroll, 'Історія змін')

        online_scroll = QScrollArea()
        online_scroll.setWidgetResizable(True)
        online_scroll.setFrameShape(QFrame.Shape.NoFrame)
        online_content = QWidget()
        online_layout = QVBoxLayout(online_content)
        online_layout.setContentsMargins(12, 12, 16, 12)
        online_layout.setSpacing(12)

        self.release_card, release_layout = _card()
        self.release_card.setProperty('accent', 'purple')
        self.release_title = _label('Оновлення MechaHub', 'Section')
        release_layout.addWidget(self.release_title)
        online_layout.addWidget(self.release_card)

        source_card, source_layout = _card()
        source_card.setProperty('accent', 'cyan')
        source_layout.addWidget(_label('Налаштування оновлень', 'Section'))
        source_layout.addWidget(_label(
            'MechaHub перевіряє наявність нової версії та пропонує оновлення. '
            'Нову версію можна завантажити заздалегідь; '
            'встановлення починається кнопкою «Встановити й перезапустити». '
            'Конспекти, пароль і налаштування збережуться.'
        ))
        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText('HTTPS-адреса інформації про версію')
        self.source_input.setAccessibleName('HTTPS-адреса джерела оновлень')
        self.source_input.setToolTip(
            'Типове джерело — GitHub Mechaty17027/flosty. '
            'Можна зберегти іншу HTTPS-адресу або очистити поле, щоб вимкнути перевірки. '
            'Джерело: HTTPS-адреса JSON до 64 КіБ. '
            'Поля: version — числова версія через крапки, '
            'download_url — HTTPS-посилання на файл, sha256 — контрольна сума з 64 символів, '
            'size — розмір до 512 МіБ, notes — опис змін.'
        )
        # An explicitly saved empty string disables checks and must not be
        # replaced by the published default on the next launch.
        stored_url = self.settings.value(MANIFEST_SETTING, DEFAULT_MANIFEST_URL, type=str)
        self.source_input.setText(stored_url)
        self.source_input.setCursorPosition(0)
        self.source_input.setClearButtonEnabled(True)
        self.source_input.textChanged.connect(self._source_edited)
        source_layout.addWidget(self.source_input)
        self.auto_checkbox = QCheckBox('Автоматично перевіряти наявність нової версії')
        self.auto_checkbox.setToolTip('MechaHub повідомить про нову версію; встановлення починається після твого підтвердження.')
        self.auto_checkbox.setChecked(bool(stored_url) and self._auto_preference)
        self.auto_checkbox.toggled.connect(self._automatic_changed)
        source_layout.addWidget(self.auto_checkbox)
        self.auto_download_checkbox = QCheckBox('Автоматично завантажувати оновлення')
        self.auto_download_checkbox.setToolTip(
            'Після перевірки MechaHub завантажить нову версію та перевірить файл. '
            'Встановлення починається лише після твого підтвердження.'
        )
        self.auto_download_checkbox.setChecked(bool(stored_url) and self._automatic_download_preference)
        self.auto_download_checkbox.toggled.connect(self._automatic_download_changed)
        source_layout.addWidget(self.auto_download_checkbox)
        actions = QHBoxLayout()
        self.save_button = QPushButton('Зберегти джерело')
        self.save_button.clicked.connect(self._save_source)
        self.check_button = QPushButton('Перевірити оновлення')
        self.check_button.setObjectName('Primary')
        self.check_button.clicked.connect(self._check)
        self.cancel_button = QPushButton('Зупинити')
        self.cancel_button.clicked.connect(self._stop)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.save_button)
        actions.addStretch()
        source_layout.addLayout(actions)
        self.status_label = _label('Джерело не задане. Вкажи його адресу для перевірки.' if not stored_url else
                                   'Джерело збережене. Натисни «Перевірити оновлення».')
        release_layout.addWidget(self.status_label)
        self.download_progress = QProgressBar()
        self.download_progress.hide()
        release_layout.addWidget(self.download_progress)
        self.progress_label = _label('')
        self.progress_label.hide()
        release_layout.addWidget(self.progress_label)
        self.remote_notes = _label('')
        self.remote_notes.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.remote_notes.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.remote_notes.hide()
        self.notes_scroll = QScrollArea()
        self.notes_scroll.setWidgetResizable(True)
        self.notes_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.notes_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.notes_scroll.setMaximumHeight(120)
        self.notes_scroll.setWidget(self.remote_notes)
        self.notes_scroll.hide()
        release_actions = QHBoxLayout()
        self.download_button = QPushButton('Завантажити оновлення')
        self.download_button.setObjectName('Primary')
        self.download_button.clicked.connect(self.begin_update)
        self.download_button.hide()
        release_actions.addWidget(self.download_button)
        self.install_button = QPushButton('Встановити й перезапустити')
        self.install_button.setObjectName('Primary')
        self.install_button.clicked.connect(self._request_install)
        self.install_button.hide()
        release_actions.addWidget(self.install_button)
        # Keep the installation action before notes, including long descriptions.
        release_layout.insertLayout(2, release_actions)
        release_layout.addWidget(self.notes_scroll)
        self.open_button = QPushButton('Відкрити сторінку версії')
        self.open_button.clicked.connect(self._open_release)
        self.open_button.hide()
        release_layout.addWidget(self.open_button)
        self.open_folder_button = QPushButton('Відкрити папку завантажень')
        self.open_folder_button.clicked.connect(self._open_download_folder)
        self.open_folder_button.hide()
        release_layout.addWidget(self.open_folder_button)
        online_layout.addWidget(source_card)
        online_layout.addStretch()
        online_scroll.setWidget(online_content)
        online_page = QWidget()
        online_page_layout = QVBoxLayout(online_page)
        online_page_layout.setContentsMargins(0, 0, 0, 0)
        online_page_layout.setSpacing(8)
        check_actions = QHBoxLayout()
        check_actions.addStretch()
        check_actions.addWidget(self.check_button)
        check_actions.addWidget(self.cancel_button)
        online_page_layout.addLayout(check_actions)
        online_page_layout.addWidget(online_scroll, 1)
        self.tabs.addTab(online_page, 'Перевірка онлайн')
        self.tabs.setCurrentIndex(0)
        close = QPushButton('Пізніше')
        close.clicked.connect(self.close)
        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(close)
        outer.addLayout(bottom)
        self._update_controls()

    def _source_edited(self) -> None:
        self.manifest = None
        self.download_result = None
        self._selected_artifact = None
        self._download_manifest = None
        self._verified_manifest = None
        self._pending_download = None
        self._install_pending = False
        if self.worker is not None:
            self.worker.stop()
        self.open_button.hide()
        self.open_folder_button.hide()
        self.download_button.hide()
        self.install_button.hide()
        self.download_progress.hide()
        self.progress_label.hide()
        self.remote_notes.hide()
        self.notes_scroll.hide()
        self.release_title.setText('Оновлення MechaHub')
        self.status_label.setText('Збережи адресу джерела або натисни «Перевірити оновлення».')
        self._update_controls()

    def _valid_source(self) -> bool:
        try:
            validate_manifest_url(self.source_input.text().strip())
            return True
        except UpdateError:
            return False

    def _automatic_changed(self, checked: bool) -> None:
        self._auto_preference = checked
        self._save_source()

    def _automatic_download_changed(self, checked: bool) -> None:
        self._automatic_download_preference = checked
        self._save_source()

    def _save_source(self, _checked: bool = False, *, quiet: bool = False) -> bool:
        if self.worker is not None or self._close_pending:
            return False
        url = self.source_input.text().strip()
        try:
            if url:
                url = validate_manifest_url(url)
        except UpdateError as error:
            self.status_label.setText(str(error))
            self.source_input.setFocus()
            return False
        self.settings.setValue(MANIFEST_SETTING, url)
        self.settings.setValue(AUTOMATIC_SETTING, bool(url) and self._auto_preference)
        self.settings.setValue(AUTOMATIC_DOWNLOAD_SETTING, bool(url) and self._automatic_download_preference)
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            self.status_label.setText('Не вдалося зберегти адресу джерела. Перевір доступ до налаштувань.')
            return False
        if not quiet:
            self.status_label.setText('Джерело збережене. Перевірка запускається кнопкою.' if url else
                                      'Джерело очищене. Онлайн-перевірка поки недоступна.')
        return True

    def _check(self) -> None:
        if self.worker is not None or self._close_pending or self._install_pending:
            return
        if not self.source_input.text().strip():
            self.status_label.setText('Джерело оновлень не задане. Вкажи HTTPS-адресу від розробника.')
            self.source_input.setFocus()
            return
        if not self._save_source(quiet=True):
            return
        self.manifest = None
        self.download_result = None
        self._selected_artifact = None
        self._verified_manifest = None
        self._download_manifest = None
        self._pending_download = None
        self._install_pending = False
        self.download_button.hide()
        self.install_button.hide()
        self.open_button.hide()
        self.open_folder_button.hide()
        self.download_progress.hide()
        self.progress_label.hide()
        self.remote_notes.hide()
        self.notes_scroll.hide()
        self.release_title.setText('Перевірка версії')
        self.status_label.setText('Отримуємо інформацію про версію…')
        self.worker = UpdateCheckThread(self.source_input.text().strip(), self)
        self.worker.received.connect(self._received)
        self.worker.failed.connect(self._failed)
        self.worker.stopped.connect(self._stopped)
        self.worker.finished.connect(self._worker_finished)
        self._update_controls()
        self.worker.start()

    def _update_controls(self) -> None:
        idle = self.worker is None and not self._close_pending and not self._install_pending
        for control in (self.source_input, self.save_button, self.check_button):
            control.setEnabled(idle)
        self.cancel_button.setEnabled(self.worker is not None and not self.worker.cancel_event.is_set())
        valid_source = self._valid_source()
        self.auto_checkbox.blockSignals(True)
        self.auto_checkbox.setChecked(valid_source and self._auto_preference)
        self.auto_checkbox.blockSignals(False)
        self.auto_checkbox.setEnabled(idle and valid_source)
        self.auto_download_checkbox.blockSignals(True)
        self.auto_download_checkbox.setChecked(valid_source and self._automatic_download_preference)
        self.auto_download_checkbox.blockSignals(False)
        self.auto_download_checkbox.setEnabled(idle and valid_source)
        selected = self._selected_artifact is not None and self._new_release()
        ready = self._result_matches()
        self.download_button.setEnabled(idle and selected and not ready)
        self.install_button.setEnabled(idle and ready and self.installation_context.mode != 'source')

    def _new_release(self) -> bool:
        try:
            return self.manifest is not None and is_newer_version(self.manifest.version, __version__)
        except UpdateError:
            return False

    def _result_matches(self) -> bool:
        """Bind a verified download to the exact artifact selected for this app.

        The installation core hashes the file again before staging it. Here we
        also prevent UI reuse after source/version/artifact changes.
        """
        selected, result = self._selected_artifact, self.download_result
        if (selected is None or result is None or self._verified_manifest != selected
                or not self._new_release() or not result.path.is_file()
                or not hmac.compare_digest(result.sha256.lower(), selected.sha256.lower())):
            return False
        return (0 < result.bytes_received <= MAX_ARTIFACT_BYTES
                and (selected.size is None or result.bytes_received == selected.size))

    def _select_download(self, manifest: ReleaseManifest) -> ReleaseManifest:
        artifact = select_artifact(manifest, installed=self.installation_context.mode == 'installed')
        validate_manifest_url(artifact.download_url)
        if not re.fullmatch(r'[a-fA-F0-9]{64}', artifact.sha256):
            raise UpdateError('Джерело не надало контрольну суму файла оновлення.')
        if Path(urlsplit(artifact.download_url).path).suffix.lower() != '.exe':
            raise UpdateError('Для оновлення у програмі потрібен файл MechaHub для Windows.')
        if artifact.size is not None and (type(artifact.size) is not int
                                         or not 1 <= artifact.size <= MAX_ARTIFACT_BYTES):
            raise UpdateError('Джерело повідомило некоректний розмір файла оновлення.')
        return artifact

    def _received(self, manifest: ReleaseManifest) -> None:
        if self._close_pending or (self.worker and self.worker.cancel_event.is_set()):
            return
        self.manifest = manifest
        self.release_title.setText(f'MechaHub {manifest.version}')
        self.download_result = None
        self._selected_artifact = None
        self._verified_manifest = None
        self._download_manifest = None
        self._pending_download = None
        self.download_button.hide()
        self.install_button.hide()
        self.open_folder_button.hide()
        if is_newer_version(manifest.version, __version__):
            self.status_label.setText(f'Доступна версія {manifest.version}. Встановлена: {__version__}.')
            try:
                self._selected_artifact = self._select_download(manifest)
                self.download_button.show()
                self.open_button.hide()
                if self.installation_context.mode == 'source':
                    self.status_label.setText(self.status_label.text() + '\nОновлення у програмі доступне '
                                              'у встановленій або переносній версії MechaHub.')
            except UpdateError as error:
                self.status_label.setText(
                    self.status_label.text() + '\n' + str(error) + '\nМожна відкрити сторінку версії.'
                )
                self.open_button.show()
            if manifest.version not in self._announced_versions:
                self._announced_versions.add(manifest.version)
                self.update_available.emit(manifest)
            if (self._selected_artifact is not None and self._automatic_download_preference
                    and self.installation_context.mode != 'source'):
                self._pending_download = self._selected_artifact
        else:
            self.status_label.setText(
                f'Джерело повідомило про версію {manifest.version}. '
                f'Вона не новіша за встановлену {__version__}.'
            )
            self.open_button.hide()
        self.remote_notes.setText(manifest.notes)
        self.remote_notes.setVisible(bool(manifest.notes))
        self.notes_scroll.setVisible(bool(manifest.notes))
        self.notes_scroll.verticalScrollBar().setValue(0)
        self._update_controls()
        if self.worker is None and self._pending_download is not None:
            artifact = self._pending_download
            self._pending_download = None
            self._start_download(artifact)

    def _failed(self, message: str) -> None:
        if not self._close_pending:
            self.status_label.setText('Не вдалося завершити оновлення: ' + message)
            self.download_progress.hide()
            self.progress_label.hide()

    def _stopped(self) -> None:
        if not self._close_pending:
            self.status_label.setText('Оновлення зупинено. Можна повторити пізніше.')
            self.download_progress.hide()
            self.progress_label.hide()

    def _stop(self) -> None:
        self._pending_download = None
        if self.worker is not None:
            self.worker.stop()
            self.status_label.setText('Завершуємо поточний мережевий запит…')
            self._update_controls()

    def _worker_finished(self) -> None:
        old = self.worker
        self.worker = None
        if old is not None:
            old.deleteLater()
        self._update_controls()
        if self._close_pending:
            super().done(self._pending_result)
            self.ready_to_close.emit()
        elif self._pending_download is not None:
            artifact = self._pending_download
            self._pending_download = None
            self._start_download(artifact)

    def show_update_offer(self) -> None:
        self.tabs.setCurrentIndex(1)
        self.show()
        self.raise_()
        self.activateWindow()

    def begin_update(self) -> bool:
        """Download only after the user chooses the offered update."""
        if (self.worker is not None or self._close_pending or self._install_pending
                or self._selected_artifact is None or not self._new_release() or self._result_matches()):
            return False
        self._start_download(self._selected_artifact)
        return self.worker is not None

    def _start_download(self, manifest: ReleaseManifest) -> None:
        if self.worker is not None or self._close_pending or self._install_pending:
            return
        if manifest != self._selected_artifact or not self._new_release():
            return
        self.download_result = None
        self._verified_manifest = None
        self._download_manifest = manifest
        self.install_button.hide()
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        folder = (Path(downloads) if downloads else Path.home() / 'Downloads') / 'MechaHub'
        self.status_label.setText(f'Завантажуємо версію {manifest.version}…')
        self.download_progress.setRange(0, 0)
        self.download_progress.show()
        self.progress_label.setText('Отримуємо файл; після завантаження перевіримо його цілісність.')
        self.progress_label.show()
        self.worker = ArtifactDownloadThread(manifest, folder, self)
        self.worker.progress_changed.connect(self._download_progress)
        self.worker.downloaded.connect(self._downloaded)
        self.worker.failed.connect(self._failed)
        self.worker.stopped.connect(self._stopped)
        self.worker.finished.connect(self._worker_finished)
        self._update_controls()
        self.worker.start()

    def _download_progress(self, received: int, total: int | None) -> None:
        if self._close_pending:
            return
        if total:
            self.download_progress.setRange(0, total)
            self.download_progress.setValue(received)
            self.progress_label.setText(f'{received / (1024 * 1024):.1f} із {total / (1024 * 1024):.1f} МіБ')
        else:
            self.download_progress.setRange(0, 0)
            self.progress_label.setText(f'Отримано {received / (1024 * 1024):.1f} МіБ')

    def _downloaded(self, result: ArtifactDownloadResult) -> None:
        if (self._close_pending or (self.worker is not None and self.worker.cancel_event.is_set())
                or self._download_manifest is None or self._download_manifest != self._selected_artifact):
            return
        self.download_result = result
        self._verified_manifest = self._download_manifest
        if not self._result_matches():
            self.download_result = None
            self._verified_manifest = None
            self._failed('Файл не відповідає вибраному оновленню. Завантаж його повторно.')
            return
        self.download_progress.setRange(0, 100)
        self.download_progress.setValue(100)
        self.status_label.setText('Нову версію завантажено; цілісність файла підтверджено.')
        self.progress_label.setText(f'Завантажено {result.bytes_received / (1024 * 1024):.1f} МіБ. '
                                    'Натисни «Встановити й перезапустити», коли буде зручно.'
                                    if self.installation_context.mode != 'source' else
                                    'Файл завантажено. Відкрий папку завантажень.')
        self.install_button.setVisible(self.installation_context.mode != 'source')
        self.download_button.hide()
        self.open_folder_button.show()
        self.artifact_ready.emit(result)
        self.download_ready.emit(str(result.path))
        self._update_controls()

    def _request_install(self) -> None:
        if (self.worker is not None or self._close_pending or self._install_pending
                or self.installation_context.mode == 'source'):
            return
        if not self._result_matches():
            if self._selected_artifact is not None and self._new_release():
                self.download_result = None
                self._verified_manifest = None
                self.install_button.hide()
                self.open_folder_button.hide()
                self.download_button.show()
                self.status_label.setText('Файл оновлення недоступний. Завантаж його повторно.')
                self._update_controls()
            return
        answer = QMessageBox.question(
            self, 'Оновити MechaHub',
            f'Встановити MechaHub {self.manifest.version}?\n\n'
            'Програма збереже відкриті конспекти, закриється на час оновлення '
            'та запуститься знову. Конспекти, пароль і налаштування збережуться.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # Recheck after the modal prompt: event processing can deliver a close,
        # a source edit, or deletion of the saved download while it is open.
        if (self.worker is not None or self._close_pending or not self._result_matches()):
            return
        self._install_pending = True
        self.status_label.setText('Зберігаємо роботу та готуємо оновлення…')
        self._update_controls()
        self.install_requested.emit(self.manifest, self.download_result)

    def installation_failed(self, message: str) -> None:
        self._install_pending = False
        self._close_pending = False
        self.status_label.setText(message)
        self._update_controls()

    def _open_download_folder(self) -> None:
        if self.download_result is not None:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.download_result.path.parent))):
                self.status_label.setText('Не вдалося відкрити папку завантажень.')

    def begin_automatic_check(self) -> bool:
        """Called by the host at startup/open; requires an enabled valid source."""
        if not self.auto_checkbox.isChecked() or not self._valid_source():
            return False
        if self.worker is not None or self._install_pending:
            return False
        # Closing the modeless window dismisses an offer. It must not disable
        # the host's later periodic checks; the host guards application shutdown.
        self._close_pending = False
        self._pending_result = QDialog.DialogCode.Rejected
        self._check()
        return self.worker is not None

    def _open_release(self) -> None:
        if self.manifest is not None and is_newer_version(self.manifest.version, __version__):
            if not QDesktopServices.openUrl(QUrl(self.manifest.download_url)):
                self.status_label.setText('Не вдалося відкрити браузер. Спробуй ще раз.')

    def request_close(self) -> bool:
        """True once no worker is owned, including queued finished signals."""
        self._close_pending = True
        if self.worker is not None:
            self._stop()
            self._update_controls()
            return False
        return True

    def done(self, result: int) -> None:
        self._pending_result = result
        if self.request_close():
            super().done(result)

    def closeEvent(self, event) -> None:
        if not self.request_close():
            event.ignore()
            return
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        if self.worker is None:
            self._close_pending = False
            self._pending_result = QDialog.DialogCode.Rejected
            self._update_controls()
        super().showEvent(event)
