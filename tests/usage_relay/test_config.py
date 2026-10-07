from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, FakeFetch, settings_dict
from usage_relay.collect_arr import ArrCollector
from usage_relay.config import (
    COLLECTOR_NAMES,
    CollectorSettings,
    build_collectors,
    from_dict,
    load,
)

ALL_KEYS = {f"USAGE_{name.upper()}_API_KEY": "example-api-key"
            for name in ("jellyfin", "seerr", "sonarr", "radarr", "sabnzbd")}

CONFIG_TEMPLATE = ROOT / "roles/svc_infra/templates/usage-relay-config.json.j2"
KEYS_TEMPLATE = ROOT / "roles/svc_infra/templates/usage-relay-keys.env.j2"
VAULT_EXAMPLE = ROOT / "inventory/group_vars/all_vault.yml.example"


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


class KeyReferenceGateTests(unittest.TestCase):
    """Spec's Testing section: 'A validate gate checking that every collector
    named in the config has a key reference in the env template.' Reads the
    three rendered/documented sources directly off disk rather than the
    Python settings this module already exercises, so a drift between the
    template and the vault example is caught even though both would still
    "load" fine in isolation."""

    def setUp(self):
        self.config_text = CONFIG_TEMPLATE.read_text(encoding="utf-8")
        self.keys_text = KEYS_TEMPLATE.read_text(encoding="utf-8")
        self.vault_text = VAULT_EXAMPLE.read_text(encoding="utf-8")

    def test_every_key_env_in_the_config_template_has_exactly_one_line_in_the_keys_template(self):
        key_envs = re.findall(r'"key_env":\s*"(USAGE_[A-Z0-9_]+)"', self.config_text)
        # Positive control: a regex that stopped matching (a renamed field,
        # a reindented template) must fail this test, not silently pass it
        # by comparing two empty sets.
        self.assertEqual(len(key_envs), 5)
        for key_env in key_envs:
            lines = re.findall(rf"^{key_env}=.*$", self.keys_text, re.M)
            self.assertEqual(len(lines), 1,
                             f"{key_env} should appear exactly once in {KEYS_TEMPLATE.name}, found {lines}")

    def test_every_vault_variable_referenced_in_the_keys_template_exists_in_the_vault_example(self):
        referenced = re.findall(r"\b(vault_usage_[a-z0-9_]+)\b", self.keys_text)
        self.assertTrue(referenced)  # positive control
        declared = set(re.findall(r"^(vault_usage_[a-z0-9_]+):", self.vault_text, re.M))
        for var in referenced:
            self.assertIn(var, declared)

    def test_the_config_templates_collectors_match_config_collector_names(self):
        names = set(re.findall(r'^\s*"([a-z]+)":\s*\{"base_url"', self.config_text, re.M))
        self.assertTrue(names)  # positive control
        self.assertEqual(names, set(COLLECTOR_NAMES))
