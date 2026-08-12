# mac-control — an always-on second control node

**Date:** 2026-08-12
**Status:** design agreed, not yet implemented

## What this is

A MacBook Pro (M1 Pro, 16 GB unified memory, 256–512 GB SSD, **broken
backlight** — the panel drives real pixels but cannot be read) joins the
estate as `mac-control`: a headless, always-on second Ansible control node
and agentic host, reachable over Tailscale, wired to the LAN through a
USB-C 1GbE adapter.

[site.yml](../../../site.yml) already says "Run from the MBP" — the control
node for this estate is a Mac. This adds a second one that never closes its
lid.

## Why, honestly

The original framing was "big battery, so run critical containers on it."
That premise deflated on inspection: the network gear **and** thurgadin are
already on a UPS, so the battery only covers outages longer than the UPS
runtime.

What survives the deflation is stronger. `mac-control` would be the first
machine in the estate outside thurgadin's failure domain — separate power,
separate kernel, separate storage, separate hypervisor — other than TERRA,
which is a gaming PC that is switched off half the time. The battery is one
facet of that independence, not the headline.

The concrete value is the agentic host. CLAUDE.md states the same anxiety in
three places: a red CI run on `main` means something already merged is
broken "and if it stays red, that is the same 'nobody looks at it' failure
this repo worries about everywhere else." The weekly release report
publishes 30 images' release notes for a human to read. The nightly scan and
verify do the same. Every one of those is a machine producing a report that
depends on a person remembering to read it. An always-on agentic host is the
reader.

Secondary: closing the working laptop's lid currently ends any session.
`mac-control` gives sessions a home that survives it.

## Decisions taken

| Decision | Choice | Where it came from |
|---|---|---|
| Operating system | macOS on the metal | Metal inference, Screen Sharing, and identical control-node toolchain to the existing MBP. Asahi Linux was considered and rejected: it would give native podman but CPU-only inference and no macOS desktop. |
| Management model | Full Ansible role (`mac_control`) | Chosen over a documented-only runbook (the TERRA precedent) and over a bootstrap-script-plus-verification hybrid. |
| Credentials on disk | Both `.vault_pass` and an SSH key | Full control node. Accepted risk, recorded below. |
| FileVault | Off, with auto-login | Unattended reboots always come back unaided. |
| Reachability | Tailnet for admin, LAN-only for Ollama, **and** re-scope TERRA | Avoids repeating an existing exposure on the machine holding the vault, and closes the existing one. |
| Scope of this spec | Foundation + control node + remote GUI + Ollama | The estate watchdog is deferred to a second spec. |

## Non-goals

- **Container hosting on `mac-control`.** macOS means a Linux VM (Podman
  machine/Colima) costing 4+ GB, to which none of this repo's roles apply.
  The battery premise that motivated it does not survive thurgadin being on
  a UPS. Not in this spec, and not recommended later without a new reason.
- **A general estate watchdog.** `nfsguard` currently runs on thurgadin,
  which is also the host running convoker (TrueNAS, VM 100) — a watcher
  inside the failure domain it watches. `mac-control` is the right place to
  fix that, but it is a second subsystem with its own positive-control
  requirements and gets its own spec.
- **Continue's configuration.** It lives on whatever machine runs the
  editor, not in this repo. Repointing it is a documented manual step.
- **Replacing the existing control node.** The working laptop stays
  hand-managed and remains the only machine that merges to `main`.

---

## 1. Identity, network, and repo placement

**Inventory.** A new top-level group `control_nodes` alongside `pve`,
`pve_mon_hosts` and `service_vms`, holding one host, `mac-control`. It is
deliberately not under `service_vms`, which carries guest-specific vars and
NFS assumptions that do not apply.

[preflight.yml](../../../preflight.yml)'s address-uniqueness assert covers
`service_vms` only, so it is **extended to include `control_nodes`**.
Otherwise the one gate that catches an IP collision silently skips the new
machine.

**Address:** `192.168.1.41`, by pfSense DHCP reservation.

**Two interfaces, deliberately.** [docs/gpu-host.md](../../gpu-host.md)
records an afternoon lost to TERRA holding a reservation on Wi-Fi while
preferring Ethernet outbound. The same trap applies here and matters more,
because Ollama's bind address is a security control rather than a
convenience.

- The **USB-C 1GbE adapter's MAC** gets `192.168.1.41` and carries the
  service address. macOS service order puts Ethernet first.
- **Wi-Fi stays enabled** with its own separate reservation, as a management
  fallback.

Wi-Fi stays on because a headless box in a closet whose only link is a USB-C
dongle is one bad connector away from being unreachable, with no screen to
explain why. Ollama binds the Ethernet address specifically, so a dropped
dongle stops Ollama and turns the verify check red while SSH survives over
Wi-Fi. The failure reports itself instead of going dark.

**Make target.** `make mac` → `--limit control_nodes` against `site.yml`,
matching `dl` / `media` / `infra` / `pve`. The play sits at the end of
`site.yml`; nothing depends on it.

**A control node may not deploy to itself.** The play's first task asserts
the target's `ansible_host` is not an address of the machine running
Ansible. Reconfiguring Ollama, Tailscale and `pmset` underneath your own SSH
session loses the session mid-play and leaves the box half-configured with
no screen to inspect. `mac-control` is configured *from* the working laptop.
The bootstrap circularity is broken by direction, not by cleverness.

---

## 2. What the role manages, and what it refuses to

### Manages

- Homebrew and packages: git, tmux, node, python, shellcheck, gitleaks.
- The repo checkout and its virtualenv (`requirements.txt`,
  `requirements-dev.txt`).
- `pmset`: `sleep 0`, `disablesleep 1`, `autorestart 1`, `womp 1` — a closed
  lid on mains power never sleeps, and power loss brings it back.
- Remote Login; Screen Sharing; auto-login; hostname via `scutil`.
- Tailscale, with the auth key from the vault.
- Ollama, with its bind set in the launchd plist (see §4).
- The Claude Code CLI and the superpowers plugin (see §3).

This adds **`community.general` to `requirements.yml`, pinned**, for the
`homebrew` module. That file's comment ("Update these together after a live
deployment") makes collection changes a deliberate act; this is one. Using
`command` instead would mean hand-rolling idempotence for every package,
which is exactly what makes `changed=0` unreliable.

### Refuses to manage

**`vault.yml` and `.vault_pass` are placed by hand, out of band.** The role
asserts they exist at mode 0600 and fails loudly otherwise. It never writes
them, never reads their contents, never renders them into a task argument.

The vault password cannot live in the vault. Any mechanism threading it
through a play is a mechanism that can put it in a log, against CLAUDE.md's
standing rule. This is the same honesty CLAUDE.md already applies: the
commit pins the code, not the secrets.

**`mac-control` generates its own ed25519 keypair locally.** The private key
never crosses the network and exists nowhere else, and it can be revoked
independently without touching the working laptop's access.

That requires closing a latent gap. `admin_ssh_pubkey` is currently a
**scalar**, referenced in exactly one place —
[roles/pve_vm/templates/user-data.yaml.j2](../../../roles/pve_vm/templates/user-data.yaml.j2)
line 18, cloud-init, at *provision* time. Nothing manages `authorized_keys`
on a running VM, so today adding or rotating a key means re-provisioning the
VM, and revoking one is impossible without doing so again.

So:

- `admin_ssh_pubkey` becomes `admin_ssh_pubkeys`, a list.
- `service_vm` gains an `authorized_keys` task, managing keys on running VMs.
- `pve_vm`'s cloud-init template renders from the same list.

This is deliberate scope creep, accepted because the alternative design
makes key rotation require rebuilding three VMs.

### The idempotence discipline

Most macOS state is not file-shaped, so most tasks are `command`. Every
`command` task pairs with a read task and derives `changed_when` from the
comparison:

- `pmset -g custom` parsed into a fact; each setting applied only if it
  differs.
- `systemsetup -getremotelogin` before `-setremotelogin`.
- `ollama list` before `ollama pull`.

No bare `changed_when: false` — that reports success without proving
anything, which is the failure mode CLAUDE.md catalogues for gates that
cannot fail. Nothing in the role writes a file that changes every run.

**Acceptance criterion:** `make mac` run twice reports `changed=0` on the
second run. If it does not, the role is not finished.

---

## 3. The agentic host layer

**Install.** Claude Code CLI plus the superpowers plugin, installed
non-interactively. Where a non-interactive install path exists the role uses
it; where one does not, the role **asserts presence and fails with
instructions**. A half-installed agent host that looks configured is worse
than one that refuses to come up.

**Sessions.** tmux, with a launchd agent creating a named `homelab` session
at login, rooted in the checkout. SSH in from anywhere on the tailnet and
`tmux attach`.

**Scheduled work: the mechanism, plus exactly one job.** The mechanism is
launchd timers invoking `claude -p` headless. The one job is the **weekly
release-report reader** — after `make release-check` publishes Friday 08:30,
it reads the comparable images' release notes and drafts a recommendation
document.

It is read-only and produces a document rather than a change. It
**deliberately prints no bump command**: CLAUDE.md is explicit that a
standing bump recommendation is the thing `BUMP PROCEDURE` exists to
prevent, and that constraint binds an agent harder than a script.

One job rather than five, because a scheduled agent that stops running is
invisible — no error, no red build, it simply goes quiet, exactly like the
port scan that found one open port where eleven were open. **The job pings a
healthchecks.io check on every completion**, joining the four already in
use. A job that stops firing raises an alarm. No second job is added until
the first has proven it can be trusted.

Unattended agent runs cost tokens. Weekly, matched to the report it reads,
is deliberate.

**Deploy serialization.** Two control nodes pointed at one estate is a more
likely way to break things than anything in the credential discussion, and a
documented rule will not hold — this repo's history is a catalogue of
procedural rules that did not.

`site.yml` acquires an **advisory lock on thurgadin** in `pre_tasks`,
created with `O_EXCL` semantics, recording holder hostname, PID and
timestamp; released in an `always` block. A second deploy fails immediately
with a message naming who holds it and since when.

The lock lives on thurgadin rather than a guest because thurgadin hosts
every VM — it is up whenever deploying is meaningful — and this keeps the
lock outside the machines being deployed to. Stale locks after a crashed
play are handled by the timestamp in the file plus a `make deploy-unlock`
target, documented rather than improvised at 2am.

**`mac-control` never pushes `main`.** CLAUDE.md's "push after committing
without waiting to be asked" was written for a human-driven session; on an
always-on box it means an agent pushing to `main` while you sleep. Amended
for this machine: `mac-control` commits and pushes *branches* freely; merges
to `main` happen from the working laptop. Enforced by a `pre-push` hook in
`mac-control`'s checkout that rejects `main` outright.

That hook stops carelessness, not determination — `--no-verify` walks past
it. It is a guardrail, not a boundary; the boundary case was settled by the
full-control-node decision.

---

## 4. Ollama, Open WebUI, and re-scoping TERRA

### On mac-control

Ollama installed with its bind set in the **launchd plist**, not as a global
environment variable: `OLLAMA_HOST=192.168.1.41:11434`. The macOS equivalent
of TERRA's system variable, except declarative, in the repo, and treated as
a security control.

Two models, **relocated** from TERRA (not duplicated):
`qwen2.5-coder:1.5b-base` and `nomic-embed-text` — the pair
[docs/gpu-host.md](../../gpu-host.md) itself labels "small, always-on,"
currently on a machine that gets gamed on and switched off. Relocation
rather than duplication is what makes the verification in this section
possible.

**Ollama is explicitly bounded** so it cannot starve the machine's primary
job. 16 GB splits roughly: macOS 3–4 GB, agent sessions 1–1.5 GB each, the
two models ~2 GB resident — comfortable. It stops being comfortable when a
7–8B chat model with a 32k KV cache wants 6–7 GB, so
`OLLAMA_MAX_LOADED_MODELS` and a short `OLLAMA_KEEP_ALIVE` are set
deliberately. Adding a chat model later is then a config change made with
the memory budget visible, not a discovery made when the control node starts
swapping.

### Open WebUI, and the trap in front of it

[inventory/group_vars/all/infra-apps.yml](../../../inventory/group_vars/all/infra-apps.yml)
lines 564–578 are explicit: with `ENABLE_PERSISTENT_CONFIG: "true"`, a DB
row beats the environment, so "editing a value here and running `make infra`
silently does nothing... no error, and `changed=0` still reports success."

`OLLAMA_BASE_URLS` is such a key and has a row. The obvious version of this
work — add the URL to the catalog, deploy, observe `changed=0` — produces a
green deploy, a satisfied verify, and **no second endpoint**.

So:

- The catalog gains `mac-control`'s endpoint in `OLLAMA_BASE_URLS` and a
  `mac_host_online` flag mirroring `gpu_host_online`, with
  `ENABLE_OLLAMA_API` gated on *either* host being up.
  `ENABLE_IMAGE_GENERATION` stays tied to `gpu_host_online` alone, since
  ComfyUI is TERRA-only.
- That env change is **a first-boot seed, not enforced state**, exactly as
  the catalog comment instructs.
- The endpoint is actually registered by reading
  `GET /api/v1/configs/export` — the endpoint that comment already
  recommends for auditing `enable_signup` — and adding it through the API if
  the row disagrees. The exact key name and separator are confirmed against
  that export during implementation rather than assumed.

**The deploy does not get to claim success on this.** Verification asserts
Open WebUI can list a model only `mac-control` serves. A container that is
up and a `changed=0` deploy prove the process started; this proves the
service functions.

### TERRA

`OLLAMA_HOST` moves from `0.0.0.0:11434` to `192.168.1.40:11434`, and
ComfyUI gains `--listen 192.168.1.40`, so neither answers on `tailscale0`.
Today both answer unauthenticated to every tailnet peer.

TERRA stays unmanaged, so this is a documented hand-edit, and
[docs/gpu-host.md](../../gpu-host.md)'s current instruction to set `0.0.0.0`
is corrected in the same change so the next rebuild does not reintroduce it.

**The check is tri-state and must be.** "TERRA's tailnet address does not
answer on 11434" passes trivially when TERRA is off, which is half the time.
The check asserts the LAN address **does** answer first; only then is a
silent tailnet address meaningful. TERRA unreachable reports
`inconclusive`, never `ok` — the same three-state verdict commit `057e1e4`
gave the credential probes, for the same reason.

---

## 5. Verification, failure handling, and offline gates

### Where the checks run from

The nightly `make verify` runs from svc-infra's git archive. That runner has
no key to `mac-control` and should not get one — the point of the machine is
that it sits above the estate, not inside it. So verification splits:

- The `control_nodes` play runs on **workstation-invoked `make verify`**.
- `mac-control` **self-checks nightly** via its own launchd timer, pinging
  healthchecks.io. That dead-man's switch catches the real risk: an
  unattended box going quiet with nobody noticing.

The nightly self-check covers **only** the checks in the table below — its
own state, and the two Ollama binding assertions. It watches nothing else
about the estate. Extending it into a general watchdog is the deferred
follow-up, not a quiet expansion of this one.

`verify.yml` runs both with and without a vault — a vault-dependent assert
broke the nightly runner for a night (fixed in `b77f27f`). So every
`mac-control` assertion reads rendered state on the host, never a vault
variable.

### The checks

Each has something that must be true if it ran.

| Check | Positive control |
|---|---|
| Ollama binding on `mac-control` | `192.168.1.41:11434` **does** answer; only then assert the tailnet address does not. LAN-not-answering is a failure, not a pass. |
| TERRA binding | Same pair, tri-state. TERRA off → `inconclusive`. |
| Open WebUI second endpoint | A model only `mac-control` serves appears in the model list. Not a config read; the config is the thing already known to lie. |
| Secrets hygiene | `vault.yml` and `.vault_pass` exist at mode 0600 with correct owner. **Mode only** — contents never read, rendered, or logged. |
| Push hook | `git push --dry-run` at `main` must fail. A hook that exists and a hook that works are different claims. |
| Power settings | `pmset -g custom` parsed and compared against intent. |
| Deploy lock | Reported with holder and age rather than silently blocking the next deploy. |

### Failure handling

- Missing prerequisites (Xcode Command Line Tools, Remote Login, the two
  secret files) fail the play loudly with instructions rather than
  proceeding into a half-built state.
- A control node targeting itself aborts before touching anything.
- A held deploy lock fails fast, naming holder and age.
- A dropped dongle stops Ollama, turns verify red, and leaves SSH alive over
  Wi-Fi.

### Offline gates

Two new validators in the existing `tests/validate_*.py` idiom:

- **Idempotence gate** — no task in `mac_control` may use `command`/`shell`
  without a `changed_when`. The macOS `changed=0` promise enforced
  statically instead of trusted.
- **launchd plist validator** — using `plistlib`, so it parses on Linux CI
  too. Same reasoning as `validate_systemd_units.py` switching to real
  parsing when `systemd-analyze` is present: the alternative is a malformed
  plist that only fails on the host.

Shellcheck covers new shell; the fixture-inventory syntax check picks up the
new play automatically.

### Backing out

`mac_host_online: false` drops the Open WebUI endpoint; removing the
`control_nodes` group removes the rest. Nothing in the estate depends on
`mac-control` existing — deliberately, since a control node the estate
needed in order to function would defeat the independence that justified it.

---

## Risks accepted

**The vault password and an SSH key sit on an always-on, remotely reachable
machine driven by an agent.** The threat model that matters is not the agent
going rogue; it is prompt injection. This estate's agent routinely reads
untrusted text — GitHub release notes, CVE scan output, web pages — and an
agent holding `.vault_pass` executes attacker-influenced text with the keys
to everything. Blast radius is every service credential, the GitHub token,
Semaphore's encryption key, and the PVE API token (scoped to VM rights, not
root, so hypervisor damage is bounded but VM create/destroy is not).

This was raised and the decision was made deliberately. Mitigations built
into the design rather than argued about further: tailnet-only admin
reachability with no LAN-wide or WAN exposure, `.vault_pass` at mode 0600,
ntfy notification on every deploy originating from `mac-control` so an
unexpected one is visible, the thurgadin deploy lock, and the `main`-push
hook.

**Withholding the vault would not have withheld the secrets anyway.** They
are rendered onto the hosts — quadlet units and env files on svc-infra
contain the actual values — and `ansible_user: straderb` with
`ansible_become: true` means any SSH access to those VMs is root access.
There is no read-only tier without building an unprivileged monitoring
account. This is noted so the decision is understood as bounded rather than
unlimited: the alternative was less protective than it sounds.

**FileVault off with auto-login** means physical access to the house is
access to a logged-in session holding those credentials. The SSD remains
hardware-encrypted at rest by the Secure Enclave; what is given up is the
requirement for a human password at boot. Bought in exchange for unattended
reboots that always come back — on a machine with no readable screen, where
FileVault's pre-boot prompt is unreachable by Screen Sharing and recoverable
only by attaching an external display.

**A bootstrap prerequisite remains manual.** macOS must be installed, an
account created, Remote Login enabled, Xcode Command Line Tools present, and
the two secret files placed, before Ansible can reach the machine at all.
This is the analog of cloud-init for the VMs, and it is documented rather
than automated.

## Files touched

**New:** `roles/mac_control/` (tasks, templates, defaults);
`docs/mac-control-node.md`; `tests/validate_mac_idempotence.py`;
`tests/validate_launchd_plists.py`.

**Modified:** `inventory/hosts.yml` (`control_nodes` group);
`inventory/group_vars/all/main.yml` (`mac_host_online`,
`admin_ssh_pubkeys`); `inventory/group_vars/all/infra-apps.yml` (Open WebUI
`OLLAMA_BASE_URLS`, gating); `site.yml` (control_nodes play, deploy lock
pre_tasks); `verify.yml` (control_nodes checks); `preflight.yml`
(address-uniqueness covers `control_nodes`); `roles/service_vm`
(`authorized_keys` task); `roles/pve_vm/templates/user-data.yaml.j2`
(pubkey list); `requirements.yml` (`community.general`, pinned); `Makefile`
(`make mac`, `make deploy-unlock`); `README.md`; `CLAUDE.md` (two control
nodes, deploy lock, `main`-push rule); `docs/gpu-host.md` (bind correction).

## Acceptance criteria

1. `make validate` passes, including the two new validators.
2. `make mac` twice in a row reports `changed=0` on the second run.
3. `make verify` passes every check in §5 from the workstation.
4. Open WebUI lists a model served only by `mac-control`.
5. TERRA's Ollama and ComfyUI do not answer on `tailscale0`, asserted with
   the LAN positive control passing in the same run.
6. `git push` to `main` from `mac-control` is rejected.
7. A second concurrent deploy fails on the thurgadin lock, naming the holder.
8. The machine survives an unplanned power cut and returns to a reachable,
   logged-in state with tmux, Ollama and Tailscale running, unaided.

## Follow-ups, each its own spec

- **Estate watchdog on `mac-control`** — moving the storage-outage guard out
  of thurgadin's failure domain.
- **A second scheduled agent job** — only after the release-report reader's
  healthchecks record shows it fires reliably.
- **An unprivileged monitoring account on the VMs** — would create the
  read-only tier that does not exist today, and would let a future design
  withhold root SSH from an always-on box.
