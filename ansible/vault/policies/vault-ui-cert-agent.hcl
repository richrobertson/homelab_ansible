# Vault "vault-ui-cert-agent" ACL policy.
#
# The vault-agent that renews the Vault web UI certificate.
#
# Recovered from the live Vault and committed 2026-09-29, following the
# admin.hcl convention in this directory. It existed ONLY in the running
# server: the playbook that creates its AppRole references the policy by name
# but does not carry the policy body, so a rebuild would not have reproduced
# it.
#
# Apply with:
#   vault policy write vault-ui-cert-agent ansible/vault/policies/vault-ui-cert-agent.hcl

path "pki_int/issue/learn" {
  capabilities = ["update"]
}
