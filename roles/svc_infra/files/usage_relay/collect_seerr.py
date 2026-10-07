"""Seerr requests: who asked for what, and when it was approved or available.

The request list is sorted by modification time, and anything modified since
the mark is re-examined. Re-emitting an event already recorded is harmless (the
store dedups on id); announcing an OLD request as new is not, so `created` is
only emitted when the request itself is newer than the mark. The title is
cosmetic, so a failed title lookup degrades to a placeholder rather than
failing the poll.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .fetch import Fetch, FetchError, ShapeError, snippet
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
            # The baseline must be at least the clock, not just the newest
            # existing row: an auto-approved request has createdAt ==
            # updatedAt, and if that row also happens to be the newest one,
            # a baseline of "latest" alone leaves the mark sitting exactly on
            # it -- the next poll's `updated < mark` check then lets it
            # through, and `created >= mark` announces it as new. Taking the
            # clock into account pushes the mark past every existing row (in
            # the ordinary case where requests predate the first poll), so
            # an unchanged request is excluded outright on the next poll.
            baseline = from_epoch(self._clock())
            return [], max(latest, baseline) if latest else baseline
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
            try:
                data = self._fetch(f"{self._base}/api/v1/{path}/{int(tmdb)}", self._headers)
                self._titles[key] = str(data[name_key])
            except (FetchError, ShapeError, KeyError, TypeError):
                return f"{media_type} tmdb:{int(tmdb)}"
        return self._titles[key]

    def _events_for(self, request: dict, mark: str) -> list[Event]:
        # Determine which events will be generated first
        event_specs: list[tuple[str, str, str]] = []
        if request["created"] >= mark:
            event_specs.append(("created", "request.created", request["created"]))
        if request["status"] == REQUEST_APPROVED:
            event_specs.append(("approved", "request.approved", request["updated"]))
        if request["media_status"] == MEDIA_AVAILABLE:
            event_specs.append(("available", "request.available", request["updated"]))

        # If there are no events, return early without fetching title
        if not event_specs:
            return []

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
        for suffix, kind, ts in event_specs:
            events.append(make(suffix, kind, ts))
        return events
