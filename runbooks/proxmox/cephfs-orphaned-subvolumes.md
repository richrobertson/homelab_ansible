# Orphaned CephFS subvolumes: finding them, tracing them, removing them

> **The prod and staging clusters share one CephFS filesystem
> (`kubernetes-prod-cephfs`) and one subvolume group (`csi`). A subvolume that
> no PV in *one* cluster references is very often a live volume in the *other*.
> Union the PV lists of EVERY cluster before calling anything an orphan.**
>
> On 2026-08-22 this was got wrong: the orphan set was computed from prod's PVs
> alone and 16 live, Bound staging volumes were deleted — roughly 76 GiB across
> bitwarden, code-server, gotify, immich, netbootxyz-assets, nextcloud,
> overseerr, plex, prowlarr, radarr, tautulli, trilium, both vikunja volumes and
> two nextcloud html volumes. None had a ReplicationSource. The data is gone.
>
> The script that did it recomputed the orphan set from live PVs rather than
> trusting a written-down list, which felt rigorous and was worthless: it could
> only see the cluster in the current kubeconfig context. **A check that cannot
> see the thing which would falsify it is not a check.**
>
> `scripts/delete-orphan-cephfs-subvolumes.sh` now requires `--contexts` naming
> every cluster and refuses to run if any of them fails to answer. Use it rather
> than ad-hoc commands. The exporter deliberately no longer publishes an orphan
> count and nothing alerts on one.

`csi-cephfs-sc` uses `reclaimPolicy: Retain`. That means deleting a PVC — **or
deleting the PV** — never calls `DeleteVolume`, so the CephFS subvolume stays
behind holding its data. Nothing in the cluster reports this. The subvolume is
invisible from Kubernetes the moment its PV object is gone.

This is a real trap, not a theoretical one. A cleanup commit on 2026-08-21 said
"csi-cephfs-sc is Retain, so the PVs must be removed too or the CephFS
subvolumes leak" — which is backwards. Removing the PV is what strands the
subvolume; with `Retain` there is no code path that reclaims it either way.

## Detect

Compare subvolume count against the CephFS PV count **summed over every
cluster on the filesystem**. Comparing against a single cluster is what caused
the 2026-08-22 data loss.

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

Permanent; there is no recycle bin for a subvolume. Use the script, which
unions every named cluster's PVs and refuses to proceed if one is unreachable:

```sh
# report only
scripts/delete-orphan-cephfs-subvolumes.sh --contexts admin@prod,admin@staging

# then, with explicit names
scripts/delete-orphan-cephfs-subvolumes.sh --contexts admin@prod,admin@staging \
  --apply csi-vol-... csi-vol-...
```

"Recompute from live PVs immediately before deleting" is only a safety check if
the recomputation can see every consumer. It could not, and that is exactly how
live volumes were destroyed.

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

There is deliberately no automated orphan alert, and there should not be one
until a single vantage point can enumerate every consumer of the filesystem.
Today it cannot: prod pods get connection refused to the staging API, and
because the class is `Retain`, ceph-csi never calls `DeleteVolume`, so its OMAP
entry outlives the PV and is not a liveness signal either.

`cephfs-orphan-exporter` therefore publishes `cephfs_subvolumes_total`,
`cephfs_subvolumes_referenced_here_total`,
`cephfs_subvolumes_unreferenced_here_total` (explicitly NOT an orphan count) and
`cephfs_orphan_detection_complete 0`. If cross-cluster enumeration ever becomes
possible, set `DETECTION_COMPLETE=1` and only then add an alert guarded on it.

The real lesson is narrower than "be careful": a reconciliation is only as
trustworthy as the completeness of the set it reconciles against. State that set
explicitly, and make the tooling refuse to run when part of it is missing rather
than quietly treating absent as empty.
