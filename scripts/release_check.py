#!/usr/bin/env python3
"""Report which upstream projects have shipped a release since the last report.

Entry points are the two wrapper scripts, not this file directly:

    scripts/release-check.sh              # the whole report
    scripts/release-check.sh --coverage   # label coverage, makes NO feed queries
    scripts/image-release.sh <ref>        # one image
    make release-check

Read-only. It reads registry manifest metadata and a release feed, and writes
nothing outside the state file it is explicitly given with --state-out.

WHY THIS IS NOT image-check.sh. That script resolves a recorded `# tag:` to the
digest the tag points at now, and reports a difference. It therefore sees only
the images that carry a tag comment — 13 of 48 — and the 35 it cannot see are
the ones the BUMP PROCEDURE block in apps.yml deliberately leaves untracked
because recording `latest` on them would invite a one-way data migration. Those
are exactly the images whose release notes somebody needs to READ.

So the current version is discovered a different way: from the pinned digest's
own OCI labels.

    org.opencontainers.image.version   4.0.19.2979-ls320
    org.opencontainers.image.source    https://github.com/linuxserver/docker-sonarr

A digest carries no memory of its tag, but it does carry a memory of its
version, and that is the better fact — a tag says where an image came from, a
version says what it is. `.source` names the upstream repository, so the release
feed is discoverable with no configuration at all.

Full reasoning, measured coverage and the deliberate non-goals are in
docs/plans/release-report.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CATALOGS = (
    "inventory/group_vars/all/apps.yml",
    "inventory/group_vars/all/infra-apps.yml",
    "inventory/group_vars/all/main.yml",
    "inventory/group_vars/all/minecraft.yml",
)

# Same contract as scripts/image-check.sh. Comment lines are skipped, which is
# not a nicety: the BUMP PROCEDURE block at the top of apps.yml contains the
# literal example `ghcr.io/owner/image@sha256:<new>`, and a parser that reads
# comments would try to resolve it every week and report it as an error.
PIN_RE = re.compile(r"([A-Za-z0-9._/-]+)@(sha256:[0-9a-f]{64})")

GITHUB_RE = re.compile(r"https?://github\.com/([^/\s]+/[^/\s#?]+)")

# Repository names whose last path segment identifies nothing. `vaultwarden/server`
# rendered as "server" in the report, which is not a service anyone here runs.
GENERIC_LEAF = frozenset({"server", "image", "images", "app", "core", "docker"})

ACCEPT = ",".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
))

# Version labels that are not versions, all of them observed in the live
# coverage run on 2026-08-05: open-webui labels `main` (a branch),
# itzg/minecraft-server labels `java25` (a variant), and three LinuxServer
# images label the upstream COMMIT they built from — calibre-web-automated
# `cd80d60b-ls59`, lazylibrarian `a7c70e36-ls311`, webtop `15bc101c-ls308`.
#
# Comparing any of these against a release tag reports the image as behind
# forever. That is not a harmless inaccuracy: it is a confident wrong answer
# that never self-corrects, sitting in a report next to true ones.
NON_VERSIONS = frozenset({
    "main", "master", "latest", "develop", "development", "edge", "nightly",
    "stable", "unstable", "rolling", "head", "", "-",
})

# A version has a digit and a dot. Crude on purpose: it cleanly separates every
# case in this estate — `4.0.19.2979-ls320`, `2026.7.28-8372f5d85` and
# `14-vectorchord0.4.3-pgvector0.8.0` pass; `java25`, `main` and the three
# commit hashes above do not — and where it is wrong it is wrong in the safe
# direction. A project versioning as a bare `v5` reads as unknown-version,
# which means UNMEASURED and says so, rather than as a permanent false `behind`.
VERSION_SHAPE_RE = re.compile(r"^(?=.*\d)(?=.*\.).+$")

CURRENT = "current"
BEHIND = "behind"
UNKNOWN_VERSION = "unknown-version"
NO_FEED = "no-feed"
ERROR = "error"


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------

def read_pins(root: Path) -> list[tuple[str, str]]:
    """Every distinct (repo, digest) pinned across the catalogs, in file order."""
    pins: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for relative in CATALOGS:
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = PIN_RE.search(line)
            if not match:
                continue
            pin = match.groups()
            if pin not in seen:
                seen.add(pin)
                pins.append(pin)
    return pins


def read_overrides(root: Path) -> dict[str, str]:
    """The hand-maintained image -> upstream repository map from main.yml.

    Parsed with a regex rather than by loading the YAML, because these files are
    Ansible group_vars full of Jinja that PyYAML cannot evaluate, and because
    this script must run from the nightly runner's `git archive` checkout where
    no Ansible context exists. tests/validate_release_overrides.py parses the
    same block the same way and checks every key still matches a real pin.
    """
    text = (root / "inventory/group_vars/all/main.yml").read_text(encoding="utf-8")
    block = re.search(r"^release_feed_overrides:\s*$(.*?)(?=^\S|\Z)",
                      text, re.MULTILINE | re.DOTALL)
    if not block:
        return {}
    overrides: dict[str, str] = {}
    for line in block.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entry = re.match(r'^"?([A-Za-z0-9._/-]+)"?:\s*"?([^"#]*?)"?\s*(?:#.*)?$', stripped)
        if entry:
            overrides[entry.group(1)] = entry.group(2).strip()
    return overrides


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def split_ref(ref: str) -> tuple[str, str]:
    """Split `host/repo` the way scripts/image-digest.sh does, deliberately.

    A leading segment counts as a registry only if it looks like a hostname, so
    bare names are Docker Hub with the implicit library/ namespace. And docker.io
    does not serve the registry API — registry-1.docker.io does — which is why
    the short form every catalog entry writes resolves and the explicit
    `docker.io/...` form would not without this rewrite.
    """
    host, _, rest = ref.partition("/")
    if "/" not in ref or ("." not in host and ":" not in host and host != "localhost"):
        host, rest = "registry-1.docker.io", ref
        if "/" not in rest:
            rest = "library/" + rest
    if host in ("docker.io", "index.docker.io"):
        host = "registry-1.docker.io"
    return host, rest


def fetch(url: str, headers: dict[str, str], timeout: int) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def registry_token(host: str, repo: str, timeout: int) -> str | None:
    """Resolve auth generically: read the 401's challenge, ask the realm it names.

    lscr.io advertises ghcr.io's realm, so one code path covers Docker Hub, GHCR,
    LinuxServer and anything added later without a per-registry table.
    """
    try:
        request = urllib.request.Request(
            f"https://{host}/v2/{repo}/manifests/latest", method="HEAD")
        urllib.request.urlopen(request, timeout=timeout)
        return None
    except urllib.error.HTTPError as error:
        challenge = error.headers.get("WWW-Authenticate", "")
    except Exception:
        return None
    realm = re.search(r'realm="([^"]+)"', challenge)
    if not realm:
        return None
    query = {"scope": f"repository:{repo}:pull"}
    service = re.search(r'service="([^"]+)"', challenge)
    if service:
        query["service"] = service.group(1)
    url = f"{realm.group(1)}?{urllib.parse.urlencode(query)}"
    try:
        payload = json.loads(fetch(url, {}, timeout))
    except Exception:
        return None
    return payload.get("token") or payload.get("access_token")


def image_labels(ref: str, digest: str, timeout: int) -> dict[str, str]:
    """OCI labels from the config blob of a PINNED digest. No pull, no tag."""
    host, repo = split_ref(ref)
    token = registry_token(host, repo, timeout)
    headers = {"Accept": ACCEPT}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    manifest = json.loads(fetch(
        f"https://{host}/v2/{repo}/manifests/{digest}", headers, timeout))

    # A multi-arch pin is an index; the labels live on a child manifest. Every
    # host in this estate is linux/amd64.
    if "manifests" in manifest:
        child = next(
            (entry["digest"] for entry in manifest["manifests"]
             if entry.get("platform", {}).get("os") == "linux"
             and entry.get("platform", {}).get("architecture") == "amd64"),
            None)
        if child is None:
            raise LookupError("no linux/amd64 manifest in the index")
        manifest = json.loads(fetch(
            f"https://{host}/v2/{repo}/manifests/{child}", headers, timeout))

    config_digest = (manifest.get("config") or {}).get("digest")
    if not config_digest:
        raise LookupError("manifest has no config descriptor")

    # The blob needs the SAME bearer token as the manifest — ghcr.io answers an
    # unauthenticated blob request with 401, not a redirect, and the whole
    # label read fails there rather than anywhere visible. Docker Hub does
    # redirect to a presigned CDN URL; urllib follows it and drops the
    # Authorization header on the cross-origin hop, which is what that target
    # wants. Both paths are exercised by tests/fixtures — and by the fact that
    # this estate pins images on ghcr.io, lscr.io and docker.io alike.
    blob_headers = {"Authorization": headers["Authorization"]} if token else {}
    blob = json.loads(fetch(
        f"https://{host}/v2/{repo}/blobs/{config_digest}", blob_headers, timeout))
    return (blob.get("config") or {}).get("Labels") or {}


# --------------------------------------------------------------------------
# upstream feed
# --------------------------------------------------------------------------

class RateLimited(Exception):
    """The GitHub API refused for quota reasons.

    Its own exception type because it must never be absorbed into "no release
    found". Unauthenticated GitHub allows 60 requests/hour per IP and this
    report needs about 35, so exhaustion is the likeliest way the whole thing
    breaks — and a rate limit that reads as an all-clear is precisely the
    failure this repo has written down four times.
    """


# Whatever the last GitHub response reported about remaining quota. Read from a
# header that every response carries, so knowing where the budget stands costs
# nothing — asking /rate_limit would itself be a request.
QUOTA = {"remaining": None, "limit": None}


def github_latest(repo: str, token: str | None,
                  timeout: int) -> dict[str, str] | None:
    """The newest published release, or None when the project publishes none.

    ON THE RATE LIMIT, WHICH IS THE TIGHT CONSTRAINT HERE. Unauthenticated
    GitHub allows 60 requests/hour per IP. Measured on 2026-08-05, a full run
    costs 45: 43 distinct upstream repositories, plus a second call owed by the
    two that 404 on /releases/latest. That fits a weekly run and nothing else —
    running the report twice within an hour exhausts the budget, and the second
    run reports `error`, correctly and loudly, rather than an all-clear.

    An earlier version of this function sent an If-None-Match and claimed 304s
    were free. THEY ARE NOT: a run in which 40 of 44 feeds answered 304 still
    consumed 45 requests of quota, measured directly against /rate_limit before
    and after. The ETag machinery was removed rather than left in place with a
    comment explaining that it does not do what it says.

    Set GITHUB_TOKEN to raise the limit to 5000/hour and make the whole question
    go away. See docs/plans/release-report.md.
    """
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "homelab-iac-release-check"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def note_quota(response_headers) -> None:
        remaining = response_headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            QUOTA["remaining"] = int(remaining)
            QUOTA["limit"] = int(response_headers.get("X-RateLimit-Limit") or 0)

    def call(path: str):
        url = f"https://api.github.com/repos/{repo}{path}"
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                note_quota(response.headers)
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            note_quota(error.headers)
            if error.code in (403, 429):
                raise RateLimited(f"HTTP {error.code} from api.github.com "
                                  f"(quota {QUOTA['remaining']}/{QUOTA['limit']})") from error
            if error.code == 404:
                return None
            raise

    payload = call("/releases/latest")
    if payload is None:
        # 404 means either no repository or no non-prerelease release. The
        # second is common enough — projects that ship only prereleases, or tag
        # without releasing — to be worth one more call before giving up.
        listing = call("/releases?per_page=1")
        if not listing:
            return None
        payload = listing[0]
    if not isinstance(payload, dict) or "tag_name" not in payload:
        return None
    return {
        "tag": payload["tag_name"],
        "name": payload.get("name") or payload["tag_name"],
        "published": payload.get("published_at") or "",
        "url": payload.get("html_url") or f"https://github.com/{repo}/releases",
    }


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def normalise(version: str) -> str:
    """Enough normalisation to make equality meaningful, and no more."""
    return version.strip().lstrip("vV").strip()


def comparable(version: str) -> bool:
    cleaned = normalise(version)
    return (bool(cleaned)
            and cleaned.lower() not in NON_VERSIONS
            and bool(VERSION_SHAPE_RE.match(cleaned)))


def compare(local: str, upstream: str) -> str:
    """Equality after normalisation. Deliberately NOT an ordering.

    Ordering these would be confident nonsense. The versions in this estate
    include `cd80d60b-ls59`, `2026.7.28-8372f5d85`, `14-vectorchord0.4.3-pgvector`
    and `4.0.19.2979-ls320`; no single scheme orders that set, and a comparator
    that guesses produces a wrong answer that looks authoritative.

    Equality is decidable, so that is what is claimed. `behind` therefore means
    exactly "the pinned version is not the newest upstream release" — which for
    a pin deliberately held to an older line (postgres 18-alpine, Immich v3.0.3)
    is permanently true and correctly so. That is why the report headlines
    what is NEW since last week rather than the standing behind-count: the
    deliberate pins say their piece once and then sit in a one-line list.
    """
    return CURRENT if normalise(local) == normalise(upstream) else BEHIND


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def feed_for(ref: str, labels: dict[str, str], overrides: dict[str, str]) -> tuple[str, str]:
    """(repository, where it came from). An override always wins over the label.

    It has to. calibre-web-automated labels its source as
    linuxserver/docker-baseimage-ubuntu — the BASE IMAGE — so followed naively
    the report would announce Ubuntu base-image releases under Calibre-Web's
    name, indefinitely, while looking perfectly healthy. A label is a default,
    not a source of truth.
    """
    if ref in overrides:
        value = overrides[ref]
        # An empty override is a deliberate "there is no feed for this", not an
        # oversight — recorded so nobody re-derives the same dead end yearly.
        return (value, "override") if value else ("", "override-none")
    match = GITHUB_RE.search(labels.get("org.opencontainers.image.source", "") or "")
    if match:
        return match.group(1).removesuffix(".git"), "label"
    return "", "none"


def display_name(ref: str) -> str:
    """A name a reader recognises, from the image reference alone.

    Deliberately not derived from the surrounding YAML key. Guessing which
    catalog entry a line belongs to means tracking indentation across four
    files of Ansible group_vars, and getting it wrong labels a release with
    the wrong service — the same class of mistake as a wrong feed, for a
    cosmetic gain.
    """
    parts = [p for p in ref.split("/") if p]
    leaf = parts[-1]
    if leaf in GENERIC_LEAF and len(parts) >= 2:
        return parts[-2]
    return leaf


def examine(pins, overrides, *, timeout, github_token, coverage_only, probed=None):
    images = []
    feed_cache: dict[str, object] = {}

    for ref, digest in pins:
        record = {
            "image": ref,
            "name": display_name(ref),
            "digest": digest,
            "version": "",
            "feed": "",
            "feed_source": "",
            "latest": "",
            "latest_name": "",
            "published": "",
            "url": "",
            "version_source": "",
            "verdict": ERROR,
            "detail": "",
        }
        try:
            labels = image_labels(ref, digest, timeout)
        except Exception as error:
            record["detail"] = f"registry: {type(error).__name__}: {error}"
            images.append(record)
            continue

        record["version"] = labels.get("org.opencontainers.image.version", "") or ""
        record["version_source"] = "label" if record["version"] else ""

        # A version the RUNNING SERVICE reported about itself beats a label.
        # The label describes what was built; this describes what is answering.
        # Grafana, Prometheus and node-exporter carry no version label at all,
        # so for those it is the difference between measured and unmeasured.
        probed_version = (probed or {}).get(ref)
        if probed_version and comparable(probed_version):
            record["version"] = probed_version
            record["version_source"] = "probe"

        feed, source = feed_for(ref, labels, overrides)
        record["feed"], record["feed_source"] = feed, source

        if not feed:
            record["verdict"] = NO_FEED
            record["detail"] = ("no upstream feed recorded and the image carries "
                                "no usable source label")
            images.append(record)
            continue

        if coverage_only:
            record["verdict"] = (UNKNOWN_VERSION if not comparable(record["version"])
                                 else "comparable")
            images.append(record)
            continue

        if feed not in feed_cache:
            # Queried per REPOSITORY, not per image. beszel and beszel-agent are
            # one project; immich-server and immich-machine-learning are one
            # project. Deduplicating here is worth several requests of a budget
            # that has no headroom to spare.
            try:
                feed_cache[feed] = github_latest(feed, github_token, timeout)
            except RateLimited as error:
                feed_cache[feed] = error
            except Exception as error:
                feed_cache[feed] = RuntimeError(f"{type(error).__name__}: {error}")

        latest = feed_cache[feed]
        if isinstance(latest, Exception):
            record["detail"] = f"feed: {latest}"
            images.append(record)
            continue
        if latest is None:
            record["verdict"] = NO_FEED
            record["detail"] = f"{feed} publishes no releases"
            images.append(record)
            continue

        record.update(latest=latest["tag"], latest_name=latest["name"],
                      published=latest["published"], url=latest["url"])
        if not comparable(record["version"]):
            record["verdict"] = UNKNOWN_VERSION
            # Two different facts, and they were reading as one. An image with
            # no version label at all is not "labelling a branch name" — that
            # sentence described the wrong half of this set, which is the kind
            # of detail that makes a reader stop trusting the other lines.
            record["detail"] = (
                f"image carries no version label; upstream latest is {latest['tag']}"
                if not record["version"] else
                f"image labels its version as {record['version']!r}, which is a "
                f"branch or variant name, not a version; upstream latest is "
                f"{latest['tag']}")
        else:
            record["verdict"] = compare(record["version"], latest["tag"])
        images.append(record)

    return images


def summarise(images, *, coverage_only=False):
    counts = {}
    for record in images:
        counts[record["verdict"]] = counts.get(record["verdict"], 0) + 1
    resolved = sum(1 for r in images if comparable(r["version"]))
    answered = sum(1 for r in images if r["latest"])
    from_probe = sum(1 for r in images if r.get("version_source") == "probe")

    # The positive control, and the reason this cannot report a quiet all-clear.
    # Both of these are impossible if the run actually happened: 48 images do
    # not all lose their version labels in one week, and thirty-odd active
    # projects do not all stop publishing releases. Either number reaching zero
    # means the parser broke, the network is gone or the API is refusing.
    problems = []
    if images and resolved == 0:
        problems.append("no image resolved a version — the label read is broken, "
                        "not the estate")
    if not coverage_only and images and answered == 0:
        problems.append("no upstream feed answered — the network or the GitHub "
                        "API is the fault, not a quiet week")

    return {
        "counts": counts,
        "versions_resolved": resolved,
        "versions_probed": from_probe,
        "feeds_answered": answered,
        "images_examined": len(images),
        "ok": not problems,
        "detail": "; ".join(problems),
    }


def split_against(images, previous: dict) -> tuple[list, list]:
    """(new since the last report, still behind).

    The user-facing question is what is NEW, not what is behind. An image three
    versions behind for a month is not news, and repeating it weekly is how a
    report turns into wallpaper. So `new` is the delta against the last observed
    upstream release, and everything else behind is carried as one line.
    """
    new, still = [], []
    for record in images:
        if record["verdict"] != BEHIND:
            continue
        if previous.get(record["image"], {}).get("latest") == record["latest"]:
            still.append(record)
        else:
            new.append(record)
    return new, still


def state_from(images, previous: dict) -> dict:
    """Next run's baseline. A failed lookup NEVER overwrites a good one.

    scan.yml learned this from a real false alarm on 2026-07-30: a run in which
    every host failed wrote zeroes as the baseline, so the next healthy run
    reported an increase that had not happened. The same trap is here in a
    nastier form — an image whose feed errored this week would drop out of the
    state, and next week its unchanged release would resurface as NEW. So the
    map is carried forward and overwritten only where something was measured.
    """
    images_state = dict(previous)
    for record in images:
        if record["verdict"] == ERROR or not record["latest"]:
            continue
        images_state[record["image"]] = {"latest": record["latest"],
                                         "published": record["published"],
                                         "version": record["version"]}
    return {"images": images_state}


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def print_report(result) -> None:
    images = result["images"]
    summary = result["summary"]
    counts = summary["counts"]

    if result["new"]:
        print(f"NEW SINCE THE LAST REPORT ({len(result['new'])}):")
        for record in sorted(result["new"], key=lambda r: r["published"], reverse=True):
            print(f"  {record['name']}  {record['version'] or '?'} -> {record['latest']}")
            print(f"    released {record['published'][:10] or 'unknown'}"
                  f"   {record['latest_name']}")
            print(f"    notes    {record['url']}")
        print()

    if result["still_behind"]:
        print(f"STILL BEHIND ({len(result['still_behind'])}), unchanged since last report:")
        for record in sorted(result["still_behind"], key=lambda r: r["name"]):
            print(f"  {record['name']:<28} {record['version']} -> {record['latest']}")
        print()

    unmeasured = [r for r in images if r["verdict"] in (ERROR, NO_FEED, UNKNOWN_VERSION)]
    if unmeasured:
        # Never dropped and never folded into the totals as good news. These are
        # unmeasured, which is not the same as up to date.
        print(f"COULD NOT CHECK ({len(unmeasured)}):")
        for record in sorted(unmeasured, key=lambda r: (r["verdict"], r["name"])):
            print(f"  {record['name']:<28} {record['verdict']:<16} {record['detail']}")
        print()

    print(f"up to date: {counts.get(CURRENT, 0)}    "
          f"behind: {counts.get(BEHIND, 0)}    "
          f"no feed: {counts.get(NO_FEED, 0)}    "
          f"no version: {counts.get(UNKNOWN_VERSION, 0)}    "
          f"errors: {counts.get(ERROR, 0)}")
    probed_note = (f" ({summary['versions_probed']} by asking the running service, "
                   "the rest from image labels)") if summary.get("versions_probed") else ""
    print(f"{summary['versions_resolved']}/{summary['images_examined']} images "
          f"resolved a version{probed_note}; "
          f"{summary['feeds_answered']} upstream feeds answered.")

    remaining = summary.get("quota_remaining")
    if remaining is not None:
        limit = summary.get("quota_limit") or 0
        note = "" if summary.get("authenticated") else \
            "  (unauthenticated; set GITHUB_TOKEN for 5000/hour)"
        print(f"GitHub API quota left: {remaining}/{limit}{note}")
        if not summary.get("authenticated") and remaining < 15:
            # Printed because the next run is the one that breaks, and the
            # symptom then is a page of `error` verdicts with no obvious cause.
            print("  A full run costs ~45 requests. Another within the hour "
                  "will not complete.")
    if result["seeded"]:
        print()
        print("This run had no previous state, so nothing is reported as NEW — the")
        print("baseline was seeded instead. Next week's report is the first real one.")
    if not summary["ok"]:
        print()
        print(f"*** THIS RUN DID NOT MEASURE ANYTHING: {summary['detail']} ***")


def print_coverage(images, summary) -> None:
    print(f"{len(images)} distinct pinned image(s).\n")
    for record in sorted(images, key=lambda r: r["name"]):
        usable = record["verdict"] == "comparable"
        print("%s %-30s %-26s %-40s %s" % (
            "+" if usable else " ",
            record["name"][:30],
            (record["version"] or "-")[:26],
            (record["feed"] or "-")[:40],
            record["feed_source"] or record["verdict"]))
    usable = sum(1 for r in images if r["verdict"] == "comparable")
    errors = [r for r in images if r["verdict"] == ERROR]
    print(f"\ncomparable: {usable}/{len(images)}")
    if errors:
        # Printed, not counted away. An image whose labels could not be read is
        # unmeasured; folding it into "not comparable" beside an image that
        # genuinely has no version label would merge a fault with a fact.
        print(f"\nCOULD NOT READ LABELS ({len(errors)}):")
        for record in sorted(errors, key=lambda r: r["name"]):
            print(f"  {record['name']:<30} {record['detail']}")
    print("\nNo feed queries were made, so this consumed no GitHub rate limit.")
    if not summary["ok"]:
        # The positive control. It was already computed here and printed
        # nowhere, which made this view report `comparable: 0/48` as though
        # that were a finding about the estate rather than about itself.
        print(f"\n*** THIS RUN DID NOT MEASURE ANYTHING: {summary['detail']} ***")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", metavar="REF",
                        help="examine one image (repo@sha256:… or a catalog repo)")
    parser.add_argument("--coverage", action="store_true",
                        help="label coverage only; makes no GitHub requests")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--previous", metavar="FILE",
                        help="last run's state, for the NEW-since-last-report split")
    parser.add_argument("--state-out", metavar="FILE",
                        help="write the next baseline here (the only write this makes)")
    parser.add_argument("--probed", metavar="FILE",
                        help="JSON {image: version} from services that report "
                             "their own version; overrides the image label")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--root", default=str(ROOT))
    arguments = parser.parse_args()

    root = Path(arguments.root)
    overrides = read_overrides(root)
    pins = read_pins(root)

    if arguments.image:
        wanted = arguments.image
        if "@" in wanted:
            pins = [tuple(wanted.split("@", 1))]
        else:
            pins = [p for p in pins if p[0] == wanted]
            if not pins:
                print(f"{wanted} is not pinned in any catalog", file=sys.stderr)
                return 1

    if not pins:
        print("no pinned images found — the catalog parser is broken, because "
              "this repo pins dozens", file=sys.stderr)
        return 1

    # Read from the environment rather than a vault variable, deliberately. The
    # nightly runner executes from a `git archive` checkout where the vault does
    # not exist, so a vault_ reference here would be undefined exactly where it
    # is needed. Absent is fine: unauthenticated is the default path.
    github_token = os.environ.get("GITHUB_TOKEN") or None

    previous: dict = {}
    seeded = False
    if arguments.previous:
        try:
            stored = json.loads(Path(arguments.previous).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            # First run, or a corrupted file. Seeding means this run reports
            # nothing as NEW rather than announcing all 48 images at once —
            # the estate did not change, the report just started looking.
            stored, seeded = {}, True
        if isinstance(stored, dict):
            previous = stored.get("images") or {}
        seeded = seeded or not previous

    probed = {}
    if arguments.probed:
        try:
            probed = json.loads(Path(arguments.probed).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            # Not fatal, and deliberately not silent. Without the probe file
            # three images fall back to having no version label at all, which
            # reports as unknown-version — honest, but a quiet downgrade if
            # nobody is told the file was expected and missing.
            print(f"note: --probed {arguments.probed} could not be read; "
                  "falling back to image labels alone", file=sys.stderr)

    images = examine(pins, overrides, timeout=arguments.timeout,
                     github_token=github_token, coverage_only=arguments.coverage,
                     probed=probed)
    summary = summarise(images, coverage_only=arguments.coverage)
    summary["quota_remaining"] = QUOTA["remaining"]
    summary["quota_limit"] = QUOTA["limit"]
    summary["authenticated"] = bool(github_token)

    if arguments.coverage:
        if arguments.json:
            json.dump({"images": images, "summary": summary}, sys.stdout, indent=2)
            print()
        else:
            print_coverage(images, summary)
        return 0 if summary["ok"] else 1

    new, still_behind = split_against(images, previous)
    if seeded:
        still_behind, new = still_behind + new, []

    result = {"images": images, "summary": summary, "new": new,
              "still_behind": still_behind, "seeded": seeded}

    if arguments.state_out:
        Path(arguments.state_out).write_text(
            json.dumps(state_from(images, previous), indent=2) + "\n",
            encoding="utf-8")

    if arguments.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        print_report(result)

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
