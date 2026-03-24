# Proxmox Ansible Bundle

This directory contains playbooks, roles, and templates for managing Proxmox clusters and related Ceph infrastructure.

- **proxmox.yml**: Inventory configuration for Proxmox nodes, uses the `community.proxmox.proxmox` dynamic inventory plugin (see `inventory/proxmox.yml`)
- **ceph_object_gw.yml**: Deploy Ceph Object Gateway on Proxmox nodes
- **rolling_restart.yml**: Safely reboot Proxmox nodes one at a time
- **intel_vpro.yml**: Configure Intel vPro interfaces on Proxmox nodes


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
   ```
   (Adjust the path to `inventory/proxmox.yml` as needed for your working directory.)
3. Adjust inventory and variables as needed

## Notes
- `inventory/proxmox.yml` is a dynamic inventory file using the `community.proxmox.proxmox` plugin. It is not a static inventory or a custom plugin script.
- `ansible.cfg` in this directory sets inventory and SSH options for Proxmox
- `handlers/restart_service.yml` is an example handler you can copy and adapt
- See top-level README for environment setup and global usage
