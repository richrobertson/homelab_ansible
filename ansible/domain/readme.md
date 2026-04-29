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

## Windows RDP Certificate Binding

Use `configure_windows_rdp_certificates.yml` to bind a CA-issued machine certificate to the `RDP-Tcp` listener.
The playbook reuses the newest matching certificate already present in `LocalMachine\My`.
If no matching certificate exists, it can issue a host certificate from Vault PKI on the control node, import it into Windows, and then bind it to `RDP-Tcp`.
AD CS enrollment remains available as a fallback, but in this environment the Vault PKI path is the reliable default.

### Inventory example

```ini
[domain_controllers_windows]
rhonda.myrobertson.net

[dhcp_primary_windows]
rhonda.myrobertson.net

[dhcp_partner_windows]
beatrice.myrobertson.net

[dhcp_servers_windows:children]
dhcp_primary_windows
dhcp_partner_windows

[rdp_servers_windows:children]
domain_controllers_windows
dhcp_servers_windows

[domain_controllers_windows:vars]
ansible_connection=winrm
ansible_port=5985
ansible_winrm_scheme=http
ansible_winrm_transport=ntlm
ansible_winrm_server_cert_validation=ignore
ansible_user=ldap@myrobertson.net

[dhcp_servers_windows:vars]
ansible_connection=winrm
ansible_port=5985
ansible_winrm_scheme=http
ansible_winrm_transport=ntlm
ansible_winrm_server_cert_validation=ignore
ansible_user=ldap@myrobertson.net
```

### Variable example

```yaml
# group_vars/rdp_servers_windows.yml
rdp_cert_vault_enabled: true
rdp_cert_vault_manage_role: true
rdp_cert_vault_mount: pki_int
rdp_cert_vault_role_name: rdp-short-hosts
rdp_cert_vault_ttl: 720h
rdp_cert_vault_primary_domain: myrobertson.net
rdp_cert_root_ca_vault_path: secret/windows/domain/root_ca_cert
rdp_cert_root_ca_vault_field: cert_pem

# Add extra DNS aliases only if the certificate is expected to contain them.
rdp_cert_dns_names_extra:
  - janice.myrobertson.net

# Optional fallback if you explicitly want AD CS enrollment instead of Vault PKI.
rdp_cert_template_name: ""
rdp_cert_enrollment_url: ldap:
rdp_cert_min_valid_days: 0
```

### Run

```sh
source ~/.bash_profile
ansible-playbook -i inventory/environments/staging.ini ansible/domain/configure_windows_rdp_certificates.yml --limit rdp_servers_windows
```

### Notes

- The playbook restarts `TermService` only when it changes the bound certificate.
- The Vault-backed path manages a dedicated role at `pki_int/roles/rdp-short-hosts`, signs CSRs through `pki_int/sign/rdp-short-hosts`, and includes both the short hostname and FQDN in the certificate SAN list.
- The playbook considers an existing certificate valid only when it covers every expected DNS name, which prevents an older FQDN-only cert from being reused for short-name RDP connections.
- It then imports the Windows domain root CA from `secret/windows/domain/root_ca_cert` so the server presents a full private-trust chain.
- Export `VAULT_ADDR`, `VAULT_TOKEN`, and any `VAULT_SKIP_VERIFY` preference before running the playbook. Sourcing `~/.bash_profile` already does that in this environment.
- If your Microsoft Remote Desktop client still warns after this rollout, verify that the domain root CA is trusted on the client and that you are connecting with the same FQDN that appears on the certificate.
- If you connect with a short hostname but the certificate only contains the FQDN, prefer connecting with the FQDN or issue a template that includes both names.
- Use the AD CS fallback only if the host can actually reach the CA and the target template is published there.

## Windows RDP Certificate GPO Rollout

Use `deploy_windows_rdp_certificate_gpo.yml` to create a domain GPO that enables computer certificate autoenrollment and configures the RDP certificate template policy for Windows machines in the domain.

This is the right domain-wide control plane when you want AD CS and Group Policy to manage the baseline instead of binding individual listener certificates by hand.
By default the playbook links the GPO at the domain root, keeps `Authenticated Users` at read-only, and grants `Apply Group Policy` to `Domain Computers` so every domain-joined Windows machine receives the policy.
If you need a smaller blast radius, you can switch the scope mode back to targeted inventory computers.

### Inventory example

```ini
[domain_controllers_windows]
rhonda.myrobertson.net

[dhcp_primary_windows]
rhonda.myrobertson.net

[dhcp_partner_windows]
dns01.myrobertson.net

[dhcp_servers_windows:children]
dhcp_primary_windows
dhcp_partner_windows

[rdp_member_servers_windows]
janice.myrobertson.net

[rdp_servers_windows:children]
rdp_member_servers_windows
domain_controllers_windows
dhcp_servers_windows

[rdp_servers_windows:vars]
ansible_connection=winrm
ansible_port=5985
ansible_winrm_scheme=http
ansible_winrm_transport=ntlm
ansible_winrm_server_cert_validation=ignore
ansible_user=ldap@myrobertson.net
```

### Variable example

```yaml
# group_vars/domain_controllers_windows.yml
rdp_gpo_name: Windows RDP Certificate Autoenrollment
rdp_gpo_scope_mode: domain_computers

# Use the certificate template short name, not the display name.
rdp_gpo_template_name: Machine

# TLS plus NLA is the recommended baseline.
rdp_gpo_security_layer: ssl
rdp_gpo_require_nla: true

# If you need a narrow rollout instead of all domain computers, set:
# rdp_gpo_scope_mode: targeted
# and then add extra short hostnames only if they are real AD computer objects
# not already represented in rdp_servers_windows.
rdp_gpo_target_computers_extra: []

# Leave this false while any hosts are still intentionally pinned to a
# manually bound listener certificate.
rdp_gpo_clear_explicit_listener_binding: false
```

### Run

```sh
ansible-playbook -i inventory/environments/production.ini ansible/domain/deploy_windows_rdp_certificate_gpo.yml
```

### Notes

- This playbook is complementary to `configure_windows_rdp_certificates.yml`, not a replacement for it. The Vault-backed listener binding playbook is still the better fit when you need short-name SANs that AD CS templates do not supply.
- Microsoft documents that the RDP template policy uses the certificate template name, so use the template short name such as `Machine` or `RemoteDesktopComputer`, not just the friendly display name. Sources: https://learn.microsoft.com/en-us/troubleshoot/windows-server/certificates-and-public-key-infrastructure-pki/remote-desktop-server-certificates-renewed and https://learn.microsoft.com/en-us/windows/client-management/mdm/policy-csp-admx-terminalserver
- Autoenrollment is enabled through the computer certificate autoenrollment policy under Public Key Policies. Source: https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2012-r2-and-2012/jj129705%28v%3Dws.11%29
- The playbook forces `gpupdate /target:computer /force` and `certutil -pulse` on the inventory hosts it can reach directly, while the domain-wide GPO still applies to the rest of `Domain Computers` on normal Group Policy refresh.
- Existing hosts with an explicit `RDP-Tcp` certificate thumbprint already pinned will keep using that certificate until you set `rdp_gpo_clear_explicit_listener_binding=true`.
- Even with a correct GPO, clients that connect by raw IP can still warn if the certificate only contains hostnames. Prefer `hostname` or `hostname.domain.tld` over `192.168.x.x`.

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
- `configure_windows_rdp_certificates.yml` complements this by ensuring those same Windows hosts present a CA-backed RDP certificate instead of the default self-signed listener certificate.

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
