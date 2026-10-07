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
