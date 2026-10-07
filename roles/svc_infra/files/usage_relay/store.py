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

    def count_by_label(self) -> list[tuple[str, str, str | None, int]]:
        """One row per (service, kind, user) seen in the log, with its total
        count -- how the relay re-seeds homelab_usage_events_total after a
        restart, so a Prometheus increase() does not lose a series' first
        sample every time the process restarts."""
        return [tuple(row) for row in self._rows(
            "SELECT service, kind, user, COUNT(*) FROM events GROUP BY service, kind, user"
        )]

    def watched_by_user(self) -> list[tuple[str | None, float]]:
        """Total watched_seconds per user, from playback.stopped rows --
        how the relay re-seeds homelab_usage_watch_seconds_total."""
        return [tuple(row) for row in self._rows(
            "SELECT user, COALESCE(SUM(json_extract(detail, '$.watched_seconds')), 0)"
            " FROM events WHERE kind = 'playback.stopped' GROUP BY user"
        )]

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
