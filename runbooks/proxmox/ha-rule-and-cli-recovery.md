# HA Rule Recovery and `ha-manager` CLI Stabilization

## Scenario
After maintenance, HA rule weights need to be restored and `ha-manager` may crash/segfault on one node.

## Update node-affinity rule
`pvesh set` on this endpoint requires `--type`.

```bash
RULE="ha-rule-9356c7af-d835"
pvesh set /cluster/ha/rules/${RULE} \
  --type node-affinity \
  --nodes 'pve3:3,pve4:3,pve5:3'

pvesh get /cluster/ha/rules/${RULE} --output-format json-pretty
```

## Validate HA from API (authoritative)
```bash
pvesh get /cluster/ha/status/current --output-format json-pretty | sed -n '1,200p'
```

## Recover `ha-manager` CLI if unstable
```bash
systemctl restart pve-ha-lrm pve-ha-crm
sleep 2
ha-manager status || true
apt-get update -qq
apt-get install --reinstall -y pve-ha-manager
ha-manager status
```

## Success criteria
- HA rule shows expected `nodes` weights.
- `pvesh get /cluster/ha/status/current` returns healthy quorum/master/lrm objects.
- `ha-manager status` runs without segfault.
