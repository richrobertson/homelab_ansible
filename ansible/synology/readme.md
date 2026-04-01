# Synology LUN Cleanup Runbook

This folder includes playbooks to discover and clean likely orphaned iSCSI LUNs on Synology NAS 192.168.1.215.
The playbooks target the inventory group `synology_nas` and use host vars from `inventory/environments/synology.ini`.

## Playbooks

- `preflight_nas_connectivity.yml`: Verifies SSH reachability, API auth, and LUN endpoint shape.
- `discover_orphaned_luns.yml`: Lists likely orphaned LUN UUIDs using mapping heuristics.
- `cleanup_orphaned_luns.yml`: Deletes explicitly provided orphan LUN UUIDs.
- `discover_and_cleanup_orphaned_luns.yml`: One-shot flow for discovery + optional cleanup.

## 1) Preflight (always run first)

```bash
ansible-playbook ansible/synology/preflight_nas_connectivity.yml \
  -i inventory/environments/synology.ini
```

## 2) Discovery-only

```bash
ansible-playbook ansible/synology/discover_orphaned_luns.yml \
  -i inventory/environments/synology.ini
```

Optionally exclude known-safe names by regex pattern list:

```bash
ansible-playbook ansible/synology/discover_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e 'synology_lun_name_exclude_patterns=["prod","backup"]'
```

By default, discovery succeeds when no orphaned LUNs are found and reports an empty result.
Enable strict non-empty behavior if your automation requires it:

```bash
ansible-playbook ansible/synology/discover_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e synology_fail_when_none_found=true
```

## 3) Cleanup with explicit UUIDs

Dry-run preview:

```bash
ansible-playbook ansible/synology/cleanup_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e 'orphan_lun_uuids=["uuid-1","uuid-2"]'
```

Actual deletion:

```bash
ansible-playbook ansible/synology/cleanup_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e 'orphan_lun_uuids=["uuid-1","uuid-2"]' \
  -e perform_cleanup=true
```

## 4) One-shot discover + cleanup

Discovery and preview only:

```bash
ansible-playbook ansible/synology/discover_and_cleanup_orphaned_luns.yml \
  -i inventory/environments/synology.ini
```

The one-shot playbook also succeeds by default when no candidates are found.
To force a non-zero exit when discovery returns no orphan UUIDs:

```bash
ansible-playbook ansible/synology/discover_and_cleanup_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e synology_fail_when_none_found=true
```

Actual cleanup (requires explicit confirmation gate):

```bash
ansible-playbook ansible/synology/discover_and_cleanup_orphaned_luns.yml \
  -i inventory/environments/synology.ini \
  -e perform_cleanup=true \
  -e cleanup_confirmation=I_UNDERSTAND_DELETE_IS_DESTRUCTIVE
```

## Audit logging

When cleanup is enabled, deletion runs append a UTC record to:

- `ansible/synology/logs/lun_cleanup_audit.log`

Disable logging if needed:

```bash
-e synology_cleanup_audit_log_enabled=false
```

Override log file path:

```bash
-e synology_cleanup_audit_log_path=ansible/synology/logs/custom_audit.log
```

## Notes

- API command syntax can vary by DSM version. If needed, override:
  - `synology_remote_list_luns_cmd`
  - `synology_remote_delete_lun_cmd_template`
- Keep cleanup in preview mode first and confirm UUIDs in DSM/SAN Manager before deletion.