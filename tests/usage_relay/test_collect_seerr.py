from __future__ import annotations

import unittest

from helpers import FakeClock, FakeFetch, fixture
from usage_relay.collect_seerr import SeerrCollector, parse_requests
from usage_relay.fetch import FetchError, ShapeError

REQUESTS = fixture("seerr_requests.json")
BASE = "http://seerr.example"


class SeerrTests(unittest.TestCase):
    def setUp(self):
        self.fetch = FakeFetch({
            f"{BASE}/api/v1/request": REQUESTS,
            f"{BASE}/api/v1/movie/9001": {"id": 9001, "title": "Example Movie"},
            f"{BASE}/api/v1/movie/9002": {"id": 9002, "title": "Old Example"},
            f"{BASE}/api/v1/tv/7001": {"id": 7001, "name": "The Example Show"},
        })
        self.c = SeerrCollector(base_url=BASE, api_key="example-api-key", fetch=self.fetch,
                                interval=60, clock=FakeClock())

    def test_the_first_poll_is_a_baseline_with_no_title_lookups(self):
        self.assertEqual(self.c.poll(None), ([], "2026-10-05T12:13:00Z"))
        self.assertEqual(len(self.fetch.calls), 1)

    def test_requests_since_the_mark_become_events(self):
        events, mark = self.c.poll("2026-10-05T12:00:00Z")
        self.assertEqual(mark, "2026-10-05T12:13:00Z")
        self.assertEqual([e.id for e in events], [
            "seerr:request:11:created",
            "seerr:request:12:created",
            "seerr:request:12:approved",
            "seerr:request:12:available",
            "seerr:request:3:approved",
            "seerr:request:3:available",
        ])

    def test_an_old_request_touched_now_is_not_announced_as_new(self):
        events, _ = self.c.poll("2026-10-05T12:00:00Z")
        self.assertNotIn("seerr:request:3:created", [e.id for e in events])

    def test_users_titles_and_match_keys(self):
        events = {e.id: e for e in self.c.poll("2026-10-05T12:00:00Z")[0]}
        movie = events["seerr:request:11:created"]
        self.assertEqual((movie.user, movie.title, movie.ts), ("alice", "Example Movie", "2026-10-05T12:10:00Z"))
        self.assertEqual(movie.detail, {"media_type": "movie", "request_id": 11, "seasons": [],
                                        "match_key": "tmdb:9001"})
        show = events["seerr:request:12:available"]
        self.assertEqual((show.user, show.title, show.ts), ("bob", "The Example Show", "2026-10-05T12:12:00Z"))
        self.assertEqual(show.detail["match_key"], "tvdb:4001")

    def test_titles_are_cached_between_polls(self):
        self.c.poll("2026-10-05T12:00:00Z")
        calls = len(self.fetch.calls)
        self.c.poll("2026-10-05T12:13:00Z")
        self.assertEqual(len(self.fetch.calls), calls + 1)

    def test_the_api_key_is_a_header(self):
        self.c.poll(None)
        self.assertEqual(self.fetch.calls[0][1], {"X-Api-Key": "example-api-key"})

    def test_unexpected_shapes_are_shape_errors(self):
        with self.assertRaises(ShapeError):
            parse_requests({"pageInfo": {}})
        with self.assertRaises(ShapeError):
            parse_requests({"results": [{"id": 1}]})

    def test_a_failed_title_lookup_degrades_to_a_placeholder(self):
        self.fetch.routes[f"{BASE}/api/v1/movie/9001"] = FetchError("HTTP 404 from x")
        events = {e.id: e for e in self.c.poll("2026-10-05T12:00:00Z")[0]}
        self.assertEqual(events["seerr:request:11:created"].title, "movie tmdb:9001")
        self.assertEqual(events["seerr:request:12:created"].title, "The Example Show")
        self.assertEqual(len(events), 6)

    def test_a_request_yielding_no_events_fetches_no_title(self):
        # updatedAt >= mark (passes the outer filter), createdAt < mark (not
        # "created"), status 1 = pending (not "approved"), media status 2 =
        # pending (not "available") -- no event_spec survives, so _events_for
        # must return before ever calling self._title(). tmdbId is set (and
        # NOT registered with FakeFetch) so that, if the early return were
        # removed, _title would attempt a real fetch and the test would fail
        # loudly instead of passing by accident (tmdbId=None short-circuits
        # _title without fetching, which would hide a removed early return).
        payload = {"results": [{
            "id": 50, "status": 1, "type": "tv",
            "createdAt": "2026-10-05T11:00:00.000Z", "updatedAt": "2026-10-05T12:05:00.000Z",
            "media": {"tmdbId": 9050, "tvdbId": 7001, "status": 2},
            "requestedBy": {"displayName": "Dana", "jellyfinUsername": "dana"}, "seasons": []}]}
        fetch = FakeFetch({f"{BASE}/api/v1/request": payload})
        c = SeerrCollector(base_url=BASE, api_key="example-api-key", fetch=fetch, interval=60,
                           clock=FakeClock())
        events, _ = c.poll("2026-10-05T12:00:00Z")
        self.assertEqual(events, [])
        self.assertNotIn(f"{BASE}/api/v1/tv/9050", [url for url, _ in fetch.calls])

    def test_a_first_deploy_does_not_resurrect_an_existing_auto_approved_request(self):
        # Reproduces F1: a request that already existed at first-poll time,
        # with createdAt == updatedAt (an auto-approved request looks like
        # this), must not be re-announced as "created" on the very next poll
        # just because it is still the newest row the collector has seen.
        payload = {"results": [{
            "id": 99, "status": 2, "type": "movie",
            "createdAt": "2026-10-05T11:00:00.000Z", "updatedAt": "2026-10-05T11:00:00.000Z",
            "media": {"tmdbId": 9099, "tvdbId": None, "status": 1},
            "requestedBy": {"displayName": "Carol", "jellyfinUsername": "carol"}, "seasons": []}]}
        fetch = FakeFetch({f"{BASE}/api/v1/request": payload})
        # FakeClock() starts at 2026-10-05T12:00:00Z, AFTER this request's
        # createdAt/updatedAt of 11:00:00Z -- the shape that makes the old
        # "baseline = latest" code set the mark to 11:00:00Z instead of the
        # clock, so the unchanged request reappears on the next poll.
        c = SeerrCollector(base_url=BASE, api_key="example-api-key", fetch=fetch, interval=60,
                           clock=FakeClock())
        _, mark = c.poll(None)
        events, _ = c.poll(mark)
        self.assertEqual(events, [])
