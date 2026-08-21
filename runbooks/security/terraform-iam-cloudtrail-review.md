# CloudTrail review — can `homelab_terraform` drop `AdministratorAccess`?

Reviewed 2026-08-21, to answer the open question left by
[aws-credential-scoping.md](aws-credential-scoping.md).

## Short answer

**A service-scoped policy is worth writing, but it buys less than it looks like.**
Terraform manages `aws_iam_user`, `aws_iam_access_key`, `aws_iam_user_policy`,
`aws_iam_role` and `aws_iam_role_policy`. Any policy that lets it do that is
**privilege-escalation-capable**: whoever holds the key can create a user and
attach `AdministratorAccess` to it. Narrowing the policy reduces *accidental*
blast radius and makes misuse noisy, but it does not meaningfully reduce
*adversarial* blast radius unless it is paired with a **permissions boundary**.

Anyone doing this work should decide up front which of those two they are buying.

## Method

No CloudTrail trail is configured, so this used Event History via
`cloudtrail lookup-events`, filtered by `Username`, across `us-west-2` (the
estate's region) and `us-east-1` (where IAM, Route53 and some SES events land).
Pagination was raised until the result set stopped truncating — the first pass
capped at exactly 600 per user/region and was silently incomplete.

`homelab_terraform`: **826 events, untruncated, 2026-07-30 → 2026-08-20.**
Zero events from the reviewing session, so this is Terraform's own footprint.

## What it actually did

Services touched (reads and writes):

| Events | Service |
| ---: | --- |
| 179 | s3 |
| 171 | ec2 |
| 133 | iam |
| 101 | cloudwatch (`monitoring`) |
| 86 | sns |
| 45 | logs |
| 32 | sts |
| 31 | ses |
| 21 | lambda |
| 15 | events |
| 10 | kms |
| 1 | cloudtrail, route53 |

Mutating calls in the window:

```
10  kms:Decrypt
 6  iam:DeleteUserPolicy        3  iam:PutUserPolicy
 3  iam:DeleteAccessKey         2  iam:CreateAccessKey
 2  iam:CreateUser              2  iam:DeleteUser
 2  iam:DetachUserPolicy        1  iam:UpdateAccessKey
 1  s3:CreateBucket             1  s3:PutBucketVersioning
 1  s3:PutBucketEncryption      1  s3:PutBucketPublicAccessBlock
 1  s3:PutBucketLifecycle
```

## Three reasons not to build the policy from that list alone

**1. Observed is not required.** No mutating calls appear for ec2, cloudwatch,
sns, route53, ses, lambda, logs or events in this window — but the code plainly
declares them: 19 cloudwatch resources, 9 sns, 9 s3, 6 route53, 5 vpc, 4 ses,
3 eip, 2 sqs, 2 sesv2, 2 lambda, plus subnet/ssm/security-group/igw/instance. A
policy built from observed mutations would work until the next time Terraform
actually creates one of those, then fail mid-apply. **Scope by declared resource
type, not by recent API calls.**

**2. S3 object access is invisible here.** Without a trail, Event History records
management events only — no `GetObject`/`PutObject`. Terraform's remote state
lives in `myrobertson-homelab-terraform` and every plan reads it, yet none of
that appears above. Any policy must explicitly grant object access to the state
bucket regardless of what the log shows.

**3. The window is three weeks, not ninety days.** `homelab_terraform` was
created 2026-04-22 but has no events before 2026-07-30. Anything quarterly or
annual — a cert rotation, a new bucket, a DR exercise — is not represented.

## Recommended shape

Scope by service against the declared resource types, and constrain the dangerous
part:

- **IAM:** restrict to the principals Terraform actually manages rather than `*`.
  It creates exactly three users (`vault_offsite_backup`, `ses_smtp`,
  `grafana_cloudwatch` — names come from locals) and two roles (`ssm`,
  `email_canary`). Constrain by name or by a dedicated path.
- **Permissions boundary:** attach a `iam:PermissionsBoundary` condition to
  `CreateUser`/`PutUserPolicy`/`AttachUserPolicy` so anything Terraform creates
  cannot exceed the boundary. **This is the control that actually closes the
  escalation path** — without it, the IAM grant is equivalent to admin.
- **S3:** full object + bucket-config rights on the buckets it owns
  (`myrobertson-homelab-terraform`, `homelab-prod-backups`,
  `myrobertson-homelab-talos-etcd-backups`, `myrobertson-homelab-pbs`,
  `homelab-prod-nextcloud`, `myrobertson-homelab-vault-offsite-backups`), not `*`.
- **Everything else:** allow the service actions for the declared resource types
  — cloudwatch, sns, sqs, route53, ses/sesv2, lambda, logs, events, ssm, ec2/vpc,
  kms:Decrypt, sts.

## Do this first

**Enable a CloudTrail trail with S3 data events for the state bucket.** Right now
there is no durable audit record — Event History is 90 days, is not exportable,
and omits exactly the object-level access that matters for a state file. Any
future review of this question hits the same wall this one did.

## Honest limitation

This review says what Terraform *used*. It cannot prove a narrower policy is
sufficient, because the code manages resources it did not touch in the window.
The only safe validation is a `terraform plan` (and ideally a no-op `apply`)
under the new policy, with the old one ready to reattach. Do not swap the policy
without that rollback path.
