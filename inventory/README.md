# Inventory Setup Guide

This directory contains tracked inventory templates plus the local inventory paths used for real environments.

## Tracked vs local files

Tracked templates:

- `inventory/production.ini.example`
- `inventory/staging.ini.example`
- `inventory/proxmox.yml.example`
- `inventory/synology.ini.example`

Local concrete inventories and overrides (gitignored):

- `inventory/environments/production.ini`
- `inventory/environments/staging.ini`
- `inventory/environments/synology.ini`
- `inventory/proxmox.yml`

Treat local inventories as sensitive. Even when they only contain hostnames and IPs, they often grow to include Vault endpoints, AppRole IDs, and cluster-specific topology.

## Quick start

1. Create the local inventory directory.

```bash
mkdir -p inventory/environments
```

2. Copy the tracked templates you need.

```bash
cp inventory/production.ini.example inventory/environments/production.ini
cp inventory/staging.ini.example inventory/environments/staging.ini
cp inventory/proxmox.yml.example inventory/proxmox.yml
cp inventory/synology.ini.example inventory/environments/synology.ini
```

3. Update the local copies with your real hosts, users, and endpoints.

4. Validate the inventory before a rollout.

```bash
ansible-inventory -i inventory/environments/staging.ini --graph
```

## Environment structure

- `environments/production.ini`: production infrastructure, kept local
- `environments/staging.ini`: staging or pre-production infrastructure, kept local
- `proxmox.yml`: dynamic inventory plugin configuration, kept local

Dynamic Proxmox inventory documentation:
https://docs.ansible.com/ansible/latest/collections/community/proxmox/proxmox_inventory.html

## Proxmox certificate bootstrap inventory

`ansible/proxmox/provision_certificates.yml` supports two operating modes:

- Delegated Vault bootstrap: define a `[vault]` host group and keep `/root/.vault-token` on that host so the playbook can mint or refresh AppRole material and fetch the Vault CA.
- Inventory-preseeded mode: provide `proxmox_vault_addr`, `proxmox_cert_vault_role_id`, and `proxmox_cert_vault_secret_id` directly in inventory when there is no dedicated `[vault]` host in the run.

If `proxmox_vault_skip_verify=false` and no delegated Vault host is present, the playbook can reuse an existing Vault CA file from the first targeted Proxmox node. That makes follow-up renewals possible even from a very small static inventory.

Minimal static inventory example for a cluster certificate rollout:

```ini
[proxmox_cert_nodes]
pve3.example.net ansible_host=192.168.1.241 ansible_user=root
pve4.example.net ansible_host=192.168.1.242 ansible_user=root
pve5.example.net ansible_host=192.168.1.243 ansible_user=root

[proxmox_nodes:children]
proxmox_cert_nodes

[proxmox_cert_nodes:vars]
ansible_python_interpreter=/usr/bin/python3
proxmox_cert_cluster_san=cl0.example.net
proxmox_vault_addr=https://vault.example.net:8200
proxmox_vault_skip_verify=false
proxmox_cert_vault_role_id=<approle-role-id>
proxmox_cert_vault_secret_id=<approle-secret-id>
```

Canary and full-rollout examples:

```bash
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/provision_certificates.yml --limit pve4.example.net
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/provision_certificates.yml --limit proxmox_cert_nodes
```

Keep `proxmox_cert_vault_secret_id` local and rotate it if it is ever copied into logs, terminals, or scratch files.

## Group structure

Common groups used in this repository:

| Group | Purpose |
|-------|---------|
| `dbservers` | Database servers (PostgreSQL, etc.) |
| `vault` | Vault server cluster used for delegated bootstrap tasks |
| `powerdns` | PowerDNS authoritative servers |
| `powerdns-recurse` | PowerDNS recursive resolvers |
| `proxmox_nodes` | Proxmox hypervisor nodes |
| `proxmox_cert_nodes` | Static cert rollout targets for Proxmox API certificates |
| `proxmox_cert_nodes_cl0` | Cluster-specific Proxmox cert targets with a `cl0` SAN |
| `proxmox_cert_nodes_cl1` | Cluster-specific Proxmox cert targets with a `cl1` SAN |
| `domain_controllers_windows` | Windows AD domain controllers managed over WinRM |
| `dhcp_primary_windows` | Primary Windows DHCP server for HA configuration |
| `dhcp_partner_windows` | Partner Windows DHCP server for HA configuration |
| `dhcp_servers_windows` | Combined Windows DHCP HA target group |
| `rdp_servers_windows` | Windows servers whose RDP certificate posture is managed either by listener binding or by the targeted RDP certificate GPO |
| `synology_nas` | Synology NAS hosts |

## Host and group variables

Place host-specific and group-specific variables under `host_vars/` and `group_vars/` when inventory files become noisy:

```text
inventory/
├── host_vars/
│   ├── vault01.example.com.yml
│   ├── pve01.example.com.yml
│   └── ...
└── group_vars/
    ├── vault.yml
    ├── proxmox_nodes.yml
    └── ...
```

Example `host_vars/vault01.example.com.yml`:

```yaml
vault_ui_cert_agent_token_file: /var/lib/vault-agent/token
vault_ui_cert_agent_secret_id_file: /etc/vault.d/agent.d/secret_id
vault_root_token_file: /root/.vault-token
vault_skip_verify: false
vault_addr: "https://{{ inventory_hostname }}:8200"
```

## Credentials management

Simple dynamic-inventory environment variables:

```bash
export PROXMOX_HOST=your-proxmox-host
export PROXMOX_USER=your-user@pam
export PROXMOX_TOKEN_ID=your-token-id
export PROXMOX_TOKEN_SECRET="$(vault read -field=token secret/proxmox_token)"
```

Example Vault lookup inside playbooks:

```yaml
vars:
  proxmox_credentials: "{{ lookup('community.hashi_vault.hashi_vault', 'secret=secret/data/proxmox') }}"
```

## Troubleshooting

### "No hosts matched"

- Check the inventory path you passed to `-i`
- Verify the host or group name exists with `ansible-inventory --graph`
- Confirm `ansible_host` is set when the inventory hostname is not directly reachable

### Proxmox dynamic inventory is not working

- Verify `PROXMOX_HOST`, `PROXMOX_USER`, `PROXMOX_TOKEN_ID`, and `PROXMOX_TOKEN_SECRET`
- Confirm the target Proxmox API certificate is trusted when `validate_certs: true`
- Test the API directly with proxmoxer:

```bash
python -c "from proxmoxer import ProxmoxAPI; prox = ProxmoxAPI('your-proxmox-host', user='your-user@pam', token_name='token-id', token_value='token-secret', verify_ssl=True); print(prox.version.get())"
```

### Proxmox certificate playbook without a `[vault]` host

- Set `proxmox_vault_addr`, `proxmox_cert_vault_role_id`, and `proxmox_cert_vault_secret_id` in inventory
- Leave `proxmox_vault_skip_verify=false` unless you are deliberately testing an insecure path
- Bootstrap one node first if the cluster does not yet have a reusable `vault-ca.pem`

### SSH connection issues

- Verify `ansible_user` matches your SSH user
- Check SSH keys are installed: `ssh-copy-id -i ~/.ssh/id_ed25519.pub user@host`
- Increase verbosity when needed: `ansible all -i inventory -vvv -m ping`

## Security reminders

- Never hardcode passwords in tracked inventory templates
- Never commit real credentials, AppRole secret IDs, or private endpoints you do not intend to publish
- Use SSH keys for authentication and disable password auth where possible
- Rotate credentials regularly, especially Vault tokens and AppRole secret IDs
- Review inventory changes in `git diff` before every commit
