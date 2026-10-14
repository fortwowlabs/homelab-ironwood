#!/usr/bin/env python3
"""Run homelab-maintenance-egress against a fake nft that provokes SIGPIPE.

The script's integrity check used to be `nft list chain ... | grep -q 'policy
drop'` under `set -euo pipefail`. grep -q exits on its first match; if nft is
still writing, it takes SIGPIPE and the pipeline reports 141 even though the
check passed. On 2026-10-12 that killed `close` after the strict backstop had
been restored but BEFORE the state file was removed. The firewall was shut, the
marker said open, and the next morning's verify failed on svc-download — the
healthchecks.io alert. The race depends on how much nft writes after the match,
so it bit intermittently and passed every static gate.

This gate makes the race deterministic: the fake nft prints the `policy drop`
line, pauses long enough for grep -q to exit, then writes well past a pipe
buffer. A pipeline-based check dies with 141 every time; the fixed script reads
the whole listing before testing it.

Positive control: the fake nft's listing is asserted to exceed the pipe buffer
and to contain the policy line, so the gate cannot pass because the fake stopped
provoking the failure it exists to provoke.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# Shell behaviour under test, alongside the template renderer's ShellCheck pass.
#
# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "shell"

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "roles/service_vm/templates/maintenance-egress.sh.j2"

PIPE_BUFFER = 64 * 1024
TRAILING_BYTES = 4 * PIPE_BUFFER

FAKE_NFT = f"""#!/usr/bin/env bash
# list chain: the policy line first, then pause so a `grep -q` reader exits,
# then keep writing — the shape that SIGPIPEs a pipeline.
case "$*" in
  "list table inet host_backstop") exit 0 ;;
  "list chain inet host_backstop output")
    printf 'table inet host_backstop {{\\n\\tchain output {{\\n\\t\\ttype filter hook output priority 0; policy %s;\\n' "${{FAKE_POLICY:-drop}}"
    sleep 0.3
    head -c {TRAILING_BYTES} /dev/zero | tr '\\0' '#'
    printf '\\n\\t}}\\n}}\\n'
    exit 0 ;;
  -f\\ *|"insert rule "*|"flush chain "*) exit 0 ;;
  *) echo "fake nft: unexpected args: $*" >&2; exit 99 ;;
esac
"""


def render(tmp: Path) -> Path:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{ lan_dns }}", "192.0.2.53")
    text = text.replace("state=/run/homelab-maintenance-egress.open", f"state={tmp}/egress.open")
    text = text.replace("backstop=/etc/nftables/host-backstop.nft", f"backstop={tmp}/backstop.nft")
    if "{{" in text:
        raise SystemExit(f"{TEMPLATE}: unrendered Jinja left after substitution")
    script = tmp / "maintenance-egress.sh"
    script.write_text(text, encoding="utf-8")
    return script


def run(script: Path, action: str, path_dir: Path, policy: str = "drop") -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": f"{path_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_POLICY": policy}
    return subprocess.run(["bash", str(script), action], capture_output=True, text=True,
                          env=env, timeout=30, check=False)


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        nft = bin_dir / "nft"
        nft.write_text(FAKE_NFT, encoding="utf-8")
        nft.chmod(nft.stat().st_mode | stat.S_IXUSR)
        (tmp / "backstop.nft").write_text("# fake\n", encoding="utf-8")

        # Positive control: the fake really does write past the pipe buffer
        # after the policy line, which is what makes a pipeline check fail.
        listing = subprocess.run([str(nft), "list", "chain", "inet", "host_backstop", "output"],
                                 capture_output=True, check=False).stdout
        if b"policy drop" not in listing or len(listing) <= 2 * PIPE_BUFFER:
            failures.append("positive control: fake nft no longer emits a SIGPIPE-sized listing")

        script = render(tmp)
        state = tmp / "egress.open"

        opened = run(script, "open", bin_dir)
        if opened.returncode != 0 or opened.stdout.strip() != "opened" or not state.exists():
            failures.append(f"open: rc={opened.returncode} stdout={opened.stdout!r} "
                            f"stderr={opened.stderr.strip()!r} state_exists={state.exists()}")

        state.touch()
        closed = run(script, "close", bin_dir)
        if closed.returncode != 0 or closed.stdout.strip() != "closed" or state.exists():
            failures.append(f"close: rc={closed.returncode} stdout={closed.stdout!r} "
                            f"stderr={closed.stderr.strip()!r} state_left_behind={state.exists()} "
                            "(rc 141 is the SIGPIPE race: the backstop was restored but the "
                            "marker was not removed)")

        # The check must still bite: an output chain that lost its drop policy
        # must fail `close` and keep the marker, or the fix is a no-op.
        state.touch()
        lax = run(script, "close", bin_dir, policy="accept")
        if lax.returncode == 0 or not state.exists():
            failures.append(f"close with policy accept: rc={lax.returncode} "
                            f"state_kept={state.exists()} — the integrity check no longer detects "
                            "a chain without its drop policy")

    if failures:
        print("maintenance-egress validation FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("maintenance-egress: OK (open and close survive a SIGPIPE-sized nft listing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
