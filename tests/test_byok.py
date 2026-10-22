"""Run with python tests/test_byok.py; all model calls use a local mock service."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote
from unittest.mock import patch


CALLS = []


class MockModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        CALLS.append((body['model'], self.headers.get('Authorization'), self.path))
        self.send_response(200)
        if body.get('stream'):
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            chunk = {'choices': [{'delta': {'content': 'mock report ' + body['model']}}]}
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode())
        else:
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'model': body['model'], 'choices': [
                {'message': {'content': 'available'}, 'finish_reason': 'stop'}
            ]}).encode())


class BYOKTests(unittest.TestCase):
    def header(self, model='model-a', key='fake-key-a', base=None):
        return {'X-WQ-LLM-Config': quote(json.dumps({
            'enabled': True, 'api_key': key, 'model': model,
            'base_url': base or BASE_URL
        }))}

    def test_no_login_and_no_account_endpoints(self):
        with APP.test_client() as client:
            self.assertEqual(len(client.get('/api/stations').json['data']), 14)
            self.assertEqual(len(client.get('/api/data_status').json['data']), 5)
            self.assertEqual(client.get('/api/users').status_code, 404)
            self.assertEqual(client.post('/api/auth/login', json={}).status_code, 404)

    def test_no_server_key_fallback(self):
        with APP.test_client() as client:
            result = client.get('/api/llm_status')
            self.assertFalse(result.json['data']['configured'])
            self.assertNotIn('server-secret-sentinel', result.get_data(as_text=True))
            self.assertEqual(client.post('/api/ai_report/generate', json={
                'station': 'st10'}).status_code, 400)

    def test_ping_and_request_isolation(self):
        def invoke(index):
            with APP.test_client() as client:
                return client.get('/api/llm_status?ping=1', headers=self.header(
                    'model-' + str(index), 'fake-key-' + str(index))).json
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, range(4)))
        for index, result in enumerate(results):
            self.assertTrue(result['data']['ping']['ok'])
            self.assertEqual(result['data']['model'], 'model-' + str(index))
            self.assertEqual(result['data']['api_key'], '***')
            self.assertIn(('model-' + str(index), 'Bearer fake-key-' + str(index),
                           '/v1/chat/completions'), CALLS)
        with APP.test_client() as client:
            self.assertFalse(client.get('/api/llm_status').json['data']['configured'])

    def test_stream_preserves_request_settings(self):
        with APP.test_client() as client:
            result = client.post('/api/ai_report/generate', headers=self.header(),
                                 json={'station': 'st10', 'style': 'brief'})
            text = result.get_data(as_text=True)
            self.assertEqual(result.status_code, 200)
            self.assertIn('mock report model-a', text)
            self.assertIn('event: done', text)
            self.assertNotIn('fake-key-a', text)
            self.assertIn(('model-a', 'Bearer fake-key-a', '/v1/chat/completions'), CALLS)

    def test_validation_and_cors(self):
        with APP.test_client() as client:
            for bad in ('file:///etc/passwd', 'https://name:secret@example.com/v1',
                        'https://example.com/v1?key=secret', 'https://example.com:invalid'):
                self.assertEqual(client.get('/api/llm_status', headers=self.header(base=bad)).status_code, 400)
            self.assertEqual(client.get('/api/llm_status', headers={
                'X-WQ-LLM-Config': 'bad-json'}).status_code, 400)
            result = client.options('/api/llm_status', headers={'Origin': 'null',
                'Access-Control-Request-Headers': 'X-WQ-LLM-Config'})
            self.assertIn('X-WQ-LLM-Config', result.headers['Access-Control-Allow-Headers'])


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'src'))
    with tempfile.TemporaryDirectory() as temporary:
        shutil.copytree(root / 'data' / 'processed', Path(temporary) / 'processed')
        environment = {'WQ_ROOT': str(root), 'WQ_DATA_DIR': temporary,
            'WQ_LOGS_DIR': str(Path(temporary) / 'logs'), 'WQ_ENABLE_SCHEDULER': '0',
            'WQ_REQUIRE_LOGIN': '0', 'WQ_LLM_API_KEY': 'server-secret-sentinel'}
        with patch.dict(os.environ, environment):
            APP = importlib.import_module('water_quality.app').app
            server = ThreadingHTTPServer(('127.0.0.1', 0), MockModel)
            BASE_URL = 'http://127.0.0.1:%d/v1' % server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = unittest.TextTestRunner(verbosity=2).run(
                    unittest.defaultTestLoader.loadTestsFromTestCase(BYOKTests))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
    sys.exit(0 if result.wasSuccessful() else 1)
