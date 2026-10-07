from __future__ import annotations

import copy
import json
import unittest

from helpers import FakeClock, FakeFetch, fixture
from usage_relay.collect_jellyfin import JellyfinCollector
from usage_relay.fetch import ShapeError

IDLE = fixture("jellyfin_sessions_idle.json")
PLAYING = fixture("jellyfin_sessions_playing.json")
SESSIONS = "http://jellyfin.example/Sessions"


class JellyfinTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.fetch = FakeFetch({SESSIONS: IDLE})
        self.c = self.collector()

    def collector(self):
        return JellyfinCollector(base_url="http://jellyfin.example", api_key="example-api-key",
                                 fetch=self.fetch, interval=30, clock=self.clock)

    def poll(self, sessions, mark, collector=None):
        self.fetch.routes[SESSIONS] = sessions
        return (collector or self.c).poll(mark)

    def test_the_first_poll_is_a_baseline_even_mid_playback(self):
        events, mark = self.poll(PLAYING, None)
        self.assertEqual(events, [])
        self.assertEqual(len(json.loads(mark)["playing"]), 2)
        self.assertEqual(sorted(self.c.active), [("alice", "transcode"), ("bob", "direct")])

    def test_the_token_goes_in_the_authorization_header(self):
        self.poll(IDLE, None)
        self.assertEqual(self.fetch.calls[0][1], {"Authorization": 'MediaBrowser Token="example-api-key"'})

    def test_new_sessions_start_playback(self):
        _, mark = self.poll(IDLE, None)
        self.clock.advance(30)
        events, _ = self.poll(PLAYING, mark)
        by_user = {e.user: e for e in events}
        alice = by_user["alice"]
        self.assertEqual(alice.kind, "playback.started")
        self.assertEqual(alice.title, "The Example Show S02E03")
        self.assertEqual(alice.ts, "2026-10-05T12:00:30Z")
        self.assertEqual(alice.id, "jellyfin:session:sess-tv:item-ep3:2026-10-05T12:00:30Z")
        self.assertEqual(alice.detail, {"item_id": "item-ep3", "series": "The Example Show",
                                        "media_type": "Episode", "device": "Living Room TV",
                                        "client": "Android TV", "mode": "transcode"})
        self.assertEqual(by_user["bob"].title, "Example Movie (2024)")
        self.assertEqual(by_user["bob"].detail["mode"], "direct")

    def test_an_unchanged_session_emits_nothing(self):
        _, mark = self.poll(IDLE, None)
        _, mark = self.poll(PLAYING, mark)
        self.assertEqual(self.poll(PLAYING, mark)[0], [])

    def test_a_finished_session_stops_with_the_time_watched(self):
        _, mark = self.poll(IDLE, None)
        self.clock.advance(30)
        _, mark = self.poll(PLAYING, mark)
        self.clock.advance(1800)
        later = copy.deepcopy(PLAYING)
        later[0]["PlayState"]["PositionTicks"] = 24_000_000_000
        _, mark = self.poll(later, mark)
        self.clock.advance(30)
        events, _ = self.poll(IDLE, mark)
        stops = {e.user: e for e in events}
        self.assertEqual(stops["alice"].kind, "playback.stopped")
        self.assertEqual(stops["alice"].id,
                         "jellyfin:session:sess-tv:item-ep3:2026-10-05T12:00:30Z:stopped")
        self.assertEqual(stops["alice"].detail["watched_seconds"], 1800)
        self.assertEqual(stops["alice"].ts, "2026-10-05T12:31:00Z")
        self.assertEqual(stops["bob"].detail["watched_seconds"], 0)

    def test_autoplay_to_the_next_episode_is_a_stop_and_a_start(self):
        _, mark = self.poll(IDLE, None)
        _, mark = self.poll(PLAYING, mark)
        following = copy.deepcopy(PLAYING)
        following[0]["NowPlayingItem"].update(Id="item-ep4", IndexNumber=4)
        events, _ = self.poll(following, mark)
        self.assertEqual(sorted((e.kind, e.title) for e in events), [
            ("playback.started", "The Example Show S02E04"),
            ("playback.stopped", "The Example Show S02E03"),
        ])

    def test_a_restart_resumes_from_the_mark_without_replaying(self):
        _, mark = self.poll(IDLE, None)
        _, mark = self.poll(PLAYING, mark)
        self.assertEqual(self.poll(PLAYING, mark, collector=self.collector())[0], [])

    def test_an_unreadable_mark_is_treated_as_a_baseline(self):
        self.assertEqual(self.poll(PLAYING, "not json")[0], [])

    def test_unexpected_shapes_are_shape_errors(self):
        with self.assertRaises(ShapeError):
            self.poll({"not": "a list"}, None)
        with self.assertRaises(ShapeError):
            self.poll([{"NowPlayingItem": {"Id": "x"}}], None)
