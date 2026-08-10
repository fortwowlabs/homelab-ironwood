#!/usr/bin/env python3
"""Exercise container-drift.sh against fixtures, including every way it must FAIL.

roles/service_vm/templates/container-drift.sh.j2 asserts that each running
container is the image its Quadlet unit declares. It runs on live hosts where
the healthy answer is always "OK", so left alone it would be a check nobody
could tell had stopped working — which is the exact failure this repo keeps
writing down, and which the script's own positive control exists to prevent.

So the failure paths are exercised here instead, offline, with a stub `podman`
and fixture unit files. Six cases, and the four that must fail matter more than
the two that must pass:

  clean      every container matches its unit                        -> 0
  drifted    a container running an image its unit does not name     -> 1
  orphan     a container with no Quadlet unit at all                 -> 1
  empty      podman answers, but reports nothing running             -> 2
  no-podman  neither context could be queried                        -> 2
  no-units   no Quadlet files where they are expected                -> 2

The last three are the ones worth having. Each is a "could not look" that an
ordinary implementation would report as "nothing found", and each would make a
broken check indistinguishable from a healthy estate.

This test renders the Jinja template with a trivial substitution rather than a
full Ansible run: the template's only variable is svc_uid, and standing up
ansible-core to interpolate one integer would make the test slow enough that
somebody would eventually skip it.
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
TEMPLATE = ROOT / "roles/service_vm/templates/container-drift.sh.j2"

# name|image lines a stub `podman ps` will emit, per case.
SONARR = "lscr.io/linuxserver/sonarr@sha256:" + "a" * 64
OTHER = "docker.io/example/other@sha256:" + "b" * 64
STRANGER = "docker.io/somebody/unmanaged@sha256:" + "c" * 64

# case -> (podman ps output, expected rc, expected substring, expected metrics line)
# The metrics line is consumed by the emitter in container-drift.yml. It must
# appear for a clean run AND for a run that found drift, and must NOT appear for
# any cannot-look: publishing counts nobody could measure is the failure the
# whole metrics design exists to avoid.
CASES = {
    "clean": (f"sonarr|{SONARR}\nsystemd-noname|{OTHER}\n", 0, "OK",
              "drift_metrics checked=2 units=2 drifted=0 orphan=0"),
    "drifted": (f"sonarr|lscr.io/linuxserver/sonarr@sha256:{'d' * 64}\n", 1, "DRIFTED",
                "drift_metrics checked=1 units=2 drifted=1 orphan=0"),
    "orphan": (f"sonarr|{SONARR}\nstranger|{STRANGER}\n", 1, "NO QUADLET",
               "drift_metrics checked=2 units=2 drifted=0 orphan=1"),
    "empty": ("", 2, "CANNOT LOOK", None),
}


def render(svc_uid: str = "10001") -> str:
    """Render the template. Only svc_uid and ansible_managed are interpolated."""
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{ ansible_managed | comment }}", "# (rendered for tests)")
    text = text.replace("{{ svc_uid | quote }}", svc_uid)

    # Scan for unhandled Jinja BEFORE unescaping the Go-template braces, not
    # after. `podman ps --format` needs literal {{.Names}}, which the template
    # escapes as {{ '{{' }} — so after unescaping those are indistinguishable
    # from Jinja this function forgot, and the scan flagged them on its first
    # run. A new variable in the template must still be caught: it would reach
    # the shell as a literal `{{ foo }}`, a syntax error at best and a silently
    # empty value at worst.
    # Substituted out before scanning rather than filtered out of the results:
    # the non-greedy `\{\{.*?\}\}` matches the inner `{{ '}}` of `{{ '}}' }}`,
    # so a membership test against the whole escape never fires.
    scanned = (text.replace("{{ '{{' }}", "\0OPEN\0")
                   .replace("{{ '}}' }}", "\0CLOSE\0"))
    leftover = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", scanned)
    if leftover:
        raise SystemExit(
            "container-drift.sh.j2 grew Jinja this test does not render: "
            + ", ".join(sorted(set(leftover)))
            + "\nAdd it to render() rather than loosening this check.")

    return text.replace("{{ '{{' }}", "{{").replace("{{ '}}' }}", "}}")


def setvar(text: str, name: str, value: str) -> str:
    """Point one of the script's top-of-file assignments at a fixture path.

    The replacement is passed as a FUNCTION, not as a string. re.sub treats a
    string replacement as a template and interprets backslash escapes in it, so
    a Windows temp directory (C:\\Users\\...\\AppData\\...) raised
    `re.PatternError: bad escape \\U` and took `make validate` down with a
    traceback before the drift gate ran at all. A function replacement is
    substituted literally, so no path can ever be read as an escape.

    Paths are handed in as POSIX anyway, because the thing consuming them is
    bash.
    """
    return re.sub(rf"(?m)^{name}=.*$", lambda _m: f"{name}={value}", text)


def write_sh(path: Path, text: str) -> None:
    """Write a file that bash will read, with LF endings on every platform.

    newline="" is not optional. Path.write_text otherwise applies the
    platform's line separator, so on Windows every fixture gained CRLF: the
    unit files' `Image=` values picked up a trailing \\r and compared unequal to
    the identical string from podman, which made the `clean` case report two
    DRIFTED containers whose running and declared digests printed the same. A
    CR in the stub podman's shebang breaks it outright.
    """
    path.write_text(text, encoding="utf-8", newline="")


def build(tmp: Path, *, units: bool = True, podman: bool = True) -> Path:
    (tmp / "bin").mkdir(parents=True, exist_ok=True)
    quadlet = tmp / "quadlet"
    quadlet.mkdir(parents=True, exist_ok=True)

    if units:
        write_sh(quadlet / "sonarr.container",
                 f"[Container]\nImage={SONARR}\nContainerName=sonarr\n")
        # No ContainerName: Quadlet names the container systemd-<unit>, and the
        # script must derive that or every such unit reads as an orphan.
        write_sh(quadlet / "noname.container", f"[Container]\nImage={OTHER}\n")

    if podman:
        stub = tmp / "bin/podman"
        write_sh(stub,
                 '#!/usr/bin/env bash\n'
                 '[[ -n "${DRIFT_FIXTURE:-}" ]] || exit 3\n'
                 'cat "$DRIFT_FIXTURE"\n')
        stub.chmod(0o755)

    script = tmp / "container-drift.sh"
    text = render()
    text = setvar(text, "ROOT_QUADLET_DIR", (quadlet if units else tmp / "absent").as_posix())
    text = setvar(text, "ROOTLESS_QUADLET_DIR", (tmp / "absent-rootless").as_posix())
    # No rootless account in the test environment; the script must tolerate a
    # context that simply is not there, which is also true of svc-download.
    text = setvar(text, "ROOTLESS_USER", "__no_such_user__")
    write_sh(script, text)
    script.chmod(0o755)
    return script


def run(script: Path, tmp: Path, fixture: str | None, *, podman: bool) -> tuple[int, str]:
    env = dict(os.environ)
    env["PATH"] = f"{(tmp / 'bin').as_posix()}:/usr/bin:/bin" if podman else "/usr/bin:/bin"
    if fixture is not None:
        path = tmp / "fixture"
        write_sh(path, fixture)
        env["DRIFT_FIXTURE"] = path.as_posix()
    result = subprocess.run(["bash", str(script)], capture_output=True,
                            text=True, env=env, check=False)
    return result.returncode, result.stdout + result.stderr


def main() -> int:
    if not shutil.which("bash"):
        print("bash is required", file=sys.stderr)
        return 127

    failures: list[str] = []
    checked = 0

    for name, (fixture, want_rc, want_text, expected_metrics) in CASES.items():
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            script = build(tmp)
            rc, out = run(script, tmp, fixture, podman=True)
        checked += 1
        if rc != want_rc or want_text not in out:
            failures.append(
                f"{name}: expected rc={want_rc} containing {want_text!r}, "
                f"got rc={rc}\n{out.rstrip()}")
        # A cannot-look (expected_metrics is None) must never publish counts;
        # a clean or drifted run must publish exactly the counts it found. Both
        # halves of this check matter equally: this is the guarantee the whole
        # task exists to prove, not a side effect of the rc/text assertion above.
        if expected_metrics is None:
            if "drift_metrics" in out:
                failures.append(
                    f"{name}: printed a drift_metrics line on a cannot-look. "
                    "Counts nobody could measure must not be published."
                )
        elif expected_metrics not in out:
            failures.append(
                f"{name}: expected {expected_metrics!r} in stdout, got:\n{out}"
            )

    # podman missing entirely
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        script = build(tmp, podman=False)
        rc, out = run(script, tmp, None, podman=False)
    checked += 1
    if rc != 2 or "CANNOT LOOK" not in out:
        failures.append(f"no-podman: expected rc=2 CANNOT LOOK, got rc={rc}\n{out.rstrip()}")
    # Same rule as the CASES loop: a cannot-look must publish nothing, so the
    # publisher keeps the previous file and the staleness is what shows up.
    if "drift_metrics" in out:
        failures.append(
            "no-podman: printed a drift_metrics line on a cannot-look. "
            "Counts nobody could measure must not be published."
        )

    # Quadlet directory absent
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        script = build(tmp, units=False)
        rc, out = run(script, tmp, CASES["clean"][0], podman=True)
    checked += 1
    if rc != 2 or "CANNOT LOOK" not in out:
        failures.append(f"no-units: expected rc=2 CANNOT LOOK, got rc={rc}\n{out.rstrip()}")
    # Same rule as the CASES loop: a cannot-look must publish nothing, so the
    # publisher keeps the previous file and the staleness is what shows up.
    if "drift_metrics" in out:
        failures.append(
            "no-units: printed a drift_metrics line on a cannot-look. "
            "Counts nobody could measure must not be published."
        )

    if failures:
        print("container drift check regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print(f"Container drift check: OK ({checked} cases, 4 of them failure paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
