# NetBox Ansible Bundle

This directory contains playbooks, roles, and templates for automating NetBox IPAM and network configuration.

## Playbooks
- **assign_ip_addresses.yml**: Assigns IP addresses in NetBox
- **create_prefixes.yml**: Creates prefixes in NetBox
- **create_vlan_interfaces.yml**: Creates VLAN interfaces in NetBox
- **generate_config.yml**: Generates network device configs from NetBox data
- **populate_netbox_ipam.yml**: Populates NetBox with IPAM data

## Inventory
- **netbox_inv.yml**: Example inventory for NetBox automation (see this file for host/group structure and variable examples)

## Roles & Templates
- See `roles/` for custom roles
- See `templates/` for Jinja2 templates for config generation
- See `configs/` for generated configs or config fragments

## Usage
1. Activate your Python/Ansible environment
2. Set NetBox API credentials as environment variables (required for playbook authentication):
   ```sh
   export NETBOX_TOKEN=<YOUR_API_TOKEN>
   export NETBOX_API=<YOUR_NETBOX_URL>
   ```
3. Run playbooks, e.g.:
   ```sh
   ansible-playbook -i netbox_inv.yml assign_ip_addresses.yml
   ```
4. Adjust variables and inventory as needed

## Notes
- `ansible.cfg` in this directory sets local options for NetBox automation
- See top-level README for environment setup and global usage
