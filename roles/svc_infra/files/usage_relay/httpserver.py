"""GET /metrics for Prometheus, GET /health for `check`, POST /selftest for `selftest`."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOOPBACK = frozenset({"127.0.0.1", "::1"})


class _Handler(BaseHTTPRequestHandler):
    server: RelayHTTPServer

    def do_GET(self) -> None:
        if self.path == "/metrics":
            self._reply(200, "text/plain; version=0.0.4; charset=utf-8", self.server.metrics.render())
        elif self.path == "/health":
            self._reply(200, "application/json", json.dumps(self.server.relay.health_report()))
        else:
            self._reply(404, "text/plain", "not found\n")

    def do_POST(self) -> None:
        if self.path != "/selftest":
            self._reply(404, "text/plain", "not found\n")
        elif self.client_address[0] not in LOOPBACK:
            # The port is open to Prometheus's address, and a selftest writes a
            # row. Loopback only, so only a shell on svc-infra can trigger one.
            self._reply(403, "text/plain", "selftest is loopback-only\n")
        else:
            self._reply(200, "application/json", json.dumps({"id": self.server.relay.selftest_insert()}))

    def _reply(self, code: int, content_type: str, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return  # one line per Prometheus scrape would bury the relay's own log


class RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], relay, metrics) -> None:
        self.relay = relay
        self.metrics = metrics
        super().__init__(address, _Handler)
