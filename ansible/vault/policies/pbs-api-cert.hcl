# Vault "pbs-api-cert" ACL policy.
#
# PBS API certificate issuance. Used by the vault-agent on pbs.myrobertson.net to renew the Proxmox Backup Server API cert.
#
# Recovered from the live Vault and committed 2026-09-29, following the
# admin.hcl convention in this directory. It existed ONLY in the running
# server: the playbook that creates its AppRole references the policy by name
# but does not carry the policy body, so a rebuild would not have reproduced
# it.
#
# Apply with:
#   vault policy write pbs-api-cert ansible/vault/policies/pbs-api-cert.hcl

path "pki_int/issue/pbs-api" {
  capabilities = ["update"]
}
path "pki_int/cert/ca" {
  capabilities = ["read"]
}
