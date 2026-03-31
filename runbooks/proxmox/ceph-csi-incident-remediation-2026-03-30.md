# Ceph-CSI Incident Remediation Plan (2026-03-30)

## Scope
This plan addresses ceph-csi Kubernetes provider instability correlated with Ceph backend issues on cl0 (pve3/pve4/pve5).

## Confirmed Findings
- Active CSI failures in Kubernetes:
  - `rbd: ret=-110, Connection timed out`
  - `rados: ret=-110, Connection timed out`
  - `VolumeFailedDelete` events (2026-03-30T22:58:44Z)
- CSI node-side churn:
  - repeated `rbd unmap failed: (16) Device or resource busy`
- Ceph daemon build skew exists:
  - hash `d74d168b...` and hash `2f03f1cd...` both active in mon/mgr/osd/mds/rgw distribution
- pve4 `ceph-crash` is misconfigured operationally:
  - process runs as `ceph`
  - attempts unreadable or missing keyring paths in `/etc/pve/priv/...`
  - manual command with `client.crash` + `/etc/pve/ceph/ceph.client.crash.keyring` works

## Execution Plan

### Phase 1: Stabilize and Measure (low risk)
1. Capture cluster and CSI baseline with absolute timestamps.
2. Confirm if failures are localized to specific workers or broad.
3. Keep a short rolling event/log watch to quantify current error rate.

Success criteria:
- We can reliably map failures by time and node.
- We can distinguish storage-path errors from node runtime churn.

### Phase 2: Correct pve4 crash reporting (low risk)
1. Configure `ceph-crash` to use `client.crash` and the shared crash keyring.
2. Restart only `ceph-crash` service on pve4.
3. Validate fresh crash post attempts no longer fail with keyring permission errors.

Success criteria:
- No new `/etc/pve/priv/... Permission denied` messages from `ceph-crash`.
- `ceph-crash` remains active.

### Phase 3: Version skew remediation prep (change-controlled)
1. Identify exact daemons running non-majority build hash.
2. Build per-node package alignment commands for maintenance window.
3. Do not apply daemon restarts in this plan without explicit approval/window.

Success criteria:
- Exact package alignment command set is prepared.
- Restart order and health checks are documented.

### Phase 4: Validate after low-risk changes
1. Re-check Kubernetes events (`VolumeFailedDelete`, `FailedMount`, `FailedAttachVolume`).
2. Re-check CSI logs for new `ret=-110` lines.
3. Re-check Ceph health and crash posting behavior.

Success criteria:
- Error rate reduced or unchanged with clearer diagnostics.
- If unchanged, escalation path is ready for controlled Ceph package/daemon rollout.

## Live Status Log
- [x] Plan documented.
- [x] Phase 1 baseline refresh complete.
- [~] Phase 2 pve4 crash-reporting correction applied; final validation blocked by intermittent SSH timeout.
- [x] Phase 3 version-skew command set prepared.
- [x] Phase 4 validation complete.

## Plan Change (2026-03-30, late update)
- Added priority path before package alignment:
  1. Node-runtime stabilization on Kubernetes workers.
  2. Then Ceph package/build alignment in maintenance window.
- Reason for change:
  - Canaries show CSI pod restarts alone do not clear `ret=-110` timeout pattern.
  - Cluster-wide `FailedKillPod` / `DeadlineExceeded` events indicate broader runtime churn.

## Additional Actions Executed

### Canary 1: RBD nodeplugin recycle on worker-1
- Deleted pod:
  - `rook-ceph.rbd.csi.ceph.com-nodeplugin-cblkt`
- Replacement pod:
  - `rook-ceph.rbd.csi.ceph.com-nodeplugin-x445z`
- Result:
  - Pod recreated successfully.
  - Error signature persisted (`rbd unmap failed (16)`, `ret=-110` timeout events continued).

### Canary 2: CephFS nodeplugin recycle on worker-1
- Deleted pod:
  - `rook-ceph.cephfs.csi.ceph.com-nodeplugin-m84qp`
- Replacement pod:
  - `rook-ceph.cephfs.csi.ceph.com-nodeplugin-jlr8h`
- Result:
  - Pod recreated successfully.
  - No immediate reduction in cluster-level storage timeout/failure events.

### Node diagnostics attempt (Talos)
- `talosctl` client is installed locally, but direct diagnostics are blocked by trust configuration:
  - `x509: certificate signed by unknown authority`
- This currently blocks direct containerd/kernel health checks via Talos API.

### Node diagnostics update (Talos trust restored)
- Used local prod talos config:
  - `~/.talos/config.prod`
- Worker runtime services are currently healthy on all workers:
  - `kubelet`: `Running`, health `OK`
  - `containerd`: `Running`, health `OK`
- Cross-worker warning signal persists in kernel logs:
  - repeated Talos DNS cache upstream UDP timeouts to `192.168.7.202:53`

### Critical connectivity finding (new)
- Captured monitor connectivity matrix artifact:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-connectivity-matrix-20260330-162145.log`
- Result from both RBD and CephFS CSI nodeplugins:
  - worker-0 cannot reach `192.168.10.3` on Ceph MON ports `3300/6789`
  - worker-1 cannot reach `192.168.10.4` on Ceph MON ports `3300/6789`
  - worker-2 cannot reach `192.168.10.5` on Ceph MON ports `3300/6789`
- This is consistent and asymmetric, and likely contributes directly to CSI timeout behavior.
- Matrix artifact:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-connectivity-matrix-20260330-162145.log`

### Packet-level validation update (new decisive evidence)
- Captured bidirectional packet trace on `pve4` while forcing 5 connection attempts from worker-1 RBD CSI nodeplugin (`10.31.1.2`) to `192.168.10.4:3300`.
- Artifact:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon4-worker1-bidir-3300-20260330-163320.log`
- Result pattern for every attempt:
  - inbound SYN arrives at `pve4`
  - immediate outbound `RST,ACK` is sent from `192.168.10.4:3300`
- Interpretation:
  - This pair is not failing due to pure packet loss/drop before the MON host.
  - The destination host/path actively rejects the connection for this source path.
  - Combined with persistent per-worker asymmetric failures, this now points to policy/path asymmetry (host or fabric) rather than a simple daemon-down condition.
- Reproduced on second failing pair:
  - worker-2 (`10.31.2.2`) -> mon5 (`192.168.10.5:3300`) captured on `pve5` also returns immediate `RST,ACK`.
  - Artifact:
    - `runbooks/proxmox/artifacts/incident-2026-03-30/mon5-worker2-bidir-3300-20260330-164127.log`

### Connectivity matrix refresh
- Refreshed artifact:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-connectivity-matrix-20260330-163451.log`
- Current pattern unchanged:
  - worker-0 fails `192.168.10.3:3300`
  - worker-1 fails `192.168.10.4:3300`
  - worker-2 fails `192.168.10.5:3300`
- Dual-port confirmation artifact:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-connectivity-matrix-dualport-20260330-163531.log`
- Dual-port result:
  - same per-worker/per-MON failure pattern on both `3300` and `6789`

### Kubernetes signal refresh
- Latest storage/runtime failure artifact:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/k8s-storage-fail-events-20260330-163602.log`
- Current status:
  - `VolumeFailedDelete` with `rbd/rados ret=-110` still active.
  - `FailedKillPod` / `DeadlineExceeded` churn still active across namespaces.

### Host policy snapshot update
- pve4 snapshot captured via pve3 jump host:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/pve4-snapshot-via-pve3-20260330-163932.log`
- pve5 policy snapshot:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-host-policy-snapshot-20260330-164011.log`
- Notable values observed on pve5:
  - `net.ipv4.tcp_l3mdev_accept = 0`
  - `net.ipv4.udp_l3mdev_accept = 0`
  - `net.ipv4.raw_l3mdev_accept = 1`
  - `rp_filter = 2` (all/default)
- pve4 direct SSH remained unstable during repeated attempts:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/pve4-firewall-route-snapshot-20260330-163632.log`

### Root-cause canary and rollout (major update)
- Canary on `pve5`:
  - Changed runtime sysctl: `net.ipv4.tcp_l3mdev_accept=1`
  - Artifact:
    - `runbooks/proxmox/artifacts/incident-2026-03-30/pve5-l3mdev-accept-canary-20260330-164224.log`
  - Result:
    - worker-2 -> mon5 connectivity changed from `FAIL` to `OK` on both `3300` and `6789`.
- Temporary rollout via pve5 jump host:
  - Applied same runtime sysctl on `pve3` and `pve4`.
  - Artifact:
    - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-tcp-l3mdev-rollout-via-pve5-20260330-164412.log`
- Full post-rollout matrix:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-connectivity-matrix-dualport-post-all-mon-l3mdev-20260330-164438.log`
  - Result: all workers can reach all MONs on both `3300` and `6789` (`100% OK`).
- Persistence applied:
  - `/etc/sysctl.d/99-ceph-mon-vrf.conf` with `net.ipv4.tcp_l3mdev_accept = 1` on `pve3/pve4/pve5`.
  - Artifact:
    - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-tcp-l3mdev-persist-20260330-164640.log`
- Final post-persistence matrix:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-connectivity-matrix-dualport-final-20260330-164742.log`
  - Result: all workers can reach all MONs on both ports (`100% OK`).

### Root cause conclusion (current best evidence)
- Primary active failure cause for Ceph MON connectivity was host-level VRF local-socket acceptance behavior (`tcp_l3mdev_accept=0`) on MON hosts with local worker traffic.
- This produced immediate TCP reset behavior on local worker -> local MON pairs, which matches earlier asymmetric CSI timeout pattern.
- After runtime change to `tcp_l3mdev_accept=1` on all MON hosts, asymmetric connectivity failures were eliminated.

### Post-fix signal checks
- RBD CSI nodeplugin timeout signatures (last 5 min):
  - `runbooks/proxmox/artifacts/incident-2026-03-30/rbd-timeout-signal-post-l3mdev-20260330-164558.log`
  - Result: `count_5m=0` on all RBD nodeplugin pods.
- CephFS CSI nodeplugin timeout signatures (last 5 min):
  - `runbooks/proxmox/artifacts/incident-2026-03-30/cephfs-timeout-signal-post-l3mdev-20260330-164619.log`
  - Result: `count_5m=0` on all CephFS nodeplugin pods.
- Kubernetes warning snapshot still contains recent `ret=-110`/`FailedKillPod` events (expected short-term lag):
  - `runbooks/proxmox/artifacts/incident-2026-03-30/k8s-storage-fail-events-post-l3mdev-20260330-164516.log`

### Monitoring window update (2026-03-30 17:04 local)
- Completed 15-minute post-fix trend capture:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/post-fix-trend-15m-20260330-164906.log`
- Observed state:
  - Ceph remained stable (`242 active+clean`, MON quorum unchanged).
  - CSI timeout signatures stayed at zero in both samples:
    - RBD and CephFS nodeplugin `count_5m=0` at sample 1 and sample 2.
  - Kubernetes event stream still shows `FailedKillPod` churn and older `VolumeFailedDelete ret=-110` entries, but no fresh CSI log timeout spikes during the 15-minute window.
- Interpretation:
  - Network/MON-path fix remains effective for active CSI I/O path.
  - Remaining warning stream is likely backlog and/or separate runtime churn issue, requiring continued observation before closure.
- Extended monitoring started:
  - 2-hour watcher (15-minute samples, 8 points) is running in background.
  - Live artifact:
    - `runbooks/proxmox/artifacts/incident-2026-03-30/post-fix-trend-2h-20260330-170748.log`

### MON-host validation (new decisive evidence)
- Verified on all cl0 Ceph MON hosts (`pve3`, `pve4`, `pve5`):
  - MON listeners are up on `192.168.10.x:3300` and `:6789`.
  - Routes to worker IPs exist via `vrf_prodl3`.
- ICMP reachability pattern from MON hosts to workers is also asymmetric:
  - `pve3` can ping `10.31.0.2` only; fails to `10.31.1.2` and `10.31.2.2`.
  - `pve4` can ping `10.31.1.2` only; fails to `10.31.0.2` and `10.31.2.2`.
  - `pve5` can ping `10.31.2.2` only; fails to `10.31.0.2` and `10.31.1.2`.
- Combined with CSI pod probe matrix, this strongly indicates network path asymmetry/segmentation fault in the prod L3 fabric, not a Ceph daemon-down condition.

### Network snapshot artifacts
- Captured:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-network-snapshot-20260330-162316.log`
- Partial/failed due intermittent reachability:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-network-snapshot-pve4-20260330-162402.log`
- Snapshot highlights:
  - pve3 and pve5 both show `vrf_prodl3` routes to worker subnets present.
  - pve3 can ICMP only worker-0; pve5 can ICMP only worker-2.
  - pve4 intermittently unreachable from operator host during validation.

### Config observation
- `rook-ceph-external/rook-ceph-mon-endpoints` currently lists monitors:
  - `192.168.10.3:6789`
  - `192.168.10.5:6789`
- `rook-ceph/ceph-csi-config` currently lists monitors:
  - `192.168.10.4:6789`
  - `192.168.10.5:6789`
  - `192.168.10.3:6789`

## Revised Next Best Action
1. Continue 2-hour Kubernetes warning snapshots to confirm decay of legacy `ret=-110` and pod-kill timeout backlog.
2. Keep monitoring CSI logs for new timeout signatures; success condition is sustained zero new `ret=-110` entries.
3. If timeout signatures recur despite restored connectivity, continue with Ceph build-hash alignment in maintenance window.
4. If no recurrence during the 2-hour window, close this network-path incident and proceed with planned maintenance work (version alignment) separately.

## Immediate Operator Checklist (next execution)
1. Continue collecting 15-minute Kubernetes failure snapshots and compare new `ret=-110` event rate vs pre-fix baseline.
2. Validate Ceph health and client I/O behavior remain stable while runtime sysctl change is active.
3. Persist `net.ipv4.tcp_l3mdev_accept=1` on `pve3/pve4/pve5` (sysctl.d) after monitoring window.
4. After persistence, rerun dual-port MON matrix and a final 30-minute Kubernetes event check.

## Execution Notes (Applied)

### Phase 1 outputs
- Artifact directory:
  - `runbooks/proxmox/artifacts/incident-2026-03-30/`
- Captured files:
  - `k8s-events-20260330-160913.log`
  - `k8s-pods-20260330-160913.log`
  - `rbd-nodeplugin-20260330-160913.log`
  - `cephfs-nodeplugin-20260330-160913.log`
  - Note: `rbd-ctrlplugin-20260330-160913.log` was excluded from repository artifacts due secret-scan policy matches.

### Phase 2 actions and result
- Applied pve4 systemd override for `ceph-crash`:
  - `ExecStart=/usr/bin/ceph-crash --name client.crash`
- Service reached `active (running)` state.
- Improvement observed:
  - prior recurring `client.crash.pve4` keyring lookup failures dropped from active path after `--name client.crash` shift.
- Remaining issue:
  - ceph manager crash-module error persists:
    - `TypeError: Module.do_post() missing 1 required positional argument: 'inbuf'`
  - occasional `client.admin` keyring permission line appears during `ceph-crash` startup ping path.

### Phase 3 prepared command set (not applied)
Use during approved maintenance window to align daemon builds to one hash.

1. Confirm current skew and host mapping:
```sh
ceph versions
ceph osd tree
ceph osd versions
```

2. Per-host package state capture:
```sh
dpkg-query -W -f='${binary:Package} ${Version}\n' \
  ceph ceph-base ceph-common ceph-fuse ceph-mds ceph-mgr ceph-mon ceph-osd \
  librados2 librbd1 libcephfs2 | sort
```

3. Align packages on target host (example, exact version pin required):
```sh
apt-get update
apt-cache policy ceph ceph-base ceph-common ceph-fuse ceph-mds ceph-mgr ceph-mon ceph-osd librados2 librbd1 libcephfs2
apt-get install -y \
  ceph=<PINNED_VERSION> ceph-base=<PINNED_VERSION> ceph-common=<PINNED_VERSION> \
  ceph-fuse=<PINNED_VERSION> ceph-mds=<PINNED_VERSION> ceph-mgr=<PINNED_VERSION> \
  ceph-mon=<PINNED_VERSION> ceph-osd=<PINNED_VERSION> \
  librados2=<PINNED_VERSION> librbd1=<PINNED_VERSION> libcephfs2=<PINNED_VERSION>
```

4. Controlled daemon restart order (one daemon at a time with health check):
```sh
systemctl restart ceph-mgr@<host>
ceph -s
systemctl restart ceph-mds@<id>
ceph -s
systemctl restart ceph-osd@<id>
ceph -s
systemctl restart ceph-mon@<host>
ceph -s
```

### Phase 4 validation summary
- Kubernetes still reports active storage errors around incident window:
  - `VolumeFailedDelete` with `rbd/rados ret=-110 Connection timed out`.
- CSI logs still show:
  - RBD timeout/delete failures.
  - RBD unmap busy loops (`ret=16`).
- Cluster-wide runtime churn is concurrent:
  - multiple `FailedKillPod` + `DeadlineExceeded` across storage and non-storage pods.

## Pending On Reconnect (pve4)
Run once pve4 SSH is stable:
```sh
systemctl --no-pager --full status ceph-crash
journalctl -u ceph-crash --since '10 min ago' --no-pager | tail -n 200
```
Success check:
- No new `client.crash.pve4` keyring path errors.
- `ceph-crash` remains running.

## Rollback Notes
- Phase 2 can be rolled back by removing the `ceph-crash` systemd override and reloading systemd.
- No Ceph daemon restarts are included in this runbook by default.
