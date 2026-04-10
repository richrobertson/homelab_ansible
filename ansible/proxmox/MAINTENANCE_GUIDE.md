# Proxmox Cluster Maintenance - Phase 1 & 2 Guide

This guide covers the automated maintenance playbooks for your Proxmox cl0 cluster, including scheduling and alerting.

## Phase 1: Weekly Critical Health Checks

Run **every Monday morning** during maintenance window.

### Included Checks:
1. **Ceph Cluster Health** (`ceph_health_check.yml`)
   - Cluster status and quorum
   - OSD health and utilization
   - PG state verification
   - Monitor status

2. **HA Cluster Verification** (`ha_cluster_verification.yml`)
   - Cluster node count and quorum
   - Corosync status
   - HA daemon health (pve-ha-lrm, pve-ha-crm)
   - HA resource states

3. **Certificate Expiry Check** (`certificate_expiry_check.yml`)
   - Proxmox API certificate expiry (30-day warning, 7-day critical)
   - Root CA certificate status
   - Vault connectivity verification

### Run Phase 1 Manually:

```bash
# Source environment with credentials
source /tmp/proxmox_cl0_env.sh
export REQUESTS_CA_BUNDLE=/tmp/proxmox-vault-ca.pem

# Run from proxmox ansible directory
cd ansible/proxmox

# Run all Phase 1 checks
ansible-playbook -u root -i proxmox.yml phase1_health_checks.yml

# Run individual check
ansible-playbook -u root -i proxmox.yml ceph_health_check.yml
ansible-playbook -u root -i proxmox.yml ha_cluster_verification.yml
ansible-playbook -u root -i proxmox.yml certificate_expiry_check.yml
```

### Expected Output:

**Ceph Health Check:**
- Cluster status (should be HEALTH_OK or HEALTH_WARN)
- OSD count and distribution
- PG states (look for active+clean majority)
- Monitor quorum status

**HA Verification:**
- Cluster node count (minimum 3 recommended)
- Corosync quorum status
- HA service states (pve-ha-lrm and pve-ha-crm should be active)
- Protected resource states

**Certificate Check:**
- Days to cert expiry (should be > 30 days)
- All certificate files present
- Vault connectivity status

---

## Phase 2: Monthly Maintenance Tasks

Run **first Monday of each month** for comprehensive maintenance.

### Included Tasks:

1. **Log Maintenance** (`log_maintenance.yml`)
   - Compress logs older than 30 days
   - Archive compressed logs after 90 days
   - Delete logs older than 365 days
   - Clean up Proxmox task logs
   - Vacuum journal logs
   - Monitor /var/log disk usage

2. **Storage Utilization Report** (`storage_utilization_report.yml`)
   - Datastore usage (warn at 80%, critical at 90%)
   - VM snapshot inventory
   - Ceph OSD balance and utilization
   - Thin-provisioned disk usage

3. **Certificate Renewal** (Optional, triggered with `-e run_cert_renewal=true`)
   - Runs `provision_certificates.yml` to renew certs from Vault
   - Updates Vault Agent configuration
   - Restarts pveproxy with new certificates

### Run Phase 2 Manually:

```bash
# Source environment with credentials
source /tmp/proxmox_cl0_env.sh
export REQUESTS_CA_BUNDLE=/tmp/proxmox-vault-ca.pem

cd ansible/proxmox

# Run all Phase 2 tasks (no cert renewal)
ansible-playbook -u root -i proxmox.yml phase2_monthly_maintenance.yml

# Run Phase 2 with certificate renewal
ansible-playbook -u root -i proxmox.yml -e run_cert_renewal=true phase2_monthly_maintenance.yml

# Run individual tasks
ansible-playbook -u root -i proxmox.yml log_maintenance.yml
ansible-playbook -u root -i proxmox.yml storage_utilization_report.yml
```

### Expected Output:

**Log Maintenance:**
- Number of logs compressed/archived/deleted
- Disk space freed
- /var/log usage percentage

**Storage Report:**
- Per-datastore usage percentages
- VM snapshot counts
- Ceph OSD utilization variance (< 10% is good)
- High usage warnings

---

## Automated Scheduling

### Option 1: Cron Jobs (Recommended for simple scheduling)

Add to root's crontab:

```bash
# Weekly Phase 1 checks (Monday 2 AM)
0 2 * * 1 /Users/rich/Documents/GitHub/homelab_ansible/scripts/proxmox-maintenance-scheduler.sh phase1 >> /var/log/proxmox-maintenance/cron.log 2>&1

# Monthly Phase 2 maintenance (First Monday of month at 3 AM)
0 3 1-7 * * [ $(date +\%w) = 1 ] && /Users/rich/Documents/GitHub/homelab_ansible/scripts/proxmox-maintenance-scheduler.sh phase2 >> /var/log/proxmox-maintenance/cron.log 2>&1

# Monthly certificate renewal (First day of month at 4 AM - optional)
# 0 4 1 * * /Users/rich/Documents/GitHub/homelab_ansible/scripts/proxmox-maintenance-scheduler.sh certs >> /var/log/proxmox-maintenance/cron.log 2>&1
```

Edit with: `sudo crontab -e`

### Option 2: Systemd Timer (More robust, recommended for production)

Create `/etc/systemd/system/proxmox-health-check.service`:

```ini
[Unit]
Description=Proxmox Phase 1 Health Checks
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/Users/rich/Documents/GitHub/homelab_ansible/scripts/proxmox-maintenance-scheduler.sh phase1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=proxmox-health-check
```

Create `/etc/systemd/system/proxmox-health-check.timer`:

```ini
[Unit]
Description=Proxmox Phase 1 Health Checks Weekly
Requires=proxmox-health-check.service

[Timer]
OnCalendar=Mon *-*-* 02:00:00
Persistent=true
Unit=proxmox-health-check.service

[Install]
WantedBy=timers.target
```

Similar files for Phase 2 (monthly) and cert renewal (monthly).

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable proxmox-health-check.timer
sudo systemctl start proxmox-health-check.timer

# Check status
sudo systemctl status proxmox-health-check.timer
sudo journalctl -u proxmox-health-check.service -f  # Follow logs
```

---

## Alerting & Notifications

### Critical Conditions to Alert On:

**Phase 1:**
- ❌ Ceph health status = HEALTH_ERR
- ❌ OSD down or out
- ❌ Monitor quorum lost
- ❌ PG state != active+clean for extended period
- ❌ HA cluster quorum loss
- ❌ Certificate expiring in < 7 days
- ❌ File not found for required certs

**Phase 2:**
- ❌ Storage > 90% full (critical)
- ❌ Storage > 80% full (warning)
- ❌ OSD utilization variance > 10%
- ❌ Snapshot count > 20 per VM

### Integration with Monitoring (Future):

The playbook output can be parsed and sent to monitoring systems:

```bash
# Example: Parse for FAIL/CRITICAL and alert via webhook
phaseX_output=$(ansible-playbook ...)
if echo "$phaseX_output" | grep -iE "CRITICAL|FAILED|ERROR" > /dev/null; then
  curl -X POST https://alerts.example.com/proxmox \
    -H "Content-Type: application/json" \
    -d "{\"status\": \"critical\", \"output\": \"$phaseX_output\"}"
fi
```

---

## Manual Credential Setup (if automated loading fails)

If the scheduler script cannot load credentials automatically:

```bash
# 1. SSH to Vault host and get credentials
ssh rich@vault.myrobertson.net

# 2. On vault host, export credentials
export VAULT_ADDR=https://vault.myrobertson.net:8200
export VAULT_SKIP_VERIFY=true
export VAULT_TOKEN=$(cat /root/.vault-token)

# 3. Retrieve cl0 Proxmox credentials
vault kv get -field=username secret/proxmox/cl0/terraform
vault kv get -field=proxmox_endpoint secret/proxmox/cl0/terraform
vault kv get -field=api_token secret/proxmox/cl0/terraform

# 4. Export required env vars locally
export PROXMOX_URL="https://cl0.myrobertson.net:8006"
export PROXMOX_USER="terraform-prov@pve"
export PROXMOX_TOKEN_ID="<token-id-from-vault>"
export PROXMOX_TOKEN_SECRET="<token-secret-from-vault>"
export REQUESTS_CA_BUNDLE=/tmp/proxmox-vault-ca.pem

# 5. Fetch CA cert
ssh rich@vault.myrobertson.net "
  export VAULT_ADDR=https://vault.myrobertson.net:8200
  export VAULT_SKIP_VERIFY=true
  export VAULT_TOKEN=\$(sudo -n cat /root/.vault-token)
  vault read -field=certificate pki_int/cert/ca
" > /tmp/proxmox-vault-ca.pem

# 6. Run playbooks
ansible-playbook -u root -i ansible/proxmox/proxmox.yml ansible/proxmox/phase1_health_checks.yml
```

---

## Troubleshooting

### "Unable to parse /opt/.../proxmox.yml as inventory source"

This means Proxmox API credentials are missing or invalid. Check:

```bash
# 1. Verify environment variables are set
env | grep PROXMOX_

# 2. Test connectivity manually
curl -k -H "Authorization: PVEAPIToken=terraform-prov@pve!<token-id>=<token-secret>" \
  https://cl0.myrobertson.net:8006/api2/json/nodes
```

### "Certificate verify failed"

REQUESTS_CA_BUNDLE not set or CA cert not available:

```bash
# Ensure CA cert is in place
ls -la /tmp/proxmox-vault-ca.pem

# Verify export
echo $REQUESTS_CA_BUNDLE
# Should output: /tmp/proxmox-vault-ca.pem
```

### "ceph status" returns error

Likely running on non-Ceph node or Ceph not initialized. The check is safe to run on all nodes (graceful failure expected on non-Ceph nodes).

### Scheduler script permission denied

Make sure script is executable:

```bash
chmod +x /Users/rich/Documents/GitHub/homelab_ansible/scripts/proxmox-maintenance-scheduler.sh
```

---

## Maintenance Window Best Practices

1. **Schedule During Low-Traffic Hours** (e.g., 2-4 AM)
2. **Never Run Multiple Checks Simultaneously** (serial: 1 enforced)
3. **Monitor Output in Real-Time** (use `-vv` flag for verbose output)
4. **Have Runbook Ready** in case critical issues are discovered
5. **Document Any Manual Interventions** in cluster journal
6. **Review Logs Post-Execution** for missed alerts

---

## Quick Reference Commands

```bash
# Activate your environment
cd /Users/rich/Documents/GitHub/homelab_ansible
source bin/activate

# Load credentials
source /tmp/proxmox_cl0_env.sh
export REQUESTS_CA_BUNDLE=/tmp/proxmox-vault-ca.pem

# Run specific check
ansible-playbook -u root -i ansible/proxmox/proxmox.yml \
  ansible/proxmox/ceph_health_check.yml --limit pve3

# List all nodes before check
ansible-inventory -i ansible/proxmox/proxmox.yml --graph | grep "@proxmox_nodes" -A10

# Tail maintenance logs
tail -f /var/log/proxmox-maintenance/maintenance_*.log

# Check crontab
sudo crontab -l | grep proxmox

# Check systemd timers
systemctl list-timers | grep proxmox
```

---

## Next Steps (Phase 3+)

- [ ] Integrate output with Prometheus/Grafana for graphing trends
- [ ] Add Slack/PagerDuty alerts for critical conditions
- [ ] Create email summaries of health status
- [ ] Implement disaster recovery testing playbook
- [ ] Add quarterly hardware health checks

For questions, review the individual playbook comments or check the Proxmox runbooks:
- `runbooks/proxmox/vault-connectivity-and-cert-agent-recovery.md`
- `runbooks/proxmox/ceph-dashboard-bind-fix.md`
- `runbooks/proxmox/ha-rule-and-cli-recovery.md`
