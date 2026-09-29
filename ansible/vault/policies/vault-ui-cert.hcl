# Vault "vault-ui-cert" ACL policy.
#
# Vault's own web UI certificate. Renewing this is what silently served an expired cert for four months - see the vault host runbook.
#
# Recovered from the live Vault and committed 2026-09-29, following the
# admin.hcl convention in this directory. It existed ONLY in the running
# server: the playbook that creates its AppRole references the policy by name
# but does not carry the policy body, so a rebuild would not have reproduced
# it.
#
# Apply with:
#   vault policy write vault-ui-cert ansible/vault/policies/vault-ui-cert.hcl

path "pki_int/issue/vault-webui-myrobertson-net" {
  capabilities = ["update"]
}
path "pki_int/cert/ca" {
  capabilities = ["read"]
}
