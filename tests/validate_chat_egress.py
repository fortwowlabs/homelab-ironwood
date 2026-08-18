#!/usr/bin/env python3
"""The nft rule and the Quadlet unit name are the same fact written twice.

container-drift.yml's lesson is that two guards which can drift will. The
rule names open-webui.service inside a cgroup path; the unit name comes from
the infra_secret_apps catalog key. This asserts they still agree, and that
the drop rule keeps its unconditional counter -- the probe reads that counter,
so losing it would silently disarm the verification while the firewall kept
working.

Every structural assertion runs against the ruleset with `#` comments
stripped, because otherwise this gate reads prose. The template's own header
explains at length why it must never carry a `flush ruleset`, and a substring
search over the raw render fails on that sentence -- a check that a file can
fail by *documenting* the right behaviour is a check that gets silenced by
rewording rather than fixed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "roles/svc_infra/templates/chat-egress.nft.j2"


def directives(rendered: str) -> str:
    """The rendered ruleset with comment lines removed."""
    return "\n".join(
        line for line in rendered.splitlines() if not line.lstrip().startswith("#")
    )


def main() -> int:
    failures: list[str] = []
    catalog = yaml.safe_load(
        (ROOT / "inventory/group_vars/all/infra-apps.yml").read_text(encoding="utf-8")
    )
    if "open-webui" not in catalog["infra_secret_apps"]:
        failures.append("catalog: open-webui entry is gone; the nft rule names a unit that will not exist")

    env = Environment(
        loader=FileSystemLoader(str(ROOT)), undefined=StrictUndefined, keep_trailing_newline=True
    )
    env.filters["comment"] = lambda text: "\n".join(f"# {line}" for line in text.splitlines())

    for log_requests in (True, False):
        rendered = directives(
            env.get_template(TEMPLATE).render(
                ansible_managed="Ansible managed",
                svc_uid=10001,
                lan_cidr="192.168.1.0/24",
                chat_egress_unit="open-webui.service",
                chat_egress_cgroup_level=5,
                chat_proxy_log_requests=log_requests,
            )
        )
        if "open-webui.service" not in rendered:
            failures.append(f"log_requests={log_requests}: rule does not name the open-webui unit")
        if "counter drop" not in rendered:
            failures.append(f"log_requests={log_requests}: drop rule lost its unconditional counter")
        if rendered.count("hook output") != 1:
            failures.append(f"log_requests={log_requests}: expected exactly one output base chain")
        if "policy accept" not in rendered:
            failures.append(
                f"log_requests={log_requests}: base chain is not policy accept -- "
                "a drop policy here would take the whole VM off the network"
            )
        if "delete table" not in rendered:
            failures.append(f"log_requests={log_requests}: missing the delete-table idempotency guard")
        if "flush ruleset" in rendered:
            failures.append(
                f"log_requests={log_requests}: flush ruleset would clobber firewalld's own tables"
            )
        has_log = 'log prefix "chat-egress-drop ' in rendered
        if has_log is not log_requests:
            failures.append(
                f"log_requests={log_requests}: drop logging did not follow chat_proxy_log_requests"
            )

    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    if failures:
        return 1
    print("Chat egress nftables policy: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
