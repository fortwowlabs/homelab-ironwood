"""The relay's core: poll collectors, record, count, push, watch its own health.

Order inside poll_collector() is what makes restarts lossless: events are
inserted BEFORE the new mark is stored, so a crash between the two re-reads a
page whose rows the store then drops as duplicates — never skips one.

SQLite errors are deliberately fatal: a relay that pushes without recording
would break "emit the number the alert used". Anything else that escapes the
run loop -- an unwritable snapshot path, say -- also exits the process
non-zero rather than being swallowed. Either way systemd's Restart=on-failure
brings it back, and a relay that keeps failing reaches `failed` once it hits
the start limit.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .collect_jellyfin import JellyfinCollector
from .fetch import FetchError, ShapeError
from .metrics import Metrics
from .model import Event, Push, from_epoch
from .ntfy import NtfyError
from .router import Router
from .store import Store


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@dataclass
class CollectorHealth:
    enabled: bool
    last_success: float | None = None
    last_error: str | None = None
    failing_since: float | None = None
    alerted: bool = False


class Relay:
    def __init__(self, *, store: Store, router: Router, ntfy, metrics: Metrics,
                 collectors: dict[str, object | None], alert_topic: str, alert_after: float,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = _stderr) -> None:
        self.store = store
        self.router = router
        self.ntfy = ntfy
        self.metrics = metrics
        self.collectors = collectors
        self.alert_topic = alert_topic
        self.alert_after = alert_after
        self.clock = clock
        self.log = log
        self.health = {name: CollectorHealth(enabled=c is not None) for name, c in collectors.items()}
        for name, collector in collectors.items():
            if collector is not None:
                # Present at 0 from the start, so a dashboard shows "no errors"
                # rather than "no data" for a collector that has never failed.
                metrics.inc("homelab_usage_collector_errors_total", {"collector": name}, 0)

    # -- recording -----------------------------------------------------------

    def _attribute(self, event: Event) -> Event:
        if event.kind != "download.imported" or not event.detail.get("match_key"):
            return event
        requester = self.store.requester_for(event.detail["match_key"])
        if requester is None:
            return event
        return replace(event, detail={**event.detail, "requested_by": requester})

    def ingest(self, events: list[Event]) -> int:
        inserted = 0
        for raw in events:
            event = self._attribute(raw)
            if not self.store.insert(event, self.router.initial_state(event)):
                continue
            inserted += 1
            self.metrics.inc("homelab_usage_events_total",
                             {"service": event.service, "kind": event.kind, "user": event.user})
            if event.kind == "playback.stopped":
                self.metrics.inc("homelab_usage_watch_seconds_total", {"user": event.user},
                                 float(event.detail.get("watched_seconds", 0)))
        return inserted

    def seed_counters(self) -> None:
        """Set (not increment) the event and watch-seconds counters from
        SQLite, so a restart does not reset a low-volume series to 1 and cost
        it Prometheus's increase() ignoring a series' first sample."""
        for service, kind, user, count in self.store.count_by_label():
            self.metrics.set("homelab_usage_events_total",
                             {"service": service, "kind": kind, "user": user}, count)
        for user, seconds in self.store.watched_by_user():
            self.metrics.set("homelab_usage_watch_seconds_total", {"user": user}, seconds)

    # -- collectors ----------------------------------------------------------

    def _failed(self, name: str, message: str) -> None:
        health = self.health[name]
        health.last_error = message
        if health.failing_since is None:
            health.failing_since = self.clock()
        self.metrics.inc("homelab_usage_collector_errors_total", {"collector": name})
        self.log(f"{name}: {message}")

    def poll_collector(self, name: str) -> None:
        collector = self.collectors[name]
        if collector is None:
            return
        try:
            events, mark = collector.poll(self.store.get_mark(name))
        except sqlite3.Error:
            raise
        except (FetchError, ShapeError) as exc:
            self._failed(name, str(exc))
            return
        except Exception as exc:  # a collector bug must not stop the others
            self._failed(name, f"{type(exc).__name__}: {exc}")
            self.log(traceback.format_exc())
            return
        self.ingest(events)
        self.store.set_mark(name, mark)
        now = self.clock()
        health = self.health[name]
        health.last_success, health.last_error, health.failing_since = now, None, None
        self.metrics.set("homelab_usage_collector_last_success_timestamp", {"collector": name}, now)
        if isinstance(collector, JellyfinCollector):
            counts = Counter(collector.active)
            self.metrics.replace("homelab_usage_playback_active",
                                 [({"user": user, "mode": mode}, n) for (user, mode), n in counts.items()])
        if health.alerted and self._alert(f"usage-relay: {name} collector recovered",
                                          "Polling succeeds again.", 3):
            health.alerted = False

    # -- pushing -------------------------------------------------------------

    def push_cycle(self) -> None:
        pushes, expired = self.router.plan(self.store.pending(), self.clock())
        if expired:
            self.store.set_pushed(expired, "expired")
        for push in pushes:
            try:
                self.ntfy.publish(push)
            except NtfyError as exc:
                self.metrics.inc("homelab_usage_push_failures_total", {"topic": push.topic})
                self.log(f"push to {push.topic} failed: {exc}")
                continue
            self.store.set_pushed(push.event_ids, "sent")

    def _alert(self, title: str, message: str, priority: int) -> bool:
        try:
            self.ntfy.publish(Push(topic=self.alert_topic, title=title, message=message,
                                   priority=priority, event_ids=()))
        except NtfyError as exc:
            self.metrics.inc("homelab_usage_push_failures_total", {"topic": self.alert_topic})
            self.log(f"alert to {self.alert_topic} failed: {exc}")
            return False
        return True

    def check_health(self) -> None:
        now = self.clock()
        for name, health in self.health.items():
            if health.alerted or health.failing_since is None:
                continue
            if now - health.failing_since < self.alert_after:
                continue
            minutes = int((now - health.failing_since) // 60)
            if self._alert(f"usage-relay: {name} collector failing",
                           f"No successful poll for {minutes} min. Last error: {health.last_error}", 4):
                health.alerted = True

    # -- verification and upkeep --------------------------------------------

    def selftest_insert(self) -> str:
        now = self.clock()
        event = Event(id=f"selftest:{int(now * 1000)}", ts=from_epoch(now), service="relay",
                      kind="selftest", user=None, title="usage-relay selftest")
        self.ingest([event])
        return event.id

    def maintenance(self, *, retention_days: int, snapshot_path: str) -> None:
        cutoff = from_epoch(self.clock() - retention_days * 86400)
        removed = self.store.prune(cutoff)
        self.store.snapshot(snapshot_path)
        self.log(f"maintenance: pruned {removed} rows older than {cutoff}; wrote {snapshot_path}")

    def health_report(self) -> dict:
        return {
            "now": self.clock(),
            "collectors": {name: {"enabled": h.enabled, "last_success": h.last_success,
                                  "last_error": h.last_error}
                           for name, h in self.health.items()},
        }

    def run(self, *, push_interval: float, maintenance_time: str, retention_days: int,
            snapshot_path: str, stop: threading.Event) -> None:
        due = {name: 0.0 for name, c in self.collectors.items() if c is not None}
        next_push = 0.0
        last_maintenance = None
        if not Path(snapshot_path).exists():
            self.maintenance(retention_days=retention_days, snapshot_path=snapshot_path)
            last_maintenance = datetime.fromtimestamp(self.clock()).date()
        while not stop.is_set():
            now = self.clock()
            for name in due:
                if now >= due[name]:
                    self.poll_collector(name)
                    due[name] = now + self.collectors[name].interval
            if now >= next_push:
                self.push_cycle()
                self.check_health()
                next_push = now + push_interval
            local = datetime.fromtimestamp(now)
            if local.strftime("%H:%M") >= maintenance_time and local.date() != last_maintenance:
                self.maintenance(retention_days=retention_days, snapshot_path=snapshot_path)
                last_maintenance = local.date()
            stop.wait(1.0)
