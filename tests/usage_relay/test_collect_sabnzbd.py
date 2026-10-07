from __future__ import annotations

import unittest

from helpers import FakeClock, FakeFetch, fixture
from usage_relay.collect_sabnzbd import SabnzbdCollector, parse_slots
from usage_relay.fetch import ShapeError

HISTORY = fixture("sabnzbd_history.json")
API = "http://sabnzbd.example/api?mode=history"


class SabnzbdTests(unittest.TestCase):
    def setUp(self):
        self.fetch = FakeFetch({API: HISTORY})
        self.c = SabnzbdCollector(base_url="http://sabnzbd.example", api_key="example-api-key",
                                  fetch=self.fetch, interval=60, clock=FakeClock())

    def test_the_first_poll_is_a_baseline_at_the_newest_finished_job(self):
        self.assertEqual(self.c.poll(None), ([], "1791201720"))

    def test_finished_jobs_after_the_mark_become_events(self):
        events, mark = self.c.poll("1791201600")
        self.assertEqual(mark, "1791201720")
        self.assertEqual([(e.id, e.kind) for e in events], [
            ("sabnzbd:SABnzbd_nzo_a1", "download.completed"),
            ("sabnzbd:SABnzbd_nzo_a2", "download.failed"),
        ])
        self.assertEqual(events[0].ts, "2026-10-05T12:01:00Z")
        self.assertEqual(events[1].detail, {"category": "movies", "reason": "Aborted, cannot be completed"})

    def test_only_jobs_newer_than_the_mark_are_emitted(self):
        events, _ = self.c.poll("1791201700")
        self.assertEqual([e.id for e in events], ["sabnzbd:SABnzbd_nzo_a2"])

    def test_the_api_key_is_a_query_parameter(self):
        self.c.poll("1791201600")
        self.assertIn("apikey=example-api-key", self.fetch.calls[0][0])

    def test_unexpected_shapes_are_shape_errors(self):
        with self.assertRaises(ShapeError):
            parse_slots({"queue": {}})
        with self.assertRaises(ShapeError):
            parse_slots({"history": {"slots": [{"nzo_id": "x"}]}})
