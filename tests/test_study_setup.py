"""Existing Ollama discovery and launch are isolated; no real service starts."""
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mechahub.core import study_setup


class StudySetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.local = self.folder / 'Local Data'
        self.program = self.local / 'Programs' / 'Ollama'
        self.program.mkdir(parents=True)
        self.environment = patch.dict(study_setup.os.environ, {'LOCALAPPDATA': str(self.local), 'PATH': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.windows = patch.object(study_setup, '_is_windows', return_value=True)
        self.windows.start()
        self.addCleanup(self.windows.stop)

    def executable(self, name, folder=None):
        path = (folder or self.program) / name
        path.write_bytes(b'fake installed executable, never run')
        return path

    def test_default_installation_prefers_gui_over_cli(self):
        self.executable('ollama.exe')
        gui = self.executable('ollama app.exe')
        with patch.object(study_setup.shutil, 'which') as which:
            self.assertEqual(study_setup.find_ollama(), gui)
        which.assert_not_called()

    def test_default_cli_is_found_when_no_gui_exists(self):
        cli = self.executable('ollama.exe')
        self.assertEqual(study_setup.find_ollama(), cli)

    def test_explicit_path_lookup_finds_cli_and_prefers_companion_gui(self):
        custom = self.folder / 'Custom installation'
        custom.mkdir()
        cli = self.executable('ollama.exe', custom)
        gui = self.executable('ollama app.exe', custom)
        with patch.dict(study_setup.os.environ, {'PATH': str(custom)}), \
                patch.object(study_setup.shutil, 'which', return_value=str(cli)) as which:
            self.assertEqual(study_setup.find_ollama(), gui)
        which.assert_called_once_with(str(custom / 'ollama.exe'))

    def test_missing_service_does_not_start_or_download_anything(self):
        self.assertIsNone(study_setup.find_ollama())
        with patch.object(study_setup.subprocess, 'Popen') as started:
            with self.assertRaisesRegex(ValueError, 'Ollama не знайдено'):
                study_setup.start_ollama()
        started.assert_not_called()

    def test_relative_localappdata_and_path_entries_do_not_search_current_directory(self):
        with patch.dict(study_setup.os.environ, {'LOCALAPPDATA': 'relative', 'PATH': os.pathsep.join(['', '.', 'relative'])}), \
                patch.object(study_setup.shutil, 'which') as which:
            self.assertIsNone(study_setup.find_ollama())
        which.assert_not_called()

    def test_path_deduplicates_absolute_entries_and_rejects_shell_wrappers(self):
        custom = self.folder / 'Path folder'
        custom.mkdir()
        wrapper = custom / 'ollama.exe.cmd'
        wrapper.write_text('never run', encoding='utf-8')
        with patch.dict(study_setup.os.environ, {'PATH': os.pathsep.join([str(custom), str(custom)])}), \
                patch.object(study_setup.shutil, 'which', return_value=str(wrapper)) as which:
            self.assertIsNone(study_setup.find_ollama())
        which.assert_called_once()

    def test_non_windows_discovery_returns_none_and_start_explains_platform(self):
        self.executable('ollama app.exe')
        with patch.object(study_setup, '_is_windows', return_value=False), \
                patch.object(study_setup.subprocess, 'Popen') as started:
            self.assertIsNone(study_setup.find_ollama())
            with self.assertRaisesRegex(ValueError, 'Windows'):
                study_setup.start_ollama()
        started.assert_not_called()

    def test_reparse_executable_is_not_used(self):
        cli = self.executable('ollama.exe')
        original = Path.lstat

        def reparse(path):
            if path == cli:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return original(path)

        with patch.object(Path, 'lstat', autospec=True, side_effect=reparse):
            self.assertIsNone(study_setup.find_ollama())

    def start(self, executable):
        process = Mock()
        with patch.object(study_setup, 'find_ollama', return_value=executable), \
                patch.object(study_setup.subprocess, 'Popen', return_value=process) as started:
            self.assertIs(study_setup.start_ollama(), process)
        return started

    def test_gui_launch_is_argument_array_without_serve_or_shell(self):
        gui = self.executable('ollama app.exe')
        started = self.start(gui)
        self.assertEqual(started.call_args.args[0], [str(gui)])
        self.assertFalse(started.call_args.kwargs['shell'])
        self.assertEqual(started.call_args.kwargs['cwd'], str(gui.parent))

    def test_cli_launch_runs_serve_hidden_and_keeps_local_service_binding(self):
        cli = self.executable('ollama.exe')
        with patch.dict(study_setup.os.environ, {'OLLAMA_HOST': '0.0.0.0:11434'}):
            started = self.start(cli)
        self.assertEqual(started.call_args.args[0], [str(cli), 'serve'])
        self.assertFalse(started.call_args.kwargs['shell'])
        self.assertTrue(started.call_args.kwargs['close_fds'])
        self.assertTrue(started.call_args.kwargs['creationflags'] & study_setup.subprocess.CREATE_NO_WINDOW)
        startup = started.call_args.kwargs['startupinfo']
        self.assertTrue(startup.dwFlags & study_setup.subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(startup.wShowWindow, study_setup.subprocess.SW_HIDE)
        self.assertEqual(started.call_args.kwargs['env']['OLLAMA_HOST'], '127.0.0.1:11434')
        self.assertEqual(started.call_args.kwargs['stdin'], study_setup.subprocess.DEVNULL)
        self.assertEqual(started.call_args.kwargs['stdout'], study_setup.subprocess.DEVNULL)
        self.assertEqual(started.call_args.kwargs['stderr'], study_setup.subprocess.DEVNULL)

    def test_removed_executable_between_find_and_launch_is_rejected(self):
        missing = self.program / 'ollama.exe'
        with patch.object(study_setup, 'find_ollama', return_value=missing), \
                patch.object(study_setup.subprocess, 'Popen') as started:
            with self.assertRaisesRegex(ValueError, 'більше недоступний'):
                study_setup.start_ollama()
        started.assert_not_called()

    def test_os_launch_failure_becomes_user_readable_error(self):
        cli = self.executable('ollama.exe')
        with patch.object(study_setup, 'find_ollama', return_value=cli), \
                patch.object(study_setup.subprocess, 'Popen', side_effect=OSError('access denied')):
            with self.assertRaisesRegex(ValueError, 'Не вдалося запустити Ollama'):
                study_setup.start_ollama()


if __name__ == '__main__':
    unittest.main()
