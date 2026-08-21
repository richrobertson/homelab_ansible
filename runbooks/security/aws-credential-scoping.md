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

## Open questions for the operator

- Does anything besides Terraform rely on `homelab` having admin rights? A
  CloudTrail review over 90 days would answer this definitively; it was not run
  as part of this scoping.
- `homelabTalosEtcdBackups` grants `s3:*` on its bucket. Confirm which operations
  `talos-etcd-backup` actually issues before narrowing it.
- Should `HomelabS3BackupProvisioningPolicy` (which allows `s3:CreateBucket` and
  `s3:ListAllMyBuckets` on `*`) belong to a workload at all, or only to
  Terraform?
