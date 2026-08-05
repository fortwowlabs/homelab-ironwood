#!/usr/bin/env bash
# Report which upstream projects have shipped a release since the last report.
#
#   scripts/release-check.sh              # the whole report
#   scripts/release-check.sh --coverage   # label coverage; makes NO GitHub calls
#   make release-check
#
# Read-only. It reads registry manifest metadata and release feeds, and writes
# nothing unless given --state-out. Nothing here pulls, bumps, deploys or
# restarts, and tests/validate_scan_readonly.py fails the build if that changes.
#
# The sibling to `make image-check`, not a replacement for it. That one asks
# whether a pin has drifted from the tag it follows, which it can only do for
# the 13 images carrying a recorded `# tag:`. This one asks whether upstream has
# released anything, which it answers for every image with an OCI version label
# — including the 35 the BUMP PROCEDURE deliberately leaves untracked, whose
# release notes are the ones somebody actually needs to read.
#
# See docs/plans/release-report.md.
set -uo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

exec python3 "${here}/release_check.py" "$@"
