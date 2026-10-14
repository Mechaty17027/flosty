import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mechahub.core.updates import parse_manifest, select_artifact
from tools import make_update_manifest
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
        self.assertNotIn('installer_download_url', manifest)

    def test_installer_hash_and_size_are_generated_from_separate_actual_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / 'MechaHub.exe'
            artifact.write_bytes(b'portable executable')
            installer = Path(folder) / 'MechaHubSetup.exe'
            installer.write_bytes(b'installer executable with different bytes')
            version = current_version()
            manifest = make_manifest('sample-owner/MechaHub', 'v' + version, artifact,
                                     ' Update ', installer_artifact=installer)
        self.assertEqual(manifest['sha256'], hashlib.sha256(b'portable executable').hexdigest())
        self.assertEqual(manifest['installer_sha256'],
                         hashlib.sha256(b'installer executable with different bytes').hexdigest())
        self.assertEqual(manifest['installer_size'], len(b'installer executable with different bytes'))
        self.assertEqual(manifest['installer_download_url'],
                         f'https://github.com/sample-owner/MechaHub/releases/download/v{version}/MechaHubSetup.exe')
        parsed = parse_manifest(json.dumps(manifest).encode('utf-8'))
        self.assertEqual(select_artifact(parsed, installed=True).sha256, manifest['installer_sha256'])
        self.assertEqual(select_artifact(parsed, installed=False).sha256, manifest['sha256'])

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

    def test_missing_wrong_empty_or_oversize_installer_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / 'MechaHub.exe'
            artifact.write_bytes(b'portable executable')
            for name in ('MechaHubSetup.exe', 'wrong.exe'):
                installer = Path(folder) / name
                with self.subTest(name=name, state='missing'), self.assertRaises(ValueError):
                    make_manifest('user/repo', 'v' + current_version(), artifact, installer_artifact=installer)
                installer.touch()
                with self.subTest(name=name, state='empty'), self.assertRaises(ValueError):
                    make_manifest('user/repo', 'v' + current_version(), artifact, installer_artifact=installer)
            installer = Path(folder) / 'MechaHubSetup.exe'
            installer.write_bytes(b'installer')
            real_stat = Path.stat

            def mocked_stat(path, *args, **kwargs):
                if path == installer:
                    info = real_stat(path, *args, **kwargs)
                    return SimpleNamespace(st_mode=info.st_mode,
                                           st_size=make_update_manifest.MAX_ARTIFACT_BYTES + 1)
                return real_stat(path, *args, **kwargs)

            with patch.object(Path, 'stat', mocked_stat), self.assertRaisesRegex(ValueError, '512 MiB'):
                make_manifest('user/repo', 'v' + current_version(), artifact, installer_artifact=installer)

    def test_cli_can_generate_manifest_with_installer_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / 'MechaHub.exe'
            artifact.write_bytes(b'portable')
            installer = Path(folder) / 'MechaHubSetup.exe'
            installer.write_bytes(b'installer')
            output = Path(folder) / 'update.json'
            arguments = ['make_update_manifest.py', '--repository', 'user/repo',
                         '--tag', 'v' + current_version(), '--artifact', str(artifact),
                         '--installer-artifact', str(installer), '--output', str(output)]
            with patch('sys.argv', arguments):
                self.assertEqual(make_update_manifest.main(), 0)
            manifest = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(manifest['installer_sha256'], hashlib.sha256(b'installer').hexdigest())
        self.assertEqual(manifest['installer_size'], len(b'installer'))

    def test_manifest_does_not_publish_notes_the_updater_cannot_read(self):
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / 'MechaHub.exe'
            artifact.write_bytes(b'content')
            for notes in ('contains\0nul', 'я' * 40000):
                with self.subTest(length=len(notes)), self.assertRaises(ValueError):
                    make_manifest('user/repo', 'v' + current_version(), artifact, notes)
