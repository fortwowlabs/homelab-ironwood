# Container inventory audit, 2026-08-05

**Question asked:** identify what the ~19 "rogue containers" belong to, stopping
them one at a time if necessary to work out what they do.

**Answer: there are none, and nothing needed stopping.** The 19 was a
misreading of the release report — mine to fix, since the wording came from my
own summary. They are not unidentified containers. They are pinned images whose
*version number* the weekly release report cannot determine, which is a
statement about the label inside the image and nothing whatever about whether
the container is known, wanted, or accounted for.

Every one of the 19 is a service in a catalog in this repo, deployed by this
repo, and named in it.

Stopping services one at a time to discover their purpose would have been a
real outage in exchange for information already in the files.

But the question underneath — *is everything running on these machines actually
accounted for?* — had never been asked here, and it is a good question. So it
was answered properly, which is what the rest of this document is about.

---

## F1 — Every running container is accounted for. 54 of 54.

Enumerated directly from the container runtime on all three VMs — root podman on
svc-download, rootless podman as `homelab` (uid 10001) on svc-media and
svc-infra — and compared by **image digest** against every pin in the four
catalogs.

| | count |
|---|---:|
| containers running | 54 |
| digest matches the committed pin exactly | **54** |
| running a digest the catalog does not pin | 0 |
| image absent from every catalog | 0 |

Per host: svc-download 12, svc-media 12, svc-infra 30.

Two pinned images are deployed nowhere, and both are deliberate and already
documented in the repo:

- **`aquasec/trivy`** — the CVE scanner. Invoked per scan and exits; it is not
  meant to be a long-running service.
- **`immich-app/immich-machine-learning`** — pinned but not deployed on purpose.
  `apps.yml:297` says so, `roles/svc_infra/tasks/images.yml:43` notes its
  deliberate absence from the pull list, and `immich-server.container.j2:41`
  explains the env flag that keeps the server from calling it. Three separate
  places say the same thing, which is why this took two minutes to confirm
  rather than an outage to discover.

This is a stronger result than `make verify` produces, and it is worth saying
why — see F2.

## F2 — Nothing automatically checks that what is RUNNING matches what is COMMITTED

This is the finding worth acting on, and the audit above only exists because
nothing does it routinely.

`make verify` asserts that services are **active**. `make infra` reporting
`changed=0` asserts that the **configuration files** Ansible manages match the
committed tree. Neither compares the digest of a **running container** against
the catalog pin.

That gap is precisely the shape CLAUDE.md already warns about everywhere else:

> A container that is `up`, a unit that is `active`, and a Caddy smoke test
> returning 200 together prove the process started — not that the service
> functions.

A container started from an older image keeps running happily across every
existing check. Quadlet only re-pulls and restarts when the unit file changes,
so a hand-run `podman pull`, a manual `podman run`, a rolled-back deploy, or a
restart that raced a half-finished pull would all leave a container serving an
image that no commit describes — and every gate in the repo would stay green.

Today the answer is 54/54. Nothing keeps it that way.

## F3 — 12 images ship no version label at all

Not a fault in this estate; the publishers simply do not set
`org.opencontainers.image.version`.

| image | running as |
|---|---|
| grafana | grafana (svc-infra) |
| prometheus | prometheus (svc-infra) |
| node-exporter | node-exporter (all three VMs) |
| valkey | paperless-redis, immich-redis, netbox-redis, nextcloud-redis (svc-infra) |
| nextcloud | nextcloud (svc-infra) |
| uptime-kuma | uptime-kuma (svc-infra) |
| ntfy | ntfy (svc-media) |
| romm | romm (svc-media) |
| it-tools | it-tools (svc-infra) |
| bambuddy | bambuddy (svc-infra) |
| jdownloader-2 | jdownloader (svc-download) |

The report knows what upstream's newest release is for all of these. It just
cannot say which one you are on, so it reports `unknown-version` — unmeasured,
not up to date.

**Several of these can be fixed**, because the applications report their own
version over HTTP even though the image does not label it. That is a better
source than a label anyway: it is what the running process says about itself,
not what the build system claimed. Done in P2 for **Grafana, Prometheus and
node-exporter**.

> **Correction.** This section first named Uptime Kuma as a fourth. It was
> asserted from familiarity, not measured, and it is wrong: `/api/entry-page`,
> `/metrics` and `/` all return no version to an unauthenticated caller,
> because it is a socket.io application whose metrics endpoint needs a key.
> Probing it was attempted during P2 and abandoned. Left in the record rather
> than quietly deleted, because "I assumed an endpoint existed" is exactly the
> kind of claim this document is meant to stop repeating.

## F4 — 5 images label something that is not a version

| image | label | what it actually is |
|---|---|---|
| calibre-web-automated | `cd80d60b-ls59` | upstream commit + LinuxServer build |
| lazylibrarian | `a7c70e36-ls311` | same |
| webtop | `15bc101c-ls308` | same |
| open-webui | `main` | a git branch |
| minecraft-server | `java25` | a Java-runtime variant |

These are correctly reported as `unknown-version`. The three LinuxServer ones
are the interesting case: their upstream projects version by commit, so there is
genuinely no version number to compare — LinuxServer's own build number
(`-ls311`) is the only thing that increments, and it moves on base-image
rebuilds that change nothing about the application.

Nothing to fix. Recorded so the next person does not try.

## F5 — 2 postgres pins have no release feed, by design

`library/postgres` (netbox-db, nextcloud-db) and `immich-app/postgres`
(immich-db). The postgres GitHub repository is a read-only mirror that publishes
no releases, and Immich's base-images repository publishes none either. Both are
recorded as deliberate empty overrides so they are not re-investigated yearly.

## F6 — My own coverage figure was optimistic: 27 compare, not 29

`scripts/release-check.sh --coverage` reports 29 comparable, and I repeated that
number in `CLAUDE.md`, in `docs/plans/release-report.md` and to you.

A full run compares **27**. The difference is `mariadb` and `searxng`: both have
a usable version label and a real upstream repository, so coverage mode counts
them — but neither repository publishes GitHub *releases*, which coverage mode
cannot know without spending the API request it exists to avoid.

The figure is not wrong so much as measuring something slightly different, and I
stated it as though it were the same thing. Corrected in both files.

**Superseded by P2, which raised it to 30.** Three images that carry no version
label are now measured by asking the running service. `--coverage` still reports
29 — it makes no probes — so the two numbers have now drifted apart in the other
direction. Neither is wrong; the report prints which it is quoting.

---

## Plan

Ordered by value. Each item stands alone.

**Status: P1 and P2 implemented 2026-08-05. P3 needs a token from you. P4 is
deliberately nothing.**

### P1 — Assert running digests against the catalog, in `make verify` (F2) — DONE

Implemented as `roles/service_vm/templates/container-drift.sh.j2`, included by
all three VM verify paths. It compares each running container against **its own
Quadlet unit** rather than against the catalog directly, which is a better
chain and needs no catalog plumbing on the host:

```
commit  ==  unit file on disk    (proved by changed=0 on a clean tree)
unit file  ==  running container (proved by this check)
```

It checks **both podman contexts on every host**, root and rootless, rather
than selecting one from a per-host flag — a manually started container does not
consult that convention, and a check that looks only where it expects trouble
is not looking for trouble.

Live result: 12 + 12 + 30 = **54 containers, all matching**, which reproduces
the manual audit above exactly.

**The failure paths are tested, and that is the part that matters.** On live
hosts the answer is always OK, so left alone this would be a check nobody could
tell had stopped working. `tests/validate_container_drift.py` runs the script
against fixtures with a stub `podman` and asserts all six outcomes — the four
failures more than the two passes:

| case | expected |
|---|---|
| every container matches its unit | pass |
| container running an image its unit does not name | **DRIFTED**, rc 1 |
| container with no Quadlet unit at all | **NO QUADLET**, rc 1 |
| podman answers but reports nothing running | **CANNOT LOOK**, rc 2 |
| neither podman context queryable | **CANNOT LOOK**, rc 2 |
| no Quadlet files where expected | **CANNOT LOOK**, rc 2 |

The three `CANNOT LOOK` cases are the point. Each is a "could not look" that an
ordinary implementation reports as "nothing found". Verified by deliberately
disabling the positive control, at which point the empty-list case printed
`container drift: OK (0 running container(s)...)` and the test caught it.

**One thing was attempted and not done.** Starting a throwaway container on
svc-download to prove the orphan path against a live host was blocked by the
safety classifier, and was not worked around. The fixture test covers the same
logic; what remains unproven is only that a real unmanaged container looks the
way the fixture says it does.

### P2 — Read versions from the applications that expose them (F3) — DONE

`release_version_probes` in `main.yml`, probed by `release.yml` and passed to
the check as `--probed`. A probed version wins over a label, because asking the
running process is a better source than reading what its build system claimed.

Three services, each confirmed against the live endpoint with the value read
back, one regex mechanism covering both JSON and Prometheus text:

| service | endpoint | reads |
|---|---|---|
| Grafana | `/api/health` | `13.1.1` |
| Prometheus | `/api/v1/status/buildinfo` | `3.13.1` |
| node-exporter | `/metrics` (loopback) | `1.12.1` |

Effect: **30 images now compare, up from 27**, and unmeasured fell 21 → 18. It
immediately found two things nothing could previously see — Grafana 13.1.1
against upstream 13.1.2, and Prometheus 3.13.1 against 3.13.2.

A captured value is discarded unless it still looks like a version, so a service
that changes its response shape reverts to `unknown-version` rather than
reporting whatever the regex happened to catch.

**Uptime Kuma was tried and deliberately excluded.** `/api/entry-page`,
`/metrics` and `/` all return no version to an unauthenticated caller — it is a
socket.io application and its metrics endpoint needs a key. Recorded in the
variable's comment so the same three URLs are not re-probed next year. Guessing
a field would be worse than the honest gap.

The rest of F3 is deliberately not attempted: Nextcloud, ntfy, romm, it-tools,
bambuddy and jdownloader would each need bespoke handling for one service
apiece, and `unknown-version` is already an honest answer.

### Original plan text follows

#### P1 as originally specified

The whole finding. Add a check per VM that reads every running container's image
digest and asserts it is pinned by a catalog, failing with the container name
and both digests when it is not.

Rules it must follow, from the repo's own scar tissue:

- **It must distinguish "none found" from "could not look."** An empty container
  list is a *failure*, not a pass — the same trap as the image scan that reported
  zero vulnerabilities because it could not `chdir`. Its positive control is
  simple and total: every VM must report at least one container, and the count
  must match the number of active Quadlet units.
- **It must not stop at the first mismatch.** Report all of them; a partial
  answer to "what has drifted" is worth much less than the whole list.
- **Non-disruptive.** `podman ps` and `inspect` only, so it belongs in
  `verify.yml`, not `verify-disruptive.yml`.

Estimated: one task file in `roles/service_vm/`, included by all three VM roles.

#### P2 as originally specified

For Grafana, Prometheus, node-exporter and Uptime Kuma, ask the running service
its version over HTTP instead of reading a label that does not exist. Moves four
services from `unknown-version` to comparable, and does it from a better source.

Do this as an explicit per-service map, not a guess: each entry names the URL and
the JSON field. A wrong field silently yields a wrong version, which is worse
than the honest gap it replaces — the same rule as the feed overrides.

Deliberately **not** attempted for the rest of F3. Nextcloud, ntfy, romm,
it-tools, bambuddy and jdownloader would each need bespoke handling for one
service apiece, and `unknown-version` is already an honest answer.

### P3 — Get a GitHub token in place — NEEDS YOU

Still open, and the only item that does. See below.

#### P3 as originally specified

Unrelated to the audit but it is the live constraint on the report: 45 requests
of a 60/hour budget, and the first real run exhausted it on the last image.
`vault_github_token` is wired and empty; setting it makes the ceiling 5000/hour.
Five minutes, no code.

### P4 — Nothing, for F4 and F5

Recorded as decisions, not TODOs. Both would mean guessing at a version that
does not exist, and the report already says so accurately.

---

## What this audit did not check

- **Processes inside containers.** This compares image digests. A compromised
  image with the right digest is out of scope here and is what `make scan`
  covers.
- **The hypervisor.** No containers run on PVE; its posture is reported
  separately by the PVE health checks.
- **Whether each service is doing its job.** That is the standing rule in
  CLAUDE.md and no inventory audit substitutes for it. F1 says every container
  is one this repo asked for. It does not say any of them work.
