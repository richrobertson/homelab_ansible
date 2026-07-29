# Ceph BlueStore fragmentation

Auditing and remediating fragmentation on the `cl0` Ceph OSDs.

## Two different problems share one name

Getting this distinction right matters, because the two have different fixes and
applying the wrong one wastes a maintenance window.

**Allocator fragmentation** is how broken up the free space on the block device
has become. Ceph reports it as a score from 0.0 (perfectly contiguous) to 1.0
(severe). As it rises, the allocator spends longer finding space for new writes
and write latency climbs. It is driven by how full the OSD is and by the
write/delete pattern over time.

Compaction does **not** fix this. The fix is to free capacity, or to drain the
OSD and refill it so free space is rebuilt contiguously.

**RocksDB / BlueFS fragmentation** is accumulated tombstones and dead SST levels
in the OSD's metadata store. It inflates BlueFS usage and slows metadata lookups
and OSD startup. This is what `ceph tell osd.N compact` fixes, and it is safe to
run on a healthy cluster.

The audit reports both. The remediation path only ever performs compaction, and
deliberately refuses to act on allocator fragmentation, because the correct
response there is a capacity or rebalance decision that should not be automated.

## Audit

Read-only, safe at any time, and the default behaviour:

```bash
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/ceph_bluestore_fragmentation.yml
```

Output is a per-OSD table of allocator score and BlueFS usage ratio, followed by
the list of compaction candidates.

Thresholds, all overridable with `-e`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `ceph_frag_alloc_warn` | `0.60` | Allocator score that gets called out |
| `ceph_frag_alloc_critical` | `0.80` | Allocator score treated as severe |
| `ceph_frag_bluefs_ratio_threshold` | `0.10` | BlueFS share of the store above which compaction is proposed |

## Remediate

Compaction is opt-in:

```bash
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/ceph_bluestore_fragmentation.yml \
  -e ceph_frag_compact=true
```

Guardrails, in order:

1. The play refuses to compact unless `ceph health detail` reports `HEALTH_OK`.
   Compaction adds I/O, and adding I/O to an already degraded cluster is how a
   slow cluster becomes an unavailable one. Override with
   `-e ceph_frag_require_health_ok=false` only deliberately.
2. Only OSDs over the BlueFS threshold are touched.
3. OSDs are compacted strictly one at a time. On a three-node cluster,
   compacting several at once can push enough PGs into a slow state to stall
   guest I/O.
4. After each OSD, the play waits `ceph_frag_settle_seconds` and then blocks
   until every PG is back to `active+clean` before moving on.

Run it in a maintenance window. A single large OSD can take several minutes.

## Interpreting the numbers

A healthy OSD sits well under 0.30 allocator score with BlueFS in low single
digit percent.

Allocator score climbing across **all** OSDs at once points at cluster-wide
capacity pressure rather than anything OSD-specific — check `ceph df` and the
pool `full_ratio` headroom first.

A single OSD far out of line with its peers is more likely a device or workload
outlier; check whether it is hosting a disproportionate share of a hot pool, and
consider `ceph osd reweight-by-utilization`.

## Continuous monitoring

The audit above is on-demand. Continuous coverage exists as well, because
fragmentation is a slow-onset problem — exactly the kind that goes unnoticed
until it presents as unexplained write latency.

The Ceph mgr prometheus module does **not** export allocator fragmentation or
per-OSD BlueFS detail, so there was no metric to alert on. A node_exporter
textfile collector fills that gap:

```bash
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/ceph_fragmentation_metrics.yml
```

This installs `/usr/local/sbin/ceph-fragmentation-metrics.sh` plus a systemd
timer on every Proxmox node, writing into the same textfile directory
node_exporter already reads for the transport metrics. Only one node collects
per interval, via `flock`, so the cluster-wide `ceph tell` sweep does not run
three times over.

Metrics published:

| Metric | Meaning |
|--------|---------|
| `ceph_bluestore_allocator_fragmentation_score{osd}` | Allocator score, 0.0 to 1.0 |
| `ceph_bluestore_bluefs_used_ratio{osd}` | BlueFS share of the OSD store |
| `ceph_bluestore_fragmentation_collector_success` | 1 when the last sweep covered every OSD |

Alert rules live in
`homelab_flux/infrastructure/configs/ceph-fragmentation-alerts.yaml` and fire on
sustained high allocator fragmentation, on BlueFS growth that warrants
compaction, and on the collector itself going missing. That last one matters:
without it, a collector that quietly stops leaves the other rules evaluating
against absent series, which reads identically to "everything is fine".

Verify the collector on a node:

```bash
systemctl status ceph-fragmentation-metrics.timer
cat /var/lib/prometheus/node-exporter/ceph_fragmentation.prom
```

## Manual equivalents

For one-off checks without the playbook:

```bash
# allocator score for one OSD
ceph tell osd.0 bluestore allocator score block

# BlueFS statistics
ceph daemon osd.0 perf dump bluefs

# compact one OSD
ceph tell osd.0 compact

# cluster capacity, which drives allocator fragmentation
ceph df detail
```
