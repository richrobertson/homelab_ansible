#!/usr/bin/env bash
# Proxmox Cluster Maintenance Scheduler
# Place in /usr/local/bin/proxmox-maintenance-scheduler or similar
# Add to root's crontab for automated execution

set -euo pipefail

REPO_ROOT="/Users/rich/Documents/GitHub/homelab_ansible"
VENV="${REPO_ROOT}/bin"
VAULT_HOST="rich@vault.myrobertson.net"
VAULT_ADDR="https://vault.myrobertson.net:8200"
VAULT_SECRET_PATH="secret/proxmox/cl0/terraform"
LOG_DIR="/var/log/proxmox-maintenance"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Create log directory
mkdir -p "${LOG_DIR}"

# Source Python/Ansible environment
source "${VENV}/activate"

# Load Proxmox credentials from Vault
echo "[${TIMESTAMP}] Loading Proxmox credentials from Vault..." | tee -a "${LOG_DIR}/maintenance_${TIMESTAMP}.log"

json="$(ssh -o BatchMode=yes "${VAULT_HOST}" "
  export VAULT_ADDR=${VAULT_ADDR}
  export VAULT_SKIP_VERIFY=true
  export VAULT_TOKEN=\$(sudo -n cat /root/.vault-token)
  vault kv get -format=json ${VAULT_SECRET_PATH}
" 2>/dev/null)"

eval "$(printf '%s' "$json" | python3 -c '
import json, sys, urllib.parse as up, shlex
d=json.load(sys.stdin)["data"]["data"]
u=d.get("username","")
endpoint=d.get("proxmox_endpoint","")
tok=d.get("api_token","")
p=up.urlparse(endpoint if "://" in endpoint else "https://"+endpoint)
host=(p.netloc or p.path).strip()
user=u
tid=""; tsec=""
if "!" in tok and "=" in tok:
 left,right=tok.split("!",1)
 tid,tsec=right.split("=",1)
 user=left if "@" in left else user
elif "=" in tok:
 tid,tsec=tok.split("=",1)
print("export PROXMOX_URL=" + shlex.quote("https://"+host))
print("export PROXMOX_USER=" + shlex.quote(user))
print("export PROXMOX_TOKEN_ID=" + shlex.quote(tid))
print("export PROXMOX_TOKEN_SECRET=" + shlex.quote(tsec))
' 2>/dev/null)"

# Fetch CA certificate if not present
if [ ! -f /tmp/proxmox-vault-ca.pem ]; then
  ssh -o BatchMode=yes "${VAULT_HOST}" "
    export VAULT_ADDR=${VAULT_ADDR}
    export VAULT_SKIP_VERIFY=true
    export VAULT_TOKEN=\$(sudo -n cat /root/.vault-token)
    vault read -field=certificate pki_int/cert/ca
  " > /tmp/proxmox-vault-ca.pem 2>/dev/null
fi

export REQUESTS_CA_BUNDLE=/tmp/proxmox-vault-ca.pem

# Determine which task to run
case "${1:-phase1}" in
  phase1)
    echo "[${TIMESTAMP}] Running Phase 1 - Weekly Health Checks" | tee -a "${LOG_DIR}/maintenance_${TIMESTAMP}.log"
    "${VENV}/ansible-playbook" \
      -u root \
      -i "${REPO_ROOT}/ansible/proxmox/proxmox.yml" \
      "${REPO_ROOT}/ansible/proxmox/phase1_health_checks.yml" \
      2>&1 | tee -a "${LOG_DIR}/maintenance_${TIMESTAMP}.log"
    ;;
  phase2)
    echo "[${TIMESTAMP}] Running Phase 2 - Monthly Maintenance" | tee -a "${LOG_DIR}/maintenance_${TIMESTAMP}.log"
    "${VENV}/ansible-playbook" \
      -u root \
      -i "${REPO_ROOT}/ansible/proxmox/proxmox.yml" \
      "${REPO_ROOT}/ansible/proxmox/phase2_monthly_maintenance.yml" \
      2>&1 | tee -a "${LOG_DIR}/maintenance_${TIMESTAMP}.log"
    ;;
  certs)
    echo "[${TIMESTAMP}] Running Certificate Renewal" | tee -a "${LOG_DIR}/maintenance_${TIMESTAMP}.log"
    "${VENV}/ansible-playbook" \
      -u root \
      -i "${REPO_ROOT}/ansible/proxmox/proxmox.yml" \
      -e "run_cert_renewal=true" \
      "${REPO_ROOT}/ansible/proxmox/phase2_monthly_maintenance.yml" \
      2>&1 | tee -a "${LOG_DIR}/maintenance_${TIMESTAMP}.log"
    ;;
  *)
    echo "Usage: $0 {phase1|phase2|certs}"
    echo "  phase1 - Run weekly critical health checks (Ceph, HA, Certs)"
    echo "  phase2 - Run monthly maintenance (logs, storage, certs)"
    echo "  certs  - Run certificate renewal only"
    exit 1
    ;;
esac

echo "[${TIMESTAMP}] Maintenance task complete. Log saved to ${LOG_DIR}/maintenance_${TIMESTAMP}.log" | tee -a "${LOG_DIR}/maintenance_${TIMESTAMP}.log"
