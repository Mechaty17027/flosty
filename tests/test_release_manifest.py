import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.make_update_manifest import current_version, make_manifest, validate_tag


class ReleaseManifestTests(unittest.TestCase):
    def test_actual_artifact_hash_size_and_tag_are_used(self):
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / 'MechaHub.exe'
            artifact.write_bytes(b'a test executable')
            version = current_version()
            manifest = make_manifest('sample-owner/MechaHub', 'v' + version, artifact, ' Changes ')
        self.assertEqual(manifest['version'], version)
        self.assertEqual(manifest['sha256'], hashlib.sha256(b'a test executable').hexdigest())
        self.assertEqual(manifest['size'], len(b'a test executable'))
        self.assertEqual(manifest['notes'], 'Changes')
        self.assertEqual(manifest['download_url'], f'https://github.com/sample-owner/MechaHub/releases/download/v{version}/MechaHub.exe')

    def test_mismatched_tag_and_invalid_repository_cannot_generate_manifest(self):
        with self.assertRaises(ValueError):
            validate_tag('v999.0.0')
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / 'MechaHub.exe'
            artifact.write_bytes(b'content')
            for repository in ('', 'https://github.com/user/repo', '../repo', 'user/repo/extra'):
                with self.subTest(repository=repository), self.assertRaises(ValueError):
                    make_manifest(repository, 'v' + current_version(), artifact)

    def test_shell_characters_in_tag_are_rejected_as_literal_data(self):
        for tag in ('v$(1+1)', 'v0.4.0;Write-Output', 'v0.4.0";Write-Output'):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, 'Release tag must be'):
                validate_tag(tag)

    def test_missing_wrong_or_empty_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('MechaHub.exe', 'wrong.exe'):
                artifact = Path(folder) / name
                with self.assertRaises(ValueError):
                    make_manifest('user/repo', 'v' + current_version(), artifact)
                artifact.touch()
                with self.assertRaises(ValueError):
                    make_manifest('user/repo', 'v' + current_version(), artifact)

    def test_manifest_does_not_publish_notes_the_updater_cannot_read(self):
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / 'MechaHub.exe'
            artifact.write_bytes(b'content')
            for notes in ('contains\0nul', 'я' * 40000):
                with self.subTest(length=len(notes)), self.assertRaises(ValueError):
                    make_manifest('user/repo', 'v' + current_version(), artifact, notes)
