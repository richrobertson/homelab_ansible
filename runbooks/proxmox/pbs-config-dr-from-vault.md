# PBS Configuration DR From Vault

This runbook captures enough Proxmox Backup Server configuration to recreate a PBS VM from Git plus Vault.

Git stores the workflow and expected shape. Vault stores the sensitive configuration archive.

## What Goes Into Vault

The export playbook stores a base64-encoded tarball at:

```text
secret/proxmox/pbs/prod/config
```

The archive includes existing paths from:

- `/etc/proxmox-backup`
- `/etc/apt/sources.list`
- `/etc/apt/sources.list.d`
- `/etc/network/interfaces`
- `/etc/hosts`
- `/etc/hostname`
- `/etc/systemd/timesyncd.conf`
- `/etc/chrony`

`/etc/proxmox-backup` may contain datastore, user, ACL, remote, sync, verify, notification, certificate, and S3 endpoint configuration. Treat the Vault path as highly sensitive.

## What Stays In Git

- PBS VM build/import procedure: [PBS Scooter VM to Proxmox Thunderbolt migration](pbs-scooter-to-proxmox-thunderbolt-migration.md)
- Export playbook: `ansible/proxmox/pbs_config_export_to_vault.yml`
- Restore playbook: `ansible/proxmox/pbs_config_restore_from_vault.yml`
- Thunderbolt proxy playbook: `ansible/proxmox/pbs_thunderbolt_proxy.yml`

## Export Current PBS Configuration

Run from the Ansible control host after sourcing the standard shell profile:

```sh
source ~/.bash_profile
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/pbs_config_export_to_vault.yml \
  -e pbs_config_source_hosts=pbs.myrobertson.net
```

If the current PBS VM is not reachable by SSH inventory name yet, add a temporary inventory entry for it or pass an inventory file containing:

```ini
[pbs]
pbs.myrobertson.net ansible_host=192.168.1.217 ansible_user=root
```

Verify the Vault item exists without printing the archive:

```sh
vault kv get -field=exported_at secret/proxmox/pbs/prod/config
vault kv get -field=source_host secret/proxmox/pbs/prod/config
vault kv get -field=pbs_version secret/proxmox/pbs/prod/config
```

## Stage Restore On A Replacement PBS VM

Build a fresh PBS VM first, then stage the config without applying it:

```sh
source ~/.bash_profile
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/pbs_config_restore_from_vault.yml \
  -e pbs_config_restore_hosts=pbs-restore.myrobertson.net
```

This writes the archive under `/root/pbs-config-restore` and prints the file list.

## Apply Restore

Only apply after reviewing the archive file list and confirming the replacement VM should assume the old PBS identity:

```sh
source ~/.bash_profile
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/pbs_config_restore_from_vault.yml \
  -e pbs_config_restore_hosts=pbs-restore.myrobertson.net \
  -e pbs_config_apply=true
```

The restore playbook:

1. Stops `proxmox-backup-proxy` and `proxmox-backup`.
2. Backs up current config paths under `/root/pbs-config-pre-restore-<timestamp>`.
3. Extracts the Vault archive onto `/`.
4. Starts `proxmox-backup` and `proxmox-backup-proxy`.

## Post-Restore Checks

```sh
proxmox-backup-manager version
proxmox-backup-manager datastore list
proxmox-backup-manager datastore status <datastore>
proxmox-backup-manager s3 endpoint list
curl -kI https://127.0.0.1:8007
```

From Proxmox:

```sh
pvesm status
pvesm list pbs.myrobertson.net --content backup | head
```

## Gaps

- This captures PBS configuration, not datastore data.
- Datastore data recovery depends on the retained Scooter datastore or the future object-backed datastore.
- VM hardware definition, cloud-init, and disk layout are still documented rather than fully provisioned as code.
- The export requires SSH or equivalent root access to the PBS VM.
