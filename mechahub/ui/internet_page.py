"""An embedded, tabbed browser; the WebEngine runtime starts on first use."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, QStandardPaths, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
    QSizePolicy, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)
from shiboken6 import delete, isValid

from mechahub.core.web import web_target
from mechahub.ui.icons import icon


HOME_HTML = '''<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MechaHub — Нова вкладка</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center;
  background: radial-gradient(ellipse at 30% 15%, #28213e, transparent 65%), #111622;
  color: #eff0f9; font: 15px/1.6 "Segoe UI", sans-serif; }
main { max-width: 660px; padding: 36px; }
.tag { color: #9dded5; font-size: 11px; letter-spacing: 3px; font-weight: 600; }
h1 { margin: 8px 0; font-size: 34px; font-weight: 650; }
p { color: #acb9d0; margin: 8px 0 22px; }
nav { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 28px; }
a { text-decoration: none; color: #ecdefe; border: 1px solid #4a4065;
  background: #2a253e; border-radius: 12px; padding: 10px 18px; }
a:hover { background: #3d3355; border-color: #a48bd3; }
small { display: block; color: #8492aa; font-size: 12px; }
@media (max-width: 480px) { main { padding: 22px; } h1 { font-size: 28px; } }
</style></head><body><main>
<div class="tag">MECHAHUB / WEB</div><h1>Нова вкладка</h1>
<p>Введи адресу сайту або пошуковий запит угорі.<br>Сторінки відкриваються тут, у MechaHub.</p>
<nav><a href="https://uk.wikipedia.org/">Wikipedia</a>
<a href="https://www.youtube.com/">YouTube</a>
<a href="https://duckduckgo.com/">Вебпошук</a></nav>
<small>Історія та cookies зберігаються лише до закриття MechaHub.</small>
<small>Для завантаження файлу ти обираєш папку збереження.</small>
</main></body></html>'''


def _load_webengine():
    # Static imports remain discoverable by PyInstaller's Qt hooks without
    # loading Chromium while the dashboard is being constructed.
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView

    class BrowserView(QWebEngineView):
        def __init__(self, profile, new_tab, parent=None):
            super().__init__(profile, parent)
            self._new_tab = new_tab
            self.settings().setAttribute(
                QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)

        def createWindow(self, _window_type):
            # Qt navigates this blank view to the requested popup URL. Loading
            # the home HTML here could interrupt that navigation.
            return self._new_tab(home=False)

    return QWebEngineProfile, BrowserView


class _NewTabButton(QToolButton):
    def sizeHint(self):
        # QTabWidget positions corner widgets using their size hint. Native
        # icon-only tool buttons otherwise report less than the fixed 34 px.
        return QSize(34, 34)


class InternetPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._profile = None
        self._view_type = None
        self._states: dict[QWidget, dict] = {}
        self._downloads: list = []
        self._page_saves: dict = {}
        self._closed = False
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.back_button = self._control('back', 'Назад', lambda: self._navigate_history('back'))
        self.forward_button = self._control('arrow', 'Вперед', lambda: self._navigate_history('forward'))
        self.reload_button = self._control('updates', 'Оновити сторінку', self.reload_or_stop)
        self.home_button = self._control('home', 'Стартова сторінка', self.home)
        self.back_button.setEnabled(False)
        self.forward_button.setEnabled(False)
        for control in (self.back_button, self.forward_button, self.reload_button, self.home_button):
            toolbar.addWidget(control)
        self.address = QLineEdit()
        self.address.setPlaceholderText('Адреса сайту або пошук')
        self.address.setClearButtonEnabled(True)
        self.address.returnPressed.connect(self.open_address)
        toolbar.addWidget(self.address, 1)
        self.open_button = QPushButton('Відкрити')
        self.open_button.setObjectName('Primary')
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.clicked.connect(self.open_address)
        toolbar.addWidget(self.open_button)
        layout.addLayout(toolbar)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.tabs = QTabWidget()
        self.tabs.setObjectName('BrowserTabs')
        self.tabs.setDocumentMode(True)
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(lambda _index: self._sync_current())
        self.new_tab_button = _NewTabButton()
        self.new_tab_button.setIcon(icon('plus', '#C9B8F5'))
        self.new_tab_button.setIconSize(QSize(20, 20))
        self.new_tab_button.setObjectName('BrowserAddTab')
        self.new_tab_button.setToolTip('Нова вкладка · Ctrl+T')
        self.new_tab_button.setAccessibleName('Нова вкладка')
        self.new_tab_button.setFixedSize(34, 34)
        self.new_tab_button.clicked.connect(lambda _checked=False: self.new_tab())
        self.tabs.setCornerWidget(self.new_tab_button, Qt.Corner.TopRightCorner)
        layout.addWidget(self.tabs, 1)
        footer = QHBoxLayout()
        self.status = QLabel('Введи сайт або запит і натисни Enter.')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setObjectName('Muted')
        self.status.setMinimumWidth(0)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer.addWidget(self.status, 1)
        self.external_button = QPushButton('↗')
        self.external_button.setObjectName('Subtle')
        self.external_button.setToolTip('Відкрити поточний сайт у зовнішньому браузері')
        self.external_button.setAccessibleName('Відкрити у зовнішньому браузері')
        self.external_button.setEnabled(False)
        self.external_button.clicked.connect(self.open_external)
        footer.addWidget(self.external_button)
        layout.addLayout(footer)
        self._placeholder = QWidget()
        pending = QVBoxLayout(self._placeholder)
        hint = QLabel('Браузер запуститься, коли відкриєш розділ «Інтернет».')
        hint.setWordWrap(True)
        hint.setObjectName('Muted')
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pending.addWidget(hint)
        self.tabs.addTab(self._placeholder, 'Нова вкладка')
        self._shortcuts = []
        for keys, callback in (
            ('Ctrl+L', self.focus_address), ('Ctrl+T', self.new_tab),
            ('Ctrl+W', lambda: self.close_tab(self.tabs.currentIndex())),
            ('F5', self.reload_or_stop), ('Alt+Left', lambda: self._navigate_history('back')),
            ('Alt+Right', lambda: self._navigate_history('forward')),
        ):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    @staticmethod
    def _control(icon_name, tooltip, callback):
        control = QPushButton()
        control.setIcon(icon(icon_name, '#C9CDE0'))
        control.setIconSize(QSize(20, 20))
        control.setObjectName('BrowserControl')
        control.setFixedSize(40, 40)
        control.setToolTip(tooltip)
        control.setAccessibleName(tooltip)
        control.setCursor(Qt.CursorShape.PointingHandCursor)
        control.clicked.connect(callback)
        return control

    def activate(self) -> bool:
        """Create one private browser profile on first visit; load local home HTML."""
        if self._closed:
            return False
        if self._profile is not None:
            return True
        try:
            profile_type, self._view_type = _load_webengine()
            self._profile = profile_type(self)
            self._profile.downloadRequested.connect(self._download_requested)
        except (ImportError, RuntimeError) as error:
            self.status.setText(f'Не вдалося запустити вбудований браузер: {error}')
            return False
        self.tabs.removeTab(self.tabs.indexOf(self._placeholder))
        self._placeholder.deleteLater()
        self._placeholder = None
        self._create_tab(home=True)
        return True

    def current_view(self):
        view = self.tabs.currentWidget()
        return view if view in self._states else None

    def new_tab(self, *, home=True):
        if self._closed:
            return None
        if self._profile is None:
            # First activation itself creates the first tab.
            return self.current_view() if self.activate() else None
        return self._create_tab(home=home)

    def _create_tab(self, *, home):
        view = self._view_type(self._profile, self.new_tab, self.tabs)
        self._states[view] = dict(home=home, loading=False, progress=0, error=False)
        view.urlChanged.connect(lambda url, tab=view: self._url_changed(tab, url))
        view.titleChanged.connect(lambda title, tab=view: self._title_changed(tab, title))
        view.loadStarted.connect(lambda tab=view: self._load_started(tab))
        view.loadProgress.connect(lambda progress, tab=view: self._load_progress(tab, progress))
        view.loadFinished.connect(lambda ok, tab=view: self._load_finished(tab, ok))
        view.page().windowCloseRequested.connect(lambda tab=view: self.close_tab(self.tabs.indexOf(tab)))
        index = self.tabs.addTab(view, 'Нова вкладка')
        self.tabs.setCurrentIndex(index)
        if home:
            view.setHtml(HOME_HTML, QUrl('about:blank'))
        self._sync_current()
        return view

    def open_address(self):
        try:
            target = web_target(self.address.text())
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.open_url(target)

    def open_url(self, target: str):
        try:
            target = web_target(target)
        except ValueError as error:
            self.status.setText(str(error))
            return
        if not self.activate():
            return
        view = self.current_view()
        self._states[view]['home'] = False
        self.address.setText(target)
        self.address.setModified(False)
        view.setUrl(QUrl(target))
        view.setFocus(Qt.FocusReason.OtherFocusReason)

    def home(self):
        if not self.activate():
            return
        view = self.current_view()
        self._states[view]['home'] = True
        view.setHtml(HOME_HTML, QUrl('about:blank'))
        self._sync_current()

    def focus_address(self):
        self.address.setFocus()
        self.address.selectAll()

    def _navigate_history(self, action):
        view = self.current_view()
        if view is not None:
            getattr(view, action)()
            self._sync_current()

    def reload_or_stop(self):
        view = self.current_view()
        if view is None:
            self.activate()
        elif self._states[view]['loading']:
            view.stop()
        else:
            view.reload()

    def open_external(self):
        view = self.current_view()
        if view is None or view.url().scheme() not in {'http', 'https'}:
            return
        if not QDesktopServices.openUrl(view.url()):
            self.status.setText('Не вдалося відкрити зовнішній браузер.')

    def _url_changed(self, view, url):
        if view not in self._states:
            return
        if url.scheme() in {'http', 'https'}:
            self._states[view]['home'] = False
        if view is self.current_view():
            self._sync_current(refresh_address=not (self.address.hasFocus() and self.address.isModified()))

    def _title_changed(self, view, title):
        index = self.tabs.indexOf(view)
        if index < 0:
            return
        title = ' '.join(title.split()) or 'Нова вкладка'
        self.tabs.setTabText(index, title[:27]+'…' if len(title) > 28 else title)
        self.tabs.setTabToolTip(index, title)

    def _load_started(self, view):
        if view not in self._states:
            return
        self._states[view].update(loading=True, progress=0, error=False)
        if view is self.current_view():
            self._sync_current(refresh_address=False)

    def _load_progress(self, view, progress):
        if view not in self._states:
            return
        self._states[view]['progress'] = progress
        if view is self.current_view():
            self._sync_current(refresh_address=False)

    def _load_finished(self, view, ok):
        if view not in self._states:
            return
        self._states[view].update(loading=False, error=not ok)
        if view is self.current_view():
            self._sync_current(refresh_address=False)

    def _sync_current(self, *, refresh_address=True):
        if self._closed:
            return
        view = self.current_view()
        if view is None:
            self.back_button.setEnabled(False)
            self.forward_button.setEnabled(False)
            self.external_button.setEnabled(False)
            self.progress.hide()
            return
        state = self._states[view]
        url = view.url()
        if refresh_address:
            self.address.setText(url.toString() if url.scheme() in {'http', 'https'} else '')
            self.address.setModified(False)
        self.back_button.setEnabled(view.history().canGoBack())
        self.forward_button.setEnabled(view.history().canGoForward())
        self.external_button.setEnabled(url.scheme() in {'http', 'https'})
        self.reload_button.setIcon(icon('stop' if state['loading'] else 'updates', '#C9CDE0'))
        self.reload_button.setToolTip('Зупинити завантаження' if state['loading'] else 'Оновити сторінку')
        self.reload_button.setAccessibleName(self.reload_button.toolTip())
        self.progress.setValue(state['progress'])
        self.progress.setVisible(state['loading'])
        if state['loading']:
            self.status.setText(f'Завантаження… {state["progress"]}%')
        elif state['error']:
            self.status.setText('Не вдалося завантажити сторінку. Перевір адресу та підключення.')
        elif state['home']:
            self.status.setText('Стартова сторінка · історія та cookies лише у цьому сеансі.')
        else:
            self.status.setText('Сторінку завантажено.')

    def close_tab(self, index: int):
        if self._closed or not 0 <= index < self.tabs.count():
            return
        view = self.tabs.widget(index)
        if view not in self._states:
            return
        self._states.pop(view)
        self.tabs.removeTab(index)
        self._dispose_view(view)
        if self.tabs.count() == 0:
            self._create_tab(home=True)
        self._sync_current()

    @staticmethod
    def _dispose_view(view):
        # Destroy owned pages before their profile. Deferred deletion can outlive
        # the GUI event loop when called from the main window's closeEvent.
        view.stop()
        page = view.page()
        delete(view)
        if isValid(page):
            delete(page)

    def _download_requested(self, request):
        if self._closed:
            request.cancel()
            return
        save_page = request.isSavePageDownload()
        page = request.page() if save_page else None
        if save_page:
            approved = self._page_saves.pop(page, None)
            if approved is not None:
                self._track_download(request, approved)
                return
            # Qt auto-accepts Save Page requests. Cancel immediately, before the
            # modal dialog's nested event loop can allow a default-path write.
            save_format = request.savePageFormat()
            request.cancel()
        suggested = Path(request.suggestedFileName().replace('\\', '/')).name or 'download'
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        filename, _ = QFileDialog.getSaveFileName(self, 'Зберегти файл', str(Path(folder) / suggested))
        if not filename or self._closed:
            if isValid(request):
                request.cancel()
            return
        destination = Path(filename)
        if save_page:
            if isValid(page):
                self._page_saves[page] = destination
                page.save(str(destination), save_format)
            return
        if not isValid(request):
            return
        request.setDownloadDirectory(str(destination.parent))
        request.setDownloadFileName(destination.name)
        self._track_download(request, destination)
        request.accept()

    def _track_download(self, request, destination):
        self._downloads.append(request)
        request.isFinishedChanged.connect(lambda item=request: self._download_finished(item))
        self.status.setText(f'Завантаження: {destination.name}')

    def _download_finished(self, request):
        if self._closed or request not in self._downloads or not request.isFinished():
            return
        self._downloads.remove(request)
        if request.state().name == 'DownloadCompleted':
            self.status.setText('Збережено файл: '+request.downloadFileName())
        else:
            self.status.setText('Завантаження не завершено: '+(request.interruptReasonString() or 'скасовано'))
        request.deleteLater()

    def shutdown(self) -> bool:
        """Stop downloads and destroy every browser page before its profile."""
        if self._closed:
            return True
        self._closed = True
        for request in tuple(self._downloads):
            if isValid(request) and not request.isFinished():
                request.cancel()
        self._downloads.clear()
        self._page_saves.clear()
        for view in tuple(self._states):
            self.tabs.removeTab(self.tabs.indexOf(view))
            self._dispose_view(view)
        self._states.clear()
        if self._profile is not None:
            delete(self._profile)
            self._profile = None
        self.setEnabled(False)
        return True

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
