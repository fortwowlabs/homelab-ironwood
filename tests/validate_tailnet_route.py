#!/usr/bin/env python3
"""Exercise homelab-tailnet-route.sh against stub tailscale output.

On a healthy estate the route check says OK every ten minutes, so every path
that matters — the router being offline, missing, or deaf, and every way the
check can fail to look at all — never runs in production until the day it is
needed. A check that had quietly become `exit 0` would look identical to a
working one. These cases are the proof that it is not.

The "router offline" case is the 2026-11-11 netmap verbatim in shape: edgar
still held 192.168.1.0/24 in PrimaryRoutes while offline. A check that only
asked "does any node hold the route?" would have said OK all day.

Each case also asserts which healthchecks.io ping went out, because for this
check that ping is the alert path that still works while the route is down
(ntfy reaches an away-from-home phone over the route itself).

Shaped after tests/validate_dnf_makecache_retry.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "shell"

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "roles/svc_media/templates/tailnet-route.sh.j2"
CIDR = "192.168.1.0/24"


def render() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{ ansible_managed | comment }}", "# (rendered for tests)")
    text = text.replace("{{ lan_cidr | quote }}", CIDR)
    text = text.replace("{{ lan_cidr }}", CIDR)
    leftover = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", text)
    if leftover:
        raise SystemExit(
            "tailnet-route.sh.j2 grew Jinja this test does not render: "
            + ", ".join(sorted(set(leftover)))
            + "\nAdd it to render() rather than loosening this check.")
    return text


def node(name: str, ip: str, online: bool, routes: list[str] | None = None) -> dict:
    return {"HostName": name, "TailscaleIPs": [ip], "Online": online,
            "PrimaryRoutes": routes}


SELF = node("svc-media", "100.93.88.34", True)
EDGAR_UP = node("edgar", "100.93.219.13", True, [CIDR])
EDGAR_DOWN = node("edgar", "100.93.219.13", False, [CIDR])
PHONE = node("pixel", "100.124.154.103", True)


def netmap(self_node: dict, *peers: dict, backend: str = "Running") -> str:
    return json.dumps({"BackendState": backend, "Self": self_node,
                       "Peer": {f"k{i}": p for i, p in enumerate(peers)}})


PONG = "pong from edgar (100.93.219.13) via 192.168.1.159:41641 in 1ms\n"

# name, status stdout (None = no tailscale binary), status rc,
#   ping output, ping rc, forwarding, want rc, want hc suffix, want text
CASES = (
    ("healthy peer router", netmap(SELF, EDGAR_UP, PHONE), 0, PONG, 0, "0",
     0, "", "OK via edgar"),
    ("router offline (2026-11-11)", netmap(SELF, EDGAR_DOWN, PHONE), 0, PONG, 0, "0",
     1, "fail", "OFFLINE"),
    ("no node holds the route", netmap(SELF, PHONE), 0, PONG, 0, "0",
     1, "fail", "no tailnet node holds"),
    ("router online but deaf", netmap(SELF, EDGAR_UP), 0, "no reply\n", 1, "0",
     1, "fail", "does not answer"),
    ("this node is the router", netmap(node("svc-media", "100.93.88.34", True, [CIDR]), PHONE),
     0, "", 1, "1", 0, "", "this node"),
    ("this node routes, forwarding off", netmap(node("svc-media", "100.93.88.34", True, [CIDR])),
     0, "", 1, "0", 1, "fail", "forwarding is off"),
    ("tailscale not installed", None, 0, "", 0, "0",
     2, "fail", "CANNOT LOOK"),
    ("status command fails", "failed to connect to local tailscaled", 1, "", 0, "0",
     2, "fail", "CANNOT LOOK"),
    ("logged out", netmap(SELF, EDGAR_UP, backend="NeedsLogin"), 0, PONG, 0, "0",
     2, "fail", "NeedsLogin"),
    ("unparseable status", "not json {", 0, PONG, 0, "0",
     2, "fail", "did not parse"),
    ("no Self address", json.dumps({"BackendState": "Running", "Self": {}, "Peer": {}}),
     0, PONG, 0, "0", 2, "fail", "no Self address"),
)


def build(tmp: Path, status: str | None, status_rc: int, ping: str, ping_rc: int,
          forwarding: str) -> Path:
    tsbin = tmp / "tailscale"
    if status is not None:
        (tmp / "status.out").write_text(status, encoding="utf-8")
        (tmp / "ping.out").write_text(ping, encoding="utf-8")
        tsbin.write_text(
            "#!/usr/bin/env bash\n"
            f'case "$1" in\n'
            f'  status) cat "{tmp}/status.out"; exit {status_rc} ;;\n'
            f'  ping) cat "{tmp}/ping.out"; exit {ping_rc} ;;\n'
            "esac\nexit 99\n", encoding="utf-8")
        tsbin.chmod(0o755)

    hc = tmp / "hc-ping.sh"
    hc.write_text(f'#!/usr/bin/env bash\necho "uuid=$1 suffix=${{2:-}}" >> "{tmp}/hc.log"\n',
                  encoding="utf-8")
    hc.chmod(0o755)
    (tmp / "ip_forward").write_text(forwarding + "\n", encoding="utf-8")

    text = render()
    for old, new in (("TAILSCALE=/usr/bin/tailscale", f'TAILSCALE="{tsbin}"'),
                     ("HC_PING=/usr/local/sbin/hc-ping.sh", f'HC_PING="{hc}"'),
                     ("FORWARD_FILE=/proc/sys/net/ipv4/ip_forward",
                      f'FORWARD_FILE="{tmp}/ip_forward"')):
        if old not in text:
            raise SystemExit(f"tailnet-route.sh.j2 no longer contains {old!r}; update this test")
        text = text.replace(old, new)
    script = tmp / "tailnet-route.sh"
    script.write_text(text, encoding="utf-8")
    return script


def main() -> int:
    for tool in ("bash", "jq"):
        if not shutil.which(tool):
            print(f"{tool} is required", file=sys.stderr)
            return 127

    failures: list[str] = []
    for (name, status, status_rc, ping, ping_rc, fwd,
         want_rc, want_suffix, want_text) in CASES:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            script = build(tmp, status, status_rc, ping, ping_rc, fwd)
            env = dict(os.environ, HC_PING_TAILNET_ROUTE="test-uuid")
            res = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                                 env=env, check=False)
            out = res.stdout + res.stderr
            hc_log = (tmp / "hc.log").read_text(encoding="utf-8") if (tmp / "hc.log").exists() else ""

        if res.returncode != want_rc:
            failures.append(f"{name}: expected rc={want_rc}, got {res.returncode}\n{out.rstrip()}")
        if want_text not in out:
            failures.append(f"{name}: expected {want_text!r} in output\n{out.rstrip()}")
        want_hc = f"uuid=test-uuid suffix={want_suffix}\n"
        if hc_log != want_hc:
            failures.append(
                f"{name}: expected exactly one healthchecks ping {want_hc.strip()!r}, "
                f"got {hc_log.strip()!r}. That ping is the only alert that leaves the "
                "house while the route is down.")

    if failures:
        print("tailnet route check regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print(f"tailnet route check: OK ({len(CASES)} cases, including the 2026-11-11 "
          "offline-router netmap and five cannot-look paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
