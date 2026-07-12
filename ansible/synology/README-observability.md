# Synology backup observability

`configure_grafana_log_forwarding.yml` configures Kermit and Scooter to forward
DSM RFC 5424 syslog over TCP to the production Promtail endpoint at
`10.31.0.74:514`.

The playbook also installs a five-minute systemd timer. Its collector emits
structured JSON events for:

- Snapshot Replication package state, replica count, replication schedules,
  local snapshot schedules, and retention policies on both NAS units.
- The latest Active Backup for Business result for each task when the ABB
  activity database is present (currently Kermit).

Run it from the repository root with credentials supplied from Vault:

```bash
export VAULT_ADDR=https://vault.myrobertson.net:8200
secret_json="$(vault read -format=json secret/data/synology/dsm-admin/local-ssh-account)"
extra="$(printf '%s' "$secret_json" | jq -c '{ansible_user:.data.data.username,ansible_password:.data.data.password,ansible_become_password:.data.data.password}')"
.venv/bin/ansible-playbook -i inventory/environments/production.ini \
  ansible/synology/configure_grafana_log_forwarding.yml --extra-vars "$extra"
```

Grafana provisions the **Synology / Snapshot Replication** and
**Synology / Active Backup for Business** dashboards from `homelab_flux`.
The Loki stream uses `job="synology"`, `nas`, `app`, `severity`, and `facility`
labels. Raw DSM logs and the structured `synology-observability` events are
retained according to the production Loki retention policy.
