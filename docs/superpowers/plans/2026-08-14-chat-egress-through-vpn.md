# Chat Egress Through the VPN — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route Open WebUI's outbound HTTP traffic through a forward proxy inside svc-download's Mullvad namespace, and enforce it with a cgroup-scoped nftables policy on svc-infra so traffic that ignores the proxy is dropped rather than leaked.

**Architecture:** A tinyproxy container runs inside the existing `vpn` netns on svc-download, so its egress leaves via `wg0` by construction. svc-infra reaches it through a `systemd-socket-proxyd` listener scoped to svc-infra's address alone. Open WebUI gets `http_proxy`/`https_proxy` pointing at it, and a new nftables table on svc-infra drops any non-LAN packet originating from open-webui's cgroup. A three-check probe proves the path is VPN'd, the enforcement is live, and the probe itself is not dead.

**Tech Stack:** Ansible, Podman Quadlet (rootless on svc-infra, rootful in the jail on svc-download), systemd socket proxy, nftables, tinyproxy, ntfy, node_exporter textfile collector.

**Spec:** `docs/superpowers/specs/2026-08-13-chat-egress-through-vpn-design.md`

## Global Constraints

- **Worktree:** all work happens in `C:\Users\tv\dev\homelab-ironwood-chat-egress` on branch `feat/chat-egress-vpn`. The main checkout is on another branch with concurrent work — do not touch it.
- **Never echo vault secrets.** Secret-bearing tasks use `no_log: true`. Nothing in this plan handles a secret, so if you find yourself adding one, stop and re-read the spec.
- **Never `git add -A`.** Stage explicit paths.
- **Never hand-edit an image digest.** Use `make image-digest REF=…`; the repo rejects tag refs.
- **Deploy order per CLAUDE.md:** edit → `make validate` → iterate with `make dl`/`make infra` → commit → clean tree → final deploy reporting `changed=0` → `make verify`. On svc-infra the first deploy after a commit reports `changed=3` (the `/opt/homelab-iac` archive sync); run `make infra` again and require `changed=0`.
- **Proxy port:** `8118`. Verified free in the shared jail netns (taken: 8080, 5800, 5299, 6767, 7878, 8084, 8888, 8989, 9696).
- **Logging default:** `chat_proxy_log_requests: true`. Governs both the proxy's `LogLevel` and the nftables drop rule's `log` statement. `counter` on the drop rule is unconditional in both states.
- **Fail-closed:** no fallback to direct egress may be added, in any task, for any reason.
- **`make validate` cannot run on the Windows workstation** (no `make`, no venv). Run individual validators with `python tests/validate_<name>.py`. The full gate runs in CI on push to `main`.

---

## File Structure

**New — svc-download (the jail side):**
- `roles/svc_download/templates/chat-proxy.container.j2` — bespoke Quadlet, modelled on `recyclarr.container.j2`. Runs tinyproxy in `ns:/run/netns/vpn`.
- `roles/svc_download/templates/tinyproxy.conf.j2` — proxy config; `LogLevel` driven by `chat_proxy_log_requests`.
- `roles/svc_download/files/chat-proxy-relay.socket` / `.service` — the LAN→jail socket proxy. Static files, not templates: unlike the catalog's generated proxies there is exactly one of these, and its listen address comes from a drop-in rendered in `files.yml`.

**New — svc-infra (the enforcement side):**
- `roles/svc_infra/templates/chat-egress.nft.j2` — the `inet chat_egress` table.
- `roles/svc_infra/files/chat-egress.service` — loads the table at boot; own unit rather than `nftables.service`, which would risk firewalld's ruleset.
- `roles/svc_infra/templates/chat-egress-probe.sh.j2` — the three-check probe.
- `roles/svc_infra/files/homelab-chat-egress.service` / `.timer` — runs the probe.
- `roles/svc_infra/tasks/chat-egress.yml` — imported from `main.yml`.

**Modified:**
- `inventory/group_vars/all/main.yml` — `chat_proxy_*` vars, `scan_images` gains the proxy image.
- `inventory/group_vars/all/infra-apps.yml` — open-webui env gains proxy vars.
- `roles/svc_download/tasks/files.yml`, `apps.yml`, `images.yml` — render/start the proxy.
- `roles/svc_download/templates/host-backstop.nft.j2` — the `$INFRA_HOST` input rule.
- `roles/svc_infra/tasks/main.yml` — import `chat-egress.yml`.
- `tests/validate_shell_templates.py`, `tests/validate_alert_topics.py` — register the new script.
- `tests/validate_chat_egress.py` (new) — the anti-drift validator.
- `docs/services.md`, `docs/security.md`.

---

## Task 1: Confirm `socket cgroupv2` matching works on svc-infra

**This is a gate, not a build step.** The entire enforcement design depends on nftables being able to match a rootless container's traffic by cgroup. If it does not work, **stop and report** — the escalation is a dedicated podman user matched by `meta skuid`, which is a different plan. Do not silently fall back to `http_proxy`-only; that was explicitly declined.

**Files:**
- Create: `docs/superpowers/plans/notes/2026-08-14-cgroup-match-finding.md` (the recorded finding)

**Interfaces:**
- Produces: the verified cgroup path string and its level count, consumed by Task 4's `chat-egress.nft.j2`.

- [ ] **Step 1: Check the tooling supports the match at all**

On svc-infra:

```bash
nft --version
uname -r
```

Expected: nft ≥ 0.9.6 and kernel ≥ 5.13. Rocky 9 ships nft 1.0.x on kernel 5.14, so this should pass. If it does not, stop here.

- [ ] **Step 2: Read the container's real cgroup path**

Do not guess the path. Ask systemd:

```bash
sudo -u homelab XDG_RUNTIME_DIR=/run/user/$(id -u homelab) \
    systemctl --user show open-webui.service -p ControlGroup
```

Expected output shape: `ControlGroup=/user.slice/user-<uid>.slice/user@<uid>.service/app.slice/open-webui.service`

Record the exact string. Count the path components after the leading `/` — that count is the `level` value. The spec assumes 5; **use what the host reports, not the assumption.**

- [ ] **Step 3: Load a throwaway counting table**

Substitute the path and level from Step 2:

```bash
sudo nft -f - <<'EOF'
table inet chat_egress_probe {
    chain output {
        type filter hook output priority 0; policy accept;
        socket cgroupv2 level 5 "user.slice/user-1000.slice/user@1000.service/app.slice/open-webui.service" counter
    }
}
EOF
```

If `nft` rejects the syntax, the kernel or nft build lacks cgroupv2 socket matching. Stop and report.

- [ ] **Step 4: Generate traffic and confirm the counter moves**

This is the positive control. A rule that loads but matches nothing is the failure mode this whole step exists to catch.

```bash
sudo nft reset counters table inet chat_egress_probe
sudo -u homelab podman exec open-webui curl -s -o /dev/null --max-time 10 https://example.com
sudo nft list table inet chat_egress_probe
```

Expected: a non-zero `counter packets N bytes M`.

**If the counter stays at zero the design does not work as specified.** The likely cause is that rootless podman re-originates traffic from a process outside the unit's cgroup. Report this and stop.

- [ ] **Step 5: Confirm the match is specific, not universal**

Equally important: the rule must *not* match everything. Generate traffic from a different unit and confirm the counter does not move.

```bash
sudo nft reset counters table inet chat_egress_probe
sudo -u homelab podman exec uptime-kuma curl -s -o /dev/null --max-time 10 https://example.com || \
    curl -s -o /dev/null --max-time 10 https://example.com
sudo nft list table inet chat_egress_probe
```

Expected: counter still zero. A rule that matches all traffic would take the whole VM offline in Task 4.

- [ ] **Step 6: Remove the throwaway table**

```bash
sudo nft delete table inet chat_egress_probe
sudo nft list ruleset | grep -c chat_egress_probe   # expect 0
```

- [ ] **Step 7: Check whether a firewalld reload survives a foreign table**

Task 4's table must coexist with firewalld. Confirm firewalld does not flush it:

```bash
sudo nft -f - <<'EOF'
table inet chat_egress_probe {
    chain output { type filter hook output priority 0; policy accept; }
}
EOF
sudo firewall-cmd --reload
sudo nft list tables | grep chat_egress_probe   # must still be listed
sudo nft delete table inet chat_egress_probe
```

If firewalld removes it, Task 4 needs a reload hook — record that here.

- [ ] **Step 8: Record the finding and commit**

Write `docs/superpowers/plans/notes/2026-08-14-cgroup-match-finding.md` containing: nft and kernel versions, the exact `ControlGroup` string, the level count, the observed counter values from Steps 4 and 5, and the firewalld-reload result from Step 7.

```bash
git add docs/superpowers/plans/notes/2026-08-14-cgroup-match-finding.md
git commit -m "docs: record that nftables can match open-webui's cgroup on svc-infra

The enforcement half of the chat egress design rests on nftables being
able to single out one rootless container, which rootless podman makes
non-obvious: the packets are re-originated by a userspace process and
carry the host's address. Records the measured cgroup path, the level,
and both directions of the control -- traffic from chat moves the
counter, traffic from another unit does not.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: The forward proxy inside the jail

**Files:**
- Create: `roles/svc_download/templates/tinyproxy.conf.j2`
- Create: `roles/svc_download/templates/chat-proxy.container.j2`
- Create: `roles/svc_download/files/chat-proxy-relay.socket`
- Create: `roles/svc_download/files/chat-proxy-relay.service`
- Modify: `inventory/group_vars/all/main.yml` (vars + `scan_images`)
- Modify: `roles/svc_download/templates/host-backstop.nft.j2`
- Modify: `roles/svc_download/tasks/images.yml`, `files.yml`, `apps.yml`

**Interfaces:**
- Produces: `chat_proxy_port` (8118), `chat_proxy_image`, `chat_proxy_log_requests` — consumed by Tasks 3, 4, 5.
- Produces: a reachable HTTP proxy at `{{ hostvars[download_host].ansible_host }}:8118` from svc-infra only.

- [ ] **Step 1: Choose and pin the image**

The repo rejects tag refs, so resolve a digest. Prefer an image that is maintained and small. Try, in order:

```bash
make image-digest REF=ghcr.io/tinyproxy/tinyproxy:latest
make image-digest REF=docker.io/vimagick/tinyproxy:latest
```

Acceptance criteria, all required: the image must resolve, must run tinyproxy as container-root (the jail's Quadlet does not set `User=`), and must read a config from a bind-mounted path. If none qualifies, **fall back to Squid** and say so in the commit message rather than forcing a bad fit — the spec names this fallback explicitly.

Record the digest for Step 2.

- [ ] **Step 2: Add the inventory variables**

In `inventory/group_vars/all/main.yml`, near the Recyclarr block (~line 200):

```yaml
# --- Chat egress proxy (v6, download jail) ---------------------------------
# Open WebUI on svc-infra fetches the pages SearXNG returns, and did so
# straight out of the house until 2026-08-13 — SearXNG's jail covered the
# query and never covered the fetch. This proxy is the fetch's way into the
# tunnel. Bespoke, not a download_apps entry: `proxy: true` publishes a Caddy
# vhost and an HTTP health check, and a forward proxy can serve neither.
chat_proxy_image: "ghcr.io/tinyproxy/tinyproxy@sha256:REPLACE_WITH_STEP_1_DIGEST"
chat_proxy_port: 8118
# Logs the destinations chat reaches for. On by default: this is the only
# deliberate record of what chat touches, and it is what makes the egress
# audit in the design doc answerable. Setting it false drops per-request
# lines from the proxy AND the `log` statement from svc-infra's drop rule —
# one variable, because two that could disagree would make `false` a setting
# that does not mean what it says. Errors log in both states, and the drop
# rule's `counter` is unconditional, so turning this off never weakens the
# probe in roles/svc_infra/templates/chat-egress-probe.sh.j2.
chat_proxy_log_requests: true
# How long the proxy's request log is retained. Stated rather than inherited:
# an always-on log grows until something bounds it, and "whatever journald
# defaults to" is not a window anyone can quote.
chat_proxy_log_retention: 14d
```

Add `chat_proxy_image` to `scan_images` (~line 520), in the final literal list:

```yaml
      + [minecraft_image, beszel_agent_image, node_exporter_image, recyclarr_image,
         chat_proxy_image]
```

- [ ] **Step 3: Write the proxy config template**

Create `roles/svc_download/templates/tinyproxy.conf.j2`:

```jinja
{{ ansible_managed | comment }}
# /srv/appdata/chat-proxy/tinyproxy.conf  (inside the vpn netns)
#
# Forward proxy for Open WebUI on svc-infra. Everything it accepts leaves via
# wg0, because the only route out of this namespace is wg0.

User root
Group root

# 0.0.0.0, not loopback: the socket proxy in the init namespace dials
# 10.77.0.2:{{ chat_proxy_port }} across the veth, so a loopback bind is
# unreachable. Same reasoning as SearXNG's GRANIAN_HOST.
Listen 0.0.0.0
Port {{ chat_proxy_port }}

Timeout 600

# Access control here is COSMETIC and must not be relied on. Every request
# arrives from 10.77.0.1 regardless of who sent it, because the socket proxy
# re-originates it — the same fact that forces SearXNG to run limiter:false.
# The real restriction is the $INFRA_HOST rule in host-backstop.nft.j2.
Allow 10.77.0.1

{% if chat_proxy_log_requests %}
# Connect: logs each request's destination. See chat_proxy_log_requests.
LogLevel Connect
{% else %}
# Error: startup and failure messages only, no per-request destinations.
LogLevel Error
{% endif %}

# CONNECT is how HTTPS is proxied; without these ports nothing but plaintext
# works. 443 for HTTPS, 563 kept off deliberately — this list is destinations
# chat may reach, not a general-purpose relay.
ConnectPort 443

# No ViaProxyName header leak of the internal hostname.
DisableViaHeader Yes
```

- [ ] **Step 4: Write the Quadlet**

Create `roles/svc_download/templates/chat-proxy.container.j2`, modelled directly on `recyclarr.container.j2`:

```jinja
{{ ansible_managed | comment }}
# /etc/containers/systemd/chat-proxy.container   (inside vpn netns)
# NOT catalog-managed (no download_apps entry): `proxy: true` generates a Caddy
# vhost, an SSO gate, a dashboard tile and an HTTP health check that expects a
# 200 from a page. A forward proxy serves none of those and would fail the
# generated verify probe. Same reasoning as recyclarr.container.j2 next door.
[Unit]
Description=Chat egress forward proxy (inside vpn netns)
Requires=vpn-netns.service
After=vpn-netns.service
BindsTo=vpn-netns.service
PartOf=vpn-netns.service

[Container]
Image={{ chat_proxy_image }}
ContainerName=chat-proxy
Environment=TZ={{ timezone }}
PodmanArgs=--network=ns:/run/netns/vpn
DNS={{ mullvad_dns }}
Volume=/srv/appdata/chat-proxy/tinyproxy.conf:/etc/tinyproxy/tinyproxy.conf:Z,ro

[Service]
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`DNS={{ mullvad_dns }}` matters more here than anywhere: it means the proxy resolves destination hostnames through Mullvad's resolver inside the namespace, which is what keeps DNS off the home connection for every name chat reaches.

- [ ] **Step 5: Write the socket proxy units**

Create `roles/svc_download/files/chat-proxy-relay.socket`:

```ini
# homelab-iac managed — roles/svc_download/files/chat-proxy-relay.socket
[Unit]
Description=Chat egress proxy socket (LAN to VPN namespace)

[Socket]
# ListenStream comes from a drop-in rendered by files.yml; this host's LAN
# address is inventory data and does not belong in a static file.
FreeBind=yes

[Install]
WantedBy=sockets.target
```

Create `roles/svc_download/files/chat-proxy-relay.service`:

```ini
# homelab-iac managed — roles/svc_download/files/chat-proxy-relay.service
[Unit]
Description=Chat egress proxy relay
Requires=vpn-netns.service chat-proxy.service
After=vpn-netns.service chat-proxy.service

[Service]
ExecStart=/usr/lib/systemd/systemd-socket-proxyd --exit-idle-time=5min 10.77.0.2:8118
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
NoNewPrivileges=yes
```

- [ ] **Step 6: Add the backstop input rule**

In `roles/svc_download/templates/host-backstop.nft.j2`, in the `input` chain immediately before the node_exporter rule (~line 105):

```jinja
        # Chat egress proxy. open-webui on {{ infra_host }} is the only
        # consumer, so this is scoped to that host like node_exporter below
        # rather than to $LAN_ADMIN — and that scoping is the ONLY place the
        # restriction can live, because tinyproxy sees every caller as
        # 10.77.0.1 and cannot tell them apart.
        ip saddr $INFRA_HOST tcp dport {{ chat_proxy_port }} accept
```

**Do not add the port to the `$LAN_ADMIN` catalog-port set** on line 103. `tests/validate_generated_catalog.py` asserts that set equals the proxy-eligible catalog ports plus 9090; adding to it fails the gate, correctly.

- [ ] **Step 7: Wire up the Ansible tasks**

In `roles/svc_download/tasks/images.yml`, add `chat_proxy_image` to the pre-pull list alongside `recyclarr_image`.

In `roles/svc_download/tasks/files.yml`, after the Recyclarr block:

```yaml
- name: Ensure the chat egress proxy appdata directory exists
  ansible.builtin.file:
    path: /srv/appdata/chat-proxy
    state: directory
    owner: root
    group: root
    mode: "0755"

- name: Render the chat egress proxy configuration
  ansible.builtin.template:
    src: tinyproxy.conf.j2
    dest: /srv/appdata/chat-proxy/tinyproxy.conf
    owner: root
    group: root
    mode: "0644"
  register: download_chat_proxy_config

- name: Render the chat egress proxy Quadlet
  ansible.builtin.template:
    src: chat-proxy.container.j2
    dest: /etc/containers/systemd/chat-proxy.container
    owner: root
    group: root
    mode: "0644"
  register: download_chat_proxy_quadlet

- name: Install the chat egress proxy relay units
  ansible.builtin.copy:
    src: "{{ item }}"
    dest: "/etc/systemd/system/{{ item }}"
    owner: root
    group: root
    mode: "0644"
  loop:
    - chat-proxy-relay.socket
    - chat-proxy-relay.service
  register: download_chat_proxy_relay_units

- name: Bind the chat egress proxy relay to this host's LAN address
  ansible.builtin.copy:
    dest: /etc/systemd/system/chat-proxy-relay.socket.d/listen.conf
    content: |
      [Socket]
      ListenStream={{ ansible_host }}:{{ chat_proxy_port }}
    owner: root
    group: root
    mode: "0644"
  register: download_chat_proxy_listen

- name: Bound the chat egress proxy's log retention
  ansible.builtin.copy:
    dest: /etc/systemd/system/chat-proxy.service.d/retention.conf
    content: |
      [Service]
      LogRetention={{ chat_proxy_log_retention }}
    owner: root
    group: root
    mode: "0644"
  register: download_chat_proxy_retention
```

> **Implementation note:** `LogRetention=` is not a systemd unit directive. If the systemd on these hosts does not support per-unit retention (check `systemd-analyze verify` and `man systemd.exec`), bound the log with `journalctl --vacuum-time` in a timer, or set `MaxRetentionSec` in a journald drop-in scoped by `SyslogIdentifier`. **Resolve this before committing — do not ship a drop-in systemd silently ignores.** Both directories need `ansible.builtin.file: state=directory` tasks before the drop-ins are written.

In `roles/svc_download/tasks/apps.yml`, after the Recyclarr start task:

```yaml
- name: Ensure the chat egress proxy is started
  ansible.builtin.systemd:
    name: chat-proxy.service
    state: "{{ 'restarted' if (download_chat_proxy_config.changed or download_chat_proxy_quadlet.changed) else 'started' }}"
    daemon_reload: "{{ download_chat_proxy_quadlet is changed }}"

- name: Enable the chat egress proxy relay socket
  ansible.builtin.systemd:
    name: chat-proxy-relay.socket
    enabled: true
    state: started
    daemon_reload: "{{ download_chat_proxy_relay_units is changed or download_chat_proxy_listen is changed }}"
```

- [ ] **Step 8: Validate offline**

```bash
python tests/validate_generated_catalog.py
python tests/validate_systemd_units.py
python tests/validate_secret_tasks.py
python tests/validate_scan_image_coverage.py
```

Expected: all pass. `validate_scan_image_coverage.py` should now count one more image.

- [ ] **Step 9: Deploy and verify functionally**

```bash
make dl
```

Then prove each layer separately — a proxy that is `active` proves nothing:

```bash
# 1. The container is in the jail and its egress is Mullvad
ssh svc-download 'sudo ip netns exec vpn curl -s https://am.i.mullvad.net/json'
# expect "mullvad_exit_ip": true

# 2. The proxy answers from svc-infra, and its exit is Mullvad
ssh svc-infra 'curl -s -x http://<svc-download-ip>:8118 https://am.i.mullvad.net/json'
# expect "mullvad_exit_ip": true  <- THIS is the check that matters

# 3. The proxy does NOT answer from anywhere else
ssh svc-media 'curl -s --max-time 5 -x http://<svc-download-ip>:8118 https://example.com'
# expect a timeout or connection refused

# 4. The leak canary is unaffected
ssh svc-download 'sudo systemctl start leak-canary.service && journalctl -u leak-canary -n 5'
```

Check 3 failing to fail is a real defect — it means the backstop rule is wrong and the LAN has an open tunnel.

- [ ] **Step 10: Commit**

```bash
git add inventory/group_vars/all/main.yml \
        roles/svc_download/templates/tinyproxy.conf.j2 \
        roles/svc_download/templates/chat-proxy.container.j2 \
        roles/svc_download/templates/host-backstop.nft.j2 \
        roles/svc_download/files/chat-proxy-relay.socket \
        roles/svc_download/files/chat-proxy-relay.service \
        roles/svc_download/tasks/images.yml \
        roles/svc_download/tasks/files.yml \
        roles/svc_download/tasks/apps.yml
git commit -m "feat: give the jail a forward proxy so chat's fetches can reach the tunnel

SearXNG's jail only ever covered the search query. The pages it returns
are fetched by Open WebUI itself, from svc-infra, which has no egress
policy -- so the engines saw Mullvad and every site in the results saw
the house.

This is the path into the tunnel for that second request. Bespoke rather
than a download_apps entry because proxy:true generates a Caddy vhost and
an HTTP health check a forward proxy cannot serve, the same reason
Recyclarr sits outside the catalog.

Reachable from svc-infra alone, not \$LAN_ADMIN. That scoping is the only
place the restriction can live: the socket proxy re-originates every
request from 10.77.0.1, so tinyproxy cannot tell its callers apart --
the same fact that forces SearXNG to run limiter:false.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: Point Open WebUI at the proxy

**Files:**
- Modify: `inventory/group_vars/all/infra-apps.yml` (open-webui `env`, ~line 641)

**Interfaces:**
- Consumes: `chat_proxy_port` from Task 2.
- Produces: an open-webui container whose outbound HTTP goes to the jail proxy.

- [ ] **Step 1: Add the proxy environment**

In the `open-webui` entry's `env` block, after `SEARXNG_QUERY_URL`:

```yaml
      # Chat's own page fetches — the second half of a web search, and the
      # half that used to leave the house directly. SearXNG returns URLs;
      # Open WebUI reads them itself, from here. See
      # docs/superpowers/specs/2026-08-13-chat-egress-through-vpn-design.md.
      #
      # This is a request, not a guarantee. A library that ignores these vars
      # would still leak, which is why svc-infra also drops non-LAN traffic
      # from this container's cgroup (roles/svc_infra/templates/
      # chat-egress.nft.j2). Env says should; the firewall says must.
      http_proxy: "http://{{ hostvars[download_host].ansible_host }}:{{ chat_proxy_port }}"
      https_proxy: "http://{{ hostvars[download_host].ansible_host }}:{{ chat_proxy_port }}"
      HTTP_PROXY: "http://{{ hostvars[download_host].ansible_host }}:{{ chat_proxy_port }}"
      HTTPS_PROXY: "http://{{ hostvars[download_host].ansible_host }}:{{ chat_proxy_port }}"
      # Everything on the LAN stays direct: Ollama and ComfyUI on the GPU host,
      # SearXNG's own IP:port, and loopback. Routing inference through Sweden
      # would be slow and pointless. Note this includes svc-download, so the
      # SEARXNG_QUERY_URL above is unaffected by the lines above it.
      no_proxy: "localhost,127.0.0.1,{{ lan_cidr }},{{ gpu_host_ip }},{{ hostvars[download_host].ansible_host }}"
      NO_PROXY: "localhost,127.0.0.1,{{ lan_cidr }},{{ gpu_host_ip }},{{ hostvars[download_host].ansible_host }}"
```

Both cases are set deliberately: Python's `requests` lowercases, but `httpx` and some libraries read the uppercase forms only.

> **Watch out:** `ENABLE_PERSISTENT_CONFIG` is `true` on this entry, so catalog env is a *first-boot seed* for any key with a DB row. These specific keys have no admin-UI equivalent and no row, so the environment governs — but confirm with `podman exec open-webui printenv https_proxy` in Step 3 rather than assuming.

- [ ] **Step 2: Validate and deploy**

```bash
python tests/validate_infra_catalog.py
make infra
```

- [ ] **Step 3: Prove chat actually uses it**

```bash
# The env reached the container
ssh svc-infra 'sudo -u homelab podman exec open-webui printenv https_proxy no_proxy'

# A fetch from inside chat exits via Mullvad
ssh svc-infra 'sudo -u homelab podman exec open-webui \
    curl -s --max-time 20 https://am.i.mullvad.net/json'
# expect "mullvad_exit_ip": true
```

Then in the browser at `https://chat.fortwow.dev`: run a chat with web search on, confirm citations still resolve and pages are summarised. **This is the functional check** — the container being up proves nothing, and a broken proxy shows as an empty or citation-less answer.

- [ ] **Step 4: Confirm the LAN path is untouched**

```bash
# Ollama still reachable (no_proxy working)
ssh svc-infra 'sudo -u homelab podman exec open-webui \
    curl -s --max-time 10 http://<gpu_host_ip>:11434/api/tags | head -c 200'
```

Then send a normal (non-search) chat message and confirm a model replies at usual speed. If inference got slow, `no_proxy` is not covering the GPU host.

- [ ] **Step 5: Commit**

```bash
git add inventory/group_vars/all/infra-apps.yml
git commit -m "feat: send chat's page fetches to the jail proxy

Open WebUI now hands its outbound HTTP to the forward proxy inside the
Mullvad namespace instead of fetching result pages itself. no_proxy keeps
the GPU host, SearXNG and the rest of the LAN direct -- routing inference
through Sweden would be slow and would buy nothing.

Both letter cases are set because requests lowercases and httpx does not.

This half is a request rather than a guarantee; the firewall that makes
it a guarantee lands in the next commit.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: Enforce it on svc-infra

**Files:**
- Create: `roles/svc_infra/templates/chat-egress.nft.j2`
- Create: `roles/svc_infra/files/chat-egress.service`
- Create: `roles/svc_infra/tasks/chat-egress.yml`
- Create: `tests/validate_chat_egress.py`
- Modify: `roles/svc_infra/tasks/main.yml`

**Interfaces:**
- Consumes: the cgroup path and level from Task 1; `chat_proxy_log_requests` from Task 2.
- Produces: `table inet chat_egress` on svc-infra, whose `chat_policy` drop counter Task 5's probe reads.

- [ ] **Step 1: Write the anti-drift validator first (it must fail)**

Create `tests/validate_chat_egress.py`:

```python
#!/usr/bin/env python3
"""The nft rule and the Quadlet unit name are the same fact written twice.

container-drift.yml's lesson is that two guards which can drift will. The
rule names open-webui.service inside a cgroup path; the unit name comes from
the infra_secret_apps catalog key. This asserts they still agree, and that
the drop rule keeps its unconditional counter -- the probe reads that counter,
so losing it would silently disarm the verification while the firewall kept
working.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "roles/svc_infra/templates/chat-egress.nft.j2"


def main() -> int:
    failures: list[str] = []
    catalog = yaml.safe_load(
        (ROOT / "inventory/group_vars/all/infra-apps.yml").read_text(encoding="utf-8")
    )
    if "open-webui" not in catalog["infra_secret_apps"]:
        failures.append("catalog: open-webui entry is gone; the nft rule names a unit that will not exist")

    env = Environment(
        loader=FileSystemLoader(str(ROOT)), undefined=StrictUndefined, keep_trailing_newline=True
    )
    env.filters["comment"] = lambda text: "\n".join(f"# {line}" for line in text.splitlines())

    for log_requests in (True, False):
        rendered = env.get_template(TEMPLATE).render(
            ansible_managed="Ansible managed",
            svc_uid=10001,
            lan_cidr="192.168.1.0/24",
            chat_egress_unit="open-webui.service",
            chat_egress_cgroup_level=5,
            chat_proxy_log_requests=log_requests,
        )
        if "open-webui.service" not in rendered:
            failures.append(f"log_requests={log_requests}: rule does not name the open-webui unit")
        if "counter drop" not in rendered:
            failures.append(f"log_requests={log_requests}: drop rule lost its unconditional counter")
        if rendered.count("hook output") != 1:
            failures.append(f"log_requests={log_requests}: expected exactly one output base chain")
        if "policy accept" not in rendered:
            failures.append(
                f"log_requests={log_requests}: base chain is not policy accept -- "
                "a drop policy here would take the whole VM off the network"
            )
        if "delete table" not in rendered:
            failures.append(f"log_requests={log_requests}: missing the delete-table idempotency guard")
        if "flush ruleset" in rendered:
            failures.append(
                f"log_requests={log_requests}: flush ruleset would clobber firewalld's own tables"
            )
        has_log = 'log prefix "chat-egress-drop ' in rendered
        if has_log is not log_requests:
            failures.append(
                f"log_requests={log_requests}: drop logging did not follow chat_proxy_log_requests"
            )

    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    if failures:
        return 1
    print("Chat egress nftables policy: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it and confirm it fails**

```bash
python tests/validate_chat_egress.py
```

Expected: FAIL — `jinja2.exceptions.TemplateNotFound: roles/svc_infra/templates/chat-egress.nft.j2`.

- [ ] **Step 3: Write the nftables template**

Create `roles/svc_infra/templates/chat-egress.nft.j2`. **Use the level and path measured in Task 1**, not the values below, if they differ:

```jinja
#!/usr/sbin/nft -f
{{ ansible_managed | comment }}
# /etc/nftables/chat-egress.nft
#
# Open WebUI may reach the LAN and nothing else. Everything it legitimately
# does — Ollama and ComfyUI on the GPU host, SearXNG, the jail proxy — is a LAN
# address, so anything internet-bound that bypassed its proxy configuration has
# an internet destination by definition and is dropped here.
#
# Scope discipline copied from svc-download's host-backstop.nft: OWN table,
# explicit `delete table` guard, NO `flush ruleset`. firewalld owns this VM's
# other rules and must not be disturbed; independent tables at the same hook
# are all evaluated and a drop in any of them is final.
#
# Rootless podman is why this matches on cgroup rather than address: the
# container's packets are re-originated by a userspace process and carry the
# host's own source IP, so there is nothing address-shaped to match on.

define LAN = {{ lan_cidr }}

table inet chat_egress
delete table inet chat_egress

table inet chat_egress {
    chain output {
        # policy ACCEPT, deliberately. svc-download's backstop guards a whole
        # VM and defaults to drop; this table has exactly one subject and must
        # leave the other ~28 containers on this host untouched. A drop policy
        # here would take svc-infra off the network.
        type filter hook output priority 0; policy accept;

        socket cgroupv2 level {{ chat_egress_cgroup_level }} "user.slice/user-{{ svc_uid }}.slice/user@{{ svc_uid }}.service/app.slice/{{ chat_egress_unit }}" jump chat_policy
    }

    chain chat_policy {
        oifname "lo" accept
        ct state established,related accept
        ip daddr $LAN accept

        # `counter` is UNCONDITIONAL and must stay that way: the probe in
        # chat-egress-probe.sh.j2 reads it to prove this rule is what refused a
        # direct fetch, rather than the network merely being down. It is a
        # number, not a destination, so it costs nothing when logging is off.
        #
        # The `log` half follows chat_proxy_log_requests, because it records
        # destinations exactly as the proxy log does — a false setting that
        # still wrote every blocked destination into the kernel log would not
        # mean what it says.
{% if chat_proxy_log_requests %}
        log prefix "chat-egress-drop " counter drop
{% else %}
        counter drop
{% endif %}
    }
}
```

- [ ] **Step 4: Run the validator and confirm it passes**

```bash
python tests/validate_chat_egress.py
```

Expected: `Chat egress nftables policy: OK`

- [ ] **Step 5: Write the loader unit**

Create `roles/svc_infra/files/chat-egress.service`:

```ini
# homelab-iac managed — roles/svc_infra/files/chat-egress.service
[Unit]
Description=Load the chat egress nftables policy
# After firewalld so a firewalld start does not race the table into existence
# and then rebuild its own ruleset on top. The tables are independent, but the
# ordering makes the boot sequence deterministic.
After=firewalld.service network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
# Deliberately NOT nftables.service: that unit loads
# /etc/sysconfig/nftables.conf, which on this distribution may carry a
# `flush ruleset` that would take firewalld's tables with it.
ExecStart=/usr/sbin/nft -f /etc/nftables/chat-egress.nft
ExecStop=/usr/sbin/nft delete table inet chat_egress

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 6: Write the Ansible tasks**

Create `roles/svc_infra/tasks/chat-egress.yml`:

```yaml
---
# The enforcement half of the chat egress design. The proxy in the jail gives
# chat a way into the tunnel; this is what stops it going any other way.
- name: Render the chat egress nftables policy
  ansible.builtin.template:
    src: chat-egress.nft.j2
    dest: /etc/nftables/chat-egress.nft
    owner: root
    group: root
    mode: "0644"
    validate: /usr/sbin/nft -c -f %s
  register: infra_chat_egress_policy

- name: Install the chat egress policy loader
  ansible.builtin.copy:
    src: chat-egress.service
    dest: /etc/systemd/system/chat-egress.service
    owner: root
    group: root
    mode: "0644"
  register: infra_chat_egress_unit

- name: Load and arm the chat egress policy
  ansible.builtin.systemd:
    name: chat-egress.service
    enabled: true
    state: "{{ 'restarted' if infra_chat_egress_policy is changed else 'started' }}"
    daemon_reload: "{{ infra_chat_egress_unit is changed }}"
  when: not ansible_check_mode

- name: Confirm the chat egress table is actually loaded
  ansible.builtin.command: nft list table inet chat_egress
  register: infra_chat_egress_loaded
  changed_when: false
  failed_when: "'chat_policy' not in infra_chat_egress_loaded.stdout"
  when: not ansible_check_mode
```

`validate: /usr/sbin/nft -c -f %s` is the important line — a syntactically bad ruleset never reaches disk.

In `roles/svc_infra/tasks/main.yml`, import it after `apps.yml` (the container must exist before its cgroup can be matched):

```yaml
- name: Enforce chat egress through the VPN
  ansible.builtin.import_tasks: chat-egress.yml
  tags: [chategress]
```

- [ ] **Step 7: Deploy and prove enforcement, both directions**

```bash
make infra
```

```bash
# 1. Through the proxy still works and is Mullvad
ssh svc-infra 'sudo -u homelab podman exec open-webui \
    curl -s --max-time 20 https://am.i.mullvad.net/json'
# expect "mullvad_exit_ip": true

# 2. Bypassing the proxy is now REFUSED, and our rule is what refused it
ssh svc-infra 'sudo nft reset counters table inet chat_egress; \
    sudo -u homelab podman exec open-webui \
      curl -s --max-time 8 --noproxy "*" https://ifconfig.me; \
    sudo nft list table inet chat_egress'
# expect: curl fails, AND the drop counter is non-zero

# 3. Nothing else on svc-infra was affected
ssh svc-infra 'curl -s --max-time 10 -o /dev/null -w "%{http_code}\n" https://example.com'
# expect 200 — the host itself is not in chat's cgroup
ssh svc-infra 'sudo -u homelab podman exec uptime-kuma \
    curl -s --max-time 10 -o /dev/null -w "%{http_code}\n" https://example.com'
# expect 200 — other containers are untouched
```

Check 3 is not optional. A cgroup match that is too broad takes the VM off the internet, and the symptom (everything slightly broken) is much harder to diagnose than the cause.

- [ ] **Step 8: Confirm the whole app still works**

Browser at `https://chat.fortwow.dev`: run a web search, confirm citations resolve. Send a plain chat message, confirm inference is normal speed. Generate an image, confirm ComfyUI is reachable.

- [ ] **Step 9: Commit**

```bash
git add roles/svc_infra/templates/chat-egress.nft.j2 \
        roles/svc_infra/files/chat-egress.service \
        roles/svc_infra/tasks/chat-egress.yml \
        roles/svc_infra/tasks/main.yml \
        tests/validate_chat_egress.py
git commit -m "feat: make chat's egress a rule rather than a request

http_proxy is a suggestion any library is free to ignore, and one that
did would leak silently -- the exact failure this whole change exists to
fix. svc-infra now drops non-LAN traffic originating from open-webui's
cgroup, so the environment says should and the firewall says must.

It matches on cgroup because rootless podman leaves nothing else to match
on: the container's packets are re-originated by a userspace process and
carry the host's own address.

policy accept on the base chain, unlike svc-download's backstop. That one
guards a whole VM; this has exactly one subject and must leave the other
containers on the host alone.

The validator renders both states of chat_proxy_log_requests and fails if
the drop rule ever loses its unconditional counter -- the probe reads that
counter, so dropping it would disarm the verification while the firewall
went on working, which is the kind of silent half-failure this repo keeps
finding.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: The probe, the alert, and the metrics

**Files:**
- Create: `roles/svc_infra/templates/chat-egress-probe.sh.j2`
- Create: `roles/svc_infra/files/homelab-chat-egress.service`
- Create: `roles/svc_infra/files/homelab-chat-egress.timer`
- Modify: `roles/svc_infra/tasks/chat-egress.yml`
- Modify: `tests/validate_shell_templates.py`, `tests/validate_alert_topics.py`

**Interfaces:**
- Consumes: the drop counter from Task 4; the proxy from Task 2.
- Produces: metrics `homelab_chat_egress_enforced`, `homelab_chat_egress_vpn_verified`, `homelab_chat_egress_probe_success`.

- [ ] **Step 1: Register the script in both validators (they must fail)**

In `tests/validate_shell_templates.py`, add to `TEMPLATES`:

```python
    "roles/svc_infra/templates/chat-egress-probe.sh.j2",
```

In `tests/validate_alert_topics.py`, add to `ALERT_TEMPLATES`:

```python
    "roles/svc_infra/templates/chat-egress-probe.sh.j2",
```

- [ ] **Step 2: Run them and confirm they fail**

```bash
python tests/validate_shell_templates.py
python tests/validate_alert_topics.py
```

Expected: both FAIL with a missing-template error. This ordering is the point — the discovery sweep in `validate_alert_topics.py` exists so an unregistered alerter cannot ship, and registering first proves the gate is live.

- [ ] **Step 3: Write the probe**

Create `roles/svc_infra/templates/chat-egress-probe.sh.j2`:

```bash
#!/usr/bin/env bash
{{ ansible_managed | comment }}
# chat-egress-probe.sh — prove chat's egress goes through the tunnel, and that
# the rule stopping it going anywhere else is still doing so.
#
# WHY THREE CHECKS AND NOT ONE
#
# The obvious check — "a direct fetch from chat must fail" — is worthless
# alone, because a DEAD probe passes it. A missing container, a broken podman,
# a typo in the exec: all produce "connection failed", which reads as
# "enforcement working". That is the credential-canary failure CLAUDE.md
# catalogues, and it is why B2 exists.
#
#   A   through the proxy      -> must be 200 AND Mullvad
#   B1  bypassing the proxy    -> must fail, AND the nft counter must move
#   B2  the same, from the host-> must SUCCEED
#
# B2 is the control. It proves the internet is reachable and curl works right
# now, so B1's failure is attributable to the rule rather than to a dead
# network. The counter delta proves it was THIS rule and not something
# incidental. If B2 fails the verdict is inconclusive, never ok.
set -uo pipefail

# shellcheck source=/dev/null
[[ -r /etc/homelab-notify.env ]] && . /etc/homelab-notify.env

CONTAINER=open-webui
RUN_USER={{ 'homelab' }}
ECHO_URL="https://ifconfig.me"
MULLVAD_URL="https://am.i.mullvad.net/json"
TEXTFILE_DIR="{{ infra_textfile_dir }}"
METRIC_WRITE="{{ infra_metric_write_bin }}"

verdict=ok
detail=""

in_chat() {
    sudo -u "$RUN_USER" podman exec "$CONTAINER" "$@" 2>/dev/null
}

nft_drop_count() {
    nft -j list table inet chat_egress 2>/dev/null \
        | grep -o '"packets":[0-9]*' | tail -n1 | cut -d: -f2
}

# --- A: through the proxy, must be Mullvad --------------------------------
a_body=$(in_chat curl -s --max-time 25 "$MULLVAD_URL")
if [[ -z "$a_body" ]]; then
    verdict=broken
    detail="chat could not reach ${MULLVAD_URL} through its proxy — the tunnel or the jail is down, and chat's web features are failing closed"
    vpn_verified=0
elif grep -q '"mullvad_exit_ip"[[:space:]]*:[[:space:]]*true' <<<"$a_body"; then
    vpn_verified=1
else
    verdict=broken
    detail="chat reached the internet and it was NOT Mullvad — this is the original leak, recurring"
    vpn_verified=0
fi

# --- B2 first: is the probe environment even alive? -----------------------
if ! curl -fsS --max-time 10 -o /dev/null "$ECHO_URL"; then
    verdict=inconclusive
    detail="the host itself could not reach ${ECHO_URL}, so a refused fetch from chat proves nothing"
    enforced=0
else
    # --- B1: bypassing the proxy must fail, and our rule must be why ------
    before=$(nft_drop_count)
    in_chat curl -s --max-time 8 --noproxy '*' -o /dev/null "$ECHO_URL"
    b1_rc=$?
    after=$(nft_drop_count)

    if [[ -z "$before" || -z "$after" ]]; then
        verdict=inconclusive
        detail="could not read the chat_egress drop counter — the nftables table is missing, so enforcement is unproven"
        enforced=0
    elif [[ $b1_rc -eq 0 ]]; then
        verdict=broken
        detail="chat reached ${ECHO_URL} directly, bypassing its proxy — enforcement is NOT in effect"
        enforced=0
    elif [[ "$after" -gt "$before" ]]; then
        enforced=1
    else
        verdict=inconclusive
        detail="the direct fetch failed but the drop counter did not move — something other than the chat_egress rule refused it"
        enforced=0
    fi
fi

# --- metrics BEFORE the alert, per CLAUDE.md ------------------------------
# Never publish zeros we did not measure: an inconclusive run exits without
# writing, leaving the previous file in place. A stale number is detectable;
# a fabricated zero reads as "not enforced" and cries wolf.
if [[ "$verdict" != "inconclusive" ]]; then
    success_flag=()
    [[ "$verdict" == "ok" ]] && success_flag=(--success)
    "$METRIC_WRITE" --dir "$TEXTFILE_DIR" --file chat-egress \
        --prefix homelab_chat_egress "${success_flag[@]}" <<EOF
homelab_chat_egress_enforced ${enforced}
homelab_chat_egress_vpn_verified ${vpn_verified}
EOF
fi

# --- alert ----------------------------------------------------------------
if [[ "$verdict" == "ok" ]]; then
    logger -t chat-egress-probe "OK (chat egress verified Mullvad, enforcement confirmed by counter)"
    exit 0
fi

logger -p daemon.err -t chat-egress-probe "${verdict}: ${detail}"

topic="${NTFY_ALERT_TOPIC:-}"
if [[ -z "$topic" || -z "${NTFY_URL:-}" ]]; then
    echo "NTFY_ALERT_TOPIC or NTFY_URL unset in /etc/homelab-notify.env; cannot alert" >&2
    exit 1
fi

AUTH=()
[[ -n "${NTFY_TOKEN:-}" ]] && AUTH=(-H "Authorization: Bearer ${NTFY_TOKEN}")

priority=high
[[ "$verdict" == "inconclusive" ]] && priority=default

curl -fsS --max-time 15 --retry 2 --retry-delay 5 "${AUTH[@]}" \
    -H "Title: homelab — chat egress ${verdict}" \
    -H "Priority: ${priority}" \
    -H "Tags: warning" \
    -d "${detail}

Checked from $(hostname -s) at $(date -u +%Y-%m-%dT%H:%M:%SZ).

ok           chat exits via Mullvad and direct egress is refused
broken       one of those two is false — read the detail above
inconclusive the probe could not establish a verdict; nothing is proven" \
    "${NTFY_URL}/${topic}" >/dev/null 2>&1

exit 1
```

- [ ] **Step 4: Run both validators and confirm they pass**

```bash
python tests/validate_shell_templates.py
python tests/validate_alert_topics.py
```

Expected: both pass. ShellCheck runs over the rendered output, so fix any warnings it raises rather than suppressing them.

- [ ] **Step 5: Write the timer units**

Create `roles/svc_infra/files/homelab-chat-egress.service`:

```ini
# homelab-iac managed — roles/svc_infra/files/homelab-chat-egress.service
[Unit]
Description=Verify chat's egress goes through the VPN and cannot go elsewhere
After=network-online.target chat-egress.service
Wants=network-online.target

[Service]
Type=oneshot
# Type=oneshot disables the start timeout by default. A probe wedged in
# `activating` is not `failed`, so OnFailure would never fire and the check
# would go quiet with every indicator green — the precise failure this unit
# exists to make impossible. Four curls at =<25s plus podman exec overhead.
TimeoutStartSec=180
EnvironmentFile=/etc/homelab-notify.env
ExecStart=/usr/local/sbin/homelab-chat-egress-probe.sh
```

Create `roles/svc_infra/files/homelab-chat-egress.timer`:

```ini
# homelab-iac managed — roles/svc_infra/files/homelab-chat-egress.timer
[Unit]
Description=Periodic chat egress verification

[Timer]
# Hourly, unlike the weekly alert canary. This one guards a live data path
# rather than a delivery route: the window between a rule silently ceasing to
# match and somebody noticing is the window in which chat is leaking, so it is
# worth keeping to an hour. Each run is four HTTP requests.
OnCalendar=hourly
# Spread off the hour so it does not collide with the estate's scheduled work
# (03:05-03:40 backups, 04:00 verify, 04:40 freshness, 05:30 scan).
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 6: Add the install tasks**

Append to `roles/svc_infra/tasks/chat-egress.yml`:

```yaml
- name: Render the chat egress probe
  ansible.builtin.template:
    src: chat-egress-probe.sh.j2
    dest: /usr/local/sbin/homelab-chat-egress-probe.sh
    owner: root
    group: root
    mode: "0750"

- name: Install the chat egress probe units
  ansible.builtin.copy:
    src: "{{ item }}"
    dest: "/etc/systemd/system/{{ item }}"
    owner: root
    group: root
    mode: "0644"
  loop:
    - homelab-chat-egress.service
    - homelab-chat-egress.timer
  register: infra_chat_egress_probe_units

- name: Arm the chat egress probe
  ansible.builtin.systemd:
    name: homelab-chat-egress.timer
    enabled: true
    state: started
    daemon_reload: "{{ infra_chat_egress_probe_units is changed }}"
  when: not ansible_check_mode
```

- [ ] **Step 7: Deploy and prove all three verdicts**

```bash
make infra
ssh svc-infra 'sudo systemctl start homelab-chat-egress.service; \
    journalctl -u homelab-chat-egress -n 20 --no-pager'
```

Expected: `OK (chat egress verified Mullvad, enforcement confirmed by counter)`.

Now prove it can actually fail — a check that has only ever passed is a check nobody has tested:

```bash
# Force `broken`: remove the table, re-run, expect an alert
ssh svc-infra 'sudo nft delete table inet chat_egress; \
    sudo systemctl start homelab-chat-egress.service; \
    journalctl -u homelab-chat-egress -n 10 --no-pager'
# expect inconclusive or broken, NOT ok

# Read the alert back out of ntfy — per CLAUDE.md, a published alert is only
# proven by reading it back
curl -s "http://<svc-media-ip>:8080/homelab-alerts/json?poll=1&since=10m"

# Restore
ssh svc-infra 'sudo systemctl restart chat-egress.service && \
    sudo systemctl start homelab-chat-egress.service'
```

- [ ] **Step 8: Confirm the metrics landed**

```bash
ssh svc-infra 'cat /opt/homelab/appdata/node-exporter-textfile/chat-egress.prom'
ssh svc-infra 'curl -s localhost:9100/metrics | grep -E "homelab_chat_egress|node_textfile_scrape_error"'
```

`node_textfile_scrape_error` must be `0`. If it is `1`, one malformed line has taken every series in that directory down — read it before anything else.

- [ ] **Step 9: Commit**

```bash
git add roles/svc_infra/templates/chat-egress-probe.sh.j2 \
        roles/svc_infra/files/homelab-chat-egress.service \
        roles/svc_infra/files/homelab-chat-egress.timer \
        roles/svc_infra/tasks/chat-egress.yml \
        tests/validate_shell_templates.py \
        tests/validate_alert_topics.py
git commit -m "feat: prove hourly that chat's egress is tunnelled and cannot escape

The obvious check -- a direct fetch from chat must fail -- is satisfied
identically by a working firewall and by a dead probe. A missing
container or a typo in the exec produces 'connection failed', which reads
as 'enforcement working'.

So the probe carries its own control. It fetches directly from the host
too, and only if THAT succeeds does a refusal from chat mean anything;
and it reads the nft drop counter across the attempt, so the refusal is
attributable to this rule rather than to a network that happens to be
down. Verdicts are tri-state -- a probe that could not look says so
instead of reporting ok.

Metrics are emitted before the alert and never on an inconclusive run: a
zero here would read as 'not enforced' and cry wolf, and a stale number
is detectable in a way a fabricated one is not.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: Documentation

**Files:**
- Modify: `docs/services.md` (AI-stack section ~line 74-116)
- Modify: `docs/security.md` (after "Download egress backstop", ~line 181)

- [ ] **Step 1: Correct the AI stack diagram and prose**

In `docs/services.md`, replace the three-line diagram (~line 79) with:

```text
chat.fortwow.dev (Open WebUI, svc-infra)
  |-- inference + images --> Ollama / ComfyUI on the Win11 4090 box (LAN, direct)
  |-- search QUERIES -----> SearXNG in svc-download's VPN jail --> Mullvad
  `-- page FETCHES -------> forward proxy in the same jail ------> Mullvad
```

Then, after the "SearXNG is jailed for its egress" paragraph, add:

```markdown
**A web search is two round trips, and only the first was ever jailed.**
SearXNG queries the upstream engines and returns a list of URLs; Open WebUI
then fetches those pages itself. Until 2026-08-13 that second request left
svc-infra directly, so the engines saw Mullvad and every site in the results
saw the house. It was found by asking a model to fetch an echo service and
reading back a home address.

Chat's outbound HTTP now goes to a forward proxy inside the same jail, and
svc-infra drops any non-LAN packet from Open WebUI's cgroup — so the proxy is
not a setting that can be ignored. `chat_proxy_log_requests` (default `true`)
controls whether the proxy records destinations; it governs the drop rule's
log statement too, and never the drop counter the hourly probe depends on.

Two consequences worth knowing:

- **Chat's web features fail closed.** If the tunnel or the jail is down,
  fetching breaks rather than falling back. Local inference, image generation
  and history are unaffected. `homelab-chat-egress.timer` alerts on it.
- **This covers chat and nothing else.** Every other service on svc-infra and
  svc-media still egresses directly; `service_guarded_egress` is `true` on
  svc-download alone. The full audit is in the design doc's appendix.
```

- [ ] **Step 2: Fix the persistent-config paragraph**

Same file, ~line 102. It says `ENABLE_PERSISTENT_CONFIG` is `false` and concludes admin-UI edits do not survive a restart. `infra-apps.yml:579` sets it `true`, so the conclusion is backwards:

```markdown
**Settings are seeded from the catalog, then owned by the database.**
`ENABLE_PERSISTENT_CONFIG` is `true`, so a value in
`inventory/group_vars/all/infra-apps.yml` applies only until that key is
touched in the admin UI — after which a row exists, the row wins, and editing
the catalog silently does nothing while `make infra` still reports success.
Admin-UI changes DO survive restarts. The sharp edge is `ENABLE_SIGNUP`, where
the drift is a security change rather than a preference: confirm
`ui.enable_signup` is false in `GET /api/v1/configs/export` after any
admin-settings session.
```

- [ ] **Step 3: Add the security boundary**

In `docs/security.md`, after the "Download egress backstop" section:

```markdown
## Chat egress boundary

Open WebUI's outbound HTTP is proxied through the download jail and enforced by
`table inet chat_egress` on svc-infra, which drops non-LAN traffic originating
from that container's cgroup. The environment variables are a request; the
table is what makes it a guarantee, and the two must be changed together.

Three things about it are load-bearing:

- **The cgroup match is a string**, and a rule that matches nothing fails open
  and silently. `homelab-chat-egress.timer` is what turns that into a loud
  failure — it confirms hourly that a direct fetch from chat is refused AND
  that the drop counter moved, with a control fetch from the host proving the
  network was up when the refusal happened.
- **The base chain is `policy accept`.** Unlike svc-download's backstop it
  guards one unit, not a VM. Changing it to drop would take svc-infra off the
  network.
- **The proxy is reachable from svc-infra only.** tinyproxy sees every caller
  as `10.77.0.1`, so the `$INFRA_HOST` rule in `host-backstop.nft.j2` is the
  only place that restriction can be expressed.

Only svc-download filters egress at all. svc-media and svc-infra do not, and
this change does not alter that for anything but chat.
```

- [ ] **Step 4: Validate and commit**

```bash
python tests/validate_links.py
```

```bash
git add docs/services.md docs/security.md
git commit -m "docs: describe the fetch half of a web search, and correct two claims

services.md drew the AI stack with web search arriving via Mullvad, which
was true of the query and silently untrue of the fetch -- the reading that
made the leak surprising when it was found.

Also fixes the persistent-config paragraph, which said the flag was false
and concluded that admin-UI edits do not survive a restart. The catalog
set it true on 2026-08-10, so the conclusion was backwards: edits do
survive, and ENABLE_SIGNUP drifting is a security change rather than a
preference.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: Close out the branch

- [ ] **Step 1: Confirm the tree is clean**

```bash
git status --porcelain    # must print nothing, untracked files included
```

- [ ] **Step 2: Final deploy from the clean tree**

```bash
make dl
make infra    # first run reports changed=3 (the /opt/homelab-iac archive sync)
make infra    # second run MUST report changed=0
make verify
```

If anything other than those three tasks reports changed, **read which ones** — do not paper over a real diff by quoting the second run's number.

- [ ] **Step 3: Merge, push, delete the branch and the worktree**

```bash
git switch main
git merge --ff-only feat/chat-egress-vpn
git push origin main
git worktree remove ../homelab-ironwood-chat-egress
git branch -d feat/chat-egress-vpn
git push origin --delete feat/chat-egress-vpn
```

Note the main checkout may still be on another branch with concurrent work — coordinate before switching it.

- [ ] **Step 4: Watch CI**

CI runs on push to `main`, after the merge. A red run means something already merged is broken and needs a follow-up commit. It is the only place `systemd-analyze verify` actually parses the new unit files, so it is the first real test of `chat-egress.service` and the two probe units.

---

## Self-Review

**Spec coverage:** §1 egress path → Task 2. §1 logging toggle → Task 2 Steps 2-3, Task 4 Step 3. §2 enforcement → Task 4. §2 fail-closed → falls out of Tasks 2+4, asserted in Task 5's verdict A. §3 three-check probe → Task 5 Step 3. §3 alerting → Task 5 Steps 3, 7. §3 metrics → Task 5 Steps 3, 8. §3 anti-drift validator → Task 4 Step 1. §4 docs → Task 6. Implementation-time checks → Task 1 (cgroup), Task 2 Step 1 (image), Task 2 Step 7 note (retention), Task 3 Step 3 (env inheritance), Task 5 Step 7 (both toggle states are exercised by the validator in Task 4 Step 1 rendering both).

**Known gap, carried deliberately:** the spec's DNS residual is documented, not fixed. No task addresses it and none should — it is named as a follow-up in the spec.

**Types and names:** `chat_proxy_port`, `chat_proxy_image`, `chat_proxy_log_requests`, `chat_proxy_log_retention`, `chat_egress_unit`, `chat_egress_cgroup_level` are used consistently across Tasks 2-5. Unit names `chat-proxy.service`, `chat-proxy-relay.socket/.service`, `chat-egress.service`, `homelab-chat-egress.service/.timer` are consistent between the templates, the Ansible tasks and the verification commands. Metric names match between the probe script and Task 5 Step 8.

**Placeholder scan:** one intentional literal remains — `REPLACE_WITH_STEP_1_DIGEST` in Task 2 Step 2 — because the repo forbids hand-written digests and Step 1 resolves it with `make image-digest`. Task 2 Step 7 carries a flagged unknown (`LogRetention=`) with three named alternatives and an explicit instruction to resolve it before committing rather than shipping a directive systemd ignores.
