# Vault "admin" ACL policy.
#
# Recovered from the live Vault on 2026-08-22 and committed because it existed
# ONLY in the running server -- it had been edited by hand at least once (see
# the sys/audit stanza, added 2026-08-03) with no copy in git, so a rebuild
# would not have reproduced it.
#
# Apply with:
#   vault policy write admin ansible/vault/policies/admin.hcl
#
# Changes from the live version at recovery time: "patch" added to the two
# stanzas that govern KV writes. Everything else is byte-identical.

path "*"
{
  capabilities = ["read", "list", "create", "update", "patch", "delete"] 
}


# Manage auth methods broadly across Vault
path "/auth/token/*"
{
  capabilities = ["read", "list", "create", "update", "delete"] 
}

path "/auth/jwt/*"
{
  capabilities = ["read", "list", "create", "update", "delete"] 
}

path "/auth/staging-kubernetes/*"
{
  capabilities = ["read", "list", "create", "update", "delete"] 
}

path "pki/*"
{
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}

path "pki_int/*"
{
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}


path "aws/*"
{
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}

# Create, update, and delete auth methods
path "sys/auth/*"
{
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}

path "sys/auth"
{
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}

# Create and manage ACL policies
path "sys/policies/acl/*"
{
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}

# To list policies
path "sys/policies/acl"
{
  capabilities = ["list"]
}

# List, create, update, and delete key/value secrets.
#
# "patch" matters more than it looks. Without it, `vault kv patch` and
# `vault kv metadata patch` fall back to (or fail into) a read-modify-write, and
# a bare `vault kv metadata put` REPLACES the whole custom_metadata map rather
# than merging. On 2026-08-22 that silently wiped rotated_at from two secrets
# while stamping unrelated retirement fields onto them, which tripped
# VaultSecretsMissingRotationMetadata. Granting patch makes the merge atomic and
# makes the non-destructive command the easy one to reach for.
path "secret/*"
{
  capabilities = ["create", "read", "update", "patch", "delete", "list", "sudo"]
}

# Create and manage secret engines broadly across Vault.
path "sys/mounts/*"
{
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}

path "/sys/remount"
{
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}

# Read health checks
path "sys/health"
{
  capabilities = ["read", "sudo"]
}

path "sys/capabilities"
{
  capabilities = ["create", "update"]
}

path "sys/capabilities-self"
{
  capabilities = ["create", "update"]
}

# Create and manage identities and groups
path "identity/*" { 
  capabilities = [ "create", "read", "update", "delete", "list" ]
}

# Audit device management. sys/audit is a root-protected endpoint: the broad
# path "*" rule above grants create/read/update/delete on it but NOT the `sudo`
# capability those endpoints additionally require, so every call returned
# "permission denied" while `vault token capabilities sys/audit` misleadingly
# reported access. Added 2026-08-03 so audit logging can be enabled and verified
# without a root token at the console.
path "sys/audit"
{
  capabilities = ["read", "list", "sudo"]
}

path "sys/audit/*"
{
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}
