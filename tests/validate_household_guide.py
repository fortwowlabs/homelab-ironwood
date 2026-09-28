#!/usr/bin/env python3
"""Validate the household guide (docs/household/, served at guide.<domain>).

The guide is read by someone who never sees this repo, on a phone, mid-task.
Four ways it can fail quietly, each checked here:

  * a broken link or missing screenshot — renders fine, dead-ends the reader;
  * a secret — the site is deliberately outside SSO, so anything on it is
    readable by every LAN and tailnet device;
  * infrastructure vocabulary — the writing rule in the design spec, which
    erodes one "just this once" at a time unless something enforces it;
  * a page with no "Last checked" line — the only signal the reader has that
    a page describes what the apps do today.

Rendering goes through scripts/render_household_guide.py, the same code the
deploy runs, so a page this gate accepts is the page that gets served. It also
renders twice and compares bytes: the deploy's `changed=0` proof depends on
the output being deterministic.

Positive controls: before judging the real pages, every rule is run against a
fixture that MUST violate it. If any fixture comes back clean the gate fails —
a checker that finds nothing because it did not run looks identical to a
clean guide (CLAUDE.md, "So does scanning").
"""

from __future__ import annotations

import filecmp
import re
import sys
import tempfile
from pathlib import Path

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "links"

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs/household"

sys.path.insert(0, str(ROOT / "scripts"))
from render_household_guide import render_site  # noqa: E402
from validate_secrets import SIGNATURES  # noqa: E402

TITLE_RE = re.compile(r"^# \S", re.MULTILINE)
LAST_CHECKED_RE = re.compile(
    r"^_Last checked: (?:\d{4}-\d{2}-\d{2} on [^_\n]+|not yet)\._$", re.MULTILINE
)
VAULT_RE = re.compile(r"\bvault_[a-z0-9_]+")
# Any "password:" / "PIN =" style line. Pages say "the password Brandon gave
# you" instead; a colon after the word is how a value ends up on the page.
CREDENTIAL_RE = re.compile(r"(?i)\b(?:password|passcode|pin|api key|token)\s*[:=]")
JARGON_RE = re.compile(
    r"\b(?:containers?|NFS|indexers?|SSO|Quadlets?|podman|VPN jail)\b", re.IGNORECASE
)
# An unfilled `<her-username>`-style placeholder. Markdown passes it through as
# a raw HTML tag, so the browser silently hides it and the sentence around it
# just reads wrong. Pages use no raw HTML or autolinks, so any <word...> is one.
PLACEHOLDER_RE = re.compile(r"<[a-z][^<>]*>", re.IGNORECASE)
REF_RE = re.compile(r'(?:href|src)="([^"]+)"')
EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|#|/)", re.IGNORECASE)

GOOD_PAGE = "# Fine\n\nHello.\n\n_Last checked: not yet._\n"
# Each fixture must produce at least one problem from source_problems().
BAD_PAGES = {
    "no title": "Hello.\n\n_Last checked: not yet._\n",
    "no last-checked": "# T\n\nHello.\n",
    "last-checked not last": "# T\n\n_Last checked: not yet._\n\nMore text.\n",
    "vault name": "# T\n\nUse vault_jellyfin_password.\n\n_Last checked: not yet._\n",
    "credential": "# T\n\nPassword: example-fixture\n\n_Last checked: not yet._\n",
    "jargon": "# T\n\nThe container restarts.\n\n_Last checked: not yet._\n",
    "signature": "# T\n\nghp_" + "A" * 36 + "\n\n_Last checked: not yet._\n",
    "placeholder": "# T\n\nYour name is <her-username>.\n\n_Last checked: not yet._\n",
    "capitalised placeholder": "# T\n\nYour name is <Her-Username>.\n\n_Last checked: not yet._\n",
}


def source_problems(name: str, text: str) -> list[str]:
    problems: list[str] = []
    if not TITLE_RE.search(text):
        problems.append(f"{name}: no '# ' title line")
    if len(LAST_CHECKED_RE.findall(text)) != 1:
        problems.append(f"{name}: needs exactly one '_Last checked: ..._' line")
    non_blank = [line for line in text.splitlines() if line.strip()]
    if not non_blank or LAST_CHECKED_RE.fullmatch(non_blank[-1].rstrip()) is None:
        problems.append(f"{name}: '_Last checked: ..._' line must be the last line")
    for label, pattern in (
        ("vault variable name", VAULT_RE),
        ("credential-style assignment", CREDENTIAL_RE),
        ("infrastructure vocabulary", JARGON_RE),
        ("unfilled placeholder", PLACEHOLDER_RE),
    ):
        match = pattern.search(text)
        if match:
            problems.append(f"{name}: {label} {match.group(0)!r}")
    for label, pattern in SIGNATURES.items():
        if pattern.search(text):
            problems.append(f"{name}: looks like a {label}")
    return problems


def link_problems(out: Path) -> list[str]:
    problems: list[str] = []
    for page in sorted(out.glob("*.html")):
        for ref in REF_RE.findall(page.read_text(encoding="utf-8")):
            if EXTERNAL_RE.match(ref):
                continue
            target = (page.parent / ref.split("#", 1)[0]).resolve()
            if not target.is_file() or out.resolve() not in target.parents:
                problems.append(f"{page.name}: broken link or image {ref!r}")
    return problems


def control_problems() -> list[str]:
    problems: list[str] = []
    if source_problems("good fixture", GOOD_PAGE):
        problems.append("control: the good fixture was rejected")
    for label, text in BAD_PAGES.items():
        if not source_problems(label, text):
            problems.append(f"control: the {label!r} fixture was not caught")
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "src"), Path(tmp, "out")
        src.mkdir()
        (src / "index.md").write_text(GOOD_PAGE + "\n[gone](missing.md)\n", encoding="utf-8")
        render_site(src, out)
        if not link_problems(out):
            problems.append("control: the broken-link fixture was not caught")
    return problems


def determinism_problems() -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        first, second = Path(tmp, "a"), Path(tmp, "b")
        written = render_site(SRC, first)
        render_site(SRC, second)
        _, mismatch, errors = filecmp.cmpfiles(first, second, written, shallow=False)
        return [f"{name}: renders differently twice" for name in mismatch + errors]


def main() -> int:
    problems = control_problems()
    if problems:
        print("Household guide gate is not trustworthy:", *problems, sep="\n  ", file=sys.stderr)
        return 1

    pages = sorted(SRC.glob("*.md"))
    for page in pages:
        problems += source_problems(page.name, page.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            written = render_site(SRC, Path(tmp))
        except ValueError as exc:
            problems.append(str(exc))
            written = []
        else:
            problems += link_problems(Path(tmp))
    if written:
        problems += determinism_problems()

    if problems:
        print("Household guide: FAILED", *problems, sep="\n  ", file=sys.stderr)
        return 1
    assets = len(written) - len(pages)
    print(
        f"Household guide: OK ({len(pages)} pages, {assets} assets, "
        f"{len(BAD_PAGES) + 1} positive controls caught)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
