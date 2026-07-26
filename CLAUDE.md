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

## Verification means the application works

A container that is `up`, a unit that is `active`, and a Caddy smoke test
returning 200 together prove the process started — not that the service
functions. Two faults hid behind all-green checks in this repo: a wedged redis
broker that blocked Django migrations for a day, and a service still accepting
its upstream default credentials. When a change is supposed to make something
work, check that specific thing, by using it.

## Rootless podman on svc-media and svc-infra

Only container-uid-0 maps to the host `homelab` user. Any non-root process
inside a container — a baked-in `USER`, or an entrypoint that drops privileges
— lands on an unwritable subuid. In order of preference: set `PUID`/`PGID=0`,
or `user: "0"` in the catalog, or `podman unshare chown -R <uid>:<gid>` the
mount. For caches and brokers, drop the volume entirely rather than fighting
ownership — that is what wedged both redis containers.

Corollary: root-squashed NFS plus any container that chowns is broken by
construction.
