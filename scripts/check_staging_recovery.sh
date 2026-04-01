#!/usr/bin/env bash
set -euo pipefail

tmp_nc_out="$(mktemp)"
cleanup() {
  rm -f "$tmp_nc_out"
}
trap cleanup EXIT

echo "RUN_AT=$(date -u)"

echo "== Proxmox host reachability =="
for host in 192.168.11.4 192.168.11.5; do
  printf "%s ssh=" "$host"
  if ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@"$host" 'echo OK' >/dev/null 2>&1; then
    echo OK
  else
    echo FAIL
  fi
done

echo
echo "== Staging API/Talos ports =="
for ip in 10.20.0.2 10.20.1.2 10.20.2.2 10.21.2.2; do
  printf "%s:6443 " "$ip"
  nc -vz -w 3 "$ip" 6443 >"$tmp_nc_out" 2>&1 || true
  tail -1 "$tmp_nc_out" || true
  printf "%s:50000 " "$ip"
  nc -vz -w 3 "$ip" 50000 >"$tmp_nc_out" 2>&1 || true
  tail -1 "$tmp_nc_out" || true
done

echo
echo "== Kubernetes nodes =="
if ! kubectl get nodes -o wide; then
  echo "kubectl get nodes failed; continuing with Talos checks" >&2
fi

echo
echo "== Talos checks =="
talosctl --nodes 10.20.2.2 --endpoints 10.20.0.2 health || true
talosctl --nodes 10.21.2.2 --endpoints 10.21.2.2 get machinestatus || true
talosctl --nodes 10.21.2.2 --endpoints 10.21.2.2 service kubelet || true

echo
echo "DONE"
