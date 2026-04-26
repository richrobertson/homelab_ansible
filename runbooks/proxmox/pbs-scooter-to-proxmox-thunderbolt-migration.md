# PBS Scooter VM -> Proxmox Thunderbolt Migration

This runbook moves the Proxmox Backup Server VM currently running on the Synology NAS `scooter` into the Proxmox cluster so Proxmox backup and restore client traffic can use the Thunderbolt ring.
The backup datastore remains on Scooter's hard drives over the existing 10 Gb network path.

## Current State

- PBS service name: `pbs.myrobertson.net`
- Current PBS address: `192.168.1.217`
- Current Proxmox storages:
  - `pbs.myrobertson.net` datastore `store1`
  - `pbs-s3` datastore `pbs-s3`
- Current route from pve3/pve4/pve5 to PBS: `vmbr1`, not `en05` or `en06`
- Thunderbolt host loopbacks:
  - pve3: `10.0.0.83/32`
  - pve4: `10.0.0.84/32`
  - pve5: `10.0.0.85/32`

## Target State

- PBS VM compute runs on one Proxmox host, preferably the host with the best non-Ceph VM boot disk capacity.
- PBS keeps its LAN address `192.168.1.217` during the first boot/cutover to avoid breaking clients.
- PBS also gets a Thunderbolt service address, for example `10.0.0.87/32`.
- Proxmox PBS storage definitions are changed from `192.168.1.217` / `pbs.myrobertson.net` to the Thunderbolt service name or IP after validation.
- `proxmox_backup_storage_route_up{expected_network="thunderbolt"}` changes from `0` to `1` on pve3/pve4/pve5.
- PBS datastore I/O remains a separate PBS-to-Scooter leg over the 10 Gb network.

Do not place the PBS datastore on the same Ceph cluster it protects. That makes disaster recovery circular.
The desired durable state is split-path:

- Proxmox PVE clients -> PBS VM: Thunderbolt ring.
- PBS VM -> Scooter datastore: 10 Gb Scooter storage network.

The dashboard's backup route metric only proves the first leg. Monitor the second leg separately with PBS task latency, datastore verify duration, garbage collection duration, and Scooter 10 Gb interface throughput/errors.

## Migration Shape

Use a two-step migration:

1. Move PBS compute from Synology VMM to Proxmox while preserving the existing LAN identity.
2. Keep the datastore on Scooter and validate the PBS VM can mount/use it correctly after import.
3. Add and validate Thunderbolt service routing, then update Proxmox backup storage endpoints.

This keeps current backups recoverable while the ring path is built.

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
- Scooter 10 Gb link is healthy and the datastore export or block device path is documented.
- The datastore is not mounted read-write by more than one PBS instance at the same time.

PBS datastores are directory-backed on a Unix filesystem. Use a Scooter-backed block device or mount that preserves the required filesystem semantics for PBS chunk directories, fsync, permissions, and locking.

## Phase 1: Export From Synology

In Synology VMM:

1. Shut down the PBS VM cleanly.
2. Export the VM as OVA/OVF if available.
3. Keep the original VM powered off but intact.
4. Do not delete Synology snapshots until several successful backup and restore cycles complete on Proxmox.

If Synology only exports disk images, copy the boot disk to a Proxmox import-capable location such as `/var/lib/vz/template/iso` or a temporary NFS mount.

## Phase 2: Import PBS Compute To Proxmox

Create the VM shell on the selected Proxmox node. Use a stable VMID that does not collide with existing IDs.

Example pattern:

```sh
qm create 217 \
  --name pbs \
  --memory 8192 \
  --cores 4 \
  --cpu host \
  --net0 virtio,bridge=vmbr1 \
  --scsihw virtio-scsi-single \
  --agent enabled=1
```

Import the exported disk:

```sh
qm importdisk 217 /path/to/pbs-disk.vmdk <target-storage>
qm set 217 --scsi0 <target-storage>:vm-217-disk-0,discard=on,ssd=1
qm set 217 --boot order=scsi0
```

Boot with only the LAN NIC first. Validate:

```sh
qm start 217
ping -c3 192.168.1.217
curl -kI https://192.168.1.217:8007
pvesm status | grep pbs
```

If the PBS certificate or fingerprint changes, update the Proxmox storage fingerprint only after confirming the new fingerprint out of band.

## Phase 3: Reattach Scooter Datastore

Keep the existing Scooter-backed datastore path intact. Depending on how the current Synology VM is provisioned, use one of these patterns:

- Preferred for PBS semantics: present the same Scooter storage as a block device to the imported PBS VM, then mount the existing filesystem at the same path.
- Acceptable only after testing: mount the Scooter export inside PBS over the 10 Gb network and point the datastore to that mount path.

Before allowing backups:

```sh
findmnt <datastore-path>
proxmox-backup-manager datastore list
proxmox-backup-manager datastore status <datastore-name>
proxmox-backup-manager verify-job list
```

Run a datastore verify or a limited namespace verify before changing Proxmox clients to the Thunderbolt endpoint.

## Phase 4: Add Thunderbolt Service Path

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

## Phase 5: Cut Over Proxmox PBS Storage Endpoints

Canary with one storage first. Prefer a host/IP entry such as `pbs-tb.myrobertson.net -> 10.0.0.87`.

```sh
pvesm set pbs.myrobertson.net --server 10.0.0.87
pvesm status
```

Run a small backup or restore canary from a non-critical VM, then check:

```sh
ssh root@pve3 'for h in pve3 pve4 pve5; do ssh root@$h "grep proxmox_backup_storage_route_up /var/lib/prometheus/node-exporter/proxmox_transport.prom"; done'
```

Expected result:

```text
proxmox_backup_storage_route_up{...,route_dev="en05" ...} 1
proxmox_backup_storage_route_up{...,route_dev="en06" ...} 1
```

## Phase 6: Soak And Restore Test

Run at least:

- one scheduled backup
- one manual backup
- one file-level restore browse
- one full VM restore to an isolated VMID

Watch:

- `Proxmox Thunderbolt Service Traffic`
- `Backup Observability`
- `ProxmoxBackupStorageNotUsingThunderbolt`
- Scooter 10 Gb interface throughput, errors, and drops
- PBS task logs
- PBS datastore verify and garbage collection duration
- Ceph health
- prod Kubernetes node readiness

## Rollback

1. Set Proxmox storage endpoints back to `192.168.1.217` or `pbs.myrobertson.net`.
2. Power off the Proxmox-imported PBS VM.
3. Power on the original Synology VM.
4. Confirm `pvesm status` and `curl -kI https://192.168.1.217:8007`.
5. Keep the failed Proxmox VM stopped for inspection; do not delete it until backups are healthy.

## Completion Criteria

- PBS runs on Proxmox.
- Current PBS datastore remains on Scooter hard drives and is visible and verified from the Proxmox-hosted PBS VM.
- Proxmox client backup and restore traffic routes over `en05`/`en06`.
- PBS datastore traffic routes over the expected 10 Gb Scooter path.
- Grafana dashboard `Proxmox Thunderbolt Service Traffic` shows backup route as `Ring`.
- Alert `ProxmoxBackupStorageNotUsingThunderbolt` is quiet.
- At least one isolated restore test succeeds.
