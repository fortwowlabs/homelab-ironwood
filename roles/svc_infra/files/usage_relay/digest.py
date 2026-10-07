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
