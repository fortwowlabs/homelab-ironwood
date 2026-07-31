#!/usr/bin/env bash
# Report which pinned images have moved on since they were pinned.
#
#   scripts/image-check.sh          # every image with a recorded tag
#   make image-check
#
# Read-only. It queries registry manifests and changes nothing.
#
# WHY THIS NEEDS A RECORDED TAG. The catalogs pin digests and nothing else, and
# a digest carries no memory of where it came from — so "is there a newer
# version?" is unanswerable without knowing which tag the pin was tracking.
# Discovering that after the fact is impractical: uptime-kuma publishes 376
# tags and the postgres library 1385, and finding which one resolves to a given
# digest means resolving them all.
#
# So the tag is recorded, as a `# tag: <tag>` comment written by image-bump.sh.
# Coverage therefore starts near zero and grows as images are bumped, which is
# why this reports UNTRACKED separately rather than pretending silence is good
# news. An unbumped image is not up to date; it is unmeasured.
set -uo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=$(dirname -- "$here")

python3 - "$here" "$root" <<'PYEOF'
import re
import subprocess
import sys

here, root = sys.argv[1:3]
catalogs = [
    f"{root}/inventory/group_vars/all/apps.yml",
    f"{root}/inventory/group_vars/all/infra-apps.yml",
    f"{root}/inventory/group_vars/all/main.yml",
    f"{root}/inventory/group_vars/all/minecraft.yml",
]

PIN = re.compile(r"([A-Za-z0-9._/-]+)@(sha256:[0-9a-f]{64})")
TAG = re.compile(r"#\s*tag:\s*(\S+)")

tracked, untracked = [], []
for path in catalogs:
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        continue
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = PIN.search(line)
        if not match:
            continue
        repo, digest = match.groups()
        # The tag record sits in the contiguous comment block directly above
        # the pin. Walking further than that is a real bug, not a nicety: in
        # apps.yml the multi-container images sit on consecutive lines, so a
        # fixed lookback credited netbox's `# tag: 18-alpine` to the valkey pin
        # on the next line and immich-server's `v3.0.3` to immich's postgres.
        # Both then "resolved" against the wrong repository.
        tag = None
        back = index - 1
        while back >= 0 and lines[back].lstrip().startswith("#"):
            found = TAG.search(lines[back])
            if found:
                tag = found.group(1)
                break
            back -= 1
        (tracked if tag else untracked).append((repo, digest, tag))

if not tracked:
    print("No images have a recorded tag yet.")
    print()
    print(f"  {len(untracked)} pinned image(s) are UNTRACKED — their pin cannot be")
    print("  compared against anything until the tag it follows is recorded.")
    print()
    print("  `make image-bump REF=<repo>:<tag>` records the tag as it bumps, so")
    print("  coverage grows as images are updated. To record one without changing")
    print("  the digest, add `# tag: <tag>` directly above its image line.")
    raise SystemExit(0)

behind, current, failed = [], [], []
for repo, digest, tag in sorted(tracked):
    result = subprocess.run(
        [f"{here}/image-digest.sh", f"{repo}:{tag}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or "@" not in result.stdout:
        failed.append((repo, tag))
        continue
    latest = result.stdout.strip().split("@", 1)[1]
    (current if latest == digest else behind).append((repo, tag, digest, latest))

if behind:
    print(f"BEHIND ({len(behind)}):")
    for repo, tag, old, new in behind:
        print(f"  {repo}:{tag}")
        print(f"    pinned {old[:26]}…")
        print(f"    now    {new[:26]}…")
        print(f"    bump:  make image-bump REF={repo}:{tag}")
    print()

if failed:
    # Never silently drop these. An image whose tag could not be resolved is
    # unknown, not current, and the difference is the entire point.
    print(f"COULD NOT CHECK ({len(failed)}): tag gone, renamed, or registry down")
    for repo, tag in failed:
        print(f"  {repo}:{tag}")
    print()

print(f"up to date: {len(current)}    behind: {len(behind)}    "
      f"unresolved: {len(failed)}    untracked: {len(untracked)}")
if untracked:
    print(f"\n{len(untracked)} image(s) have no recorded tag and were NOT checked.")
    print("They are unmeasured rather than current. Recording accrues with each bump.")
PYEOF
