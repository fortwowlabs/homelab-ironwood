#!/usr/bin/env python3
"""Exercise dnf-makecache-retry.sh, especially the case where it must still FAIL.

roles/service_vm/templates/dnf-makecache-retry.sh.j2 absorbs a transient mirror
404 by retrying once. On a healthy host it succeeds on the first call every
time, so the interesting behaviour — the retry, and the refusal to retry
forever — is never exercised in production. Left alone, a wrapper that had
quietly become `exit 0` would look identical to a working one, and would have
turned homelab-failedunits blind to every dnf repository problem.

So all three outcomes are asserted here, against a stub `dnf`:

  succeeds first call    exit 0, no retry logged   no needless second run
  fails then succeeds    exit 0, retry LOGGED      transient case absorbed,
                                                   and still visible
  fails both calls       NON-ZERO                  a persistent repo outage
                                                   still fails the unit

The third is the one worth having. The second matters nearly as much: a retry
that happens monthly is noise absorbed, but one that happens hourly is a real
problem wearing a transient error's clothes, and the only thing separating
those two readings is that the script says so in the journal. If the message
ever stops being emitted, this test fails.

`sleep` is stubbed too, so the suite does not pay the 120s retry delay.

Shaped after tests/validate_container_drift.py, which does the same job for the
container drift check for the same reason.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "roles/service_vm/templates/dnf-makecache-retry.sh.j2"

RETRY_MARKER = "retrying once"


def render(delay: str = "120") -> str:
    """Render the template. Only ansible_managed and the delay are interpolated."""
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{ ansible_managed | comment }}", "# (rendered for tests)")
    text = re.sub(r"\{\{\s*dnf_makecache_retry_delay[^}]*\}\}", delay, text)
    leftover = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", text)
    if leftover:
        # A new variable would otherwise reach the shell as a literal
        # `{{ foo }}` — a syntax error at best, a silently empty value at worst.
        raise SystemExit(
            "dnf-makecache-retry.sh.j2 grew Jinja this test does not render: "
            + ", ".join(sorted(set(leftover)))
            + "\nAdd it to render() rather than loosening this check.")
    return text


def build(tmp: Path, outcomes: list[int]) -> Path:
    """Write a stub dnf that exits with `outcomes` in order, and a no-op sleep."""
    binary = tmp / "bin"
    binary.mkdir(parents=True, exist_ok=True)

    (binary / "counter").write_text("0", encoding="utf-8")
    codes = " ".join(str(code) for code in outcomes)
    stub = binary / "dnf"
    stub.write_text(
        '#!/usr/bin/env bash\n'
        f'codes=({codes})\n'
        f'n=$(cat "{binary}/counter")\n'
        f'echo $((n + 1)) > "{binary}/counter"\n'
        'code=${codes[$n]:-1}\n'
        'echo "stub dnf call $((n + 1)) -> $code"\n'
        'exit "$code"\n', encoding="utf-8")
    stub.chmod(0o755)

    napper = binary / "sleep"
    napper.write_text('#!/usr/bin/env bash\nexit 0\n', encoding="utf-8")
    napper.chmod(0o755)

    script = tmp / "dnf-makecache-retry.sh"
    # The script calls /usr/bin/dnf by absolute path, which is right on the host
    # and untestable here; point it at the stub. The absolute path is retained
    # in the template deliberately, so a PATH surprise cannot change what a
    # root-run unit executes.
    text = render().replace("DNF=/usr/bin/dnf", f'DNF="{stub}"')
    script.write_text(text, encoding="utf-8")
    script.chmod(0o755)
    return script


def run(script: Path, tmp: Path) -> tuple[int, str, int]:
    env = dict(os.environ)
    env["PATH"] = f"{tmp / 'bin'}:/usr/bin:/bin"
    result = subprocess.run(["bash", str(script)], capture_output=True,
                            text=True, env=env, check=False)
    calls = int((tmp / "bin/counter").read_text(encoding="utf-8").strip())
    return result.returncode, result.stdout + result.stderr, calls


CASES = (
    # name,            dnf exit codes,  want rc, want calls, want retry logged
    ("first call succeeds", [0],        0,       1,          False),
    ("fails then succeeds", [1, 0],     0,       2,          True),
    ("fails both calls",    [1, 1],     1,       2,          True),
)


def main() -> int:
    if not shutil.which("bash"):
        print("bash is required", file=sys.stderr)
        return 127

    failures: list[str] = []
    for name, codes, want_rc, want_calls, want_retry in CASES:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            rc, out, calls = run(build(tmp, codes), tmp)

        if rc != want_rc:
            failures.append(f"{name}: expected rc={want_rc}, got {rc}\n{out.rstrip()}")
        if calls != want_calls:
            failures.append(
                f"{name}: expected {want_calls} dnf call(s), got {calls}. "
                "A wrapper that retries when it should not wastes the delay on "
                "every healthy run; one that does not retry when it should is "
                "the whole point of this change, undone.")
        logged = RETRY_MARKER in out
        if logged != want_retry:
            failures.append(
                f"{name}: expected retry logged={want_retry}, got {logged}. "
                "The journal line is the only thing distinguishing a monthly "
                "transient blip from an hourly repository failure.")

    if failures:
        print("dnf makecache retry regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print(f"dnf makecache retry: OK ({len(CASES)} cases, "
          "including that a persistent failure still fails)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
