# Retiring `ldap@myrobertson.net`

Replacing one shared Domain Admin with per-service least-privilege identities
that rotate.

This runbook is the detailed plan for [Workstream 4, task
3](credential-lifecycle-roadmap.md#workstream-4--managed-service-accounts) of
the credential lifecycle roadmap. Everything below was measured against the live
estate on 2026-07-29 unless marked otherwise.

## Why

`ldap@myrobertson.net` (`CN=ldap,CN=Users,DC=myrobertson,DC=net`) is a Domain
Admin whose password was last set **2020-12-15** — 2,052 days ago — with
`DONT_EXPIRE_PASSWORD` set. It sat in a public GitHub repository for roughly
nine months. The compromise assessment found no evidence of misuse.

The problem is not the age of the password. It is that one Tier-0 credential is
simultaneously:

- the LDAP bind for Authelia (prod and staging),
- the LDAP bind for Nextcloud (prod and staging, two deployments),
- the LDAP bind for the Vault `ldap/` auth method,
- the LDAP bind for the Proxmox VE `myrobertson` realm,
- the LDAP bind for the Proxmox Backup Server `myrobertson.net` realm,
- the Terraform credential for GSSAPI dynamic DNS and AD CS enrolment,
- and the WinRM automation account for every Windows host in the estate.

Rotating it is a synchronised nine-way outage. That is why it has never been
rotated. **The decision is to retire it, not to rotate it.**

## Measured starting state

| Property | Value |
|----------|-------|
| DN | `CN=ldap,CN=Users,DC=myrobertson,DC=net` |
| `adminCount` | 1 |
| `memberOf` | `CN=Domain Admins,CN=Users,DC=myrobertson,DC=net` |
| `userAccountControl` | 66048 (`NORMAL_ACCOUNT` + `DONT_EXPIRE_PASSWORD`) |
| `pwdLastSet` | 2020-12-15 |
| `lastLogonTimestamp` | 2026-07-22 — **actively in use** |
| `servicePrincipalName` | none |
| `userWorkstations` | not set |
| `msDS-RevealedDSAs` | empty — **not cached on the RODC `dns01`** |

Domain: `myrobertson.net`, functional level 7 (Server 2016). Writable DCs `dc1`
(192.168.1.3, Server 2025) and `rhonda` (192.168.1.245, Server 2022); RODC
`dns01` (192.168.1.101, Server 2025). Member server `janice` (192.168.1.33).

`lockoutThreshold: 0`, `minPwdLength: 7`, `maxPwdAge: 42 days`,
`ms-DS-MachineAccountQuota: 10`.

**KDS root key: absent.** `CN=Master Root Keys,CN=Group Key Distribution
Service,CN=Services,CN=Configuration,DC=myrobertson,DC=net` exists but is empty.
Zero gMSAs. The gMSA schema (`ms-DS-ManagedPasswordInterval`) is present.

---

## 1. Consumer inventory

Nine live consumers. The known-consumer list in the original brief was
incomplete: **Proxmox VE, Proxmox Backup Server, and the Terraform `dns` and
`microsoftadcs` providers were not on it.**

Every location holding the credential was found by hashing the Vault value with
SHA-256 and comparing against every field of every Vault KV secret, and against
every base64-decoded value of all 430 prod and 318 staging Kubernetes Secrets.
No value was ever printed.

### Where the credential lives today

| # | Location | Kind |
|---|----------|------|
| 1 | Vault `secret/windows/domain/ldap` field `password` | canonical |
| 2 | Vault `secret/windows/domain/ldap` field `authentication.ldap.password.txt` | duplicate |
| 3 | Vault `secret/authelia/prod` field `authentication.ldap.password.txt` | copy |
| 4 | Vault `secret/authelia/stage` field `authentication.ldap.password.txt` | copy |
| 5 | Vault `secret/nextcloud/prod/ldap` field `LDAP_BIND_PASSWORD` | copy |
| 6 | Vault `secret/nextcloud/staging/ldap` field `LDAP_BIND_PASSWORD` | copy |
| 7 | Vault `secret/proxmox/pbs/prod/config` field `config_tar_gz_b64` → `etc/proxmox-backup/ldap_passwords.json` | **hidden copy inside a config archive** |
| 8 | k8s prod `default/authelia-secret` key `authentication.ldap.password.txt` | VSO-synced |
| 9 | k8s prod `default/nextcloud-ldap-secret` key `LDAP_BIND_PASSWORD` | VSO-synced |
| 10 | k8s prod `nextcloud/nextcloud-ldap-secret` key `LDAP_BIND_PASSWORD` | **orphan, no VaultStaticSecret owns it** |
| 11 | k8s staging `default/authelia-secret` key `authentication.ldap.password.txt` | VSO-synced |
| 12 | k8s staging `default/nextcloud-ldap-secret` key `LDAP_BIND_PASSWORD` | VSO-synced |
| 13 | Proxmox VE `/etc/pve/priv/realm/myrobertson.pw` | on-appliance |
| 14 | PBS `/etc/proxmox-backup/ldap_passwords.json` | on-appliance |
| 15 | Vault `auth/ldap/config` field `bindpass` | write-only, not readable |

Item 7 matters disproportionately. The PBS disaster-recovery archive documented
in [PBS configuration DR from Vault](../proxmox/pbs-config-dr-from-vault.md)
contains a cleartext copy of the AD bind password, exported 2026-04-27. Any
rotation that does not re-export it leaves a stale credential that a DR restore
would faithfully reinstate. **A rotation would silently miss it.**

Item 10 is an orphan: the secret carries VSO ownership labels but has no
`ownerReferences` and no `VaultStaticSecret` exists in the `nextcloud`
namespace. It feeds the three running `nextcloud-migration-ldap` replicas and
nothing will ever rotate it.

### Per-consumer detail

| Consumer | What it does with the account | Minimum AD privilege actually needed | How it obtains the credential | Restart needed to change it? |
|---|---|---|---|---|
| **Authelia** prod + staging | LDAP simple bind to `ldaps://rhonda.myrobertson.net`, `base_dn=cn=Users,dc=myrobertson,dc=net`, AD implementation defaults. Searches users, reads `memberOf` for group authorisation. | Read on user and group objects under `CN=Users`. No write. | VSO → `default/authelia-secret` → **file** at `/secrets/internal/authentication.ldap.password.txt` via `AUTHELIA_AUTHENTICATION_BACKEND_LDAP_PASSWORD_FILE` | **Yes.** Authelia reads the file once at startup. No Reloader is installed; `rolloutRestartTargets` is empty. |
| **Nextcloud** `default` ns, prod + staging | LDAP bind to `rhonda.myrobertson.net:389` with StartTLS (`ldapTLS=1`) and **`turnOffCertCheck=1`**. User + group directory. | Read on user and group objects. No write. | VSO → `nextcloud-ldap-secret` → **env vars**, which seed Nextcloud's own DB config on first boot | **No, if changed correctly.** The live value is in Nextcloud's database, not the env. `occ ldap:set-config s01 ldapAgentPassword <new>` takes effect on the next request with no restart. Changing only the env var/Secret does nothing until the pod restarts. |
| **Nextcloud** `nextcloud` ns (`nextcloud-migration-ldap`, prod, 3 replicas) | As above, but `ldapBase=dc=myrobertson,dc=net` (domain root). | Read on user and group objects. | ~~Orphan Secret, no Vault source.~~ **Fixed 2026-08-03**: a `VaultStaticSecret` in the `nextcloud` namespace now owns it, sourced from `secret/nextcloud/prod/ldap`, with `rolloutRestartTargets` on the live deployment. homelab_flux #118. | As above. Note the deployment uses `strategy: Recreate`, so any restart of it *is* a full outage (~3 min) — never assume a rolling update here. |
| **Nextcloud** `nextcloud` ns (`nextcloud-migration`, **staging**, 1 replica) | As above. **Missing from this inventory until 2026-08-03**, when it was measured still binding as `ldap@myrobertson.net` at ~4-7 binds/hour — the last significant consumer after Authelia was retired. A retirement driven only by this table would have missed it. | Read on user and group objects. | **Nothing.** No `LDAP_*` env vars, no Secret, no `VaultStaticSecret`. The settings exist *only* in the Nextcloud database. | **No.** `occ ldap:set-config s01 ldapAgentName/ldapAgentPassword` takes effect on the next request. There is no manifest to change. |
| **Vault `ldap/` auth method** | Binds to find a user's DN, then re-binds as the user to authenticate. Reads `memberOf` for policy mapping. | Read `sAMAccountName`, `memberOf`, `objectClass` under `CN=Users`. No write. | `auth/ldap/config` `bindpass`, stored inside Vault | **No.** Vault re-reads config per request. |
| **Proxmox VE** realm `myrobertson` (type `ad`, **default realm**) | Realm sync and user authentication. `filter=(memberOf=CN=proxAdmins,...)`, `sync_attributes=email=mail`. Mode `ldaps`. | Read on user and group objects under `CN=Users`. No write. | `/etc/pve/priv/realm/myrobertson.pw` on each node | **No.** `pveproxy` reads the file per request. Set via `PUT /access/domains/myrobertson`. |
| **Proxmox Backup Server** realm `myrobertson.net` (type `ad`, default) | Realm sync and authentication. Same filter. **Mode `ldap` — cleartext.** | Read on user and group objects under `CN=Users`. No write. | `/etc/proxmox-backup/ldap_passwords.json` | **No.** Set via `PUT /access/domains/myrobertson.net`. |
| **Terraform `dns` provider** | GSSAPI dynamic DNS updates against `dc1.myrobertson.net` for the `myrobertson.net` zone. | Create/delete `dnsNode` and write `dnsRecord` in the delegated zone. **Not DnsAdmins.** | `data.vault_generic_secret.windows_domain_admin` → `secret/windows/domain/ldap` (`terraform/data.tf:11-13`, `providers.tf:3-12`) | N/A — read at plan/apply time. |
| **Terraform `microsoftadcs` provider** | Issues certificates from AD CS on `dc1`, over NTLM. Used for the Talos CA cert and the Vault PKI intermediate. | Read + **Enroll** on the two named certificate templates, and Request Certificates on the CA. Nothing else. | Same Vault path (`providers.tf:59-64`) | N/A. |
| **Ansible WinRM** | `ansible_user` for `dc1`, `rhonda`, `dns01`, `janice` over **HTTP/5985 with NTLM** (`inventory/environments/production.ini:47-53`). Installs Windows features, edits `HKLM` registry, manages services, registers scheduled tasks, creates and links GPOs, authorises DHCP in AD, provisions AD users/OUs and writes OU ACLs. | See §4 — this genuinely requires Tier-0 on the domain controllers and cannot be reduced below it. | Operator exports it from Vault into `SYN_ABB_WINDOWS_USERNAME`/`SYN_ABB_WINDOWS_PASSWORD` | N/A — read per run. |

### Consumers that are *not* affected

Verified and ruled out, so nobody has to re-check them:

- **Keycloak** — already binds as `keycloak-ldap@myrobertson.net`, over
  `ldaps://rhonda.myrobertson.net`. Not a consumer of `ldap@`.
- **Guacamole** — OIDC only (`OPENID_ISSUER=https://sso.myrobertson.com/realms/homelab`,
  `EXTENSION_PRIORITY=openid,postgresql,ban`). No LDAP extension.
- **Grafana** — generic OAuth against Keycloak. The `ldap-toml` key in
  `kube-prometheus-stack-grafana` is empty and LDAP is not enabled.
- **Synology DSM** (`scooter`, `kermit`) — uses `svc-syno-admin` and
  `svc-syno-repl`, not `ldap@`.
- **Mealie / Immich / other apps** — OIDC against Keycloak or Authelia.
- **`claude_workstation` sssd/realmd role** — joins with a separate account at
  Vault `windows/domain/service-accounts/linux-realm-join`. Note that Vault path
  **does not currently exist**; the role would fail if run today. Out of scope
  here, but worth raising.

---

## 2. Findings surfaced while mapping consumers

These are independent of the retirement and should be tracked separately.

1. **Vault's `ldap/` auth method binds over cleartext.**
   `auth/ldap/config` has `url = ldap://rizzo.myrobertson.net,ldap://rhonda.myrobertson.net`,
   `starttls = false`. Every Vault login by a human sends their AD password in
   the clear. This was the specific item flagged for verification in the brief:
   **confirmed.**

2. **`rizzo.myrobertson.net` does not resolve.** Vault's *primary* LDAP URL
   points at a host that does not exist; it silently fails over to `rhonda`.

3. **Proxmox VE's realm `server1` is `192.168.1.244`, which is dead** (no
   response on 389 or 636). It falls back to `server2` = `192.168.1.245`. PBS has
   the same pair inverted, so PBS's *secondary* is the dead address.

4. **PBS binds over cleartext.** `domains.cfg` has `mode ldap`, not `ldaps`.

5. **Nextcloud disables TLS certificate validation.** `turnOffCertCheck = 1` in
   both prod and staging, both deployments, while using StartTLS on 389. Its
   LDAP traffic is trivially machine-in-the-middle-able. Authelia
   (`skip_verify: false`) and Keycloak (`useTruststoreSpi: ldapsOnly`) both
   validate correctly. Three different transport postures against one DC.

6. **Prod and staging share the identical AD bind password.** A staging
   compromise is a production compromise. This is true for `ldap@` and for
   `keycloak-ldap`.

7. **`GG-Synology-Snapshot-Replication` is a member of `Domain Admins`**, so
   `svc-syno-repl` is effectively a Domain Admin. It is also in `Backup
   Operators`, as is `synbackup`. Backup Operators can read `NTDS.dit` and is
   Tier-0 in practice.

8. **`netsvc` is in `DnsAdmins`** — a documented privilege-escalation path to
   SYSTEM on a domain controller via arbitrary DLL load.

9. **`synbackup` last authenticated 2024-01-23** — 918 days ago. It is almost
   certainly dead and carries `adminCount=1`.

10. **Authelia cannot see `OU=Family`.** Its `base_dn` is
    `cn=Users,dc=myrobertson,dc=net` and no `additional_users_dn` is set, so
    `stella` (the only user in `OU=Family`) cannot log in through Authelia. Prod
    Nextcloud searches the domain root, so she *can* log into Nextcloud. This is
    a pre-existing inconsistency, not something this work introduces.

11. **Staging Authelia is one restart from an outage.** Its Deployment projects
    four Secret keys (`pve_oidc`, `pbs_oidc`, `synology_scooter_oidc`,
    `synology_kermit_oidc`) that exist in neither the staging Secret nor
    `secret/authelia/stage`. The running pod predates the divergence.

12. **A plain, group-less service account can already read the entire
    directory.** Verified empirically by binding as `keycloak-ldap` (no group
    memberships, `adminCount=0`): it can enumerate all 16 users, read `Domain
    Admins` membership, list `OU=Domain Controllers`, and read the Configuration
    naming context. See §5 — this determines whether the ACL work is meaningful.

---

## 3. Naming convention

The estate already contains two conventions:

| Existing account | Convention | Location |
|---|---|---|
| `keycloak-ldap` | `<service>-<function>` | `OU=Service Accounts` |
| `svc-syno-repl` | `svc-<service>-<function>` | `CN=Users` |
| `svc-syno-admin` | `svc-<service>-<function>` | `CN=Users` |
| `netsvc` | none | `CN=Users` |
| `synbackup` | none | `CN=Users` |

**Adopt `svc-<service>-<function>`.** It is the more recent convention (the
`svc-syno-*` pair was created 2026-04), it prefixes rather than suffixes so all
service accounts sort together, and it already covers two of five accounts.

**Do not rename `keycloak-ldap`.** Renaming a `sAMAccountName` changes the bind
DN, which must then be updated in the Keycloak component config, three Vault
paths, and the `domain-admins-rbac-job`. Once Authelia is retired, Keycloak is
the sole identity provider for the estate and every login depends on that bind.
Changing its DN for cosmetic consistency is a gratuitous risk with no security
benefit. Enumerability is better served by the OU than by the prefix:
`Get-ADUser -SearchBase "OU=Service Accounts,DC=myrobertson,DC=net" -Filter *`
is the authoritative list, and it is exact.

`netsvc`, `synbackup`, `svc-syno-repl` and `svc-syno-admin` should be **moved
into `OU=Service Accounts`** (a move, not a rename — cheap, and `objectGUID` is
preserved so Keycloak's federation is unaffected). Rename `netsvc` →
`svc-dhcp-dnsupdate` only if its consumer is confirmed and can be updated in the
same change; otherwise leave the name and fix the group membership, which is the
part that actually matters.

### Target OU structure

```
DC=myrobertson,DC=net
├── CN=Users                      Administrator, krbtgt, rich, roy, groups
├── OU=Family                     stella, CN=Family
├── OU=Domain Controllers         dc1, rhonda, dns01
├── OU=Endpoints                  janice, bobo
└── OU=Service Accounts
    ├── OU=Bind                   read-only directory binds; Vault-rotated
    │     svc-vault-ldap, svc-nextcloud-ldap, svc-nextcloud-stg,
    │     svc-pve-ldap, svc-pbs-ldap, svc-authelia-ldap (interim),
    │     keycloak-ldap
    └── OU=Automation             write-capable or elevated
          svc-vault-ldapmgr, svc-tf-adcs, svc-tf-dnsupdate,
          svc-ansible-ad, svc-ansible-win, svc-syno-admin, svc-syno-repl,
          netsvc
```

The `OU=Bind` / `OU=Automation` split is not cosmetic. Vault's LDAP secrets
engine manager is delegated password-reset rights over **`OU=Bind` only**, so
compromising the rotation manager cannot reach the Tier-0 WinRM account. That
boundary is the reason the two OUs exist.

---

## 4. Target account design

All new accounts are created by
[`ansible/domain/provision_service_account_identities.yml`](../../ansible/domain/provision_service_account_identities.yml),
which reasserts the least-privilege invariants on every run and **fails the play**
if any account acquires `adminCount=1`, a privileged group, an SPN,
`DONT_EXPIRE_PASSWORD`, or `TRUSTED_FOR_DELEGATION`.

Common invariants for every identity below:

- `adminCount = 0`
- no membership of Domain Admins, Enterprise Admins, Schema Admins,
  Administrators, Account Operators, Backup Operators, Server Operators, Print
  Operators, DnsAdmins, Group Policy Creator Owners, Key Admins, or Enterprise
  Key Admins
- no `servicePrincipalName`
- no `DONT_EXPIRE_PASSWORD` (the whole point is that the password changes)
- member of `GG-Deny-Interactive-Logon`, referenced by a GPO denying *Log on
  locally*, *Log on through Remote Desktop Services*, and *Log on as a batch
  job* on all machines

### The table

Rotation column: **V-LDAP** = Vault LDAP secrets engine static role;
**V-AUTH** = Vault's native LDAP auth-method self-rotation.

| Identity | Type | Consumer | Exact privileges granted | Rotation | Blast radius if the credential leaks |
|---|---|---|---|---|---|
| `svc-vault-ldap` | conventional | Vault `ldap/` auth method | `GG-Directory-Bind-Read` only | **V-AUTH, 8h** | Read the user/group attributes in `CN=Users` and `OU=Family`. Cannot authenticate as anyone — Vault re-binds as the user itself. Cannot write. Cannot log on to any host. |
| `svc-nextcloud-ldap` | conventional | Nextcloud prod (both deployments) | `GG-Directory-Bind-Read` only | **V-LDAP, 8h** | Same read set. Nothing else. |
| `svc-nextcloud-stg` | conventional | Nextcloud staging | `GG-Directory-Bind-Read` only | **V-LDAP, 8h** | Same read set. Nothing else. Separate from prod so staging can be revoked independently. |
| `svc-pve-ldap` | conventional | Proxmox VE realm `myrobertson` | `GG-Directory-Bind-Read` only | **V-LDAP, 24h** | Same read set. Cannot log into Proxmox — the realm bind is not a Proxmox user. |
| `svc-pbs-ldap` | conventional | PBS realm `myrobertson.net` | `GG-Directory-Bind-Read` only | **V-LDAP, 24h** | Same read set. Note the value is duplicated into the PBS DR archive; see §7. |
| `svc-authelia-ldap` | conventional | Authelia prod + staging (**interim**) | `GG-Directory-Bind-Read` only | manual / 90d | Same read set. Delete when Authelia is decommissioned. |
| `keycloak-ldap` *(existing, rescoped)* | conventional | Keycloak user federation | `GG-Directory-Bind-Read`; **plus** `Reset Password` + `Change Password` + write `pwdLastSet` on descendant `user` objects of `OU=Family` **only if `editMode` stays `WRITABLE`** | **V-LDAP, 24h** | Read set, **plus the ability to reset the password of any user in `OU=Family`** (today: `stella`). If `editMode` is set to `READ_ONLY`, the write delegation is dropped entirely and the blast radius collapses to read-only. **This is the single highest-value decision in the design** — see §4.1. |
| `svc-vault-ldapmgr` | conventional | Vault LDAP secrets engine `ldap/config` binddn | `Reset Password` extended right + write `pwdLastSet` on descendant `user` objects of **`OU=Bind` only** | `vault write -f ldap/rotate-root`, 8h | Can reset the password of the seven read-only bind accounts, i.e. can become any of them. Net effect is still **directory read only**. Cannot reach `OU=Automation`, cannot reach human or admin accounts, cannot log on anywhere. |
| `svc-tf-adcs` | conventional | Terraform `microsoftadcs` | Read + `Enroll` on the two named certificate templates; Request Certificates on the CA. No directory write, no group. | **V-LDAP, 8h** | Can request certificates from those two templates. Because those templates issue a **subordinate CA certificate** for Vault PKI, an attacker could mint a CA trusted by the estate. **This is the highest-value new account.** See §4.2. |
| `svc-tf-dnsupdate` | conventional | Terraform `dns` (GSSAPI) | `Create Child`/`Delete Child` for `dnsNode` and `Write Property` `dnsRecord` on the `myrobertson.net` zone object. **Explicitly not DnsAdmins.** | **V-LDAP, 8h** | Can create, modify and delete DNS records in the `myrobertson.net` zone → can hijack any internal name, redirect SSO, and satisfy DNS-01 certificate challenges. Significant, but bounded to one zone and confers no code execution on a DC (which DnsAdmins does). |
| `svc-ansible-ad` | conventional | `provision_domain_accounts.yml`, `provision_service_account_identities.yml` | `Create Child`/`Delete Child` for `user`, `group`, `organizationalUnit`; `Generic Read`/`Generic Write` on descendants; `Write DACL` on `OU=Family` and `OU=Service Accounts` | **V-LDAP, 8h** | Full control of `OU=Family` and `OU=Service Accounts`, including the ability to grant itself more on those OUs. Can therefore become any bind account. **Cannot** touch `CN=Users`, so it cannot reach `rich`, `roy`, `Administrator` or any privileged group. Net effect: directory read plus control of family and service identities. |
| `svc-ansible-win` | conventional | Ansible WinRM: `dc1`, `rhonda`, `dns01`, `janice` | `BUILTIN\Administrators` on the three DCs via a GPO Restricted Group scoped to `OU=Domain Controllers`; local `Administrators` on `janice`. `userWorkstations = DC1,RHONDA,DNS01,JANICE`. | **V-LDAP, 8h** | **Complete compromise of the domain.** Administrator on a domain controller is Domain Admin equivalent — it can read `NTDS.dit`, inject a DLL into the DNS service, or edit any GPO. This is not reducible; see §4.3. |
| `ldap` | **retire** | everything above | — | — | Currently: complete compromise of the domain, plus the Vault-stored credential, plus every consumer above simultaneously. |

### Existing accounts in scope for least-privilege treatment

| Identity | Action | Why | Blast radius before → after |
|---|---|---|---|
| `svc-syno-repl` | **Done 2026-07-29, and the consumer has since been migrated off this account — see the correction below.** Removed `GG-Synology-Snapshot-Replication` from `Domain Admins`; removed from `Backup Operators`. Still to do: move to `OU=Automation`, remove `DONT_EXPIRE_PASSWORD`, rotate via V-LDAP. | ~~Snapshot replication between two Synology NAS does not require any AD privilege beyond authenticating; DSM enforces share ACLs itself.~~ **This was wrong — see below.** | Domain Admin → directory read; no longer used by replication |
| `svc-syno-admin` | Move to `OU=Automation`. Remove `DONT_EXPIRE_PASSWORD`. Rotate via V-LDAP. Already `adminCount=0`. | Already close to correct. | DSM administrator → unchanged, but now rotating |
| `netsvc` | **Remove from `DnsAdmins`.** Keep `DnsUpdateProxy` if it is the DHCP dynamic-DNS registration credential — confirm first. Move to `OU=Automation`. Remove `DONT_EXPIRE_PASSWORD`. | `DnsAdmins` is a privilege-escalation path to SYSTEM on a DC. `DnsUpdateProxy` is the correct group for DHCP DNS registration and is not privileged. | SYSTEM on a domain controller → DNS record ownership only |
| `synbackup` | **Verify dead, then delete.** Last logon 2024-01-23. If genuinely needed, remove from `Backup Operators` and give it explicitly delegated backup rights on the specific hosts. | Backup Operators can read `NTDS.dit`; it is Tier-0. An account unused for 918 days should not hold it. | Tier-0 → none |
| `rich`, `roy` | Remove `DONT_EXPIRE_PASSWORD` (roadmap Workstream 1). Consider `Protected Users`. | Human Domain Admins should follow the domain password policy. | — |

#### Correction (2026-08-02): Snapshot Replication DOES require privilege

The `svc-syno-repl` row above originally justified de-privileging with
"Snapshot replication between two Synology NAS does not require any AD
privilege beyond authenticating; DSM enforces share ACLs itself."

**That is false, and acting on it caused a three-day DR outage.** The
de-privileging ran at 2026-07-29 23:59. Ninety seconds later, at the
2026-07-30 00:00 scheduled run, 14 replication plans began failing with
`no remote permission` and did not recover until 2026-08-02. Successful
syncs on `scooter` fell from 16/day to 2/day. Nothing alerted, because the
exporter read `plan_status` — a stored field that stays `normal` through a
failed run.

Evidence that it is privilege and not authentication:

- DSM returns **402 "denied permission"** for the account, not 400 "no such
  account or incorrect password". The credential is valid; the account is
  refused.
- The account was still in DSM's local `administrators` group and still in
  its domain user cache throughout, so this is not identity resolution.
- Re-adding `Backup Operators` alone was tested and did **not** restore
  replication, so the load-bearing grant was the `Domain Admins` nesting.

DSM's Snapshot Replication *partner API* (port 5010) requires
administrative privilege to establish the connection. Share ACLs govern
what is replicated, not whether the partner connection may be made at all.

**The de-privileging was still the right call.** The error was assuming it
had no consumer. The consumer has since been moved off AD entirely:
replication plans now authenticate as a *local* DSM account, which is not
subject to AD group evaluation — see
`ansible/synology/repoint_snapshot_replication_credentials.yml`. Both NAS
now hold zero DSM credentials referencing `svc-syno-repl`, and the Vault
secret `secret/synology/snapshot-replication/ad-service-account` is
annotated `status=superseded`.

Note also that Synology's own documentation never states that domain
accounts are supported for the replication pairing — every reference says
only "administrator account". A local DSM account is the safer default.

**Generalisable lesson for the rest of this runbook: verify the consumer,
do not reason about it.** Every "does not require privilege" claim in the
tables above is a hypothesis until someone has watched the consumer work
without it.

### 4.1 The Keycloak decision

Keycloak's federation is configured `editMode: WRITABLE`
(`apps/base/keycloak/realm-configmap.yaml:269`). That is the reason
`keycloak-ldap` holds `Reset Password` over `OU=Family`.

Once Authelia is retired and Keycloak is the sole IdP, this account carries more
weight than any other in the estate — every login depends on it. Its blast
radius should therefore be as small as it can possibly be.

**Determine whether `WRITABLE` is actually required.** It is required only if
users change their AD password *through Keycloak*. If passwords are set in AD
(or Keycloak is used only for authentication and federated read), set
`editMode: READ_ONLY` and drop the password-reset delegation entirely. That
turns the most critical account in the estate into a pure read-only bind, which
is a materially better outcome than any amount of rotation on a write-capable
one.

The decision belongs with the
[Authelia-to-Keycloak migration](../../../homelab_flux/docs/runbooks/authelia-to-keycloak-migration.md),
which is being planned separately. This runbook's requirement is only that the
answer is recorded and the ACEs match it.

Also note the asymmetry already present: Keycloak's `usersDn` is the domain root
while its group mapper's `groups.dn` is `cn=Users`. Narrow `usersDn` to match
the granted scope once §5 is complete.

### 4.2 The AD CS account

`svc-tf-adcs` is the account whose leak would hurt most per unit of privilege,
because `terraform/kubernetes/vault_pki_secret_backend/main.tf:27` uses it to
issue the **Vault PKI intermediate CA certificate**. Anyone holding it can mint
a CA the estate trusts.

Three options, best first:

1. **Retire the consumer.** The estate already runs `pki/`, `pki_int/`,
   `pki_int_prod/` and `pki_int_staging/` in Vault and issues 720h certificates
   renewed automatically. Rooting the Vault intermediate at Vault's own root
   rather than at AD CS removes this account entirely. This is the right answer
   and should be considered on its own merits.
2. **Require manager approval** on the subordinate-CA template, so issuance
   needs a human. Terraform apply then blocks, which is acceptable for a CA
   certificate that is issued once every few years.
3. **Accept it** with 8h rotation and Vault audit logging on the read path.

Do not grant this account `Enroll` on any template beyond the two it uses, and
do not put it in `Cert Publishers`.

### 4.3 The WinRM automation identity — honest limits

This is the one place where least privilege cannot be achieved, and it is worth
being precise about why rather than dressing it up.

`configure_windows_rdp_certificates.yml` creates and links Group Policy Objects
at the domain and `OU=Domain Controllers` level. `configure_windows_dhcp_ha.yml`
runs `Add-DhcpServerInDC`. `fix_dc_time_drift_windows.yml` writes to `HKLM` on a
domain controller. `configure_windows_domain_server_maintenance.yml` registers a
scheduled task running as `SYSTEM` on a domain controller.

**Any identity that can do those things on a domain controller is Domain Admin
equivalent.** There is no local Administrators group on a DC that is separate
from `BUILTIN\Administrators`; membership *is* domain-wide control. Writing
`HKLM` on a DC, or registering a SYSTEM task on a DC, is game over regardless of
what group the account is nominally in.

**What was considered and rejected:**

- **gMSA** — impossible. gMSA passwords are retrieved by a domain-joined
  *Windows* host using its own Kerberos identity. The Ansible control host is
  macOS/Linux and `pywinrm` needs a password. See §6.
- **JEA (Just Enough Administration)** — the correct pattern in principle, but
  these playbooks use `ansible.windows.win_powershell` to run *arbitrary*
  PowerShell. A JEA endpoint constrains the language mode and the visible
  cmdlets; arbitrary script execution is precisely what it forbids. Adopting JEA
  means rewriting every Windows playbook against a fixed set of role
  capabilities. That is a genuine project, not a step in this one. **Recommend
  it as follow-on work, do not claim it here.**
- **Constrained delegation** — solves a different problem (a service acting on
  behalf of a user). It does not reduce what the automation account itself can
  do.
- **`Protected Users`** — **would break Ansible immediately.** The inventory
  uses `ansible_winrm_transport=ntlm`, and `Protected Users` disables NTLM for
  its members. It cannot be applied until the transport moves to Kerberos over
  HTTPS/5986.

**What is genuinely achievable, and should be done:**

| Control | Effect |
|---|---|
| Separate `svc-ansible-win` from `svc-ansible-ad` | Directory object provisioning no longer needs host administration, and vice versa. The AD account is *not* Tier-0. |
| `userWorkstations = DC1,RHONDA,DNS01,JANICE` | Enforced for the NTLM and Kerberos logons WinRM performs. A stolen credential cannot be used against any other host. **Note: `userWorkstations` is not reliably enforced for LDAP simple binds, which is why the bind tier does not set it.** |
| `GG-Deny-Interactive-Logon` + GPO | No console logon, no RDP, no batch. Network logon only. |
| 8h Vault-managed rotation | A captured credential is useful for at most 8 hours. |
| Vault audit logging on the read path | Every retrieval of the credential is attributable. Currently `sys/audit` returns 403 and audit logging cannot even be confirmed — roadmap Workstream 2, task 4. |
| Move WinRM to Kerberos over HTTPS/5986 | Removes NTLM (relay-able), and **unblocks putting the account in `Protected Users`**, which then prevents credential delegation and caching. This is the prerequisite that makes the remaining hardening possible. |

**Statement of residual risk, to be recorded and accepted:** `svc-ansible-win`
is a Tier-0 identity. If it leaks, the domain is compromised. The design reduces
the *window* (8 hours) and the *reachable hosts* (four), not the *consequence*.

**Vault becomes Tier-0 in this design.** Vault holds and can reset the password
of a Tier-0 account. That is already true today — Vault holds the Domain Admin
password — but it should be stated explicitly rather than inherited silently.
The alternative is to exclude `svc-ansible-win` from Vault-managed rotation and
rotate it manually each quarter, which trades an 8-hour exposure window for
removing Vault from the Tier-0 boundary. **Recommendation: keep it in Vault.** A
credential that rotates every 8 hours under audit is safer than one that rotates
every 90 days when someone remembers, and Vault is already in the boundary.

---

## 5. Access control — what actually works

### The uncomfortable measurement

The instinct is to grant each bind account narrow `Read Property` ACEs on a
narrow OU. That instinct is right, but on its own **it changes nothing**, and it
is important to say so before writing 300 ACEs that achieve zero.

Binding as `keycloak-ldap` — an account with **no group memberships at all** and
`adminCount=0` — the following all succeed today:

```
# enumerate every user in the domain
ldapsearch -H ldaps://192.168.1.245:636 -D keycloak-ldap@myrobertson.net -W \
  -b "DC=myrobertson,DC=net" "(&(objectCategory=person)(objectClass=user))" dn
# -> 16 results

# read Domain Admins membership
ldapsearch ... -b "CN=Domain Admins,CN=Users,DC=myrobertson,DC=net" -s base member
# -> 5 members, including CN=ldap

# list the domain controllers, and read the Configuration naming context
ldapsearch ... -b "OU=Domain Controllers,DC=myrobertson,DC=net" "(objectClass=computer)" dn
ldapsearch ... -b "CN=Configuration,DC=myrobertson,DC=net" -s base dn
```

This is default Active Directory behaviour: `Authenticated Users` (S-1-5-11) is
a member of `Pre-Windows 2000 Compatible Access`, which grants read across user
and group objects domain-wide. Confirmed on this domain:

```
CN=Pre-Windows 2000 Compatible Access,CN=Builtin,DC=myrobertson,DC=net
    <- CN=S-1-5-11,CN=ForeignSecurityPrincipals,DC=myrobertson,DC=net
    <- CN=DC1,OU=Domain Controllers,DC=myrobertson,DC=net
```

**Adding ALLOW read ACEs to a bind account is therefore decorative unless the
inherited read is removed.** Grant-then-remove is the only sequence that works.

### The sequence that makes it meaningful

1. Grant explicit read ACEs to `GG-Directory-Bind-Read` on `CN=Users` and
   `OU=Family`, scoped to the attributes each consumer was measured to read.
2. Verify every consumer still authenticates users correctly.
3. **Remove `S-1-5-11` (Authenticated Users) from `Pre-Windows 2000 Compatible
   Access`.** Leave `CN=DC1` in place.
4. Re-verify. Re-bind as a bind account and confirm it can read `CN=Users` and
   `OU=Family` and can **no longer** read `OU=Domain Controllers`,
   `CN=Computers`, `OU=Endpoints`, `CN=System` or `OU=Service Accounts`.

Step 3 is what converts steps 1–2 from decoration into control. It is also the
only step with real breakage risk. Assessed for this estate:

- No Exchange, no RRAS/IAS, no pre-Windows-2000 clients. Domain members are
  Server 2012/2022/2025 and Windows 10 — all unaffected.
- The consumers that *would* break are exactly the ones step 1 has just been
  granted for. That is the point.
- Reversible instantly: re-add S-1-5-11 to the group.

Do not overclaim the result. `Authenticated Users` retains some default read
(notably on the domain head and certain property sets), and `Everyone` retains a
small amount. The measurable effect is loss of general read on user, group and
computer objects, which is the bulk of what an attacker with a stolen bind
credential would want.

### The attribute set

Measured from the live configuration of every consumer, not assumed:

| Consumer | Attributes it reads |
|---|---|
| Authelia (`activedirectory` defaults) | `sAMAccountName`, `userPrincipalName`, `mail`, `displayName`, `memberOf`, `sAMAccountType`, `userAccountControl`, `objectClass`, `cn`, `distinguishedName` |
| Nextcloud | `sAMAccountName`, `userPrincipalName`, `mail`, `displayName`, `objectClass`, `memberOf`, `cn`, `objectGUID`; on groups `cn`, `objectClass`, `member` |
| Keycloak | `sAMAccountName`, `cn`, `mail`, `givenName`, `sn`, `objectGUID`, `objectClass`, `distinguishedName`, `userAccountControl`, `pwdLastSet`; on groups `cn`, `member`, `objectClass` |
| Vault `ldap/` auth | `sAMAccountName`, `memberOf`, `objectClass` |
| Proxmox VE | `sAMAccountName`, `mail`, `memberOf`, `objectClass`, `cn`; on groups `sAMAccountName`, `member` |
| PBS | as Proxmox VE, plus `givenName`, `sn` |

Union, which is what `GG-Directory-Bind-Read` is granted:

`objectClass`, `cn`, `name`, `distinguishedName`, `sAMAccountName`,
`sAMAccountType`, `userPrincipalName`, `mail`, `displayName`, `givenName`, `sn`,
`memberOf`, `userAccountControl`, `objectGUID`, `objectSid` — and on group
objects additionally `member`.

Note what is **not** in that list: `pwdLastSet`, `lastLogonTimestamp`,
`servicePrincipalName`, `msDS-KeyCredentialLink`, `userWorkstations`,
`nTSecurityDescriptor`, `unicodePwd`, `ntPwdHistory`.

### A note on the delegation group

The instruction was "explicitly delegated ACEs instead of group membership".
That rule is about *privileged* groups, and `GG-Directory-Bind-Read` is not one:
it confers nothing except the ACEs deliberately placed on it. Expressing the
grant once against a group rather than 15 attributes × 2 containers × 7 accounts
= **210 ACEs** is the difference between a delegation that can be audited and
one that cannot. Per-account ACEs are supported by the playbook if preferred —
set `sai_read_group` per account — but the group is the maintainable form and is
the recommendation.

### The `dsacls` invocations

The playbook applies these through `System.DirectoryServices.ActiveDirectoryAccessRule`,
which is what the existing `provision_domain_accounts.yml` uses. The equivalent
`dsacls` commands, for manual application and for verification:

```powershell
# --- Read delegation for the bind tier -------------------------------------
# For each container: CN=Users,DC=myrobertson,DC=net and OU=Family,DC=myrobertson,DC=net
$scopes = @('CN=Users,DC=myrobertson,DC=net', 'OU=Family,DC=myrobertson,DC=net')
$userAttrs = 'objectClass','cn','name','distinguishedName','sAMAccountName',
             'sAMAccountType','userPrincipalName','mail','displayName',
             'givenName','sn','memberOf','userAccountControl','objectGUID','objectSid'
$groupAttrs = 'objectClass','cn','name','distinguishedName','sAMAccountName',
              'member','memberOf','objectGUID','objectSid'

foreach ($scope in $scopes) {
  # List Contents on the container itself, so the tier can enumerate
  dsacls "$scope" /I:T /G "MYROBERTSON\GG-Directory-Bind-Read:LC"

  foreach ($a in $userAttrs) {
    dsacls "$scope" /I:S /G "MYROBERTSON\GG-Directory-Bind-Read:RP;$a;user"
  }
  foreach ($a in $groupAttrs) {
    dsacls "$scope" /I:S /G "MYROBERTSON\GG-Directory-Bind-Read:RP;$a;group"
  }
}

# --- Vault LDAP secrets engine manager, scoped to the bind OU only ---------
dsacls "OU=Bind,OU=Service Accounts,DC=myrobertson,DC=net" /I:S `
  /G "MYROBERTSON\svc-vault-ldapmgr:CA;Reset Password;user"
dsacls "OU=Bind,OU=Service Accounts,DC=myrobertson,DC=net" /I:S `
  /G "MYROBERTSON\svc-vault-ldapmgr:WP;pwdLastSet;user"

# --- Keycloak write-back, ONLY if editMode stays WRITABLE (see 4.1) --------
dsacls "OU=Family,DC=myrobertson,DC=net" /I:S `
  /G "MYROBERTSON\keycloak-ldap:CA;Reset Password;user"
dsacls "OU=Family,DC=myrobertson,DC=net" /I:S `
  /G "MYROBERTSON\keycloak-ldap:WP;pwdLastSet;user"

# --- Terraform DNS: the zone object, NOT DnsAdmins -------------------------
$zone = 'DC=myrobertson.net,CN=MicrosoftDNS,DC=DomainDnsZones,DC=myrobertson,DC=net'
dsacls "$zone" /I:T /G "MYROBERTSON\svc-tf-dnsupdate:CCDC;dnsNode"
dsacls "$zone" /I:S /G "MYROBERTSON\svc-tf-dnsupdate:WP;dnsRecord;dnsNode"
dsacls "$zone" /I:S /G "MYROBERTSON\svc-tf-dnsupdate:RP;dnsRecord;dnsNode"

# --- Terraform AD CS: Enroll on the two templates, nothing else ------------
$tplBase = 'CN=Certificate Templates,CN=Public Key Services,CN=Services,CN=Configuration,DC=myrobertson,DC=net'
foreach ($tpl in @('<TalosCaTemplate>', '<VaultIntermediateTemplate>')) {
  dsacls "CN=$tpl,$tplBase" /G "MYROBERTSON\svc-tf-adcs:CA;Enroll"
  dsacls "CN=$tpl,$tplBase" /G "MYROBERTSON\svc-tf-adcs:GR"
}
# Request Certificates on the CA itself is set through the Certification
# Authority MMC (Security tab) or certutil -setcasecurity. There is no dsacls
# equivalent; this is the one step that cannot be codified. See the manual
# procedure in the appendix.

# --- Ansible AD object provisioning ----------------------------------------
foreach ($ou in @('OU=Family,DC=myrobertson,DC=net',
                  'OU=Service Accounts,DC=myrobertson,DC=net')) {
  dsacls "$ou" /I:T /G "MYROBERTSON\svc-ansible-ad:CCDC;user"
  dsacls "$ou" /I:T /G "MYROBERTSON\svc-ansible-ad:CCDC;group"
  dsacls "$ou" /I:T /G "MYROBERTSON\svc-ansible-ad:CCDC;organizationalUnit"
  dsacls "$ou" /I:S /G "MYROBERTSON\svc-ansible-ad:GRGW;;user"
  dsacls "$ou" /I:S /G "MYROBERTSON\svc-ansible-ad:GRGW;;group"
  dsacls "$ou" /I:T /G "MYROBERTSON\svc-ansible-ad:WD"   # needed for Set-Acl
}
```

Verify any of these with `dsacls "<dn>"` and read the resulting ACE list, or
non-destructively from the Ansible control host:

```sh
ldapsearch -LLL -x -H ldaps://192.168.1.3:636 -D "$BIND" -W \
  -b "OU=Bind,OU=Service Accounts,DC=myrobertson,DC=net" -s base nTSecurityDescriptor
```

### Logon restrictions

`GG-Deny-Interactive-Logon` is populated by the playbook. The GPO that
references it is a one-time manual creation (Group Policy user-rights assignment
is not expressible through `ansible.windows` without `secedit` templating, and
the existing repo convention is to reference a group from a GPO rather than
manage rights directly):

- GPO `Deny Service Account Interactive Logon`, linked at the domain root.
- Computer Configuration → Policies → Windows Settings → Security Settings →
  Local Policies → User Rights Assignment:
  - **Deny log on locally** → `MYROBERTSON\GG-Deny-Interactive-Logon`
  - **Deny log on through Remote Desktop Services** → same
  - **Deny log on as a batch job** → same

Do **not** add *Deny access to this computer from the network* — that is the
right the bind accounts and `svc-ansible-win` actually use.

`userWorkstations` is set on `svc-ansible-win` only. It is deliberately **not**
set on the bind tier: an LDAP simple bind does not supply a NetBIOS workstation
name, so the restriction is not reliably enforced and would give false
assurance.

### `Protected Users`

| Candidate | Verdict | Reason |
|---|---|---|
| `rich`, `roy` | **Yes, recommended** | Human Domain Admins. Protected Users blocks NTLM, DES/RC4, delegation and credential caching. They authenticate interactively with Kerberos; nothing in the estate requires NTLM for them. Test RDP to `janice` and Proxmox/PBS realm login first. |
| `Administrator` | **Yes, after break-glass is proven** | Same benefits. Confirm the DSRM/break-glass path does not depend on NTLM before adding. |
| `svc-ansible-win` | **No, not yet** | `ansible_winrm_transport=ntlm`. Protected Users would break every Windows playbook on the next run. Becomes possible once WinRM moves to Kerberos over HTTPS/5986 — track that as the prerequisite. |
| All bind accounts | **No** | They perform LDAP *simple* binds. Protected Users does not block simple bind, so it adds no protection, but it does block the NTLM fallback some clients use during failover. No benefit, non-zero risk. |
| `svc-tf-adcs` | **No** | `use_ntlm = true` in `providers.tf:63`. |
| `svc-tf-dnsupdate` | **Possible** | Uses GSSAPI/Kerberos. Test, then add. |

---

## 6. gMSA assessment — the honest answer

The requirement was to use managed service accounts wherever the consumer
genuinely supports them. Assessed rigorously, **no consumer in this estate can
use one today.**

| Identity | gMSA? | Reason |
|---|---|---|
| `svc-vault-ldap`, `svc-nextcloud-ldap*`, `svc-pve-ldap`, `svc-pbs-ldap`, `svc-authelia-ldap`, `keycloak-ldap` | **No** | All perform an LDAP **simple bind** from a Linux or Kubernetes workload. A gMSA password is retrieved by a *domain-joined Windows host* using its own machine Kerberos identity via `msDS-ManagedPassword`; there is no password to put in a config file. Keycloak's own config makes this explicit: `authType: simple`, `allowKerberosAuthentication: false`. Authelia, Nextcloud's `user_ldap`, Proxmox VE and PBS all take a bind password string and have no gMSA code path. |
| `svc-ansible-win` | **No** | Ansible control host is macOS/Linux and `pywinrm` NTLM requires a password. Not domain-joined, so it cannot retrieve a managed password. |
| `svc-ansible-ad`, `svc-tf-adcs`, `svc-tf-dnsupdate` | **No** | Same — Terraform and Ansible run from non-domain-joined Linux/macOS. |
| `svc-syno-admin`, `svc-syno-repl` | **No** | Consumed by Synology DSM, a Linux appliance. DSM's directory integration takes a bind password; it has no gMSA support. |
| `synbackup` | **N/A** | Recommend deletion — unused for 918 days. |
| `netsvc` | **No** | If it is the DHCP dynamic-DNS registration credential, Microsoft's DHCP server requires a standard user account for `netsh dhcp server set dnscredentials`; gMSA is not supported there. |
| Windows maintenance scheduled task | **No** | Already runs as `SYSTEM` (`configure_windows_domain_server_maintenance.yml:104-105`), which needs no account at all. Using a gMSA here would *increase* the attack surface. |

**Conclusion: create the KDS root key anyway, but create no gMSAs in this
workstream.** The root key is a cheap, one-time prerequisite with a long lead
time; having it in place means the first genuine Windows-side consumer is not
blocked for half a day. Forcing the pattern onto any identity above would
require rewriting the consumer, and in the scheduled-task case would make things
worse.

### KDS root key bootstrap (prerequisite only)

```powershell
# On dc1, as a Domain Admin. Run once, forest-wide.
Add-KdsRootKey -EffectiveImmediately
```

`-EffectiveImmediately` still requires a **10-hour wait** before the key is
usable, because AD assumes the key must replicate to every DC and allows for
replication convergence plus clock skew.

Back-dating shortens it:

```powershell
Add-KdsRootKey -EffectiveTime ((Get-Date).AddHours(-10))
```

**What back-dating costs on this forest.** There is one writable DC pair (`dc1`,
`rhonda`) plus an RODC. Back-dating asserts that the key has already replicated
everywhere when it has not. If a gMSA password is requested from `rhonda` before
the key reaches it, the request fails until replication completes — an error, not
a corruption. With two writable DCs on the same LAN, replication is a matter of
seconds to minutes. The risk is low but not zero, and it is a genuine deviation
from the documented procedure.

**Recommendation: run `Add-KdsRootKey -EffectiveImmediately` and wait the 10
hours.** Nothing in this workstream depends on it, so there is no reason to
accept even a small correctness risk to save half a day.

---

## 7. Rotation

### The conflict, resolved

Two requirements pull against each other: *use gMSA where possible*, and *rotate
every 8 hours*. They cannot both be satisfied for the same identity, because
gMSA's `ManagedPasswordIntervalInDays` is expressed in **whole days with a
minimum of 1**. **A gMSA cannot rotate every 8 hours.**

That conflict is moot here, because §6 established that no consumer can use a
gMSA at all. Every identity therefore takes the Vault path. But the general
principle should be recorded for future accounts:

> Least privilege plus a 1-day gMSA rotation beats an 8-hour rotation on an
> over-privileged account. Cadence is the weaker of the two controls. Choose
> gMSA whenever a Windows-side consumer supports it, and reserve the 8-hour
> Vault static role for identities that cannot use one.

### The mechanism

**Enable the Vault LDAP secrets engine.** This estate has an `ldap/` *auth*
method; the LDAP *secrets* engine is a different thing and is **not currently
mounted** (`vault secrets list` shows `aws`, `cubbyhole`, `identity`, `pki`,
`pki_int`, `pki_int_prod`, `pki_int_staging`, `postgres`, `prod-kubernetes`,
`secret`, `staging-kubernetes`, `sys`, `transit`). Vault is 1.20.4, so static
roles, dynamic roles and service-account check-out are all available.

```sh
export VAULT_ADDR=https://vault.myrobertson.net:8200

vault secrets enable -path=ldap ldap

vault write ldap/config \
  binddn='CN=svc-vault-ldapmgr,OU=Automation,OU=Service Accounts,DC=myrobertson,DC=net' \
  bindpass=- \
  url='ldaps://dc1.myrobertson.net,ldaps://rhonda.myrobertson.net' \
  userdn='OU=Bind,OU=Service Accounts,DC=myrobertson,DC=net' \
  schema=ad \
  password_policy=ad-strong \
  request_timeout=90

# Vault takes ownership of its own manager credential. After this nobody,
# including the operator, knows the value.
vault write -f ldap/rotate-root
```

Note `ldaps://` — do not repeat the auth method's cleartext mistake. Note also
that both DCs are listed and neither is the dead `192.168.1.244` or the
non-existent `rizzo`.

A matching `ldap-automation/` mount, with `svc-vault-ldapmgr-auto` as its binddn
and `userdn=OU=Automation,...`, isolates the Tier-0 rotation path from the bind
tier. Restrict read on `ldap-automation/static-cred/*` by Vault policy to the
Ansible AppRole alone.

Static role per identity:

```sh
vault write ldap/static-role/nextcloud-ldap \
  dn='CN=svc-nextcloud-ldap,OU=Bind,OU=Service Accounts,DC=myrobertson,DC=net' \
  username='svc-nextcloud-ldap' \
  rotation_period=8h

vault read ldap/static-cred/nextcloud-ldap
# -> username, dn, password, last_password, ttl, last_vault_rotation
```

**`last_password` is the property that makes short rotation survivable.** Vault
returns the previous password alongside the current one, so a consumer that
reads the credential a moment before a rotation is not instantly broken. It only
helps consumers that read from Vault at connect time — it does nothing for a
consumer holding a value injected at boot.

**Dynamic roles are wrong for every consumer here.** They create and destroy an
ephemeral AD account per lease, so the *bind DN changes on every lease*. Every
consumer in §1 stores a fixed bind DN in its configuration. Do not use them.

**Service-account check-out (libraries)** is a poor fit for the automation
identities — check-out is designed for a pool of interchangeable shared accounts
with human check-out/check-in semantics, and Ansible runs unattended. It *is* a
good fit for a future **break-glass Domain Admin**: a one-account library with
`disable_check_in_enforcement=false` and a short `max_ttl` gives an
auto-rotating emergency credential with an audit trail. Recommended as follow-on
work, not part of this cutover.

### Blocking dependency: VSO

**Vault Secrets Operator is broken.** Measured today:

| Cluster | VaultStaticSecrets | Syncing | Failing |
|---|---|---|---|
| prod | 74 | 1 | **73** |
| staging | 25 | 0 | **25** |

Failure message: `Failed to sync the secret, horizon=<N>s, err=context deadline
exceeded`. `VaultConnection` and `VaultAuth` both report `valid=true`. Another
agent is root-causing this; it is not re-investigated here.

Two consequences that this design must state plainly:

1. **Vault → Kubernetes propagation is dead.** Any rotation of a credential
   consumed by Authelia or Nextcloud will change Vault and not the cluster.
2. **Every failing object still reports `Healthy=True` and `Ready=True`.** Only
   `SecretSynced` is false, and the operator logs nothing about the failures.
   Alerting keyed on Ready/Healthy will never fire. This is the same failure
   shape the roadmap identifies in Workstream 2: a control that exists, reports
   fine, and does nothing.

**Maximum safe rotation cadence until VSO is fixed: none, for the Kubernetes
consumers.** Rotating `svc-nextcloud-ldap` or `svc-authelia-ldap` on any cadence
while propagation is broken breaks them at the first rotation. Set
`rotation_period` on those static roles **only after VSO syncs cleanly**, and
verify a manual `vault write -f ldap/rotate-role/<name>` propagates end to end
before enabling the schedule.

The non-Kubernetes identities — `svc-vault-ldap`, `svc-pve-ldap`,
`svc-pbs-ldap`, `svc-tf-*`, `svc-ansible-*` — have **no VSO dependency** and can
rotate on schedule immediately.

### Does the consumer pick up the new value?

This is the question that decides the cadence, and the answer differs per
consumer.

| Consumer | How it reads the credential | Picks up a change without restart? | Consequence for 8h |
|---|---|---|---|
| Vault `ldap/` auth | `auth/ldap/config`, re-read per request | **Yes** | 8h is free. Vault rotates its own bind with `rotation_period` on the auth config — no external driver, no restart, nothing to propagate. |
| Nextcloud | Its own database (`oc_appconfig`), read per request. Env vars only seed it on first boot. | **Yes**, via `occ ldap:set-config s01 ldapAgentPassword` | 8h achievable with a CronJob calling `occ`. **Do not** rely on updating the Secret alone — env vars are immutable for a container's lifetime, so that path requires a pod restart and would silently do nothing until one happened. |
| Proxmox VE | `/etc/pve/priv/realm/myrobertson.pw`, read per request by `pveproxy` | **Yes**, via `PUT /access/domains/myrobertson` with `password=` | 8h achievable, but needs an external driver (scheduled playbook). |
| PBS | `/etc/proxmox-backup/ldap_passwords.json` | **Yes**, via `PUT /access/domains/myrobertson.net` | As above, plus the DR archive must be re-exported. |
| Keycloak | Component config in its own database, live | **Yes**, via `kcadm update components/<id>` | Achievable. Better: point `bindCredential` at Keycloak's `files-plaintext` vault SPI reading a mounted file, so a projected-Secret update propagates with no API call. That path depends on VSO. |
| Terraform | Read at plan/apply | **Yes** — nothing persists | 8h is free. |
| Ansible | Read at playbook start | **Yes** — nothing persists | 8h is free. |
| **Authelia** | File at `/secrets/internal/authentication.ldap.password.txt`, **read once at startup** | **No** | An 8h rotation means restarting the SSO for the entire estate three times a day. No Reloader is installed and `rolloutRestartTargets` is empty on all 99 VaultStaticSecrets. |

On Authelia specifically: `refreshAfter: 60s` plus `rolloutRestartTargets`
pointing at the Deployment *would* work mechanically, but the result is a pod
restart every rotation. **For the estate's SSO that is a self-inflicted
availability problem, and it should be called what it is.** Since Authelia is
being retired in favour of Keycloak, the right answer is not to engineer around
it: give Authelia a dedicated bind account so `ldap@` can be retired on
schedule, and leave that account on a manual/90-day cadence for the short
remainder of Authelia's life.

### Recommended cadence per identity

Not a uniform number. What I would actually run:

| Identity | Recommended | Why |
|---|---|---|
| `svc-vault-ldap` | **8h** | Native Vault self-rotation, no driver, no restart, no propagation. Free. |
| `svc-vault-ldapmgr` | **8h** | `ldap/rotate-root`, internal to Vault. Free. |
| `svc-ansible-win` | **8h** | Tier-0. Read from Vault per run, nothing persists. The shortest window matters most here and costs nothing. |
| `svc-ansible-ad` | **8h** | Same mechanism, no cost. |
| `svc-tf-adcs` | **8h** | High blast radius (§4.2), read per apply, no cost. |
| `svc-tf-dnsupdate` | **8h** | Same. |
| `svc-nextcloud-ldap` / `-stg` | **24h**, after VSO is fixed | Restart-free via `occ`, but the rotation depends on a CronJob succeeding. A failed rotation locks Nextcloud out of the directory. 24h gives a full day of alert-and-repair headroom for a directory-read-only credential. 8h is technically achievable — take it once the CronJob has a month of clean runs. |
| `svc-pve-ldap` | **24h** | Restart-free but driver-dependent. Proxmox is the management plane; a broken realm bind locks admins out of the UI (though `root@pam` remains). |
| `svc-pbs-ldap` | **24h** | Same, plus the DR archive re-export. |
| `keycloak-ldap` | **24h** | Post-migration this is the single most important credential in the estate. Restart-free rotation exists, but a failed rotation is a total login outage. 24h with alerting beats 8h with three times the chance of a bad night. **Least privilege on this account (§4.1) buys far more than cadence does.** |
| `svc-authelia-ldap` | **manual / 90d** | Requires an SSO restart. Interim account on a retiring service. Do not engineer a rotation for something being deleted. |
| `svc-syno-admin`, `svc-syno-repl` | **7d** | Appliance-side config change; DSM has no API-driven credential update that is safe to run unattended three times a day. |

**Making rotation actually happen** rather than being an aspiration is roadmap
Workstream 2: export `pwdLastSet` age per account and Vault `rotated_at` as
metrics, alert past the interval, and — critically — `absent()`-guard the
exporter so a dead exporter does not read as "everything current". Without that,
this section is documentation.

---

## 8. Cutover sequence

### The key property

**Both accounts can be valid concurrently.** `ldap@` and each `svc-*` account
are independent AD principals; creating and using the new ones does not affect
the old one in any way. Nothing in AD, and nothing in any consumer, requires
them to be exclusive.

This is confirmed rather than assumed: each consumer holds exactly one bind DN
in its own configuration, and changing it is a per-consumer operation. There is
no shared registry, no session state tied to the bind identity, and no
consumer-to-consumer dependency on which account is in use. **Consumers can
therefore be migrated one at a time, in any order, with no coordination between
them.**

That is the whole basis of a zero-downtime cutover, and it means the risky
step — retiring `ldap@` — happens only after every consumer has independently
proven it no longer needs it.

### Interleaving with the Authelia retirement

Two projects touch this credential. The correct interleaving:

- **They are independent, and must be kept independent.** Do not gate the
  retirement of a five-year-old Domain Admin on the completion of an SSO
  migration whose timeline is not yet fixed.
- Give Authelia its own `svc-authelia-ldap` in Phase 2 like every other
  consumer. It costs one account and perhaps ten minutes. `ldap@` can then be
  retired on schedule regardless of when Authelia dies, and `svc-authelia-ldap`
  is deleted with Authelia.
- The **only** ordering constraint in the other direction: `svc-authelia-ldap`
  must not be deleted before Authelia is gone.
- Nextcloud is the genuine uncertainty. If it moves to OIDC-only against
  Keycloak, `svc-nextcloud-ldap` may become unnecessary — but Nextcloud
  frequently retains an LDAP backend for the *user directory* (group
  membership, quotas, provisioning) even when *authentication* moves to OIDC.
  **Do not assume the migration removes it.** Create the account, cut over to
  it, and delete it later if it proves redundant. That ordering is safe in both
  outcomes.

See [Authelia to Keycloak migration](../../../homelab_flux/docs/runbooks/authelia-to-keycloak-migration.md)
for the SSO side.

### Phases

Steps marked **[M]** mutate the estate. Everything before Phase 1 is read-only.

#### Phase 0 — prerequisites (no user impact)

| # | Action | Verify |
|---|---|---|
| 0.1 | Fix VSO. Blocking for Phases 4 and 5 only. | `kubectl get vaultstaticsecrets -A` shows `SecretSynced=True` on all objects in both clusters |
| 0.2 | **[M]** Enable Vault audit logging (roadmap W2.4) | `vault audit list` returns a device |
| 0.3 | **[M]** `Add-KdsRootKey -EffectiveImmediately` on `dc1`, then wait 10h | `Get-KdsRootKey` returns a key |
| 0.4 | **[M]** Enable the LDAP secrets engine and both mounts (§7) | `vault read ldap/config` returns the manager DN |
| 0.5 | **[M]** Generate a password for each new account into Vault under `secret/windows/domain/service-accounts/<name>` | `vault kv list secret/windows/domain/service-accounts/` |
| 0.6 | **[M]** Fix `auth/ldap/config` `url` to `ldaps://dc1...,ldaps://rhonda...` — removes the cleartext bind and the dead `rizzo` in one change | `vault read auth/ldap/config`; then log in with an AD account |
| 0.7 | **[M]** Fix Proxmox VE realm `server1` from the dead `192.168.1.244` to `192.168.1.3` | `GET /access/domains/myrobertson` |

Rollback for 0.6/0.7: restore the previous field value. Both are single-field
changes with no dependency.

#### Phase 1 — create the identities **[M]**

```sh
cd homelab_ansible
source .venv/bin/activate
export VAULT_ADDR=https://vault.myrobertson.net:8200

ansible-playbook -i inventory/environments/production.ini \
  ansible/domain/provision_service_account_identities.yml --check --diff

ansible-playbook -i inventory/environments/production.ini \
  ansible/domain/provision_service_account_identities.yml
```

Verify — the playbook's own `verify` tag asserts the least-privilege invariants
and fails the run if any is violated:

```sh
ansible-playbook -i inventory/environments/production.ini \
  ansible/domain/provision_service_account_identities.yml --tags verify
```

Independently, from the control host:

```sh
ldapsearch -LLL -x -o ldif-wrap=no -H ldaps://192.168.1.3:636 -D "$BIND" -W \
  -b "OU=Service Accounts,DC=myrobertson,DC=net" "(objectClass=user)" \
  sAMAccountName adminCount userAccountControl memberOf servicePrincipalName
```

Expect `adminCount` absent or 0, `userAccountControl` = 512 (no
`DONT_EXPIRE_PASSWORD`), `memberOf` limited to the two delegation groups, and no
`servicePrincipalName` on any account.

**User impact: none.** No existing consumer has changed.

**Rollback:** disable and delete the new accounts. `ldap@` is untouched.

#### Phase 2 — register the accounts with Vault **[M]**

Create a static role per identity (§7), but **leave `rotation_period` unset for
now**. Confirm Vault can read and rotate each one on demand before putting it on
a schedule:

```sh
vault write ldap/static-role/nextcloud-ldap \
  dn='CN=svc-nextcloud-ldap,OU=Bind,OU=Service Accounts,DC=myrobertson,DC=net' \
  username='svc-nextcloud-ldap'

vault read ldap/static-cred/nextcloud-ldap        # returns the current password
vault write -f ldap/rotate-role/nextcloud-ldap    # forces one rotation
```

Verify each account can still bind after Vault takes ownership:

```sh
ldapwhoami -x -H ldaps://192.168.1.245:636 -D "svc-nextcloud-ldap@myrobertson.net" -W
```

**User impact: none.**

**Rollback:** delete the static role. The account keeps whatever password Vault
last set; reset it manually from `dc1` if needed.

#### Phase 3 — migrate the consumers with no VSO dependency, one at a time **[M]**

Order chosen so that the least critical goes first and each step is independently
reversible.

| Step | Consumer | Change | Verify | Rollback | Downtime |
|---|---|---|---|---|---|
| 3.1 | **PBS** | `PUT /access/domains/myrobertson.net` `bind-dn=svc-pbs-ldap@myrobertson.net`, `password=<from Vault>`. Also change `mode ldap` → `mode ldaps` and fix `server2`. Then **re-run the config export playbook** so the DR archive no longer holds the old credential. | Log into the PBS UI as `rich@myrobertson.net`. `vault kv get -field=exported_at secret/proxmox/pbs/prod/config` shows today. | Restore the previous bind-dn and password. `root@pam` always works. | **None.** Existing PBS sessions are unaffected; only new logins use the realm. |
| 3.2 | **Proxmox VE** | `PUT /access/domains/myrobertson` `bind_dn=svc-pve-ldap@myrobertson.net`, `password=<from Vault>` | Log into the PVE UI as `rich@myrobertson`. `pvesh get /access/domains/myrobertson` | Restore previous values. `root@pam` and both OIDC realms remain available. | **None.** |
| 3.3 | **Vault `ldap/` auth** | `vault write auth/ldap/config binddn='CN=svc-vault-ldap,OU=Bind,OU=Service Accounts,DC=myrobertson,DC=net' bindpass=<...>` (URL already fixed in 0.6) | `vault login -method=ldap username=rich` in a second shell **before closing the first**. Confirm the resulting token has the expected policies. | Restore the previous `binddn`/`bindpass`. Keep a valid root or admin token in a separate shell throughout. | **None**, provided an existing token is held. |
| 3.4 | **Terraform** | Point `data.vault_generic_secret.windows_domain_admin` at `secret/windows/domain/service-accounts/svc-tf-dnsupdate` and add a second data source for `svc-tf-adcs`; update `providers.tf:8-9` and `:61-62` to use them separately. Rename the data source — `windows_domain_admin` is now actively misleading. | `terraform plan` shows no diff | Revert the commit | **None** — nothing is running. |
| 3.5 | **Ansible WinRM** | `inventory/environments/production.ini:53` → `ansible_user=svc-ansible-win@myrobertson.net`. Grant `BUILTIN\Administrators` on the DCs via the GPO restricted group first. | `ansible -i inventory/environments/production.ini rdp_servers_windows -m ansible.windows.win_ping` | Revert the line | **None** — nothing is running. |

After 3.5, `ldap@` still has three consumers, all in Kubernetes.

#### Phase 4 — migrate Keycloak **[M]** *(VSO-dependent)*

Keycloak does not use `ldap@`, so this is not on the critical path for
retirement. It is here because the Keycloak bind becomes the most important
credential in the estate and should be brought under management at the same
time.

1. Decide `editMode` (§4.1) and apply the matching ACEs.
2. Register `keycloak-ldap` as a Vault static role and rotate once.
3. Update `secret/keycloak/prod` and `secret/keycloak/stage`
   `ldap-bind-password`; let VSO sync `keycloak-secret`.
4. Run the `keycloak-domain-admins-rbac` job to push the new `bindCredential`.
5. Verify: log into Keycloak, trigger a manual user-federation sync, confirm the
   user count is unchanged.

Rollback: restore the previous `bindCredential` through `kcadm` and re-run the
job. **Do this in staging first** — staging and prod currently share the same
`keycloak-ldap` password, so staging is a genuine rehearsal.

#### Phase 5 — migrate the Kubernetes consumers of `ldap@` **[M]** *(VSO-dependent — blocked until 0.1)*

| Step | Consumer | Change | Verify | Rollback | Downtime |
|---|---|---|---|---|---|
| 5.1 | **Nextcloud staging** | Update `secret/nextcloud/staging/ldap` `LDAP_BIND_DN` and `LDAP_BIND_PASSWORD`. Then run `occ ldap:set-config s01 ldapAgentName/ldapAgentPassword` in the pod so the live DB config changes without a restart. | `occ ldap:test-config s01`; log in as a directory user | Set the values back with `occ` | **None** |
| 5.2 | **Nextcloud prod, `default` ns** | As 5.1 against `secret/nextcloud/prod/ldap` | As above | As above | **None** |
| 5.3 | **Nextcloud prod, `nextcloud` ns** | **First create the missing `VaultStaticSecret`** so the orphan Secret has an owner (see §1 item 10). Then as 5.1 against all three replicas. | `occ ldap:test-config s01` in each replica | As above | **None** |
| 5.4 | **Authelia staging** | Update `secret/authelia/stage` `authentication.ldap.password.txt`, wait for VSO to sync, then `kubectl rollout restart deploy/authelia`. **Fix the four missing Secret keys first** (§2 item 11) or the pod will not start. | `kubectl rollout status`; log into a staging protected app | Restore the Vault value and roll back | **Staging SSO: one pod restart, ~15–30s** |
| 5.5 | **Authelia prod** | As 5.4 against `secret/authelia/prod`. Set `maxUnavailable: 0` / `maxSurge: 1` on the Deployment strategy first so the new pod is ready before the old one goes. | Log into a protected app in a private window; confirm an existing session survives | Restore the Vault value and roll back | **See below** |

### Where downtime is genuinely unavoidable

**Everywhere except Authelia, it is not.** Steps 3.1–3.5, 5.1–5.3 and Phase 4
are all zero-downtime, because in each case the consumer re-reads the credential
from a live source and both accounts are valid concurrently. That is engineered
away, not lucky.

**Authelia is the one real exposure, and it is small and bounded.** Authelia
reads its bind password from a file exactly once at startup, so changing it
requires a process restart. Two facts limit the impact:

- **Existing sessions survive.** Authelia session state lives in Postgres and in
  the session cookie, not in the LDAP connection. Users who are already
  authenticated are not logged out.
- **Only in-flight *new* authentications fail**, and only for the seconds
  between the old pod terminating and the new pod passing its readiness probe.

With a single replica and `maxUnavailable: 1` — the current configuration —
that gap is roughly **15–30 seconds during which nobody can complete a new
login**. Everything behind Authelia (Proxmox, PBS, Synology, Mealie, and the
`forward-auth` path generally) rejects new logins for that window.

**It can be reduced to zero.** Set `replicas: 2` with `maxUnavailable: 0` and
`maxSurge: 1` before step 5.5, and the new pod becomes ready before the old one
is removed. Authelia is stateless given a shared Postgres session store, which
this deployment has (`cluster-authelia-ceph`). If two replicas are acceptable
for the short remaining life of Authelia, do that and the cutover is genuinely
zero-downtime end to end. If not, take the 15–30 seconds at a quiet hour.

**Second unavoidable-ish item:** step 0.3's 10-hour KDS wait. It blocks nothing
in this workstream, so it is latency rather than downtime.

---

## 9. Decommission

Do not shorten this. The measured `lastLogonTimestamp` of `ldap` is 2026-07-22,
so it was in use days ago; the whole point of the phased cutover is to reach a
state where that is no longer true and to *prove* it.

### Step 1 — prove it is unused

`lastLogonTimestamp` replicates lazily and is only accurate to within 9–14 days.
It is a good negative signal and a poor positive one. Use three sources.

**(a) `lastLogon` on every DC individually.** Unlike `lastLogonTimestamp`, the
non-replicated `lastLogon` attribute is exact — but it is per-DC, so all three
must be queried and the maximum taken.

```sh
for DC in 192.168.1.3 192.168.1.245 192.168.1.101; do
  echo "== $DC"
  ldapsearch -LLL -x -o ldif-wrap=no -H ldaps://$DC:636 -D "$BIND" -W \
    -b "CN=ldap,CN=Users,DC=myrobertson,DC=net" -s base \
    lastLogon lastLogonTimestamp logonCount pwdLastSet
done
```

**(b) LDAP bind logging on the DCs.** Off by default. Enable the "simple bind"
and "LDAP interface events" diagnostics on each writable DC:

```powershell
# On dc1 and rhonda. Level 2 logs unsigned/cleartext simple binds to the
# Directory Service event log; this is the authoritative source.
Set-ItemProperty `
  'HKLM:\SYSTEM\CurrentControlSet\Services\NTDS\Diagnostics' `
  -Name '16 LDAP Interface Events' -Value 2

# Then watch for binds by this account:
Get-WinEvent -LogName 'Directory Service' -MaxEvents 500 |
  Where-Object { $_.Id -in 2886,2887,2888,2889 -and $_.Message -match 'ldap' }
```

Event 2889 names the client IP and the bind identity for every simple bind. Also
enable *Audit Logon* success on the DCs and filter Security event 4624 /
4776 for `ldap`.

**(c) Vault audit log.** Once 0.2 is done, every read of
`secret/windows/domain/ldap` is recorded:

```sh
grep 'windows/domain/ldap' /var/log/vault/audit.log | jq -r \
  '[.time, .auth.display_name, .request.remote_address] | @tsv'
```

This catches the consumer nobody remembered — a human or a script reading the
credential from Vault. It is the single most useful of the three, which is why
enabling Vault audit logging is a Phase 0 prerequisite rather than an
afterthought.

**Proceed only when all three agree, over at least 14 days**, that no bind and
no Vault read has occurred. Fourteen days is not arbitrary: it exceeds the
`lastLogonTimestamp` replication window (9–14 days), so a clean fortnight means
the attribute has had a full cycle to reveal a use it might have been hiding.

### Step 2 — remove from Domain Admins **[M]**

```powershell
Remove-ADGroupMember -Identity 'Domain Admins' -Members 'ldap' -Confirm:$false
```

**Wait 60 minutes** before the next step. The SDProp task runs hourly and will
clear `adminCount` and restore inheritance on the object. Verify:

```sh
ldapsearch ... -b "CN=ldap,CN=Users,DC=myrobertson,DC=net" -s base adminCount memberOf
```

This is the single largest reduction in blast radius, and it is fully
reversible in seconds. If anything breaks, it breaks now, loudly, and
`Add-ADGroupMember` restores it.

**Wait 7 days here.** A week catches anything on a weekly schedule — backup
jobs, weekend reporting, scheduled Terraform runs. It costs nothing.

### Step 3 — disable **[M]**

```powershell
Disable-ADAccount -Identity 'ldap'
```

**Wait 30 days.** A month catches monthly jobs, certificate renewals, quarterly
reconciliation, and — most importantly — anything nobody has thought of yet.
Disabling is instantly reversible with `Enable-ADAccount`; deletion is not.
Thirty days of a disabled account costs nothing and buys the entire
recoverability margin.

Keep the Vault secret `secret/windows/domain/ldap` in place, versioned, during
this window. Do not delete it until after step 4.

### Step 4 — delete **[M]**

```powershell
Remove-ADUser -Identity 'ldap' -Confirm:$false
```

Then, and only then:

```sh
vault kv metadata delete secret/windows/domain/ldap
```

Check the AD Recycle Bin state before deleting. If it is not enabled, there is
no undo:

```powershell
Get-ADOptionalFeature -Filter 'name -like "Recycle Bin Feature"' |
  Select-Object Name, EnabledScopes
```

**If the Recycle Bin is not enabled, enable it before step 4** — it is a
one-way, forest-wide change that is worth making regardless, and it converts
this step from irreversible to recoverable for 180 days.

### Total timeline

14 days of proof + 7 days post-Domain-Admins + 30 days disabled ≈ **51 days**
from the end of Phase 5 to deletion. The account stops being a Domain Admin on
day 14 — which is where essentially all the risk reduction lands. Everything
after that is cheap insurance.

---

## Appendix — steps that cannot be codified

Recorded honestly rather than implied to be automated.

| Step | Why not | Manual procedure |
|---|---|---|
| GPO user-rights assignment for `GG-Deny-Interactive-Logon` | `ansible.windows` has no user-rights module, and `microsoft.ad` is not an installed collection. `secedit` templating is possible but brittle. | Create GPO `Deny Service Account Interactive Logon`, link at domain root, set the three Deny rights to the group (§5). |
| GPO Restricted Group granting `svc-ansible-win` `BUILTIN\Administrators` on the DCs | Same. | Create GPO `Windows Automation Host Admin`, link to `OU=Domain Controllers`, Restricted Groups → `BUILTIN\Administrators` → add `MYROBERTSON\svc-ansible-win`. |
| `Request Certificates` on the AD CS CA for `svc-tf-adcs` | CA security is stored in the CA's registry, not the directory; `dsacls` cannot reach it. | Certification Authority MMC on `dc1` → right-click the CA → Properties → Security → add `svc-tf-adcs` → allow *Request Certificates* only. |
| `Add-KdsRootKey` | Runs once, forest-wide, with a 10-hour propagation delay. Not idempotent in a way that suits a playbook. | §6. |
| Vault LDAP secrets engine mount and roles | Should be Terraform, not Ansible. The `vault` provider is already in use. | §7 gives the CLI form; port it to `homelab_bootstrap/terraform/` alongside the existing `vault_db_secret_backend` module. |
| Proxmox VE / PBS realm bind-DN change | No Ansible module; `community.proxmox` does not cover `/access/domains`. | `PUT /access/domains/<realm>` (§8, steps 3.1–3.2). Worth writing as a small playbook using `ansible.builtin.uri`. |

## Related

- [Credential lifecycle roadmap](credential-lifecycle-roadmap.md) — the
  workstream this belongs to
- [Windows domain disaster recovery](../domain/windows-domain-disaster-recovery.md)
- [PBS configuration DR from Vault](../proxmox/pbs-config-dr-from-vault.md) —
  the archive that contains a copy of the credential
- [Proxmox MCP server](../proxmox/proxmox-mcp-server.md) — the token-rotation
  pattern this generalises
- `homelab_ansible/SECURITY.md` — secret-handling rules for this repository
- `homelab_ansible/ansible/domain/provision_service_account_identities.yml` —
  the playbook that creates these identities
