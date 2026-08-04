#!/usr/bin/env python3
"""Severe alerts must publish to the alert topic, not the muted deploy feed.

inventory/group_vars/all/main.yml defines two ntfy topics and they are not
interchangeable:

  ntfy_topic:       homelab-deploy   routine; every deploy notice; subscribed muted
  ntfy_alert_topic: homelab-alerts   the one that is allowed to wake somebody

Four scripts published straight to ${NTFY_TOPIC}, including leak-canary.sh —
whose loudest message is "LEAK: download jail ... download stack STOPPED" at
priority urgent, the single most severe alert in the estate. It was landing on
the muted feed.

This is the failure mode CLAUDE.md keeps describing, in its nastiest form. There
is nothing to observe on the sending side: the curl succeeds, ntfy returns 200,
the journal records a delivered alert, the unit exits 0, `make verify` is green.
The alert is generated correctly, delivered correctly, and read by nobody. The
only way to catch it by hand is to notice that a phone did not ring for an
incident nobody knew had happened.

So it is checked here instead. For every shell template that publishes an ntfy
message at priority `high` or `urgent`, the URL it posts to must resolve through
NTFY_ALERT_TOPIC — in practice the house idiom:

    topic="${NTFY_ALERT_TOPIC:-${NTFY_TOPIC}}"
    ... "${NTFY_URL}/${topic}"

The fallback is deliberate and accepted: a host whose /etc/homelab-notify.env
predates NTFY_ALERT_TOPIC should still deliver somewhere rather than post to an
empty topic name.

Three things this gate refuses to do quietly, per the standing rule that a check
must distinguish "none found" from "could not look":

  - A listed template that does not exist is a failure, not a skip.
  - A severe template whose publish URL cannot be located is a failure. "I could
    not find where it posts" must never read as "it posts to the right place".
  - A template outside ALERT_TEMPLATES that sets a Priority header is a failure,
    so a newly added alerter cannot be born ungated.

And the gate has a positive control of its own: if the severity scan classifies
nothing as severe across the whole set, the pattern has broken rather than the
estate having gone quiet, and that fails too.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Every shell template that publishes to ntfy. Explicit, in the manner of
# validate_scan_readonly.py's SCAN_PATHS — but backed by the discovery sweep at
# the bottom of main(), so forgetting to extend this list is itself caught.
ALERT_TEMPLATES = (
    "roles/mon/templates/disk-alert.sh.j2",
    "roles/mon/templates/failed-units-watch.sh.j2",
    "roles/pve_mon/templates/diskguard.sh.j2",
    "roles/pve_mon/templates/smartd-ntfy.sh.j2",
    "roles/pve_mon/templates/zed-ntfy.sh.j2",
    "roles/service_vm/templates/notify-failure.sh.j2",
    "roles/svc_download/templates/backup-dl-appdata.sh.j2",
    "roles/svc_download/templates/leak-canary.sh.j2",
    "roles/svc_infra/templates/backup-infra-appdata.sh.j2",
    "roles/svc_media/templates/backup-media.sh.j2",
    # Publishes at `min`, so the severity rules below pass it without looking
    # at its topic — and yet it is the one script whose entire purpose is the
    # alert topic. It is listed because the discovery sweep at the bottom of
    # main() would otherwise fail it for setting a Priority header while
    # unregistered, and because an alerter absent from this list is an alerter
    # whose routing nobody is checking. Its own routing is enforced in the
    # script instead: it refuses to fall back to NTFY_TOPIC, which every other
    # template here is allowed to do.
    "roles/svc_infra/templates/alert-canary.sh.j2",
)

# Where the discovery sweep looks for an alerter nobody registered above.
CANDIDATE_GLOBS = ("roles/**/templates/*.sh.j2", "roles/**/files/*.sh")

# ntfy's priority header, as these scripts write it: either a literal
# (-H "Priority: urgent") or a positional passed by the script's own alert()
# helper (-H "Priority: $1").
PRIORITY_RE = re.compile(r'-H\s+"Priority:\s*([^"]+)"')

# The publish URL: ${NTFY_URL}/${something}. The something is either the topic
# variable itself or a shell variable holding it.
PUBLISH_RE = re.compile(
    r"\$\{NTFY_URL\}/\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[-?][^}]*)?\}"
)

SEVERE_LITERALS = {"high", "urgent", "4", "5"}
ROUTINE_LITERALS = {"min", "low", "default", "1", "2", "3"}


def assignment(text: str, name: str) -> str | None:
    """The right-hand side of the last `name=...` assignment, or None."""
    matches = re.findall(
        rf"^[ \t]*{re.escape(name)}=(.*)$", text, re.MULTILINE
    )
    return matches[-1] if matches else None


def is_severe(priority: str) -> bool | None:
    """True/False for a decided priority, None when it is unrecognised."""
    value = priority.strip()
    # An interpolated priority ($1, ${sev}) cannot be decided statically. Treat
    # it as severe: every script in this repo that does this passes `urgent` or
    # `high` down some branch, and guessing the other way is how a real alert
    # ends up on the muted topic.
    if "$" in value:
        return True
    lowered = value.lower()
    if lowered in SEVERE_LITERALS:
        return True
    if lowered in ROUTINE_LITERALS:
        return False
    return None


def check_template(relative: str, text: str) -> tuple[list[str], bool]:
    """Return (failures, whether this template publishes anything severe)."""
    failures: list[str] = []

    priorities = PRIORITY_RE.findall(text)
    if not priorities:
        failures.append(
            f"{relative} is listed as an alert template but sets no "
            f"'Priority:' header. Either it no longer publishes to ntfy (drop "
            f"it from ALERT_TEMPLATES) or the header moved and this gate can no "
            f"longer see what severity it sends."
        )
        return failures, False

    severe = False
    for priority in priorities:
        verdict = is_severe(priority)
        if verdict is None:
            failures.append(
                f"{relative} publishes at an unrecognised priority "
                f"{priority.strip()!r}. This gate cannot tell whether that is "
                f"severe, so it will not pass it — use an ntfy priority name or "
                f"1-5, or teach SEVERE_LITERALS/ROUTINE_LITERALS about it."
            )
            continue
        severe = severe or verdict

    if not severe:
        return failures, False

    targets = PUBLISH_RE.findall(text)
    if not targets:
        failures.append(
            f"{relative} publishes at high/urgent but this gate could not find "
            f"the URL it posts to. It looks for ${{NTFY_URL}}/${{...}}; if the "
            f"URL is now built some other way, the routing is unverified — which "
            f"is not the same as correct. Rewrite it in the house idiom or "
            f"extend PUBLISH_RE."
        )
        return failures, True

    for name in targets:
        if name == "NTFY_ALERT_TOPIC":
            continue
        if name == "NTFY_TOPIC":
            failures.append(
                f"{relative} publishes at high/urgent to ${{NTFY_TOPIC}}, which "
                f"is homelab-deploy — the routine feed that is subscribed muted. "
                f"The send will succeed and nobody will be told. Use "
                f'topic="${{NTFY_ALERT_TOPIC:-${{NTFY_TOPIC}}}}".'
            )
            continue
        rhs = assignment(text, name)
        if rhs is None:
            failures.append(
                f"{relative} posts to ${{NTFY_URL}}/${{{name}}} but never "
                f"assigns {name}, so where the alert lands cannot be determined "
                f"from the file."
            )
        elif "NTFY_ALERT_TOPIC" not in rhs:
            failures.append(
                f"{relative} posts at high/urgent to ${{{name}}}, assigned "
                f"{rhs.strip()!r}, which does not derive from NTFY_ALERT_TOPIC. "
                f"Severe alerts must resolve the alert topic, not the muted "
                f"deploy feed."
            )

    return failures, True


def main() -> int:
    failures: list[str] = []
    severe_templates = 0

    for relative in ALERT_TEMPLATES:
        path = ROOT / relative
        if not path.exists():
            failures.append(
                f"{relative} is listed in {Path(__file__).name} but does not "
                f"exist — remove it from ALERT_TEMPLATES or restore the file; a "
                f"gate that silently skips its subject is not a gate."
            )
            continue
        template_failures, severe = check_template(
            relative, path.read_text(encoding="utf-8")
        )
        failures.extend(template_failures)
        severe_templates += int(severe)

    # An alerter nobody registered. Cheap to detect and the only way this list
    # stays honest: the four bugs this gate exists for were all in files that
    # predated it, and the next one will be in a file added after it.
    listed = set(ALERT_TEMPLATES)
    for glob in CANDIDATE_GLOBS:
        for path in sorted(ROOT.glob(glob)):
            relative = path.relative_to(ROOT).as_posix()
            if relative in listed or not path.is_file():
                continue
            if PRIORITY_RE.search(path.read_text(encoding="utf-8")):
                failures.append(
                    f"{relative} publishes an ntfy message but is not in "
                    f"ALERT_TEMPLATES, so its topic routing is unchecked. Add "
                    f"it to the list."
                )

    # The gate's own positive control. If nothing at all reads as severe, the
    # likely cause is PRIORITY_RE no longer matching how these scripts are
    # written — not that the estate stopped sending urgent alerts.
    if severe_templates == 0:
        failures.append(
            f"none of the {len(ALERT_TEMPLATES)} listed templates were found to "
            f"publish at high or urgent priority. That is not plausible — "
            f"leak-canary.sh alone sends urgent — so the severity scan has "
            f"broken and a clean result here means nothing."
        )

    if failures:
        print("Alert topic routing validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(
        f"Alert topic routing: OK ({severe_templates} of "
        f"{len(ALERT_TEMPLATES)} ntfy templates publish at high/urgent and all "
        f"resolve NTFY_ALERT_TOPIC)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
