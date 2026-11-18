# Vault "backblaze-key-rotation" ACL policy.
#
# Created 2026-09-29 with the AppRole of the same name. Grants read on exactly
# one path: the scoped Backblaze B2 application key used to mint and revoke other
# B2 keys.
#
# Apply with:
#   vault policy write backblaze-key-rotation ansible/vault/policies/backblaze-key-rotation.hcl
#
# It lives in the `ops` KV mount, NOT under secret/, because until 2026-09-29 the
# `default` policy granted secret/* to every token - see default.hcl. `ops` is
# covered by no cluster-facing policy, so grant it one path at a time.
#
# Deliberately does NOT grant ops/data/backblaze/masterkey: the rotator holds 10
# B2 capabilities, the master holds 38 including deleteBuckets and
# bypassGovernance.

path "ops/data/backblaze/key-rotator" {
  capabilities = ["read"]
}
path "ops/metadata/backblaze/key-rotator" {
  capabilities = ["read"]
}

# Added 2026-11-17 for ansible/proxmox/roles/pbs_b2_key_rotation (the PBS host's
# pbs-rotate-b2-key.timer): record the rotated pbs-b2 key - and only that.
path "secret/data/proxmox/pbs/prod/backblaze-b2" {
  capabilities = ["read", "create", "update", "patch"]
}
path "secret/metadata/proxmox/pbs/prod/backblaze-b2" {
  capabilities = ["read", "patch"]
}
