from __future__ import annotations

import unittest
from datetime import datetime

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.digest import render
from usage_relay.model import Event

START = datetime(2026, 9, 28, 18, 0)
END = datetime(2026, 10, 5, 18, 0)


def ev(event_id, kind, user=None, service="jellyfin", title="", **detail):
    return Event(id=event_id, ts="2026-10-01T20:00:00Z", service=service, kind=kind, user=user,
                 title=title or event_id, detail=detail)


SHOW = "The Example Show"
WEEK = [
    ev("p1", "playback.started", "alice", title=f"{SHOW} S02E01", item_id="e1", series=SHOW, mode="transcode"),
    ev("p2", "playback.started", "alice", title=f"{SHOW} S02E02", item_id="e2", series=SHOW, mode="direct"),
    ev("p3", "playback.started", "alice", title="Example Movie (2024)", item_id="m1", series=None, mode="direct"),
    ev("p4", "playback.started", "alice", title=f"{SHOW} S02E01", item_id="e1", series=SHOW, mode="direct"),
    ev("s1", "playback.stopped", "alice", watched_seconds=3600),
    ev("s2", "playback.stopped", "alice", watched_seconds=1800),
    ev("s3", "playback.stopped", "alice", watched_seconds=1800),
    ev("r1", "request.created", "bob", service="seerr", request_id=12),
    ev("r2", "request.available", "bob", service="seerr", request_id=12),
    ev("i1", "download.imported", service="sonarr"),
    ev("i2", "download.imported", service="sonarr"),
    ev("i3", "download.imported", service="radarr"),
    ev("f1", "download.failed", service="sonarr"),
    ev("probe", "selftest", service="relay"),
]


class DigestTests(unittest.TestCase):
    def test_a_full_week(self):
        self.assertEqual(render(WEEK, START, END), (
            "Week of Sep 28 – Oct 5\n"
            "Alice     3 plays · 2.0h · The Example Show (2), Example Movie (2024)\n"
            "Requests  1 (Bob 1) · 1 fulfilled, 0 pending\n"
            "Library   +2 episodes, +1 movie · 1 failed import\n"
            "Streams   67% direct play, 33% transcode\n"
            "Read      13 events · 2 active accounts\n"
        ))

    def test_an_empty_week_says_so_and_still_reports_what_it_read(self):
        self.assertEqual(render([], START, END), (
            "Week of Sep 28 – Oct 5\n"
            "No activity\n"
            "Read      0 events · 0 active accounts\n"
        ))

    def test_a_week_of_only_selftests_is_empty(self):
        self.assertIn("No activity\n", render([ev("probe", "selftest", service="relay")], START, END))
