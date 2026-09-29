# Vault "proxmox-pve-cert" ACL policy.
#
# Proxmox VE node certificate issuance. Used by the vault-agent-proxmox-pveproxy units on pve3/4/5.
#
# Recovered from the live Vault and committed 2026-09-29, following the
# admin.hcl convention in this directory. It existed ONLY in the running
# server: the playbook that creates its AppRole references the policy by name
# but does not carry the policy body, so a rebuild would not have reproduced
# it.
#
# Apply with:
#   vault policy write proxmox-pve-cert ansible/vault/policies/proxmox-pve-cert.hcl

path "pki_int/issue/proxmox-myrobertson-net" {
  capabilities = ["update"]
}
path "pki_int/cert/ca" {
  capabilities = ["read"]
}
