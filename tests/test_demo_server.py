import json
import os
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from demo_app.server import DemoServer, demo_connection


class DemoServerTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.state.return_value = {"source": {"kind": "atlas"}, "products": []}
        self.repository.preview.return_value = {"preview_id": "saved-preview"}
        self.repository.accept.return_value = {"accepted_count": 1}
        self.server = DemoServer(('127.0.0.1', 0), repository=self.repository)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None, origin=None):
        headers = {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if origin:
            headers['Origin'] = origin
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers)
        return urlopen(request, timeout=2)

    def test_static_and_saved_state_are_served_without_model_routes(self):
        for path in ('/', '/static/app.js', '/static/style.css', '/healthz', '/api/state'):
            with self.request(path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])
        for path in ('/.env', '/.git/config', '/../.env', '/api/model', '/api/final/run'):
            with self.assertRaises(HTTPError) as caught:
                self.request(path)
            self.assertEqual(caught.exception.code, 404)
        self.repository.preview.assert_not_called()
        self.repository.accept.assert_not_called()

    def test_placement_workflow_and_cross_origin_rejection(self):
        with self.request('/api/placement/preview', {'store_id': 'homeware'}, origin=self.url) as response:
            self.assertEqual(json.load(response)['preview_id'], 'saved-preview')
        with self.request('/api/placement/accept', {'preview_id': 'saved-preview', 'source_record_ids': ['id']}) as response:
            self.assertEqual(json.load(response)['accepted_count'], 1)
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/placement/accept', {'preview_id': 'saved-preview'}, origin='https://elsewhere.invalid')
        self.assertEqual(caught.exception.code, 403)
        self.repository.accept.assert_called_once_with('saved-preview', ['id'])

    def test_driver_failures_do_not_leak_exception_text(self):
        self.repository.preview.side_effect = RuntimeError('SECRET_DRIVER_CONNECTION_STRING')
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/placement/preview', {'store_id': 'homeware'})
        body = caught.exception.read().decode()
        self.assertNotIn('SECRET_DRIVER', body)
        self.assertIn('Atlas placement operation unavailable', body)

    def test_unavailable_sources_give_useful_error(self):
        self.server.repository = None
        self.server.repository_error = 'Check Atlas access or restore saved local artifacts'
        with self.assertRaises(HTTPError) as caught:
            self.request('/api/state')
        self.assertEqual(caught.exception.code, 503)
        self.assertIn('Check Atlas', caught.exception.read().decode())

    def test_hosted_startup_cannot_use_automatically_provisioned_database(self):
        with patch.dict(os.environ, {'PORT': '10000', 'MONGODB_URI': 'wrong-provisioned-database'}, clear=True):
            with patch('demo_app.server.connect') as connect:
                with self.assertRaises(ValueError):
                    demo_connection()
                connect.assert_not_called()
        with patch.dict(os.environ, {'PORT': '10000', 'MONGODB_URI': 'wrong-provisioned-database',
                                   'CATALOG_MONGODB_URI': 'explicit-event-database',
                                   'CATALOG_MONGODB_DATABASE': 'event-demo'}, clear=True):
            with patch('demo_app.server.connect') as connect:
                demo_connection()
                self.assertEqual(os.environ['MONGODB_URI'], 'explicit-event-database')
                self.assertEqual(os.environ['MONGODB_DATABASE'], 'event-demo')
                connect.assert_called_once()


if __name__ == '__main__':
    unittest.main()
