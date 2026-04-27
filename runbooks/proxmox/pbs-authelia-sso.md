# PBS Authelia SSO

## Goal

Allow Proxmox Backup Server management UI logins through Authelia OIDC only for Authelia/AD `proxAdmins`.

PBS 4.1 supports OpenID Connect realms, but it does not currently support OIDC group claim mapping for ACLs. The access model is therefore:

- Authelia enforces that only `proxAdmins` can complete the `pbs_prod` OIDC authorization flow.
- PBS auto-creates OIDC users from the `preferred_username` claim.
- Ansible pre-creates and grants `Admin` to `*@authelia` users derived from the existing PBS AD realm users in `myrobertson.net`, which is already filtered to `proxAdmins`.

## Authelia

- Client ID: `pbs_prod`
- Client name: `Proxmox Backup Server`
- Secret key in the Kubernetes Secret: `pbs_oidc`
- Allowed subject: `group:proxAdmins`
- Redirect URIs:
  - `https://pbs.myrobertson.net:8007`
  - `https://pbs.myrobertson.net:8007/`

## Vault

- `secret/proxmox/pbs/prod/sso/authelia`: plaintext OIDC client secret used by PBS.
- `secret/authelia/prod`: hashed OIDC client secret consumed by the Authelia HelmRelease through Vault Secrets Operator.

## Apply

Run from `homelab_ansible` with Vault credentials exported:

```sh
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/configure_pbs_authelia_sso.yml
```

Then reconcile the Flux `apps` kustomization from `homelab_flux`.

## Validate

On PBS:

```sh
proxmox-backup-manager openid show authelia
proxmox-backup-manager acl list
proxmox-backup-manager user list
```

Expected:

- OIDC realm: `authelia`
- OIDC client: `pbs_prod`
- OIDC username claim: `preferred_username`
- `Admin` ACL on `/` for each configured `*@authelia` proxAdmins user.

## Notes

PBS ACLs are user-based for this OIDC flow because PBS does not support OIDC group claim mapping. Re-run the playbook after adding a new `proxAdmins` user who should administer PBS.
