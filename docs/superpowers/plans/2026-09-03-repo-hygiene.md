# Repo Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the completed plan document sitting in the repo root, make the 14 accumulated plan documents distinguishable from live design work, and write down the vault's blast radius as a decision rather than leaving it as an unexamined default.

**Architecture:** Three unrelated small changes that share a branch because each is a few minutes and none justifies its own deploy cycle. None touches role code.

**Tech Stack:** Markdown, git.

**Spec:** The architecture review of 2026-09-03 (this repo, conversation record). Finding #11 and the "Smaller things" list.

## Global Constraints

- **Never `git add -A`.** `CLAUDE.md` is explicit: the repo root holds working notes that quote live credentials. Stage explicit paths in every commit here.
- `make validate-links` must pass after every file move. The link checker walks the docs, and moving a file that something references breaks it.
- Do not delete `origin/feat/mac-control-node`. See the note below — it is not what the review assumed.

---

## Correction to the review: the branch is not stale

The architecture review listed `origin/feat/mac-control-node` as leftover cruft that step 8 of the workflow exists to prevent. That was wrong, and checking cost one command:

```
$ git log --oneline main..origin/feat/mac-control-node
f814b94 feat: add a tri-state Ollama binding check
3db8655 fix: make the mac_control self-deploy guard actually able to fire
9e1e544 docs: fix the self-deploy guard so it can actually fire
00b535b feat: add the mac_control role skeleton with an idempotence gate
8c02b94 fix: keep the deploy lock held when the wrapper is killed mid-deploy
```

Five commits that are not on `main`: a `mac_control` role skeleton, a deploy lock that survives the wrapper being killed, and an idempotence gate. That is in-flight feature work, not a branch somebody forgot to delete.

**This plan does not touch it.** Whether to finish it, or abandon it deliberately, is a decision for the repo owner — and it is exactly the decision step 8 exists to surface, which it has now done. If it is abandoned, delete it in its own commit with a message saying why, so the reasoning survives the branch.

---

## File Structure

- Move: `PLAN-fortwow-dev-letsencrypt.md` → `docs/superpowers/plans/2026-07-27-fortwow-dev-letsencrypt.md` (date from its first commit)
- Create: `docs/superpowers/plans/README.md` — a status index
- Modify: `docs/security.md` — a new subsection under "Secret handling"

---

### Task 1: Retire the root plan document

**Files:**
- Move: `PLAN-fortwow-dev-letsencrypt.md` → `docs/superpowers/plans/<date>-fortwow-dev-letsencrypt.md`

**Interfaces:**
- Consumes: nothing.
- Produces: a path Task 2's index references.

- [ ] **Step 1: Confirm the work described is actually done**

The document plans the `fort.wow` → `fortwow.dev` migration with a Let's Encrypt wildcard via certbot DNS-01. Confirm it shipped:

```bash
grep -n "service_domain" inventory/group_vars/all/main.yml | head -3
ls roles/svc_media/templates/certbot-deploy-caddy.sh.j2 roles/svc_media/templates/certwatch.sh.j2
```

Expected: `service_domain` is `fortwow.dev`, and both certbot templates exist. That means this is a completed plan, and the move below is correct. If the work is *not* done, stop — it is live design work and belongs in `docs/plans/`, not the completed-plans directory.

- [ ] **Step 2: Find the date it was written**

```bash
git log --diff-filter=A --format=%ad --date=short -- PLAN-fortwow-dev-letsencrypt.md
```

Use that date in the filename, matching the `YYYY-MM-DD-<topic>.md` convention every other file in `docs/superpowers/plans/` follows.

- [ ] **Step 3: Move it with git, so history follows**

```bash
git mv PLAN-fortwow-dev-letsencrypt.md \
       docs/superpowers/plans/2026-07-27-fortwow-dev-letsencrypt.md
```

Use `git mv`, not `mv` plus `git add` — the rename is then recorded as a rename and `git log --follow` still reaches the original.

- [ ] **Step 4: Find and fix anything that referenced it**

```bash
grep -rn "PLAN-fortwow-dev-letsencrypt" --exclude-dir=.git .
```

Expected: no hits, or hits only in docs that should now point at the new path. Fix any that exist.

- [ ] **Step 5: Verify the link gate**

Run: `make validate-links`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add PLAN-fortwow-dev-letsencrypt.md docs/superpowers/plans/
git commit -m "docs: move the completed letsencrypt plan out of the repo root

The fortwow.dev migration shipped — service_domain is fortwow.dev and both
certbot templates are deployed — so this is a finished plan, and finished
plans live in docs/superpowers/plans/ with a dated filename.

It predates that directory, which is why it was at the root. git mv so
--follow still reaches the original."
```

---

### Task 2: Give the plans directory a status index

**Files:**
- Create: `docs/superpowers/plans/README.md`

**Interfaces:**
- Consumes: the path from Task 1.
- Produces: nothing consumed later.

- [ ] **Step 1: Establish each plan's status**

There are 14 plan documents plus a `notes` directory. For each, determine whether the work shipped, partly shipped, or was abandoned. The fastest reliable check is whether the thing it describes exists:

```bash
ls docs/superpowers/plans/
git log --oneline --diff-filter=A --format='%ad %f' --date=short -- docs/superpowers/plans/
```

Then for each plan, grep the tree for the artifact it names — a role, a script, a catalog key. Do not guess from the title; two of these are handoff documents whose work continued elsewhere.

- [ ] **Step 2: Write the index**

Create `docs/superpowers/plans/README.md`:

```markdown
# Implementation plans

Historical implementation plans, one per feature, named `YYYY-MM-DD-<topic>.md`.

**These are records, not instructions.** Every plan here describes work that was
already decided; most of it shipped. A plan is written before the work and is
not updated afterwards, so where a plan and the current tree disagree, the tree
is right. Read a plan for *why* something is shaped the way it is, never as a
description of how the estate works today — `docs/architecture.md` and the role
comments are for that.

Live design work in progress lives in `docs/plans/` instead.

| Plan | Status |
|---|---|
| `2026-07-27-fortwow-dev-letsencrypt.md` | Shipped |
| ... | ... |

## `notes/`

Working notes kept alongside the plans. Same caveat: point-in-time records.
```

Fill the table from Step 1. Use exactly three status values — `Shipped`, `Partly shipped`, `Abandoned` — and for anything not `Shipped`, add a half-sentence saying what remains or why it stopped. A status column that is uniformly "Shipped" tells a reader nothing they could not assume; the value here is entirely in the exceptions.

- [ ] **Step 3: Verify the links resolve**

Run: `make validate-links`

Expected: PASS. If the gate does not check relative links inside `docs/superpowers/`, verify the filenames by hand against `ls` — a table of 14 filenames is exactly where a typo hides.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/README.md
git commit -m "docs: index the plans directory with per-plan status

Fourteen plan documents, ~13,000 lines, with nothing distinguishing a
record of shipped work from a live design. They read identically, and the
directory is adjacent to docs/plans/, which IS live design.

The status column earns its place only through the entries that are not
'Shipped'."
```

---

### Task 3: Write down the vault's blast radius

**Files:**
- Modify: `docs/security.md` — new subsection at the end of "Secret handling" (currently lines 23–46)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Read what is already there**

Run: `sed -n '23,47p' docs/security.md`

The new subsection must not restate what "Secret handling" already covers. Read it first and write only what is missing: the *scope* of a compromise, and the fact that the current arrangement is a choice.

- [ ] **Step 2: Add the subsection**

Append to the end of the "Secret handling" section, before "## Credential exposure response":

```markdown
### Blast radius, and why it is shaped this way

One `vault.yml` holds every secret in the estate, under one password. There is
no per-host vault and no per-secret key. So a compromise of the vault password
is a compromise of everything: Proxmox API token, Mullvad configuration,
Authelia hashes, every database password, every service admin account.

Two copies of that password exist. One is `.vault_pass` on the workstation. The
other is `/opt/homelab-iac/.vault_pass` on svc-infra, which the nightly
verification runner needs in order to decrypt the inventory. **svc-infra can
therefore decrypt every secret in the estate**, which makes it the highest-value
host here by some distance — a fact worth knowing before deciding what else to
run on it.

This is a decision, not an oversight, and the reasoning is:

- **Per-host vaults would not help much.** svc-infra's runner verifies all three
  VMs, so it would need all three vaults. The split would move the boundary
  without shrinking what the interesting host can read.
- **SOPS or age with per-secret recipients would help**, and costs a key
  management story, a second tool in the validation chain, and a rebuild path
  that no longer works from a bare clone plus one passphrase. For three VMs and
  one operator that is a worse trade.
- **The one mitigation that is in place** is that Ansible never installs the
  runner's copy for you. `roles/svc_infra/tasks/verify-runner.yml` renders every
  other part of the runner and deliberately not that file; it is placed by hand,
  once, per `docs/operations.md`. Verification fails loudly when it is missing,
  so the gap cannot go unnoticed — but nothing about the deploy path ever writes
  a vault password to a VM.

Revisit this if a second operator appears, if a host outside the LAN ever needs
to decrypt anything, or if svc-infra starts running something with a wider
attack surface than the current catalog.
```

- [ ] **Step 3: Verify against the code before committing**

Every claim above is checkable. Confirm each:

```bash
grep -n "vault password is NOT installed" roles/svc_infra/tasks/verify-runner.yml
grep -rn "verify_runner_secret_file" roles/svc_infra/tasks/verify-runner.yml | head
ls inventory/group_vars/all/
```

Expected: the runner's comment confirms the password is not installed by Ansible; `verify_runner_secret_file` is referenced but never written by a `copy`/`template` task; there is exactly one vault file. If any of these turns out otherwise, fix the prose — a security document that describes the wrong arrangement is worse than none.

- [ ] **Step 4: Verify the docs gates**

Run: `make validate-links && make validate-secrets`

Expected: both PASS. `validate-secrets` matters here — the new text names vault variables in prose, and the secret scanner reads docs.

- [ ] **Step 5: Commit**

```bash
git add docs/security.md
git commit -m "docs: state the vault's blast radius as a decision

One vault, one password, two copies — and svc-infra holds the second,
so it can decrypt every secret in the estate. That was true before this
commit; it just was not written anywhere, which made it read as an
unexamined default rather than a trade that was considered.

Records why per-host vaults would not shrink it, what SOPS would cost,
and the one mitigation actually in place: nothing in the deploy path ever
writes a vault password to a VM."
```

---

## Verification before merge

No role code, no deploy, no `make verify`.

- [ ] `make validate` passes.
- [ ] `git status --porcelain` prints nothing.
- [ ] `ls PLAN-*.md` finds nothing in the repo root.
- [ ] Every claim in the new `docs/security.md` subsection was checked against the tree in Task 3 Step 3, not assumed.

## Merge

Standard workflow: confirm clean tree, merge to `main`, push, delete the branch.

**Not part of this branch:** the `origin/feat/mac-control-node` decision. Raise it separately.
