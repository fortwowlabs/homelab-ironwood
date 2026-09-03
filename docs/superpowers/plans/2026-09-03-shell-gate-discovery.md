# Shell Gate Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `tests/validate_shell_templates.py` discover every `*.sh.j2` in the repo instead of reading a hand-maintained list, so a shell template cannot be added and silently left unchecked.

**Architecture:** Replace the `TEMPLATES` tuple with a glob over `roles/*/templates/*.sh.j2`. Because the Jinja environment already uses `StrictUndefined`, a newly added template whose variables are absent from the render context raises `UndefinedError` — the gate catches that and fails with a message naming the template and the missing variable. That failure *is* the coverage control: adding a template without wiring its context breaks the build rather than being skipped.

**Tech Stack:** Python 3.13, Jinja2 (`StrictUndefined`), ShellCheck, `tests/run_gates.py` group `shell`.

**Spec:** The architecture review of 2026-09-03 (this repo, conversation record). Finding #1.

## Global Constraints

- The gate keeps `GATE_GROUP = "shell"` so `tests/run_gates.py` continues to collect it. Do not add a Makefile line.
- Rendering uses `StrictUndefined`. Never add `| default(...)` to a template to make this gate pass — add the variable to the render context instead.
- Fixture values that a shipping script actually consumes must be read from `inventory/group_vars/all/main.yml` or the role defaults, never hardcoded. A fixture that drifts from production still renders and still ShellChecks clean, so the gate would pass while proving nothing. This convention is already established in the file; follow it.
- The gate must fail, not pass, when discovery returns zero templates.

---

## Current state (measured 2026-09-03)

27 `*.sh.j2` files exist. `TEMPLATES` lists 23. The four uncovered:

| Template | Variables it needs | Already in context? |
|---|---|---|
| `roles/service_vm/templates/container-drift.sh.j2` | `ansible_managed`, `svc_uid` | yes, both |
| `roles/svc_infra/templates/release-run.sh.j2` | `ansible_managed`, `verify_runner_root`, `verify_runner_secret_file`, `verify_runner_user` | yes, all |
| `roles/svc_media/templates/certbot-deploy-caddy.sh.j2` | `service_domain` | yes |
| `roles/pve_mon/templates/nfsguard.sh.j2` | `ansible_managed`, `alert_realert_hours`, `nfsguard_connect_timeout`, `nfsguard_port`, `truenas_ip`, `truenas_vmid` | **no** — four missing |

So three of the four render against the existing context unchanged. Only `nfsguard.sh.j2` needs new context entries:

- `truenas_ip` (`192.168.1.20`) and `truenas_vmid` (`100`) — read from `inventory/group_vars/all/main.yml`, per the constraint above.
- `nfsguard_port` (`2049`) and `nfsguard_connect_timeout` (`5`) — read from `roles/pve_mon/defaults/main.yml`.

`container-drift.sh.j2` is separately behaviour-tested by `tests/validate_container_drift.py`; adding it here is additive, not redundant — that gate exercises exit paths, this one checks the shell is well-formed.

## File Structure

- Modify: `tests/validate_shell_templates.py` — the whole change lives here.
  - Delete: the `TEMPLATES` tuple (lines 24–46).
  - Add: `discover_templates()` returning a sorted list of repo-relative POSIX paths.
  - Add: four context entries for `nfsguard.sh.j2`, sourced from real files.
  - Change: the render loop, to catch `jinja2.UndefinedError` per template and collect it as a failure rather than crashing.
  - Change: the success line, to report the discovered count.
  - Fix en route: `F401` — `AttrDict` is imported on line 15 and unused.

No other file changes. `EXTRA_RENDERS` stays exactly as it is.

---

### Task 1: Discover templates by glob, and fail on an empty discovery

**Files:**
- Modify: `tests/validate_shell_templates.py:24-46` (delete `TEMPLATES`), `:69` (`main`), `:160-175` (render loop and summary)

**Interfaces:**
- Consumes: nothing from earlier tasks; this is the first task.
- Produces: `discover_templates() -> list[str]` — repo-relative POSIX paths, sorted. Task 2 does not call it; it only adds context keys.

- [ ] **Step 1: Write the failing test**

There is no pytest harness in this repo — gates are standalone scripts run by `tests/run_gates.py`. The test is therefore a temporary assertion added to the gate itself, run once, then replaced by the real implementation. Add this at the top of `main()`, before anything else:

```python
    # TEMPORARY — proves discovery finds more than the old hand-list of 23.
    discovered = discover_templates()
    assert len(discovered) == 27, f"expected 27, found {len(discovered)}: {discovered}"
```

And add the function above `main()`:

```python
def discover_templates() -> list[str]:
    """Every shell template in the repo, as repo-relative POSIX paths.

    Globbed rather than listed. The list this replaces had fallen four
    templates behind — including certbot-deploy-caddy.sh.j2, which nothing
    else checked at all — and nothing could report that, because a gate that
    silently checks a subset looks exactly like a gate that passes.
    """
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.glob("roles/*/templates/*.sh.j2")
    )
```

- [ ] **Step 2: Run it to verify the assertion holds**

Run: `.venv/bin/python tests/validate_shell_templates.py`

Expected: the assertion passes (27 found), then the run FAILS later with a `jinja2.exceptions.UndefinedError` traceback mentioning `nfsguard_port` or another nfsguard variable — because `TEMPLATES` is still driving the render loop but the glob proves the count. If the assertion itself fails, the glob pattern is wrong; fix it before continuing.

- [ ] **Step 3: Replace the hand-list with discovery**

Delete the entire `TEMPLATES = (...)` tuple (lines 24–46) and its preceding comment. Delete the temporary assertion from Step 1. Change the `renders` construction in `main()` from:

```python
    renders = [(name, context) for name in TEMPLATES]
```

to:

```python
    templates = discover_templates()
    if not templates:
        print(
            "Shell template discovery found nothing under roles/*/templates/"
            "*.sh.j2. A gate that checks zero templates passes while proving "
            "nothing; refusing to report success.",
            file=sys.stderr,
        )
        return 1
    renders = [(name, context) for name in templates]
```

Update the summary print at the end of `main()` from `len(TEMPLATES)` to `len(templates)`.

- [ ] **Step 4: Make the render loop report an unwired template instead of crashing**

Replace the body of the render loop so an `UndefinedError` becomes a collected failure with an actionable message:

```python
    from jinja2 import UndefinedError  # add to the top-level jinja2 import

    failures: list[str] = []
    for template_name, render_context in renders:
        try:
            rendered = environment.get_template(template_name).render(**render_context)
        except UndefinedError as error:
            failures.append(
                f"{template_name}: render failed — {error}.\n"
                "    This template is discovered by glob but its variables are "
                "not in the fixture context.\n"
                "    Add them to `context` in this file (reading real values "
                "from group_vars or role defaults),\n"
                "    rather than adding a `| default(...)` to the template."
            )
            continue
        result = subprocess.run(
            [shellcheck, "--shell=bash", "-"],
            input=rendered,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            failures.append(f"{template_name}:\n{result.stdout}{result.stderr}")
```

Move `UndefinedError` into the existing import line so it reads:

```python
from jinja2 import Environment, FileSystemLoader, StrictUndefined, UndefinedError
```

- [ ] **Step 5: Run it and confirm it now fails on nfsguard specifically**

Run: `.venv/bin/python tests/validate_shell_templates.py`

Expected: FAIL, with a message naming `roles/pve_mon/templates/nfsguard.sh.j2` and an undefined variable, plus the "add them to `context`" guidance. The other 26 templates should render and ShellCheck clean. If any template *other* than nfsguard fails, stop and read the error — it is a real finding in a script that has never been checked.

- [ ] **Step 6: Commit the discovery half**

```bash
git add tests/validate_shell_templates.py
git commit -m "test: discover shell templates by glob instead of a hand-list

The list had fallen four templates behind — nfsguard, container-drift,
release-run, and certbot-deploy-caddy, the last of which nothing checked
at all despite running unattended in the cert renewal path.

Discovery makes adding a template a build failure until its render
context is wired, which is the coverage control the list never had. An
empty discovery is fatal for the same reason."
```

---

### Task 2: Wire the nfsguard render context

**Files:**
- Modify: `tests/validate_shell_templates.py` — the `main()` context dict, and the defaults/vars loading above it

**Interfaces:**
- Consumes: `discover_templates()` from Task 1; the `main_vars` dict already loaded in `main()`.
- Produces: a context that renders all 27 templates. Nothing later consumes this.

- [ ] **Step 1: Load the pve_mon defaults alongside the existing loads**

`main()` already loads `infra_defaults` from `roles/svc_infra/defaults/main.yml`. Add the same treatment for pve_mon, immediately after it:

```python
    # nfsguard.sh.j2's port and connect timeout live in the role defaults as
    # plain YAML (no Jinja), so load them straight from there — a hardcoded
    # fixture would still render and still ShellCheck clean if the real value
    # changed, which is the failure this file's other real-value loads exist
    # to prevent.
    pve_mon_defaults = yaml.safe_load(
        (ROOT / "roles/pve_mon/defaults/main.yml").read_text(encoding="utf-8")
    )
```

- [ ] **Step 2: Add the four context entries**

In the `context` dict, after the `disk_alert_nfs_threshold` entry, add:

```python
        # nfsguard.sh.j2 watches the TrueNAS export and can bounce the VM by
        # id, so both identities are read from the real inventory rather than
        # invented here — same reasoning as trivy_image and infra_textfile_dir
        # above.
        "truenas_ip": main_vars["truenas_ip"],
        "truenas_vmid": main_vars["truenas_vmid"],
        "nfsguard_port": pve_mon_defaults["nfsguard_port"],
        "nfsguard_connect_timeout": pve_mon_defaults["nfsguard_connect_timeout"],
```

- [ ] **Step 3: Run the gate and confirm all 27 render and pass**

Run: `.venv/bin/python tests/validate_shell_templates.py`

Expected: PASS, printing `Rendered shell templates: ShellCheck OK (27 templates, 28 renders)`. The 28th render is the existing `credential-canary.sh.j2` infra-context override in `EXTRA_RENDERS`.

If ShellCheck reports findings in any of the four newly covered scripts, **fix the script, not the gate** — these are genuine defects in code that ships. Commit such a fix as its own commit before Step 5, with a message naming the finding.

- [ ] **Step 4: Remove the now-unused import**

Line 15 imports `AttrDict` from `validate_generated_catalog` and never uses it (ruff `F401`). Change:

```python
from validate_generated_catalog import AttrDict, as_attr, comment, dict2items, split_url
```

to:

```python
from validate_generated_catalog import as_attr, comment, dict2items, split_url
```

- [ ] **Step 5: Run the whole shell group**

Run: `make validate-shell`

Expected: every gate in the `shell` group passes, including `validate_container_drift.py` and `validate_metric_write.py`.

- [ ] **Step 6: Run the full offline gate set**

Run: `make validate`

Expected: PASS. This is the repo's actual gate; the change touches a file `validate-shell` runs, so nothing else should move, but confirm rather than assume.

- [ ] **Step 7: Commit**

```bash
git add tests/validate_shell_templates.py
git commit -m "test: render nfsguard.sh.j2 in the shell gate

The last of the four templates discovery newly reaches. Its port and
timeout come from roles/pve_mon/defaults/main.yml and its TrueNAS
identity from group_vars, rather than fixture constants that could drift
from what ships without the gate noticing."
```

---

## Verification before merge

This branch changes no role code, so no deploy is required and `make verify` is not part of its evidence. The gate is the deliverable and `make validate` is the proof.

- [ ] `make validate` passes.
- [ ] `git status --porcelain` prints nothing.
- [ ] The summary line reports 27 templates. If it reports fewer, discovery regressed.
- [ ] Sanity-check the control by hand: `touch roles/mon/templates/zz-probe.sh.j2`, write `{{ deliberately_undefined }}` into it, run the gate, confirm it FAILS naming that file, then `rm` it. This is the whole point of the change — confirm it works once, then delete the probe.

## Merge

Standard workflow: commit, confirm clean tree, merge to `main`, push, delete the branch. No deploy step.
