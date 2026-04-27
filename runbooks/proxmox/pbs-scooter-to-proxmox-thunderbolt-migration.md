# PBS Scooter VM -> Proxmox Thunderbolt Migration

This runbook moves the Proxmox Backup Server VM currently running on the Synology NAS `scooter` into the Proxmox cluster so Proxmox backup and restore client traffic can use the Thunderbolt ring.
It also stages a new S3-backed PBS datastore so backup data can move off Scooter and into object storage.

PBS S3 datastore support is currently a technology preview. Keep the Scooter datastore intact until the new datastore has passed backup, verify, prune, garbage collection, and isolated restore tests.

## Current State

- PBS service name: `pbs.myrobertson.net`
- Current PBS address: `192.168.1.217`
- Current Proxmox storages:
  - `pbs-s3` datastore `pbs-s3`
- Current Proxmox PBS storage endpoints point at Thunderbolt service IP `10.0.0.87`.
- Transitional Thunderbolt service path: `pve5` listens on `10.0.0.87:8007` and proxies to `192.168.1.217:8007`.
- Current primary PBS datastore capacity: about 5.0 TiB total, about 1.4 TiB used.
- Current primary PBS snapshot count visible from Proxmox: 310.
- Replacement VM shell staged on `pve5`:
  - VMID `217`, name `pbs-restore`
  - state `running`, installed from unattended PBS auto-install ISO after the Scooter PBS VM was shut down
  - boot disk `local-lvm:vm-217-disk-0`, 64 GiB
  - object/cache disk `local-lvm:vm-217-disk-1`, 128 GiB
  - installer ISO detached after install; boot order is `scsi0`
  - NIC `vmbr1`
  - `192.168.1.217:8007` is online
  - PBS configuration was restored from Vault with the fresh Proxmox VM network configuration preserved
  - generated install root password is stored at `secret/proxmox/pbs/prod/install`
  - local S3 cache disk is mounted at `/mnt/datastore/pbs-s3-cache`
- Current datastore status:
  - `pbs-s3` is restored in PBS config and should continue to use the existing AWS S3 endpoint `aws-homelab-pbs` with bucket `myrobertson-homelab-pbs`.
  - `pbs-s3` is active after rebuilding the replacement cache path's `.chunks` shard tree under `/mnt/datastore/pbs-s3-cache`.
  - `pbs-b2` is a new additional datastore using Backblaze B2 endpoint `backblaze-b2-pbs`, bucket `myrobertson-pbs`, and cache path `/mnt/datastore/pbs-b2-cache`.
  - `pbs-b2` is also configured as Proxmox storage through the Thunderbolt service IP `10.0.0.87`.
  - `pbs-b2-encrypted` is a Proxmox client storage alias for datastore `pbs-b2` with PBS client-side encryption enabled. The encryption key is escrowed in Vault at `secret/proxmox/pbs/prod/client-encryption/pbs-b2-encrypted`.
  - Vault VM backups intentionally remain on an unencrypted PBS client target because the Vault guest disk is already encrypted and the recovery key is managed separately.
  - Legacy `store1` history is no longer wired into active Proxmox storage configuration and should be treated as retired unless deliberately reattached for historical restore work.
- Thunderbolt host loopbacks:
  - pve3: `10.0.0.83/32`
  - pve4: `10.0.0.84/32`
  - pve5: `10.0.0.85/32`

## Target State

- PBS VM compute runs on one Proxmox host, preferably the host with the best non-Ceph VM boot disk capacity.
- PBS keeps its LAN address `192.168.1.217` during the first boot/cutover to avoid breaking clients.
- PBS also gets a Thunderbolt service address, for example `10.0.0.87/32`.
- `proxmox_backup_storage_route_up{expected_network="thunderbolt"}` changes from `0` to `1` on pve3/pve4/pve5.
- New backups land on an S3-backed PBS datastore with a persistent local cache disk on the Proxmox-hosted PBS VM.
- The old Scooter datastore remains mounted read-only or offline-retained until the S3 datastore has proven restore reliability.
- Non-k8s, non-Jellyfin, non-PBS, non-Vault Proxmox guests use the encrypted Proxmox storage alias `pbs-b2-encrypted` so object-store backups are client-side encrypted before PBS writes them to Backblaze B2.

Do not place the PBS datastore on the same Ceph cluster it protects. That makes disaster recovery circular.
The desired durable state is split-path:

- Proxmox PVE clients -> PBS VM: Thunderbolt ring.
- PBS VM -> object storage datastore: HTTPS to S3-compatible object storage.
- PBS VM -> Scooter datastore: retained only for rollback and historical restores during the transition.

The dashboard's backup route metric only proves the first leg. Monitor the object-store leg separately with PBS task latency, datastore verify duration, garbage collection duration, object-store API error rates, and cloud storage cost/egress usage.

## Migration Shape

Use a two-step migration:

1. Build a new Proxmox-hosted PBS VM or import the current PBS boot disk, preserving the existing LAN identity.
2. Attach a dedicated local cache disk or dataset for the S3 datastore. Size target: 128 GiB unless object-store request costs or restore working set require more.
3. Export the current PBS configuration into Vault using [PBS configuration DR from Vault](pbs-config-dr-from-vault.md).
4. Add an S3-backed datastore for object storage.
5. Repoint one non-critical Proxmox backup job to the new datastore and validate backup plus restore.
6. Gradually migrate scheduled backup jobs from Scooter datastore to object datastore.
7. Keep the old Scooter datastore intact until the retention horizon and restore drills are complete.

This keeps current backups recoverable while the new PBS host and object datastore are proven.

## Client-Side Encryption Policy

Use two Proxmox PBS storage IDs for the same Backblaze-backed PBS datastore:

- `pbs-b2`: unencrypted PBS client target for guests that are already protected at the disk/application layer, currently the Vault VM.
- `pbs-b2-encrypted`: encrypted PBS client target for regular Proxmox VM/LXC backups. Its key must be restored from Vault before relying on encrypted snapshots in a full DR scenario.

Current encrypted backup job:

- Job ID: `non-k8s-r2-encrypted-nightly`
- Schedule: `mon,tue,wed,thu,fri,sat,sun 05:30`
- Storage: `pbs-b2-encrypted`
- Included guests: `1001,1002,101,112,129,137,200,202,208`
- Excluded by policy: k8s nodes, Jellyfin, PBS, and Vault.

The encryption key is stored at:

```sh
vault kv get secret/proxmox/pbs/prod/client-encryption/pbs-b2-encrypted
```

Apply or recover the encrypted Proxmox storage alias and job with:

```sh
ansible-playbook ansible/proxmox/pbs_client_encrypted_backups.yml
```

## Object Store Choice

The phrase "Backblaze R2" is ambiguous:

- Backblaze's S3-compatible product is Backblaze B2.
- Cloudflare's S3-compatible product is Cloudflare R2.

Use the matching endpoint profile:

### Backblaze B2

- Endpoint: `s3.us-west-002.backblazeb2.com`
- Region: `us-west-002`
- Bucket: `myrobertson-pbs`
- Bucket ID: `bb288c2b85cbce8e9cd3091b`
- Bucket type: private
- File lifecycle: keep all versions
- Object Lock: default 3 days
- Vault secret path: `secret/proxmox/pbs/prod/backblaze-b2`
- PBS S3 endpoint: `backblaze-b2-pbs`
- PBS datastore: `pbs-b2`
- PBS cache path: `/mnt/datastore/pbs-b2-cache`
- Recommended key scope: bucket-specific key with list, read, write, and delete permissions only for this bucket

Example PBS endpoint:

```sh
proxmox-backup-manager s3 endpoint create backblaze-b2-pbs \
  --access-key "$B2_APPLICATION_KEY_ID" \
  --secret-key "$B2_APPLICATION_KEY" \
  --endpoint s3.us-west-002.backblazeb2.com \
  --region us-west-002 \
  --path-style true \
  --provider-quirks skip-if-none-match-header \
  --provider-quirks delete-objects-via-delete-object
```

### Cloudflare R2

- Endpoint format: `<account-id>.r2.cloudflarestorage.com`
- Region: `auto`
- Path-style addressing: enabled
- Bucket: create a dedicated PBS bucket, for example `myrobertson-pbs-prod`

Example PBS endpoint:

```sh
proxmox-backup-manager s3 endpoint create cloudflare-r2-pbs \
  --access-key "$R2_ACCESS_KEY_ID" \
  --secret-key "$R2_SECRET_ACCESS_KEY" \
  --endpoint "<account-id>.r2.cloudflarestorage.com" \
  --region auto \
  --path-style true
```

## New Datastore Shape

Create a persistent local cache on non-Ceph Proxmox storage attached to the PBS VM. Do not put the cache on the Ceph cluster being protected.

Example inside the PBS VM:

```sh
mkfs.xfs /dev/disk/by-id/<pbs-s3-cache-disk>
mkdir -p /mnt/datastore/pbs-object-cache
echo '/dev/disk/by-id/<pbs-s3-cache-disk> /mnt/datastore/pbs-object-cache xfs defaults,noatime 0 2' >> /etc/fstab
mount /mnt/datastore/pbs-object-cache
```

Create the S3 datastore after the endpoint is configured:

```sh
proxmox-backup-manager datastore create pbs-object /mnt/datastore/pbs-object-cache \
  --backend type=s3,client=backblaze-b2-pbs,bucket=myrobertson-pbs
proxmox-backup-manager datastore update pbs-object --gc-schedule 'daily 04:15'
proxmox-backup-manager datastore list
```

The production Backblaze datastore was created as:

```sh
proxmox-backup-manager datastore create pbs-b2 /mnt/datastore/pbs-b2-cache \
  --backend type=s3,client=backblaze-b2-pbs,bucket=myrobertson-pbs \
  --gc-schedule daily \
  --notification-mode notification-system
```

The local cache path is not the full datastore. It is a required persistent cache used by PBS to reduce backend API calls and improve performance.

## Rebuild An Existing S3 Datastore Cache

When restoring an S3-backed PBS datastore onto a fresh VM, the datastore configuration and S3 credentials can be correct while the local cache path is empty. PBS expects the local cache to contain the `.chunks` directory and all 65,536 shard directories. If those are missing, Proxmox reports errors like:

```text
unable to open chunk store at "/mnt/datastore/pbs-s3-cache/.chunks" - No such file or directory
```

Rebuild the cache skeleton, then let PBS use the S3 backend:

```sh
CACHE=/mnt/datastore/pbs-s3-cache
install -d -o backup -g backup -m 0755 "$CACHE"
install -d -o backup -g backup -m 0750 "$CACHE/.chunks"
for a in $(seq 0 255); do
  aa=$(printf "%02x" "$a")
  for b in $(seq 0 255); do
    bb=$(printf "%02x" "$b")
    install -d -o backup -g backup -m 0750 "$CACHE/.chunks/$aa$bb"
  done
done
proxmox-backup-manager datastore update pbs-s3 --delete maintenance-mode || true
proxmox-backup-manager datastore s3-refresh pbs-s3
```

Validate from Proxmox:

```sh
pvesm status --storage pbs-s3
```

## Preflight

Run from a workstation with Ansible and Proxmox access:

```sh
source ~/.bash_profile
ssh root@pve3 'ceph -s; pvesm status; pvesh get /cluster/resources --type vm --output-format json'
ssh root@pve3 'for h in pve3 pve4 pve5; do ssh root@$h "ip route get 192.168.1.217"; done'
```

From the PBS VM or DSM console, record:

```sh
hostname -f
ip -br addr
ip route
proxmox-backup-manager version
proxmox-backup-manager datastore list
proxmox-backup-manager cert info || true
```

Confirm:

- Current PBS backups are not running.
- The PBS VM has a clean shutdown path from Synology VMM.
- A fresh Scooter snapshot/export exists before shutdown.
- PBS datastore storage location and size are known.
- Proxmox target storage has enough capacity for the VM boot disk.
- Proxmox target storage has enough capacity for a 64-128 GiB persistent S3 datastore cache disk.
- Object-store bucket exists before PBS configuration.
- Object-store lifecycle rules, object lock, retention, and cost alerts are reviewed before production use.
- Object-store key is scoped to the PBS bucket and stored in Vault, not committed to Git.
- Backblaze B2 application key for bucket `myrobertson-pbs` is stored in Vault before creating the PBS S3 endpoint.
- Existing Scooter datastore is not mounted read-write by more than one PBS instance at the same time.

The existing Scooter datastore is directory-backed on a Unix filesystem. Use a Scooter-backed block device or mount that preserves the required filesystem semantics for PBS chunk directories, fsync, permissions, and locking while it remains attached.

## Phase 1: Export From Synology

In Synology VMM:

1. Shut down the PBS VM cleanly.
2. Export the VM as OVA/OVF if available.
3. Keep the original VM powered off but intact.
4. Do not delete Synology snapshots until several successful backup and restore cycles complete on Proxmox.

If Synology only exports disk images, copy the boot disk to a Proxmox import-capable location such as `/var/lib/vz/template/iso` or a temporary NFS mount.

## Phase 2: Build Or Import PBS Compute To Proxmox

Create the VM shell on the selected Proxmox node. Use a stable VMID that does not collide with existing IDs.

Current staged shell on `pve5`:

```sh
qm create 217 \
  --name pbs-restore \
  --memory 8192 \
  --cores 4 \
  --cpu host \
  --net0 virtio,bridge=vmbr1,link_down=1 \
  --scsihw virtio-scsi-single \
  --agent enabled=1 \
  --onboot 0
qm set 217 --scsi0 local-lvm:64,discard=on,ssd=1
qm set 217 --scsi1 local-lvm:128,discard=on,ssd=1
qm set 217 --ide2 local:iso/proxmox-backup-server_4.1-1.iso,media=cdrom
qm set 217 --boot order=ide2\;scsi0
```

For a Synology disk import path, import the exported disk instead of installing fresh:

```sh
qm importdisk 217 /path/to/pbs-disk.vmdk <target-storage>
qm set 217 --scsi0 <target-storage>:vm-217-disk-0,discard=on,ssd=1
qm set 217 --boot order=scsi0
```

For a config-from-Vault rebuild path, install PBS from an unattended auto-install ISO, update packages, then stage the Vault restore. Keep the NIC link down until the restore has been reviewed and the live Synology PBS VM is stopped or otherwise isolated.

The replacement install used `proxmox-auto-install-assistant` with `reboot-mode = "power-off"` to avoid an auto-install reboot loop. After the installer powered the VM off, the ISO was detached and boot order was changed to `scsi0`.

Apply the Vault restore with the replacement VM network preserved:

```sh
source ~/.bash_profile
ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i '192.168.1.217,' \
  ansible/proxmox/pbs_config_restore_from_vault.yml \
  -e pbs_config_restore_hosts=192.168.1.217 \
  -e ansible_user=root \
  -e pbs_config_apply=true
```

Boot with only the intended LAN NIC active first. Validate:

```sh
qm start 217
ping -c3 192.168.1.217
curl -kI https://192.168.1.217:8007
pvesm status | grep pbs
```

If the PBS certificate or fingerprint changes, update the Proxmox storage fingerprint only after confirming the new fingerprint out of band.

## Phase 3: Reattach Scooter Datastore

Keep the existing Scooter-backed datastore path intact for rollback and historical restores. Depending on how the current Synology VM is provisioned, use one of these patterns:

- Preferred for PBS semantics: present the same Scooter storage as a block device to the imported PBS VM, then mount the existing filesystem at the same path.
- Acceptable only after testing: mount the Scooter export inside PBS over the 10 Gb network and point the datastore to that mount path.

Before allowing backups:

```sh
findmnt <datastore-path>
proxmox-backup-manager datastore list
proxmox-backup-manager datastore status <datastore-name>
proxmox-backup-manager verify-job list
```

Run a datastore verify or a limited namespace verify before allowing this imported PBS VM to become the primary backup target.

After the new object datastore has passed restore testing, set the Scooter datastore to read-only or maintenance mode before any destructive cleanup work.

## Phase 4: Add Object Datastore

Create the object-storage endpoint and datastore using the Object Store Choice and New Datastore Shape sections above.

Validation:

```sh
proxmox-backup-manager s3 endpoint list
proxmox-backup-manager datastore list
proxmox-backup-manager datastore status pbs-object
```

Do not move all backup jobs at once. First run a non-critical backup and one isolated restore against `pbs-object`.

## Phase 5: Add Thunderbolt Service Path

There are two supported designs.

### Preferred: routed VM service IP

Add a routed VM interface or host route that gives the PBS VM a dedicated Thunderbolt service IP such as `10.0.0.87/32`.

Requirements:

- pve3/pve4/pve5 route `10.0.0.87` over `en05`/`en06`.
- The PBS VM replies back through the Proxmox host that owns the service IP.
- FRR/OpenFabric or static routes advertise/reach `10.0.0.87`.

Validation:

```sh
ssh root@pve3 'for h in pve3 pve4 pve5; do ssh root@$h "ip route get 10.0.0.87"; done'
ssh root@pve3 'for h in pve3 pve4 pve5; do ssh root@$h "nc -vz 10.0.0.87 8007"; done'
```

### Transitional: host proxy

Run a TCP proxy on the Proxmox host that owns PBS:

- Listens on a host Thunderbolt service IP.
- Forwards to PBS VM `192.168.1.217:8007`.

This proves backup/restore client traffic across the ring before changing PBS guest networking. Treat it as transitional; direct routed VM service IP is cleaner.

## Phase 6: Cut Over Proxmox PBS Storage Endpoints

Canary with one storage first. Prefer a host/IP entry such as `pbs-tb.myrobertson.net -> 10.0.0.87`.

```sh
pvesm set pbs.myrobertson.net --server 10.0.0.87
pvesm status
```

Run a small backup or restore canary from a non-critical VM to `pbs-object`, then check:

```sh
ssh root@pve3 'for h in pve3 pve4 pve5; do ssh root@$h "grep proxmox_backup_storage_route_up /var/lib/prometheus/node-exporter/proxmox_transport.prom"; done'
```

Expected result:

```text
proxmox_backup_storage_route_up{...,route_dev="en05" ...} 1
proxmox_backup_storage_route_up{...,route_dev="en06" ...} 1
```

## Phase 7: Soak And Restore Test

Run at least:

- one scheduled backup
- one manual backup
- one file-level restore browse
- one full VM restore to an isolated VMID
- one verify job against `pbs-object`
- one prune and garbage collection cycle against `pbs-object`

Watch:

- `Proxmox Thunderbolt Service Traffic`
- `Backup Observability`
- `ProxmoxBackupStorageNotUsingThunderbolt`
- `ProxmoxBackupTasksFailing`
- `ProxmoxBackupSuccessStale`
- Object-store API errors, request volume, storage growth, and egress/cost alerts
- PBS task logs
- PBS datastore verify and garbage collection duration
- Ceph health
- prod Kubernetes node readiness

## Rollback

1. Stop sending new backup jobs to `pbs-object`.
2. Set Proxmox storage endpoints back to the old Scooter-hosted PBS endpoint if the Proxmox-hosted PBS VM is unhealthy.
3. Power off the Proxmox-imported PBS VM only if it cannot safely coexist.
4. Power on the original Synology VM if it was shut down.
5. Confirm `pvesm status` and `curl -kI https://192.168.1.217:8007`.
6. Keep the failed Proxmox VM and object datastore intact for inspection; do not delete cloud data until backups are healthy and restore paths are known.

## Completion Criteria

- PBS runs on Proxmox.
- Proxmox client backup and restore traffic routes over `en05`/`en06`.
- New scheduled backups land on `pbs-object`.
- `pbs-object` verify, prune, and garbage collection jobs complete without errors.
- At least one isolated full restore from `pbs-object` succeeds.
- The old Scooter datastore remains available for historical restores until the agreed retention horizon expires.
- Grafana dashboard `Proxmox Thunderbolt Service Traffic` shows backup route as `Ring`.
- Alert `ProxmoxBackupStorageNotUsingThunderbolt` is quiet.
- Alerts `ProxmoxBackupTasksFailing` and `ProxmoxBackupSuccessStale` are quiet.
