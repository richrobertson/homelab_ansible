# Synology LUN Cleanup Runbook

This folder includes playbooks to discover and clean likely orphaned iSCSI LUNs on Synology NAS 192.168.1.215.
The playbooks target the inventory group `synology_nas` and use host vars from `inventory/environments/synology.ini`.

## Playbooks

- `preflight_nas_connectivity.yml`: Verifies SSH reachability, API auth, and LUN endpoint shape.
- `audit_snapshot_replication.yml`: Audits Snapshot Replication package status, configured replica count, schedules, local share snapshot config, retention policy coverage, required Nextcloud share snapshot counts, and DSM notification email settings. It can also apply the desired DSM notification email settings when explicitly enabled.
- `configure_authelia_sso.yml`: Seeds per-NAS Authelia OIDC client secrets in Vault and configures DSM OIDC SSO for scooter and kermit.
- `provision_nextcloud_nfs_share.yml`: Creates separate `nextcloud-data-stage` and `nextcloud-data-prod` Btrfs shared folders, keeps recycle bin disabled, verifies data checksumming is not disabled, and applies Kubernetes-worker-only NFS privileges.
- `configure_nextcloud_data_protection.yml`: Configures Scooter Snapshot Replication plans for `nextcloud-data-prod` and `nextcloud-data-stage` to Kermit, enforces snapshot retention, and manages the existing Hyper Backup schedule and retention for backing up `nextcloud-data-prod` to Backblaze B2.
- `disable_snapshot_replication_worm_lock.yml`: Repair playbook for selected legacy Snapshot Replication jobs that fail with permission errors when DSM attempts WORM/locked-snapshot replication. It preserves the daily schedule and disables only the replication policy's WORM lock flag before triggering a fresh sync.
- `discover_orphaned_luns.yml`: Lists likely orphaned LUN UUIDs using mapping heuristics.
- `cleanup_orphaned_luns.yml`: Deletes explicitly provided orphan LUN UUIDs.
- `discover_and_cleanup_orphaned_luns.yml`: One-shot flow for discovery + optional cleanup.

## Synology observation and notification email

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

Apply the DSM notification email configuration from the same audit/observation playbook:

```bash
source ~/.bash_profile
SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
SYNO_PASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"
SMTP_USER="$(vault kv get -field=username -mount=secret mailu/prod/accounts/nas-notifications)"
SMTP_PASS="$(vault kv get -field=password -mount=secret mailu/prod/accounts/nas-notifications)"
.venv/bin/ansible-playbook ansible/synology/audit_snapshot_replication.yml \
  -i inventory/environments/production.ini \
  -e "ansible_user=${SYNO_USER}" \
  -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}" \
  -e synology_notification_email_apply=true \
  -e "synology_notification_smtp_user=${SMTP_USER}" \
  -e "synology_notification_smtp_password=${SMTP_PASS}"
```

The notification sender account is `nas-notifications@myrobertson.net`, with its password stored at `secret/mailu/prod/accounts/nas-notifications`. The playbook defaults the SMTP endpoint to `mail.myrobertson.net:587` with DSM's SSL/TLS flag enabled and preserves the current notification recipients, `rich@myrobertson.com` and `roy@myrobertson.com`.

## Synology Authelia SSO

The Synology SSO playbook provisions per-NAS OIDC client secrets, stores the Authelia-compatible PBKDF2 hashes in `secret/authelia/prod`, and configures DSM to use `https://auth.myrobertson.com/.well-known/openid-configuration`.

```bash
source ~/.bash_profile
SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
SYNO_PASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"
.venv/bin/ansible-playbook ansible/synology/configure_authelia_sso.yml \
  -i inventory/environments/production.ini \
  -e "ansible_user=${SYNO_USER}" \
  -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}"
```

By default the playbook keeps DSM local login as the default and enables Authelia as an available OIDC sign-in path. The OIDC user claim is `preferred_username`, and local DSM users are allowed so the existing break-glass admin account remains usable.

## Nextcloud NFS shares

Provision the staging and production backend-only Nextcloud data shares on Scooter:

```bash
source ~/.bash_profile
SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
SYNO_PASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ANSIBLE_FORKS=1 .venv/bin/ansible-playbook \
  ansible/synology/provision_nextcloud_nfs_share.yml \
  -i inventory/environments/production.ini \
  --limit scooter.myrobertson.net \
  -e "ansible_user=${SYNO_USER}" \
  -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}"
```

The shares are intended for Kubernetes PV mounts only. Do not grant normal SMB
user access, and do not use the Synology recycle bin for Nextcloud data.

The Snapshot Replication audit checks that `nextcloud-data-stage` and
`nextcloud-data-prod` have local snapshot configuration, retention policy
coverage, and at least reports the current snapshot count.

Configure data protection for the Nextcloud shares on Scooter:

```bash
source ~/.bash_profile
SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
SYNO_PASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ANSIBLE_FORKS=1 .venv/bin/ansible-playbook \
  ansible/synology/configure_nextcloud_data_protection.yml \
  -i inventory/environments/production.ini \
  -e "ansible_user=${SYNO_USER}" \
  -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}"
```

The playbook creates Snapshot Replication plans from Scooter to Kermit for both
`nextcloud-data-prod` and `nextcloud-data-stage` when those plans are missing.
It also enforces the current Nextcloud retention profile: 24 hourly, 7 daily,
2 weekly, 1 monthly, 0 yearly, retain all snapshots for 1 day, and keep a
minimum of 5 snapshots.

The Hyper Backup task is expected to be created in DSM first, because the
Backblaze credential exchange belongs on Scooter/DSM and should not be stored in
Git. After the task exists, Ansible manages the backup schedule, integrity-check
schedule, and retention policy for the task named
`NextCloud Prod Backup Backblaze`.

The managed Hyper Backup target is:

- Source shared folder: `nextcloud-data-prod`
- S3 server: `s3.us-west-002.backblazeb2.com`
- Bucket: `nextcloud-data-prod`
- Directory/target id: `scooter.hbk`
- Backup schedule: daily at `22:30`
- Integrity check: Saturdays at `00:30`
- Retention: custom retention with a maximum of 256 versions

To reconcile only the Hyper Backup task without touching Snapshot Replication:

```bash
source ~/.bash_profile
SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
SYNO_PASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ANSIBLE_FORKS=1 .venv/bin/ansible-playbook \
  ansible/synology/configure_nextcloud_data_protection.yml \
  -i inventory/environments/production.ini \
  --limit scooter.myrobertson.net \
  --tags hyperbackup \
  -e "ansible_user=${SYNO_USER}" \
  -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}"
```

Backblaze credentials belong on Scooter/DSM and in Vault only. They must not be
made available to the Nextcloud pod or committed to Git. The playbook validates
that the managed task points at the expected Backblaze bucket and Nextcloud prod
share before it changes schedule or retention settings.

## Snapshot Replication WORM lock repair

Some legacy Snapshot Replication plans can fail with DSM permission errors when
their sync policy tries to replicate WORM/locked snapshots. The repair playbook
targets only the explicitly listed plans, disables `worm_lock_enable`, preserves
the existing daily midnight schedule, and starts a fresh sync.

```bash
source ~/.bash_profile
SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
SYNO_PASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ANSIBLE_FORKS=1 .venv/bin/ansible-playbook \
  ansible/synology/disable_snapshot_replication_worm_lock.yml \
  -i inventory/environments/production.ini \
  -e "ansible_user=${SYNO_USER}" \
  -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}"
```

This is a replication-policy repair, not a deletion workflow. It does not delete
snapshots or shared folders.

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
