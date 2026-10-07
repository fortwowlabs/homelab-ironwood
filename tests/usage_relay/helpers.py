"""Shared scaffolding for the usage relay tests.

Importing this module puts roles/svc_infra/files on sys.path, which is how
every test module reaches the usage_relay package. Imports of usage_relay
inside this file are deferred into functions for the same reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = ROOT / "roles/svc_infra/files"
FIXTURES = ROOT / "tests/fixtures/usage_relay"

if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

TOPIC_KEYS = ("requests", "library", "playback", "failures", "digest", "selftest")
TOPICS = {key: f"usage-{key}" for key in TOPIC_KEYS}


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClock:
    """A settable time.time(). Starts at 2026-10-05T12:00:00Z."""

    def __init__(self, start: float = 1_791_201_600.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeFetch:
    """Answers by longest matching URL prefix and records every request."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> object:
        self.calls.append((url, dict(headers)))
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                result = self.routes[prefix]
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected fetch {url}")


class FakeNtfy:
    """Records pushes; set .down to fail the way the real client does."""

    def __init__(self) -> None:
        self.sent: list = []
        self.down = False

    def publish(self, push) -> None:
        from usage_relay.ntfy import NtfyError

        if self.down:
            raise NtfyError("ntfy is down (test)")
        self.sent.append(push)

    def poll(self, topic: str, since: str = "5m") -> list[dict]:
        return [{"event": "message", "topic": p.topic, "message": p.message}
                for p in list(self.sent) if p.topic == topic]


def settings_dict(tmp: str) -> dict:
    def collector(name: str, interval: int) -> dict:
        return {"base_url": f"http://{name}.example", "interval": interval,
                "key_env": f"USAGE_{name.upper()}_API_KEY"}

    return {
        "db_path": f"{tmp}/usage.db",
        "snapshot_path": f"{tmp}/usage.snapshot.db",
        "report_path": f"{tmp}/usage.txt",
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "push_interval": 10,
        "maintenance_time": "02:30",
        "topics": dict(TOPICS),
        "batch_window": 120,
        "resume_window": 1800,
        "playback_expiry": 600,
        "collector_alert_after": 900,
        "retention_days": 365,
        "push_ignore_users": ["Brandon", "admin"],
        "collectors": {
            "jellyfin": collector("jellyfin", 30),
            "seerr": collector("seerr", 60),
            "sonarr": collector("sonarr", 60),
            "radarr": collector("radarr", 60),
            "sabnzbd": collector("sabnzbd", 60),
        },
    }


def make_settings(tmp: str, **overrides):
    from usage_relay.config import from_dict

    return from_dict({**settings_dict(tmp), **overrides})
