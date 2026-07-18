# pfSense — flat-LAN egress backstop for svc-download (no-VLAN variant)

The netns jail is the primary control: inside the namespace, non-tunnel
destinations are unroutable, full stop. These pfSense rules are the second
layer — they exist so that a compromise or misconfiguration of the *VM
itself* (its init namespace, not the jail) still can't reach the internet
in the clear, and so violations get logged.

Key fact this variant relies on: internet-bound traffic from any LAN host
traverses pfSense even on a flat LAN, so per-source-IP egress rules work
exactly as VLAN-interface rules did. What pfSense canNOT see on a flat LAN
is same-subnet traffic (svc-download <-> TrueNAS, <-> Proxmox, <-> your MBP)
— that's switched at L2. Consequences at the bottom.

## Aliases

| Alias               | Type    | Content                                    |
|---------------------|---------|--------------------------------------------|
| `SVC_DOWNLOAD`      | Host    | 192.168.1.31                               |
| `MULLVAD_ENDPOINTS` | Host(s) | the WireGuard server IP(s) from your Mullvad config(s) — list every server you generated a peer for |

## LAN interface rules (insert ABOVE the default allow-LAN-to-any, top-down)

| # | Action | Proto   | Source         | Destination         | Port  | Log | Purpose |
|---|--------|---------|----------------|---------------------|-------|-----|---------|
| 1 | Pass   | UDP     | `SVC_DOWNLOAD` | `MULLVAD_ENDPOINTS` | 51820 | no  | WireGuard handshake (from the VM's init ns) |
| 2 | Pass   | TCP/UDP | `SVC_DOWNLOAD` | any                 | 53,443| yes | **DISABLED by default** — patch-window rule for dnf/registry |
| 3 | Block  | any     | `SVC_DOWNLOAD` | any                 | any   | yes | Egress backstop; the log line is your tripwire |

Notes:
* Rule 3 also covers destination "This Firewall" for ports 53/853 — the
  DNS-recursion path is closed by the same rule, no separate entry needed.
  The jail never needs pfSense DNS (Mullvad's 10.64.0.1 rides wg0); the
  *host* resolves only when rule 2 is enabled.
* No NFS rule exists in this variant because none is possible or needed:
  svc-download -> TrueNAS is same-subnet and never touches pfSense.
* Rules 1–3 must sit above pfSense's default "LAN to any" allow or they
  never match.

## What you give up vs the VLAN design, stated plainly

svc-download can reach every LAN host at L2: pfSense web UI, Proxmox :8006,
TrueNAS UIs, your other machines. pfSense cannot mediate that. Mitigations,
in order of value:

1. `svc-download/host-backstop.nft` (optional, in this bundle) — a scoped
   nftables output policy ON the VM restricting the init namespace to
   {Mullvad:51820, TrueNAS:2049, established inbound-admin}. Covers
   misconfiguration and casual malware; a root-level compromise of the VM
   can remove it, which is the honest limit of host-side controls.
2. Strong auth on the LAN admin surfaces regardless (you were doing this
   anyway): TOTP on pfSense and Proxmox, SSH keys only.
3. Accept it. The threat model here is "download-adjacent code goes bad";
   that code runs *in the jail*, which has no LAN route at all. The exposed
   surface is the VM host, which runs nothing but Podman, systemd units,
   and sshd.

If any of that sits badly, the VLAN design is a 20-minute retrofit later —
these rules convert to VLAN-interface rules nearly 1:1.

## Validation

```
# from svc-download host (init ns):
curl -4 --max-time 5 https://ifconfig.me      # times out (rule 3) unless rule 2 enabled
dig @192.168.1.1 example.com                  # refused/timeout (rule 3)
# from inside the jail:
ip netns exec vpn curl -4 https://am.i.mullvad.net/connected
                                              # "You are connected to Mullvad"
# pfSense: Status > System Logs > Firewall, filter SVC_DOWNLOAD —
# steady state = zero rule-3 hits.
```
