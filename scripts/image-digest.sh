#!/usr/bin/env bash
# Resolve a container image tag to the immutable digest this repo pins.
#
#   scripts/image-digest.sh lscr.io/linuxserver/sonarr:latest
#   make image-digest REF=docker.io/library/postgres:18-alpine
#
# Every image in this estate is pinned by digest and the catalog validators
# reject anything else, so bumping one always means resolving a tag to a digest.
# Doing that by hand is a two-step OCI token dance, and the shortcut everybody
# reaches for is wrong: the Docker Hub API can report a different digest from
# the registry manifest, which is why the existing provenance comments say
# "verified against the registry manifest digest directly, not just the Hub API".
# This asks the registry.
#
# Auth is resolved GENERICALLY rather than per registry: request the manifest,
# read the WWW-Authenticate challenge from the 401, and fetch a token from
# whatever realm it names. lscr.io advertises ghcr.io's realm, so one code path
# covers Docker Hub, GHCR, LinuxServer and anything added later.
#
# Read-only: it fetches manifest metadata and nothing else. No image is pulled,
# no local state changes.
set -uo pipefail

ref=${1:-}
if [[ -z $ref ]]; then
    echo "usage: ${0##*/} <image>[:tag]     e.g. ghcr.io/immich-app/immich-server:v3.0.3" >&2
    exit 64
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required" >&2
    exit 69
fi

# --- split the reference -----------------------------------------------------
# A leading segment counts as a registry only if it looks like a hostname. Bare
# names ("postgres:18-alpine") and single-slash names ("library/postgres") are
# Docker Hub, where the official-image namespace is implicit.
host=${ref%%/*}
rest=${ref#*/}
if [[ $ref != */* || ( $host != *.* && $host != *:* && $host != localhost ) ]]; then
    host=registry-1.docker.io
    rest=$ref
    [[ $rest == */* ]] || rest="library/${rest}"
fi

# docker.io does NOT serve the registry API; registry-1.docker.io does. Every
# catalog entry here writes the short form, so this rewrite is the common path
# rather than an edge case — without it the bare form resolves and the explicit
# `docker.io/...` form fails, which is the opposite of what anyone would guess.
[[ $host == "docker.io" || $host == "index.docker.io" ]] && host=registry-1.docker.io

repo=${rest%%:*}
tag=${rest##*:}
[[ $repo == "$tag" ]] && tag=latest

# All four media types. A multi-arch image is an index, and asking only for a
# v2 manifest returns nothing useful for most of what this estate runs.
accept='application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.manifest.v1+json'

# --- authenticate if the registry asks ---------------------------------------
challenge=$(curl -sI "https://${host}/v2/${repo}/manifests/${tag}" 2>/dev/null \
    | tr -d '\r' | grep -i '^www-authenticate:' || true)

auth=()
if [[ -n $challenge ]]; then
    realm=$(sed -n 's/.*realm="\([^"]*\)".*/\1/p' <<<"$challenge")
    service=$(sed -n 's/.*service="\([^"]*\)".*/\1/p' <<<"$challenge")
    if [[ -n $realm ]]; then
        token=$(curl -s "${realm}?service=${service}&scope=repository:${repo}:pull" 2>/dev/null \
            | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("token") or d.get("access_token") or "")' 2>/dev/null)
        [[ -n $token ]] && auth=(-H "Authorization: Bearer ${token}")
    fi
fi

# ${auth[@]+"${auth[@]}"} rather than "${auth[@]}": under `set -u` an empty
# array is an unbound variable on the bash that ships with macOS, and the
# unauthenticated path is exactly when the array is empty.
digest=$(curl -sI ${auth[@]+"${auth[@]}"} -H "Accept: ${accept}" \
    "https://${host}/v2/${repo}/manifests/${tag}" 2>/dev/null \
    | tr -d '\r' | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: //p')

if [[ -z $digest ]]; then
    echo "could not resolve a digest for ${ref}" >&2
    echo "  tried: https://${host}/v2/${repo}/manifests/${tag}" >&2
    echo "  check the tag exists, and that the repository is public." >&2
    exit 1
fi

# Print the reference in the exact form the catalogs pin, so it can be pasted
# without editing. The original registry spelling is preserved: the catalogs
# say docker.io, and rewriting them to registry-1.docker.io would be churn.
printed_host=${ref%%/*}
if [[ $ref != */* || ( $printed_host != *.* && $printed_host != *:* && $printed_host != localhost ) ]]; then
    printed="docker.io/${repo}"
else
    printed="${printed_host}/${repo}"
fi

echo "${printed}@${digest}"
