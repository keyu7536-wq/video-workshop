import base64
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import model_api


class ModelAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                cls.received = {'path': self.path, 'authorization': self.headers.get('Authorization'),
                                'body': json.loads(self.rfile.read(int(self.headers['Content-Length'])))}
                self.send_response(cls.status)
                self.send_header('Content-Type', 'application/json')
                if cls.status == 302:
                    self.send_header('Location', cls.url + '/leak')
                self.end_headers()
                self.wfile.write(json.dumps(cls.response).encode())

            def do_GET(self):
                cls.redirect_followed = True
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        type(self).received = None
        type(self).redirect_followed = False
        type(self).status = 200
        type(self).response = {'model': 'vision-test', 'choices': [{'finish_reason': 'stop', 'message': {'content': '{"color":"red"}'}}], 'usage': {'total_tokens': 12}}
        self.config = model_api.APIConfig(self.url + '/v1', 'vision-test', 'test-secret-key')

    def test_vision_request_and_json_response(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / 'image.png'
            Image.new('RGB', (64, 64), 'red').save(image)
            result, metadata = model_api.request_json(self.config, '返回JSON', [image], {'type': 'object'})
            self.assertEqual(result, {'color': 'red'})
            self.assertEqual(metadata['usage']['total_tokens'], 12)
            self.assertEqual(self.received['authorization'], 'Bearer test-secret-key')
            self.assertEqual(self.received['path'], '/v1/chat/completions')
            content = self.received['body']['messages'][1]['content']
            image_url = content[1]['image_url']['url']
            self.assertEqual(base64.b64decode(image_url.split(',', 1)[1]), image.read_bytes())
            self.assertEqual(self.received['body']['response_format'], {'type': 'json_object'})

    def test_http_error_redacts_key(self):
        type(self).status = 401
        type(self).response = {'error': {'message': 'Invalid key test-secret-key'}}
        with self.assertRaises(RuntimeError) as error:
            model_api.request_json(self.config, 'JSON', [], {})
        self.assertIn('401', str(error.exception))
        self.assertNotIn('test-secret-key', str(error.exception))

    def test_rejects_truncated_result(self):
        type(self).response['choices'][0]['finish_reason'] = 'length'
        with self.assertRaisesRegex(RuntimeError, '被截断'):
            model_api.request_json(self.config, 'JSON', [], {})

    def test_rejects_non_json_model_content(self):
        type(self).response['choices'][0]['message']['content'] = 'plain text'
        with self.assertRaisesRegex(RuntimeError, 'JSON 对象'):
            model_api.request_json(self.config, 'JSON', [], {})

    def test_does_not_follow_redirect(self):
        type(self).status = 302
        with self.assertRaisesRegex(RuntimeError, '302'):
            model_api.request_json(self.config, 'JSON', [], {})
        self.assertFalse(self.redirect_followed)

    def test_api_result_files_do_not_include_key(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'PAPER_VIDEO_API_KEY': self.config.key}):
            root = Path(folder)
            model_api.call_api('JSON', [], root, {'api_base': self.config.base_url, 'model': self.config.model}, {})
            for path in root.iterdir():
                self.assertNotIn(self.config.key, path.read_text(encoding='utf-8'))

    def test_connection_checks_images(self):
        message = model_api.test_connection({'api_base': self.config.base_url, 'model': self.config.model}, self.config.key)
        self.assertIn('测试通过', message)

    def test_disallows_remote_plain_http(self):
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            model_api.APIConfig('http://example.com/v1', 'm', 'key').validate()

    def test_disallows_credentials_in_endpoint(self):
        with self.assertRaisesRegex(ValueError, '密钥'):
            model_api.APIConfig('https://example.com/v1?api_key=secret', 'm', 'key').validate()

    def test_pipeline_dispatches_api_without_codex(self):
        from paper_video import call_model
        with patch('model_api.call_api', return_value={'title': 'API测试'}) as api, patch('paper_video.call_codex') as codex:
            result = call_model('prompt', [], Path('.'), {'backend': 'api'})
            self.assertEqual(result, {'title': 'API测试'})
            api.assert_called_once()
            codex.assert_not_called()

    def test_pipeline_preserves_default_codex(self):
        from paper_video import call_model
        with patch('paper_video.call_codex', return_value={'title': 'Codex测试'}) as codex, patch('model_api.call_api') as api:
            result = call_model('prompt', [], Path('.'), {})
            self.assertEqual(result, {'title': 'Codex测试'})
            codex.assert_called_once()
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
