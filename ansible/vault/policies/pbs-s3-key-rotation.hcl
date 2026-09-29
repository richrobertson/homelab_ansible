# Vault "pbs-s3-key-rotation" ACL policy.
#
# Unattended rotation of the PBS S3 (AWS) backup credential at 03:30. Mints short-lived AWS creds via the aws secrets engine rather than holding a long-lived key.
#
# Recovered from the live Vault and committed 2026-09-29, following the
# admin.hcl convention in this directory. It existed ONLY in the running
# server: the playbook that creates its AppRole references the policy by name
# but does not carry the policy body, so a rebuild would not have reproduced
# it.
#
# Apply with:
#   vault policy write pbs-s3-key-rotation ansible/vault/policies/pbs-s3-key-rotation.hcl

path "aws/creds/pbs-key-rotator" {
  capabilities = ["read"]
}

path "sys/leases/revoke/*" {
  capabilities = ["update"]
}

path "sys/leases/revoke" {
  capabilities = ["update"]
}

path "secret/data/aws/pbs-backup/credentials" {
  capabilities = ["create", "update", "read"]
}

path "secret/metadata/aws/pbs-backup/credentials" {
  capabilities = ["create", "update", "read"]
}
