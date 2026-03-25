# homelab_ansible

Host and node configuration automation for the homelab infrastructure outside Kubernetes manifests.

## Repository purpose

This repository is the Ansible layer for non-Kubernetes infrastructure operations, including:

- PowerDNS authoritative and recursor configuration
- Vault host configuration and service management
- Proxmox and Ceph host workflows
- NetBox data-model and IPAM population automation
- SSH/sudo helper scripts for onboarding and remote access hygiene

## Logical layout

Canonical paths are organized by function:

```
.
├── ansible/                         # domain-specific automation bundles
│   ├── domain/
│   ├── netbox/
│   └── proxmox/
├── inventory/
│   ├── environments/                # canonical static inventories
│   │   ├── production.ini
│   │   └── staging.ini
│   ├── production.ini               # minimal stub for backward compatibility (see environments/production.ini)
│   ├── staging.ini                  # minimal stub for backward compatibility (see environments/staging.ini)
│   └── proxmox.yml                  # dynamic proxmox inventory plugin config
├── playbooks/
│   └── core/                        # canonical root playbooks
│       ├── site.yml
│       ├── powerdns_servers.yml
│       └── vault_server.yml
├── roles/
│   ├── pdns-ansible/                # git submodule
│   └── postgresql/
├── scripts/
│   ├── ceph_pve4_containment.sh
│   ├── enable_passwordless_sudo_remote.sh
│   └── enable_ssh_key_access.sh
├── ceph-ansible/                    # git submodule (upstream Ceph roles/playbooks)
├── ansible.cfg                      # default config (staging inventory)
├── site.yml                         # compatibility entrypoint -> playbooks/core/site.yml
├── powerdns_servers.yml             # compatibility entrypoint -> playbooks/core/powerdns_servers.yml
└── vault_server.yml                 # compatibility entrypoint -> playbooks/core/vault_server.yml
```

## Compatibility behavior

To avoid breaking existing workflows, root playbook names are preserved as wrappers:

- `site.yml` imports `playbooks/core/site.yml`
- `powerdns_servers.yml` imports `playbooks/core/powerdns_servers.yml`
- `vault_server.yml` imports `playbooks/core/vault_server.yml`

This means old commands still work while new canonical paths are available.

## Getting started

### 1) Activate the local virtual environment

```bash
source bin/activate
```

### 2) Verify Ansible defaults

```bash
ansible --version
ansible-config dump --only-changed
```

The default inventory in `ansible.cfg` is:

- `./inventory/environments/staging.ini`

### 3) Run core playbooks

Canonical path:

```bash
ansible-playbook playbooks/core/site.yml
```

Compatibility path (still valid):

```bash
ansible-playbook site.yml
```

## Core automation areas

### Root/core playbooks

- `playbooks/core/site.yml` orchestrates:
	- `playbooks/core/powerdns_servers.yml`
	- `playbooks/core/vault_server.yml`
	- `ansible/proxmox/provision_certificates.yml` (tagged: `certs`, `proxmox_certs`)

Tag examples for `playbooks/core/site.yml`:

- Run only certificate automation:
	- `ansible-playbook playbooks/core/site.yml --tags certs`
- Skip certificate automation:
	- `ansible-playbook playbooks/core/site.yml --skip-tags certs`

### Proxmox bundle

Location: `ansible/proxmox/`

Common playbooks:

- `create_thunder_ring.yml` (Thunderbolt interfaces + FRR fabric)
- `ceph_object_gw.yml` (Ceph object gateway role application)
- `rolling_restart.yml` (serial reboot workflow)
- `provision_certificates.yml` (Vault Agent-managed Proxmox API certificate provisioning and rotation)
	- set `proxmox_cert_cluster_san` per cluster group (for example `cl0.myrobertson.net` or `cl1.myrobertson.net`)

### NetBox bundle

Location: `ansible/netbox/`

Includes inventory/data build playbooks such as:

- `populate_netbox_ipam.yml`
- `create_prefixes.yml`
- `create_vlan_interfaces.yml`
- `assign_ip_addresses.yml`

### Domain bundle

Location: `ansible/domain/`

Currently includes domain documentation and a placeholder playbook (`domain.yml`).

## Inventory model

- Static inventories:
	- `inventory/environments/staging.ini`
	- `inventory/environments/production.ini`
- Dynamic Proxmox plugin inventory:
	- `inventory/proxmox.yml`
	- `ansible/proxmox/proxmox.yml`

## Helper scripts

Under `scripts/`:

- `enable_ssh_key_access.sh` - installs/verifies SSH key access
- `enable_passwordless_sudo_remote.sh` - configures and validates NOPASSWD sudo remotely
- `ceph_pve4_containment.sh` - read-only Ceph/Proxmox diagnostic/containment checks

## Submodules

This repository uses git submodules:

- `ceph-ansible` (upstream Ceph automation source)
- `roles/pdns-ansible` (PowerDNS role source)

After cloning:

```bash
git submodule update --init --recursive
```

## Related repositories

This repository is one part of a shared homelab stack:

- [homelab_bootstrap](https://github.com/richrobertson/homelab_bootstrap) - first-stage cluster bootstrap/orchestration before Flux management.
- [homelab_ansible](https://github.com/richrobertson/homelab_ansible) - host and node configuration automation outside Kubernetes manifests.
- [homelab_flux](https://github.com/richrobertson/homelab_flux) - in-cluster GitOps state (apps, controllers, configs, and gateway resources).
