# AWS credential scoping — retiring `aws/user/homelab`

Scoped 2026-08-21. This is Workstream 3 task 4 ("map dependencies before
automating") of [credential-lifecycle-roadmap.md](credential-lifecycle-roadmap.md),
carried out for the single credential that roadmap names as "the oldest secret in
the estate".

## The finding that changes the priority

`aws/user/homelab` was on the rotation list because it is 331 days old against a
90-day interval. That is not the interesting part.

**The IAM user `homelab` has `AdministratorAccess` attached.** Its access key is
copied into three Vault paths and a shell profile, and is used by Nextcloud's
primary object storage and the etcd backup job. A leak is not "backups affected";
it is full control of AWS account `229517242621`.

The user also carries five *scoped* inline policies which `AdministratorAccess`
renders irrelevant. Those policies are, usefully, an accurate description of what
each consumer actually needs.

`homelab` was created 2025-09-23 by hand — it is **not** Terraform-managed
(`aws_iam_user` appears only in `vault_offsite_backup_iam.tf` and
`mail_edge/main.tf`, which define other users). Changes to it will not fight a
Terraform apply.

## Consumer map

The task-4 deliverable. Rotating `aws/user/homelab` alone would have broken
Nextcloud and etcd backups, because neither reads that path.

| Consumer | Reads from | Delivered by | Bucket |
| --- | --- | --- | --- |
| Nextcloud primary storage | `secret/nextcloud/prod/s3` | VSO -> `default/nextcloud-s3-secret` | `homelab-prod-nextcloud` |
| Talos etcd backup | `secret/talos/backup/prod` | VSO -> `talos-etcd-backup/talos-etcd-backup-credentials` | `myrobertson-homelab-talos-etcd-backups` |
| PBS `pbs-s3` datastore | `/etc/proxmox-backup/s3.cfg` on the PBS VM | **nothing — static file.** `secret/aws/pbs-backup/credentials` is a rotation record only, read by nobody | `myrobertson-homelab-pbs` |
| Terraform (run from the workstation) | `~/.bash_profile` | shell env | `myrobertson-homelab-terraform` + everything else |
| Backup provisioning | inline policy only | — | `homelab-prod-backups` |

Same key `...5KQF` in `secret/aws/user/homelab`, `secret/nextcloud/prod/s3` and
`secret/talos/backup/prod`. `secret/aws/user/homelab_terraform` holds a
*different* key (`...DEHY`).

Neither VaultStaticSecret sets `rolloutRestartTargets`, so VSO updates the
Kubernetes Secret but nothing restarts the consumer — a rotation would appear to
succeed while pods kept using the old value.

## Why "rotate the key" is the wrong unit of work

Rotating resets a 90-day clock and leaves an admin-scoped credential in three
places. The goal should be that no workload holds an admin key at all. Terraform
legitimately needs broad permissions — it manages IAM users, access keys,
CloudWatch alarms, SNS topics, Route53 records, SES, VPC and S3 — but **Nextcloud
and the etcd backup do not**, and today they share Terraform's identity.

Splitting workload credentials from Terraform's is the win. It is achievable
without dynamic credentials, and does not touch Terraform.

## Target state per consumer

Vault's `aws/` secrets engine is already mounted and configured
(`disable_automated_rotation: false`, root key `AKIATK4...`, one existing role
`terraformRemoteState` using `credential_type: iam_user` with an inline
`policy_document`). That role is the template.

**Dynamic credentials — good fit**

- **Talos etcd backup.** A CronJob: it gets fresh credentials each run, so lease
  expiry between runs is harmless. Move to a `VaultDynamicSecret` against a new
  role scoped to `myrobertson-homelab-talos-etcd-backups`. Tighten `s3:*` (its
  current inline policy) to the operations it actually performs.

**Dynamic credentials — possible but riskier**

- **Nextcloud primary storage.** A long-running service holding credentials for
  its object store. If a lease expires and the pod is not restarted with the new
  value, primary storage breaks — an outage, not a degraded backup. Requires
  `rolloutRestartTargets` on the VaultStaticSecret/VaultDynamicSecret and a TTL
  comfortably longer than the reload interval. Do this *after* the etcd job has
  proven the pattern.

**Dynamic credentials — not viable**

- **PBS.** `pbs-s3` reads a static `/etc/proxmox-backup/s3.cfg` on a Proxmox VM.
  There is no VSO there and no reload hook. This one stays static and is the
  genuine candidate for scheduled rotation (mint, write, verify, revoke, using
  the two-live-credential overlap window from
  `secret_rotation_classes.yml -> external_provider.safe_procedure`).

- **Terraform.** Human-run, needs broad rights. Should use its own identity —
  `homelab_terraform` (`...DEHY`) already exists — rather than sharing the key
  that workloads use.

## Implementation order

1. **Give each workload its own scoped credential.** Highest value, lowest risk,
   no dynamic-secret machinery required. Removes admin-level access from
   Nextcloud, the etcd backup and PBS immediately. The five existing inline
   policies define the scopes.
2. **Point Terraform at `homelab_terraform`** so the workstation shell no longer
   carries the same key as the workloads.
3. **Only then** remove `AdministratorAccess` from `homelab` — and verify a
   Terraform plan still succeeds first, because that policy is currently
   load-bearing for it.
4. **Convert the etcd backup to a `VaultDynamicSecret`**, prove the pattern.
5. **Convert Nextcloud**, with `rolloutRestartTargets` set.
6. **Schedule PBS rotation** — **DONE 2026-08-21.**
   `ansible/proxmox/pbs_s3_key_rotation.yml`, proven end to end by a forced
   rotation the same day. It drives the `s3.cfg` change, the proxy reload and the
   verify, and updates Vault last; writing to Vault was never the rotation.

## Progress

### Step 1 — Talos etcd backup — DONE 2026-08-21

IAM user **`homelab-talos-etcd-backup`**, no managed policies, one inline policy
`talos-etcd-backup-s3`. Key `...H7SW` written to `secret/talos/backup/prod` with
`vault kv patch` (v1 -> v2) so the other six keys at that path were preserved.

The policy is much narrower than the `s3:*` it replaces, because the CronJob does
exactly one S3 operation — `aws s3 cp` of a single snapshot, no list, no delete:

```
PutObject, AbortMultipartUpload, ListMultipartUploadParts
  on arn:aws:s3:::myrobertson-homelab-talos-etcd-backups/*
ListBucket, GetBucketLocation
  on arn:aws:s3:::myrobertson-homelab-talos-etcd-backups
```

That answers the open question about `homelabTalosEtcdBackups` granting `s3:*` —
it was over-scoped and nothing needed it.

Verified, in order: the new key uploads to its own bucket; it is **denied** on
`s3 ls` of all buckets, on the Nextcloud bucket, on the PBS bucket, and on
`iam list-users`; VSO propagated the new value to
`talos-etcd-backup/talos-etcd-backup-credentials`; and a real job run uploaded a
242.7 MiB snapshot successfully.

Two notes for whoever does the next one:

- **IAM is eventually consistent.** The first upload with a brand-new key failed
  with `InvalidAccessKeyId` and succeeded ~10 s later. Retry before concluding
  the policy is wrong.
- **`refreshAfter` on this VaultStaticSecret is 5m, not 60s.** A three-minute
  wait looks like VSO has stopped propagating when it simply has not run yet.
  Check `refreshAfter` on the specific resource before diagnosing. `destination.
  overwrite: false` (set on 66 of 77) is *not* a blocker — VSO owns these Secrets
  via ownerReference, and that flag only guards Secrets it does not own.

The old admin key `...5KQF` is deliberately still **Active** — Nextcloud and PBS
still use it. It can only be deleted after those two are migrated.

### Step 1 — Nextcloud — NOT APPLICABLE, reclassify as `review_obsolete`

**Do not create a scoped credential for this one. The consumer is dead.**

Investigated 2026-08-21 before making any change:

- `default/nextcloud`, the only workload referencing `nextcloud-s3-secret`, is
  scaled to **0/0** and last changed state **2026-05-02T06:07:34Z**.
- The bucket `homelab-prod-nextcloud` holds 2,924 objects / 8.2 GiB, and its most
  recent object is **2026-05-02 01:14** — the same day the deployment stopped.
- The Nextcloud that is actually running is `nextcloud/nextcloud-migration-ldap`
  (3/3). It has **no S3 or AWS environment at all** and stores data on the
  `nextcloud-data` PVC. It never reads this credential.

So `secret/nextcloud/prod/s3` is a live copy of an admin-scoped AWS key serving a
workload retired four months ago. Scoping it would be busywork; it should be
retired, which also removes one of the three places the `...5KQF` key is copied.

**Operator decision required before deleting anything:** the bucket still holds
8.2 GiB from when S3 was Nextcloud's primary object storage. Confirm that content
was migrated to the PVC (or is otherwise not needed) before removing the bucket
or the credential. Retiring the *Vault path* is safe once the deployment is
confirmed dead; deleting the *bucket data* is a separate, irreversible call.

This is exactly the `review_obsolete` class from
`ansible/proxmox/vars/secret_rotation_classes.yml` — "delete rather than rotate"
— and it was sitting in the rotation list as if it needed rotating.

### Step 1 — PBS — DONE 2026-08-21

IAM user **`homelab-pbs-backup`**, key `...KXM2`, one inline policy
`pbs-s3-datastore` scoped to `myrobertson-homelab-pbs` (get/put/delete plus the
multipart actions on objects; list/multipart-list/location on the bucket). PBS
needs delete because GC and prune remove chunks, unlike the etcd job.

Written directly into the `aws-homelab-pbs` section of
`/etc/proxmox-backup/s3.cfg`; the `backblaze-b2-pbs` section was verified
byte-identical afterwards. Backup at `/etc/proxmox-backup/s3.cfg.bak.<stamp>`.

**Trap that cost an outage window: `s3.cfg` must stay `0640 root:backup`.**
The edit script set it `0600`, and `proxmox-backup-proxy` runs as `backup`, so
every S3 task failed with `unable to read '/etc/proxmox-backup/s3.cfg' -
Permission denied (os error 13)`. That reads like a credential problem and is
not. Restore ownership/mode and reload the proxy.

Verified: scoped key does put/list/delete on its own bucket and is denied on
all-buckets, the etcd bucket and `iam list-users`; after the permissions fix and
a reload, a real verify of `pbs-s3 vm/119` read archives from S3 with `0 errors`.

#### Tracked in Vault at `secret/aws/pbs-backup/credentials` — 2026-08-21

The key lived **only** in `s3.cfg`, so the rotation exporter could not see it and
it would have aged past 90 days silently. It is now recorded in Vault with
`rotated_at=2026-08-21` (matching the IAM `CreateDate` of
`2026-08-21T05:27:07Z`) and `rotation_interval_days=90`, alongside `IAM_USER`
and the S3 endpoint. Confirmed exported as
`credential_vault_secret_rotated_timestamp_seconds{path="aws/pbs-backup/credentials"}`.

**This path is a rotation *record*, not the source of truth, and the distinction
is load-bearing.** PBS reads `/etc/proxmox-backup/s3.cfg` at runtime. There is no
VSO sync, no Vault Agent template and no reload hook behind this path, so
`vault kv put` here changes **nothing** about what PBS authenticates with. That
is exactly the trap the 28 orphaned `secret/volsync/prod/*` paths set (see
"Orphaned path cleanup" below): a path that looks authoritative, is read by
nobody, and drifts until someone trusts it during an incident. A `note` field
carrying this warning is stored *inside* the secret so it is visible to anyone
who reads the path without finding this runbook.

**This is now automated — see the next section.** The manual procedure below is
retained as the fallback and as the description of what the automation does:

1. `aws iam create-access-key --user-name homelab-pbs-backup` (two live keys).
2. Edit the `aws-homelab-pbs` section of `s3.cfg`, leaving `backblaze-b2-pbs`
   untouched. **Keep the file `0640 root:backup`** — see the trap above.
3. `systemctl reload proxmox-backup-proxy`, then verify and confirm `0 errors`.
   Do not treat a clean `systemctl status` as proof.
4. Update this Vault path and set `rotated_at` to the new date.
5. Only then delete the old key, and confirm it fails with `InvalidClientTokenId`.

Steps 3 and 5 are the ones worth not skipping: the verify is what proves the new
key works, and deleting before verifying is what turns a rotation into an outage.

### Rotation automated — 2026-08-21 (closes step 6)

`ansible/proxmox/pbs_s3_key_rotation.yml` and the `pbs_s3_key_rotation` role.
This was the estate's last purely static credential and the final open item of
Workstream 3 task 3 in [credential-lifecycle-roadmap.md](credential-lifecycle-roadmap.md).

**Proven end to end on 2026-08-21**, not just deployed: a forced run rotated
`...KXM2` to `...HOZ6` in 28 seconds, and IAM, the PBS config and Vault were all
confirmed independently afterwards.

    /usr/local/sbin/pbs-rotate-s3-key.py --check     # validate, change nothing
    /usr/local/sbin/pbs-rotate-s3-key.py --force     # rotate now

A `pbs-rotate-s3-key.timer` checks daily and rotates at 75 days. **75, not 90**:
the alert fires at 90, so rotating on the interval itself races the alert — the
same mistake the infra-cert thresholds made.

#### Where the rotator's own permission comes from

The obvious design is to let `homelab-pbs-backup` rotate its own key with
`iam:CreateAccessKey` on `${aws:username}`. **Don't.** A key that can mint its
own successor lets a thief survive every future rotation, which removes the only
thing rotation buys. A dedicated static rotator key is no better: it would be a
*more* privileged credential that nothing rotates either.

So the rotator is dynamic. `aws/roles/pbs-key-rotator` mints a throwaway IAM user
whose inline policy is `iam:*AccessKey*` on exactly one user ARN, and the script
revokes the lease when it finishes. Nothing long-lived and IAM-capable is stored
on the PBS host.

Two consequences worth knowing:

- Vault rejects `default_sts_ttl`/`max_sts_ttl` for `iam_user` credentials, and
  the only other knob is the mount-wide `aws/config/lease` — which
  `terraformRemoteState` shares, so tightening it there would expire Terraform's
  remote-state credentials mid-apply. Instead the script records its lease id and
  the next run reaps anything a killed run stranded. **This is not theoretical:**
  the first failed `--check` stranded a lease and the next run cleaned it up.
- The rotator is a brand-new IAM principal, so its first calls return
  `InvalidClientTokenId` for ~10 seconds. Measured: 4 attempts. Both the rotator
  and the newly minted PBS key retry past this.

#### What each check actually proves

Worth being precise about, because these are not interchangeable:

| Check | Proves | Does not prove |
| --- | --- | --- |
| `s3 ListObjectsV2` with the new key, before any change | the new key is valid at AWS | anything about PBS |
| `proxmox-backup-manager s3 check` | the credential works — it does a real PUT and read against the bucket — and `s3.cfg` is readable and parseable | that the *proxy* reloaded; the CLI builds its own S3 client from the same file |
| journal scan after reload | the proxy did not start logging auth or permission errors | — |
| optional snapshot verify (off by default) | the proxy itself reads chunks from S3 with the new credential | — |

The snapshot verify is opt-in because the smallest snapshot in `pbs-s3` is
~32 GiB, so it is roughly an hour of re-reading, not a quick sanity check.

**Gotcha:** `proxmox-backup-manager s3 check backblaze-b2-pbs myrobertson-pbs`
**fails, and always has.** B2 returns `InvalidRequest: Content-MD5 OR
x-amz-checksum- HTTP header is required for Put Object requests with Object Lock
parameters` — the check's test PUT is incompatible with an Object Lock bucket.
It is not a credential problem and not caused by rotation; the B2 stanza was
confirmed byte-identical before and after. Do not "fix" it by rotating B2.

#### Failure behaviour

AWS allows two live keys, and that overlap is the whole safety mechanism — the
old key stays valid until the new one is proven, so every failure before the
final revoke is recoverable.

- Failure at or after the config swap: restores the backed-up `s3.cfg`, reloads,
  re-checks, and deletes the new key. The host ends as it was found.
- Failure *recording to Vault*: deliberately does **not** roll back and does
  **not** revoke. The new key is live and working, so the safe state is two live
  keys and a loud non-zero exit rather than silently losing track of which key is
  in use. The next run refuses to start ("already has 2 access keys"), which is
  the correct signal for a human.
- `SIGTERM` is trapped, so `systemctl stop` mid-rotation still rolls back and
  revokes rather than leaving the host half-swapped.
- It refuses to run at all if `s3 check` is already failing, if a task is active
  against the datastore, or if IAM and `s3.cfg` disagree about which key is live.

Status is exported for alerting as `pbs_s3_key_rotation_last_status`
(0 ok / 1 failed / 2 deferred) plus `..._key_age_days`.

### The `...5KQF` admin key — ROTATED AND DELETED 2026-08-21

The secret for this key was exposed in plaintext during this work (a failed
redaction while reading `s3.cfg`), so it was treated as compromised rather than
merely overdue.

Replaced with `...BZMN` on the same `homelab` user, using the two-live-keys
overlap: minted, verified against IAM/S3/CloudWatch/SNS/Route53/Lambda, written
to `secret/aws/user/homelab` (v2) and `~/.bash_profile`, then the old key was
deactivated, confirmed to fail with `InvalidClientTokenId`, and deleted.

`homelab` now has exactly one key. Note `...BZMN` still carries
`AdministratorAccess` — the exposure was fixed, the over-permissioning was not.

### Terraform moved to its own identity, and `homelab` deleted — 2026-08-21

`homelab_terraform` already existed but **also carried `AdministratorAccess`**, so
this move separates duties rather than reducing privilege. Its key was 120 days
old, so it was rotated rather than adopted: new key `...FOH3`, old `...DEHY`
deactivated then deleted (it had no Kubernetes consumers). Vault
`secret/aws/user/homelab_terraform` patched to v5, console fields preserved, and
`~/.bash_profile` switched over — `sts get-caller-identity` now returns
`user/homelab_terraform`.

With every workload on its own scoped key, `homelab` had **no consumers left**
(its last key use was a verification call, service `iam`). It was deleted
outright: 1 access key, 5 inline policies, `AdministratorAccess` and
`IAMUserChangePassword` detached, then the user removed. A full snapshot of its
configuration was captured immediately beforehand.

`secret/aws/user/homelab` was soft-deleted in Vault (recoverable with
`vault kv undelete`). It was the estate's oldest secret at 3.7x its interval, so
the credential-age alert loses its worst offender once the exporter next runs.

**Verification limit worth recording:** a real `terraform plan` was attempted and
failed on missing input variables (`wireguard_ec2_public_key`, `mail_domain`),
not on credentials — those come from a tfvars file not present locally, and
`mail_edge` has no `backend "s3"` block, taking backend config from the
`TF_STATE_*` environment instead. So the new identity was proven against S3
(including the state bucket), IAM, CloudWatch, SNS, Route53, Lambda, SES and EC2,
but **not** by a green plan. Run one with real tfvars before relying on it.

### Deleting the key broke Terraform — the consumer map was incomplete

**Read this before deleting any credential.** The map above listed Kubernetes
secrets and three Vault paths. It was wrong. A full sweep of Vault afterwards
found **32 paths still holding `...5KQF`**, and one of them mattered:

```
terraform/main.tf:34
  aws_access_key_id = var.aws_access_key_id != null ? var.aws_access_key_id
                    : data.vault_generic_secret.volsync_s3_settings.data["AWS_ACCESS_KEY_ID"]
terraform/variables.tf:30
  default = "secret/volsync/prod/plex-config-ceph"
```

Terraform's `provider "aws"` takes its credentials from a Vault path named
**`volsync/prod/plex-config-ceph`** — via a `vault_generic_secret` data source,
not via VSO and not from the environment. Deleting the key therefore failed every
plan with `InvalidClientTokenId`. Fixed by patching that path to `...FOH3`.

**Scan all of Vault, not just the paths you expect.** Grepping the repos for a
path name finds `vault_generic_secret` consumers that a Kubernetes-only scan
misses entirely.

What was *not* broken, verified rather than assumed:

- **CNPG backs up to Backblaze B2**, not AWS (`endpointURL:
  https://s3.us-west-002.backblazeb2.com`). Its live secret holds a B2 key, not
  an `AKIA` one. All ten clusters had successful backups throughout.
- **VolSync reads `secret/backblaze/k8s/prod/volsync`** — 33 VaultStaticSecrets
  point there. All 26 replicationsources kept syncing, including one at
  05:55:45Z, the minute the key was deleted.

So the 29 `secret/volsync/prod/*` paths holding AWS keys were legacy copies with
no consumer at all.

### Orphaned path cleanup — 2026-08-21

Of the 31 paths still holding the dead key, 28 had **no VSO consumer and no
reference anywhere in the three repos**. Soft-deleted (recoverable with
`vault kv undelete`), which also removes them from the credential-age metrics.

Three were kept because they are referenced:

| Path | Why kept |
| --- | --- |
| `secret/cnpg/prod/backup-s3` | named in `secret_rotation_classes.yml` |
| `secret/nextcloud/prod/s3` | still has a VSO consumer (the 0/0 deployment) |
| `secret/talos/backup/stage` | referenced in `clusters/staging/infrastructure.yaml` |

Post-cleanup health, all verified: VolSync 26/26 synced within 2h, CNPG 10/10
with successful backups, VaultStaticSecrets 77/77 synced, Terraform 0
`InvalidClientTokenId`.

Note `vault kv get` **exits 0 on a soft-deleted KV-v2 path** — it returns
metadata carrying `deletion_time`. Checking the exit code says "still there";
check whether `data.data` is populated instead.

### Terraform credential paths separated — 2026-08-21 (homelab_bootstrap `03dab63`)

`data.vault_generic_secret.volsync_s3_settings` was feeding two things it should
never have fed, both now split out:

| Consumer | Was | Now |
| --- | --- | --- |
| `provider "aws"` | `secret/volsync/prod/plex-config-ceph` | `secret/aws/terraform/provider` |
| `secret/talos/backup/<env>` writer | same volsync path | `secret/aws/talos-etcd-backup/credentials` |

The second was the dangerous one and was **not** noticed until reading
`talos_backup_vault_secret.tf`: Terraform *writes* the etcd backup's Vault secret,
using whatever credentials that data source held. The next `terraform apply`
would therefore have overwritten the scoped `...H7SW` key with the provider's
`AdministratorAccess` credential and silently re-privileged the backup job —
undoing the scoping done earlier the same day, with no error and no alert.

Both new paths were seeded with the values already in use, so applying is a no-op.
`volsync_s3_settings` now supplies only `RESTIC_REPOSITORY`, used to derive the S3
region, so the data source finally matches its name.

**Generalisable lesson:** when Terraform both *reads* a credential and *writes*
it into another system's secret, scoping the consumer is not enough — the
Terraform code has to be changed too, or the next apply reverts it. Check for
`vault_kv_secret_v2` resources writing credentials before assuming a manual Vault
patch will hold.

### Remaining

- **`homelab_terraform` still holds `AdministratorAccess`.** Reviewed against
  CloudTrail 2026-08-21 — see
  [terraform-iam-cloudtrail-review.md](terraform-iam-cloudtrail-review.md). The
  finding: because Terraform manages IAM users, access keys and roles, any policy
  permitting that is privilege-escalation-capable, so narrowing it only helps if
  paired with a permissions boundary. Also blocked on there being no CloudTrail
  trail, which means S3 object access is invisible to any review. This is now the only
  admin identity, used deliberately for Terraform. Narrowing it to what the
  stacks actually manage (IAM, CloudWatch, SNS, Route53, SES, VPC, S3) is the
  next reduction, and needs a CloudTrail review to do safely.
- `secret/nextcloud/prod/s3` still holds the deleted `...5KQF` value and VSO still
  syncs it to `default/nextcloud-s3-secret`. Harmless — that deployment is 0/0 —
  but it should go when the path is retired, which depends on the decision about
  the 8.2 GiB still in `homelab-prod-nextcloud`.

## Open questions for the operator

- Does anything besides Terraform rely on `homelab` having admin rights? A
  CloudTrail review over 90 days would answer this definitively; it was not run
  as part of this scoping.
- `homelabTalosEtcdBackups` grants `s3:*` on its bucket. Confirm which operations
  `talos-etcd-backup` actually issues before narrowing it.
- Should `HomelabS3BackupProvisioningPolicy` (which allows `s3:CreateBucket` and
  `s3:ListAllMyBuckets` on `*`) belong to a workload at all, or only to
  Terraform?
