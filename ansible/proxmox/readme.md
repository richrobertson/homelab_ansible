# Proxmox Ansible Bundle

This directory contains playbooks, roles, and templates for managing Proxmox clusters and related Ceph infrastructure.

- **proxmox.yml**: Inventory configuration for Proxmox nodes, uses the `community.proxmox.proxmox` dynamic inventory plugin (see `inventory/proxmox.yml`)
- **ceph_object_gw.yml**: Deploy Ceph Object Gateway on Proxmox nodes
- **rolling_restart.yml**: Safely reboot Proxmox nodes one at a time
- **intel_vpro.yml**: Configure Intel vPro interfaces on Proxmox nodes
- **provision_certificates.yml**: Configure Vault PKI + Vault Agent automation for Proxmox API certificates
- **disable_vlan_hw_filtering.yml**: Disable VLAN hardware filtering on Proxmox interfaces and keep it persistent across reboots


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
   ansible-playbook -i ../../inventory/proxmox.yml ceph_object_gw.yml
   ansible-playbook -i ../../inventory/proxmox.yml rolling_restart.yml
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

## Proxmox certificate automation prerequisites
- Vault must expose a reachable API endpoint for Proxmox nodes (default: `https://vault.myrobertson.net:8200`).
- Set `proxmox_cert_cluster_san` per cluster group so each node gets only its cluster VIP SAN (for example `cl0.myrobertson.net` for cl0 nodes and `cl1.myrobertson.net` for cl1 nodes).
- A `vault` host group must exist and include the Vault server used to manage PKI/AppRole.
- `/root/.vault-token` must exist on the Vault host for role/policy/bootstrap tasks.
- `vault` CLI must be installed on both the Vault host and each Proxmox node.
- Proxmox nodes must be able to restart `pveproxy` after certificate updates.

## Rolling out certificate automation
Suggested phased rollout:

1. Add/verify at least one host in `[vault]` inside `inventory/environments/production.ini`.
2. Add nodes to `[proxmox_cert_nodes_cl0]` and `[proxmox_cert_nodes_cl1]` as needed; each child group sets its own `proxmox_cert_cluster_san`.
3. Run a canary bootstrap rollout (static inventory only):
  ```sh
  ansible-playbook -i ../../inventory/environments/production.ini provision_certificates.yml --limit <canary-node-fqdn>
  ```
4. Verify dynamic Proxmox inventory can be queried with TLS validation:
  ```sh
  ansible-inventory -i ../../inventory/proxmox.yml --graph
  ```
5. Roll out to all Proxmox nodes using dynamic inventory (the playbook applies nodes one at a time via `serial: 1`):
  ```sh
  ansible-playbook -i ../../inventory/environments/production.ini -i ../../inventory/proxmox.yml provision_certificates.yml
  ```
