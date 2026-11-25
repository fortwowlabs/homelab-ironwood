#!/usr/bin/env python3
"""Exercise homelab-certwatch.sh against fixtures, including every way it must FAIL.

roles/svc_media/templates/certwatch.sh.j2 used to check only the certificate
FILES. On 2026-11-25 certbot renewed the wildcard, the deploy hook copied it to
/etc/caddy/certs, and `caddy reload` answered "config is unchanged" — so Caddy
kept serving the old certificate from memory while both files on disk were
perfect. A check that reads files cannot see that. The script now also asks
Caddy for the certificate it is actually serving and compares fingerprints.

On a healthy host the answer is always "OK", so the failure paths are
exercised here, offline, against local TLS listeners:

  clean         served == caddy copy == live copy                  -> 0
  stale-served  Caddy serves a cert other than the copy on disk    -> 1
  hook-missed   caddy copy differs from the live copy              -> 1
  unreachable   nothing listening on the served port               -> 1
  closes        the listener accepts then closes without TLS       -> 1
  silent        the listener accepts and never answers             -> 1
  expiring      everything agrees, but expires inside the window   -> 1

The last four matter most. Each is a "could not look" that an ordinary
implementation would report as "nothing wrong". `silent` needs coreutils
`timeout`, which macOS lacks; it is skipped there and the summary says so.

Shaped after tests/validate_container_drift.py.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined
from validate_generated_catalog import comment

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "shell"

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "roles/svc_media/templates/certwatch.sh.j2"
CASE_TIMEOUT = 30


def render(template: str, **ctx: object) -> str:
    """Render any repo template with the same strict environment."""
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    env.filters["comment"] = comment
    env.filters["quote"] = lambda v: shlex.quote(str(v))
    main_vars = yaml.safe_load(
        (ROOT / "inventory/group_vars/all/main.yml").read_text(encoding="utf-8"))
    context = {
        "ansible_managed": "fixture managed",
        "service_domain": "example.test",
        "ansible_host": "127.0.0.1",
        "cert_expiry_days": main_vars["cert_expiry_days"],
    }
    context.update(ctx)
    text = env.from_string((ROOT / template).read_text(encoding="utf-8")).render(**context)
    if "{{" in text:
        raise SystemExit(f"render: {template} still contains '{{{{' after rendering")
    return text


def setvar(text: str, name: str, value: str) -> str:
    """Point one top-of-file assignment at a fixture; exactly one must match."""
    new, count = re.subn(rf"(?m)^{name}=.*$", lambda _m: f"{name}={value}", text)
    if count != 1:
        raise SystemExit(f"certwatch: expected exactly one `{name}=` line, found {count}")
    return new


def write_sh(path: Path, text: str) -> None:
    """Write a file bash will read, with LF endings on every platform."""
    path.write_text(text, encoding="utf-8", newline="")


def make_cert(tmp: Path, name: str, days: int) -> Path:
    pem = tmp / f"{name}.pem"
    key = tmp / f"{name}.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-subj", "/CN=*.example.test", "-days", str(days),
         "-keyout", str(key), "-out", str(pem)],
        check=True, capture_output=True)
    return pem


class Listener:
    """A loopback listener in a daemon thread; kind is tls|closes|silent."""

    def __init__(self, kind: str, cert: Path | None = None, key: Path | None = None):
        self.kind = kind
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.held: list[socket.socket] = []
        self.ctx: ssl.SSLContext | None = None
        if kind == "tls":
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(str(cert), str(key))
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.settimeout(5)
            if self.kind == "closes":
                conn.close()
            elif self.kind == "silent":
                self.held.append(conn)
            else:
                threading.Thread(target=self._handshake, args=(conn,), daemon=True).start()

    def _handshake(self, conn: socket.socket) -> None:
        try:
            if self.ctx is None:
                raise OSError("no TLS context")
            self.ctx.wrap_socket(conn, server_side=True).close()
        except (OSError, ssl.SSLError):
            conn.close()

    def close(self) -> None:
        self.sock.close()
        for held in self.held:
            held.close()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


HOOK = "roles/svc_media/templates/certbot-deploy-caddy.sh.j2"


def check_hook() -> list[str]:
    """The deploy hook must force the reload; a plain one is a no-op here.

    On 2026-11-25 `systemctl reload caddy` logged "config is unchanged" and
    Caddy kept serving the old certificate, because the Caddyfile's `tls`
    paths never change. Only `caddy reload --force` re-runs the cert loaders.
    """
    text = render(HOOK)
    problems = []
    if "caddy reload --force --config /etc/caddy/Caddyfile" not in text:
        problems.append(
            f"hook: {HOOK} must run `caddy reload --force --config /etc/caddy/Caddyfile`; "
            "on 2026-11-25 a plain reload said \"config is unchanged\" and Caddy kept "
            "serving the old certificate")
    if "systemctl reload caddy" in text:
        problems.append(
            f"hook: {HOOK} still runs `systemctl reload caddy`, which is a no-op "
            "(\"config is unchanged\") when only the certificate files changed")
    return problems


def main() -> int:
    if not shutil.which("openssl"):
        print("certwatch: openssl is required", file=sys.stderr)
        return 127
    if not shutil.which("bash"):
        print("certwatch: bash is required", file=sys.stderr)
        return 127

    have_timeout = shutil.which("timeout") is not None
    base = render(TEMPLATE)

    failures: list[str] = []
    ran = 0
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        certs = {
            "long_a": make_cert(tmp, "long_a", 365),
            "long_b": make_cert(tmp, "long_b", 365),
            "short": make_cert(tmp, "short", 5),
        }
        env = dict(os.environ)
        if not have_timeout:
            # macOS has no coreutils timeout; a shim that drops the duration
            # is enough for every case except `silent`, which is skipped.
            bindir = tmp / "bin"
            bindir.mkdir()
            shim = bindir / "timeout"
            write_sh(shim, '#!/bin/sh\nshift\nexec "$@"\n')
            shim.chmod(0o755)
            env["PATH"] = f"{bindir}:{env['PATH']}"

        # name, served, caddy, live, want_rc, must_contain, probe_timeout
        cases = [
            ("clean", "tls:long_a", "long_a", "long_a", 0, "Caddy is serving", 10),
            ("stale-served", "tls:long_a", "long_b", "long_b", 1,
             "Caddy is serving a different certificate", 10),
            ("hook-missed", "tls:long_a", "long_a", "long_b", 1,
             "deploy hook did not copy", 10),
            ("unreachable", "unreachable", "long_a", "long_a", 1,
             "could not read the certificate Caddy is serving", 10),
            ("closes", "closes", "long_a", "long_a", 1,
             "could not read the certificate Caddy is serving", 10),
            ("silent", "silent", "long_a", "long_a", 1,
             "could not read the certificate Caddy is serving", 2),
            ("expiring", "tls:short", "short", "short", 1, "expires within", 10),
        ]
        for name, served, caddy, live, want_rc, want_text, probe in cases:
            if name == "silent" and not have_timeout:
                continue
            listener = None
            try:
                if served.startswith("tls:"):
                    c = certs[served[4:]]
                    listener = Listener("tls", c, c.with_suffix(".key"))
                    port = listener.port
                elif served == "unreachable":
                    port = closed_port()
                else:
                    listener = Listener(served)
                    port = listener.port

                text = base
                text = setvar(text, "LIVE_CERT", shlex.quote(certs[live].as_posix()))
                text = setvar(text, "CADDY_CERT", shlex.quote(certs[caddy].as_posix()))
                text = setvar(text, "SERVED_HOST", "127.0.0.1")
                text = setvar(text, "SERVED_PORT", str(port))
                text = setvar(text, "SERVED_SNI", "home.example.test")
                text = setvar(text, "PROBE_TIMEOUT", str(probe))
                script = tmp / f"certwatch-{name}.sh"
                write_sh(script, text)
                script.chmod(0o755)

                ran += 1
                try:
                    result = subprocess.run(
                        ["bash", str(script)], capture_output=True, text=True,
                        env=env, check=False, timeout=CASE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    failures.append(f"{name}: script did not finish within {CASE_TIMEOUT}s")
                    continue
                out = result.stdout + result.stderr
                # clean's text is a progress line on stdout; the failure texts
                # are on stderr. Match each where it belongs.
                haystack = result.stdout if name == "clean" else out
                if result.returncode != want_rc or want_text not in haystack:
                    failures.append(
                        f"{name}: expected rc={want_rc} containing {want_text!r}, "
                        f"got rc={result.returncode}\n{out.rstrip()}")
            finally:
                if listener is not None:
                    listener.close()

    failures.extend(check_hook())
    if failures:
        print("certwatch: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    note = "" if have_timeout else "; silent skipped: no coreutils timeout"
    print(f"certwatch: OK ({ran} cases, {ran - 1} of them must fail{note}; deploy hook forces reload)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
