# Vault "flux-promoter" ACL policy.
#
# Created 2026-09-29 for the immich/plex staging->prod image promoter CronJobs in
# the staging cluster (homelab_flux: infrastructure/promoter). Grants read on
# exactly one path: the GitHub deploy key the promoter uses to commit a promoted
# image tag to main.
#
# Apply with:
#   vault policy write flux-promoter ansible/vault/policies/flux-promoter.hcl
#
# Reached through a dedicated VaultAuth bound to the `flux-promoter` Kubernetes
# auth role on the staging-kubernetes mount, itself bound to one ServiceAccount
# (flux-promoter/flux-system). The shared VSO role cannot read it.
#
# NOTE: auth/staging-kubernetes has token_reviewer_jwt_set=false, so Vault runs
# the TokenReview with the CLIENT's own token - that ServiceAccount also needs
# system:auth-delegator or the login fails with a bare 403 "permission denied".

path "ops/data/github/flux-promoter" {
  capabilities = ["read"]
}
