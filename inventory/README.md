# Inventory Setup Guide

This directory contains Ansible inventory configurations for different environments.

## Quick Start

1. **Copy the example files** for your environment:
   ```bash
   mkdir -p environments

   # For production
   cp production.ini.example environments/production.ini
   
   # For staging
   cp staging.ini.example environments/staging.ini
   
   # For Proxmox dynamic inventory
   cp proxmox.yml.example proxmox.yml
   ```

2. **Update with your infrastructure details**:
   - Replace `example.com` hostnames with your actual FQDNs or IPs
   - Update vault_addr with your Vault server location
   - Adjust ansible_user and other connection parameters

3. **Never commit sensitive files**:
   - Treat concrete environment inventory data as sensitive
   - Keep hostnames/IPs/credentials for real infrastructure out of version control
   - Only commit `*.example` inventory templates
   - Keep `environments/*.ini` and `proxmox.yml` local and ignored by git
   - Use `*.example` files as templates only

## Environment Structure

### Production
- **File**: `environments/production.ini`
- **Purpose**: Production infrastructure
- **Note**: Keep this file local and never commit (ignored by `.gitignore`)

### Staging  
- **File**: `environments/staging.ini`
- **Purpose**: Testing and non-production deployments
- **Note**: Keep this file local and never commit (ignored by `.gitignore`)

### Dynamic Inventory (Proxmox)
- **File**: `proxmox.yml`
- **Purpose**: Dynamically discover VMs/LXC containers from Proxmox
- **Note**: Keep this file local and never commit (ignored by `.gitignore`)
- **Docs**: https://docs.ansible.com/ansible/latest/collections/community/proxmox/proxmox_inventory.html

## Credentials Management

### Option 1: Environment Variables (Simple)
```bash
export PROXMOX_HOST=your-proxmox-host
export PROXMOX_USER=your-user@pam
export PROXMOX_TOKEN_ID=your-token-id
export PROXMOX_TOKEN_SECRET=$(vault read -field=token secret/proxmox_token)
```

### Option 2: Vault Integration (Recommended)
Use Vault to store and retrieve credentials dynamically:

```yaml
# In your playbook
vars:
  proxmox_credentials: "{{ lookup('community.hashi_vault.hashi_vault', 'secret=secret/data/proxmox') }}"
```

### Option 3: Vault Agent (Advanced)
For long-running processes, use Vault Agent to transparently inject credentials.

## Group Structure

Common groups used in this repository:

| Group | Purpose |
|-------|---------|
| `dbservers` | Database servers (PostgreSQL, etc.) |
| `vault` | Vault server cluster |
| `powerdns` | PowerDNS authoritative servers |
| `powerdns-recurse` | PowerDNS recursive resolvers |
| `proxmox_nodes` | Proxmox hypervisor nodes |

## Host Variables

Place host-specific variables in `host_vars/`:
```
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

### Example host_vars (vault01.example.com.yml):
```yaml
# Vault-specific configuration
vault_ui_cert_agent_token_file: /var/lib/vault-agent/token
vault_ui_cert_agent_secret_id_file: /etc/vault.d/agent.d/secret_id
vault_root_token_file: /root/.vault-token

# TLS settings
vault_skip_verify: false
vault_addr: "https://{{ inventory_hostname }}:8200"
```

## Best Practices

1. **Use FQDNs** for hostnames when possible (easier for certificate validation)
2. **Organize by environment** (production, staging, testing)
3. **Use Vault** for all secrets (passwords, tokens, keys)
4. **Document group membership** in comments
5. **Test inventory** with `ansible-inventory --graph`:
   ```bash
   ansible-inventory -i environments/staging.ini --graph
   ```

## Troubleshooting

### "No hosts matched"
- Check hostname/IP is correct
- Verify host is reachable: `ping hostname` or `ssh hostname`
- Check `ansible_host` override if using complex networking

### Dynamic inventory not working
- Verify Proxmox credentials in environment variables
- Check PROXMOX_HOST, PROXMOX_USER, PROXMOX_TOKEN_ID/SECRET are set
- Test Proxmox API with a simple proxmoxer one-liner:
   ```bash
   python -c "from proxmoxer import ProxmoxAPI; prox = ProxmoxAPI('your-proxmox-host', user='your-user@pam', token_name='token-id', token_value='token-secret', verify_ssl=True); print(prox.version.get())"
   ```
   See: https://github.com/proxmoxer/proxmoxer#usage

### SSH connection issues
- Verify `ansible_user` matches your SSH user
- Check SSH keys are installed: `ssh-copy-id -i ~/.ssh/id_ed25519.pub user@host`
- Enable verbose output: `ansible all -i inventory -vvv -m ping`

## Security Reminders

- **Never hardcode passwords** in inventory files
- **Don't commit actual credentials** - use examples and environment vars instead
- **Use SSH keys** for authentication (disable password auth)
- **Rotate credentials regularly**, especially Vault tokens
- **Review inventory changes** in git diffs - ensure no secrets leak
