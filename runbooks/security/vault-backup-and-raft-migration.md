# Vault Backup, Key Custody, and the Raft Migration

Vault is the trust root for the entire estate. This runbook covers what can be
backed up today, what cannot, the key-custody problem that makes the current
backups dangerous rather than merely incomplete, and the concrete migration that
removes the constraint.

Companion material:

- `playbooks/core/vault_offsite_snapshot.yml` — the snapshot job this describes
- `playbooks/core/vault_server.yml` — the server configuration being changed
- `homelab_flux/infrastructure/configs/vault-alerts.yaml` — the alert rules
- `runbooks/proxmox/vault-connectivity-and-cert-agent-recovery.md` — network recovery
- `runbooks/security/credential-lifecycle-roadmap.md` — the rotation work this depends on

## Measured starting state

All figures verified against the live estate on 2026-07-29.

| Property | Value | Source |
|----------|-------|--------|
| Vault version | 1.20.4 OSS | `sys/health` |
| Storage backend | **`file`** | `sys/seal-status` → `"storage_type":"file"` |
| `sys/storage/raft/configuration` | **unsupported path** | 404 — raft APIs do not exist |
| Seal type | Shamir, 5 shares, threshold 3 | `sys/seal-status` |
| HA | **disabled** | `sys/leader` → `"ha_enabled":false`, empty `leader_address` |
| Prometheus telemetry | **not enabled** | `sys/metrics?format=prometheus` → HTTP 400 `prometheus is not enabled` |
| Scrape target in Prometheus | **none** | no `vault` job in kube-prometheus-stack |
| node_exporter on the VM | **none** | 192.168.7.128:9100 closed |
| VM location | **VM 119 on pve4**, HA-managed, `hastate: started` | `/cluster/resources` |
| VM backup job | `vault-vm-119-nightly` → `pbs-s3`, daily 04:30 | `/cluster/backup` |
| That job in Git | **not present** | no match anywhere in the monorepo |
| Client-side encryption on `pbs-s3` | **none** | storage config has no `encryption-key`; snapshots report `encrypted: null` |

Two corrections to the standing audit are worth recording, because both change
what needs doing:

1. **Vault is not unbacked-up.** `vault-vm-119-nightly` has been running daily
   and has 13 retained snapshots, the most recent from 2026-07-29T11:30Z. The
   VM has `agent = 1`, so vzdump `mode: snapshot` drives a qemu-guest-agent
   fs-freeze and the resulting image is filesystem-consistent, not merely
   crash-consistent. As a copy of the Vault barrier it is technically valid.
2. **The runbook location is stale.** VM 119 now runs on **pve4**, not pve3.
   `runbooks/proxmox/vault-connectivity-and-cert-agent-recovery.md:13` should be
   corrected, or better, stop naming a node: the VM is HA-managed and will move.

The real problems are different from "no backup", and worse in one respect.

## The actual findings

### 1. The Vault VM backup is unencrypted, offsite, and contains the unseal keys

`pbs-s3` is an AWS S3-backed PBS datastore with **no client-side encryption
key**. `pbs-b2-encrypted` has one; `pbs-s3` does not. So every night a
filesystem-consistent image of VM 119 is written to an S3 bucket in a form that
PBS — and anyone with read access to the datastore or the bucket — can open.

That image contains, on the same root filesystem:

| Path | Contents |
|------|----------|
| `/root/.vault_unseal_keys.gpg` | all 5 Shamir shares |
| `/root/.gpg_passphrase` | the plaintext passphrase that decrypts the above |
| `/root/.vault-token` | a persistent root token |
| `/opt/vault/data` | the Vault barrier itself |

The 3-of-5 threshold provides **no protection whatsoever** in this arrangement.
All five shares and the passphrase that decrypts them travel together, alongside
the data they protect. Recovering the estate's every secret from a copy of that
backup requires no cryptanalysis — just `gpg -d` and the file that sits next to
it.

This is a confidentiality finding, and it is more urgent than the availability
finding that prompted this work.

### 2. Do not "fix" this by pointing VM 119 at `pbs-b2-encrypted`

The obvious remediation is to add `119` to `pbs_encrypted_backup_vmids` in
`ansible/proxmox/pbs_client_encrypted_backups.yml`. **Do not.** That job's
client encryption key is fetched from
`secret/proxmox/pbs/prod/client-encryption/pbs-b2-encrypted` — i.e. from Vault.
Encrypting Vault's own backup with a key that only exists inside Vault creates
exactly the deadlock the audit feared: Vault is gone, so the key is gone, so the
backup that contains Vault cannot be opened.

Today no such deadlock exists, precisely *because* `pbs-s3` is unencrypted. The
fix must add encryption **and** break the key dependency at the same time. Any
key used to protect a Vault backup must be escrowed outside Vault.

### 3. The file backend has no online snapshot API

`vault operator raft snapshot save` is a raft-backend feature. Against this
server it returns:

```
Error taking the snapshot: Error making API request.
URL: GET https://vault.myrobertson.net:8200/v1/sys/storage/raft/snapshot
Code: 404.
```

There is no file-backend equivalent. Vault offers no consistent online export of
the file backend at all — not via `sys/`, not via the CLI. The options are:

| Method | Consistent? | Downtime | Verdict |
|--------|-------------|----------|---------|
| `vault operator raft snapshot save` | yes | none | **unavailable** on `file` |
| Cold copy of `/opt/vault/data`, service stopped | yes | seconds | the only guaranteed method today |
| Hot copy of `/opt/vault/data`, service running | **no** | none | can capture a torn write; not a backup |
| vzdump with guest-agent fs-freeze | yes | none | valid, but 34 GB per copy and whole-VM granularity |
| Per-path `vault kv get` export | no | none | misses auth mounts, policies, PKI issuers, leases, entities. Not a backup |

`playbooks/core/vault_offsite_snapshot.yml` implements the cold copy, detects the
backend on every run, and switches to the raft API automatically once the
migration below is done.

### 4. Nothing observes any of it

Vault has no telemetry stanza, no scrape target, and no alerts. The nightly VM
backup has no freshness signal either. A Vault that sealed itself at 02:00 would
be discovered by a human noticing that something else broke.

## Key custody

### The constraint, stated honestly

Auto-unseal with Shamir requires the machine to hold enough key material to
unseal itself. There is no arrangement of file permissions that changes this: if
`vault-unseal.service` can unseal Vault unattended, then anyone who can read
that VM's disk can too. Splitting shares across directories, or holding 3 on the
VM and 2 elsewhere, buys nothing — 3 *is* the threshold.

So the choice is real and cannot be engineered away:

| Option | Unseal after reboot | Key material on VM 119 | Cost |
|--------|--------------------|------------------------|------|
| A. Status quo | automatic | all 5 shares + passphrase | full compromise from one disk read |
| B. Protect the backups only | automatic | unchanged | cheap; closes the offsite exposure, not the host exposure |
| C. Manual unseal | **none** — estate stays down until a human arrives | none | strongest, worst availability |
| D. AWS KMS auto-unseal | automatic | IAM credential only | removes Shamir material entirely |

### Recommendation

**Do B now, plan D, keep C as the break-glass.**

**B — minimum viable fix, this week.** Two changes, neither of which touches
Vault itself:

1. Codify `vault-vm-119-nightly` in Ansible. It exists only as live Proxmox
   state; a cluster rebuild loses it silently and nothing alerts.
2. Give it client-side encryption with a key that is **not** stored in Vault.
   Generate a dedicated PBS encryption key, escrow it offline (printed and in a
   sealed envelope, plus a copy in a password manager that does not depend on
   this estate), and reference it from the job. This is the single highest-value
   change in this document: it closes an unencrypted offsite copy of the estate's
   entire trust root.

**D — the strategic fix.** `seal "awskms"` removes Shamir material from the VM
completely: Vault unseals by calling KMS, and the root of trust becomes an IAM
credential plus a KMS key policy, both of which are revocable and audited.
This estate already runs AWS under Terraform (`homelab_bootstrap/terraform/mail_edge`,
the `myrobertson-homelab-pbs` bucket, `secret/aws/user`), so this is an
incremental addition rather than a new dependency. The task brief is right that
Vault transit auto-unseal is impossible with one Vault — but KMS auto-unseal is
not, and it is the standard answer to exactly this problem.

Note the trade: KMS makes Vault's availability depend on reaching AWS. Keep the
Shamir shares escrowed offline as the break-glass path (`-migrate` back to
Shamir is supported).

**Independently of all of the above: the root token should not exist on disk.**
`/root/.vault-token` holds a persistent root token used by
`playbooks/core/vault_server.yml` for PKI role management. A root token that
never expires is a standing credential with no lifecycle, which is precisely the
pattern `runbooks/security/credential-lifecycle-roadmap.md` exists to eliminate.
Replace it with an AppRole carrying a narrow PKI policy — the playbook already
uses AppRole for the cert agent, so the pattern is present. Then revoke the root
token. When root is genuinely needed, `vault operator generate-root`
reconstructs one from the unseal shares, which means root access is gated by the
same quorum that protects the barrier, rather than by a file mode.

## Enabling metrics

Vault must be told to expose Prometheus metrics; it does not by default.
`vault.hcl` is generated by `playbooks/core/vault_server.yml`, so the change
belongs in that template, not on the host.

Add a telemetry stanza and unauthenticated metrics access to the listener:

```hcl
telemetry {
  prometheus_retention_time = "24h"
  disable_hostname          = true
}

listener "tcp" {
  # ... existing settings ...
  telemetry {
    unauthenticated_metrics_access = true
  }
}
```

`unauthenticated_metrics_access` is deliberate. The alternative is a Vault token
in Prometheus, which must be renewed by something — and the thing that would
renew it is VSO, which depends on Vault. Monitoring for Vault must not depend on
Vault being healthy, so token-authenticated metrics are the wrong choice here.
The exposure is a set of operational counters on an internal VLAN; no secret
material is served on that path.

Then add the scrape jobs. These belong in
`homelab_flux/infrastructure/controllers/kube-prometheus-stack/release.yaml`
under the existing `additionalScrapeConfigs` list:

```yaml
          - job_name: vault
            scheme: https
            metrics_path: /v1/sys/metrics
            params:
              format: [prometheus]
            scrape_interval: 30s
            tls_config:
              # Vault serves a cert from its own pki_int, which the cluster
              # does not trust; the target is pinned by address on an
              # internal VLAN.
              insecure_skip_verify: true
            static_configs:
              - targets:
                  - vault.myrobertson.net:8200
                labels:
                  domain: vault
          - job_name: vault-node
            scrape_interval: 30s
            static_configs:
              - targets:
                  - vault.myrobertson.net:9100
                labels:
                  domain: vault
```

`vault-node` is not redundant. A sealed Vault stops serving `/v1/sys/metrics`,
so the `vault` job goes down for both "sealed" and "host dead". node_exporter on
the same VM — installed by `playbooks/core/vault_offsite_snapshot.yml` — is what
lets `VaultDown` and `VaultHostUnreachable` tell those apart. It also carries the
snapshot freshness metrics.

## The raft migration

### What it buys, and what it does not

Migrating a single node to raft gives:

- `vault operator raft snapshot save` — consistent snapshots with **zero
  downtime**, replacing the stop-copy-start cycle
- `vault operator raft snapshot restore` — a supported, tested restore path
- autopilot state and raft metrics
- the prerequisite for real HA later

It does **not** give high availability. One raft node is still one VM and still
a single point of failure. HA needs three voting nodes, which is a separate and
much larger change (three VMs, anti-affinity across pve3/pve4/pve5, a load
balancer or client-side retry_join). Do not let "migrated to raft" be recorded
as "Vault is no longer a SPOF" — it is not.

### Requirements

- **`disable_mlock = true` is mandatory.** Raft mmaps its BoltDB file and Vault
  refuses to start on raft without it. This is the one that will bite:
  `playbooks/core/vault_server.yml` computes `disable_mlock` from whether the
  systemd unit sets `LimitMEMLOCK=infinity`, and the HashiCorp package unit
  does. So today `disable_mlock` is almost certainly **not** written into
  `vault.hcl`, and raft will refuse to start until it is. Verify before the
  window — SSH to the VM is blocked from the automation sandbox, so this is a
  console check:

  ```bash
  grep -E 'disable_mlock|storage' /etc/vault.d/vault.hcl
  systemctl cat vault | grep -i memlock
  ```

  If `disable_mlock` is absent, force it for the raft run by setting
  `vault_disable_mlock: true` explicitly rather than letting the fact be derived.

- `cluster_addr` and `api_addr` must be set. Both already are.
- The listener needs a `cluster_address`. Already `0.0.0.0:8201`.
- A unique `node_id`.
- Free disk for a second full copy of the barrier during migration. The dataset
  is small relative to the 32 GB disk, but confirm with `du -sh /opt/vault/data`.
- The Shamir shares. Storage migration does **not** change the seal, so the
  existing 5 shares and threshold of 3 continue to work unchanged. No
  `-migrate` unseal flag is needed — that flag is for *seal* migration, which
  this is not.

### Downtime

`vault operator migrate` requires Vault to be **stopped**; it takes an exclusive
lock on the source. For a dataset of this size expect the copy itself to take
seconds. Budget a **30-minute maintenance window**: most of it is verification
and rollback headroom, not migration.

Everything that depends on Vault is down for the window: VSO sync, the Proxmox
`vault-agent-*` cert agents, and any PBS client-encryption key lookup. Schedule
away from the 03:00 snapshot and 04:30/05:30 vzdump jobs.

### Procedure

Take a cold snapshot first — this is the rollback artifact:

```bash
systemctl stop vault
tar -C /opt/vault -czf /root/vault-data-pre-raft-$(date -u +%Y%m%dT%H%M%SZ).tar.gz data
```

Migrate into a **new directory**. Do not reuse `/opt/vault/data`: leaving the
file backend tree untouched is what makes rollback trivial.

```bash
install -d -o vault -g vault -m 0750 /opt/vault/raft

cat > /root/vault-migrate.hcl <<'EOF'
storage_source "file" {
  path = "/opt/vault/data"
}

storage_destination "raft" {
  path    = "/opt/vault/raft"
  node_id = "vault-1"
}

cluster_addr = "https://192.168.7.128:8201"
EOF

vault operator migrate -config=/root/vault-migrate.hcl
```

Expect `Success! All of the keys have been migrated.` Then repoint the server
config. In Ansible terms this is `vault_storage_backend: raft`, plus the
`node_id`, plus `vault_disable_mlock: true`; the storage stanza template in
`playbooks/core/vault_server.yml` currently emits only `path` and will need a
`node_id` line for the raft case.

```bash
chown -R vault:vault /opt/vault/raft
systemctl start vault
systemctl start vault-unseal
```

### Verification

```bash
export VAULT_ADDR=https://127.0.0.1:8200 VAULT_SKIP_VERIFY=true

vault status | grep -E 'Storage Type|Sealed|HA Enabled'   # expect: raft, false
vault operator raft list-peers                            # expect: one voter, vault-1
vault operator raft snapshot save /tmp/verify.snap        # the whole point
ls -l /tmp/verify.snap && rm -f /tmp/verify.snap
```

Then confirm the estate came back, not just Vault:

```bash
vault kv get -field=key_b64 secret/proxmox/pbs/prod/client-encryption/pbs-b2-encrypted >/dev/null && echo pbs-key-ok
kubectl --context=admin@prod get vaultstaticsecret -A     # VSO should resync
```

Finally, re-run the snapshot playbook so the collector re-detects the backend
and `vault_backup_storage_backend_is_raft` flips to 1, resolving
`VaultOnFileStorageBackend`:

```bash
ansible-playbook -i inventory/environments/production.ini \
  playbooks/core/vault_offsite_snapshot.yml
```

### Rollback

Rollback is cheap because `vault operator migrate` **reads** from the source and
never modifies it. `/opt/vault/data` is still a complete, valid file backend
after a successful migration.

```bash
systemctl stop vault
# restore storage "file" { path = "/opt/vault/data" } in /etc/vault.d/vault.hcl
systemctl start vault
systemctl start vault-unseal
vault status | grep -E 'Storage Type|Sealed'   # expect: file, false
```

Any writes that landed while running on raft are lost by this rollback, which is
why the window should be quiet. Keep `/opt/vault/raft` until the next successful
snapshot proves the new backend is good; delete it only afterwards, and only
once `/opt/vault/data` is confirmed no longer in use.

If Vault will not unseal on either backend, the recovery order is: the cold tar
above → the newest `vault-file-*.snap.gpg` from the offsite bucket → the
`vault-vm-119-nightly` PBS image. All three need the Shamir shares.

## Restore procedures

A backup nobody has restored is a hypothesis. Each of these should be exercised
at least once against a scratch VM, never against 119.

### From an offsite encrypted snapshot

Requires, none of which may come from Vault: the GPG private key from offline
escrow, S3 read credentials from offline escrow, and 3 of 5 Shamir shares.

```bash
aws s3 cp s3://<bucket>/<prefix>/vault-file-<stamp>.snap.gpg.sha256 .
aws s3 cp s3://<bucket>/<prefix>/vault-file-<stamp>.snap.gpg .
sha256sum -c <<<"$(cat vault-file-<stamp>.snap.gpg.sha256)  vault-file-<stamp>.snap.gpg"

gpg --decrypt --output vault-data.tar.zst vault-file-<stamp>.snap.gpg

systemctl stop vault
mv /opt/vault/data /opt/vault/data.broken
zstd -d -c vault-data.tar.zst | tar -C /opt/vault -xf -
chown -R vault:vault /opt/vault/data
systemctl start vault && systemctl start vault-unseal
```

For a raft-era snapshot the last block is instead
`vault operator raft snapshot restore -force vault-<stamp>.snap` against a
running, unsealed, initialised Vault.

### From the PBS VM image

`qmrestore` the newest `vault-vm-119-nightly` snapshot to a **new VMID** on an
isolated bridge, boot it, and confirm it unseals before touching production.
Restoring over 119 in place destroys the running Vault and the tar above.

Note the dependency direction that makes this the last resort: PBS restores from
`pbs-b2-encrypted` need the key held in Vault, so a total Vault loss leaves
`pbs-s3` — currently the unencrypted datastore — as the only readable one. Once
finding 1 is fixed, that stops being true, which is exactly why the replacement
key must be escrowed offline before the change is made.

## Order of work

1. Escrow: generate the PBS encryption key and the snapshot GPG keypair, store
   the private halves offline, in two physical locations, outside this estate.
2. Codify `vault-vm-119-nightly` in Ansible and switch it to that escrowed key.
   This closes the unencrypted offsite copy of the trust root.
3. Deploy `playbooks/core/vault_offsite_snapshot.yml` — small, independently
   encrypted, restorable snapshots plus the freshness metrics.
4. Enable telemetry and add both scrape jobs; deploy `vault-alerts.yaml`.
5. Rehearse a restore onto a scratch VM. Record the time it took.
6. Migrate to raft. Zero-downtime snapshots from here on.
7. Replace `/root/.vault-token` with an AppRole; revoke the root token.
8. Evaluate KMS auto-unseal, then remove the Shamir material from VM 119.
9. Only then consider three-node HA, which is the change that stops Vault being
   a single point of failure at all.

Steps 1 and 2 are worth doing this week regardless of whether anything else on
this list happens.
