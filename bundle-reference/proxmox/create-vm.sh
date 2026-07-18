#!/usr/bin/env bash
# create-vm.sh — create one Rocky 10 service VM on Proxmox from the verified
# GenericCloud image + cloud-init snippet. Run on the PVE host as root.
#
#   ./create-vm.sh vms/svc-media.env
#   ./create-vm.sh vms/svc-download.env
#
# Refuses to touch an existing VMID (no silent clobbering). Rollback of a
# fresh VM is one command:  qm stop <id>; qm destroy <id> --purge
set -euo pipefail
cd "$(dirname "$0")"

[[ $# -eq 1 && -f "$1" ]] || { echo "usage: $0 vms/<name>.env" >&2; exit 1; }
source ./provision.env
source "$1"

IMG="${IMAGE_DIR}/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2"

# ---------- preflight ----------
[[ -f "${IMG}" && -f "${IMG}.sha256" ]] \
    || { echo "FATAL: run ./fetch-image.sh first" >&2; exit 1; }
sha256sum -c "${IMG}.sha256" >/dev/null \
    || { echo "FATAL: image fails checksum vs fetch-time record" >&2; exit 1; }

qm status "${VMID}" &>/dev/null \
    && { echo "FATAL: VMID ${VMID} already exists — refusing" >&2; exit 1; }

pvesm status --storage "${STORAGE}" >/dev/null
grep -qE "^\s*content.*snippets" "/etc/pve/storage.cfg" \
    || echo "WARN: verify '${SNIPPET_STORAGE}' has 'snippets' content enabled (pvesm set ${SNIPPET_STORAGE} --content <existing>,snippets)"

if [[ -n "${VLAN_TAG}" ]]; then
    grep -qE "bridge-vlan-aware\s+yes" /etc/network/interfaces \
        || { echo "FATAL: ${BRIDGE} is not VLAN-aware; add 'bridge-vlan-aware yes' + 'bridge-vids 2-4094' and ifreload -a" >&2; exit 1; }
fi

# EL10 userspace is built for x86-64-v3. PVE's default guest CPU
# (x86-64-v2-AES) will NOT boot it. 'host' on the 3700X (Zen 2) qualifies.
CPU_TYPE=host

ADMIN_SSH_PUBKEY=$(cat "${ADMIN_SSH_PUBKEY_FILE}")
NAMESERVER="${!NAMESERVER_VAR}"

# ---------- render cloud-init snippet ----------
EXTRA_PACKAGES_YAML=""
for p in ${EXTRA_PACKAGES:-}; do EXTRA_PACKAGES_YAML+="  - ${p}"$'\n'; done
export NAME SEARCHDOMAIN TIMEZONE ADMIN_USER ADMIN_SSH_PUBKEY EXTRA_PACKAGES_YAML
mkdir -p "${SNIPPET_DIR}"
envsubst '${NAME} ${SEARCHDOMAIN} ${TIMEZONE} ${ADMIN_USER} ${ADMIN_SSH_PUBKEY} ${EXTRA_PACKAGES_YAML}' \
    < user-data.tmpl.yaml > "${SNIPPET_DIR}/${NAME}-user.yaml"

# ---------- create ----------
qm create "${VMID}" \
    --name "${NAME}" \
    --ostype l26 \
    --machine q35 \
    --bios ovmf \
    --cpu "${CPU_TYPE}" \
    --cores "${CORES}" \
    --memory "${MEMORY}" --balloon 0 \
    --scsihw virtio-scsi-single \
    --agent enabled=1,fstrim_cloned_disks=1 \
    --net0 "virtio,bridge=${BRIDGE}${VLAN_TAG:+,tag=${VLAN_TAG}}" \
    --serial0 socket --vga serial0

# OVMF vars disk; pre-enrolled-keys=0 => Secure Boot off (avoids first-boot
# SB friction; flip later if you want to enroll and enforce).
qm set "${VMID}" --efidisk0 "${STORAGE}:1,efitype=4m,pre-enrolled-keys=0"

# Import the cloud image as scsi0 (PVE 7.2+ import-from), then grow it.
qm set "${VMID}" --scsi0 "${STORAGE}:0,import-from=${IMG},discard=on,iothread=1"
qm set "${VMID}" --boot order=scsi0
qm disk resize "${VMID}" scsi0 "${DISK_SIZE}"

# Cloud-init: network via PVE-managed NoCloud config; user-data via snippet.
qm set "${VMID}" --ide2 "${STORAGE}:cloudinit"
qm set "${VMID}" --ipconfig0 "ip=${IP_CIDR},gw=${GATEWAY}"
qm set "${VMID}" --nameserver "${NAMESERVER}" --searchdomain "${SEARCHDOMAIN}"
qm set "${VMID}" --cicustom "user=${SNIPPET_STORAGE}:snippets/${NAME}-user.yaml"
qm cloudinit update "${VMID}"

qm set "${VMID}" --onboot 1 --startup "${STARTUP}"

qm start "${VMID}"
echo "== ${NAME} (${VMID}) started. Watch first boot: qm terminal ${VMID}"
echo "== Validate:"
echo "   qm agent ${VMID} ping                       # guest agent up"
echo "   ssh ${ADMIN_USER}@${IP_CIDR%%/*} cloud-init status --wait --long"
