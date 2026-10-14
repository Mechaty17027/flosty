"""Update checks use mocked responses and never contact a release server."""
import io
import hashlib
import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import Request

from mechahub.core import updates


class ManifestResponse(io.BytesIO):
    def __init__(self, payload, *, length=None, url='https://updates.example/manifest.json'):
        super().__init__(payload)
        self.headers = {} if length is None else {'Content-Length': str(length)}
        self.url = url

    def geturl(self):
        return self.url


class UpdatesTests(unittest.TestCase):
    def payload(self, **changes):
        data = {'version': '0.5.0', 'download_url': 'https://releases.example/v0.5', 'notes': 'Нові функції'}
        data.update(changes)
        return json.dumps(data, ensure_ascii=False).encode('utf-8')

    def fetch(self, response, event=None):
        opener = Mock()
        opener.open.return_value = response
        with patch.object(updates, 'build_opener', return_value=opener):
            result = updates.fetch_manifest('https://updates.example/manifest.json', event)
        return result, opener

    def test_numeric_versions_are_ordered_and_padded(self):
        self.assertTrue(updates.is_newer_version('0.10.0', '0.9.9'))
        self.assertFalse(updates.is_newer_version('0.4', '0.4.0'))
        self.assertTrue(updates.is_newer_version('0.4.0.1', '0.4.0'))
        for version in ('v0.4.0', '0.4-beta', '0.-1', '', '0.4.0.0.1', None, 4):
            with self.subTest(version=version), self.assertRaises(updates.UpdateError):
                updates.version_key(version)

    def test_manifest_validates_all_fields_and_accepts_bom(self):
        result = updates.parse_manifest(b'\xef\xbb\xbf' + self.payload(notes='  Опис  '))
        self.assertEqual(result, updates.ReleaseManifest('0.5.0', 'https://releases.example/v0.5', 'Опис'))
        self.assertEqual(updates.parse_manifest(self.payload(download_url='http://releases.example/v0.5')).version, '0.5.0')

    def test_bad_manifest_shapes_encoding_and_fields_are_rejected(self):
        cases = [b'[]', b'null', b'invalid', b'\xff', self.payload(version=5),
                 self.payload(download_url='file:///installer.exe'), self.payload(notes=['note']),
                 self.payload(notes='bad\0note'), b'{"version": "0.5"}']
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(updates.UpdateError):
                updates.parse_manifest(payload)

    def test_download_manifest_requires_valid_checksum_https_and_bounded_size(self):
        checksum = hashlib.sha256(b'release').hexdigest()
        manifest = updates.parse_manifest(self.payload(
            download_url='https://releases.example/MechaHub.exe',
            sha256=checksum.upper(), size=7,
        ))
        self.assertEqual(manifest.sha256, checksum)
        self.assertEqual(manifest.size, 7)
        for changes in ({'sha256': 'bad'}, {'sha256': None}, {'size': -1}, {'size': True},
                        {'size': updates.MAX_ARTIFACT_BYTES + 1},
                        {'sha256': checksum, 'download_url': 'http://releases.example/MechaHub.exe'}):
            with self.subTest(changes=changes), self.assertRaises(updates.UpdateError):
                updates.parse_manifest(self.payload(**changes))

    def test_manifest_source_requires_https_without_credentials(self):
        for url in ('http://updates.example/file.json', 'https://', 'file:///manifest.json',
                    'https://user:pass@updates.example/file.json', 'https://example:70000/a',
                    'https://exa\nmple/a', 'https://example/a b'):
            with self.subTest(url=url), self.assertRaises(updates.UpdateError):
                updates.validate_manifest_url(url)
        self.assertEqual(updates.validate_manifest_url('  https://updates.example/a  '), 'https://updates.example/a')

    def test_successful_request_has_timeout_and_closes_response(self):
        response = ManifestResponse(self.payload())
        result, opener = self.fetch(response)
        self.assertEqual(result.version, '0.5.0')
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 4.0)
        self.assertEqual(opener.open.call_args.args[0].full_url, 'https://updates.example/manifest.json')
        self.assertTrue(response.closed)

    def test_declared_oversize_is_rejected_before_reading_body(self):
        response = ManifestResponse(self.payload(), length=updates.MAX_MANIFEST_BYTES + 1)
        with patch.object(response, 'read1', wraps=response.read1) as read:
            with self.assertRaises(updates.UpdateError):
                self.fetch(response)
            read.assert_not_called()
        self.assertTrue(response.closed)

    def test_undeclared_oversize_is_bounded_and_rejected(self):
        response = ManifestResponse(b'x' * (updates.MAX_MANIFEST_BYTES + 10))
        with self.assertRaises(updates.UpdateError):
            self.fetch(response)
        self.assertTrue(response.closed)

    def test_invalid_content_length_is_rejected(self):
        response = ManifestResponse(self.payload(), length='unknown')
        with self.assertRaises(updates.UpdateError):
            self.fetch(response)

    def test_cancellation_before_request_does_not_open_connection(self):
        event = threading.Event()
        event.set()
        with patch.object(updates, 'build_opener') as opener:
            with self.assertRaises(updates.UpdateCancelled):
                updates.fetch_manifest('https://updates.example/manifest.json', event)
            opener.assert_not_called()

    def test_cancellation_during_read_discards_result_and_closes_response(self):
        event = threading.Event()
        response = ManifestResponse(self.payload())
        real_read = response.read1

        def read_and_cancel(size):
            event.set()
            return real_read(size)

        with patch.object(response, 'read1', side_effect=read_and_cancel):
            with self.assertRaises(updates.UpdateCancelled):
                self.fetch(response, event)
        self.assertTrue(response.closed)

    def test_slow_response_stops_at_deadline_between_chunks(self):
        response = ManifestResponse(self.payload())
        with patch.object(updates.time, 'monotonic', side_effect=[10.0, 15.0]):
            with self.assertRaises(updates.UpdateError):
                self.fetch(response)
        self.assertTrue(response.closed)

    def test_connection_error_is_reported_without_fabricated_version(self):
        opener = Mock()
        opener.open.side_effect = URLError('offline')
        with patch.object(updates, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(updates.UpdateError, 'Не вдалося отримати'):
                updates.fetch_manifest('https://updates.example/manifest.json')

    def test_https_to_http_redirect_is_rejected(self):
        handler = updates._HttpsRedirectHandler()
        with self.assertRaises(updates.UpdateError):
            handler.redirect_request(Request('https://updates.example/a'), None, 302,
                                     'Found', {}, 'http://updates.example/b')

    def test_insecure_final_response_is_rejected(self):
        response = ManifestResponse(self.payload(), url='http://updates.example/manifest.json')
        with self.assertRaises(updates.UpdateError):
            self.fetch(response)


if __name__ == '__main__':
    unittest.main()
