# Tailscale Subnet Router on svc-media + svc-infra — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every remote device reaches `192.168.1.0/24` through an Ansible-managed Tailscale subnet router on svc-media, with svc-infra as an automatic-failover second router, and edgar (a Windows desktop VM) no longer carries the route.

**Architecture:** A new role, `tailscale_router`, applies to a new inventory group `tailnet_routers` (svc-media, svc-infra). It installs a pinned `tailscale` package with auto-update off, sets forwarding, firewall and Tailscale prefs idempotently, and refuses to log in by itself: it asserts the node is logged in and stops with instructions if not. The existing `homelab-tailnet-route` check moves into that role so each router checks the other, gains a node-key-expiry alarm, and still sends exactly one healthchecks.io stream (from svc-media). Firewall rules that today admit only `lan_cidr` also admit the tailnet range on the router hosts, because traffic a router delivers to *itself* arrives with a `100.x` source, not a LAN source.

**Tech Stack:** Ansible (ansible.builtin / ansible.posix.firewalld / ansible.posix.sysctl), Rocky Linux 10.2, firewalld (nftables backend), Tailscale Linux client from `pkgs.tailscale.com/stable/rhel/10`, bash + jq, Python gate tests under `tests/`.

## Global Constraints

- Work only from **TERRA's WSL clone** (`~/dev/homelab-ironwood`), or a Mac clone whose vault is current. The MacBook clone at `~/Documents/homelab/arr-stack-v1/20260716/homelab-iac` has a stale vault: `make infra` there fails on `'vault_openwebui_admin_token' is undefined`. Never edit the vault to work around it.
- Follow the repo change workflow in `CLAUDE.md` exactly: branch → `make validate` → iterative deploys → commit → `git status --porcelain` empty → `make deploy-proof` → `make verify` → ff-merge → push → delete branch.
- **Never** `git add -A`; stage explicit paths. Never echo vault secrets; secret-bearing tasks get `no_log: true` and `diff: false`.
- Wrap every deploy/verify from a laptop in `caffeinate -dims` (macOS) and keep the lid open. On 2026-11-11 three `make verify` runs died to Mac sleep, each reading as a host going `UNREACHABLE` after a ~16-minute stall.
- Tailscale auto-update must be **off** on every router. Upgrades happen only by changing `tailscale_version` in git.
- Do not stop, restart or log out `tailscaled` on whichever node currently holds the route (check with Task 1 Step 2) except in the agreed failover test (Task 7). That cuts off every remote household device.
- `lan_cidr` is `192.168.1.0/24` (`inventory/group_vars/all/main.yml:11`). The Tailscale CGNAT range is `100.64.0.0/10`.

---

## Context (read this first — the new session has no other source)

### What happened

On **2026-11-10 16:17 local** Tailscale's Windows auto-updater on **edgar** (PVE VM 108 `w10-edgar`, `192.168.1.159`, tailnet `100.93.219.13`) removed version 1.102.2, then failed to install 1.104.6. Its error was `1603`, "Disk full… Volume: 'C:'", with 55 MB free on a 32 GB disk. edgar was the **only** node advertising `192.168.1.0/24`, so every remote device lost the LAN for most of a day while every service check stayed green.

It was fixed on 2026-11-11. The disk was grown to 40 GB (the recovery partition was removed and WinRE moved into C:), and Tailscale 1.104.6 was reinstalled from `C:\ProgramData\Tailscale\MSICache\`. edgar rejoined as the same node with its route still approved. The disk had filled with Microsoft auto-installed content: the Copilot app (1.3 GB), Edge runtime copies (2.1 GB), the Edge updater cache (0.5 GB) and a Photos update (0.9 GB).

### Why svc-media stopped being a router candidate

Commit `b89a692` (2026-07-20) removed the whole Tailscale section from `roles/svc_media`, on the premise "Now that the subnet router is a dedicated VM (confirmed)". **No dedicated VM ever existed**; the router was edgar all along. That commit also deleted `vault_tailscale_authkey` from the vault example. It only removed code: the package and daemon stayed on svc-media, unmanaged. Run `git show b89a692 -- roles/svc_media/tasks/main.yml` for the removed tasks; the repo definition they used is reused in Task 3.

### Live state measured 2026-11-11 (re-measure in Task 1; do not trust this blindly)

| Node | Facts |
|---|---|
| **edgar** | Sole holder of `192.168.1.0/24` in `PrimaryRoutes`. Tailscale 1.104.6. Windows auto-update ON. **Node key expires 2027-01-15T14:19:55Z.** |
| **svc-media** (`192.168.1.30`, tailnet `100.93.88.34`) | `tailscale-1.98.9` hand-installed 2026-07-18 from repo `tailscale-stable` (EL10), logged in, untagged. Prefs: `AdvertiseRoutes: null`, `RouteAll: false`, **`CorpDNS: true`**, `NoSNAT: false`, `NetfilterMode: 2`, `AutoUpdate {Check: true, Apply: true}` (yet still 1.98.9, so Linux auto-apply evidently isn't working). `net.ipv4.ip_forward = 0`. firewalld default zone `public` holds only `eth0`; `tailscale0` is in no zone. Zone `public`: `forward: yes`, `masquerade: no`. **Node key expires 2027-01-14T23:15:11Z.** |
| svc-media resolver | `/etc/resolv.conf` is `nameserver 100.100.100.100`, i.e. **Tailscale MagicDNS**, because `CorpDNS` is on. If tailscaled stops, svc-media cannot resolve names. NetworkManager holds `IP4.DNS 192.168.1.1`, and `/etc/resolv.pre-tailscale-backup.conf` exists. Nothing in the repo uses `ts.net` names (grep finds none). |
| **svc-infra** (`192.168.1.32`) | Rocky 10.2, **no tailscale package**, `ip_forward = 0`, firewalld `public` holds `eth0`. |
| **ts-exit** (`100.90.208.5`) | A `tailscale/tailscale:latest` container on **kunark** (VM 111, an unmanaged Debian Docker host with 99% root fill). Offers an exit node only; **does not advertise the LAN route**. Out of scope. |
| Clients | macOS clients have `RouteAll: true` (they accept subnet routes). On the LAN, `route get 192.168.1.30` still resolves to `en0`. |
| Tailnet DNS | Split DNS `fortwow.dev → 192.168.1.30` (svc-media's dnsmasq) is set in the admin console. Household apps use only `https://<name>.fortwow.dev`, i.e. Caddy on `:443` plus DNS on `:53` (`docs/household/getting-connected.md`). |

### Already shipped (main `28a3642`)

`homelab-tailnet-route.timer` on svc-media, every 10 minutes:
- **Files:** `roles/svc_media/templates/tailnet-route.sh.j2`, `roles/svc_media/files/homelab-tailnet-route.{service,timer}`, tasks in `roles/svc_media/tasks/access.yml`, gate in `roles/svc_media/tasks/verify.yml` and `verify.yml`, and test `tests/validate_tailnet_route.py` (11 cases).
- **What it checks:** that some node holds the CIDR in `PrimaryRoutes`, is `Online`, and answers `tailscale ping`. When the router is the node itself, it checks only that `ip_forward` is 1.
- **Exit codes:** 1 means the route is down, 2 means it couldn't look.
- **Alerting:** OnFailure goes to ntfy, and it pings healthchecks.io if `HC_PING_TAILNET_ROUTE` is set. That is rendered only when `vault_hc_ping_tailnet_route` is non-empty (`roles/service_vm/templates/healthchecks.env.j2`).

**Gaps this plan closes:**
- When svc-media itself is primary, the check proves no data path.
- Nothing asserts that a *standby* router exists.
- Nothing watches node-key expiry, and both live router keys expire in mid-January 2027.

### The source-address trap (why Task 4 exists)

With default `--snat-subnet-routes` (`NoSNAT: false`):
- **Forwarded traffic:** traffic a router forwards to *another* LAN host is masqueraded to the router's LAN IP (e.g. `192.168.1.30`), which `lan_cidr` firewalld rules already accept.
- **Self-delivered traffic:** traffic whose destination is the router's *own* LAN IP is not forwarded. It is delivered locally from `tailscale0` with the client's `100.x.y.z` source. svc-media's Caddy (`:443`), dnsmasq (`:53`) and every other `source address={{ lan_cidr }}` rich rule will **reject** it.

So the moment svc-media becomes primary, the household loses DNS and HTTPS unless the router hosts also admit `100.64.0.0/10`. The same applies on svc-infra for its own ports when it is primary.

Today via edgar, remote devices are SNATed to `192.168.1.159` and can reach everything the LAN can. Task 4 keeps that parity on purpose (see Decision D3).

---

## Decisions for the user (resolve before Task 3; the plan states a default)

- **D1 — How svc-infra logs in.** *Default: one-time interactive login.* The role never runs `tailscale up`; it asserts `BackendState == Running` and, if not, fails with the exact command to run by hand (`sudo tailscale up …`, which prints a login URL). No long-lived secret is stored. The alternative is to restore `vault_tailscale_authkey`: a pre-approved, non-reusable, tagged key used once with `no_log`, a `REPLACE_` guard (`tests/validate_vault_guards.py`) and `diff: false`. Auth keys expire after at most 90 days, so a stale vault value would fail future rebuilds anyway.
- **D2 — Node-key expiry.** *Default: disable key expiry for svc-media and svc-infra in the admin console* (Machines → ⋯ → Disable key expiry). The alternative is tagging both nodes (e.g. `tag:subnet-router`; tagged nodes don't expire by default), which needs a `tagOwners` ACL edit and re-authenticates the node. Either way the route check gains an expiry alarm (Task 5).
- **D3 — What tailnet clients may reach on the router hosts.** *Default: parity.* Every port open to `lan_cidr` on svc-media/svc-infra is also open to `100.64.0.0/10`, matching today's edgar behaviour. The tighter alternative is only `443/tcp` and `53/udp+tcp` on svc-media plus nothing on svc-infra, which breaks direct-port access for anyone using `http://192.168.1.x:port` remotely (ntfy `:8080`, Cockpit `:9090`, Grafana and so on). Real least-privilege belongs in a Tailscale ACL. The open item `gpu-services-tailnet-exposed` (Ollama/ComfyUI answering unauthenticated on the tailnet) is the same question.
- **D4 — Which router should be primary.** *Default: svc-media.* All remote traffic ends at Caddy/dnsmasq on `.30` anyway, so routing there saves a hop and adds no new failure point. Tailscale elects the primary itself (normally the longest-online approved advertiser) and offers no direct "make primary" control. Task 6 gets svc-media elected by withdrawing edgar while svc-infra isn't advertising yet. If svc-infra later becomes primary after a failover, it stays primary; that works, just one hop longer.
- **D5 — edgar afterwards.** *Default: keep Tailscale installed (it's used as a verification host), stop it advertising, and disable its auto-update* (`tailscale set --auto-update=false`). Removing the Copilot app from edgar is optional cleanup, not required here.
- **D6 — A maintenance window with the household for Task 7's failover test** (about 2 minutes of possible disruption).

## User-owned prerequisites

- [ ] Create healthchecks.io check **`homelab-tailnet-route`** with period 10 min and grace 30 min. Put its UUID in the vault as `vault_hc_ping_tailnet_route` (`make vault-edit`). This is documented in `inventory/group_vars/all_vault.yml.example`. Until it's set, the check only logs a warning. Note that `make vault-edit` alone changes nothing on the host; the next `make media` renders it.
- [ ] Decide D1–D6.

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `inventory/hosts.yml` | Modify | New group `tailnet_routers` (svc-media, svc-infra) |
| `inventory/group_vars/all/main.yml` | Modify | `tailnet_cidr`, `tailscale_version`, `tailscale_advertise_routes`, `access_cidrs`, `tailnet_key_expiry_days`, `tailnet_route_hc_host` |
| `roles/tailscale_router/defaults/main.yml` | Create | Role defaults |
| `roles/tailscale_router/tasks/main.yml` | Create | Repo, pinned package, tailscaled, sysctl, firewalld interface, prefs, login gate, route-check install |
| `roles/tailscale_router/tasks/verify.yml` | Create | Read-only gates: logged in, advertising, forwarding, check timer, run check |
| `roles/tailscale_router/templates/tailnet-route.sh.j2` | Move from `roles/svc_media/templates/` | Route check (+ key-expiry, + HC host gating) |
| `roles/tailscale_router/files/homelab-tailnet-route.{service,timer}` | Move from `roles/svc_media/files/` | Units |
| `roles/svc_media/tasks/access.yml` | Modify | Drop route-check install (moved); rich rules loop `access_cidrs` |
| `roles/svc_media/tasks/verify.yml` | Modify | Drop route-check gate (moved) |
| `roles/svc_infra/tasks/files.yml` | Modify | Rich rules at ~1003–1070 loop `access_cidrs` |
| `roles/mon/tasks/main.yml` | Modify | Cockpit rule (~line 41) loops `access_cidrs` |
| `inventory/host_vars/svc-media.yml`, `svc-infra.yml` | Modify | `onfailure_units_extra` gains/keeps `homelab-tailnet-route.service` |
| `site.yml` | Modify | New play `Configure Tailscale subnet routers` |
| `verify.yml` | Modify | Include `tailscale_router` verify; final assert reads per-router facts |
| `tests/validate_tailnet_route.py` | Modify | New template path, key-expiry + HC-gating cases |
| `tests/validate_tailscale_router.py` | Create | Static gates for the role (pin, auto-update off, accept-routes/dns off, firewall parity) |
| `tests/validate_shell_templates.py` | Modify | Context for new Jinja vars |
| `docs/architecture.md`, `docs/dns-pfsense-caddy.md`, `README.md`, `docs/services.md` | Modify | Describe the real topology |

---

### Task 1: Session preflight and baseline (no code)

**Files:** none.

- [ ] **Step 1: Sync and check the clone.**

```bash
cd ~/dev/homelab-ironwood            # TERRA WSL — not the Windows path
git switch main && git pull --ff-only
git status --porcelain               # must print nothing
make validate                        # must pass before any change
```

- [ ] **Step 2: Record who holds the route right now.** Run this from any tailnet node; svc-media works without root:

```bash
ssh straderb@192.168.1.30 'tailscale status --json | jq -r "
  ([.Self] + [.Peer[]])[] | select((.PrimaryRoutes // []) | length > 0)
  | \"\(.HostName) online=\(.Online) primary=\(.PrimaryRoutes) keyExpiry=\(.KeyExpiry // \"none\")\""'
```

Expected: one line for `edgar online=true primary=["192.168.1.0/24"]`. If it's offline, fix that first (see Context); do not start a migration during an outage.

- [ ] **Step 3: Bring the nightly runner current.** It was at `801a933` on 2026-11-11; read `sudo cat /opt/homelab-iac/.deployed-rev` on svc-infra. Run `make deploy-proof TARGET=infra` twice (the first is expected to report only the three runner-sync tasks, and the second must say `PROOF OK: clean`), then `make verify` (green).

- [ ] **Step 4: Re-measure the svc-media facts in the Context table:**

```bash
ssh straderb@192.168.1.30 'rpm -q tailscale; sysctl -n net.ipv4.ip_forward; sudo firewall-cmd --get-active-zones;
  sudo tailscale debug prefs | jq -c "{RouteAll,CorpDNS,AdvertiseRoutes,NoSNAT,AutoUpdate}"; head -3 /etc/resolv.conf'
```

If anything differs from the Context table, stop and reconcile before writing code. The plan's ordering depends on these.

---

### Task 2: Inventory group, variables and the role's static gate (TDD)

**Files:**
- Modify: `inventory/hosts.yml`, `inventory/group_vars/all/main.yml`
- Create: `tests/validate_tailscale_router.py`, `roles/tailscale_router/defaults/main.yml`

**Interfaces:**
- Produces: group `tailnet_routers`; vars `tailnet_cidr` (str), `tailscale_version` (str, exact RPM version), `tailscale_advertise_routes` (list[str]), `access_cidrs` (list[str], per host), `tailnet_key_expiry_days` (int), `tailnet_route_hc_host` (hostname str).

- [ ] **Step 1: Write the failing gate** `tests/validate_tailscale_router.py`:

```python
#!/usr/bin/env python3
"""Static gates for the Tailscale subnet-router role.

Each assertion is a lesson from the 2026-11-10 outage or the trap it exposed:
  * auto-update OFF — Tailscale's Windows updater uninstalled before it
    installed and left edgar with no client at all; upgrades are a git change.
  * an exact pinned version — `state: latest` would make every deploy an
    unreviewed upgrade and break the changed=0 proof.
  * accept-routes OFF on routers — a LAN host accepting its own LAN route
    can blackhole its own LAN traffic into the tunnel.
  * accept-dns OFF on routers — svc-media's resolver was MagicDNS, so a
    tailscaled outage took name resolution with it.
  * firewall parity — a router delivers traffic for its OWN address with the
    client's 100.x source, so lan_cidr-only rules reject the household.
  * at least two routers — one router is how this outage happened.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

GATE_GROUP = "systemd"  # same group as validate_onfailure.py; see tests/run_gates.py

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/tailscale_router"
MAIN_VARS = ROOT / "inventory/group_vars/all/main.yml"
HOSTS = ROOT / "inventory/hosts.yml"

# Rich rules that may stay lan_cidr-only, with the reason. Everything else
# that opens a port by `source address=` must loop over access_cidrs.
LAN_ONLY_ALLOWED: dict[str, str] = {}


def find_group(tree: dict, name: str) -> dict | None:
    if not isinstance(tree, dict):
        return None
    for key, value in tree.items():
        if key == name:
            return value
        found = find_group(value, name)
        if found is not None:
            return found
    return None


def main() -> int:
    failures: list[str] = []
    main_vars = yaml.safe_load(MAIN_VARS.read_text(encoding="utf-8"))

    version = str(main_vars.get("tailscale_version", ""))
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        failures.append(f"tailscale_version must be an exact x.y.z pin, got {version!r}")
    if main_vars.get("tailnet_cidr") != "100.64.0.0/10":
        failures.append("tailnet_cidr must be 100.64.0.0/10")
    if main_vars.get("tailscale_advertise_routes") != ["{{ lan_cidr }}"]:
        failures.append("tailscale_advertise_routes must be exactly ['{{ lan_cidr }}']")

    group = find_group(yaml.safe_load(HOSTS.read_text(encoding="utf-8")), "tailnet_routers")
    hosts = list((group or {}).get("hosts", {}) or {})
    if len(hosts) < 2:
        failures.append(f"tailnet_routers needs >= 2 hosts for failover, has {hosts}")

    tasks = ROLE / "tasks/main.yml"
    text = tasks.read_text(encoding="utf-8") if tasks.exists() else ""
    for needle, why in (
        ("--auto-update=false", "auto-update must be explicitly off"),
        ("--accept-routes=false", "routers must not accept subnet routes"),
        ("--accept-dns=false", "routers must not use MagicDNS as their resolver"),
        ("tailscale-{{ tailscale_version }}", "install must pin the exact version"),
    ):
        if needle not in text:
            failures.append(f"roles/tailscale_router/tasks/main.yml: {why} ({needle!r} missing)")
    if re.search(r"state:\s*latest", text):
        failures.append("roles/tailscale_router/tasks/main.yml: never `state: latest`")

    rule = re.compile(r"source address=\{\{\s*lan_cidr\s*\}\}")
    for path in sorted(ROOT.glob("roles/*/tasks/*.yml")):
        rel = str(path.relative_to(ROOT))
        if rule.search(path.read_text(encoding="utf-8")) and rel not in LAN_ONLY_ALLOWED:
            failures.append(f"{rel}: firewalld rich rule uses lan_cidr directly; "
                            "loop over access_cidrs so router hosts admit tailnet sources")

    if failures:
        print("tailscale router gate failed:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"tailscale router: OK ({len(hosts)} routers, version {version} pinned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it and watch it fail.** Run: `.venv/bin/python tests/validate_tailscale_router.py`. Expected: FAIL listing the missing `tailscale_version`, the missing group, the missing role file, and the `lan_cidr` rich rules in `roles/svc_media/tasks/access.yml`, `roles/svc_infra/tasks/files.yml` and `roles/mon/tasks/main.yml`. Then confirm `tests/run_gates.py` discovers it: `make validate-systemd` should run it.

- [ ] **Step 3: Add the group** to `inventory/hosts.yml`, as a sibling of `service_vms` under `all.children`. Hosts are referenced, not redefined, so `ansible_host` stays single-sourced:

```yaml
    # Tailscale subnet routers for lan_cidr (roles/tailscale_router). Two, so
    # one can fail without cutting off every remote device — the single-router
    # design is what turned edgar's 2026-11-10 failed auto-update into an
    # all-day outage. Members are service_vms; ansible_host lives there.
    tailnet_routers:
      hosts:
        svc-media:
        svc-infra:
```

- [ ] **Step 4: Add the variables** to `inventory/group_vars/all/main.yml`, directly after `lan_cidr` (line 11):

```yaml
# Tailscale's CGNAT range. Traffic a subnet router delivers to ITS OWN LAN
# address arrives from tailscale0 with the client's 100.x source — it is not
# SNATed — so router hosts must admit this range wherever they admit lan_cidr.
tailnet_cidr: 100.64.0.0/10
# Sources a host's LAN-facing firewalld rules admit. Routers add the tailnet;
# everyone else only ever sees router-SNATed LAN sources.
access_cidrs: >-
  {{ [lan_cidr] + ([tailnet_cidr]
     if inventory_hostname in groups.get('tailnet_routers', []) else []) }}
```

And in a new `# --- Tailscale subnet routers ---` section near the access-layer block (~line 627):

```yaml
# Exact RPM version from pkgs.tailscale.com/stable/rhel/10. Auto-update is OFF
# on routers (roles/tailscale_router); bumping this value IS the upgrade.
# Measure before bumping: `dnf --showduplicates list tailscale` on a router.
tailscale_version: "1.98.9"
tailscale_advertise_routes:
  - "{{ lan_cidr }}"
# homelab-tailnet-route fails this many days before a router's node key expires.
tailnet_key_expiry_days: 21
# The one router whose route check pings healthchecks.io, so two hosts never
# report one fact to one check and make it flap.
tailnet_route_hc_host: "{{ media_host }}"
```

Note: `tailscale_version` starts at the **installed** version (1.98.9), so the first deploy changes nothing package-wise. The upgrade is a separate, deliberate later bump.

- [ ] **Step 5: Create** `roles/tailscale_router/defaults/main.yml`:

```yaml
---
# All real values live in inventory/group_vars/all/main.yml; these exist so
# the role fails loudly rather than rendering an empty route.
tailscale_repo_baseurl: "https://pkgs.tailscale.com/stable/rhel/10/$basearch"
tailscale_repo_gpgkey: "https://pkgs.tailscale.com/stable/rhel/10/repo.gpg"
```

- [ ] **Step 6: Re-run the gate.** `.venv/bin/python tests/validate_tailscale_router.py`. Expected: the version, CIDR and group failures are gone; the role-tasks and `lan_cidr` rule failures remain (fixed in Tasks 3–4).

- [ ] **Step 7: Branch, then commit** (create the branch now if it doesn't exist):

```bash
git switch -c feat/tailscale-subnet-routers
git add inventory/hosts.yml inventory/group_vars/all/main.yml \
        roles/tailscale_router/defaults/main.yml tests/validate_tailscale_router.py
git commit -m "feat: declare two Tailscale subnet routers and their variables"
```

---

### Task 3: The `tailscale_router` role — install, configure, login gate

**Files:**
- Create: `roles/tailscale_router/tasks/main.yml`
- Modify: `site.yml` (new play after `Monitoring (Cockpit + disk alarm on all VMs)`, before `Configure the media VM`)

**Interfaces:**
- Consumes: the Task 2 vars.
- Produces: `tailscale_router_backend` (fact, str); the node advertising `tailscale_advertise_routes` with auto-update, accept-routes and accept-dns off; `net.ipv4.ip_forward=1`; `tailscale0` explicitly in firewalld zone `public`.

- [ ] **Step 1: Write the tasks file.**

```yaml
---
# Tailscale subnet router for lan_cidr. See docs/architecture.md and
# docs/superpowers/plans/2026-11-11-tailscale-subnet-router.md for why each
# setting is what it is. This role never logs a node in: it asserts the node
# is Running and stops with instructions otherwise (no long-lived auth key).

- name: Tailscale package repository (official EL10)
  ansible.builtin.yum_repository:
    name: tailscale-stable
    description: Tailscale stable (EL10)
    baseurl: "{{ tailscale_repo_baseurl }}"
    gpgkey: "{{ tailscale_repo_gpgkey }}"
    gpgcheck: true
    repo_gpgcheck: true
    enabled: true

# Exact version, never latest: an unreviewed upgrade of the thing every remote
# device depends on is exactly the failure this role exists to prevent.
- name: Install the pinned Tailscale version
  ansible.builtin.dnf:
    name: "tailscale-{{ tailscale_version }}"
    state: present
    allow_downgrade: false

- name: Enable tailscaled
  ansible.builtin.systemd:
    name: tailscaled
    enabled: true
    state: started

- name: Enable IPv4 forwarding for the subnet route
  ansible.posix.sysctl:
    name: net.ipv4.ip_forward
    value: "1"
    sysctl_file: /etc/sysctl.d/90-tailscale-router.conf
    state: present
    reload: true

# Explicit, not implicit: an interface in no zone falls into the default zone
# today, but that is firewalld's default talking, not a decision. Same zone as
# eth0 so firewalld's intra-zone forwarding (public: forward yes) carries
# routed traffic, and access_cidrs rich rules govern what reaches this host.
- name: Bind tailscale0 to the public zone
  ansible.posix.firewalld:
    zone: public
    interface: tailscale0
    permanent: true
    immediate: true
    state: enabled

- name: Read Tailscale state
  ansible.builtin.command: tailscale status --json
  register: tailscale_router_status
  changed_when: false
  failed_when: false
  check_mode: false

- name: Record backend state
  ansible.builtin.set_fact:
    tailscale_router_backend: >-
      {{ ((tailscale_router_status.stdout | default('', true)) or '{}')
         | from_json | json_query('BackendState') | default('Unknown', true) }}

- name: Stop until this router has been logged in by hand
  ansible.builtin.assert:
    that: tailscale_router_backend == 'Running'
    fail_msg: >-
      tailscaled on {{ inventory_hostname }} is '{{ tailscale_router_backend }}',
      not Running. Log it in ONCE by hand, then re-run:
      ssh {{ ansible_user }}@{{ ansible_host }}
      'sudo tailscale up --advertise-routes={{ tailscale_advertise_routes | join(",") }}
      --accept-routes=false --accept-dns=false'
      and open the URL it prints. Then approve the route and disable key expiry
      for this machine in the Tailscale admin console.

- name: Read current Tailscale prefs
  ansible.builtin.command: tailscale debug prefs
  register: tailscale_router_prefs_raw
  changed_when: false
  check_mode: false

- name: Decide whether prefs need changing
  ansible.builtin.set_fact:
    tailscale_router_prefs_ok: >-
      {{ (p.AdvertiseRoutes | default([], true) | sort) == (tailscale_advertise_routes | sort)
         and not (p.RouteAll | bool)
         and not (p.CorpDNS | bool)
         and not (p.AutoUpdate.Apply | default(false) | bool) }}
  vars:
    p: "{{ tailscale_router_prefs_raw.stdout | from_json }}"

# `tailscale set` only touches the named prefs, never re-authenticates, and is
# applied only on mismatch so the deploy-proof's changed=0 means something.
- name: Apply router prefs
  ansible.builtin.command:
    argv:
      - tailscale
      - set
      - "--advertise-routes={{ tailscale_advertise_routes | join(',') }}"
      - --accept-routes=false
      - --accept-dns=false
      - --auto-update=false
  when: not (tailscale_router_prefs_ok | bool)
  changed_when: true
```

Notes for the implementer:
- `json_query` needs `jmespath`. If `.venv` lacks it, use `(... | from_json).BackendState | default('Unknown')` instead, which is what the removed b89a692 code did.
- `AdvertiseRoutes` in `tailscale debug prefs` is a JSON list or `null`, hence `default([], true)`.
- If the prefs JSON nests `AutoUpdate` differently on the pinned version, measure it (`sudo tailscale debug prefs | jq .AutoUpdate`) and adjust the expression. Don't guess.

- [ ] **Step 2: Add the play to `site.yml`.** Copy the block/rescue shape of the `Monitoring` play; place it after that play:

```yaml
# ---------- Tailscale subnet routers (svc-media + svc-infra) ----------
- name: Configure Tailscale subnet routers
  hosts: tailnet_routers
  gather_facts: true
  any_errors_fatal: true
  tasks:
    - name: Configure the subnet router
      block:
        - name: Run the Tailscale router role
          ansible.builtin.include_role:
            name: tailscale_router
            apply:
              tags: [tailscale]
          tags: [always]
      rescue:
        - name: Notify subnet router failure
          ansible.builtin.include_role:
            name: notify
            apply:
              tags: [always]
          tags: [always]
          vars:
            ntfy_title: "homelab deploy FAILED (tailscale router)"
            ntfy_result: "Subnet router configuration failed on {{ inventory_hostname }}."
            ntfy_priority: high
            ntfy_topic_override: "{{ ntfy_alert_topic }}"
            ntfy_tags: rotating_light
        - name: Abort after subnet router failure
          ansible.builtin.fail:
            msg: "tailscale router failed on {{ inventory_hostname }}"
          tags: [always]
```

`any_errors_fatal` means svc-infra's not-yet-logged-in assert would abort svc-media's run too. During Task 6, deploy with `--limit svc-media` until svc-infra is logged in. Check that the Makefile's `media`/`infra` targets' `--limit` covers this play; they use `--limit <host>` with the whole `site.yml`.

- [ ] **Step 3: Run the gates.** `make validate`. Expected: `validate_tailscale_router.py` now fails only on the `lan_cidr` rich rules (Task 4). ansible-lint is clean.

- [ ] **Step 4: Commit.**

```bash
git add roles/tailscale_router/tasks/main.yml site.yml
git commit -m "feat: manage Tailscale on the subnet routers with a pinned version and no auto-update"
```

---

### Task 4: Firewall parity for tailnet sources on router hosts

**Files:**
- Modify: `roles/svc_media/tasks/access.yml` (three rich-rule tasks: `Open LAN-scoped media and notification ports`, `Open LAN-scoped ports for enabled Minecraft servers, close parked ones`, `Open DNS to the LAN source range`, plus `Open Caddy HTTPS to the LAN source range` inside the Caddy block)
- Modify: `roles/svc_infra/tasks/files.yml` (~1003–1070: the catalog ui_port, extra_ports, Paperless, NetBox, Immich, Nextcloud tasks)
- Modify: `roles/mon/tasks/main.yml` (`Open Cockpit 9090 to the LAN`)

**Interfaces:**
- Consumes: `access_cidrs` (Task 2).
- Produces: on router hosts, every LAN-scoped port is also open to `100.64.0.0/10`. On svc-download, rules are unchanged in effect (`access_cidrs == [lan_cidr]`).

- [ ] **Step 1: The gate is already failing** (Task 2 Step 6). That's the failing test.

- [ ] **Step 2: Transform every rule mechanically.**
  - A task with no loop gains `loop: "{{ access_cidrs }}"` and uses `{{ item }}` as the source.
  - A task that already loops over ports gets a product. For example, svc-media's media ports:

```yaml
- name: Open LAN-scoped media and notification ports
  ansible.posix.firewalld:
    rich_rule: >-
      rule family=ipv4 source address={{ item.0 }} port
      port={{ item.1 }} protocol=tcp accept
    permanent: true
    immediate: true
    state: enabled
  loop: "{{ access_cidrs | product([8080, 8096]) | list }}"
  loop_control:
    label: "{{ item.0 }} -> {{ item.1 }}"
```

  - The Minecraft task loops over `minecraft_servers | dict2items` and sets `state` per item. Use `loop: "{{ access_cidrs | product(minecraft_servers | dict2items) | list }}"`, with `item.1.value.port`, `item.1.key` in the state expression, and `label: "{{ item.0 }} {{ item.1.key }}"`.
  - **svc-infra secret catalog:** the `ui_port` task's comment warns that looping over catalog entries would leak secrets. Keep its port-list expression and wrap it: `loop: "{{ access_cidrs | product(<existing port list expression>) | list }}"`.
  - **Removing a tailnet rule later** (on a host that leaves `tailnet_routers`) needs `state: disabled` for the `100.64.0.0/10` variant. Write that down in a comment on `access_cidrs`; don't build it now (YAGNI).
  - Do **not** touch `roles/svc_download/templates/host-backstop.nft.j2` (`LAN_ADMIN`) or `roles/svc_infra/templates/chat-egress.nft.j2` (`LAN`). They're nftables `define`s, not firewalld rich rules, the gate's regex doesn't match them, and svc-download is not a router.

- [ ] **Step 3: Run the gates.** `.venv/bin/python tests/validate_tailscale_router.py` → `OK (2 routers, version 1.98.9 pinned)`, then `make validate` → green.

- [ ] **Step 4: Dry-run on svc-media only** to see the rule diff without applying it: `make check ARGS="--limit svc-media --tags access"` (check the Makefile `check` target's exact name first). Expected: new `100.64.0.0/10` rich rules only, and no LAN rule removed.

- [ ] **Step 5: Commit.**

```bash
git add roles/svc_media/tasks/access.yml roles/svc_infra/tasks/files.yml roles/mon/tasks/main.yml
git commit -m "feat: admit tailnet sources wherever router hosts admit the LAN"
```

---

### Task 5: Move the route check into the role; add standby, key-expiry and HC gating (TDD)

**Files:**
- Move (`git mv`): `roles/svc_media/templates/tailnet-route.sh.j2` → `roles/tailscale_router/templates/tailnet-route.sh.j2`; `roles/svc_media/files/homelab-tailnet-route.service` and `.timer` → `roles/tailscale_router/files/`
- Modify: `roles/tailscale_router/tasks/main.yml` (append install tasks), `roles/svc_media/tasks/access.yml` (remove the three moved tasks), `roles/svc_media/tasks/verify.yml` (remove the timer + `Check the Tailscale subnet route` + `Record the tailnet route result` tasks)
- Create: `roles/tailscale_router/tasks/verify.yml`
- Modify: `verify.yml`, `inventory/host_vars/svc-infra.yml`, `tests/validate_tailnet_route.py`, `tests/validate_shell_templates.py`

**Interfaces:**
- Consumes: `tailnet_key_expiry_days`, `tailnet_route_hc_host`, `lan_cidr`.
- Produces: facts `tailnet_router_verified` (bool), `tailnet_route_ok` (bool) and `tailnet_route_detail` (str) on each router; the script's exit contract is unchanged (0 OK / 1 down / 2 cannot look).

- [ ] **Step 1: Write the failing test cases.** In `tests/validate_tailnet_route.py`:
  - Point `TEMPLATE` at `roles/tailscale_router/templates/tailnet-route.sh.j2`.
  - Extend `render()` to substitute `{{ tailnet_key_expiry_days }}` → `21` and the HC line (below) → `HC_ENABLED=1`.
  - Give `node()` a `key_expiry: str | None = None` parameter that sets `"KeyExpiry"` when not None.
  - Add the cases below. The "expiry soon" timestamp is computed at runtime:

```python
from datetime import datetime, timedelta, timezone

SOON = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
LATER = (datetime.now(timezone.utc) + timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")

# appended to CASES (same tuple shape):
    ("primary router key expiring", netmap(SELF, node("edgar", "100.93.219.13", True, [CIDR], SOON)),
     0, PONG, 0, "0", 1, "fail", "node key expires"),
    ("this node's key expiring", netmap(node("svc-media", "100.93.88.34", True, None, SOON), EDGAR_UP),
     0, PONG, 0, "0", 1, "fail", "node key expires"),
    ("keys far from expiry", netmap(node("svc-media", "100.93.88.34", True, None, LATER),
                                    node("edgar", "100.93.219.13", True, [CIDR], LATER)),
     0, PONG, 0, "0", 0, "", "OK via edgar"),
```

  - Add one HC-gating case outside `CASES`: render with `HC_ENABLED=0`, run the healthy netmap, and assert `hc.log` does **not** exist and rc is 0.

- [ ] **Step 2: Run it and watch it fail.** First `git mv` the template, then run `.venv/bin/python tests/validate_tailnet_route.py`. Expected: FAIL on the new expiry cases (rc 0 instead of 1) and on the HC-gating case.

- [ ] **Step 3: Implement in the template.**
  - Replace the hard-coded `HC_PING=/usr/local/sbin/hc-ping.sh` block's call sites with a gate. Add after `FORWARD_FILE=…`:

```bash
# Only one router reports to healthchecks.io, so two hosts never send one
# check conflicting pings. The others still fail their unit (ntfy) and verify.
HC_ENABLED={{ (inventory_hostname == tailnet_route_hc_host) | ternary('1', '0') }}
EXPIRY_DAYS={{ tailnet_key_expiry_days }}
```

  - In `report()`, wrap the warning and both `"$HC_PING"` calls in `if [[ "$HC_ENABLED" == 1 ]]; then … fi`.
  - Before the `routers=` computation, add the expiry check for Self and every primary-router peer:

```bash
# A router whose node key expires drops off the tailnet silently, exactly
# like edgar's failed update did. Keys measured 2026-11-11 expired 2027-01-14
# (svc-media) and 2027-01-15 (edgar). Null KeyExpiry = expiry disabled.
expiring=$(jq -r --arg cidr "$CIDR" --argjson days "$EXPIRY_DAYS" '
    ([.Self] + [(.Peer // {})[] | select((.PrimaryRoutes // []) | index($cidr))])[]
    | select(.KeyExpiry != null)
    | select((.KeyExpiry | sub("\\.[0-9]+"; "") | fromdateiso8601) - now < ($days * 86400))
    | "\(.HostName) \(.KeyExpiry)"
' <<<"$status")
if [[ -n "$expiring" ]]; then
    report 1 "ROUTE AT RISK: node key expires within ${EXPIRY_DAYS} days — disable key expiry in the Tailscale admin console (Machines → ⋯ → Disable key expiry): ${expiring//$'\n'/; }"
fi
```

  - Update the header comment: the check now lives in `roles/tailscale_router`, runs on every router, and alarms on key expiry.
  - Add to `tests/validate_shell_templates.py` `context`: `"tailnet_key_expiry_days": main_vars["tailnet_key_expiry_days"]`, `"tailnet_route_hc_host": "svc-media"`, `"inventory_hostname": "svc-media"`. Check that `inventory_hostname` isn't already set elsewhere in that context first.

- [ ] **Step 4: Run the tests until they pass.** `.venv/bin/python tests/validate_tailnet_route.py` → OK (14 cases + the HC-gating case). Then mutation-check: temporarily change `< ($days * 86400)` to `< 0` and confirm the expiry cases go red. Restore the line.

- [ ] **Step 5: Append the install tasks** to `roles/tailscale_router/tasks/main.yml`. Move the three tasks verbatim from `roles/svc_media/tasks/access.yml` (`Install the tailnet route check`, `Install the tailnet route check units`, `Arm the tailnet route check`) and delete them there along with their comment block. Then add `homelab-tailnet-route.service` to `onfailure_units_extra` in `inventory/host_vars/svc-infra.yml`; svc-media already has it.

- [ ] **Step 6: Write `roles/tailscale_router/tasks/verify.yml`.** It must be read-only and must not need the vault: `verify.yml` also runs on the nightly runner, sometimes without one.

```yaml
---
- name: Read Tailscale status for verification
  ansible.builtin.command: tailscale status --json
  register: tailnet_verify_status
  changed_when: false

- name: Read Tailscale prefs for verification
  ansible.builtin.command: tailscale debug prefs
  register: tailnet_verify_prefs
  changed_when: false

# This is the standby assertion: EVERY router must be logged in and
# advertising, not just whichever one holds PrimaryRoutes. A standby that
# quietly stopped advertising is invisible to the route check until the
# primary fails — at which point there is no failover.
- name: Assert this router is logged in, advertising, and not auto-updating
  ansible.builtin.assert:
    that:
      - (tailnet_verify_status.stdout | from_json).BackendState == 'Running'
      - ((tailnet_verify_prefs.stdout | from_json).AdvertiseRoutes | default([], true) | sort)
        == (tailscale_advertise_routes | sort)
      - not ((tailnet_verify_prefs.stdout | from_json).RouteAll | bool)
      - not ((tailnet_verify_prefs.stdout | from_json).CorpDNS | bool)
    fail_msg: >-
      {{ inventory_hostname }} is not a healthy standby router (not Running,
      not advertising {{ tailscale_advertise_routes | join(',') }}, or accepting
      routes/DNS). Run make media/infra and read the role's login gate.

- name: Assert IPv4 forwarding is on
  ansible.builtin.command: sysctl -n net.ipv4.ip_forward
  register: tailnet_verify_forward
  changed_when: false
  failed_when: tailnet_verify_forward.stdout | trim != '1'

- name: Verify the tailnet route check timer is active and enabled
  ansible.builtin.command:
    argv: [systemctl, "{{ item }}", homelab-tailnet-route.timer]
  loop: [is-active, is-enabled]
  changed_when: false

- name: Check the Tailscale subnet route
  ansible.builtin.command: /usr/local/sbin/homelab-tailnet-route.sh
  register: tailnet_route_gate
  changed_when: false
  failed_when: false

- name: Record the route result for the final gate
  ansible.builtin.set_fact:
    tailnet_route_ok: "{{ tailnet_route_gate.rc | default(1) == 0 }}"
    tailnet_route_detail: >-
      {{ (tailnet_route_gate.stderr | default('') ~ ' '
      ~ tailnet_route_gate.stdout | default('')) | trim }}
    tailnet_router_verified: true
```

**Measure, don't guess, whether approval is visible locally.** After Task 6 approves svc-infra's route while it is *not* primary, run `tailscale status --json | jq .Self` on svc-infra and diff it against before approval. If a field reveals approval (for example `AllowedIPs` containing the CIDR), add it to the standby assert above. If none does, write that down in a comment; the admin console is then the only place approval shows.

- [ ] **Step 7: Wire it into `verify.yml`.**
  - In the first play's block, after `Verify host monitoring`, add:

```yaml
        - name: Verify the Tailscale subnet router
          ansible.builtin.include_role:
            name: tailscale_router
            tasks_from: verify
          when: "'tailnet_routers' in group_names"
```

  - In the final localhost play, replace the `Assert remote access has a working Tailscale subnet route` task (which reads `hostvars[media_host].media_tailnet_route_*`) with:

```yaml
    - name: Assert every subnet router is healthy and the route works
      ansible.builtin.assert:
        that:
          - groups['tailnet_routers'] | map('extract', hostvars, 'tailnet_router_verified')
            | map('default', false) | map('bool') | min
          - groups['tailnet_routers'] | map('extract', hostvars, 'tailnet_route_ok')
            | map('default', false) | map('bool') | min
        fail_msg: >-
          Remote access is down or unprotected:
          {% for h in groups['tailnet_routers'] %}{{ h }}: {{ hostvars[h].tailnet_route_detail
          | default('route check did not run') }}. {% endfor %}
```

  - Remove the svc-media gate tasks listed under **Files** from `roles/svc_media/tasks/verify.yml`.

- [ ] **Step 8: `make validate`** → green. That includes `validate_onfailure.py`, which must still find an OnFailure entry for the timer's service on both hosts.

- [ ] **Step 9: Commit.**

```bash
git add roles/tailscale_router roles/svc_media/tasks/access.yml roles/svc_media/tasks/verify.yml \
        verify.yml inventory/host_vars/svc-infra.yml tests/validate_tailnet_route.py \
        tests/validate_shell_templates.py
git status --porcelain   # git mv'd paths must show as renames, nothing untracked
git commit -m "feat: run the route check on every router and alarm on node-key expiry"
```

---

### Task 6: Live cut-over in an order that never drops the route

**Files:** none (ops). Run every command from TERRA WSL with the current vault. **Read every step's expected output before moving on.** If anything is unexpected, stop: the route is still on edgar and nothing is lost.

- [ ] **Step 1: Deploy svc-media only.** `make media` (its `--limit svc-media` includes the new play).
  - **Expected:** svc-media is already `Running`, so the login gate passes and `Apply router prefs` reports `changed` once (advertise on; CorpDNS, RouteAll and auto-update off). The sysctl, firewalld interface and `100.64.0.0/10` rich rules also report changed.
  - Then check:

```bash
ssh straderb@192.168.1.30 'head -3 /etc/resolv.conf; getent hosts hc-ping.com; sysctl -n net.ipv4.ip_forward;
  sudo tailscale debug prefs | jq -c "{AdvertiseRoutes,CorpDNS,RouteAll,AutoUpdate}"'
```

  - **Expected:** `nameserver 192.168.1.1` (no longer `100.100.100.100`), `hc-ping.com` resolves, forwarding is `1`, and the prefs match. **If resolv.conf is still MagicDNS or empty, stop**: NetworkManager may need `nmcli con up eth0` to rewrite it. Fix that before anything else; every svc-media job that resolves names depends on it.

- [ ] **Step 2: Approve svc-media's route** in the admin console (Machines → svc-media → Edit route settings → enable `192.168.1.0/24`), and **disable key expiry** for svc-media (decision D2).
  - **Expected** from the Mac: edgar is *still* primary (incumbent), and svc-media is approved but standby.
  - Run Task 1 Step 2's command again: edgar should still be the only line, and svc-media's `keyExpiry` should now be `none`.

- [ ] **Step 3: Install svc-infra.** `make infra`.
  - **Expected:** it **fails at the login gate** with the exact `ssh … sudo tailscale up …` command. That's by design (decision D1).
  - Run that command; it prints a URL. Open it and log in as the tailnet owner (`brandon.strader@`).
  - **Do not approve svc-infra's route yet.** D4's ordering needs svc-media to be the only approved candidate when edgar is withdrawn (Step 5); svc-infra's approval is Step 6.
  - Disable key expiry for svc-infra.
  - Re-run `make infra`. **Expected:** green. svc-infra's own route check passes, pinging edgar.

- [ ] **Step 4: Measure standby visibility** (Task 5 Step 6's note). Compare `tailscale status --json | jq .Self` on svc-media before and after approval, record the finding, and add it to the verify assert if a field reveals approval.

- [ ] **Step 5: Move the primary to svc-media (D4).** With svc-infra's route **not yet approved** (or not yet advertising), withdraw edgar. In the Windows GUI, untick "Run as subnet router", or from the PVE host:

```bash
ssh root@192.168.1.10 'qm guest exec 108 --timeout 60 -- "C:\\Program Files\\Tailscale\\tailscale.exe" set --advertise-routes= --auto-update=false'
```

  - Then within about 30 seconds run Task 1 Step 2 again. **Expected:** `svc-media online=true primary=["192.168.1.0/24"]`.
  - From svc-infra: `tailscale ping -c 3 100.93.88.34` gives a pong.
  - Remove edgar's route approval in the admin console too, so a future re-advertise can't silently reclaim it.

- [ ] **Step 6: Approve svc-infra's route.** **Expected:** svc-media stays primary and svc-infra is standby. `make verify` → green, including `Assert every subnet router is healthy and the route works`.

- [ ] **Step 7: Use the application from off the LAN.** On a phone on **cellular** (Wi-Fi off) with Tailscale on:
  - Open `https://home.fortwow.dev` and `https://jellyfin.fortwow.dev`; both load with a normal padlock.
  - In the Tailscale app, check that `svc-media` shows as the subnet router.
  - On the router, `sudo journalctl -u caddy --since -5min | grep -c 100.` should show a non-zero count: the household now reaches Caddy with real `100.x` client addresses, not edgar's `192.168.1.159`.
  - Record what was tested and the result. Optionally ask Valerie to open Jellyfin on the TV at her place.

---

### Task 7: Prove failover and alerting (agreed window, D6)

**Files:** none (ops).

- [ ] **Step 1: Announce the window to the household.** Then on the **primary** (svc-media):

```bash
ssh straderb@192.168.1.30 'date; sudo systemctl stop tailscaled'
```

- [ ] **Step 2: Watch failover.** From the Mac, repeat Task 1 Step 2 every 10 seconds until `svc-infra` shows as primary. Record the seconds taken. The phone on cellular should reload `https://home.fortwow.dev` successfully, now routed via svc-infra and SNATed to `192.168.1.32`.

- [ ] **Step 3: Watch the alert.** On svc-infra, `sudo systemctl start homelab-tailnet-route.service; journalctl -u homelab-tailnet-route -n 5`.
  - **Expected:** OK via svc-infra (this node), forwarding 1.
  - svc-media's own check is now "could not look" (tailscaled stopped), so its unit fails → ntfy. Read it back: `curl -s "http://192.168.1.30:8080/homelab-alerts/json?poll=1&since=10m"`. In healthchecks.io, `homelab-tailnet-route` shows a `/fail` ping.

- [ ] **Step 4: Restore.** `ssh straderb@192.168.1.30 'sudo systemctl start tailscaled'`.
  - **Expected:** svc-infra **stays** primary. Tailscale doesn't fail back, which is acceptable per D4.
  - If you want svc-media primary again, briefly clear and re-set svc-infra's advertisement: `sudo tailscale set --advertise-routes=` then `sudo tailscale set --advertise-routes=192.168.1.0/24`. Do it on svc-infra only, and only after svc-media shows `online=true`.
  - Then `make verify` → green, and healthchecks.io returns to up.

- [ ] **Step 5: Record in the commit message** of Task 8: the failover seconds, which router ended as primary, and that the alert was seen in ntfy and healthchecks.io. If any step didn't happen, say so plainly.

---

### Task 8: Docs, final proof, merge

**Files:**
- Modify: `docs/architecture.md` (the "Remote access is one Tailscale subnet route…" paragraph under *Storage and access*): say there are two Ansible-managed routers (svc-media primary-by-default, svc-infra standby), edgar is retired from routing, why tailnet sources are admitted on router hosts, and that routers run with auto-update, accept-routes and accept-dns off.
- Modify: `docs/dns-pfsense-caddy.md` — step "Confirm the dedicated subnet router still advertises…" (~line 89) now names `tailnet_routers`. The troubleshooting row for "LAN works but tailnet does not" adds `make verify` and the per-router route check.
- Modify: `README.md` — the line describing "a dedicated subnet-router VM on thurgadin's network". Also check `docs/services.md:185`.
- **Household guide:** no change. Addresses and apps are identical for users. Say so in the commit message rather than editing `docs/household/*`.

- [ ] **Step 1: Update the docs** as listed. Run `make validate` (the link and household-guide gates included) → green.

- [ ] **Step 2: Commit the docs**, then confirm a clean tree:

```bash
git add docs/architecture.md docs/dns-pfsense-caddy.md README.md docs/services.md
git commit -m "docs: two managed subnet routers replace edgar"
git status --porcelain     # must print nothing
```

- [ ] **Step 3: Final proof from the clean tree.**

```bash
caffeinate -dims make deploy-proof TARGET=media       # PROOF OK: clean
caffeinate -dims make deploy-proof TARGET=infra       # first: sync trio only (expected)
caffeinate -dims make deploy-proof TARGET=infra       # second: PROOF OK: clean
caffeinate -dims make verify                          # green on every host
```

Any `changed` beyond the infra sync trio means deployed state and the commit differ. Explain it before merging. A likely culprit is `Apply router prefs` firing every run because the prefs comparison is wrong. Fix the comparison; don't silence it.

- [ ] **Step 4: Merge, push, clean up.**

```bash
git switch main && git pull --ff-only
git merge --ff-only feat/tailscale-subnet-routers
git push origin main
git branch -d feat/tailscale-subnet-routers
```

Then confirm CI on `main` went green using the `gh` recipe in `docs/deployment.md`.

---

## Rollback

The route stays recoverable at every step, because edgar keeps Tailscale installed (D5):

1. **Restore edgar as router:** `qm guest exec 108 -- "C:\Program Files\Tailscale\tailscale.exe" set --advertise-routes=192.168.1.0/24`, then re-approve the route in the admin console.
2. **Withdraw the Linux routers:** `sudo tailscale set --advertise-routes=` on svc-infra, then svc-media, and remove their approvals. Do this only after edgar shows `online=true` as primary.
3. **Code:** `git revert` the merge's commits and redeploy `make media` / `make infra`.
   - The `100.64.0.0/10` rich rules aren't removed by a revert; the reverted tasks simply stop managing them. Remove them by hand with `firewall-cmd --permanent --remove-rich-rule=…` if you want them gone.
   - The sysctl file `/etc/sysctl.d/90-tailscale-router.conf` also remains; delete it and `sysctl -w net.ipv4.ip_forward=0`.
4. **Do not** run `tailscale set --accept-dns=true` on svc-media as part of rollback. Its resolver going back to MagicDNS was a hidden dependency, not a feature.

## Self-review notes (for the reviewer)

- Re-verify every `~line` reference against HEAD before editing; they were read on 2026-11-11 at `28a3642`.
- Shell command quoting inside `qm guest exec` for Windows paths was tested only for `cmd /c`-style calls. If it misparses, write a `.ps1` into the guest with `pvesh create /nodes/$(hostname)/qemu/108/agent/file-write` and run it with `powershell -File`. That pattern worked on 2026-11-11.
- `qm guest exec --timeout N` returning does **not** stop the guest process. Never re-run a destructive guest script to "check status".
