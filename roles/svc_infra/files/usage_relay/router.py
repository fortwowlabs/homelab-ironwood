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
