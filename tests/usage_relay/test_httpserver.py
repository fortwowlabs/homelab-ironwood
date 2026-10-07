from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay import httpserver
from usage_relay.httpserver import RelayHTTPServer
from usage_relay.metrics import Metrics


class FakeRelay:
    def __init__(self):
        self.selftest_calls = 0

    def health_report(self):
        return {"now": 1.0, "collectors": {}}

    def selftest_insert(self):
        self.selftest_calls += 1
        return "selftest:1"


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        metrics = Metrics()
        metrics.inc("homelab_usage_collector_errors_total", {"collector": "seerr"}, 0)
        cls.server = RelayHTTPServer(("127.0.0.1", 0), FakeRelay(), metrics)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, data=None):
        request = urllib.request.Request(self.base + path, data=data,
                                         method="POST" if data is not None else "GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

    def test_metrics_are_served(self):
        self.assertIn("# TYPE homelab_usage_collector_errors_total counter", self.get("/metrics"))

    def test_health_is_json(self):
        self.assertEqual(json.loads(self.get("/health")), {"now": 1.0, "collectors": {}})

    def test_selftest_from_loopback_returns_the_event_id(self):
        self.assertEqual(json.loads(self.get("/selftest", data=b"")), {"id": "selftest:1"})

    def test_unknown_paths_are_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)

    def test_selftest_is_refused_when_the_caller_is_not_recognised_as_loopback(self):
        # The 403 branch is the only guard on a write path. Shrinking LOOPBACK
        # to empty simulates a request this server would treat as non-local
        # without needing an actual non-loopback client.
        before = self.server.relay.selftest_calls
        with mock.patch.object(httpserver, "LOOPBACK", frozenset()):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get("/selftest", data=b"")
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.server.relay.selftest_calls, before)
