from __future__ import annotations

import unittest

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.model import Event, from_epoch, norm_user, parse_ts, utc_iso


def make(**overrides) -> Event:
    fields = {"id": "x:1", "ts": "2026-10-05T12:00:00Z", "service": "sonarr",
              "kind": "download.imported", "user": None, "title": "t"}
    fields.update(overrides)
    return Event(**fields)


class TimeTests(unittest.TestCase):
    def test_seven_digit_dotnet_fractions_parse(self):
        self.assertEqual(utc_iso(parse_ts("2026-10-05T12:00:00.1234567Z")), "2026-10-05T12:00:00Z")

    def test_offsets_convert_to_utc(self):
        self.assertEqual(utc_iso(parse_ts("2026-10-05T14:00:00+02:00")), "2026-10-05T12:00:00Z")

    def test_naive_timestamps_are_treated_as_utc(self):
        self.assertEqual(utc_iso(parse_ts("2026-10-05T12:00:00")), "2026-10-05T12:00:00Z")

    def test_from_epoch(self):
        self.assertEqual(from_epoch(1_791_201_600), "2026-10-05T12:00:00Z")


class EventTests(unittest.TestCase):
    def test_a_valid_event_constructs_with_empty_detail(self):
        self.assertEqual(make().detail, {})

    def test_an_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            make(kind="download.exploded")

    def test_an_unnormalised_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            make(ts="2026-10-05T12:00:00+00:00")

    def test_an_empty_id_is_rejected(self):
        with self.assertRaises(ValueError):
            make(id="")


class UserTests(unittest.TestCase):
    def test_names_are_trimmed_and_lowercased(self):
        self.assertEqual(norm_user(" Alice "), "alice")

    def test_blank_and_missing_names_are_none(self):
        self.assertIsNone(norm_user(""))
        self.assertIsNone(norm_user(None))
