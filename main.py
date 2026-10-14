"""MechaHub Windows desktop application."""
from __future__ import annotations

import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QTimer, Qt
from PySide6.QtGui import QIcon
from mechahub.ui.main_window import MainWindow
from pathlib import Path
from mechahub import __version__


def main() -> int:
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName('MechaHub')
    app.setApplicationVersion(__version__)
    app.setOrganizationName('MechaHub')
    app.setStyle('Fusion')
    icon = Path(__file__).resolve().parent / 'assets' / 'icon.svg'
    if hasattr(sys, '_MEIPASS'):
        icon = Path(sys._MEIPASS) / 'assets' / 'icon.svg'
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    diagnostic = '--self-check' in sys.argv
    if diagnostic:
        app.setQuitOnLastWindowClosed(False)
        window._automatic_updates_started = True
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.show()
    if diagnostic:
        _self_check(app, window)
    return app.exec()


def _self_check(app: QApplication, window: MainWindow) -> None:
    """Exercise the packaged browser on local HTML, then exit with a status code."""
    timer = QTimer(window)
    timer.setSingleShot(True)
    completed = False

    def finish(code: int):
        nonlocal completed
        if completed:
            return
        completed = True
        timer.stop()
        window.close()
        app.exit(code)

    def begin():
        window._navigate(4)
        view = window.internet_page.new_tab(home=False)
        if view is None:
            finish(1)
            return

        def loaded(ok: bool):
            if not ok:
                QTimer.singleShot(0, lambda: finish(1))
                return

            def verified(value):
                valid = isinstance(value, str) and 'MechaHub' in value and 'Нова вкладка' in value
                QTimer.singleShot(0, lambda: finish(0 if valid else 1))

            view.page().runJavaScript(
                "document.title + ' | ' + (document.body ? document.body.innerText : '')", verified,
            )

        view.loadFinished.connect(loaded)
        # Register first, then trigger a fresh local load: the home page may
        # already have completed while the browser was being activated.
        view.setHtml('<!doctype html><html><head><title>MechaHub</title></head>'
                     '<body>Нова вкладка · MechaHub self-check</body></html>')

    timer.timeout.connect(lambda: finish(1))
    timer.start(20000)
    QTimer.singleShot(0, begin)


if __name__ == '__main__':
    raise SystemExit(main())
