#!/usr/bin/env python3
"""Generate one image end to end and prove OUR workflow produced it.

A green container, an active unit and a 200 together prove a process started.
This feature spent days looking exactly like that while never once reaching
ComfyUI. So this check generates a real image, fetches the bytes, and asserts
they are a PNG of the expected size.

The size assertion is the load-bearing one. Open WebUI has a workflow compiled
in whose EmptyLatentImage is 512x512; that is what was being submitted during
the defect. A 1024x1024 result therefore proves the width/height mapping
reached ComfyUI -- not merely that an image appeared.

Verdicts are tri-state. `inconclusive` means could-not-look (GPU host asleep,
Open WebUI unreachable, auth rejected, timeout) and escalates rather than
passing, because "could not look" reading as "fine" is how every other check
in this repo has failed at least once.

Design: docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from owui_image_config import api_get, api_post  # noqa: E402

CATALOG_PATH = ROOT / "inventory" / "group_vars" / "all" / "images.yml"
PROMPT = "a plain grey ceramic mug on a wooden table, soft daylight"


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG IHDR. Raises ValueError if not a PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def probe(base_url: str, token: str, catalog: dict,
          timeout: int) -> tuple[str, dict[str, float]]:
    expected_model = catalog["image_generation_model"]
    expected_w, expected_h = (int(n) for n in catalog["image_size"].split("x"))
    started = time.monotonic()

    # Cheapest first: this evaluates workflow[model_node_id]["class_type"]
    # internally, so a mapping naming an absent node 400s here rather than
    # failing silently later. It also returns ComfyUI's live checkpoint list.
    try:
        models = api_get(base_url, "/api/v1/images/models", token, timeout)
    except urllib.error.HTTPError as error:
        if error.code == 400:
            # Broken mapping normally surfaces as a 400 here.
            return "broken", {}
        return "inconclusive", {}
    except (urllib.error.URLError, OSError, ValueError):
        return "inconclusive", {}

    available = {m.get("id") for m in models} if isinstance(models, list) else set()
    if expected_model not in available:
        print(f"FAIL: {expected_model!r} is not among the checkpoints ComfyUI "
              f"offers ({sorted(available)})", file=sys.stderr)
        return "broken", {}

    try:
        result = api_post(base_url, "/api/v1/images/generations", token,
                          {"prompt": PROMPT, "n": 1}, timeout)
    except urllib.error.HTTPError:
        return "broken", {}
    except (urllib.error.URLError, OSError, ValueError):
        return "inconclusive", {}

    if not isinstance(result, list) or not result:
        print("FAIL: generation returned no image. comfyui_create_image "
              "swallows every exception into None, which can arise from a broken "
              "mapping (500+ error from ComfyUI wraps as a 400), an empty list, "
              "or other failures upstream", file=sys.stderr)
        return "broken", {}

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
        return "broken", {}

    duration = time.monotonic() - started
    metrics = {
        "homelab_image_generation_duration_seconds": round(duration, 2),
        "homelab_image_generation_width": float(width),
    }

    if (width, height) != (expected_w, expected_h):
        print(f"FAIL: image is {width}x{height}, expected "
              f"{expected_w}x{expected_h}. Open WebUI's compiled-in default "
              "workflow is 512x512 — this size means the width/height mapping "
              "never reached ComfyUI and the default workflow ran",
              file=sys.stderr)
        metrics["homelab_image_generation_ok"] = 0.0
        return "broken", metrics

    metrics["homelab_image_generation_ok"] = 1.0
    print(f"image generation OK: {width}x{height} PNG, {len(data)} bytes, "
          f"{duration:.1f}s")
    return "ok", metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://192.168.1.32:3007")
    parser.add_argument("--token-file", metavar="FILE")
    parser.add_argument("--metrics-dir", metavar="DIR",
                        help="publish metrics via homelab-metric-write. Omit "
                             "for a local run: an operator diagnostic must not "
                             "write to the nightly series")
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
    if not catalog.get("image_generation_enabled"):
        print("image_generation_enabled is false in images.yml — not probing")
        return 0

    verdict, metrics = probe(args.base_url, token, catalog, args.timeout)

    # Emit BEFORE asserting, so the chart does not go blank exactly when
    # something is wrong. Publish nothing when inconclusive:
    # homelab-metric-write leaves the previous file in place, and a stale
    # number is detectable where a fabricated zero reads as good news.
    if args.metrics_dir and metrics:
        lines = "".join(f"{name} {value}\n" for name, value in sorted(metrics.items()))
        subprocess.run(
            ["homelab-metric-write", "--dir", args.metrics_dir,
             "--file", "image-generation", "--prefix", "homelab_image_generation",
             *(["--success"] if verdict == "ok" else [])],
            input=lines, text=True, check=True,
        )

    print(f"verdict={verdict}")
    return {"ok": 0, "broken": 1, "inconclusive": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
