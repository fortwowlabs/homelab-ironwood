# Household Guide — Stage 1 (watch, read, listen) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make movies/TV, ebooks and audiobooks actually work for Brandon and
his fiancée, and publish a phone-friendly guide at `https://guide.fortwow.dev`
that she can follow unaided.

**Architecture:** Markdown pages in `docs/household/` are rendered by one
Python script (`scripts/render_household_guide.py`) that both the validate
gate and the deploy call. The deploy renders on the controller and copies the
HTML to svc-infra, where a second static-web-server catalog entry (`guide`,
same image as `scan-reports`) serves it behind a new open Caddy vhost; the
Homepage tile appears automatically from `caddy_services`. Audiobookshelf
gains a read-only mount of the Calibre library. Everything else is UI setup
done by hand and verified by use.

**Tech Stack:** Ansible (rootless podman Quadlets on Rocky 10), Python 3.12,
Python-Markdown 3.11, static-web-server, Caddy, Jellyfin, Seerr,
Audiobookshelf, Calibre-Web Automated, Shelfmark, Tailscale.

**Spec:** `docs/superpowers/specs/2026-09-28-household-guide-design.md`

## Global Constraints

- Work on ONE branch, `feat/household-guide`, off current `main`. Follow the
  `CLAUDE.md` change workflow exactly: iterate with dirty deploys, commit,
  confirm `git status --porcelain` is empty, then `make deploy-proof`.
- **Never `git add -A`.** Stage explicit paths only.
- **Never echo a secret** — API keys, passwords, vault values. Where a
  command needs an API key, read it into a shell variable on the remote host
  and use it there; never print it.
- **No passwords in any guide page, ever.** Usernames only.
- Guide pages use no infrastructure vocabulary: no *container, NFS, indexer,
  SSO, Quadlet, podman, VPN jail*. The gate enforces this.
- Every guide page has a `# ` title line and ends with exactly one line of the
  form `_Last checked: YYYY-MM-DD on <devices>._` or `_Last checked: not yet._`
- `guide` is deliberately **not** in `sso_protected_services`.
- Ansible does not automate application wizards (`docs/services.md`). UI setup
  steps are marked 🧑 and done by the operator; the implementer verifies them
  afterwards with read-only checks.
- Devices in scope: iPhone/iPad, Android phone/tablet, Google TV Streamer.
- Apps: Jellyfin (Google TV: *Jellyfin for Android TV*; Android: *Jellyfin*;
  iPhone: *Swiftfin*, falling back to *Jellyfin Mobile* only if Swiftfin fails
  verification), Audiobookshelf (Android: official *Audiobookshelf*; iPhone:
  *Still: for Audiobookshelf*), Readest (both).
- Addresses used in the guide: `https://jellyfin.fortwow.dev`,
  `https://seerr.fortwow.dev`, `https://shelfmark.fortwow.dev`,
  `https://abs.fortwow.dev`, `https://calibre-web.fortwow.dev`,
  `https://guide.fortwow.dev`, `https://home.fortwow.dev`.
- Host addresses: svc-media `192.168.1.30`, svc-download `192.168.1.31`,
  svc-infra `192.168.1.32`. SSH as `straderb`.

## File map

| File | Responsibility |
|---|---|
| `scripts/render_household_guide.py` (new) | The one renderer: Markdown dir → deterministic static HTML dir |
| `tests/validate_household_guide.py` (new) | Gate: page rules, link/asset resolution, determinism, positive controls |
| `requirements.txt` | Pin `markdown==3.11` (controller runtime; the deploy needs it) |
| `docs/household/*.md`, `docs/household/img/` (new) | Guide source |
| `inventory/group_vars/all/infra-apps.yml` | `guide` static-web-server entry |
| `inventory/group_vars/all/main.yml` | `household_guide_dir`; `guide` in `caddy_services`; SSO-open comment |
| `roles/svc_infra/tasks/guide.yml` (new) | Render on controller, copy to svc-infra, prune stale files |
| `roles/svc_infra/tasks/main.yml` | Import `guide.yml` |
| `roles/svc_media/templates/audiobookshelf.container.j2` | Read-only `/books` mount |
| `docs/services.md`, `docs/first-login-walkthrough.md` | Correct paths; record guide + household accounts |
| `docs/superpowers/specs/2026-09-28-household-guide-design.md` | Status line at the end |

---

### Task 1: Branch and read-only readiness audit

Establishes the facts every setup task starts from. Touches nothing.

**Files:** none (findings go into a scratch file and the task report).

**Interfaces:**
- Produces: a findings list the operator uses in Tasks 5–6 — for each item,
  `ready` or `needs: <what>`.

- [ ] **Step 1: Branch**

```bash
cd ~/dev/homelab-ironwood
git switch main && git pull --ff-only
git switch -c feat/household-guide
```

- [ ] **Step 2: Player apps — public status endpoints**

```bash
curl -s https://jellyfin.fortwow.dev/System/Info/Public | python3 -c 'import json,sys; d=json.load(sys.stdin); print("jellyfin wizard done:", d["StartupWizardCompleted"])'
curl -s https://seerr.fortwow.dev/api/v1/settings/public | python3 -c 'import json,sys; print("seerr initialized:", json.load(sys.stdin)["initialized"])'
curl -s https://abs.fortwow.dev/status | python3 -c 'import json,sys; print("abs initialized:", json.load(sys.stdin)["isInit"])'
```

Expected on 2026-09-27: `False`, `False`, `False`. Record what you see.

- [ ] **Step 3: Download pipeline — arr APIs, key never printed**

Write this to the scratchpad as `arr-audit.sh` and run it with
`ssh straderb@192.168.1.31 'bash -s' < arr-audit.sh`:

```bash
set -u
key() { sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "/srv/appdata/$1/config.xml"; }
get() { # app port path
  local k; k=$(key "$1")
  curl -s -m 10 -H "X-Api-Key: $k" "http://192.168.1.31:$2$3"
}
echo "== prowlarr indexers";  get prowlarr 9696 /api/v1/indexer | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d), [i["name"] for i in d])'
echo "== prowlarr apps";      get prowlarr 9696 /api/v1/applications | python3 -c 'import json,sys; print([a["name"] for a in json.load(sys.stdin)])'
for app in "sonarr 8989" "radarr 7878"; do
  set -- $app
  echo "== $1 root folders";     get "$1" "$2" /api/v3/rootfolder | python3 -c 'import json,sys; print([r["path"] for r in json.load(sys.stdin)])'
  echo "== $1 download clients"; get "$1" "$2" /api/v3/downloadclient | python3 -c 'import json,sys; print([(c["name"], c["enable"]) for c in json.load(sys.stdin)])'
  echo "== $1 indexers";         get "$1" "$2" /api/v3/indexer | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'
done
echo "== sabnzbd categories"; sudo grep -A0 -E '^\[\[[a-z]+\]\]' /srv/appdata/sabnzbd/sabnzbd.ini
echo "== shelfmark config files"; sudo ls -la /srv/appdata/shelfmark
```

Ready means: ≥1 Prowlarr indexer; Prowlarr apps include Sonarr and Radarr;
Sonarr root folder under `/data/tv` and Radarr under `/data/movies` (confirm
exact paths against the output); each has an enabled SABnzbd client and ≥1
indexer; SABnzbd has `tv`, `movies`, `books`, `audiobooks` categories.
Anything else is a `needs:` item.

- [ ] **Step 4: Book pipeline on disk**

```bash
ssh straderb@192.168.1.30 'ls -la /srv/media/book-ingest /srv/media/audiobooks /srv/media/ebooks; find /srv/media/books -maxdepth 2 | head -30'
```

A file sitting in `book-ingest` means Calibre-Web's ingest is stuck or
unprocessed — record it as `needs: investigate CWA ingest` and look at
`journalctl --user -M homelab@ -u calibre-web-automated` on svc-media.

- [ ] **Step 5: Report**

Give the operator the findings list. Do not fix anything in this task. No commit.

---

### Task 2: Guide renderer and its gate

**Files:**
- Create: `tests/validate_household_guide.py`
- Create: `scripts/render_household_guide.py`
- Create: `docs/household/index.md` (stub; filled in Task 7)
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `render_household_guide.render_site(src: Path, out: Path) -> list[str]`
  — renders every `src/*.md` to `out/<stem>.html` and copies `src/img/**`,
  returns sorted relative POSIX paths written; raises `ValueError` on a page
  with no `# ` title or a missing `index.md`.
- Produces: `render_household_guide.render_page(text: str, name: str) -> str`.
- Produces: CLI `render_household_guide.py --src DIR --out DIR` (exit 0 ok,
  1 on `ValueError`).
- Produces: gate group `links`, run by `make validate-links`.

- [ ] **Step 1: Pin the dependency and install it**

Add to `requirements.txt` after `requests`:

```text
markdown==3.11
```

Run: `make deps-dev`
Expected: installs `Markdown-3.11`.

- [ ] **Step 2: Write the failing gate**

Create `tests/validate_household_guide.py`:

```python
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
PLACEHOLDER_RE = re.compile(r"<[a-z][^<>]*>")
REF_RE = re.compile(r'(?:href|src)="([^"]+)"')
EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|#|/)", re.IGNORECASE)

GOOD_PAGE = "# Fine\n\nHello.\n\n_Last checked: not yet._\n"
# Each fixture must produce at least one problem from source_problems().
BAD_PAGES = {
    "no title": "Hello.\n\n_Last checked: not yet._\n",
    "no last-checked": "# T\n\nHello.\n",
    "vault name": "# T\n\nUse vault_jellyfin_password.\n\n_Last checked: not yet._\n",
    "credential": "# T\n\nPassword: example-fixture\n\n_Last checked: not yet._\n",
    "jargon": "# T\n\nThe container restarts.\n\n_Last checked: not yet._\n",
    "signature": "# T\n\nghp_" + "A" * 36 + "\n\n_Last checked: not yet._\n",
    "placeholder": "# T\n\nYour name is <her-username>.\n\n_Last checked: not yet._\n",
}


def source_problems(name: str, text: str) -> list[str]:
    problems: list[str] = []
    if not TITLE_RE.search(text):
        problems.append(f"{name}: no '# ' title line")
    if len(LAST_CHECKED_RE.findall(text)) != 1:
        problems.append(f"{name}: needs exactly one '_Last checked: ..._' line")
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
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python tests/validate_household_guide.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'render_household_guide'`.

- [ ] **Step 4: Write the renderer**

Create `scripts/render_household_guide.py` (mode 0755):

```python
#!/usr/bin/env python3
"""Render docs/household/*.md into the static site served at guide.<domain>.

One renderer, two callers: roles/svc_infra/tasks/guide.yml runs it on the
controller at deploy time, and tests/validate_household_guide.py runs it in
`make validate`. A page the gate accepts is therefore byte-for-byte the page
that gets served.

The output must be deterministic — no timestamps, sorted traversal — because
the deploy copies it with checksums and `make deploy-proof` requires an
unchanged guide to report changed=0.

Links between pages are written as `other.md` in the source, so they work on
GitHub and in an editor too; they are rewritten to `other.html` here.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
from pathlib import Path
from string import Template

import markdown

EXTENSIONS = ["tables", "fenced_code", "sane_lists", "attr_list"]
ASSET_DIR = "img"
TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
# A relative href ending in .md (optionally with a #fragment). Anything with a
# scheme, an absolute path or a bare fragment is left alone.
MD_LINK_RE = re.compile(r'href="(?![a-z][a-z0-9+.-]*:|/|#)([^"#]+)\.md(#[^"]*)?"', re.IGNORECASE)

STYLE = """
:root { --bg:#fbfaf7; --fg:#1d1d1f; --muted:#5f6368; --accent:#2f6f4e;
        --card:#ffffff; --line:#e3e1dc; --code:#f1efea; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141517; --fg:#ececec; --muted:#a0a4a8; --accent:#7cc8a0;
          --card:#1d1f22; --line:#2e3135; --code:#25282c; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:17px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { position:sticky; top:0; background:var(--card); border-bottom:1px solid var(--line);
         padding:12px 16px; }
header a { color:var(--accent); font-weight:600; text-decoration:none; }
main { max-width:720px; margin:0 auto; padding:16px 16px 48px; }
h1 { font-size:1.6rem; line-height:1.25; margin:.6em 0 .4em; }
h2 { font-size:1.25rem; margin:1.6em 0 .4em; padding-top:.4em; border-top:1px solid var(--line); }
h3 { font-size:1.05rem; margin:1.2em 0 .3em; }
a { color:var(--accent); }
ol, ul { padding-left:1.4em; }
li { margin:.35em 0; }
img { max-width:100%; height:auto; border:1px solid var(--line); border-radius:8px; }
code { background:var(--code); padding:.1em .35em; border-radius:4px; font-size:.95em;
       overflow-wrap:anywhere; }
table { width:100%; border-collapse:collapse; display:block; overflow-x:auto; }
th, td { text-align:left; padding:8px; border-bottom:1px solid var(--line); vertical-align:top; }
blockquote { margin:1em 0; padding:.6em 1em; background:var(--card);
             border-left:4px solid var(--accent); border-radius:4px; }
em:last-child { color:var(--muted); }
"""

PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title · Home guide</title>
<style>$style</style>
</head>
<body>
<header><a href="index.html">&#8962; Home guide</a></header>
<main>
$body
</main>
</body>
</html>
"""
)


def render_page(text: str, name: str) -> str:
    match = TITLE_RE.search(text)
    if match is None:
        raise ValueError(f"{name}: no '# ' title line")
    body = markdown.markdown(text, extensions=EXTENSIONS, output_format="html")
    body = MD_LINK_RE.sub(lambda m: f'href="{m.group(1)}.html{m.group(2) or ""}"', body)
    return PAGE.substitute(title=html.escape(match.group(1).strip()), style=STYLE, body=body)


def render_site(src: Path, out: Path) -> list[str]:
    if not (src / "index.md").is_file():
        raise ValueError(f"{src}: no index.md, so the site would have no front page")
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for page in sorted(src.glob("*.md")):
        target = out / f"{page.stem}.html"
        target.write_text(
            render_page(page.read_text(encoding="utf-8"), page.name),
            encoding="utf-8",
            newline="\n",
        )
        written.append(target.name)
    assets = src / ASSET_DIR
    if assets.is_dir():
        for asset in sorted(p for p in assets.rglob("*") if p.is_file()):
            relative = asset.relative_to(src)
            (out / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(asset, out / relative)
            written.append(relative.as_posix())
    return sorted(written)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        written = render_site(args.src, args.out)
    except ValueError as exc:
        print(f"render_household_guide: {exc}", file=sys.stderr)
        return 1
    print(f"rendered {len(written)} files into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run it — expect the missing-front-page failure**

Run: `.venv/bin/python tests/validate_household_guide.py`
Expected: FAIL with `docs/household: no index.md, so the site would have no front page`.
(Controls pass first — they build their own fixture site.)

- [ ] **Step 6: Add the stub front page**

Create `docs/household/index.md`:

```markdown
# Home guide

How to use the things the home server does for us. This page is filled in
as each part is set up and tested.

_Last checked: not yet._
```

- [ ] **Step 7: Run the gate and the full suite**

Run: `.venv/bin/python tests/validate_household_guide.py`
Expected: `Household guide: OK (1 pages, 0 assets, 8 positive controls caught)`

Run: `make validate`
Expected: exit 0. (`validate-python` lints both new files with ruff; fix any
finding it reports rather than suppressing it.)

- [ ] **Step 8: Commit**

```bash
chmod 0755 scripts/render_household_guide.py
git add requirements.txt scripts/render_household_guide.py tests/validate_household_guide.py docs/household/index.md
git commit -m "feat: render the household guide, gated on links, secrets and wording"
git push -u origin feat/household-guide
```

---

### Task 3: Serve the guide at guide.fortwow.dev

**Files:**
- Modify: `inventory/group_vars/all/main.yml` (near `scan_report_dir`, in
  `caddy_services`, and the "Deliberately NOT protected" comment above
  `sso_protected_services`)
- Modify: `inventory/group_vars/all/infra-apps.yml` (after `scan-reports`)
- Create: `roles/svc_infra/tasks/guide.yml`
- Modify: `roles/svc_infra/tasks/main.yml` (after the `files.yml` import)

**Interfaces:**
- Consumes: CLI `scripts/render_household_guide.py --src --out` (Task 2).
- Produces: `household_guide_dir` = `/opt/homelab/appdata/guide`; a `guide`
  service on svc-infra port 8086; `https://guide.fortwow.dev`; a Homepage tile
  in group `Household`.

- [ ] **Step 1: Directory variable**

In `inventory/group_vars/all/main.yml`, directly after the
`scan_report_dir:` line, add:

```yaml
# The household guide's served files (docs/household/, rendered by
# scripts/render_household_guide.py in roles/svc_infra/tasks/guide.yml).
# Must match the `guide` volume source in infra-apps.yml.
household_guide_dir: /opt/homelab/appdata/guide
```

- [ ] **Step 2: Catalog entry**

In `inventory/group_vars/all/infra-apps.yml`, after the `scan-reports`
entry's `backup_paths: []`, add:

```yaml

  # The household guide — docs/household/ rendered to static HTML at deploy
  # time by roles/svc_infra/tasks/guide.yml. Same static-web-server digest as
  # scan-reports on purpose: one static file server, two directories, and a
  # bump that moved one without the other would be a decision nobody made.
  # Directory listing off: the guide has a front page. Nothing to back up —
  # every file is regenerated from git on each deploy.
  guide:
    image: "ghcr.io/static-web-server/static-web-server@sha256:2c1a7c3e0feaea5859307403b74e1c575f3ec1499094fc077344173d11abaae2"
    ui_port: 8086
    container_port: 8080
    user: "0"
    volumes:
      - /opt/homelab/appdata/guide:/public:ro
    env:
      SERVER_ROOT: /public
      SERVER_PORT: "8080"
      SERVER_DIRECTORY_LISTING: "false"
    backup_paths: []
```

- [ ] **Step 3: Caddy vhost**

In `caddy_services`, after the `scan:` line, add:

```yaml
  # The household guide. Deliberately open (not in sso_protected_services):
  # the family accounts cannot pass the admins-only rule, and the guide holds
  # no secrets — tests/validate_household_guide.py fails the build if a page
  # looks like it does.
  guide:       { backend: "{{ hostvars[infra_host].ansible_host }}:8086", group: Household, icon: bookstack }
```

In the comment block above `sso_protected_services`, change
`bambuddy, the cockpit-* vhosts (own PAM auth), and shelfmark.` to
`bambuddy, the cockpit-* vhosts (own PAM auth), shelfmark, and guide (the
household guide, which must be readable by accounts outside admins).`

- [ ] **Step 4: Run the catalog gates**

Run: `make validate`
Expected: exit 0. `validate_infra_catalog.py` checks the port is unclaimed
and the required fields exist; `validate_sso.py` checks nothing new is
protected by accident. If a gate names a missing field or a port clash,
fix the entry — do not change the gate.

- [ ] **Step 5: The publish task**

Create `roles/svc_infra/tasks/guide.yml`:

```yaml
---
# The household guide (docs/household/ -> https://guide.<domain>).
#
# Rendered on the CONTROLLER by scripts/render_household_guide.py — the same
# renderer tests/validate_household_guide.py runs — then copied here and served
# read-only by the `guide` infra_apps entry. static-web-server reads files per
# request, so new content needs no restart.
#
# Idempotence is the point of the shape below. The scratch directory and the
# render are controller-side and report changed_when: false; the copy compares
# checksums; stale-file removal only fires when a page was deleted from git.
# An unchanged guide therefore contributes nothing to `make deploy-proof`.
# This is not the metrics-file trap in CLAUDE.md: the content changes only
# when a commit changes it.
- name: Render and publish the household guide
  block:
    - name: Create a controller scratch directory for the household guide
      ansible.builtin.tempfile:
        state: directory
        suffix: -household-guide
      delegate_to: localhost
      become: false
      run_once: true
      check_mode: false
      changed_when: false
      register: household_guide_build

    - name: Render the household guide on the controller
      ansible.builtin.command:
        argv:
          - "{{ ansible_playbook_python }}"
          - "{{ playbook_dir }}/scripts/render_household_guide.py"
          - --src
          - "{{ playbook_dir }}/docs/household"
          - --out
          - "{{ household_guide_build.path }}"
      delegate_to: localhost
      become: false
      run_once: true
      check_mode: false
      changed_when: false

    - name: List the rendered household guide files
      ansible.builtin.find:
        paths: "{{ household_guide_build.path }}"
        recurse: true
      delegate_to: localhost
      become: false
      run_once: true
      check_mode: false
      register: household_guide_rendered

    - name: Copy the household guide to its served directory
      ansible.builtin.copy:
        src: "{{ household_guide_build.path }}/"
        dest: "{{ household_guide_dir }}/"
        owner: "{{ svc_uid }}"
        group: "{{ svc_gid }}"
        mode: "0644"
        directory_mode: "0755"

    - name: List the household guide files currently served
      ansible.builtin.find:
        paths: "{{ household_guide_dir }}"
        recurse: true
      register: household_guide_served

    # A page deleted from git must stop being served; `copy` only ever adds.
    - name: Remove household guide files that are no longer in the source
      ansible.builtin.file:
        path: "{{ item }}"
        state: absent
      loop: >-
        {{ household_guide_served.files | map(attribute='path')
           | reject('in', household_guide_rendered.files | map(attribute='path')
                    | map('regex_replace',
                          '^' ~ (household_guide_build.path | regex_escape),
                          household_guide_dir)
                    | list)
           | list }}
  always:
    - name: Remove the controller scratch directory
      ansible.builtin.file:
        path: "{{ household_guide_build.path }}"
        state: absent
      delegate_to: localhost
      become: false
      run_once: true
      check_mode: false
      changed_when: false
      when: household_guide_build.path is defined
```

- [ ] **Step 6: Import it**

In `roles/svc_infra/tasks/main.yml`, directly after the
`Configure infra files` import (which creates the appdata directory from the
catalog volume), add:

```yaml

- name: Publish the household guide
  ansible.builtin.import_tasks: guide.yml
  tags: [files, guide]
```

- [ ] **Step 7: Validate**

Run: `make validate`
Expected: exit 0 (ansible-lint and yamllint cover the new task file).

- [ ] **Step 8: Deploy svc-infra, then svc-media**

svc-infra first so the backend exists before Caddy's smoke test probes it.

Run: `make infra`
Expected: `PLAY RECAP` with `failed=0` on svc-infra; tasks
`Copy the household guide to its served directory` and the new Quadlet
render report `changed`. The nightly runner's venv also rebuilds once,
because its stamp is keyed on `requirements.txt` and Task 2 added `markdown`
— expected, and a one-off.

Run: `make media`
Expected: `failed=0`; the Caddyfile, dnsmasq zone and Homepage services
render as `changed`; the svc-media smoke test passes for `guide`.

- [ ] **Step 9: Verify by use**

```bash
curl -s https://guide.fortwow.dev/ | grep -o '<title>[^<]*</title>'
curl -s -o /dev/null -w '%{http_code}\n' https://guide.fortwow.dev/nope.html
ssh straderb@192.168.1.30 'grep -c guide.fortwow.dev /opt/homelab/appdata/homepage/services.yaml'
```

Expected: `<title>Home guide · Home guide</title>`, `404`, and `1`. (Homepage
renders tiles client-side, so its HTML is the wrong thing to grep.) 🧑 The
**Household** group with a *guide* tile shows on `https://home.fortwow.dev`.

Then run `make infra` again. Expected: the guide tasks report `ok`, not
`changed` (the sync trio may appear — that is the documented svc-infra
behaviour after a commit, not the guide).

- [ ] **Step 10: Commit**

```bash
git add inventory/group_vars/all/main.yml inventory/group_vars/all/infra-apps.yml roles/svc_infra/tasks/guide.yml roles/svc_infra/tasks/main.yml
git commit -m "feat: serve the household guide at guide.<domain>, open like home"
git push
```

---

### Task 4: Audiobookshelf can see the Calibre library

**Files:**
- Modify: `roles/svc_media/templates/audiobookshelf.container.j2:20`

**Interfaces:**
- Produces: `/books` inside the `audiobookshelf` container, read-only,
  = `/srv/media/books` (the Calibre-Web Automated library).

- [ ] **Step 1: Confirm the gap**

```bash
ssh straderb@192.168.1.30 'sudo -u homelab XDG_RUNTIME_DIR=/run/user/10001 podman exec audiobookshelf ls /books'
```

Expected: `ls: /books: No such file or directory`.

- [ ] **Step 2: Add the mount**

In `audiobookshelf.container.j2`, after the
`Volume=/srv/media/ebooks:/ebooks:ro` line, add:

```ini
# The Calibre-Web Automated library, so Audiobookshelf can serve its ebooks
# (the household guide's default reader). Read-only: Calibre owns this tree
# and its metadata.db, and Audiobookshelf keeps its own metadata in appdata.
Volume=/srv/media/books:/books:ro
```

- [ ] **Step 3: Validate and deploy**

Run: `make validate` — expected exit 0 (systemd unit parse included).
Run: `make media` — expected `failed=0`, the Quadlet reports `changed` and
audiobookshelf restarts.

- [ ] **Step 4: Verify by use**

```bash
ssh straderb@192.168.1.30 'sudo -u homelab XDG_RUNTIME_DIR=/run/user/10001 podman exec audiobookshelf ls /books'
ssh straderb@192.168.1.30 'sudo -u homelab XDG_RUNTIME_DIR=/run/user/10001 podman exec audiobookshelf touch /books/probe' ; echo "exit=$?"
```

Expected: the author folders (`Matt Dinniman`, `Stephen King`, …) and
`metadata.db`; then `Read-only file system` with a non-zero exit. If the
`touch` succeeds instead, the mount is writable: remove the file with
`ssh straderb@192.168.1.30 'rm /srv/media/books/probe'` and stop — do not
point Audiobookshelf at a writable Calibre library.

- [ ] **Step 5: Commit**

```bash
git add roles/svc_media/templates/audiobookshelf.container.j2
git commit -m "feat: mount the Calibre library read-only into Audiobookshelf"
git push
```

---

### Task 5: 🧑 Movies and TV — admin setup

Operator does the UI steps; the implementer verifies. Before starting, ask
the operator for **her preferred username**; it is written `<her-username>`
below and used identically in every app. Brandon's is `brandon`. Passwords
are chosen in the UI, handed over in person, and never written down in the
repo or the chat.

**Files:** none.

- [ ] **Step 1: 🧑 Fix whatever Task 1 listed as `needs:` for the video pipeline**

Per `docs/first-login-walkthrough.md` Phase 4: Prowlarr indexers →
*Settings > Apps* → Sonarr (`http://localhost:8989`) and Radarr
(`http://localhost:7878`); in Sonarr and Radarr, root folders under `/data`
and SABnzbd at `localhost:8080`.

- [ ] **Step 2: 🧑 Jellyfin wizard — https://jellyfin.fortwow.dev**

1. Language English; create the admin user `brandon`.
2. Add library: **Movies**, folder `/media/movies`.
3. Add library: **Shows**, folder `/media/tv`.
4. Metadata language English, country United States. Allow remote
   connections: on. Finish.
5. *Dashboard > Users > +*: create `<her-username>`, access to all
   libraries, not an administrator.

- [ ] **Step 3: Verify Jellyfin**

```bash
curl -s https://jellyfin.fortwow.dev/System/Info/Public | python3 -c 'import json,sys; print(json.load(sys.stdin)["StartupWizardCompleted"])'
curl -s https://jellyfin.fortwow.dev/Users/Public | python3 -c 'import json,sys; print([u["Name"] for u in json.load(sys.stdin)])'
```

Expected: `True`; the user list includes both names (if the list is empty,
the operator hid users from the login screen — ask them to confirm in
*Dashboard > Users* instead).

- [ ] **Step 4: 🧑 Seerr — https://seerr.fortwow.dev**

1. *Sign in with Jellyfin*: server `http://192.168.1.30:8096`, the `brandon`
   Jellyfin account; email any address.
2. Sync libraries; enable Movies and Shows.
3. Radarr: host `192.168.1.31`, port `7878`, API key from Radarr
   *Settings > General*, root folder and quality profile from the dropdowns,
   *Default server* on. Sonarr: same with port `8989`.
4. *Settings > Users*: default permissions **Request** + **Auto-Approve**
   (movies and series).
5. *Users > Import Jellyfin Users*: import `<her-username>`; confirm she has
   Request + Auto-Approve.

- [ ] **Step 5: Verify Seerr**

```bash
curl -s https://seerr.fortwow.dev/api/v1/settings/public | python3 -c 'import json,sys; print(json.load(sys.stdin)["initialized"])'
```

Expected: `True`.

- [ ] **Step 6: End-to-end proof, signed in as her**

🧑 In Seerr as `<her-username>`, request one movie not already present.
Implementer then watches it arrive:

```bash
ssh straderb@192.168.1.30 'ls -t /srv/media/movies | head -3'
```

Expected: the requested title appears (minutes to an hour). 🧑 It then
appears in Jellyfin under Movies. If it stalls, check SABnzbd's queue and the
Radarr *Activity* page before anything else.

No commit.

---

### Task 6: 🧑 Books and audiobooks — admin setup

**Files:** none.

- [ ] **Step 1: 🧑 Audiobookshelf — https://abs.fortwow.dev**

1. Create root user `brandon`.
2. *Settings > Libraries > Add*: **Audiobooks**, media type *Books*, folder
   `/audiobooks`.
3. *Add*: **Ebooks**, media type *Books*, folder `/books`. Leave
   *Store metadata with item* off (the mount is read-only).
4. *Settings > Users > Add*: `<her-username>`, type *User*, access to both
   libraries, *Can download* on.
5. *Settings > Backups*: enable, daily, keep 7.

- [ ] **Step 2: Verify Audiobookshelf**

```bash
curl -s https://abs.fortwow.dev/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["isInit"])'
```

Expected: `True`. 🧑 The Ebooks library shows the existing Calibre books.

- [ ] **Step 3: 🧑 Calibre-Web — https://calibre-web.fortwow.dev**

*Admin > Add new user*: `brandon` and `<her-username>`, each with
*Allow Downloads* on (Readest needs download to open a book).

- [ ] **Step 4: 🧑 Shelfmark — https://shelfmark.fortwow.dev**

Only what Task 1 listed as `needs:`, per `docs/first-login-walkthrough.md`
Phase 4 step 4: Prowlarr `http://127.0.0.1:9696`, SABnzbd
`http://127.0.0.1:8080`, categories `books` and `audiobooks`.

- [ ] **Step 5: End-to-end proof — one ebook, one audiobook**

🧑 In Shelfmark, request one ebook and one audiobook. Implementer follows
them:

```bash
ssh straderb@192.168.1.30 'ls -la /srv/media/book-ingest; find /srv/media/books -newer /srv/media/books/metadata.db -maxdepth 2 | head; ls -t /srv/media/audiobooks | head -3'
```

Expected: the ebook passes through `book-ingest` (empty again once CWA
imports it) and appears under `/srv/media/books/<Author>/`; the audiobook
appears under `/srv/media/audiobooks/`. 🧑 In Audiobookshelf *Scan* both
libraries; both items appear.

- [ ] **Step 6: Record**

Note in the task report the real time each item took to arrive — the
"what happens next" text on the Shelfmark and Seerr pages quotes it.

No commit.

---

### Task 7: 🧑 Away from home, and the Readest spike

**Files:** none.

- [ ] **Step 1: 🧑 Tailscale on her devices**

Invite her to the tailnet as her own user (Tailscale admin console →
*Users → Invite users*) rather than signing her devices into Brandon's
account, so her devices can be removed on their own. On each of her phones:
install *Tailscale*, sign in with the invited account, turn it on.

Tell the operator plainly: her devices will reach the LAN like every other
tailnet peer, including the unauthenticated ComfyUI port recorded as an open
finding in `docs/gpu-host.md`. That finding's recommended fix (a tailnet ACL)
is still the fix; this step does not widen it beyond "one more trusted
household device".

- [ ] **Step 2: 🧑 Verify over cellular**

On her phone with Wi-Fi **off** and Tailscale on: open
`https://guide.fortwow.dev`, then `https://jellyfin.fortwow.dev`. Both load.
With Tailscale off: neither loads (expected — say so in the guide).

- [ ] **Step 3: 🧑 Readest spike**

On one iPhone and one Android device, install *Readest*.

1. Add an OPDS catalog: `https://calibre-web.fortwow.dev/opds`, username and
   password of the reader's Calibre-Web account. Open a book.
2. Look in Readest's settings for KOReader sync (KOSync). If present, set the
   server to `https://calibre-web.fortwow.dev/kosync` with the same account.
3. Read a few pages on device A, close the book, open it on device B.

Record exactly one of:
- **Syncs through our server** — position moved with the KOSync setting
  pointed at Calibre-Web.
- **Syncs only through Readest's cloud** — position moved only after signing
  in to a Readest account (note: that sends reading progress to Readest).
- **Does not sync** — position did not move.

The "Read a book" page states the recorded result verbatim in plain words.

No commit.

---

### Task 8: Write the guide pages

Drafts from what Tasks 5–7 established. Every page keeps
`_Last checked: not yet._` until Task 9 walks it on real devices. Where a
draft names a button, it is the label the app showed during Tasks 5–7;
correct it in Task 9 if the device shows something else.

**Files:**
- Modify: `docs/household/index.md`
- Create: `docs/household/getting-connected.md`, `watch.md`, `request.md`,
  `find-books.md`, `read.md`, `listen.md`, `help.md`

- [ ] **Step 1: `index.md`**

```markdown
# Home guide

The home server does five things for us. Tap one.

| I want to… | Go to |
|---|---|
| Watch a movie or TV show | [Watch movies and TV](watch.md) |
| Get a movie or show we don't have | [Ask for a movie or show](request.md) |
| Get an ebook or audiobook | [Find a book or audiobook](find-books.md) |
| Read a book | [Read a book](read.md) |
| Listen to an audiobook | [Listen to an audiobook](listen.md) |

**First time?** Start with [Getting connected](getting-connected.md) — it
covers the apps to install and using all this away from home.

**Something wrong?** See [Something's not working](help.md).

Save this page to your home screen: it lives at `guide.fortwow.dev`.

_Last checked: not yet._
```

- [ ] **Step 2: `getting-connected.md`**

```markdown
# Getting connected

## At home

On the home Wi-Fi everything just works. Nothing to set up.

## Away from home

Your phone needs **Tailscale** turned on. It's a free app that makes your
phone act as if it were at home.

### iPhone

1. Install **Tailscale** from the App Store.
2. Open it and sign in with the account Brandon invited you with.
3. Tap the switch so it says **Connected**.

### Android

1. Install **Tailscale** from the Play Store.
2. Open it and sign in with the account Brandon invited you with.
3. Tap **Connect**.

Leave it on when you're out. If a page won't load away from home, check
Tailscale first.

## Your apps

| For | iPhone | Android | Google TV |
|---|---|---|---|
| Movies and TV | Swiftfin | Jellyfin | Jellyfin |
| Books and audiobooks | Still | Audiobookshelf | — |

Each app asks for a **server address** the first time. Use the one listed on
that app's page in this guide.

## Your accounts

You sign in with the same username everywhere: **<her-username>**. Brandon
gave you the password in person. If you've lost it, ask him — he can reset
it.

_Last checked: not yet._
```

(Substitute her real username for `<her-username>` before committing — it is
a username, not a secret. Left in, the gate fails with `unfilled placeholder`.)

- [ ] **Step 3: `watch.md`**

```markdown
# Watch movies and TV

Everything we have is in **Jellyfin**. Server address:
`https://jellyfin.fortwow.dev`

## On the TV (Google TV Streamer)

1. On the home screen, open **Jellyfin**. (First time: install it from
   **Apps → Search → Jellyfin**.)
2. First time only: enter the server address above, then your username and
   password.
3. Pick **Movies** or **Shows**, choose something, press **Play**.

Stopping partway is fine — it remembers where you were, on every device.

## On iPhone or iPad

1. Install **Swiftfin** from the App Store.
2. Tap **Connect to Server**, enter the server address, then sign in.
3. Pick something and tap **Play**.

## On Android

1. Install **Jellyfin** from the Play Store.
2. Enter the server address, then sign in.
3. Pick something and tap **Play**.

## Subtitles

While it's playing, open the player menu and choose **Subtitles**.

## Watching without internet (plane, car)

On your phone, open the movie or episode and tap **Download** while you're
still at home. Downloads play with no connection.

## Don't see what you want?

[Ask for it](request.md).

_Last checked: not yet._
```

- [ ] **Step 4: `request.md`**

Use the arrival time recorded in Task 5 Step 6 for the "how long" line.

```markdown
# Ask for a movie or show

Use **Seerr**: `https://seerr.fortwow.dev` (works in any browser — save it to
your home screen).

1. Sign in with **Use your Jellyfin account** — same username and password as
   Jellyfin.
2. Search for the movie or show.
3. Tap it, then **Request**. For a show, pick which seasons.

That's it. You don't need to do anything else.

## What happens next

| Status in Seerr | Meaning |
|---|---|
| Requested / Processing | It's being fetched. Usually ready within about an hour. |
| Partially available | Some episodes are ready; the rest are on the way. |
| Available | Ready — open Jellyfin and it's there. |

If something has said *Processing* for more than a day, tell Brandon.

_Last checked: not yet._
```

- [ ] **Step 5: `find-books.md`**

```markdown
# Find a book or audiobook

Use **Shelfmark**: `https://shelfmark.fortwow.dev` (any browser — save it to
your home screen). There's no sign-in.

1. Search by title or author.
2. Choose whether you want the **ebook** or the **audiobook**.
3. Tap the one you want to get it.

## What happens next

- **Ebooks** appear in your reading app within a few minutes.
- **Audiobooks** are bigger and can take longer. They appear in
  Audiobookshelf when they're done.

Not there after an hour? See [Something's not working](help.md).

_Last checked: not yet._
```

- [ ] **Step 6: `read.md`**

State Task 7 Step 3's recorded Readest result in the marked section.

```markdown
# Read a book

Your books are in **Audiobookshelf** — the same app as audiobooks. Server
address: `https://abs.fortwow.dev`

## iPhone or iPad

1. Install **Still: for Audiobookshelf** from the App Store.
2. Enter the server address, then your username and password.
3. Open the **Ebooks** library, tap a book, tap **Read**.

## Android

1. Install **Audiobookshelf** from the Play Store.
2. Enter the server address, then your username and password.
3. Switch to the **Ebooks** library, tap a book, tap **Read**.

Your place is saved: start on your phone, carry on on your tablet.

## If you read a lot: Readest

Readest is a nicer reading app, with better fonts and page turning. It uses
your separate book-library account (ask Brandon if you don't have one).

1. Install **Readest** (App Store or Play Store).
2. Add a catalog (**OPDS**) with the address
   `https://calibre-web.fortwow.dev/opds` and your book-library username and
   password.
3. Browse, tap a book to open it.

**Keeping your place between devices:** <one plain sentence stating the Task
7 Step 3 result, e.g. "Readest keeps your place across your phone and tablet
automatically." or "Readest does not keep your place between devices — use
one device per book.">

Readest and Audiobookshelf don't share your place with each other. Pick one
for each book.

_Last checked: not yet._
```

(Replace the `<one plain sentence…>` line with the recorded result before
committing. If it is left in, the gate fails with `unfilled placeholder`.)

- [ ] **Step 7: `listen.md`**

```markdown
# Listen to an audiobook

Audiobooks are in **Audiobookshelf**. Server address:
`https://abs.fortwow.dev`. Use the same app as for reading: **Still** on
iPhone, **Audiobookshelf** on Android.

1. Open the **Audiobooks** library.
2. Tap a book, then **Play**.

## Handy controls

- **Speed:** tap the speed button (1.0×) to go faster or slower.
- **Sleep timer:** tap the moon or timer icon; it stops playing for you.
- Your place is saved and follows you between devices.

## Listening without internet

Tap the **download** button on the book while you're at home. It plays with
no connection — good for the car or a flight.

_Last checked: not yet._
```

- [ ] **Step 8: `help.md`**

```markdown
# Something's not working

Try the first check for your problem. If that doesn't fix it, tell Brandon
what you were doing and what you saw.

| Problem | First check |
|---|---|
| Nothing loads | Away from home? Turn on **Tailscale** ([how](getting-connected.md)). At home? Check you're on the home Wi-Fi, not mobile data. |
| A request is stuck on *Processing* | Give it a day. After that, tell Brandon the title. |
| A book never showed up | Ebooks: pull down to refresh the library. Audiobooks can take a few hours. After a day, tell Brandon. |
| A video keeps pausing to load | Away from home on mobile data, try a lower quality in the player's settings. At home, tell Brandon. |
| Forgot a password | Ask Brandon — he can reset it. |

_Last checked: not yet._
```

- [ ] **Step 9: Gate, deploy, look at it on a phone**

Run: `.venv/bin/python tests/validate_household_guide.py`
Expected: `Household guide: OK (8 pages, 0 assets, 8 positive controls caught)`

Run: `make validate && make infra`
Expected: exit 0; `Copy the household guide to its served directory`
reports `changed`.

🧑 Open `https://guide.fortwow.dev` on a phone: every link on the front
page opens the right page, nothing scrolls sideways, dark mode is readable.

- [ ] **Step 10: Commit**

```bash
git add docs/household/index.md docs/household/getting-connected.md docs/household/watch.md docs/household/request.md docs/household/find-books.md docs/household/read.md docs/household/listen.md docs/household/help.md
git commit -m "docs: draft the Stage 1 household guide pages"
git push
```

---

### Task 9: 🧑 Walk every page on real devices

Done-criteria 1–5 from the spec. Signed in as `<her-username>` throughout,
never as an admin.

**Files:**
- Modify: every `docs/household/*.md` (corrections, `Last checked` lines)
- Create: `docs/household/img/*.png` (only where a step was ambiguous)

- [ ] **Step 1: 🧑 Movie** — the movie from Task 5 plays on the Google TV
  Streamer, iPhone (Swiftfin) and Android. Stop at 5 minutes on one device;
  it resumes near 5 minutes on another. If Swiftfin fails to play, switch
  the iPhone app to *Jellyfin Mobile* on `watch.md` and
  `getting-connected.md`, and retest.
- [ ] **Step 2: 🧑 TV** — request one season in Seerr; episodes appear and
  play the same way; Seerr shows *Available*.
- [ ] **Step 3: 🧑 Ebook** — the Task 6 ebook opens in Still (iPhone) and
  the Audiobookshelf app (Android); read a few pages on one, and the position
  moves on the other. If EPUBs render badly in either (missing text, broken
  layout, fonts ignored on more than one book), apply the spec's fallback:
  rewrite `read.md` with Readest as the default and Audiobookshelf as the
  alternative, and tell the operator.
- [ ] **Step 4: 🧑 Audiobook** — the Task 6 audiobook plays on both phones;
  download it, turn on airplane mode, it plays; progress follows between
  phones.
- [ ] **Step 5: 🧑 Away from home** — Wi-Fi off, Tailscale on: one video
  and one audiobook play.
- [ ] **Step 6: Fix the pages** — every label, step or order the devices
  disagreed with. Add a screenshot only where a step was genuinely ambiguous
  (candidates: Tailscale's switch, Seerr's *Request* button, ebook vs
  audiobook in Shelfmark), saved as `docs/household/img/<page>-<what>.png`,
  cropped, under 300 KB, and referenced with standard Markdown image syntax
  whose alt text says what it shows and whose target is `img/` plus the file
  name.
- [ ] **Step 7: Last-checked lines** — replace each `_Last checked: not yet._`
  with `_Last checked: YYYY-MM-DD on <devices actually used>._`, e.g.
  `_Last checked: 2026-10-03 on iPhone 15, Pixel 8, Google TV Streamer._`
- [ ] **Step 8: Gate, deploy, commit**

```bash
.venv/bin/python tests/validate_household_guide.py   # OK, N assets
make validate && make infra
git add docs/household/
git status --short docs/household/   # confirm only intended files
git commit -m "docs: correct the household guide against real devices"
git push
```

(`git add docs/household/` is a single explicit path containing only guide
pages and images — confirm with the `status` line that nothing else is in it.)

---

### Task 10: 🧑 Cold read

Done-criterion 6.

- [ ] **Step 1: 🧑** She follows **Ask for a movie or show** and **Listen to
  an audiobook** on her own phone, with the guide as her only help. Brandon
  watches and says nothing; he notes every hesitation, wrong tap or question.
- [ ] **Step 2:** Each noted moment is a defect: fix the page wording (or add
  a screenshot) so the moment would not recur.
- [ ] **Step 3:** Gate, deploy, commit:

```bash
.venv/bin/python tests/validate_household_guide.py
make infra
git add docs/household/
git commit -m "docs: fix what the household guide's cold read tripped on"
git push
```

Skip the commit if the read found nothing — and say so in the report.

---

### Task 11: Correct the admin docs

**Files:**
- Modify: `docs/first-login-walkthrough.md` (§3.1, §3.2, §3.3, the
  "Not behind SSO" paragraph in §0.2)
- Modify: `docs/services.md` (service map; One-time UI wiring steps 6–8)

- [ ] **Step 1: `first-login-walkthrough.md`**

- §3 intro: replace `Verified 2026-07-25: Jellyfin's startup wizard has **not**
  been completed yet.` with `Completed <Task 5 date> for the household guide.`
- §3.1: replace the three host-path bullets with container paths —
  `Movies → /media/movies`, `TV → /media/tv` — and delete the Music bullet
  (no music library is mounted). Add: `Then create one non-admin user per
  household member; the guide at guide.fortwow.dev assumes it.`
- §3.2: add `Set default permissions to Request + Auto-Approve before
  importing Jellyfin users.`
- §3.3: replace `Library path /srv/media/audiobooks.` with `Libraries:
  Audiobooks → /audiobooks, Ebooks → /books (the Calibre library, mounted
  read-only). One user per household member, with download allowed.`
- §0.2 "Not behind SSO" sentence: add `the household guide (guide),` to the
  open list.

- [ ] **Step 2: `services.md`**

- Service map: add a row after `seerr`:
  `| guide | svc-infra | Household guide — how to watch, read and listen (docs/household/) |`
- One-time UI wiring step 6: `add the movie (/media/movies) and TV
  (/media/tv) libraries, then one non-admin user per household member.`
- Step 8: replace `add /audiobooks, /books, and /ebooks` with
  `add Audiobooks (/audiobooks) and Ebooks (/books — the Calibre library,
  read-only)`.
- In "Single sign-on", the sentence listing services left open for native
  clients stays; add a sentence: `The household guide (guide) is also open:
  the family accounts cannot pass the admins-only rule, and
  tests/validate_household_guide.py keeps secrets off it.`

- [ ] **Step 3: Validate and commit**

Run: `make validate` — expected exit 0 (`validate-links` covers the edits).

```bash
git add docs/first-login-walkthrough.md docs/services.md
git commit -m "docs: correct media library paths and record the household setup"
git push
```

---

### Task 12: Final deploy proof, merge, clean up

- [ ] **Step 1: Mark the spec**

In `docs/superpowers/specs/2026-09-28-household-guide-design.md`, change the
status line to `**Status: Stage 1 implemented <date>.** Stages 2–5 not
started.` Commit:

```bash
git add docs/superpowers/specs/2026-09-28-household-guide-design.md
git commit -m "docs: mark household guide Stage 1 as implemented"
```

- [ ] **Step 2: Clean tree**

Run: `git status --porcelain`
Expected: no output. Anything listed must be committed or explained before
continuing.

- [ ] **Step 3: Deploy proof — svc-infra**

Run: `make deploy-proof TARGET=infra`
Expected: sync-only (the three `.deployed-rev` tasks) on the first run. Run
it again: fully clean, exit 0.

- [ ] **Step 4: Deploy proof — svc-media**

Run: `make deploy-proof TARGET=media`
Expected: exit 0, no changed tasks.

- [ ] **Step 5: Verify**

Run: `make verify`
Expected: green on all three service VMs, including the `guide` smoke test.

- [ ] **Step 6: Merge, push, delete the branch**

```bash
git switch main && git pull --ff-only
git merge --ff-only feat/household-guide
git push origin main
git branch -d feat/household-guide
git push origin --delete feat/household-guide
```

If `--ff-only` refuses because `main` moved, rebase the branch onto `main`,
re-run Steps 2–5, then merge.

- [ ] **Step 7: Check CI**

Check the `push` run of `.github/workflows/validate.yml` on `main` with the
`gh` recipe in `docs/deployment.md`. Red means a follow-up commit, not a
revert-and-forget.
