from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import FakeFetch, settings_dict
from usage_relay.collect_arr import ArrCollector
from usage_relay.config import CollectorSettings, build_collectors, from_dict, load

ALL_KEYS = {f"USAGE_{name.upper()}_API_KEY": "example-api-key"
            for name in ("jellyfin", "seerr", "sonarr", "radarr", "sabnzbd")}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.raw = settings_dict(tmp.name)

    def test_a_rendered_config_loads(self):
        settings = from_dict(self.raw)
        self.assertEqual(settings.collectors["sonarr"],
                         CollectorSettings(base_url="http://sonarr.example", interval=60,
                                           key_env="USAGE_SONARR_API_KEY"))
        self.assertEqual(settings.push_ignore_users, frozenset({"brandon", "admin"}))
        self.assertEqual(settings.topics["digest"], "usage-digest")

    def test_load_reads_a_json_file(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps(self.raw), encoding="utf-8")
        self.assertEqual(load(str(path)).retention_days, 365)

    def test_a_missing_setting_is_rejected_by_name(self):
        del self.raw["db_path"]
        with self.assertRaisesRegex(ValueError, "db_path"):
            from_dict(self.raw)

    def test_a_missing_topic_is_rejected(self):
        del self.raw["topics"]["digest"]
        with self.assertRaisesRegex(ValueError, "digest"):
            from_dict(self.raw)

    def test_an_unknown_collector_is_rejected(self):
        self.raw["collectors"]["lidarr"] = {"base_url": "http://x", "interval": 60, "key_env": "K"}
        with self.assertRaisesRegex(ValueError, "lidarr"):
            from_dict(self.raw)

    def test_a_malformed_maintenance_time_is_rejected(self):
        self.raw["maintenance_time"] = "2:30"
        with self.assertRaises(ValueError):
            from_dict(self.raw)

    def test_collectors_without_a_key_are_disabled(self):
        built = build_collectors(from_dict(self.raw), {"USAGE_SONARR_API_KEY": "example-api-key"}, FakeFetch())
        self.assertIsInstance(built["sonarr"], ArrCollector)
        self.assertEqual(built["sonarr"].name, "sonarr")
        self.assertEqual({n for n, c in built.items() if c is None}, {"jellyfin", "seerr", "radarr", "sabnzbd"})

    def test_every_collector_type_builds(self):
        built = build_collectors(from_dict(self.raw), ALL_KEYS, FakeFetch())
        self.assertEqual({n: type(c).__name__ for n, c in built.items()}, {
            "jellyfin": "JellyfinCollector", "seerr": "SeerrCollector", "sonarr": "ArrCollector",
            "radarr": "ArrCollector", "sabnzbd": "SabnzbdCollector"})
