# Domain Services Ansible Bundle

This directory contains playbooks and roles for managing domain services for myrobertson.net and related infrastructure.

## Example Playbook: domain.yml

```yaml
# Minimal example for real domain automation
- name: Example domain automation
  hosts: all
  gather_facts: true
  vars:
    example_var: "value"
  roles:
    - example_role
```

Replace `example_role` and `example_var` with the roles and variables your domain automation actually needs.

## Placeholder Playbook

If you do not have a real domain playbook yet, keep `domain.yml` as a safe stub. It always fails immediately, which prevents accidental runs of incomplete or destructive automation.

For real domain automation, see [homelab_bootstrap](https://github.com/richrobertson/homelab_bootstrap) or replace the placeholder with your own tasks and roles.

```yaml
# Placeholder playbook
- name: Domain automation placeholder
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Domain playbook not implemented
      ansible.builtin.fail:
        msg: >-
          ansible/domain/domain.yml is a placeholder and is intentionally not
          implemented. Use the homelab_bootstrap project for Proxmox inventory
          and related bootstrap configuration.
```

## Roles

See `roles/` for included custom roles, such as:

- Active Directory user management
- AD Certificate Services (Root CA)
- Vault PKI (Intermediate CA)
- Additional domain-specific roles as needed

## Usage

1. Activate your Python/Ansible environment.
2. Run the main playbook:

```sh
ansible-playbook -i <your_inventory> domain.yml
```

3. Adjust variables and inventory as needed.

### Core orchestration entrypoint

When running the repository-wide orchestration, use the canonical core entrypoint:

```sh
ansible-playbook playbooks/core/site.yml
```

The root `site.yml` remains available as a compatibility wrapper.

## Notes

- `ansible.cfg` in this directory sets local options for domain automation.
- See the top-level README for environment setup and global usage.
