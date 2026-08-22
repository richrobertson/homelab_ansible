#!/usr/bin/env bash
# Delete the 17 orphaned CephFS subvolumes identified on 2026-08-22: 12
# superseded predecessors of still-live volumes, and 5 that are empty.
#
# This is PERMANENT. `ceph fs subvolume rm` destroys the data; there is no
# recycle bin for a CephFS subvolume.
#
# The safety here is that the script does not trust the hardcoded list. It
# recomputes which subvolumes are referenced by a live PersistentVolume, and
# refuses to delete anything that is referenced, anything not currently
# orphaned, or the netbootxyz volume that is deliberately being kept.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export VAULT_ADDR="${VAULT_ADDR:-https://vault.myrobertson.net:8200}"
NS=rook-ceph-external
FS=kubernetes-prod-cephfs
GROUP=csi
SCRATCH="$(dirname "$0")"

# Deliberately retained: netboot.xyz config, 27MB, no live counterpart.
KEEP="csi-vol-050ed500-f3c3-479c-868a-1838754c2a2a"

TARGETS="
csi-vol-140c14e1-f0c9-42ff-8f8b-c0ce836fdaae
csi-vol-1610d8c3-3b8b-4252-82bf-2db48e8ccafa
csi-vol-1b7a6a3c-cb1d-40d9-a0cb-4242d1a3072b
csi-vol-2e8d5a4e-19c6-43f3-8ceb-f9429a8bc44c
csi-vol-4e541985-6e64-4ed9-a787-c01ec6ff6316
csi-vol-60d5bd9c-6557-4877-a8e8-600706837e5b
csi-vol-6088af25-88db-4e2a-98b5-70230825ecc6
csi-vol-b20b6ee6-9677-445f-a918-532af0ef200a
csi-vol-c5e74b23-cef3-446f-bed2-6ff6f5485372
csi-vol-ce619ae6-0960-46af-afef-f8404c8e7966
csi-vol-d2cef40f-fced-4b5c-af5b-bd75f01a58c0
csi-vol-de3097bf-7c99-481b-a72f-c31181e769bc
csi-vol-2f994707-3889-4367-9b2e-c68e32af1b8d
csi-vol-add41a41-c65e-43f3-a1f2-a7674a567eb6
csi-vol-c508dfa2-2b2a-4087-a637-6223b5d45464
csi-vol-cd996c5c-40ea-4bf4-b4c7-b2ef31384a35
csi-vol-e19538c2-12bd-47f9-9be4-e4f6ff8060e3
"

TB=$(kubectl --request-timeout=30s -n "$NS" get pod -l app=rook-ceph-tools -o jsonpath='{.items[0].metadata.name}')
[ -n "$TB" ] || { echo "no toolbox pod" >&2; exit 1; }
ADMIN=$(vault kv get -field=userKey secret/proxmox/cl0/ceph/csi-cephfs-secret)

ceph_do() {
  printf '[client.admin]\n\tkey = %s\n' "$ADMIN" | \
  kubectl --request-timeout=180s -n "$NS" exec -i "$TB" -- sh -c "
    cat > /tmp/a.keyring && chmod 600 /tmp/a.keyring; unset CEPH_ARGS
    $1
    rm -f /tmp/a.keyring"
}

echo "=== recomputing which subvolumes are in use, from live PVs ==="
ceph_do 'ceph --name client.admin --keyring /tmp/a.keyring fs subvolume ls '"$FS"' --group_name '"$GROUP"' -f json 2>/dev/null' > "$SCRATCH/_subs.json"
kubectl --request-timeout=60s get pv -o json > "$SCRATCH/_pvs.json"

python3 - "$SCRATCH" "$KEEP" <<'PY' > "$SCRATCH/_orphans.txt"
import json,sys
base=sys.argv[1]; keep=sys.argv[2]
subs={s['name'] for s in json.load(open(base+'/_subs.json'))}
used=set()
for p in json.load(open(base+'/_pvs.json'))['items']:
    csi=p['spec'].get('csi') or {}
    if not csi.get('driver','').endswith('cephfs.csi.ceph.com'): continue
    n=(csi.get('volumeAttributes') or {}).get('subvolumeName')
    if n: used.add(n)
for o in sorted(subs-used):
    print(o)
PY

ORPHANS=$(cat "$SCRATCH/_orphans.txt")
echo "  subvolumes total : $(python3 -c "import json;print(len(json.load(open('$SCRATCH/_subs.json'))))")"
echo "  currently orphan : $(echo "$ORPHANS" | grep -c . || true)"

echo "=== validating the target list ==="
fail=0
for t in $TARGETS; do
  if [ "$t" = "$KEEP" ]; then echo "  REFUSING: $t is the retained netbootxyz volume"; fail=1; continue; fi
  if ! echo "$ORPHANS" | grep -qx "$t"; then
    echo "  REFUSING: $t is NOT currently orphaned (a PV references it, or it is gone)"; fail=1
  fi
done
[ "$fail" -eq 0 ] || { echo "ABORTING, nothing deleted." >&2; exit 1; }
echo "  all $(echo "$TARGETS" | grep -c .) targets confirmed orphaned; $KEEP excluded"

echo "=== deleting ==="
for t in $TARGETS; do
  out=$(ceph_do "ceph --name client.admin --keyring /tmp/a.keyring fs subvolume rm $FS $t --group_name $GROUP 2>&1" || true)
  if [ -z "$out" ]; then echo "  removed $t"; else echo "  $t -> $out"; fi
done

echo "=== after ==="
ceph_do 'ceph --name client.admin --keyring /tmp/a.keyring fs subvolume ls '"$FS"' --group_name '"$GROUP"' -f json 2>/dev/null' > "$SCRATCH/_subs_after.json"
python3 -c "
import json
a=[s['name'] for s in json.load(open('$SCRATCH/_subs_after.json'))]
print('  subvolumes now:', len(a))
print('  netbootxyz retained:', '$KEEP' in a)
"
ceph_do 'ceph --name client.admin --keyring /tmp/a.keyring health 2>&1'
unset ADMIN
rm -f "$SCRATCH"/_subs.json "$SCRATCH"/_pvs.json "$SCRATCH"/_subs_after.json "$SCRATCH"/_orphans.txt
