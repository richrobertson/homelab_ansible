# Staging Cluster Rebuild and SDN Investigation (2026-03-31)

## Scope
This runbook captures the work performed across Synology SAN cleanup, staging node recreation, and the live Proxmox SDN investigation that followed.

Systems touched:
- Synology NAS `192.168.1.215:5022`
- Proxmox hosts `pve3`, `pve4`, `pve5`
- Staging control-plane and worker nodes:
  - `k8s-stg-cp-0` `10.20.0.2`
  - `k8s-stg-cp-1` `10.20.1.2`
  - `k8s-stg-cp-2` `10.20.2.2`
  - `k8s-stg-worker-0` `10.21.0.2`
  - `k8s-stg-worker-1` `10.21.1.2`
  - `k8s-stg-worker-2` `10.21.2.2`

## Executive Summary
- Synology disconnected k8s-backed LUN discovery was corrected to use live target `status` instead of `max_sessions`.
- Disconnected k8s LUNs were ultimately removed manually in DSM after automation hit hard feasibility gates.
- The exact legacy staging Terraform path was used to recreate `k8s-stg-cp-2` and `k8s-stg-worker-2`.
- A stale Proxmox disk/state problem also broke `k8s-stg-worker-1`; that node was surgically recreated and recovered.
- The remaining staging outage is not a generic Talos bootstrap problem. It is a host-localized staging SDN dataplane issue centered on `pve5`.
- `pve5` has the expected FRR, VXLAN, VRF, and route objects, but traffic from `pve5` toward remote staging control-plane IPs does not complete, which prevents `cp-2` and `worker-2` from joining.

## Update (2026-03-31 23:26 UTC)
- Additional live collection was attempted for firewall/EVPN diffs and repeat packet capture.
- New artifact snapshots were written in:
  - `runbooks/proxmox/artifacts/incident-2026-03-31/live-remediation/192.168.11.4.snapshot.txt`
  - `runbooks/proxmox/artifacts/incident-2026-03-31/live-remediation/192.168.11.5.snapshot.txt`
- During this window, SSH to both `pve4` and `pve5` repeatedly timed out, preventing completion of a fresh full tcpdump session.
- This does not invalidate earlier captured packet evidence; it strengthens the separate management-path instability finding (`:8006` reachable while `:22` intermittently fails).

## Update (2026-03-31 23:31 UTC)
- Continued retries from the operator host showed both SSH and Proxmox API calls failing to return usable data for `pve4` and `pve5`.
- This is a temporary escalation from earlier behavior and currently blocks additional remote evidence collection.
- Existing packet and host-state evidence remains the basis for diagnosis until management-plane access stabilizes.

## Update (2026-03-31 23:34 UTC)
- A fresh live capture window on `pve5` was completed successfully and saved to:
  - `runbooks/proxmox/artifacts/incident-2026-03-31/live-remediation/pve5-stg6443-live-capture.txt`
- During this window, active probes from `pve5` to `10.20.0.2:6443` and `10.20.1.2:6443` timed out.
- Packet evidence confirms repeated SYN egress from affected nodes on `pve5` (`10.20.2.2`, `10.21.2.2`) toward healthy control-plane endpoints with no successful connection completion for those flows.
- Capture also shows occasional inbound attempts from remote nodes toward `10.20.2.2:6443`, which receive immediate RST responses because `cp-2` is not yet a serving API endpoint.
- Follow-up reference collection from `pve4` was attempted again, but `pve5` SSH flapped immediately afterward and blocked jump-host collection.

## Update (2026-03-31 23:40 UTC)
- Direct `pve5` console access produced the cleanest evidence yet and was saved to:
  - `runbooks/proxmox/artifacts/incident-2026-03-31/live-remediation/pve5-console-probe-20260331-1634.txt`
- From the `pve5` host itself:
  - `nc -vz -w 3 10.20.0.2 6443` timed out
  - `nc -vz -w 3 10.20.1.2 6443` timed out
  - `ping 10.20.0.2` showed 100% loss
- The capture confirms that packets from affected guests do traverse the local bridge/VRF path on `pve5`:
  - `10.20.2.2 -> 10.20.0.2:6443` SYN is seen on `tap217i0`, `stgctr`, `vrfbr_stgl3`, and `vrfvx_stgl3`
  - `10.21.2.2 -> 10.20.0.2:6443` SYN is seen on `tap218i0`, `stgdata`, `vrfbr_stgl3`, and `vrfvx_stgl3`
- That means the affected guests are successfully emitting traffic into the `pve5` staging bridge and VRF path.
- The same capture also shows inbound remote traffic reaching `cp-2` over VXLAN:
  - `10.20.0.2 -> 10.20.2.2:6443` SYN arrives on `vxlan_stgctr` and is forwarded to `tap217i0`
  - `cp-2` immediately responds with `RST`, which is expected because kube-apiserver is not serving yet on that node
- This is important because it proves at least some remote-to-`pve5` overlay traffic is working.
- The failure is therefore more precise than “all pve5 staging overlay is down”:
  - guest-to-remote control-plane sessions from `cp-2` and `worker-2` do not complete
  - host-originated sessions from `pve5` VRF addresses (`10.20.0.1`) to remote control-plane nodes also do not complete
  - remote-to-guest overlay delivery into `cp-2` is at least partially functional
- Current best interpretation:
  - this is an asymmetric or selective staging dataplane failure centered on `pve5`, not a total loss of VXLAN control-plane state
  - the next most valuable capture is underlay UDP `4789` on `pve5` while probing, to confirm whether VXLAN encapsulated traffic is leaving toward the remote VTEPs and whether any return encapsulated packets arrive

## Update (2026-03-31 23:47 UTC)
- Additional direct-console route, neighbor, and FDB state from `pve5` was saved to:
  - `runbooks/proxmox/artifacts/incident-2026-03-31/live-remediation/pve5-console-routing-fdb-20260331-1640.txt`
- This snapshot materially sharpens the diagnosis:
  - EVPN-installed host routes for remote staging endpoints are present in `vrf_stgl3`
    - `10.20.0.2 via 192.168.1.241`
    - `10.20.1.2 via 192.168.1.242`
    - `10.21.1.2 via 192.168.1.242`
  - Remote neighbor entries are present and externally learned by zebra on the staging bridges
    - `10.20.0.2 dev stgctr lladdr bc:24:11:77:a1:a3 extern_learn`
    - `10.20.1.2 dev stgctr lladdr bc:24:11:77:70:41 extern_learn`
    - `10.21.1.2 dev stgdata lladdr bc:24:11:61:24:49 extern_learn`
  - VXLAN FDB entries also correctly map those remote MACs to the expected remote VTEPs
    - `bc:24:11:77:a1:a3 dst 192.168.1.241`
    - `bc:24:11:77:70:41 dst 192.168.1.242`
    - `bc:24:11:61:24:49 dst 192.168.1.242`
- This means the `pve5` EVPN control plane, MAC learning, and route programming look correct for the affected staging subnets.
- One earlier operator-host inference needs tightening: plain host `ping`/`nc` from `pve5` without `ip vrf exec vrf_stgl3` is not a clean proof of failure by itself, because the relevant routes live in the staging VRF rather than the default namespace.
- The stronger evidence remains the packet captures and the guest symptoms:
  - guest-originated SYN packets from `cp-2` and `worker-2` traverse local tap, bridge, and VRF interfaces on `pve5`
  - remote-to-`cp-2` ingress over `vxlan_stgctr` is observed
  - those guest-to-remote sessions still do not complete
- Refined current diagnosis:
  - this is less likely to be a missing EVPN route/MAC advertisement problem
  - it is more likely an asymmetric underlay/VXLAN transport issue or selective filtering affecting session completion for traffic sourced from `pve5` staging guests toward remote staging control-plane nodes

## Update (2026-03-31 23:53 UTC)
- A VRF-aware direct-console probe on `pve5` was run with `ip vrf exec vrf_stgl3` and confirms the failure is not an artifact of probing from the default namespace.
- Results:
  - `ip vrf exec vrf_stgl3 nc -vz -w 3 10.20.0.2 6443` timed out
  - `ip vrf exec vrf_stgl3 nc -vz -w 3 10.20.1.2 6443` timed out
  - concurrent capture still showed only locally-originated SYN traffic traversing `vrfbr_stgl3` and `vrfvx_stgl3` with no successful handshake completion
- The same capture again showed inbound remote traffic to `cp-2` arriving over the local staging path, reinforcing the asymmetry:
  - remote-to-`pve5` overlay delivery is at least partially working
  - `pve5`-sourced or `pve5`-guest-sourced sessions to remote staging control-plane nodes are not completing
- This removes the earlier ambiguity about plain host probes and strengthens the current best theory:
  - EVPN control-plane state and bridge/FDB programming on `pve5` look correct
  - the remaining failure domain is selective/asymmetric dataplane handling on or beyond `pve5`, most likely in underlay/VXLAN transport or filtering

## Update (2026-03-31 23:58 UTC)
- Additional direct-console capture output from `pve5` materially narrows the fault location.
- In the host capture saved at `runbooks/proxmox/artifacts/incident-2026-03-31/live-remediation/pve5-console-probe-20260331-1634.txt`:
  - guest SYN traffic from `10.20.2.2` to `10.20.0.2:6443` is observed on:
    - `tap217i0`
    - `stgctr`
    - `vrfbr_stgl3`
    - `vrfvx_stgl3`
  - guest SYN traffic from `10.20.2.2` to `10.20.1.2:6443` is also observed on the same local interfaces
  - host-sourced SYN traffic from `10.20.0.1` to both remote control-plane endpoints is likewise observed on `vrfbr_stgl3` and `vrfvx_stgl3`
  - inbound remote overlay traffic from `10.20.0.2` to `10.20.2.2:6443` is observed on `vxlan_stgctr` and then on `tap217i0`
- The important asymmetry is what is *not* seen:
  - no corresponding outbound packets were observed on `vxlan_stgctr` for the guest/host traffic leaving `pve5` toward remote control-plane endpoints during that capture window
- Refined interpretation:
  - EVPN control-plane state, remote FDB entries, and neighbor learning still look correct
  - local guest-to-bridge-to-VRF forwarding on `pve5` is working
  - remote-to-local overlay ingress on `vxlan_stgctr` is working
  - the strongest remaining fault domain is now the local handoff from `vrfvx_stgl3` to the outbound VXLAN device on `pve5`, or a closely adjacent egress filtering path
- The next decisive check is a simultaneous capture on `vrfvx_stgl3`, `vxlan_stgctr`, and the underlay uplink while generating a very small number of probes

## Update (2026-04-01 00:02 UTC)
- A three-point direct-console capture on `pve5` was completed while probing remote staging control-plane endpoints and saved in operator notes from the `pve5_vxlan_egress_check` run.
- The capture gives the clearest fault isolation yet:
  - On `vrfvx_stgl3`, locally-originated staging traffic is present:
    - guest SYNs from `10.20.2.2 -> 10.20.0.2:6443`
    - guest SYNs from `10.20.2.2 -> 10.20.1.2:6443`
    - guest SYNs from `10.21.2.2 -> 10.20.0.2:6443` and `10.21.2.2 -> 10.20.1.2:6443`
    - host VRF-sourced SYNs from `10.20.0.1 -> 10.20.0.2:6443`
  - On `vxlan_stgctr`, only inbound remote SYNs toward `10.20.2.2:6443` were observed from `10.20.0.2` and `10.20.1.2`.
  - The expected matching outbound `10.20.2.2/10.21.2.2/10.20.0.1 -> remote:6443` flows were not observed on `vxlan_stgctr` during the same probe window.
  - On the underlay capture (`vmbr1`, UDP `4789`), only inbound encapsulated traffic from `192.168.1.241 -> 192.168.1.243` was observed during the window; there were no matching outbound VXLAN packets from `192.168.1.243` carrying the staging API probes.
- This materially changes the confidence level of the diagnosis:
  - EVPN control-plane state on `pve5` still looks correct.
  - Remote MAC/IP learning still looks correct.
  - Inbound overlay delivery to `pve5` is working.
  - The break is now strongly localized to outbound handoff from the local staging VRF/bridge path into the VXLAN egress path on `pve5`, or a closely adjacent local egress filter path.
- In short: this is no longer best described as a generic underlay asymmetry. It is most likely a `pve5` local bridge-to-VXLAN egress problem for staging traffic.
- Immediate next checks before remediation:
  - compare `ip -d link show vxlan_stgctr vxlan_stgdata vrfvx_stgl3` on `pve5` vs a healthy host
  - inspect bridge/VXLAN policy state (`bridge mdb show`, `bridge vlan show`, `bridge -s link show`)
  - inspect bridge-family nftables/ebtables/tc state for local egress filtering on `pve5`
  - if the above stay clean, consider a controlled bounce of only the staging VXLAN/VRF interfaces on `pve5` during a maintenance window

## Update (2026-04-01 00:06 UTC)
- A follow-up direct-console bridge/policy collection on `pve5` partially succeeded but the pasted helper script was corrupted mid-entry, so only the early sections are reliable.
- Reliable additional observations from that partial run:
  - `vxlan_stgctr`, `vxlan_stgdata`, and `vrfvx_stgl3` remain `UP,LOWER_UP` and attached to the expected staging bridges.
  - `tap217i0` remains attached to `stgctr` and `tap218i0` remains attached to `stgdata` with MTU `1450`, matching the recreated staging node NICs.
  - The underlay uplink chosen for remote VTEPs is `vmbr1`.
  - `bridge vlan show` on `vmbr1` is extremely broad and not by itself useful evidence of the staging fault.
- The broken script did not capture the decisive remaining sections:
  - bridge-family `nft`
  - `ebtables`
  - `tc` filters/qdisc on `stgctr`, `vrfbr_stgl3`, `vrfvx_stgl3`, and `vxlan_stgctr`
- Current best interpretation is unchanged from the prior update:
  - staging VXLAN/VRF devices are up and attached correctly on `pve5`
  - the remaining likely fault domain is local policy/egress handling between the staging VRF/bridge path and outbound VXLAN transmission on `pve5`

## Update (2026-04-01 00:10 UTC)
- A direct-console minimal policy check on `pve5` completed successfully enough to rule out the most obvious local filtering paths.
- Confirmed from the live console output:
  - `stgctr`, `stgdata`, `vrfbr_stgl3`, `vrfvx_stgl3`, `vxlan_stgctr`, and `vxlan_stgdata` are all `UP,LOWER_UP`
  - `vxlan_stgctr` and `vxlan_stgdata` are configured with the expected local VTEP `192.168.1.243`, VNIs `1000` and `2000`, `dstport 4789`, and `nolearning`
  - `vrfvx_stgl3` is configured with VNI `4000` and the expected local VTEP `192.168.1.243`
  - bridge netfilter flags on the staging bridges are disabled (`nf_call_iptables=0`, `nf_call_ip6tables=0`, `nf_call_arptables=0`)
  - there are no `nft` bridge-family rules
  - `ebtables` filter chains are empty with `ACCEPT` policy
  - visible `tc` state is just `qdisc noqueue` with no explicit ingress or egress filters shown in the operator output
- This substantially lowers the probability of a local nftables/ebtables/tc policy block on `pve5`.
- Refined conclusion:
  - EVPN control-plane state appears correct
  - staging VXLAN/VRF devices are present and up
  - inbound VXLAN traffic is observed
  - outbound bridge-to-VXLAN egress for staging probe traffic still appears broken
  - the next practical remediation step is a controlled bounce of only the staging VXLAN/VRF interfaces on `pve5`, followed by immediate re-test before any broader FRR or host networking restart

## Update (2026-04-01 00:13 UTC)
- The controlled bounce of only the staging VXLAN/VRF interfaces on `pve5` was completed:
  - `vxlan_stgctr`
  - `vxlan_stgdata`
  - `vrfvx_stgl3`
- Immediate post-bounce VRF-aware reachability tests still failed:
  - `ip vrf exec vrf_stgl3 nc -vz -w 3 10.20.0.2 6443` timed out
  - `ip vrf exec vrf_stgl3 nc -vz -w 3 10.20.1.2 6443` timed out
  - `ip vrf exec vrf_stgl3 ping -c 3 10.20.0.2` returned 100% loss
  - `ip vrf exec vrf_stgl3 ping -c 3 10.20.1.2` returned 100% loss
- This failed remediation step is important because it rules out a simple stale state on the staging VXLAN devices themselves.
- Refined conclusion after the bounce test:
  - the fault is likely higher in the local staging dataplane on `pve5` than the `vxlan_*` devices alone
  - the next smallest active remediation is to bounce the staging bridges as well (`stgctr`, `stgdata`, `vrfbr_stgl3`) together with the staging VXLAN devices
  - if that also fails, the next escalation should be a maintenance-window restart of only the `pve5` SDN/network stack rather than FRR-first changes

## Update (2026-04-01 00:20 UTC)
- A broader but still staging-scoped dataplane bounce on `pve5` was completed successfully at the interface level:
  - `vxlan_stgctr`
  - `vxlan_stgdata`
  - `vrfvx_stgl3`
  - `stgctr`
  - `stgdata`
  - `vrfbr_stgl3`
- All interfaces came back `UP,LOWER_UP` after the bounce.
- Immediate post-bounce VRF-aware reachability checks still failed exactly as before:
  - `ip vrf exec vrf_stgl3 nc -vz -w 3 10.20.0.2 6443` timed out
  - `ip vrf exec vrf_stgl3 nc -vz -w 3 10.20.1.2 6443` timed out
  - `ip vrf exec vrf_stgl3 ping -c 3 10.20.0.2` returned 100% loss
  - `ip vrf exec vrf_stgl3 ping -c 3 10.20.1.2` returned 100% loss
- This rules out stale state in the local staging bridges and VXLAN devices as the primary cause.
- Refined conclusion after the bridge-plus-VXLAN bounce:
  - the failure is deeper than the local lifecycle state of `stgctr`, `stgdata`, `vrfbr_stgl3`, `vrfvx_stgl3`, `vxlan_stgctr`, and `vxlan_stgdata`
  - EVPN control-plane state, remote MAC/IP learning, and local interface state all continue to look correct
  - the next rational escalation is no longer another interface bounce; it is a maintenance-window restart of the `pve5` SDN/network stack, followed by immediate staging VRF reachability re-checks and Talos/Kubernetes recovery validation
  - FRR-only restart is not the preferred next step because the evidence continues to implicate the local dataplane rather than BGP/EVPN control-plane convergence

## Update (2026-04-01 00:52 UTC)
- `pve5` was restarted, and post-restart validation showed staging recovery.
- Operator-side connectivity checks now show:
  - `10.20.0.2:6443` reachable
  - `10.20.1.2:6443` reachable
  - `10.20.2.2:6443` reachable
  - `10.20.0.2:50000`, `10.20.1.2:50000`, `10.20.2.2:50000`, and `10.21.2.2:50000` reachable
  - `10.21.2.2:6443` is `connection refused` (expected for worker node)
- Kubernetes node status recovered fully:
  - `k8s-stg-cp-0` `Ready`
  - `k8s-stg-cp-1` `Ready`
  - `k8s-stg-cp-2` `Ready`
  - `k8s-stg-worker-0` `Ready`
  - `k8s-stg-worker-1` `Ready`
  - `k8s-stg-worker-2` `Ready`
- Direct Talos checks also confirm recovery on the previously affected worker:
  - `talosctl --nodes 10.21.2.2 --endpoints 10.21.2.2 get machinestatus` reports `STAGE=running` and `READY=true`
  - `talosctl --nodes 10.21.2.2 --endpoints 10.21.2.2 service kubelet` reports `STATE=Running` and `HEALTH=OK`
- Outcome:
  - the staging outage affecting `cp-2` and `worker-2` is resolved
  - the failed local interface bounces plus successful host restart strongly suggest the fault lived in `pve5` host networking/SDN runtime state rather than Talos node configuration

## Update (2026-04-01 01:00 UTC)
- A post-recovery baseline capture pass was completed and stored in:
  - `runbooks/proxmox/artifacts/incident-2026-03-31/post-recovery-baseline/rapid-check.txt`
  - `runbooks/proxmox/artifacts/incident-2026-03-31/post-recovery-baseline/192.168.11.5.baseline.txt`
  - `runbooks/proxmox/artifacts/incident-2026-03-31/post-recovery-baseline/192.168.11.4.baseline.txt`
- Baseline results:
  - cluster health is fully recovered (`kubectl get nodes` shows all staging nodes `Ready`)
  - Talos health checks pass across the cluster
  - worker-2 machine status is `running/READY=true` and kubelet is `Running/HEALTH=OK`
  - `pve5` EVPN/VRF baseline is healthy and consistent
  - `pve4` SSH timed out during this specific snapshot window (`Operation timed out`), so this host baseline file currently records the access failure rather than full host state
- A reusable rapid verification script was added:
  - `scripts/check_staging_recovery.sh`
- Script purpose:
  - verify Proxmox host SSH reachability (`pve4`, `pve5`)
  - verify staging API and Talos ports
  - run Kubernetes node readiness check
  - run focused Talos checks for `cp-2` and `worker-2`

## Synology Findings

### What changed
- Added Synology inventory and playbooks under `ansible/synology/`.
- Added reusable detection scripts:
  - `scripts/identify_disconnected_synology_luns.sh`
  - `scripts/cleanup_empty_synology_iqns.py`

### Discovery logic corrections
- Initial orphan logic was wrong because the DSM LUN list payload did not expose authoritative mapping state.
- Mapping detection was corrected to use `/usr/syno/etc/iscsi_mapping.conf`.
- Live disconnected-target detection was corrected again to use `SYNO.Core.ISCSI.Target` `status` rather than `max_sessions`.
- Result: the correct filter for actionable cleanup was:
  - target name starts with `k8s-csi-pvc-`
  - target is still mapped
  - target `status != connected`

### Synology cleanup outcome
- Disconnected k8s LUN report was generated successfully.
- Empty-IQN report was generated successfully at `ansible/synology/logs/empty_iqn_targets_report.csv`.
- Automated LUN deletion worked for one batch and was logged in `ansible/synology/logs/lun_destroy_run_2026-03-31.txt`.
- Remaining LUN deletion attempts were rejected by DSM with hard feasibility error `18990505` and logged in `ansible/synology/logs/lun_destroy_run_2026-03-31_remaining.txt`.
- Those remaining disconnected k8s LUNs were then deleted manually in DSM.
- After manual DSM cleanup, disconnected k8s LUN detection returned empty again.

### Synology open item
- Empty k8s IQN targets still exist in `ansible/synology/logs/empty_iqn_targets_report.csv`.
- The current `SYNO.Core.ISCSI.Target method=delete` automation attempts do not yet have a verified working DSM contract for those empty targets.

## Terraform and Node Recreation Findings

### Legacy Terraform path
- The current `homelab_bootstrap` branch does not match the legacy staging state path.
- The working historical config path was the legacy `module.kubernetes-cluster[0].module.nodes...` layout.
- Recreating the exact staging nodes required using the matching historical commit rather than the current module layout.

### Node recreation results
- `k8s-stg-cp-2` recreated successfully:
  - VM ID `217`
  - IP `10.20.2.2`
- `k8s-stg-worker-2` recreated successfully:
  - VM ID `218`
  - IP `10.21.2.2`
- `k8s-stg-worker-1` was found to be genuinely broken due to stale/missing Proxmox disk state and was surgically recreated:
  - new VM ID `219`
  - IP `10.21.1.2`

### Safety findings during Terraform work
- Broad legacy Terraform plans were unsafe and could expand into unrelated resource actions.
- A targeted legacy apply attempted to affect `worker-1`, which required stopping and switching to state-audit-first recovery.
- Full state backup was taken before surgical state repair and recreation.

## Talos and Kubernetes Findings

### Healthy nodes
- `k8s-stg-cp-0` and `k8s-stg-cp-1` remained healthy control-plane nodes.
- `k8s-stg-worker-0` remained healthy.
- `k8s-stg-worker-1` was restored to `Ready` after recreation.

### Broken nodes
- `k8s-stg-cp-2` and `k8s-stg-worker-2` remained missing from Kubernetes after VM recreation.
- Talos API `50000` was reachable on both recreated nodes.
- The failure mode was not raw node loss; it was join failure.

### Key node-level symptom
- On the broken nodes, `kubelet` was running but requests via local kube-prism `127.0.0.1:7445` failed with `EOF`.
- Upstream connections from the broken nodes toward healthy control-plane API endpoints remained stuck in `SYN_SENT`.
- `10.20.2.2:6443` returned `Connection refused`, confirming `cp-2` had not become an active joined control-plane API endpoint.

## Proxmox SDN Investigation Findings

### Confirmed on `pve5`
- Expected staging objects exist and are up:
  - `stgctr`
  - `stgdata`
  - `vrf_stgl3`
  - `vxlan_stgctr`
  - `vxlan_stgdata`
- Expected staging gateway IPs exist on-host:
  - `10.20.0.1/24`
  - `10.20.1.1/24`
  - `10.20.2.1/24`
  - `10.21.0.1/24`
  - `10.21.1.1/24`
  - `10.21.2.1/24`
- FRR is active.
- EVPN/BGP sessions to `pve3` and `pve4` are established.
- VXLAN FDB entries exist for both remote VTEPs.
- Obvious host knobs were not missing:
  - `net.ipv4.ip_forward = 1`
  - `rp_filter = 2`
  - Proxmox firewall status reported `disabled/running`

### Dataplane behavior
- Despite correct control-plane-looking config, `pve5` itself could not complete connectivity to healthy staging control-plane endpoints:
  - `ping 10.20.0.2` failed
  - `ping 10.20.1.2` failed
  - `nc 10.20.0.2 6443` timed out
  - `nc 10.20.1.2 6443` timed out
- That matches the Talos-side `SYN_SENT` symptom seen from `cp-2` and `worker-2`.
- Packet-level capture on `pve5` during active probes confirms failed session establishment:
  - Repeated SYN egress from `10.20.2.2` and `10.21.2.2` toward `10.20.0.2:6443` and `10.20.1.2:6443`.
  - No corresponding successful SYN/SYN-ACK completion observed for the outbound probe flows.
  - Direct console capture further refines this: those SYNs are seen traversing `tap*`, staging bridge, and staging VRF interfaces on `pve5`, so the local guest-to-VRF handoff is working.
  - The same direct console capture also shows at least one remote SYN from `10.20.0.2` arriving over `vxlan_stgctr` to `cp-2`, so remote-to-guest overlay delivery is not universally broken.
  - Example evidence in capture: `10.21.2.2.57592 > 10.20.0.2.6443 [S]` repeated with probe timeout.
- Reference capture on `pve4` during the same method shows successful bidirectional flows to at least one healthy endpoint (`10.20.1.2:6443` opened and exchanged payload), proving the method and cluster API path are valid from other hosts.

### Management path instability
- Proxmox HTTPS/API `:8006` remained reachable while SSH `:22` to `pve3`, `pve4`, and especially `pve5` was intermittently timing out.
- SSH instability repeatedly interrupted deeper diagnostics, but a short successful capture window on `pve5` was eventually obtained and is now included in evidence.
- A later retry window showed simultaneous SSH timeouts to both `pve4` and `pve5`, captured in `live-remediation` artifacts above.

### Current best diagnosis
- The staging SDN issue is real.
- It is not a missing bridge, missing VXLAN, missing FRR, or missing route object problem.
- It is a staging inter-host dataplane problem, most visible on `pve5`, with management-plane instability on the same host adding noise.
- Because `cp-2` and `worker-2` both live on `pve5`, they cannot reach the active staging control plane well enough to join.

## Root Cause Statement (Current Best Evidence)
- There is a host-localized staging SDN dataplane failure affecting `pve5`.
- The host has expected EVPN/VXLAN/VRF control-plane state, but staging payload traffic from `pve5` to remote staging control-plane IPs does not establish.
- This prevents kube-prism, kubelet, and etcd join traffic from succeeding on `k8s-stg-cp-2` and `k8s-stg-worker-2`.
- This is the primary blocker for staging recovery at this point.

## Evidence Summary
- Existing prior incident context:
  - `runbooks/proxmox/ceph-csi-incident-remediation-2026-03-30.md`
  - `runbooks/proxmox/artifacts/incident-2026-03-30/mon-network-snapshot-20260330-162316.log`
- Synology evidence:
  - `ansible/synology/logs/lun_destroy_run_2026-03-31.txt`
  - `ansible/synology/logs/lun_destroy_run_2026-03-31_remaining.txt`
  - `ansible/synology/logs/empty_iqn_targets_report.csv`
  - `ansible/synology/logs/orphan_luns_dry_run_report.txt`
- Operator command transcript evidence from this incident window included:
  - direct `pve5` reachability tests to `10.20.0.2`, `10.20.1.2`, and `10.21.1.2`
  - host-side `ip`, `bridge fdb`, `vtysh`, `sysctl`, and `pve-firewall` comparisons
  - successful short `tcpdump`+probe captures from `pve5` and reference host `pve4`
  - capture artifacts:
    - `runbooks/proxmox/artifacts/incident-2026-03-31/pve5-staging-6443-probe-capture.txt`
    - `runbooks/proxmox/artifacts/incident-2026-03-31/pve4-staging-6443-reference-capture.txt`
    - `runbooks/proxmox/artifacts/incident-2026-03-31/live-remediation/pve5-stg6443-live-capture.txt`

## Next Steps for Staging

### Immediate next steps
1. Stabilize management access to `pve5` first.
   - Confirm whether SSH flaps are a separate host issue or related to the same underlay problem.
   - Keep `:8006` API access available as fallback.
  - Validate SSH service health and host-level CPU/memory pressure during timeout windows.
  - If direct management remains down, use out-of-band host access (console/IPMI) to restore SSH/API availability before continuing SDN remediation.
2. Capture packets directly on `pve5` during a controlled probe.
   - Run:
     - `tcpdump -ni any '(host 10.20.0.2 or host 10.20.1.2) and tcp port 6443'`
     - then probe `10.20.0.2:6443` and `10.20.1.2:6443` from `pve5`
   - Goal: determine whether packets leave, whether replies return, and whether anything is locally reset or dropped.
3. Compare `pve5` staging bridge, EVPN, and VRF behavior against a healthy host during the same capture window.
   - Focus on `pve4` as the comparison point because it currently hosts a healthy staging worker.

### Host-network remediation path
1. Audit `pve5` host firewall and nftables/ebtables state beyond `pve-firewall status`.
2. Compare bridge forwarding database churn and MAC learning between `pve5` and `pve4`.
3. Compare FRR EVPN route installation counts and per-VNI remote MAC/IP advertisements between hosts.
4. Validate underlay path between VTEPs `192.168.1.241`, `192.168.1.242`, and `192.168.11.5` while staging traffic is probed.

### After host-network fix
1. Re-test from `pve5`:
   - `ping 10.20.0.2`
   - `ping 10.20.1.2`
   - `nc -vz -w 3 10.20.0.2 6443`
   - `nc -vz -w 3 10.20.1.2 6443`
2. Re-test from nodes:
   - `k8s-stg-cp-2`
   - `k8s-stg-worker-2`
3. Verify Talos and Kubernetes recovery:
   - `talosctl health`
   - `talosctl etcd members`
   - `kubectl get nodes -o wide`
4. Only after network recovery, revisit any remaining Talos config reconciliation if `cp-2` or `worker-2` still do not register.

### Synology follow-up
1. Keep the disconnected-LUN detector and current reports; they are now using the correct live target-state heuristic.
2. If empty IQN cleanup is still needed, reverse-engineer the exact DSM `Target delete` workflow or remove those empty targets manually in DSM and then re-run the report.

## Operational Cautions
- Do not run broad legacy Terraform applies against the historical staging module path without first reviewing the full plan.
- Continue using state-backup-first and targeted actions for any further staging rebuilds.
- Do not assume Talos config changes will fix `cp-2` and `worker-2` until the `pve5` staging dataplane issue is resolved.