"""Isolated tests: no network access required."""
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from mechahub.core import speedtest as s


class SpeedDiagnosticsTests(unittest.TestCase):
    def test_mbps(self):
        self.assertAlmostEqual(s.mbps(1_000_000, 1), 8.0)
        self.assertAlmostEqual(s.mbps(0, 1), 0.0)

    def test_jitter(self):
        self.assertIsNone(s.jitter_ms([15]))
        self.assertEqual(s.jitter_ms([10, 20, 5]), 12.5)

    def test_ping_samples(self):
        text = 'Reply from 1.1.1.1: bytes=32 time=17ms TTL=60\nОтвет от: время=24мс\nВідповідь: час<1мс\n'
        self.assertEqual(s.parse_ping_replies(text), [17.0,24.0,.5])

    def test_ping_totals(self):
        self.assertEqual(s.parse_ping_sent_received('Packets: Sent = 10, Received = 9, Lost = 1'), (10,9))
        self.assertEqual(s.parse_ping_sent_received('10 packets transmitted, 8 received, 20% packet loss'), (10,8))
        self.assertIsNone(s.parse_ping_sent_received('No totals available'))

    def test_packet_delta(self):
        before = SimpleNamespace(packets_sent=10,packets_recv=15,errout=0,errin=1,dropout=0,dropin=2)
        after = SimpleNamespace(packets_sent=25,packets_recv=22,errout=1,errin=1,dropout=0,dropin=3)
        result = s.nic_packet_delta(before,after)
        self.assertEqual(result['packets_sent'],15)
        self.assertEqual(result['packets_recv'],7)
        self.assertEqual(result['dropin'],1)
        self.assertEqual(s.nic_packet_delta(None,None)['packets_recv'],None)

    def test_mocked_speed_run(self):
        stages=[]; readings=[]
        def measure(amount, report, cancelled, **kwargs):
            report(amount, 1.0)
            return amount, 1.0
        with patch.object(s,'choose_interface',return_value='Ethernet'), \
             patch.object(s,'nic_snapshot',return_value=None), \
             patch.object(s,'ping_quality',return_value={'sent':10,'received':9,'lost_percent':10.,'ping_ms':18.,'jitter_ms':2.,'error':None}), \
             patch.object(s,'_http_download',side_effect=measure), \
             patch.object(s,'_http_upload',side_effect=measure), \
             patch.object(s,'PHASE_SECONDS', 0.001):
            result = s.speed_test(stages.append, lambda kind,speed: readings.append((kind,speed)),threading.Event())
        self.assertGreater(result.download_mbps,0)
        self.assertGreater(result.upload_mbps,0)
        self.assertIn('download',stages)
        self.assertIn('upload',stages)
        self.assertIn('done',stages)
        self.assertTrue(readings)
        self.assertEqual(result.interface,'Ethernet')

    def test_cancelled(self):
        event=threading.Event(); event.set()
        with self.assertRaises(s.TestCancelled):
            s.speed_test(lambda stage:None,lambda *args:None,event)

    def test_ping_cancellation_terminates_and_reaps_its_process(self):
        event = threading.Event()
        process = Mock()
        process.poll.return_value = None
        def communicate(timeout=None):
            if not process.terminate.called:
                event.set()
                raise s.subprocess.TimeoutExpired('ping', timeout)
            return b'', b''
        process.communicate.side_effect = communicate
        with patch.object(s.subprocess, 'Popen', return_value=process):
            with self.assertRaises(s.TestCancelled):
                s.ping_quality(cancelled=event)
        process.terminate.assert_called_once()
        self.assertEqual(process.communicate.call_count, 2)
        process.kill.assert_not_called()

    def test_ping_without_recognizable_totals_does_not_guess_packet_loss(self):
        process = Mock()
        process.communicate.return_value = (b'Reply from 1.1.1.1: time=12ms', b'')
        with patch.object(s.subprocess, 'Popen', return_value=process):
            result = s.ping_quality()
        self.assertEqual(result['received'], 1)
        self.assertEqual(result['ping_ms'], 12)
        self.assertIsNone(result['sent'])
        self.assertIsNone(result['lost_percent'])

    def test_download_stops_at_requested_byte_limit_even_if_server_has_more(self):
        response = Mock()
        response.status = 200
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read1.side_effect = lambda limit: bytes(min(3, limit))
        readings = []
        with patch.object(s, 'urlopen', return_value=response):
            count, elapsed = s._http_download(8, lambda *reading: readings.append(reading), threading.Event())
        self.assertEqual(count, 8)
        self.assertEqual([call.args[0] for call in response.read1.call_args_list], [8, 5, 2])
        response.read.assert_not_called()
        self.assertEqual(readings[-1][0], 8)

    def test_cancelled_download_does_not_open_connection(self):
        event = threading.Event()
        event.set()
        with patch.object(s, 'urlopen') as open_request:
            with self.assertRaises(s.TestCancelled):
                s._http_download(10, lambda *args: None, event)
        open_request.assert_not_called()

    def test_download_cancellation_closes_response_and_counts_received_chunk(self):
        event = threading.Event()
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        def read_chunk(limit):
            event.set()
            return b'1234'
        response.read1.side_effect = read_chunk
        readings = []
        with patch.object(s, 'urlopen', return_value=response):
            with self.assertRaises(s.TestCancelled):
                s._http_download(10, lambda count, elapsed: readings.append(count), event)
        self.assertEqual(readings, [4])
        response.__exit__.assert_called_once()

    def test_upload_closes_connection_without_reading_unbounded_ack_body(self):
        conn = Mock()
        conn.getresponse.return_value.status = 200
        with patch.object(s.http.client, 'HTTPSConnection', return_value=conn):
            count, elapsed = s._http_upload(10, lambda *args: None, threading.Event())
        self.assertEqual(count, 10)
        self.assertEqual(sum(len(call.args[0]) for call in conn.send.call_args_list), 10)
        conn.getresponse.return_value.read.assert_not_called()
        conn.close.assert_called_once()

    def test_upload_cancellation_during_headers_closes_connection_before_body(self):
        event = threading.Event()
        conn = Mock()
        conn.endheaders.side_effect = event.set
        with patch.object(s.http.client, 'HTTPSConnection', return_value=conn):
            with self.assertRaises(s.TestCancelled):
                s._http_upload(10, lambda *args: None, event)
        conn.send.assert_not_called()
        conn.getresponse.assert_not_called()
        conn.close.assert_called_once()

    def test_slow_download_obeys_overall_deadline(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read1.return_value = b'1'
        with patch.object(s, 'urlopen', return_value=response), \
             patch.object(s.time, 'perf_counter', side_effect=[0, 0, 0, 0, 1.1, 1.1]):
            with self.assertRaises(TimeoutError):
                s._http_download(10, lambda *args: None, threading.Event(), deadline=1.0)
        self.assertEqual(response.read1.call_count, 1)
        response.__exit__.assert_called_once()

    def test_partial_transfer_failure_retains_actual_traffic_and_estimate(self):
        def broken_download(amount, report, cancelled, **kwargs):
            report(amount//2, 1.0)
            raise TimeoutError('read timeout')
        def upload(amount, report, cancelled, **kwargs):
            report(amount, 1.0)
            return amount, 1.0
        with patch.object(s, 'choose_interface', return_value=None), \
             patch.object(s, 'nic_snapshot', return_value=None), \
             patch.object(s, 'ping_quality', return_value={}), \
             patch.object(s, '_http_download', side_effect=broken_download), \
             patch.object(s, '_http_upload', side_effect=upload), \
             patch.object(s, 'MAX_DOWNLOAD_BYTES', 100), \
             patch.object(s, 'MAX_UPLOAD_BYTES', 80):
            result = s.speed_test(lambda stage: None, lambda *args: None, threading.Event())
        self.assertEqual(result.downloaded_bytes, 50)
        self.assertEqual(result.download_mbps, round(s.mbps(50, 1), 2))
        self.assertTrue(result.partial_download)
        self.assertEqual(result.error_download, 'read timeout')
        self.assertEqual(result.uploaded_bytes, 80)

    def test_cancelled_test_carries_partial_result_and_skips_next_phase(self):
        event = threading.Event()
        def cancel_download(amount, report, cancelled, **kwargs):
            report(50, 1.0)
            event.set()
            raise s.TestCancelled()
        with patch.object(s, 'choose_interface', return_value=None), \
             patch.object(s, 'nic_snapshot', return_value=None), \
             patch.object(s, 'ping_quality', return_value={'ping_ms': 20}), \
             patch.object(s, '_http_download', side_effect=cancel_download), \
             patch.object(s, '_http_upload') as upload:
            with self.assertRaises(s.TestCancelled) as caught:
                s.speed_test(lambda stage: None, lambda *args: None, event)
        result = caught.exception.result
        self.assertTrue(result.cancelled)
        self.assertTrue(result.partial_download)
        self.assertEqual(result.downloaded_bytes, 50)
        self.assertEqual(result.ping['ping_ms'], 20)
        self.assertIsNone(result.upload_mbps)
        upload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
