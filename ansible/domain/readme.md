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

## Domain Controller Time Drift Fix

Use `fix_dc_time_drift.yml` to remediate clock drift on Linux-based domain controllers.
It configures Chrony with stable upstream sources, enables client NTP access, and enables Samba signed NTP socket support.

### Inventory example

```ini
[domain_controllers]
dc01 ansible_host=192.168.4.20
```

### Run

```sh
ansible-playbook -i inventory/environments/staging.ini ansible/domain/fix_dc_time_drift.yml
```

### Override defaults (optional)

```yaml
# group_vars/domain_controllers.yml
dc_ntp_upstream_servers:
  - "0.us.pool.ntp.org"
  - "1.us.pool.ntp.org"

dc_ntp_allow_cidrs:
  - "192.168.4.0/24"

dc_timezone: "America/Chicago"
```

## Windows AD Domain Controller Time Drift Fix

Use `fix_dc_time_drift_windows.yml` to remediate drift on Windows AD domain controllers using `w32time`.
It configures manual upstream peers, marks the DC as a reliable time source, and forces immediate resync.

### Inventory example

```ini
[domain_controllers_windows]
rhonda.myrobertson.net

[domain_controllers_windows:vars]
ansible_connection=winrm
ansible_port=5986
ansible_winrm_transport=ntlm
ansible_winrm_server_cert_validation=ignore
ansible_user=Administrator
```

### Run

```sh
ansible-playbook -i inventory/environments/staging.ini ansible/domain/fix_dc_time_drift_windows.yml
```

### Override defaults (optional)

```yaml
# group_vars/domain_controllers_windows.yml
dc_w32time_ntp_peers:
  - "0.us.pool.ntp.org"
  - "1.us.pool.ntp.org"

dc_w32time_special_poll_interval: 900
dc_w32time_announce_flags: 5
```

## Windows DHCP High Availability

Use `configure_windows_dhcp_ha.yml` to install and authorize the DHCP role on two Windows servers, create IPv4 scopes, apply common DHCP options, and configure Microsoft DHCP failover.

This playbook expects two inventory groups:

- `dhcp_primary_windows`: the server where scopes are defined first
- `dhcp_partner_windows`: the failover partner that receives replicated scopes

### Inventory example

```ini
[dhcp_primary_windows]
rhonda.myrobertson.net

[dhcp_partner_windows]
beatrice.myrobertson.net

[dhcp_servers_windows:children]
dhcp_primary_windows
dhcp_partner_windows

[dhcp_servers_windows:vars]
ansible_connection=winrm
ansible_port=5986
ansible_winrm_transport=ntlm
ansible_winrm_server_cert_validation=ignore
ansible_user=Administrator
```

### Variable example

```yaml
# group_vars/dhcp_servers_windows.yml
dhcp_domain_name: myrobertson.net
dhcp_dns_servers:
  - 192.168.4.20
  - 192.168.4.21

dhcp_scopes:
  - scope_id: 192.168.4.0
    name: Corp LAN
    start_range: 192.168.4.100
    end_range: 192.168.4.199
    subnet_mask: 255.255.255.0
    lease_duration_days: 8
    state: active
    router:
      - 192.168.4.1
    dns_servers:
      - 192.168.4.20
      - 192.168.4.21
    exclusions:
      - start_range: 192.168.4.100
        end_range: 192.168.4.119

dhcp_failover_name: corp-dhcp-ha
dhcp_failover_shared_secret: "{{ vault_dhcp_failover_shared_secret }}"
dhcp_failover_mode: loadbalance
dhcp_failover_load_balance_percent: 50
dhcp_failover_max_client_lead_time: 01:00:00
dhcp_failover_auto_state_transition: true
dhcp_failover_state_switch_interval: 01:00:00
```

### Run

```sh
ansible-playbook -i inventory/environments/staging.ini ansible/domain/configure_windows_dhcp_ha.yml
```

### Notes

- The shared secret should come from Vault, not inventory plaintext.
- Scope creation happens on the primary server and is then replicated to the partner by DHCP failover.
- Existing failover relationships with the same name are reused if already present.

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
