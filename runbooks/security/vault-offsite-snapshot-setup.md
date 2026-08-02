# Vault offsite snapshot — operator setup

Turnkey steps to bring `playbooks/core/vault_offsite_snapshot.yml` fully online and
clear the two alerts it backs:

- `TargetDown{job="vault-node"}` — the Vault VM (192.168.7.128) serves no
  node_exporter. Cleared by **Step 1 alone** (no S3, no Vault downtime).
- `VaultBackupMetricsMissing` — no `vault_backup_*` metrics exist yet. Requires a
  real snapshot run, which needs S3 credentials and a GPG escrow key
  (**Steps 2–5**). This alert stays firing until then; that is correct.

Vault is the estate trust root. On the **file** storage backend a consistent
snapshot requires briefly stopping the service (seconds). The snapshot script
preflights the unseal material and restarts+unseals in an EXIT trap, so it never
leaves Vault stopped — but treat Step 5 as a (short) change window.

---

## Step 1 — node_exporter only (clears `TargetDown{job=vault-node}`)

This installs the exporter and the textfile collector directory **without**
touching Vault and **without** needing any S3/GPG material. It is non-disruptive.

```bash
ansible-playbook -i inventory/environments/production.ini \
  playbooks/core/vault_node_exporter.yml
```

`vault_node_exporter.yml` installs only `prometheus-node-exporter` with
`--collector.textfile.directory=/var/lib/prometheus/node-exporter` — the same
config the full snapshot playbook would apply, so there is no drift. The
`vault-node` scrape target goes green within one scrape. (The full
`vault_offsite_snapshot.yml` also installs this exporter, but it is blocked on
the S3/GPG material in Steps 2–5, so use this playbook to clear the target now.)

> Note: the `[vault]` host group lives in the git-ignored local production
> inventory, not the tracked repo. Run this from the real controller, or supply
> the host with `-i '192.168.7.128,' -e ansible_user=<user>`.

---

## Step 2 — S3 bucket + scoped credentials

Create a bucket (or a prefix in an existing one) and an IAM identity whose policy
is limited to that prefix: `s3:PutObject`, `s3:GetObject`, `s3:ListBucket`,
`s3:DeleteObject` (retention pruning needs delete). Record: access key id, secret
access key, region, bucket, prefix, and — for a non-AWS/S3-compatible endpoint —
the endpoint URL.

## Step 3 — GPG escrow keypair (generated OFFLINE)

The snapshot is encrypted to a public key **on the Vault host**. The matching
private key must **never** exist on that host — a host that can decrypt its own
snapshots hands an attacker who owns the host both ciphertext and key. Generate
the keypair on a workstation, not on 192.168.7.128:

```bash
gpg --batch --gen-key <<'EOF'
Key-Type: eddsa
Key-Curve: ed25519
Subkey-Type: ecdh
Subkey-Curve: cv25519
Name-Real: Vault Offsite Snapshot Escrow
Name-Email: vault-escrow@myrobertson.net
Expire-Date: 0
%no-protection
EOF

# Export the PUBLIC key (goes into Vault, Step 4):
gpg --armor --export vault-escrow@myrobertson.net > vault-escrow-public.asc

# Export the PRIVATE key and escrow it OFFLINE (password manager / offline media).
# Do NOT leave it on any homelab host. It is the only way to restore a snapshot.
gpg --armor --export-secret-keys vault-escrow@myrobertson.net > vault-escrow-PRIVATE.asc
```

`GPG_RECIPIENT` is the key's email/fingerprint (`vault-escrow@myrobertson.net`).
`GPG_PUBLIC_KEY` is the full armored contents of `vault-escrow-public.asc`.

## Step 4 — Populate the Vault secret

The playbook reads `secret/vault/backup/offsite` and asserts these keys are
present (verify against the `assert` task in the playbook before running):

```bash
export VAULT_ADDR=https://vault.myrobertson.net:8200
# VAULT_CACERT + a token with write on secret/vault/backup/offsite

vault kv put secret/vault/backup/offsite \
  AWS_ACCESS_KEY_ID='...' \
  AWS_SECRET_ACCESS_KEY='...' \
  AWS_DEFAULT_REGION='...' \
  S3_BUCKET='...' \
  S3_PREFIX='vault-snapshots' \
  GPG_RECIPIENT='vault-escrow@myrobertson.net' \
  GPG_PUBLIC_KEY="$(cat vault-escrow-public.asc)"
# For a non-AWS endpoint also add: S3_ENDPOINT_URL='https://...'
```

Add rotation metadata to match the estate convention (see
`credential-lifecycle-roadmap.md`):
`vault kv metadata put -custom-metadata=rotated_at=<YYYY-MM-DD> -custom-metadata=rotation_interval_days=180 secret/vault/backup/offsite`.

## Step 5 — Seed the first snapshot (brief Vault downtime)

```bash
ansible-playbook -i inventory/environments/production.ini \
  playbooks/core/vault_offsite_snapshot.yml
```

This installs the script/timer and runs one snapshot immediately
(`vault_backup_run_now: true`), which **stops Vault for the cold copy** (seconds
on the file backend), then restarts + unseals it. Set
`-e vault_backup_run_now=false` to install the timer and let it fire on schedule
(03:00 daily) instead of taking the window now.

## Step 6 — Verify

- `systemctl status vault-offsite-snapshot.timer` on 192.168.7.128
- `vault status` → `Sealed false`
- The `vault_backup_last_success_timestamp_seconds` series appears →
  `VaultBackupMetricsMissing` clears; `VaultBackupStale` has a series to evaluate.

## Restore

Restore is the disaster path and is out of scope here — it needs the escrowed
**private** key from Step 3. See `vault-backup-and-raft-migration.md` for the
backend details and the raft migration that removes the snapshot downtime.
