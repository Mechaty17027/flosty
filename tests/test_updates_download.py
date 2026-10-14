"""Artifact download checks are isolated and never access a network."""
import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import Request

from mechahub.core import updates_download as downloader
from mechahub.core.updates import ReleaseManifest, UpdateCancelled, UpdateError


class ArtifactResponse(io.BytesIO):
    def __init__(self, payload, *, length=None, url='https://releases.example/download.exe'):
        super().__init__(payload)
        self.headers = {} if length is None else {'Content-Length': str(length)}
        self.url = url

    def geturl(self):
        return self.url


class ArtifactDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        # Windows runners may expose TEMP through an 8.3 alias (RUNNER~1).
        # The downloader returns canonical paths, so fixtures use the same form.
        self.folder = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.payload = b'Mocked release artifact\x00\x01\xff'

    def manifest(self, **changes):
        values = dict(version='0.5.0', download_url='https://releases.example/download.exe',
                      sha256=hashlib.sha256(self.payload).hexdigest(), size=len(self.payload))
        values.update(changes)
        return ReleaseManifest(**values)

    def download(self, response=None, *, manifest=None, event=None, progress=None):
        if response is None:
            response = ArtifactResponse(self.payload, length=len(self.payload))
        opener = Mock()
        opener.open.return_value = response
        with patch.object(downloader, 'build_opener', return_value=opener):
            result = downloader.download_release(manifest or self.manifest(), self.folder, event, progress)
        return result, opener, response

    def assert_empty(self):
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_success_verifies_hash_size_progress_timeout_and_closes_response(self):
        progress = Mock()
        with patch.object(downloader.os, 'fsync', wraps=os.fsync) as synced:
            result, opener, response = self.download(progress=progress)
        self.assertEqual(result.path, self.folder / 'MechaHub-0.5.0.exe')
        self.assertEqual(result.path.read_bytes(), self.payload)
        self.assertEqual(result.bytes_received, len(self.payload))
        self.assertEqual(result.sha256, hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 4.0)
        self.assertEqual(opener.open.call_args.args[0].full_url, self.manifest().download_url)
        self.assertTrue(response.closed)
        synced.assert_called_once()
        self.assertEqual(progress.call_args_list[0].args, (0, len(self.payload)))
        self.assertEqual(progress.call_args_list[-1].args, (len(self.payload), len(self.payload)))
        self.assertEqual(list(self.folder.iterdir()), [result.path])

    def test_missing_length_and_size_report_unknown_total(self):
        progress = Mock()
        result, _, _ = self.download(ArtifactResponse(self.payload), manifest=self.manifest(size=None),
                                    progress=progress)
        self.assertEqual(progress.call_args_list[0].args, (0, None))
        self.assertEqual(progress.call_args_list[-1].args, (len(self.payload), None))
        self.assertEqual(result.path.read_bytes(), self.payload)

    def test_size_from_header_drives_progress_when_manifest_omits_it(self):
        progress = Mock()
        self.download(manifest=self.manifest(size=None), progress=progress)
        self.assertEqual(progress.call_args_list[0].args, (0, len(self.payload)))

    def test_checksum_is_case_insensitive(self):
        result, _, _ = self.download(manifest=self.manifest(sha256=self.manifest().sha256.upper()))
        self.assertEqual(result.sha256, self.manifest().sha256)

    def test_hash_mismatch_removes_partial_file(self):
        response = ArtifactResponse(self.payload)
        with self.assertRaisesRegex(UpdateError, 'SHA-256'):
            self.download(response, manifest=self.manifest(sha256='a' * 64))
        self.assertTrue(response.closed)
        self.assert_empty()

    def test_invalid_artifact_metadata_never_opens_connection(self):
        cases = [dict(sha256=''), dict(sha256='g' * 64), dict(sha256=None),
                 dict(download_url='http://releases.example/download.exe'),
                 dict(download_url='https://user:pass@releases.example/download.exe'),
                 dict(version='../escape'), dict(size=-1), dict(size=True), dict(size='24'),
                 dict(size=downloader.MAX_ARTIFACT_BYTES + 1)]
        with patch.object(downloader, 'build_opener') as opener:
            for changes in cases:
                with self.subTest(changes=changes), self.assertRaises(UpdateError):
                    downloader.download_release(self.manifest(**changes), self.folder)
            opener.assert_not_called()
        self.assert_empty()

    def test_safe_filename_ignores_remote_name_query_and_fragment(self):
        manifest = self.manifest(download_url='https://releases.example/a/b/strange.MSI?filename=evil.exe#other')
        result, _, _ = self.download(manifest=manifest)
        self.assertEqual(result.path.name, 'MechaHub-0.5.0.msi')
        self.assertEqual(result.path.parent, self.folder)

    def test_only_supported_artifact_extensions_are_accepted(self):
        for suffix in ('.exe', '.zip', '.msix', '.msi'):
            with self.subTest(suffix=suffix):
                result, _, _ = self.download(manifest=self.manifest(
                    download_url=f'https://releases.example/release{suffix}'))
                self.assertEqual(result.path.suffix, suffix)
        for path in ('release', 'release.exe.html', 'release.bat', 'release%2Eexe'):
            with self.subTest(path=path), patch.object(downloader, 'build_opener') as opener:
                with self.assertRaises(UpdateError):
                    downloader.download_release(self.manifest(
                        download_url=f'https://releases.example/{path}'), self.folder)
                opener.assert_not_called()

    def test_oversized_header_is_rejected_before_body_or_folder_creation(self):
        response = ArtifactResponse(self.payload, length=downloader.MAX_ARTIFACT_BYTES + 1)
        with patch.object(response, 'read1', wraps=response.read1) as read:
            with self.assertRaises(UpdateError):
                self.download(response)
            read.assert_not_called()
        self.assert_empty()

    def test_oversized_body_without_header_is_bounded_and_cleaned(self):
        response = ArtifactResponse(b'x' * 40)
        with patch.object(downloader, 'MAX_ARTIFACT_BYTES', 32), \
                patch.object(response, 'read1', wraps=response.read1) as read:
            with self.assertRaises(UpdateError):
                self.download(response, manifest=self.manifest(size=None))
            read.assert_called_once_with(33)
        self.assertTrue(response.closed)
        self.assert_empty()

    def test_invalid_and_mismatched_content_lengths_are_rejected(self):
        for length in ('unknown', -1, len(self.payload) + 1):
            with self.subTest(length=length), self.assertRaises(UpdateError):
                self.download(ArtifactResponse(self.payload, length=length))
            self.assert_empty()

    def test_truncated_and_overlong_body_are_rejected_and_cleaned(self):
        for payload in (self.payload[:-1], self.payload + b'x'):
            with self.subTest(payload=payload), self.assertRaises(UpdateError):
                self.download(ArtifactResponse(payload))
            self.assert_empty()

    def test_header_is_also_checked_for_truncation_when_manifest_omits_size(self):
        with self.assertRaises(UpdateError):
            self.download(ArtifactResponse(self.payload[:-1], length=len(self.payload)),
                          manifest=self.manifest(size=None))
        self.assert_empty()

    def test_cancellation_before_request_has_no_side_effects(self):
        event = threading.Event()
        event.set()
        with patch.object(downloader, 'build_opener') as opener:
            with self.assertRaises(UpdateCancelled):
                downloader.download_release(self.manifest(), self.folder, event)
            opener.assert_not_called()
        self.assert_empty()

    def test_cancellation_during_read_removes_partial_file(self):
        event = threading.Event()
        response = ArtifactResponse(self.payload)
        real_read = response.read1

        def read_and_cancel(size):
            event.set()
            return real_read(size)

        with patch.object(response, 'read1', side_effect=read_and_cancel):
            with self.assertRaises(UpdateCancelled):
                self.download(response, event=event)
        self.assertTrue(response.closed)
        self.assert_empty()

    def test_cancellation_after_progress_prevents_publication(self):
        event = threading.Event()

        def progress(received, _total):
            if received:
                event.set()

        with self.assertRaises(UpdateCancelled):
            self.download(event=event, progress=progress)
        self.assert_empty()

    def test_deadline_after_read_removes_partial_file(self):
        response = ArtifactResponse(self.payload)
        real_read = response.read1
        with patch.object(downloader.time, 'monotonic', return_value=0) as clock:
            def slow_read(size):
                result = real_read(size)
                clock.return_value = 301
                return result

            with patch.object(response, 'read1', side_effect=slow_read):
                with self.assertRaisesRegex(UpdateError, 'надто довго'):
                    self.download(response)
        self.assertTrue(response.closed)
        self.assert_empty()

    def test_verified_existing_download_is_reused_without_connection(self):
        cached = self.folder / 'MechaHub-0.5.0.exe'
        cached.write_bytes(self.payload)
        progress = Mock()
        with patch.object(downloader, 'build_opener') as opener:
            result = downloader.download_release(self.manifest(), self.folder, progress=progress)
            opener.assert_not_called()
        self.assertEqual(result.path, cached)
        self.assertEqual(result.bytes_received, len(self.payload))
        self.assertEqual(result.sha256, self.manifest().sha256)
        progress.assert_called_once_with(len(self.payload), len(self.payload))
        self.assertEqual(list(self.folder.iterdir()), [cached])

    def test_corrupt_cache_is_preserved_and_new_numbered_file_is_reused(self):
        cached = self.folder / 'MechaHub-0.5.0.exe'
        corrupt = b'x' * len(self.payload)
        cached.write_bytes(corrupt)
        result, opener, _ = self.download()
        opener.open.assert_called_once()
        self.assertEqual(result.path.name, 'MechaHub-0.5.0 (2).exe')
        self.assertEqual(cached.read_bytes(), corrupt)
        with patch.object(downloader, 'build_opener') as second_opener:
            reused = downloader.download_release(self.manifest(), self.folder)
            second_opener.assert_not_called()
        self.assertEqual(reused, result)
        self.assertEqual(len(list(self.folder.iterdir())), 2)

    def test_cancellation_during_cache_verification_preserves_existing_file(self):
        cached = self.folder / 'MechaHub-0.5.0.exe'
        cached.write_bytes(self.payload)
        event = threading.Event()
        manifest = self.manifest()
        hasher = hashlib.sha256()
        proxy = Mock(wraps=hasher)

        def update_and_cancel(chunk):
            hasher.update(chunk)
            event.set()

        proxy.update.side_effect = update_and_cancel
        with patch.object(downloader.hashlib, 'sha256', return_value=proxy), \
                patch.object(downloader, 'build_opener') as opener:
            with self.assertRaises(UpdateCancelled):
                downloader.download_release(manifest, self.folder, event)
            opener.assert_not_called()
        self.assertEqual(cached.read_bytes(), self.payload)
        self.assertEqual(list(self.folder.iterdir()), [cached])

    def test_regular_looking_windows_reparse_file_is_not_reused(self):
        cached = self.folder / 'MechaHub-0.5.0.exe'
        cached.write_bytes(self.payload)
        real_lstat = Path.lstat

        def reported_link(path):
            if path == cached:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            return real_lstat(path)

        with patch.object(Path, 'lstat', autospec=True, side_effect=reported_link):
            result, opener, _ = self.download()
        opener.open.assert_called_once()
        self.assertEqual(result.path.name, 'MechaHub-0.5.0 (2).exe')
        self.assertEqual(cached.read_bytes(), self.payload)

    def test_cache_hashing_has_a_total_byte_budget(self):
        manifest = self.manifest()
        for index in range(1, 4):
            name = 'MechaHub-0.5.0.exe' if index == 1 else f'MechaHub-0.5.0 ({index}).exe'
            (self.folder / name).write_bytes(self.payload if index == 3 else b'x' * len(self.payload))
        with patch.object(downloader, 'MAX_CACHE_VERIFY_BYTES', len(self.payload)):
            result, opener, _ = self.download(manifest=manifest)
        opener.open.assert_called_once()
        self.assertEqual(result.path.name, 'MechaHub-0.5.0 (4).exe')
        self.assertEqual(result.path.read_bytes(), self.payload)

    def test_unrelated_filename_is_preserved_and_not_reused(self):
        unrelated = self.folder / 'OtherProgram.exe'
        unrelated.write_bytes(self.payload)
        result, opener, _ = self.download()
        opener.open.assert_called_once()
        self.assertEqual(result.path.name, 'MechaHub-0.5.0.exe')
        self.assertEqual(unrelated.read_bytes(), self.payload)

    def test_existing_file_and_directory_are_preserved(self):
        existing = self.folder / 'MechaHub-0.5.0.exe'
        existing.write_bytes(b'keep existing')
        existing_directory = self.folder / 'MechaHub-0.5.0 (2).exe'
        existing_directory.mkdir()
        result, _, _ = self.download()
        self.assertEqual(result.path.name, 'MechaHub-0.5.0 (3).exe')
        self.assertEqual(existing.read_bytes(), b'keep existing')
        self.assertTrue(existing_directory.is_dir())
        self.assertEqual(result.path.read_bytes(), self.payload)
        self.assertFalse(any(path.suffix == '.part' for path in self.folder.iterdir()))

    def test_concurrent_publication_collision_retries_without_overwrite(self):
        destination = self.folder / 'MechaHub-0.5.0.exe'
        publish_name = 'rename' if os.name == 'nt' else 'link'
        real_publish = getattr(downloader.os, publish_name)
        calls = 0

        def concurrent_publish(source, target, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                Path(target).write_bytes(b'concurrent writer')
                raise FileExistsError('claimed')
            return real_publish(source, target, **kwargs)

        with patch.object(downloader.os, publish_name, side_effect=concurrent_publish):
            result, _, _ = self.download()
        self.assertEqual(destination.read_bytes(), b'concurrent writer')
        self.assertEqual(result.path.name, 'MechaHub-0.5.0 (2).exe')
        self.assertEqual(result.path.read_bytes(), self.payload)

    def test_https_redirect_handler_rejects_downgrade(self):
        handler = downloader._HttpsRedirectHandler()
        with self.assertRaises(UpdateError):
            handler.redirect_request(Request('https://releases.example/a.exe'), None, 302,
                                     'Found', {}, 'http://releases.example/b.exe')

    def test_disappearing_collision_retries_next_name(self):
        publish_name = 'rename' if os.name == 'nt' else 'link'
        real_publish = getattr(downloader.os, publish_name)
        calls = 0

        def disappearing_collision(source, target, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FileExistsError('another writer already removed the file')
            return real_publish(source, target, **kwargs)

        with patch.object(downloader.os, publish_name, side_effect=disappearing_collision):
            result, _, _ = self.download()
        self.assertEqual(result.path.name, 'MechaHub-0.5.0 (2).exe')
        self.assertEqual(result.path.read_bytes(), self.payload)

    def test_publication_failure_is_reported_and_removes_partial_file(self):
        publish_name = 'rename' if os.name == 'nt' else 'link'
        with patch.object(downloader.os, publish_name, side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(UpdateError, 'denied'):
                self.download()
        self.assert_empty()

    def test_insecure_final_response_is_rejected_without_saved_file(self):
        response = ArtifactResponse(self.payload, url='http://releases.example/download.exe')
        with self.assertRaises(UpdateError):
            self.download(response)
        self.assertTrue(response.closed)
        self.assert_empty()

    def test_connection_error_is_reported_and_no_file_is_saved(self):
        opener = Mock()
        opener.open.side_effect = URLError('offline')
        with patch.object(downloader, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(UpdateError, 'Не вдалося завантажити'):
                downloader.download_release(self.manifest(), self.folder)
        self.assert_empty()

    def test_output_failure_closes_response_and_removes_partial_file(self):
        response = ArtifactResponse(self.payload)
        with patch.object(downloader.os, 'fsync', side_effect=OSError('disk unavailable')):
            with self.assertRaisesRegex(UpdateError, 'disk unavailable'):
                self.download(response)
        self.assertTrue(response.closed)
        self.assert_empty()


if __name__ == '__main__':
    unittest.main()
