# Security

The security model is fail-closed: a failed Proxmox query does not authorize VM
creation, a failed VPN invariant stops downloads, a missing NFS mount blocks
consumers, and a failed host verification prevents a green deployment result.

## Trust boundaries

- The control node is highly trusted. Its SSH key can reach the guests and the
  explicit root PVE SSH target; the latter can bypass API-token scoping.
- The Proxmox API token is privilege-separated and scoped to the service pool,
  configured storages, bridge use, and the minimum serial permission.
- `svc-download` is treated as hostile application space. Its containers use a
  dedicated WireGuard namespace behind a host nftables output backstop.
- `svc-media` applications are rootless. Host listeners are LAN-scoped and the
  normal entry point is Caddy.
- TrueNAS owns NFS authorization and on-disk ownership. Guest root squash is
  expected and must not be worked around.
- The Caddy internal CA establishes private service identity. The Proxmox CA
  independently establishes API identity; trusting one does not trust the
  other.

## Secret handling

Secrets belong only in the encrypted
`inventory/group_vars/all/vault.yml`, application-owned configuration stores,
or root-owned mode-`0600` environment files. The vault password file is local,
mode `0600`, and ignored by Git. ntfy credentials are loaded through systemd
`EnvironmentFile=`; they are not embedded in executable scripts.

Every Ansible task that handles a token, password, private key, rendered secret
file, or secret command uses both `no_log: true` and `diff: false`. This applies
to normal, verbose, check, and diff runs. `make check-diff` is useful only
because secret-bearing resources remain suppressed.

Avoid these common disclosure paths:

- `ansible-inventory --list` against the real inventory;
- debugging `hostvars`, registered secret results, or template contents;
- `ansible-vault view` in a captured terminal;
- copying verbose output into tickets, chats, or session transcripts;
- placing provider keys in Homepage widgets or unencrypted group vars.

Use `make preflight`, which authenticates without serializing decrypted
inventory. Edit vault data with `make vault-edit`.

## Credential exposure response

A repository review command serialized decrypted host variables into an
execution transcript. Treat every populated vault value as exposed even if the
transcript was later deleted or described as redacted. Rotation must happen
before a live rollout.

1. Preserve only the minimum incident metadata; restrict transcript access and
   remove working-tree copies. Deletion does not substitute for rotation.
2. Open the vault in the editor, inventory all populated secrets without
   printing them, and identify the issuing system and revocation path for each.
3. Rotate the PVE API token secret. Prefer creating a new scoped token, update
   the vault, pass preflight, and then revoke the old token.
4. Generate a new Mullvad WireGuard configuration/private key, update all
   associated endpoint/address values, deploy `svc-download`, pass safe and
   disruptive verification, and revoke the old key where supported.
5. Rotate the ntfy access token and every populated RomM metadata-provider key.
   Rotate other provider/application credentials if they were present in the
   decrypted data.
6. Rotate RomM database user/root passwords in MariaDB during a maintenance
   window, then update both application and database environment values in the
   vault. Merely changing bootstrap variables does not update an initialized
   database. Rotating the RomM auth secret logs out existing sessions.
7. Rekey the Ansible vault and replace every local `.vault_pass` copy if the
   passphrase itself may have been observed.
8. Run `make validate`, `make preflight`, targeted deployment, and verification.
   Confirm old credentials fail only after the new ones are proven.
9. Scan the full Git history with the approved secret scanner. Rewrite shared
   history only when a verified secret exists there, coordinate the forced
   update, and rotate again if a secret remained valid during distribution.

Record issuer-side revocation timestamps and validation results, never secret
values. See [Secret exposure](incidents.md#secret-exposure) for incident exit
criteria.

## Proxmox and provisioning

Keep `pve_validate_certs: true` and configure CA trust as described in
[Deployment](deployment.md#trust-the-proxmox-api-certificate). TLS or API
authentication failures must abort; they must never be converted into “VM not
found.” The SSH fallback check is read-only (`qm status`) and uses only the
explicit `pve_ssh_host`.

Existing VMs must match inventory name, pool, bridge, boot disk, cloud-init
disk, and storage. A storage mismatch requires a separate, backed-up
maintenance procedure. The role may resume incomplete EFI/import/boot/resize
steps, but it never destroys, recreates, moves, or shrinks an existing disk.

## Supply chain

- Container images are pinned to reviewed immutable digests in the relevant
  catalog. Updating means reviewing the release, changing the digest,
  deploying one VM at a time, testing, and recording rollback digests.
- The Rocky image is pinned to a dated filename and SHA-256 committed in Git.
  Verify the actual image on every use; a stamp file is not evidence that its
  current bytes are valid.
- Dependency versions are committed. Upgrade Ansible, proxmoxer, or collections
  as a tested set, not opportunistically on a deployment workstation.

## Download egress backstop

The strict nftables backstop is a defense independent of container namespace
configuration. The maintenance-egress helper may open temporary package/image
egress only after confirming that strict policy exists. Its cleanup path must
always restore the backstop; failure to close the window is fatal.

Never loosen the permanent output policy to solve a package or image-pull
problem. During planned maintenance, pause the canary, keep console access
available, apply the narrow change, restore policy, then run safe and
disruptive verification. Emergency backstop recovery is documented in
[Incidents](incidents.md#download-vm-ssh-lockout).

## Certificate material

CA certificates are public, but they change who the machine trusts. Obtain the
PVE CA through an authenticated administrative path and compare its fingerprint
out of band. Obtain the Caddy root from the deployed host through the Ansible
access task and install it only on clients intended to trust private homelab
services. Never commit environment-specific roots or private keys.
