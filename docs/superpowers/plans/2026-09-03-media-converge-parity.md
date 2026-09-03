# svc-media Converge Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give svc-media the two convergence properties svc-download and svc-infra already have — a stale-Quadlet sweep, and a user-manager reload that can heal a half-finished deploy — so that all three service VMs converge the same way.

**Architecture:** Port the sweep from `roles/svc_infra/tasks/files.yml:17-93` and the unconditional reload from `roles/svc_infra/tasks/apps.yml:5-24`, keeping their comments' reasoning intact. The sweep discovers files by an `Ansible managed` marker and deletes only marked files its expected list no longer names, so it must be preceded by adding that marker to the three svc-media Quadlets that lack it — otherwise the sweep ships blind to them and is an incomplete check that looks complete.

**Tech Stack:** Ansible, Podman Quadlet, rootless `systemctl --user -M homelab@`.

**Spec:** The architecture review of 2026-09-03 (this repo, conversation record). Findings #2 and #3.

## Global Constraints

- **This branch requires a deploy.** `make media`, then `make verify`, then a second `make media` reporting `changed=0`. It cannot be merged on `make validate` alone.
- **Deploy during a quiet window.** Task 1 changes rendered Quadlet content for jellyfin, homepage and romm, which restarts those services. A Jellyfin restart interrupts playback.
- The sweep's `contains: 'Ansible managed'` guard is **load-bearing, not decoration**. `roles/svc_infra/files/quadlets/paperless.network` says so in its own header and explains why. Never drop the guard to catch more files — that is how a sweep deletes something placed by hand.
- Every "expected" list must be a **single bare `{{ }}` expression**, never `{% set %}` or `{% for %}`. `roles/svc_download/tasks/files.yml` documents the gotcha: otherwise `difference()` operates on a string repr of a list rather than a list, and the sweep silently matches nothing.
- `beszel-agent` is force-included in the expected list **even when disabled**, so clearing its token never makes a previously rendered agent look stale. svc-infra does this; match it.

---

## Current state (measured 2026-09-03)

| Property | svc-download | svc-infra | svc-media |
|---|---|---|---|
| Stale-Quadlet sweep | yes | yes | **no** |
| Unconditional user `daemon-reload` | n/a (rootful) | yes | **no** |
| All container Quadlets carry `ansible_managed` | yes | yes (14/14) | **no (9/11)** |
| `.network` file carries the marker | n/a | yes (4/4) | **no (0/1)** |

Missing the marker on svc-media: `homepage.container.j2`, `jellyfin.container.j2`, `files/quadlets/romm.network`.

Two pieces of evidence that this gap is real rather than theoretical:

1. `roles/svc_media/tasks/migrations.yml` is 44 lines of hand-written Jellyseerr teardown — stop the unit, remove the Quadlet, remove the container. That is exactly what a sweep does generically. Every future service removal needs another one of these.
2. `roles/svc_infra/files/quadlets/paperless.network` describes romm.network as "the same pattern on svc-media" — the pattern was copied, the sweep that makes it safe was not.

The reload gap is documented at `roles/svc_infra/tasks/apps.yml:5-24` with a dated incident: a deploy rendered a new Quadlet, firewalld reloaded, the deploy's own SSH connection dropped before handlers flushed, and svc-infra stayed wedged across three further converges because the file looked unchanged and nothing regenerated the unit. svc-media's reload is handler-only and has the identical exposure.

---

## File Structure

- Modify: `roles/svc_media/templates/jellyfin.container.j2` — add the managed marker
- Modify: `roles/svc_media/templates/homepage.container.j2` — add the managed marker
- Modify: `roles/svc_media/files/quadlets/romm.network` — add the literal marker and its explanation
- Modify: `roles/svc_media/tasks/files.yml` — the sweep, inserted after the directory creation and before the Quadlet renders
- Modify: `roles/svc_media/tasks/apps.yml` — the unconditional reload
- Modify: `roles/svc_media/tasks/migrations.yml` — delete the Jellyseerr teardown the sweep now subsumes

---

### Task 1: Mark the three unmarked Quadlets

**Files:**
- Modify: `roles/svc_media/templates/jellyfin.container.j2`, `roles/svc_media/templates/homepage.container.j2`, `roles/svc_media/files/quadlets/romm.network`

**Interfaces:**
- Consumes: nothing.
- Produces: every svc-media Quadlet carries the string `Ansible managed`, which Task 2's `find` depends on.

- [ ] **Step 1: Confirm exactly which files lack the marker**

```bash
for f in roles/svc_media/templates/*.container.j2 roles/svc_media/files/quadlets/*.network; do
  printf "%-62s %s\n" "$(basename "$f")" "$(grep -c 'ansible_managed\|Ansible managed' "$f")"
done
```

Expected: `0` for `homepage.container.j2`, `jellyfin.container.j2`, `romm.network`; `1` for the other nine. If the set differs, use what you measure — do not trust this plan over the tree.

- [ ] **Step 2: Add the marker to the two templates**

In each of `jellyfin.container.j2` and `homepage.container.j2`, insert as the **second line**, matching the placement in `audiobookshelf.container.j2`:

```jinja
{{ ansible_managed | comment }}
```

Check `audiobookshelf.container.j2`'s first three lines and match that shape exactly — the surrounding comment convention differs slightly between templates and consistency here is what makes the `find` predictable.

- [ ] **Step 3: Add the literal marker to romm.network**

`romm.network` is `copy:`d verbatim, not templated, so it cannot inherit `{{ ansible_managed }}`. Prepend the same header its svc-infra counterparts carry, adapted:

```
# Ansible managed
#
# That marker is load-bearing, not decoration. The stale-Quadlet sweep in
# roles/svc_media/tasks/files.yml discovers *.network files by it and deletes
# the ones its expected list no longer names; a file without it is invisible
# to the sweep, so retiring this network would leave the unit behind on the
# host. Templated Quadlets inherit the string from {{ ansible_managed }} —
# this one is copy:'d verbatim, so it carries the marker literally.
#
```

immediately above the existing `# ~homelab/.config/containers/systemd/romm.network` line.

- [ ] **Step 4: Validate**

Run: `make validate`

Expected: PASS. `validate_generated_catalog.py` renders Quadlets; a malformed Jinja comment filter shows up here.

- [ ] **Step 5: Commit**

```bash
git add roles/svc_media/templates/jellyfin.container.j2 \
        roles/svc_media/templates/homepage.container.j2 \
        roles/svc_media/files/quadlets/romm.network
git commit -m "fix: mark the three svc-media Quadlets that carried no marker

All 14 of svc-infra's carry 'Ansible managed'; svc-media had 9 of 11 plus
an unmarked romm.network. The stale sweep added in the next commit
discovers files by that marker, so an unmarked file is invisible to it —
which would ship a sweep that looks complete and silently is not.

romm.network is copy:'d rather than templated, so it carries the marker
literally, with the same header its svc-infra counterparts use."
```

---

### Task 2: Port the stale-Quadlet sweep

**Files:**
- Modify: `roles/svc_media/tasks/files.yml` — insert after "Create rootless Quadlet and application directories" (ends line 33), before "Check whether the RomM database directory exists"

**Interfaces:**
- Consumes: the markers from Task 1; `media_quadlet_catalog` (role defaults), `minecraft_servers` (group_vars), `beszel_agent_enabled` (group_vars).
- Produces: `media_expected_container_paths`, `media_expected_network_paths`, `media_stale_container_paths`, `media_stale_network_paths` — none consumed outside this file.

- [ ] **Step 1: Enumerate what is actually on the host, before writing anything that deletes**

This is a destructive change and the first run is the dangerous one. On svc-media:

```bash
sudo -u homelab ls -la /opt/homelab/.config/containers/systemd/
sudo -u homelab grep -L 'Ansible managed' /opt/homelab/.config/containers/systemd/*
```

Write down the full list. The second command names the files the sweep will *not* touch; anything surprising there is worth understanding now rather than after a deploy.

- [ ] **Step 2: Compute the expected set by hand and compare**

The expected containers are `media_quadlet_catalog` names + `beszel-agent` + `minecraft_servers` keys:

```bash
.venv/bin/python - <<'PY'
import yaml, pathlib
d = yaml.safe_load(pathlib.Path("roles/svc_media/defaults/main.yml").read_text())
m = yaml.safe_load(pathlib.Path("inventory/group_vars/all/minecraft.yml").read_text())
names = [e["name"] for e in d["media_quadlet_catalog"]] + ["beszel-agent"] + list(m["minecraft_servers"])
print("\n".join(sorted(names)))
PY
```

Compare against Step 1's listing. **Every `.container` file on the host that is marked and not in this list will be deleted on the next deploy.** If that set is not empty, decide about each one deliberately before proceeding — that is the sweep doing its job, but the first time it does it, a human should agree.

- [ ] **Step 3: Write the sweep**

Insert into `roles/svc_media/tasks/files.yml` after the directory-creation task:

```yaml
# Stale-unit reconciliation, mirroring roles/svc_infra/tasks/files.yml and
# roles/svc_download/tasks/files.yml. svc-media was the one service VM without
# it, and roles/svc_media/tasks/migrations.yml is what that cost: 44 lines of
# hand-written Jellyseerr teardown doing by name what this does generically.
#
# Every expected list below is a single bare `{{ }}` expression (never
# {% set %}/{% for %}), so difference() operates on a real list rather than a
# string repr of one — the gotcha documented in svc_download/tasks/files.yml.
- name: Calculate the rootless media unit files that should exist
  ansible.builtin.set_fact:
    # beszel-agent is force-included even when disabled so a previously
    # rendered agent is never treated as stale by a token/key being cleared —
    # its own `when: beszel_agent_enabled` render + WARN already handle that.
    media_expected_container_paths: >-
      {{ ((media_quadlet_catalog | map(attribute='name') | list)
          + (minecraft_servers.keys() | list))
         | union(['beszel-agent'])
         | map('regex_replace', '^(.*)$',
                '/opt/homelab/.config/containers/systemd/\1.container') | list }}
    media_expected_network_paths: >-
      {{ ['romm']
         | map('regex_replace', '^(.*)$',
                '/opt/homelab/.config/containers/systemd/\1.network') | list }}
  changed_when: false

- name: Discover Ansible-managed rootless media container Quadlets
  ansible.builtin.find:
    paths: /opt/homelab/.config/containers/systemd
    patterns: "*.container"
    file_type: file
    contains: 'Ansible managed'
  register: media_managed_containers
  check_mode: false

# The `contains:` filter matters more here than on the container find above,
# because this list feeds `state: absent` while that one only feeds a `stop`.
# Without it the sweep would delete every .network file it did not expect,
# including one placed by hand — a destructive action against files it had
# never established it owned. romm.network is `copy:`d verbatim rather than
# templated, so it carries the marker as a literal comment line; deleting that
# line makes this find skip it and the next converge delete the deployed copy.
- name: Discover Ansible-managed rootless media network Quadlets
  ansible.builtin.find:
    paths: /opt/homelab/.config/containers/systemd
    patterns: "*.network"
    file_type: file
    contains: 'Ansible managed'
  register: media_managed_networks
  check_mode: false

- name: Calculate stale rootless media unit paths
  ansible.builtin.set_fact:
    media_stale_container_paths: >-
      {{ media_managed_containers.files | map(attribute='path') | list
         | difference(media_expected_container_paths) }}
    media_stale_network_paths: >-
      {{ media_managed_networks.files | map(attribute='path') | list
         | difference(media_expected_network_paths) }}
  changed_when: false

# Tolerating a failure here is correct, not lazy. A stale .container file whose
# generated unit systemd never loaded — the file was added and the daemon never
# reloaded, or the unit was already stopped and reset — makes `systemctl stop`
# exit non-zero, which without this would abort the deploy inside the very
# block whose job is to reconcile units that should not exist. Nothing
# downstream depends on the stop succeeding: the `state: absent` task below
# removes the file, and the reload is what actually retires the unit.
- name: Stop stale rootless media services
  ansible.builtin.command: >-
    systemctl --user -M homelab@ stop
    {{ item | basename | regex_replace('\.container$', '.service') }}
  loop: "{{ media_stale_container_paths }}"
  changed_when: true
  failed_when: false

- name: Remove stale rootless media unit files
  ansible.builtin.file:
    path: "{{ item }}"
    state: absent
  loop: "{{ media_stale_container_paths + media_stale_network_paths }}"
  notify: media configuration changed
```

- [ ] **Step 4: Validate offline**

Run: `make validate`

Expected: PASS. `validate-ansible` with the newly enabled `jinja` rule (if the python-lint-gate branch has merged) will reject a malformed expression here.

- [ ] **Step 5: Check-mode the deploy before running it for real**

Run: `make check ARGS="--limit media_vms --tags files"`

Expected: no errors. The two `find` tasks carry `check_mode: false` so they execute and report honestly; read the output and confirm the stale lists are what Step 2 predicted. **If check mode names a file for removal that Step 2 did not predict, stop and investigate.**

- [ ] **Step 6: Commit before deploying**

```bash
git add roles/svc_media/tasks/files.yml
git commit -m "fix: sweep stale Quadlets on svc-media

svc-download and svc-infra both reconcile units that should no longer
exist; svc-media only rendered, so removing a catalog entry left the
Quadlet and its running container behind. migrations.yml's 44 lines of
Jellyseerr teardown is the receipt.

Marked-file discovery only, so a hand-placed unit is never deleted, and
the expected lists are bare {{ }} expressions so difference() sees lists
rather than string reprs."
```

- [ ] **Step 7: Deploy and verify**

Run: `make media` then `make verify`

Expected: the deploy succeeds; verify passes. If the sweep removed anything, the `media configuration changed` handler reloads the user manager and restarts the affected services.

**This step requires the operator — deploys are not available to an agent in this environment.**

---

### Task 3: Add the unconditional user-manager reload

**Files:**
- Modify: `roles/svc_media/tasks/apps.yml` — insert after the `flush_handlers` meta task, before "Read rootless media service state"

**Interfaces:**
- Consumes: nothing from Tasks 1–2.
- Produces: nothing consumed later.

- [ ] **Step 1: Add the task**

```yaml
# Unconditional, and deliberately not a handler. The `media configuration
# changed` handler only reloads when a Quadlet render reports `changed`, which
# makes the converge unable to heal itself: if a deploy renders a NEW .container
# file and then dies before handlers flush — an unreachable host, an interrupted
# run — the next converge sees the file as unchanged, never reloads, and the
# generated unit therefore never comes into existence. Every subsequent run then
# fails on "Unit <name>.service not found" while the Quadlet sits on disk
# looking correct.
#
# Observed exactly that way on svc-infra on 2026-07-30 (see
# roles/svc_infra/tasks/apps.yml): adding a catalog entry reloaded firewalld,
# which dropped the deploy's own SSH connection mid-play, and the host stayed
# wedged across three further converges until the reload was run by hand.
# svc-media's exposure is identical; it simply had not happened here yet.
#
# A reload costs ~40ms and is idempotent, so paying it every run is far cheaper
# than a state only a human can notice and clear.
- name: Reload the rootless user manager so new Quadlets are generated
  ansible.builtin.command: systemctl --user -M homelab@ daemon-reload
  changed_when: false
```

`changed_when: false` is essential: a task reporting `changed` on every run would make `changed=0` — the repo's proof that deployed state matches the commit — impossible on this host.

- [ ] **Step 2: Validate**

Run: `make validate`

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add roles/svc_media/tasks/apps.yml
git commit -m "fix: reload svc-media's user manager unconditionally

svc-infra pays a 40ms idempotent reload every converge so a deploy that
dies before handlers flush can heal itself on the next run. svc-media's
reload was handler-only and had the identical exposure — a rendered
Quadlet whose unit is never generated, failing every subsequent run on
'Unit not found' while the file on disk looks correct.

changed_when: false, so changed=0 still means what it means."
```

- [ ] **Step 4: Deploy twice and require changed=0 on the second**

Run: `make media`, then `make media` again.

Expected: the second run reports `changed=0` for svc-media. If the reload task reports `changed`, `changed_when: false` is missing.

**Operator step.**

---

### Task 4: Delete the Jellyseerr migration the sweep subsumes

**Files:**
- Modify: `roles/svc_media/tasks/migrations.yml`

**Interfaces:**
- Consumes: the working sweep from Task 2, deployed and verified.
- Produces: nothing.

**Do not start this task until Task 2 has been deployed and verified on the live host.** The sweep must be proven to work before the hand-written teardown it replaces is removed.

- [ ] **Step 1: Confirm the legacy unit is genuinely gone from the host**

```bash
sudo -u homelab ls /opt/homelab/.config/containers/systemd/ | grep -i jellyseerr
sudo -u homelab podman container exists jellyseerr; echo "exit: $?"
```

Expected: no Quadlet file, and `exit: 1` from `container exists`. If either still shows Jellyseerr, the migration is still doing work — leave it in place and stop this task.

- [ ] **Step 2: Remove the Quadlet and unit halves, keep the container half**

The sweep handles the Quadlet file and the systemd unit. It does **not** remove a leftover *container* — `podman rm` is outside its scope. So delete the first four tasks in `migrations.yml` (the `is-active` check, the `stop`, the `stat` and the `state: absent` file removal) and keep the last two (`podman container exists` and `podman rm -f`).

Add a comment above what remains:

```yaml
# What stays here and what does not. The stale-Quadlet sweep in files.yml now
# stops the unit and removes the .container file for anything the catalog no
# longer names, so those four tasks are gone. It does not remove a leftover
# CONTAINER — that is outside a unit-file sweep's scope — so the two tasks
# below remain until svc-media has been converged everywhere Jellyseerr ever
# ran. Delete them once `podman container exists jellyseerr` has reported
# absent on a fresh converge.
```

- [ ] **Step 3: Validate**

Run: `make validate`

Expected: PASS.

- [ ] **Step 4: Deploy and verify**

Run: `make media` then `make verify`

Expected: both clean. **Operator step.**

- [ ] **Step 5: Commit**

```bash
git add roles/svc_media/tasks/migrations.yml
git commit -m "refactor: drop the Jellyseerr teardown the sweep now covers

Four of the six tasks did by name what files.yml's stale sweep now does
generically. The two podman tasks stay: removing a leftover container is
outside a unit-file sweep's scope."
```

---

## Verification before merge

This branch changes role code on a live host, so `make validate` is not sufficient evidence.

- [ ] `make validate` passes.
- [ ] `git status --porcelain` prints nothing.
- [ ] `make media` deployed from the clean tree, then `make media` again reporting `changed=0` for svc-media. (Use `make deploy-proof TARGET=media` if the deploy-proof branch has merged.)
- [ ] `make verify` passes on all three VMs — not just svc-media. The sweep touches the user manager, and svc-media's verify smoke-tests every Caddy backend including svc-infra's.
- [ ] Every service in `media_quadlet_catalog` plus the Minecraft servers is `active`: `sudo -u homelab systemctl --user list-units 'homelab*' --state=failed` returns nothing.
- [ ] Jellyfin actually serves — open it, not just `is-active`. Task 1 restarted it, and "the container is up" is not the property this repo verifies.
- [ ] Sanity-check the sweep's control once, deliberately: `sudo -u homelab touch /opt/homelab/.config/containers/systemd/zz-probe.container`, write `# Ansible managed` into it, run `make media`, confirm it is removed and reported. Then confirm an *unmarked* probe file survives the same run. Both halves matter — the first proves it sweeps, the second proves it does not sweep what it does not own.

## Merge

Standard workflow. Note that this branch's evidence includes the two-deploy `changed=0` proof and the live verify — do not merge on validation alone.
