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
