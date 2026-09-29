# Vault "default" ACL policy.
#
# `default` is attached to EVERY token Vault issues, so its contents are the
# floor of what every workload, AppRole and LDAP user can do. Recovered from the
# live server and committed 2026-09-29 for the same reason as admin.hcl: it
# existed ONLY in the running Vault, so a rebuild would not have reproduced it
# and a future widening would have left no trace in git.
#
# Apply with:
#   vault policy write default ansible/vault/policies/default.hcl
#
# NARROWED 2026-09-29. It previously also granted:
#
#   path "secret/*"             create read update delete list sudo
#   path "kubernetes/*"         create read update delete list sudo
#   path "staging-kubernetes/*" create read update delete list sudo
#
# meaning every pod in both clusters, and every LDAP user, could read, write and
# delete every KV secret. That is why admin credentials - the Backblaze master
# key, the GitOps deploy key - live in the separate `ops` mount rather than under
# secret/. Verified before removal that nothing depended on it: every auth role's
# own policy already grants what it reads. A bare `kubernetes/` mount does not
# even exist (only prod-kubernetes/ and staging-kubernetes/), so that stanza was
# dead. What remains is stock Vault default.
#
# Do NOT re-add a blanket secret/ grant here. Grant the specific path in the
# consuming policy instead.

path "auth/token/lookup-self" {
    capabilities = ["read"]
}

path "auth/token/renew-self" {
    capabilities = ["update"]
}

path "auth/token/revoke-self" {
    capabilities = ["update"]
}

path "sys/capabilities-self" {
    capabilities = ["update"]
}

path "identity/entity/id/{{identity.entity.id}}" {
  capabilities = ["read"]
}
path "identity/entity/name/{{identity.entity.name}}" {
  capabilities = ["read"]
}


path "sys/internal/ui/resultant-acl" {
    capabilities = ["read"]
}

path "sys/renew" {
    capabilities = ["update"]
}
path "sys/leases/renew" {
    capabilities = ["update"]
}

path "sys/leases/lookup" {
    capabilities = ["update"]
}

path "cubbyhole/*" {
    capabilities = ["create", "read", "update", "delete", "list"]
}

path "sys/wrapping/wrap" {
    capabilities = ["update"]
}

path "sys/wrapping/lookup" {
    capabilities = ["update"]
}

path "sys/wrapping/unwrap" {
    capabilities = ["update"]
}

path "sys/tools/hash" {
    capabilities = ["update"]
}
path "sys/tools/hash/*" {
    capabilities = ["update"]
}

path "sys/control-group/request" {
    capabilities = ["update"]
}

path "identity/oidc/provider/+/authorize" {
    capabilities = ["read", "update"]
}
