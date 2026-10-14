"""Project persistence and process arguments without a real compiler."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mechahub.core.code_workspace import (
    CodeWorkspace, CodeWorkspaceError, DEFAULT_PROFILES, LanguageProfile,
    MAX_CODE_BYTES, RuntimeTool, find_python, validate_profile,
)


class CodeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.workspace = CodeWorkspace(self.root)

    def test_project_stays_in_workspace_and_utf8_code_round_trips(self):
        path = self.workspace.create_project(DEFAULT_PROFILES[0], '../../Моя ідея')
        self.assertTrue(path.is_relative_to(self.root / 'projects'))
        self.assertEqual(path.name, 'main.py')
        code = 'print("Привіт, світе")\n'
        self.workspace.save_file(path, code)
        self.assertEqual(self.workspace.read_file(path), code)

    def test_failed_atomic_save_preserves_previous_file(self):
        path = self.workspace.create_project(DEFAULT_PROFILES[0])
        previous = path.read_bytes()
        with patch('mechahub.core.code_workspace.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(CodeWorkspaceError):
                self.workspace.save_file(path, 'new text')
        self.assertEqual(path.read_bytes(), previous)
        self.assertFalse(list(path.parent.glob('.mechahub-code-*')))

    def test_invalid_binary_oversized_and_non_utf8_files_are_rejected(self):
        path = self.root / 'main.py'
        for content in (b'print\0bad', b'\xff\xfe', b'x' * (MAX_CODE_BYTES + 1)):
            path.write_bytes(content)
            with self.subTest(content=content[:20]), self.assertRaises(CodeWorkspaceError):
                self.workspace.read_file(path)

    def test_profiles_roundtrip_and_custom_language_has_explicit_program(self):
        profile = LanguageProfile('My Python', '.custom', (sys.executable, '-u', '{file}'))
        self.workspace.add_language(profile)
        self.workspace.python_executable = sys.executable
        self.workspace.save_settings()
        loaded = CodeWorkspace(self.root)
        self.assertEqual(loaded.profiles[-1], profile)
        self.assertEqual(loaded.python_executable, sys.executable)

    def test_duplicate_language_and_invalid_command_templates_are_rejected(self):
        with self.assertRaises(CodeWorkspaceError):
            self.workspace.add_language(LanguageProfile('python', '.abc', (sys.executable, '{file}')))
        for profile in (LanguageProfile('x', '../abc', (sys.executable, '{file}')),
                        LanguageProfile('x', '.js', ('node', '{file}')),
                        LanguageProfile('x', '.js', (sys.executable, '{unknown}')),
                        LanguageProfile('x', '.js', (sys.executable, '--x={compiler}')),
                        LanguageProfile('x', '.js', ('{output}',)),
                        LanguageProfile('x', '.js', (sys.executable, '\0'))):
            with self.subTest(profile=profile), self.assertRaises(CodeWorkspaceError):
                validate_profile(profile)

    def test_failed_settings_save_keeps_existing_profiles_in_memory_and_on_disk(self):
        self.workspace.save_settings()
        before = self.workspace.settings_path.read_bytes()
        with patch('mechahub.core.code_workspace.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(CodeWorkspaceError):
                self.workspace.add_language(LanguageProfile('Test', '.t', (sys.executable, '{file}')))
        self.assertEqual(self.workspace.profiles, list(DEFAULT_PROFILES))
        self.assertEqual(self.workspace.settings_path.read_bytes(), before)

    def test_invalid_settings_do_not_crash_the_page_or_modify_the_file(self):
        self.workspace.settings_path.write_text('{invalid', encoding='utf8')
        loaded = CodeWorkspace(self.root)
        self.assertTrue(loaded.load_error)
        self.assertEqual(loaded.profiles, list(DEFAULT_PROFILES))
        self.assertEqual(loaded.settings_path.read_text(), '{invalid')

    def test_build_python_command_runs_actual_utf8_script_from_folder_with_spaces(self):
        path = self.workspace.create_project(DEFAULT_PROFILES[0], 'Space project')
        self.workspace.save_file(path, 'print("Український Python")\n')
        compile_command, run = self.workspace.build_commands(DEFAULT_PROFILES[0], path,
                                                              python=RuntimeTool(Path(sys.executable), ('-u', '-X', 'utf8')))
        self.assertIsNone(compile_command)
        result = subprocess.run(run, cwd=path.parent, capture_output=True, timeout=10, check=True)
        self.assertEqual(result.stdout.decode('utf8').strip(), 'Український Python')

    def test_cpp_compile_arguments_and_output_are_not_shell_text(self):
        path = self.workspace.create_project(DEFAULT_PROFILES[1])
        compile_command, run = self.workspace.build_commands(DEFAULT_PROFILES[1], path,
                                                              compiler=RuntimeTool(Path(sys.executable)))
        self.assertIn(str(path), compile_command)
        self.assertIn('-std=c++17', compile_command)
        self.assertEqual(compile_command[compile_command.index('-o') + 1], run[0])
        self.assertTrue(Path(run[0]).parent.is_relative_to(self.root / 'builds'))

    def test_missing_runtime_and_compiler_produce_clear_errors(self):
        path = self.workspace.create_project(DEFAULT_PROFILES[0])
        with patch('mechahub.core.code_workspace.find_python', return_value=None):
            with self.assertRaisesRegex(CodeWorkspaceError, 'Python не знайдено'):
                self.workspace.build_commands(DEFAULT_PROFILES[0], path)
        with patch('mechahub.core.code_workspace.find_compiler', return_value=None):
            with self.assertRaisesRegex(CodeWorkspaceError, r'g\+\+'):
                self.workspace.build_commands(DEFAULT_PROFILES[1], path)

    def test_python_detection_prefers_bundled_launcher_and_never_frozen_mechahub(self):
        bundle = self.root / 'bundle'
        bundle.mkdir()
        executable = bundle / 'python.exe'
        executable.write_bytes(b'fake detection fixture')
        launcher = bundle / 'run_user_code.py'
        launcher.write_text('pass', encoding='utf8')
        tool = find_python(bundled_dir=bundle)
        self.assertEqual(tool.executable, executable)
        self.assertEqual(tool.arguments, ('-u', '-X', 'utf8', str(launcher)))
        with patch('mechahub.core.code_workspace.sys.frozen', True, create=True), \
                patch('mechahub.core.code_workspace._path_tool', return_value=None):
            self.assertIsNone(find_python(bundled_dir=self.root / 'missing'))

    def test_selected_mechahub_and_windows_store_alias_are_rejected(self):
        mechahub = self.root / 'MechaHub.exe'
        mechahub.write_bytes(b'fake')
        alias = self.root / 'WindowsApps' / 'python.exe'
        alias.parent.mkdir()
        alias.write_bytes(b'fake')
        for path in (mechahub, alias):
            with self.subTest(path=path), self.assertRaises(CodeWorkspaceError):
                find_python(path)


if __name__ == '__main__':
    unittest.main()
