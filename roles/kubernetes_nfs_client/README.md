# Kubernetes NFS Client Prep

This role prepares Kubernetes worker nodes to mount NFS volumes. Kubernetes mounts the Synology export directly through the PersistentVolume; this Ansible role does not create a production hostPath or a production host-level mount for Nextcloud data.

The optional Ansible-controlled mount is only a diagnostic validation path. It is disabled by default.

## Variables

```yaml
synology_nfs_server: "scooter.myrobertson.net"
synology_nfs_export_path: "/volume1/nextcloud-data"
synology_nfs_test_mount_path: "/mnt/synology-nextcloud-data-test"
synology_nfs_mount_options: "nfsvers=4.1,hard,noatime,rsize=1048576,wsize=1048576,timeo=600,retrans=2"
synology_nfs_test_mount_enabled: false
```

## Example

```bash
ansible-playbook -i inventory/environments/production.ini playbooks/core/kubernetes_nfs_clients.yml --limit kubernetes_workers
```

To test the export from the nodes, temporarily enable the diagnostic mount:

```bash
ansible-playbook -i inventory/environments/production.ini playbooks/core/kubernetes_nfs_clients.yml \
  --limit kubernetes_workers \
  -e synology_nfs_test_mount_enabled=true \
  -e synology_nfs_server=scooter.myrobertson.net \
  -e synology_nfs_export_path=/volume1/nextcloud-data
```

Do not use this diagnostic mount as the production Nextcloud data path. Production Nextcloud data should be mounted by Kubernetes from the NFS-backed PV/PVC.
