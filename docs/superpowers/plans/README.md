# Implementation plans

Historical implementation plans, one per feature, named `YYYY-MM-DD-<topic>.md`.

**These are records, not instructions.** Every plan here describes work that was
already decided; most of it shipped. A plan is written before the work and is
not updated afterwards, so where a plan and the current tree disagree, the tree
is right. Read a plan for *why* something is shaped the way it is, never as a
description of how the estate works today — [Architecture](../../architecture.md)
and the role comments are for that.

Live design work in progress lives in [`docs/plans/`](../../plans/) instead, and
the design documents these plans implement are in [`../specs/`](../specs/).

## Status

Established by checking for the artifact each plan names, not by reading its
title. The column is only worth having for the rows that are not "Shipped".

| Plan | Status |
|---|---|
| [2026-07-27-fortwow-dev-letsencrypt](2026-07-27-fortwow-dev-letsencrypt.md) | Shipped |
| [2026-08-07-estate-metrics](2026-08-07-estate-metrics.md) | Shipped |
| [2026-08-08-gpu-host-ollama-comfyui](2026-08-08-gpu-host-ollama-comfyui.md) | Shipped — on the GPU host, which Ansible does not manage; evidence is [gpu-host.md](../../gpu-host.md) |
| [2026-08-09-uncensored-model-roster](2026-08-09-uncensored-model-roster.md) | Shipped |
| [2026-08-10-uncensored-models-handoff](2026-08-10-uncensored-models-handoff.md) | Handoff note, not a plan — context passed between sessions |
| [2026-08-11-inference-capacity-and-roster](2026-08-11-inference-capacity-and-roster.md) | Shipped |
| [2026-08-12-inference-capacity-handoff](2026-08-12-inference-capacity-handoff.md) | Handoff note, not a plan |
| [2026-08-14-chat-egress-through-vpn](2026-08-14-chat-egress-through-vpn.md) | Shipped |
| [2026-08-14-comfyui-image-generation](2026-08-14-comfyui-image-generation.md) | Shipped |
| [2026-08-22-owui-seed-export-handoff](2026-08-22-owui-seed-export-handoff.md) | Handoff note, not a plan |
| [2026-08-27-comfyui-image-editing](2026-08-27-comfyui-image-editing.md) | Shipped |
| [2026-08-27-video-generation](2026-08-27-video-generation.md) | Shipped — GPU host only, no repo code by design; evidence is [gpu-host.md](../../gpu-host.md) "MiniMax H3" |
| [2026-09-03-shell-gate-discovery](2026-09-03-shell-gate-discovery.md) | Shipped |
| [2026-09-03-python-lint-gate](2026-09-03-python-lint-gate.md) | Shipped |
| [2026-09-03-deploy-proof](2026-09-03-deploy-proof.md) | Shipped — including the live run its verification section left open; the regexes were confirmed against real callback output on 2026-09-03 and again on 2026-09-04 |
| [2026-09-03-repo-hygiene](2026-09-03-repo-hygiene.md) | Shipped |
| [2026-09-03-media-converge-parity](2026-09-03-media-converge-parity.md) | Shipped |
| [2026-09-03-secret-guard-loop](2026-09-03-secret-guard-loop.md) | Deferred — the plan leads with the argument against itself; read that before starting |
| [2026-09-04-software-version-bump-backlog](2026-09-04-software-version-bump-backlog.md) | Phase 1 shipped (beszel-agent, jdownloader-2, homepage); Phases 2-5 not started |

Three of the eighteen are **handoff notes** rather than implementation plans:
they carry context from one session to the next mid-feature and do not stand on
their own. They are kept because the reasoning in them is not recorded anywhere
else, not because they describe a discrete piece of work.

Two record work that lives **entirely on the GPU host**, which is deliberately
not managed by Ansible (see `gpu_host_ip` in
`inventory/group_vars/all/main.yml`). Nothing in this repo can confirm they
shipped; the evidence is in `docs/gpu-host.md`, written from measurement at the
time.

## `notes/`

Working notes kept alongside the plans. Same caveat as above: point-in-time
records, superseded by the tree wherever the two disagree.
