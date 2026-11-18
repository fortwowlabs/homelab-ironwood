#!/usr/bin/env python3
"""Offline test of the verdicts in the nightly image generation/edit checks.

Regression for 2026-11-17/18: ComfyUI was down, Open WebUI answered a generic
HTTP 400 after a long connect timeout, and the generation check read that as
`broken`. An unreachable or busy GPU host must read as `inconclusive` in
seconds, the card must be cleared first, and every HTTP error must be logged.
"""

from __future__ import annotations

import contextlib
import io
import sys
import time
from pathlib import Path

GATE_GROUP = "catalog"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import gpu_host  # noqa: E402
import image_edit_check  # noqa: E402
import image_generation_check  # noqa: E402
from gpu_fake import FakeGpu, closed_port_url  # noqa: E402

STATS = {"devices": [{"vram_total": 100, "vram_free": 40,
                      "torch_vram_total": 50, "torch_vram_free": 10}]}
IDLE = {"queue_running": [], "queue_pending": []}
BUSY = {"queue_running": [["x"]], "queue_pending": []}
MODEL = "gen-model.safetensors"
EDIT_MODEL = "edit-model.safetensors"


def png(w: int, h: int) -> bytes:
    import struct
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"


def history(unet: str) -> dict:
    return {"p1": {"prompt": [0, "p1", {"1": {
        "class_type": "UNETLoader", "inputs": {"unet_name": unet}}}]}}


def gpu_routes(f: FakeGpu, queue=IDLE) -> None:
    f.routes[("GET", "/queue")] = (200, queue)
    f.routes[("GET", "/system_stats")] = (200, STATS)
    f.routes[("POST", "/free")] = (200, {})
    f.routes[("GET", "/api/ps")] = (200, {"models": []})


def catalog(comfy: str, ollama: str) -> dict:
    return {"comfyui_base_url": comfy, "ollama_base_url": ollama,
            "image_generation_model": MODEL, "image_size": "1024x1024",
            "image_edit_model": EDIT_MODEL}


def run(module, f: FakeGpu, comfy: str, ollama: str):
    err = io.StringIO()
    t = time.monotonic()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        verdict, _ = module.probe(f.url, "tok", catalog(comfy, ollama), 10)
    return verdict, err.getvalue(), time.monotonic() - t


def gen_routes(f: FakeGpu) -> None:
    gpu_routes(f)
    f.routes[("GET", "/api/v1/images/models")] = (200, [{"id": MODEL}])
    f.routes[("POST", "/api/v1/images/generations")] = (200, [{"url": "/img"}])
    f.routes[("GET", "/img")] = (200, png(1024, 1024))


def paths(f: FakeGpu) -> list[tuple[str, str]]:
    return [(m, p) for m, p, _ in f.requests]


def t_gen_unreachable() -> str | None:
    with FakeGpu() as f:
        v, err, dt = run(image_generation_check, f, closed_port_url(), f.url)
    if v != "inconclusive" or dt >= 10 or "unreachable" not in err:
        return f"closed port: {v!r} in {dt:.1f}s, stderr={err!r}"
    return None


def t_gen_busy() -> str | None:
    with FakeGpu() as f:
        gen_routes(f)
        f.routes[("GET", "/queue")] = (200, BUSY)
        v, err, _ = run(image_generation_check, f, f.url, f.url)
        if v != "inconclusive" or ("POST", "/free") in paths(f) or "busy" not in err:
            return f"busy: {v!r}, requests={paths(f)}"
    return None


def t_gen_models_400() -> str | None:
    with FakeGpu() as f:
        gen_routes(f)
        f.routes[("GET", "/api/v1/images/models")] = (400, b'{"detail":"x"}')
        v, err, _ = run(image_generation_check, f, f.url, f.url)
    if v != "broken" or "HTTP 400" not in err or '{"detail":"x"}' not in err:
        return f"models 400: {v!r}, stderr={err!r}"
    return None


def t_gen_post_500() -> str | None:
    with FakeGpu() as f:
        gen_routes(f)
        f.routes[("POST", "/api/v1/images/generations")] = (500, b"boom")
        v, err, _ = run(image_generation_check, f, f.url, f.url)
        if v != "broken" or "HTTP 500" not in err or "boom" not in err:
            return f"room confirmed, 500: {v!r}, stderr={err!r}"
    with FakeGpu() as f:
        gen_routes(f)
        f.routes[("POST", "/api/v1/images/generations")] = (500, b"boom")
        v, err, _ = run(image_generation_check, f, f.url, closed_port_url())
        if v != "inconclusive" or "boom" not in err:
            return f"room unconfirmed, 500: {v!r}, stderr={err!r}"
    return None


def t_gen_happy() -> str | None:
    with FakeGpu() as f:
        gen_routes(f)
        v, err, _ = run(image_generation_check, f, f.url, f.url)
        p = paths(f)
        post = ("POST", "/api/v1/images/generations")
        if v != "ok" or post not in p:
            return f"happy: {v!r}, stderr={err!r}"
        i = p.index(post)
        if ("POST", "/free") not in p[:i] or ("GET", "/api/ps") not in p[:i]:
            return f"free/ps not before generation: {p}"
    return None


def edit_routes(f: FakeGpu) -> None:
    gpu_routes(f)
    f.routes[("POST", "/api/v1/images/edit")] = (200, [{"url": "/img"}])
    f.routes[("GET", "/img")] = (200, png(64, 64))
    f.routes[("GET", "/history")] = (200, history(EDIT_MODEL))


def t_edit() -> str | None:
    with FakeGpu() as f:
        v, err, dt = run(image_edit_check, f, closed_port_url(), f.url)
    if v != "inconclusive" or dt >= 10 or "unreachable" not in err:
        return f"edit closed port: {v!r}, stderr={err!r}"
    with FakeGpu() as f:
        edit_routes(f)
        f.routes[("GET", "/queue")] = (200, BUSY)
        v, _, _ = run(image_edit_check, f, f.url, f.url)
        if v != "inconclusive" or ("POST", "/free") in paths(f):
            return f"edit busy: {v!r}, requests={paths(f)}"
    with FakeGpu() as f:
        edit_routes(f)
        v, err, _ = run(image_edit_check, f, f.url, f.url)
        p = paths(f)
        post = ("POST", "/api/v1/images/edit")
        if v != "ok" or post not in p or ("POST", "/free") not in p[:p.index(post)]:
            return f"edit happy: {v!r}, requests={p}, stderr={err!r}"
    with FakeGpu() as f:
        edit_routes(f)
        f.routes[("POST", "/api/v1/images/edit")] = (400, b'{"detail":"y"}')
        v, err, _ = run(image_edit_check, f, f.url, f.url)
        if v != "inconclusive" or "HTTP 400" not in err or '{"detail":"y"}' not in err:
            return f"edit 400: {v!r}, stderr={err!r}"
    return None


def main() -> int:
    # Production settle is 20 s; the checks call make_room through the module.
    real = gpu_host.make_room
    gpu_host.make_room = lambda c, o, settle=0.2: real(c, o, settle=settle)
    failed = 0
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("t_"):
                msg = fn()
                if msg:
                    failed += 1
                    print(f"FAIL {name}: {msg}")
    finally:
        gpu_host.make_room = real
    if failed:
        return 1
    print("OK: image check verdicts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
