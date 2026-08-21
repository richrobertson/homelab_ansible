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
| PBS `pbs-s3` datastore | `/etc/proxmox-backup/s3.cfg` on the PBS VM | **nothing — static file** | `myrobertson-homelab-pbs` |
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
6. **Schedule PBS rotation** (Ansible or a CronJob) as the only remaining static
   credential.

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

### Remaining

- PBS (`/etc/proxmox-backup/s3.cfg` on the PBS VM) — bucket
  `myrobertson-homelab-pbs`. The only remaining *live* consumer of `...5KQF`
  besides Terraform. Not VSO-managed; needs an on-host edit and a
  `proxmox-backup-proxy` reload.
- Then Terraform onto its own identity, and only then remove
  `AdministratorAccess` from `homelab`.

## Open questions for the operator

- Does anything besides Terraform rely on `homelab` having admin rights? A
  CloudTrail review over 90 days would answer this definitively; it was not run
  as part of this scoping.
- `homelabTalosEtcdBackups` grants `s3:*` on its bucket. Confirm which operations
  `talos-etcd-backup` actually issues before narrowing it.
- Should `HomelabS3BackupProvisioningPolicy` (which allows `s3:CreateBucket` and
  `s3:ListAllMyBuckets` on `*`) belong to a workload at all, or only to
  Terraform?
