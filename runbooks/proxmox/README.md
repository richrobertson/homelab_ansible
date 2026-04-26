# Proxmox Runbooks

This folder contains operational runbooks captured from real maintenance and incident work on the homelab Proxmox cluster.

## Runbooks
- [SFP28 interface cutover (cl0)](./network-sfp28-cutover-cl0.md)
- [Ceph dashboard bind fix after network/IP change](./ceph-dashboard-bind-fix.md)
- [HA rule recovery and ha-manager CLI stabilization](./ha-rule-and-cli-recovery.md)
- [CephFS storage activation and mountpoint recovery](./cephfs-storage-recovery.md)
- [Vault connectivity and Proxmox cert-agent recovery](./vault-connectivity-and-cert-agent-recovery.md)
- [PBS Scooter VM to Proxmox Thunderbolt migration](./pbs-scooter-to-proxmox-thunderbolt-migration.md)
- [PBS configuration DR from Vault](./pbs-config-dr-from-vault.md)
- [Proxmox Authelia SSO](./proxmox-authelia-sso.md)
- [Staging cluster rebuild and SDN investigation (2026-03-31)](./staging-cluster-rebuild-and-sdn-investigation-2026-03-31.md)

## Scope
- Cluster: `cl0`
- Nodes: `pve3`, `pve4`, `pve5`
- Network domains: `192.168.88.0/24` (vmbr0), `192.168.10.0/24` (Ceph/public)

## Usage notes
- Run commands as `root` on a Proxmox node unless otherwise stated.
- Use one-node-at-a-time changes for networking and reboots.
- Validate quorum and Ceph health between disruptive steps.
