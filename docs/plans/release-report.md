# Weekly release report

**Status:** implemented 2026-08-05.

A weekly report of which upstream projects have shipped a new version since the
last report, for every container image this estate pins.

It answers a question nothing here answers today, and it is deliberately *not*
the same question `make image-check` answers.

---

## Why `make image-check` is not this

`image-check` resolves a recorded `# tag:` to the digest that tag points at
today, and reports the pin as BEHIND when they differ. That is a **digest**
question. Three consequences make it unable to be a release report:

1. **It covers 13 of 48 pinned images.** A pin with no `# tag:` comment is
   invisible to it. That is by design — the `BUMP PROCEDURE` block at the top
   of `apps.yml` argues at length that recording `latest` on a service whose
   major version migrates data one way turns the report into a standing
   recommendation to destroy a database.

2. **The 35 it cannot see are the interesting ones.** Sonarr, Jellyfin,
   Authelia, Grafana, Syncthing, Open WebUI, Immich, Home Assistant — every
   image deliberately left untracked is one where you actually want to *read
   about* the release before deciding, which is precisely what a report is for
   and precisely what a bump recommendation is not.

3. **A moved digest is not a new version.** LinuxServer rebuilds on base-image
   changes; `4.0.19.2979-ls320` becoming `4.0.19.2979-ls321` moves the digest
   without Sonarr having released anything. Conversely a pin can sit on an old
   version for months with its tag unmoved, and `image-check` calls that "up to
   date".

So this is a sibling, not a replacement. `image-check` says *your pin has
drifted from the tag you follow*. This says *upstream shipped 2.1.3 on Tuesday,
here are the notes, you are on 2.1.2*.

---

## How the current version is discovered without a recorded tag

This is the part that makes the whole thing possible, and it was verified
against the live registries before any of it was designed.

An image's config blob carries OCI labels. Two of them matter:

```
org.opencontainers.image.version   4.0.19.2979-ls320
org.opencontainers.image.source    https://github.com/linuxserver/docker-sonarr
```

Both are readable **from the pinned digest**, over the same read-only registry
manifest API `scripts/image-digest.sh` already speaks — no pull, no local state,
no recorded tag. The digest that carries no memory of its tag does carry a
memory of its version, and that is a strictly better fact: a tag is where the
image came from, a version is what it *is*.

`.source` gives the upstream GitHub repository, so the release feed is
discoverable too, with no configuration.

### Measured coverage, 2026-08-05

Across all 48 distinct pins in the four catalogs:

| | count | what it means |
|---|---:|---|
| version **and** feed usable | 29 | fully comparable |
| feed known, version not usable | 15 | upstream release reportable, local version unknown |
| no feed at all | 4 | unmeasured, and says so |

29/48 comparable with almost no configuration, against `image-check`'s 13/48
with a hand-recorded tag each. That gap is the argument for building this.

**Two corrections to this number, both downward, both mine.**

The first draft said 33. That was before the version-shape rule below rejected
four labels that are not versions, and it was wrong in the direction that
matters — it counted four images as measured that would have reported a
permanent false "behind".

And 29 is what `--coverage` reports; a **full run compared 27**. Coverage mode
counts mariadb and searxng, which have a usable version label and a real
upstream repository but publish no GitHub *releases* — something it cannot know
without spending the request it exists to avoid. Both figures are honest about
different things; the full-run one is what to quote for "how much does this
actually measure". See `docs/plans/container-inventory-audit.md` F6.

**Since then it is 30**, because three images that carry no version label are
now measured by asking the running service instead — see "Versions from the
running process" below. `--coverage` still says 29: it makes no probes, so its
number and the report's have drifted apart in the other direction now. Neither
is wrong; they answer different questions, and the report prints which.

### The four ways a label lies, all of them observed

None of these are hypothetical. Each was seen in the coverage run above.

- **A version that is not a version.** `open-webui` labels `main`;
  `itzg/minecraft-server` labels `java25`. Branch and variant names.
- **A version that is a commit.** Three LinuxServer images label the upstream
  commit they built from: `cd80d60b-ls59`, `a7c70e36-ls311`, `15bc101c-ls308`.
  These look far more like versions than `main` does, which is exactly why they
  slipped through the first implementation and reported as behind.
- **A source that points at the wrong project.** `calibre-web-automated`
  labels its source as `linuxserver/docker-baseimage-ubuntu` — the *base
  image*. Followed naively, the report would announce Ubuntu base-image
  releases under Calibre-Web's name. This is the worst of the four because it
  is confidently wrong rather than empty, and nothing about it looks broken.
- **No labels at all.** The Docker official images (`postgres`, `nextcloud`)
  and several others carry neither.

So the labels are a default, not a source of truth. Every one is overridable
per image, and a version must look like a version — a digit and a dot — before
it is compared to anything. Where that rule is wrong it is wrong in the safe
direction: a project versioning as a bare `v5` reads as `unknown-version`,
which means unmeasured and says so, rather than as a permanent false `behind`.

### Versions from the running process

Added 2026-08-05, after the container inventory audit. Some images carry no
version label at all, but the *application inside* reports its version over
HTTP. Asking it is strictly better than reading a label: the label says what the
build system claimed, the endpoint says what is actually answering right now.

So `release_version_probes` in `main.yml` maps an image to a URL and a regex,
`release.yml` probes them, and a probed version **wins over a label** wherever
both exist.

| service | endpoint | field |
|---|---|---|
| Grafana | `/api/health` | `version` |
| Prometheus | `/api/v1/status/buildinfo` | `version` |
| node-exporter | `/metrics`, over **loopback** | `node_exporter_build_info{version=…}` |

One regex mechanism covers JSON and Prometheus text alike, which avoids a
parser branch that would need testing of its own. node-exporter is probed on
127.0.0.1 rather than the LAN address because firewalld admits `:9100` only
from svc-infra — scraping it from anywhere else returns nothing, which reads
exactly like a service with no version. That trap already cost a day during the
NFS outage work.

Effect: compared images 27 → 30, unmeasured 21 → 18, and it immediately found
Grafana on 13.1.1 against 13.1.2 and Prometheus on 3.13.1 against 3.13.2 —
neither visible to any check here before.

A captured value is discarded unless it still looks like a version, so a service
that changes its response shape reverts to `unknown-version` rather than
reporting whatever the regex happened to catch.

**Uptime Kuma is deliberately absent, and was named as a candidate before being
measured.** `/api/entry-page`, `/metrics` and `/` all return no version to an
unauthenticated caller — it is a socket.io application whose metrics endpoint
needs a key. The three URLs are recorded in the variable's comment so they are
not re-probed next year.

`make release-check` from a workstation makes no probes, so those three read as
`unknown-version` there. That is honest rather than ideal: the probes need to
run on the host that can reach the services.

### A guessed feed is worse than no feed

The same rule `apps.yml` states for tags applies here, for the same reason and
more sharply. An override that points at a plausible-looking repository which
is not actually the upstream will report someone else's releases as yours,
indefinitely, and it will look completely healthy doing it.

So an override is recorded only after confirming the repository exists and
publishes releases. Anything unconfirmed stays unmapped and reports as
`no-feed`, which reads as **unmeasured** — the same vocabulary, and the same
honesty, as `image-check`'s UNTRACKED.

---

## Verdicts

Five states per image, never a boolean. The repo has been bitten four times by
a check whose empty result meant "could not look" and rendered as "all clear";
`scan.yml` escalates on `inconclusive` for exactly this reason.

| verdict | meaning |
|---|---|
| `current` | local version equals the newest upstream release |
| `behind` | upstream has shipped a newer release |
| `unknown-version` | feed resolved, but no usable local version to compare |
| `no-feed` | no upstream feed known — **unmeasured**, not current |
| `error` | the lookup itself failed: registry down, rate limit, 5xx |

`error` and `no-feed` are counted and printed separately in every output. They
never fold into "nothing new".

---

## What the report actually says

The user-facing question is *what is new since last week*, not *what is behind*.
Those differ: an image that has been 3 versions behind for a month is not news,
and repeating it weekly is how a report becomes wallpaper.

So state is carried in `release_state_file` (beside `scan_state_file`), holding
the last observed upstream release per image. Each run splits into:

- **NEW SINCE LAST REPORT** — the headline. Upstream release that was not in
  last week's state. Name, date, and a link to the notes.
- **STILL BEHIND** — carried forward, one line each, no notes. Present so a
  deferred upgrade cannot quietly become invisible.
- **COULD NOT CHECK** — the `error` and `no-feed` sets, always shown.

First run has no state and would otherwise report all 48 images as "new". It
seeds the baseline and says so, exactly as `scan.yml` does for its first-night
totals.

---

## Positive control

Per the standing rule — *a check with no positive control is a check nobody can
tell is broken* — the run fails loudly rather than reporting a quiet all-clear
when:

- **zero images resolved a version**, or
- **zero feeds returned a release**.

Both are impossible if the thing ran. 48 images cannot all lose their labels in
one week, and 30-odd active projects cannot all stop publishing. Either result
means the parser broke, the network is gone, or the GitHub API is refusing —
not that the world stopped shipping software.

This is a real positive control, not the weaker tri-state substitute the
credential canary had to fall back on: there is a fact that *must* be true if
the check ran, and it is asserted.

### GitHub rate limiting is the likeliest way this breaks

Unauthenticated, the GitHub API allows **60 requests/hour per IP**. Measured, a
full run costs **45**: 43 distinct upstream repositories after deduplicating
shared upstreams — beszel and beszel-agent are one repo, immich-server and
immich-machine-learning are one repo — plus a second call owed by the two that
404 on `/releases/latest`.

45 of 60 fits a weekly run and nothing else. Running the report twice within an
hour exhausts the budget, and the second run reports `error`, loudly, rather
than an all-clear.

**A conditional-request scheme was built for this and then removed, because the
premise was false.** The design sent last week's `ETag` and claimed GitHub does
not charge quota for a 304. Measured directly against `/rate_limit` before and
after: a run in which **40 of 44 feeds answered 304 still consumed 45 requests**.
The machinery was deleted rather than left in place under a comment explaining
that it does not do what it says — a caching layer that saves nothing is worse
than none, because the next person reads the comment instead of the meter.

What is actually done about it:

1. Deduplicate by repository before querying, not per image.
2. A 403/429 is `error`, never `current`. Exhaustion is visible, not silent.
3. Every response's `X-RateLimit-Remaining` header is read and the remaining
   quota is printed in the report and on the console — free, because asking
   `/rate_limit` would itself be a request. Below 15 remaining, the report says
   outright that another run within the hour will not complete, since the
   symptom otherwise is a page of `error` verdicts with no obvious cause.
4. `GITHUB_TOKEN` raises the limit to 5000/hour and makes the whole question go
   away. Set `vault_github_token` and redeploy; it renders to
   `/etc/homelab-release.env` (0600 root) at **deploy** time, when the vault
   exists, and is read from that file at **run** time — never as a `vault_`
   variable inside the playbook, because the runner executes from a
   `git archive` where the vault does not exist. That is the mistake `b77f27f`
   fixed and it is not going to be made again here.

   The token needs **no scopes**: every repository read is public and it is
   there only to count faster. A classic token with nothing ticked, or a
   fine-grained one limited to public read, is the correct shape.

The timer carries `RandomizedDelaySec=600` for the same reason: 45 requests
against a 60/hour budget is worth keeping away from anything else that might
share the address.

---

## Where it runs

**svc-infra, weekly, Friday 08:30**, on the same runner as the nightly scan —
same checkout, same venv, same account (`svcops`).

Verified reachable from svc-infra before choosing it: `api.github.com` returns
200 and `ghcr.io/v2/` returns its 401 auth challenge, both in ~0.14s. Registry
egress was already proven by Trivy, which scans every image from this host.

**08:30** sits in the gap between the alert canary (Mon 08:00) and certwatch
(09:15), clear of the whole nightly sequence, which ends at 07:00 + jitter.

**Friday, changed from Monday on 2026-08-06** at the owner's request. A report
is worth what the chance of acting on it is worth, and upgrading this estate is
weekend work: a Friday morning report lands with the time to do something about
it still ahead, where a Monday one lands at the start of the week most likely to
swallow it.

It also resolves an argument the Monday slot had to *accept* rather than answer.
`homelab-alert-canary.timer` records that "two weekly checks on one day means
one bad night can take out both", and the old slot sat thirty minutes after it.
The original reasoning here — that the two share only the host and ntfy, that
they are 30 minutes apart, and that a missed report costs a week of reading
rather than a week of undetected silence — was defensible, and it is repeated
here because it was the honest trade at the time. But defensible is not the same
as fixed. Friday removes the coupling outright, which is a better outcome
arrived at for an unrelated reason.

Runnable by hand as `make release-check`, identically to `make scan`.

---

## Report-only, and gated as such

Nothing in this path pulls, bumps, deploys or restarts. It reads registry
metadata and a release feed, and writes a report.

The new paths are added to `SCAN_PATHS` in `tests/validate_scan_readonly.py`,
which fails the build if `--remediate`, an upgrade invocation, `state: latest`
or `podman pull` appears under any of them. Per CLAUDE.md: add the path to the
gate, never work around it.

**It also does not print `make image-bump` for untracked images.** That
restraint is the entire point. For the images the `BUMP PROCEDURE` deliberately
leaves untracked, printing a one-line bump command is the exact harm that block
was written to prevent. The report links the release notes and stops there —
*decide*, then bump by hand.

---

## Deliverables

| file | purpose |
|---|---|
| `scripts/release_check.py` | the engine: catalogs → labels → feeds → verdicts |
| `scripts/release-check.sh` | wrapper; `make release-check` |
| `scripts/image-release.sh` | one image; `make image-release REF=…` |
| `release.yml` | the playbook; `make release-report` |
| `roles/svc_infra/templates/release-run.sh.j2` | runner wrapper, mirrors `scan-run.sh.j2` |
| `roles/svc_infra/templates/release-report.txt.j2` | text report |
| `roles/svc_infra/templates/release-report.html.j2` | browsable report at `scan.<domain>` |
| `roles/svc_infra/files/homelab-release@.service/.timer` | the weekly unit |
| `inventory/group_vars/all/main.yml` | `release_*` vars and `release_feed_overrides` |
| `inventory/host_vars/svc-infra.yml` | the `OnFailure` drop-in registration |
| `tests/validate_release_overrides.py` | new gate (below) |

### The new gate

`release_feed_overrides` is a hand-maintained map, which makes it the part most
likely to rot: an image gets removed from a catalog and its override lingers,
pointing the report at a project this estate no longer runs. The gate fails the
build when an override names an image that no pin references — the same shape as
`validate_scan_image_coverage.py`.

It does not check that the repositories exist. That needs the network, and
`make validate` is offline by construction.

---

## What this does not do

Stated plainly, so none of it reads as an oversight later.

- **It does not bump anything.** No `image-bump` invocation, no PR, no
  automation. See above — this is deliberate, not a missing feature.
- **It does not read release notes for breaking changes.** It links them. The
  judgement about whether Sonarr v5 migrates a database one way is exactly the
  judgement `BUMP PROCEDURE` step 4 reserves for a person.
- **It does not cover non-container software.** Host packages are `dnf` errata,
  already reported nightly by `scan.yml`; the hypervisor's patch posture is
  reported by the PVE checks. Overlapping them here would be a second number
  that disagrees with the first.
- **It does not track projects that publish no GitHub release.** Some tag
  without releasing; those read as `no-feed`. Adding tag-listing as a fallback
  is possible and deliberately deferred — tags are noisier (release candidates,
  nightly builds, per-arch tags) and getting that filter wrong produces
  confident nonsense, which is worse than an honest gap.
- **It does not verify the override targets are the right project.** Only that
  the image still exists. Correctness of a mapping is a human judgement made
  once, when it is recorded.

---

## Verification, 2026-08-05

Four things were checked, in this order, because each one only means something
if the previous one held.

**1. The label read works against live registries.** 48 pins, 47 resolved their
config blob; 29 yielded a usable version. Verified on ghcr.io, lscr.io and
docker.io, which authenticate and redirect differently — the blob fetch needs
the same bearer token as the manifest on ghcr.io, and returns 401 rather than a
redirect without it, which is how the first implementation failed silently
across every image at once.

**2. The whole report ran end to end**, first from a workstation and then on
svc-infra through `make release-report`: 48 images, 18 genuinely behind, 9 up to
date, 21 unmeasured. Among the findings were Immich v3.0.3 → v3.1.0,
paperless-ngx 2.20.15 → v3.0.5 and Home Assistant 2026.7.3 → 2026.8.0. Both the
text and HTML reports rendered, and the baseline was seeded.

That run also **ran out of GitHub quota on its 48th image**, starting from 50
available against a cost of ~45. It reported that one image as
`error  feed: HTTP 403 from api.github.com (quota 0/60)` and escalated, rather
than reporting it as current. The design worked exactly as intended — and the
margin it demonstrated is why `GITHUB_TOKEN` stopped being a documented
possibility and became a rendered file: `/etc/homelab-release.env`, 0600 root,
from `vault_github_token`, empty by default, needing no scopes because every
repository read is public.

**3. The degraded path was exercised for real, not simulated.** The quota was
already exhausted by the measurements above, so the first live run on svc-infra
had nothing to spend. It produced no parseable output, and:

- the check exited non-zero and the playbook carried on rather than aborting,
- the state file was **not** promoted (`when: release_ok`), so a broken run
  could not poison next week's baseline,
- `homelab release check DEGRADED` was published to `homelab-alerts` and read
  back off ntfy.

That last point found a real defect. The routine message on the muted topic
read *"0 new upstream release(s) … 0 up to date across 0 pinned images"* — at a
glance, indistinguishable from a quiet week. The two messages disagreed, and
only one of them gets read casually. The summary line now leads with
`DEGRADED — this run measured nothing`, on both topics.

The same run also caught `{{ domain }}` where the variable is `service_domain`,
which failed the escalation task outright. Both defects were in the reporting
path, which is the part no offline gate can see and the part that only matters
when something is already wrong.

**4. The baseline survives a partial run**, which is the rule scan.yml learned
from a real false alarm and the one most likely to bite here. A later run with
the quota already at zero measured only 8 of 48 images and reported 33 as
`error` — and the state file still held **42** remembered releases afterwards,
because `state_from` carries the previous entry forward wherever this run
measured nothing. Had it written only what it measured, every one of those 34
unchanged releases would have resurfaced as NEW the following week, and a
report that cries wolf once has spent the only credibility it had.

**5. `make validate` passes**, 25 gates including the new
`validate_release_overrides.py`, and `make infra` reports `changed=0` on a
second run.

### What is not yet proven

**The timer has not fired on its own schedule.** It is enabled and next due
**Friday 2026-08-07 08:30** (+ up to 600s jitter). Everything below the timer
has been run by hand and by `make release-report`, so what remains unverified is
specifically systemd starting the unit — the same gap the NFS guard closed by
waiting, and it will close the same way.

Moving the day from Monday to Friday on 2026-08-06 brought that first
unattended run forward from the 10th to the 7th, which is a day away rather
than four. Incidental, but it means this closes sooner.

~~**No `NEW SINCE THE LAST REPORT` section has ever rendered with content.**~~
**Proven 2026-08-06.** A run against an established baseline reported exactly
two, correctly split from the eighteen carried forward:

```
NEW SINCE THE LAST REPORT (2)
semaphore  v2.18.28 -> v2.19.7      released 2026-08-06
sabnzbd    5.0.4-ls262 -> 5.0.4-ls265   released 2026-08-06
```

Both had genuinely shipped that day, and the other eighteen behind-images
stayed in `STILL BEHIND` rather than being re-announced — which is the whole
point of the delta and the thing that keeps this from becoming wallpaper.

The sabnzbd entry is also a fair illustration of the report's limits: the
application version did not change at all, only LinuxServer's build number
(`-ls262` to `-ls265`). That is a real new image worth knowing about and it is
not a new SABnzbd. The report says what moved and links the notes; deciding
whether a base-image rebuild is worth a deploy is the reader's call.

## Reproducing the coverage measurement

```bash
scripts/release-check.sh --coverage     # label coverage only, no feed queries
scripts/image-release.sh lscr.io/linuxserver/sonarr@sha256:24acea…
```

Both are read-only and safe to run at any time. `--coverage` makes no GitHub
requests at all, so it cannot consume the rate limit.
