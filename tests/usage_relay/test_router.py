from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import TOPICS, FakeClock
from usage_relay.model import Event, from_epoch
from usage_relay.router import Router
from usage_relay.store import Store


def ev(event_id, kind, ts, user=None, service="test", title="T", **detail):
    return Event(id=event_id, ts=ts, service=service, kind=kind, user=user, title=title, detail=detail)


class RouterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(str(Path(tmp.name) / "usage.db"))
        self.addCleanup(self.store.close)
        self.clock = FakeClock()
        self.router = Router(store=self.store, topics=TOPICS, batch_window=120, resume_window=1800,
                             playback_expiry=600, push_ignore_users=frozenset({"brandon"}))

    def at(self, offset: float) -> str:
        return from_epoch(self.clock() + offset)

    def record(self, event):
        state = self.router.initial_state(event)
        self.store.insert(event, state)
        return state

    def test_recorded_only_kinds_are_never_pushed(self):
        for kind in ("download.grabbed", "download.completed", "playback.stopped",
                     "request.approved", "request.available"):
            self.assertEqual(self.router.initial_state(ev("x", kind, self.at(0))), "none", kind)

    def test_requests_imports_failures_and_selftest_are_pending(self):
        for kind in ("request.created", "download.imported", "download.failed", "selftest"):
            self.assertEqual(self.router.initial_state(ev("x", kind, self.at(0))), "pending", kind)

    def test_the_ignore_list_silences_playback_only(self):
        self.assertEqual(self.router.initial_state(
            ev("p", "playback.started", self.at(0), "brandon", item_id="i")), "suppressed")
        self.assertEqual(self.router.initial_state(
            ev("r", "request.created", self.at(0), "brandon")), "pending")

    def test_a_resume_within_the_window_is_suppressed(self):
        self.assertEqual(self.record(ev("p1", "playback.started", self.at(0), "alice", item_id="i1")), "pending")
        self.assertEqual(self.record(ev("p2", "playback.started", self.at(1200), "alice", item_id="i1")), "suppressed")
        self.assertEqual(self.record(ev("p3", "playback.started", self.at(1200), "alice", item_id="i2")), "pending")
        self.assertEqual(self.record(ev("p4", "playback.started", self.at(3100), "alice", item_id="i1")), "pending")

    def test_each_kind_gets_its_topic_priority_and_wording(self):
        pending = [
            ev("r", "request.created", self.at(0), "alice", title="Example Movie", media_type="movie"),
            ev("f", "download.failed", self.at(0), service="radarr", title="Example Movie (2024)",
               reason="no matching file"),
            ev("p", "playback.started", self.at(0), "erin", title="The Example Show S02E03",
               device="Living Room TV", mode="transcode", item_id="i"),
            ev("i", "download.imported", self.at(0), service="radarr", title="Example Movie (2024)",
               requested_by="alice"),
            ev("s", "selftest", self.at(0), service="relay", title="usage-relay selftest"),
        ]
        pushes, expired = self.router.plan(pending, self.clock())
        self.assertEqual(expired, [])
        got = {p.event_ids[0]: (p.topic, p.priority, p.title, p.message) for p in pushes}
        self.assertEqual(got, {
            "r": ("usage-requests", 3, "📥 New request", "Alice requested Example Movie (movie)"),
            "f": ("usage-failures", 4, "⚠️ Radarr failed", "Example Movie (2024): no matching file"),
            "p": ("usage-playback", 2, "▶️ Erin", "The Example Show S02E03 · Living Room TV · transcoding"),
            "i": ("usage-library", 3, "✅ Ready", "Example Movie (2024) · requested by Alice"),
            "s": ("usage-selftest", 1, "usage-relay selftest", "s"),
        })

    def test_a_season_pack_is_held_then_sent_as_one_push(self):
        imports = [ev(f"i{n}", "download.imported", self.at(n * 10), service="sonarr",
                      title=f"The Example Show S02E0{n}", series="The Example Show",
                      series_key="sonarr:series:7", season=2) for n in (1, 2, 3)]
        held, _ = self.router.plan(imports, self.clock() + 90)
        self.assertEqual(held, [])
        pushes, _ = self.router.plan(imports, self.clock() + 151)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0].message, "The Example Show S02: 3 episodes ready")
        self.assertEqual(pushes[0].event_ids, ("i1", "i2", "i3"))

    def test_a_lone_episode_after_the_window_is_pushed_by_name(self):
        lone = ev("i1", "download.imported", self.at(0), service="sonarr", title="The Example Show S02E01",
                  series="The Example Show", series_key="sonarr:series:7", season=2)
        pushes, _ = self.router.plan([lone], self.clock() + 121)
        self.assertEqual([p.message for p in pushes], ["The Example Show S02E01"])

    def test_stale_playback_expires_but_other_kinds_still_send(self):
        stale = [ev("p", "playback.started", self.at(0), "alice", item_id="i"),
                 ev("r", "request.created", self.at(0), "alice", media_type="tv")]
        pushes, expired = self.router.plan(stale, self.clock() + 601)
        self.assertEqual(expired, ["p"])
        self.assertEqual([p.event_ids for p in pushes], [("r",)])
