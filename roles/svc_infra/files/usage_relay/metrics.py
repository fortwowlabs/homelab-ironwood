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
