#!/usr/bin/env python3
"""Release ComfyUI's models and memory after an image check, whatever happened.

Run from ExecStopPost= in homelab-image-gen@ and homelab-image-edit@, so it
fires after success, failure, crash and a TimeoutStartSec kill alike. It
reports and never judges: it always exits 0, because a release problem must
never change the check's verdict. Unexpected exceptions (malformed catalog,
missing key) are logged and still exit 0 -- the "-" on ExecStopPost already
ignores failures, but a non-zero stop hook still adds a failure line to the
journal.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gpu_host  # noqa: E402

CATALOG_PATH = ROOT / "inventory" / "group_vars" / "all" / "images.yml"


def release(catalog_path: Path, settle: float) -> None:
    with open(catalog_path, encoding="utf-8") as fh:
        url = yaml.safe_load(fh)["comfyui_base_url"]
    if not gpu_host.reachable(url):
        print(f"gpu-release: ComfyUI {url} unreachable; nothing to release")
        return
    if gpu_host.comfy_queue_busy(url) is not False:
        # True = someone's job is running; None = could not tell. Either way
        # freeing could kill a real job, so leave the card alone.
        print("gpu-release: ComfyUI queue busy or unreadable; models left resident")
        return
    r = gpu_host.comfy_free(url, settle=settle)
    print(f"gpu-release: posted={r['posted']} "
          f"before {gpu_host._mib(r['before'])}, after {gpu_host._mib(r['after'])}")


def main(argv: list[str] | None = None) -> int:
    try:
        ap = argparse.ArgumentParser(description=__doc__)
        ap.add_argument("--catalog", type=Path, default=CATALOG_PATH)
        ap.add_argument("--settle", type=float, default=20.0)
        args = ap.parse_args(argv)
        release(args.catalog, args.settle)
    except SystemExit:
        print("gpu-release: bad arguments (ignored)", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - must never change the verdict
        print(f"gpu-release: could not release ({type(e).__name__}: {e})", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
