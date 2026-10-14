"""Regression tests for preview integrity and exclusive destination moves."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mechahub.core import organizer


class OrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'downloads'
        self.folder.mkdir()
        self.source = self.folder / 'notes.pdf'
        self.source.write_bytes(b'original')

    def _directory_link(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                self.skipTest('Directory symlinks are unavailable')
            # Windows junctions do not require administrator/symlink privileges.
            result = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                capture_output=True, check=False,
            )
            if result.returncode:
                self.skipTest('Directory symlinks and junctions are unavailable')

    def test_collision_during_move_does_not_overwrite_other_writer(self):
        plans = organizer.plan_organization(self.folder)
        target = plans[0].destination
        real_move = organizer._move_without_overwrite
        attempts = []

        def move_after_concurrent_write(source, destination, expected):
            attempts.append(destination)
            if len(attempts) == 1:
                destination.write_bytes(b'other writer')
            real_move(source, destination, expected)

        with patch.object(organizer, '_move_without_overwrite', side_effect=move_after_concurrent_write):
            self.assertEqual(organizer.apply_organization(plans), (1, []))
        self.assertEqual(target.read_bytes(), b'other writer')
        self.assertEqual(target.with_name('notes (2).pdf').read_bytes(), b'original')
        self.assertFalse(self.source.exists())
        self.assertEqual(len(attempts), 2)

    def test_file_modified_after_preview_is_left_in_place(self):
        plans = organizer.plan_organization(self.folder)
        self.source.write_bytes(b'new, longer content')
        moved, errors = organizer.apply_organization(plans)
        self.assertEqual(moved, 0)
        self.assertIn('змінився після перегляду', errors[0])
        self.assertEqual(self.source.read_bytes(), b'new, longer content')
        self.assertFalse(plans[0].destination.exists())

    def test_file_replaced_after_preview_is_left_in_place(self):
        plans = organizer.plan_organization(self.folder)
        replacement = self.folder / 'replacement.tmp'
        replacement.write_bytes(b'replaced')
        # Keep size and timestamp unchanged: the file identity must still differ.
        stamp = self.source.stat().st_mtime_ns
        os.utime(replacement, ns=(stamp, stamp))
        os.replace(replacement, self.source)
        moved, errors = organizer.apply_organization(plans)
        self.assertEqual(moved, 0)
        self.assertTrue(errors)
        self.assertEqual(self.source.read_bytes(), b'replaced')

    def test_missing_source_does_not_stop_other_files(self):
        second = self.folder / 'second.pdf'
        second.write_bytes(b'second')
        plans = organizer.plan_organization(self.folder)
        self.source.unlink()
        moved, errors = organizer.apply_organization(plans)
        self.assertEqual(moved, 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual((self.folder / 'Документи' / 'second.pdf').read_bytes(), b'second')

    def test_category_link_created_after_preview_is_rejected(self):
        plans = organizer.plan_organization(self.folder)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        category = plans[0].destination.parent
        self._directory_link(category, outside)
        moved, errors = organizer.apply_organization(plans)
        self.assertEqual(moved, 0)
        self.assertIn('посиланням', errors[0])
        self.assertTrue(self.source.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_selected_folder_replaced_by_link_is_rejected(self):
        plans = organizer.plan_organization(self.folder)
        original_folder = self.folder.with_name('original-downloads')
        self.folder.rename(original_folder)
        self._directory_link(self.folder, original_folder)
        moved, errors = organizer.apply_organization(plans)
        self.assertEqual(moved, 0)
        self.assertIn('папка змінилася', errors[0])
        self.assertEqual((original_folder / self.source.name).read_bytes(), b'original')

    def test_windows_junction_is_recognized_without_is_junction_api(self):
        info = SimpleNamespace(st_mode=stat.S_IFDIR, st_reparse_tag=0xA0000003)
        with patch.object(Path, 'lstat', return_value=info):
            self.assertTrue(organizer._is_link(self.folder))

    def test_dangling_link_destination_is_reserved(self):
        target = self.folder / 'notes (2).pdf'
        linked_folder = self.folder / 'missing'
        linked_folder.mkdir()
        self._directory_link(target, linked_folder)
        linked_folder.rmdir()
        self.assertEqual(
            organizer.free_destination(target, set()),
            self.folder / 'notes (2) (2).pdf',
        )

    def test_invalid_plan_cannot_move_outside_selected_folder(self):
        outside = Path(self.temp.name) / 'elsewhere' / 'notes.pdf'
        plan = organizer.MovePlan(self.source, outside, 'Документи')
        moved, errors = organizer.apply_organization([plan])
        self.assertEqual(moved, 0)
        self.assertTrue(errors)
        self.assertTrue(self.source.exists())
        self.assertFalse(outside.exists())

    def test_preview_keeps_absolute_paths_when_working_directory_changes(self):
        original_cwd = Path.cwd()
        try:
            os.chdir(self.folder.parent)
            plans = organizer.plan_organization(Path('downloads'))
            os.chdir(original_cwd)
            self.assertTrue(plans[0].source.is_absolute())
            self.assertEqual(organizer.apply_organization(plans), (1, []))
        finally:
            os.chdir(original_cwd)


if __name__ == '__main__':
    unittest.main()
