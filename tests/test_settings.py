"""Regression tests for complete, validated local shortcut saves."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from mechahub.core import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'config.json'
        self.original = [{'name': 'Редактор', 'path': r'C:\Apps\editor.exe'}]
        settings.write_shortcuts(self.original, self.path)

    def test_read_accepts_bom_and_filters_malformed_entries(self):
        entries = [
            {'name': '  Фото  ', 'path': '  C:\\Фото  '},
            {'name': '', 'path': 'C:\\Apps'},
            {'name': 'Не текст', 'path': 7},
            {'name': 'NUL', 'path': 'bad\0path'},
            None, ['name', 'path'], {'path': 'missing name'},
        ]
        self.path.write_text(json.dumps({'shortcuts': entries}), encoding='utf-8-sig')
        self.assertEqual(settings.read_shortcuts(self.path), [{'name': 'Фото', 'path': 'C:\\Фото'}])

    def test_invalid_json_shapes_and_encoding_are_safe(self):
        for payload in (b'\xff\xfe\x00', b'not-json', b'[]', b'{"shortcuts": {}}', b'null'):
            with self.subTest(payload=payload):
                self.path.write_bytes(payload)
                self.assertEqual(settings.read_shortcuts(self.path), [])

    def test_invalid_save_preserves_previous_config(self):
        for entries in (None, {}, [{'name': ' ', 'path': 'app'}],
                        [{'name': 'Bad', 'path': 'x\0y'}], [{'name': 1, 'path': 'app'}]):
            with self.subTest(entries=entries):
                with self.assertRaises(ValueError):
                    settings.write_shortcuts(entries, self.path)
                self.assertEqual(settings.read_shortcuts(self.path), self.original)
                self.assertEqual(list(self.folder.glob('*.tmp')), [])

    def test_save_normalizes_without_mutating_input(self):
        entries = [{'name': '  Нова  ', 'path': '  C:\\Нова  ', 'source': 'ignored'}]
        settings.write_shortcuts(entries, self.path)
        self.assertEqual(settings.read_shortcuts(self.path), [{'name': 'Нова', 'path': 'C:\\Нова'}])
        self.assertEqual(entries[0]['name'], '  Нова  ')

    def test_failed_replace_preserves_previous_config_and_cleans_temp(self):
        with patch.object(settings.os, 'replace', side_effect=PermissionError('config is locked')):
            with self.assertRaises(PermissionError):
                settings.write_shortcuts([{'name': 'New', 'path': 'new.exe'}], self.path)
        self.assertEqual(settings.read_shortcuts(self.path), self.original)
        self.assertEqual(list(self.folder.glob('*.tmp')), [])

    def test_failed_flush_does_not_replace_config(self):
        with patch.object(settings.os, 'fsync', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                settings.write_shortcuts([{'name': 'New', 'path': 'new.exe'}], self.path)
        self.assertEqual(settings.read_shortcuts(self.path), self.original)
        self.assertEqual(list(self.folder.glob('*.tmp')), [])

    def test_concurrent_saves_use_independent_complete_files(self):
        barrier = threading.Barrier(4)
        real_replace = settings.os.replace
        real_fsync = settings.os.fsync
        temp_paths = []

        def sync_together(descriptor):
            real_fsync(descriptor)
            barrier.wait(timeout=10)

        def replace_complete_file(source, destination):
            temp_paths.append(source)
            self.assertEqual(Path(source).parent, self.folder)
            json.loads(Path(source).read_text(encoding='utf-8'))
            real_replace(source, destination)

        entries = [[{'name': f'App {index}', 'path': f'app{index}.exe'}] for index in range(4)]
        with patch.object(settings.os, 'replace', side_effect=replace_complete_file), \
                patch.object(settings.os, 'fsync', side_effect=sync_together):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(settings.write_shortcuts, item, self.path) for item in entries]
                for future in futures:
                    future.result(timeout=15)
        self.assertEqual(len(set(temp_paths)), 4)
        self.assertIn(settings.read_shortcuts(self.path), entries)
        self.assertEqual(list(self.folder.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
