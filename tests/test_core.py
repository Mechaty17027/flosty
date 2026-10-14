"""Run from project root: python -m unittest discover -s tests -v"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mechahub.core.organizer import apply_organization, category_for, plan_organization
from mechahub.core.settings import read_shortcuts, write_shortcuts
from mechahub.core.system import format_bytes, format_uptime


class CoreTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_bytes(1024), '1.0 КБ')
        self.assertEqual(format_uptime(3660), '1 год 1 хв')

    def test_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'app' / 'config.json'
            items = [{'name': 'Тест', 'path': 'C:\\Apps\\example.exe'}]
            write_shortcuts(items, path)
            self.assertEqual(read_shortcuts(path), items)
            path.write_text('not-json', encoding='utf-8')
            self.assertEqual(read_shortcuts(path), [])

    def test_organization_safe_duplicates_and_skips(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / 'photo.jpg').write_bytes(b'a')
            (folder / 'photo2.JPG').write_bytes(b'b')
            (folder / 'keep.part').write_bytes(b'x')
            (folder / 'Зображення').mkdir()
            (folder / 'Зображення' / 'photo.jpg').write_bytes(b'existing')
            plans = plan_organization(folder)
            self.assertEqual(len(plans), 2)
            self.assertTrue(any(p.destination.name == 'photo (2).jpg' for p in plans))
            moved, errors = apply_organization(plans)
            self.assertEqual(moved, 2)
            self.assertEqual(errors, [])
            self.assertEqual((folder / 'Зображення' / 'photo.jpg').read_bytes(), b'existing')
            self.assertEqual((folder / 'Зображення' / 'photo (2).jpg').read_bytes(), b'a')
            self.assertTrue((folder / 'keep.part').exists())
            self.assertEqual(plan_organization(folder), [])

    def test_organizer_file_created_after_preview_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / 'notes.pdf').write_bytes(b'first')
            plans = plan_organization(folder)
            plans[0].destination.parent.mkdir(parents=True, exist_ok=True)
            plans[0].destination.write_bytes(b'new file')
            moved, errors = apply_organization(plans)
            self.assertEqual((moved, errors), (1, []))
            self.assertEqual((folder / 'Документи' / 'notes.pdf').read_bytes(), b'new file')
            self.assertEqual((folder / 'Документи' / 'notes (2).pdf').read_bytes(), b'first')


if __name__ == '__main__':
    unittest.main()
