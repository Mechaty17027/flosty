"""Discovery and launcher regressions; never start apps or system commands."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mechahub.core import apps, launch


class AppDiscoveryTests(unittest.TestCase):
    def test_display_icon_preserves_commas_and_removes_only_resource_index(self):
        self.assertEqual(apps._clean_display_icon('"C:\\Games, Tools\\game.exe",-12'),
                         r'C:\Games, Tools\game.exe')
        self.assertEqual(apps._clean_display_icon(r'C:\Games, Tools\game.exe,0'),
                         r'C:\Games, Tools\game.exe')
        self.assertEqual(apps._clean_display_icon(r'C:\Games, Tools\game.exe'),
                         r'C:\Games, Tools\game.exe')
        self.assertEqual(apps._clean_display_icon(''), '')

    def test_display_icon_expands_environment_variables(self):
        with patch.dict(os.environ, {'MECHAHUB_TEST_DIR': 'test-directory'}):
            reference = '%MECHAHUB_TEST_DIR%' if os.name == 'nt' else '$MECHAHUB_TEST_DIR'
            self.assertEqual(apps._clean_display_icon(f'"{reference}/app.exe",0'),
                             'test-directory/app.exe')

    def test_install_executable_ignores_installer_and_helper(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            for name in ('setup.exe', 'unins.exe', 'helper.exe', 'updater.exe', 'app.exe', 'long-name.exe'):
                (folder / name).touch()
            self.assertEqual(apps._best_install_executable(folder), str(folder / 'app.exe'))
            self.assertEqual(apps._best_install_executable(folder / 'missing'), '')

    def test_registry_falls_back_from_dll_missing_or_uninstaller_icon_and_closes_keys(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            executable = folder / 'app.exe'
            executable.touch()
            (folder / 'icon.dll').touch()
            (folder / 'unins000.exe').touch()
            values = {
                'DLL': {'DisplayName': 'DLL icon app', 'DisplayIcon': str(folder / 'icon.dll') + ',0',
                        'InstallLocation': str(folder)},
                'Missing': {'DisplayName': 'Missing icon app', 'DisplayIcon': str(folder / 'gone.exe'),
                            'InstallLocation': f'"{folder}"'},
                'Uninstaller': {'DisplayName': 'Uninstaller icon app',
                                'DisplayIcon': str(folder / 'unins000.exe') + ',0',
                                'InstallLocation': str(folder)},
                'Nameless': {'DisplayIcon': str(executable)},
                'Denied': {},
            }
            keys = []

            class Key:
                def __init__(self, name):
                    self.name, self.closed = name, False
                    keys.append(self)

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    self.closed = True

            def open_key(parent, name, _reserved, access):
                if isinstance(parent, Key):
                    if name == 'Denied':
                        raise PermissionError('inaccessible subkey')
                    return Key(name)
                return Key('root')

            def enum_key(_key, index):
                if index >= len(values):
                    raise OSError('no more keys')
                return list(values)[index]

            def query_value(key, name):
                if name not in values[key.name]:
                    raise FileNotFoundError(name)
                return values[key.name][name], 1

            registry = SimpleNamespace(HKEY_LOCAL_MACHINE='machine', HKEY_CURRENT_USER='user',
                                       KEY_WOW64_64KEY=256, KEY_WOW64_32KEY=512, KEY_READ=1,
                                       OpenKey=Mock(side_effect=open_key), EnumKey=enum_key,
                                       QueryValueEx=query_value)
            with patch.object(apps.sys, 'platform', 'win32'), patch.dict('sys.modules', {'winreg': registry}):
                found = apps._registry_programs()
            self.assertEqual({item['name'] for item in found},
                             {'DLL icon app', 'Missing icon app', 'Uninstaller icon app'})
            self.assertTrue(all(item['path'] == str(executable) for item in found))
            self.assertTrue(all(key.closed for key in keys))
            roots = [call for call in registry.OpenKey.call_args_list if not isinstance(call.args[0], Key)]
            self.assertEqual({call.args[3] for call in roots}, {257, 513})
            self.assertEqual(len(roots), 4)

    def test_push_unique_rejects_directories_and_nonlaunchable_files(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            exe = folder / 'example.exe'
            exe.touch()
            (folder / 'icon.dll').touch()
            (folder / 'folder.exe').mkdir()
            rows, seen = [], set()
            for name in ('icon.dll', 'folder.exe', 'missing.exe'):
                apps._push_unique(rows, seen, 'Rejected', str(folder / name), 'Test')
            apps._push_unique(rows, seen, 'Example', str(exe), 'Test')
            apps._push_unique(rows, seen, 'EXAMPLE', str(exe), 'Duplicate')
            self.assertEqual(rows, [{'name': 'Example', 'path': str(exe), 'source': 'Test'}])

    def test_start_menu_finds_nested_links_and_ignores_documents(self):
        with tempfile.TemporaryDirectory() as d:
            programs = Path(d) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Games'
            programs.mkdir(parents=True)
            (programs / 'Game.lnk').touch()
            (programs / 'Web.url').touch()
            (programs / 'Readme.txt').touch()
            with patch.object(apps.sys, 'platform', 'win32'), \
                    patch.dict(os.environ, {'APPDATA': d, 'PROGRAMDATA': str(Path(d) / 'missing')}):
                found = apps._start_menu_programs()
            self.assertEqual({item['name'] for item in found}, {'Game', 'Web'})
            self.assertTrue(all(item['source'] == 'Пуск · Games' for item in found))

    def test_limit_deduplication_and_sorting(self):
        menu = [{'name': 'Zulu', 'path': '/z.exe', 'source': 'Menu'},
                {'name': 'Alpha', 'path': '/a.exe', 'source': 'Menu'}]
        registry = [{'name': 'ALPHA', 'path': '/a.exe', 'source': 'Registry'}]
        with patch.object(apps, '_start_menu_programs', return_value=menu), \
                patch.object(apps, '_registry_programs', return_value=registry):
            self.assertEqual(apps.installed_programs(1), [menu[1]])
            self.assertEqual(apps.installed_programs(), [menu[1], menu[0]])
        with patch.object(apps, '_start_menu_programs') as scan:
            self.assertEqual(apps.installed_programs(0), [])
            self.assertEqual(apps.installed_programs(-1), [])
            scan.assert_not_called()


class LaunchTests(unittest.TestCase):
    def test_empty_or_missing_paths_never_launch(self):
        with patch.object(launch.os, 'startfile', create=True) as start, \
                patch.object(launch.subprocess, 'Popen') as popen:
            for value in ('', '  ', '""'):
                with self.assertRaises(ValueError):
                    launch.open_path(value)
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(FileNotFoundError):
                    launch.open_path(str(Path(d) / 'missing.exe'))
            start.assert_not_called()
            popen.assert_not_called()

    def test_windows_launch_accepts_quoted_path_and_sets_app_directory(self):
        with tempfile.TemporaryDirectory() as d:
            executable = Path(d) / 'Example App.exe'
            executable.touch()
            with patch.object(launch.sys, 'platform', 'win32'), \
                    patch.object(launch.os, 'startfile', create=True) as start:
                launch.open_path(f' "{executable}" ')
            start.assert_called_once_with(str(executable), cwd=str(executable.parent))

    def test_launch_expands_environment_path(self):
        with tempfile.TemporaryDirectory() as d:
            reference = '%MECHAHUB_TEST_DIR%' if os.name == 'nt' else '$MECHAHUB_TEST_DIR'
            with patch.dict(os.environ, {'MECHAHUB_TEST_DIR': d}), \
                    patch.object(launch.sys, 'platform', 'win32'), \
                    patch.object(launch.os, 'startfile', create=True) as start:
                launch.open_path(reference)
            start.assert_called_once_with(d, cwd=str(Path(d).parent))

    def test_system_tools_use_windows_directory_and_reject_unknown_actions(self):
        with patch.object(launch.sys, 'platform', 'win32'), \
                patch.dict(os.environ, {'SystemRoot': r'C:\Windows'}), \
                patch.object(launch.subprocess, 'Popen') as popen, \
                patch.object(launch.os, 'startfile', create=True) as start:
            launch.system_action('explorer')
            popen.assert_called_with([str(Path(r'C:\Windows') / 'explorer.exe')])
            launch.system_action('taskmgr')
            popen.assert_called_with([str(Path(r'C:\Windows') / 'System32' / 'taskmgr.exe')])
            launch.system_action('settings')
            start.assert_called_once_with('ms-settings:')
            with self.assertRaises(ValueError):
                launch.system_action('shutdown')
            self.assertEqual(popen.call_count, 2)

    def test_nonwindows_system_actions_are_rejected(self):
        with patch.object(launch.sys, 'platform', 'linux'), \
                patch.object(launch.subprocess, 'Popen') as popen:
            with self.assertRaises(OSError):
                launch.system_action('taskmgr')
            popen.assert_not_called()


@unittest.skipUnless(os.name == 'nt', 'Windows batch launchers')
class BatchLauncherTests(unittest.TestCase):
    def test_debug_launcher_preserves_exit_code_and_handles_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / 'MechaHub test ! folder'
            project.mkdir()
            launcher = project / 'RUN_DEBUG.bat'
            shutil.copy2(Path(__file__).resolve().parents[1] / launcher.name, launcher)
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(project / '.venv')],
                           check=True, capture_output=True, timeout=30)
            for code in (0, 7):
                with self.subTest(exit_code=code):
                    (project / 'main.py').write_text(
                        f'from pathlib import Path\nprint(Path.cwd())\nraise SystemExit({code})\n',
                        encoding='utf-8')
                    completed = subprocess.run(
                        ['cmd.exe', '/d', '/c', str(launcher)], cwd=d, input='\n',
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(completed.returncode, code, completed.stdout + completed.stderr)
                    self.assertIn(str(project), completed.stdout)

    def test_run_and_build_report_missing_environment(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ('RUN_MECHAHUB.bat', 'RUN_DEBUG.bat', 'BUILD_EXE.bat'):
                with self.subTest(launcher=name):
                    launcher = Path(d) / name
                    shutil.copy2(Path(__file__).resolve().parents[1] / name, launcher)
                    completed = subprocess.run(
                        ['cmd.exe', '/d', '/c', str(launcher)], cwd=d, input='\n',
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                    self.assertIn('INSTALL_FIRST.bat', completed.stdout, completed.stderr)


if __name__ == '__main__':
    unittest.main()
