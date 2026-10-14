"""Browser controls use a fake engine; real Chromium stays in a child process."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QUrl, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from mechahub.ui import internet_page as ui
from mechahub.ui.styles import STYLESHEET


class FakeProfile(QObject):
    downloadRequested = Signal(object)


class FakePage(QObject):
    windowCloseRequested = Signal()

    def save(self, *args):
        self.saved = args


class FakeView(QWidget):
    urlChanged = Signal(QUrl)
    titleChanged = Signal(str)
    loadStarted = Signal()
    loadProgress = Signal(int)
    loadFinished = Signal(bool)

    def __init__(self, profile, new_tab, parent=None):
        super().__init__(parent)
        self._page = FakePage(self)
        self._new_tab = new_tab
        self._url = QUrl()
        self._urls = []
        self._index = -1
        self.html = None
        self.urls_loaded = []
        self.reload_calls = 0
        self.stop_calls = 0

    def page(self):
        return self._page

    def setHtml(self, html, base_url):
        self.html = html
        self._load(base_url)
        self.titleChanged.emit('MechaHub — Нова вкладка')

    def setUrl(self, url):
        self.urls_loaded.append(url.toString())
        self._load(url)

    def _load(self, url):
        self.loadStarted.emit()
        self._urls = self._urls[:self._index+1]+[url]
        self._index += 1
        self._url = url
        self.urlChanged.emit(url)
        self.loadProgress.emit(100)
        self.loadFinished.emit(True)

    def url(self):
        return self._url

    def history(self):
        return self

    def canGoBack(self):
        return self._index > 0

    def canGoForward(self):
        return self._index+1 < len(self._urls)

    def back(self):
        if self.canGoBack():
            self._index -= 1
            self._url = self._urls[self._index]
            self.urlChanged.emit(self._url)

    def forward(self):
        if self.canGoForward():
            self._index += 1
            self._url = self._urls[self._index]
            self.urlChanged.emit(self._url)

    def reload(self):
        self.reload_calls += 1

    def stop(self):
        self.stop_calls += 1


class FakeDownload(QObject):
    isFinishedChanged = Signal()

    def __init__(self, *, save_page=False, page=None):
        super().__init__()
        self._save_page = save_page
        self._page = page
        self.finished = False
        self.accepted = False
        self.cancelled = False
        self.filename = 'report.pdf'
        self.folder = ''
        self.result = 'DownloadRequested'

    def suggestedFileName(self):
        return self.filename

    def isSavePageDownload(self):
        return self._save_page

    def page(self):
        return self._page

    def savePageFormat(self):
        return 'CompleteHtmlSaveFormat'

    def setDownloadDirectory(self, value):
        self.folder = value

    def setDownloadFileName(self, value):
        self.filename = value

    def downloadFileName(self):
        return self.filename

    def isFinished(self):
        return self.finished

    def state(self):
        return SimpleNamespace(name=self.result)

    def interruptReasonString(self):
        return 'Unknown reason' if self.result == 'DownloadCompleted' else 'cancelled'

    def accept(self):
        self.accepted = True

    def cancel(self):
        self.cancelled = True


class BrowserControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.loader = patch.object(ui, '_load_webengine', return_value=(FakeProfile, FakeView))
        self.engine = self.loader.start()
        self.page = ui.InternetPage()

    def tearDown(self):
        self.page.shutdown()
        self.page.deleteLater()
        self.app.processEvents()
        self.loader.stop()

    def test_engine_is_lazy_and_activation_only_loads_local_home(self):
        self.engine.assert_not_called()
        self.assertIsNone(self.page.current_view())
        self.assertTrue(self.page.activate())
        self.assertTrue(self.page.activate())
        self.engine.assert_called_once()
        view = self.page.current_view()
        self.assertIn('Нова вкладка', view.html)
        self.assertEqual(view.urls_loaded, [])
        self.assertEqual(view.url().toString(), 'about:blank')
        self.assertEqual(self.page.address.text(), '')

    def test_enter_navigates_embedded_view_and_never_launches_external_browser(self):
        self.page.resize(680, 535)
        self.page.show()
        with patch.object(ui.QDesktopServices, 'openUrl') as external:
            self.page.address.setText('wikipedia.org/path')
            QTest.keyClick(self.page.address, Qt.Key.Key_Return)
            external.assert_not_called()
        self.assertEqual(self.page.current_view().urls_loaded, ['https://wikipedia.org/path'])
        self.assertEqual(self.page.address.text(), 'https://wikipedia.org/path')
        self.assertIn('завантажено', self.page.status.text())

    def test_blocked_address_schemes_do_not_even_start_engine(self):
        for value in ('file:///C:/test.txt', 'javascript:alert(1)', 'data:text/html,hi'):
            with self.subTest(value=value):
                self.page.address.setText(value)
                self.page.open_address()
                self.engine.assert_not_called()
                self.assertIn('HTTP', self.page.status.text())

    def test_tab_switching_updates_address_and_history_navigation(self):
        self.page.open_url('https://example.com/first')
        first = self.page.current_view()
        self.page.open_url('https://example.com/second')
        self.assertTrue(self.page.back_button.isEnabled())
        self.page.back_button.click()
        self.assertEqual(self.page.address.text(), 'https://example.com/first')
        self.assertTrue(self.page.forward_button.isEnabled())
        self.page.forward_button.click()
        self.assertEqual(self.page.address.text(), 'https://example.com/second')
        self.page.new_tab()
        self.assertEqual(self.page.address.text(), '')
        self.page.tabs.setCurrentWidget(first)
        self.assertEqual(self.page.address.text(), 'https://example.com/second')

    def test_background_loading_cannot_overwrite_active_tab_status(self):
        self.page.activate()
        first = self.page.current_view()
        second = self.page.new_tab()
        status = self.page.status.text()
        first.loadStarted.emit()
        first.loadProgress.emit(37)
        first.loadFinished.emit(False)
        self.assertEqual(self.page.status.text(), status)
        self.page.tabs.setCurrentWidget(first)
        self.assertIn('Не вдалося', self.page.status.text())
        self.page.tabs.setCurrentWidget(second)
        second.loadStarted.emit()
        second.loadProgress.emit(55)
        self.assertIn('Зупинити', self.page.reload_button.toolTip())
        self.assertIn('55%', self.page.status.text())
        self.page.reload_button.click()
        self.assertEqual(second.stop_calls, 1)
        second.loadFinished.emit(True)
        self.page.reload_button.click()
        self.assertEqual(second.reload_calls, 1)

    def test_close_last_tab_replaces_it_and_shutdown_destroys_pages_before_profile(self):
        self.page.activate()
        first = self.page.current_view()
        first_page = first.page()
        self.page.close_tab(0)
        self.assertFalse(isValid(first))
        self.assertFalse(isValid(first_page))
        self.assertEqual(self.page.tabs.count(), 1)
        self.assertIsNotNone(self.page.current_view().html)
        order = []
        current = self.page.current_view()
        profile = self.page._profile
        current.page().destroyed.connect(lambda: order.append('page'))
        profile.destroyed.connect(lambda: order.append('profile'))
        self.assertTrue(self.page.shutdown())
        self.assertEqual(order, ['page', 'profile'])
        self.assertFalse(isValid(profile))
        self.assertTrue(self.page.shutdown())
        self.assertFalse(self.page.activate())

    def test_external_open_requires_its_own_explicit_action(self):
        self.page.activate()
        with patch.object(ui.QDesktopServices, 'openUrl', return_value=True) as external:
            self.page.open_external()
            external.assert_not_called()
            self.page.open_url('https://example.com/')
            external.assert_not_called()
            self.page.open_external()
        self.assertEqual(external.call_args.args[0].toString(), 'https://example.com/')

    def test_missing_engine_reports_error_without_external_fallback(self):
        self.engine.side_effect = ImportError('missing WebEngine')
        with patch.object(ui.QDesktopServices, 'openUrl') as external:
            self.assertFalse(self.page.activate())
            external.assert_not_called()
        self.assertIn('Не вдалося', self.page.status.text())
        self.assertIn('missing WebEngine', self.page.status.text())

    def test_download_only_accepts_after_user_selects_destination_and_cancel_writes_nothing(self):
        rejected = FakeDownload()
        with patch.object(ui.QFileDialog, 'getSaveFileName', return_value=('', '')):
            self.page._download_requested(rejected)
        self.assertTrue(rejected.cancelled)
        self.assertFalse(rejected.accepted)
        accepted = FakeDownload()
        destination = Path.cwd() / 'chosen' / 'report.pdf'
        with patch.object(ui.QFileDialog, 'getSaveFileName', return_value=(str(destination), '')):
            self.page._download_requested(accepted)
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.folder, str(destination.parent))
        self.assertEqual(accepted.filename, destination.name)
        self.assertFalse(destination.exists())
        accepted.finished = True
        accepted.result = 'DownloadCompleted'
        accepted.isFinishedChanged.emit()
        self.assertIn('Збережено файл', self.page.status.text())
        self.assertEqual(self.page._downloads, [])

    def test_save_page_auto_download_is_cancelled_before_dialog_and_restarted_with_selected_path(self):
        self.page.activate()
        web_page = self.page.current_view().page()
        request = FakeDownload(save_page=True, page=web_page)
        destination = Path.cwd() / 'chosen' / 'page.html'
        def choose_path(*args):
            self.assertTrue(request.cancelled)
            return str(destination), ''
        with patch.object(ui.QFileDialog, 'getSaveFileName', side_effect=choose_path) as dialog:
            self.page._download_requested(request)
            self.assertEqual(web_page.saved[0], str(destination))
            approved = FakeDownload(save_page=True, page=web_page)
            self.page._download_requested(approved)
        dialog.assert_called_once()
        self.assertFalse(approved.cancelled)
        self.assertIn(approved, self.page._downloads)

    def test_browser_canvas_dominates_small_page_and_toolbar_is_reachable(self):
        self.page.setStyleSheet(STYLESHEET)
        self.page.activate()
        self.page.resize(680, 535)
        self.page.show()
        self.app.processEvents()
        self.assertLessEqual(self.page.width(), 680)
        view = self.page.current_view()
        self.assertGreater(view.height(), self.page.height()*.65)
        for control in (self.page.address, self.page.open_button, self.page.back_button,
                        self.page.forward_button, self.page.home_button, self.page.reload_button,
                        self.page.new_tab_button):
            bounds = QRect(control.mapTo(self.page, QPoint(0, 0)), control.size())
            self.assertTrue(control.isVisible())
            self.assertTrue(self.page.rect().contains(bounds),
                            f'{control.objectName()} clipped: {bounds} inside {self.page.rect()}')


class RealBrowserIntegrationTests(unittest.TestCase):
    def test_local_html_javascript_popup_and_shutdown_in_fresh_process(self):
        script = textwrap.dedent('''
            import time
            from PySide6.QtCore import QCoreApplication, Qt, QUrl
            QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
            from PySide6.QtWidgets import QApplication
            from PySide6.QtTest import QTest
            from PySide6.QtWebEngineCore import QWebEngineUrlRequestInterceptor
            from shiboken6 import isValid
            from mechahub.ui.internet_page import InternetPage
            app = QApplication([])
            app.setQuitOnLastWindowClosed(False)
            class NoNetwork(QWebEngineUrlRequestInterceptor):
                def __init__(self, parent):
                    super().__init__(parent)
                    self.requests = []
                def interceptRequest(self, info):
                    if info.requestUrl().scheme() in {'http', 'https'}:
                        self.requests.append(info.requestUrl().toString())
                        info.block(True)
            def wait_for(condition):
                deadline = time.monotonic()+10
                while not condition() and time.monotonic() < deadline:
                    QTest.qWait(20)
                assert condition(), 'WebEngine timed out'
            window = InternetPage()
            window.resize(680, 535)
            window.show()
            assert window.activate()
            profile = window._profile
            assert profile.isOffTheRecord()
            interceptor = NoNetwork(profile)
            profile.setUrlRequestInterceptor(interceptor)
            view = window.current_view()
            wait_for(lambda: view.title() == 'MechaHub — Нова вкладка' and
                     not window._states[view]['loading'])
            assert window.address.text() == ''
            view.setHtml('<html><title>Local browser regression</title><body><h1>Local page</h1></body></html>', QUrl('about:blank'))
            wait_for(lambda: view.title() == 'Local browser regression' and
                     not window._states[view]['loading'])
            values = []
            view.page().runJavaScript('document.querySelector("h1").textContent', values.append)
            wait_for(lambda: bool(values))
            assert values == ['Local page'], values
            view.page().runJavaScript('window.open("about:blank", "_blank");')
            wait_for(lambda: window.tabs.count() == 2)
            popup = window.current_view()
            assert popup is not view
            window.close_tab(window.tabs.indexOf(popup))
            assert not isValid(popup)
            pages = [item.page() for item in window._states]
            assert interceptor.requests == [], interceptor.requests
            assert window.shutdown()
            assert all(not isValid(page) for page in pages)
            assert not isValid(profile)
            window.close()
            app.processEvents()
            print('LOCAL_BROWSER_OK', flush=True)
        ''')
        env = os.environ.copy()
        env['QT_QPA_PLATFORM'] = 'offscreen'
        env['QTWEBENGINE_CHROMIUM_FLAGS'] = '--disable-gpu --disable-background-networking'
        completed = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                                   env=env, capture_output=True, text=True, encoding='utf-8', timeout=35)
        self.assertEqual(completed.returncode, 0, completed.stdout+'\n'+completed.stderr)
        self.assertIn('LOCAL_BROWSER_OK', completed.stdout)
        self.assertNotIn('WebEnginePage still not deleted', completed.stderr)


if __name__ == '__main__':
    unittest.main()
