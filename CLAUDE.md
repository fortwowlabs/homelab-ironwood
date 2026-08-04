# Working in this repo

## The change workflow

Every change to this repo is deployed to live infrastructure, so the ordering
below is not bureaucracy — it exists so that the state running on the VMs
always corresponds to a commit that exists in git.

1. **Branch.** `git switch -c <type>/<name>` off current `main`. Parallel work
   gets a worktree instead so the deploys can be serialized without the
   checkouts fighting each other.
2. **Edit**, then `make validate` (offline gates, no VMs touched).
3. **Deploy iteratively** while developing — `make dl` / `make media` /
   `make infra` scoped to the VM being changed. Expect an uncommitted tree
   here; that is what iteration is.
4. **Commit** once the change is finished.
5. **Confirm the tree is clean** — `git status --porcelain` must print
   nothing. Untracked files count.
6. **Final deploy from the clean tree**, then `make verify`. The deploy must
   report `changed=0`.
7. **Merge to `main` and push.**
8. **Delete the branch** — locally with `git branch -d`, and on the remote if
   it was pushed. Remove the worktree if one was used.

### Why the commit comes before the final deploy

If the last deploy runs from an uncommitted tree, then "verified" and
"committed" are two different states and nothing checks that they match. Commit
first and the check becomes mechanical rather than procedural: a deploy that
reports `changed=0` against a clean tree is proof that what is running equals
what is committed. That is the single most valuable line in the whole sequence
— if it reports anything other than `changed=0`, the deployed state and the
commit have diverged and the difference must be explained before merging.

Deploying from a dirty tree during step 3 is fine and expected. The guarantee
is only claimed at the end.

**Caveat, stated honestly:** `inventory/group_vars/all/vault.yml` is
gitignored, so a clean tree still does not fully describe the deployed state.
The commit pins the code, not the secrets. Rebuilding from a bare clone
requires the vault out of band.

**Second caveat: on svc-infra the first deploy after any commit reports
`changed=3`, not `changed=0`.** The nightly runner keeps a `git archive` of the
tree at `/opt/homelab-iac` with the deployed revision in `.deployed-rev`. Right
after a commit that file still names the previous revision, so the sync block
rebuilds the archive, unpacks it and records the new revision — three changed
tasks, every time. Run `make infra` again and it settles to `changed=0`.

So step 6 in practice is: deploy, and if svc-infra reports exactly those three,
deploy once more and require `changed=0` from the second run. Anything else
still has to be explained before merging. Do not paper over a genuine diff by
running the deploy twice and quoting the second number — check *which* tasks
changed.

### Step 8 was skipped for the repo's first 75 commits

Twenty-two merged branches accumulated because the workflow had no delete step.
They cost nothing but they hide whether anything is genuinely in flight. Delete
the branch as part of merging, not as an occasional cleanup.

## CI does not gate anything right now

`.github/workflows/validate.yml` triggers on `pull_request` only. This repo
ff-merges locally and pushes straight to `main`, so **CI has never run** — zero
workflow runs, zero PRs. `make validate` on the workstation is the only gate
that has ever fired.

Do not describe a change as "CI validated". Either add a `push:` trigger for
`main`, or route changes through PRs — until one of those happens, local
`make validate` is the whole story.

## Standing rules

- **Never echo vault secrets** to the terminal, into logs, or into a commit.
  Secret-bearing Ansible tasks use `no_log: true`.
- **Never commit `vault.yml`.** It is gitignored; keep it that way.
- **Never `git add -A`.** Stage explicit paths. The repo root holds working
  notes that quote live credentials.
- **Respect classifier blocks.** If a command is denied, stop and explain what
  was being attempted and why. Do not look for a way around it.
- **Push after committing** without waiting to be asked.

## Updating container images

Never hand-edit a digest. Three commands do it, and the middle one exists so the
mechanical steps cannot be done wrong:

```bash
make image-check                                       # which pins have fallen behind
make image-bump REF=docker.io/louislam/uptime-kuma:1   # resolve + rewrite + record
make image-digest REF=ghcr.io/owner/image:tag          # just resolve, no edit
```

`image-bump` **edits only**. It never validates, deploys or restarts — those
stay deliberate. Follow it with `make validate`, then one VM (`make dl` /
`make media` / `make infra`), then `make scan` to confirm the CVE count actually
moved. The full procedure is the `BUMP PROCEDURE` block at the top of
`inventory/group_vars/all/apps.yml`.

Four things that will bite:

- **`image-bump` refuses a digest pinned in more than one place.** Three
  services share one valkey pin and two share one postgres pin, deliberately.
  That is a decision about all of them, so it is never a mechanical edit.
- **`image-check` only sees images with a recorded `# tag:`.** A digest carries
  no memory of the tag it came from, and it cannot be inferred — uptime-kuma
  publishes 376 tags and the postgres library 1385. Untracked pins are reported
  separately because an unbumped pin is *unmeasured*, not up to date. Coverage
  accrues as images are bumped; do not backfill by guessing.
- **A guessed tag is worse than no tag.** If `latest` does not resolve to the
  pinned digest, that does not mean the image is behind — it may be pinned to a
  version line on purpose (postgres `18-alpine`, Immich `v3.0.3`). Recording
  `latest` there would invite a major-version jump. Only record a tag you have
  confirmed resolves to the digest currently pinned.
- **`latest` resolving correctly is not sufficient either.** A recorded tag is
  what `image-check` follows, so `# tag: latest` on a service whose major
  version migrates its data one way makes the report a standing recommendation
  to do the thing the next bullet warns about — and it prints the same
  one-line `make image-bump REF=…:latest` for Authelia as for a static file
  server. Record a tag only where a major bump cannot cost data, or where a
  version line exists to follow. Sonarr, Jellyfin, Calibre-Web, Syncthing,
  Grafana, Authelia, Open WebUI and the Beszel hub are deliberately untracked
  for this reason; the full list and the reasoning are in the `BUMP PROCEDURE`
  block at the top of `apps.yml`. Untracked reads as *unmeasured*, which is
  the honest answer — never record `latest` to make the untracked count fall.
- **A bad digest is safe; a bad *version* is not.** The pre-pull means an
  unreachable digest aborts the deploy with the old container still serving. It
  does not protect you from an image that starts fine and migrates a database
  one way. Check what the service persists before bumping it.

## Verification means the application works

A container that is `up`, a unit that is `active`, and a Caddy smoke test
returning 200 together prove the process started — not that the service
functions. Two faults hid behind all-green checks in this repo: a wedged redis
broker that blocked Django migrations for a day, and a service still accepting
its upstream default credentials. When a change is supposed to make something
work, check that specific thing, by using it.

### So does scanning, and it fails the same way

`scan.yml` and `make scan` are report-only by construction:
`tests/validate_scan_readonly.py` fails the build if `--remediate`, an upgrade
invocation, `state: latest` or `podman pull` appears under a scan path. Do not
work around that gate — add the path to it.

Every check under it went through the same failure once: **it returned a clean
result because it had not actually run.** An image scan reported zero
vulnerabilities because it could not `chdir`. A benchmark reported hosts
unscanned because its result counter broke after a successful 620-second
evaluation. A port sweep found one open port where eleven were open. A
CSRF-protected login returned identical responses for the right and the wrong
password. Every one passed `make validate`.

So when adding or changing a check here: make it distinguish "none found" from
"could not look", and give it something that must be true if it ran. The port
scan fails outright if it cannot see the sshd port Ansible is connected over.
A check with no positive control is a check nobody can tell is broken.

**The credential canary no longer has one, and that is worth stating plainly.**
It used to be trusted because it caught Calibre-Web's live `admin`/`admin123` —
one probe that *had* to come back `vulnerable`, so a broken run showed up as
that probe going quiet. The password was changed on 2026-08-03, all three
probes now report clean, and a working canary and a completely dead one would
produce byte-identical JSON. What keeps it trustworthy instead is commit
`057e1e4`, which gave every probe a **three-state verdict** — `vulnerable`,
`ok`, `inconclusive`. A missing CSRF token, a connection refused, a curl
timeout or a 5xx used to emit `false` and render as the word `ok`; they now
emit `inconclusive`, and `scan.yml` escalates on it. So "could not look" is a
state of its own rather than an all-clear.

That is weaker than a live positive control and should not be described as
equivalent. Tri-state catches a probe that *failed*; it cannot catch a probe
that succeeds at asking the wrong question — a changed login route that still
returns 200, say, would read as `ok` forever. Restoring a real positive control
means a synthetic one: a throwaway service, or a disposable account on an
existing one, deliberately left on a known default so exactly one finding must
come back `vulnerable` every night. That was considered and deliberately not
done — it means standing up a genuinely insecure thing to prove a check works.
If the canary is ever extended to more services, revisit it.

### Alerting counts as an application

The same rule applies to the alert paths themselves, and it is easier to get
wrong here than anywhere else, because a broken alerter looks exactly like a
healthy estate. Do not conclude that a watcher works because its timer is
active or its script exits 0 — publish an alert and read it back out of ntfy:

```bash
curl -s "http://<svc-media>:8080/homelab-alerts/json?poll=1&since=10m"
```

(`since` accepts `24h`/`168h`/`all`, not `7d`. Retention is ~12h and in
memory, so poll promptly; the journals are the durable record.)

Three real bugs from the unattended-alerting work, all of which passed every
offline gate and every liveness check:

- The zed and smartd hooks read `NTFY_*` from their unit's `EnvironmentFile`.
  Those daemons are long-lived and exec hooks from *their own* environment, so
  the variables were unset and the hook died on `set -u`. Every disk-fault
  alert would have produced nothing. Daemon-invoked hooks must source
  `/etc/homelab-notify.env` themselves.
- `hc-ping.sh` was 0750 root-owned while the nightly verify unit runs as
  `svcops`; the ping was silently permission-denied, which would have made
  healthchecks.io report a daily false alarm.
- Ansible's `copy` under `become_user` needs `setfacl`, absent on these hosts.
  Drop to an unprivileged user for `command` tasks only; write files as root
  with `owner:` set.

## Rootless podman on svc-media and svc-infra

Only container-uid-0 maps to the host `homelab` user. Any non-root process
inside a container — a baked-in `USER`, or an entrypoint that drops privileges
— lands on an unwritable subuid. In order of preference: set `PUID`/`PGID=0`,
or `user: "0"` in the catalog, or `podman unshare chown -R <uid>:<gid>` the
mount. For caches and brokers, drop the volume entirely rather than fighting
ownership — that is what wedged both redis containers.

Corollary: root-squashed NFS plus any container that chowns is broken by
construction.
