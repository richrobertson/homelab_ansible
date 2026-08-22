#!/usr/bin/env bash
# Delete orphaned CephFS subvolumes.
#
# READ THIS FIRST.
#
# On 2026-08-22 an earlier version of this script deleted 16 live, Bound staging
# volumes -- roughly 76 GiB across bitwarden, plex, nextcloud, immich, prowlarr,
# radarr, tautulli, trilium, vikunja and two nextcloud html volumes. None had a
# ReplicationSource. The data is unrecoverable.
#
# It happened because prod and staging share ONE CephFS filesystem and ONE
# subvolume group, and the script computed "orphaned" as "not referenced by a
# PersistentVolume" while reading only ONE cluster. Its safety check recomputed
# the orphan set from live PVs rather than trusting a hardcoded list, which
# looked rigorous and was worthless: it could only ever see the cluster in the
# current kubeconfig context.
#
# So this script now refuses to run unless it is given EVERY cluster context
# that mounts the filesystem, and it unions their PV lists before deciding
# anything. A subvolume is a deletion candidate only if NO cluster references
# it.
#
#   usage: delete-orphan-cephfs-subvolumes.sh --contexts ctx1,ctx2 [--apply] <subvol>...
#
# Without --apply it only reports. Nothing is deleted without both --apply and
# every named context answering.
set -euo pipefail

CONTEXTS=""
APPLY=0
TARGETS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --contexts) CONTEXTS="$2"; shift 2 ;;
    --apply)    APPLY=1; shift ;;
    -*)         echo "unknown flag: $1" >&2; exit 2 ;;
    *)          TARGETS+=("$1"); shift ;;
  esac
done

[ -n "$CONTEXTS" ] || {
  echo "REFUSING: --contexts is required, listing every cluster on the filesystem." >&2
  echo "  e.g. --contexts admin@prod,admin@staging" >&2
  exit 2
}

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export VAULT_ADDR="${VAULT_ADDR:-https://vault.myrobertson.net:8200}"
NS=rook-ceph-external
FS="${CEPHFS_NAME:-kubernetes-prod-cephfs}"
GROUP="${CEPHFS_GROUP:-csi}"
SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT

TB=$(kubectl --request-timeout=30s -n "$NS" get pod -l app=rook-ceph-tools -o jsonpath='{.items[0].metadata.name}')
[ -n "$TB" ] || { echo "no toolbox pod" >&2; exit 1; }
ADMIN=$(vault kv get -field=key secret/proxmox/cl0/ceph/client-admin)

ceph_do() {
  printf '[client.admin]\n\tkey = %s\n' "$ADMIN" | \
  kubectl --request-timeout=180s -n "$NS" exec -i "$TB" -- sh -c "
    cat > /tmp/a.keyring && chmod 600 /tmp/a.keyring; unset CEPH_ARGS
    $1
    rm -f /tmp/a.keyring"
}

echo "=== subvolumes in ${FS}/${GROUP} ==="
ceph_do "ceph --name client.admin --keyring /tmp/a.keyring fs subvolume ls $FS --group_name $GROUP -f json 2>/dev/null" \
  > "$SCRATCH/subs.json"
python3 -c "import json;print('  total:', len(json.load(open('$SCRATCH/subs.json'))))"

# Union the PV lists. A failure to read ANY context is fatal: a missing cluster
# is exactly how live volumes get classified as orphans.
echo "=== reading PersistentVolumes from every named cluster ==="
: > "$SCRATCH/referenced.txt"
IFS=',' read -r -a CTXS <<< "$CONTEXTS"
for ctx in "${CTXS[@]}"; do
  if ! kubectl --context "$ctx" --request-timeout=60s get pv -o json > "$SCRATCH/pv-$ctx.json" 2>/dev/null; then
    echo "REFUSING: could not read PersistentVolumes from context '$ctx'." >&2
    echo "  Every cluster on the filesystem must answer, or live volumes look orphaned." >&2
    exit 1
  fi
  n=$(python3 -c "
import json,sys
items=json.load(open('$SCRATCH/pv-$ctx.json'))['items']
c=0
with open('$SCRATCH/referenced.txt','a') as f:
    for p in items:
        csi=p['spec'].get('csi') or {}
        if not csi.get('driver','').endswith('cephfs.csi.ceph.com'): continue
        n=(csi.get('volumeAttributes') or {}).get('subvolumeName')
        if n: f.write(n+'\n'); c+=1
print(c)
")
  echo "  $ctx: $n cephfs PVs"
done

python3 - "$SCRATCH" > "$SCRATCH/orphans.txt" <<'PY'
import json,sys
base=sys.argv[1]
subs={s['name'] for s in json.load(open(base+'/subs.json'))}
refs={l.strip() for l in open(base+'/referenced.txt') if l.strip()}
for o in sorted(subs-refs): print(o)
PY
echo "  referenced across all clusters: $(sort -u "$SCRATCH/referenced.txt" | grep -c . || true)"
echo "  unreferenced by ANY cluster:    $(grep -c . "$SCRATCH/orphans.txt" || true)"

[ "${#TARGETS[@]}" -gt 0 ] || { echo; echo "No targets given. Candidates above; re-run with explicit names and --apply."; exit 0; }

echo "=== validating targets ==="
fail=0
for t in "${TARGETS[@]}"; do
  if grep -qx "$t" "$SCRATCH/orphans.txt"; then
    echo "  OK        $t"
  else
    echo "  REFUSING  $t -- referenced by a live PV in one of: $CONTEXTS" >&2
    fail=1
  fi
done
[ "$fail" -eq 0 ] || { echo "ABORTING, nothing deleted." >&2; exit 1; }

[ "$APPLY" -eq 1 ] || { echo; echo "Dry run. Re-run with --apply to delete."; exit 0; }

echo "=== deleting (permanent; there is no recycle bin for a subvolume) ==="
for t in "${TARGETS[@]}"; do
  out=$(ceph_do "ceph --name client.admin --keyring /tmp/a.keyring fs subvolume rm $FS $t --group_name $GROUP 2>&1" || true)
  [ -z "$out" ] && echo "  removed $t" || echo "  $t -> $out"
done
unset ADMIN
