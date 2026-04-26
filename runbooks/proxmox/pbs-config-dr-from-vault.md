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

Current prod export:

```text
exported_at=2026-04-26T06:36:13Z
source_host=pbs.myrobertson.net
pbs_version=proxmox-backup-server 4.1.8-1 running version: 4.1.6
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

The current Proxmox restore target is VMID `217` on `pve5`, named `pbs-restore`. It is intentionally powered off with its NIC link down until PBS is installed and the restore/cutover window is ready.

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

By default `pbs_config_preserve_network=true`, so `/etc/network/interfaces` from the archive is not applied. This keeps the replacement VM reachable when its NIC names differ from the original Synology VM.

The exported archive includes `/etc/network/interfaces`, `/etc/hosts`, and `/etc/hostname`. Applying it with `pbs_config_preserve_network=false` will make the replacement VM assume the live PBS network identity from the archive, including `192.168.1.217` and `192.168.7.10`, so the Synology PBS VM must not be simultaneously active on those addresses.

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
