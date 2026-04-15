# Proxmox Backup Server (PBS) Maintenance Playbook

This playbook provides comprehensive maintenance and certificate management for the Proxmox Backup Server at `https://pbs.myrobertson.net:8007/`.

## Overview

The playbook includes:

1. **Vault PKI Bootstrap** - Sets up certificate generation for PBS via Vault (matching Proxmox cl0 pattern)
2. **PBS Maintenance Checks** - Monitors API health, datastores, and backup status
3. **Certificate Renewal** - Automated certificate renewal via Vault AppRole
4. **Backup Pruning** - Optional cleanup of old backups with retention policies

## Usage

### Run Full Maintenance Check

```bash
cd /Users/rich/Documents/GitHub/homelab_ansible
ansible-playbook playbooks/proxmox-backup/maintenance.yml
```

### Bootstrap Vault PKI for PBS Certificates

```bash
# Run on vault host only
ansible-playbook playbooks/proxmox-backup/maintenance.yml \
  --tags=pbs_certs \
  -l vault
```

This will:
- Enable AppRole auth in Vault
- Create PKI role for PBS certificates
- Create policy and AppRole for PBS cert agent
- Output credentials to `/tmp/pbs-approle-creds.env`

### Transfer AppRole Credentials to PBS

After running the Vault bootstrap:

```bash
# From vault host
scp /tmp/pbs-approle-creds.env root@pbs.myrobertson.net:/etc/proxmox-backup/vault-approle.env
```

### Deploy Certificate Renewal Script

Display the certificate renewal script template:

```bash
ansible-playbook playbooks/proxmox-backup/maintenance.yml \
  -e "deploy_cert_script=true"
```

Then:

1. Copy the script to PBS: `/usr/local/sbin/pbs-cert-renewal.sh`
2. Make executable: `chmod 0755 /usr/local/sbin/pbs-cert-renewal.sh`
3. Test: `/usr/local/sbin/pbs-cert-renewal.sh`
4. Set up cron: `0 2 * * * root /usr/local/sbin/pbs-cert-renewal.sh`

### Run Backup Pruning

```bash
ansible-playbook playbooks/proxmox-backup/maintenance.yml \
  -e "prune_backups=true"
```

## Playbook Structure

### Play 1: Bootstrap Vault PKI/AppRole (Vault host)
- Checks Vault root token
- Enables AppRole auth method
- Creates PKI role for PBS API certificates
- Creates policy and AppRole
- Outputs credentials to `/tmp/pbs-approle-creds.env`

**Tags:** `pbs_certs`

### Play 2: Maintenance Checks (localhost)
- Verifies PBS API connectivity
- Authenticates with PBS
- Lists datastores and usage
- Checks system version and health
- Displays certificate renewal instructions
- Warns on disk usage >80%

**Variables:**
- `pbs_user`: PBS API user (default: `maintenance@pam`)
- `pbs_password`: PBS API password (from environment)
- `pbs_verify_ssl`: SSL verification (default: `false`)

### Play 3: Backup Pruning (localhost)
- Displays pruning instructions
- Shows retention policy example

**Variables:**
- `prune_backups`: Set to `true` to display pruning help (default: `false`)
- `prune_retention`: Retention policy format (default: `keep-last=10,keep-daily=7,keep-weekly=4,keep-monthly=12`)

### Play 4: Certificate Renewal Script (localhost)
- Generates script template for manual deployment
- Includes Vault AppRole authentication
- Handles certificate backup and renewal
- Restarts PBS API service

**Variables:**
- `deploy_cert_script`: Set to `true` to display script template (default: `false`)

## Prerequisites

### Vault Side
- Vault instance at `https://vault.myrobertson.net:8200`
- Vault root token - stored in `/root/.vault-token`
- PKI mount: `pki_int`
- Vault CLI available on vault host

### PBS Side
- PBS instance at `https://pbs.myrobertson.net:8007`
- User account with admin privileges (e.g., `maintenance@pam`)
- Directory for certificates: `/etc/proxmox-backup/default`
- jq installed on PBS (for certificate renewal script)

## Certificate Renewal Flow

1. **Manual Setup Phase:**
   - Run Vault bootstrap (`--tags=pbs_certs`)
   - Transfer AppRole credentials to PBS
   - Deploy renewal script to PBS
   - Add cron job for automatic renewal

2. **Automatic Renewal Phase:**
   - Cron triggers renewal script daily at 02:00
   - Script authenticates with Vault using AppRole
   - Requests new certificate from PKI `pbs-api` role
   - Backs up current certificate
   - Updates `/etc/proxmox-backup/default/pbs-api.pem` and `-key.pem`
   - Restarts PBS API service
   - Certificates expire every 720 hours (30 days)

## Monitoring & Alerts

The maintenance playbook can be run periodically via cron or Kubernetes CronJob to:

```bash
# Weekly maintenance check
0 3 * * 0 ansible-playbook /path/to/playbooks/proxmox-backup/maintenance.yml
```

Based on the output, alerts can be sent via:
- Gotify notifications (via systemd services)
- Email (via mail modules)
- Prometheus/Alertmanager (via exporter)

## Troubleshooting

### Certificate Renewal Script Fails

Check the AppRole credentials:

```bash
source /etc/proxmox-backup/vault-approle.env
echo $PBS_VAULT_ROLE_ID
echo $PBS_VAULT_SECRET_ID
```

Verify Vault connectivity:

```bash
curl -k https://vault.myrobertson.net:8200/v1/sys/version
```

Check PBS certificate directories:

```bash
ls -la /etc/proxmox-backup/default/pbs-api*
```

### PBS API Unreachable

```bash
# Check PBS service status
systemctl status proxmox-backup-proxy

# Check API port
netstat -tlnp | grep 8007

# Verify certificate validity
openssl x509 -in /etc/proxmox-backup/default/pbs-api.pem -text
```

## Related Documentation

- [Proxmox Backup Server Docs](https://pbs.proxmox.com/docs/index.html)
- [Vault PKI Engine](https://www.vaultproject.io/docs/secret/pki)
- [AppRole Auth Method](https://www.vaultproject.io/docs/auth/approle)
- [Proxmox API Documentation](https://pbs.proxmox.com/docs/api-viewer/index.html)

## Contributing

When modifying this playbook:

1. Update certificate hosts in corresponding variables
2. Test Vault bootstrap with `--tags=pbs_certs`
3. Validate YAML syntax
4. Test maintenance checks against live PBS instance
5. Update this README with any new plays or variables
