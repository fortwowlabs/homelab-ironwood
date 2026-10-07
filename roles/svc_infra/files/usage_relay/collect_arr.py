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
