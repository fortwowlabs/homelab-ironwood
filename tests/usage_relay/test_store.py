from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.model import Event
from usage_relay.store import Store


def ev(event_id: str, kind: str = "download.imported", ts: str = "2026-10-05T12:00:00Z",
       user: str | None = None, **detail) -> Event:
    return Event(id=event_id, ts=ts, service="test", kind=kind, user=user, title=event_id, detail=detail)


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = Store(str(self.tmp / "usage.db"))
        self.addCleanup(self.store.close)

    def test_insert_reports_a_new_row_exactly_once(self):
        self.assertTrue(self.store.insert(ev("a"), "none"))
        self.assertFalse(self.store.insert(ev("a"), "none"))

    def test_insert_rejects_an_unknown_push_state(self):
        with self.assertRaises(ValueError):
            self.store.insert(ev("a"), "maybe")

    def test_round_trip_preserves_detail(self):
        self.store.insert(ev("a", series="S", season=2), "none")
        self.assertEqual(self.store.get("a").detail, {"series": "S", "season": 2})
        self.assertIsNone(self.store.get("missing"))

    def test_delete_removes_one_row(self):
        self.store.insert(ev("a"), "none")
        self.store.insert(ev("b"), "none")
        self.store.delete("a")
        self.assertIsNone(self.store.get("a"))
        self.assertIsNotNone(self.store.get("b"))

    def test_marks_round_trip_and_overwrite(self):
        self.assertIsNone(self.store.get_mark("sonarr"))
        self.store.set_mark("sonarr", "one")
        self.store.set_mark("sonarr", "two")
        self.assertEqual(self.store.get_mark("sonarr"), "two")

    def test_pending_is_ordered_and_set_pushed_clears_it(self):
        self.store.insert(ev("late", ts="2026-10-05T12:05:00Z"), "pending")
        self.store.insert(ev("early", ts="2026-10-05T12:01:00Z"), "pending")
        self.store.insert(ev("quiet"), "none")
        self.assertEqual([e.id for e in self.store.pending()], ["early", "late"])
        self.store.set_pushed(["early"], "sent")
        self.assertEqual([e.id for e in self.store.pending()], ["late"])
        self.assertEqual(self.store.push_state("early"), "sent")
        with self.assertRaises(ValueError):
            self.store.set_pushed(["late"], "maybe")

    def test_requester_is_the_latest_matching_request(self):
        self.store.insert(ev("r1", "request.created", "2026-10-01T00:00:00Z", "alice", match_key="tmdb:1"), "none")
        self.store.insert(ev("r2", "request.created", "2026-10-02T00:00:00Z", "bob", match_key="tmdb:1"), "none")
        self.store.insert(ev("r3", "request.created", user="carol", match_key="tmdb:2"), "none")
        self.assertEqual(self.store.requester_for("tmdb:1"), "bob")
        self.assertIsNone(self.store.requester_for("tmdb:404"))

    def test_started_within_finds_an_earlier_start_of_the_same_item(self):
        self.store.insert(ev("p1", "playback.started", "2026-10-05T12:00:00Z", "alice", item_id="i1"), "pending")
        window = ("2026-10-05T11:50:00Z", "2026-10-05T12:20:00Z")
        self.assertTrue(self.store.started_within("alice", "i1", *window, exclude_id="p2"))
        self.assertFalse(self.store.started_within("alice", "i1", *window, exclude_id="p1"))
        self.assertFalse(self.store.started_within("alice", "i2", *window, exclude_id="p2"))
        self.assertFalse(self.store.started_within("bob", "i1", *window, exclude_id="p2"))

    def test_events_between_is_half_open_and_skips_selftest(self):
        self.store.insert(ev("before", ts="2026-09-27T23:59:59Z"), "none")
        self.store.insert(ev("start", ts="2026-09-28T00:00:00Z"), "none")
        self.store.insert(ev("probe", "selftest", "2026-09-29T00:00:00Z"), "pending")
        self.store.insert(ev("end", ts="2026-10-05T00:00:00Z"), "none")
        got = self.store.events_between("2026-09-28T00:00:00Z", "2026-10-05T00:00:00Z")
        self.assertEqual([e.id for e in got], ["start"])

    def test_prune_deletes_only_older_rows(self):
        self.store.insert(ev("old", ts="2025-01-01T00:00:00Z"), "none")
        self.store.insert(ev("new"), "none")
        self.assertEqual(self.store.prune("2026-01-01T00:00:00Z"), 1)
        self.assertIsNone(self.store.get("old"))
        self.assertIsNotNone(self.store.get("new"))

    def test_count_by_label_groups_by_service_kind_and_user(self):
        self.store.insert(ev("a", "request.created", user="alice"), "none")
        self.store.insert(ev("b", "request.created", user="alice"), "none")
        self.store.insert(ev("c", "request.created", user="bob"), "none")
        counts = {(service, kind, user): n for service, kind, user, n in self.store.count_by_label()}
        self.assertEqual(counts[("test", "request.created", "alice")], 2)
        self.assertEqual(counts[("test", "request.created", "bob")], 1)

    def test_watched_by_user_sums_watched_seconds_from_stops_only(self):
        self.store.insert(ev("s1", "playback.stopped", user="alice", watched_seconds=600), "none")
        self.store.insert(ev("s2", "playback.stopped", user="alice", watched_seconds=300), "none")
        self.store.insert(ev("s3", "playback.stopped", user="bob", watched_seconds=100), "none")
        self.store.insert(ev("p1", "playback.started", user="alice"), "none")
        watched = dict(self.store.watched_by_user())
        self.assertEqual(watched["alice"], 900)
        self.assertEqual(watched["bob"], 100)

    def test_snapshot_is_a_complete_readable_copy(self):
        self.store.insert(ev("a"), "none")
        dest = self.tmp / "snap.db"
        self.store.snapshot(str(dest))
        self.store.snapshot(str(dest))  # a second night replaces it rather than failing
        conn = sqlite3.connect(dest)
        try:
            self.assertEqual(conn.execute("SELECT id FROM events").fetchall(), [("a",)])
        finally:
            conn.close()
        self.assertFalse(Path(f"{dest}.tmp").exists())
