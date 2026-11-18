#!/usr/bin/env python3
"""Probe, evict and free the Windows GPU host over HTTP (stdlib only).

ComfyUI (:8188) and Ollama (:11434) share one GPU. The nightly image checks
need room on it, and need to tell "the host is not there / not answering"
apart from "the feature is broken". So nothing here raises on a network,
HTTP or parse error: callers get False/None and decide what that means. A
raise would surface as exit 1, which the checks read as `broken`.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

VRAM_KEYS = ("vram_total", "vram_free", "torch_vram_total", "torch_vram_free")


def _request(url: str, timeout: float, payload: dict | None = None):
    """Return decoded JSON, or None on any failure. Non-2xx is a failure."""
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        return None
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        return None


def _post_ok(url: str, payload: dict, timeout: float) -> bool:
    """POST succeeded (2xx), regardless of whether the body is JSON."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        return False


def reachable(base_url: str, timeout: float = 5.0) -> bool:
    """True if something accepts a TCP connection at base_url's host:port."""
    try:
        u = urlparse(base_url)
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((u.hostname, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def comfy_queue_busy(base_url: str, timeout: float = 10.0) -> bool | None:
    d = _request(base_url.rstrip("/") + "/queue", timeout)
    if not isinstance(d, dict) or "queue_running" not in d or "queue_pending" not in d:
        return None
    try:
        return bool(d["queue_running"]) or bool(d["queue_pending"])
    except TypeError:
        return None


def comfy_vram(base_url: str, timeout: float = 10.0) -> dict[str, int] | None:
    d = _request(base_url.rstrip("/") + "/system_stats", timeout)
    try:
        dev = d["devices"][0]
        return {k: int(dev[k]) for k in VRAM_KEYS}
    except (TypeError, KeyError, IndexError, ValueError):
        return None


def comfy_free(base_url: str, settle: float = 20.0, timeout: float = 10.0) -> dict:
    """Ask ComfyUI to unload models and free memory, then watch torch's
    reservation settle. Stops when torch_vram_total stops decreasing for one
    poll, or after `settle` seconds."""
    base = base_url.rstrip("/")
    before = comfy_vram(base, timeout)
    posted = _post_ok(base + "/free", {"unload_models": True, "free_memory": True}, timeout)
    after = None
    if posted:
        deadline = time.monotonic() + settle
        prev = None
        interval = min(1.0, settle / 4)  # 1 s polls; shorter only for tiny test windows
        while True:
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            cur = comfy_vram(base, timeout)
            if cur is not None:
                after = cur
                if prev is not None and cur["torch_vram_total"] >= prev["torch_vram_total"]:
                    break
                prev = cur
            if time.monotonic() >= deadline:
                break
    return {"posted": posted, "before": before, "after": after}


def ollama_resident(base_url: str, timeout: float = 10.0) -> list[str] | None:
    d = _request(base_url.rstrip("/") + "/api/ps", timeout)
    try:
        return [str(m["name"]) for m in d["models"]]
    except (TypeError, KeyError):
        return None


def ollama_evict(base_url: str, timeout: float = 30.0, wait: float = 5.0) -> bool:
    """keep_alive 0 on every resident model; True only if /api/ps is then empty."""
    base = base_url.rstrip("/")
    names = ollama_resident(base, timeout)
    if names is None:
        return False
    if not names:
        return True
    for n in names:
        _post_ok(base + "/api/generate", {"model": n, "keep_alive": 0}, timeout)
    deadline = time.monotonic() + wait
    while True:
        left = ollama_resident(base, timeout)
        if left == []:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _mib(v: dict | None) -> str:
    """`<free>/<total> MiB free (torch <free>/<total> MiB free)`; the torch
    part is the allocator's reservation inside ComfyUI, which is what tells
    a /free that worked apart from one that left the reservation in place."""
    if v is None:
        return "?"
    out = f"{v['vram_free'] // 2**20}/{v['vram_total'] // 2**20} MiB free"
    if "torch_vram_free" in v and "torch_vram_total" in v:
        out += (f" (torch {v['torch_vram_free'] // 2**20}/"
                f"{v['torch_vram_total'] // 2**20} MiB free)")
    return out


def make_room(comfy_url: str, ollama_url: str, settle: float = 20.0) -> bool:
    """Evict Ollama, then free ComfyUI; True only if both were confirmed."""
    before = comfy_vram(comfy_url)
    ev = ollama_evict(ollama_url)
    print(f"gpu: ollama evict {'confirmed' if ev else 'NOT confirmed'}; "
          f"vram before {_mib(before)}", file=sys.stderr)
    r = comfy_free(comfy_url, settle=settle)
    ok = r["posted"] and r["after"] is not None
    print(f"gpu: comfy free {'confirmed' if ok else 'NOT confirmed'} "
          f"(posted={r['posted']}); vram {_mib(r['before'])} -> {_mib(r['after'])}",
          file=sys.stderr)
    return ev and ok
