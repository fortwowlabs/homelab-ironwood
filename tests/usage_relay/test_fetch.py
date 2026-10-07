from __future__ import annotations

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.fetch import FetchError, get_json, redact, snippet


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen_headers = dict(self.headers)
        if self.path.startswith("/ok"):
            code, body = 200, b'{"answer": 42}'
        elif self.path.startswith("/denied"):
            code, body = 401, b"{}"
        else:
            code, body = 200, b"<html>not json</html>"
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class GetJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_json_is_returned_and_headers_are_sent(self):
        self.assertEqual(get_json(f"{self.base}/ok", {"X-Api-Key": "example-api-key"}), {"answer": 42})
        self.assertEqual(self.server.seen_headers.get("X-Api-Key"), "example-api-key")

    def test_an_http_error_names_the_status_and_hides_the_key(self):
        with self.assertRaises(FetchError) as caught:
            get_json(f"{self.base}/denied?apikey=example-secret-value", {})
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("example-secret-value", str(caught.exception))

    def test_a_non_json_body_is_a_fetch_error(self):
        with self.assertRaises(FetchError) as caught:
            get_json(f"{self.base}/html", {})
        self.assertIn("non-JSON", str(caught.exception))

    def test_an_unreachable_host_is_a_fetch_error(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(FetchError):
            get_json(f"http://127.0.0.1:{port}/", {}, timeout=2)


class HelperTests(unittest.TestCase):
    def test_redact_hides_key_parameters(self):
        self.assertEqual(redact("http://x/api?mode=history&apikey=abc&limit=5"),
                         "http://x/api?mode=history&apikey=REDACTED&limit=5")

    def test_snippet_is_bounded(self):
        self.assertEqual(len(snippet("x" * 5000)), 2048)
