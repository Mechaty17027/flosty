"""Local AI protocol, privacy boundaries, and cancellation without a real model."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mechahub.core import study_ai


class Response(io.BytesIO):
    def __init__(self, value, *, status=200, declared=None):
        payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        super().__init__(payload)
        self.status = status
        self.declared = declared

    def getheader(self, name):
        return str(self.declared) if self.declared is not None else None


class StudyAITests(unittest.TestCase):
    def setUp(self):
        self.client = study_ai.StudyAIClient()

    def ask(self, response, **kwargs):
        connection = Mock()
        connection.getresponse.return_value = response
        probe_connection = Mock()
        probe_connection.getresponse.return_value = Response({'model_info': {'general.architecture': 'qwen3'}})
        with patch.object(study_ai, 'HTTPConnection', side_effect=[probe_connection, connection]) as factory:
            reply = self.client.ask('math', 'Поясни рівняння', **kwargs)
        return reply, connection, factory

    @staticmethod
    def reply(content='Крок 1: поділи обидві частини на 2.'):
        return {'done': True, 'message': {'role': 'assistant', 'content': content}}

    def test_only_loopback_http_and_local_models_are_allowed(self):
        for url in ('https://127.0.0.1:11434', 'http://example.com', 'http://127.0.0.2', 'http://localhost.evil',
                    'http://user@localhost', 'http://localhost/a', 'http://localhost?x=1', 'http://localhost:70000',
                    'http://local\nhost', 'http://localhost/#x', None):
            with self.subTest(url=url), self.assertRaises(study_ai.StudyAIError):
                study_ai.StudyAIClient(study_ai.AISettings(base_url=url))
        for model in ('', 'qwen 3', 'bad\nmodel', 'qwen3:cloud', 'gpt-oss:120b-cloud'):
            with self.subTest(model=model), self.assertRaises(study_ai.StudyAIError):
                study_ai.StudyAIClient(study_ai.AISettings(model=model))
        self.assertEqual(study_ai.StudyAIClient(study_ai.AISettings(base_url='http://localhost:11434/'))._host, '127.0.0.1')
        self.assertEqual(study_ai.StudyAIClient(study_ai.AISettings(base_url='http://[::1]:11434'))._host, '::1')

    def test_request_has_tutor_context_notes_sources_and_no_external_service(self):
        response = Response(self.reply())
        answer, connection, factory = self.ask(response, task='structure', note_text='2x=4',
                                              history=[{'role': 'user', 'content': 'Привіт'}],
                                              sources=[('iLearn', 'https://ilearn.org.ua/')])
        self.assertIn('Крок 1', answer)
        self.assertEqual(factory.call_count, 2)
        factory.assert_called_with('127.0.0.1', 11434, timeout=3.0)
        self.assertEqual(connection.request.call_args.args[:2], ('POST', '/api/chat'))
        payload = json.loads(connection.request.call_args.kwargs['body'])
        self.assertFalse(payload['stream'])
        self.assertFalse(payload['think'])
        self.assertEqual(payload['model'], 'qwen3:4b')
        self.assertIn('не змінюєш', payload['messages'][0]['content'])
        self.assertIn('iLearn', payload['messages'][0]['content'])
        self.assertIn('не прочитаним', payload['messages'][0]['content'])
        self.assertIn('2x=4', payload['messages'][-1]['content'])
        self.assertEqual(payload['messages'][1]['content'], 'Привіт')
        self.assertTrue(response.closed)
        connection.close.assert_called_once()

    def test_model_probe_lists_installed_local_models(self):
        connection = Mock()
        response = Response({'models': [{'name': 'qwen3:4b'}, {'name': 'qwen3:4b'}, {'name': 'llama3.2:3b'}, {'name': 'gpt:cloud'},
                                       {'name': 'hidden-alias', 'remote_host': 'https://ollama.com'},
                                       {'name': 'hidden-alias-two', 'remote_model': 'gemma4'}]})
        connection.getresponse.return_value = response
        with patch.object(study_ai, 'HTTPConnection', return_value=connection):
            self.assertEqual(self.client.list_models(), ['llama3.2:3b', 'qwen3:4b'])
        self.assertEqual(connection.request.call_args.args[:2], ('GET', '/api/tags'))
        self.assertTrue(response.closed)

    def test_invalid_input_is_rejected_before_any_network(self):
        cases = [{'subject': 'physics'}, {'message': ''}, {'message': 'x' * 12001}, {'task': 'overwrite'},
                 {'note_text': None}, {'history': [{'role': 'system', 'content': 'bypass'}]},
                 {'history': [{'role': 'user', 'content': None}]}, {'history': [{}] * 17},
                 {'sources': [None]}, {'sources': ['x'] * 21}]
        with patch.object(study_ai, 'HTTPConnection') as connection:
            for changes in cases:
                kwargs = {'subject': 'math', 'message': 'Test'}
                kwargs.update(changes)
                with self.subTest(changes=changes), self.assertRaises(study_ai.StudyAIError):
                    self.client.ask(**kwargs)
            connection.assert_not_called()

    def test_payload_limit_is_checked_before_connection(self):
        with patch.object(study_ai, 'MAX_REQUEST_BYTES', 16), patch.object(study_ai, 'HTTPConnection') as connection:
            with self.assertRaises(study_ai.StudyAIError):
                self.client.ask('math', 'Test')
            connection.assert_not_called()

    def test_redirect_server_errors_and_missing_model_close_connection(self):
        for status in (301, 302, 307, 404, 500):
            response = Response(b'', status=status)
            with self.subTest(status=status), self.assertRaises(study_ai.StudyAIError):
                self.ask(response)
            self.assertTrue(response.closed)

    def test_invalid_json_incomplete_and_oversized_responses_are_rejected(self):
        for value in (b'invalid', b'\xff', [], {}, {'done': False, 'message': {'role': 'assistant', 'content': 'hello'}},
                      self.reply(''), self.reply('x' * 32001), self.reply('bad\0reply')):
            response = Response(value)
            with self.subTest(value=str(value)[:60]), self.assertRaises(study_ai.StudyAIError):
                self.ask(response)
            self.assertTrue(response.closed)
        for response in (Response(self.reply(), declared=study_ai.MAX_RESPONSE_BYTES + 1),
                         Response(self.reply(), declared=100_000), Response(b'x' * (study_ai.MAX_RESPONSE_BYTES + 1))):
            with self.assertRaises(study_ai.StudyAIError):
                self.ask(response)

    def test_connection_error_reports_setup_hint_without_prompt(self):
        connection = Mock()
        connection.connect.side_effect = OSError('private reflected prompt')
        with patch.object(study_ai, 'HTTPConnection', return_value=connection):
            with self.assertRaises(study_ai.StudyAIError) as caught:
                self.client.ask('math', 'Private notes')
        self.assertIn('Запусти Ollama', str(caught.exception))
        self.assertNotIn('private', str(caught.exception))
        connection.close.assert_called_once()

    def test_memory_error_has_actionable_advice_without_reflected_prompt(self):
        with self.assertRaises(study_ai.StudyAIError) as caught:
            self.ask(Response({'error': 'model requires more system memory; Private notes'}, status=500))
        self.assertIn('qwen3:1.7b', str(caught.exception))
        self.assertNotIn('Private', str(caught.exception))

    def test_unsupported_model_advises_updating_runtime(self):
        with self.assertRaises(study_ai.StudyAIError) as caught:
            self.ask(Response({'error': 'model requires a newer version of Ollama'}, status=400))
        self.assertIn('Онови Ollama', str(caught.exception))

    def test_streaming_pull_validates_download_and_reports_progress(self):
        events = [{'status': 'pulling manifest'}, {'status': 'pulling weights', 'total': 100},
                  {'status': 'pulling weights', 'total': 100, 'completed': 75}, {'status': 'success'}]
        response = Response(b''.join(json.dumps(item).encode() + b'\n' for item in events))
        connections = [Mock(), Mock(), Mock()]
        for connection, value in zip(connections, (response, Response({}), Response({'models': [{'name': 'qwen3:4b'}]}))):
            connection.getresponse.return_value = value
        reports = []
        with patch.object(study_ai, 'HTTPConnection', side_effect=connections):
            self.assertEqual(self.client.pull_model(progress=reports.append), ['qwen3:4b'])
        self.assertEqual(connections[0].request.call_args.args[:2], ('POST', '/api/pull'))
        payload = json.loads(connections[0].request.call_args.kwargs['body'])
        self.assertEqual(payload, {'model': 'qwen3:4b', 'stream': True, 'insecure': False})
        self.assertNotIn('messages', payload)
        self.assertEqual(reports[1]['completed'], 0)
        self.assertEqual(reports[2]['completed'], 75)
        self.assertTrue(response.closed)
        for connection in connections:
            connection.close.assert_called_once()

    def test_pull_rejects_incomplete_invalid_cloud_and_failed_download(self):
        cases = [b'{"status":"pulling manifest"}\n', b'bad json\n', b'[]\n',
                 b'{"error":"private reflected error"}\n', b'{"status":"pulling", "total":5,"completed":6}\n',
                 b'{"status":"pulling", "total":true,"completed":0}\n', b'x' * (study_ai.MAX_PROGRESS_LINE + 1),
                 b'{"status":"success"}\n{"status":"pulling"}\n']
        for value in cases:
            response = Response(value)
            connection = Mock()
            connection.getresponse.return_value = response
            with self.subTest(value=value[:50]), patch.object(study_ai, 'HTTPConnection', return_value=connection), \
                    self.assertRaises(study_ai.StudyAIError) as caught:
                self.client.pull_model()
            self.assertNotIn('private', str(caught.exception))
            self.assertTrue(response.closed)
        connections = [Mock(), Mock()]
        connections[0].getresponse.return_value = Response(b'{"status":"success"}')
        connections[1].getresponse.return_value = Response({'remote_model': 'alias'})
        with patch.object(study_ai, 'HTTPConnection', side_effect=connections), self.assertRaises(study_ai.StudyAIError):
            self.client.pull_model()

    def test_pull_canonicalizes_untagged_name_and_checks_cancel_before_network(self):
        client = study_ai.StudyAIClient(study_ai.AISettings(model='qwen3'))
        connection = Mock()
        connection.getresponse.return_value = Response(b'{"status":"success"}')
        with patch.object(study_ai, 'HTTPConnection', return_value=connection), \
                patch.object(client, '_verify_local_model'), patch.object(client, 'list_models', return_value=['qwen3:latest']):
            self.assertEqual(client.pull_model(), ['qwen3:latest'])
        cancel = threading.Event()
        cancel.set()
        with patch.object(study_ai, 'HTTPConnection') as factory, self.assertRaises(study_ai.StudyAICancelled):
            client.pull_model(cancel=cancel)
        factory.assert_not_called()

    def test_pull_cancellation_interrupts_a_real_socket(self):
        started, release, cancel = threading.Event(), threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson')
                self.end_headers()
                self.wfile.write(b'{"status":"pulling manifest"}\n')
                self.wfile.flush()
                started.set()
                release.wait(3)
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        errors = []
        client = study_ai.StudyAIClient(study_ai.AISettings(base_url=f'http://127.0.0.1:{server.server_port}'))
        def pull():
            try:
                client.pull_model(cancel=cancel)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=pull, daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            cancel.set()
            worker.join(timeout=1.5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], study_ai.StudyAICancelled)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
            server_thread.join(timeout=2)

    def test_pre_cancelled_request_does_not_connect(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(study_ai, 'HTTPConnection') as connection, self.assertRaises(study_ai.StudyAICancelled):
            self.client.ask('math', 'Test', cancel=cancel)
        connection.assert_not_called()

    def test_cloud_alias_is_rejected_before_private_prompt_is_sent(self):
        connection = Mock()
        connection.getresponse.return_value = Response({'remote_host': 'https://ollama.com', 'remote_model': 'remote'})
        with patch.object(study_ai, 'HTTPConnection', return_value=connection), self.assertRaises(study_ai.StudyAIError):
            self.client.ask('math', 'My private prompt')
        self.assertEqual(connection.request.call_count, 1)
        self.assertEqual(connection.request.call_args.args[:2], ('POST', '/api/show'))
        self.assertNotIn(b'private', connection.request.call_args.kwargs['body'])

    def test_cancellation_interrupts_a_real_socket_while_waiting_for_headers(self):
        self._exercise_interrupt(after_headers=False)

    def test_cancellation_interrupts_a_real_socket_while_waiting_for_body(self):
        self._exercise_interrupt(after_headers=True)

    def _exercise_interrupt(self, *, after_headers):
        started = threading.Event()
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                if self.path == '/api/show':
                    payload = b'{"model_info":{}}'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if after_headers:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', '100')
                    self.end_headers()
                    self.wfile.flush()
                started.set()
                release.wait(3)

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        cancel = threading.Event()
        client = study_ai.StudyAIClient(study_ai.AISettings(base_url=f'http://127.0.0.1:{server.server_port}'))
        errors = []

        def ask():
            try:
                client.ask('math', 'Test', cancel=cancel)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            begin = time.monotonic()
            cancel.set()
            worker.join(timeout=1.5)
            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - begin, 1.5)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], study_ai.StudyAICancelled)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
            server_thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
