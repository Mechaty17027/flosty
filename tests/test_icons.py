"""Render actual Qt icons offscreen and isolate native shell provider behavior."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QFileInfo, QSize
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QApplication

from mechahub.ui import icons


class IconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        icons.clear_icon_cache()
        icons._cached_vector_icon.cache_clear()

    def assert_visible(self, image):
        self.assertFalse(image.isNull())
        self.assertTrue(any(image.pixelColor(x, y).alpha() > 0
                            for y in range(image.height()) for x in range(image.width())))

    def test_every_navigation_and_action_icon_renders_at_multiple_sizes(self):
        for name in icons._SHAPES:
            with self.subTest(icon=name):
                result = icons.icon(name)
                self.assertFalse(result.isNull())
                for size in (18, 32, 128):
                    image = result.pixmap(size, size).toImage()
                    self.assertEqual(image.size(), QSize(size, size))
                    self.assert_visible(image)

    def test_color_disabled_mode_and_high_dpi_rendering(self):
        result = icons.icon('plus', '#E1358B')
        normal = result.pixmap(48, 48).toImage()
        pixel = normal.pixelColor(24, 24)
        self.assertEqual(pixel.name(), QColor('#E1358B').name())
        disabled = result.pixmap(48, 48, QIcon.Mode.Disabled).toImage()
        self.assertLess(disabled.pixelColor(24, 24).alpha(), pixel.alpha())
        retina = result.pixmap(QSize(40, 40), 2.0)
        self.assertEqual(retina.size(), QSize(80, 80))
        self.assertEqual(retina.devicePixelRatio(), 2.0)
        self.assert_visible(retina.toImage())

    def test_missing_empty_and_unknown_icons_have_visible_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(icons, '_file_icon_provider') as provider:
                for path in ('', '  ""  ', str(Path(d) / 'gone.exe')):
                    self.assert_visible(icons.app_icon(path).pixmap(64, 64).toImage())
                provider.assert_not_called()
        self.assert_visible(icons.icon('unknown-name').pixmap(24, 24).toImage())

    def test_provider_is_cached_and_uses_absolute_qfileinfo(self):
        with tempfile.TemporaryDirectory() as d:
            executable = Path(d) / 'Example.exe'
            executable.touch()
            provider = Mock()
            provider.icon.return_value = icons.icon('cpu')
            with patch.object(icons, '_file_icon_provider', return_value=provider):
                first = icons.app_icon(str(executable))
                second = icons.app_icon(f'"{executable}"')
            provider.icon.assert_called_once()
            argument = provider.icon.call_args.args[0]
            self.assertIsInstance(argument, QFileInfo)
            self.assertTrue(argument.isAbsolute())
            self.assertEqual(first.cacheKey(), second.cacheKey())

    def test_native_icons_render_for_existing_folder_and_file(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / 'sample.txt'
            file.write_text('sample', encoding='utf-8')
            for path in (d, str(file)):
                with self.subTest(path=path):
                    self.assert_visible(icons.app_icon(path).pixmap(48, 48).toImage())

    @unittest.skipUnless(os.name == 'nt', 'Windows shell shortcuts')
    def test_windows_executable_and_real_shortcut_render_native_icons(self):
        with tempfile.TemporaryDirectory() as d:
            shortcut = Path(d) / 'Python Test.lnk'
            environment = dict(os.environ, MECHAHUB_TEST_LINK=str(shortcut), MECHAHUB_TEST_EXE=sys.executable)
            # Create only a temporary shortcut; never execute its target.
            command = (
                "$ErrorActionPreference = 'Stop'\n"
                "$shell = New-Object -ComObject WScript.Shell\n"
                "$link = $shell.CreateShortcut($env:MECHAHUB_TEST_LINK)\n"
                "$link.TargetPath = $env:MECHAHUB_TEST_EXE\n"
                "$link.Save()\n"
            )
            completed = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', '-'],
                input=command, text=True, env=environment, capture_output=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(shortcut.is_file(), completed.stderr)
            for path in (sys.executable, str(shortcut)):
                with self.subTest(path=path):
                    self.assert_visible(icons.app_icon(path).pixmap(48, 48).toImage())

    def test_null_or_failed_native_icon_uses_stylized_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            provider = Mock()
            provider.icon.return_value = QIcon()
            with patch.object(icons, '_file_icon_provider', return_value=provider):
                self.assert_visible(icons.app_icon(d).pixmap(48, 48).toImage())
                icons.clear_icon_cache()
                provider.icon.side_effect = OSError('shell unavailable')
                self.assert_visible(icons.app_icon(d).pixmap(48, 48).toImage())

    def test_file_change_and_explicit_refresh_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / 'app.exe'
            file.write_bytes(b'a')
            provider = Mock()
            provider.icon.return_value = icons.icon('apps')
            with patch.object(icons, '_file_icon_provider', return_value=provider):
                icons.app_icon(str(file))
                file.write_bytes(b'new-version')
                icons.app_icon(str(file))
                self.assertEqual(provider.icon.call_count, 2)
                icons.clear_icon_cache()
                icons.app_icon(str(file))
                self.assertEqual(provider.icon.call_count, 3)

    def test_caches_are_bounded(self):
        for number in range(icons.VECTOR_ICON_CACHE_LIMIT + 5):
            icons.icon('home', f'#{number:06x}')
        self.assertEqual(icons._cached_vector_icon.cache_info().currsize, icons.VECTOR_ICON_CACHE_LIMIT)
        with tempfile.TemporaryDirectory() as d:
            provider = Mock()
            provider.icon.return_value = icons.icon('apps')
            with patch.object(icons, '_file_icon_provider', return_value=provider):
                for number in range(icons.APP_ICON_CACHE_LIMIT + 5):
                    file = Path(d) / f'{number}.exe'
                    file.touch()
                    icons.app_icon(str(file))
            self.assertEqual(icons._cached_app_icon.cache_info().currsize, icons.APP_ICON_CACHE_LIMIT)

    def test_worker_thread_is_rejected_before_shell_calls(self):
        with patch.object(icons, '_file_icon_provider') as provider, ThreadPoolExecutor(max_workers=1) as worker:
            with self.assertRaisesRegex(RuntimeError, 'GUI thread'):
                worker.submit(icons.app_icon, str(Path.cwd())).result(timeout=5)
            with self.assertRaisesRegex(RuntimeError, 'GUI thread'):
                worker.submit(icons.icon, 'home').result(timeout=5)
            provider.assert_not_called()

    def test_invalid_color_is_rejected(self):
        with self.assertRaises(ValueError):
            icons.icon('home', '<invalid>')


if __name__ == '__main__':
    unittest.main()
