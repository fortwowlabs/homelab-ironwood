# GPU Check Verdicts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop a GPU-host outage from paging as a defect and from failing verify on every VM, and make the image checks clear TERRA's card before they run and release it after.

**Architecture:** A stdlib helper (`scripts/gpu_host.py`) talks to ComfyUI and Ollama over HTTP. Both image checks call it to probe reachability and make room; a release script runs from each unit's `ExecStopPost=-`. Verify stops cascading (`any_errors_fatal` removed on the service-VM play) and svc-infra's failed-units gate exempts the app-check units, guarded by a new gate.

**Tech Stack:** Python 3 stdlib (urllib, socket, http.server for fakes), PyYAML, Ansible, systemd, bash.

**Spec:** `docs/superpowers/specs/2026-11-18-gpu-check-verdicts-design.md`

## Global Constraints

- Worktree: `/Users/straderb/dev/worktrees/homelab-iac-gpu-checks`, branch `fix/gpu-check-verdicts`, based on `main` `c124481`. `.venv`, `.vault_pass` and `inventory/group_vars/all/vault.yml` are symlinks to the primary clone — never `git add` them.
- Never `git add -A`; stage explicit paths. Never echo vault values.
- Gates are `tests/validate_*.py` scripts declaring `GATE_GROUP = "<group>"` (existing groups only: shell, links, catalog, provisioning, systemd, secrets, ci), with `main() -> int` returning non-zero on failure; `tests/run_gates.py` discovers them. Run one with `.venv/bin/python tests/<file>.py`.
- `ruff check --no-cache tests scripts` must pass (config in `ruff.toml`).
- Verdict exit codes are fixed: ok=0, broken=1, inconclusive=2. Units keep `SuccessExitStatus=2`.
- ComfyUI `/free` body: `{"unload_models": true, "free_memory": true}`. Ollama eviction: `POST /api/generate {"model": <name>, "keep_alive": 0}`, confirmed via `GET /api/ps`.
- Reachability probe timeout: 5 s. `comfy_free` settle window: 20 s.
- Nothing is installed or configured on TERRA.
- Deploy only svc-infra and svc-download (svc-media runs unmerged Tailscale-branch state).

## Review Focus

1. ComfyUI reachable but `/queue` or `/system_stats` returns non-JSON or 5xx → helpers return `None`/report, never raise into the check (a crash would exit 1 = `broken`). Test in Task 1.
2. Ollama unreachable while ComfyUI is up → eviction reports failure, check still runs, generation HTTPError then classifies `inconclusive` (room unconfirmed). Test in Task 3.
3. Release script with ComfyUI unreachable or images.yml unreadable → exits 0, logs why. Test in Task 4.
4. `systemctl --failed` output with only exempt units → gate passes but the exempt units are still printed; mixed exempt + real → gate fails naming only the real one. Test in Task 6.
5. Notifier with an empty InvocationID (unit never ran) → falls back to `-u -n 20`, still sends. Test in Task 7.

---

### Task 1: GPU-host helper module

**Files:**
- Create: `scripts/gpu_host.py`
- Create: `tests/validate_gpu_host.py` (`GATE_GROUP = "catalog"`), containing a reusable fake: `tests/gpu_fake.py`
- Create: `tests/gpu_fake.py` — stdlib `ThreadingHTTPServer` on `127.0.0.1:0` serving canned ComfyUI/Ollama responses, recording received requests.

**Interfaces:**
- Produces (all in `scripts/gpu_host.py`, none raise on network/parse errors):
  - `reachable(base_url: str, timeout: float = 5.0) -> bool`
  - `comfy_queue_busy(base_url: str, timeout: float = 10.0) -> bool | None`
  - `comfy_vram(base_url: str, timeout: float = 10.0) -> dict[str, int] | None` — keys `vram_total, vram_free, torch_vram_total, torch_vram_free` from `devices[0]`
  - `comfy_free(base_url: str, settle: float = 20.0, timeout: float = 10.0) -> dict` — keys `posted: bool, before: dict|None, after: dict|None`
  - `ollama_resident(base_url: str, timeout: float = 10.0) -> list[str] | None`
  - `ollama_evict(base_url: str, timeout: float = 30.0) -> bool` — True only if `/api/ps` is empty afterwards (True immediately when nothing was resident)
  - `make_room(comfy_url: str, ollama_url: str) -> bool` — runs `ollama_evict` then `comfy_free`, prints one line per step with before/after VRAM to stderr, returns True only if both confirmed (`comfy_free` confirmed = `posted` and `after` not None)
  - `tests/gpu_fake.py`: `class FakeGpu` context manager with `.url` (base URL), `.requests` (list of `(method, path, body)`), and settable dicts `routes[(method, path)] = (status, json_or_bytes)`; plus `closed_port_url() -> str` returning a URL on a port nothing listens on.

- [ ] **Step 1: Write the failing gate** with these cases, each a function returning an error string or None, `main()` collecting them:
  - `reachable(closed_port_url())` is False and returns in < 6 s; `reachable(fake.url)` is True.
  - `comfy_queue_busy`: `{"queue_running": [], "queue_pending": []}` → False; `{"queue_running": [["x"]], "queue_pending": []}` → True; route returning 500 → None; route returning `b"not json"` → None.
  - `comfy_vram` parses `{"devices": [{"vram_total": 100, "vram_free": 40, "torch_vram_total": 50, "torch_vram_free": 10}]}`; 500 → None.
  - `comfy_free`: fake records `("POST", "/free", {"unload_models": True, "free_memory": True})`; with `settle=0.5` returns `posted=True` and both `before`/`after` dicts.
  - `ollama_evict`: `/api/ps` first returns `{"models": [{"name": "qwen3:32b"}]}`, after the generate call returns `{"models": []}` → True and a `POST /api/generate` with `{"model": "qwen3:32b", "keep_alive": 0}` was recorded; if `/api/ps` keeps listing the model → False; unreachable → False.
- [ ] **Step 2:** `.venv/bin/python tests/validate_gpu_host.py` → fails with ImportError for `gpu_host`.
- [ ] **Step 3:** Implement `scripts/gpu_host.py` (urllib + socket only; `comfy_free` polls `/system_stats` every 1 s and stops when `torch_vram_total` stops decreasing for one poll or `settle` elapses).
- [ ] **Step 4:** Gate passes; `.venv/bin/ruff check --no-cache tests scripts` clean.
- [ ] **Step 5: Commit** `scripts/gpu_host.py tests/gpu_fake.py tests/validate_gpu_host.py` — `feat: add a GPU-host helper that probes, evicts and frees over HTTP`.

Note: `tests/gpu_fake.py` has no `GATE_GROUP` — confirm `tests/run_gates.py` only discovers `validate_*.py` so a helper module is not treated as an ungrouped gate.

### Task 2: `ollama_base_url` in the catalog

**Files:**
- Modify: `inventory/group_vars/all/images.yml` (beside `comfyui_base_url`, ~line 33)
- Modify: `tests/validate_openwebui_image_config.py` (fixture ~132, drift cases ~209/277, live check ~478)

**Interfaces:** Produces catalog key `ollama_base_url: "http://192.168.1.40:11434"`.

- [ ] **Step 1:** Add to the validator: fixture catalog gains `ollama_base_url`; new drift case `("ollama_base_url disagrees with gpu_host_ip", _drift_ollama_url, "gpu_host_ip")` setting it to `http://192.168.1.99:11434`; the live check requires `ollama_base_url == f"http://{gpu_host_ip}:11434"`.
- [ ] **Step 2:** Run the gate → fails on the real catalog (key missing).
- [ ] **Step 3:** Add the key to images.yml; extend the existing duplication comment to name both URLs.
- [ ] **Step 4:** Gate passes.
- [ ] **Step 5: Commit** both files — `feat: record Ollama's address in the image catalog`.

### Task 3: Image checks probe, make room, and log every HTTP error

**Files:**
- Modify: `scripts/image_generation_check.py` (`probe`, ~53–130; docstring)
- Modify: `scripts/image_edit_check.py` (`probe`, ~66–175)
- Create: `tests/validate_image_check_verdicts.py` (`GATE_GROUP = "catalog"`), using `tests/gpu_fake.py`

**Interfaces:**
- Consumes: `reachable`, `comfy_queue_busy`, `make_room` from Task 1; `catalog["comfyui_base_url"]`, `catalog["ollama_base_url"]` (Task 2).
- `probe(base_url, token, catalog, timeout)` signatures unchanged; the fake serves both the Open WebUI routes and ComfyUI/Ollama routes (the test passes `catalog` with `comfyui_base_url`/`ollama_base_url` pointing at fakes).

Behaviour (both checks, before any Open WebUI call):
1. `not reachable(comfy)` → stderr `INCONCLUSIVE: ComfyUI at <url> is unreachable` → `("inconclusive", {})`.
2. `comfy_queue_busy(comfy) is True` → stderr `INCONCLUSIVE: ComfyUI queue is busy; not freeing memory under someone else's job` → inconclusive. `None` → log and continue.
3. `room_confirmed = make_room(comfy, ollama)`.

Then, generation: every `HTTPError` logs `HTTP <code> from <path>: <body[:500]>`; models-endpoint 400 → `broken`; generations-POST `HTTPError` → `broken` if `room_confirmed` else `inconclusive`. Edit: classification unchanged, `HTTPError` on the image fetch also logs.

- [ ] **Step 1: Write the failing gate**, cases for generation:
  - comfy URL = closed port → verdict `inconclusive`, elapsed < 10 s, stderr contains `unreachable`.
  - busy queue → `inconclusive`, no `POST /free` recorded.
  - reachable, idle, OWUI models returns 400 with body `{"detail":"x"}` → `broken`, stderr contains `HTTP 400` and `{"detail":"x"}`.
  - room confirmed, generations POST 500 → `broken`; Ollama unreachable (room unconfirmed), generations POST 500 → `inconclusive`.
  - happy path (models list includes expected model, generation returns `[{"url": "/img"}]`, `/img` returns a 1024x1024 PNG header) → `ok`, and `POST /free` plus `/api/ps` were requested before the generation POST.
  Edit: closed port → inconclusive; busy → inconclusive; happy path records `/free` before `POST /api/v1/images/edit`.
- [ ] **Step 2:** Run → fails (no probe step exists).
- [ ] **Step 3:** Implement in both scripts; update the generation module docstring's verdict paragraph to say a reachable-ComfyUI 400 is ours and an unreachable one is could-not-look.
- [ ] **Step 4:** Gate passes; ruff clean; `.venv/bin/python tests/validate_openwebui_image_config.py` still passes.
- [ ] **Step 5: Commit** the two scripts and the gate — `fix: classify an unreachable GPU host as inconclusive and clear the card first`.

### Task 4: Release script wired into both units

**Files:**
- Create: `scripts/gpu_host_release.py`
- Modify: `roles/svc_infra/files/homelab-image-gen@.service`, `roles/svc_infra/files/homelab-image-edit@.service`
- Modify: `tests/validate_image_check_verdicts.py` (release cases)
- Check: `roles/svc_infra/tasks/files.yml` / `verify-runner.yml` — confirm `scripts/` reaches `/opt/homelab-iac` through the runner archive (no new copy task needed); if the units are templated through a list, nothing else changes.

**Interfaces:** Consumes `reachable`, `comfy_queue_busy`, `comfy_free`. Produces `main() -> int` that always returns 0; reads `comfyui_base_url` from `inventory/group_vars/all/images.yml` relative to the repo root (same `ROOT` pattern as the checks); optional `--catalog PATH` for tests.

- [ ] **Step 1:** Gate cases: closed port → exit 0, stdout/stderr contains `unreachable`; busy → exit 0, contains `left resident`, no `/free` posted; idle → exit 0, `/free` posted, output contains `before` and `after`; `--catalog` pointing at a missing file → exit 0 with a message.
- [ ] **Step 2:** Run → fails.
- [ ] **Step 3:** Implement; add to both units, after `ExecStart=`:
  ```
  # Release TERRA's card whatever the outcome — success, failure, crash or a
  # TimeoutStartSec kill. "-" so a release problem never changes the verdict.
  ExecStopPost=-/opt/homelab-iac/.venv/bin/python /opt/homelab-iac/scripts/gpu_host_release.py
  ```
- [ ] **Step 4:** Gate passes; `.venv/bin/python tests/validate_systemd_units.py` and `tests/validate_onfailure.py` pass.
- [ ] **Step 5: Commit** — `feat: release TERRA's GPU memory after every image check`.

### Task 5: Verify stops cascading across VMs

**Files:**
- Modify: `verify.yml:7` (service-VM play only)
- Modify (comments only, if now inaccurate): `roles/svc_media/tasks/verify.yml:~291`, `roles/service_vm/tasks/container-drift.yml:~78`
- Modify: `tests/validate_verify_safety.py` — add an assertion

- [ ] **Step 1:** Add to `validate_verify_safety.py`: the play in `verify.yml` with `hosts: service_vms` must not set `any_errors_fatal: true`, and the localhost play must still assert `service_vm_verified` for all three hosts (guards the property removal relies on).
- [ ] **Step 2:** Run → fails on line 7.
- [ ] **Step 3:** Replace line 7 with `any_errors_fatal: false` plus a comment: each host's rescue fires only for that host; the localhost play's fact assertions keep a partial run red; cite 2026-11-18 (one svc-infra app check paged all three VMs).
- [ ] **Step 4:** Gate passes; `make validate-syntax validate-ansible` pass.
- [ ] **Step 5: Commit** — `fix: report a verify failure only on the host that failed`.

### Task 6: Exempt the app-check units from svc-infra's failed-units gate

**Files:**
- Modify: `roles/service_vm/tasks/failed-units.yml` (system check, lines 2–18)
- Modify: `roles/service_vm/defaults/main.yml` (`service_vm_failed_units_exempt: []`)
- Modify: `inventory/host_vars/svc-infra.yml`
- Create: `tests/validate_failed_units_exempt.py` (`GATE_GROUP = "systemd"`)

**Interfaces:** Variable `service_vm_failed_units_exempt: list[str]` of fnmatch globs on the unit name (first whitespace field of each `systemctl --failed --plain --no-legend` line). svc-infra value: `["homelab-image-gen@*.service", "homelab-image-edit@*.service"]`.

- [ ] **Step 1:** Write the gate: for every host_vars/group_vars/defaults value of `service_vm_failed_units_exempt`, each pattern (a) is not `*`/`*.service`, (b) matches at least one unit file under `roles/*/files` or `roles/*/templates` (template name `foo@.service` matches `foo@*.service` by substituting `*` for an instance), (c) that unit has a timer and is not in `validate_onfailure.py`'s `EXEMPT` (import it). Include an in-file negative fixture (`["*"]` and a pattern matching nothing) that must be rejected.
- [ ] **Step 2:** Run → fails (variable absent → assert the svc-infra value exists, so the gate has a positive control).
- [ ] **Step 3:** Implement: register raw output as now; `set_fact` `service_vm_failed_units_real` = lines whose first field matches no exempt glob (Jinja: `select` with a custom test is unavailable, so loop patterns via `reject('search', regex)` after translating globs — builder's choice, keep it in one expression); `failed_when` keeps `rc != 0` and tests the filtered list; a following `debug` task prints exempt failed units when any (`when:` non-empty), message saying they alert through their own OnFailure hook. Set the svc-infra value in host_vars with a comment citing 2026-11-18.
- [ ] **Step 4:** Gate passes; `make validate-ansible validate-yaml` pass.
- [ ] **Step 5: Commit** — `fix: keep app-check failures out of svc-infra's failed-units gate`.

### Task 7: Failure alerts quote only the failing run

**Files:**
- Modify: `roles/service_vm/templates/notify-failure.sh.j2:~44`
- Modify: whichever of `tests/validate_shell_templates.py` / a new case covers it (render + shellcheck already run there)

- [ ] **Step 1:** Add a gate assertion that the rendered template selects journal lines by `_SYSTEMD_INVOCATION_ID` and keeps the `-u "$unit" -n 20` fallback.
- [ ] **Step 2:** Run → fails.
- [ ] **Step 3:** Implement:
  ```bash
  inv=$(systemctl show -p InvocationID --value "$unit" 2>/dev/null || true)
  if [[ -n "$inv" ]]; then
      context=$(journalctl "_SYSTEMD_INVOCATION_ID=$inv" + "INVOCATION_ID=$inv" --no-pager -o cat 2>/dev/null | tail -c 3000)
  fi
  [[ -n "${context:-}" ]] || context=$(journalctl -u "$unit" -n 20 --no-pager -o cat 2>/dev/null | tail -c 3000)
  ```
  The fault-identity hash stays unchanged.
- [ ] **Step 4:** `make validate-shell` passes.
- [ ] **Step 5: Commit** — `fix: quote only the failing run in unit-failure alerts`.

### Task 8: Docs

**Files:** `docs/gpu-host.md` (new short section: what the checks do to TERRA's memory before/after, the before/after log lines, and that the `/free` reservation question is now observable per run); `roles/svc_infra/files/homelab-image-gen@.service` header comment if it still says verify collapse is avoided (now true by construction — say so).

- [ ] **Step 1:** Write the section; `make validate-links` passes.
- [ ] **Step 2: Commit** — `docs: describe the image checks' GPU housekeeping`.

### Task 9: Deploy and prove (controller, not a subagent)

- [ ] `make validate` green in the worktree; `git status --porcelain` shows only intended files.
- [ ] **Deploy svc-infra from the Mac clone only after the user has synced `vault.yml` from TERRA's WSL clone (verify key presence by name only), wrapped in `caffeinate -i`.** The Mac's vault was stale (Aug 11) and lacked `vault_openwebui_admin_token`; deploying with it would break the check's token. Never print vault values. Before deploying, confirm `/opt/homelab-iac/.deployed-rev` on svc-infra is main-based and that `origin/main` has not moved (`git fetch` and compare).
- [ ] Iterate: `make infra USE_VAULT_FILE=1` (from the Mac clone after the vault sync, wrapped in `caffeinate -i`) (and `make dl USE_VAULT_FILE=1` for Task 7's notifier). Not `make media`.
- [ ] Live, TERRA ComfyUI still down: on svc-infra `sudo systemctl start homelab-image-gen@svcops.service`; expect exit status 2, `verdict=inconclusive` within ~10 s, ExecStopPost logs `unreachable`, unit not failed. Same for `homelab-image-edit@svcops.service`. Do NOT reset-failed yet if the exemption proof below still needs them.
- [ ] Notifier proof on svc-infra (positive control: an older run's line must be absent). Run a throwaway unit twice with different output, both failing, via the real hook. Use the literal unit name, because `%n` is not expanded in transient-unit properties:
  `sudo systemd-run --unit=gpu-notify-probe.service -p OnFailure=notify-failure@gpu-notify-probe.service.service /bin/sh -c 'echo probe-run-one; exit 1'`
  Then `sudo systemctl reset-failed gpu-notify-probe.service` AND `sudo rm -f /var/lib/homelab/notify-failure.gpu-notify-probe.service.state` (the notifier dedupes on unit + result, so run two would be suppressed otherwise), then the same with `probe-run-two`. Confirm from the journal or syslog that run two was NOT suppressed (no dedupe line for it). Poll `curl -s "http://192.168.1.30:8080/homelab-alerts/json?poll=1&since=10m"`: the second alert body contains `probe-run-two` and not `probe-run-one`. Then `sudo systemctl reset-failed gpu-notify-probe.service`.
- [ ] Gate exemption proof, with a positive control, BEFORE any reset-failed:
  1. On svc-infra `sudo systemctl start homelab-image-gen@probe.service` (systemd refuses a transient `systemd-run` instance of a template that has a fragment file; this form fails with 217/USER because no `probe` user exists, lands in the failed state, and matches the exempt glob). `make verify USE_VAULT_FILE=1` must be green and print the "left out of this gate" debug line.
  2. Start a failing non-exempt transient unit (`sudo systemd-run --unit=gpu-gate-probe.service /bin/false`) and run `make verify USE_VAULT_FILE=1` again. Expect only svc-infra to go red, while svc-media and svc-download run to completion. That proves the `any_errors_fatal` removal live.
  3. Expect the failed-units watcher to page for both units. Then `sudo systemctl reset-failed homelab-image-gen@probe.service gpu-gate-probe.service homelab-image-gen@svcops.service homelab-image-edit@svcops.service homelab-verify@svcops.service`.
- [ ] Live ComfyUI run, once TERRA is back: `sudo systemctl start homelab-image-gen@svcops.service` should end `ok`. `journalctl -u homelab-image-gen@svcops.service` must show the `gpu:` lines with torch figures and the `gpu-release:` lines. Use the figures to judge whether the torch reservation was released (docs/gpu-host.md says `/free` once left it in place).
- [ ] Commit any fixes; `git status --porcelain` empty; `make deploy-proof TARGET=infra USE_VAULT_FILE=1` (expect sync-trio first, then rerun → clean) and `make deploy-proof TARGET=dl USE_VAULT_FILE=1` → clean.
- [ ] `make verify USE_VAULT_FILE=1` green.
- [ ] Merge to `main` (ff), push, delete branch local + remote, remove worktree.
- [ ] Report the unproven half: eviction/`/free` against a live ComfyUI waits on TERRA coming back.
