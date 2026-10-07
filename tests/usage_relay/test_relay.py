from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import TOPICS, FakeClock, FakeFetch, FakeNtfy, fixture
from usage_relay.collect_jellyfin import JellyfinCollector
from usage_relay.fetch import FetchError
from usage_relay.metrics import Metrics
from usage_relay.model import Event, from_epoch
from usage_relay.relay import Relay
from usage_relay.router import Router
from usage_relay.store import Store

EVENTS = "homelab_usage_events_total"
ERRORS = "homelab_usage_collector_errors_total"


class FakeCollector:
    def __init__(self, name, *results, interval=60):
        self.name = name
        self.interval = interval
        self.results = list(results)
        self.marks = []

    def poll(self, mark):
        self.marks.append(mark)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class RelayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = Store(str(self.tmp / "usage.db"))
        self.addCleanup(self.store.close)
        self.clock = FakeClock()
        self.ntfy = FakeNtfy()
        self.metrics = Metrics()
        self.router = Router(store=self.store, topics=TOPICS, batch_window=120, resume_window=1800,
                             playback_expiry=600, push_ignore_users=frozenset({"brandon"}))

    def relay(self, **collectors):
        return Relay(store=self.store, router=self.router, ntfy=self.ntfy, metrics=self.metrics,
                     collectors=collectors, alert_topic="homelab-alerts", alert_after=900,
                     clock=self.clock, log=lambda _message: None)

    def ev(self, event_id, kind, user=None, service="seerr", offset=0, **detail):
        return Event(id=event_id, ts=from_epoch(self.clock() + offset), service=service, kind=kind,
                     user=user, title=event_id, detail=detail)

    def test_the_first_poll_records_the_mark(self):
        c = FakeCollector("sonarr", ([], "2026-10-05T12:00:00Z"))
        r = self.relay(sonarr=c)
        r.poll_collector("sonarr")
        r.push_cycle()
        self.assertEqual(c.marks, [None])
        self.assertEqual(self.store.get_mark("sonarr"), "2026-10-05T12:00:00Z")
        self.assertEqual(self.ntfy.sent, [])

    def test_a_repeated_event_is_counted_once(self):
        request = self.ev("seerr:request:1:created", "request.created", "alice")
        c = FakeCollector("seerr", ([request], "m1"), ([request], "m2"))
        r = self.relay(seerr=c)
        r.poll_collector("seerr")
        r.poll_collector("seerr")
        self.assertEqual(c.marks, [None, "m1"])
        self.assertEqual(self.metrics.value(EVENTS, {"service": "seerr", "kind": "request.created",
                                                     "user": "alice"}), 1)

    def test_imports_are_attributed_to_the_requester(self):
        r = self.relay()
        r.ingest([self.ev("req", "request.created", "alice", match_key="tmdb:9001")])
        r.ingest([self.ev("imp", "download.imported", service="radarr", match_key="tmdb:9001")])
        self.assertEqual(self.store.get("imp").detail["requested_by"], "alice")

    def test_a_failing_collector_keeps_its_mark_and_the_others_still_run(self):
        broken = FakeCollector("sonarr", FetchError("HTTP 500 from http://sonarr.example"))
        healthy = FakeCollector("radarr", ([], "m"))
        r = self.relay(sonarr=broken, radarr=healthy)
        self.store.set_mark("sonarr", "old")
        r.poll_collector("sonarr")
        r.poll_collector("radarr")
        self.assertEqual(self.store.get_mark("sonarr"), "old")
        self.assertEqual(self.store.get_mark("radarr"), "m")
        self.assertEqual(self.metrics.value(ERRORS, {"collector": "sonarr"}), 1)
        self.assertEqual(r.health["sonarr"].last_error, "HTTP 500 from http://sonarr.example")

    def test_a_collector_bug_is_contained_but_a_database_error_is_fatal(self):
        r = self.relay(buggy=FakeCollector("buggy", KeyError("oops")),
                       db=FakeCollector("db", sqlite3.OperationalError("disk I/O error")))
        r.poll_collector("buggy")
        self.assertEqual(self.metrics.value(ERRORS, {"collector": "buggy"}), 1)
        with self.assertRaises(sqlite3.OperationalError):
            r.poll_collector("db")

    def test_a_collector_failing_for_15_minutes_alerts_once_then_recovers(self):
        c = FakeCollector("seerr", FetchError("x"), FetchError("x"), FetchError("x"), ([], "m"))
        r = self.relay(seerr=c)
        r.poll_collector("seerr")
        self.clock.advance(600)
        r.poll_collector("seerr")
        r.check_health()
        self.assertEqual(self.ntfy.sent, [])
        self.clock.advance(301)
        r.poll_collector("seerr")
        r.check_health()
        r.check_health()
        self.assertEqual([(p.topic, p.priority) for p in self.ntfy.sent], [("homelab-alerts", 4)])
        self.assertIn("seerr collector failing", self.ntfy.sent[0].title)
        r.poll_collector("seerr")
        self.assertEqual(len(self.ntfy.sent), 2)
        self.assertIn("recovered", self.ntfy.sent[1].title)

    def test_pushes_wait_out_an_ntfy_outage(self):
        r = self.relay()
        r.ingest([self.ev("req", "request.created", "alice", media_type="movie")])
        self.ntfy.down = True
        r.push_cycle()
        self.assertEqual(self.store.push_state("req"), "pending")
        self.assertEqual(self.metrics.value("homelab_usage_push_failures_total",
                                            {"topic": "usage-requests"}), 1)
        self.ntfy.down = False
        r.push_cycle()
        self.assertEqual(self.store.push_state("req"), "sent")

    def test_stale_playback_expires_after_an_outage(self):
        r = self.relay()
        r.ingest([self.ev("play", "playback.started", "alice", service="jellyfin", item_id="i"),
                  self.ev("req", "request.created", "alice", media_type="tv")])
        self.clock.advance(700)
        r.push_cycle()
        self.assertEqual(self.store.push_state("play"), "expired")
        self.assertEqual(self.store.push_state("req"), "sent")

    def test_a_selftest_is_recorded_counted_and_pushed_next_cycle(self):
        r = self.relay()
        event_id = r.selftest_insert()
        r.push_cycle()
        self.assertEqual([(p.topic, p.message) for p in self.ntfy.sent], [("usage-selftest", event_id)])
        self.assertEqual(self.metrics.value(EVENTS, {"service": "relay", "kind": "selftest", "user": None}), 1)

    def test_a_jellyfin_poll_publishes_the_stream_gauge(self):
        fetch = FakeFetch({"http://jellyfin.example/Sessions": fixture("jellyfin_sessions_playing.json")})
        jf = JellyfinCollector(base_url="http://jellyfin.example", api_key="example-api-key",
                               fetch=fetch, interval=30, clock=self.clock)
        r = self.relay(jellyfin=jf)
        r.poll_collector("jellyfin")
        gauge = "homelab_usage_playback_active"
        self.assertEqual(self.metrics.value(gauge, {"user": "alice", "mode": "transcode"}), 1)
        self.assertEqual(self.metrics.value(gauge, {"user": "bob", "mode": "direct"}), 1)

    def test_watch_seconds_accumulate_from_stops(self):
        r = self.relay()
        r.ingest([self.ev("s1", "playback.stopped", "alice", service="jellyfin", watched_seconds=600),
                  self.ev("s2", "playback.stopped", "alice", service="jellyfin", watched_seconds=300)])
        self.assertEqual(self.metrics.value("homelab_usage_watch_seconds_total", {"user": "alice"}), 900)

    def test_maintenance_prunes_old_rows_and_writes_a_snapshot(self):
        r = self.relay()
        r.ingest([self.ev("old", "request.created", "alice", offset=-400 * 86400),
                  self.ev("new", "request.created", "alice")])
        snapshot = self.tmp / "usage.snapshot.db"
        r.maintenance(retention_days=365, snapshot_path=str(snapshot))
        self.assertIsNone(self.store.get("old"))
        self.assertIsNotNone(self.store.get("new"))
        self.assertTrue(snapshot.exists())

    def test_seed_counters_restores_totals_from_sqlite_after_a_restart(self):
        r = self.relay()
        r.ingest([self.ev("p1", "request.created", "alice"),
                  self.ev("p2", "request.created", "alice"),
                  self.ev("s1", "playback.stopped", "bob", service="jellyfin", watched_seconds=600)])
        # A fresh Metrics + Relay on the SAME store simulates a restart: the
        # in-memory counters start at zero until seed_counters() reads them
        # back out of SQLite.
        new_metrics = Metrics()
        new_relay = Relay(store=self.store, router=self.router, ntfy=self.ntfy, metrics=new_metrics,
                          collectors={}, alert_topic="homelab-alerts", alert_after=900,
                          clock=self.clock, log=lambda _message: None)
        new_relay.seed_counters()
        self.assertEqual(new_metrics.value(EVENTS, {"service": "seerr", "kind": "request.created",
                                                     "user": "alice"}), 2)
        self.assertEqual(new_metrics.value("homelab_usage_watch_seconds_total", {"user": "bob"}), 600)
        new_relay.ingest([self.ev("p3", "request.created", "alice")])
        self.assertEqual(new_metrics.value(EVENTS, {"service": "seerr", "kind": "request.created",
                                                     "user": "alice"}), 3)

    def test_the_health_report_marks_disabled_collectors(self):
        r = self.relay(sonarr=None, radarr=FakeCollector("radarr", ([], "m")))
        r.poll_collector("radarr")
        report = r.health_report()
        self.assertEqual(report["collectors"]["sonarr"],
                         {"enabled": False, "last_success": None, "last_error": None})
        self.assertEqual(report["collectors"]["radarr"]["last_success"], self.clock())
