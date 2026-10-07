from __future__ import annotations

import unittest

from helpers import FakeClock, FakeFetch, fixture
from usage_relay.collect_arr import ArrCollector, parse_history
from usage_relay.fetch import ShapeError

SONARR = fixture("sonarr_history.json")
RADARR = fixture("radarr_history.json")


class ParseHistoryTests(unittest.TestCase):
    def test_only_usage_event_types_become_events(self):
        events, latest = parse_history("sonarr", SONARR)
        self.assertEqual([e.kind for e in events],
                         ["download.grabbed", "download.imported", "download.failed"])
        self.assertEqual([e.id for e in events],
                         ["sonarr:history:101", "sonarr:history:102", "sonarr:history:104"])
        self.assertEqual(latest, "2026-10-05T12:03:00Z")

    def test_episode_titles_keys_and_normalised_times(self):
        events, _ = parse_history("sonarr", SONARR)
        self.assertEqual(events[0].ts, "2026-10-05T11:58:00Z")
        self.assertEqual(events[1].title, "The Example Show S02E03")
        self.assertEqual(events[1].detail, {"series": "The Example Show", "series_key": "sonarr:series:7",
                                            "season": 2, "match_key": "tvdb:4001"})
        self.assertIsNone(events[1].user)

    def test_failures_carry_the_reason(self):
        events, _ = parse_history("sonarr", SONARR)
        self.assertEqual(events[2].detail["reason"], "Download client reported failure")

    def test_radarr_titles_and_match_key(self):
        events, _ = parse_history("radarr", RADARR)
        self.assertEqual(events[0].title, "Example Movie (2024)")
        self.assertEqual(events[0].detail, {"match_key": "tmdb:9001"})

    def test_unexpected_shapes_are_shape_errors(self):
        with self.assertRaises(ShapeError):
            parse_history("sonarr", {"records": []})
        with self.assertRaises(ShapeError):
            parse_history("sonarr", [{"id": 1}])


class ArrCollectorTests(unittest.TestCase):
    def collector(self, service, routes):
        self.fetch = FakeFetch(routes)
        return ArrCollector(service=service, base_url=f"http://{service}.example",
                            api_key="example-api-key", fetch=self.fetch, interval=60, clock=FakeClock())

    def test_the_first_poll_is_a_baseline_and_fetches_nothing(self):
        c = self.collector("sonarr", {})
        self.assertEqual(c.poll(None), ([], "2026-10-05T12:00:00Z"))
        self.assertEqual(self.fetch.calls, [])

    def test_a_poll_asks_for_history_since_the_mark(self):
        c = self.collector("sonarr", {"http://sonarr.example/api/v3/history/since": SONARR})
        events, mark = c.poll("2026-10-05T11:00:00Z")
        url, headers = self.fetch.calls[0]
        self.assertEqual(url, "http://sonarr.example/api/v3/history/since"
                              "?date=2026-10-05T11%3A00%3A00Z&includeSeries=true&includeEpisode=true")
        self.assertEqual(headers, {"X-Api-Key": "example-api-key"})
        self.assertEqual(len(events), 3)
        self.assertEqual(mark, "2026-10-05T12:03:00Z")

    def test_an_empty_page_keeps_the_mark(self):
        c = self.collector("sonarr", {"http://sonarr.example/api/v3/history/since": []})
        self.assertEqual(c.poll("2026-10-05T11:00:00Z"), ([], "2026-10-05T11:00:00Z"))

    def test_radarr_asks_for_movies(self):
        c = self.collector("radarr", {"http://radarr.example/api/v3/history/since": RADARR})
        c.poll("2026-10-05T11:00:00Z")
        self.assertTrue(self.fetch.calls[0][0].endswith("&includeMovie=true"))

    def test_only_sonarr_and_radarr_are_accepted(self):
        with self.assertRaises(ValueError):
            ArrCollector(service="lidarr", base_url="http://x", api_key="k", fetch=FakeFetch(), interval=60)
