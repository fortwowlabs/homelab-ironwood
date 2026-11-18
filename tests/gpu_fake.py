"""A fake ComfyUI/Ollama HTTP server for offline gate tests (stdlib only).

Not a gate itself (no GATE_GROUP, not named validate_*), so tests/run_gates.py
does not discover it.

    with FakeGpu() as fake:
        fake.routes[("GET", "/api/ps")] = [
            (200, {"models": [{"name": "m"}]}),   # first call
            (200, {"models": []}),                # every later call
        ]
        ... fake.url, fake.requests ...

A route value is one of:
  * (status, body)            fixed response, body is JSON-able or bytes
  * [(status, body), ...]     one response per call; the last one repeats
  * callable(request_body) -> (status, body)
Unrouted requests get 404. `requests` records (method, path, parsed_body),
where parsed_body is the decoded JSON, or None when the request had none.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence stderr
        pass

    def _serve(self, method: str) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        fake: FakeGpu = self.server.fake  # type: ignore[attr-defined]
        status, payload = fake._respond(method, self.path, body)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._serve("GET")

    def do_POST(self):  # noqa: N802
        self._serve("POST")


class FakeGpu:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[tuple[str, str, object]] = []
        self._calls: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fake = self  # type: ignore[attr-defined]
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def _respond(self, method: str, path: str, body):
        key = (method, path)
        with self._lock:
            self.requests.append((method, path, body))
            route = self.routes.get(key)
            n = self._calls.get(key, 0)
            self._calls[key] = n + 1
        if route is None:
            return 404, {"error": "no route"}
        if callable(route):
            return route(body)
        if isinstance(route, list):
            return route[min(n, len(route) - 1)]
        return route

    def __enter__(self) -> FakeGpu:
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def closed_port_url() -> str:
    """A URL on a port nothing is listening on (bound then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"
