# Finding: `socket cgroupv2` matching works for open-webui on svc-infra

**Date:** 2026-08-14 (measured 2026-08-18)
**Host:** svc-infra (192.168.1.32)
**Verdict: GO** — nftables can single out open-webui's rootless-podman traffic by
cgroup, and the match is specific rather than universal.

## Tooling

```
$ nft --version
nftables v1.1.5 (Commodore Bullmoose #6)
$ uname -r
6.12.0-211.34.1.el10_2.x86_64
```

Both comfortably exceed the spec's floor (nft >= 0.9.6, kernel >= 5.13).

## Container cgroup path

```
$ sudo -u homelab XDG_RUNTIME_DIR=/run/user/$(id -u homelab) \
    systemctl --user show open-webui.service -p ControlGroup
ControlGroup=/user.slice/user-10001.slice/user@10001.service/app.slice/open-webui.service
```

- `homelab` is uid/gid 10001 on this host (not 1000 — the brief's example UID was
  illustrative only; the measured value was used throughout).
- Path components after the leading `/`: `user.slice`, `user-10001.slice`,
  `user@10001.service`, `app.slice`, `open-webui.service` = **5**.
- `chat_egress_cgroup_level = 5` (bare integer, for the inventory variable of
  that name in a later task).
- `chat_egress_unit = open-webui.service` (for the inventory variable of that
  name in a later task).

## Step 3: throwaway table loads

```
table inet chat_egress_probe {
    chain output {
        type filter hook output priority 0; policy accept;
        socket cgroupv2 level 5 "user.slice/user-10001.slice/user@10001.service/app.slice/open-webui.service" counter
    }
}
```

Loaded without error via `nft -f -`.

## Step 4: positive control — traffic from open-webui moves the counter

```
counter before curl: packets 0 bytes 0
$ sudo -u homelab podman exec open-webui curl -s -o /dev/null --max-time 10 https://example.com
http_code=200
counter after curl:  packets 11 bytes 1363
```

**PASS** — non-zero, confirming the rule matches open-webui's re-originated
rootless-podman traffic.

## Step 5: negative control — traffic from a different unit does not move the counter

```
counter before curl: packets 0 bytes 0
$ sudo -u homelab podman exec uptime-kuma curl -s -o /dev/null --max-time 10 https://example.com
http_code=200
counter after curl:  packets 0 bytes 0
```

**PASS** — the uptime-kuma request succeeded (http_code=200, so traffic did
leave the host) but the counter stayed at zero, confirming the match is scoped
to open-webui's cgroup and not universal.

## Step 6: throwaway table removed

```
$ sudo nft delete table inet chat_egress_probe
$ sudo nft list ruleset | grep -c chat_egress_probe
0
```

Confirmed deleted.

## Step 7: firewalld reload does not flush a foreign table

```
$ sudo nft -f - <<'EOF'
table inet chat_egress_probe {
    chain output { type filter hook output priority 0; policy accept; }
}
EOF
$ sudo firewall-cmd --reload
success
$ sudo nft list tables | grep chat_egress_probe
table inet chat_egress_probe
```

**Survives.** firewalld's reload does not remove a coexisting foreign `inet`
table — Task 4 does not need a reload hook for this reason. Table deleted
again afterward and confirmed absent (`grep -c` = 0).

## Deviation from the brief worth recording

The brief's Step 4 reset command, `sudo nft reset counters table inet
chat_egress_probe`, executes with exit 0 but does **not** zero an inline
rule counter on this nft version (1.1.5) — `reset counters` targets named
counter objects (`counter <name> { }`), not counters embedded directly in a
rule. The counter kept accumulating across supposed resets. The command that
actually zeroes an inline rule counter is:

```
sudo nft reset rules table inet chat_egress_probe
```

All counter values above were captured using `reset rules`, with an explicit
list immediately after each reset to confirm it read back as `packets 0 bytes
0` before generating traffic. Task 4's implementation (and any operational
runbook for resetting `chat_egress` counters) should use `reset rules`, not
`reset counters`.

A second, unrelated wrinkle: `sudo -u homelab podman exec ...` run directly
over an interactive ssh command failed with `cannot chdir to /home/straderb:
Permission denied` when the shell's cwd was straderb's home directory;
running the same command from `/tmp` (or any directory straderb/homelab can
both traverse) worked. Not a finding about the cgroup match itself, just a
note for anyone reproducing these commands by hand.
