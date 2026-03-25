# SFP28 Interface Cutover (cl0)

## Goal
Migrate node network config from legacy SFP28 interfaces to replacement interfaces with minimal outage and fast rollback visibility.

## Interface mapping
- Old: `enp3s0f0np0`, `enp3s0f1np1`
- New: `enp1s0f0np0`, `enp1s0f1np1`

## Preconditions
- Workloads drained from target node (or acceptable maintenance window).
- HA-affinity temporarily adjusted if services are pinned to the node.
- Confirm Ceph and quorum healthy before maintenance.

## Procedure (per node)
1. Apply prepared network config changes (Ansible playbook or reviewed manual edit).
2. Confirm config expects new NICs:
   - `vmbr0` path uses `enp1s0f0np0.88`
   - Ceph VLAN path uses `enp1s0f1np1.10`
3. Perform physical cable move old NIC ports -> new NIC ports.
4. Reload networking:
   - `ifreload -a` (or `systemctl restart networking` if needed)
5. Validate connectivity:
   - `ping -c 3 192.168.88.1`
   - `ping -c 3 192.168.10.1`
6. Validate interface state:
   - `ip -br link show | egrep '^(enp1s0f0np0|enp1s0f1np1|enp3s0f0np0|enp3s0f1np1|vmbr0)\b'`
   - `ip -br addr show | egrep '^(vmbr0|enp1s0f0np0\.88|enp1s0f1np1\.10)\b'`

## Success criteria
- New NICs show `UP/LOWER_UP`.
- Old NICs show `NO-CARRIER`.
- `vmbr0` and VLAN interfaces are up with expected addresses.
- Ceph and quorum remain healthy.

## Post-checks
- `ceph -s`
- `pvecm status`
- `pvesm status`
- If certificate automation or Vault-backed secrets fail after cutover, run:
   - [`vault-connectivity-and-cert-agent-recovery.md`](./vault-connectivity-and-cert-agent-recovery.md)
