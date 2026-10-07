from __future__ import annotations

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.model import Push
from usage_relay.ntfy import Ntfy, NtfyError


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.posts.append((json.loads(self.rfile.read(length)), dict(self.headers)))
        code = 500 if self.server.fail else 200
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        body = (b'{"event":"open","topic":"usage-selftest"}\n'
                b'{"event":"message","topic":"usage-selftest","message":"selftest:1"}\n')
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.gets.append(self.path)

    def log_message(self, *args):
        return


class NtfyTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.posts, self.server.gets, self.server.fail = [], [], False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def push(self):
        return Push(topic="usage-requests", title="📥 New request", message="Alice requested X (movie)",
                    priority=3, event_ids=("e1",))

    def test_publish_sends_json_so_emoji_titles_survive(self):
        Ntfy(self.url, token="example-token").publish(self.push())
        body, headers = self.server.posts[0]
        self.assertEqual(body, {"topic": "usage-requests", "title": "📥 New request",
                                "message": "Alice requested X (movie)", "priority": 3})
        self.assertEqual(headers.get("Authorization"), "Bearer example-token")

    def test_no_token_means_no_authorization_header(self):
        Ntfy(self.url).publish(self.push())
        self.assertNotIn("Authorization", self.server.posts[0][1])

    def test_a_server_error_is_an_ntfy_error(self):
        self.server.fail = True
        with self.assertRaises(NtfyError):
            Ntfy(self.url).publish(self.push())

    def test_an_unreachable_server_is_an_ntfy_error(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(NtfyError):
            Ntfy(f"http://127.0.0.1:{port}", timeout=2).publish(self.push())

    def test_poll_returns_only_messages(self):
        messages = Ntfy(self.url).poll("usage-selftest", since="5m")
        self.assertEqual([m["message"] for m in messages], ["selftest:1"])
        self.assertEqual(self.server.gets[0], "/usage-selftest/json?poll=1&since=5m")
