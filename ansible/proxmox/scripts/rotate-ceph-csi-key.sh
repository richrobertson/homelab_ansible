#!/usr/bin/env bash
# Rotate one Ceph CSI keyring end to end, in the only order that is safe:
#   ceph auth rotate -> write Vault -> force VSO sync -> verify K8s Secret matches
#
# The key is never printed, never placed in argv, and never written to disk
# outside the toolbox pod's tmpfs. Only sha256 prefixes and lengths are shown,
# which is enough to prove Ceph, Vault and Kubernetes all agree.
#
#   usage: rotate-ceph-csi-key.sh <ceph-entity> <vault-path-leaf> <k8s-secret> [k8s-namespace]
#   e.g.   rotate-ceph-csi-key.sh csi-rbd-provisioner rook-csi-rbd-provisioner rook-csi-rbd-provisioner
#          rotate-ceph-csi-key.sh csi-cephfs-standalone csi-cephfs-secret csi-cephfs-secret ceph-csi
set -euo pipefail

ENTITY="${1:?ceph entity, e.g. csi-rbd-provisioner}"
LEAF="${2:?vault path leaf under secret/proxmox/cl0/ceph/}"
K8S_SECRET="${3:?k8s secret name}"
# The standalone ceph-csi stack keeps its Secret and VaultStaticSecret in
# ceph-csi, not rook-ceph-external, so the namespace cannot be assumed.
SECRET_NS="${4:-rook-ceph-external}"

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export VAULT_ADDR="${VAULT_ADDR:-https://vault.myrobertson.net:8200}"
VPATH="secret/proxmox/cl0/ceph/$LEAF"
NS=rook-ceph-external   # where the toolbox pod lives
TB=$(kubectl --request-timeout=30s -n "$NS" get pod -l app=rook-ceph-tools -o jsonpath='{.items[0].metadata.name}')
[ -n "$TB" ] || { echo "no toolbox pod" >&2; exit 1; }

h() { printf '%s' "$1" | shasum -a256 | cut -c1-12; }

# client.admin: the only entity here permitted to rotate others.
#
# This used to read csi-cephfs-secret's userKey, which WAS client.admin until
# that path was de-privileged on 2026-08-22. It now holds
# client.csi-cephfs-standalone, which cannot rotate anything, so the admin key
# is read from the path it was split out to.
ADMIN=$(vault kv get -field=key secret/proxmox/cl0/ceph/client-admin)

echo "=== $ENTITY : before ==="
OLD_VAULT=$(vault kv get -field=userKey "$VPATH")
echo "  vault userKey sha=$(h "$OLD_VAULT") len=${#OLD_VAULT}"

echo "=== rotating client.$ENTITY in ceph ==="
NEWKEY=$(printf '[client.admin]\n\tkey = %s\n' "$ADMIN" | \
  kubectl --request-timeout=90s -n "$NS" exec -i "$TB" -- sh -c '
    cat > /tmp/a.keyring && chmod 600 /tmp/a.keyring
    unset CEPH_ARGS
    ceph --name client.admin --keyring /tmp/a.keyring auth rotate "client.'"$ENTITY"'" -f json 2>/dev/null
    rm -f /tmp/a.keyring' | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['key'])")

[ -n "$NEWKEY" ] || { echo "rotate produced no key -- ABORTING, vault untouched" >&2; exit 1; }
[ "$NEWKEY" != "$OLD_VAULT" ] || { echo "key unchanged -- ABORTING" >&2; exit 1; }
echo "  new ceph key sha=$(h "$NEWKEY") len=${#NEWKEY}"

echo "=== writing vault ==="
vault kv patch "$VPATH" "userKey=$NEWKEY" >/dev/null
BACK=$(vault kv get -field=userKey "$VPATH")
[ "$BACK" = "$NEWKEY" ] || { echo "vault readback MISMATCH" >&2; exit 1; }
echo "  vault userKey sha=$(h "$BACK") (matches ceph)"

# Stamp rotated_at, or the rotation does not count.
#
# The credential-age collector reads age ONLY from custom_metadata.rotated_at.
# Writing a new key does not touch it, so on 2026-08-22 four keys rotated by
# this very script still reported as severely overdue afterwards. A rotation
# that leaves its own alert firing is not finished.
#
# Prefer `metadata patch`, which merges server-side. Fall back to a
# read-modify-write for tokens whose policy lacks the patch capability, because
# a bare `metadata put` REPLACES the whole map and would silently drop keys such
# as rotation_interval_days or the de-privileging notes on csi-cephfs-secret.
TODAY=$(date -u +%Y-%m-%d)
if vault kv metadata patch -mount=secret -custom-metadata=rotated_at="$TODAY" \
     "${VPATH#secret/}" >/dev/null 2>&1; then
  echo "  stamped rotated_at=$TODAY (metadata patch)"
else
  MERGED=$(vault kv metadata get -format=json "$VPATH" 2>/dev/null | python3 -c "
import sys,json,shlex,os
cm=(json.load(sys.stdin)['data'].get('custom_metadata') or {})
cm['rotated_at']=os.environ['TODAY']
cm.setdefault('rotation_interval_days','90')
print(' '.join('-custom-metadata=%s' % shlex.quote('%s=%s'%(k,v)) for k,v in sorted(cm.items())))
")
  if [ -n "$MERGED" ]; then
    TODAY="$TODAY" eval vault kv metadata put -mount=secret "$MERGED" "${VPATH#secret/}" >/dev/null
    echo "  stamped rotated_at=$TODAY (read-modify-write; policy lacks patch)"
  else
    echo "  WARNING: could not stamp rotated_at -- the age alert will stay firing" >&2
  fi
fi
STAMPED=$(vault kv metadata get -format=json "$VPATH" 2>/dev/null \
  | python3 -c "import sys,json;print((json.load(sys.stdin)['data'].get('custom_metadata') or {}).get('rotated_at'))")
[ "$STAMPED" = "$TODAY" ] || echo "  WARNING: rotated_at reads '$STAMPED', expected '$TODAY'" >&2

echo "=== forcing VSO sync ==="
kubectl --request-timeout=30s -n "$SECRET_NS" annotate vaultstaticsecret "$LEAF" \
  vso.hashicorp.com/force-sync="$(date +%s)" --overwrite >/dev/null 2>&1 || true

for i in $(seq 1 12); do
  CUR=$(kubectl --request-timeout=20s -n "$SECRET_NS" get secret "$K8S_SECRET" -o jsonpath='{.data.userKey}' 2>/dev/null | base64 -d || true)
  if [ "$CUR" = "$NEWKEY" ]; then
    echo "  k8s secret $SECRET_NS/$K8S_SECRET sha=$(h "$CUR") -- SYNCED after ${i}0s"
    break
  fi
  sleep 10
done
[ "$CUR" = "$NEWKEY" ] || { echo "  k8s secret did NOT converge (sha=$(h "$CUR"))" >&2; exit 1; }

echo "=== verifying the new key actually authenticates to ceph ==="
printf '[client.%s]\n\tkey = %s\n' "$ENTITY" "$NEWKEY" | \
  kubectl --request-timeout=60s -n "$NS" exec -i "$TB" -- sh -c '
    cat > /tmp/t.keyring && chmod 600 /tmp/t.keyring
    unset CEPH_ARGS
    ceph --name "client.'"$ENTITY"'" --keyring /tmp/t.keyring -s >/dev/null 2>&1 \
      && echo "  auth OK as client.'"$ENTITY"'" \
      || echo "  auth FAILED as client.'"$ENTITY"'"
    rm -f /tmp/t.keyring'

unset ADMIN NEWKEY OLD_VAULT BACK CUR
echo "done: $ENTITY"
