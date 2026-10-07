"""Run the relay, send the weekly digest, or prove the relay works.

    python3 -m usage_relay --config /etc/usage-relay/config.json run
    python3 -m usage_relay --config ... digest
    python3 -m usage_relay --config ... selftest     # make verify's positive control
    python3 -m usage_relay --config ... check --wait 90
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings, build_collectors, load
from .digest import render
from .fetch import get_json
from .httpserver import RelayHTTPServer
from .metrics import Metrics
from .model import Push, utc_iso
from .ntfy import Ntfy, NtfyError
from .relay import Relay
from .router import Router
from .store import Store

FRESH_SECONDS = 300
_SELFTEST_SAMPLE = re.compile(r'^homelab_usage_events_total\{[^}]*kind="selftest"[^}]*\} (\S+)$', re.M)


def _err(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _http(url: str, data: bytes | None = None, timeout: float = 10.0) -> str:
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def selftest_count(metrics_text: str) -> float:
    return sum(float(value) for value in _SELFTEST_SAMPLE.findall(metrics_text))


def assess(report: dict) -> tuple[list[str], list[str]]:
    """One line per collector, plus the names that are enabled but not fresh."""
    now = float(report["now"])
    lines: list[str] = []
    stale: list[str] = []
    for name, health in sorted(report["collectors"].items()):
        if not health["enabled"]:
            lines.append(f"{name}: disabled (no API key)")
            continue
        error = f" — {health['last_error']}" if health.get("last_error") else ""
        if health.get("last_success") is None:
            stale.append(name)
            lines.append(f"{name}: never looked{error}")
            continue
        age = int(now - float(health["last_success"]))
        if age > FRESH_SECONDS:
            stale.append(name)
            lines.append(f"{name}: stale, last looked {age}s ago{error}")
        else:
            lines.append(f"{name}: ok ({age}s ago)")
    return lines, stale


def cmd_run(settings: Settings, env: Mapping[str, str]) -> int:
    ntfy_url = env.get("NTFY_URL", "")
    if not ntfy_url:
        _err("usage-relay: NTFY_URL is not set; is /etc/homelab-notify.env loaded?")
        return 1
    store = Store(settings.db_path)
    metrics = Metrics()
    router = Router(store=store, topics=settings.topics, batch_window=settings.batch_window,
                    resume_window=settings.resume_window, playback_expiry=settings.playback_expiry,
                    push_ignore_users=settings.push_ignore_users)
    collectors = build_collectors(settings, env, get_json)
    for name, collector in collectors.items():
        if collector is None:
            _err(f"WARNING: {name} collector disabled: {settings.collectors[name].key_env} is empty")
    relay = Relay(store=store, router=router, ntfy=Ntfy(ntfy_url, env.get("NTFY_TOKEN", "")),
                  metrics=metrics, collectors=collectors,
                  alert_topic=env.get("NTFY_ALERT_TOPIC") or env.get("NTFY_TOPIC") or "homelab-alerts",
                  alert_after=settings.collector_alert_after)
    server = RelayHTTPServer((settings.listen_host, settings.listen_port), relay, metrics)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        relay.run(push_interval=settings.push_interval, maintenance_time=settings.maintenance_time,
                  retention_days=settings.retention_days, snapshot_path=settings.snapshot_path, stop=stop)
    finally:
        server.shutdown()
        server.server_close()
        store.close()
    return 0


def cmd_digest(settings: Settings, ntfy, now: datetime) -> int:
    if not Path(settings.db_path).exists():
        _err(f"digest: no database at {settings.db_path}; refusing to report an empty week")
        return 1
    start = now - timedelta(days=7)
    try:
        store = Store(settings.db_path)
        try:
            events = store.events_between(utc_iso(start), utc_iso(now))
        finally:
            store.close()
    except sqlite3.Error as exc:
        _err(f"digest: could not read events: {exc}")
        return 1
    text = render(events, start, now)
    tmp = f"{settings.report_path}.tmp"
    Path(tmp).write_text(text, encoding="utf-8")
    os.replace(tmp, settings.report_path)
    try:
        ntfy.publish(Push(topic=settings.topics["digest"], title="Weekly usage", message=text,
                          priority=3, event_ids=()))
    except NtfyError as exc:
        _err(f"digest: written to {settings.report_path} but not published: {exc}")
        return 1
    print(text, end="")
    return 0


def _selftest_checks(settings: Settings, ntfy, base: str, event_id: str, before: float,
                     timeout: float, sleep: Callable[[float], None],
                     clock: Callable[[], float]) -> str | None:
    topic = settings.topics["selftest"]
    deadline = clock() + timeout
    last_error = ""
    while True:
        try:
            if any(event_id in m.get("message", "") for m in ntfy.poll(topic)):
                break
        except NtfyError as exc:
            last_error = f" (last poll: {exc})"
        if clock() >= deadline:
            return f"{event_id} never arrived on {topic} within {timeout:g}s{last_error}"
        sleep(3)
    try:
        after = selftest_count(_http(f"{base}/metrics"))
    except OSError as exc:
        return f"could not re-read {base}/metrics: {exc}"
    if after < before + 1:
        return f"the events counter did not move ({before:g} -> {after:g})"
    store = Store(settings.db_path)
    try:
        if store.get(event_id) is None:
            return f"{event_id} is not in {settings.db_path}"
    finally:
        store.close()
    return None


def cmd_selftest(settings: Settings, ntfy, timeout: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> int:
    base = f"http://127.0.0.1:{settings.listen_port}"
    try:
        before = selftest_count(_http(f"{base}/metrics"))
        event_id = json.loads(_http(f"{base}/selftest", data=b""))["id"]
    except (OSError, ValueError, KeyError) as exc:
        _err(f"selftest: the relay did not answer on {base}: {exc}")
        return 1
    try:
        problem = _selftest_checks(settings, ntfy, base, event_id, before, timeout, sleep, clock)
    finally:
        store = Store(settings.db_path)
        try:
            store.delete(event_id)
        finally:
            store.close()
    if problem:
        _err(f"selftest: {problem}")
        return 1
    print(f"selftest OK: {event_id} recorded, counted and pushed")
    return 0


def cmd_check(settings: Settings, wait: float, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.time) -> int:
    url = f"http://127.0.0.1:{settings.listen_port}/health"
    deadline = clock() + wait
    while True:
        try:
            lines, stale = assess(json.loads(_http(url)))
        except (OSError, ValueError, KeyError) as exc:
            _err(f"check: the relay did not answer on {url}: {exc}")
            return 1
        if not stale or clock() >= deadline:
            break
        sleep(5)
    print("\n".join(lines))
    return 1 if stale else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="usage_relay", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("run", "digest", "selftest", "check"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--wait", type=float, default=60.0,
                        help="check: seconds to wait for every enabled collector to look")
    args = parser.parse_args(argv)
    settings = load(args.config)
    env = os.environ
    if args.command == "run":
        return cmd_run(settings, env)
    if args.command == "check":
        return cmd_check(settings, args.wait)
    ntfy_url = env.get("NTFY_URL", "")
    if not ntfy_url:
        _err(f"{args.command}: NTFY_URL is not set; is /etc/homelab-notify.env loaded?")
        return 1
    ntfy = Ntfy(ntfy_url, env.get("NTFY_TOKEN", ""))
    if args.command == "digest":
        return cmd_digest(settings, ntfy, datetime.now().astimezone())
    return cmd_selftest(settings, ntfy)
