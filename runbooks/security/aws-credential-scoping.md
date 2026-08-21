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
reference anywhere in the three repos**. Soft-deleted, and so recoverable with
`vault kv undelete`.

> **Correction, later the same day: soft-deleting does _not_ remove them from the
> credential-age metrics, as originally claimed here.** `vault kv delete` removes
> the *data* and leaves the *metadata*, and the collector is metadata-only by
> design — it needs a date, not a password. So all 28 still export
> `credential_vault_secret_rotated_timestamp_seconds` with their original
> `rotated_at`, still count toward the rotation backlog, and still feed
> `VaultSecretsSeverelyOverdue`. Measured 2026-08-21: 29 of 148 tracked secrets
> were records for things that no longer exist.
>
> Fully removing one needs `vault kv metadata delete`, which is **permanent** —
> it destroys every version, so `undelete` is no longer possible. That is the
> reason not to do it reflexively here: `secret/volsync/prod/*` are restic
> configs, and `secret_rotation_classes.yml -> restic_key` explains that
> discarding a restic password can strand every snapshot in that repository
> permanently. Confirm each repository is genuinely abandoned before destroying
> its metadata; the soft-deleted state is the safer place to leave them until
> then.
>
> `secret/aws/user/homelab` **was** fully removed, because it is an
> `external_provider` record rather than a restic key and the IAM user it
> described is confirmed gone (`NoSuchEntity`). It was the single oldest tracked
> secret in the estate at 332 days — a ghost topping the age table. Tracked count
> 148 -> 147.

### What the volsync ghosts actually are — 2026-08-21

Investigated before deleting them, and the answer inverts the original
assessment. **These are not disposable records.**

Each `secret/volsync/prod/<pvc>` holds a **unique** `RESTIC_PASSWORD` for a
repository under `s3://homelab-prod-backups/volsync/default/<pvc>` — the *old*
AWS-S3 backup scheme. That is why they held the dead `...5KQF` key at all. Live
VolSync now backs up to Backblaze B2: all 31 `restic-config-*` Secrets are built
from the single path `secret/backblaze/k8s/prod/volsync`, and **no**
VaultStaticSecret references any of these per-PVC paths. So they are genuinely
unreferenced — but "unreferenced" is not "safe to destroy".

**The old S3 repositories were never deleted.** As of 2026-08-21
`s3://homelab-prod-backups/volsync/default/` still holds **12,133 objects,
182.7 GiB**, across 22 repository prefixes, all last written 2026-04-24 — the
migration cutover. A restic repository is encrypted with a master key that the
password unlocks, so for each of these the Vault record is **the only thing that
can ever read that repository again**. `vault kv metadata delete` on them would
strand 110 GiB of retained backups permanently.

Split, after checking every prefix against S3 directly:

| | Count | Action |
| --- | ---: | --- |
| No surviving repository | 7 | **Destroyed 2026-08-21.** Tracked 147 -> 140. |
| Repository still holds data | 21 | **Resolved 2026-08-21** — S3 data deleted, then the records. See below. |

The 21 are `immich-data-files-pvc-ceph` (101.4 GiB) and `radarr-config-ceph`
(6.8 GiB), which are almost all of it, plus 19 small ones. `plex-config-ceph`
accounts for a further 72.7 GiB in the same bucket but is not a ghost — it is the
one live path in that tree.

**Their current soft-deleted state is the wrong resting place either way.** The
only keys to 110 GiB sit in a state that reads as already-discarded, so a routine
bulk `metadata delete` would destroy them without anyone noticing. Resolve it in
one of two directions:

- **Keeping the old backups:** `vault kv undelete` the 21 so the keys are live
  and visible, and reclassify them so the age alert reflects that they are
  archival keys rather than credentials in rotation.
- **Not keeping them:** delete the S3 data first, *then* destroy the metadata.
  Doing it in that order means nothing is ever stranded, and it also stops paying
  for 182.7 GiB of S3 Standard (~$4/month).

Note for the next person: `while read` over a file with no trailing newline
silently skips the last entry. That very nearly let one of the seven be destroyed
without its prefix having been checked.

#### B2 coverage checked before deciding — 2026-08-21

The question that decides it is not "are these referenced" but "is anything else
holding this data". Checked against the live Backblaze bucket
`myrobertson-k8s-prod-volsync`, and **all 21 have a B2 counterpart carrying
restic snapshots**:

| Group | Count | Current protection |
| --- | ---: | --- |
| Actively backed up by VolSync | 14 | B2, and **restore-verified 5 days ago** |
| Database volumes | 4 | CNPG barman to B2, last backup ~5h |
| Retired app (`authelia`) | 2 | none — but B2 holds the same frozen copy |
| `netbootxyz-config-ceph` | 1 | **none — see the gap below** |

The restore verification is real evidence rather than inference: the weekly
`volsync-restore-verify` CronJob in `default` iterates *every* live
ReplicationSource and performs an actual one-file `restic dump` from each
repository's latest snapshot. It last completed on 2026-08-16, taking 44 minutes.

**The decisive comparison:** for every one of the 21, the B2 copy is at least as
recent as the S3 copy — the S3 repositories all froze on 2026-04-24, while B2
ranges from that same date to today. So deleting the S3 data never leaves
anything as the sole copy. The database and `authelia` repositories are the
frozen ones on both sides, which is expected: those workloads moved to CNPG
barman or were retired at the same cutover.

#### Resolved: old S3 repositories deleted, then the records — 2026-08-21

Done in that order deliberately, so that an interruption at any point would leave
data with its key rather than a key with no data, or worse, data with no key.

**Deleted** `s3://homelab-prod-backups/volsync/default/` for the 21: 7,560
objects, **110.0 GiB**, verified as zero objects remaining per prefix. The bucket
is unversioned with no object lock and no lifecycle rules, so the space is
genuinely released rather than hidden behind delete markers. A manifest of every
deleted object key was captured first.

`plex-config-ceph` was **excluded** — it is the one live path in that tree, not a
ghost. Its 4,573 objects / 72.7 GiB remain, and it is now the only thing left in
the bucket.

**Then destroyed** the 21 Vault metadata records. `secret/volsync/prod/` now
contains only `plex-config-ceph`.

Verified afterwards: B2 untouched and still growing (13,995 objects), all 26
ReplicationSources reporting a `lastSyncTime` with the newest landing *during*
the deletion, and all 77 VaultStaticSecrets synced.

Effect on the metrics, which was the original reason for touching any of this:

| | Before | After |
| --- | ---: | ---: |
| Tracked secrets | 148 | 119 |
| Rotation backlog (past interval) | 92 | 64 |
| Severely overdue (>2x interval) | 20 | 19 |

The severely-overdue figure barely moved, and that is the honest result: at ~142
days against a 90-day interval the volsync ghosts were in the general backlog but
never in the >180-day set. The single drop there came from `aws/user/homelab` at
332 days. **The 19 that remain are all real** — Ceph CSI keyrings, docker
credentials, Talos configs and CA certificates — and are exactly the classes
`secret_rotation_classes.yml` marks as not rotatable by writing Vault. Removing
ghosts was never going to fix those.

#### `plex-config-ceph` retired too — 2026-08-21

The last of the old S3 scheme: 4,573 objects / 72.7 GiB deleted, then the Vault
record. `secret/volsync/prod/` no longer exists. Tracked secrets 119 -> 118.

**It needed three code changes first, and finding them is the whole lesson.**
This path was live rather than a ghost, and searching for consumers turned up one
more each time:

1. `data.vault_generic_secret.volsync_s3_settings` in Terraform — used *only* to
   recover an AWS region by regex-matching the S3 hostname out of the
   `RESTIC_REPOSITORY` string. It produced `us-west-2`, which was already the
   literal fallback at the end of the same `coalesce`, so the read bought nothing
   while making a Plex backup secret undeletable: removing it would have failed
   every plan on a missing data source.
2. `scripts/backup_talos_etcd_to_s3.sh` — still defaulted to reading
   `RESTIC_REPOSITORY` *and* the AWS credentials from this path. Now uses
   `secret/aws/talos-etcd-backup/credentials`, the scoped key.
3. `docs/runbooks/talos-etcd-backups.md` — still described the credentials as
   coming from here.

That is the third, fourth and fifth consumer of a path named for a Plex backup,
after the provider credentials found on 2026-08-21. A credential path named after
one workload but read by others is the recurring failure in this estate, and
grep-before-delete is what keeps catching it. Verified afterwards with
`terraform validate` and a read-only plan showing no reference to
`volsync_s3_settings` and no missing-data-source error.

#### Stale CNPG backups deleted, bucket now empty — 2026-08-21

The `cnpg/` prefix held **12,572 objects, 151.8 GiB** for six clusters, all last
written 2026-04-23/24 — the same cutover. Every live CNPG cluster now backs up to
`s3://myrobertson-k8s-prod-volsync/cnpg/...` on Backblaze.

The risk here was different from the restic repositories. There is no key to
strand — barman backups are not sealed behind a password held in Vault — so the
danger was a *reference*: a cluster bootstrapping or recovering from the old
object store would have broken. Checked and clear: no repository references the
bucket, no cluster declares `externalClusters`, and all ten bootstrap with
`initdb` rather than `recovery`.

Every one of the six had a **newer** B2 counterpart, including the retired
`cluster-authelia-ceph` whose B2 copy runs to 2026-08-02, so nothing became a
sole copy. A manifest was captured first.

`homelab-prod-backups` is now **completely empty** — 0 objects, no versions, no
delete markers. Verified afterwards: all ten clusters fully ready and every
`ScheduledBackup` reporting a completed run within the last ~6 hours.

Two leftovers worth knowing:

- **1,128 `Backup` CRs still reference the deleted bucket.** They are historical
  records of pre-migration backups, not live configuration, and now point at
  nothing. Harmless, but they will mislead anyone reading backup history.
- **The bucket itself still exists**, empty, along with
  `HomelabS3BackupProvisioningPolicy` which grants `s3:CreateBucket` and
  `s3:ListAllMyBuckets` on `*`. Both are now candidates for removal.

#### Gap: most CNPG clusters have no restore verification

Surfaced while checking coverage, and it matters more now that the second copy is
gone. Only **4 of 10** clusters have a `cnpg-restore-verify-*` CronJob —
`keycloak`, `guacamole`, `grafana` and `nextcloud-migration-clean`. The other six,
including `cluster-immich-ceph` and `nextcloud-migration-ldap-cnpg` (352 GiB in
B2, the largest dataset in the estate), have backups that are scheduled and
completing but never proven restorable.

The VolSync side is better served: its single weekly job iterates *every* live
ReplicationSource. The CNPG jobs are per-cluster, so each new cluster needs one
adding and nothing notices when that is forgotten.

#### Separate finding: netbootxyz has no current backup

`netbootxyz-config-ceph-v3` and `netbootxyz-assets-ceph-v3` are Bound and 129
days old, and **no ReplicationSource covers either**. Its last backup of any kind
is the frozen B2 restic repository from 2026-07-12. This is unrelated to the
credential cleanup — it surfaced while checking coverage — but it is a live
backup gap and wants a ReplicationSource.

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

### The Vault AWS engine root key — ROTATED 2026-08-21

`...2UM7` on the IAM user `vault`, **331 days old** against a 90-day interval and
by a wide margin the oldest credential in the estate once the workload keys were
scoped. Rotated to `...QHAH`.

This one is delicate because it is the credential the `aws/` secrets engine runs
on: both `aws/roles/terraformRemoteState` and `aws/roles/pbs-key-rotator` mint
through it, so breaking it breaks Terraform's remote state *and* the PBS rotation.
It is used for nothing else — `iam` in `us-east-1` only, no copy in any KV path,
no reference in any repo, and not Terraform-managed.

**The blocker, which is not obvious.** The policy `vault.myrobertson.com` scopes
key management to `arn:...:user/vault-*`. That wildcard does **not** match
`user/vault` — the user itself — so `vault write -f aws/config/rotate-root` would
have failed with AccessDenied. Fixed by adding a second statement granting
`iam:GetUser`, `iam:ListAccessKeys`, `iam:CreateAccessKey`, `iam:DeleteAccessKey`
on `user/vault` alone (policy **v3**; roll back with
`aws iam set-default-policy-version --policy-arn <arn> --version-id v2`).

Verified with `simulate-principal-policy` that the grant is exactly that narrow:
the engine root can rotate its own key, and still **cannot** touch
`homelab-pbs-backup` or `homelab_terraform`, and cannot attach or inline a policy
to itself.

Rotation used Vault's own `aws/config/rotate-root` rather than a manual
two-key overlap, so **the new secret is known only to Vault** and never passed
through a shell, a file or this session. That is the one case where the
mint-prove-swap-verify-revoke ordering used everywhere else is worth giving up:
there is no way to verify a credential you are not allowed to see, and the
recovery path is cheap — an admin can create a fresh key and rewrite
`aws/config/root` at any time.

Deliberately **not** recorded in a KV path. A record nothing updates is the trap
documented above for PBS, and here it would also be a second copy of the most
privileged credential in the account.

Verified after rotation: `aws/config/root` reports `...QHAH`; IAM shows exactly
one key; both engine roles leased successfully on the first attempt and their
test leases revoked cleanly with no leftover dynamic users; and
`pbs-rotate-s3-key.py --check` passed end to end on the PBS host, which exercises
the whole chain through the new root.

**Automated root rotation is Enterprise-only.** Vault 1.20.4 *has* the
`rotation_period` / `rotation_schedule` fields on `aws/config/root`, but setting
either returns `rotation manager capabilities not supported in Vault community
edition`. So the schedule had to live outside Vault — see below.

#### Scheduled — 2026-08-21

`vault-aws-root-rotation`, a monthly CronJob in the `vault-maintenance`
namespace (`homelab_flux/infrastructure/configs/vault-root-rotation/`), with the
Vault policy, self-test role and Kubernetes auth role provisioned by
`homelab_ansible/ansible/vault/aws_root_rotation.yml`.

**Proven on its first real run**, not just deployed: it rotated `...QHAH` to
`...6H5S`, and IAM, `aws/config/root`, both engine roles and the PBS rotation
chain were all confirmed independently afterwards.

    kubectl -n vault-maintenance create job \
      --from=cronjob/vault-aws-root-rotation manual-1
    kubectl -n vault-maintenance logs job/manual-1

Monthly against a 90-day interval, so the key stays far younger than the limit
and two consecutive failures still leave room before anything is overdue.

**Why a CronJob and not a host timer.** This rotation needs no secret material:
the job asks Vault to rotate its own credential and the new value never leaves
Vault, so a Kubernetes ServiceAccount is a complete identity for it. A host-based
timer would have needed an AppRole `secret_id` on disk — a static credential
whose only job is protecting the rotation of another static credential.

**It verifies rather than assumes.** Confirming `config/root` changed does not
prove the new credential can reach IAM, and a broken engine takes out Terraform's
remote state and the PBS key rotation silently for weeks. So the job mints from
`aws/roles/rotation-selftest`, whose inline policy is an explicit `Deny` on
everything — that forces Vault to call IAM with the new credential, which is the
actual proof, while handing the job no AWS capability at all. Pointing the check
at a real role such as `pbs-key-rotator` would have proven the same thing and
given the job the ability to manage the PBS key. It retries past IAM's eventual
consistency, then revokes the identity.

Three things that are load-bearing and easy to mistake for boilerplate:

- **The `system:auth-delegator` ClusterRoleBinding is required.**
  `auth/prod-kubernetes/config` has no `token_reviewer_jwt`, so Vault performs
  the TokenReview using the *client's* JWT. Without the binding the Vault role
  exists, the ServiceAccount exists, and login still fails — with a *Kubernetes*
  permission error, which sends you looking in the wrong place.
- **Revocation is by lease id, not `sys/leases/revoke-prefix`.** That endpoint
  requires `sudo`; revoking a known lease id by path does not. This job holds
  `sudo` on nothing.
- **TLS is verified, unlike the VSO `VaultConnection`, which sets
  `skipTLSVerify`.** The job presents a token exchangeable for one that can
  rotate the engine root, so anything able to impersonate Vault to it would
  capture that. Pinned to the Vault *intermediate* rather than the
  `myrobertson-DC1-CA-1` root that issued it: both verify, but trusting the root
  would accept any certificate the domain CA has ever issued.

Failures surface as `KubeJobFailed`, which is already active — no custom rule
needed. `backoffLimit` is 1 because `rotate-root` is not idempotent and each
retry burns another access key.

Its age is still not in the credential-age metrics, and deliberately so: a KV
record would be a stale copy nothing updates, and the schedule now answers the
freshness question structurally instead.

Incidentally confirmed safe: a partial write to `config/root` (rotation fields
only, no `access_key`) does **not** wipe the stored credential. Established on a
throwaway `aws` mount configured with a deliberately fake key rather than by
experimenting on the live engine.

## Open questions for the operator

- **`vault-offsite-snapshot` sits inside the engine root's wildcard.** The
  `user/vault-*` grant was written for the throwaway users Vault creates, but it
  also matches `vault-offsite-snapshot`, a real, permanent user backing the Vault
  offsite snapshot job. So the AWS secrets engine can create and delete access
  keys on it. Pre-existing, not introduced by the 2026-08-21 rotation, and not
  fixed there because narrowing the wildcard risks breaking dynamic credential
  generation. Fixing it properly means giving Vault's dynamic users a distinct
  path or prefix (`vault-dyn-*`, or an IAM path) and scoping the policy to that.
- **`homelab-prod-backups` is now empty and can probably go**, along with
  `HomelabS3BackupProvisioningPolicy`, which grants `s3:CreateBucket` and
  `s3:ListAllMyBuckets` on `*`. 1,128 historical CNPG `Backup` CRs still
  reference the emptied bucket and will mislead anyone reading backup history.
- **6 of 10 CNPG clusters have no restore verification**, including the two
  largest datasets. Backups are scheduled and completing, but nothing proves they
  restore. See the section above.
- **`netbootxyz` has no ReplicationSource.** Two Bound PVCs, no current backup of
  any kind. Unrelated to this work, found while checking coverage.
- Does anything besides Terraform rely on `homelab` having admin rights? A
  CloudTrail review over 90 days would answer this definitively; it was not run
  as part of this scoping.
- `homelabTalosEtcdBackups` grants `s3:*` on its bucket. Confirm which operations
  `talos-etcd-backup` actually issues before narrowing it.
- Should `HomelabS3BackupProvisioningPolicy` (which allows `s3:CreateBucket` and
  `s3:ListAllMyBuckets` on `*`) belong to a workload at all, or only to
  Terraform?
