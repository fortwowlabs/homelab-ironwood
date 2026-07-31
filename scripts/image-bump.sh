#!/usr/bin/env bash
# Bump one pinned image to the digest its tag currently resolves to.
#
#   scripts/image-bump.sh ghcr.io/corentinth/it-tools:latest
#   make image-bump REF=docker.io/louislam/uptime-kuma:1
#
# Does the three mechanical steps of the BUMP PROCEDURE (see the top of
# inventory/group_vars/all/apps.yml) so none of them can be done wrong:
#   1. resolve the tag to a digest from the registry manifest
#   2. rewrite the pinned line
#   3. record the digest being replaced, and the tag being tracked
#
# It EDITS ONLY. It does not validate, deploy, or restart anything — those stay
# deliberate steps, because the whole point of the surrounding workflow is that
# a human decides when live infrastructure changes.
set -uo pipefail

ref=${1:-}
if [[ -z $ref ]]; then
    echo "usage: ${0##*/} <image>:<tag>     e.g. docker.io/louislam/uptime-kuma:1" >&2
    exit 64
fi
if [[ $ref != *:* ]]; then
    echo "no tag in '${ref}'. A bump needs the tag to track, not just a repository." >&2
    exit 64
fi

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=$(dirname -- "$here")
catalogs=(
    "${root}/inventory/group_vars/all/apps.yml"
    "${root}/inventory/group_vars/all/infra-apps.yml"
    "${root}/inventory/group_vars/all/main.yml"
    "${root}/inventory/group_vars/all/minecraft.yml"
)

tag=${ref##*:}

new_ref=$("${here}/image-digest.sh" "$ref") || exit 1
new_digest=${new_ref##*@}
# image-digest normalises Docker Hub short forms, so match on what it prints:
# `postgres:18-alpine` then finds the `docker.io/library/postgres@…` line.
repo_printed=${new_ref%@*}

# Locating and rewriting are done in python rather than bash. macOS ships bash
# 3.2, which has no `mapfile`, and this script runs on the workstation — an
# earlier version used it and died on the only machine that runs this command.
python3 - "$repo_printed" "$new_digest" "$tag" "$(date +%Y-%m-%d)" "${catalogs[@]}" <<'PYEOF'
import re
import sys

repo, new_digest, tag, today = sys.argv[1:5]
catalogs = sys.argv[5:]

pin = re.compile(re.escape(repo) + r"@(sha256:[0-9a-f]{64})")
hits = []
for path in catalogs:
    try:
        lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
    except FileNotFoundError:
        continue
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue          # a digest in a comment is a record, not a pin
        match = pin.search(line)
        if match:
            hits.append((path, index, match.group(1), lines))

if not hits:
    sys.exit(f"no pinned line found for {repo}\n"
             f"  searched: {', '.join(p.split('/')[-1] for p in catalogs)}")

# A digest shared by several services is a decision about all of them, never a
# mechanical edit: three services share one valkey pin and two share one
# postgres pin, deliberately, so there is one thing to track instead of several.
if len(hits) > 1:
    where = "\n".join(f"  {p}:{i + 1}" for p, i, _, _ in hits)
    sys.exit(f"{repo} is pinned in {len(hits)} places:\n{where}\n\n"
             "Shared pins are deliberate. Decide whether ALL of them move\n"
             "together, then edit by hand — see the BUMP PROCEDURE in apps.yml.")

path, index, old_digest, lines = hits[0]
if old_digest == new_digest:
    print(f"{repo} is already at the digest {tag} resolves to — nothing to do.")
    print(f"  {old_digest}")
    raise SystemExit(3)

indent = re.match(r"[ \t]*", lines[index]).group(0)

# Replace any existing record rather than stacking one per bump: the useful
# rollback target is the digest that was live until a moment ago, and the full
# history is what `git log -p` is for.
start = index
while start > 0 and lines[start - 1].lstrip().startswith(("# was ", "# tag:")):
    start -= 1

lines[start:index] = [f"{indent}# tag: {tag}\n",
                      f"{indent}# was {today}: {old_digest}\n"]
lines[start + 2] = lines[start + 2].replace(old_digest, new_digest)
open(path, "w", encoding="utf-8").write("".join(lines))
PYEOF
status=$?
(( status == 3 )) && exit 0
(( status != 0 )) && exit "$status"

echo
echo "bumped ${repo_printed} -> ${new_digest}  (tag ${tag})"
echo
echo "next:"
echo "  make validate                     # the provenance gate checks the recorded digest"
echo "  make <dl|media|infra>             # one VM; a bad digest aborts before any restart"
echo "  make scan                         # confirm the CVE count actually moved"
