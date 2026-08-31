#!/usr/bin/env python3
"""Edit one image end to end and prove OUR workflow produced it.

Generation's runtime check proves itself by asserting an exact output size —
the compiled-in default is 512x512 and ours is 1024x1024, so a size match is
strong evidence our mapping reached ComfyUI. Editing has no such fixed target:
this workflow derives its output size from the input image
(ImageScaleToTotalPixels), so a size assertion here would prove nothing.
Instead this check reads back ComfyUI's own /history for the executed prompt
and asserts the UNETLoader node's unet_name equals IMAGE_EDIT_MODEL -- direct
proof our checkpoint executed, not merely that *an* image came back.

Verdicts are tri-state, same convention as image_generation_check.py:
`inconclusive` means could-not-look and escalates rather than passing.

Design: docs/superpowers/specs/2026-08-27-comfyui-image-editing-design.md
"""

from __future__ import annotations

import argparse
import base64
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from owui_image_config import api_get, api_post  # noqa: E402

CATALOG_PATH = ROOT / "inventory" / "group_vars" / "all" / "images.yml"
PROMPT = "make the background bright red"


def tiny_png(width: int = 64, height: int = 64,
             rgb: tuple[int, int, int] = (120, 90, 200)) -> bytes:
    """A minimal valid PNG built from the stdlib alone -- no test fixture,
    no PIL dependency, deterministic bytes every run."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    idat = chunk(b"IDAT", zlib.compress(raw, 9))
    return sig + ihdr + idat + chunk(b"IEND", b"")


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def probe(base_url: str, token: str, catalog: dict,
          timeout: int) -> tuple[str, dict[str, float]]:
    expected_model = catalog["image_edit_model"]
    started = time.monotonic()

    image_b64 = base64.b64encode(tiny_png()).decode("ascii")
    payload = {"image": f"data:image/png;base64,{image_b64}", "prompt": PROMPT}

    try:
        result = api_post(base_url, "/api/v1/images/edit", token, payload, timeout)
    except urllib.error.HTTPError:
        # No documented status code here means "the edit mapping is
        # specifically broken" the way generation's 400 does, so default
        # conservatively: any HTTP error is could-not-look, not "broken".
        return "inconclusive", {}
    except (urllib.error.URLError, OSError, ValueError):
        return "inconclusive", {}

    if not isinstance(result, list) or not result:
        print("FAIL: edit returned no image", file=sys.stderr)
        return "broken", {"homelab_image_edit_ok": 0.0}

    url = result[0].get("url", "")
    if url.startswith("/"):
        url = f"{base_url.rstrip('/')}{url}"
    try:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return "inconclusive", {}

    try:
        width, height = png_size(data)
    except ValueError:
        print(f"FAIL: returned {len(data)} bytes that are not a PNG",
              file=sys.stderr)
        return "broken", {"homelab_image_edit_ok": 0.0}

    duration = time.monotonic() - started
    metrics = {
        "homelab_image_edit_duration_seconds": round(duration, 2),
        "homelab_image_edit_width": float(width),
    }

    # The direct proof: read ComfyUI's own history for the most recent
    # prompt and confirm OUR checkpoint executed, not merely that an image
    # of some size came back. This is the PRIMARY proof mechanism for
    # editing -- there's no dimension-based fallback the way generation has
    # (that check asserts an exact 1024x1024 output; this workflow's output
    # size is derived from the input image, so a size match here would prove
    # nothing). So failing to confirm is treated as could-not-look, not as
    # success: anything short of a confirmed True downgrades the verdict to
    # inconclusive rather than falling through to ok.
    try:
        history = api_get(catalog["comfyui_base_url"], "/history", token="",
                          timeout=timeout)
    except Exception:
        history = None
    ran_expected_model = None
    if isinstance(history, dict) and history:
        latest = list(history.values())[-1]
        prompt_graph = latest.get("prompt", [None, None, {}])[2]
        for node in prompt_graph.values():
            if node.get("class_type") == "UNETLoader":
                ran_expected_model = (
                    node.get("inputs", {}).get("unet_name") == expected_model)

    if (width, height) == (0, 0):
        metrics["homelab_image_edit_ok"] = 0.0
        return "broken", metrics

    if ran_expected_model is False:
        print(f"FAIL: ComfyUI's history shows a different UNETLoader model "
              f"than {expected_model!r} -- our mapping did not reach it",
              file=sys.stderr)
        metrics["homelab_image_edit_ok"] = 0.0
        return "broken", metrics

    if ran_expected_model is None:
        print("INCONCLUSIVE: could not confirm from ComfyUI's history that "
              f"our checkpoint ({expected_model!r}) executed -- the history "
              "fetch failed, came back empty, or had no UNETLoader node in "
              "the latest prompt graph. An image did come back, but that is "
              "not proof our mapping produced it.", file=sys.stderr)
        return "inconclusive", {}

    metrics["homelab_image_edit_ok"] = 1.0
    print(f"image edit OK: {width}x{height} PNG, {len(data)} bytes, "
          f"{duration:.1f}s, model confirmed={ran_expected_model}")
    return "ok", metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://192.168.1.32:3007")
    parser.add_argument("--token-file", metavar="FILE")
    parser.add_argument("--metrics-dir", metavar="DIR")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    else:
        token = os.environ.get("OWUI_ADMIN_TOKEN", "").strip()
    if not token:
        print("no admin token: set OWUI_ADMIN_TOKEN or pass --token-file",
              file=sys.stderr)
        return 1

    catalog = yaml.safe_load(CATALOG_PATH.read_text())
    if not catalog.get("image_edit_enabled"):
        print("image_edit_enabled is false in images.yml — the feature is "
              "disabled, so this run could not look and is not an all-clear")
        return 2

    verdict, metrics = probe(args.base_url, token, catalog, args.timeout)

    if args.metrics_dir and metrics:
        lines = "".join(f"{name} {value}\n" for name, value in sorted(metrics.items()))
        try:
            subprocess.run(
                ["homelab-metric-write", "--dir", args.metrics_dir,
                 "--file", "image-edit", "--prefix", "homelab_image_edit",
                 *(["--success"] if verdict == "ok" else [])],
                input=lines, text=True, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            print(f"failed to publish metrics: {error}", file=sys.stderr)

    print(f"verdict={verdict}")
    return {"ok": 0, "broken": 1, "inconclusive": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
