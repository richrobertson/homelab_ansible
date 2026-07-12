# Synology disaster recovery: Kermit and Scooter

## Scope

This runbook covers loss or corruption of Kermit (`192.168.1.141`) and Scooter
(`192.168.1.215`), shared-folder snapshots, Snapshot Replication, Hyper Backup,
Active Backup for Business (ABB), and NAS replacement. Kubernetes and
application cutovers remain governed by the master plan in `homelab_flux`.

## Current protection topology

- Kermit and Scooter both run Snapshot Replication and Replication Service.
- Shared-folder replication exists in both directions; determine the source of
  authority per share before promotion.
- Scooter protects the production and staging Nextcloud data shares with local
  snapshots, replication to Kermit, and Hyper Backup for production data to B2.
- Kermit runs ABB. Current Windows recovery points include the domain, DNS/DHCP,
  and member-server systems recorded in ABB; verify current task results instead
  of relying on static task IDs.
- NAS logs and structured backup-health events are forwarded to Loki.

Credentials are read from Vault only:

- NAS break-glass SSH: `secret/synology/dsm-admin/local-ssh-account`
- ABB enrollment account: `secret/synology/dsm-admin/ad-service-account`
- Windows automation account: `secret/windows/domain/ldap`
- Hyper Backup/B2 credentials remain on DSM and in their approved Vault path.

## Recovery objectives and authority

Use the latest healthy local snapshot for accidental deletion, the peer NAS for
loss of a source volume or NAS, and Hyper Backup/B2 for site-level loss or when
both peers contain the same corruption. Never promote both sides read/write.
Record the share, source NAS, destination NAS, snapshot timestamp, replication
direction, and accepted recovery point before changing state.

## 1. Capture current state

Load short-lived SSH credentials without printing them:

```bash
export VAULT_ADDR=https://vault.myrobertson.net:8200
secret_json="$(vault read -format=json secret/data/synology/dsm-admin/local-ssh-account)"
extra_file="$(mktemp)"
chmod 600 "$extra_file"
trap 'rm -f "$extra_file"' EXIT
printf '%s' "$secret_json" | jq \
  '{ansible_user:.data.data.username,ansible_password:.data.data.password,ansible_become_password:.data.data.password}' \
  > "$extra_file"
unset secret_json

OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ANSIBLE_FORKS=1 \
  .venv/bin/ansible-playbook -i inventory/environments/production.ini \
  ansible/synology/audit_snapshot_replication.yml --extra-vars "@$extra_file"
```

Also record:

```bash
kubectl --context admin@prod get pv
kubectl --context admin@prod get pvc -A
kubectl --context admin@prod get replicationsource -A
```

In DSM, capture Storage Manager health, current replication role, last
successful replication, snapshot counts, Hyper Backup status, and ABB task
results. Do not delete failed tasks or snapshots during evidence collection.

## 2. Restore a deleted file or directory

1. Stop or quiesce the application that writes the affected path.
2. In Snapshot Replication, browse snapshots for the authoritative share.
3. Prefer restoring to a temporary path or clone.
4. Validate ownership, ACLs, checksums, and application readability.
5. Copy the selected data into place only after owner approval.
6. Resume writers and verify a new snapshot and backup complete.

Do not roll back an entire shared folder when a file-level restore is sufficient.

## 3. Promote a replicated share after source failure

1. Confirm the original source cannot still accept writes. Fence it by network,
   service shutdown, or power-off if necessary.
2. Record the last successful replication timestamp and expected data loss.
3. On the destination NAS, use Snapshot Replication to perform the supported
   failover/promotion for the affected share.
4. Validate the promoted share locally before changing clients.
5. Update the consuming endpoint through its source of truth:
   - Kubernetes NFS server/path changes go through `homelab_flux`.
   - Windows mappings and service dependencies go through Ansible/GPO.
   - DNS changes use the authoritative DNS workflow.
6. Confirm read/write behavior and start a new protection chain.

For failback, quiesce writers again, replicate changes back to the repaired NAS,
verify convergence, then perform the supported switchover. Never reverse roles
while both copies are writable.

## 4. Restore with Hyper Backup

Use Hyper Backup when the peer replica is unavailable, corrupted, or later than
the desired recovery point.

1. Confirm B2 reachability and the destination identifier (`scooter.hbk` where
   applicable).
2. Browse versions and select a timestamp before the incident.
3. Restore to an alternate shared folder first.
4. Validate file counts, ACLs, representative checksums, and application data.
5. Cut over only after validation; retain the damaged/original share.

Do not rotate B2 keys, delete repository data, or run retention cleanup until
the restore is accepted.

## 5. Recover ABB data or a Windows machine

### File-level recovery

Use Active Backup for Business Portal to select the device and recovery point,
then restore to an alternate path or download first. Validate ACLs and content
before overwriting live files.

### Bare-metal recovery

1. Confirm the most recent successful ABB result and recovery timestamp.
2. Create boot media matching the installed ABB version.
3. Isolate the replacement machine from production if restoring a domain
   controller or a host with a conflicting identity.
4. Restore disks and boot once in the isolated network.
5. Follow the Windows domain runbook before reconnecting any domain controller.
6. Verify the ABB agent reconnects and completes a new backup.

Deleting PC, physical-server, NAS, or VM tasks normally deletes their backed-up
data. File-server task deletion removes the task/settings but retains copied
data according to Synology's File Server behavior. Always confirm the task type
and DSM confirmation dialog; never remove ABB database rows manually.

## 6. Replace a failed NAS

1. Preserve failed disks and do not initialize them unless the incident lead
   approves destructive recovery.
2. Install the replacement NAS and DSM at a compatible or newer patch level.
3. Recreate the storage pool only when disk migration/recovery is not possible.
4. Restore local administrator access, network identity, time, DNS, and Vault
   connectivity.
5. Install Snapshot Replication, Replication Service, Hyper Backup, and ABB only
   where required.
6. Restore shares from the healthy peer or Hyper Backup into new targets.
7. Reapply configuration with the playbooks under `ansible/synology/`:

```bash
.venv/bin/ansible-playbook -i inventory/environments/production.ini \
  ansible/synology/configure_snapshot_retention.yml --extra-vars "@$extra_file"
.venv/bin/ansible-playbook -i inventory/environments/production.ini \
  ansible/synology/configure_grafana_log_forwarding.yml --extra-vars "@$extra_file"
```

Run the targeted Nextcloud protection, notification, certificate, SSO, and ABB
playbooks only after their prerequisites are restored. Re-enroll Windows ABB
agents with `configure_windows_activebackup_agent.yml` if the replacement ABB
server has a new identity.

## 7. Validation and closure

- Storage pools and all RAID members are healthy.
- Required shares, ACLs, NFS permissions, and quotas match the source of truth.
- Replication direction and schedules are correct and a new sync succeeds.
- Hyper Backup repository browsing and a test restore succeed.
- ABB reports fresh successful recovery points for protected devices.
- Kubernetes consumers mount the intended NAS and pass read/write tests.
- Grafana receives current Kermit/Scooter health events.
- Original data and snapshots remain retained until owner acceptance.
