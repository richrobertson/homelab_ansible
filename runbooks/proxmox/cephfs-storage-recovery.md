# CephFS Storage Activation and Mountpoint Recovery

## Symptoms
- Proxmox UI/API error:
  - `unable to activate storage '<id>' - directory '/mnt/pve/<id>' does not exist or is unreachable (500)`
- `pvesm status` prints activation warnings.

## Quick checks
```bash
STORE_ID="kubernetes-prod-cephfs"   # or cephfs
MP="/mnt/pve/${STORE_ID}"

findmnt -T "$MP" || true
pvesm status | grep -E "^${STORE_ID}\\b|Name|Type|Status" || true
```

## Recovery procedure
1. Ensure directory exists if mountpoint is genuinely absent:
```bash
mkdir -p "$MP"
chmod 755 "$MP"
```

2. If directory operations fail with permission errors (stale mountpoint behavior), clear and recreate:
```bash
umount -lf "$MP" 2>/dev/null || true
rm -rf --one-file-system "$MP" 2>/dev/null || true
install -d -m 755 "$MP"
```

3. Refresh storage object and services:
```bash
systemctl restart pvestatd pvedaemon pveproxy
sleep 2
pvesm set "$STORE_ID" --disable 1 || true
sleep 1
pvesm set "$STORE_ID" --disable 0 || true
sleep 2
```

4. Verify mounted + active:
```bash
findmnt -T "$MP" || true
mount | grep "$MP" || true
pvesm status | grep -E "^${STORE_ID}\\b|Name|Type|Status" || true
```

## pveproxy crash after storage/poller restart
If `pveproxy` fails after service restarts:
```bash
systemctl --no-pager --full status pveproxy || true
journalctl -u pveproxy -n 120 --no-pager || true
pvecm updatecerts --force
systemctl reset-failed pveproxy
systemctl restart pvedaemon
systemctl restart pveproxy
systemctl is-active pvedaemon pveproxy
```

## Success criteria
- Storage shows `active` in `pvesm status`.
- Mount exists in `findmnt` for expected path.
- `pveproxy` active and listening on port `8006`.
