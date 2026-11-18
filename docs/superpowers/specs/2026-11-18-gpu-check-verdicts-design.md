# GPU check verdicts, verify isolation, and TERRA memory housekeeping

Status: approved 2026-11-18 (approach C + verify changes a and b).

## Why

On 2026-11-17 and 2026-11-18 one fact — ComfyUI on TERRA (192.168.1.40:8188)
stopped answering — produced eight urgent alerts:

1. `homelab-image-gen@svcops` reported `broken` (exit 1) instead of
   `inconclusive` (exit 2). Open WebUI turns "cannot connect to ComfyUI" into
   the same generic HTTP 400 (`{"detail":"[ERROR: Failed to retrieve image
   generation models]"}`, after a 135 s connect timeout) that the check reads as
   a broken workflow mapping. Its sibling `image_edit_check.py` handled the same
   outage correctly.
2. The check printed only `verdict=broken`: the HTTP status and body were
   dropped, so the journal carried no clue to the cause.
3. The nightly verify's `systemctl --failed` gate on svc-infra saw the failed
   app-check unit and failed verify — the exact conflation the unit's own header
   says it exists to avoid.
4. `any_errors_fatal: true` on verify's service-VM play then sent svc-media and
   svc-download into their own rescue blocks, so all three VMs paged "VERIFY
   FAILED" although nothing on two of them was wrong.
5. The OnFailure alert body quoted the previous day's successful run
   (`verdict=ok`, "Deactivated successfully") because it takes the unit's last
   20 journal lines across all invocations.

Separately, and requested at the same time: the 2026-08-31 incident showed that
a model stack left resident on TERRA's 24.5 GB card breaks these checks (Qwen
Image Edit peaks ~23.5 GB). The checks should make room before they run and
release the card after.

## Goals

- GPU host unreachable → `inconclusive`, quickly (seconds, not 135 s), no page.
- Every HTTP-error path logs status and body.
- An app-check failure on svc-infra does not fail verify, and a failure on one
  VM does not report as a failure on the others.
- Failure alerts quote only the failing run.
- Before each image check: evict resident Ollama models and ComfyUI models so
  the test starts from a known-empty card. After each check, whatever the
  outcome (including timeout kill): release ComfyUI's models and memory.

## Non-goals

- Anything installed or configured on TERRA. docs/gpu-host.md: "not managed by
  Ansible and will not be." All housekeeping is over the existing HTTP APIs
  from svc-infra.
- Fixing whatever stopped ComfyUI on TERRA on 2026-11-17 (operator action).
- The open tailnet-exposure finding for ComfyUI/Ollama.

## Anti-goals

- A check that reads "could not look" as fine. Unreachable stays `inconclusive`
  and is still visible (no metric published, verdict logged) — it just does not
  page, which is the existing contract of `SuccessExitStatus=2`.
- Housekeeping that can change a verdict. Release failures are logged, never
  fail the unit.
- Freeing memory out from under a human's in-progress generation.
- An exemption list that silently drops alerts.

## Approaches considered

Where the housekeeping lives:

- **A — inside the check scripts (try/finally).** Simple, but a run killed by
  `TimeoutStartSec` never reaches `finally`, leaving the model resident — the
  2026-08-31 failure.
- **B — systemd `ExecStartPre` / `ExecStopPost`.** Release always runs, but a
  pre-step that cannot reach TERRA fails the unit — the wrong verdict, which is
  the bug being fixed.
- **C — hybrid (chosen).** The check script does reachability + make-room,
  because their outcome decides the verdict. A separate release script runs as
  `ExecStopPost=-` so it runs after success, failure, crash or timeout and its
  own failure is ignored by systemd.

Verify cascade: both (a) drop `any_errors_fatal` from the service-VM play and
(b) exempt the app-check units from svc-infra's failed-units gate. Chosen
together; either alone leaves half the noise.

## Design

### 1. Shared GPU-host helper: `scripts/gpu_host.py`

Stdlib-only (urllib, socket), importable by both checks and the release script.

- `reachable(base_url, timeout=5) -> bool` — TCP connect to host:port.
- `comfy_queue_busy(base_url) -> bool | None` — `GET /queue`; busy if
  `queue_running` or `queue_pending` non-empty; `None` if it cannot be read.
- `comfy_vram(base_url) -> dict | None` — `GET /system_stats`, first device's
  `vram_total`, `vram_free`, `torch_vram_total`, `torch_vram_free`.
- `comfy_free(base_url, settle=20) -> dict` — `POST /free`
  `{"unload_models": true, "free_memory": true}`, then poll `/system_stats`
  until `torch_vram_total` stops falling or `settle` seconds pass. Returns
  before/after figures. ComfyUI applies `/free` asynchronously in its prompt
  worker, hence the poll. docs/gpu-host.md records that `/free` once left
  torch's reservation in place; the before/after figures make that observable
  rather than assumed.
- `ollama_resident(base_url) -> list[str] | None` — `GET /api/ps`.
- `ollama_evict(base_url) -> bool` — `keep_alive: 0` per resident model, then
  confirm via `/api/ps` (same technique as `vram_survey.py`; that file is not
  refactored).

### 2. Catalog

Add `ollama_base_url: "http://192.168.1.40:11434"` to
`inventory/group_vars/all/images.yml` beside `comfyui_base_url`, with the same
duplication note, and extend `validate_openwebui_image_config.py` so it fails
when it disagrees with `gpu_host_ip` (positive and negative fixture cases, as
for `comfyui_base_url`).

### 3. Check flow (both `image_generation_check.py` and `image_edit_check.py`)

1. `reachable(comfyui_base_url)` false → log `INCONCLUSIVE: ComfyUI at <url>
   unreachable` → `inconclusive`.
2. `comfy_queue_busy` true → log → `inconclusive` (someone is generating; do not
   free under them, do not compete for VRAM). `None` → proceed, log it.
3. Make room: `ollama_evict` and `comfy_free`; log before/after VRAM. Record
   `room_confirmed` = both succeeded and confirmed.
4. Existing probe. Changes to error handling:
   - Every `HTTPError` path logs `HTTP <code>: <body[:500]>`.
   - Generation: models-endpoint 400 stays `broken` — ComfyUI was just proven
     reachable, so the remaining cause is ours. Generations-POST `HTTPError` is
     `broken` only if `room_confirmed`; otherwise `inconclusive` (an OOM from a
     card we could not clear is not a mapping defect).
   - Edit: unchanged classification (400 stays `inconclusive`, per its existing
     OOM reasoning), plus steps 1–3.
5. The scripts do not release memory themselves; the unit's `ExecStopPost` does.

### 4. Release script: `scripts/gpu_host_release.py`

Run as `ExecStopPost=-/opt/homelab-iac/.venv/bin/python
/opt/homelab-iac/scripts/gpu_host_release.py` in both
`homelab-image-gen@.service` and `homelab-image-edit@.service`. Reads
`comfyui_base_url` from images.yml. Unreachable → log and exit 0. Queue busy →
log "left resident: queue busy" and exit 0. Otherwise `comfy_free` and log
before/after. Always exits 0 — it reports, it never judges.

### 5. Verify isolation

- `verify.yml`: remove `any_errors_fatal: true` from the service-VM play only
  (keep it on the hypervisor play and everywhere else). Each host's rescue then
  fires only for that host. The localhost success play already asserts every
  host's `*_verified` facts, so a partial run still ends red without a success
  notification. Update the comments in `roles/svc_media/tasks/verify.yml`
  (~291) and `roles/service_vm/tasks/container-drift.yml` (~78) that cite
  `any_errors_fatal` if they become inaccurate.
- `roles/service_vm/tasks/failed-units.yml`: new variable
  `service_vm_failed_units_exempt` (default `[]` in role defaults). Lines of
  `systemctl --failed` whose unit name matches an exempt glob are removed
  before the emptiness test and reported in a separate debug task (visible in
  the verify log, never silent). The `rc != 0` test is unchanged. svc-infra
  sets the list to `homelab-image-gen@*.service` and
  `homelab-image-edit@*.service` (inventory host_vars or the include's vars —
  builder's call; host_vars preferred so the record sits beside other
  svc-infra exemptions).
- New gate (group `systemd`): every exempt pattern must match a unit file in
  the repo whose alerting is covered by `validate_onfailure.py` (i.e. not in
  its `EXEMPT`), and must not be a bare `*`. Include a fixture case that fails.

### 6. Failure-alert context

`roles/service_vm/templates/notify-failure.sh.j2`: take
`InvocationID` from `systemctl show`, and select journal lines with
`_SYSTEMD_INVOCATION_ID=<id> + INVOCATION_ID=<id>` (process output plus the
manager's own messages about that run). Fall back to the current `-u -n 20`
form when the ID is empty. Keep the 3000-byte tail and the fault-identity hash
unchanged.

## Deployment constraint

svc-media currently runs state from the unmerged `feat/tailscale-subnet-routers`
branch (it is the advertising subnet router, with branch-only route-check
changes). A deploy to svc-media from a main-based tree would revert that. This
change therefore deploys to **svc-infra and svc-download only**. Everything here
except §6 lives on svc-infra (check scripts, units, the nightly runner's copy of
verify.yml and the gate). §6 reaches svc-media when the Tailscale branch is
rebased onto main and deployed; until then svc-media's alert bodies keep the old
behaviour. The changed=0 proof is claimed for svc-infra and svc-download.

## Verification

- Offline: unit-style gate driving `gpu_host.py` and both checks against a local
  stdlib HTTP server fake (closed port → inconclusive fast; 400 with reachable
  ComfyUI → broken and body logged; busy queue → inconclusive; `/free` posted
  and `/system_stats` re-read). `make validate` green.
- Live, while TERRA's ComfyUI is down: run `homelab-image-gen@svcops` and
  `homelab-image-edit@svcops` by hand → `inconclusive` in seconds, unit not
  failed, ExecStopPost logs unreachable. `make verify` from svc-infra → only
  the failing host (if any) reports.
- Live, once TERRA is back (operator): one run of each check → `ok`, journal
  shows eviction and before/after VRAM, and `/system_stats` after the run
  shows torch's reservation released. Until then this half is unproven and is
  reported as such.

## Left to the builder

Exact log wording; helper timeouts beyond those stated; where the exempt list
lives (host_vars preferred); whether the fake-server tests are one gate or two.
