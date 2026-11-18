#!/usr/bin/env python3
"""Release ComfyUI's models and memory after an image check, whatever happened.

Run from ExecStopPost= in homelab-image-gen@ and homelab-image-edit@, so it
fires after success, failure, crash and a TimeoutStartSec kill alike. It
reports and never judges: it always exits 0, because a release problem must
never change the check's verdict. Unexpected exceptions (malformed catalog,
missing key) are logged and still exit 0 -- the "-" on ExecStopPost already
ignores failures, but a non-zero stop hook still adds a failure line to the
journal.

Only a TimeoutStartSec kill ($SERVICE_RESULT == "timeout") earns a wait for a
busy queue; for any other result a busy queue is someone else's job and is
left alone at once. Nothing is released for a check whose feature flag is off.
One --deadline bounds the whole run so it finishes inside TimeoutStopSec.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gpu_host  # noqa: E402

CATALOG_PATH = ROOT / "inventory" / "group_vars" / "all" / "images.yml"


FLAGS = {"gen": "image_generation_enabled", "edit": "image_edit_enabled"}


def release(catalog_path: Path, check: str, service_result: str, settle: float,
            drain: float, poll: float, deadline: float) -> None:
    end = time.monotonic() + deadline
    with open(catalog_path, encoding="utf-8") as fh:
        catalog = yaml.safe_load(fh)
    flag = FLAGS[check]
    if not catalog.get(flag):
        print(f"gpu-release: {flag} is false (feature disabled); this check never touched the card; "
              "nothing to release")
        return
    url = catalog["comfyui_base_url"]
    if not gpu_host.reachable(url):
        print(f"gpu-release: ComfyUI {url} unreachable; nothing to release")
        return
    busy = gpu_host.comfy_queue_busy(url)
    if busy is None:
        print("gpu-release: queue state unreadable; models left resident")
        return
    if busy and service_result != "timeout":
        # Only a TimeoutStartSec kill leaves our own prompt queued behind us.
        # Any other result with a busy queue means the job is someone else's
        # (busy at the check's start, or a human is generating): do not wait,
        # do not free.
        print(f"gpu-release: queue busy (service result {service_result or 'unset'}); "
              "someone else's job; models left resident")
        return
    if busy:
        # Timeout kill: killing our client does not cancel its prompt in
        # ComfyUI. Wait for it to drain rather than leave the stack resident,
        # but never past the overall deadline.
        wait = max(0.0, min(drain, end - time.monotonic()))
        stop = time.monotonic() + wait
        while busy:
            if time.monotonic() >= stop:
                print(f"gpu-release: queue still busy after {wait:g}s; models left resident")
                return
            time.sleep(min(poll, max(0.0, stop - time.monotonic())))
            busy = gpu_host.comfy_queue_busy(url)
            if busy is None:
                print("gpu-release: queue state unreadable; models left resident")
                return
    remaining = end - time.monotonic()
    if remaining <= 0:
        print("gpu-release: deadline reached before freeing; models left resident")
        return
    r = gpu_host.comfy_free(url, settle=min(settle, remaining))
    print(f"gpu-release: posted={r['posted']} "
          f"before {gpu_host._mib(r['before'])}, after {gpu_host._mib(r['after'])}")


def main(argv: list[str] | None = None) -> int:
    try:
        ap = argparse.ArgumentParser(description=__doc__)
        ap.add_argument("--catalog", type=Path, default=CATALOG_PATH)
        ap.add_argument("--check", choices=sorted(FLAGS), required=True,
                        help="which check this follows; its feature flag gates the release")
        ap.add_argument("--service-result",
                        default=os.environ.get("SERVICE_RESULT", ""),
                        help="systemd's $SERVICE_RESULT; only 'timeout' drains a busy queue")
        ap.add_argument("--settle", type=float, default=20.0)
        ap.add_argument("--drain", type=float, default=300.0,
                        help="seconds to wait for a busy queue to drain")
        ap.add_argument("--poll", type=float, default=5.0)
        ap.add_argument("--deadline", type=float, default=390.0,
                        help="overall budget in seconds; keeps the hook inside "
                             "TimeoutStopSec=420")
        args = ap.parse_args(argv)
        release(args.catalog, args.check, args.service_result, args.settle,
                args.drain, args.poll, args.deadline)
    except SystemExit:
        print("gpu-release: bad arguments (ignored)", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - must never change the verdict
        print(f"gpu-release: could not release ({type(e).__name__}: {e})", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
