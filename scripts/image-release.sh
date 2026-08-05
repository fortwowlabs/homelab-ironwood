#!/usr/bin/env bash
# What version is this image, and what has upstream released since?
#
#   scripts/image-release.sh lscr.io/linuxserver/sonarr
#   scripts/image-release.sh ghcr.io/authelia/authelia@sha256:1b363e…
#   make image-release REF=docker.io/syncthing/syncthing
#
# Given a bare repository it looks up the digest this repo currently pins.
# Given an explicit @sha256:… it examines that digest, pinned here or not.
#
# Read-only, and the single-image counterpart to scripts/release-check.sh —
# useful when deciding whether one specific bump is worth doing, without
# spending the whole GitHub rate-limit budget on a full report.
set -uo pipefail

ref=${1:-}
if [[ -z $ref ]]; then
    echo "usage: ${0##*/} <repo>[@sha256:…]   e.g. lscr.io/linuxserver/sonarr" >&2
    exit 64
fi

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

exec python3 "${here}/release_check.py" --image "$ref" "${@:2}"
