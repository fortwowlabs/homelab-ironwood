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
