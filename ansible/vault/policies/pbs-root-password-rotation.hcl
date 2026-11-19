# PBS root password rotation (ansible/proxmox/pbs_root_password_rotation.yml).
# Its own AppRole so a compromise of the B2 or cert credentials on the PBS host
# does not grant this, and vice versa: read + patch exactly one record.
path "secret/data/proxmox/pbs/prod/install" {
  capabilities = ["read", "patch"]
}
path "secret/metadata/proxmox/pbs/prod/install" {
  capabilities = ["read", "patch"]
}
