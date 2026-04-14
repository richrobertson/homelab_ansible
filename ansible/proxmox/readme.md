# Proxmox Ansible Bundle

This directory contains playbooks, roles, and templates for managing Proxmox clusters and related Ceph infrastructure.

## Core Playbooks

- **proxmox.yml**: Inventory configuration for Proxmox nodes, uses the `community.proxmox.proxmox` dynamic inventory plugin (see `inventory/proxmox.yml`)
- **ceph_admin_portal.yml**: Configure Ceph Dashboard (admin portal) bind settings and admin login credentials
- **ceph_object_gw.yml**: Deploy Ceph Object Gateway on Proxmox nodes
- **rolling_restart.yml**: Safely reboot Proxmox nodes one at a time
- **regular_maintenance.yml**: Apply rolling package updates, cleanup, and reboot only when required (targets `proxmox_nodes` by default; override with `-e proxmox_maintenance_hosts=<group_or_hosts>`)
- **proxmox_temperature_exporters.yml**: Ensure node exporter and hardware sensor packages are installed/running for Proxmox temperature monitoring
- **intel_vpro.yml**: Configure Intel vPro interfaces on Proxmox nodes
- **provision_certificates.yml**: Configure Vault PKI + Vault Agent automation for Proxmox API certificates
- **disable_vlan_hw_filtering.yml**: Disable VLAN hardware filtering on Proxmox interfaces and keep it persistent across reboots

## Scheduled Maintenance Playbooks (Phase 1 & 2)

See **MAINTENANCE_GUIDE.md** for details on scheduling and alerting.

### Phase 1: Weekly Critical Health Checks

Run **every Monday** (recommended during maintenance window):

- **phase1_health_checks.yml**: Composite playbook running all Phase 1 checks below
  - **ceph_health_check.yml**: Verify Ceph cluster status, OSDs, PGs, monitors, and quorum
  - **ha_cluster_verification.yml**: Check Proxmox HA quorum, node status, services, and resources
  - **certificate_expiry_check.yml**: Alert on certs expiring within 7/30 days, Vault connectivity

### Phase 2: Monthly Maintenance Tasks

Run **first Monday of each month** (recommended at 3 AM):

- **phase2_monthly_maintenance.yml**: Composite playbook running all Phase 2 tasks below
  - **log_maintenance.yml**: Compress, archive, and delete old logs; manage disk usage
  - **storage_utilization_report.yml**: Monitor datastore usage, snapshots, Ceph OSD balance
  - **provision_certificates.yml** (optional): Renew Proxmox API certificates via Vault

### Automated Scheduling

See **scripts/proxmox-maintenance-scheduler.sh** for:
- Cron task setup (simple scheduling)
- Systemd timer setup (recommended for production)
- Manual credential loading from Vault
- Log aggregation and failure reporting


## Roles and Templates
- **roles/**: Contains only Ansible roles—reusable logic for tasks like Ceph dashboard, object gateway, Intel vPro, SFP28 fabric, and Thunderbolt networking. Each role encapsulates tasks, handlers, and variables for a specific function.
  - ceph_dashboard
  - ceph_object_gateway
  - intel_vpro
  - sfp28_fabric
  - thunderbolt_fabric
  - thunderbolt_network_interfaces
- **templates/**: Contains only Jinja2 templates for generating network and udev configuration files dynamically during playbook runs. No roles or playbooks should be placed here.

## Handlers
The `handlers/` directory is for custom handlers (e.g., service restarts or notifications). See the sample handler in `handlers/restart_service.yml` for a template you can copy and adapt.

## Requirements
- `requirements.yml` lists required Ansible collections (e.g., ceph.automation)

## Usage
1. Activate your Python/Ansible environment
2. Run playbooks with:
   ```sh
  # Recommended: store dashboard credentials in an Ansible Vault vars file.
  ansible-playbook -i ../../inventory/proxmox.yml ceph_admin_portal.yml --extra-vars @../../inventory/ceph_dashboard_credentials.vault.yml --ask-vault-pass
   ansible-playbook -i ../../inventory/proxmox.yml ceph_object_gw.yml
   ansible-playbook -i ../../inventory/proxmox.yml rolling_restart.yml
   ansible-playbook -i ../../inventory/proxmox.yml regular_maintenance.yml
  ansible-playbook -i ../../inventory/proxmox.yml proxmox_temperature_exporters.yml
   ansible-playbook -i ../../inventory/proxmox.yml intel_vpro.yml
   ansible-playbook -i ../../inventory/proxmox.yml provision_certificates.yml
   ansible-playbook -i ../../inventory/proxmox.yml disable_vlan_hw_filtering.yml
   ```
   (Adjust the path to `inventory/proxmox.yml` as needed for your working directory.)
3. Adjust inventory and variables as needed

### Core site tag usage
When running the core orchestration playbook, certificate automation can be targeted or skipped with tags:

- Run only certificate automation:
  ```sh
  ansible-playbook playbooks/core/site.yml --tags certs
  ```
- Skip certificate automation:
  ```sh
  ansible-playbook playbooks/core/site.yml --skip-tags certs
  ```

## Notes
- `inventory/proxmox.yml` is a dynamic inventory file using the `community.proxmox.proxmox` plugin. It is not a static inventory or a custom plugin script.
- `inventory/proxmox.yml` and `ansible/proxmox/proxmox.yml` now use `validate_certs: true` and expect trusted Proxmox API certificates.
- `ansible.cfg` in this directory sets inventory and SSH options for Proxmox
- `handlers/restart_service.yml` is an example handler you can copy and adapt
- See top-level README for environment setup and global usage

## Runbooks
- Operational Proxmox runbooks are in `runbooks/proxmox/` at the repository root.
- Start with: `runbooks/proxmox/README.md`
- Includes procedures for:
  - SFP28 interface cutover workflow
  - Ceph dashboard bind recovery after network changes
  - HA rule recovery and `ha-manager` stabilization
  - CephFS storage activation/mountpoint recovery

## Proxmox certificate automation prerequisites
- Vault must expose a reachable API endpoint for Proxmox nodes (default: `https://vault.myrobertson.net:8200`).
- Set `proxmox_cert_cluster_san` per cluster group so each node gets only its cluster VIP SAN (for example `cl0.myrobertson.net` for cl0 nodes and `cl1.myrobertson.net` for cl1 nodes).
- A `vault` host group must exist and include the Vault server used to manage PKI/AppRole.
- `/root/.vault-token` must exist on the Vault host for role/policy/bootstrap tasks.
- `vault` CLI must be installed on both the Vault host and each Proxmox node.
- Proxmox nodes must be able to restart `pveproxy` after certificate updates.
- Control-node trust for Proxmox API validation uses a Vault CA bundle exported by `provision_certificates.yml` to `inventory/certs/proxmox-vault-ca.pem`.

## Rolling out certificate automation
Suggested phased rollout:

1. Add/verify at least one host in `[vault]` inside `inventory/environments/production.ini`.
2. Add nodes to `[proxmox_cert_nodes_cl0]` and `[proxmox_cert_nodes_cl1]` as needed; each child group sets its own `proxmox_cert_cluster_san`.
3. Run a canary bootstrap rollout (static inventory only):
   ```sh
   ansible-playbook -i ../../inventory/environments/production.ini provision_certificates.yml --limit <canary-node-host-or-ip>
   ```
  (`--limit` can be an inventory hostname, group, or host IP present in `production.ini`.)
4. Verify dynamic Proxmox inventory can be queried with TLS validation:
   ```sh
  REQUESTS_CA_BUNDLE=../../inventory/certs/proxmox-vault-ca.pem ansible-inventory -i ../../inventory/proxmox.yml --graph
   ```
5. Roll out to all Proxmox nodes using dynamic inventory (the playbook applies nodes one at a time via `serial: 1`):
   ```sh
  REQUESTS_CA_BUNDLE=../../inventory/certs/proxmox-vault-ca.pem ansible-playbook -i ../../inventory/environments/production.ini -i ../../inventory/proxmox.yml provision_certificates.yml
   ```
