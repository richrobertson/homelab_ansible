# Orphaned CephFS subvolumes: finding them, tracing them, removing them

`csi-cephfs-sc` uses `reclaimPolicy: Retain`. That means deleting a PVC — **or
deleting the PV** — never calls `DeleteVolume`, so the CephFS subvolume stays
behind holding its data. Nothing in the cluster reports this. The subvolume is
invisible from Kubernetes the moment its PV object is gone.

This is a real trap, not a theoretical one. A cleanup commit on 2026-08-21 said
"csi-cephfs-sc is Retain, so the PVs must be removed too or the CephFS
subvolumes leak" — which is backwards. Removing the PV is what strands the
subvolume; with `Retain` there is no code path that reclaims it either way.

## Detect

Compare subvolume count against CephFS PV count. They should be equal.

```sh
# in the rook-ceph-tools pod. NOTE: unset CEPH_ARGS first -- the toolbox sets
# CEPH_ARGS=--id healthchecker, which silently overrides --name and yields
# "RADOS permission error" that looks exactly like a bad key.
unset CEPH_ARGS
ceph --name client.admin --keyring /tmp/a.keyring \
  fs subvolume ls kubernetes-prod-cephfs --group_name csi -f json | jq length
```

```sh
kubectl get pv -o json | jq '[.items[]
  | select(.spec.csi.driver // "" | endswith("cephfs.csi.ceph.com"))] | length'
```

A live PV names its subvolume in `.spec.csi.volumeAttributes.subvolumeName`, so
the orphan set is `all subvolumes - referenced subvolumes`.

## Trace: what was it?

The PV object is gone, so Kubernetes cannot tell you. ceph-csi keeps a reverse
map in RADOS, and this is the part that is hard to find: **the objects are not
in the pool root, they live in RADOS namespaces inside the metadata pool.**

```sh
# discover the namespaces -- 'cephfs-csi-standalone' is the csi-cephfs-sc stack,
# 'csi' is Rook's
rados -p kubernetes-prod-cephfs_metadata --all ls | grep csi.volume

# then, per orphan uuid (the part after csi-vol-)
rados -p kubernetes-prod-cephfs_metadata -N cephfs-csi-standalone \
  getomapval csi.volume.<uuid> csi.volname /dev/stdout        # -> pvc-<uuid>
rados -p kubernetes-prod-cephfs_metadata -N cephfs-csi-standalone \
  getomapval csi.volume.<uuid> csi.volume.owner /dev/stdout   # -> namespace
```

That gives the original PV name and owning namespace, but not the application.
For that, mount the subvolume **read-only** as a static PV and look:

```yaml
spec:
  persistentVolumeReclaimPolicy: Retain   # so cleanup cannot destroy data
  storageClassName: ""
  csi:
    driver: cephfs.csi.ceph.com
    volumeHandle: <any unique string>
    volumeAttributes:
      clusterID: "<ceph fsid>"
      fsName: "kubernetes-prod-cephfs"
      staticVolume: "true"
      rootPath: "<ceph fs subvolume getpath ...>"
    nodeStageSecretRef: { name: csi-cephfs-secret, namespace: ceph-csi }
```

`bytes_quota` from `fs subvolume info` is also a strong identifier — it matches
the `storage:` request in the original PVC manifest, which is usually enough to
tell two candidates apart.

## Remove

Permanent; there is no recycle bin for a subvolume. Do not work from a list
written down earlier — recompute the orphan set immediately before deleting and
assert every target is still in it.

```sh
ceph ... fs subvolume rm kubernetes-prod-cephfs <name> --group_name csi
```

Two failure modes seen in practice:

- `Error ENOTEMPTY: subvolume ... has snapshots`. Check what they are before
  forcing. A subvolume with snapshots but **no nested UUID directory** and no
  `subvolume info` was removed earlier with `--retain-snapshots`. If the
  snapshots are named `csi-vol-*` (not `csi-snap-*`) they are clone sources; if
  no `VolumeSnapshotContent` references them and no subvolume of that name
  exists, they are leftovers of a clone that never completed. Remove the
  snapshots, then the subvolume.
- Space does not come back immediately. CephFS purges asynchronously.

## The structural gap

Nothing alerts on this. Subvolume count vs CephFS PV count is a one-line check
and would have caught 18 orphans holding ~2.94 GiB that accumulated from
2026-04-28 onward. Worth wiring into the ceph metrics collector.
