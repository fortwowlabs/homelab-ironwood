#!/usr/bin/env python3
"""Offline test of scripts/gpu_host.py against a fake ComfyUI/Ollama server.

The helper's contract is that it never raises into a check: a crash would exit
1, which the nightly checks read as "broken". So every failure shape here
(5xx, non-JSON, closed port) must come back as None/False, not an exception.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

GATE_GROUP = "catalog"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import gpu_host  # noqa: E402
from gpu_fake import FakeGpu, closed_port_url  # noqa: E402

STATS = {"devices": [{"vram_total": 100, "vram_free": 40,
                      "torch_vram_total": 50, "torch_vram_free": 10}]}
Q = ("GET", "/queue")
S = ("GET", "/system_stats")
PS = ("GET", "/api/ps")
GEN = ("POST", "/api/generate")


def t_reachable() -> str | None:
    t = time.monotonic()
    r = gpu_host.reachable(closed_port_url())
    if r is not False or time.monotonic() - t >= 6:
        return f"closed port: reachable={r!r} in {time.monotonic() - t:.1f}s"
    with FakeGpu() as f:
        if gpu_host.reachable(f.url) is not True:
            return "fake server not reachable"
    return None


def t_queue() -> str | None:
    cases = [
        ((200, {"queue_running": [], "queue_pending": []}), False),
        ((200, {"queue_running": [["x"]], "queue_pending": []}), True),
        ((200, {"queue_running": [], "queue_pending": [["y"]]}), True),
        ((500, {"error": "x"}), None),
        ((200, b"not json"), None),
    ]
    for resp, want in cases:
        with FakeGpu() as f:
            f.routes[Q] = resp
            got = gpu_host.comfy_queue_busy(f.url)
            if got is not want:
                return f"queue {resp!r}: got {got!r}, want {want!r}"
    if gpu_host.comfy_queue_busy(closed_port_url()) is not None:
        return "queue on closed port should be None"
    return None


def t_vram() -> str | None:
    with FakeGpu() as f:
        f.routes[S] = (200, STATS)
        want = STATS["devices"][0]
        if gpu_host.comfy_vram(f.url) != want:
            return f"vram parse: {gpu_host.comfy_vram(f.url)!r}"
    for resp in [(500, {}), (200, b"nope"), (200, {"devices": []}),
                 (200, {"devices": [{"vram_total": 1}]})]:
        with FakeGpu() as f:
            f.routes[S] = resp
            if gpu_host.comfy_vram(f.url) is not None:
                return f"vram {resp!r} should be None"
    return None


def t_free() -> str | None:
    with FakeGpu() as f:
        f.routes[S] = (200, STATS)
        f.routes[("POST", "/free")] = (200, {})
        r = gpu_host.comfy_free(f.url, settle=0.5)
        if ("POST", "/free", {"unload_models": True, "free_memory": True}) not in f.requests:
            return f"/free body not recorded: {f.requests!r}"
        if not (r["posted"] and r["before"] and r["after"]):
            return f"comfy_free result: {r!r}"
    with FakeGpu() as f:  # /free 500 -> not posted
        f.routes[S] = (200, STATS)
        f.routes[("POST", "/free")] = (500, {})
        if gpu_host.comfy_free(f.url, settle=0.2)["posted"]:
            return "500 on /free reported posted"
    r = gpu_host.comfy_free(closed_port_url(), settle=0.2)
    if r["posted"] or r["after"] is not None:
        return f"closed port comfy_free: {r!r}"
    return None


def t_evict() -> str | None:
    with FakeGpu() as f:
        f.routes[PS] = [(200, {"models": [{"name": "qwen3:32b"}]}),
                        (200, {"models": []})]
        f.routes[GEN] = (200, {})
        if gpu_host.ollama_evict(f.url) is not True:
            return "evict should succeed"
        if ("POST", "/api/generate", {"model": "qwen3:32b", "keep_alive": 0}) not in f.requests:
            return f"eviction POST missing: {f.requests!r}"
    with FakeGpu() as f:  # nothing resident -> True, no POST
        f.routes[PS] = (200, {"models": []})
        if gpu_host.ollama_evict(f.url) is not True or any(m == "POST" for m, _, _ in f.requests):
            return "empty ps should be True with no POST"
    with FakeGpu() as f:  # stuck
        f.routes[PS] = (200, {"models": [{"name": "m"}]})
        f.routes[GEN] = (200, {})
        if gpu_host.ollama_evict(f.url, wait=0.6) is not False:
            return "still-resident should be False"
    with FakeGpu() as f:  # bad ps
        f.routes[PS] = (200, b"junk")
        if gpu_host.ollama_resident(f.url) is not None or gpu_host.ollama_evict(f.url) is not False:
            return "non-JSON ps should be None / evict False"
    if gpu_host.ollama_evict(closed_port_url()) is not False:
        return "unreachable evict should be False"
    return None


def t_make_room() -> str | None:
    with FakeGpu() as c, FakeGpu() as o:
        c.routes[S] = (200, STATS)
        c.routes[("POST", "/free")] = (200, {})
        o.routes[PS] = (200, {"models": []})
        if gpu_host.make_room(c.url, o.url, settle=0.2) is not True:
            return "make_room should confirm"
        o.routes[PS] = (200, {"models": [{"name": "m"}]})
        o.routes[GEN] = (200, {})
        if gpu_host.make_room(c.url, o.url, settle=0.2) is not False:
            return "make_room with stuck ollama should be False"
    return None


def t_malformed_http() -> str | None:
    """A half-up host can answer with garbage; that must read as None/False."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    url = f"http://127.0.0.1:{srv.getsockname()[1]}"

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.recv(4096)
            c.sendall(b"GARBAGE\r\n\r\n")
            c.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        got = {
            "queue": gpu_host.comfy_queue_busy(url),
            "vram": gpu_host.comfy_vram(url),
            "ps": gpu_host.ollama_resident(url),
            "evict": gpu_host.ollama_evict(url),
            "free": gpu_host.comfy_free(url, settle=0.2)["posted"],
        }
    finally:
        srv.close()
    want = {"queue": None, "vram": None, "ps": None, "evict": False, "free": False}
    return None if got == want else f"malformed HTTP: {got!r}"


def main() -> int:
    failures = []
    for fn in (t_reachable, t_queue, t_vram, t_free, t_evict, t_make_room, t_malformed_http):
        try:
            err = fn()
        except Exception as e:  # a raise is itself the defect under test
            err = f"raised {type(e).__name__}: {e}"
        if err:
            failures.append(f"{fn.__name__}: {err}")
    for f in failures:
        print(f"FAIL {f}", file=sys.stderr)
    print("OK gpu_host" if not failures else f"{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
