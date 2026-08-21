# Credential lifecycle roadmap

Follow-up work arising from the 2026-07-29 credential exposure incident.

## Why this exists

Five production credentials sat in a public GitHub repository for roughly nine
months. The compromise assessment found no evidence any of them were used, so
the damage was contained. But the incident exposed something the leak itself did
not cause: **this estate has no credential lifecycle.** Secrets are created once,
stored, and then live indefinitely. Nothing expires them, nothing rotates them,
and nothing notices when rotation has not happened.

The clearest single measurement: the AD account `ldap@myrobertson.net` is a
Domain Admin whose password was last set **2020-12-15** — 2,052 days before the
incident. Its exposure window was 9 months. Its rotation interval was never.

The rotation attempt during the incident also surfaced the second-order problem.
Two of three credentials could not be rotated at all, because rotation had
dependencies nobody had mapped: PowerDNS authenticating to Postgres as the
superuser, Authelia and Nextcloud binding to AD with the same account. A
credential you cannot rotate is a credential you cannot contain.

This roadmap is ordered so that each item makes the next one possible.

## Measured starting state

All figures verified against the live estate on 2026-07-29.

**Active Directory**

| Property | Value | Assessment |
|----------|-------|------------|
| Domain / forest functional level | 7 (Server 2016) | Supports gMSA (needs ≥ 2012) |
| KDS root key | **absent** | gMSA unavailable until created |
| Existing gMSAs | **0** | — |
| `maxPwdAge` | 42 days | Reasonable, but bypassed below |
| `minPwdLength` | **7** | Weak |
| `pwdHistoryLength` | 24 | Fine |
| `lockoutThreshold` | **0** | No lockout: unlimited password guessing |
| `krbtgt` last rotated | 2020-11-30 | Never rotated |

Nine accounts carry `DONT_EXPIRE_PASSWORD`, five of them privileged
(`adminCount=1`), which is how the 42-day policy is bypassed in practice:

| Account | Password age | Privileged |
|---------|--------------|------------|
| `ldap` | 2,052 days | yes — Domain Admin, and the leaked credential |
| `synbackup` | 1,387 days | yes |
| `roy` | 986 days | yes |
| `rich` | 986 days | yes |
| `netsvc` | 325 days | no — but sits in DnsAdmins (privesc-to-SYSTEM path) |
| `svc-syno-repl` | 92 days | yes |
| `svc-syno-admin` | 91 days | no |
| `keycloak-ldap` | 23 days | no |
| `Guest` | never set | no |

**Vault** already has most of the machinery required and it is under-used:

| Mount | Type | Status |
|-------|------|--------|
| `postgres/` | database | **mounted**, dynamic credentials available, not in use |
| `aws/` | aws | mounted |
| `pki/`, `pki_int/`, `pki_int_prod/`, `pki_int_staging/` | pki | in active use — short-lived certs already work |
| `secret/` | kv | where the long-lived static secrets live |

Auth methods available: `approle`, `jwt`, `ldap`, `prod-kubernetes`,
`staging-kubernetes`.

The PKI story is the proof this estate can do lifecycle well: certificates are
issued short (720h), renewed automatically by Vault Agent, and applied without
human involvement. Nothing equivalent exists for passwords.

---

## Workstream 1 — Make passwords expire

**Problem.** A 42-day `maxPwdAge` that nine accounts opt out of is not a policy,
it is a suggestion. The two longest-lived exemptions are both privileged.

**Tasks**

1. Remove `DONT_EXPIRE_PASSWORD` from every account that is a human user
   (`rich`, `roy`). These should follow the domain policy like any other user.
2. For each service account (`synbackup`, `netsvc`, `svc-syno-repl`,
   `svc-syno-admin`, `keycloak-ldap`, `ldap`), do not simply remove the flag —
   an unattended account whose password expires is an outage waiting for a
   maintenance window. Instead, route each through Workstream 3 or 4 first, and
   remove the flag only once rotation is automated or the account is retired.
3. Raise `minPwdLength` from 7 to at least 14. Seven characters is below any
   current guidance and trivially brute-forced offline from a captured hash.
4. Set `lockoutThreshold` to a non-zero value (10 is a reasonable balance).
   Today an attacker may guess passwords indefinitely with no interruption and
   no signal.
5. Disable the `Guest` account if it is not deliberately in use.

**Acceptance.** No account carries `DONT_EXPIRE_PASSWORD` except those
explicitly registered in Workstream 4 as managed. `lockoutThreshold > 0`.
`minPwdLength >= 14`.

**Codify in** `homelab_ansible/ansible/domain/`, alongside the existing domain
playbooks, so the policy is enforced rather than set once by hand.

---

## Workstream 2 — Enforce and detect rotation

**Problem.** Nothing measures credential age, so drift is invisible. The `ldap`
account aged five and a half years without anything noticing. This is the same
failure shape as the VSO sync (73/74 secrets failing while reporting
`Healthy=True`) and the unenforced NetworkPolicies: a control that exists,
reports fine, and does nothing.

**Tasks**

1. Add rotation metadata to every Vault secret: a `rotated_at` date and a
   `rotation_interval_days`. The Proxmox rotation on 2026-07-29 already set
   `password_rotated_at` on `secret/proxmox/cl0/terraform` — adopt that as the
   convention everywhere.
2. Write an exporter publishing credential age as a metric — both AD
   `pwdLastSet` age and Vault `rotated_at` age. Follow the existing
   node_exporter textfile pattern used by
   `ansible/proxmox/proxmox_transport_metrics.yml` and
   `ansible/proxmox/ceph_fragmentation_metrics.yml`.
3. Add Prometheus alert rules in `homelab_flux/infrastructure/configs/`:
   warn past the rotation interval, escalate well past it, and — importantly —
   alert when the exporter itself stops reporting. The fragmentation alerts
   added on 2026-07-29 include exactly this `absent()` guard; copy the pattern.
   Without it, a dead exporter reads identically to "everything current".
4. Enable Vault audit logging. During the incident `sys/audit` returned 403, so
   it could not even be confirmed that audit logging is on. Without it there is
   no record of which secrets were read, by whom, or when — which is precisely
   what an exposure investigation needs.
5. Enable Postgres connection logging on `subdb1` (`log_connections`,
   `log_disconnections`). Its compromise verdict was only "no evidence" rather
   than "no compromise" because no authentication trail exists at all.

**Acceptance.** A dashboard shows every credential's age against its interval,
and an alert fires before anything exceeds it — including when the measurement
stops working.

---

## Workstream 3 — Automate rotation

**Problem.** Manual rotation does not scale and, as the incident showed, is
often blocked by unmapped dependencies. The estate already proves the pattern
works for certificates; extend it to passwords.

**Tasks**

1. **Postgres dynamic credentials.** The `postgres/` database secrets engine is
   already mounted, and `homelab_bootstrap/terraform/modules/vault_db_secret_backend/`
   already defines a connection and a `pgx-role`. It is instantiated at
   `terraform/substrate/postgresql-database/main.tf:30`. Finish it:
   - Blocked on the `pg_hba.conf` fix — dynamic credentials are meaningless
     while `host all all 0.0.0.0 trust` accepts unauthenticated superusers.
   - Give PowerDNS its own database role instead of the `postgres` superuser.
     This is the change that makes the Postgres credential rotatable at all.
   - Then move consumers to short-lived leased credentials.
2. **Retire static credentials in favour of Vault auth methods.** Where a
   consumer runs in Kubernetes, use the `prod-kubernetes` / `staging-kubernetes`
   auth mounts rather than a stored password. Where it runs on a host, use
   AppRole, as the Proxmox certificate agent already does.
3. **Automate what cannot be made dynamic.** Some credentials will stay static
   (appliance logins, third-party services). For these, write rotation playbooks
   that change the credential *and* update Vault *and* restart consumers as one
   operation. The incident's lesson is that these three steps must be atomic —
   rotating Postgres without rewriting `pdns.conf` breaks DNS, and updating
   Vault without a working VSO changes nothing.

   **First one shipped 2026-08-21: the PBS S3 key**
   (`ansible/proxmox/pbs_s3_key_rotation.yml`), the estate's last purely static
   credential. Proven by a forced live rotation the same day, not just deployed.
   Details in [aws-credential-scoping.md](aws-credential-scoping.md).

   Two things from it generalise to the rest of this task:

   - **Don't let a credential rotate itself.** The easy implementation grants
     the key `iam:CreateAccessKey` on its own user. That would let a stolen key
     mint its own successor and survive every future rotation, removing the only
     thing rotation buys. The rotator identity is leased from Vault's `aws/`
     engine per run and revoked afterwards, so automating rotation did not
     create a new static credential that nothing rotates. Any rotation
     automation that needs provider API rights should be held to the same bar.
   - **Order it so every failure before the last step is recoverable.** Mint,
     prove, swap, reload, verify, record, *then* revoke. The provider's support
     for two live credentials is the safety mechanism, and Vault is written
     second-to-last because it is a record rather than the thing being rotated.
     A failure at the record step deliberately leaves two live credentials and
     exits loudly rather than revoking with a stale record.

   PBS was the easy case: nothing reads its key from Vault, so there is no
   consumer to restart. The remaining `external_provider` members (Cloudflare,
   Docker) have Kubernetes consumers, so their rotation is not atomic until the
   VSO propagation step is included — which is what the prerequisite below is
   about.
4. **Map dependencies before automating.** For each credential, record every
   consumer, how it obtains the value, and whether it needs a restart. The
   absence of this map is why two of three rotations were abandoned. Keep it
   next to the secret, in Vault metadata or in this directory.

   **Started 2026-08-03.** `ansible/proxmox/vars/secret_rotation_classes.yml`
   classifies all 92 secrets that `VaultSecretsPastRotationInterval` fires on
   (of 139 total; 20 are 180–365 days old, 72 are 90–180). Every one is assigned
   a class; none is unclassified.

   | Class | Count | Automatable |
   |---|---:|---|
   | `k8s_secret` | 30 | yes, once a non-AD consumer map exists |
   | `restic_key` | 29 | **no — see below** |
   | `appliance` | 13 | partial |
   | `external_provider` | 8 | yes |
   | `ceph_auth` | 6 | **no — see below** |
   | `ad_account` | 3 | partial; tooling exists |
   | `review_obsolete` | 3 | delete rather than rotate |

   **The finding that matters: this alert must not be treated as a bulk
   action.** 29 of the 92 are VolSync restic repository passwords. A restic
   repository is encrypted with a master key held in key slots, and
   `RESTIC_PASSWORD` unlocks a slot rather than being the key. Writing a new
   value into Vault re-keys nothing: the next backup fails to open the
   repository, and if the old password has been discarded, every snapshot in it
   is unrecoverable. The correct order is `restic key add`, verify, `restic key
   remove`, *then* Vault — the repository operation is the rotation and Vault
   records it afterwards.

   A further 6 are Ceph CSI keyrings mirroring `ceph auth` entities in the
   external cluster, where a Vault-only write silently breaks volume
   attach/detach cluster-wide.

   So 35 of 92 would be actively destructive to rotate the obvious way, which is
   precisely the dependency mapping this task exists to force.

**Acceptance.** Postgres credentials are leased and short-lived. Every remaining
static credential has a one-command rotation playbook and a documented consumer
list.

**Prerequisite.** Fix the VSO sync failure first. Until Vault → Kubernetes
propagation works, no automated rotation can reach the cluster, and rotation
that does not propagate is worse than none: Vault and reality diverge silently.

---

## Workstream 4 — Managed service accounts

**Problem.** Six AD service accounts hold static passwords that a human set and
no process changes. Windows solves this natively and the domain is already
capable of it.

Group Managed Service Accounts have their passwords generated and rotated by AD
itself, by default every 30 days, with no human ever knowing the value. There is
nothing to leak into a Terraform state file.

**Tasks**

1. Create the KDS root key. This is the missing prerequisite — the domain is at
   functional level 7 (Server 2016), comfortably above the 2012 minimum, but no
   `msKds-ProvRootKey` exists and no gMSAs have been created.
   `Add-KdsRootKey -EffectiveImmediately` normally requires a 10-hour
   propagation wait; with a single writable DC the effective time may be
   back-dated, at the cost of a brief window where replication assumptions do
   not hold.
2. Migrate candidates, easiest first:
   - `svc-syno-repl`, `svc-syno-admin` — only if Synology DSM supports gMSA
     binding, which it likely does not. Verify before committing; if not, these
     stay static and go to Workstream 3 instead.
   - `netsvc` — check what consumes it. Also remove it from **DnsAdmins**,
     which is a documented privilege-escalation path to SYSTEM on a DC.
   - `synbackup` — likely a backup service; check whether the consumer supports
     gMSA.
   - `keycloak-ldap`, and the LDAP bind currently using `ldap@` — most
     applications cannot use a gMSA for an LDAP simple bind, so these probably
     need a dedicated least-privilege static account under Workstream 3 rather
     than a gMSA. Be realistic about this rather than forcing the pattern.
3. **Split `ldap@myrobertson.net` regardless of gMSA feasibility.** A directory
   bind account needs read access to the directory and nothing else. Create
   separate least-privilege accounts per consumer and remove `ldap` from Domain
   Admins. This shrinks the blast radius of the next leak far more than
   rotation does.

   **Planned in detail in [Retiring
   `ldap@myrobertson.net`](ldap-account-retirement.md).** That document
   supersedes this task. Three corrections to the assumptions above, measured
   on 2026-07-29:

   - The consumer list is **nine, not four**. Proxmox VE (realm `myrobertson`),
     Proxmox Backup Server (realm `myrobertson.net`), and the Terraform `dns`
     and `microsoftadcs` providers also bind as `ldap@`. A copy of the password
     is additionally embedded in the PBS config archive at
     `secret/proxmox/pbs/prod/config`, where any rotation would silently miss
     it.
   - **No consumer in this estate can use a gMSA.** Every one performs an LDAP
     simple bind from Linux/Kubernetes, or runs from a non-domain-joined control
     host. Create the KDS root key as a prerequisite, but create no gMSAs.
   - **gMSA cannot meet the 8-hour rotation target** —
     `ManagedPasswordIntervalInDays` has a one-day minimum. Rotation therefore
     runs through the Vault **LDAP secrets engine**, which is not currently
     mounted. Cadence is set per identity rather than uniformly, and the
     Kubernetes-facing accounts are gated on the VSO fix.
4. Rotate `krbtgt` twice, with the replication interval between rotations.
   It has never been rotated since 2020-11-30. A single rotation invalidates
   only the current key; two are required to fully retire the previous one.

**Acceptance.** No AD service account has a human-set password except where a
consumer demonstrably cannot support an alternative — and each of those is
documented with the reason. `ldap` is no longer a Domain Admin and no longer
serves four purposes.

---

## Workstream 5 — Prevent recurrence at the source

Rotation limits exposure duration. These stop the exposure happening.

**Tasks**

1. Enable **GitHub push protection** on all repositories. This is the strongest
   available control: it blocks the commit rather than detecting it afterwards.
   Both `homelab_bootstrap` and `homelab_flux` are public.
2. Add a `schedule` trigger to `secret-scan.yml` in all three repos. The
   `gitleaks-action` scopes its scan to the triggering event — on `push` and
   `pull_request` it only examines that event's commits, so **repository history
   is never scanned**. Full-history scanning happens only on `schedule` or
   `workflow_dispatch`.
3. Be clear-eyed about what scanning would have caught. Terraform state stores
   passwords as ordinary JSON strings with no distinguishing format. Pattern
   scanners would have matched the `ghp_` GitHub token — and plausibly nothing
   else. The Proxmox, AD, and Postgres passwords would likely have passed a
   scheduled gitleaks run cleanly. **Scanning is a backstop, not the control.**
   The control is never producing the artifact: `.gitignore` coverage,
   `terraform.tfstate` in remote state, and push protection.
4. Fix the `.gitignore` gap: `**/terraform.tfstate` does not match
   `errored.tfstate`, the file Terraform writes when an apply fails. Cover
   `**/*.tfstate` and `**/*.tfstate.*`.
5. Move Terraform state to a remote backend with encryption if it is not
   already. Local state that can be accidentally committed is the root cause
   here, and no amount of scanning addresses it.

---

## Suggested order

1. Push protection and `.gitignore` (Workstream 5) — minutes, zero risk, stops
   the bleeding.
2. `lockoutThreshold`, `minPwdLength` (Workstream 1) — low risk, immediate.
3. Fix VSO sync — blocks Workstream 3 entirely.
4. Fix `pg_hba.conf` and give PowerDNS its own role — unblocks Postgres
   rotation and closes unauthenticated superuser access.
5. Split `ldap@` and rotate it (Workstream 4, task 3) — the largest single
   reduction in blast radius.
6. Credential-age metrics and alerts (Workstream 2) — makes everything after
   this self-policing.
7. KDS root key and gMSA migration (Workstream 4).
8. `krbtgt` double rotation.

## Related

- [Proxmox MCP server](../proxmox/proxmox-mcp-server.md) — token rotation
  procedure, an example of the pattern this roadmap generalises
- `homelab_ansible/SECURITY.md` — secret-handling rules for this repository
