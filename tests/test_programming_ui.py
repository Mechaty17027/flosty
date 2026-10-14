"""Asynchronous print/input/stop and compilation stages with isolated projects."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mechahub.core.code_workspace import CodeWorkspaceError, LanguageProfile
from mechahub.ui.programming_page import MAX_OUTPUT_CHARS, ProgrammingPage


class ProgrammingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name).resolve()
        self.page = ProgrammingPage(workspace_dir=self.root)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.request_close()
        deadline = time.monotonic() + 5
        while self.page.process is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.folder.cleanup()

    def wait_finished(self, timeout=12):
        deadline = time.monotonic() + timeout
        while self.page.process is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertIsNone(self.page.process, self.page.output.toPlainText())

    def code(self, text):
        self.page.editor.setPlainText(text)
        self.page.editor.document().setModified(True)

    def test_actual_python_print_is_saved_before_run_and_output_contains_unicode(self):
        self.code('print("Привіт з редактора")\n')
        self.page.run_code()
        self.wait_finished()
        self.assertIn('Привіт з редактора', self.page.output.toPlainText())
        self.assertIn('Привіт з редактора', self.page.current_path.read_text(encoding='utf8'))
        self.assertTrue(self.page.run_button.isEnabled())
        self.assertFalse(self.page.stop_button.isEnabled())

    def test_actual_bundled_python_supports_input_and_local_sibling_import(self):
        self.assertTrue(self.page.new_project('Inputs'))
        (self.page.current_path.parent / 'sibling.py').write_text('VALUE = "модуль"\n', encoding='utf8')
        self.code('from sibling import VALUE\nprint("READY", flush=True)\nname=input()\nprint(VALUE + ":" + name)\n')
        self.page.run_code()
        deadline = time.monotonic() + 10
        while 'READY' not in self.page.output.toPlainText() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertIn('READY', self.page.output.toPlainText())
        self.assertIsNotNone(self.page.process)
        self.page.stdin_input.setText('дані')
        self.page.send_input()
        self.wait_finished()
        self.assertIn('модуль:дані', self.page.output.toPlainText())

    def test_stop_terminates_only_running_owned_program_and_close_signal_is_delivered(self):
        self.code('import time\nprint("START", flush=True)\ntime.sleep(60)\n')
        self.page.run_code()
        deadline = time.monotonic() + 10
        while 'START' not in self.page.output.toPlainText() and time.monotonic() < deadline:
            QTest.qWait(20)
        received = []
        self.page.ready_to_close.connect(lambda: received.append(True))
        self.assertFalse(self.page.request_close())
        self.wait_finished(timeout=5)
        self.assertEqual(received, [True])
        self.assertIn('зупинено', self.page.status.text())

    def test_save_failure_prevents_execution_and_keeps_dirty_draft_for_retry(self):
        self.assertTrue(self.page.new_project('Protected'))
        self.code('print("Unsaved draft")\n')
        with patch.object(self.page.workspace, 'save_file', side_effect=CodeWorkspaceError('locked')):
            self.page.run_code()
            self.assertIsNone(self.page.process)
            self.assertFalse(self.page.request_close())
        self.assertIn('Unsaved draft', self.page.editor.toPlainText())
        self.assertTrue(self.page.editor.document().isModified())
        self.assertTrue(self.page.save_code())
        self.assertIn('Unsaved draft', self.page.current_path.read_text(encoding='utf8'))

    def test_new_untitled_save_failure_does_not_become_falsely_clean(self):
        self.code('print("keep draft")')
        real = self.page.workspace.save_file
        calls = 0
        def save(path, text):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise CodeWorkspaceError('locked after initial project creation')
            return real(path, text)
        with patch.object(self.page.workspace, 'save_file', side_effect=save):
            self.assertFalse(self.page.save_code())
        self.assertTrue(self.page.editor.document().isModified())
        self.assertTrue(self.page.save_code())
        self.assertEqual(self.page.current_path.read_text(encoding='utf8'), 'print("keep draft")')

    def test_stdout_and_stderr_are_read_and_total_display_is_bounded(self):
        self.code('import sys\nprint("STDERR",file=sys.stderr)\nprint("x" * 400000)\n')
        self.page.run_code()
        self.wait_finished()
        self.assertTrue(self.page._output_truncated)
        self.assertLessEqual(len(self.page.output.toPlainText()), MAX_OUTPUT_CHARS + 150)
        self.assertIn('Вивід обмежено', self.page.output.toPlainText())

    def simulated_compiler(self, compile_code):
        profile = LanguageProfile('Compiler test', '.cpp', (sys.executable, '-u', '-X', 'utf8', '{output}'),
                                  (sys.executable, '-u', '-X', 'utf8', '-c', compile_code))
        self.page.workspace.add_language(profile)
        self.page._refresh_languages(profile.name)

    def test_successful_compile_stage_runs_output_after_exit_zero(self):
        self.simulated_compiler("from pathlib import Path; Path(r'{output}').write_text('print(\"COMPILED\")\\n', encoding='utf8')")
        self.code('// source fixture\n')
        self.page.run_code()
        self.wait_finished()
        self.assertIn('COMPILED', self.page.output.toPlainText())
        self.assertIn('кодом 0', self.page.status.text())

    def test_failed_compiler_never_runs_previous_output(self):
        self.simulated_compiler('import sys; print("COMPILE ERROR"); sys.exit(7)')
        self.code('// source fixture\n')
        with patch.object(self.page, '_launch', wraps=self.page._launch) as launch:
            self.page.run_code()
            self.wait_finished()
        self.assertEqual(launch.call_count, 1)
        self.assertIn('COMPILE ERROR', self.page.output.toPlainText())
        self.assertIn('кодом 7', self.page.status.text())

    def test_compiler_and_run_receive_tool_path_without_changing_global_environment(self):
        global_path = os.environ.get('PATH')
        compile_code = ('import os; from pathlib import Path; print("COMPILE_PATH="+os.environ["PATH"]); '
                        'Path(r"{output}").write_text("import os\\nprint(\\\"RUN_PATH=\\\"+os.environ[\\\"PATH\\\"])\\n",encoding="utf8")')
        profile = LanguageProfile('Process PATH test', '.cpp', ('{python}', '{output}'),
                                  (sys.executable, '-u', '-X', 'utf8', '-c', compile_code))
        self.page.workspace.add_language(profile)
        self.page._refresh_languages(profile.name)
        self.code('// compiler PATH fixture')
        self.page.run_code()
        self.wait_finished()
        output = self.page.output.toPlainText()
        compiler_folder = str(Path(sys.executable).parent)
        self.assertIn('COMPILE_PATH=' + compiler_folder + os.pathsep, output)
        self.assertIn('RUN_PATH=' + compiler_folder + os.pathsep, output)
        self.assertEqual(os.environ.get('PATH'), global_path)

    def test_stop_during_compile_never_launches_a_run_stage(self):
        self.simulated_compiler('import time; print("COMPILING", flush=True); time.sleep(60)')
        self.code('// compile cancellation\n')
        with patch.object(self.page, '_launch', wraps=self.page._launch) as launch:
            self.page.run_code()
            deadline = time.monotonic() + 10
            while 'COMPILING' not in self.page.output.toPlainText() and time.monotonic() < deadline:
                QTest.qWait(20)
            self.assertIn('COMPILING', self.page.output.toPlainText())
            self.page.stop_code()
            self.wait_finished(timeout=5)
        self.assertEqual(launch.call_count, 1)
        self.assertIn('зупинено', self.page.status.text())

    def test_failed_to_start_releases_process_and_reenables_controls(self):
        fake = self.root / 'not-a-real-program.exe'
        fake.write_bytes(b'invalid executable fixture')
        profile = LanguageProfile('Invalid executable', '.invalid', (str(fake), '{file}'))
        self.page.workspace.add_language(profile)
        self.page._refresh_languages(profile.name)
        self.code('input fixture')
        self.page.run_code()
        self.wait_finished(timeout=5)
        self.assertIn('Не вдалося запустити', self.page.output.toPlainText())
        self.assertTrue(self.page.run_button.isEnabled())
        self.assertFalse(self.page.stop_button.isEnabled())

    def test_stop_terminates_owned_child_without_stopping_unrelated_process(self):
        import psutil
        import subprocess
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            self.code('import subprocess,sys,time\nchild=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\nprint("CHILD="+str(child.pid),flush=True)\ntime.sleep(60)\n')
            self.page.run_code()
            deadline = time.monotonic() + 10
            while 'CHILD=' not in self.page.output.toPlainText() and time.monotonic() < deadline:
                QTest.qWait(20)
            self.assertIn('CHILD=', self.page.output.toPlainText())
            child_pid = int(self.page.output.toPlainText().split('CHILD=')[1].splitlines()[0])
            self.page.stop_code()
            self.wait_finished(timeout=5)
            deadline = time.monotonic() + 3
            while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
                QTest.qWait(20)
            self.assertFalse(psutil.pid_exists(child_pid))
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
