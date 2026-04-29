# Synology LUN Cleanup Runbook

This folder includes playbooks to discover and clean likely orphaned iSCSI LUNs on Synology NAS 192.168.1.215.
The playbooks target the inventory group `synology_nas` and use host vars from `inventory/environments/synology.ini`.

## Playbooks

- `preflight_nas_connectivity.yml`: Verifies SSH reachability, API auth, and LUN endpoint shape.
- `audit_snapshot_replication.yml`: Performs a read-only audit of Snapshot Replication package status, configured replica count, schedules, and local share snapshot config.
- `configure_synology_admin_ad_account.yml`: Creates or reuses a Vault-managed AD service account for DSM administration and prepares the AD group intended for Synology local administrators membership.
- `configure_snapshot_replication_ad_account.yml`: Creates or reuses a Vault-managed AD service account for Snapshot Replication and validates the scooter/kermit partner path before applying DSM-specific credential update commands.
- `discover_orphaned_luns.yml`: Lists likely orphaned LUN UUIDs using mapping heuristics.
- `cleanup_orphaned_luns.yml`: Deletes explicitly provided orphan LUN UUIDs.
- `discover_and_cleanup_orphaned_luns.yml`: One-shot flow for discovery + optional cleanup.

## DSM admin AD service account

This workflow prepares a dedicated AD account for Synology DSM SSH/API administration and stores the credential in Vault at:

- `secret/data/synology/dsm-admin/ad-service-account`

Default identity:

- AD user: `svc-syno-admin@myrobertson.net`
- AD group: `GG-Synology-DSM-Administrators`

Apply the Vault secret and AD account/group:

```bash
source ~/.bash_profile
.venv/bin/ansible-playbook ansible/synology/configure_synology_admin_ad_account.yml \
  -i inventory/environments/production.ini \
  --tags vault,ad
```

The AD step uses the inventory WinRM user, currently `ldap@myrobertson.net`, so provide its password through Ansible in the usual local-only way if it is not already available in your shell or inventory:

```bash
source ~/.bash_profile
.venv/bin/ansible-playbook ansible/synology/configure_synology_admin_ad_account.yml \
  -i inventory/environments/production.ini \
  --tags ad \
  -e 'ansible_password=...'
```

Once an existing DSM administrator SSH path works, grant the AD group local DSM administrators membership:

```bash
source ~/.bash_profile
.venv/bin/ansible-playbook ansible/synology/configure_synology_admin_ad_account.yml \
  -i inventory/environments/production.ini \
  --tags nas \
  -e synology_admin_apply_dsm_group=true
```

## Local DSM SSH account

Synology DSM may authenticate domain users while still refusing SSH shell startup. For Ansible and read-only `synowebapi` audits, use a dedicated local DSM administrator account and store its credential in Vault:

- `secret/data/synology/dsm-admin/local-ssh-account`

Suggested local DSM user:

- `svc-syno-ssh`

Create the Vault secret:

```bash
source ~/.bash_profile
vault kv put -mount=secret synology/dsm-admin/local-ssh-account \
  username=svc-syno-ssh \
  password="$(openssl rand -base64 36 | tr -d '=+/' | cut -c1-32)" \
  purpose="Synology local SSH administrator for Ansible automation"
```

Then create the same local user on scooter and kermit in DSM:

- Add `svc-syno-ssh` as a local user.
- Set the password from Vault.
- Add it to the local `administrators` group.
- Allow SSH/Terminal access.

Use it for ad hoc Ansible checks:

```bash
source ~/.bash_profile
SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
SYNO_PASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"
.venv/bin/ansible synology_snapshot_replication_nas \
  -i inventory/environments/production.ini \
  -e "ansible_user=${SYNO_USER}" \
  -e "ansible_password=${SYNO_PASS}" \
  -m raw \
  -a 'id; hostname'
```

Run the Snapshot Replication audit with the Vault-backed local SSH account:

```bash
source ~/.bash_profile
SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
SYNO_PASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"
.venv/bin/ansible-playbook ansible/synology/audit_snapshot_replication.yml \
  -i inventory/environments/production.ini \
  -e "ansible_user=${SYNO_USER}" \
  -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}"
```

Current review notes:

- Keep `SnapshotReplication` package status `running` on both scooter and kermit.
- Scooter currently points `/var/services/homes` at `/volume1/@fake_home_link`; the local SSH account works, but the missing home directory warning is expected until user homes are deliberately enabled or remapped.
- Kermit has one extra Vault-to-Vault replica config compared with scooter. Review it in DSM before deleting or demoting any replica.

Snapshot retention policy applied on 2026-04-28:

- Use the native DSM tool: `/usr/syno/bin/synoretentionconf --set-policy 'Share#' <share> retain_by_adv <hourly> <daily> <weekly> <monthly> <yearly> <retain_all_days> <minimum_snapshots>`.
- Critical/user/app/config shares use `retain_by_adv 0 30 12 12 2 7 30`: keep 30 daily, 12 weekly, 12 monthly, 2 yearly, all snapshots from the last 7 days, and at least 30 snapshots.
- The kermit `video` share uses `retain_by_adv 0 14 8 6 1 7 14`: a lighter media policy with 14 daily, 8 weekly, 6 monthly, 1 yearly, all snapshots from the last 7 days, and at least 14 snapshots.
- Existing media and backup repository policies that already retained many versions were left in place to avoid reducing protection without a separate storage-capacity review.

Shares updated:

- `kermit`: `docker`, `homes`, `Vault`, `Vault-1`, `web`, `web_packages`, `radarr`, `video`.
- `scooter`: `docker`, `homes-replicated`, `Vault`, `web`, `web_packages`, `radarr`, `System`, `janice`, `remotes`.

## Snapshot Replication AD service account

This workflow prepares a dedicated AD account for Synology Snapshot Replication and stores the credential in Vault at:

- `secret/data/synology/snapshot-replication/ad-service-account`

Default identity:

- AD user: `svc-syno-repl@myrobertson.net`
- AD group: `GG-Synology-Snapshot-Replication`

The playbook does not add the group to `Domain Admins` unless explicitly requested with `-e synology_replication_add_group_to_domain_admins=true`. Prefer granting the AD group DSM administrative rights on `scooter` and `kermit` directly, then use the service account for Snapshot Replication partner authentication.

Preview/check mode:

```bash
VAULT_ADDR=https://vault.myrobertson.net:8200 VAULT_TOKEN=... \
ansible-playbook ansible/synology/configure_snapshot_replication_ad_account.yml \
  -i inventory/environments/production.ini \
  --check
```

Apply the Vault secret and AD account/group:

```bash
VAULT_ADDR=https://vault.myrobertson.net:8200 VAULT_TOKEN=... \
ansible-playbook ansible/synology/configure_snapshot_replication_ad_account.yml \
  -i inventory/environments/production.ini
```

Rotate the Vault-managed password and reset the AD account password:

```bash
VAULT_ADDR=https://vault.myrobertson.net:8200 VAULT_TOKEN=... \
ansible-playbook ansible/synology/configure_snapshot_replication_ad_account.yml \
  -i inventory/environments/production.ini \
  -e synology_replication_rotate_password=true
```

The Synology credential application step is intentionally gated behind:

```bash
-e synology_replication_apply_partner_credentials=true
-e 'synology_replication_task_update_commands=[...]'
```

Run once without those flags to print the local Snapshot Replication API inventory from each NAS, then fill in the DSM-version-specific update command(s). This avoids blindly calling private package APIs that vary across DSM and Snapshot Replication package versions.

## 1) Preflight (always run first)

```bash
ansible-playbook ansible/synology/preflight_nas_connectivity.yml \
  -i inventory/environments/synology.ini
```

## 2) Discovery-only

```bash
ansible-playbook ansible/synology/discover_orphaned_luns.yml \
  -i inventory/environments/synology.ini
```

Optionally exclude known-safe names by regex pattern list:

```bash
ansible-playbook ansible/synology/discover_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e 'synology_lun_name_exclude_patterns=["prod","backup"]'
```

By default, discovery succeeds when no orphaned LUNs are found and reports an empty result.
Enable strict non-empty behavior if your automation requires it:

```bash
ansible-playbook ansible/synology/discover_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e synology_fail_when_none_found=true
```

## 3) Cleanup with explicit UUIDs

Dry-run preview:

```bash
ansible-playbook ansible/synology/cleanup_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e 'orphan_lun_uuids=["uuid-1","uuid-2"]'
```

Actual deletion:

```bash
ansible-playbook ansible/synology/cleanup_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e 'orphan_lun_uuids=["uuid-1","uuid-2"]' \
  -e perform_cleanup=true
```

## 4) One-shot discover + cleanup

Discovery and preview only:

```bash
ansible-playbook ansible/synology/discover_and_cleanup_orphaned_luns.yml \
  -i inventory/environments/synology.ini
```

The one-shot playbook also succeeds by default when no candidates are found.
To force a non-zero exit when discovery returns no orphan UUIDs:

```bash
ansible-playbook ansible/synology/discover_and_cleanup_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e synology_fail_when_none_found=true
```

Actual cleanup (requires explicit confirmation gate):

```bash
ansible-playbook ansible/synology/discover_and_cleanup_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e perform_cleanup=true \
  -e cleanup_confirmation=I_UNDERSTAND_DELETE_IS_DESTRUCTIVE
```

## Audit logging

When cleanup is enabled, deletion runs append a UTC record to:

- `ansible/synology/logs/lun_cleanup_audit.log`

Disable logging if needed:

```bash
-e synology_cleanup_audit_log_enabled=false
```

Override log file path:

```bash
-e synology_cleanup_audit_log_path=ansible/synology/logs/custom_audit.log
```

## Notes

- API command syntax can vary by DSM version. If needed, override:
  - `synology_remote_list_luns_cmd`
  - `synology_remote_delete_lun_cmd_template`
- Keep cleanup in preview mode first and confirm UUIDs in DSM/SAN Manager before deletion.
