#!/usr/bin/env python3
"""The nft rule and the Quadlet unit name are the same fact written twice.

container-drift.yml's lesson is that two guards which can drift will. The rule
names open-webui.service inside a cgroup path; the unit name comes from the
infra_secret_apps catalog key. This asserts they still agree, and that the drop
rule keeps its unconditional counter -- the probe reads that counter, so losing
it would silently disarm the verification while the firewall kept working.

Read the first version of this file before changing it. It rendered the
template with HARDCODED values and then asserted the render contained those
same values, which proves only that Jinja interpolates its own arguments. The
stated purpose -- that the rule and the catalog still agree -- was never
tested, and five mutations that would each have been a live incident all
passed: the cgroup level off by one (which widens the match from open-webui to
every rootless container on the host), a typo'd unit name, user.slice swapped
for system.slice, the LAN accept widened to 0.0.0.0/0, and the jump deleted
entirely.

So: every value comes from the inventory and the role defaults, nothing is
hardcoded except the invariants being asserted, and MUTATIONS below re-renders
deliberately broken templates and fails if any of them passes. A gate with no
positive control is a gate nobody can tell is broken.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from jinja2 import DictLoader, Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "roles/svc_infra/templates/chat-egress.nft.j2"
MAIN_VARS = "inventory/group_vars/all/main.yml"
ROLE_DEFAULTS = "roles/svc_infra/defaults/main.yml"
CATALOG = "inventory/group_vars/all/infra-apps.yml"

SOCKET_RE = re.compile(r'socket cgroupv2 level (\d+) "([^"]+)" jump (\w+)')

# Each mutation is applied to the TEMPLATE SOURCE and must be caught. The
# comment is what shipping it would have meant on the live host.
MUTATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "level off by one",
        "level {{ chat_egress_cgroup_level }}",
        "level {{ chat_egress_cgroup_level - 1 }}",
        # matches app.slice: drops non-LAN egress for ALL ~28 rootless containers
    ),
    (
        "cgroup path hardcoded past the inventory",
        '"{{ chat_egress_cgroup_path }}"',
        '"user.slice/user-10001.slice/user@10001.service/app.slice/typo.service"',
        # matches nothing; enforcement silently absent
    ),
    (
        "user slice swapped for system slice",
        '"{{ chat_egress_cgroup_path }}"',
        '"system.slice/{{ chat_egress_unit }}"',
        # matches nothing; rootless containers do not live under system.slice
    ),
    (
        "LAN accept widened to everything",
        "ip daddr $LAN accept",
        "ip daddr 0.0.0.0/0 accept",
        # accepts all egress: the policy becomes decorative
    ),
    (
        "jump removed from the base chain",
        "jump chat_policy",
        "counter",
        # nothing is ever evaluated against the policy chain
    ),
    (
        "drop rule loses its counter",
        "counter drop",
        "drop",
        # the firewall still works; Task 5's probe can no longer prove it did
    ),
    (
        "a second counter added to the policy chain",
        'oifname "lo" accept',
        'oifname "lo" counter accept',
        # the firewall is unchanged, but the probe can no longer tell which
        # counter moved and reports `inconclusive` forever with everything green
    ),
    (
        "base chain set to policy drop",
        "policy accept;",
        "policy drop;",
        # takes svc-infra off the network entirely
    ),
    (
        "ruleset flush reintroduced",
        "define LAN =",
        "flush ruleset\ndefine LAN =",
        # clobbers firewalld's tables on every load
    ),
)


def directives(rendered: str) -> str:
    """The rendered ruleset with comment lines removed.

    Structural assertions run against this rather than the raw render, because
    the template's header explains at length why it carries no ruleset flush
    and a substring search over the prose fails on that sentence. A gate a file
    can fail by DOCUMENTING the right behaviour gets silenced by rewording
    rather than fixed.
    """
    return "\n".join(
        line for line in rendered.splitlines() if not line.lstrip().startswith("#")
    )


def build_env(extra_templates: dict[str, str] | None = None) -> Environment:
    loader = FileSystemLoader(str(ROOT))
    if extra_templates:
        loader = DictLoader(extra_templates)
    env = Environment(loader=loader, undefined=StrictUndefined, keep_trailing_newline=True)
    env.filters["comment"] = lambda text: "\n".join(
        f"# {line}" for line in text.splitlines()
    )
    return env


def check(rendered: str, ctx: dict, catalog: dict) -> list[str]:
    """Every assertion here is against a value read from the inventory."""
    failures: list[str] = []
    body = directives(rendered)
    tag = f"log_requests={ctx['chat_proxy_log_requests']}"

    match = SOCKET_RE.search(body)
    if not match:
        return [f"{tag}: no `socket cgroupv2 level N \"path\" jump chain` rule found at all"]

    level, path, target = int(match.group(1)), match.group(2), match.group(3)
    components = path.split("/")

    # The level IS the component count -- that is what the nft manual means by
    # level, and getting it wrong by one widens the match to the parent slice.
    if level != len(components):
        failures.append(
            f"{tag}: cgroup level {level} does not match the {len(components)} components "
            f"of {path!r} -- level {len(components) - 1} would match "
            f"{'/'.join(components[:-1])!r}, i.e. every rootless container on the host"
        )
    if level != ctx["chat_egress_cgroup_level"]:
        failures.append(
            f"{tag}: rendered level {level} is not the inventory's "
            f"chat_egress_cgroup_level ({ctx['chat_egress_cgroup_level']})"
        )
    if components[0] != "user.slice":
        failures.append(
            f"{tag}: cgroup path starts at {components[0]!r}; rootless containers live "
            "under user.slice, so this matches nothing"
        )
    expected = [
        "user.slice",
        f"user-{ctx['svc_uid']}.slice",
        f"user@{ctx['svc_uid']}.service",
        "app.slice",
        ctx["chat_egress_unit"],
    ]
    if components != expected:
        failures.append(
            f"{tag}: cgroup path is {path!r}, expected {'/'.join(expected)!r} "
            "built from svc_uid and chat_egress_unit"
        )
    if target != "chat_policy":
        failures.append(f"{tag}: base chain jumps to {target!r}, not chat_policy")

    # The catalog cross-check the docstring has always promised.
    unit = ctx["chat_egress_unit"]
    if not unit.endswith(".service"):
        failures.append(f"{tag}: chat_egress_unit {unit!r} is not a .service unit name")
    elif unit[: -len(".service")] not in catalog:
        failures.append(
            f"{tag}: {unit!r} has no {unit[: -len('.service')]!r} entry in "
            "infra_secret_apps; the nft rule names a unit that will not exist"
        )

    if f"define LAN = {ctx['lan_cidr']}" not in body:
        failures.append(f"{tag}: LAN is not defined from the inventory's lan_cidr")
    if ctx["lan_cidr"] in ("0.0.0.0/0", "0.0.0.0/0.0.0.0"):
        failures.append(f"{tag}: lan_cidr is {ctx['lan_cidr']}, which accepts everything")
    if "ip daddr $LAN accept" not in body:
        failures.append(
            f"{tag}: the only destination accept is not `ip daddr $LAN` -- a literal "
            "here can widen the policy without touching lan_cidr"
        )
    if "counter drop" not in body:
        failures.append(f"{tag}: drop rule lost its unconditional counter")
    # Exactly one, not merely at least one, and this is about the PROBE rather
    # than about the firewall. chat-egress-probe.sh.j2 reads the drop counter by
    # scanning `nft list table` for `counter packets N`, and it requires a single
    # match -- it refuses to guess between two, because attributing a delta to
    # the wrong counter is worse than declining to attribute it at all. So a
    # second counter anywhere in this table leaves the policy working perfectly
    # and pins the verification at `inconclusive` for good: enforcement intact,
    # nothing able to prove it. That is the exact shape of half-failure this
    # feature exists to make impossible, so it is asserted rather than assumed.
    counters = len(re.findall(r"\bcounter\b", body))
    if counters != 1:
        failures.append(
            f"{tag}: expected exactly one `counter` statement in the table, found "
            f"{counters}. chat-egress-probe.sh.j2 refuses to choose between two, so a "
            "second one disarms the verification while leaving the policy intact"
        )
    if body.count("hook output") != 1:
        failures.append(f"{tag}: expected exactly one output base chain")
    if "policy accept" not in body:
        failures.append(
            f"{tag}: base chain is not policy accept -- a drop policy here would take "
            "the whole VM off the network"
        )
    if "delete table" not in body:
        failures.append(f"{tag}: missing the delete-table idempotency guard")
    if "flush ruleset" in body:
        failures.append(f"{tag}: flush ruleset would clobber firewalld's own tables")
    has_log = 'log prefix "chat-egress-drop ' in body
    if has_log is not ctx["chat_proxy_log_requests"]:
        failures.append(f"{tag}: drop logging did not follow chat_proxy_log_requests")
    return failures


def main() -> int:
    main_vars = yaml.safe_load((ROOT / MAIN_VARS).read_text(encoding="utf-8"))
    defaults = yaml.safe_load((ROOT / ROLE_DEFAULTS).read_text(encoding="utf-8"))
    catalog = yaml.safe_load((ROOT / CATALOG).read_text(encoding="utf-8"))["infra_secret_apps"]

    base = {
        "ansible_managed": "Ansible managed",
        "svc_uid": main_vars["svc_uid"],
        "lan_cidr": main_vars["lan_cidr"],
        "chat_egress_unit": main_vars["chat_egress_unit"],
        "chat_egress_cgroup_level": main_vars["chat_egress_cgroup_level"],
    }
    # chat_egress_cgroup_path is a Jinja expression in the role defaults, and
    # the whole point of it living in one place is that the policy, the apply
    # script and the confirm assertion cannot disagree. Render it the same way
    # Ansible will.
    base["chat_egress_cgroup_path"] = (
        build_env().from_string(defaults["chat_egress_cgroup_path"]).render(**base)
    )

    source = (ROOT / TEMPLATE).read_text(encoding="utf-8")
    failures: list[str] = []

    for log_requests in (True, False):
        ctx = dict(base, chat_proxy_log_requests=log_requests)
        rendered = build_env().get_template(TEMPLATE).render(**ctx)
        failures.extend(check(rendered, ctx, catalog))

    # Positive control: prove the assertions above can actually fail.
    for name, find, replace, *_ in MUTATIONS:
        if find not in source:
            failures.append(
                f"self-test {name!r}: anchor {find!r} is no longer in the template, so "
                "this control has silently stopped testing anything"
            )
            continue
        mutated = source.replace(find, replace, 1)
        ctx = dict(base, chat_proxy_log_requests=True)
        try:
            rendered = build_env({TEMPLATE: mutated}).get_template(TEMPLATE).render(**ctx)
        except Exception as exc:  # a mutation that will not even render is caught
            del exc
            continue
        if not check(rendered, ctx, catalog):
            failures.append(
                f"self-test {name!r}: the mutated template PASSED every check; "
                "this gate would not catch it on the live host"
            )

    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    if failures:
        return 1
    print(
        f"Chat egress nftables policy: OK "
        f"(level {base['chat_egress_cgroup_level']} == "
        f"{len(base['chat_egress_cgroup_path'].split('/'))} path components, "
        f"{len(MUTATIONS)} mutations all caught)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
