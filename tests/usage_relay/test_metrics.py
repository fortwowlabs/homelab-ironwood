from __future__ import annotations

import unittest

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.metrics import Metrics, metric_names

EVENTS = "homelab_usage_events_total"


class MetricsTests(unittest.TestCase):
    def test_the_catalog_names_exactly_the_spec_series(self):
        self.assertEqual(set(metric_names()), {
            "homelab_usage_events_total",
            "homelab_usage_playback_active",
            "homelab_usage_watch_seconds_total",
            "homelab_usage_collector_last_success_timestamp",
            "homelab_usage_collector_errors_total",
            "homelab_usage_push_failures_total",
        })

    def test_counters_accumulate_per_label_set(self):
        m = Metrics()
        labels = {"service": "seerr", "kind": "request.created", "user": "alice"}
        m.inc(EVENTS, labels)
        m.inc(EVENTS, labels)
        self.assertEqual(m.value(EVENTS, labels), 2)
        self.assertEqual(m.value(EVENTS, {**labels, "user": "bob"}), 0)

    def test_render_has_help_type_and_sorted_labels(self):
        m = Metrics()
        m.inc("homelab_usage_collector_errors_total", {"collector": "seerr"}, 0)
        m.inc(EVENTS, {"user": "a", "kind": "k", "service": "s"})
        text = m.render()
        self.assertIn("# TYPE homelab_usage_collector_errors_total counter\n", text)
        self.assertIn('homelab_usage_collector_errors_total{collector="seerr"} 0\n', text)
        self.assertIn('homelab_usage_events_total{kind="k",service="s",user="a"} 1\n', text)

    def test_a_missing_user_renders_as_none(self):
        m = Metrics()
        m.inc(EVENTS, {"service": "sonarr", "kind": "download.imported", "user": None})
        self.assertIn('user="none"', m.render())

    def test_label_values_are_escaped(self):
        m = Metrics()
        m.inc(EVENTS, {"service": "s", "kind": "k", "user": 'a"b\\c'})
        self.assertIn('user="a\\"b\\\\c"', m.render())

    def test_timestamps_render_without_an_exponent(self):
        m = Metrics()
        m.set("homelab_usage_collector_last_success_timestamp", {"collector": "seerr"}, 1791201600.0)
        m.set("homelab_usage_collector_last_success_timestamp", {"collector": "sonarr"}, 0.5)
        text = m.render()
        self.assertIn('{collector="seerr"} 1791201600\n', text)
        self.assertIn('{collector="sonarr"} 0.5\n', text)

    def test_replace_swaps_a_whole_gauge(self):
        m = Metrics()
        name = "homelab_usage_playback_active"
        m.replace(name, [({"user": "alice", "mode": "direct"}, 1)])
        self.assertIn('homelab_usage_playback_active{mode="direct",user="alice"} 1\n', m.render())
        m.replace(name, [])
        self.assertNotIn("homelab_usage_playback_active{", m.render())
        self.assertIn("# TYPE homelab_usage_playback_active gauge\n", m.render())

    def test_an_unknown_metric_is_an_error(self):
        with self.assertRaises(KeyError):
            Metrics().inc("homelab_usage_typo_total", {})
