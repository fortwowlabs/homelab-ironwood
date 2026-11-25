# Caddy Certificate Reload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a certbot renewal actually change the certificate Caddy serves, and make `homelab-certwatch` fail whenever the served certificate differs from the one on disk.

**Architecture:**
- The deploy hook switches from `systemctl reload caddy` to `caddy reload --force`. A plain reload does nothing when the adapted config is byte-identical, and here it always is.
- `homelab-certwatch.sh` adds two fingerprint comparisons. It fetches the leaf certificate Caddy serves on `ansible_host:443` and requires its SHA-256 fingerprint to equal the fingerprint of `/etc/caddy/certs/fullchain.pem`, and requires that file to match certbot's live copy.
- The verify-time assert that consumes certwatch's verdict stops calling every failure "near expiry".
- A new fixture gate runs the rendered script against real local TLS listeners, including every case where it must fail.

**Tech Stack:** bash, OpenSSL CLI, Jinja2 templates (Ansible), systemd units, Python 3 stdlib (`ssl`, `socket`, `threading`, `subprocess`) for the gate.

**Spec:** The Background section below, which is the 2026-11-25 investigation. No separate spec doc exists.

## Background (the spec)

On 2026-11-25 at 05:51, certbot renewed `*.fortwow.dev` on svc-media. Its log shows `Running deploy-hook command: /etc/letsencrypt/renewal-hooks/deploy/caddy.sh`. The hook copied the new pair into `/etc/caddy/certs/` and ran `systemctl reload caddy`. Caddy logged `"msg":"config is unchanged"` and kept serving the old certificate:

```
served   serial=06204F…  notAfter=Dec 25 01:24:39 2026 GMT
on disk  serial=06A1FD…  (both /etc/letsencrypt/live and /etc/caddy/certs)
```

The Caddyfile loads the certificate with `tls /etc/caddy/certs/fullchain.pem …` (Caddyfile.j2:49). Those paths never change, so a plain reload is a no-op. The Sep 25 renewal only appeared to work because svc-media rebooted on 2026-10-14.

Nothing detects this today:
- `certwatch` checks both certificate *files*, and both are new.
- The verify gate reads the live socket but checks only issuer and SAN, which a stale or even expired certificate still passes.

If nothing restarts Caddy, every `*.fortwow.dev` site breaks at 2026-12-25 01:24 GMT, and every check stays green until then.

## Adversarial review (2026-11-25) and what was done with it

A fresh-context reviewer attacked v1 of this plan. Dispositions:

| # | Finding | Disposition |
|---|---|---|
| 1 | BLOCKER: `make media` (site.yml) never asserts `media_cert_ok`; only root `verify.yml:96-103` does. So the planned live positive control would read green, and a correct check would look broken. | **Accepted.** Task 3 now runs certwatch directly (rc=1 expected) and `make verify` (red expected on the cert assert). |
| 2 | The plan file is untracked, so the clean-tree checks can never pass. | **Accepted.** Task 0 commits it plus a README status row. |
| 3 | `verify.yml`'s assert is named "not near expiry" and its message says "inside its expiry warning window", which would mislabel the new failures in the nightly alert. | **Accepted.** Task 2 renames it. svc-infra's runner archive then needs `deploy-proof TARGET=infra` (Task 3). |
| 4 | The gate can hang `make validate` forever: `run_gates.py:91` has no timeout, macOS has no `timeout`, and an accept thread can die on an `SSLError`. | **Accepted.** Per-case `subprocess.run(timeout=30)`, a try/except per connection, and socket timeouts. |
| 5 | "Listener answers but nothing parses" and the probe timeout were not really pinned. | **Accepted.** New `closes` and `silent` cases, and a `PROBE_TIMEOUT` assignment. |
| 6 | `setvar` silently does nothing when a variable is renamed, so the gate would probe the workstation's real `:443` and `/etc`. | **Accepted.** `re.subn` must report exactly one replacement. |
| 7 | Boot race: a `Persistent=true` catch-up run with `RandomizedDelaySec` near 0 and no `After=caddy.service` reads "could not read" and stays failed for a day. | **Accepted.** `After=caddy.service` goes on the certwatch unit. |
| 8 | Prove the hook through certbot with `certbot renew --dry-run --run-deploy-hooks`. | **Declined.** The 05:51 log already proves certbot finds and runs this exact hook path. What is new is the reload behaviour, which running the hook directly proves. A dry run costs a staging DNS-01 round for evidence we already have. |
| 9 | Use a caddy.service `ExecReload=` drop-in instead of calling the CLI in the hook. | **Declined, with the reason recorded in the hook.** A drop-in changes the distro unit's behaviour for every reload, including Ansible's. The hook is the one place that needs `--force`. The Caddyfile has no `{$ENV}` placeholders and no `admin` directive, so the root CLI adapts it identically. |
| 10 | Docs missed: the troubleshooting row, the manual check, the README row, a runbook line. | **Accepted.** These go in Task 2; README is in Task 0. |
| 11 | Name any failed units before the final verify. | **Accepted.** This is Task 3 Step 0. svc-media read 0 failed units at 05:52. |
| 12 | Fetch and check ancestry before deploying and merging; push the branch after commits. | **Accepted.** |

**Final review (2026-11-25).** The whole-branch review added: the runbook clears the failed unit with `systemctl start homelab-certwatch.service`; one probe retry (`PROBE_RETRY_DELAY`); a `systemctl restart caddy` fallback in every piece of advice; a first response for "could not read"; an accurate unit Description; a gate assertion of exactly one problem per failing case; quoted `SERVED_HOST`/`SERVED_SNI`; and the Task 3 Step 4 serial-to-serial comparison with a failure branch.

## Global Constraints

- Caddy binds only `{{ ansible_host }}` (Caddyfile.j2:48). The probe must connect there, never to `127.0.0.1`.
- SNI is `home.{{ service_domain }}`, the same name the existing verify gate uses.
- certwatch's contract is unchanged: exit 0 = fine, exit 1 + reasons on stderr = problem. `roles/svc_media/tasks/verify.yml` passes that stderr to `media_cert_detail`.
- "Could not look" is a failure, never a pass. An unreachable or unparseable served certificate must exit 1 with its own message.
- Never `git add -A`; stage explicit paths. Never echo vault secrets. Push after committing.
- Deploys from this Mac run as `USE_VAULT_FILE=1 caffeinate -i make …`, and `git status --porcelain` must be empty before each deploy-proof (the iCloud gotcha).

## Review Focus

1. **The probe cannot connect** (Caddy stopped, wrong address): exit 1 with `could not read the certificate Caddy is serving`. Case `unreachable`.
2. **A TCP listener answers but sends no certificate** (empty fingerprint): the same "could not look" outcome, never a match between two empty strings. Case `closes`.
3. **The listener accepts and then says nothing**: the probe's `timeout` must end it, giving "could not look", not a hang. Case `silent`, which runs only where coreutils `timeout` exists and prints a skip note otherwise.
4. **The hook copied the certificate but Caddy did not reload** (today's live state): `Caddy is serving a different certificate`. Case `stale-served`.
5. **The hook never ran**: `deploy hook did not copy`. Case `hook-missed`.

Accepted and not tested: certwatch running in the seconds between certbot writing `live/` and the hook finishing would see a mismatch. The daily timer makes that window negligible, and the next run clears it.

---

### Task 0: commit the plan

- [ ] Add a row to `docs/superpowers/plans/README.md`: `| [2026-11-25-caddy-cert-reload](2026-11-25-caddy-cert-reload.md) | Shipped |`. It is written as Shipped because the README is a record. If this branch does not merge, the row does not merge either.
- [ ] `git add docs/superpowers/plans/2026-11-25-caddy-cert-reload.md docs/superpowers/plans/README.md && git commit -m "docs: plan forcing Caddy to load renewed certificates and watching what it serves"`, then `git push -u origin fix/caddy-cert-reload`.

### Task 1: certwatch compares the served certificate

**Files:**
- Modify: `roles/svc_media/templates/certwatch.sh.j2`
- Modify: `roles/svc_media/files/homelab-certwatch.service` (add `After=caddy.service` to `[Unit]`, with a comment explaining the boot race)
- Create: `tests/validate_certwatch.py`
- Modify: `tests/validate_shell_templates.py` (base `context`: `"ansible_host": "192.0.2.30"`, a documentation-range address, with a one-line comment)

**Interfaces:**
- Produces single-line, top-of-file assignments in the rendered script. The gate rewrites each of these:
  - `LIVE_CERT=` (the live fullchain path, `| quote`d)
  - `CADDY_CERT=/etc/caddy/certs/fullchain.pem`
  - `SERVED_HOST={{ ansible_host }}`
  - `SERVED_PORT=443`
  - `SERVED_SNI=home.{{ service_domain }}`
  - `PROBE_TIMEOUT=10`
  - `DAYS=` (unchanged)

  `CERTS=("$LIVE_CERT" "$CADDY_CERT")` replaces the multi-line array literal.
- Pinned stderr substrings: `could not read the certificate Caddy is serving`, `Caddy is serving a different certificate`, `deploy hook did not copy`, and `expires within` (existing).
- Produces `render(template: str, **ctx) -> str` and `setvar(text, name, value) -> str` in `tests/validate_certwatch.py`. Task 2 reuses `render`.

- [ ] **Step 1: Write the failing gate `tests/validate_certwatch.py`**

Use a docstring in the style of `tests/validate_dnf_makecache_retry.py` that gives the 2026-11-25 reason and the case table. Set `GATE_GROUP = "shell"`.

- **Render.** Use jinja2 with `StrictUndefined` and the filters `comment` (from `validate_generated_catalog`) and `quote` (`shlex.quote`). The context is `ansible_managed`, `service_domain="example.test"`, `ansible_host="127.0.0.1"`, and `cert_expiry_days` loaded from `inventory/group_vars/all/main.yml`. Fail loudly if any `{{` survives.
- **`setvar`.** Use `re.subn` with a function replacement. Anything other than exactly one replacement raises `SystemExit` naming the variable.
- **Certificates.** Generate them with the `openssl` CLI in a temp dir: `long_a` and `long_b` with `-days 365` and distinct keys, and `short` with `-days 5`. Each is `req -x509 -newkey rsa:2048 -nodes -subj /CN=*.example.test`. If `openssl` is missing, exit 127. Never pass.
- **Listeners.** All run on `127.0.0.1:0` in daemon threads.
  - The TLS listener serves a given certificate. Each accepted connection gets `settimeout(5)`, and each handshake is in try/except so one bad client cannot kill the thread.
  - The `closes` listener accepts and immediately closes.
  - The `silent` listener accepts and holds the connection open, sending nothing.
  - `unreachable` binds a port, closes it, and uses that port.
- **Run.** Write the script with `newline=""` and keep the inherited PATH. If `shutil.which("timeout")` is None (macOS), prepend a temp `bin/timeout` shim that drops its first argument and `exec`s the rest. Use `subprocess.run(..., timeout=30)` per case; a `TimeoutExpired` is a named case failure.

Cases are `(served, caddy_file, live_file, want_rc, must_contain)`:

| name | served | caddy | live | rc | must contain |
|---|---|---|---|---|---|
| `clean` | TLS long_a | long_a | long_a | 0 | `Caddy is serving` (stdout) |
| `stale-served` | TLS long_a | long_b | long_b | 1 | `Caddy is serving a different certificate` |
| `hook-missed` | TLS long_a | long_a | long_b | 1 | `deploy hook did not copy` |
| `unreachable` | closed port | long_a | long_a | 1 | `could not read the certificate Caddy is serving` |
| `closes` | accept+close | long_a | long_a | 1 | `could not read the certificate Caddy is serving` |
| `silent` | accept+silence, `PROBE_TIMEOUT=2`, **only with real `timeout`** | long_a | long_a | 1 | `could not read the certificate Caddy is serving` |
| `expiring` | TLS short | short | short | 1 | `expires within` |

On success print `certwatch: OK (N cases, N-1 of them must fail)`, where N counts the cases actually run. Append `; silent skipped: no coreutils timeout` when it was skipped. Any mismatch prints the case, the expected and actual rc, and the full output, then exits 1.

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python tests/validate_certwatch.py`
Expected: FAIL, on a missing `setvar` target or on rc=0 for `stale-served` and `hook-missed`.

- [ ] **Step 3: Implement the comparison in `certwatch.sh.j2`**

- Refactor to the Interfaces assignments. Keep the per-file readability and `-checkend` loop behaviour identical.
- `fp_file() { openssl x509 -noout -fingerprint -sha256 -in "$1" 2>/dev/null | cut -d= -f2; }`
- Served: `timeout "$PROBE_TIMEOUT" openssl s_client -connect "${SERVED_HOST}:${SERVED_PORT}" -servername "$SERVED_SNI" </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2`.
- Empty → `could not read the certificate Caddy is serving on ${SERVED_HOST}:${SERVED_PORT} (SNI ${SERVED_SNI})`. Otherwise echo `Caddy is serving ${fp}`.
- Served ≠ `CADDY_CERT` (both non-empty) → `Caddy is serving a different certificate than ${CADDY_CERT} — the reload after renewal did not take; run: caddy reload --force --config /etc/caddy/Caddyfile`.
- `CADDY_CERT` ≠ `LIVE_CERT` (both non-empty) → `${CADDY_CERT} differs from ${LIVE_CERT} — the certbot deploy hook did not copy the renewal`.
- Header comment: there are three copies, the one in Caddy's memory is the one served, and cite 2026-11-25 `config is unchanged`.

- [ ] **Step 4: Unit ordering, plus `ansible_host` in the shell-template context** (per Files above).

- [ ] **Step 5: Run the gates and confirm they pass**

Run: `.venv/bin/python tests/validate_certwatch.py && make validate-shell validate-python validate-systemd`
Expected: `certwatch: OK (6 cases, 5 of them must fail; silent skipped: no coreutils timeout)` on macOS (7/6 on Linux), and ShellCheck, ruff and systemd all clean.

- [ ] **Step 6: Commit and push**

```bash
git add roles/svc_media/templates/certwatch.sh.j2 roles/svc_media/files/homelab-certwatch.service tests/validate_certwatch.py tests/validate_shell_templates.py
git commit -m "fix: make certwatch fail when Caddy serves a certificate other than the one on disk"
git push
```

### Task 2: the hook forces the reload, and the verify assert names what failed

**Files:**
- Modify: `roles/svc_media/templates/certbot-deploy-caddy.sh.j2`
- Modify: `verify.yml:94-103`
- Modify: `tests/validate_certwatch.py` (adds `check_hook()`)
- Modify: `docs/dns-pfsense-caddy.md` (renewal section, the manual check at :129-134, and the troubleshooting row at :151), `docs/unattended.md:70`, `docs/operations.md:110`, `docs/incidents.md` ("DNS, Caddy, or service-name failure" section)

- [ ] **Step 1: Write a failing `check_hook()`.** It renders the hook with Task 1's `render`. It requires `caddy reload --force --config /etc/caddy/Caddyfile` and the absence of `systemctl reload caddy`; the failure message cites 2026-11-25 and "config is unchanged". The success line gains `; deploy hook forces reload`.
- [ ] **Step 2: Run it and confirm it fails** (`.venv/bin/python tests/validate_certwatch.py`; expect a FAIL from `check_hook`).
- [ ] **Step 3: Change the hook.** Keep the `systemctl is-active --quiet caddy` guard, and replace the reload with `caddy reload --force --config /etc/caddy/Caddyfile`. The comment says why `--force`, and why not a unit drop-in (review finding 9).
- [ ] **Step 4: Change the verify assert.** The task name becomes `Assert the TLS certificate check passed (expiry, and Caddy serving the on-disk certificate)`. `fail_msg` becomes `The wildcard certificate check failed: {{ … media_cert_detail … }}`. Update the comment above it to match.
- [ ] **Step 5: Docs.**
  - `dns-pfsense-caddy.md`:
    - The hook "force-reloads Caddy (a plain reload is a no-op — the config is unchanged)".
    - The manual check gains a fingerprint comparison of the socket against `/etc/caddy/certs/fullchain.pem`.
    - The troubleshooting row names `homelab-certwatch` and `caddy reload --force`.
  - `unattended.md:70`: the failure means "under 21 days, or Caddy is serving a different certificate than the one on disk, or it couldn't read the served certificate". If it's a different certificate, run `sudo caddy reload --force --config /etc/caddy/Caddyfile`.
  - `operations.md:110`: "wildcard under 21 days, or served ≠ on-disk".
  - `incidents.md`: add one paragraph on the certwatch "different certificate" message and its fix.
- [ ] **Step 6: Run `make validate; echo rc=$?`**. Expect `rc=0`.
- [ ] **Step 7: Commit and push** with explicit paths: `fix: force Caddy to reload the renewed certificate and name the cert failure accurately`.

### Task 3: live proof, remediation, ship (controller only)

- [ ] **Step 0: Preconditions.**
  - Run `git fetch`, then `git merge-base --is-ancestor origin/main HEAD`.
  - On svc-media, `systemctl --failed` must be empty. Name anything that isn't.
  - `git status --porcelain` must be empty.
- [ ] **Step 1: Deploy svc-media.** Run `USE_VAULT_FILE=1 caffeinate -i make media; echo rc=$?`. Expect rc=0, with changes to the hook, certwatch script and unit, and **no** Caddy restart. Then check the journal: no new `Started caddy` / `Reloading caddy`.
- [ ] **Step 2: Live positive control.**
  - Run `ssh straderb@192.168.1.30 'sudo /usr/local/sbin/homelab-certwatch.sh; echo rc=$?'`. Expect **rc=1** and `Caddy is serving a different certificate`.
  - Then run `USE_VAULT_FILE=1 caffeinate -i make verify`. Expect it red on exactly `Assert the TLS certificate check passed…`, carrying that message.
  - If either check reads green, stop.
- [ ] **Step 3: Run the fixed hook.** `ssh … 'sudo /etc/letsencrypt/renewal-hooks/deploy/caddy.sh; echo rc=$?'` should give rc=0. The caddy journal must show `/load` **without** `config is unchanged`.
- [ ] **Step 4: Prove the rotation.** The serial from `openssl s_client` against `192.168.1.30:443` (SNI `chat.fortwow.dev`) piped to `openssl x509 -noout -serial` must equal `openssl x509 -noout -serial -in /etc/caddy/certs/fullchain.pem` (on-disk serial `06A1FD…`). certwatch must give `rc=0`. Clear the certwatch unit with `sudo systemctl start homelab-certwatch.service` (re-checks and leaves `failed` on success).

  **If Caddy still serves the old serial after the forced reload:** remediate with `sudo systemctl restart caddy` (the live expiry is 2026-12-25 01:24 GMT), do NOT merge, and re-plan the hook.
- [ ] **Step 5: Final proof from a clean tree.**
  - `git status --porcelain` must be empty.
  - Run `make deploy-proof TARGET=media`. Expect `changed=0`.
  - Run `make deploy-proof TARGET=infra`. Expect the sync-only trio first, then re-run to `changed=0`.
  - Run `make verify`. Expect green.
- [ ] **Step 6: Clear the stale nightly failure.** On svc-infra, run `sudo systemctl start homelab-verify@svcops.service`. It must end `inactive`, not `failed`, with a success line in the journal.
- [ ] **Step 7: Ship.**
  - Run `git fetch` and repeat the ancestry check.
  - Run `git switch main && git merge --ff-only fix/caddy-cert-reload && git push`, then confirm CI is green on that push.
  - Delete the branch locally and on the remote, and remove the worktree.
  - Mark the `caddy-stale-cert-reload` memory CLOSED.
