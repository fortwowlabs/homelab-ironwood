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
import os
import sys
import tempfile
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
MIB = 2**20
BIG_STATS = {"devices": [{"vram_total": 24000 * MIB, "vram_free": 4000 * MIB,
                          "torch_vram_total": 20000 * MIB, "torch_vram_free": 1000 * MIB}]}
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


def t_gen_wedged_comfy() -> str | None:
    """TCP accepts but HTTP does not answer: Open WebUI's 400 is not ours."""
    with FakeGpu() as f:
        gen_routes(f)
        f.routes[("GET", "/queue")] = (500, b"wedged")
        f.routes[("GET", "/api/v1/images/models")] = (400, b'{"detail":"x"}')
        v, err, _ = run(image_generation_check, f, f.url, f.url)
        if v != "inconclusive" or "HTTP layer did not answer" not in err or "HTTP 400" not in err:
            return f"wedged comfy: {v!r}, stderr={err!r}"
    return None


def t_make_room_logs_torch() -> str | None:
    with FakeGpu() as c, FakeGpu() as o:
        gpu_routes(c)
        gpu_routes(o)
        c.routes[("GET", "/system_stats")] = (200, BIG_STATS)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            gpu_host.make_room(c.url, o.url, settle=0.2)
        want = ("vram 4000/24000 MiB free (torch 1000/20000 MiB free) -> "
                "4000/24000 MiB free (torch 1000/20000 MiB free)")
        if want not in err.getvalue():
            return f"torch figures missing from make_room log: {err.getvalue()!r}"
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


def release(url: str | None, catalog_text: str | None = None, missing: bool = False,
            extra: tuple[str, ...] = (), check: str = "gen", result: str = "success",
            marker: bool = False):
    """Run scripts/gpu_host_release.py as the unit would; (rc, combined output)."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cat = Path(d) / ("nope.yml" if missing else "images.yml")
        if not missing:
            cat.write_text(catalog_text if catalog_text is not None
                           else f"comfyui_base_url: {url}\n"
                           "image_generation_enabled: true\nimage_edit_enabled: true\n")
        rt = Path(d) / "rt"
        rt.mkdir()
        if marker:
            (rt / "submitted").write_text("1\n")
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "gpu_host_release.py"),
             "--catalog", str(cat), "--settle", "0.2",
             "--drain", "1", "--poll", "0.2",
             "--check", check, "--service-result", result,
             "--runtime-dir", str(rt), *extra],
            capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


def t_release_unreachable() -> str | None:
    rc, out = release(closed_port_url())
    if rc != 0 or "unreachable" not in out:
        return f"rc={rc} out={out!r}"
    return None


def t_release_busy() -> str | None:
    # Result exit-code: the check never owned the queue. No wait, no /free.
    with FakeGpu() as f:
        gpu_routes(f, BUSY)
        t = time.monotonic()
        rc, out = release(f.url, result="exit-code", extra=("--drain", "30"))
        dt = time.monotonic() - t
        if rc != 0 or "someone else's job; models left resident" not in out \
                or ("POST", "/free") in paths(f) or dt > 10 \
                or paths(f).count(("GET", "/queue")) != 1:
            return f"exit-code busy: rc={rc} dt={dt:.1f} out={out!r} requests={paths(f)}"
    # Result timeout: our own prompt may still be queued, so wait, then give up.
    with FakeGpu() as f:
        gpu_routes(f, BUSY)
        rc, out = release(f.url, result="timeout")
        if rc != 0 or "still busy after 1s; models left resident" not in out \
                or ("POST", "/free") in paths(f):
            return f"timeout busy: rc={rc} out={out!r} requests={paths(f)}"
    return None


def t_release_drains() -> str | None:
    with FakeGpu() as f:
        gpu_routes(f)
        f.routes[("GET", "/queue")] = [(200, BUSY), (200, BUSY), (200, IDLE)]
        rc, out = release(f.url, result="timeout", extra=("--drain", "10"))
        if rc != 0 or ("POST", "/free") not in paths(f):
            return f"timeout drain: rc={rc} out={out!r} requests={paths(f)}"
    # The same busy-then-idle queue under any other result must NOT be drained.
    with FakeGpu() as f:
        gpu_routes(f)
        f.routes[("GET", "/queue")] = [(200, BUSY), (200, BUSY), (200, IDLE)]
        rc, out = release(f.url, result="exit-code", extra=("--drain", "10"))
        if rc != 0 or ("POST", "/free") in paths(f):
            return f"exit-code no-drain: rc={rc} out={out!r} requests={paths(f)}"
    return None


def t_release_marker_drains() -> str | None:
    """Result success (the check's own client timed out) but our prompt was
    submitted: drain the busy queue, then free."""
    with FakeGpu() as f:
        gpu_routes(f)
        f.routes[("GET", "/queue")] = [(200, BUSY), (200, BUSY), (200, IDLE)]
        rc, out = release(f.url, result="success", marker=True, extra=("--drain", "10"))
        p = paths(f)
        if rc != 0 or ("POST", "/free") not in p or p.count(("GET", "/queue")) < 3:
            return f"marker drain: rc={rc} out={out!r} requests={p}"
    with FakeGpu() as f:
        gpu_routes(f)
        f.routes[("GET", "/queue")] = [(200, BUSY), (200, BUSY), (200, IDLE)]
        rc, out = release(f.url, result="success", marker=False, extra=("--drain", "10"))
        p = paths(f)
        if rc != 0 or ("POST", "/free") in p or p.count(("GET", "/queue")) != 1 \
                or "someone else's job" not in out:
            return f"no marker, success, busy: rc={rc} out={out!r} requests={p}"
    return None


def with_runtime_dir(value: str, fn):
    old = os.environ.get("RUNTIME_DIRECTORY")
    os.environ["RUNTIME_DIRECTORY"] = value
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("RUNTIME_DIRECTORY", None)
        else:
            os.environ["RUNTIME_DIRECTORY"] = old


def t_marker_written_on_submit() -> str | None:
    for name, module, routes in (("gen", image_generation_check, gen_routes),
                                 ("edit", image_edit_check, edit_routes)):
        with tempfile.TemporaryDirectory() as d, FakeGpu() as f:
            routes(f)
            v, _, _ = with_runtime_dir(d, lambda m=module, g=f: run(m, g, g.url, g.url))
            if v != "ok":
                return f"{name}: verdict {v!r}"
            if not (Path(d) / "submitted").exists():
                return f"{name}: no submitted marker after a submitted request"
    return None


def t_marker_not_written_when_unreachable() -> str | None:
    for module in (image_generation_check, image_edit_check):
        with tempfile.TemporaryDirectory() as d, FakeGpu() as f:
            v, _, _ = with_runtime_dir(
                d, lambda m=module, g=f: run(m, g, closed_port_url(), g.url))
            if v != "inconclusive" or (Path(d) / "submitted").exists():
                return f"{module.__name__}: verdict {v!r}, marker={list(Path(d).iterdir())}"
    return None


def t_marker_failure_never_changes_verdict() -> str | None:
    """RUNTIME_DIRECTORY pointing nowhere: logged, verdict untouched."""
    with FakeGpu() as f:
        gen_routes(f)
        v, err, _ = with_runtime_dir(
            "/nonexistent/dir/for/marker",
            lambda: run(image_generation_check, f, f.url, f.url))
    if v != "ok" or "marker" not in err:
        return f"unwritable marker dir: {v!r}, stderr={err!r}"
    return None


def units_wiring_problem(units: dict[str, str]) -> str | None:
    import re
    for check, text in units.items():
        stop = [ln for ln in text.splitlines() if ln.startswith("ExecStopPost=")]
        if len(stop) != 1 or not re.search(
                rf"gpu_host_release\.py\b.*--check {check}\b", stop[0]):
            return f"{check}: ExecStopPost does not pass --check {check}: {stop}"
        if not re.search(r"^RuntimeDirectory=\S+", text, re.M):
            return f"{check}: unit declares no RuntimeDirectory="
    return None


def t_units_wire_release_and_runtime_dir() -> str | None:
    """Gate: the release hook must name its own check and the unit must
    export RUNTIME_DIRECTORY, or the marker silently never reaches it."""
    files = ROOT / "roles" / "svc_infra" / "files"
    return units_wiring_problem({
        "gen": (files / "homelab-image-gen@.service").read_text(),
        "edit": (files / "homelab-image-edit@.service").read_text()})


def t_units_gate_can_fail() -> str | None:
    """Positive control: the gate above must reject each defect."""
    files = ROOT / "roles" / "svc_infra" / "files"
    gen = (files / "homelab-image-gen@.service").read_text()
    edit = (files / "homelab-image-edit@.service").read_text()
    good = {"gen": gen, "edit": edit}

    def drop(text: str, prefix: str) -> str:
        return "\n".join(ln for ln in text.splitlines() if not ln.startswith(prefix))

    for label, bad in (
            ("gen RuntimeDirectory removed", {**good, "gen": drop(gen, "RuntimeDirectory=")}),
            ("edit RuntimeDirectory removed", {**good, "edit": drop(edit, "RuntimeDirectory=")}),
            ("gen wrong --check", {**good, "gen": gen.replace("--check gen", "--check edit")}),
            ("edit wrong --check", {**good, "edit": edit.replace("--check edit", "--check gen")}),
            ("gen ExecStopPost removed", {**good, "gen": drop(gen, "ExecStopPost=")})):
        if not units_wiring_problem(bad):
            return f"gate did not fail for: {label}"
    return None


def t_release_deadline() -> str | None:
    """A long drain is cut off by the single overall --deadline."""
    with FakeGpu() as f:
        gpu_routes(f, BUSY)
        t = time.monotonic()
        rc, out = release(f.url, result="timeout",
                          extra=("--drain", "100", "--deadline", "1"))
        dt = time.monotonic() - t
        if rc != 0 or dt > 15 or ("POST", "/free") in paths(f) \
                or "models left resident" not in out:
            return f"deadline: rc={rc} dt={dt:.1f} out={out!r}"
    return None


def t_release_feature_disabled() -> str | None:
    for check, flag in (("gen", "image_generation_enabled"), ("edit", "image_edit_enabled")):
        with FakeGpu() as f:
            gpu_routes(f)
            text = (f"comfyui_base_url: {f.url}\nimage_generation_enabled: true\n"
                    f"image_edit_enabled: true\n").replace(f"{flag}: true", f"{flag}: false")
            rc, out = release(f.url, catalog_text=text, check=check, result="timeout")
            if rc != 0 or f.requests or "disabled" not in out:
                return f"{check} disabled: rc={rc} out={out!r} requests={paths(f)}"
    return None


def t_release_flag_scoped() -> str | None:
    """Gen disabled does not suppress the edit release (flags are per check)."""
    with FakeGpu() as f:
        gpu_routes(f)
        text = (f"comfyui_base_url: {f.url}\nimage_generation_enabled: false\n"
                "image_edit_enabled: true\n")
        rc, out = release(f.url, catalog_text=text, check="edit")
        if rc != 0 or ("POST", "/free") not in paths(f):
            return f"edit with gen disabled: rc={rc} out={out!r}"
    return None


def t_release_unreadable() -> str | None:
    with FakeGpu() as f:
        gpu_routes(f)
        f.routes[("GET", "/queue")] = (500, {})
        rc, out = release(f.url)
        if rc != 0 or "queue state unreadable; models left resident" not in out \
                or ("POST", "/free") in paths(f):
            return f"rc={rc} out={out!r} requests={paths(f)}"
    return None


def t_release_idle() -> str | None:
    with FakeGpu() as f:
        gpu_routes(f)
        rc, out = release(f.url)
        if rc != 0 or ("POST", "/free") not in paths(f) or "before" not in out or "after" not in out:
            return f"rc={rc} out={out!r} requests={paths(f)}"
    with FakeGpu() as f:
        gpu_routes(f)
        f.routes[("GET", "/system_stats")] = (200, BIG_STATS)
        rc, out = release(f.url)
        want = "before 4000/24000 MiB free (torch 1000/20000 MiB free)"
        if want not in out:
            return f"torch figures missing from release line: {out!r}"
    return None


def t_release_bad_catalog() -> str | None:
    for kw in ({"missing": True}, {"catalog_text": "foo: [unclosed\n  - : :"},
               {"catalog_text": "other_key: 1\n"}):
        rc, out = release(None, **kw)
        if rc != 0 or not out.strip():
            return f"{kw}: rc={rc} out={out!r}"
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
