# Vault Connectivity and Proxmox Cert-Agent Recovery

## Symptoms
- Proxmox certificate agent service is active but repeatedly fails authentication:
  - `vault-agent-proxmox-pveproxy`
  - `error authenticating ... dial tcp <vault_ip>:8200: connect: no route to host`
- Proxmox nodes cannot reach Vault API endpoint (`https://vault.myrobertson.net:8200`).
- Certificate renewals stop progressing.

## Typical root cause in this environment
- Vault runs as VM `119` (`vault`) on `pve3`.
- Vault VM NIC is attached to `vmbr1` with VLAN tag `7`.
- `vmbr1` on one or more nodes lost VLAN trunk allowance (`bridge-vids 2-4094`) after network changes, isolating VLAN-tagged guests.
- Vault VM may also boot without an IP on `ens18` after disruption.

## Quick triage
### 1) Confirm cert-agent failures on Proxmox nodes
```bash
for n in 192.168.1.241 192.168.1.242 192.168.1.243; do
  echo "=== $n ==="
  ssh root@$n 'systemctl is-active vault-agent-proxmox-pveproxy; journalctl -u vault-agent-proxmox-pveproxy -n 40 --no-pager | grep -E "error authenticating|authentication successful|no route to host" || true'
done
```

### 2) Identify Vault workload location
```bash
ssh root@192.168.1.242 'pvesh get /cluster/resources --type vm --output-format json | grep -Ei "vault|\"vmid\":119" -n'
```

### 3) Check Vault VM network attachment and reachability from host
```bash
ssh root@192.168.1.241 '
  qm config 119 | grep -E "^name:|^net0:";
  bridge vlan show | egrep "vmbr1|enp1s0f0np0|tap119i0|veth113i0";
  ping -c 2 -W 1 192.168.7.128 >/dev/null && echo ping-ok || echo ping-fail
'
```

### 4) Check guest NIC/IP using QEMU Guest Agent
```bash
ssh root@192.168.1.241 'qm guest cmd 119 network-get-interfaces'
```
If guest only shows `lo` with IP and no IPv4 on `ens18`, Vault VM networking is not up.

## Recovery procedure
### A) Restore vmbr1 VLAN trunk allowance where missing
On each affected node (`pve3`, `pve4`):
```bash
cp -a /etc/network/interfaces /etc/network/interfaces.bak.$(date +%Y%m%d%H%M%S)

# Ensure vmbr1 has both:
#   bridge-vlan-aware yes
#   bridge-vids 2-4094

ifreload -a
```

Immediate live mitigation (before/while reloading):
```bash
bridge vlan add dev enp1s0f0np0 vid 7 2>/dev/null || true
bridge vlan add dev vmbr1 vid 7 self 2>/dev/null || true
```

### B) Recover Vault VM networking
If Vault guest still has no IP:
```bash
# on pve3
qm reboot 119 || qm reset 119
sleep 20
qm guest cmd 119 network-get-interfaces
ping -c 2 -W 1 192.168.7.128 >/dev/null && echo vault-ping-ok || echo vault-ping-fail
```

### C) Re-establish cert-agent authentication
```bash
for n in 192.168.1.241 192.168.1.242 192.168.1.243; do
  ssh root@$n 'systemctl restart vault-agent-proxmox-pveproxy; sleep 2; systemctl is-active vault-agent-proxmox-pveproxy; journalctl -u vault-agent-proxmox-pveproxy -n 20 --no-pager | grep -E "authentication successful|error authenticating" || true'
done
```

### D) Validate Vault API health from a Proxmox node
```bash
ssh root@192.168.1.242 'curl -k -sS -o /tmp/vault_health.json -w "%{http_code}\n" https://vault.myrobertson.net:8200/v1/sys/health && cat /tmp/vault_health.json'
```
Expected:
- HTTP status `200`
- JSON contains `"initialized":true` and `"sealed":false`

## Success criteria
- `vault-agent-proxmox-pveproxy` is `active` on all Proxmox nodes.
- Recent logs show `authentication successful` and token renewal.
- Vault health endpoint responds with HTTP `200` and `sealed:false`.
- Vault VM is reachable on `192.168.7.128:8200` from Proxmox nodes.

## Prevention
- Keep `bridge-vids 2-4094` explicitly defined on `vmbr1` in automation for all cluster nodes using tagged VM/LXC traffic.
- After any network migration or bridge change, always run:
  - Vault reachability check (`curl /v1/sys/health`)
  - Vault-agent auth check (`journalctl -u vault-agent-proxmox-pveproxy`)
  - Tagged workload reachability checks for critical VLANs.
