#!/usr/bin/env python3
"""Release ComfyUI's models and memory after an image check, whatever happened.

Run from ExecStopPost= in homelab-image-gen@ and homelab-image-edit@, so it
fires after success, failure, crash and a TimeoutStartSec kill alike. It
reports and never judges: it always exits 0, because a release problem must
never change the check's verdict. Unexpected exceptions (malformed catalog,
missing key) are logged and still exit 0 -- the "-" on ExecStopPost already
ignores failures, but a non-zero stop hook still adds a failure line to the
journal.

A busy queue earns a wait (then a free) only when our own prompt may still be in
it: $SERVICE_RESULT == "timeout" (TimeoutStartSec kill), or the check left a
`submitted` marker in $RUNTIME_DIRECTORY (--runtime-dir) just before POSTing
its request. The marker matters because the checks' own client --timeout is
shorter than TimeoutStartSec, so a slow generation ends as `inconclusive`,
exit 2, SERVICE_RESULT=success, with the prompt still running in ComfyUI.
Otherwise a busy queue is someone else's job and is left alone at once. Nothing is released for a check whose feature flag is off.
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
            drain: float, poll: float, deadline: float,
            runtime_dir: Path | None = None) -> None:
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
    submitted = runtime_dir is not None and (runtime_dir / gpu_host.SUBMITTED_MARKER).exists()
    if busy and service_result != "timeout" and not submitted:
        # Our own prompt is queued only after a TimeoutStartSec kill or once
        # the check submitted its request (marker). Otherwise the job is
        # someone else's (busy at the check's start, or a human is
        # generating): do not wait, do not free.
        print(f"gpu-release: queue busy (service result {service_result or 'unset'}, "
              "no submitted marker); someone else's job; models left resident")
        return
    if busy:
        # Our own prompt may still be running: a timeout kill does not cancel
        # it in ComfyUI, and neither does the check's own client timeout. Wait for it to drain rather than leave the stack resident,
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
                        help="systemd's $SERVICE_RESULT; 'timeout' drains a busy queue")
        rd = os.environ.get("RUNTIME_DIRECTORY")
        ap.add_argument("--runtime-dir", type=Path, default=Path(rd) if rd else None,
                        help="directory holding the check's `submitted` marker "
                             "(default $RUNTIME_DIRECTORY); a marker drains a busy queue")
        ap.add_argument("--settle", type=float, default=20.0)
        ap.add_argument("--drain", type=float, default=300.0,
                        help="seconds to wait for a busy queue to drain")
        ap.add_argument("--poll", type=float, default=5.0)
        ap.add_argument("--deadline", type=float, default=390.0,
                        help="overall budget in seconds; keeps the hook inside "
                             "TimeoutStopSec=420")
        args = ap.parse_args(argv)
        release(args.catalog, args.check, args.service_result, args.settle,
                args.drain, args.poll, args.deadline, args.runtime_dir)
    except SystemExit:
        print("gpu-release: bad arguments (ignored)", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - must never change the verdict
        print(f"gpu-release: could not release ({type(e).__name__}: {e})", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
