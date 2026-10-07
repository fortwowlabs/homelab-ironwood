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
