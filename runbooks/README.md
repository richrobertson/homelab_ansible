# Infrastructure recovery runbooks

This directory owns recovery procedures for systems outside Kubernetes. The
production recovery coordinator starts with the master plan in
`homelab_flux/docs/runbooks/DISASTER_RECOVERY_PLAN.md`, then uses the runbooks
below for the affected protection domain.

## Recovery index

- [Synology disaster recovery](synology/synology-disaster-recovery.md): Kermit,
  Scooter, Snapshot Replication, Hyper Backup, Active Backup for Business, and
  NAS replacement.
- [Synology share access audit](synology/synology-share-access-audit.md):
  repeatable read-only audit of every Kermit/Scooter share for unauthenticated
  and guest access across SMB, NFS, rsync, iSCSI, WebDAV, and internet reach —
  including NFS squash flags and the gateway routes that publish DSM.
- [Windows domain disaster recovery](domain/windows-domain-disaster-recovery.md):
  Active Directory, DNS, DHCP failover, and Synology bare-metal recovery.
- [Proxmox recovery index](proxmox/README.md): cluster, Ceph, networking,
  certificates, HA, and PBS procedures.
- [Credential lifecycle roadmap](security/credential-lifecycle-roadmap.md):
  follow-up workstreams from the 2026-07-29 exposure incident — password
  expiry, enforced and automated rotation, managed service accounts.
- [Retiring `ldap@myrobertson.net`](security/ldap-account-retirement.md):
  consumer inventory, per-service least-privilege replacement accounts,
  Vault-managed rotation, zero-downtime cutover, and decommission.
- [PBS configuration recovery from Vault](proxmox/pbs-config-dr-from-vault.md).
- [PBS migration and datastore recovery](proxmox/pbs-scooter-to-proxmox-thunderbolt-migration.md).

## Safety boundary

Run read-only inventory and health checks first. Promoting a replica, restoring
a domain controller, replacing a live NAS, applying a PBS configuration, or
accepting data loss requires an incident lead and a recorded recovery point.
Never place Vault values, DSRM passwords, PBS encryption keys, or backup
credentials in a runbook or incident record.
