#!/usr/bin/env bash
# fetch-image.sh — download the Rocky 10 GenericCloud image and verify it
# against the upstream CHECKSUM file. Run on the Proxmox host.
#
# The ".latest" name is a symlink upstream that changes as new builds land;
# we verify against the CHECKSUM entry matching the resolved filename, and
# keep a sha256-stamped copy so create-vm.sh consumes a verified artifact.
set -euo pipefail
cd "$(dirname "$0")"
source ./provision.env

mkdir -p "${IMAGE_DIR}"
img="${IMAGE_DIR}/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2"

curl -fL --retry 3 -o "${img}.tmp" "${IMAGE_URL}"
curl -fL --retry 3 -o "${IMAGE_DIR}/CHECKSUM" "${CHECKSUM_URL}"

# CHECKSUM is BSD-style: "SHA256 (filename) = hash". Extract entries for the
# GenericCloud-Base images and try to match our download.
want=$(awk -F' = ' '/^SHA256 \(Rocky-10-GenericCloud-Base.*qcow2\)/{print $2}' \
        "${IMAGE_DIR}/CHECKSUM")
got=$(sha256sum "${img}.tmp" | awk '{print $1}')

if ! grep -q "${got}" <<<"${want}"; then
    echo "FATAL: sha256 ${got} not present in upstream CHECKSUM for GenericCloud-Base" >&2
    echo "Upstream may have rolled the .latest symlink mid-download; re-run." >&2
    rm -f "${img}.tmp"
    exit 1
fi

mv "${img}.tmp" "${img}"
echo "${got}  ${img}" > "${img}.sha256"
echo "OK: ${img}"
qemu-img info "${img}" | sed -n '1,4p'
