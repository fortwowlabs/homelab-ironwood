# Software Version Bump Backlog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Work down the backlog of outdated container images and this repo's own Ansible/Python tooling pins that the 2026-09-04 audit surfaced, using the estate's existing, deliberate bump machinery — never PVE, never the VM OS (Rocky) errata, both explicitly out of scope.

**Architecture:** No new mechanism. Every task below is an application of the `BUMP PROCEDURE` block already documented at the top of `inventory/group_vars/all/apps.yml` (mechanical `make image-bump` where a `# tag:` is recorded, hand-bump-by-digest where it is not) plus, for tooling, a plain pin edit in `requirements.txt`/`requirements-dev.txt`. Each task ends in the same shape: validate offline, bump, validate again, deploy exactly one affected VM, verify the *application* (not just the container), commit.

**Tech Stack:** Ansible, Podman/Quadlet on Rocky Linux 10 (svc-download/svc-media/svc-infra), the repo's own `scripts/image-bump.sh` / `scripts/image-check.sh` / `scripts/release_check.py`.

**Spec:** No separate spec document. This plan implements the `BUMP PROCEDURE` comment block in `inventory/group_vars/all/apps.yml` (the authoritative, already-written procedure) against the concrete drift found by `make image-check`, `make release-report` and `make scan` on 2026-09-04. Read that block before starting Phase 1 — it is the contract every task below assumes.

## Global Constraints

- **Never PVE.** No task here touches `thurgadin`/`thurgadin-ssh`. PVE version bumps are a separate, not-yet-planned piece of work.
- **Never the Rocky OS errata.** The 104 pending security errata on the three VMs are governed by the estate's deliberate no-auto-update policy (`docs/unattended.md`) and are out of scope for this plan.
- **One VM at a time.** `make dl` / `make media` / `make infra` — never a blind full `make deploy` mid-backlog.
- **A bad digest is safe (pre-pull aborts before restart); a bad *version* is not.** Before bumping anything that persists data, re-read what it persists. Never write `state: latest` or guess a tag.
- **`make image-bump` refuses a digest pinned in more than one place.** Shared pins (the `postgres:18-alpine` pin, the three-way `valkey` pin) are hand-edited, and the edit is a decision about every consumer at once.
- **Follow every bump with `make scan`** (or, where noted, `make scan ARGS="-e scan_oscap_force=true"` is *not* needed — that flag is for the weekly OpenSCAP benchmark only) and confirm the image's CVE count actually moved. A bump that doesn't move the count bought nothing and is worth knowing before calling a task done.
- **Branching.** One branch per phase below (`chore/bump-<phase-slug>`), multiple commits inside it — the same shape used for the 2026-09-04 PVE/tooling-visibility work. `git switch -c` off current `main` at the start of each phase; merge, push, delete the branch at the end of that phase before starting the next (so drift is never hiding behind an open branch).

---

## Phase 1 — Tracked, mechanical bumps (lowest risk)

These four already carry a recorded `# tag:` in `apps.yml`, so `make image-check` has already told us they drifted and `make image-bump` resolves and rewrites the pin without any manual digest work. Confirmed live on 2026-09-04 via `make image-check`:

| Image | Pinned tag | Service(s) | Why low risk |
|---|---|---|---|
| `docker.io/henrygd/beszel-agent:latest` | `latest` | Beszel agent (all three service VMs — svc-download, svc-media, svc-infra) | Stateless metrics agent |
| `docker.io/jlesage/jdownloader-2:latest` | `latest` | jdownloader-2 (svc-download) | Config persists outside the image; explicitly named safe-to-track in the BUMP PROCEDURE block |
| `ghcr.io/gethomepage/homepage:latest` | `latest` | Homepage dashboard (svc-media) | Config-file only, no database |

(The fourth tracked-behind hit from `image-check`, `docker.io/library/postgres:18-alpine`, is **not** mechanical — it is pinned in two places and is handled separately in Phase 2.)

### Task 1.1: Branch for Phase 1

**Files:** none yet.

- [ ] **Step 1: Create the branch**

```bash
cd /home/tv/dev/homelab-ironwood
git status --porcelain   # must be empty before branching
git switch -c chore/bump-tracked-images
```

- [ ] **Step 2: Confirm current drift**

```bash
make image-check
```

Expected: the three images above (plus postgres, which this phase ignores) listed under `BEHIND`.

### Task 1.2: Bump beszel-agent

**Files:**
- Modify: `inventory/group_vars/all/main.yml` (the standalone `beszel_agent_image` variable, consumed by all three service-VM roles — `make image-bump` finds and rewrites it; do not hand-edit)

**Interfaces:**
- Consumes: `scripts/image-bump.sh` (already exists, unmodified)
- Produces: an updated `@sha256:` pin + `# was <date>: sha256:<old>` comment that `tests/validate_image_provenance.py` will check against git history once committed

- [ ] **Step 1: Bump**

```bash
make image-bump REF=docker.io/henrygd/beszel-agent:latest
```

- [ ] **Step 2: Validate offline**

```bash
make validate
```

Expected: exit 0, including `tests/validate_image_provenance.py` (it only fully confirms after commit — that's fine, it still parses the new pin shape now).

- [ ] **Step 3: Deploy the affected VM(s)**

Beszel agent runs on all three service VMs (`beszel_agent_image` in `inventory/group_vars/all/main.yml`, consumed by svc_download/svc_media/svc_infra roles alike):

```bash
make dl USE_VAULT_FILE=1
make media USE_VAULT_FILE=1
make infra USE_VAULT_FILE=1
```

Expected: `changed=1` for the beszel-agent container on each, everything else `ok`.

- [ ] **Step 4: Verify the application, not just the container**

```bash
make verify USE_VAULT_FILE=1
```

Then confirm the agent is actually reporting, not just running — check the Beszel hub UI (`https://<beszel-hub-domain>`) shows both hosts as online with a recent "last seen" timestamp, not merely `systemctl status` green.

- [ ] **Step 5: Confirm the CVE count moved**

```bash
make scan USE_VAULT_FILE=1
```

Read `https://scan.<domain>/latest.txt` (or the fetched copy) and confirm `docker.io/henrygd/beszel-agent`'s critical/high count changed from the 2026-09-04 baseline. If it didn't move, note that in the commit message rather than silently proceeding.

- [ ] **Step 6: Commit**

```bash
git add inventory/group_vars/all/main.yml
git commit -m "$(cat <<'EOF'
chore: bump beszel-agent to the current latest digest

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 1.3: Bump jdownloader-2

Same shape as Task 1.2, different image and VM.

- [ ] **Step 1: Bump**

```bash
make image-bump REF=docker.io/jlesage/jdownloader-2:latest
```

- [ ] **Step 2: Validate**

```bash
make validate
```

- [ ] **Step 3: Deploy svc-download only**

```bash
make dl USE_VAULT_FILE=1
```

- [ ] **Step 4: Verify**

```bash
make verify USE_VAULT_FILE=1
```

Then open jdownloader-2's UI through Caddy and confirm it logs in and shows its existing download queue/config (proves the config volume survived the bump, not just that the container started).

- [ ] **Step 5: Confirm the CVE count moved**

```bash
make scan USE_VAULT_FILE=1
```

- [ ] **Step 6: Commit**

```bash
git add inventory/group_vars/all/apps.yml
git commit -m "$(cat <<'EOF'
chore: bump jdownloader-2 to the current latest digest

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 1.4: Bump homepage

Same shape again.

- [ ] **Step 1: Bump**

```bash
make image-bump REF=ghcr.io/gethomepage/homepage:latest
```

- [ ] **Step 2: Validate**

```bash
make validate
```

- [ ] **Step 3: Deploy svc-media**

```bash
make media USE_VAULT_FILE=1
```

- [ ] **Step 4: Verify**

```bash
make verify USE_VAULT_FILE=1
```

Then load the Homepage dashboard in a browser and confirm every service tile still renders and links resolve (Homepage reads a config file this repo templates — a bump that silently changed the config schema would show as a blank or broken dashboard, not a failed health check).

- [ ] **Step 5: Confirm the CVE count moved**

```bash
make scan USE_VAULT_FILE=1
```

- [ ] **Step 6: Commit**

```bash
git add inventory/group_vars/all/apps.yml
git commit -m "$(cat <<'EOF'
chore: bump homepage to the current latest digest

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 1.5: Close out Phase 1

- [ ] **Step 1: Confirm the tree is clean**

```bash
git status --porcelain
```

Expected: empty.

- [ ] **Step 2: Final deploy-proof**

```bash
make deploy-proof TARGET=infra USE_VAULT_FILE=1
make media USE_VAULT_FILE=1   # svc-media has no deploy-proof wrapper; confirm changed=0 by eye
make dl USE_VAULT_FILE=1      # same
make verify USE_VAULT_FILE=1
```

All three must show `changed=0` (or the svc-infra sync-trio caveat on the first of the two `deploy-proof` runs — run it twice if so, per CLAUDE.md).

- [ ] **Step 3: Merge, push, delete the branch**

```bash
git switch main
git merge --ff-only chore/bump-tracked-images
git push origin main
git branch -d chore/bump-tracked-images
```

---

## Phase 2 — Shared postgres:18-alpine (hand-bump, two consumers)

**Files:**
- Modify: `inventory/group_vars/all/apps.yml` (both `netbox_images.postgres` and `nextcloud_images.postgres` — same digest, hand-edited together per the BUMP PROCEDURE's explicit rule that `image-bump` refuses a digest pinned twice)

**Interfaces:**
- Consumes: `make image-digest REF=docker.io/library/postgres:18-alpine` (resolves the new digest without editing anything)
- Produces: the new shared digest, applied identically to both `netbox_images.postgres` and `nextcloud_images.postgres`, each keeping its own `# tag: 18-alpine` / `# was <date>: sha256:<old>` comment pair

### Task 2.1: Branch and resolve the new digest

- [ ] **Step 1: Branch**

```bash
git status --porcelain   # empty
git switch -c chore/bump-shared-postgres
```

- [ ] **Step 2: Resolve (do not edit yet)**

```bash
make image-digest REF=docker.io/library/postgres:18-alpine
```

Record the digest it prints — this is going into two places by hand.

- [ ] **Step 3: Read what each consumer persists**

NetBox and Nextcloud each run their own postgres instance (separate data volumes, not a shared database) — the pin being identical is coincidence-of-choice, not a shared cluster. Still: postgres 18.x point releases are backward-compatible on-disk (no major-version migration), so this is a normal minor bump, not a `pg_upgrade`-class event. Confirm that's still true by checking the digest's version label:

```bash
scripts/image-release.sh docker.io/library/postgres:18-alpine
```

Expected: still an `18.x` point release, not a jump to `19`. **If it reports a major-version jump, stop this task and re-scope — a major postgres bump is not a mechanical edit.**

### Task 2.2: Apply the new digest to both pins

- [ ] **Step 1: Edit `netbox_images.postgres`**

In `inventory/group_vars/all/apps.yml`, replace the `postgres:` line under `netbox_images` (around line 289):

```yaml
  # tag: 18-alpine
  # was <today's date>: sha256:<the digest scripts/image-digest.sh printed for the OLD pin>
  postgres: "docker.io/library/postgres@sha256:<the NEW digest from Task 2.1>"
```

- [ ] **Step 2: Edit `nextcloud_images.postgres`** (around line 324) — the identical change, same new digest, same `# was` comment naming the same old digest.

- [ ] **Step 3: Validate**

```bash
make validate
```

Expected: `tests/validate_image_provenance.py` passes once committed (it diffs against git history, so this step is really confirming syntax now and the provenance check happens for real at commit+push).

- [ ] **Step 4: Deploy svc-infra** (both NetBox and Nextcloud live there)

```bash
make infra USE_VAULT_FILE=1
```

Expected: both postgres containers show `changed=1` (recreated on the new digest), NetBox and Nextcloud webserver containers unaffected.

- [ ] **Step 5: Verify the applications, not just the containers**

```bash
make verify USE_VAULT_FILE=1
```

Then, by hand: log into NetBox and confirm existing IPAM data is still there (list a device or prefix that existed before the bump); log into Nextcloud and confirm existing files/shares still list correctly. This is the step that would catch a wedged migration — a container that starts fine and can't reach its data still passes `active` + `200 from Caddy`.

- [ ] **Step 6: Confirm the CVE count moved**

```bash
make scan USE_VAULT_FILE=1
```

Check both `docker.io/library/postgres` digest rows in the scan report (there are two apps using it — the report labels by `image`+`digest`, so after the bump both should show the new 7-character digest prefix).

- [ ] **Step 7: Commit**

```bash
git add inventory/group_vars/all/apps.yml
git commit -m "$(cat <<'EOF'
chore: bump the shared postgres:18-alpine pin (NetBox + Nextcloud)

Hand-edited rather than make image-bump: this digest is pinned in two
places (netbox_images.postgres and nextcloud_images.postgres) on
purpose, and image-bump refuses to touch a shared pin because bumping
one is a decision about every consumer at once. Confirmed still an
18.x point release (no major-version migration) before applying.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 2.3: Close out Phase 2

- [ ] **Step 1:** `git status --porcelain` — empty.
- [ ] **Step 2:** `make deploy-proof TARGET=infra USE_VAULT_FILE=1` (twice if the sync-trio fires) and `make verify USE_VAULT_FILE=1` — both clean.
- [ ] **Step 3:** Merge to `main`, push, delete the branch (same commands as Task 1.5 Step 3, branch name `chore/bump-shared-postgres`).

---

## Phase 3 — Tooling pins (no live-service risk)

**Files:**
- Modify: `requirements.txt` (ansible, ansible-core — the file's own header says "update these together after a live deployment")
- Modify: `requirements-dev.txt` (ansible-lint — ruff also drifted, bump alongside it since both are the offline-only lint toolchain)

**Do not touch `requirements.yml`.** `community.proxmox` showing `2.0.0 -> 2.0.0-beta1` in the release report is a Galaxy API artifact (the "latest" the API returns is a pre-release, older in real terms than the pinned stable `2.0.0`), not real drift — bumping it would be a downgrade to a beta. `ansible.posix`, `proxmoxer`, `requests`, `yamllint` were all current as of 2026-09-04 and need no action.

### Task 3.1: Branch and bump the runtime pins

- [ ] **Step 1: Branch**

```bash
git status --porcelain   # empty
git switch -c chore/bump-tooling-pins
```

- [ ] **Step 2: Edit `requirements.txt`**

```diff
-ansible==14.2.0
-ansible-core==2.21.2
+ansible==14.3.1
+ansible-core==2.21.3
```

(Confirm these are still the current versions with `scripts/release-check.sh` or a direct PyPI check before pinning — the audit numbers are from 2026-09-04 and may have moved.)

- [ ] **Step 3: Rebuild the venv against the new pins**

```bash
rm -rf .venv
make deps
```

- [ ] **Step 4: Validate offline**

```bash
source .venv/bin/activate
make validate
```

Expected: exit 0. This is the real test here — `ansible-lint --profile min` and every `ansible-playbook --syntax-check` in `make validate` exercising the new `ansible-core` against every playbook in the repo is a far more thorough check than any unit test would be.

- [ ] **Step 5: Live-deployment smoke test (per the file's own "update together after a live deployment" instruction)**

```bash
make preflight USE_VAULT_FILE=1
make check USE_VAULT_FILE=1
```

`make check` is check-mode (no changes applied) — confirms the new ansible-core can actually connect to and gather facts from every host without touching anything. Expected: clean run, no unexpected diffs.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt
git commit -m "$(cat <<'EOF'
chore: bump ansible and ansible-core (update together, per file header)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 3.2: Bump the lint toolchain

- [ ] **Step 1: Edit `requirements-dev.txt`**

```diff
-ansible-lint==26.6.0
+ansible-lint==26.8.0
-ruff==0.16.5
+ruff==0.16.6
```

(Same caveat: confirm current versions before pinning, numbers may have moved since 2026-09-04.)

- [ ] **Step 2: Rebuild dev deps**

```bash
make deps-dev
```

- [ ] **Step 3: Validate**

```bash
source .venv/bin/activate
make validate
```

Expected: exit 0. A version bump to the linter itself is the one case where a *new* failure here is plausible and legitimate (a newer ansible-lint/ruff can add rules) — read any new failure before assuming it's a false positive.

- [ ] **Step 4: Commit**

```bash
git add requirements-dev.txt
git commit -m "$(cat <<'EOF'
chore: bump ansible-lint and ruff (offline lint toolchain)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 3.3: Close out Phase 3

- [ ] **Step 1:** `git status --porcelain` — empty.
- [ ] **Step 2:** No VM deploy needed — this phase touches only the control-node toolchain. `make validate` passing IS the verification.
- [ ] **Step 3:** Merge to `main`, push, delete the branch (`chore/bump-tooling-pins`).

---

## Phase 4 — EOL-base-OS images (security-priority, untracked)

Both flagged in the 2026-09-04 scan report as sitting on a base OS that has reached end-of-life — their CVE counts can only grow from here regardless of anything upstream does, which is why they're prioritized ahead of the much larger untracked backlog in Phase 5.

Neither carries a `# tag:` (deliberately untracked), so these are hand-bumps: research the current safe tag, resolve its digest, verify it isn't a data-migrating major jump, apply, deploy, verify.

### Task 4.1: Branch

```bash
git status --porcelain   # empty
git switch -c chore/bump-eol-base-images
```

### Task 4.2: Bump it-tools

**Files:** Modify `inventory/group_vars/all/apps.yml` (the `it_tools` or equivalent pin — grep for `corentinth/it-tools` to find the exact key).

it-tools is stateless (a static collection of browser-side utilities, no database, no persisted config) — lowest-risk of the two EOL images.

- [ ] **Step 1: Find the current pin and research the replacement**

```bash
grep -n "corentinth/it-tools" inventory/group_vars/all/apps.yml
scripts/image-release.sh ghcr.io/corentinth/it-tools:latest
```

Read what version/build the `latest` tag now resolves to and skim its release notes for anything unexpected (there shouldn't be much — it's a UI toolbox).

- [ ] **Step 2: Resolve and record the new digest**

```bash
make image-digest REF=ghcr.io/corentinth/it-tools:latest
```

- [ ] **Step 3: Apply the pin by hand**

```yaml
  # was <today's date>: sha256:<old digest>
  it_tools: "ghcr.io/corentinth/it-tools@sha256:<new digest>"
```

Consider recording `# tag: latest` here too, now that it's freshly confirmed — it-tools has no data to migrate, so it fits the same category as jdownloader/homepage/beszel-agent in the BUMP PROCEDURE's tracked list. If you do, `make image-check` will start reporting it going forward.

- [ ] **Step 4: Validate**

```bash
make validate
```

- [ ] **Step 5: Deploy**

```bash
make media USE_VAULT_FILE=1   # or make infra, whichever host it-tools runs on — confirm with grep first
```

- [ ] **Step 6: Verify**

```bash
make verify USE_VAULT_FILE=1
```

Then open it-tools in a browser and confirm the page loads and at least one tool (e.g. a UUID generator) works.

- [ ] **Step 7: Confirm the EOL flag and CVE count both cleared**

```bash
make scan USE_VAULT_FILE=1
```

Confirm `ghcr.io/corentinth/it-tools` no longer appears under "BASE OS PAST END-OF-LIFE" in the report.

- [ ] **Step 8: Commit**

```bash
git add inventory/group_vars/all/apps.yml
git commit -m "$(cat <<'EOF'
chore: bump it-tools off its end-of-life base image

Stateless UI toolbox, no data to migrate — verified the CVE/EOL flag
cleared after the bump.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

### Task 4.3: Bump uptime-kuma

**Files:** Modify `inventory/group_vars/all/apps.yml` (grep for `louislam/uptime-kuma`).

Uptime Kuma **does** persist state (monitor definitions, history, notification config) in a SQLite database inside its data volume — treat this with the same care as Phase 2's postgres bump, not like the stateless it-tools above.

- [ ] **Step 1: Find the current pin and research the replacement**

```bash
grep -n "louislam/uptime-kuma" inventory/group_vars/all/apps.yml
scripts/image-release.sh docker.io/louislam/uptime-kuma:latest
```

Read the release notes between the currently-pinned build (2025-10-20 per the 2026-09-04 scan) and the current `latest`. Specifically check for any noted database schema migration or breaking config change — Uptime Kuma has historically done clean in-place SQLite migrations on startup, but confirm for the specific version gap found here rather than assuming.

- [ ] **Step 2: Back up its data volume before bumping** (extra caution for a stateful EOL image with a large version gap)

```bash
# on the host running uptime-kuma — confirm which with grep from Step 1
sudo podman volume export <uptime-kuma-data-volume> --output /tmp/uptime-kuma-backup-$(date +%F).tar
```

Copy that tarball somewhere off-host before proceeding (e.g. via `scp` to the control node).

- [ ] **Step 3: Resolve the new digest**

```bash
make image-digest REF=docker.io/louislam/uptime-kuma:latest
```

- [ ] **Step 4: Apply the pin by hand**

```yaml
  # was <today's date>: sha256:<old digest>
  uptime_kuma: "docker.io/louislam/uptime-kuma@sha256:<new digest>"
```

- [ ] **Step 5: Validate**

```bash
make validate
```

- [ ] **Step 6: Deploy**

```bash
make media USE_VAULT_FILE=1   # or make infra — confirm with grep
```

Watch the deploy output closely — the pre-pull means a bad digest aborts before restart, but a schema migration failure would show up as the container restarting into a crash loop *after* the pre-pull succeeded.

- [ ] **Step 7: Verify the application, thoroughly**

```bash
make verify USE_VAULT_FILE=1
```

Then, by hand: log into Uptime Kuma and confirm every existing monitor is still listed with its history intact, and that at least one notification channel test-fires successfully. This is the step that catches a migration that silently dropped history — a running container with an empty monitor list would still pass every automated check.

- [ ] **Step 8: Confirm the EOL flag and CVE count both cleared**

```bash
make scan USE_VAULT_FILE=1
```

- [ ] **Step 9: Commit**

```bash
git add inventory/group_vars/all/apps.yml
git commit -m "$(cat <<'EOF'
chore: bump uptime-kuma off its end-of-life base image

Stateful (SQLite monitor history) — backed up the data volume before
bumping and confirmed every existing monitor and its history survived
the migration before calling this done.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 10: Delete the backup tarball** once Step 7's verification is confirmed good, per the normal rule against keeping stray copies of application data lying around.

### Task 4.4: Close out Phase 4

- [ ] **Step 1:** `git status --porcelain` — empty.
- [ ] **Step 2:** `make deploy-proof TARGET=infra USE_VAULT_FILE=1` (if either image lives on svc-infra; twice if the sync trio fires) plus `make verify USE_VAULT_FILE=1` — clean.
- [ ] **Step 3:** Merge to `main`, push, delete the branch (`chore/bump-eol-base-images`).

---

## Phase 5 — The untracked backlog (research-and-decide, one at a time)

This is the largest group and deliberately **not** pre-scripted per-image: every one of these is untracked *on purpose* (a major bump could migrate a database one way — see the BUMP PROCEDURE block), so the release notes have to actually be read before deciding what to bump to. Task 5.1 is a fully worked template; the table after it is the backlog to run that template against, one image per branch, prioritized by CVE severity from the 2026-09-04 scan.

**Special couplings to know before starting any of these** (from `inventory/group_vars/all/apps.yml`):

- **Immich's three images move together**: `immich_server`, `immich_machine_learning`, and Immich's *own* `postgres` pin (a vectorchord-patched build, NOT the shared `postgres:18-alpine` from Phase 2) are all locked to the same upstream release (currently v3.0.3) because server/DB schema migrations are version-coupled. Bump all three in one task, to one release, or not at all.
- **The three-way valkey pin** (`docker.io/valkey/valkey@sha256:c9b779...`, currently reported "unknown-version" since the image carries no label) is shared by paperless-ngx, NetBox, and Nextcloud. Immich has its *own separate* valkey pin — do not conflate the two when bumping.
- **Beszel hub and beszel-agent are the same upstream repo/release** — if bumping the hub here in Phase 5, note the agent was already moved to the newest `latest` in Phase 1, so confirm the hub version chosen is compatible with whatever agent version Phase 1 landed (Beszel's hub/agent protocol has historically been backward compatible across minor versions, but confirm in the release notes rather than assuming).

### Task 5.1: Template — research, decide, bump, verify (worked example: paperless-ngx)

Paperless-ngx is the worst single CVE offender in the 2026-09-04 scan (46 critical / 519 high) and is `NEW SINCE THE LAST REPORT` (2.20.15 -> v3.1.3, a **major** version jump) in the release-check — the highest-value and highest-risk item in this phase, which is exactly why it makes the best template: every later item in this phase is a strict subset of the care this one needs.

**Files:** Modify `inventory/group_vars/all/apps.yml` (`paperless_images.webserver` — grep to confirm the exact key).

- [ ] **Step 1: Branch**

```bash
git status --porcelain   # empty
git switch -c chore/bump-paperless-ngx
```

- [ ] **Step 2: Read the release notes for the full gap, not just the latest entry**

```bash
scripts/image-release.sh docker.io/paperlessngx/paperless-ngx
```

Then read the actual GitHub release notes (the URL the release-check report prints, `https://github.com/paperless-ngx/paperless-ngx/releases/tag/v3.1.3`) **and every release between the pinned 2.20.15 and v3.1.3** — a 2.x -> 3.x jump crossing multiple majors needs the migration notes from each major boundary, not just the newest tag's changelog. Paperless-ngx documents its breaking changes per major version in its own docs; check for required manual migration steps (there have historically been settings-file and Docker Compose variable renames across its majors).

- [ ] **Step 3: Decide**

Write the decision down in the commit message before touching code (Step 8 below) — either "bumping straight to v3.1.3, no manual migration steps required per the v3.0.0 release notes" or "bumping to an intermediate version first because X requires it." This step has no command to run; it is the judgment the BUMP PROCEDURE exists to force a human through, and it is why this phase cannot be scripted ahead of time.

- [ ] **Step 4: Back up its data before bumping** (database-backed app, major version jump)

```bash
# on svc-infra
sudo podman volume export <paperless-data-volume> --output /tmp/paperless-backup-$(date +%F).tar
```

Copy off-host before proceeding.

- [ ] **Step 5: Resolve the new digest**

```bash
make image-digest REF=docker.io/paperlessngx/paperless-ngx:<the tag decided on in Step 3>
```

- [ ] **Step 6: Apply the pin by hand**

```yaml
  # was <today's date>: sha256:<old digest>
  webserver: "docker.io/paperlessngx/paperless-ngx@sha256:<new digest>"
```

(Leave it untracked — no `# tag:` — unless Step 3's research concluded a specific version-line tag is safe to follow long-term; a major-migrating app like this is exactly the case the BUMP PROCEDURE says to leave untracked.)

- [ ] **Step 7: Validate**

```bash
make validate
```

- [ ] **Step 8: Commit the decision and the pin together**

```bash
git add inventory/group_vars/all/apps.yml
git commit -m "$(cat <<'EOF'
chore: bump paperless-ngx to v3.1.3

<Paste the Step 3 decision here — what was read, what migration was
or wasn't required, and why this target version was chosen.>

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 9: Deploy svc-infra**

```bash
make infra USE_VAULT_FILE=1
```

Watch closely for a migration running in the startup logs.

- [ ] **Step 10: Verify the application**

```bash
make verify USE_VAULT_FILE=1
```

Then, by hand: log into Paperless-ngx and confirm existing documents are still listed, searchable, and that at least one document's OCR text and tags survived. Check the container's own startup logs for a completed-migration message rather than just "container is up."

- [ ] **Step 11: Confirm the CVE count moved**

```bash
make scan USE_VAULT_FILE=1
```

- [ ] **Step 12: Delete the backup tarball** once satisfied, and close out the branch (same shape as Task 1.5 Step 3, branch `chore/bump-paperless-ngx`).

### Task 5.2+: The rest of the backlog

Repeat Task 5.1's template — research (reading the *actual* release notes, not just the version-check script's one-line summary), decide, back up if stateful, resolve, apply by hand, validate, deploy one VM, verify the application specifically, scan, commit, close the branch — for each row below, roughly in this priority order. One branch per image (small ones may reasonably share a branch with 2-3 adjacent low-risk rows; use judgment, but never mix a stateful and stateless bump in one commit).

| Priority | Image | Service | 2026-09-04 finding | Stateful? | Note |
|---|---|---|---|---|---|
| 1 | `ghcr.io/calibrain/shelfmark` | svc-download | 45 crit / 851 high, `1.3.4 -> v1.3.15` | Yes (library metadata) | Second-worst CVE count |
| 2 | `docker.io/library/nextcloud` | svc-infra | 32 crit / 498 high, unmeasured (no version label) | Yes (files, DB via Phase 2's postgres) | Postgres already current after Phase 2 — this bumps the webserver image only |
| 3 | `ghcr.io/open-webui/open-webui` | svc-infra | 26 crit / 622 high, unmeasured (`main` tag, not a real version) | Yes (chat history, settings) | Check `docs/plans/openwebui-settings-as-code.md` before bumping — this repo pushes config into Open WebUI separately |
| 4 | `ghcr.io/immich-app/immich-server` + `immich-machine-learning` + Immich's own `postgres` | svc-infra | 25 crit / 198 high (server); ML not currently deployed | Yes | **Bump all three together** — see coupling note above |
| 5 | `ghcr.io/maziggy/bambuddy` | — | 14 crit / 341 high, unmeasured | Check | Confirm what this persists before bumping |
| 6 | `docker.io/semaphoreui/semaphore` | — | 11 crit / 224 high, `v2.18.28 -> v2.19.12` | Check | |
| 7 | `ghcr.io/mealie-recipes/mealie` | — | 11 crit / 293 high, `v3.21.0 -> v3.25.1` | Yes (recipes) | |
| 8 | `docker.io/binwiederhier/ntfy` | svc-media | unmeasured (no version label) | Config only | This estate's own alert-delivery path — verify alerting still works post-bump using the `curl .../homelab-alerts/json?poll=1&since=10m` check from `docs/operations.md`, not just that the container is up |
| 9 | `ghcr.io/authelia/authelia` | svc-infra | `4.39.20 -> v4.39.22` | Yes (SQLite + config schema) | This estate's SSO — verify login still works for a real account before calling it done, not just that the container starts |
| 10 | `docker.io/henrygd/beszel` (hub) | svc-infra | `0.18.7 -> v0.19.0` | Yes (monitor config) | Confirm compatible with the agent version Phase 1 landed |
| 11 | The remaining `NEW SINCE THE LAST REPORT` / `STILL BEHIND` rows: recyclarr, sabnzbd, netbox webserver, prowlarr, bazarr, sonarr, radarr, code-server, syncthing, vaultwarden, seerr, audiobookshelf, glances, prometheus, trivy | various | see `releases.txt` for each image's exact ref and current-vs-latest | Mostly low | Same template, lower individual risk per row — batch 2-3 clearly stateless ones (glances, trivy, prometheus: config-only, no persisted app data) per branch if desired, but keep any database- or history-backed row (sonarr, radarr, vaultwarden, syncthing) on its own branch |
| — | `docker.io/valkey/valkey` (both pins) | paperless-ngx/netbox/nextcloud + Immich | unmeasured, two separate digests | Yes (broker persistence) | Do **not** bump opportunistically — re-read the "two purposes" comment in `apps.yml` first; this is a shared-pin decision like Phase 2's postgres, not a Phase 5 one-off |

For the exact current-vs-latest numbers on any row, re-run `make release-report USE_VAULT_FILE=1` (writes the live report to svc-infra) or `scripts/image-release.sh <ref>` for one image at a time — the table above is a snapshot from 2026-09-04 and will have moved.

### Task 5.N: Close out each row

Same three steps as every phase above: `git status --porcelain` empty, `make verify` (and `make deploy-proof TARGET=infra` if svc-infra was touched) clean, merge/push/delete the branch. Do this **per branch**, not once at the end of the whole backlog — the point of small branches here is that a bad bump three rows in doesn't block or get tangled with the two before it.

---

## Explicitly out of scope (name them so nobody assumes they were forgotten)

- **PVE** (`thurgadin`) — 8.2.4 vs upstream 9.2, a full major version. User excluded this explicitly; needs its own plan (Debian 12->13, kernel 6.8->7.0 are not a `make image-bump`-shaped change).
- **Rocky OS security errata** on the three VMs (104 pending as of 2026-09-04) — governed by the deliberate no-auto-update policy in `docs/unattended.md`, requires a reboot plan, and was not asked for here.
- **The 19 `COULD NOT CHECK` (unmeasured) images** not listed in Phase 5's table above (calibre-web-automated, it-tools — done in Phase 4, jdownloader-2 — done in Phase 1, lazylibrarian, mariadb, minecraft-server, romm, searxng, tinyproxy, uptime-kuma — done in Phase 4, webtop) — these have no version label at all, so "behind" can't even be established without opening each one's release notes cold. Worth a future pass, not folded into this backlog because there's no CVE-count or release-check signal driving priority for them yet.
