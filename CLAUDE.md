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

## CI runs after the merge, not before it

`.github/workflows/validate.yml` triggers on `push` to `main` as well as on
`pull_request` (added 2026-08-04). Until then it triggered on `pull_request`
only, and because this repo ff-merges locally and pushes straight to `main`, CI
had **never run** — zero workflow runs, zero PRs, every gate enforced only by
whoever remembered to type `make validate`.

Be precise about what this does and does not buy.

**It does not gate.** The run happens after the merge, so it cannot stop a bad
change from landing on `main`. Local `make validate` is still the gate that
fires first and most often, and it is still the one to run before committing —
"CI will catch it" is not a reason to skip it.

**What it adds** is a second opinion from a machine that is not the author's
workstation, and one check the workstation physically cannot perform:
`systemd-analyze verify` against the unit files. `tests/validate_systemd_units.py`
switches from static text matching to real parsing when `systemd-analyze` is on
PATH, which on macOS it never is — so before this, no systemd had ever parsed a
unit in this repo. A unit that is syntactically wrong in a way text matching
cannot see would have deployed and failed on the host.

So: a red CI run on `main` means something already merged is broken and needs a
follow-up commit. Treat it as an alarm, not as a gate — and if it stays red,
that is the same "nobody looks at it" failure this repo worries about
everywhere else.

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

### Deciding *what* to bump: `make release-check`

`image-check` answers "has my pin drifted from the tag it follows", which it can
only do for the 13 images carrying a `# tag:`. `make release-check` answers
"what has upstream released", which it answers for **30 of 48** — including the
untracked ones, whose release notes are the ones actually worth reading before a
decision. It reads the version out of each pinned digest's OCI labels, so it
needs no recorded tag, and for three services that ship no label it asks the
running process instead (`release_version_probes`).

(Two other numbers appear and mean different things. `--coverage` says 29: it
counts mariadb and searxng, which have a version and a real upstream repository
but publish no GitHub *releases*, which coverage mode cannot know without
spending the request it exists to avoid — and it makes no probes. The report
says "33 resolved a version", of which 30 had a feed to compare against. **30
is the number that actually compares.**)

It runs weekly on svc-infra (Fri 08:30) and publishes to ntfy and
`https://scan.<domain>/releases.txt`. Three things to know:

- **It deliberately prints no bump command.** For an untracked image that would
  be the exact standing recommendation `BUMP PROCEDURE` exists to prevent. Read
  the notes, decide, bump by hand.
- **`behind` means "the pinned version is not the newest upstream release"** and
  nothing more. No ordering is claimed — nothing sensibly orders
  `cd80d60b-ls59`, `2026.7.28-8372f5d85` and `4.0.19.2979-ls320`. A pin held
  deliberately to an older line reads as behind for as long as that decision
  stands, which is correct.
- **A full run costs ~45 of the 60 unauthenticated GitHub requests/hour.** Two
  runs in one hour is one too many; the second reports `error`, and the report
  prints the remaining quota so that is diagnosable rather than mysterious.

`make image-release REF=<repo>` asks the same question about one image, which is
the cheap way to check before bumping. Full design in
`docs/plans/release-report.md`.

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

### Trending is separate from alerting, and both are needed

Every check here reduced its numbers to a threshold and threw the number away.
That is why the rpool fill was an outage rather than a slope somebody noticed a
week earlier: nothing was wrong until it was entirely wrong, because no series
existed to look at.

node_exporter's textfile collector on svc-infra is the bridge. Anything that
already knows a number can publish it:

```bash
homelab-metric-write --dir /opt/homelab/appdata/node-exporter-textfile \
    --file scan --prefix homelab_scan --success <<'EOF'
homelab_scan_images_total 48
EOF
```

The full contract is `--dir DIR --file BASENAME --prefix PREFIX [--labels
'k="v"'] [--success]`. Its exit codes are worth knowing before scripting
against it: `0` published, `1` bad arguments or an I/O failure, `2` malformed
input lines (previous file left in place), `3` no input lines at all (same).
argparse's own usage errors are remapped from its default exit code of `2` to
`1` deliberately, because `2` is reserved for "the data was bad" — a caller
can tell "my flag was wrong" from "the numbers I handed it don't parse"
without reading stderr.

Four rules, learned the hard way:

- **The write goes in the play, never in `roles/svc_infra`.** A metrics file
  changes every run, so a template task in the role would make every
  `make infra` report `changed` and the `changed=0` proof would stop meaning
  anything.
- **Emit the number the alert used.** Every emitter reads the same fact that
  feeds ntfy. A second independent parse of `state.json` can drift from the
  first, and then two numbers disagree with no way to tell which is right.
- **Never publish zeros you did not measure.** `homelab-metric-write` refuses
  empty input and leaves the previous file in place, because a stale number is
  detectable and a zero reads as good news. The drift script's cannot-look
  paths exit *before* printing counts for the same reason — and the guard
  around them in `container-drift.yml` is stricter than a plain length check:
  `regex_search` returns Python `None`, not `[]`, when the counts line is
  absent, and `| default([])` only substitutes for `Undefined` — it does not
  catch `None`. So the play hoists an explicit
  `service_vm_drift_counts is not none and service_vm_drift_counts | length
  == 4` into its own fact, tested once and shared by both the emit task and
  the assert after it, so the two guards cannot drift apart.
- **Emit before you assert.** `container-drift.yml` publishes and then asserts,
  in that order. The other way round, the chart goes blank exactly when
  something is wrong.

Per-image scan series carry three labels, not two:
`homelab_scan_image_vulnerabilities{image, digest, severity}`. `image` is the
ref with its digest stripped, so the series does not churn on every
`make image-bump`; `digest` is the pinned digest's first 7 characters and
exists because two *different* pins collapsed to the same `image` label set —
apps.yml deliberately holds valkey at two digests for two purposes — and
node_exporter silently kept one and dropped the other with
`node_textfile_scrape_error` still `0`. Nothing groups on `digest`; a per-repo
total needs an explicit `sum by (image)`.

`node_textfile_scrape_error` is node_exporter's own verdict on whether it could
parse the files, and the estate dashboard charts it. If a panel is empty, read
that before anything else — one malformed line makes node_exporter reject a
whole file, so a typo takes every series in it down at once. This is why the
publisher validates each line before writing, and why nothing emits `# HELP` or
`# TYPE`: the three drift files (one per service VM) share timestamp metric
names, and duplicate TYPE declarations across merged textfiles make
node_exporter reject them.

The release emitter's coverage count, `homelab_release_images_comparable`, is
`images_examined` minus `images_unmeasured` — it moves as pins gain `# tag:`
coverage or the catalog grows, so read it off the chart rather than quoting a
number here; it already moved once during this feature's own branch.

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
