# Ceph Dashboard Bind Fix After IP/Network Change

## Symptom
Ceph health shows dashboard module bind failures, for example:
- `MGR_MODULE_ERROR`
- `No socket could be created ... Cannot assign requested address`

## Cause
Dashboard bind address is pinned to an IP that is no longer valid on the active mgr node.

## Fix
Run from any node with Ceph CLI access:

```bash
for daemon in pve3 pve4 pve5; do
  ceph config set "mgr.${daemon}" mgr/dashboard/server_addr 0.0.0.0 || true
done
ceph config set mgr mgr/dashboard/server_addr 0.0.0.0 || true
ceph config rm mgr mgr/dashboard/server_addr 2>/dev/null || true
ceph config set mgr mgr/dashboard/server_addr 0.0.0.0

ceph mgr module disable dashboard || true
ceph mgr module enable dashboard

active="$(ceph mgr dump | sed -n 's/.*"active_name": "\(.*\)".*/\1/p' | head -n1)"
[ -n "$active" ] && ceph mgr fail "$active" || true

ceph crash archive-all || true
ceph -s
ceph health detail | sed -n '1,80p'
```

## Success criteria
- `ceph -s` returns `HEALTH_OK` or at least no dashboard bind error.
- Active mgr rotates successfully and dashboard module remains loaded.
