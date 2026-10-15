"""MechaHub Windows desktop application."""
from __future__ import annotations

import sys
from pathlib import Path
from mechahub import __version__


def main() -> int:
    if '--apply-update' in sys.argv:
        from mechahub.core.updates_install import run_update_helper
        if len(sys.argv) != 3 or sys.argv[1] != '--apply-update':
            return 1
        return run_update_helper(Path(sys.argv[2]))
    from mechahub.core.updates_install import cleanup_finished_stages
    try:
        cleanup_finished_stages()
    except (OSError, ValueError):
        pass
    # The independent updater waits for this app to quit, so it must not load
    # Qt or construct another application window before dispatching above.
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QIcon
    from mechahub.ui.main_window import MainWindow
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
    """Exercise bundled catalogue photos, browser HTML and coding Python."""
    import tempfile
    from PySide6.QtCore import QTimer, Qt
    from mechahub.core.cash_registers import MODELS
    from mechahub.ui.cash_registers_page import _model_image
    from mechahub.ui.programming_page import ProgrammingPage
    timer = QTimer(window)
    timer.setSingleShot(True)
    coding_timer = QTimer(window)
    coding_timer.setInterval(50)
    completed = False
    coding_page = None
    coding_folder = None
    input_sent = False
    pending_code = None

    def finish(code: int):
        nonlocal completed, pending_code
        if completed:
            return
        timer.stop()
        coding_timer.stop()
        if pending_code is None:
            pending_code = code
        if coding_page is not None and not coding_page.request_close():
            coding_page.ready_to_close.connect(lambda: finish(pending_code))
            return
        completed = True
        if coding_page is not None:
            coding_page.close()
        if coding_folder is not None:
            # Only remove the exact private temporary folder created below.
            path = Path(coding_folder.name).resolve()
            if path.parent == Path(tempfile.gettempdir()).resolve() and path.name.startswith('mechahub-coding-check-'):
                coding_folder.cleanup()
        window.close()
        app.exit(pending_code)

    def check_coding():
        nonlocal input_sent
        if coding_page.process is None:
            output = coding_page.output.toPlainText()
            finish(0 if 'MECHAHUB_CODING_OK Привіт 42' in output else 1)
            return
        if not input_sent and coding_page.stdin_input.isEnabled():
            coding_page.stdin_input.setText('Привіт')
            coding_page.send_input()
            input_sent = True

    def begin_coding():
        nonlocal coding_page, coding_folder
        if completed or coding_page is not None:
            return
        coding_folder = tempfile.TemporaryDirectory(prefix='mechahub-coding-check-')
        workspace = Path(coding_folder.name)
        coding_page = ProgrammingPage(window, workspace_dir=workspace)
        coding_page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        coding_page.editor.setPlainText("from decimal import Decimal\nname = input('Name: ')\nprint('MECHAHUB_CODING_OK', name, int(Decimal('21') * 2))\n")
        coding_page.run_code()
        coding_timer.start()

    def begin():
        # Exercise every image decoder in the actual packaged Qt runtime too
        # (including WebP); a source-only asset check cannot verify its plugins.
        catalogue = window.cash_registers_page
        if catalogue.models.count() != len(MODELS):
            finish(1)
            return
        for row, model in enumerate(MODELS):
            if _model_image(model).isNull() or not model.issues:
                finish(1)
                return
            catalogue.models.setCurrentRow(row)
            if catalogue.detail_tabs.count() != 3 or catalogue.issue_selector.count() != len(model.issues):
                finish(1)
                return
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
                QTimer.singleShot(0, begin_coding if valid else lambda: finish(1))

            view.page().runJavaScript(
                "document.title + ' | ' + (document.body ? document.body.innerText : '')", verified,
            )

        view.loadFinished.connect(loaded)
        # Register first, then trigger a fresh local load: the home page may
        # already have completed while the browser was being activated.
        view.setHtml('<!doctype html><html><head><title>MechaHub</title></head>'
                     '<body>Нова вкладка · MechaHub self-check</body></html>')

    timer.timeout.connect(lambda: finish(1))
    coding_timer.timeout.connect(check_coding)
    timer.start(25000)
    QTimer.singleShot(0, begin)


if __name__ == '__main__':
    raise SystemExit(main())
