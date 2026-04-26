# Proxmox Authelia SSO

## Goal

Allow Proxmox VE management UI logins through Authelia OIDC only for Authelia/AD `proxAdmins`.

Break-glass access remains `root@pam`.

## Authelia

Production Authelia defines the OIDC client in:

- `homelab_flux/apps/prod/authelia/authelia-values.yaml`

Client settings:

- Client ID: `proxmox_prod`
- Client name: `Proxmox VE cl0`
- Authorization policy: `proxmox_domain_admins`
- Allowed subject: `group:proxAdmins`
- Required policy: `two_factor`
- Proxmox RBAC claim: `proxmox_groups`
- Proxmox admin claim value: `proxAdmins`
- Scopes: `openid profile email groups proxmox_groups`
- Redirect URIs:
  - `https://cl0.myrobertson.net:8006`
  - `https://cl0.myrobertson.net:8006/`
  - `https://pve3.myrobertson.net:8006`
  - `https://pve3.myrobertson.net:8006/`
  - `https://pve4.myrobertson.net:8006`
  - `https://pve4.myrobertson.net:8006/`
  - `https://pve5.myrobertson.net:8006`
  - `https://pve5.myrobertson.net:8006/`

The committed Authelia value reads the PBKDF2 client-secret hash from a Vault-synced file. The plaintext client secret is stored separately for Proxmox.

Vault paths managed by Ansible:

- `secret/proxmox/cl0/sso/authelia`: plaintext OIDC client secret used by Proxmox.
- `secret/authelia/prod`: hashed OIDC client secret consumed by Authelia through `VaultStaticSecret`.

## Configure With Ansible

From `homelab_ansible`:

```sh
export VAULT_ADDR=https://vault.myrobertson.net:8200
export VAULT_TOKEN=<token with read/write access to the paths above>
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/configure_authelia_sso.yml
```

Rotate the OIDC client secret and update both Vault paths plus Proxmox:

```sh
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/configure_authelia_sso.yml \
  -e proxmox_sso_rotate_secret=true
```

After the playbook writes Vault, reconcile Flux so Authelia receives the new secret file and rendered config.

## Apply Authelia Via Flux

```sh
flux reconcile kustomization apps --with-source --context admin@prod
kubectl --context admin@prod -n default rollout status deploy/authelia
```

Verify OIDC discovery is reachable:

```sh
curl -fsS https://auth.myrobertson.com/.well-known/openid-configuration | jq '.issuer'
```

Expected issuer:

```text
https://auth.myrobertson.com
```

## Proxmox Realm

The Ansible playbook configures the Proxmox realm automatically. The equivalent UI configuration is:

1. Go to `Datacenter -> Permissions -> Realms`.
2. Add an `OpenID Connect Server`.
3. Configure:

```text
Realm: authelia
Issuer URL: https://auth.myrobertson.com
Client ID: proxmox_prod
Client Key: <from secret/proxmox/cl0/sso/authelia client_secret>
Username Claim: Default (subject)
Scopes: openid email profile groups proxmox_groups
Query UserInfo: enabled
Autocreate Users: enabled
Autocreate Groups: enabled
Groups Claim: proxmox_groups
```

CLI equivalent:

```sh
CLIENT_SECRET="$(vault kv get -field=client_secret secret/proxmox/cl0/sso/authelia)"
pveum realm add authelia \
  --type openid \
  --issuer-url https://auth.myrobertson.com \
  --client-id proxmox_prod \
  --client-key "$CLIENT_SECRET" \
  --username-claim subject \
  --scopes 'openid email profile groups proxmox_groups' \
  --query-userinfo 1 \
  --autocreate 1 \
  --groups-autocreate 1 \
  --groups-claim proxmox_groups
```

## Permissions

The Ansible playbook pre-creates the mapped admin group and grants it `Administrator`. After the first successful `proxAdmins` login, Proxmox should create:

- user: `<subject-uuid>@authelia`
- group: `proxAdmins-authelia`

Grant the group administrator access:

```sh
pveum aclmod / -group 'proxAdmins-authelia' -role Administrator
```

If the group name differs, inspect the created groups before granting:

```sh
pveum group list
pveum user list
```

## Local Account Lockdown

Authelia limits the `authelia` realm, but it does not disable existing `pam` or `pve` realm users. Audit local users and disable anything that should not remain as break-glass access:

```sh
pveum user list
pveum user modify '<user>@pve' --enable 0
```

Keep `root@pam` available until SSO has been validated from more than one browser/session.

## Validation

1. Log out of Proxmox.
2. Select realm `authelia`.
3. Confirm a `proxAdmins` member can log in and receives administrator access.
4. Confirm a non-`proxAdmins` AD user is denied by Authelia before Proxmox creates a usable session.
5. Confirm `root@pam` still works.

## Rollback

Disable or remove the realm without touching `pam`:

```sh
pveum realm delete authelia
```

Then remove or disable the `proxmox_prod` client from the Authelia production overlay and reconcile Flux.
