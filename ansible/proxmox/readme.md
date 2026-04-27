# Proxmox Ansible Bundle

This directory contains playbooks, roles, and templates for managing Proxmox clusters and related Ceph infrastructure.

## Main playbooks

- `proxmox.yml`: inventory configuration for Proxmox nodes, using the `community.proxmox.proxmox` dynamic inventory plugin
- `ceph_admin_portal.yml`: configure Ceph Dashboard bind settings and admin login credentials
- `ceph_object_gw.yml`: deploy Ceph Object Gateway on Proxmox nodes
- `rolling_restart.yml`: safely reboot Proxmox nodes one at a time
- `regular_maintenance.yml`: apply rolling package updates, cleanup, and reboot only when required
- `cpu_thermal_policy.yml`: persist the Proxmox CPU thermal policy across reboots
- `intel_vpro.yml`: configure Intel vPro interfaces on Proxmox nodes
- `provision_certificates.yml`: configure Vault PKI and Vault Agent automation for Proxmox API certificates
- `disable_vlan_hw_filtering.yml`: disable VLAN hardware filtering on Proxmox interfaces and keep it persistent across reboots
- `configure_thunderbolt_transport.yml`: point Proxmox live migration and scheduled replication at the Thunderbolt ring
- `ceph_thunderbolt_cluster_network.yml`: move Ceph OSD backend replication/recovery traffic onto the Thunderbolt ring
- `pbs_thunderbolt_proxy.yml`: publish the Scooter-hosted PBS API through a Proxmox Thunderbolt service IP
- `pbs_config_export_to_vault.yml`: archive PBS configuration into Vault for disaster recovery
- `pbs_config_restore_from_vault.yml`: stage or apply a PBS configuration archive from Vault onto a replacement PBS VM
- `proxmox_transport_metrics.yml`: export Proxmox migration, replication, backup storage route, and Ceph transport metrics through node-exporter textfile collection
- `pbs_guest_agent_freeze_audit.yml`: audit PBS-backed QEMU VMs for Proxmox guest-agent enablement, guest-agent responsiveness, and filesystem freeze/thaw readiness
- `configure_authelia_sso.yml`: seed Proxmox/Authelia OIDC secrets in Vault and configure the Proxmox OpenID Connect realm for Authelia SSO
- `configure_pbs_authelia_sso.yml`: seed PBS/Authelia OIDC secrets in Vault and configure the Proxmox Backup Server OpenID Connect realm for Authelia SSO

## Roles and templates

- `roles/`: reusable automation for Ceph dashboard, object gateway, Intel vPro, SFP28 fabric, and Thunderbolt networking
- `templates/`: Jinja2 templates used by the playbooks and roles in this bundle
- `handlers/`: custom handlers for service restarts and similar notifications

## Usage

Run from the repository root unless noted otherwise:

```sh
ansible-playbook -i inventory/proxmox.yml ansible/proxmox/ceph_admin_portal.yml --extra-vars @inventory/ceph_dashboard_credentials.vault.yml --ask-vault-pass
ansible-playbook -i inventory/proxmox.yml ansible/proxmox/ceph_object_gw.yml
ansible-playbook -i inventory/proxmox.yml ansible/proxmox/rolling_restart.yml
ansible-playbook -i inventory/proxmox.yml ansible/proxmox/regular_maintenance.yml
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/cpu_thermal_policy.yml --limit proxmox_nodes
ansible-playbook -i inventory/proxmox.yml ansible/proxmox/intel_vpro.yml
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/provision_certificates.yml --limit proxmox_cert_nodes
ansible-playbook -i inventory/proxmox.yml ansible/proxmox/disable_vlan_hw_filtering.yml
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/configure_thunderbolt_transport.yml
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/ceph_thunderbolt_cluster_network.yml
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/pbs_thunderbolt_proxy.yml
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/pbs_config_export_to_vault.yml -e pbs_config_source_hosts=pbs.myrobertson.net
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/pbs_config_restore_from_vault.yml -e pbs_config_restore_hosts=pbs-restore.myrobertson.net
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/pbs_guest_agent_freeze_audit.yml
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/proxmox_transport_metrics.yml
VAULT_ADDR=https://vault.myrobertson.net:8200 VAULT_TOKEN=<token> ansible-playbook -i inventory/environments/production.ini ansible/proxmox/configure_authelia_sso.yml
VAULT_ADDR=https://vault.myrobertson.net:8200 VAULT_TOKEN=<token> ansible-playbook -i inventory/environments/production.ini ansible/proxmox/configure_pbs_authelia_sso.yml
```

Adjust the inventory path to match the environment you are targeting.

### Core site tag usage

When running the core orchestration playbook, certificate automation can be targeted or skipped with tags:

- Run only certificate automation:

```sh
ansible-playbook playbooks/core/site.yml --tags certs
```

- Skip certificate automation:

```sh
ansible-playbook playbooks/core/site.yml --skip-tags certs
```

## Proxmox certificate automation

`provision_certificates.yml` manages Proxmox API certificates with Vault Agent and applies nodes one at a time with `serial: 1`.

Required node-side prerequisites:

- `vault` CLI installed on each Proxmox node
- permission to restart `pveproxy` after certificate updates
- `proxmox_cert_cluster_san` set per cluster group so each node receives only the correct VIP SAN

Supported bootstrap modes:

- Delegated Vault bootstrap: define a `[vault]` group, keep `/root/.vault-token` on that Vault host, and let the playbook fetch or mint the AppRole inputs it needs.
- Inventory-preseeded bootstrap: provide `proxmox_vault_addr`, `proxmox_cert_vault_role_id`, and `proxmox_cert_vault_secret_id` directly in inventory when no delegated Vault host is part of the run.

TLS trust behavior:

- When `proxmox_vault_skip_verify=false`, the playbook installs `vault-ca.pem` for Vault Agent trust.
- If no delegated Vault host is present, the playbook can reuse an existing `vault-ca.pem` from the first targeted Proxmox node.
- If the cluster has never been bootstrapped before, do a single-node canary first so later runs have CA material to reuse.

The certificate apply helper writes into `/etc/pve/local` with `cp` instead of `install`. That is intentional: `/etc/pve` is backed by `pmxcfs`, and metadata-setting `install(1)` writes can fail there with `Operation not permitted`.

## Proxmox CPU thermal policy

`cpu_thermal_policy.yml` installs and enables `proxmox-cpu-thermal-policy.service` on each targeted Proxmox node. The service applies `intel_pstate/no_turbo=1` at boot so identical hosts use the same thermal policy and avoid sustained package temperatures above the warning threshold.

Run it against all identical Proxmox nodes together:

```sh
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/cpu_thermal_policy.yml --limit proxmox_nodes
```

### Suggested rollout

1. Add or verify a target group such as `[proxmox_cert_nodes_cl0]` in `inventory/environments/production.ini`, and set `proxmox_cert_cluster_san` for that group.
2. Run a canary node:

```sh
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/provision_certificates.yml --limit pve4.example.net
```

3. Verify the served certificate:

```sh
openssl s_client -connect pve4.example.net:8006 -servername pve4.example.net </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates
```

4. Roll out to the rest of the cluster:

```sh
ansible-playbook -i inventory/environments/production.ini ansible/proxmox/provision_certificates.yml --limit proxmox_cert_nodes_cl0
```

5. Validate the dynamic inventory with the trusted CA bundle if needed:

```sh
REQUESTS_CA_BUNDLE=inventory/certs/proxmox-vault-ca.pem ansible-inventory -i inventory/proxmox.yml --graph
```

## Notes

- `inventory/proxmox.yml` is a dynamic inventory file using the `community.proxmox.proxmox` plugin, not a custom script.
- `inventory/proxmox.yml` and `ansible/proxmox/proxmox.yml` use `validate_certs: true` and expect trusted Proxmox API certificates.
- `ansible.cfg` in this directory sets inventory and SSH options specific to Proxmox automation.
- See the top-level [README](../../README.md) for repository-wide setup and [inventory/README.md](../../inventory/README.md) for static inventory examples.

## Runbooks

- Operational Proxmox runbooks live under `runbooks/proxmox/` at the repository root.
- Start with [runbooks/proxmox/README.md](../../runbooks/proxmox/README.md).
- PBS migration from Scooter to Proxmox for Thunderbolt-routed backup traffic is documented in [runbooks/proxmox/pbs-scooter-to-proxmox-thunderbolt-migration.md](../../runbooks/proxmox/pbs-scooter-to-proxmox-thunderbolt-migration.md).
- PBS configuration recovery from Git plus Vault is documented in [runbooks/proxmox/pbs-config-dr-from-vault.md](../../runbooks/proxmox/pbs-config-dr-from-vault.md).
