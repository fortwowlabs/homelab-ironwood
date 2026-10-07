from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from helpers import TOPICS, FakeNtfy, make_settings
from usage_relay.cli import assess, cmd_digest, cmd_selftest, selftest_count, verdict
from usage_relay.httpserver import RelayHTTPServer
from usage_relay.metrics import Metrics
from usage_relay.model import Event
from usage_relay.relay import Relay
from usage_relay.router import Router
from usage_relay.store import Store


class AssessTests(unittest.TestCase):
    def test_each_collector_state_is_named(self):
        report = {"now": 1000.0, "collectors": {
            "jellyfin": {"enabled": True, "last_success": 990.0, "last_error": None},
            "seerr": {"enabled": False, "last_success": None, "last_error": None},
            "sonarr": {"enabled": True, "last_success": 600.0, "last_error": "HTTP 500 from http://x"},
            "radarr": {"enabled": True, "last_success": None, "last_error": None},
        }}
        lines, stale = assess(report)
        self.assertEqual(stale, ["radarr", "sonarr"])
        self.assertEqual(lines, [
            "jellyfin: ok (10s ago)",
            "radarr: never looked",
            "seerr: disabled (no API key)",
            "sonarr: stale, last looked 400s ago — HTTP 500 from http://x",
        ])


class VerdictTests(unittest.TestCase):
    """F4: `check` must not read as a pass when nothing was actually looked
    at -- "could not look" is a fail, not a silent disabled-only report."""

    def test_zero_enabled_collectors_fails_with_an_explanatory_line(self):
        report = {"now": 1000.0, "collectors": {
            "jellyfin": {"enabled": False, "last_success": None, "last_error": None},
            "seerr": {"enabled": False, "last_success": None, "last_error": None},
        }}
        lines, failed = verdict(report)
        self.assertTrue(failed)
        self.assertIn("no collector is enabled — nothing was checked", lines)

    def test_at_least_one_enabled_and_fresh_collector_passes(self):
        report = {"now": 1000.0, "collectors": {
            "jellyfin": {"enabled": True, "last_success": 990.0, "last_error": None},
            "seerr": {"enabled": False, "last_success": None, "last_error": None},
        }}
        lines, failed = verdict(report)
        self.assertFalse(failed)
        self.assertNotIn("no collector is enabled — nothing was checked", lines)

    def test_a_stale_enabled_collector_still_fails(self):
        report = {"now": 10_000.0, "collectors": {
            "jellyfin": {"enabled": True, "last_success": 1.0, "last_error": None},
        }}
        lines, failed = verdict(report)
        self.assertTrue(failed)


class SelftestCountTests(unittest.TestCase):
    def test_selftest_samples_are_summed(self):
        text = ('homelab_usage_events_total{kind="selftest",service="relay",user="none"} 2\n'
                'homelab_usage_events_total{kind="request.created",service="seerr",user="a"} 7\n')
        self.assertEqual(selftest_count(text), 2.0)
        self.assertEqual(selftest_count(""), 0.0)


class DigestCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = make_settings(tmp.name)
        self.now = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)

    def seed(self):
        store = Store(self.settings.db_path)
        store.insert(Event(id="p1", ts="2026-10-04T20:00:00Z", service="jellyfin", kind="playback.started",
                           user="alice", title="Example Movie (2024)", detail={"item_id": "m1", "mode": "direct"}),
                     "sent")
        store.close()

    def test_the_report_is_written_and_published(self):
        self.seed()
        ntfy = FakeNtfy()
        self.assertEqual(cmd_digest(self.settings, ntfy, self.now), 0)
        report = Path(self.settings.report_path).read_text(encoding="utf-8")
        self.assertIn("Alice     1 play · 0.0h · Example Movie (2024)\n", report)
        self.assertEqual([(p.topic, p.message) for p in ntfy.sent], [("usage-digest", report)])

    def test_a_missing_database_publishes_nothing(self):
        ntfy = FakeNtfy()
        self.assertEqual(cmd_digest(self.settings, ntfy, self.now), 1)
        self.assertEqual(ntfy.sent, [])
        self.assertFalse(Path(self.settings.report_path).exists())

    def test_an_ntfy_failure_still_leaves_the_report(self):
        self.seed()
        ntfy = FakeNtfy()
        ntfy.down = True
        self.assertEqual(cmd_digest(self.settings, ntfy, self.now), 1)
        self.assertTrue(Path(self.settings.report_path).exists())


class SelftestCommandTests(unittest.TestCase):
    def start_relay(self, push: bool):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = make_settings(tmp.name).db_path
        self.store = Store(db_path)
        self.addCleanup(self.store.close)
        self.ntfy = FakeNtfy()
        metrics = Metrics()
        router = Router(store=self.store, topics=TOPICS, batch_window=120, resume_window=1800,
                        playback_expiry=600, push_ignore_users=frozenset())
        relay = Relay(store=self.store, router=router, ntfy=self.ntfy, metrics=metrics, collectors={},
                      alert_topic="homelab-alerts", alert_after=900, log=lambda _message: None)
        server = RelayHTTPServer(("127.0.0.1", 0), relay, metrics)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        if push:
            stop = threading.Event()

            def pusher():
                while not stop.wait(0.05):
                    relay.push_cycle()

            thread = threading.Thread(target=pusher, daemon=True)
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(stop.set)
        return make_settings(tmp.name, listen_port=server.server_address[1])

    def test_a_working_relay_passes_and_the_probe_row_is_removed(self):
        settings = self.start_relay(push=True)
        rc = cmd_selftest(settings, self.ntfy, timeout=5, sleep=lambda _s: time.sleep(0.05))
        self.assertEqual(rc, 0)
        self.assertIsNone(self.store.get(self.ntfy.sent[0].message))

    def test_a_relay_that_never_pushes_fails_and_still_cleans_up(self):
        settings = self.start_relay(push=False)
        rc = cmd_selftest(settings, self.ntfy, timeout=0.3, sleep=lambda _s: time.sleep(0.05))
        self.assertEqual(rc, 1)
        self.assertEqual(self.ntfy.sent, [])
        self.assertEqual(self.store.pending(), [])
