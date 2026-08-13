# mac-control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an M1 Pro MacBook Pro to the estate as `mac-control` — a headless, always-on second Ansible control node and agentic host, managed by a new `mac_control` role.

**Architecture:** A new `control_nodes` inventory group with one macOS host, configured over SSH *from* the working laptop (never from itself). A `mac_control` role manages Homebrew packages, power settings, remote access, Tailscale, Ollama, the Claude Code CLI and launchd units. Two new offline validators police the things macOS makes easy to get wrong: `command` tasks without `changed_when`, and malformed launchd plists. Verification splits — workstation-invoked `make verify` runs the `control_nodes` play, and the machine self-checks nightly against a healthchecks.io dead-man's switch.

**Tech Stack:** Ansible (`community.general` for `homebrew`, `ansible.posix` for `authorized_key`), macOS launchd, Ollama, Tailscale, Python 3 for validators, Bash for scripts.

**Spec:** [docs/superpowers/specs/2026-08-12-mac-control-node-design.md](../specs/2026-08-12-mac-control-node-design.md)

## Global Constraints

- **Branch:** `feat/mac-control-node` (already exists, holds the spec). Never `git add -A` — stage explicit paths.
- **Host:** `mac-control` at `192.168.1.41` (USB-C 1GbE adapter, pfSense DHCP reservation). Wi-Fi stays enabled with its own separate reservation.
- **`ansible_python_interpreter: /usr/bin/python3`** — the Xcode Command Line Tools python. Never Homebrew's, because Homebrew is installed *by* the role.
- **A control node may not deploy to itself.** The play's first task aborts if the target address belongs to the machine running Ansible.
- **Every `command`/`shell` task in `roles/mac_control` must set `changed_when`.** Enforced statically by `tests/validate_mac_idempotence.py`. Bare `changed_when: false` is permitted only on read-only probe tasks that a subsequent task compares against.
- **`make mac` twice in a row must report `changed=0` on the second run.**
- **Never read, render, or log the contents of `vault.yml` or `.vault_pass`.** The role asserts their existence and mode only. Secret-bearing tasks use `no_log: true`.
- **Every check distinguishes "none found" from "could not look."** Binding checks are tri-state (`ok` / `exposed` / `inconclusive`) and assert a positive control before drawing a negative conclusion.
- **Ollama binds `192.168.1.41:11434`** — the LAN address specifically, never `0.0.0.0`.
- **Run `make validate` before every commit.** CI on `main` runs after the merge and does not gate.

---

### Task 1: Inventory skeleton, preflight coverage, `make mac`

Adds the `control_nodes` group and makes the one gate that catches IP collisions actually look at it. Today [preflight.yml](../../../preflight.yml) collects addresses from `groups['service_vms']` only, so a new group is invisible to it.

**Files:**
- Modify: `inventory/hosts.yml`
- Create: `inventory/group_vars/control_nodes.yml`
- Modify: `tests/fixtures/inventory.yml`
- Modify: `preflight.yml:56-78`
- Modify: `Makefile` (`.PHONY` list, new `mac` target)
- Modify: `inventory/group_vars/all_vault.yml.example`
- Test: `tests/validate_preflight_addressing.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: inventory group `control_nodes` containing host `mac-control`; `make mac` → `ansible-playbook site.yml --limit control_nodes`; vault vars `vault_mac_become_password`, `vault_tailscale_auth_key`.

- [ ] **Step 1: Write the failing test**

Create `tests/validate_preflight_addressing.py`:

```python
#!/usr/bin/env python3
"""Prove preflight's address-uniqueness assert actually covers control_nodes.

inventory/hosts.yml's `ansible_host` is the single source of truth for every
machine's address, and preflight.yml exists to catch a missing or duplicated
one before it surfaces as a confusing failure deep inside cloud-init rendering
or Caddy templating.

That assert was written when `service_vms` was the only group with addresses.
Adding `control_nodes` without extending it would leave the new machine
uncovered — and the failure mode is silent: the gate still passes, it just
stops looking at half the estate.

So this runs the real playbook against a synthetic inventory, twice:

  distinct addresses      rc 0        the gate does not false-positive
  duplicate in            non-zero    the gate actually looks at the new group
  control_nodes

The second case is the point. The first is its positive control: without it, a
gate that had degraded to `assert: true` would pass this test.

Only preflight's localhost play runs (`--limit localhost`), so nothing here
touches the homelab or needs a vault.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "preflight.yml"
VENV_ANSIBLE = ROOT / ".venv/bin/ansible-playbook"


def ansible_playbook() -> str:
    return str(VENV_ANSIBLE) if VENV_ANSIBLE.exists() else "ansible-playbook"


def inventory(control_address: str) -> dict:
    """A minimal inventory with three service VMs and one control node."""
    return {
        "all": {
            "children": {
                "service_vms": {
                    "children": {
                        "download_vms": {
                            "hosts": {"svc-download": {"ansible_host": "127.0.0.2"}}
                        },
                        "media_vms": {
                            "hosts": {"svc-media": {"ansible_host": "127.0.0.1"}}
                        },
                        "infra_vms": {
                            "hosts": {"svc-infra": {"ansible_host": "127.0.0.3"}}
                        },
                    }
                },
                "control_nodes": {
                    "hosts": {"mac-control": {"ansible_host": control_address}}
                },
            }
        }
    }


CASES = [
    ("distinct addresses", "127.0.0.4", 0),
    ("control_nodes duplicates a service VM", "127.0.0.2", 1),
]


def run(control_address: str) -> int:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
        yaml.safe_dump(inventory(control_address), handle)
        path = handle.name
    try:
        completed = subprocess.run(
            [
                ansible_playbook(),
                str(PLAYBOOK),
                "--inventory",
                path,
                "--limit",
                "localhost",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        return completed.returncode
    finally:
        Path(path).unlink(missing_ok=True)


def main() -> int:
    failures = []
    for name, address, expected_nonzero in CASES:
        rc = run(address)
        got_nonzero = 1 if rc != 0 else 0
        if got_nonzero != expected_nonzero:
            failures.append(
                f"{name}: expected {'failure' if expected_nonzero else 'success'}, "
                f"got exit {rc}. preflight.yml must collect addresses from "
                "control_nodes as well as service_vms."
            )

    if failures:
        print("preflight addressing regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print(f"preflight addressing: OK ({len(CASES)} cases, "
          "including that a duplicate in control_nodes still fails)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python tests/validate_preflight_addressing.py`
Expected: FAIL — "control_nodes duplicates a service VM: expected failure, got exit 0". The assert currently only inspects `service_vms`.

- [ ] **Step 3: Extend preflight.yml**

Replace the `Collect each service VM's configured address` task and the two asserts that follow it (`preflight.yml:56-78`) with:

```yaml
    # inventory/hosts.yml's ansible_host is the single source of truth for
    # each machine's IP (see the comment there). Catch a missing or duplicated
    # address here, before it produces a confusing failure deep inside pve_vm's
    # cloud-init rendering or svc_media's Caddy/dnsmasq templating.
    #
    # control_nodes is included because mac-control's address is a security
    # control, not just a route: Ollama binds it specifically so the service
    # never answers on the tailnet. A collision there is worth catching early.
    - name: Collect every addressed host's configured address
      ansible.builtin.set_fact:
        addressed_hosts: >-
          {{ (groups['service_vms'] | default([]))
             + (groups['control_nodes'] | default([])) }}
        managed_addresses: >-
          {{ ((groups['service_vms'] | default([]))
              + (groups['control_nodes'] | default([])))
             | map('extract', hostvars, 'ansible_host') | list }}

    - name: Assert every service VM and control node defines ansible_host
      ansible.builtin.assert:
        that:
          - managed_addresses | select('none') | list | length == 0
        fail_msg: >-
          Every host under service_vms or control_nodes in inventory/hosts.yml
          must define ansible_host — it's the single source of truth for that
          machine's IP. Hosts checked: {{ addressed_hosts }}.
        quiet: true

    - name: Assert managed addresses are well-formed IPv4 and unique
      ansible.builtin.assert:
        that:
          - managed_addresses | select('match', '^[0-9]{1,3}(\\.[0-9]{1,3}){3}$') | list | length == managed_addresses | length
          - managed_addresses | unique | length == managed_addresses | length
        fail_msg: >-
          inventory/hosts.yml's service_vms and control_nodes ansible_host
          values must be well-formed, distinct IPv4 addresses
          (got: {{ managed_addresses }}).
        quiet: true
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/validate_preflight_addressing.py`
Expected: PASS — "preflight addressing: OK (2 cases, including that a duplicate in control_nodes still fails)"

- [ ] **Step 5: Add the inventory group**

Append to `inventory/hosts.yml`, at the same indentation as `service_vms` (inside `all.children`):

```yaml
    # The always-on second Ansible control node: a headless M1 Pro MacBook Pro
    # with a broken backlight, wired to the LAN through a USB-C 1GbE adapter.
    #
    # It is deliberately NOT under service_vms — that group carries guest and
    # NFS assumptions that do not apply to a macOS laptop.
    #
    # Configured FROM the working laptop, never from itself: the play's first
    # task refuses a self-target, because reconfiguring Ollama, Tailscale and
    # pmset underneath your own SSH session loses the session mid-play and
    # leaves the box half-built with no readable screen to inspect.
    #
    # /usr/bin/python3 is the Xcode Command Line Tools interpreter, which is a
    # documented prerequisite. Homebrew's python is NOT used here — the role
    # installs Homebrew, so depending on it would be circular.
    control_nodes:
      hosts:
        mac-control:
          ansible_host: 192.168.1.41
      vars:
        ansible_user: straderb
        ansible_become: true
        ansible_become_method: sudo
        ansible_python_interpreter: /usr/bin/python3
```

Create `inventory/group_vars/control_nodes.yml`:

```yaml
---
# macOS admin accounts require a password for sudo. `| default(omit)` keeps
# vault-less runs (which verify.yml supports) from failing on an undefined
# variable — they fail on the become prompt instead, which is an honest
# failure rather than a template error.
ansible_become_password: "{{ vault_mac_become_password | default(omit) }}"

# Absolute paths on the control node. Everything the role manages lives under
# these two, so a rebuild has one place to look.
mac_control_home: /Users/straderb
mac_control_checkout: /Users/straderb/homelab-iac
```

Add to `inventory/group_vars/all_vault.yml.example`, near the other host-credential entries:

```yaml
# --- mac-control (the always-on second control node) ---------------------
# straderb's macOS login password. Needed because macOS admin accounts
# require a password for sudo and this repo does not weaken that with a
# NOPASSWD sudoers entry.
vault_mac_become_password: "REPLACE_macos_login_password"
# Tailscale auth key for enrolling mac-control. Generate a reusable,
# non-ephemeral key in the Tailscale admin console.
vault_tailscale_auth_key: "REPLACE_tskey_auth_value"
```

- [ ] **Step 6: Add the fixture host and the Make target**

Append to `tests/fixtures/inventory.yml` inside `all.children`:

```yaml
    control_nodes:
      hosts:
        mac-control:
          ansible_connection: local
          ansible_host: 127.0.0.4
          ansible_python_interpreter: "{{ ansible_playbook_python }}"
```

In `Makefile`, add `mac` to the `.PHONY` line that already lists `deploy dl media infra pve`, and add this target immediately after the `pve:` target (around line 181):

```makefile
mac: ## Configure and verify the always-on control node (mac-control)
	$(ANSIBLE) $(PLAYBOOK) $(VAULT) --limit control_nodes $(ARGS)
```

Add the new validator to the `validate-provisioning` target so it runs in `make validate`:

```makefile
	$(PYTHON) tests/validate_preflight_addressing.py
```

- [ ] **Step 7: Verify the whole gate suite still passes**

Run: `make validate`
Expected: PASS, including `preflight addressing: OK (2 cases, ...)`.

- [ ] **Step 8: Commit**

```bash
git add inventory/hosts.yml inventory/group_vars/control_nodes.yml \
        inventory/group_vars/all_vault.yml.example tests/fixtures/inventory.yml \
        preflight.yml Makefile tests/validate_preflight_addressing.py
git commit -m "feat: add the control_nodes inventory group and cover it in preflight

preflight.yml's address-uniqueness assert collected from service_vms only, so
a new group would have been invisible to the one gate that catches an IP
collision. It now covers control_nodes too, and a test proves it by running
the real playbook against a synthetic inventory with a deliberate collision.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `admin_ssh_pubkeys` as a list, managed on running VMs

`admin_ssh_pubkey` is a scalar referenced in exactly one place — cloud-init, at provision time. Nothing manages `authorized_keys` on a running VM, so adding a key today means re-provisioning, and revoking one is impossible without doing so again. `mac-control` generates its own keypair, so this gap has to close first.

**Files:**
- Modify: `inventory/group_vars/all/main.yml:124-126`
- Modify: `roles/pve_vm/templates/user-data.yaml.j2:18`
- Create: `roles/service_vm/tasks/authorized-keys.yml`
- Modify: `roles/service_vm/tasks/main.yml`
- Modify: `site.yml:23` (prerequisite comment)
- Test: `tests/validate_admin_keys.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `admin_ssh_pubkeys` (list of strings) in `inventory/group_vars/all/main.yml`, consumed by `roles/pve_vm` at provision time and `roles/service_vm` on every run.

- [ ] **Step 1: Write the failing test**

Create `tests/validate_admin_keys.py`:

```python
#!/usr/bin/env python3
"""Prove the admin SSH key list renders every key, not just the first.

admin_ssh_pubkey used to be a scalar, consumed only by cloud-init at provision
time. That made key rotation require rebuilding a VM and made revocation
impossible without doing so again — so mac-control, which generates its own
keypair, could not be granted access at all.

Turning it into a list is easy to get subtly wrong in two ways, and both look
fine on a healthy first deploy:

  - the template renders `{{ admin_ssh_pubkeys }}` (a Python list repr) or
    only `[0]`, so the second key silently never lands;
  - service_vm manages authorized_keys with `exclusive: true` against a single
    key, quietly evicting the others on the next run.

So this asserts a two-key list renders two keys AND a one-key list renders
exactly one. The second case is the positive control: a template that emitted
every key it could find regardless of input would pass the first check alone.

It also fails if the scalar name survives anywhere, because a leftover
reference reads as working code and silently uses an undefined variable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "roles/pve_vm/templates/user-data.yaml.j2"

KEY_A = "ssh-ed25519 AAAAKEYAAA workstation@example"
KEY_B = "ssh-ed25519 AAAAKEYBBB mac-control@example"


def render(keys: list[str]) -> str:
    """Render only the ssh_authorized_keys block, with Jinja we control."""
    import jinja2

    text = TEMPLATE.read_text(encoding="utf-8")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    return env.from_string(text).render(
        admin_ssh_pubkeys=keys,
        # Everything else the template needs, with values that do not matter
        # to this test but must be defined for StrictUndefined.
        vm_name="fixture",
        vm_address="127.0.0.1",
        vm_gateway="127.0.0.1",
        vm_nameservers=["127.0.0.1"],
        ansible_user="fixture",
    )


def main() -> int:
    failures = []

    for label, keys in (("two keys", [KEY_A, KEY_B]), ("one key", [KEY_A])):
        try:
            rendered = render(keys)
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures.append(f"{label}: template failed to render: {exc}")
            continue
        for key in keys:
            if key not in rendered:
                failures.append(f"{label}: {key!r} missing from rendered cloud-init")
        absent = KEY_B if len(keys) == 1 else None
        if absent and absent in rendered:
            failures.append(
                f"{label}: {absent!r} appeared though it was not in the list — "
                "the template is not driven by its input")
        # A bare `{{ admin_ssh_pubkeys }}` renders Python's list repr on one
        # line, which cloud-init accepts as a single malformed key.
        if "['" in rendered or '["' in rendered:
            failures.append(
                f"{label}: rendered a Python list repr rather than one key per "
                "line — the template needs a for loop")

    stale = subprocess.run(
        ["git", "grep", "-n", "admin_ssh_pubkey\\b"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if stale.returncode == 0 and stale.stdout.strip():
        failures.append(
            "the singular admin_ssh_pubkey still appears:\n      "
            + "\n      ".join(stale.stdout.strip().splitlines()))

    if failures:
        print("admin SSH key regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print("admin ssh keys: OK (2 cases, including that a one-key list renders one key)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python tests/validate_admin_keys.py`
Expected: FAIL — the template still references the singular `admin_ssh_pubkey`, so `StrictUndefined` raises, and the `git grep` finds live references.

- [ ] **Step 3: Convert the variable and the template**

In `inventory/group_vars/all/main.yml`, replace lines 124-126 with:

```yaml
# --- Admin SSH keys injected into every VM (public key CONTENT, not paths) ---
# A LIST, not a scalar. Every entry is installed on each service VM on every
# run by roles/service_vm/tasks/authorized-keys.yml, and baked into cloud-init
# at provision time by roles/pve_vm.
#
# It became a list on 2026-08-12 so mac-control could hold a key of its own —
# generated on that machine, never copied, and revocable by deleting one line
# here and running `make deploy`. While this was a scalar consumed only by
# cloud-init, adding a key meant rebuilding a VM and revoking one was not
# possible at all.
#
# Removing an entry REVOKES it: the authorized_keys task is exclusive.
admin_ssh_pubkeys:
  # The working laptop — the only machine that merges to main.
  - "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMjZAEI+FfdW74JRrWjUbSK9HmF7AVHaa8qlYsN9YSLB straderb@Mac.fort.wow"
```

In `roles/pve_vm/templates/user-data.yaml.j2`, replace line 18 (`      - {{ admin_ssh_pubkey }}`) with:

```jinja
{% for pubkey in admin_ssh_pubkeys %}
      - {{ pubkey }}
{% endfor %}
```

In `site.yml`, update the prerequisite comment on line 23:

```yaml
#   - admin_ssh_pubkeys set in inventory/group_vars/all/main.yml (a LIST)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/validate_admin_keys.py`
Expected: PASS — "admin ssh keys: OK (2 cases, including that a one-key list renders one key)"

> **RULING 2026-08-13 — Steps 5 and 6 are CANCELLED. Do not implement them.**
>
> `exclusive: true` is not available on `~/.ssh/authorized_keys`.
> `roles/svc_infra/tasks/verify-runner.yml` already pushes a second key there
> (generated on svc-infra, `state: present`, scoped
> `from="192.168.1.32"`). An exclusive task would strip it on every
> `make dl`, killing the nightly verification runner, and would put two roles
> in a fight over one file so **no play could report `changed=0` again**.
>
> Task 2 ends after Step 4, plus the Step 7 commit covering Steps 1-4 only.
> `mac-control` uses the working laptop's existing key. Managing keys on
> running VMs needs admin keys in their own `AuthorizedKeysFile`, which is a
> separate spec — see the spec's follow-ups.

- [ ] ~~**Step 5: Manage authorized_keys on running VMs**~~ (CANCELLED)

Create `roles/service_vm/tasks/authorized-keys.yml`:

```yaml
---
# Manage the admin keys on RUNNING guests, not only at provision time.
#
# roles/pve_vm bakes admin_ssh_pubkeys into cloud-init, but cloud-init runs
# once. Before this file existed, adding a key meant rebuilding the VM and
# revoking one was impossible without doing so again — which is why the
# variable was a scalar for as long as it was.
#
# exclusive: true is the point. Without it this only ever ADDS, and a key
# removed from main.yml would keep working forever, which is a revocation
# that silently does not revoke.
- name: Install the admin SSH keys for the connecting user
  ansible.posix.authorized_key:
    user: "{{ ansible_user }}"
    key: "{{ admin_ssh_pubkeys | join('\n') }}"
    exclusive: true
    state: present
```

In `roles/service_vm/tasks/main.yml`, add this import immediately after the `Validate VM bootstrap invariants` block:

```yaml
- name: Manage admin SSH keys on the running guest
  ansible.builtin.import_tasks: authorized-keys.yml
  tags: [files]
```

- [ ] ~~**Step 6: Validate and deploy to one VM**~~ (CANCELLED — see the ruling above; no deploy is needed, since nothing about a running VM changes)

- [ ] **Step 7: Commit**

Run `make validate` first — it must pass.

```bash
git add inventory/group_vars/all/main.yml roles/pve_vm/templates/user-data.yaml.j2 \
        site.yml tests/validate_admin_keys.py
git commit -m "feat: manage admin SSH keys as a list on running VMs

admin_ssh_pubkey was a scalar consumed only by cloud-init at provision time,
so adding a key meant rebuilding a VM and revoking one was not possible at
all. It becomes admin_ssh_pubkeys, installed on every run with exclusive:true
so that removing an entry actually revokes it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The deploy lock on thurgadin

Two control nodes pointed at one estate is a more likely way to break things than anything in the credential discussion, and a documented rule will not hold. The lock lives on thurgadin because it hosts every VM — it is up whenever deploying is meaningful — and that keeps it outside the machines being deployed to.

**Files:**
- Create: `scripts/deploy-lock.sh`
- Modify: `site.yml` (pre_tasks on the first play, release on the last)
- Modify: `Makefile` (`deploy-unlock` target, `.PHONY`)
- Test: `tests/validate_deploy_lock.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `scripts/deploy-lock.sh acquire|release|status <lockfile> <holder>`; exit 0 on success, 1 when the lock is held by someone else. `make deploy-unlock`.

- [ ] **Step 1: Write the failing test**

Create `tests/validate_deploy_lock.py`:

```python
#!/usr/bin/env python3
"""Exercise the deploy lock, especially the case where it must REFUSE.

Two control nodes now deploy to one estate. A second `make infra` starting
while the first is mid-play is how this repo gets a genuinely confusing
failure — half-converged units, a Quadlet rewritten under a running restart.

A lock only earns its place if it says no. On a healthy single-operator day it
succeeds every time, so the refusal is never exercised in production; left
alone, a lock that had degraded to `exit 0` would look identical to a working
one and this whole mechanism would be decoration.

Four cases, all against a temp directory:

  acquire on a free lock      exit 0     the normal path
  acquire while held          NON-ZERO   the case worth having
  the refusal names the holder           so the operator can act on it
  release then re-acquire     exit 0     the lock does not wedge itself

The third matters more than it looks: a refusal that does not say who holds it
sends the operator hunting, and at 2am they will delete the file instead.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy-lock.sh"


def run(action: str, lock: Path, holder: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), action, str(lock), holder],
        capture_output=True, text=True,
    )


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "deploy.lock"

        first = run("acquire", lock, "workstation")
        if first.returncode != 0:
            failures.append(
                f"acquire on a free lock exited {first.returncode}: {first.stderr.strip()}")

        second = run("acquire", lock, "mac-control")
        if second.returncode == 0:
            failures.append(
                "acquire succeeded while the lock was held — the lock does not lock")
        combined = second.stdout + second.stderr
        if "workstation" not in combined:
            failures.append(
                "the refusal did not name the holder; got: " + combined.strip())

        released = run("release", lock, "workstation")
        if released.returncode != 0:
            failures.append(
                f"release exited {released.returncode}: {released.stderr.strip()}")

        third = run("acquire", lock, "mac-control")
        if third.returncode != 0:
            failures.append(
                f"acquire after release exited {third.returncode} — the lock wedged")

    if failures:
        print("deploy lock regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print("deploy lock: OK (4 cases, including that a held lock refuses and says who holds it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python tests/validate_deploy_lock.py`
Expected: FAIL — `scripts/deploy-lock.sh` does not exist.

- [ ] **Step 3: Write the lock script**

Create `scripts/deploy-lock.sh`:

```bash
#!/usr/bin/env bash
# Advisory deploy lock, so two control nodes cannot deploy to one estate at
# once. Held on thurgadin: it hosts every VM, so it is up whenever deploying
# is meaningful, and the lock stays outside the machines being deployed to.
#
# Usage: deploy-lock.sh acquire|release|status <lockfile> <holder>
#
# Acquisition uses `set -o noclobber`, which makes the shell open the file
# with O_EXCL — the test-and-create is one syscall, so two deploys racing
# cannot both win. A plain `[ -f ]` test would leave a window between the
# check and the write, which is exactly the case this exists for.
set -euo pipefail

action=${1:?usage: deploy-lock.sh acquire|release|status <lockfile> <holder>}
lock=${2:?missing lockfile path}
holder=${3:-unknown}

case "$action" in
  acquire)
    if (set -o noclobber; printf '%s\n%s\n' "$holder" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
          > "$lock") 2>/dev/null; then
      echo "deploy lock acquired by ${holder}"
      exit 0
    fi
    # Held. Say who and since when, so the operator can act rather than guess
    # — at 2am an unexplained refusal gets the file deleted.
    existing_holder=$(sed -n '1p' "$lock" 2>/dev/null || echo unknown)
    existing_since=$(sed -n '2p' "$lock" 2>/dev/null || echo unknown)
    echo "deploy lock is HELD by ${existing_holder} since ${existing_since}" >&2
    echo "If that deploy is genuinely gone, clear it with: make deploy-unlock" >&2
    exit 1
    ;;
  release)
    rm -f "$lock"
    echo "deploy lock released by ${holder}"
    exit 0
    ;;
  status)
    if [ -f "$lock" ]; then
      echo "held by $(sed -n '1p' "$lock") since $(sed -n '2p' "$lock")"
    else
      echo "free"
    fi
    exit 0
    ;;
  *)
    echo "unknown action: ${action}" >&2
    exit 2
    ;;
esac
```

Make it executable: `chmod +x scripts/deploy-lock.sh`

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/validate_deploy_lock.py`
Expected: PASS — "deploy lock: OK (4 cases, ...)"

- [ ] **Step 5: Wire it into site.yml**

Add a new play at the very top of `site.yml`, immediately after the header comment block and before `- name: Provision service VMs`:

```yaml
# ---------- Deploy lock: one control node at a time ----------
# Two control nodes (the working laptop and mac-control) can both reach this
# estate. A documented rule would not hold, so the lock is real. It is taken
# on thurgadin because that host runs every VM — it is up whenever deploying
# is meaningful — and holding it there keeps it off the machines being
# deployed to.
- name: Acquire the deploy lock
  hosts: pve_mon_hosts
  gather_facts: false
  any_errors_fatal: true
  tasks:
    - name: Take the estate-wide deploy lock
      ansible.builtin.script:
        cmd: >-
          scripts/deploy-lock.sh acquire /var/lock/homelab-deploy.lock
          {{ lookup('pipe', 'hostname') }}
      changed_when: true
```

Add the release to the `Announce success` play at the end of `site.yml`, as its final task:

```yaml
    - name: Release the deploy lock
      ansible.builtin.script:
        cmd: >-
          scripts/deploy-lock.sh release /var/lock/homelab-deploy.lock
          {{ lookup('pipe', 'hostname') }}
      delegate_to: "{{ groups['pve_mon_hosts'] | first }}"
      changed_when: true
```

Add to `Makefile`, after the `mac:` target, and add `deploy-unlock` to `.PHONY`:

```makefile
deploy-unlock: ## Clear a stale deploy lock left by a crashed run
	$(ANSIBLE_ADHOC) pve_mon_hosts -i inventory/hosts.yml $(VAULT) \
	  -m ansible.builtin.script \
	  -a "cmd='scripts/deploy-lock.sh release /var/lock/homelab-deploy.lock manual'"
```

Add the validator to `validate-ci` in the Makefile:

```makefile
	$(PYTHON) tests/validate_deploy_lock.py
```

- [ ] **Step 6: Validate and prove the lock in the real path**

Run: `make validate`
Expected: PASS, including "deploy lock: OK (4 cases, ...)".

Run: `make infra`
Expected: succeeds, lock taken and released.

Now prove the refusal actually fires against the live estate — this is the whole point of the task, and a lock nobody has seen say no is a lock nobody should trust:

```bash
ssh root@192.168.1.10 "printf 'someone-else\n2026-08-12T00:00:00Z\n' > /var/lock/homelab-deploy.lock"
make infra   # expected: FAILS, naming someone-else
make deploy-unlock
make infra   # expected: succeeds again
```

- [ ] **Step 7: Commit**

```bash
git add scripts/deploy-lock.sh tests/validate_deploy_lock.py site.yml Makefile
git commit -m "feat: serialize deploys behind a lock on thurgadin

Two control nodes can now reach this estate, and a documented rule that only
one deploys at a time would not hold. Acquisition uses O_EXCL via noclobber so
a race cannot let both win, and the refusal names the holder and the time —
an unexplained refusal gets the lockfile deleted at 2am.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The `mac_control` role skeleton, its idempotence gate, and prerequisite asserts

The role's foundation plus the static gate that keeps it honest. macOS state is mostly not file-shaped, so most tasks are `command` — and `changed=0` is only as good as their `changed_when`.

**Files:**
- Create: `roles/mac_control/defaults/main.yml`
- Create: `roles/mac_control/tasks/main.yml`
- Create: `roles/mac_control/tasks/prereqs.yml`
- Modify: `requirements.yml`
- Modify: `Makefile` (`validate-ansible` gets the new validator)
- Test: `tests/validate_mac_idempotence.py`

**Interfaces:**
- Consumes: `control_nodes` group and `mac_control_checkout` / `mac_control_home` from Task 1.
- Produces: role `mac_control` with entry points `main` and `verify`; fact `mac_control_verified` (set in Task 12); defaults `mac_control_lan_ip`, `mac_control_ollama_port`, `mac_control_models`.

- [ ] **Step 1: Write the failing test**

Create `tests/validate_mac_idempotence.py`:

```python
#!/usr/bin/env python3
"""Require every command/shell task in roles/mac_control to set changed_when.

CLAUDE.md leans harder on `changed=0` than on any other single signal: a deploy
that reports it against a clean tree is the proof that what is running equals
what is committed. Every Linux role here earns that mostly for free, because
Ansible modules know whether they changed anything.

macOS does not work that way. Its state is mostly not file-shaped — pmset,
systemsetup, scutil, launchctl, ollama — so the role is largely `command`
tasks, and Ansible reports every one of them as changed unless told otherwise.
A role with one bare command can never report changed=0, and the proof stops
meaning anything for the whole estate rather than just for this host.

The opposite mistake is worse and this gate does NOT catch it: `changed_when:
false` on a task that really does change something reports idempotence without
having it. That is why the rule is written down in the spec and why every
setter here is paired with a reader it compares against.

The role must exist and must contain at least one command task, so this cannot
pass by finding nothing to check.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/mac_control"

COMMAND_MODULES = {
    "command", "shell", "ansible.builtin.command", "ansible.builtin.shell",
    "raw", "ansible.builtin.raw", "script", "ansible.builtin.script",
}


def iter_tasks(node, path):
    """Yield (task, path) for every task-shaped mapping, including nested blocks."""
    if isinstance(node, list):
        for item in node:
            yield from iter_tasks(item, path)
    elif isinstance(node, dict):
        if any(key in node for key in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                if key in node:
                    yield from iter_tasks(node[key], path)
        else:
            yield node, path


def main() -> int:
    if not ROLE.is_dir():
        print(f"roles/mac_control does not exist ({ROLE})", file=sys.stderr)
        return 1

    task_files = sorted((ROLE / "tasks").glob("*.yml"))
    if not task_files:
        print("roles/mac_control/tasks contains no task files", file=sys.stderr)
        return 1

    failures = []
    command_tasks = 0

    for path in task_files:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for task, _ in iter_tasks(loaded, path):
            modules = COMMAND_MODULES & set(task)
            if not modules:
                continue
            command_tasks += 1
            if "changed_when" not in task:
                name = task.get("name", "<unnamed>")
                failures.append(
                    f"{path.relative_to(ROOT)}: task {name!r} uses "
                    f"{sorted(modules)[0]} without changed_when")

    if command_tasks == 0:
        print(
            "roles/mac_control has no command/shell tasks at all. That is almost\n"
            "certainly wrong for a macOS role, and a gate that finds nothing to\n"
            "check is a gate nobody can tell is broken.",
            file=sys.stderr)
        return 1

    if failures:
        print("mac_control idempotence regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        print("  Every command task must derive changed_when from a reader task.\n"
              "  `make mac` twice must report changed=0 on the second run.\n",
              file=sys.stderr)
        return 1

    print(f"mac_control idempotence: OK ({command_tasks} command tasks, all with changed_when)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python tests/validate_mac_idempotence.py`
Expected: FAIL — "roles/mac_control does not exist".

- [ ] **Step 3: Create the role skeleton and prerequisite asserts**

Create `roles/mac_control/defaults/main.yml`:

```yaml
---
# The wired LAN address. Ollama binds THIS specifically, never 0.0.0.0 —
# see roles/mac_control/tasks/ollama.yml for why that distinction matters.
mac_control_lan_ip: "{{ hostvars['mac-control'].ansible_host }}"
mac_control_ollama_port: 11434

# The two "small, always-on" models, relocated from TERRA (docs/gpu-host.md)
# — a machine that gets rebooted, gamed on and turned off. Relocated rather
# than duplicated, deliberately: their presence in Open WebUI's model list is
# then proof that the second endpoint works.
mac_control_models:
  - qwen2.5-coder:1.5b-base
  - nomic-embed-text

# Bound Ollama so it can never starve this machine's primary job. 16 GB splits
# roughly: macOS 3-4 GB, agent sessions 1-1.5 GB each, these two models ~2 GB.
# A 7-8B chat model with a 32k KV cache would want 6-7 GB and change that.
mac_control_ollama_max_loaded_models: 1
mac_control_ollama_keep_alive: 5m

mac_control_brew_packages:
  - git
  - tmux
  - node
  - shellcheck
  - gitleaks
  # Ansible connects through /usr/bin/python3 (the Xcode CLT one, because
  # Homebrew is installed BY this role and depending on it would be circular).
  # This one is different: it builds the checkout's .venv, which is what makes
  # this machine a control node rather than a laptop with the repo on it.
  - python@3.13
```

Create `roles/mac_control/tasks/prereqs.yml`:

```yaml
---
# A control node may not deploy to itself. Reconfiguring Ollama, Tailscale and
# pmset underneath your own SSH session loses the session mid-play and leaves
# the box half-built — with no readable screen to inspect. The circularity is
# broken by direction, not by cleverness.
- name: Collect the addresses of the machine running Ansible
  ansible.builtin.command: /usr/sbin/ipconfig getifaddr en0
  register: mac_control_local_addr
  changed_when: false
  failed_when: false
  delegate_to: localhost
  become: false

- name: Refuse to configure a control node from itself
  ansible.builtin.assert:
    that:
      - mac_control_local_addr.stdout | trim != mac_control_lan_ip
    fail_msg: >-
      This play is running ON {{ mac_control_lan_ip }}, the machine it would
      reconfigure. Run `make mac` from the other control node. Restarting
      Ollama, Tailscale and pmset underneath your own SSH session leaves this
      host half-configured with no screen to read.

# Xcode Command Line Tools are the analog of cloud-init for the VMs: without
# them there is no /usr/bin/python3 for Ansible to run through, so this cannot
# self-heal — it can only say so clearly.
- name: Check for the Xcode Command Line Tools
  ansible.builtin.command: /usr/bin/xcode-select -p
  register: mac_control_clt
  changed_when: false
  failed_when: false

- name: Require the Xcode Command Line Tools
  ansible.builtin.assert:
    that:
      - mac_control_clt.rc == 0
    fail_msg: >-
      Xcode Command Line Tools are missing. Run `xcode-select --install` on
      mac-control and complete the GUI prompt (Screen Sharing works for this),
      then re-run `make mac`.

# The two secret files are placed BY HAND, out of band. The role asserts they
# exist with the right mode and never reads, renders or logs their contents.
# The vault password cannot live in the vault, and any mechanism threading it
# through a play is a mechanism that can put it in a log.
- name: Stat the out-of-band secret files
  ansible.builtin.stat:
    path: "{{ item }}"
  loop:
    - "{{ mac_control_checkout }}/.vault_pass"
    - "{{ mac_control_checkout }}/inventory/group_vars/all/vault.yml"
  register: mac_control_secret_files

- name: Require the secret files to exist at mode 0600
  ansible.builtin.assert:
    that:
      - item.stat.exists
      - item.stat.mode == '0600'
    fail_msg: >-
      {{ item.item }} must exist at mode 0600 on mac-control. Both are placed
      by hand, out of band — see docs/mac-control-node.md. The commit pins the
      code, not the secrets.
  loop: "{{ mac_control_secret_files.results }}"
  loop_control:
    label: "{{ item.item }}"
```

Create `roles/mac_control/tasks/main.yml`:

```yaml
---
- name: Validate control node prerequisites
  ansible.builtin.import_tasks: prereqs.yml
  tags: [always]

- name: Verify control node invariants
  ansible.builtin.import_tasks: verify.yml
  when: not ansible_check_mode
  tags: [verify]
```

Create a placeholder `roles/mac_control/tasks/verify.yml` that will grow in Task 12:

```yaml
---
- name: Mark control node verification complete
  ansible.builtin.set_fact:
    mac_control_verified: true
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/validate_mac_idempotence.py`
Expected: PASS — "mac_control idempotence: OK (2 command tasks, all with changed_when)"

- [ ] **Step 5: Add `community.general` and wire the gate in**

In `requirements.yml`, add:

```yaml
  # Added 2026-08-12 for the `homebrew` module, which roles/mac_control needs.
  # Doing packages with `command` instead would mean hand-rolling idempotence
  # for every one of them, which is exactly what makes changed=0 unreliable.
  - name: community.general
    version: "11.4.0"
```

In `Makefile`, add to `validate-ansible`:

```makefile
	$(PYTHON) tests/validate_mac_idempotence.py
```

Run: `make deps` to install the new collection.

- [ ] **Step 6: Validate**

Run: `make validate`
Expected: PASS, including "mac_control idempotence: OK".

- [ ] **Step 7: Commit**

```bash
git add roles/mac_control tests/validate_mac_idempotence.py requirements.yml Makefile
git commit -m "feat: add the mac_control role skeleton with an idempotence gate

macOS state is mostly not file-shaped, so this role is largely command tasks —
and Ansible reports every one as changed unless told otherwise. One bare
command would make changed=0 unreachable, and that signal is what CLAUDE.md
leans on hardest. A static gate now requires changed_when on all of them, and
fails if it finds no command tasks at all.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Base system configuration

Packages, power settings, remote access, hostname and Tailscale. Every setter reads first and compares, so the second `make mac` reports nothing.

**Files:**
- Create: `roles/mac_control/tasks/packages.yml`
- Create: `roles/mac_control/tasks/power.yml`
- Create: `roles/mac_control/tasks/remote-access.yml`
- Create: `roles/mac_control/tasks/tailscale.yml`
- Modify: `roles/mac_control/tasks/main.yml`

**Interfaces:**
- Consumes: `mac_control_brew_packages` (Task 4), `vault_tailscale_auth_key` (Task 1).
- Produces: Homebrew at `/opt/homebrew`; `pmset` configured for unattended operation; Remote Login and Screen Sharing enabled; Tailscale enrolled.

- [ ] **Step 1: Write `packages.yml`**

```yaml
---
- name: Check whether Homebrew is installed
  ansible.builtin.stat:
    path: /opt/homebrew/bin/brew
  register: mac_control_brew

- name: Install Homebrew
  ansible.builtin.shell:
    cmd: >-
      NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL
      https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  become: true
  become_user: "{{ ansible_user }}"
  when: not mac_control_brew.stat.exists
  changed_when: not mac_control_brew.stat.exists

- name: Install the control node packages
  community.general.homebrew:
    name: "{{ mac_control_brew_packages }}"
    state: present
    path: /opt/homebrew/bin
  become: true
  become_user: "{{ ansible_user }}"

# The checkout itself is placed by hand (docs/mac-control-node.md) — it holds
# the two out-of-band secrets, so cloning it is not something the role does.
# The virtualenv inside it IS managed: without ansible, proxmoxer and the lint
# tooling, this machine is a laptop rather than a control node.
- name: Check for the control node virtualenv
  ansible.builtin.stat:
    path: "{{ mac_control_checkout }}/.venv/bin/ansible-playbook"
  register: mac_control_venv

- name: Create the control node virtualenv
  ansible.builtin.command:
    cmd: /opt/homebrew/bin/python3 -m venv .venv
    chdir: "{{ mac_control_checkout }}"
  become: true
  become_user: "{{ ansible_user }}"
  when: not mac_control_venv.stat.exists
  changed_when: not mac_control_venv.stat.exists

- name: Install the control node python requirements
  ansible.builtin.pip:
    requirements: "{{ item }}"
    virtualenv: "{{ mac_control_checkout }}/.venv"
  loop:
    - "{{ mac_control_checkout }}/requirements.txt"
    - "{{ mac_control_checkout }}/requirements-dev.txt"
  become: true
  become_user: "{{ ansible_user }}"
```

- [ ] **Step 2: Write `power.yml`**

```yaml
---
# A closed lid in a closet must never sleep, and a power cut must bring the
# machine back unaided — there is no readable screen to notice it did not.
#
# `pmset -g custom` is read ONCE into a fact and each setting applied only if
# it differs. Applying unconditionally would make every `make mac` report
# changed, and CLAUDE.md's changed=0 proof would stop meaning anything.
- name: Read the current power settings
  ansible.builtin.command: /usr/bin/pmset -g custom
  register: mac_control_pmset
  changed_when: false

- name: Apply the unattended power settings
  ansible.builtin.command: "/usr/bin/pmset -a {{ item.key }} {{ item.value }}"
  loop:
    # Never sleep on mains power.
    - { key: sleep, value: "0" }
    # Ignore the lid entirely — this machine runs closed.
    - { key: disablesleep, value: "1" }
    # Come back on its own after a power failure. Without this the box stays
    # dark and the only symptom is a dead-man's switch going quiet.
    - { key: autorestart, value: "1" }
    # Wake for network access, so a dropped link does not strand it.
    - { key: womp, value: "1" }
  when: >-
    mac_control_pmset.stdout is not search('^\s*' ~ item.key ~ '\s+' ~ item.value ~ '\s*$')
  changed_when: >-
    mac_control_pmset.stdout is not search('^\s*' ~ item.key ~ '\s+' ~ item.value ~ '\s*$')
  loop_control:
    label: "{{ item.key }}={{ item.value }}"
```

- [ ] **Step 3: Write `remote-access.yml`**

```yaml
---
- name: Read the hostname
  ansible.builtin.command: /usr/sbin/scutil --get ComputerName
  register: mac_control_hostname
  changed_when: false
  failed_when: false

- name: Set the hostname
  ansible.builtin.command: "/usr/sbin/scutil --set {{ item }} mac-control"
  loop: [ComputerName, HostName, LocalHostName]
  when: mac_control_hostname.stdout | trim != 'mac-control'
  changed_when: mac_control_hostname.stdout | trim != 'mac-control'

- name: Read the Remote Login state
  ansible.builtin.command: /usr/sbin/systemsetup -getremotelogin
  register: mac_control_remotelogin
  changed_when: false

- name: Enable Remote Login
  ansible.builtin.command: /usr/sbin/systemsetup -setremotelogin on
  when: "'On' not in mac_control_remotelogin.stdout"
  changed_when: "'On' not in mac_control_remotelogin.stdout"

- name: Read the Screen Sharing state
  ansible.builtin.command: /bin/launchctl print system/com.apple.screensharing
  register: mac_control_screensharing
  changed_when: false
  failed_when: false

# Screen Sharing over the tailnet is the remote GUI. It is not exposed to the
# LAN beyond what macOS does by default, and never to the internet.
- name: Enable Screen Sharing
  ansible.builtin.command: /bin/launchctl enable system/com.apple.screensharing
  when: mac_control_screensharing.rc != 0
  changed_when: mac_control_screensharing.rc != 0

- name: Read the auto-login user
  ansible.builtin.command: >-
    /usr/bin/defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser
  register: mac_control_autologin
  changed_when: false
  failed_when: false

# FileVault is deliberately OFF with auto-login ON, so an unplanned reboot
# always comes back unaided. FileVault's pre-boot prompt is unreachable by
# Screen Sharing and this machine has no readable screen — recovery would mean
# carrying a monitor to a closet. The SSD stays hardware-encrypted at rest by
# the Secure Enclave; what is given up is the password requirement at boot.
# The accepted risk is recorded in the spec.
- name: Set the auto-login user
  ansible.builtin.command: >-
    /usr/bin/defaults write /Library/Preferences/com.apple.loginwindow
    autoLoginUser {{ ansible_user }}
  when: mac_control_autologin.stdout | trim != ansible_user
  changed_when: mac_control_autologin.stdout | trim != ansible_user
```

- [ ] **Step 4: Write `tailscale.yml`**

```yaml
---
- name: Install Tailscale
  community.general.homebrew_cask:
    name: tailscale
    state: present
    path: /opt/homebrew/bin
  become: true
  become_user: "{{ ansible_user }}"

- name: Read the Tailscale backend state
  ansible.builtin.command: /Applications/Tailscale.app/Contents/MacOS/Tailscale status --json
  register: mac_control_tailscale
  changed_when: false
  failed_when: false

- name: Enrol in the tailnet
  ansible.builtin.command: >-
    /Applications/Tailscale.app/Contents/MacOS/Tailscale up
    --authkey {{ vault_tailscale_auth_key }}
    --hostname mac-control
  when: >-
    mac_control_tailscale.rc != 0
    or (mac_control_tailscale.stdout | from_json).BackendState != 'Running'
  changed_when: >-
    mac_control_tailscale.rc != 0
    or (mac_control_tailscale.stdout | from_json).BackendState != 'Running'
  no_log: true
```

- [ ] **Step 5: Wire them into `main.yml`**

In `roles/mac_control/tasks/main.yml`, insert between the prereqs import and the verify import:

```yaml
- name: Install control node packages
  ansible.builtin.import_tasks: packages.yml
  tags: [files]

- name: Configure unattended power behaviour
  ansible.builtin.import_tasks: power.yml
  tags: [files]

- name: Configure remote access
  ansible.builtin.import_tasks: remote-access.yml
  tags: [files]

- name: Enrol in the tailnet
  ansible.builtin.import_tasks: tailscale.yml
  tags: [files]
```

- [ ] **Step 6: Validate, then prove idempotence on the real host**

Run: `make validate`
Expected: PASS.

Run: `make mac`
Expected: converges.

Run: `make mac` again
Expected: **`changed=0`**. If it is not zero, find which task reported changed and fix its `changed_when` — do not proceed. A role that cannot report `changed=0` makes the whole estate's proof weaker, not just this host's.

- [ ] **Step 7: Commit**

```bash
git add roles/mac_control/tasks/packages.yml roles/mac_control/tasks/power.yml \
        roles/mac_control/tasks/remote-access.yml roles/mac_control/tasks/tailscale.yml \
        roles/mac_control/tasks/main.yml
git commit -m "feat: configure mac-control's base system

Homebrew packages, pmset for unattended operation, Remote Login, Screen
Sharing, hostname and Tailscale. Every setter reads its current value first
and applies only on a difference, so the second make mac reports changed=0.

FileVault stays off with auto-login on, deliberately: its pre-boot prompt is
unreachable by Screen Sharing and this machine has no readable screen, so an
unplanned reboot would otherwise leave it dark until someone carries a monitor
to the closet.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Ollama, bound to the LAN address

**Files:**
- Create: `tests/validate_launchd_plists.py`
- Create: `roles/mac_control/templates/homelab-ollama.plist.j2`
- Create: `roles/mac_control/tasks/ollama.yml`
- Modify: `roles/mac_control/tasks/main.yml`
- Modify: `Makefile` (`validate-systemd` gets the plist validator)

**Interfaces:**
- Consumes: `mac_control_lan_ip`, `mac_control_ollama_port`, `mac_control_models`, `mac_control_ollama_max_loaded_models`, `mac_control_ollama_keep_alive` (Task 4).
- Produces: Ollama listening on `192.168.1.41:11434` only; launchd job `com.homelab.ollama`.

- [ ] **Step 1: Write the failing test**

Create `tests/validate_launchd_plists.py`:

```python
#!/usr/bin/env python3
"""Parse every launchd plist this repo ships, on any platform.

tests/validate_systemd_units.py switches from text matching to real parsing
when systemd-analyze is on PATH — which on macOS it never is, so before CI
existed no systemd had ever parsed a unit here. launchd has the mirror
problem: `plutil -lint` exists only on macOS, and this repo's CI runs on
Linux, so a malformed plist would pass every gate and fail on the host.

plistlib is in the standard library and parses the same XML everywhere, so
the check runs identically on a workstation and in CI.

Beyond well-formedness it asserts the thing that actually matters for the
Ollama job: that the plist binds a specific address rather than 0.0.0.0.
docs/gpu-host.md tells you to set OLLAMA_HOST=0.0.0.0 on TERRA, which is how
that machine's models ended up answering unauthenticated to every tailnet
peer. Repeating it here would put the same hole on the host that holds
.vault_pass.

Templates are rendered with the Jinja this repo actually uses before parsing;
a plist that only parses after Ansible has run is a plist nobody checked.
"""

from __future__ import annotations

import plistlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "roles/mac_control/templates"

LAN_IP = "192.168.1.41"

SUBSTITUTIONS = {
    r"\{\{\s*ansible_managed[^}]*\}\}": "rendered for tests",
    r"\{\{\s*mac_control_lan_ip\s*\}\}": LAN_IP,
    r"\{\{\s*mac_control_ollama_port\s*\}\}": "11434",
    r"\{\{\s*mac_control_ollama_max_loaded_models\s*\}\}": "1",
    r"\{\{\s*mac_control_ollama_keep_alive\s*\}\}": "5m",
    r"\{\{\s*mac_control_home\s*\}\}": "/Users/fixture",
    r"\{\{\s*mac_control_checkout\s*\}\}": "/Users/fixture/homelab-iac",
    r"\{\{\s*ansible_user\s*\}\}": "fixture",
}


def render(text: str) -> str:
    for pattern, value in SUBSTITUTIONS.items():
        text = re.sub(pattern, value, text)
    leftover = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", text)
    if leftover:
        raise ValueError(
            "grew Jinja this test does not render: "
            + ", ".join(sorted(set(leftover)))
            + " — add it to SUBSTITUTIONS rather than loosening this check")
    return text


def main() -> int:
    plists = sorted(TEMPLATE_DIR.glob("*.plist.j2"))
    if not plists:
        print(
            f"no launchd plist templates found under {TEMPLATE_DIR}.\n"
            "A gate that finds nothing to check is a gate nobody can tell is broken.",
            file=sys.stderr)
        return 1

    failures = []
    for path in plists:
        try:
            rendered = render(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            failures.append(f"{path.name}: {exc}")
            continue
        try:
            parsed = plistlib.loads(rendered.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures.append(f"{path.name}: not a valid plist: {exc}")
            continue

        if "Label" not in parsed:
            failures.append(f"{path.name}: no Label key — launchd will refuse it")

        env = parsed.get("EnvironmentVariables", {})
        host = env.get("OLLAMA_HOST")
        if host is not None:
            if host.startswith("0.0.0.0") or host.startswith("*"):
                failures.append(
                    f"{path.name}: OLLAMA_HOST is {host!r}. Binding every interface "
                    "puts Ollama on the tailnet unauthenticated — the exposure that "
                    "already exists on TERRA. Bind the LAN address specifically.")
            elif not host.startswith(LAN_IP):
                failures.append(
                    f"{path.name}: OLLAMA_HOST is {host!r}, expected to start with {LAN_IP}")

    if failures:
        print("launchd plist regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print(f"launchd plists: OK ({len(plists)} parsed, Ollama bound to a specific address)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python tests/validate_launchd_plists.py`
Expected: FAIL — "no launchd plist templates found".

- [ ] **Step 3: Write the plist template**

Create `roles/mac_control/templates/homelab-ollama.plist.j2`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- {{ ansible_managed }} -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.homelab.ollama</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/ollama</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- The LAN address SPECIFICALLY, never 0.0.0.0. Ollama has no
         authentication of any kind, so its bind address is the whole of its
         access control. TERRA sets 0.0.0.0 and consequently answers to every
         tailnet peer; this host holds .vault_pass, so it does not. -->
    <key>OLLAMA_HOST</key>
    <string>{{ mac_control_lan_ip }}:{{ mac_control_ollama_port }}</string>
    <!-- Bounded so inference can never starve this machine's primary job as
         a control node and agentic host. -->
    <key>OLLAMA_MAX_LOADED_MODELS</key>
    <string>{{ mac_control_ollama_max_loaded_models }}</string>
    <key>OLLAMA_KEEP_ALIVE</key>
    <string>{{ mac_control_ollama_keep_alive }}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <!-- The bind address only exists while the USB-C adapter is attached.
       KeepAlive retries rather than giving up, so a reseated dongle recovers
       on its own; if it does not, the verify check goes red while SSH stays
       up over Wi-Fi. -->
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/var/log/homelab-ollama.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/homelab-ollama.log</string>
</dict>
</plist>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/validate_launchd_plists.py`
Expected: PASS — "launchd plists: OK (1 parsed, Ollama bound to a specific address)"

- [ ] **Step 5: Write the Ollama tasks**

Create `roles/mac_control/tasks/ollama.yml`:

```yaml
---
- name: Install Ollama
  community.general.homebrew:
    name: ollama
    state: present
    path: /opt/homebrew/bin
  become: true
  become_user: "{{ ansible_user }}"

- name: Install the Ollama launchd job
  ansible.builtin.template:
    src: homelab-ollama.plist.j2
    dest: /Library/LaunchDaemons/com.homelab.ollama.plist
    owner: root
    group: wheel
    mode: "0644"
  register: mac_control_ollama_plist

- name: Reload the Ollama launchd job
  ansible.builtin.command: >-
    /bin/launchctl bootout system/com.homelab.ollama
  when: mac_control_ollama_plist.changed
  changed_when: mac_control_ollama_plist.changed
  failed_when: false

- name: Load the Ollama launchd job
  ansible.builtin.command: >-
    /bin/launchctl bootstrap system /Library/LaunchDaemons/com.homelab.ollama.plist
  when: mac_control_ollama_plist.changed
  changed_when: mac_control_ollama_plist.changed
  failed_when: false

- name: Read the installed models
  ansible.builtin.command: /opt/homebrew/bin/ollama list
  environment:
    OLLAMA_HOST: "{{ mac_control_lan_ip }}:{{ mac_control_ollama_port }}"
  register: mac_control_ollama_list
  changed_when: false
  retries: 6
  delay: 5
  until: mac_control_ollama_list.rc == 0

- name: Pull the small always-on models
  ansible.builtin.command: "/opt/homebrew/bin/ollama pull {{ item }}"
  environment:
    OLLAMA_HOST: "{{ mac_control_lan_ip }}:{{ mac_control_ollama_port }}"
  loop: "{{ mac_control_models }}"
  when: item not in mac_control_ollama_list.stdout
  changed_when: item not in mac_control_ollama_list.stdout
```

In `roles/mac_control/tasks/main.yml`, add after the tailnet import:

```yaml
- name: Install and configure Ollama
  ansible.builtin.import_tasks: ollama.yml
  tags: [files]
```

In `Makefile`, add to `validate-systemd`:

```makefile
	$(PYTHON) tests/validate_launchd_plists.py
```

- [ ] **Step 6: Deploy and confirm the binding by hand**

Run: `make validate && make mac && make mac`
Expected: second run reports `changed=0`.

Now confirm the bind is what the plist claims — the plist is a statement of intent, and a running process is the fact:

```bash
ssh straderb@192.168.1.41 "curl -fsS http://192.168.1.41:11434/api/tags | head -c 200"   # expect JSON
ssh straderb@192.168.1.41 "curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags"       # expect FAILURE
```

- [ ] **Step 7: Commit**

```bash
git add tests/validate_launchd_plists.py roles/mac_control/templates/homelab-ollama.plist.j2 \
        roles/mac_control/tasks/ollama.yml roles/mac_control/tasks/main.yml Makefile
git commit -m "feat: run Ollama on mac-control, bound to the LAN address only

Ollama has no authentication, so its bind address is the whole of its access
control. TERRA sets 0.0.0.0 and answers to every tailnet peer as a result;
this host holds .vault_pass, so the plist binds 192.168.1.41 specifically and
a validator fails the build if that ever becomes 0.0.0.0.

The plist validator uses plistlib rather than plutil so it parses on Linux CI
too — the same gap validate_systemd_units.py had in the other direction.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: The tri-state Ollama binding check

A check that asserts "the tailnet address is silent" passes trivially when the service is dead or the host is off. This one asserts the positive control first.

**Files:**
- Create: `roles/mac_control/templates/ollama-binding-check.sh.j2`
- Test: `tests/validate_ollama_binding_check.py`
- Modify: `Makefile` (`validate-ci`)

**Interfaces:**
- Consumes: `mac_control_lan_ip`, `mac_control_ollama_port` (Task 4).
- Produces: script printing exactly one of `verdict=ok`, `verdict=exposed`, `verdict=inconclusive`; exit 0 for `ok`, 1 for `exposed`, 2 for `inconclusive`.

- [ ] **Step 1: Write the failing test**

Create `tests/validate_ollama_binding_check.py`:

```python
#!/usr/bin/env python3
"""Exercise all three verdicts of the Ollama binding check.

The check answers one question: is Ollama reachable on the tailnet, where it
would be unauthenticated to every peer? The naive form — "curl the tailnet
address, pass if it does not answer" — passes for the wrong reason far more
often than the right one. A stopped Ollama passes it. An unplugged USB-C
adapter passes it. TERRA switched off passes it, and TERRA is off half the
time.

So the LAN address must answer FIRST. Only against a service that is
demonstrably alive does a silent tailnet address mean anything.

Three cases, against a stub curl:

  LAN answers, tailnet silent    verdict=ok             correctly scoped
  LAN answers, tailnet answers   verdict=exposed        the finding
  LAN silent                     verdict=inconclusive   could not look

The third is the one this exists for. Before the credential probes got a
three-state verdict in 057e1e4, a connection refused rendered as the word
`ok`, and a check that cannot tell "clean" from "I could not look" is a check
nobody can tell is broken.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "roles/mac_control/templates/ollama-binding-check.sh.j2"

LAN = "192.168.1.41"
TAILNET = "100.64.0.41"


def render() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{ ansible_managed | comment }}", "# (rendered for tests)")
    text = re.sub(r"\{\{\s*mac_control_ollama_port\s*\}\}", "11434", text)
    leftover = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", text)
    if leftover:
        raise SystemExit(
            "ollama-binding-check.sh.j2 grew Jinja this test does not render: "
            + ", ".join(sorted(set(leftover))))
    return text


def stub_curl(directory: Path, answering: set[str]) -> None:
    """A curl that succeeds only for addresses in `answering`."""
    hosts = " ".join(sorted(answering))
    script = f"""#!/usr/bin/env bash
for arg in "$@"; do
  for host in {hosts}; do
    case "$arg" in
      *"$host"*) echo '{{"models":[]}}'; exit 0 ;;
    esac
  done
done
exit 7
"""
    path = directory / "curl"
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


CASES = [
    ("LAN answers, tailnet silent", {LAN}, "ok", 0),
    ("LAN answers, tailnet answers", {LAN, TAILNET}, "exposed", 1),
    ("LAN silent", set(), "inconclusive", 2),
]


def main() -> int:
    rendered = render()
    failures = []

    for name, answering, want_verdict, want_rc in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            stub_curl(tmpdir, answering)
            script = tmpdir / "check.sh"
            script.write_text(rendered, encoding="utf-8")

            env = dict(os.environ, PATH=f"{tmpdir}:{os.environ['PATH']}")
            completed = subprocess.run(
                ["bash", str(script), LAN, TAILNET],
                capture_output=True, text=True, env=env,
            )
            out = completed.stdout + completed.stderr
            if f"verdict={want_verdict}" not in out:
                failures.append(
                    f"{name}: expected verdict={want_verdict}, got: {out.strip()!r}")
            if completed.returncode != want_rc:
                failures.append(
                    f"{name}: expected exit {want_rc}, got {completed.returncode}")

    if failures:
        print("ollama binding check regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print("ollama binding check: OK (3 cases, including that a dead service "
          "reads as inconclusive rather than ok)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python tests/validate_ollama_binding_check.py`
Expected: FAIL — the template does not exist.

- [ ] **Step 3: Write the check**

Create `roles/mac_control/templates/ollama-binding-check.sh.j2`:

```bash
#!/usr/bin/env bash
{{ ansible_managed | comment }}
#
# Is Ollama reachable on the tailnet, where it would answer unauthenticated to
# every peer? Ollama has no authentication at all, so the bind address is the
# whole of its access control.
#
# The LAN address must answer FIRST. Without that positive control this check
# passes for the wrong reason most of the time: a stopped Ollama, an unplugged
# USB-C adapter, or a host that is simply switched off all produce a silent
# tailnet address, and all would render as "correctly scoped".
#
# Both addresses are ARGUMENTS rather than baked in, so the same check serves
# mac-control and TERRA. TERRA is the one that needs the tri-state most: it is
# switched off half the time, and an off machine answers nothing at all.
#
# Usage: ollama-binding-check.sh <lan-address> <tailnet-address>
#
# Exit codes are the verdict:
#   0  ok            LAN answers, tailnet silent
#   1  exposed       both answer — the finding
#   2  inconclusive  LAN does not answer, so nothing can be concluded
set -euo pipefail

lan_addr=${1:?usage: ollama-binding-check.sh <lan-address> <tailnet-address>}
tailnet_addr=${2:?usage: ollama-binding-check.sh <lan-address> <tailnet-address>}
lan_url="http://${lan_addr}:{{ mac_control_ollama_port }}/api/tags"
tailnet_url="http://${tailnet_addr}:{{ mac_control_ollama_port }}/api/tags"

if ! curl -fsS --max-time 5 "$lan_url" >/dev/null 2>&1; then
  echo "verdict=inconclusive reason=lan-address-did-not-answer url=${lan_url}"
  exit 2
fi

if curl -fsS --max-time 5 "$tailnet_url" >/dev/null 2>&1; then
  echo "verdict=exposed reason=answers-on-tailnet url=${tailnet_url}"
  exit 1
fi

echo "verdict=ok reason=lan-only lan=${lan_url}"
exit 0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/validate_ollama_binding_check.py`
Expected: PASS — "ollama binding check: OK (3 cases, ...)"

- [ ] **Step 5: Add the gate to the Makefile**

In `validate-ci`:

```makefile
	$(PYTHON) tests/validate_ollama_binding_check.py
```

- [ ] **Step 6: Validate**

Run: `make validate`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add roles/mac_control/templates/ollama-binding-check.sh.j2 \
        tests/validate_ollama_binding_check.py Makefile
git commit -m "feat: add a tri-state Ollama binding check

'The tailnet address does not answer' passes trivially when the service is
dead, the dongle is unplugged, or the host is off. The LAN address must answer
first; only then does a silent tailnet address mean anything. A dead service
now reads as inconclusive rather than ok, the same three-state verdict 057e1e4
gave the credential probes and for the same reason.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Claude Code, superpowers, and persistent tmux sessions

**Files:**
- Create: `roles/mac_control/tasks/agent.yml`
- Create: `roles/mac_control/templates/homelab-tmux.plist.j2`
- Modify: `roles/mac_control/tasks/main.yml`

**Interfaces:**
- Consumes: `mac_control_checkout`, `mac_control_home` (Task 1); node from `mac_control_brew_packages` (Task 4).
- Produces: `claude` on PATH; the superpowers plugin present; launchd agent `com.homelab.tmux` creating a `homelab` session at login.

- [ ] **Step 1: Write the tmux launchd agent**

Create `roles/mac_control/templates/homelab-tmux.plist.j2`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- {{ ansible_managed }} -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.homelab.tmux</string>
  <!-- A long-lived session rooted in the checkout, so SSH-in-and-attach picks
       up exactly where the last one left off. This is the thing that makes an
       always-on agentic host worth having: closing the working laptop's lid
       currently ends any session outright. -->
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/tmux</string>
    <string>new-session</string>
    <string>-d</string>
    <string>-s</string>
    <string>homelab</string>
    <string>-c</string>
    <string>{{ mac_control_checkout }}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <!-- Deliberately NOT KeepAlive: tmux forks a server and exits, so launchd
       would restart it forever and destroy the session it just made. -->
  <key>KeepAlive</key>
  <false/>
</dict>
</plist>
```

- [ ] **Step 2: Write the agent tasks**

Create `roles/mac_control/tasks/agent.yml`:

```yaml
---
- name: Check for the Claude Code CLI
  ansible.builtin.command: /opt/homebrew/bin/npm ls -g --depth 0 @anthropic-ai/claude-code
  register: mac_control_claude
  changed_when: false
  failed_when: false
  become: true
  become_user: "{{ ansible_user }}"

- name: Install the Claude Code CLI
  ansible.builtin.command: /opt/homebrew/bin/npm install -g @anthropic-ai/claude-code
  when: mac_control_claude.rc != 0
  changed_when: mac_control_claude.rc != 0
  become: true
  become_user: "{{ ansible_user }}"

# superpowers is a Claude Code PLUGIN, not a VS Code extension — it attaches to
# the CLI and lives under ~/.claude/plugins. Anywhere the CLI runs, it runs.
- name: Check for the superpowers plugin
  ansible.builtin.stat:
    path: "{{ mac_control_home }}/.claude/plugins/cache/superpowers-marketplace"
  register: mac_control_superpowers

# A half-installed agent host that LOOKS configured is worse than one that
# refuses to come up, so this asserts rather than attempting a fix it cannot
# verify. The install is an interactive `/plugin` step, documented in
# docs/mac-control-node.md.
- name: Require the superpowers plugin
  ansible.builtin.assert:
    that:
      - mac_control_superpowers.stat.exists
    fail_msg: >-
      The superpowers plugin is not installed for {{ ansible_user }}. Attach to
      mac-control (ssh + tmux), run `claude`, and install it with `/plugin`.
      See docs/mac-control-node.md.

- name: Install the tmux session launchd agent
  ansible.builtin.template:
    src: homelab-tmux.plist.j2
    dest: "{{ mac_control_home }}/Library/LaunchAgents/com.homelab.tmux.plist"
    owner: "{{ ansible_user }}"
    group: staff
    mode: "0644"
```

In `roles/mac_control/tasks/main.yml`, add after the Ollama import:

```yaml
- name: Configure the agentic host
  ansible.builtin.import_tasks: agent.yml
  tags: [files]
```

- [ ] **Step 3: Validate**

Run: `make validate`
Expected: PASS — the plist validator now parses two templates.

- [ ] **Step 4: Deploy and complete the manual plugin step**

Run: `make mac`
Expected: FAILS on the superpowers assert the first time, with the instruction.

Then, on mac-control:

```bash
ssh straderb@192.168.1.41
tmux new -s bootstrap
claude          # then: /plugin  -> install superpowers
```

Run: `make mac`
Expected: passes.

Run: `make mac` again
Expected: `changed=0`.

- [ ] **Step 5: Confirm the session actually survives**

```bash
ssh straderb@192.168.1.41 "/opt/homebrew/bin/tmux ls"
```
Expected: a `homelab` session listed. If it is absent after a reboot, the launchd agent did not load — check `launchctl print gui/$(id -u straderb)/com.homelab.tmux` on the host.

- [ ] **Step 6: Commit**

```bash
git add roles/mac_control/tasks/agent.yml \
        roles/mac_control/templates/homelab-tmux.plist.j2 \
        roles/mac_control/tasks/main.yml
git commit -m "feat: install Claude Code and a persistent tmux session on mac-control

superpowers is a CLI plugin rather than an editor extension, so it needs no
code-server. The role asserts the plugin is present instead of pretending to
install something it cannot verify — a half-installed agent host that looks
configured is worse than one that refuses to come up.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: The `pre-push` hook that refuses `main`

CLAUDE.md's "push after committing without waiting to be asked" was written for a human-driven session. On an always-on box it means an agent pushing to `main` while you sleep.

**Files:**
- Create: `roles/mac_control/files/pre-push`
- Create: `roles/mac_control/tasks/git-guard.yml`
- Modify: `roles/mac_control/tasks/main.yml`
- Test: `tests/validate_push_guard.py`
- Modify: `Makefile` (`validate-ci`)

**Interfaces:**
- Consumes: `mac_control_checkout` (Task 1).
- Produces: `.git/hooks/pre-push` on mac-control; exit 1 for `refs/heads/main`, exit 0 otherwise.

- [ ] **Step 1: Write the failing test**

Create `tests/validate_push_guard.py`:

```python
#!/usr/bin/env python3
"""Prove the pre-push hook refuses main and permits everything else.

mac-control commits and pushes branches freely; merges to main happen from the
working laptop, where a human is watching. The hook is what makes that a rule
rather than an intention.

Both halves need testing. A hook that rejects everything would "pass" a test
that only checks main is refused — and it would make the machine useless for
the branch work that is the whole point of it.

git feeds pre-push one line per ref on stdin:
  <local ref> <local sha> <remote ref> <remote sha>

This is a guardrail, not a boundary: `git push --no-verify` walks past it, and
the spec says so plainly. It stops carelessness, which is the realistic risk
on a machine running unattended agent sessions at 3am.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "roles/mac_control/files/pre-push"

SHA_A = "1111111111111111111111111111111111111111"
SHA_B = "2222222222222222222222222222222222222222"

CASES = [
    ("push to main", f"refs/heads/main {SHA_A} refs/heads/main {SHA_B}\n", 1),
    ("push to a feature branch",
     f"refs/heads/feat/x {SHA_A} refs/heads/feat/x {SHA_B}\n", 0),
    ("a batch containing main",
     f"refs/heads/feat/x {SHA_A} refs/heads/feat/x {SHA_B}\n"
     f"refs/heads/main {SHA_A} refs/heads/main {SHA_B}\n", 1),
]


def main() -> int:
    if not HOOK.exists():
        print(f"hook not found at {HOOK}", file=sys.stderr)
        return 1

    failures = []
    for name, stdin, want_rc in CASES:
        completed = subprocess.run(
            ["bash", str(HOOK), "origin", "git@example:repo.git"],
            input=stdin, capture_output=True, text=True,
        )
        got = 1 if completed.returncode != 0 else 0
        if got != want_rc:
            failures.append(
                f"{name}: expected {'rejection' if want_rc else 'acceptance'}, "
                f"got exit {completed.returncode}. "
                f"stderr: {completed.stderr.strip()!r}")

    if failures:
        print("push guard regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print("push guard: OK (3 cases, including that feature branches are still allowed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python tests/validate_push_guard.py`
Expected: FAIL — "hook not found".

- [ ] **Step 3: Write the hook**

Create `roles/mac_control/files/pre-push`:

```bash
#!/usr/bin/env bash
# Managed by Ansible (roles/mac_control). Do not edit on the host.
#
# mac-control never pushes main.
#
# CLAUDE.md's standing rule is "push after committing without waiting to be
# asked". That was written for a session with a human in it. This machine runs
# unattended agent sessions, so here the rule is amended: branches are pushed
# freely, and merges to main happen from the working laptop.
#
# This is a guardrail, not a boundary — `git push --no-verify` walks past it.
# It stops carelessness, which is the realistic failure at 3am. The boundary
# case was settled when this machine was given .vault_pass.
set -euo pipefail

protected_ref="refs/heads/main"

while read -r _local_ref _local_sha remote_ref _remote_sha; do
  if [ "$remote_ref" = "$protected_ref" ]; then
    echo "pre-push: refusing to push ${protected_ref} from mac-control." >&2
    echo "          Merges to main happen from the working laptop." >&2
    exit 1
  fi
done

exit 0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/validate_push_guard.py`
Expected: PASS — "push guard: OK (3 cases, ...)"

- [ ] **Step 5: Install it on the host**

Create `roles/mac_control/tasks/git-guard.yml`:

```yaml
---
- name: Install the pre-push guard in the control node's checkout
  ansible.builtin.copy:
    src: pre-push
    dest: "{{ mac_control_checkout }}/.git/hooks/pre-push"
    owner: "{{ ansible_user }}"
    group: staff
    mode: "0755"
```

In `roles/mac_control/tasks/main.yml`, add after the agent import:

```yaml
- name: Guard the control node's git remote
  ansible.builtin.import_tasks: git-guard.yml
  tags: [files]
```

In `Makefile`, add to `validate-ci`:

```makefile
	$(PYTHON) tests/validate_push_guard.py
```

- [ ] **Step 6: Deploy and prove the refusal on the host**

Run: `make validate && make mac && make mac`
Expected: second run reports `changed=0`.

Prove it where it matters — a hook that exists and a hook that fires are different claims:

```bash
ssh straderb@192.168.1.41 "cd ~/homelab-iac && git push --dry-run origin HEAD:main"
```
Expected: FAILS with "refusing to push refs/heads/main from mac-control".

- [ ] **Step 7: Commit**

```bash
git add roles/mac_control/files/pre-push roles/mac_control/tasks/git-guard.yml \
        roles/mac_control/tasks/main.yml tests/validate_push_guard.py Makefile
git commit -m "feat: refuse pushes to main from mac-control

CLAUDE.md's auto-push rule assumes a human in the session. This machine runs
unattended agents, so branches push freely and merges to main stay on the
working laptop. Tested in both directions: a hook that rejected everything
would pass a main-only test and make the machine useless for branch work.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: The weekly release-report reader

The mechanism for scheduled agentic work, plus exactly one job — with a dead-man's switch, because a scheduled agent that stops running is otherwise invisible.

**Files:**
- Create: `roles/mac_control/templates/release-reader.sh.j2`
- Create: `roles/mac_control/templates/homelab-release-reader.plist.j2`
- Create: `roles/mac_control/tasks/scheduled-agent.yml`
- Modify: `roles/mac_control/tasks/main.yml`
- Modify: `inventory/group_vars/all_vault.yml.example`

**Interfaces:**
- Consumes: `mac_control_checkout`, `healthchecks_base_url`, new vault var `vault_hc_ping_mac_release`.
- Produces: launchd job `com.homelab.release-reader` firing Fridays at 09:30 (an hour after `make release-check` publishes at 08:30).

- [ ] **Step 1: Write the reader script**

Create `roles/mac_control/templates/release-reader.sh.j2`:

```bash
#!/usr/bin/env bash
{{ ansible_managed | comment }}
#
# Read the weekly release report and draft a recommendation.
#
# `make release-check` runs Friday 08:30 on svc-infra and publishes 30 images'
# release notes to ntfy and scan.<domain>/releases.txt. Nothing reads them.
# CLAUDE.md names that failure three separate ways — a report nobody opens is
# the same "nobody looks at it" problem this repo worries about everywhere.
#
# It DELIBERATELY PRINTS NO BUMP COMMAND. For an untracked image that would be
# the exact standing recommendation the BUMP PROCEDURE block exists to
# prevent, and an agent is more likely to produce one than a script is. The
# output is a document to read, not a change to apply.
#
# The healthchecks.io ping at the end is the point of the whole design: a
# scheduled agent that stops running produces no error and no red build, it
# just goes quiet. The ping is what turns that into an alarm.
set -euo pipefail

checkout={{ mac_control_checkout | quote }}
report_dir="${checkout}/local-backups/release-reviews"
stamp=$(date -u +%Y-%m-%d)
outfile="${report_dir}/${stamp}-release-review.md"

mkdir -p "$report_dir"
cd "$checkout"

/opt/homebrew/bin/claude -p "$(cat <<'PROMPT'
Read the latest weekly release report for this estate: fetch
https://scan.{{ service_domain }}/releases.txt and, for every image reported
`behind`, read that project's release notes for the versions between the
pinned one and the newest.

Write a recommendation document. For each image: what changed, whether it
touches data the service persists, and whether you would bump it now, later,
or not at all — with the reason.

Do NOT print any `make image-bump` command, and do not edit any file in
inventory/. This is a document to read, not a change to apply. An untracked
image especially must not get a standing bump recommendation; read
inventory/group_vars/all/apps.yml's BUMP PROCEDURE block for why.

Note explicitly any image where you could not find release notes, rather than
omitting it. "I could not look" is a finding.
PROMPT
)" > "$outfile"

echo "release review written to ${outfile}"

# Dead-man's switch. Without this, a job that silently stopped firing would
# look exactly like an estate with nothing to report.
if [ -n "${HC_PING_MAC_RELEASE:-}" ]; then
  curl -fsS --max-time 10 --retry 3 "$HC_PING_MAC_RELEASE" >/dev/null || \
    echo "WARNING: healthchecks ping failed — the dead-man's switch is not armed" >&2
else
  echo "WARNING: HC_PING_MAC_RELEASE is unset; this job has no dead-man's switch" >&2
fi
```

- [ ] **Step 2: Write the launchd job**

Create `roles/mac_control/templates/homelab-release-reader.plist.j2`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- {{ ansible_managed }} -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.homelab.release-reader</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{{ mac_control_home }}/bin/release-reader.sh</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HC_PING_MAC_RELEASE</key>
    <string>{{ vault_hc_ping_mac_release | default('') }}</string>
  </dict>
  <!-- Friday 09:30 — an hour after make release-check publishes at 08:30. -->
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>5</integer>
    <key>Hour</key><integer>9</integer>
    <key>Minute</key><integer>30</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/var/log/homelab-release-reader.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/homelab-release-reader.log</string>
</dict>
</plist>
```

- [ ] **Step 3: Write the tasks**

Create `roles/mac_control/tasks/scheduled-agent.yml`:

```yaml
---
- name: Create the control node's script directory
  ansible.builtin.file:
    path: "{{ mac_control_home }}/bin"
    state: directory
    owner: "{{ ansible_user }}"
    group: staff
    mode: "0755"

- name: Install the release reader script
  ansible.builtin.template:
    src: release-reader.sh.j2
    dest: "{{ mac_control_home }}/bin/release-reader.sh"
    owner: "{{ ansible_user }}"
    group: staff
    mode: "0755"

- name: Install the release reader launchd job
  ansible.builtin.template:
    src: homelab-release-reader.plist.j2
    dest: "{{ mac_control_home }}/Library/LaunchAgents/com.homelab.release-reader.plist"
    owner: "{{ ansible_user }}"
    group: staff
    mode: "0644"
  no_log: true
```

In `roles/mac_control/tasks/main.yml`, add after the git-guard import:

```yaml
- name: Schedule the weekly release reader
  ansible.builtin.import_tasks: scheduled-agent.yml
  tags: [files]
```

Add to `inventory/group_vars/all_vault.yml.example`, in the healthchecks block near line 255:

```yaml
# mac-control's weekly release reader. A scheduled agent that stops running
# produces no error and no red build — it just goes quiet. This is the only
# thing that turns that into an alarm.
vault_hc_ping_mac_release: "REPLACE_healthchecks_ping_url"
```

- [ ] **Step 4: Validate and deploy**

Run: `make validate && make mac && make mac`
Expected: second run reports `changed=0`.

- [ ] **Step 5: Prove the job runs, rather than assuming it will**

```bash
ssh straderb@192.168.1.41 \
  "launchctl kickstart -k gui/\$(id -u straderb)/com.homelab.release-reader"
sleep 120
ssh straderb@192.168.1.41 "ls -l ~/homelab-iac/local-backups/release-reviews/"
```
Expected: a dated review document exists. Then confirm the ping landed by checking that healthchecks.io check went green — an unarmed dead-man's switch is the failure this whole design is built around.

- [ ] **Step 6: Commit**

```bash
git add roles/mac_control/templates/release-reader.sh.j2 \
        roles/mac_control/templates/homelab-release-reader.plist.j2 \
        roles/mac_control/tasks/scheduled-agent.yml roles/mac_control/tasks/main.yml \
        inventory/group_vars/all_vault.yml.example
git commit -m "feat: schedule the weekly release-report reader on mac-control

make release-check publishes 30 images' release notes every Friday and nothing
reads them. This does, and writes a recommendation document — deliberately
printing no bump command, because for an untracked image that is exactly the
standing recommendation BUMP PROCEDURE exists to prevent.

One job, not five: a scheduled agent that stops firing is invisible, so it
pings healthchecks.io and no second job is added until this one has a record
of firing reliably.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: Open WebUI's second Ollama endpoint

The catalog change is a first-boot seed, not enforced state. [infra-apps.yml:564-578](../../../inventory/group_vars/all/infra-apps.yml#L564-L578) is explicit that a DB row beats the environment and `changed=0` still reports success.

**Files:**
- Modify: `inventory/group_vars/all/main.yml` (add `mac_host_online`)
- Modify: `inventory/group_vars/all/infra-apps.yml` (open-webui env)
- Create: `tests/validate_openwebui_endpoints.py`
- Modify: `Makefile` (`validate-catalog`)

**Interfaces:**
- Consumes: `mac_control_lan_ip` concept via `hostvars['mac-control'].ansible_host` (Task 1); relocated models (Task 6).
- Produces: `mac_host_online` boolean; Open WebUI reaching both Ollama endpoints.

- [ ] **Step 1: Write the failing test**

Create `tests/validate_openwebui_endpoints.py`:

```python
#!/usr/bin/env python3
"""Assert Open WebUI's Ollama wiring names both hosts and gates on either.

Two failure modes, and the first is silent in a way this repo has learned to
distrust.

ENABLE_PERSISTENT_CONFIG is true, so a DB row beats the environment: editing
OLLAMA_BASE_URLS here and running `make infra` does nothing for a key that has
ever been touched in the admin UI — no error, and changed=0 still reports
success. This gate cannot fix that (only the round-trip check in verify.yml
can), but it can make sure the seed is at least correct, so a rebuild from a
clean database gets both endpoints.

Second: ENABLE_OLLAMA_API must be gated on EITHER host being online. Gated on
gpu_host_online alone, switching TERRA off would also switch off the models
mac-control is serving — which are the always-on ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "inventory/group_vars/all/infra-apps.yml"


def main() -> int:
    text = CATALOG.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)

    app = None
    for group in loaded.values():
        if isinstance(group, dict) and "open-webui" in group:
            app = group["open-webui"]
            break

    if app is None:
        print("open-webui not found in the infra catalog", file=sys.stderr)
        return 1

    env = app.get("env", {})
    failures = []

    urls = str(env.get("OLLAMA_BASE_URLS", ""))
    if "gpu_host_ip" not in urls:
        failures.append("OLLAMA_BASE_URLS does not reference gpu_host_ip (TERRA)")
    if "mac-control" not in urls:
        failures.append(
            "OLLAMA_BASE_URLS does not reference mac-control. Its two models are "
            "the always-on ones; without this endpoint they are unreachable.")

    api = str(env.get("ENABLE_OLLAMA_API", ""))
    if "gpu_host_online" not in api or "mac_host_online" not in api:
        failures.append(
            "ENABLE_OLLAMA_API must be gated on gpu_host_online OR mac_host_online. "
            "Gated on TERRA alone, switching that PC off also switches off the "
            "models mac-control serves.")

    image = str(env.get("ENABLE_IMAGE_GENERATION", ""))
    if "mac_host_online" in image:
        failures.append(
            "ENABLE_IMAGE_GENERATION must stay tied to gpu_host_online alone — "
            "ComfyUI runs only on TERRA.")

    if failures:
        print("Open WebUI endpoint regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print("open-webui endpoints: OK (both Ollama hosts seeded, API gated on either)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python tests/validate_openwebui_endpoints.py`
Expected: FAIL — mac-control is not referenced and `mac_host_online` does not exist.

- [ ] **Step 3: Add the variable and the catalog entries**

In `inventory/group_vars/all/main.yml`, immediately after `gpu_host_online`:

```yaml
# --- mac-control's Ollama endpoint ---
# Mirrors gpu_host_online for the second inference host. While false, Open
# WebUI drops that endpoint rather than showing dead-backend errors.
#
# Unlike TERRA, this machine is always on by design — so if this is false for
# more than a maintenance window, something is wrong rather than merely off.
mac_host_online: true
```

In `inventory/group_vars/all/infra-apps.yml`, inside the `open-webui` `env` block, replace the existing `OLLAMA_BASE_URLS` and `ENABLE_OLLAMA_API` entries with:

```yaml
      # TWO inference hosts. TERRA (the 4090) serves the chat and coding
      # models; mac-control serves the two small always-on ones, which moved
      # there precisely because TERRA gets gamed on and switched off.
      #
      # SEED ONLY. ENABLE_PERSISTENT_CONFIG is true, so if a DB row exists for
      # this key the environment is IGNORED and `make infra` will report
      # changed=0 while doing nothing. verify.yml proves the endpoint is
      # actually registered by listing a model only mac-control serves — do
      # not trust this line alone.
      OLLAMA_BASE_URLS: >-
        {{ ([('http://' ~ gpu_host_ip ~ ':11434') if gpu_host_online | bool else '']
            + [('http://' ~ hostvars['mac-control'].ansible_host ~ ':11434')
               if mac_host_online | bool else ''])
           | reject('equalto', '') | join(';') }}
      # Gated on EITHER host. Tied to gpu_host_online alone, switching TERRA
      # off would also switch off the always-on models.
      ENABLE_OLLAMA_API: "{{ (gpu_host_online | bool or mac_host_online | bool) | lower }}"
```

In `Makefile`, add to `validate-catalog`:

```makefile
	$(PYTHON) tests/validate_openwebui_endpoints.py
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/validate_openwebui_endpoints.py`
Expected: PASS — "open-webui endpoints: OK (...)"

- [ ] **Step 5: Deploy, then check whether the deploy actually did anything**

Read your domain out of `inventory/group_vars/all/main.yml` (`service_domain`) and use it below.

Run: `make validate && make infra`
Expected: `changed=0` or a Quadlet change — and **this tells you nothing about whether the endpoint registered.** Check the database's opinion, which is the one that counts:

```bash
curl -fsS https://chat.$SERVICE_DOMAIN/api/v1/configs/export | python3 -m json.tool | grep -i ollama
```

If mac-control's URL is absent, add it through the admin UI (Settings → Connections → Ollama) — the DB row is winning, exactly as the catalog comment warns.

- [ ] **Step 6: Complete the relocation by removing the models from TERRA**

The models were *relocated*, not duplicated, and this is the step that makes that true. It is also what makes the next step a real proof: while `nomic-embed-text` still exists on TERRA, its appearance in Open WebUI's model list proves nothing about mac-control.

On TERRA:

```powershell
ollama rm qwen2.5-coder:1.5b-base
ollama rm nomic-embed-text
ollama list          # confirm both are gone
```

Repoint Continue at `http://192.168.1.41:11434` for autocomplete and embeddings — its config lives on whatever machine runs your editor, not in this repo, so this is a hand edit and there is no gate that will notice if you skip it.

- [ ] **Step 7: Prove the round trip**

```bash
curl -fsS https://chat.$SERVICE_DOMAIN/api/models | grep nomic-embed-text
```
Expected: present. That model now lives **only** on mac-control, so its appearance is proof the second endpoint works — it cannot be satisfied by a stale config row, and it would have been meaningless before Step 6.

- [ ] **Step 8: Commit**

```bash
git add inventory/group_vars/all/main.yml inventory/group_vars/all/infra-apps.yml \
        tests/validate_openwebui_endpoints.py Makefile
git commit -m "feat: give Open WebUI a second Ollama endpoint on mac-control

Gated on either host being online: tied to gpu_host_online alone, switching
TERRA off would also switch off the always-on models that moved to mac-control
precisely because TERRA gets switched off.

The env value is a seed, not enforced state — ENABLE_PERSISTENT_CONFIG means a
DB row wins and changed=0 still reports success. Proof lives in verify.yml as a
model-list round trip instead.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 12: Verification wiring and the nightly self-check

**Files:**
- Modify: `roles/mac_control/tasks/verify.yml`
- Create: `roles/mac_control/templates/selfcheck.sh.j2`
- Create: `roles/mac_control/templates/homelab-selfcheck.plist.j2`
- Modify: `roles/mac_control/tasks/main.yml`
- Modify: `site.yml` (control_nodes play, announce assert)
- Modify: `verify.yml` (control_nodes play)
- Modify: `inventory/group_vars/all_vault.yml.example`

**Interfaces:**
- Consumes: everything from Tasks 4-11.
- Produces: fact `mac_control_verified`; launchd job `com.homelab.selfcheck` (nightly 03:00); vault var `vault_hc_ping_mac_selfcheck`.

- [ ] **Step 1: Fill in the role's verify entry point**

Replace `roles/mac_control/tasks/verify.yml` with:

```yaml
---
# Ollama must answer on the LAN address. This is the positive control for the
# binding assertion below — without it, a stopped Ollama would satisfy "not
# reachable on the tailnet" and read as correctly scoped.
- name: Probe Ollama on the LAN address
  ansible.builtin.uri:
    url: "http://{{ mac_control_lan_ip }}:{{ mac_control_ollama_port }}/api/tags"
    return_content: true
  register: mac_control_ollama_probe
  failed_when: false
  changed_when: false

- name: Assert Ollama answers on the LAN address
  ansible.builtin.assert:
    that:
      - mac_control_ollama_probe.status == 200
    fail_msg: >-
      Ollama is not answering on {{ mac_control_lan_ip }}:{{ mac_control_ollama_port }}.
      Most likely the USB-C 1GbE adapter has dropped, so the bind address no
      longer exists — SSH is reaching this host over Wi-Fi. Check the dongle
      before anything else.

- name: Assert the relocated models are present
  ansible.builtin.assert:
    that:
      - item in mac_control_ollama_probe.content
    fail_msg: >-
      {{ item }} is missing from mac-control. Open WebUI's second endpoint is
      proved by listing a model only this host serves, so a missing model makes
      that check unfalsifiable rather than merely red.
  loop: "{{ mac_control_models }}"

- name: Read this host's tailnet address
  ansible.builtin.command: /Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4
  register: mac_control_tailnet_ip
  changed_when: false

# Runs the copy already installed on the host by scheduled-agent.yml, which
# imports earlier in main.yml — so verification exercises the same script the
# nightly self-check runs, rather than a second rendering that could drift.
- name: Run the tri-state Ollama binding check
  ansible.builtin.command:
    cmd: >-
      {{ mac_control_home }}/bin/ollama-binding-check.sh
      {{ mac_control_lan_ip }} {{ mac_control_tailnet_ip.stdout | trim }}
  register: mac_control_binding
  changed_when: false
  failed_when: false

- name: Assert Ollama is not exposed on the tailnet
  ansible.builtin.assert:
    that:
      - "'verdict=ok' in mac_control_binding.stdout"
    fail_msg: >-
      Ollama binding check returned: {{ mac_control_binding.stdout | trim }}.
      `exposed` means it answers unauthenticated to every tailnet peer on the
      machine holding .vault_pass. `inconclusive` means the check could not
      look, which is not an all-clear.

- name: Stat the out-of-band secret files
  ansible.builtin.stat:
    path: "{{ item }}"
  loop:
    - "{{ mac_control_checkout }}/.vault_pass"
    - "{{ mac_control_checkout }}/inventory/group_vars/all/vault.yml"
  register: mac_control_secret_stat

- name: Assert the secret files are still mode 0600
  ansible.builtin.assert:
    that:
      - item.stat.exists
      - item.stat.mode == '0600'
    fail_msg: "{{ item.item }} must exist at mode 0600."
  loop: "{{ mac_control_secret_stat.results }}"
  loop_control:
    label: "{{ item.item }}"

# A hook that exists and a hook that fires are different claims, and only the
# second one is worth checking.
- name: Assert the pre-push guard actually refuses main
  ansible.builtin.command:
    cmd: git push --dry-run origin HEAD:main
    chdir: "{{ mac_control_checkout }}"
  become: true
  become_user: "{{ ansible_user }}"
  register: mac_control_push_guard
  changed_when: false
  failed_when: mac_control_push_guard.rc == 0

- name: Read the power settings
  ansible.builtin.command: /usr/bin/pmset -g custom
  register: mac_control_pmset_verify
  changed_when: false

- name: Assert the machine will not sleep and will return after a power cut
  ansible.builtin.assert:
    that:
      - mac_control_pmset_verify.stdout is search('autorestart\s+1')
      - mac_control_pmset_verify.stdout is search('disablesleep\s+1')
    fail_msg: >-
      pmset is not configured for unattended operation. Without autorestart a
      power cut leaves this machine dark, and the only symptom is a dead-man's
      switch going quiet.

- name: Mark control node verification complete
  ansible.builtin.set_fact:
    mac_control_verified: true
```

No extra rendering task is needed: `scheduled-agent.yml` already installs `ollama-binding-check.sh` to `{{ mac_control_home }}/bin/` on the host, and it imports before `verify.yml` in `main.yml`. Verification therefore exercises the exact script the nightly self-check runs — a second, locally-rendered copy could drift from it silently.

- [ ] **Step 2: Write the nightly self-check**

Create `roles/mac_control/templates/selfcheck.sh.j2`:

```bash
#!/usr/bin/env bash
{{ ansible_managed | comment }}
#
# mac-control's nightly self-check.
#
# The nightly `make verify` runs from svc-infra's git archive, and that runner
# has no key to this machine and should not get one — the point of this host is
# that it sits above the estate rather than inside it. So this is how nightly
# coverage happens.
#
# It checks ONLY this machine's own state and the two Ollama bindings (here and
# on TERRA). It is NOT an estate watchdog; moving the storage-outage guard out
# of thurgadin's failure domain is separate work with its own positive-control
# requirements.
#
# Coverage limit, stated rather than implied: this probes Ollama's port only.
# TERRA also runs ComfyUI on 8188, which had the same 0.0.0.0 exposure and was
# fixed by hand at the same time — but nothing here re-checks it, so a future
# ComfyUI relaunch without --listen 192.168.1.40 would go unnoticed.
#
# The healthchecks.io ping is the real product: an always-on box going quiet is
# the failure mode nobody notices for a fortnight.
set -euo pipefail

failures=0

if ! curl -fsS --max-time 5 \
     "http://{{ mac_control_lan_ip }}:{{ mac_control_ollama_port }}/api/tags" >/dev/null; then
  echo "FAIL: Ollama is not answering on the LAN address (check the USB-C adapter)"
  failures=$((failures + 1))
fi

tailscale=/Applications/Tailscale.app/Contents/MacOS/Tailscale

# This host's own binding. An `inconclusive` here IS a failure: this machine is
# always on by design, so "could not look" means something is wrong.
tailnet_ip=$($tailscale ip -4 2>/dev/null | head -n1)
if [ -n "$tailnet_ip" ]; then
  verdict=$({{ mac_control_home }}/bin/ollama-binding-check.sh \
    "{{ mac_control_lan_ip }}" "$tailnet_ip" || true)
  echo "mac-control: ${verdict}"
  case "$verdict" in
    *verdict=ok*) ;;
    *) failures=$((failures + 1)) ;;
  esac
else
  echo "FAIL: could not read this host's tailnet address"
  failures=$((failures + 1))
fi

# TERRA's binding, probed from here because this is the machine with tailnet
# access and a schedule. TERRA is a desktop that gets gamed on and switched
# off, so `inconclusive` is NORMAL there and does not fail the run — failing
# nightly whenever a PC is off would make this dead-man's switch useless, which
# is a slower way of having no check at all. It is printed distinctly so a
# permanent inconclusive is still visible in the log rather than absorbed.
#
# `exposed` fails here exactly as it would for this host. That is the finding
# this probe exists for.
terra_tailnet=$($tailscale status --json 2>/dev/null | /usr/bin/python3 -c '
import json, sys
try:
    peers = json.load(sys.stdin).get("Peer", {}).values()
except Exception:
    sys.exit(0)
for peer in peers:
    if peer.get("HostName", "").lower().startswith("terra"):
        ips = peer.get("TailscaleIPs") or []
        if ips:
            print(ips[0])
        break
')

if [ -n "$terra_tailnet" ]; then
  terra_verdict=$({{ mac_control_home }}/bin/ollama-binding-check.sh \
    "{{ gpu_host_ip }}" "$terra_tailnet" || true)
  echo "TERRA: ${terra_verdict}"
  case "$terra_verdict" in
    *verdict=exposed*)
      echo "FAIL: TERRA's Ollama answers unauthenticated on the tailnet"
      failures=$((failures + 1))
      ;;
  esac
else
  echo "TERRA: verdict=inconclusive reason=no-tailnet-peer-found"
fi

if [ "$failures" -ne 0 ]; then
  echo "self-check FAILED with ${failures} problem(s); not pinging the dead-man's switch"
  exit 1
fi

# Ping only on success. Pinging regardless would arm a switch that can never
# fire, which is worse than having none — it reads as proof.
if [ -n "${HC_PING_MAC_SELFCHECK:-}" ]; then
  curl -fsS --max-time 10 --retry 3 "$HC_PING_MAC_SELFCHECK" >/dev/null || \
    echo "WARNING: healthchecks ping failed" >&2
else
  echo "WARNING: HC_PING_MAC_SELFCHECK is unset; this host has no dead-man's switch" >&2
fi

echo "self-check OK"
```

Create `roles/mac_control/templates/homelab-selfcheck.plist.j2`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- {{ ansible_managed }} -->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.homelab.selfcheck</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{{ mac_control_home }}/bin/selfcheck.sh</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HC_PING_MAC_SELFCHECK</key>
    <string>{{ vault_hc_ping_mac_selfcheck | default('') }}</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/var/log/homelab-selfcheck.log</string>
  <key>StandardErrorPath</key>
  <string>/var/log/homelab-selfcheck.log</string>
</dict>
</plist>
```

Add to `roles/mac_control/tasks/scheduled-agent.yml`:

```yaml
- name: Install the Ollama binding check on the host
  ansible.builtin.template:
    src: ollama-binding-check.sh.j2
    dest: "{{ mac_control_home }}/bin/ollama-binding-check.sh"
    owner: "{{ ansible_user }}"
    group: staff
    mode: "0755"

- name: Install the nightly self-check script
  ansible.builtin.template:
    src: selfcheck.sh.j2
    dest: "{{ mac_control_home }}/bin/selfcheck.sh"
    owner: "{{ ansible_user }}"
    group: staff
    mode: "0755"

- name: Install the nightly self-check launchd job
  ansible.builtin.template:
    src: homelab-selfcheck.plist.j2
    dest: "{{ mac_control_home }}/Library/LaunchAgents/com.homelab.selfcheck.plist"
    owner: "{{ ansible_user }}"
    group: staff
    mode: "0644"
  no_log: true
```

Add to `inventory/group_vars/all_vault.yml.example` beside the release reader entry:

```yaml
# mac-control's nightly self-check. An always-on box going quiet is the
# failure nobody notices for a fortnight.
vault_hc_ping_mac_selfcheck: "REPLACE_healthchecks_ping_url"
```

- [ ] **Step 3: Add the plays**

Append to `site.yml`, immediately before the `Announce success` play:

```yaml
# ---------- The always-on second control node ----------
# Placed last: nothing in the estate depends on mac-control existing, and it
# is deliberately kept that way. A control node the estate NEEDED in order to
# function would defeat the independence that justified building it.
- name: Configure the always-on control node
  hosts: control_nodes
  gather_facts: true
  any_errors_fatal: true
  tasks:
    - name: Configure block
      block:
        - name: Apply the control node role
          ansible.builtin.include_role:
            name: mac_control
      rescue:
        - name: Notify control node failure
          ansible.builtin.include_role:
            name: notify
          vars:
            ntfy_title: "homelab mac-control FAILED"
            ntfy_result: "Configuration of the always-on control node failed."
            ntfy_priority: urgent
            ntfy_topic_override: "{{ ntfy_alert_topic }}"
            ntfy_tags: rotating_light

        - name: Abort after a failed control node run
          ansible.builtin.fail:
            msg: "mac-control configuration failed; see the task above."
```

In `site.yml`'s `Announce success` play, add to the `Assert every deployment phase completed` list:

```yaml
          - hostvars['mac-control'].mac_control_verified | default(false) | bool
```

Append to `verify.yml`, immediately before the final assert play:

```yaml
# mac-control is verified from a workstation, never from svc-infra's nightly
# runner — that runner has no key here and should not get one. Nightly
# coverage comes from the host's own launchd self-check, which pings
# healthchecks.io.
- name: Verify the always-on control node
  hosts: control_nodes
  gather_facts: false
  any_errors_fatal: true
  tasks:
    - name: Verify control node invariants
      ansible.builtin.include_role:
        name: mac_control
        tasks_from: verify
```

- [ ] **Step 4: Validate and deploy**

Run: `make validate && make mac && make mac`
Expected: second run reports `changed=0`.

Run: `make verify`
Expected: PASS, with the control node play running every check.

- [ ] **Step 5: Prove the checks can fail**

A check nobody has seen fail is a check nobody should trust. Stop Ollama and confirm `make verify` goes red for the right reason:

```bash
ssh straderb@192.168.1.41 "sudo launchctl bootout system/com.homelab.ollama"
make verify    # expected: FAILS on "Ollama is not answering on the LAN address"
ssh straderb@192.168.1.41 \
  "sudo launchctl bootstrap system /Library/LaunchDaemons/com.homelab.ollama.plist"
make verify    # expected: passes again
```

Then fire the self-check by hand and confirm the healthchecks.io check goes green:

```bash
ssh straderb@192.168.1.41 \
  "launchctl kickstart -k gui/\$(id -u straderb)/com.homelab.selfcheck"
```

- [ ] **Step 6: Commit**

```bash
git add roles/mac_control/tasks/verify.yml roles/mac_control/tasks/ollama.yml \
        roles/mac_control/tasks/scheduled-agent.yml \
        roles/mac_control/templates/selfcheck.sh.j2 \
        roles/mac_control/templates/homelab-selfcheck.plist.j2 \
        site.yml verify.yml inventory/group_vars/all_vault.yml.example
git commit -m "feat: verify mac-control, and give it a nightly self-check

Verification splits: the control_nodes play runs on workstation-invoked make
verify, because svc-infra's nightly runner has no key here and should not get
one. Nightly coverage comes from the host's own launchd self-check pinging
healthchecks.io — an always-on box going quiet is the failure nobody notices
for a fortnight.

Every check carries a positive control. The binding assertion refuses to draw
a conclusion from a silent tailnet address unless the LAN address answered
first, and the push guard is checked by making it actually refuse.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 13: Re-scope TERRA, and write the docs

Closes the existing exposure rather than merely not repeating it, and records everything a rebuild needs.

**Files:**
- Modify: `docs/gpu-host.md`
- Create: `docs/mac-control-node.md`
- Modify: `README.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: everything above.
- Produces: documentation only.

- [ ] **Step 1: Correct the TERRA binding instructions**

In `docs/gpu-host.md`, replace the `OLLAMA_HOST = 0.0.0.0:11434` instruction (around line 44) and its surrounding paragraph with:

````markdown
```text
OLLAMA_HOST = 192.168.1.40:11434
```

**Bind the LAN address, not `0.0.0.0`.** Ollama has no authentication of any
kind, so its bind address is the whole of its access control. `0.0.0.0` binds
every interface — including `tailscale0`, which means every peer on the tailnet
can use these models, list them, and pull new ones. This document told you to
set `0.0.0.0` until 2026-08-12, and consequently that is what the machine did.

Set it under *System Properties → Environment Variables → System variables*,
not in a shell — Ollama runs as a background service and will not see a
variable set in one terminal. Restart Ollama afterwards.

ComfyUI needs the same treatment, for the same reason. Launch it with
`--listen 192.168.1.40` rather than the bare `--listen`, which means all
interfaces.

`mac-control` checks both of these nightly and reports `inconclusive` — never
`ok` — when this PC is switched off. See `docs/mac-control-node.md`.
````

- [ ] **Step 2: Write the runbook**

Create `docs/mac-control-node.md`:

````markdown
# mac-control — the always-on second control node

An M1 Pro MacBook Pro (16 GB, broken backlight) that runs headless in a
closet as a second Ansible control node and agentic host.

Design and accepted risks:
[docs/superpowers/specs/2026-08-12-mac-control-node-design.md](superpowers/specs/2026-08-12-mac-control-node-design.md).

| Fact | Value |
|---|---|
| Address | `192.168.1.41` (USB-C 1GbE adapter, pfSense reservation) |
| Wi-Fi | enabled, separate reservation, management fallback only |
| Deploy | `make mac` — **from the working laptop, never from itself** |
| Ollama | `192.168.1.41:11434`, LAN only |
| Merges to `main` | not from here; the `pre-push` hook refuses |

## One-time prerequisites, before Ansible can reach it

These are the analog of cloud-init for the VMs. Nothing below is automated,
and `make mac` fails with instructions if any is missing.

1. **Install macOS** and create the `straderb` account. Use an external
   display over USB-C — the internal panel drives real pixels but has no
   backlight, so you cannot read it. A bright torch held at an angle works in
   a pinch.
2. **FileVault off, auto-login on.** Deliberate: FileVault's pre-boot prompt
   is unreachable by Screen Sharing, so an unplanned reboot would leave this
   machine dark until someone carries a monitor to the closet. The SSD stays
   hardware-encrypted at rest by the Secure Enclave.
3. **Enable Remote Login** (System Settings → General → Sharing).
4. **`xcode-select --install`** — provides `/usr/bin/python3`, which Ansible
   connects through.
5. **Install the admin SSH key** in `~/.ssh/authorized_keys`.
6. **Clone the repo** to `~/homelab-iac`.
7. **Place the two secrets by hand**, both mode 0600:
   - `~/homelab-iac/.vault_pass`
   - `~/homelab-iac/inventory/group_vars/all/vault.yml`

   Neither is ever written, read or logged by the role. The vault password
   cannot live in the vault, and any mechanism threading it through a play is
   a mechanism that can put it in a log.
8. **Copy the working laptop's admin SSH private key** to `~/.ssh/` here, so
   this machine can reach the service VMs.

   This is not what the design originally wanted. The intent was a keypair
   generated here that never left the machine and could be revoked on its
   own — but that needs `authorized_keys` managed exclusively on running VMs,
   and `roles/svc_infra/tasks/verify-runner.yml` already owns a key in that
   same file non-exclusively. An exclusive task would strip the verification
   runner's key and make `changed=0` unreachable.

   So: **this machine holds a copy of an existing private key and cannot be
   de-authorized on its own.** Revoking its access means rotating the admin
   key everywhere, which today means re-provisioning the VMs. Fixing that
   properly is a follow-up spec (admin keys in their own
   `AuthorizedKeysFile`).
9. **Install the superpowers plugin** — `claude`, then `/plugin`.

## Routine operation

```bash
make mac                       # converge (from the working laptop)
make mac                       # again: must report changed=0
make verify                    # includes the control_nodes checks
ssh straderb@192.168.1.41      # then: tmux attach -t homelab
```

## Two control nodes

Deploys are serialized by a lock on thurgadin. A second deploy fails
immediately naming the holder. If a run crashed and left the lock behind:

```bash
make deploy-unlock
```

`main` is merged only from the working laptop. The `pre-push` hook here
refuses it — a guardrail, not a boundary: `--no-verify` walks past it.

## When it does not come back

The battery makes a hard power loss unlikely but not impossible, and there is
no readable screen.

1. **Check the dead-man's switch first.** healthchecks.io alerts if the
   nightly self-check has not pinged.
2. **SSH over Wi-Fi.** If the wired address is silent but Wi-Fi answers, the
   USB-C adapter has dropped — Ollama's bind address no longer exists. Reseat
   it and `launchctl kickstart -k system/com.homelab.ollama`.
3. **Neither answers.** Attach an external display and a keyboard. With
   FileVault off and auto-login on it should boot straight to a session; if it
   did not, the machine is genuinely down rather than merely unreachable.

## What this machine deliberately does not do

- **Run containers.** macOS would mean a Linux VM costing 4+ GB, and none of
  this repo's roles apply to it.
- **Watch the estate.** `nfsguard` still runs on thurgadin — a watcher inside
  the failure domain it watches. Fixing that is separate, deferred work.
- **Serve chat models.** 16 GB, shared with agent sessions. It serves the two
  small always-on models; TERRA serves everything else.
````

- [ ] **Step 3: Update README and CLAUDE.md**

In `README.md`, add `make mac` and `make deploy-unlock` to the target table alongside `make dl` / `make media` / `make infra`, and link `docs/mac-control-node.md`.

In `CLAUDE.md`, add this section immediately after "The change workflow":

````markdown
## There are two control nodes

The working laptop and `mac-control` (`192.168.1.41`) can both deploy to this
estate. Two rules keep that from being a way to break things:

- **Deploys are serialized by a lock on thurgadin.** A second `make infra`
  fails immediately, naming the holder and the time. `make deploy-unlock`
  clears a lock left by a crashed run — check that the other deploy really is
  gone first.
- **`main` is merged only from the working laptop.** `mac-control` pushes
  branches freely; its `pre-push` hook refuses `main`. This amends the standing
  "push after committing" rule, which was written for a session with a human in
  it — on an always-on box it would mean an agent pushing to `main` while you
  sleep.

`mac-control` is configured **from** the working laptop, never from itself; the
play refuses a self-target. Full runbook: `docs/mac-control-node.md`.
````

- [ ] **Step 4: Validate**

Run: `make validate`
Expected: PASS, including `Local Markdown links: OK`.

- [ ] **Step 5: Apply the TERRA change by hand and confirm it took**

On TERRA: set `OLLAMA_HOST` to `192.168.1.40:11434` in System Environment
Variables, restart Ollama, and relaunch ComfyUI with `--listen 192.168.1.40`.

Then confirm from a tailnet peer — the doc change is intent, the probe is fact:

```bash
curl -fsS --max-time 5 http://192.168.1.40:11434/api/tags        # expect: works
curl -fsS --max-time 5 http://<TERRA-tailnet-ip>:11434/api/tags  # expect: FAILS
curl -fsS --max-time 5 http://<TERRA-tailnet-ip>:8188/           # expect: FAILS
```

- [ ] **Step 6: Commit**

```bash
git add docs/gpu-host.md docs/mac-control-node.md README.md CLAUDE.md
git commit -m "docs: re-scope TERRA's bindings and document mac-control

docs/gpu-host.md told you to set OLLAMA_HOST=0.0.0.0, so TERRA's models
answered unauthenticated to every tailnet peer. Both Ollama and ComfyUI now
bind the LAN address, and the instruction that caused it is corrected so the
next rebuild does not reintroduce it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Finishing the branch

Follow CLAUDE.md's workflow exactly — the ordering is what makes "verified" and
"committed" the same state.

- [ ] `git status --porcelain` prints nothing. Untracked files count.
- [ ] `make deploy` from the clean tree. Expect `changed=0`, except svc-infra's
      known post-commit `changed=3` (the runner's git-archive sync). Run
      `make infra` again and require `changed=0` from the second run. **Check
      which tasks changed** — do not paper over a genuine diff by quoting the
      second number.
- [ ] `make mac` twice: `changed=0` on the second run.
- [ ] `make verify` passes, including the control_nodes play.
- [ ] Merge to `main` from the working laptop, push.
- [ ] Delete the branch locally and on the remote.
