# Usage Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a relay on svc-infra that polls Jellyfin, Seerr, Sonarr, Radarr
and SABnzbd. It records household usage to SQLite, pushes four kinds of event
to their own ntfy topics, exports Prometheus counters for a Grafana dashboard,
and sends a weekly digest.

**Architecture:**

- **Pull, not push.** A stdlib-only Python package runs as a host systemd
  service under the existing `homelab` user. Each collector polls one
  service's history or session API and returns normalized `Event` rows.
- **One place for each event.** The relay dedups rows into SQLite on their
  primary key. Then the router decides which rows push and to which topic,
  and a metrics registry counts each new row exactly once.
- **Verification.** A loopback-only `POST /selftest` and a `/health` endpoint
  let `make verify` prove the whole pipeline works and that each collector
  actually looked.

**Tech Stack:**

- Python 3.12 stdlib only: `sqlite3`, `urllib`, `http.server`, `threading`,
  `unittest`.
- Ansible for deployment: `copy`, `template`, `systemd`, `ansible.posix.firewalld`.
- systemd for the relay service and the digest timer.
- Prometheus and a provisioned Grafana dashboard.
- ntfy, publishing with JSON bodies.

**Spec:** `docs/superpowers/specs/2026-10-06-usage-notifications-design.md`.
Read it before starting. This plan implements sub-project 1 only.

## Global Constraints

### Language, layout and tests

- **Python and dependencies.** Python 3.12 (svc-infra runs 3.12.13; ruff
  targets `py312`). Use the **stdlib only**: no pip installs, on the host or
  in the venv.
- **Package layout.** A flat package at `roles/svc_infra/files/usage_relay/`.
  Every module is a sibling, with no subpackages. That way Ansible installs
  it with one `with_fileglob: "usage_relay/*.py"`.
- **Tests.** Tests are stdlib `unittest` in `tests/usage_relay/test_*.py`,
  with fixtures in `tests/fixtures/usage_relay/`.
  - They are run by the discovered gate `tests/validate_usage_relay.py`
    (`GATE_GROUP = "shell"`).
  - There is no pytest in this repo. Do not add it.
  - Run a subset with `.venv/bin/python tests/validate_usage_relay.py <substring>`.
- **Fixture values.** Fixture credentials are `example-api-key`, and fixture
  hostnames use `.example`. Realistic-looking fake keys trip
  `tests/scan_history_secrets.py`.
- **Lint.** `ruff` must pass on the package and on the tests. Task 1 adds the
  package path to `make validate-python`.

### Timestamps, event kinds and push states

- **Timestamps.** Every timestamp in an `Event` is `YYYY-MM-DDTHH:MM:SSZ`, in
  UTC with no fraction. Produce it with `model.utc_iso()` or
  `model.from_epoch()`. `Event` rejects anything else.
- **Event kinds** (from the spec, exact):
  - `request.created`, `request.approved`, `request.available`
  - `download.grabbed`, `download.completed`, `download.imported`, `download.failed`
  - `playback.started`, `playback.stopped`
  - `selftest`
- **Push states:** `pending`, `sent`, `suppressed`, `expired`, `none`.

### Topics, users and windows

- **Topics:**

  | Key | Topic |
  |---|---|
  | requests | `usage-requests` |
  | library | `usage-library` |
  | playback | `usage-playback` |
  | failures | `usage-failures` |
  | digest | `usage-digest` |
  | selftest | `usage-selftest` |

  The relay's own collector-health alerts go to `NTFY_ALERT_TOPIC`
  (`homelab-alerts`).
- **Priorities:** requests 3, library 3, failures 4, playback 2, selftest 1,
  digest 3. Collector failing 4, recovered 3.
- **Users** are lowercased source usernames. `usage_push_ignore_users`
  defaults to `[brandon, admin]` and applies to `playback.started` only.
- **Windows:**
  - Batching of imports: 120 s.
  - Resume suppression: 1800 s.
  - Playback push expiry: 600 s.
  - Collector alert: 900 s.
  - Retention: 365 days.
  - Freshness, as judged by `check`: 300 s.

### Metric names (exact)

- `homelab_usage_events_total{service,kind,user}`
- `homelab_usage_playback_active{user,mode}`
- `homelab_usage_watch_seconds_total{user}`
- `homelab_usage_collector_last_success_timestamp{collector}`
- `homelab_usage_collector_errors_total{collector}`
- `homelab_usage_push_failures_total{topic}`

A null user renders as `user="none"`.

### Host paths

| What | Path |
|---|---|
| Live database | `/var/lib/usage-relay/usage.db` (`StateDirectory=usage-relay`) |
| Snapshot | `/opt/homelab/appdata/usage-relay/usage.snapshot.db` |
| Code | `/opt/usage-relay/usage_relay/` |
| Config | `/etc/usage-relay/config.json` |
| Keys | `/etc/usage-relay/keys.env` (0600 root) |
| Digest report | `/opt/homelab/appdata/scan-reports/usage.txt` |
| Metrics port | 9470 |

### Repo rules that apply to every task

- **Branch base.** Work on `feat/usage-relay`, cut from
  `origin/feat/household-guide`, **not** from `main`. The household-guide
  branch is deployed on svc-infra and svc-media but not merged. Deploying
  from a branch cut from `main` would roll it back.
- **Commits.** Never `git add -A`; stage explicit paths. Push after every
  commit.
- **Secrets.** Never echo a vault secret. Any Ansible task whose text or
  template references `vault_*` gets `no_log: true`.
- **Running Ansible on TERRA.**
  - Use `.venv/bin/ansible…`.
  - Use `--vault-password-file .vault_pass`, or `make … USE_VAULT_FILE=1`.
  - Always redirect stdin, stdout and stderr: `</dev/null >LOG 2>&1`.

---

## File Structure

**New: the relay package** (`roles/svc_infra/files/usage_relay/`)

| File | Responsibility |
|---|---|
| `__init__.py` | Package docstring only |
| `__main__.py` | `python3 -m usage_relay` entry point; calls `cli.main()` |
| `model.py` | `Event` and `Push` dataclasses, `KINDS`, `PUSH_STATES`, time and user helpers |
| `store.py` | `Store`, the SQLite event log: dedup insert, marks, pending, attribution and resume lookups, windowed reads, prune, snapshot |
| `fetch.py` | `get_json`, `FetchError`, `ShapeError`, `redact`, `snippet`, and the `Fetch` type |
| `collect_arr.py` | `ArrCollector` and `parse_history`, for Sonarr and Radarr |
| `collect_sabnzbd.py` | `SabnzbdCollector` and `parse_slots` |
| `collect_seerr.py` | `SeerrCollector` and `parse_requests` |
| `collect_jellyfin.py` | `JellyfinCollector` and `playing`, which diff sessions between polls |
| `metrics.py` | `CATALOG`, `metric_names()`, `Metrics` (counters, gauges, exposition) |
| `ntfy.py` | `Ntfy` (JSON publish, poll read-back) and `NtfyError` |
| `router.py` | `Router`: initial push state, topic, message text, batching, expiry |
| `relay.py` | `Relay`: ingest, attribution, collector loop, health and alerts, push cycle, selftest, maintenance |
| `digest.py` | `render()`, the weekly text |
| `config.py` | `Settings`, `from_dict`, `load`, `build_collectors` |
| `httpserver.py` | `RelayHTTPServer`: `/metrics`, `/health`, and `POST /selftest` (loopback only) |
| `cli.py` | `main()` plus `cmd_run`, `cmd_digest`, `cmd_selftest`, `cmd_check`, `assess`, `selftest_count` |

**New: tests**

- `tests/validate_usage_relay.py`
- `tests/usage_relay/helpers.py`
- `tests/usage_relay/test_model.py`, `test_store.py`, `test_fetch.py`
- `tests/usage_relay/test_collect_arr.py`, `test_collect_sabnzbd.py`,
  `test_collect_seerr.py`, `test_collect_jellyfin.py`
- `tests/usage_relay/test_metrics.py`, `test_ntfy.py`, `test_router.py`
- `tests/usage_relay/test_relay.py`, `test_digest.py`, `test_config.py`,
  `test_httpserver.py`, `test_cli.py`
- `tests/fixtures/usage_relay/`: `sonarr_history.json`, `radarr_history.json`,
  `sabnzbd_history.json`, `seerr_requests.json`,
  `jellyfin_sessions_idle.json`, `jellyfin_sessions_playing.json`

**New: deployment**

- `roles/svc_infra/tasks/usage-relay.yml`
- `roles/svc_infra/templates/usage-relay-config.json.j2`
- `roles/svc_infra/templates/usage-relay-keys.env.j2`
- `roles/svc_infra/files/usage-relay.service`
- `roles/svc_infra/files/homelab-usage-digest.service`
- `roles/svc_infra/files/homelab-usage-digest.timer`
- `roles/svc_infra/files/grafana-dashboards/homelab-usage.json`

**Modified**

| File | Change |
|---|---|
| `Makefile` | ruff path |
| `roles/svc_infra/tasks/main.yml` | import `usage-relay.yml` |
| `roles/svc_infra/tasks/verify.yml` | selftest and collector check |
| `roles/svc_infra/defaults/main.yml` | `usage_*` vars; `infra_extra_backup_paths` |
| `roles/svc_infra/templates/prometheus.yml.j2` | scrape job |
| `inventory/host_vars/svc-infra.yml` | `onfailure_units_extra` |
| `inventory/group_vars/all_vault.yml.example` | five keys |
| `tests/validate_grafana_dashboards.py` | emitter path, label names |
| `docs/services.md` | new section and one-time wiring |

---

### Task 0: Branch setup

**Files:** none changed. This task sets up git state only.

- [ ] **Step 1: Cut the branch from the live household-guide branch, and bring in the spec and this plan**

```bash
cd ~/dev/homelab-ironwood
git fetch origin
git status --porcelain            # must print nothing
git switch -c feat/usage-relay origin/feat/household-guide
git merge --no-edit origin/docs/usage-notifications-spec
git push -u origin feat/usage-relay
```

Expected: a merge commit. `docs/superpowers/specs/2026-10-06-usage-notifications-design.md`
and `docs/superpowers/plans/2026-10-06-usage-relay.md` both exist.

- [ ] **Step 2: Confirm the baseline validates**

Run: `make validate > /tmp/validate.log 2>&1 </dev/null; echo exit=$?; tail -5 /tmp/validate.log`

Expected: `exit=0`.

---

### Task 1: Test gate, `model.py`, and lint coverage

**Files:**
- Create: `tests/validate_usage_relay.py`
- Create: `tests/usage_relay/helpers.py`
- Create: `tests/usage_relay/test_model.py`
- Create: `roles/svc_infra/files/usage_relay/__init__.py`
- Create: `roles/svc_infra/files/usage_relay/model.py`
- Modify: `Makefile` (the `validate-python` recipe)

**Interfaces:**
- Produces:
  - `model.KINDS: frozenset[str]` and `model.PUSH_STATES: frozenset[str]`
  - `model.parse_ts(str) -> datetime` (aware, UTC)
  - `model.utc_iso(datetime) -> str`
  - `model.from_epoch(float) -> str`
  - `model.norm_user(object) -> str | None`
  - `model.Event(id, ts, service, kind, user, title, detail={})` (frozen)
  - `model.Push(topic, title, message, priority: int, event_ids: tuple[str, ...])` (frozen)
- Produces, for tests:
  - `helpers.FakeClock`, which is callable, has `.now` and `.advance(s)`, and
    starts at `1_791_201_600.0` (2026-10-05T12:00:00Z)
  - `helpers.FakeFetch(routes)`, which is callable as `(url, headers)`,
    matches the longest URL prefix and records `.calls`
  - `helpers.FakeNtfy`, with `.sent`, `.down`, `.publish(push)` and
    `.poll(topic, since)`
  - `helpers.fixture(name)`
  - `helpers.TOPICS`
  - `helpers.settings_dict(tmp)` and `helpers.make_settings(tmp, **overrides)`

- [ ] **Step 1: Write the gate**

Create `tests/validate_usage_relay.py`:

```python
#!/usr/bin/env python3
"""Run the usage relay's unit tests as a validation gate.

The relay (roles/svc_infra/files/usage_relay/) is a long-running Python service,
and most of what it does is a judgement that passes silently when wrong: a
duplicate counted twice, a resume pushed as a new play, a year of history
replayed on first deploy. The tests under tests/usage_relay/ pin those
judgements; this gate is what runs them.

Positive control: zero discovered tests fails the gate. A discovery pattern
that stopped matching would otherwise report OK having run nothing.

Pass a substring to run a subset while iterating:
    .venv/bin/python tests/validate_usage_relay.py jellyfin
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Python behaviour tests, grouped with homelab-metric-write's gate: scripts
# whose refusal paths are the point and which succeed every time when healthy.
#
# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "shell"

ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = ROOT / "tests/usage_relay"


def main() -> int:
    # No __pycache__ under roles/svc_infra/files/usage_relay/ or tests/: it is
    # gitignored, but it is clutter in a directory Ansible copies from.
    sys.dont_write_bytecode = True
    loader = unittest.TestLoader()
    if len(sys.argv) > 1:
        loader.testNamePatterns = [f"*{sys.argv[1]}*"]
    suite = loader.discover(str(SUITE_DIR), pattern="test_*.py", top_level_dir=str(SUITE_DIR))
    count = suite.countTestCases()
    if count == 0:
        print("Usage relay: discovered zero tests — discovery is broken, not the suite empty",
              file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        return 1
    print(f"Usage relay: OK ({count} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Write the shared test helpers**

Create `tests/usage_relay/helpers.py`:

```python
"""Shared scaffolding for the usage relay tests.

Importing this module puts roles/svc_infra/files on sys.path, which is how
every test module reaches the usage_relay package. Imports of usage_relay
inside this file are deferred into functions for the same reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = ROOT / "roles/svc_infra/files"
FIXTURES = ROOT / "tests/fixtures/usage_relay"

if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

TOPIC_KEYS = ("requests", "library", "playback", "failures", "digest", "selftest")
TOPICS = {key: f"usage-{key}" for key in TOPIC_KEYS}


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClock:
    """A settable time.time(). Starts at 2026-10-05T12:00:00Z."""

    def __init__(self, start: float = 1_791_201_600.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeFetch:
    """Answers by longest matching URL prefix and records every request."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> object:
        self.calls.append((url, dict(headers)))
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix):
                result = self.routes[prefix]
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected fetch {url}")


class FakeNtfy:
    """Records pushes; set .down to fail the way the real client does."""

    def __init__(self) -> None:
        self.sent: list = []
        self.down = False

    def publish(self, push) -> None:
        from usage_relay.ntfy import NtfyError

        if self.down:
            raise NtfyError("ntfy is down (test)")
        self.sent.append(push)

    def poll(self, topic: str, since: str = "5m") -> list[dict]:
        return [{"event": "message", "topic": p.topic, "message": p.message}
                for p in list(self.sent) if p.topic == topic]


def settings_dict(tmp: str) -> dict:
    def collector(name: str, interval: int) -> dict:
        return {"base_url": f"http://{name}.example", "interval": interval,
                "key_env": f"USAGE_{name.upper()}_API_KEY"}

    return {
        "db_path": f"{tmp}/usage.db",
        "snapshot_path": f"{tmp}/usage.snapshot.db",
        "report_path": f"{tmp}/usage.txt",
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "push_interval": 10,
        "maintenance_time": "02:30",
        "topics": dict(TOPICS),
        "batch_window": 120,
        "resume_window": 1800,
        "playback_expiry": 600,
        "collector_alert_after": 900,
        "retention_days": 365,
        "push_ignore_users": ["Brandon", "admin"],
        "collectors": {
            "jellyfin": collector("jellyfin", 30),
            "seerr": collector("seerr", 60),
            "sonarr": collector("sonarr", 60),
            "radarr": collector("radarr", 60),
            "sabnzbd": collector("sabnzbd", 60),
        },
    }


def make_settings(tmp: str, **overrides):
    from usage_relay.config import from_dict

    return from_dict({**settings_dict(tmp), **overrides})
```

- [ ] **Step 3: Write the failing model tests**

Create `tests/usage_relay/test_model.py`:

```python
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
```

- [ ] **Step 4: Run the tests and see them fail**

Run: `.venv/bin/python tests/validate_usage_relay.py model`

Expected: FAIL with `ModuleNotFoundError: No module named 'usage_relay'`, and
exit code 1.

- [ ] **Step 5: Write the package and the model**

Create `roles/svc_infra/files/usage_relay/__init__.py`:

```python
"""Household usage relay — see docs/superpowers/specs/2026-10-06-usage-notifications-design.md."""
```

Create `roles/svc_infra/files/usage_relay/model.py`:

```python
"""The one record every collector emits, plus the helpers that keep it comparable.

Every timestamp in the store is a string shaped 2026-10-05T12:00:00Z. The store
orders and windows events by comparing those strings, which is only correct if
every writer produced exactly that shape — so Event refuses any other, rather
than letting one collector's ".1234567+00:00" sort wrongly and silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

KINDS = frozenset({
    "request.created",
    "request.approved",
    "request.available",
    "download.grabbed",
    "download.completed",
    "download.imported",
    "download.failed",
    "playback.started",
    "playback.stopped",
    "selftest",
})

# "none" marks kinds that are recorded but never pushed.
PUSH_STATES = frozenset({"pending", "sent", "suppressed", "expired", "none"})

# .NET (Sonarr, Radarr) writes seven fractional digits; fromisoformat takes six.
_FRACTION = re.compile(r"(\.\d{1,6})\d*")


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp from any source. Naive means UTC."""
    text = _FRACTION.sub(r"\1", value.strip(), count=1)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_epoch(seconds: float) -> str:
    return utc_iso(datetime.fromtimestamp(seconds, UTC))


def norm_user(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    text = name.strip().lower()
    return text or None


@dataclass(frozen=True)
class Event:
    id: str
    ts: str
    service: str
    kind: str
    user: str | None
    title: str
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("event id must not be empty")
        if self.kind not in KINDS:
            raise ValueError(f"unknown event kind {self.kind!r}")
        if utc_iso(parse_ts(self.ts)) != self.ts:
            raise ValueError(f"timestamp {self.ts!r} is not normalised; pass it through utc_iso()")


@dataclass(frozen=True)
class Push:
    topic: str
    title: str
    message: str
    priority: int
    event_ids: tuple[str, ...]
```

- [ ] **Step 6: Run the tests and see them pass**

Run: `.venv/bin/python tests/validate_usage_relay.py model`

Expected: `Usage relay: OK (10 tests)`, exit 0.

- [ ] **Step 7: Lint the package and the tests**

In `Makefile`, change the `validate-python` recipe line

```make
	$(RUFF) check --no-cache tests scripts
```

to

```make
	$(RUFF) check --no-cache tests scripts roles/svc_infra/files/usage_relay
```

Run: `make validate-python validate-shell > /tmp/v.log 2>&1 </dev/null; echo exit=$?; tail -5 /tmp/v.log`

Expected: `exit=0`. The output includes `Usage relay: OK (10 tests)`.

- [ ] **Step 8: Commit**

```bash
git add Makefile tests/validate_usage_relay.py tests/usage_relay/helpers.py \
  tests/usage_relay/test_model.py roles/svc_infra/files/usage_relay/__init__.py \
  roles/svc_infra/files/usage_relay/model.py
git commit -m "feat: add the usage relay event model and its test gate"
git push
```

---

### Task 2: `store.py`, the SQLite event log

**Files:**
- Create: `roles/svc_infra/files/usage_relay/store.py`
- Test: `tests/usage_relay/test_store.py`

**Interfaces:**
- Consumes: `model.Event` and `model.PUSH_STATES`.
- Produces `Store(path: str)`. Every method is thread-safe.
  - `close()`
  - `insert(event, pushed: str) -> bool`: True only for a new row.
  - `get(event_id) -> Event | None` and `push_state(event_id) -> str | None`
  - `delete(event_id) -> None`
  - `get_mark(collector) -> str | None` and `set_mark(collector, value) -> None`
  - `pending() -> list[Event]`, ordered by `(ts, id)`
  - `set_pushed(event_ids: Iterable[str], state: str) -> None`
  - `requester_for(match_key) -> str | None`
  - `started_within(user, item_id, since, until, *, exclude_id) -> bool`
  - `events_between(start, end) -> list[Event]`: half-open `[start, end)`,
    excluding `selftest`
  - `prune(before) -> int`
  - `snapshot(dest) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/usage_relay/test_store.py`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.model import Event
from usage_relay.store import Store


def ev(event_id: str, kind: str = "download.imported", ts: str = "2026-10-05T12:00:00Z",
       user: str | None = None, **detail) -> Event:
    return Event(id=event_id, ts=ts, service="test", kind=kind, user=user, title=event_id, detail=detail)


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = Store(str(self.tmp / "usage.db"))
        self.addCleanup(self.store.close)

    def test_insert_reports_a_new_row_exactly_once(self):
        self.assertTrue(self.store.insert(ev("a"), "none"))
        self.assertFalse(self.store.insert(ev("a"), "none"))

    def test_insert_rejects_an_unknown_push_state(self):
        with self.assertRaises(ValueError):
            self.store.insert(ev("a"), "maybe")

    def test_round_trip_preserves_detail(self):
        self.store.insert(ev("a", series="S", season=2), "none")
        self.assertEqual(self.store.get("a").detail, {"series": "S", "season": 2})
        self.assertIsNone(self.store.get("missing"))

    def test_delete_removes_one_row(self):
        self.store.insert(ev("a"), "none")
        self.store.insert(ev("b"), "none")
        self.store.delete("a")
        self.assertIsNone(self.store.get("a"))
        self.assertIsNotNone(self.store.get("b"))

    def test_marks_round_trip_and_overwrite(self):
        self.assertIsNone(self.store.get_mark("sonarr"))
        self.store.set_mark("sonarr", "one")
        self.store.set_mark("sonarr", "two")
        self.assertEqual(self.store.get_mark("sonarr"), "two")

    def test_pending_is_ordered_and_set_pushed_clears_it(self):
        self.store.insert(ev("late", ts="2026-10-05T12:05:00Z"), "pending")
        self.store.insert(ev("early", ts="2026-10-05T12:01:00Z"), "pending")
        self.store.insert(ev("quiet"), "none")
        self.assertEqual([e.id for e in self.store.pending()], ["early", "late"])
        self.store.set_pushed(["early"], "sent")
        self.assertEqual([e.id for e in self.store.pending()], ["late"])
        self.assertEqual(self.store.push_state("early"), "sent")
        with self.assertRaises(ValueError):
            self.store.set_pushed(["late"], "maybe")

    def test_requester_is_the_latest_matching_request(self):
        self.store.insert(ev("r1", "request.created", "2026-10-01T00:00:00Z", "alice", match_key="tmdb:1"), "none")
        self.store.insert(ev("r2", "request.created", "2026-10-02T00:00:00Z", "bob", match_key="tmdb:1"), "none")
        self.store.insert(ev("r3", "request.created", user="carol", match_key="tmdb:2"), "none")
        self.assertEqual(self.store.requester_for("tmdb:1"), "bob")
        self.assertIsNone(self.store.requester_for("tmdb:404"))

    def test_started_within_finds_an_earlier_start_of_the_same_item(self):
        self.store.insert(ev("p1", "playback.started", "2026-10-05T12:00:00Z", "alice", item_id="i1"), "pending")
        window = ("2026-10-05T11:50:00Z", "2026-10-05T12:20:00Z")
        self.assertTrue(self.store.started_within("alice", "i1", *window, exclude_id="p2"))
        self.assertFalse(self.store.started_within("alice", "i1", *window, exclude_id="p1"))
        self.assertFalse(self.store.started_within("alice", "i2", *window, exclude_id="p2"))
        self.assertFalse(self.store.started_within("bob", "i1", *window, exclude_id="p2"))

    def test_events_between_is_half_open_and_skips_selftest(self):
        self.store.insert(ev("before", ts="2026-09-27T23:59:59Z"), "none")
        self.store.insert(ev("start", ts="2026-09-28T00:00:00Z"), "none")
        self.store.insert(ev("probe", "selftest", "2026-09-29T00:00:00Z"), "pending")
        self.store.insert(ev("end", ts="2026-10-05T00:00:00Z"), "none")
        got = self.store.events_between("2026-09-28T00:00:00Z", "2026-10-05T00:00:00Z")
        self.assertEqual([e.id for e in got], ["start"])

    def test_prune_deletes_only_older_rows(self):
        self.store.insert(ev("old", ts="2025-01-01T00:00:00Z"), "none")
        self.store.insert(ev("new"), "none")
        self.assertEqual(self.store.prune("2026-01-01T00:00:00Z"), 1)
        self.assertIsNone(self.store.get("old"))
        self.assertIsNotNone(self.store.get("new"))

    def test_snapshot_is_a_complete_readable_copy(self):
        self.store.insert(ev("a"), "none")
        dest = self.tmp / "snap.db"
        self.store.snapshot(str(dest))
        self.store.snapshot(str(dest))  # a second night replaces it rather than failing
        conn = sqlite3.connect(dest)
        try:
            self.assertEqual(conn.execute("SELECT id FROM events").fetchall(), [("a",)])
        finally:
            conn.close()
        self.assertFalse(Path(f"{dest}.tmp").exists())
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `.venv/bin/python tests/validate_usage_relay.py store`

Expected: FAIL with `No module named 'usage_relay.store'`.

- [ ] **Step 3: Write the store**

Create `roles/svc_infra/files/usage_relay/store.py`:

```python
"""SQLite event log: the single source the pushes, counters and digest all read.

Dedup is the primary key. INSERT OR IGNORE makes overlapping poll windows,
restart catch-up and re-read pages harmless, and insert() reports whether the
row was new so the caller counts each event exactly once.

One connection, shared across the relay's threads behind a lock. WAL mode lets
the digest and selftest commands read the same file from their own processes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable

from .model import PUSH_STATES, Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      TEXT PRIMARY KEY,
    ts      TEXT NOT NULL,
    service TEXT NOT NULL,
    kind    TEXT NOT NULL,
    user    TEXT,
    title   TEXT NOT NULL,
    detail  TEXT NOT NULL,
    pushed  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_pushed ON events (pushed);
CREATE TABLE IF NOT EXISTS marks (
    collector TEXT PRIMARY KEY,
    value     TEXT NOT NULL
);
"""

_COLUMNS = "id, ts, service, kind, user, title, detail"


def _event(row: tuple) -> Event:
    return Event(id=row[0], ts=row[1], service=row[2], kind=row[3], user=row[4],
                 title=row[5], detail=json.loads(row[6]))


def _check_state(state: str) -> None:
    if state not in PUSH_STATES:
        raise ValueError(f"unknown push state {state!r}")


class Store:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, timeout=30, isolation_level=None,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def insert(self, event: Event, pushed: str) -> bool:
        _check_state(pushed)
        return self._write(
            "INSERT OR IGNORE INTO events (id, ts, service, kind, user, title, detail, pushed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event.id, event.ts, event.service, event.kind, event.user, event.title,
             json.dumps(event.detail, sort_keys=True), pushed),
        ) == 1

    def get(self, event_id: str) -> Event | None:
        rows = self._rows(f"SELECT {_COLUMNS} FROM events WHERE id = ?", (event_id,))
        return _event(rows[0]) if rows else None

    def push_state(self, event_id: str) -> str | None:
        rows = self._rows("SELECT pushed FROM events WHERE id = ?", (event_id,))
        return rows[0][0] if rows else None

    def delete(self, event_id: str) -> None:
        self._write("DELETE FROM events WHERE id = ?", (event_id,))

    def get_mark(self, collector: str) -> str | None:
        rows = self._rows("SELECT value FROM marks WHERE collector = ?", (collector,))
        return rows[0][0] if rows else None

    def set_mark(self, collector: str, value: str) -> None:
        self._write(
            "INSERT INTO marks (collector, value) VALUES (?, ?)"
            " ON CONFLICT(collector) DO UPDATE SET value = excluded.value",
            (collector, value),
        )

    def pending(self) -> list[Event]:
        rows = self._rows(f"SELECT {_COLUMNS} FROM events WHERE pushed = 'pending' ORDER BY ts, id")
        return [_event(row) for row in rows]

    def set_pushed(self, event_ids: Iterable[str], state: str) -> None:
        _check_state(state)
        with self._lock:
            self._conn.executemany("UPDATE events SET pushed = ? WHERE id = ?",
                                   [(state, event_id) for event_id in event_ids])

    def requester_for(self, match_key: str) -> str | None:
        rows = self._rows(
            "SELECT user FROM events WHERE kind = 'request.created' AND user IS NOT NULL"
            " AND json_extract(detail, '$.match_key') = ? ORDER BY ts DESC LIMIT 1",
            (match_key,),
        )
        return rows[0][0] if rows else None

    def started_within(self, user: str, item_id: str, since: str, until: str, *,
                       exclude_id: str) -> bool:
        return bool(self._rows(
            "SELECT 1 FROM events WHERE kind = 'playback.started' AND user = ?"
            " AND json_extract(detail, '$.item_id') = ? AND ts >= ? AND ts <= ? AND id != ?"
            " LIMIT 1",
            (user, item_id, since, until, exclude_id),
        ))

    def events_between(self, start: str, end: str) -> list[Event]:
        rows = self._rows(
            f"SELECT {_COLUMNS} FROM events WHERE ts >= ? AND ts < ? AND kind != 'selftest'"
            " ORDER BY ts, id",
            (start, end),
        )
        return [_event(row) for row in rows]

    def prune(self, before: str) -> int:
        return self._write("DELETE FROM events WHERE ts < ?", (before,))

    def snapshot(self, dest: str) -> None:
        """Write a consistent copy for the backup to tar. Atomic: tmp then rename."""
        tmp = f"{dest}.tmp"
        if os.path.exists(tmp):
            os.unlink(tmp)
        with self._lock:
            self._conn.execute("VACUUM INTO ?", (tmp,))
        os.replace(tmp, dest)
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `.venv/bin/python tests/validate_usage_relay.py store`

Expected: `Usage relay: OK (11 tests)`.

- [ ] **Step 5: Commit**

```bash
git add roles/svc_infra/files/usage_relay/store.py tests/usage_relay/test_store.py
git commit -m "feat: add the usage relay's SQLite event log"
git push
```

---

### Task 3: `fetch.py` and the Sonarr/Radarr collector

**Files:**
- Create: `roles/svc_infra/files/usage_relay/fetch.py`
- Create: `roles/svc_infra/files/usage_relay/collect_arr.py`
- Create: `tests/fixtures/usage_relay/sonarr_history.json`
- Create: `tests/fixtures/usage_relay/radarr_history.json`
- Test: `tests/usage_relay/test_fetch.py`, `tests/usage_relay/test_collect_arr.py`

**Interfaces:**
- Consumes: `model.Event`, `from_epoch`, `parse_ts` and `utc_iso`.
- Produces from `fetch.py`:
  - `Fetch = Callable[[str, dict[str, str]], object]`
  - `get_json(url, headers, timeout=15.0) -> object`
  - `FetchError` and `ShapeError`
  - `redact(url) -> str` and `snippet(value) -> str`
- Produces from `collect_arr.py`:
  - `parse_history(service, records) -> tuple[list[Event], str | None]`
  - `ArrCollector(*, service, base_url, api_key, fetch, interval, clock=time.time)`
    with `.name`, `.interval` and `.poll(mark) -> (events, mark)`
- **The collector contract**, shared by every collector in Tasks 3 to 6:
  - Each has `.name: str` and `.interval: int`, and implements
    `.poll(mark: str | None) -> tuple[list[Event], str]`.
  - `mark=None` means first run. The collector returns `([], baseline_mark)`
    and never emits history.
  - Errors are raised as `FetchError` or `ShapeError` only.

- [ ] **Step 1: Write the fixtures**

Create `tests/fixtures/usage_relay/sonarr_history.json`:

```json
[
  {"id": 101, "eventType": "grabbed", "date": "2026-10-05T11:58:00.1234567Z", "seriesId": 7, "episodeId": 70,
   "sourceTitle": "The.Example.Show.S02E03.1080p.WEB",
   "series": {"title": "The Example Show", "tvdbId": 4001},
   "episode": {"seasonNumber": 2, "episodeNumber": 3, "title": "Episode Three"}},
  {"id": 102, "eventType": "downloadFolderImported", "date": "2026-10-05T12:01:00Z", "seriesId": 7, "episodeId": 70,
   "sourceTitle": "The.Example.Show.S02E03.1080p.WEB",
   "series": {"title": "The Example Show", "tvdbId": 4001},
   "episode": {"seasonNumber": 2, "episodeNumber": 3, "title": "Episode Three"}},
  {"id": 103, "eventType": "episodeFileRenamed", "date": "2026-10-05T12:02:00Z", "seriesId": 7, "episodeId": 70,
   "sourceTitle": "The.Example.Show.S02E03.1080p.WEB",
   "series": {"title": "The Example Show", "tvdbId": 4001},
   "episode": {"seasonNumber": 2, "episodeNumber": 3, "title": "Episode Three"}},
  {"id": 104, "eventType": "downloadFailed", "date": "2026-10-05T12:03:00Z", "seriesId": 7, "episodeId": 71,
   "sourceTitle": "The.Example.Show.S02E04.1080p.WEB",
   "data": {"message": "Download client reported failure"},
   "series": {"title": "The Example Show", "tvdbId": 4001},
   "episode": {"seasonNumber": 2, "episodeNumber": 4, "title": "Episode Four"}}
]
```

Create `tests/fixtures/usage_relay/radarr_history.json`:

```json
[
  {"id": 55, "eventType": "downloadFolderImported", "date": "2026-10-05T12:05:00Z", "movieId": 3,
   "sourceTitle": "Example.Movie.2024.1080p.WEB",
   "movie": {"title": "Example Movie", "year": 2024, "tmdbId": 9001}}
]
```

- [ ] **Step 2: Write the failing fetch tests**

Create `tests/usage_relay/test_fetch.py`:

```python
from __future__ import annotations

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.fetch import FetchError, get_json, redact, snippet


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen_headers = dict(self.headers)
        if self.path.startswith("/ok"):
            code, body = 200, b'{"answer": 42}'
        elif self.path.startswith("/denied"):
            code, body = 401, b"{}"
        else:
            code, body = 200, b"<html>not json</html>"
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class GetJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_json_is_returned_and_headers_are_sent(self):
        self.assertEqual(get_json(f"{self.base}/ok", {"X-Api-Key": "example-api-key"}), {"answer": 42})
        self.assertEqual(self.server.seen_headers.get("X-Api-Key"), "example-api-key")

    def test_an_http_error_names_the_status_and_hides_the_key(self):
        with self.assertRaises(FetchError) as caught:
            get_json(f"{self.base}/denied?apikey=example-secret-value", {})
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn("example-secret-value", str(caught.exception))

    def test_a_non_json_body_is_a_fetch_error(self):
        with self.assertRaises(FetchError) as caught:
            get_json(f"{self.base}/html", {})
        self.assertIn("non-JSON", str(caught.exception))

    def test_an_unreachable_host_is_a_fetch_error(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(FetchError):
            get_json(f"http://127.0.0.1:{port}/", {}, timeout=2)


class HelperTests(unittest.TestCase):
    def test_redact_hides_key_parameters(self):
        self.assertEqual(redact("http://x/api?mode=history&apikey=abc&limit=5"),
                         "http://x/api?mode=history&apikey=REDACTED&limit=5")

    def test_snippet_is_bounded(self):
        self.assertEqual(len(snippet("x" * 5000)), 2048)
```

- [ ] **Step 3: Write the failing Arr tests**

Create `tests/usage_relay/test_collect_arr.py`:

```python
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
```

- [ ] **Step 4: Run the tests and see them fail**

Run: `.venv/bin/python tests/validate_usage_relay.py fetch; .venv/bin/python tests/validate_usage_relay.py arr`

Expected: both FAIL with `No module named 'usage_relay.fetch'`.

- [ ] **Step 5: Write `fetch.py`**

Create `roles/svc_infra/files/usage_relay/fetch.py`:

```python
"""HTTP JSON fetching, with credentials kept out of every error message.

Two error types, because the relay treats them alike but a reader should not:
FetchError means the source could not be read at all; ShapeError means it
answered with something its collector does not understand. Both count against
the collector and both keep its high-water mark where it was.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable

Fetch = Callable[[str, dict[str, str]], object]

SNIPPET_CHARS = 2048
_KEY_PARAM = re.compile(r"(?i)\b(apikey|api_key|token)=[^&]*")


class FetchError(Exception):
    """A source could not be read: unreachable, non-2xx, or not JSON."""


class ShapeError(Exception):
    """A source answered, but not in the shape its collector parses."""


def redact(url: str) -> str:
    return _KEY_PARAM.sub(lambda match: f"{match.group(1)}=REDACTED", url)


def snippet(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:SNIPPET_CHARS]


def get_json(url: str, headers: dict[str, str], timeout: float = 15.0) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} from {redact(url)}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"{type(exc).__name__} reaching {redact(url)}") from None
    try:
        return json.loads(body)
    except ValueError:
        text = body.decode("utf-8", "replace")
        raise FetchError(f"non-JSON reply from {redact(url)}: {snippet(text)}") from None
```

- [ ] **Step 6: Write `collect_arr.py`**

Create `roles/svc_infra/files/usage_relay/collect_arr.py`:

```python
"""Sonarr and Radarr history. One parser: their v3 history APIs share a shape.

Only three event types are usage. Renames, deletions and ignored downloads are
housekeeping and are skipped — but they still advance the mark, so a page full
of renames is not re-read forever.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import quote

from .fetch import Fetch, ShapeError, snippet
from .model import Event, from_epoch, parse_ts, utc_iso

EVENT_KINDS = {
    "grabbed": "download.grabbed",
    "downloadFolderImported": "download.imported",
    "downloadFailed": "download.failed",
}


def _describe(service: str, record: dict) -> tuple[str, dict]:
    fallback = record.get("sourceTitle") or "unknown title"
    if service == "sonarr":
        series = record.get("series") or {}
        episode = record.get("episode") or {}
        name = series.get("title") or fallback
        season = episode.get("seasonNumber")
        number = episode.get("episodeNumber")
        if isinstance(season, int) and isinstance(number, int):
            title = f"{name} S{season:02d}E{number:02d}"
        else:
            title = name
        detail: dict = {"series": name, "season": season}
        if record.get("seriesId") is not None:
            detail["series_key"] = f"sonarr:series:{record['seriesId']}"
        if series.get("tvdbId"):
            detail["match_key"] = f"tvdb:{series['tvdbId']}"
        return title, detail
    movie = record.get("movie") or {}
    name = movie.get("title") or fallback
    year = movie.get("year")
    detail = {}
    if movie.get("tmdbId"):
        detail["match_key"] = f"tmdb:{movie['tmdbId']}"
    return (f"{name} ({year})" if year else name), detail


def parse_history(service: str, records: object) -> tuple[list[Event], str | None]:
    """Events for the usage event types, plus the newest record time seen."""
    if not isinstance(records, list):
        raise ShapeError(f"{service} history is not a list: {snippet(records)}")
    events: list[Event] = []
    latest: str | None = None
    for record in records:
        try:
            record_id = record["id"]
            event_type = record["eventType"]
            ts = utc_iso(parse_ts(record["date"]))
        except (KeyError, TypeError, ValueError):
            raise ShapeError(f"{service} history record lacks id/eventType/date: {snippet(record)}") from None
        if latest is None or ts > latest:
            latest = ts
        kind = EVENT_KINDS.get(event_type)
        if kind is None:
            continue
        title, detail = _describe(service, record)
        if kind == "download.failed":
            detail["reason"] = (record.get("data") or {}).get("message") or "no reason given"
        events.append(Event(id=f"{service}:history:{record_id}", ts=ts, service=service,
                            kind=kind, user=None, title=title, detail=detail))
    return events, latest


class ArrCollector:
    def __init__(self, *, service: str, base_url: str, api_key: str, fetch: Fetch,
                 interval: int, clock: Callable[[], float] = time.time) -> None:
        if service not in ("sonarr", "radarr"):
            raise ValueError(f"ArrCollector serves sonarr or radarr, not {service!r}")
        self.name = service
        self.interval = interval
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._fetch = fetch
        self._clock = clock

    def poll(self, mark: str | None) -> tuple[list[Event], str]:
        if mark is None:
            return [], from_epoch(self._clock())
        include = ("includeSeries=true&includeEpisode=true" if self.name == "sonarr"
                   else "includeMovie=true")
        url = f"{self._base}/api/v3/history/since?date={quote(mark)}&{include}"
        events, latest = parse_history(self.name, self._fetch(url, {"X-Api-Key": self._key}))
        return events, (max(mark, latest) if latest else mark)
```

- [ ] **Step 7: Run the tests and see them pass**

Run: `.venv/bin/python tests/validate_usage_relay.py fetch && .venv/bin/python tests/validate_usage_relay.py arr`

Expected: `OK (6 tests)` and then `OK (10 tests)`.

- [ ] **Step 8: Commit**

```bash
git add roles/svc_infra/files/usage_relay/fetch.py roles/svc_infra/files/usage_relay/collect_arr.py \
  tests/usage_relay/test_fetch.py tests/usage_relay/test_collect_arr.py \
  tests/fixtures/usage_relay/sonarr_history.json tests/fixtures/usage_relay/radarr_history.json
git commit -m "feat: collect Sonarr and Radarr history for the usage relay"
git push
```

### Task 4: The SABnzbd collector

**Files:**
- Create: `roles/svc_infra/files/usage_relay/collect_sabnzbd.py`
- Create: `tests/fixtures/usage_relay/sabnzbd_history.json`
- Test: `tests/usage_relay/test_collect_sabnzbd.py`

**Interfaces:**
- Consumes: `fetch.Fetch`, `ShapeError` and `snippet`; `model.Event` and `from_epoch`.
- Produces:
  - `parse_slots(data) -> list[dict]`
  - `SabnzbdCollector(*, base_url, api_key, fetch, interval, clock=time.time)`
    with `name = "sabnzbd"`
  - The mark is the newest finished job's `completed` epoch, as a string.

- [ ] **Step 1: Write the fixture**

Create `tests/fixtures/usage_relay/sabnzbd_history.json`. Times are 12:01:00Z
and 12:02:00Z on 2026-10-05.

```json
{"history": {"slots": [
  {"nzo_id": "SABnzbd_nzo_a1", "name": "The.Example.Show.S02E03.1080p.WEB", "status": "Completed",
   "completed": 1791201660, "category": "tv", "fail_message": ""},
  {"nzo_id": "SABnzbd_nzo_a2", "name": "Example.Movie.2024.1080p.WEB", "status": "Failed",
   "completed": 1791201720, "category": "movies", "fail_message": "Aborted, cannot be completed"},
  {"nzo_id": "SABnzbd_nzo_a3", "name": "Still.Unpacking", "status": "Extracting",
   "completed": 0, "category": "tv", "fail_message": ""}
]}}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/usage_relay/test_collect_sabnzbd.py`:

```python
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
```

- [ ] **Step 3: Run the tests and see them fail**

Run: `.venv/bin/python tests/validate_usage_relay.py sabnzbd`

Expected: FAIL with `No module named 'usage_relay.collect_sabnzbd'`.

- [ ] **Step 4: Write the collector**

Create `roles/svc_infra/files/usage_relay/collect_sabnzbd.py`:

```python
"""SABnzbd job history: completed and failed downloads.

Jobs still post-processing (Extracting, Verifying, ...) sit in history with
completed=0. They are skipped and do not move the mark, so they are picked up
on the poll after they finish.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import quote

from .fetch import Fetch, ShapeError, snippet
from .model import Event, from_epoch

FINISHED = ("Completed", "Failed")


def parse_slots(data: object) -> list[dict]:
    try:
        slots = data["history"]["slots"]
    except (KeyError, TypeError):
        raise ShapeError(f"sabnzbd history has no history.slots: {snippet(data)}") from None
    if not isinstance(slots, list):
        raise ShapeError(f"sabnzbd history.slots is not a list: {snippet(slots)}")
    parsed = []
    for slot in slots:
        try:
            parsed.append({
                "nzo_id": str(slot["nzo_id"]),
                "name": str(slot["name"]),
                "status": str(slot["status"]),
                "completed": int(slot["completed"]),
                "category": slot.get("category"),
                "fail_message": slot.get("fail_message") or "",
            })
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ShapeError(f"sabnzbd history slot is malformed: {snippet(slot)}") from None
    return parsed


def _event(slot: dict) -> Event:
    failed = slot["status"] == "Failed"
    detail: dict = {"category": slot["category"]}
    if failed:
        detail["reason"] = slot["fail_message"] or "no reason given"
    return Event(id=f"sabnzbd:{slot['nzo_id']}", ts=from_epoch(slot["completed"]),
                 service="sabnzbd", kind="download.failed" if failed else "download.completed",
                 user=None, title=slot["name"], detail=detail)


class SabnzbdCollector:
    name = "sabnzbd"

    def __init__(self, *, base_url: str, api_key: str, fetch: Fetch, interval: int,
                 clock: Callable[[], float] = time.time) -> None:
        self.interval = interval
        self._url = (f"{base_url.rstrip('/')}/api?mode=history&output=json&limit=50"
                     f"&apikey={quote(api_key)}")
        self._fetch = fetch
        self._clock = clock

    def poll(self, mark: str | None) -> tuple[list[Event], str]:
        finished = [s for s in parse_slots(self._fetch(self._url, {})) if s["status"] in FINISHED]
        newest = max((s["completed"] for s in finished), default=None)
        if mark is None:
            return [], str(newest if newest is not None else int(self._clock()))
        floor = int(mark)
        events = [_event(s) for s in finished if s["completed"] > floor]
        return events, str(max(floor, newest if newest is not None else floor))
```

- [ ] **Step 5: Run the tests and see them pass**

Run: `.venv/bin/python tests/validate_usage_relay.py sabnzbd`

Expected: `Usage relay: OK (5 tests)`.

- [ ] **Step 6: Commit**

```bash
git add roles/svc_infra/files/usage_relay/collect_sabnzbd.py tests/usage_relay/test_collect_sabnzbd.py \
  tests/fixtures/usage_relay/sabnzbd_history.json
git commit -m "feat: collect SABnzbd job history for the usage relay"
git push
```

---

### Task 5: The Seerr collector

**Files:**
- Create: `roles/svc_infra/files/usage_relay/collect_seerr.py`
- Create: `tests/fixtures/usage_relay/seerr_requests.json`
- Test: `tests/usage_relay/test_collect_seerr.py`

**Interfaces:**
- Consumes: the same imports as Task 4, plus `model.norm_user`, `parse_ts`
  and `utc_iso`.
- Produces:
  - `parse_requests(data) -> list[dict]`
  - `SeerrCollector(*, base_url, api_key, fetch, interval, clock=time.time)`
    with `name = "seerr"`
- **Event ids:** `seerr:request:<id>:created`, `:approved` and `:available`.
- **Detail fields:** `media_type`, `request_id` (int), `seasons`, and
  `match_key` (`tmdb:<id>` for movies, `tvdb:<id>` for TV).
- **The mark** is the newest `updatedAt` seen.

- [ ] **Step 1: Write the fixture**

Create `tests/fixtures/usage_relay/seerr_requests.json`:

```json
{"pageInfo": {"pages": 1, "pageSize": 50, "results": 3, "page": 1},
 "results": [
  {"id": 11, "status": 1, "type": "movie",
   "createdAt": "2026-10-05T12:10:00.000Z", "updatedAt": "2026-10-05T12:10:00.000Z",
   "media": {"tmdbId": 9001, "tvdbId": null, "status": 2},
   "requestedBy": {"displayName": "Alice", "jellyfinUsername": "Alice"}, "seasons": []},
  {"id": 12, "status": 2, "type": "tv",
   "createdAt": "2026-10-05T12:11:00.000Z", "updatedAt": "2026-10-05T12:12:00.000Z",
   "media": {"tmdbId": 7001, "tvdbId": 4001, "status": 5},
   "requestedBy": {"displayName": "Bob", "jellyfinUsername": "bob"}, "seasons": [{"seasonNumber": 2}]},
  {"id": 3, "status": 2, "type": "movie",
   "createdAt": "2026-01-01T09:00:00.000Z", "updatedAt": "2026-10-05T12:13:00.000Z",
   "media": {"tmdbId": 9002, "tvdbId": null, "status": 5},
   "requestedBy": {"displayName": "Alice", "jellyfinUsername": "alice"}, "seasons": []}
 ]}
```

- [ ] **Step 2: Write the failing tests**

Create `tests/usage_relay/test_collect_seerr.py`:

```python
from __future__ import annotations

import unittest

from helpers import FakeClock, FakeFetch, fixture
from usage_relay.collect_seerr import SeerrCollector, parse_requests
from usage_relay.fetch import ShapeError

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
```

- [ ] **Step 3: Run the tests and see them fail**

Run: `.venv/bin/python tests/validate_usage_relay.py seerr`

Expected: FAIL with `No module named 'usage_relay.collect_seerr'`.

- [ ] **Step 4: Write the collector**

Create `roles/svc_infra/files/usage_relay/collect_seerr.py`:

```python
"""Seerr requests: who asked for what, and when it was approved or available.

The request list is sorted by modification time, and anything modified since
the mark is re-examined. Re-emitting an event already recorded is harmless (the
store dedups on id); announcing an OLD request as new is not, so `created` is
only emitted when the request itself is newer than the mark.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .fetch import Fetch, ShapeError, snippet
from .model import Event, from_epoch, norm_user, parse_ts, utc_iso

REQUEST_APPROVED = 2
MEDIA_AVAILABLE = 5
PAGE = "/api/v1/request?take=50&skip=0&sort=modified&filter=all"


def parse_requests(data: object) -> list[dict]:
    try:
        results = data["results"]
    except (KeyError, TypeError):
        raise ShapeError(f"seerr request list has no results: {snippet(data)}") from None
    if not isinstance(results, list):
        raise ShapeError(f"seerr results is not a list: {snippet(results)}")
    parsed = []
    for raw in results:
        try:
            media = raw.get("media") or {}
            who = raw.get("requestedBy") or {}
            parsed.append({
                "id": int(raw["id"]),
                "status": int(raw["status"]),
                "type": str(raw["type"]),
                "created": utc_iso(parse_ts(raw["createdAt"])),
                "updated": utc_iso(parse_ts(raw["updatedAt"])),
                "tmdb": media.get("tmdbId"),
                "tvdb": media.get("tvdbId"),
                "media_status": media.get("status"),
                "user": norm_user(who.get("jellyfinUsername") or who.get("username")
                                  or who.get("displayName")),
                "seasons": [s.get("seasonNumber") for s in raw.get("seasons") or []],
            })
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ShapeError(f"seerr request is malformed: {snippet(raw)}") from None
    return parsed


class SeerrCollector:
    name = "seerr"

    def __init__(self, *, base_url: str, api_key: str, fetch: Fetch, interval: int,
                 clock: Callable[[], float] = time.time) -> None:
        self.interval = interval
        self._base = base_url.rstrip("/")
        self._headers = {"X-Api-Key": api_key}
        self._fetch = fetch
        self._clock = clock
        self._titles: dict[tuple[str, int], str] = {}

    def poll(self, mark: str | None) -> tuple[list[Event], str]:
        requests = parse_requests(self._fetch(self._base + PAGE, self._headers))
        latest = max((r["updated"] for r in requests), default=None)
        if mark is None:
            return [], latest or from_epoch(self._clock())
        events: list[Event] = []
        for request in requests:
            if request["updated"] < mark:
                continue
            events.extend(self._events_for(request, mark))
        return events, (max(mark, latest) if latest else mark)

    def _title(self, media_type: str, tmdb: object) -> str:
        if tmdb is None:
            return "unknown title"
        key = (media_type, int(tmdb))
        if key not in self._titles:
            path, name_key = ("movie", "title") if media_type == "movie" else ("tv", "name")
            data = self._fetch(f"{self._base}/api/v1/{path}/{int(tmdb)}", self._headers)
            try:
                self._titles[key] = str(data[name_key])
            except (KeyError, TypeError):
                raise ShapeError(f"seerr {path} {tmdb} has no {name_key}: {snippet(data)}") from None
        return self._titles[key]

    def _events_for(self, request: dict, mark: str) -> list[Event]:
        title = self._title(request["type"], request["tmdb"])
        detail: dict = {"media_type": request["type"], "request_id": request["id"],
                        "seasons": request["seasons"]}
        if request["type"] == "movie" and request["tmdb"]:
            detail["match_key"] = f"tmdb:{request['tmdb']}"
        elif request["type"] == "tv" and request["tvdb"]:
            detail["match_key"] = f"tvdb:{request['tvdb']}"
        base = f"seerr:request:{request['id']}"

        def make(suffix: str, kind: str, ts: str) -> Event:
            return Event(id=f"{base}:{suffix}", ts=ts, service="seerr", kind=kind,
                         user=request["user"], title=title, detail=dict(detail))

        events = []
        if request["created"] >= mark:
            events.append(make("created", "request.created", request["created"]))
        if request["status"] == REQUEST_APPROVED:
            events.append(make("approved", "request.approved", request["updated"]))
        if request["media_status"] == MEDIA_AVAILABLE:
            events.append(make("available", "request.available", request["updated"]))
        return events
```

- [ ] **Step 5: Run the tests and see them pass**

Run: `.venv/bin/python tests/validate_usage_relay.py seerr`

Expected: `Usage relay: OK (7 tests)`.

- [ ] **Step 6: Commit**

```bash
git add roles/svc_infra/files/usage_relay/collect_seerr.py tests/usage_relay/test_collect_seerr.py \
  tests/fixtures/usage_relay/seerr_requests.json
git commit -m "feat: collect Seerr requests for the usage relay"
git push
```

---

### Task 6: The Jellyfin collector (session diffing)

**Files:**
- Create: `roles/svc_infra/files/usage_relay/collect_jellyfin.py`
- Create: `tests/fixtures/usage_relay/jellyfin_sessions_idle.json`
- Create: `tests/fixtures/usage_relay/jellyfin_sessions_playing.json`
- Test: `tests/usage_relay/test_collect_jellyfin.py`

**Interfaces:**
- Consumes: the same imports as Task 4, plus `model.norm_user`.
- Produces:
  - `playing(sessions) -> dict[str, dict]`, keyed by `"<sessionId>:<itemId>"`
  - `JellyfinCollector(*, base_url, api_key, fetch, interval, clock=time.time)`
    with `name = "jellyfin"`
  - `.active: list[tuple[str | None, str]]`, the `(user, mode)` of each
    stream at the last poll. `Relay` reads it for the
    `homelab_usage_playback_active` gauge.
- **Event ids:**
  - started: `jellyfin:session:<key>:<first_seen>`
  - stopped: the same id plus `:stopped`
- **Detail fields:**
  - started: `item_id`, `series`, `media_type`, `device`, `client` and
    `mode`, where mode is `transcode` or `direct`
  - stopped: the same, plus `watched_seconds`

- [ ] **Step 1: Write the fixtures**

Create `tests/fixtures/usage_relay/jellyfin_sessions_idle.json`:

```json
[
  {"Id": "sess-tv", "UserName": "Alice", "DeviceName": "Living Room TV", "Client": "Android TV"}
]
```

Create `tests/fixtures/usage_relay/jellyfin_sessions_playing.json`. A
`PositionTicks` of 6000000000 is 600 s.

```json
[
  {"Id": "sess-tv", "UserName": "Alice", "DeviceName": "Living Room TV", "Client": "Android TV",
   "NowPlayingItem": {"Id": "item-ep3", "Name": "Episode Three", "Type": "Episode",
                      "SeriesName": "The Example Show", "ParentIndexNumber": 2, "IndexNumber": 3},
   "PlayState": {"PositionTicks": 6000000000, "PlayMethod": "Transcode", "IsPaused": false}},
  {"Id": "sess-phone", "UserName": "Bob", "DeviceName": "Pixel", "Client": "Jellyfin Android",
   "NowPlayingItem": {"Id": "item-movie", "Name": "Example Movie", "Type": "Movie", "ProductionYear": 2024},
   "PlayState": {"PositionTicks": 0, "PlayMethod": "DirectPlay", "IsPaused": false}}
]
```

- [ ] **Step 2: Write the failing tests**

Create `tests/usage_relay/test_collect_jellyfin.py`:

```python
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
```

- [ ] **Step 3: Run the tests and see them fail**

Run: `.venv/bin/python tests/validate_usage_relay.py jellyfin`

Expected: FAIL with `No module named 'usage_relay.collect_jellyfin'`.

- [ ] **Step 4: Write the collector**

Create `roles/svc_infra/files/usage_relay/collect_jellyfin.py`:

```python
"""Jellyfin playback, inferred by diffing /Sessions between polls.

Jellyfin has no playback-history endpoint. A start is a (session, item) pair
that was not playing at the previous poll; a stop is one that has gone. The
mark carries the previous poll's playing set, so a relay restart diffs against
what was playing when it stopped rather than replaying anything.

Watched time is position travelled (last position - first position seen), not
wall-clock: pauses do not count, and resuming mid-episode does not count the
part watched last week.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from .fetch import Fetch, ShapeError, snippet
from .model import Event, from_epoch, norm_user

TICKS_PER_SECOND = 10_000_000
_DETAIL_KEYS = ("item_id", "series", "media_type", "device", "client", "mode")


def _title(item: dict) -> str:
    name = str(item.get("Name") or "unknown title")
    if item.get("Type") == "Episode":
        series = item.get("SeriesName") or name
        season, number = item.get("ParentIndexNumber"), item.get("IndexNumber")
        if isinstance(season, int) and isinstance(number, int):
            return f"{series} S{season:02d}E{number:02d}"
        return series
    year = item.get("ProductionYear")
    return f"{name} ({year})" if item.get("Type") == "Movie" and year else name


def playing(sessions: object) -> dict[str, dict]:
    if not isinstance(sessions, list):
        raise ShapeError(f"jellyfin /Sessions is not a list: {snippet(sessions)}")
    found: dict[str, dict] = {}
    for session in sessions:
        if not isinstance(session, dict):
            raise ShapeError(f"jellyfin session is not an object: {snippet(session)}")
        item = session.get("NowPlayingItem")
        if not item:
            continue
        try:
            session_id, item_id = str(session["Id"]), str(item["Id"])
        except (KeyError, TypeError):
            raise ShapeError(f"jellyfin session lacks Id/NowPlayingItem.Id: {snippet(session)}") from None
        state = session.get("PlayState") or {}
        found[f"{session_id}:{item_id}"] = {
            "user": norm_user(session.get("UserName")),
            "item_id": item_id,
            "title": _title(item),
            "series": item.get("SeriesName"),
            "media_type": item.get("Type"),
            "device": session.get("DeviceName") or "unknown device",
            "client": session.get("Client"),
            "mode": "transcode" if state.get("PlayMethod") == "Transcode" else "direct",
            "pos": int((state.get("PositionTicks") or 0) // TICKS_PER_SECOND),
        }
    return found


def _event_id(key: str, state: dict) -> str:
    return f"jellyfin:session:{key}:{state['first_seen']}"


def _started(key: str, state: dict) -> Event:
    return Event(id=_event_id(key, state), ts=state["first_seen"], service="jellyfin",
                 kind="playback.started", user=state["user"], title=state["title"],
                 detail={k: state[k] for k in _DETAIL_KEYS})


def _stopped(key: str, state: dict, now: str) -> Event:
    detail = {k: state[k] for k in _DETAIL_KEYS}
    detail["watched_seconds"] = max(0, state["pos"] - state["first_pos"])
    return Event(id=f"{_event_id(key, state)}:stopped", ts=now, service="jellyfin",
                 kind="playback.stopped", user=state["user"], title=state["title"], detail=detail)


class JellyfinCollector:
    name = "jellyfin"

    def __init__(self, *, base_url: str, api_key: str, fetch: Fetch, interval: int,
                 clock: Callable[[], float] = time.time) -> None:
        self.interval = interval
        self._url = f"{base_url.rstrip('/')}/Sessions"
        self._headers = {"Authorization": f'MediaBrowser Token="{api_key}"'}
        self._fetch = fetch
        self._clock = clock
        self.active: list[tuple[str | None, str]] = []

    def poll(self, mark: str | None) -> tuple[list[Event], str]:
        current = playing(self._fetch(self._url, self._headers))
        self.active = [(info["user"], info["mode"]) for info in current.values()]
        now = from_epoch(self._clock())
        previous: dict | None = None
        if mark is not None:
            try:
                previous = json.loads(mark)["playing"]
            except (ValueError, KeyError, TypeError):
                # An unreadable mark becomes a fresh baseline rather than a
                # collector that fails every poll until someone edits SQLite.
                previous = None
        events: list[Event] = []
        state: dict[str, dict] = {}
        for key, info in current.items():
            before = (previous or {}).get(key)
            if before is not None:
                state[key] = {**info, "first_seen": before["first_seen"], "first_pos": before["first_pos"]}
            else:
                state[key] = {**info, "first_seen": now, "first_pos": info["pos"]}
                if previous is not None:
                    events.append(_started(key, state[key]))
        for key, before in (previous or {}).items():
            if key not in current:
                events.append(_stopped(key, before, now))
        return events, json.dumps({"playing": state}, sort_keys=True)
```

- [ ] **Step 5: Run the tests and see them pass**

Run: `.venv/bin/python tests/validate_usage_relay.py jellyfin`

Expected: `Usage relay: OK (9 tests)`.

- [ ] **Step 6: Commit**

```bash
git add roles/svc_infra/files/usage_relay/collect_jellyfin.py tests/usage_relay/test_collect_jellyfin.py \
  tests/fixtures/usage_relay/jellyfin_sessions_idle.json tests/fixtures/usage_relay/jellyfin_sessions_playing.json
git commit -m "feat: infer Jellyfin playback by diffing sessions"
git push
```

---

### Task 7: The metrics registry

**Files:**
- Create: `roles/svc_infra/files/usage_relay/metrics.py`
- Test: `tests/usage_relay/test_metrics.py`

**Interfaces:**
- Produces:
  - `CATALOG: str`. Each line is `name type help`, starting at column 0.
    `tests/validate_grafana_dashboards.py` reads metric names from these
    lines (Task 13).
  - `metric_names() -> tuple[str, ...]`
  - `Metrics()`, with these methods:
    - `inc(name, labels: dict[str, str | None], amount=1.0)`
    - `set(name, labels, value)`
    - `replace(name, series: list[tuple[dict, float]])`
    - `value(name, labels) -> float`
    - `render() -> str`
  - An unknown name raises `KeyError`. A `None` label value renders as `"none"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/usage_relay/test_metrics.py`:

```python
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
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `.venv/bin/python tests/validate_usage_relay.py metrics`

Expected: FAIL with `No module named 'usage_relay.metrics'`.

- [ ] **Step 3: Write the registry**

Create `roles/svc_infra/files/usage_relay/metrics.py`:

```python
"""Prometheus exposition for the relay, scraped directly on usage_relay_port.

CATALOG is the single list of series this relay emits. Its lines start at
column 0 with the metric name on purpose: tests/validate_grafana_dashboards.py
reads emitted names from this file, so a dashboard panel querying a name that
is not here fails the build instead of rendering blank.

Titles are never labels — that would be unbounded cardinality. `user` is
bounded by the household's accounts; a missing user is "none", never "".
"""

from __future__ import annotations

import threading

CATALOG = """
homelab_usage_events_total counter Events recorded, by service, kind and user
homelab_usage_playback_active gauge Streams in progress at the last Jellyfin poll, by user and play method
homelab_usage_watch_seconds_total counter Seconds watched, summed from playback.stopped
homelab_usage_collector_last_success_timestamp gauge Unix time of each collector's last successful poll
homelab_usage_collector_errors_total counter Failed polls, by collector
homelab_usage_push_failures_total counter Failed ntfy publishes, by topic
"""

_SPECS: dict[str, tuple[str, str]] = {}
for _line in CATALOG.strip().splitlines():
    _name, _type, _help = _line.split(" ", 2)
    _SPECS[_name] = (_type, _help)

LabelKey = tuple[tuple[str, str], ...]


def metric_names() -> tuple[str, ...]:
    return tuple(_SPECS)


def _key(labels: dict[str, str | None]) -> LabelKey:
    return tuple(sorted((k, "none" if v is None else str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, dict[LabelKey, float]] = {name: {} for name in _SPECS}

    def _series(self, name: str) -> dict[LabelKey, float]:
        if name not in self._values:
            raise KeyError(f"unknown metric {name!r}; add it to CATALOG")
        return self._values[name]

    def inc(self, name: str, labels: dict[str, str | None], amount: float = 1.0) -> None:
        with self._lock:
            series = self._series(name)
            key = _key(labels)
            series[key] = series.get(key, 0.0) + amount

    def set(self, name: str, labels: dict[str, str | None], value: float) -> None:
        with self._lock:
            self._series(name)[_key(labels)] = float(value)

    def replace(self, name: str, series: list[tuple[dict, float]]) -> None:
        with self._lock:
            target = self._series(name)
            target.clear()
            for labels, value in series:
                target[_key(labels)] = float(value)

    def value(self, name: str, labels: dict[str, str | None]) -> float:
        with self._lock:
            return self._series(name).get(_key(labels), 0.0)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, (kind, help_text) in _SPECS.items():
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {kind}")
                for key in sorted(self._values[name]):
                    labels = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
                    sample = f"{name}{{{labels}}}" if labels else name
                    lines.append(f"{sample} {_number(self._values[name][key])}")
        return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `.venv/bin/python tests/validate_usage_relay.py metrics`

Expected: `Usage relay: OK (8 tests)`.

- [ ] **Step 5: Commit**

```bash
git add roles/svc_infra/files/usage_relay/metrics.py tests/usage_relay/test_metrics.py
git commit -m "feat: add the usage relay's Prometheus metrics registry"
git push
```

---

### Task 8: The ntfy client and the router

**Files:**
- Create: `roles/svc_infra/files/usage_relay/ntfy.py`
- Create: `roles/svc_infra/files/usage_relay/router.py`
- Test: `tests/usage_relay/test_ntfy.py`, `tests/usage_relay/test_router.py`

**Interfaces:**
- Consumes: `model.Event`, `Push`, `parse_ts`, `utc_iso` and `from_epoch`;
  `store.Store` (`started_within`).
- Produces from `ntfy.py`:
  - `NtfyError`
  - `Ntfy(base_url, token="", timeout=10.0)` with `.publish(push) -> None`
    and `.poll(topic, since="5m") -> list[dict]`. `poll` returns only the
    `event == "message"` items.
- Produces from `router.py`:
  - `TOPIC_KEYS: dict[str, str]`, mapping each kind to a topic key
  - `PRIORITIES: dict[str, int]`
  - `Router(*, store, topics, batch_window, resume_window, playback_expiry, push_ignore_users)`
  - `.initial_state(event) -> str`, one of `pending`, `suppressed` or `none`
  - `.plan(pending: list[Event], now: float) -> tuple[list[Push], list[str]]`,
    returning the pushes and the expired event ids

- [ ] **Step 1: Write the failing ntfy tests**

Create `tests/usage_relay/test_ntfy.py`:

```python
from __future__ import annotations

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.model import Push
from usage_relay.ntfy import Ntfy, NtfyError


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.posts.append((json.loads(self.rfile.read(length)), dict(self.headers)))
        code = 500 if self.server.fail else 200
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        body = (b'{"event":"open","topic":"usage-selftest"}\n'
                b'{"event":"message","topic":"usage-selftest","message":"selftest:1"}\n')
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.gets.append(self.path)

    def log_message(self, *args):
        return


class NtfyTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.posts, self.server.gets, self.server.fail = [], [], False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def push(self):
        return Push(topic="usage-requests", title="📥 New request", message="Alice requested X (movie)",
                    priority=3, event_ids=("e1",))

    def test_publish_sends_json_so_emoji_titles_survive(self):
        Ntfy(self.url, token="example-token").publish(self.push())
        body, headers = self.server.posts[0]
        self.assertEqual(body, {"topic": "usage-requests", "title": "📥 New request",
                                "message": "Alice requested X (movie)", "priority": 3})
        self.assertEqual(headers.get("Authorization"), "Bearer example-token")

    def test_no_token_means_no_authorization_header(self):
        Ntfy(self.url).publish(self.push())
        self.assertNotIn("Authorization", self.server.posts[0][1])

    def test_a_server_error_is_an_ntfy_error(self):
        self.server.fail = True
        with self.assertRaises(NtfyError):
            Ntfy(self.url).publish(self.push())

    def test_an_unreachable_server_is_an_ntfy_error(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(NtfyError):
            Ntfy(f"http://127.0.0.1:{port}", timeout=2).publish(self.push())

    def test_poll_returns_only_messages(self):
        messages = Ntfy(self.url).poll("usage-selftest", since="5m")
        self.assertEqual([m["message"] for m in messages], ["selftest:1"])
        self.assertEqual(self.server.gets[0], "/usage-selftest/json?poll=1&since=5m")
```

- [ ] **Step 2: Write the failing router tests**

Create `tests/usage_relay/test_router.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import TOPICS, FakeClock
from usage_relay.model import Event, from_epoch
from usage_relay.router import Router
from usage_relay.store import Store


def ev(event_id, kind, ts, user=None, service="test", title="T", **detail):
    return Event(id=event_id, ts=ts, service=service, kind=kind, user=user, title=title, detail=detail)


class RouterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(str(Path(tmp.name) / "usage.db"))
        self.addCleanup(self.store.close)
        self.clock = FakeClock()
        self.router = Router(store=self.store, topics=TOPICS, batch_window=120, resume_window=1800,
                             playback_expiry=600, push_ignore_users=frozenset({"brandon"}))

    def at(self, offset: float) -> str:
        return from_epoch(self.clock() + offset)

    def record(self, event):
        state = self.router.initial_state(event)
        self.store.insert(event, state)
        return state

    def test_recorded_only_kinds_are_never_pushed(self):
        for kind in ("download.grabbed", "download.completed", "playback.stopped",
                     "request.approved", "request.available"):
            self.assertEqual(self.router.initial_state(ev("x", kind, self.at(0))), "none", kind)

    def test_requests_imports_failures_and_selftest_are_pending(self):
        for kind in ("request.created", "download.imported", "download.failed", "selftest"):
            self.assertEqual(self.router.initial_state(ev("x", kind, self.at(0))), "pending", kind)

    def test_the_ignore_list_silences_playback_only(self):
        self.assertEqual(self.router.initial_state(
            ev("p", "playback.started", self.at(0), "brandon", item_id="i")), "suppressed")
        self.assertEqual(self.router.initial_state(
            ev("r", "request.created", self.at(0), "brandon")), "pending")

    def test_a_resume_within_the_window_is_suppressed(self):
        self.assertEqual(self.record(ev("p1", "playback.started", self.at(0), "alice", item_id="i1")), "pending")
        self.assertEqual(self.record(ev("p2", "playback.started", self.at(1200), "alice", item_id="i1")), "suppressed")
        self.assertEqual(self.record(ev("p3", "playback.started", self.at(1200), "alice", item_id="i2")), "pending")
        self.assertEqual(self.record(ev("p4", "playback.started", self.at(3100), "alice", item_id="i1")), "pending")

    def test_each_kind_gets_its_topic_priority_and_wording(self):
        pending = [
            ev("r", "request.created", self.at(0), "alice", title="Example Movie", media_type="movie"),
            ev("f", "download.failed", self.at(0), service="radarr", title="Example Movie (2024)",
               reason="no matching file"),
            ev("p", "playback.started", self.at(0), "erin", title="The Example Show S02E03",
               device="Living Room TV", mode="transcode", item_id="i"),
            ev("i", "download.imported", self.at(0), service="radarr", title="Example Movie (2024)",
               requested_by="alice"),
            ev("s", "selftest", self.at(0), service="relay", title="usage-relay selftest"),
        ]
        pushes, expired = self.router.plan(pending, self.clock())
        self.assertEqual(expired, [])
        got = {p.event_ids[0]: (p.topic, p.priority, p.title, p.message) for p in pushes}
        self.assertEqual(got, {
            "r": ("usage-requests", 3, "📥 New request", "Alice requested Example Movie (movie)"),
            "f": ("usage-failures", 4, "⚠️ Radarr failed", "Example Movie (2024): no matching file"),
            "p": ("usage-playback", 2, "▶️ Erin", "The Example Show S02E03 · Living Room TV · transcoding"),
            "i": ("usage-library", 3, "✅ Ready", "Example Movie (2024) · requested by Alice"),
            "s": ("usage-selftest", 1, "usage-relay selftest", "s"),
        })

    def test_a_season_pack_is_held_then_sent_as_one_push(self):
        imports = [ev(f"i{n}", "download.imported", self.at(n * 10), service="sonarr",
                      title=f"The Example Show S02E0{n}", series="The Example Show",
                      series_key="sonarr:series:7", season=2) for n in (1, 2, 3)]
        held, _ = self.router.plan(imports, self.clock() + 90)
        self.assertEqual(held, [])
        pushes, _ = self.router.plan(imports, self.clock() + 151)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0].message, "The Example Show S02: 3 episodes ready")
        self.assertEqual(pushes[0].event_ids, ("i1", "i2", "i3"))

    def test_a_lone_episode_after_the_window_is_pushed_by_name(self):
        lone = ev("i1", "download.imported", self.at(0), service="sonarr", title="The Example Show S02E01",
                  series="The Example Show", series_key="sonarr:series:7", season=2)
        pushes, _ = self.router.plan([lone], self.clock() + 121)
        self.assertEqual([p.message for p in pushes], ["The Example Show S02E01"])

    def test_stale_playback_expires_but_other_kinds_still_send(self):
        stale = [ev("p", "playback.started", self.at(0), "alice", item_id="i"),
                 ev("r", "request.created", self.at(0), "alice", media_type="tv")]
        pushes, expired = self.router.plan(stale, self.clock() + 601)
        self.assertEqual(expired, ["p"])
        self.assertEqual([p.event_ids for p in pushes], [("r",)])
```

- [ ] **Step 3: Run the tests and see them fail**

Run: `.venv/bin/python tests/validate_usage_relay.py ntfy; .venv/bin/python tests/validate_usage_relay.py router`

Expected: both FAIL with a missing-module error.

- [ ] **Step 4: Write `ntfy.py`**

Create `roles/svc_infra/files/usage_relay/ntfy.py`:

```python
"""Publishing to ntfy, and reading a topic back for the selftest."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .model import Push


class NtfyError(Exception):
    """ntfy could not be reached, refused the request, or answered garbage."""


class Ntfy:
    def __init__(self, base_url: str, token: str = "", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _send(self, request: urllib.request.Request, what: str) -> bytes:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise NtfyError(f"{what} failed: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NtfyError(f"{what} failed: {type(exc).__name__}") from None

    def publish(self, push: Push) -> None:
        # JSON publishing to the root URL rather than Title/Priority headers:
        # HTTP headers are latin-1, and every title here starts with an emoji.
        body = json.dumps({"topic": push.topic, "title": push.title,
                           "message": push.message, "priority": push.priority}).encode("utf-8")
        request = urllib.request.Request(self.base_url, data=body, method="POST",
                                         headers=self._headers({"Content-Type": "application/json"}))
        self._send(request, f"publish to {push.topic}")

    def poll(self, topic: str, since: str = "5m") -> list[dict]:
        request = urllib.request.Request(f"{self.base_url}/{topic}/json?poll=1&since={since}",
                                         headers=self._headers())
        body = self._send(request, f"poll of {topic}")
        messages = []
        try:
            for line in body.decode("utf-8").splitlines():
                if line.strip():
                    item = json.loads(line)
                    if item.get("event") == "message":
                        messages.append(item)
        except ValueError:
            raise NtfyError(f"poll of {topic} returned something that is not ndjson") from None
        return messages
```

- [ ] **Step 5: Write `router.py`**

Create `roles/svc_infra/files/usage_relay/router.py`:

```python
"""Which recorded events become pushes, on which topic, and what they say.

initial_state() runs once, at insert. plan() runs every push cycle over the
rows still pending, so batching and expiry are decided against the clock rather
than at insert time: a season pack is held until no new episode has arrived for
batch_window, and a playback push that sat out an ntfy outage longer than
playback_expiry is dropped as stale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .model import Event, Push, parse_ts, utc_iso
from .store import Store

TOPIC_KEYS = {
    "request.created": "requests",
    "download.imported": "library",
    "download.failed": "failures",
    "playback.started": "playback",
    "selftest": "selftest",
}
PRIORITIES = {"requests": 3, "library": 3, "failures": 4, "playback": 2, "selftest": 1}
SERVICE_NAMES = {"sonarr": "Sonarr", "radarr": "Radarr", "sabnzbd": "SABnzbd"}


def who(user: str | None) -> str:
    return user.capitalize() if user else "Someone"


def _requested_by(detail: dict) -> str:
    requester = detail.get("requested_by")
    return f" · requested by {who(requester)}" if requester else ""


class Router:
    def __init__(self, *, store: Store, topics: dict[str, str], batch_window: float,
                 resume_window: float, playback_expiry: float,
                 push_ignore_users: frozenset[str]) -> None:
        self.store = store
        self.topics = topics
        self.batch_window = batch_window
        self.resume_window = resume_window
        self.playback_expiry = playback_expiry
        self.push_ignore_users = push_ignore_users

    def initial_state(self, event: Event) -> str:
        if event.kind not in TOPIC_KEYS:
            return "none"
        if event.kind == "playback.started":
            if event.user in self.push_ignore_users:
                return "suppressed"
            since = utc_iso(parse_ts(event.ts) - timedelta(seconds=self.resume_window))
            item_id = str(event.detail.get("item_id", ""))
            if event.user and self.store.started_within(event.user, item_id, since, event.ts,
                                                        exclude_id=event.id):
                return "suppressed"
        return "pending"

    def plan(self, pending: list[Event], now: float) -> tuple[list[Push], list[str]]:
        current = datetime.fromtimestamp(now, UTC)
        pushes: list[Push] = []
        expired: list[str] = []
        series: dict[str, list[Event]] = {}
        for event in pending:
            age = (current - parse_ts(event.ts)).total_seconds()
            if event.kind.startswith("playback.") and age > self.playback_expiry:
                expired.append(event.id)
            elif event.kind == "download.imported" and event.detail.get("series_key"):
                series.setdefault(event.detail["series_key"], []).append(event)
            else:
                pushes.append(self._single(event))
        for group in series.values():
            newest = max(parse_ts(e.ts) for e in group)
            if (current - newest).total_seconds() < self.batch_window:
                continue
            pushes.append(self._single(group[0]) if len(group) == 1 else self._batch(group))
        return pushes, expired

    def _single(self, event: Event) -> Push:
        key = TOPIC_KEYS[event.kind]
        detail = event.detail
        if event.kind == "request.created":
            title = "📥 New request"
            message = f"{who(event.user)} requested {event.title} ({detail.get('media_type', 'media')})"
        elif event.kind == "download.imported":
            title, message = "✅ Ready", event.title + _requested_by(detail)
        elif event.kind == "download.failed":
            title = f"⚠️ {SERVICE_NAMES.get(event.service, event.service)} failed"
            message = f"{event.title}: {detail.get('reason', 'no reason given')}"
        elif event.kind == "playback.started":
            how = "transcoding" if detail.get("mode") == "transcode" else "direct play"
            title = f"▶️ {who(event.user)}"
            message = f"{event.title} · {detail.get('device', 'unknown device')} · {how}"
        else:
            title, message = "usage-relay selftest", event.id
        return Push(topic=self.topics[key], title=title, message=message,
                    priority=PRIORITIES[key], event_ids=(event.id,))

    def _batch(self, group: list[Event]) -> Push:
        first = group[0]
        name = first.detail.get("series") or first.title
        seasons = {e.detail.get("season") for e in group}
        season = next(iter(seasons)) if len(seasons) == 1 else None
        what = f"{name} S{season:02d}" if isinstance(season, int) else name
        requested = next((e.detail for e in group if e.detail.get("requested_by")), {})
        return Push(topic=self.topics["library"], title="✅ Ready",
                    message=f"{what}: {len(group)} episodes ready" + _requested_by(requested),
                    priority=PRIORITIES["library"], event_ids=tuple(e.id for e in group))
```

- [ ] **Step 6: Run the tests and see them pass**

Run: `.venv/bin/python tests/validate_usage_relay.py ntfy && .venv/bin/python tests/validate_usage_relay.py router`

Expected: `OK (5 tests)` and then `OK (8 tests)`.

- [ ] **Step 7: Commit**

```bash
git add roles/svc_infra/files/usage_relay/ntfy.py roles/svc_infra/files/usage_relay/router.py \
  tests/usage_relay/test_ntfy.py tests/usage_relay/test_router.py
git commit -m "feat: route usage events to ntfy topics with batching and suppression"
git push
```

---

### Task 9: The relay core

**Files:**
- Create: `roles/svc_infra/files/usage_relay/relay.py`
- Test: `tests/usage_relay/test_relay.py`

**Interfaces:**
- Consumes everything above. The `ntfy` argument is anything with
  `.publish(push)` that raises `NtfyError`.
- Produces:
  - `CollectorHealth` (a dataclass)
  - `Relay(*, store, router, ntfy, metrics, collectors: dict[str, collector | None], alert_topic, alert_after, clock=time.time, log=<stderr>)`
- `Relay` methods:
  - `ingest(events) -> int`, the count of new rows
  - `poll_collector(name) -> None`. A `sqlite3.Error` propagates; any other
    exception is contained.
  - `push_cycle() -> None` and `check_health() -> None`
  - `selftest_insert() -> str`, the event id
  - `maintenance(*, retention_days, snapshot_path) -> None`
  - `health_report() -> dict`, shaped as
    `{"now": float, "collectors": {name: {"enabled", "last_success", "last_error"}}}`
  - `run(*, push_interval, maintenance_time, retention_days, snapshot_path, stop: threading.Event) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/usage_relay/test_relay.py`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import TOPICS, FakeClock, FakeFetch, FakeNtfy, fixture
from usage_relay.collect_jellyfin import JellyfinCollector
from usage_relay.fetch import FetchError
from usage_relay.metrics import Metrics
from usage_relay.model import Event, from_epoch
from usage_relay.relay import Relay
from usage_relay.router import Router
from usage_relay.store import Store

EVENTS = "homelab_usage_events_total"
ERRORS = "homelab_usage_collector_errors_total"


class FakeCollector:
    def __init__(self, name, *results, interval=60):
        self.name = name
        self.interval = interval
        self.results = list(results)
        self.marks = []

    def poll(self, mark):
        self.marks.append(mark)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class RelayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.store = Store(str(self.tmp / "usage.db"))
        self.addCleanup(self.store.close)
        self.clock = FakeClock()
        self.ntfy = FakeNtfy()
        self.metrics = Metrics()
        self.router = Router(store=self.store, topics=TOPICS, batch_window=120, resume_window=1800,
                             playback_expiry=600, push_ignore_users=frozenset({"brandon"}))

    def relay(self, **collectors):
        return Relay(store=self.store, router=self.router, ntfy=self.ntfy, metrics=self.metrics,
                     collectors=collectors, alert_topic="homelab-alerts", alert_after=900,
                     clock=self.clock, log=lambda _message: None)

    def ev(self, event_id, kind, user=None, service="seerr", offset=0, **detail):
        return Event(id=event_id, ts=from_epoch(self.clock() + offset), service=service, kind=kind,
                     user=user, title=event_id, detail=detail)

    def test_the_first_poll_records_the_mark_and_pushes_nothing(self):
        c = FakeCollector("sonarr", ([], "2026-10-05T12:00:00Z"))
        r = self.relay(sonarr=c)
        r.poll_collector("sonarr")
        r.push_cycle()
        self.assertEqual(c.marks, [None])
        self.assertEqual(self.store.get_mark("sonarr"), "2026-10-05T12:00:00Z")
        self.assertEqual(self.ntfy.sent, [])

    def test_a_repeated_event_is_counted_once(self):
        request = self.ev("seerr:request:1:created", "request.created", "alice")
        c = FakeCollector("seerr", ([request], "m1"), ([request], "m2"))
        r = self.relay(seerr=c)
        r.poll_collector("seerr")
        r.poll_collector("seerr")
        self.assertEqual(c.marks, [None, "m1"])
        self.assertEqual(self.metrics.value(EVENTS, {"service": "seerr", "kind": "request.created",
                                                     "user": "alice"}), 1)

    def test_imports_are_attributed_to_the_requester(self):
        r = self.relay()
        r.ingest([self.ev("req", "request.created", "alice", match_key="tmdb:9001")])
        r.ingest([self.ev("imp", "download.imported", service="radarr", match_key="tmdb:9001")])
        self.assertEqual(self.store.get("imp").detail["requested_by"], "alice")

    def test_a_failing_collector_keeps_its_mark_and_the_others_still_run(self):
        broken = FakeCollector("sonarr", FetchError("HTTP 500 from http://sonarr.example"))
        healthy = FakeCollector("radarr", ([], "m"))
        r = self.relay(sonarr=broken, radarr=healthy)
        self.store.set_mark("sonarr", "old")
        r.poll_collector("sonarr")
        r.poll_collector("radarr")
        self.assertEqual(self.store.get_mark("sonarr"), "old")
        self.assertEqual(self.store.get_mark("radarr"), "m")
        self.assertEqual(self.metrics.value(ERRORS, {"collector": "sonarr"}), 1)
        self.assertEqual(r.health["sonarr"].last_error, "HTTP 500 from http://sonarr.example")

    def test_a_collector_bug_is_contained_but_a_database_error_is_fatal(self):
        r = self.relay(buggy=FakeCollector("buggy", KeyError("oops")),
                       db=FakeCollector("db", sqlite3.OperationalError("disk I/O error")))
        r.poll_collector("buggy")
        self.assertEqual(self.metrics.value(ERRORS, {"collector": "buggy"}), 1)
        with self.assertRaises(sqlite3.OperationalError):
            r.poll_collector("db")

    def test_a_collector_failing_for_15_minutes_alerts_once_then_recovers(self):
        c = FakeCollector("seerr", FetchError("x"), FetchError("x"), FetchError("x"), ([], "m"))
        r = self.relay(seerr=c)
        r.poll_collector("seerr")
        self.clock.advance(600)
        r.poll_collector("seerr")
        r.check_health()
        self.assertEqual(self.ntfy.sent, [])
        self.clock.advance(301)
        r.poll_collector("seerr")
        r.check_health()
        r.check_health()
        self.assertEqual([(p.topic, p.priority) for p in self.ntfy.sent], [("homelab-alerts", 4)])
        self.assertIn("seerr collector failing", self.ntfy.sent[0].title)
        r.poll_collector("seerr")
        self.assertEqual(len(self.ntfy.sent), 2)
        self.assertIn("recovered", self.ntfy.sent[1].title)

    def test_pushes_wait_out_an_ntfy_outage(self):
        r = self.relay()
        r.ingest([self.ev("req", "request.created", "alice", media_type="movie")])
        self.ntfy.down = True
        r.push_cycle()
        self.assertEqual(self.store.push_state("req"), "pending")
        self.assertEqual(self.metrics.value("homelab_usage_push_failures_total",
                                            {"topic": "usage-requests"}), 1)
        self.ntfy.down = False
        r.push_cycle()
        self.assertEqual(self.store.push_state("req"), "sent")

    def test_stale_playback_expires_after_an_outage(self):
        r = self.relay()
        r.ingest([self.ev("play", "playback.started", "alice", service="jellyfin", item_id="i"),
                  self.ev("req", "request.created", "alice", media_type="tv")])
        self.clock.advance(700)
        r.push_cycle()
        self.assertEqual(self.store.push_state("play"), "expired")
        self.assertEqual(self.store.push_state("req"), "sent")

    def test_a_selftest_is_recorded_counted_and_pushed_next_cycle(self):
        r = self.relay()
        event_id = r.selftest_insert()
        r.push_cycle()
        self.assertEqual([(p.topic, p.message) for p in self.ntfy.sent], [("usage-selftest", event_id)])
        self.assertEqual(self.metrics.value(EVENTS, {"service": "relay", "kind": "selftest", "user": None}), 1)

    def test_a_jellyfin_poll_publishes_the_stream_gauge(self):
        fetch = FakeFetch({"http://jellyfin.example/Sessions": fixture("jellyfin_sessions_playing.json")})
        jf = JellyfinCollector(base_url="http://jellyfin.example", api_key="example-api-key",
                               fetch=fetch, interval=30, clock=self.clock)
        r = self.relay(jellyfin=jf)
        r.poll_collector("jellyfin")
        gauge = "homelab_usage_playback_active"
        self.assertEqual(self.metrics.value(gauge, {"user": "alice", "mode": "transcode"}), 1)
        self.assertEqual(self.metrics.value(gauge, {"user": "bob", "mode": "direct"}), 1)

    def test_watch_seconds_accumulate_from_stops(self):
        r = self.relay()
        r.ingest([self.ev("s1", "playback.stopped", "alice", service="jellyfin", watched_seconds=600),
                  self.ev("s2", "playback.stopped", "alice", service="jellyfin", watched_seconds=300)])
        self.assertEqual(self.metrics.value("homelab_usage_watch_seconds_total", {"user": "alice"}), 900)

    def test_maintenance_prunes_old_rows_and_writes_a_snapshot(self):
        r = self.relay()
        r.ingest([self.ev("old", "request.created", "alice", offset=-400 * 86400),
                  self.ev("new", "request.created", "alice")])
        snapshot = self.tmp / "usage.snapshot.db"
        r.maintenance(retention_days=365, snapshot_path=str(snapshot))
        self.assertIsNone(self.store.get("old"))
        self.assertIsNotNone(self.store.get("new"))
        self.assertTrue(snapshot.exists())

    def test_the_health_report_marks_disabled_collectors(self):
        r = self.relay(sonarr=None, radarr=FakeCollector("radarr", ([], "m")))
        r.poll_collector("radarr")
        report = r.health_report()
        self.assertEqual(report["collectors"]["sonarr"],
                         {"enabled": False, "last_success": None, "last_error": None})
        self.assertEqual(report["collectors"]["radarr"]["last_success"], self.clock())
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `.venv/bin/python tests/validate_usage_relay.py relay`

Expected: FAIL with `No module named 'usage_relay.relay'`.

- [ ] **Step 3: Write the relay**

Create `roles/svc_infra/files/usage_relay/relay.py`:

```python
"""The relay's core: poll collectors, record, count, push, watch its own health.

Order inside poll_collector() is what makes restarts lossless: events are
inserted BEFORE the new mark is stored, so a crash between the two re-reads a
page whose rows the store then drops as duplicates — never skips one.

A SQLite error is the one failure that is allowed to kill the process. A relay
that pushes without recording would break "emit the number the alert used",
and systemd's Restart=on-failure brings it back.
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
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `.venv/bin/python tests/validate_usage_relay.py relay`

Expected: `Usage relay: OK (13 tests)`.

- [ ] **Step 5: Commit**

```bash
git add roles/svc_infra/files/usage_relay/relay.py tests/usage_relay/test_relay.py
git commit -m "feat: add the usage relay core loop, health alerts and selftest"
git push
```

### Task 10: The weekly digest renderer

**Files:**
- Create: `roles/svc_infra/files/usage_relay/digest.py`
- Test: `tests/usage_relay/test_digest.py`

**Interfaces:**
- Consumes: `model.Event`.
- Produces: `render(events: list[Event], start: datetime, end: datetime) -> str`.
  The text ends with a newline.

**Rules from the spec:**
- An account with no activity is omitted.
- The closing line is always `Read      <n> events · <k> active accounts`.
- A week with no non-selftest events renders `No activity`.
- Plays count distinct `(user, item_id)` pairs among `playback.started`, so a
  resume is not a second play.

- [ ] **Step 1: Write the failing tests**

Create `tests/usage_relay/test_digest.py`:

```python
from __future__ import annotations

import unittest
from datetime import datetime

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.digest import render
from usage_relay.model import Event

START = datetime(2026, 9, 28, 18, 0)
END = datetime(2026, 10, 5, 18, 0)


def ev(event_id, kind, user=None, service="jellyfin", title="", **detail):
    return Event(id=event_id, ts="2026-10-01T20:00:00Z", service=service, kind=kind, user=user,
                 title=title or event_id, detail=detail)


SHOW = "The Example Show"
WEEK = [
    ev("p1", "playback.started", "alice", title=f"{SHOW} S02E01", item_id="e1", series=SHOW, mode="transcode"),
    ev("p2", "playback.started", "alice", title=f"{SHOW} S02E02", item_id="e2", series=SHOW, mode="direct"),
    ev("p3", "playback.started", "alice", title="Example Movie (2024)", item_id="m1", series=None, mode="direct"),
    ev("p4", "playback.started", "alice", title=f"{SHOW} S02E01", item_id="e1", series=SHOW, mode="direct"),
    ev("s1", "playback.stopped", "alice", watched_seconds=3600),
    ev("s2", "playback.stopped", "alice", watched_seconds=1800),
    ev("s3", "playback.stopped", "alice", watched_seconds=1800),
    ev("r1", "request.created", "bob", service="seerr", request_id=12),
    ev("r2", "request.available", "bob", service="seerr", request_id=12),
    ev("i1", "download.imported", service="sonarr"),
    ev("i2", "download.imported", service="sonarr"),
    ev("i3", "download.imported", service="radarr"),
    ev("f1", "download.failed", service="sonarr"),
    ev("probe", "selftest", service="relay"),
]


class DigestTests(unittest.TestCase):
    def test_a_full_week(self):
        self.assertEqual(render(WEEK, START, END), (
            "Week of Sep 28 – Oct 5\n"
            "Alice     3 plays · 2.0h · The Example Show (2), Example Movie (2024)\n"
            "Requests  1 (Bob 1) · 1 fulfilled, 0 pending\n"
            "Library   +2 episodes, +1 movie · 1 failed import\n"
            "Streams   67% direct play, 33% transcode\n"
            "Read      13 events · 2 active accounts\n"
        ))

    def test_an_empty_week_says_so_and_still_reports_what_it_read(self):
        self.assertEqual(render([], START, END), (
            "Week of Sep 28 – Oct 5\n"
            "No activity\n"
            "Read      0 events · 0 active accounts\n"
        ))

    def test_a_week_of_only_selftests_is_empty(self):
        self.assertIn("No activity\n", render([ev("probe", "selftest", service="relay")], START, END))
```

- [ ] **Step 2: Run the tests and see them fail**

Run: `.venv/bin/python tests/validate_usage_relay.py digest`

Expected: FAIL with `No module named 'usage_relay.digest'`.

- [ ] **Step 3: Write the renderer**

Create `roles/svc_infra/files/usage_relay/digest.py`:

```python
"""The weekly digest: one plain-text summary of the household's week.

Accounts with no activity are left out, so the last line always says how many
rows were read and how many accounts were active. That line is what tells a
quiet week apart from a digest that could not read anything.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .model import Event

LABEL_WIDTH = 9


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _line(label: str, text: str) -> str:
    return f"{label:<{LABEL_WIDTH}} {text}"


def _requests_line(events: list[Event]) -> str:
    created = [e for e in events if e.kind == "request.created"]
    if not created:
        return _line("Requests", "0")
    available = {e.detail.get("request_id") for e in events if e.kind == "request.available"}
    fulfilled = sum(1 for e in created if e.detail.get("request_id") in available)
    by_user = Counter(e.user for e in created if e.user)
    who = ", ".join(f"{user.capitalize()} {n}" for user, n in by_user.most_common())
    total = f"{len(created)} ({who})" if who else str(len(created))
    return _line("Requests", f"{total} · {fulfilled} fulfilled, {len(created) - fulfilled} pending")


def _library_line(events: list[Event]) -> str:
    imported = [e for e in events if e.kind == "download.imported"]
    episodes = sum(1 for e in imported if e.service == "sonarr")
    movies = sum(1 for e in imported if e.service == "radarr")
    failed = sum(1 for e in events if e.kind == "download.failed")
    return _line("Library", f"+{_count(episodes, 'episode')}, +{_count(movies, 'movie')}"
                            f" · {_count(failed, 'failed import')}")


def _streams_line(started: list[Event]) -> str:
    if not started:
        return _line("Streams", "no plays")
    direct = sum(1 for e in started if e.detail.get("mode") != "transcode")
    share = round(100 * direct / len(started))
    return _line("Streams", f"{share}% direct play, {100 - share}% transcode")


def render(events: list[Event], start: datetime, end: datetime) -> str:
    header = f"Week of {start:%b} {start.day} – {end:%b} {end.day}"
    counted = [e for e in events if e.kind != "selftest"]
    users = {e.user for e in counted if e.user}
    footer = _line("Read", f"{_count(len(counted), 'event')} · {_count(len(users), 'active account')}")
    if not counted:
        return "\n".join([header, "No activity", footer]) + "\n"

    plays: dict[str, dict[str, Event]] = {}
    for e in counted:
        if e.kind == "playback.started" and e.user:
            plays.setdefault(e.user, {}).setdefault(str(e.detail.get("item_id", e.id)), e)
    watched: Counter[str] = Counter()
    for e in counted:
        if e.kind == "playback.stopped" and e.user:
            watched[e.user] += float(e.detail.get("watched_seconds", 0))

    lines = [header]
    for user in sorted(plays, key=lambda u: (-len(plays[u]), u)):
        titles = Counter(e.detail.get("series") or e.title for e in plays[user].values())
        named = ", ".join(t if n == 1 else f"{t} ({n})" for t, n in titles.most_common(3))
        lines.append(_line(user.capitalize(),
                           f"{_count(len(plays[user]), 'play')} · {watched[user] / 3600:.1f}h · {named}"))
    lines.append(_requests_line(counted))
    lines.append(_library_line(counted))
    lines.append(_streams_line([e for per_user in plays.values() for e in per_user.values()]))
    lines.append(footer)
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `.venv/bin/python tests/validate_usage_relay.py digest`

Expected: `Usage relay: OK (3 tests)`.

- [ ] **Step 5: Commit**

```bash
git add roles/svc_infra/files/usage_relay/digest.py tests/usage_relay/test_digest.py
git commit -m "feat: render the weekly household usage digest"
git push
```

---

### Task 11: Config, the HTTP server and the CLI

**Files:**
- Create: `roles/svc_infra/files/usage_relay/config.py`
- Create: `roles/svc_infra/files/usage_relay/httpserver.py`
- Create: `roles/svc_infra/files/usage_relay/cli.py`
- Create: `roles/svc_infra/files/usage_relay/__main__.py`
- Test: `tests/usage_relay/test_config.py`, `tests/usage_relay/test_httpserver.py`,
  `tests/usage_relay/test_cli.py`

**Interfaces:**
- Consumes everything above.
- Produces from `config.py`:
  - `CollectorSettings(base_url, interval, key_env)`
  - `Settings` (the fields are in the code below)
  - `from_dict(raw) -> Settings`, which raises `ValueError` and names the
    problem
  - `load(path) -> Settings`
  - `build_collectors(settings, env, fetch, clock=time.time) -> dict[str, collector | None]`
- Produces from `httpserver.py`:
  - `RelayHTTPServer(address, relay, metrics)`, which serves `GET /metrics`,
    `GET /health` and `POST /selftest`
  - `POST /selftest` is loopback-only (403 otherwise) and returns `{"id": ...}`.
- Produces from `cli.py`:
  - `main(argv=None) -> int`
  - `cmd_run(settings, env) -> int`
  - `cmd_digest(settings, ntfy, now) -> int`
  - `cmd_selftest(settings, ntfy, timeout=60, sleep=time.sleep, clock=time.time) -> int`
  - `cmd_check(settings, wait, sleep=time.sleep, clock=time.time) -> int`
  - `assess(report) -> (lines, stale_names)` and `selftest_count(metrics_text) -> float`
- **The command line:** `python3 -m usage_relay --config PATH <run|digest|selftest|check> [--wait SECONDS]`.
  It reads `NTFY_URL`, `NTFY_TOKEN` and `NTFY_ALERT_TOPIC` from the
  environment, which comes from `/etc/homelab-notify.env`, plus the
  `USAGE_*_API_KEY` variables from `/etc/usage-relay/keys.env`.

- [ ] **Step 1: Write the failing config tests**

Create `tests/usage_relay/test_config.py`:

```python
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
```

- [ ] **Step 2: Write the failing HTTP server tests**

Create `tests/usage_relay/test_httpserver.py`:

```python
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

import helpers  # noqa: F401  (puts roles/svc_infra/files on sys.path)
from usage_relay.httpserver import RelayHTTPServer
from usage_relay.metrics import Metrics


class FakeRelay:
    def health_report(self):
        return {"now": 1.0, "collectors": {}}

    def selftest_insert(self):
        return "selftest:1"


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        metrics = Metrics()
        metrics.inc("homelab_usage_collector_errors_total", {"collector": "seerr"}, 0)
        cls.server = RelayHTTPServer(("127.0.0.1", 0), FakeRelay(), metrics)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, data=None):
        request = urllib.request.Request(self.base + path, data=data,
                                         method="POST" if data is not None else "GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

    def test_metrics_are_served(self):
        self.assertIn("# TYPE homelab_usage_collector_errors_total counter", self.get("/metrics"))

    def test_health_is_json(self):
        self.assertEqual(json.loads(self.get("/health")), {"now": 1.0, "collectors": {}})

    def test_selftest_from_loopback_returns_the_event_id(self):
        self.assertEqual(json.loads(self.get("/selftest", data=b"")), {"id": "selftest:1"})

    def test_unknown_paths_are_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)
```

- [ ] **Step 3: Write the failing CLI tests**

Create `tests/usage_relay/test_cli.py`:

```python
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from helpers import TOPICS, FakeNtfy, make_settings
from usage_relay.cli import assess, cmd_digest, cmd_selftest, selftest_count
from usage_relay.httpserver import RelayHTTPServer
from usage_relay.metrics import Metrics
from usage_relay.model import Event
from usage_relay.relay import Relay
from usage_relay.router import Router
from usage_relay.store import Store


class AssessTests(unittest.TestCase):
    def test_each_collector_state_is_named(self):
        report = {"now": 1000.0, "collectors": {
            "jellyfin": {"enabled": True, "last_success": 990.0, "last_error": None},
            "seerr": {"enabled": False, "last_success": None, "last_error": None},
            "sonarr": {"enabled": True, "last_success": 600.0, "last_error": "HTTP 500 from http://x"},
            "radarr": {"enabled": True, "last_success": None, "last_error": None},
        }}
        lines, stale = assess(report)
        self.assertEqual(stale, ["radarr", "sonarr"])
        self.assertEqual(lines, [
            "jellyfin: ok (10s ago)",
            "radarr: never looked",
            "seerr: disabled (no API key)",
            "sonarr: stale, last looked 400s ago — HTTP 500 from http://x",
        ])


class SelftestCountTests(unittest.TestCase):
    def test_selftest_samples_are_summed(self):
        text = ('homelab_usage_events_total{kind="selftest",service="relay",user="none"} 2\n'
                'homelab_usage_events_total{kind="request.created",service="seerr",user="a"} 7\n')
        self.assertEqual(selftest_count(text), 2.0)
        self.assertEqual(selftest_count(""), 0.0)


class DigestCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = make_settings(tmp.name)
        self.now = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)

    def seed(self):
        store = Store(self.settings.db_path)
        store.insert(Event(id="p1", ts="2026-10-04T20:00:00Z", service="jellyfin", kind="playback.started",
                           user="alice", title="Example Movie (2024)", detail={"item_id": "m1", "mode": "direct"}),
                     "sent")
        store.close()

    def test_the_report_is_written_and_published(self):
        self.seed()
        ntfy = FakeNtfy()
        self.assertEqual(cmd_digest(self.settings, ntfy, self.now), 0)
        report = Path(self.settings.report_path).read_text(encoding="utf-8")
        self.assertIn("Alice     1 play · 0.0h · Example Movie (2024)\n", report)
        self.assertEqual([(p.topic, p.message) for p in ntfy.sent], [("usage-digest", report)])

    def test_a_missing_database_publishes_nothing(self):
        ntfy = FakeNtfy()
        self.assertEqual(cmd_digest(self.settings, ntfy, self.now), 1)
        self.assertEqual(ntfy.sent, [])
        self.assertFalse(Path(self.settings.report_path).exists())

    def test_an_ntfy_failure_still_leaves_the_report(self):
        self.seed()
        ntfy = FakeNtfy()
        ntfy.down = True
        self.assertEqual(cmd_digest(self.settings, ntfy, self.now), 1)
        self.assertTrue(Path(self.settings.report_path).exists())


class SelftestCommandTests(unittest.TestCase):
    def start_relay(self, push: bool):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = make_settings(tmp.name).db_path
        self.store = Store(db_path)
        self.addCleanup(self.store.close)
        self.ntfy = FakeNtfy()
        metrics = Metrics()
        router = Router(store=self.store, topics=TOPICS, batch_window=120, resume_window=1800,
                        playback_expiry=600, push_ignore_users=frozenset())
        relay = Relay(store=self.store, router=router, ntfy=self.ntfy, metrics=metrics, collectors={},
                      alert_topic="homelab-alerts", alert_after=900, log=lambda _message: None)
        server = RelayHTTPServer(("127.0.0.1", 0), relay, metrics)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        if push:
            stop = threading.Event()

            def pusher():
                while not stop.wait(0.05):
                    relay.push_cycle()

            thread = threading.Thread(target=pusher, daemon=True)
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(stop.set)
        return make_settings(tmp.name, listen_port=server.server_address[1])

    def test_a_working_relay_passes_and_the_probe_row_is_removed(self):
        settings = self.start_relay(push=True)
        rc = cmd_selftest(settings, self.ntfy, timeout=5, sleep=lambda _s: time.sleep(0.05))
        self.assertEqual(rc, 0)
        self.assertIsNone(self.store.get(self.ntfy.sent[0].message))

    def test_a_relay_that_never_pushes_fails_and_still_cleans_up(self):
        settings = self.start_relay(push=False)
        rc = cmd_selftest(settings, self.ntfy, timeout=0.3, sleep=lambda _s: time.sleep(0.05))
        self.assertEqual(rc, 1)
        self.assertEqual(self.ntfy.sent, [])
        self.assertEqual(self.store.pending(), [])
```

- [ ] **Step 4: Run the tests and see them fail**

Run: `for t in config httpserver cli; do .venv/bin/python tests/validate_usage_relay.py $t; done`

Expected: all three FAIL with a missing-module error.

- [ ] **Step 5: Write `config.py`**

Create `roles/svc_infra/files/usage_relay/config.py`:

```python
"""Settings, rendered by Ansible into /etc/usage-relay/config.json.

No defaults live here: every value comes from inventory, so the deployed
behaviour is readable in git. A missing or malformed key fails at startup,
naming itself, rather than running with a guess.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .collect_arr import ArrCollector
from .collect_jellyfin import JellyfinCollector
from .collect_sabnzbd import SabnzbdCollector
from .collect_seerr import SeerrCollector
from .fetch import Fetch

TOPIC_KEYS = ("requests", "library", "playback", "failures", "digest", "selftest")
COLLECTOR_NAMES = ("jellyfin", "seerr", "sonarr", "radarr", "sabnzbd")


@dataclass(frozen=True)
class CollectorSettings:
    base_url: str
    interval: int
    key_env: str


@dataclass(frozen=True)
class Settings:
    db_path: str
    snapshot_path: str
    report_path: str
    listen_host: str
    listen_port: int
    push_interval: int
    maintenance_time: str
    topics: dict[str, str]
    batch_window: int
    resume_window: int
    playback_expiry: int
    collector_alert_after: int
    retention_days: int
    push_ignore_users: frozenset[str]
    collectors: dict[str, CollectorSettings]


def _require(raw: Mapping, key: str, where: str = "config"):
    if key not in raw:
        raise ValueError(f"{where} is missing {key!r}")
    return raw[key]


def from_dict(raw: Mapping) -> Settings:
    topics = {str(k): str(v) for k, v in dict(_require(raw, "topics")).items()}
    missing = [key for key in TOPIC_KEYS if not topics.get(key)]
    if missing:
        raise ValueError(f"config topics are missing {missing}")
    collectors: dict[str, CollectorSettings] = {}
    for name, entry in dict(_require(raw, "collectors")).items():
        if name not in COLLECTOR_NAMES:
            raise ValueError(f"unknown collector {name!r}; known: {', '.join(COLLECTOR_NAMES)}")
        where = f"collector {name!r}"
        collectors[name] = CollectorSettings(
            base_url=str(_require(entry, "base_url", where)).rstrip("/"),
            interval=int(_require(entry, "interval", where)),
            key_env=str(_require(entry, "key_env", where)),
        )
    maintenance_time = str(_require(raw, "maintenance_time"))
    if not re.fullmatch(r"[0-2]\d:[0-5]\d", maintenance_time):
        raise ValueError(f"maintenance_time must be HH:MM, not {maintenance_time!r}")
    return Settings(
        db_path=str(_require(raw, "db_path")),
        snapshot_path=str(_require(raw, "snapshot_path")),
        report_path=str(_require(raw, "report_path")),
        listen_host=str(_require(raw, "listen_host")),
        listen_port=int(_require(raw, "listen_port")),
        push_interval=int(_require(raw, "push_interval")),
        maintenance_time=maintenance_time,
        topics=topics,
        batch_window=int(_require(raw, "batch_window")),
        resume_window=int(_require(raw, "resume_window")),
        playback_expiry=int(_require(raw, "playback_expiry")),
        collector_alert_after=int(_require(raw, "collector_alert_after")),
        retention_days=int(_require(raw, "retention_days")),
        push_ignore_users=frozenset(str(u).strip().lower() for u in _require(raw, "push_ignore_users")),
        collectors=collectors,
    )


def load(path: str) -> Settings:
    with open(path, encoding="utf-8") as handle:
        return from_dict(json.load(handle))


def build_collectors(settings: Settings, env: Mapping[str, str], fetch: Fetch,
                     clock: Callable[[], float] = time.time) -> dict[str, object | None]:
    """One collector per configured name; None where its key is empty (disabled)."""
    built: dict[str, object | None] = {}
    for name, cfg in settings.collectors.items():
        key = env.get(cfg.key_env, "")
        if not key:
            built[name] = None
            continue
        common = {"base_url": cfg.base_url, "api_key": key, "fetch": fetch,
                  "interval": cfg.interval, "clock": clock}
        if name in ("sonarr", "radarr"):
            built[name] = ArrCollector(service=name, **common)
        elif name == "jellyfin":
            built[name] = JellyfinCollector(**common)
        elif name == "seerr":
            built[name] = SeerrCollector(**common)
        else:
            built[name] = SabnzbdCollector(**common)
    return built
```

- [ ] **Step 6: Write `httpserver.py`**

Create `roles/svc_infra/files/usage_relay/httpserver.py`:

```python
"""GET /metrics for Prometheus, GET /health for `check`, POST /selftest for `selftest`."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOOPBACK = frozenset({"127.0.0.1", "::1"})


class _Handler(BaseHTTPRequestHandler):
    server: RelayHTTPServer

    def do_GET(self) -> None:
        if self.path == "/metrics":
            self._reply(200, "text/plain; version=0.0.4; charset=utf-8", self.server.metrics.render())
        elif self.path == "/health":
            self._reply(200, "application/json", json.dumps(self.server.relay.health_report()))
        else:
            self._reply(404, "text/plain", "not found\n")

    def do_POST(self) -> None:
        if self.path != "/selftest":
            self._reply(404, "text/plain", "not found\n")
        elif self.client_address[0] not in LOOPBACK:
            # The port is open to Prometheus's address, and a selftest writes a
            # row. Loopback only, so only a shell on svc-infra can trigger one.
            self._reply(403, "text/plain", "selftest is loopback-only\n")
        else:
            self._reply(200, "application/json", json.dumps({"id": self.server.relay.selftest_insert()}))

    def _reply(self, code: int, content_type: str, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return  # one line per Prometheus scrape would bury the relay's own log


class RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], relay, metrics) -> None:
        self.relay = relay
        self.metrics = metrics
        super().__init__(address, _Handler)
```

- [ ] **Step 7: Write `cli.py` and `__main__.py`**

Create `roles/svc_infra/files/usage_relay/cli.py`:

```python
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
```

Create `roles/svc_infra/files/usage_relay/__main__.py`:

```python
"""`python3 -m usage_relay --config PATH <run|digest|selftest|check>`."""

from .cli import main

raise SystemExit(main())
```

- [ ] **Step 8: Run the tests and see them pass**

Run: `for t in config httpserver cli; do .venv/bin/python tests/validate_usage_relay.py $t || break; done`

Expected: `OK (8 tests)`, then `OK (4 tests)`, then `OK (7 tests)`.

- [ ] **Step 9: Smoke-test the entry point and run the whole suite plus lint**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=roles/svc_infra/files .venv/bin/python -m usage_relay --help
make validate-python validate-shell > /tmp/v.log 2>&1 </dev/null; echo exit=$?; grep -E 'Usage relay|error' /tmp/v.log
```

Expected: the help text lists `{run,digest,selftest,check}`, then `exit=0`
and `Usage relay: OK (114 tests)`.

- [ ] **Step 10: Commit**

```bash
git add roles/svc_infra/files/usage_relay/config.py roles/svc_infra/files/usage_relay/httpserver.py \
  roles/svc_infra/files/usage_relay/cli.py roles/svc_infra/files/usage_relay/__main__.py \
  tests/usage_relay/test_config.py tests/usage_relay/test_httpserver.py tests/usage_relay/test_cli.py
git commit -m "feat: add the usage relay CLI, config loader and HTTP endpoints"
git push
```

---

### Task 12: Deploy wiring (Ansible, systemd, firewall, Prometheus, backup, verify)

**Files:**
- Create: `roles/svc_infra/tasks/usage-relay.yml`
- Create: `roles/svc_infra/templates/usage-relay-config.json.j2`
- Create: `roles/svc_infra/templates/usage-relay-keys.env.j2`
- Create: `roles/svc_infra/files/usage-relay.service`
- Create: `roles/svc_infra/files/homelab-usage-digest.service`
- Create: `roles/svc_infra/files/homelab-usage-digest.timer`
- Modify: `roles/svc_infra/defaults/main.yml`
- Modify: `roles/svc_infra/tasks/main.yml`
- Modify: `roles/svc_infra/tasks/verify.yml`
- Modify: `roles/svc_infra/templates/prometheus.yml.j2`
- Modify: `inventory/host_vars/svc-infra.yml`
- Modify: `inventory/group_vars/all_vault.yml.example`

**Interfaces:**
- Consumes the CLI from Task 11 and the config keys from `config.from_dict`.
  The template must render every key that `from_dict` requires.
- Produces:
  - The inventory vars `usage_relay_port`, `usage_topics`,
    `usage_batch_window`, `usage_resume_window`, `usage_playback_expiry`,
    `usage_collector_alert_after`, `usage_retention_days`,
    `usage_push_ignore_users` and `usage_relay_cli`
  - The vault vars `vault_usage_{jellyfin,seerr,sonarr,radarr,sabnzbd}_api_key`

- [ ] **Step 1: Add the defaults**

In `roles/svc_infra/defaults/main.yml`, change

```yaml
infra_extra_backup_paths: [paperless, netbox, nextcloud]
```

to

```yaml
infra_extra_backup_paths: [paperless, netbox, nextcloud, usage-relay]
```

Append to the end of the same file:

```yaml

# ---------------------------------------------------------- usage relay ---
# Household usage notifications. The reasoning behind every number here is in
# docs/superpowers/specs/2026-10-06-usage-notifications-design.md.
usage_relay_port: 9470
usage_topics:
  requests: usage-requests
  library: usage-library
  playback: usage-playback
  failures: usage-failures
  digest: usage-digest
  selftest: usage-selftest
usage_batch_window: 120           # seconds a season pack is held to become one push
usage_resume_window: 1800         # re-starting the same item within this is a resume
usage_playback_expiry: 600        # playback pushes older than this after an ntfy outage are dropped
usage_collector_alert_after: 900  # a collector failing this long alerts homelab-alerts, once
usage_retention_days: 365         # household viewing history is bounded on purpose
# Jellyfin/Seerr usernames, lowercased. Their playback is recorded, never pushed.
usage_push_ignore_users: [brandon, admin]
# The relay's own CLI, run exactly as the unit runs it: same user, environment
# and state directory. verify.yml appends `selftest` or `check`.
usage_relay_cli:
  - systemd-run
  - --wait
  - --pipe
  - --quiet
  - --collect
  - --property=User=homelab
  - --property=Group=homelab
  - --property=EnvironmentFile=/etc/homelab-notify.env
  - --property=StateDirectory=usage-relay
  - --property=StateDirectoryMode=0750
  - --setenv=PYTHONPATH=/opt/usage-relay
  - --setenv=PYTHONDONTWRITEBYTECODE=1
  - /usr/bin/python3
  - -m
  - usage_relay
  - --config
  - /etc/usage-relay/config.json
```

- [ ] **Step 2: Write the two templates**

Create `roles/svc_infra/templates/usage-relay-config.json.j2`. JSON has no
comment syntax, so the provenance note is a Jinja comment.

```jinja
{#- Rendered by roles/svc_infra/tasks/usage-relay.yml; read by usage_relay/config.py,
    which rejects a missing key by name. Every value comes from inventory. -#}
{%- set _dl = hostvars[download_host].ansible_host -%}
{%- set config = {
  "db_path": "/var/lib/usage-relay/usage.db",
  "snapshot_path": "/opt/homelab/appdata/usage-relay/usage.snapshot.db",
  "report_path": scan_report_dir ~ "/usage.txt",
  "listen_host": "0.0.0.0",
  "listen_port": usage_relay_port,
  "push_interval": 10,
  "maintenance_time": "02:30",
  "topics": usage_topics,
  "batch_window": usage_batch_window,
  "resume_window": usage_resume_window,
  "playback_expiry": usage_playback_expiry,
  "collector_alert_after": usage_collector_alert_after,
  "retention_days": usage_retention_days,
  "push_ignore_users": usage_push_ignore_users,
  "collectors": {
    "jellyfin": {"base_url": "http://" ~ hostvars[media_host].ansible_host ~ ":8096",
                 "interval": 30, "key_env": "USAGE_JELLYFIN_API_KEY"},
    "seerr": {"base_url": "https://seerr." ~ service_domain,
              "interval": 60, "key_env": "USAGE_SEERR_API_KEY"},
    "sonarr": {"base_url": "http://" ~ _dl ~ ":" ~ download_apps.sonarr.ui_port,
               "interval": 60, "key_env": "USAGE_SONARR_API_KEY"},
    "radarr": {"base_url": "http://" ~ _dl ~ ":" ~ download_apps.radarr.ui_port,
               "interval": 60, "key_env": "USAGE_RADARR_API_KEY"},
    "sabnzbd": {"base_url": "http://" ~ _dl ~ ":" ~ download_apps.sabnzbd.ui_port,
                "interval": 60, "key_env": "USAGE_SABNZBD_API_KEY"}
  }
} -%}
{{ config | to_nice_json }}
```

Seerr is published on svc-media's loopback only (`PublishPort=127.0.0.1:5055`),
so the relay reaches it through Caddy at `https://seerr.<domain>`. That was
probed from svc-infra on 2026-10-06 and returned 200. Jellyfin, Sonarr,
Radarr, SABnzbd and ntfy were probed the same way and all returned 200.

Create `roles/svc_infra/templates/usage-relay-keys.env.j2`:

```jinja
{{ ansible_managed | comment }}
# Read-only API keys for the usage relay's collectors. An empty value disables
# that collector, and the relay and `make verify` both report it as disabled.
# 0600 root: systemd reads it before dropping to the homelab user.
USAGE_JELLYFIN_API_KEY={{ vault_usage_jellyfin_api_key | default('') | quote }}
USAGE_SEERR_API_KEY={{ vault_usage_seerr_api_key | default('') | quote }}
USAGE_SONARR_API_KEY={{ vault_usage_sonarr_api_key | default('') | quote }}
USAGE_RADARR_API_KEY={{ vault_usage_radarr_api_key | default('') | quote }}
USAGE_SABNZBD_API_KEY={{ vault_usage_sabnzbd_api_key | default('') | quote }}
```

- [ ] **Step 3: Write the three units**

Create `roles/svc_infra/files/usage-relay.service`:

```ini
# homelab-iac managed — roles/svc_infra/files/usage-relay.service
#
# Household usage relay: polls Jellyfin, Seerr, Sonarr, Radarr and SABnzbd,
# records to SQLite, pushes to ntfy, and serves /metrics on usage_relay_port.
# Design: docs/superpowers/specs/2026-10-06-usage-notifications-design.md
#
# User=homelab rather than a dedicated account: the nightly backup tars the
# snapshot directory as homelab through `podman unshare`, so the snapshot must
# be homelab's to read.
[Unit]
Description=Household usage relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=homelab
Group=homelab
EnvironmentFile=/etc/homelab-notify.env
EnvironmentFile=/etc/usage-relay/keys.env
Environment=PYTHONPATH=/opt/usage-relay
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 -m usage_relay --config /etc/usage-relay/config.json run
# A SQLite failure exits non-zero on purpose: a relay that pushed without
# recording would break "emit the number the alert used". systemd brings it
# back, and high-water marks plus dedup make the restart lossless.
Restart=on-failure
RestartSec=10
# The live database. Its snapshot goes to appdata (ReadWritePaths below) for
# the backup; the WAL file itself is never tarred.
StateDirectory=usage-relay
StateDirectoryMode=0750
ReadWritePaths=/opt/homelab/appdata/usage-relay
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
```

Create `roles/svc_infra/files/homelab-usage-digest.service`:

```ini
# homelab-iac managed — roles/svc_infra/files/homelab-usage-digest.service
[Unit]
Description=Weekly household usage digest
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# See homelab-alert-canary.service: a oneshot wedged in `activating` is not
# `failed`, so OnFailure= would never fire. One SQLite read and one ntfy POST.
TimeoutStartSec=120
User=homelab
Group=homelab
EnvironmentFile=/etc/homelab-notify.env
Environment=PYTHONPATH=/opt/usage-relay
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 -m usage_relay --config /etc/usage-relay/config.json digest
# Reading a WAL database needs write access to its -shm file, so the digest
# declares the relay's state directory too. scan-reports is scan_report_dir;
# usage.txt is published next to releases.txt.
StateDirectory=usage-relay
StateDirectoryMode=0750
ReadWritePaths=/opt/homelab/appdata/scan-reports
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
```

Create `roles/svc_infra/files/homelab-usage-digest.timer`:

```ini
# homelab-iac managed — roles/svc_infra/files/homelab-usage-digest.timer
[Unit]
Description=Publish the weekly household usage digest

[Timer]
# Sunday 18:00: the week is over and people are home to read it. Nothing else
# in the estate's schedule runs near it (see homelab-alert-canary.timer).
OnCalendar=Sun *-*-* 18:00:00
# A host that was off at 18:00 publishes when it returns. A late digest says
# "the host was down"; a missing one would read as "the timer is broken".
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Write the task file and import it**

Create `roles/svc_infra/tasks/usage-relay.yml`:

```yaml
---
# The household usage relay: polls Jellyfin, Seerr, Sonarr, Radarr and SABnzbd
# from svc-infra, records to SQLite, pushes to ntfy, serves /metrics.
# Design: docs/superpowers/specs/2026-10-06-usage-notifications-design.md
#
# Keys are optional one by one: an empty key disables that collector, and the
# relay and `make verify` report it as `disabled`, never as healthy.
- name: Refuse usage-relay API keys still set to a placeholder
  ansible.builtin.assert:
    that:
      - not ((vault_usage_jellyfin_api_key | default('')) is match('REPLACE_'))
      - not ((vault_usage_seerr_api_key | default('')) is match('REPLACE_'))
      - not ((vault_usage_sonarr_api_key | default('')) is match('REPLACE_'))
      - not ((vault_usage_radarr_api_key | default('')) is match('REPLACE_'))
      - not ((vault_usage_sabnzbd_api_key | default('')) is match('REPLACE_'))
    fail_msg: >-
      A vault_usage_*_api_key is still its all_vault.yml.example placeholder.
      Set the real key, or "" to disable that collector.
    quiet: true
  no_log: true

- name: Create the usage-relay code and config directories
  ansible.builtin.file:
    path: "{{ item }}"
    state: directory
    owner: root
    group: root
    mode: "0755"
  loop:
    - /opt/usage-relay
    - /opt/usage-relay/usage_relay
    - /etc/usage-relay

# The snapshot is the only file here, so the nightly backup (which tars this
# directory as homelab) can only ever capture a consistent copy.
- name: Create the usage-relay snapshot directory
  ansible.builtin.file:
    path: /opt/homelab/appdata/usage-relay
    state: directory
    owner: "{{ svc_uid }}"
    group: "{{ svc_gid }}"
    mode: "0750"

- name: Install the usage-relay package
  ansible.builtin.copy:
    src: "{{ item }}"
    dest: "/opt/usage-relay/usage_relay/{{ item | basename }}"
    owner: root
    group: root
    mode: "0644"
  with_fileglob: "usage_relay/*.py"
  register: usage_relay_code

- name: Render the usage-relay config
  ansible.builtin.template:
    src: usage-relay-config.json.j2
    dest: /etc/usage-relay/config.json
    owner: root
    group: root
    mode: "0644"
  register: usage_relay_config

- name: Render the usage-relay API keys
  ansible.builtin.template:
    src: usage-relay-keys.env.j2
    dest: /etc/usage-relay/keys.env
    owner: root
    group: root
    mode: "0600"
  no_log: true
  register: usage_relay_keys

- name: Install the usage-relay units
  ansible.builtin.copy:
    src: "{{ item }}"
    dest: "/etc/systemd/system/{{ item }}"
    owner: root
    group: root
    mode: "0644"
  loop:
    - usage-relay.service
    - homelab-usage-digest.service
    - homelab-usage-digest.timer
  register: usage_relay_units

# /metrics is a scrape target, not a UI: open it ONLY to Prometheus's host,
# exactly like node_exporter's :9100 in files.yml.
- name: Open the usage-relay metrics port to Prometheus only
  ansible.posix.firewalld:
    rich_rule: >-
      rule family=ipv4 source address={{ hostvars[infra_host].ansible_host }}/32
      port port={{ usage_relay_port }} protocol=tcp accept
    permanent: true
    immediate: true
    state: enabled

- name: Run the usage relay
  ansible.builtin.systemd:
    name: usage-relay.service
    enabled: true
    state: started
    daemon_reload: "{{ usage_relay_units is changed }}"
  when: not ansible_check_mode
  register: usage_relay_started

- name: Restart the usage relay when its code, config, keys or unit changed
  ansible.builtin.systemd:
    name: usage-relay.service
    state: restarted
  when:
    - not ansible_check_mode
    - usage_relay_started is not changed
    - (usage_relay_code is changed) or (usage_relay_config is changed)
      or (usage_relay_keys is changed) or (usage_relay_units is changed)

- name: Arm the weekly usage digest
  ansible.builtin.systemd:
    name: homelab-usage-digest.timer
    enabled: true
    state: started
  when: not ansible_check_mode
```

In `roles/svc_infra/tasks/main.yml`, insert the following directly after the
`Install the weekly alert-topic canary` import. It must stay above
`chat-egress.yml`; the comment there explains why.

```yaml
- name: Install the household usage relay
  ansible.builtin.import_tasks: usage-relay.yml
  tags: [usagerelay]
```

- [ ] **Step 5: Add the verify checks, the scrape job, OnFailure and the vault example**

Append to `roles/svc_infra/tasks/verify.yml`:

```yaml

# Usage relay, in two halves. selftest is the end-to-end positive control: a
# synthetic event must come back out of ntfy, move the counter and exist in
# SQLite (and is then removed). check is the "could not look" guard: every
# collector with a key must have polled successfully within 5 minutes, and a
# keyless one is printed as `disabled` rather than passed. Both run the relay's
# own CLI through systemd-run, so user, environment and sandbox match the unit.
- name: Prove the usage relay records, counts and pushes
  ansible.builtin.command:
    argv: "{{ usage_relay_cli + ['selftest'] }}"
  register: usage_relay_selftest
  changed_when: false

- name: Prove every enabled usage collector has looked recently
  ansible.builtin.command:
    argv: "{{ usage_relay_cli + ['check', '--wait', '90'] }}"
  register: usage_relay_check
  changed_when: false
```

In `roles/svc_infra/templates/prometheus.yml.j2`, append at the end of
`scrape_configs:`, after the `node` job's `{% endfor %}`:

```jinja

  # The household usage relay: a host service on svc-infra, not a container,
  # reached the same way as svc-infra's own node_exporter above.
  - job_name: usage-relay
    static_configs:
      - targets: ['host.containers.internal:{{ usage_relay_port }}']
        labels:
          instance: {{ infra_host }}
```

In `inventory/host_vars/svc-infra.yml`, append to the end of
`onfailure_units_extra:`, after `- homelab-image-edit@.service`:

```yaml
  # The household usage relay. Restart=on-failure handles a crash; this drop-in
  # fires when systemd gives up restarting it, which is the state where pushes
  # and the usage dashboard stop with nothing else saying so.
  - usage-relay.service
  # The weekly digest. A failed run means no Sunday message, which would
  # otherwise be indistinguishable from a quiet week.
  - homelab-usage-digest.service
```

In `inventory/group_vars/all_vault.yml.example`, insert directly after the
`vault_beszel_key:` line:

```yaml

# Usage relay (docs/superpowers/specs/2026-10-06-usage-notifications-design.md):
# read-only API keys it polls with. "" disables that collector — the relay and
# `make verify` report it as `disabled`, never as healthy. See docs/services.md
# "One-time UI wiring" for where each one is found.
vault_usage_jellyfin_api_key: ""   # Jellyfin -> Dashboard -> API Keys -> +
vault_usage_seerr_api_key:    ""   # Seerr -> Settings -> General -> API Key
vault_usage_sonarr_api_key:   ""   # Sonarr -> Settings -> General -> API Key
vault_usage_radarr_api_key:   ""   # Radarr -> Settings -> General -> API Key
vault_usage_sabnzbd_api_key:  ""   # SABnzbd -> Config -> General -> API Key
```

- [ ] **Step 6: Validate**

Run: `make validate > /tmp/validate.log 2>&1 </dev/null; echo exit=$?; grep -iE 'fail|error|usage' /tmp/validate.log | head -20`

Expected: `exit=0`. Specifically:
- `validate-systemd` parses the three new units, and `validate_onfailure`
  finds `homelab-usage-digest.service` covered on svc-infra.
- `validate_secret_tasks` accepts the `no_log` tasks.
- `validate_verify_safety` stays OK, because `systemd-run` is neither
  `systemctl start` nor `restart`.

If a gate fails, fix the cause it names. Do not loosen the gate.

- [ ] **Step 7: Commit**

```bash
git add roles/svc_infra/tasks/usage-relay.yml roles/svc_infra/templates/usage-relay-config.json.j2 \
  roles/svc_infra/templates/usage-relay-keys.env.j2 roles/svc_infra/files/usage-relay.service \
  roles/svc_infra/files/homelab-usage-digest.service roles/svc_infra/files/homelab-usage-digest.timer \
  roles/svc_infra/defaults/main.yml roles/svc_infra/tasks/main.yml roles/svc_infra/tasks/verify.yml \
  roles/svc_infra/templates/prometheus.yml.j2 inventory/host_vars/svc-infra.yml \
  inventory/group_vars/all_vault.yml.example
git commit -m "feat: deploy the usage relay on svc-infra with verify, backup and scrape wiring"
git push
```

---

### Task 13: The Household usage dashboard and its gate

**Files:**
- Create: `roles/svc_infra/files/grafana-dashboards/homelab-usage.json`
- Modify: `tests/validate_grafana_dashboards.py`

**Interfaces:**
- Consumes: `CATALOG` in `usage_relay/metrics.py` (Task 7). Its lines start
  at column 0 with the metric name.
- Each stat panel needs `noValue`, and every datasource uid is `prometheus`.
  Both rules are enforced by the gate.

- [ ] **Step 1: Teach the dashboard gate about the relay, and make it fail first**

In `tests/validate_grafana_dashboards.py`:

1. Add this entry to the end of the `EMITTER_PATHS` tuple:

   ```python
       # The usage relay serves /metrics itself rather than writing a textfile.
       # Its CATALOG lines start at column 0 with the metric name, so the
       # metric-line regex below reads it with no special case.
       "roles/svc_infra/files/usage_relay/metrics.py",
   ```

2. Replace the `NOT_METRICS` set with:

   ```python
   NOT_METRICS = {"sum", "topk", "time", "rate", "increase", "avg", "max", "min",
                  "count", "by", "without", "and", "or", "unless", "instance",
                  # Label names in `sum by (...)` clauses on the usage dashboard.
                  "user", "mode", "service", "collector", "topic"}
   ```

3. Add this to the end of `EXTRACTION_CASES`:

   ```python
       # A label in a by-clause next to a range selector, as the usage dashboard writes it.
       ('sum by (user) (increase(homelab_usage_events_total{kind="playback.started"}[1d]))',
        {"homelab_usage_events_total"}),
   ```

Write the dashboard (Step 2) with one deliberate typo first: in the "Playback
starts per person" panel, write `homelab_usage_event_total` (missing `s`).

Run: `.venv/bin/python tests/validate_grafana_dashboards.py`

Expected: FAIL, naming `'homelab_usage_event_total', which no emitter writes`.
This proves the gate reads the new file. Then fix the typo.

- [ ] **Step 2: Write the dashboard**

Create `roles/svc_infra/files/grafana-dashboards/homelab-usage.json`:

```json
{
  "uid": "homelab-usage",
  "title": "Household usage",
  "description": "Requests, imports, failures and playback recorded by the usage relay on svc-infra (roles/svc_infra/files/usage_relay). Managed by Ansible — UI edits are reverted on restart; copy it to a new name to experiment. Titles are deliberately absent: they live in the relay's SQLite log and the weekly digest at https://scan.<domain>/usage.txt.",
  "tags": ["homelab"],
  "timezone": "browser",
  "schemaVersion": 39,
  "version": 1,
  "refresh": "1m",
  "time": { "from": "now-30d", "to": "now" },
  "panels": [
    { "type": "row", "title": "Now", "collapsed": false, "gridPos": { "h": 1, "w": 24, "x": 0, "y": 0 }, "panels": [] },
    {
      "type": "stat", "title": "Streams now",
      "description": "0 when Jellyfin is being polled and nothing plays; blank when the relay itself is not reporting.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 4, "w": 6, "x": 0, "y": 1 },
      "fieldConfig": { "defaults": { "noValue": "relay not reporting", "color": { "mode": "thresholds" },
        "thresholds": { "mode": "absolute", "steps": [ { "color": "green", "value": null } ] } }, "overrides": [] },
      "options": { "colorMode": "value", "graphMode": "none", "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false } },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "sum(homelab_usage_playback_active) or (0 * homelab_usage_collector_last_success_timestamp{collector=\"jellyfin\"})" } ]
    },
    {
      "type": "stat", "title": "Transcoding now",
      "description": "Streams costing server CPU. Software transcode on svc-media: more than one 4K transcode at a time will stutter.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 4, "w": 6, "x": 6, "y": 1 },
      "fieldConfig": { "defaults": { "noValue": "relay not reporting", "color": { "mode": "thresholds" },
        "thresholds": { "mode": "absolute", "steps": [ { "color": "green", "value": null }, { "color": "orange", "value": 2 } ] } }, "overrides": [] },
      "options": { "colorMode": "value", "graphMode": "none", "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false } },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "sum(homelab_usage_playback_active{mode=\"transcode\"}) or (0 * homelab_usage_collector_last_success_timestamp{collector=\"jellyfin\"})" } ]
    },
    {
      "type": "stat", "title": "Seconds since each collector looked",
      "description": "Red past 300s: that collector is blind, which is different from the house being quiet.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 4, "w": 6, "x": 12, "y": 1 },
      "fieldConfig": { "defaults": { "noValue": "no collector has looked", "unit": "s", "color": { "mode": "thresholds" },
        "thresholds": { "mode": "absolute", "steps": [ { "color": "green", "value": null }, { "color": "red", "value": 300 } ] } }, "overrides": [] },
      "options": { "colorMode": "value", "graphMode": "none", "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false } },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "time() - homelab_usage_collector_last_success_timestamp", "legendFormat": "{{collector}}" } ]
    },
    {
      "type": "stat", "title": "Collector errors (24h)",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 4, "w": 6, "x": 18, "y": 1 },
      "fieldConfig": { "defaults": { "noValue": "relay not reporting", "color": { "mode": "thresholds" },
        "thresholds": { "mode": "absolute", "steps": [ { "color": "green", "value": null }, { "color": "red", "value": 1 } ] } }, "overrides": [] },
      "options": { "colorMode": "value", "graphMode": "none", "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false } },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "sum by (collector) (increase(homelab_usage_collector_errors_total[1d]))", "legendFormat": "{{collector}}" } ]
    },
    { "type": "row", "title": "Usage (rolling 24h)", "collapsed": false, "gridPos": { "h": 1, "w": 24, "x": 0, "y": 5 }, "panels": [] },
    {
      "type": "timeseries", "title": "Playback starts per person",
      "description": "Includes resumes and the owner's own viewing; the weekly digest counts distinct titles instead.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 6 },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "sum by (user) (increase(homelab_usage_events_total{kind=\"playback.started\"}[1d]))", "legendFormat": "{{user}}" } ]
    },
    {
      "type": "timeseries", "title": "Hours watched per person",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 6 },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "sum by (user) (increase(homelab_usage_watch_seconds_total[1d])) / 3600", "legendFormat": "{{user}}" } ]
    },
    {
      "type": "timeseries", "title": "Streams by play method",
      "description": "Direct play costs svc-media almost nothing; transcode is the CPU-bound path.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 14 },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "sum by (mode) (homelab_usage_playback_active)", "legendFormat": "{{mode}}" } ]
    },
    {
      "type": "timeseries", "title": "Requests per person",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 14 },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "sum by (user) (increase(homelab_usage_events_total{kind=\"request.created\"}[1d]))", "legendFormat": "{{user}}" } ]
    },
    {
      "type": "timeseries", "title": "Imports by service",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 22 },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "sum by (service) (increase(homelab_usage_events_total{kind=\"download.imported\"}[1d]))", "legendFormat": "{{service}}" } ]
    },
    {
      "type": "timeseries", "title": "Download failures by service",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 22 },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "sum by (service) (increase(homelab_usage_events_total{kind=\"download.failed\"}[1d]))", "legendFormat": "{{service}}" } ]
    },
    {
      "type": "timeseries", "title": "Push failures by topic",
      "description": "Non-zero means ntfy refused or was unreachable; pending pushes retry each cycle.",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 30 },
      "targets": [ { "refId": "A", "datasource": { "type": "prometheus", "uid": "prometheus" },
        "expr": "sum by (topic) (increase(homelab_usage_push_failures_total[1d]))", "legendFormat": "{{topic}}" } ]
    }
  ]
}
```

- [ ] **Step 3: Run the gate and see it pass**

Run: `.venv/bin/python tests/validate_grafana_dashboards.py; echo exit=$?`

Expected: `exit=0`.

- [ ] **Step 4: Commit**

```bash
git add roles/svc_infra/files/grafana-dashboards/homelab-usage.json tests/validate_grafana_dashboards.py
git commit -m "feat: add the Household usage Grafana dashboard"
git push
```

---

### Task 14: Documentation

**Files:**
- Modify: `docs/services.md`

- [ ] **Step 1: Add the one-time wiring step**

In `docs/services.md`, in the numbered list under `## One-time UI wiring`,
add after item 9 (RomM):

```markdown
10. For the usage relay, put five read-only API keys in the vault
    (`make vault-edit`). An empty key disables just that collector.
    - `vault_usage_jellyfin_api_key`: Jellyfin → Dashboard → API Keys → **+**
      (name it `usage-relay`).
    - `vault_usage_seerr_api_key`: Seerr → Settings → General → API Key.
    - `vault_usage_sonarr_api_key` and `vault_usage_radarr_api_key`: Settings
      → General → API Key in each app.
    - `vault_usage_sabnzbd_api_key`: SABnzbd → Config → General → API Key.

    Then run `make infra`.
```

- [ ] **Step 2: Add the section**

In `docs/services.md`, insert this section directly before
`## Seerr and RomM migration notes`:

```markdown
## Usage notifications

`usage-relay.service` on svc-infra records how the household uses the media
stack. Full design:
[usage notifications](superpowers/specs/2026-10-06-usage-notifications-design.md).

It polls five services: Jellyfin sessions every 30 s, and Seerr requests,
Sonarr and Radarr history, and SABnzbd history every 60 s. It does not
receive webhooks. The download apps live in the VPN jail, which cannot open
connections out to svc-infra, so the relay pulls from them instead.

Subscribe to these ntfy topics on `http://<svc-media>:8080`:

| Topic | What arrives | Suggested phone setting |
|---|---|---|
| `usage-requests` | Someone requested a movie or show | normal |
| `usage-library` | Something is ready to watch (season packs arrive as one message) | normal |
| `usage-playback` | Someone started playing something, with device and direct play vs transcode | low or silent |
| `usage-failures` | A grab, import or SABnzbd job failed | high |
| `usage-digest` | The weekly summary, Sunday 18:00 | normal |

Your own playback (`usage_push_ignore_users`) is recorded but never pushed. If
the relay itself cannot reach a service for 15 minutes, that goes to
`homelab-alerts`, not to a usage topic.

Where the data lives:

- **Dashboard:** Grafana's **Household usage** dashboard shows trends.
- **Digest:** the latest weekly digest is at `https://scan.<domain>/usage.txt`.
- **Database:**
  - The event log is SQLite at `/var/lib/usage-relay/usage.db`, kept for
    `usage_retention_days` (365).
  - A consistent snapshot is written nightly to
    `/opt/homelab/appdata/usage-relay/usage.snapshot.db`. That snapshot is
    what the backup tars.
  - To restore: stop the relay, copy the snapshot over `usage.db`, and start
    it again.

Checking it by hand, on svc-infra:

    sudo systemd-run --wait --pipe --quiet --collect -p User=homelab -p Group=homelab \
      -p EnvironmentFile=/etc/homelab-notify.env -p StateDirectory=usage-relay -p StateDirectoryMode=0750 \
      -E PYTHONPATH=/opt/usage-relay /usr/bin/python3 -m usage_relay \
      --config /etc/usage-relay/config.json check

`make verify` runs that `check`, plus a `selftest`. The selftest pushes a
synthetic event to `usage-selftest` and reads it back. A collector without a
key prints as `disabled`. It is never reported as a pass.
```

- [ ] **Step 3: Validate the links**

Run: `make validate-links > /tmp/l.log 2>&1 </dev/null; echo exit=$?; tail -3 /tmp/l.log`

Expected: `exit=0`.

- [ ] **Step 4: Commit**

```bash
git add docs/services.md
git commit -m "docs: document the usage relay, its topics and its keys"
git push
```

---

### Task 15: Live rollout, verification and merge

This task touches live infrastructure. Follow CLAUDE.md's change workflow
exactly. Each step says what must be observed. **A step whose observation
differs is a stop, not a retry.**

`$SCRATCH` below is any directory outside the repo for logs: the session
scratchpad, or `SCRATCH=$(mktemp -d)`. Shell state does not persist between
tool calls, so set it in each command that uses it.

- [ ] **Step 1: STOP for the human. The keys.**

Ask the user to do two things:
1. Create the Jellyfin API key: Dashboard → API Keys → **+**, named `usage-relay`.
2. Run `make vault-edit` and set the five `vault_usage_*_api_key` values. The
   sources are listed in `docs/services.md` under "One-time UI wiring", item 10.

Wait for confirmation. Never read, print or ask for the key values.

- [ ] **Step 2: Confirm the ignore list matches the real Jellyfin usernames**

Ask the user for the login name they watch Jellyfin under, as shown on
Jellyfin → Dashboard → Users. Usernames are not secrets. Lowercased, it must
appear in `usage_push_ignore_users` (default `[brandon, admin]`) in
`roles/svc_infra/defaults/main.yml`. If it does not, add it there and commit
before deploying. Otherwise every one of the owner's own plays pushes.

- [ ] **Step 3: First deploy (dirty tree is fine here)**

```bash
make infra USE_VAULT_FILE=1 > "$SCRATCH/infra1.log" 2>&1 </dev/null; echo exit=$?
grep -E 'usage|FAILED|fatal' "$SCRATCH/infra1.log" | head -30
```

Expected: the usage-relay tasks report `changed`, both
`Prove the usage relay …` tasks pass, and the selftest prints
`selftest OK: …`.

**Expected first-run failure:** `Assert every catalog infra backup artifact is
fresh and nonempty`. `usage-relay` is newly in `infra_extra_backup_paths`, and
no nightly backup has run since. If that is the only failure, take one backup
now and re-run:

```bash
.venv/bin/ansible svc-infra --vault-password-file .vault_pass -b -m ansible.builtin.systemd \
  -a 'name=backup-infra-appdata.service state=started' > "$SCRATCH/backup.log" 2>&1 </dev/null; echo exit=$?
make infra USE_VAULT_FILE=1 > "$SCRATCH/infra2.log" 2>&1 </dev/null; echo exit=$?
```

Expected: `exit=0` on both.

- [ ] **Step 4: Use the thing (CLAUDE.md: verification means the application works)**

Do each of these and record what was observed:

1. **Prometheus.** `curl -s http://<svc-infra>:9090/api/v1/targets` shows job
   `usage-relay` with health `up`. Grafana's **Household usage** dashboard
   renders, and "Seconds since each collector looked" shows five values under
   300.
2. **Playback push.** Ask the user to start playback on a **household**
   (non-ignored) account. A `usage-playback` message arrives within about
   40 s, with the right device and play method:
   `curl -s "http://<svc-media>:8080/usage-playback/json?poll=1&since=10m"`.
   Then the user plays on their own account: no push arrives, but
   `homelab_usage_events_total{kind="playback.started",user="<them>"}`
   increases.
3. **Restart is lossless.** On svc-infra, run `systemctl stop usage-relay`,
   wait 2 minutes, then run `systemctl start usage-relay`. Poll every usage
   topic since 10m: there must be no duplicate messages.
4. **Request flow.** If the user is willing, request something small in
   Seerr. `usage-requests` fires, and when it imports, `usage-library` fires
   with `requested by …`. If they don't want to now, record that this check
   was skipped.
5. **Digest.** On svc-infra, run `systemctl start homelab-usage-digest.service`.
   A `usage-digest` message arrives, and `https://scan.<domain>/usage.txt`
   serves the same text, ending in a `Read …` line.

- [ ] **Step 5: Commit any fixes, then confirm the tree is clean**

```bash
git status --porcelain   # must print nothing
```

- [ ] **Step 6: Final deploy proof from the clean tree**

```bash
make deploy-proof TARGET=infra USE_VAULT_FILE=1 > "$SCRATCH/proof1.log" 2>&1 </dev/null; echo exit=$?
make deploy-proof TARGET=infra USE_VAULT_FILE=1 > "$SCRATCH/proof2.log" 2>&1 </dev/null; echo exit=$?
make verify USE_VAULT_FILE=1 > "$SCRATCH/verify.log" 2>&1 </dev/null; echo exit=$?
```

Expected:
- The first proof may report only the three svc-infra runner-sync tasks.
- The second proof must be fully clean: `changed=0`.
- `make verify` exits 0.

Anything else means the deployed state and the commit differ, and the
difference must be explained before going further.

- [ ] **Step 7: STOP for the human. The merge carries the household guide.**

`feat/usage-relay` was cut from `feat/household-guide`, so merging it to
`main` also merges every unmerged household-guide commit. Ask the user which
they want:
- (a) Merge `feat/household-guide` to `main` first, then this branch.
- (b) Merge this branch alone, which brings the guide with it.
- (c) Hold this branch until the guide is ready.

Do not merge without an answer.

- [ ] **Step 8: Merge, push and delete branches** (after the answer in Step 7)

```bash
git switch main && git pull --ff-only
git merge --no-ff feat/usage-relay
git push
git branch -d feat/usage-relay docs/usage-notifications-spec
git push origin --delete feat/usage-relay docs/usage-notifications-spec
```

Then read CI for the push to `main`, using the `gh` recipe in
`docs/deployment.md`, quote-strip included. A red run is an alarm and needs a
follow-up commit.
