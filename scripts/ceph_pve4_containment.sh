#!/usr/bin/env bash
set -euo pipefail

PVE3_HOST="${PVE3_HOST:-192.168.11.3}"
PVE4_HOST="${PVE4_HOST:-192.168.11.4}"
PVE5_HOST="${PVE5_HOST:-192.168.11.5}"
SSH_USER="${SSH_USER:-root}"
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10}"
declare -a SSH_ARGS=()
MODE="snapshot"
INTERVAL="30"
ITERATIONS="10"

usage() {
  cat <<'EOF'
Usage:
  ceph_pve4_containment.sh [--snapshot] [--monitor --interval <sec> --iterations <n>]

What it does:
  - Runs read-only Ceph cluster safety checks from pve3 and pve5.
  - Runs read-only pve4 failure diagnostics via hop host (pve5 -> pve4).
  - Prints recommended containment actions (does NOT execute any state changes).

Examples:
  scripts/ceph_pve4_containment.sh --snapshot
  scripts/ceph_pve4_containment.sh --monitor --interval 20 --iterations 15

Environment overrides:
  PVE3_HOST, PVE4_HOST, PVE5_HOST, SSH_USER, SSH_OPTS
EOF
}

log() {
  printf "\n[%s] %s\n" "$(date +'%Y-%m-%d %H:%M:%S')" "$*"
}

warn_insecure_ssh_opts() {
  if [[ "${SSH_OPTS}" == *"StrictHostKeyChecking=no"* ]] || [[ "${SSH_OPTS}" == *"UserKnownHostsFile=/dev/null"* ]]; then
    >&2 echo "WARNING: SSH host key checking is disabled by SSH_OPTS ('${SSH_OPTS}')."
    >&2 echo "WARNING: This is insecure and may expose you to man-in-the-middle attacks."
    >&2 echo "WARNING: Set SSH_OPTS with verified host keys to enable strict host identity checks."
  fi
}

init_ssh_args() {
  SSH_ARGS=()
  if [[ -n "${SSH_OPTS}" ]]; then
    read -r -a SSH_ARGS <<< "${SSH_OPTS}"
  fi
}

ssh_run() {
  local host="$1"
  local cmd="$2"
  ssh "${SSH_ARGS[@]}" "${SSH_USER}@${host}" "${cmd}"
}

cluster_gate_one_node() {
  local host="$1"
  log "Cluster safety gate from ${host}"
  ssh_run "${host}" "hostname; ceph -s; ceph health detail; ceph mgr stat; ceph pg stat"
}

cluster_gate_with_fallback() {
  if cluster_gate_one_node "${PVE3_HOST}"; then
    return 0
  fi
  log "Warning: safety gate failed on ${PVE3_HOST}, trying ${PVE5_HOST}"
  cluster_gate_one_node "${PVE5_HOST}"
}

pve4_diagnostics_via_pve5() {
  log "pve4 read-only diagnostics via pve5"
  ssh "${SSH_ARGS[@]}" -J "${SSH_USER}@${PVE5_HOST}" "${SSH_USER}@${PVE4_HOST}" '
    hostname
    echo ===== service states =====
    systemctl is-active ceph-mgr@pve4 pve-ha-crm pve-cluster corosync || true
    echo ===== failed unit details =====
    systemctl status ceph-mgr@pve4 pve-ha-crm --no-pager -n 120 || true
    echo ===== fault signature =====
    journalctl -k --since "48 hours ago" --no-pager | grep -Ei "segfault|general protection|machine check|mce|edac|hardware error" | tail -n 120 || true
    echo ===== runtime quick check =====
    python3 --version || true
    perl -v | head -n 2 || true
    ceph -s 2>&1 | head -n 30 || true
    pveversion -v 2>&1 | head -n 30 || true
  '
}

print_containment_actions() {
  cat <<'EOF'

===== Recommended containment actions (manual; not executed) =====
1) Freeze placement on pve4 in Proxmox UI (avoid new workloads there).
2) Keep Ceph control plane on pve3/pve5; do not restart cluster-wide Ceph.
3) Keep running read-only safety checks from pve3 or pve5:
     ceph -s
     ceph pg stat
4) Plan maintenance window for pve4 hardware diagnostics (RAM/CPU/firmware).

Stop conditions:
- If PGs leave active+clean, OSD count drops, or mon quorum shrinks, stop all non-essential actions.
EOF
}

monitor_loop() {
  local i
  for ((i=1; i<=ITERATIONS; i++)); do
    log "Monitor iteration ${i}/${ITERATIONS}"
    if ! ssh_run "${PVE3_HOST}" "ceph -s; ceph pg stat"; then
      log "Warning: failed to run monitor check on ${PVE3_HOST}"
    fi
    sleep "${INTERVAL}"
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot)
      MODE="snapshot"
      shift
      ;;
    --monitor)
      MODE="monitor"
      shift
      ;;
    --interval)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "Error: --interval requires a positive integer argument." >&2
        usage
        exit 1
      fi
      if ! [[ "${2}" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: --interval must be a positive integer (seconds), got '${2}'." >&2
        usage
        exit 1
      fi
      INTERVAL="${2}"
      shift 2
      ;;
    --iterations)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "Error: --iterations requires a positive integer argument." >&2
        usage
        exit 1
      fi
      if ! [[ "${2}" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: --iterations must be a positive integer, got '${2}'." >&2
        usage
        exit 1
      fi
      ITERATIONS="${2}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

log "Starting ceph_pve4_containment (${MODE})"
warn_insecure_ssh_opts
init_ssh_args
if ! cluster_gate_with_fallback; then
  log "Error: unable to run cluster safety gate on either ${PVE3_HOST} or ${PVE5_HOST}"
  exit 1
fi

if ! cluster_gate_one_node "${PVE5_HOST}"; then
  log "Warning: secondary safety gate on ${PVE5_HOST} failed (continuing with available data)"
fi

if ! pve4_diagnostics_via_pve5; then
  log "Warning: diagnostics via ${PVE5_HOST} failed; continuing to containment recommendations"
fi
print_containment_actions

if [[ "${MODE}" == "monitor" ]]; then
  monitor_loop
fi

log "Completed"