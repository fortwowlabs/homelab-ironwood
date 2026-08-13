#!/usr/bin/env python3
"""Exercise with-deploy-lock.sh's release logic, especially the signal case.

tests/validate_deploy_lock.py covers scripts/deploy-lock.sh (acquire/release/
status on a lockfile) but nothing exercised the wrapper that actually calls
it in production. That gap let a real bug ship: the wrapper's original single
`trap ... EXIT` released the remote lock on ANY exit, including one caused by
a signal — but the wrapper exiting does not prove the wrapped deploy exited.
A targeted `kill <wrapper-pid>` hits only the wrapper; ansible-playbook
handles SIGINT/SIGTERM itself and keeps running for several more seconds.
Reproduced live: signal the wrapper, the remote lock is released within a
second, while the wrapped deploy keeps applying for another eight — handing
the lock to the other control node mid-apply. A stale lock merely refuses
the next deploy loudly; an early release is silent and worse.

The fix (see scripts/with-deploy-lock.sh) traps INT/TERM separately, records
that a signal occurred, and lets the EXIT trap consult that flag: release on
a normal exit (success or failure), but leave the lock deliberately HELD —
with a message explaining why — when the wrapper died to a signal.

Three cases, all against a stubbed `ssh` on PATH so nothing touches the
network or the real thurgadin lock:

  normal exit (success)         lock RELEASED   the ordinary path
  non-zero exit (failure)       lock RELEASED   a failed deploy still frees it
  killed with SIGTERM mid-run   lock STILL HELD the case this test exists for

The first two are the third's positive control: a wrapper that had regressed
to never releasing (e.g. a typo'd condition) would pass the SIGTERM case
alone and still be broken, exactly as a wrapper degraded to always releasing
would pass the first two alone. Only the trio together proves the flag is
read correctly in both directions.

The stub `ssh` skips past the `-o ...` flags and `user@host` target and execs
the trailing `bash -s -- <action> <lockfile> <holder>` directly, inheriting
stdin — so the real scripts/deploy-lock.sh (piped in unmodified by the
wrapper) still runs, and only the network transport is faked. Same pattern as
tests/validate_dnf_makecache_retry.py stubbing `dnf`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts/with-deploy-lock.sh"

STUB_SSH = """#!/usr/bin/env bash
# Stub ssh for testing with-deploy-lock.sh without touching the network:
# skip past the -o flags and user@host target, then exec the trailing
# "bash -s -- <action> <lockfile> <holder>" directly, inheriting stdin so the
# real deploy-lock.sh (piped in by the caller) runs unmodified.
args=("$@")
for i in "${!args[@]}"; do
  if [ "${args[$i]}" = "bash" ]; then
    exec "${args[@]:$i}"
  fi
done
echo "stub ssh: no bash invocation found in: $*" >&2
exit 1
"""


def build_stub_bin(tmp: Path) -> Path:
    binary = tmp / "bin"
    binary.mkdir(parents=True, exist_ok=True)
    stub = binary / "ssh"
    stub.write_text(STUB_SSH, encoding="utf-8")
    stub.chmod(0o755)
    return binary


def env_for(tmp: Path, lock: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{build_stub_bin(tmp)}:{env.get('PATH', '')}"
    env["DEPLOY_LOCK_PATH"] = str(lock)
    return env


def main() -> int:
    failures: list[str] = []

    # Case 1: normal successful exit -> lock released.
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        lock = tmp / "deploy.lock"
        result = subprocess.run(
            ["bash", str(WRAPPER), "true"],
            capture_output=True, text=True, env=env_for(tmp, lock),
        )
        if result.returncode != 0:
            failures.append(
                f"normal exit: wrapper exited {result.returncode}, expected 0: "
                f"{result.stderr.strip()}")
        if lock.exists():
            failures.append(
                "normal exit: lock file still present — a healthy deploy must "
                "free the lock for the next one")

    # Case 2: non-zero exit from the wrapped command -> lock still released.
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        lock = tmp / "deploy.lock"
        result = subprocess.run(
            ["bash", str(WRAPPER), "false"],
            capture_output=True, text=True, env=env_for(tmp, lock),
        )
        if result.returncode == 0:
            failures.append(
                "non-zero exit: wrapper exited 0 — it swallowed the wrapped "
                "command's failure")
        if lock.exists():
            failures.append(
                "non-zero exit: lock file still present — a failed deploy "
                "must not strand the lock for the next attempt")

    # Case 3: the wrapper is killed with SIGTERM while the wrapped command
    # (standing in for ansible-playbook) is still running. The lock must
    # stay HELD, not be released early.
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        lock = tmp / "deploy.lock"
        proc = subprocess.Popen(
            ["bash", str(WRAPPER), "sleep", "2"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env_for(tmp, lock),
        )

        # Wait for acquire to actually land before signalling, so the test
        # doesn't race the wrapper's own startup.
        deadline = time.monotonic() + 5
        while not lock.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not lock.exists():
            failures.append(
                "SIGTERM case: lock was never acquired within 5s — cannot "
                "exercise the signal path")
            proc.kill()
            proc.wait()
        else:
            time.sleep(0.3)  # let `sleep 2` be genuinely in flight
            proc.send_signal(signal.SIGTERM)  # targets only the wrapper's pid

            # The lock must still be held immediately after the targeted
            # kill — this is the exact window the original bug lost.
            if not lock.exists():
                failures.append(
                    "SIGTERM case: lock was released within the window right "
                    "after the signal, while the wrapped command was still "
                    "running — this is the bug this test exists to catch")

            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                failures.append(
                    "SIGTERM case: wrapper never exited after being signalled")
            else:
                if proc.returncode != 130:
                    failures.append(
                        f"SIGTERM case: wrapper exited {proc.returncode}, "
                        f"expected 130 (128+SIGTERM): {err.strip()}")
                if "NOT released" not in err:
                    failures.append(
                        "SIGTERM case: no explanation printed for why the "
                        "lock was left held; got stderr: " + err.strip())

            if not lock.exists():
                failures.append(
                    "SIGTERM case: lock file absent after the wrapper exited "
                    "— a signalled wrapper must leave the lock HELD, since "
                    "the deploy it was guarding may still be running")

    if failures:
        print("with-deploy-lock regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print("with-deploy-lock: OK (3 cases, including that a signalled wrapper "
          "leaves the lock held)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
