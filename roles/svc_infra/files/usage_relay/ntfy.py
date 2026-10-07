"""Publishing to ntfy, and reading a topic back for the selftest."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .model import Push


class NtfyError(Exception):
    """ntfy could not be reached, refused the request, or answered garbage."""


class Ntfy:
    def __init__(self, base_url: str, token: str = "", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _send(self, request: urllib.request.Request, what: str) -> bytes:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise NtfyError(f"{what} failed: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NtfyError(f"{what} failed: {type(exc).__name__}") from None

    def publish(self, push: Push) -> None:
        # JSON publishing to the root URL rather than Title/Priority headers:
        # HTTP headers are latin-1, and every title here starts with an emoji.
        body = json.dumps({"topic": push.topic, "title": push.title,
                           "message": push.message, "priority": push.priority}).encode("utf-8")
        request = urllib.request.Request(self.base_url, data=body, method="POST",
                                         headers=self._headers({"Content-Type": "application/json"}))
        self._send(request, f"publish to {push.topic}")

    def poll(self, topic: str, since: str = "5m") -> list[dict]:
        request = urllib.request.Request(f"{self.base_url}/{topic}/json?poll=1&since={since}",
                                         headers=self._headers())
        body = self._send(request, f"poll of {topic}")
        messages = []
        try:
            for line in body.decode("utf-8").splitlines():
                if line.strip():
                    item = json.loads(line)
                    if item.get("event") == "message":
                        messages.append(item)
        except ValueError:
            raise NtfyError(f"poll of {topic} returned something that is not ndjson") from None
        return messages
