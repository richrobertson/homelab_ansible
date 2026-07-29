# Synology share access audit: unauthenticated and guest exposure

## Why this exists

Kermit and Scooter hold the data this estate cannot rebuild: Nextcloud user
files, Active Backup for Business recovery points for the domain controllers,
Hyper Backup vaults, and the media libraries. Every one of those shares is
published over at least two protocols at once — SMB, NFS, rsync, iSCSI, WebDAV —
and each protocol has its own authentication model. DSM's UI shows them on
separate tabs, so it is easy for a share to be correctly locked down on the
SMB tab and wide open on the NFS tab.

This runbook is the repeatable procedure for answering one question across both
units: **can share data, or the list of shares, be reached without
authenticating as a named DSM or AD principal?**

It is read-only. Nothing in the audit path changes a share, an ACL, or a service
flag.

## Automation

| Purpose | Path |
|---------|------|
| Audit (read-only, fails on finding) | `ansible/synology/audit_share_access_exposure.yml` |
| Remediation for the two codifiable findings | `ansible/synology/remediate_share_access_exposure.yml` |

Related: [Synology disaster recovery](synology-disaster-recovery.md) for the
protection topology, and
[credential lifecycle roadmap](../security/credential-lifecycle-roadmap.md) for
the account-hygiene work these findings feed into.

## Threat model in one paragraph

DSM NFS is `AUTH_SYS` unless Kerberos is enabled — it is not, on either unit.
That means an NFS export performs **no authentication at all**: the only control
is the export's source-address list, and the client asserts its own UID. Any
host that can place a packet on the network with an allowed source address
mounts the share and reads it. SMB and rsync do authenticate, but the DSM rsync
daemon answers a module listing **before** authentication, which discloses every
shared-folder name and description. iSCSI authenticates only if CHAP is
configured on the target. Treat "reachable from network X" and "readable by
anyone on network X" as the same statement for NFS.

## Running the audit

The audit needs a DSM account in the local `administrators` group with SSH
enabled. Load it from Vault without printing it.

```bash
cd homelab_ansible
source .venv/bin/activate
export VAULT_ADDR=https://vault.myrobertson.net:8200

SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
SYNO_PASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"

OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ANSIBLE_FORKS=1 \
  ansible-playbook -i inventory/environments/production.ini \
  ansible/synology/audit_share_access_exposure.yml \
  -e "ansible_user=${SYNO_USER}" \
  -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}"
```

Add `-e synology_audit_fail_on_finding=false` to collect evidence from every
host before the first assertion aborts the run, and `-l <host>` to scope to one
NAS.

Per host the playbook prints a `synology_audit_service_summary` block, then a
severity-tagged `synology_audit_findings` list, then asserts that the list is
empty. The findings are aggregated into one assert on purpose — Ansible abandons
a host at its first failed assert, so per-check asserts would hide every finding
after the first, which is the opposite of what an audit is for.

A host is clean when it reports zero findings for all of:

- the DSM `guest` account is expired,
- no share carries a guest ACE with read or write,
- SMB1 and NTLMv1 are refused, SMB signing is required,
- anonymous FTP is off,
- no NFS export is unrestricted (`*`), `no_root_squash`, `insecure`, or matched
  by a wildcard hostname,
- every iSCSI target requires CHAP,
- QuickConnect is off,
- DSM Auto Block is on and the DSM firewall is on,
- the rsync daemon is not listening on TCP 873.

Run it against **both** hosts. Kermit's `/etc/exports` is empty, so a Kermit-only
run exercises none of the NFS checks and will look far cleaner than the estate
is. The playbook also reports `all_squash` as `INFO` — not an exposure, but it
rewrites client UIDs and belongs in the picture before anyone edits an export.

`synology_audit_findings` is a list, and the gate is a single assert at the end.
That is deliberate: Ansible abandons a host at its first failed assert, so
per-check asserts would report one finding and hide the rest.

## Running the audit without DSM credentials

Every unauthenticated check below is exactly what an attacker already on the LAN
can run, so they double as the external view. Run them from a workstation, not
from the NAS.

```bash
# 1. NFS exports and their host restrictions. An empty list means the NFS
#    service may still be running with no exports defined - check port 2049.
#    showmount is a LOWER BOUND: it never reports squash flags, it omits
#    export rules whose hostname does not resolve, and on scooter it omits
#    /volume1/docker entirely. Use it to find exports, not to clear them.
showmount -e 192.168.1.141
showmount -e 192.168.1.215

# 2. Service surface, SMB dialects, and whether SMB1 is still offered.
nmap -Pn -p 21,22,80,111,139,443,445,548,873,2049,3260,5000,5001,5022,5566,6690 \
  --script "smb-protocols,smb2-security-mode,smb-enum-shares" \
  192.168.1.141 192.168.1.215

# 3. Pre-auth rsync module listing. Every module name is a shared folder name.
rsync rsync://192.168.1.141/
rsync rsync://192.168.1.215/

# 4. Does the rsync module actually serve data without credentials?
#    "@ERROR: auth failed" is the expected, correct answer.
rsync --list-only rsync://192.168.1.141/NetBackup/

# 5. Is the unit published to the internet by Synology's relay? errno 0 with
#    "ds_state":"CONNECTED" means yes, regardless of gateway port forwards.
#    Read the alias off the NAS first - this repo is public, so it is not
#    recorded here:
#      synowebapi --exec api=SYNO.Core.QuickConnect method=get version=1
QC_ALIAS='<alias from the command above>'
curl -s -X POST https://global.quickconnect.to/Serv.php \
  -H 'Content-Type: application/json' \
  -d "{\"version\":1,\"command\":\"get_server_info\",\"stop_when_error\":false,
       \"stop_when_success\":false,\"id\":\"dsm_portal_https\",
       \"serverID\":\"${QC_ALIAS}\"}"
```

Cross-reference what the exports actually serve:

```bash
kubectl --context admin@prod get pv -o json | \
  jq -r '.items[] | select((.spec.nfs != null) or (.spec.csi.volumeAttributes.server? != null))
         | [.metadata.name, (.spec.nfs.server // .spec.csi.volumeAttributes.server),
            (.spec.nfs.path // .spec.csi.volumeAttributes.share)] | @tsv'
kubectl --context admin@prod get nodes \
  -o custom-columns='NAME:.metadata.name,IP:.status.addresses[?(@.type=="InternalIP")].address'
```

and what the gateway forwards from the internet — the source of truth is
`playbooks/core/update_unifi_gateway_port_forwards.yml`, not the UniFi UI.

## Findings, 2026-07-29

Verified against the live estate on 2026-07-29 and **re-verified authenticated
on both units** the same day. There is no coverage gap: the Vault break-glass
account authenticates on Scooter as well as Kermit (see finding 12 for the
detail that made it look otherwise).

### Ranked by exposure

| # | Finding | Host | Reach | Severity |
|---|---------|------|-------|----------|
| 0 | `kermit.myrobertson.com` publishes Kermit's DSM login page to the internet through **this repo's own Istio Gateway**, with no Authelia `AuthorizationPolicy` in front | kermit | internet | critical |
| 1 | QuickConnect enabled — DSM login page published on the public internet through Synology's relay | both | internet | critical |
| 2 | DSM Auto Block disabled — unlimited password attempts against DSM logon, SSH, SMB, rsync | **both** | internet, via #0 and #1 | critical |
| 2a | Every NFS export on Scooter has at least one `no_root_squash` rule, including all three `nextcloud-data*` exports; the `nextcloud-data*` and `plex` exports additionally carry `insecure` | scooter | LAN | high |
| 3 | TCP 6690 (Synology Drive) port-forwarded from the internet to Kermit | kermit | internet | high |
| 4 | A Synology DDNS record resolves to the WAN address (name held on the NAS, not recorded here) | scooter | internet | high |
| 5 | rsync daemon discloses every shared-folder name and description pre-auth on TCP 873 | both | LAN | medium |
| 6 | NFS exports use wildcard hostnames (`pve*`, `k8s-prod-worker-*`) resolved by reverse DNS | scooter | LAN | medium |
| 7 | NFS export `/volume1/plex` allows the whole `10.21.0.0/16` staging supernet | scooter | LAN | medium |
| 8 | Legacy NFS export `/volume1/nextcloud-data` superseded by `-prod`/`-stage` but still exported | scooter | LAN | medium |
| 9 | Default iSCSI target enabled with `auth_type: 0` (no CHAP) on all interfaces | **both** | LAN | low |
| 10 | DSM firewall disabled | **both** | LAN | low |
| 11 | SMB signing not required, SMB transport encryption off | both | LAN | low |
| 12 | `svc-syno-ssh` has no home directory on Scooter, and Scooter's `sshd` also listens on 22 | scooter | n/a | operational |

**0 — Kermit's DSM is published on the internet by the GitOps repo itself
(critical).** This is the finding that changes the remediation order, and it is
the one a QuickConnect-only view misses in the opposite direction to #1.

`homelab_flux/infrastructure/gateway/externalServices/kermit.yaml` declares a
`ServiceEntry` for `kermit.myrobertson.net:5010` and an `HTTPRoute`
`kermit-ext-route` for hostname `kermit.myrobertson.com`, parented to
`myrobertson-com-gateway`, which has a matching listener at
`infrastructure/gateway/myrobertson-com/myrobertson-com-gateway.yaml`. The
UniFi gateway forwards 443 to `10.31.0.69`, that gateway. `kubectl --context
admin@prod get httproute -A` shows the route live, and
`kubectl --context admin@prod get authorizationpolicy -A` shows **no** policy
covering it — the nine `auth-policy-*` objects are for lidarr, radarr, sonarr,
prowlarr, syncthing, ntfy, n8n, code-server and seerr. Reproduce:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://kermit.myrobertson.com/   # 200, DSM login page
```

So **disabling QuickConnect does not remove Kermit's internet-facing DSM login
page.** Unlike QuickConnect this one is fully codified, so it can be closed in
IaC — but not blindly: `apps/prod/authelia/authelia-values.yaml` registers
`https://kermit.myrobertson.com/webman/ssoclient/token_relay.html` as the
**only** redirect URI for the `synology_kermit_prod` OIDC client. Deleting the
route without adding an internal redirect URI breaks DSM SSO on Kermit for LAN
users too. See "Close the Kermit gateway route" under Remediation.

**2a — NFS root squashing is off across the board (high, scooter).** This is
what the earlier "squash flags unknown on Scooter" gap was hiding, and it is
worse than the gap implied. Read from Scooter's `/etc/exports`:

| Export | Squash | `insecure` | Note |
|--------|--------|-----------|------|
| `/volume1/nextcloud-data-prod` | `no_root_squash` (all 3 rules) | yes | every prod worker mounts as root |
| `/volume1/nextcloud-data-stage` | `no_root_squash` (all 3 rules) | yes | |
| `/volume1/nextcloud-data` (legacy) | `no_root_squash` (all 6 rules) | yes | still exported |
| `/volume1/plex` | `no_root_squash` (all rules) | on `10.21.0.0/16`, `10.21.{0,1,2}.2`, `k8s-stg-worker-*` | widest export |
| `/volume1/janice` | mixed: `root_squash` for `pve*`, `10.31.*`, `.241`; `no_root_squash` for `192.168.{10,11,88}.1/24`, `.242`, `.243` | no | inconsistent |
| `/volume1/downloads`, `/volume1/radarr`, `/volume1/unraid`, `/volume1/docker` | `no_root_squash` | no | |
| `/volume1/NetBackup` | `all_squash,anonuid=1024` for the IP rules, `no_root_squash` for host `animal` | no | backup target |

Two things follow.

- With `no_root_squash`, an allowed client that is root **is root on the share**.
  For `/volume1/nextcloud-data-prod` that means root on any prod worker node
  reads and writes every Nextcloud user's file regardless of mode bits. The
  earlier write-up called this "accepted, inherent to AUTH_SYS"; it is not
  inherent — `root_squash` would stop exactly this and DSM offers it. It is a
  configuration choice, and it is recorded as one now.
- `insecure` allows a client to mount from an unprivileged source port. On a
  host with `no_root_squash` that removes the last barrier: a **non-root**
  local user on an allowed host can speak NFS directly and assert uid 0.

`/etc/exports` also lists a rule for host `animal` on `/volume1/docker`,
`/volume1/NetBackup`, `/volume1/downloads` and `/volume1/unraid` with
`rw,no_root_squash`. `showmount` does not show it because the name does not
resolve. An unresolvable hostname in an export list is a rule nobody is
maintaining; confirm what `animal` was and delete it.

> **Do not "fix" the squash flags on the Nextcloud exports without testing.**
> The Nextcloud pods run with `fsGroup: 33` and no `runAsUser`, so the image
> entrypoint runs as root and chowns the data directory on start. Moving
> `/volume1/nextcloud-data-prod` to `root_squash` squashes that root to
> `anonuid=1025` and can leave Nextcloud unable to write its own data
> directory. Test on `-stage` first — it is the same configuration and it is
> what staging binds. See the UID/squash warnings under Remediation.

`nextcloud-data-prod` and `nextcloud-data-stage` are still correctly pinned to
exactly the three worker node IPs each. The host list was never the problem
here; the mount options are.

### What is clean

These were checked and are correctly configured. Recording them matters as much
as recording the failures — it stops the next audit re-deriving them.

| Control | Kermit | Scooter |
|---------|--------|---------|
| DSM `guest` account | expired (disabled) | expired (disabled) |
| Shares with a guest ACE | none of 20 | none of 24 |
| SMB minimum protocol | SMB2 (`smb_min_protocol: 1`) | SMB2 (`smb_min_protocol: 1`) |
| NTLMv1 | refused (`enable_ntlmv1_auth: false`) | refused (`enable_ntlmv1_auth: false`) |
| SMB null-session share enumeration | denied | denied |
| Anonymous FTP | FTP service off; 21 filtered | FTP service off; 21 closed |
| AFP | off; 548 filtered | off; 548 closed |
| Anonymous rsync **data** read | `@ERROR: auth failed` | `@ERROR: account system disabled` |
| NFS exports with no host restriction (`*`) | none — export table empty | none |
| iSCSI CSI storage classes in use | — | all three `disabled: true`, no PVs bound |
| Tailscale package | running | running |

`guest` deserves a note. DSM's `SYNO.Core.Share.Permission` lists the `guest`
principal against *every* share with `is_writable: false, is_readonly: false,
is_deny: false`, which means "listed, not granted". The audit playbook now
extracts the `guest` object and requires `is_writable` or `is_readonly` to be
true before it calls it a grant. An earlier revision of that expression used
double-backslash escapes inside a YAML folded scalar, never matched anything,
and reported "no guest grants" on every host regardless of the real ACLs — see
"Known parser traps" below.

### Detail

**1 — QuickConnect (critical, both units, internet).** Kermit's DSM reports
`"enabled": true` with a configured `server_alias`. Querying Synology's
coordinator with that alias confirms both units are live:

| Unit | `ds_state` | External | Synology DDNS |
|------|-----------|----------|---------------|
| kermit | CONNECTED | WAN address, `ext_port` 443 | none |
| scooter | CONNECTED | WAN address, relay only | one record configured |

The aliases, server IDs and WAN address are deliberately not recorded here —
this repo is public and those values are the live endpoint. Read them off each
NAS with `synowebapi --exec api=SYNO.Core.QuickConnect method=get version=1`.

Anyone on the internet reaches the DSM login page at
`https://quickconnect.to/<alias>` or the matching `*.direct.quickconnect.to`
SmartDNS name. No gateway rule permits this and none can block it —
QuickConnect is an outbound relay registration made by the NAS itself. **A
port-forward audit will not find it.** Both units are AD-joined to
`MYROBERTSON.NET` with
`enable_http_negotiate: true`, so this is an internet-facing logon surface for
domain credentials — including `ldap@myrobertson.net`, the Domain Admin account
that was public for 9.5 months and is currently being retired.

**2 — Auto Block disabled (critical, BOTH units).**
`SYNO.Core.Security.AutoBlock` returns `{"enable": false, "attempts": 10,
"within_mins": 5, "expire_day": 0}` on Kermit **and on Scooter**. Auto Block is
DSM's only brute-force control and it covers
DSM logon, SSH, SMB, FTP and rsync. The domain's `lockoutThreshold` is `0` (see
the credential lifecycle roadmap), so **nothing anywhere in the chain limits
password guessing against an AD account through the internet-facing DSM login
page.** Findings 1 and 2 together are the highest-severity item on this page;
neither alone is as bad as the pair.

**3 — Port 6690 forwarded to Kermit (high).** Declared in IaC at
`playbooks/core/update_unifi_gateway_port_forwards.yml`:

```yaml
- import_playbook: update_unifi_port_forward.yml
  vars:
    unifi_forward_ip: 192.168.1.141
    unifi_port_forward_updates:
      - rule_name: synology-drive
        external_port: 6690
        forward_port: 6690
```

Synology Drive's sync protocol does require a login, so this is not
unauthenticated access to a share. It is a pre-auth attack surface on a unit
with Auto Block off. The only other forwards are 80/443 to `10.31.0.69`, the
Istio ingress — **and that is not a reason to stop looking**, because finding 0
is a NAS service published through exactly that ingress. A port-forward
inventory is necessary and not sufficient: check the Gateway/HTTPRoute set in
`homelab_flux/infrastructure/gateway/` for backends that point at a NAS too.

**5 — Pre-auth rsync module disclosure (medium, both).** TCP 873 is open on
both units and answers an unauthenticated module listing. `rsync rsync://<host>/`
returns every shared folder with its description. Attempting to read a module
fails (`@ERROR: auth failed` on Kermit, `@ERROR: account system disabled` on
Scooter), so this is disclosure, not data access — but it hands an attacker the
full share inventory, and the names are informative: `Vault`, `HyperBackupVault`,
`NetBackup`, `ActiveBackupforBusiness`, `homes`, `nextcloud-data-prod`.

Kermit: 21 modules. Scooter: 25 modules, including `System`, `remotes`, `logs`,
`janice`, `unraid`, and `home`.

Note that the playbook's `rsync_conf_modules_without_auth` field reads `0` on
Kermit. That is not a contradiction and not reassurance: DSM generates the
module list at runtime from the shared-folder table rather than writing it into
`/etc/rsyncd.conf`, so the file-based count cannot see the 21 modules that are
actually served. `rsync_daemon_listening` is the field that carries this
finding.

**6, 7, 8 — NFS export host restrictions (medium, scooter).** Kermit's export
table is empty, though the NFS service is enabled and 2049 is listening. Scooter
exports **ten** paths. `showmount -e` shows only nine: it omits `/volume1/docker`
and it silently drops export rules whose hostname does not resolve, so it
under-reports. Read `/etc/exports` (the audit playbook does) rather than trusting
`showmount` for an inventory. Squash and `insecure` per export are in finding 2a
above; the host lists are:

| Export | Allowed hosts | Assessment |
|--------|---------------|------------|
| `/volume1/nextcloud-data-prod` | `10.31.2.2 10.31.1.2 10.31.0.2` | correct — exactly the three prod worker node IPs |
| `/volume1/nextcloud-data-stage` | `10.21.2.2 10.21.1.2 10.21.0.2` | correct — exactly the three staging worker node IPs |
| `/volume1/nextcloud-data` | both sets above | **legacy** — superseded by `-prod`/`-stage`, still exported |
| `/volume1/unraid` | `kermit`, `animal` | name-based; `animal` does not resolve |
| `/volume1/docker` | `animal` | not shown by `showmount`; `animal` does not resolve |
| `/volume1/downloads` | `animal`, `10.31.{0,1,2}.1/24` | whole prod worker subnets, not just node IPs |
| `/volume1/radarr` | prod worker /24s, `192.168.88.0/24`, `192.168.12.0/24`, `192.168.1.{18,43,241,242,243}` | broad |
| `/volume1/NetBackup` | `animal`, `192.168.88.0/24`, `192.168.1.{17,19,110,241,242,243}` | broad, and this is a backup target |
| `/volume1/janice` | `pve*`, `192.168.{10,11,88}.0/24`, prod worker /24s, `192.168.1.{241,242,243}` | wildcard hostname |
| `/volume1/plex` | `pve*`, `k8s-stg-worker-*`, `k8s-prod-worker-*`, `10.21.0.0/16`, prod worker /24s, `192.168.88.0/24`, `192.168.12.0/24`, `192.168.11.0/24`, `192.168.1.{18,241,242,243}` | **widest export on the estate** |

Two structural problems:

- **Wildcard hostnames.** `pve*` and `k8s-*-worker-*` are matched by DSM through
  reverse DNS on the client address. The estate runs AD-integrated DNS with
  dynamic updates, so a client that can register a PTR matching the pattern
  gains the export. Prefer IP literals; the `nextcloud-data-*` exports already do.
- **Supernets.** `10.21.0.0/16` on `/volume1/plex` covers 65 534 addresses where
  only three are wanted (`10.21.{0,1,2}.2`). Note it does **not** reach the
  staging control planes — those are `10.20.{0,1,2}.2`, a different /16. The
  problem is the size of the range, not which nodes it happens to contain.

Because NFS here is `AUTH_SYS` with no Kerberos (`enable_kerberos: false`,
`unix_pri_enable: true`, NFSv4.1 on), each row above is a list of addresses that
can read that share's bytes without any credential. For
`/volume1/nextcloud-data-prod` this means **any process on a prod worker node —
including a hostNetwork pod — can mount and read every Nextcloud user's files,
bypassing Nextcloud's own ACLs entirely**, and because the export is
`no_root_squash,insecure` it can also write them as root. The AUTH_SYS part is
inherent to the design; the `no_root_squash,insecure` part is not — see finding
2a.

**9 — iSCSI without CHAP (low, BOTH units).** On Kermit the target is
`iqn.2000-01.com.synology:kermit.default-target.3d35caf9bf1`, `is_enabled: true`,
`auth_type: 0`, portal `all interfaces:3260`, and `mapping_index: -1` — no LUN
is mapped, so there is no data behind it. It is DSM's default target that was
never removed. **Scooter reports the same shape: 1 of 1 targets with
`auth_type: 0`.** Scooter is the DSM the CSI driver targets, so that is the unit
where a future CHAP decision actually matters. Low impact today; it is an
enabled, unauthenticated iSCSI endpoint that will silently serve any LUN mapped
to it in future.

Kubernetes does not depend on it. All three `synology-iscsi-*` storage classes
in `homelab_flux/infrastructure/controllers/synology-iscsi-csi/release.yaml` are
`disabled: true`, no PV in either cluster uses `csi.san.synology.com` for iSCSI,
and the only Synology CSI storage class actually defined is
`synostorage-nfs-delete` (NFS, not iSCSI). The CSI driver authenticates to DSM
with a DSM account from `client-info.yml`, not with CHAP; CHAP is a per-target
setting the driver would have to be told to apply.

**10, 11 — DSM firewall and SMB hardening (low, BOTH units).**
`enable_firewall: false` on Kermit **and Scooter**. `enable_server_signing: 0`
(signing offered, not required — confirmed independently by
`nmap smb2-security-mode`: "Message signing enabled but not required") and
`smb_encrypt_transport: 0` on both. On a flat LAN this is a relay/downgrade
opportunity, not a direct data exposure.

A DSM firewall that reads `enable_firewall: false` still produces `filtered`
rather than `closed` results from `nmap` on Kermit (21, 22, 548, 5000, 5001).
That is not a hidden firewall: `iptables -S` shows DSM's `DOS_PROTECT` chain
rate-limiting outbound RSTs to 1/sec on `bond0`, so a scanner sees silence
instead of a reset. Do not read `filtered` as "a firewall is on".

**12 — Break-glass account works; the earlier "credential drift" finding was
wrong (operational, scooter).** `secret/synology/dsm-admin/local-ssh-account`
(`svc-syno-ssh`) **authenticates on Scooter** on port 5022, and the full audit
playbook runs against Scooter with it. Verify in one command:

```bash
ansible -i inventory/environments/production.ini scooter.myrobertson.net \
  -e "ansible_user=${SYNO_USER}" -e "ansible_password=${SYNO_PASS}" -m raw -a 'echo OK'
```

What is real, and what made it look like a rejection: the account has **no home
directory** on Scooter, so every session opens with

```
Could not chdir to home directory /var/services/homes/svc-syno-ssh: No such file or directory
```

on stderr. `raw` merges stderr into stdout, so that banner lands in the output
of every command and gets parsed as data unless it is filtered — it was being
counted as a shared folder and as an NFS export line. The audit playbook now
strips it. Two things to fix on the unit anyway, neither urgent:

- create `/var/services/homes/svc-syno-ssh` (enable the user's home in DSM >
  Control Panel > User & Group > Advanced > User Home) so the banner stops;
- `sshd` on Scooter listens on **both** 22 and 5022, where Kermit listens only
  on 5022.

The lesson worth keeping: a failed `ssh` to a DSM box is not evidence that the
credential is wrong. Confirm with a second method before writing a break-glass
account off — declaring a working emergency path broken is its own incident.

Also corrected: `ansible/synology/readme.md` referenced
`inventory/environments/synology.ini`, which does not exist — the working
inventory is `inventory/environments/production.ini`.

### Known parser traps

Both are fixed in `audit_share_access_exposure.yml`; they are recorded because
anyone extending that playbook will hit them again.

- **Backslashes in YAML folded scalars.** A `>-` block performs no escape
  processing, so `'\\s\\*\\('` reaches the regex engine as a literal backslash
  followed by an unterminated group and aborts the play, while `'[A-Za-z0-9]\\*'`
  quietly becomes "any alphanumeric followed by zero or more backslashes" and
  matches everything. Use **single** backslashes in folded and plain scalars,
  and **double** backslashes in double-quoted scalars (`"{{ ... '^\\S+' ... }}"`),
  which is why both spellings appear in the same file. The export-parsing block
  originally used doubles; it never ran, because Kermit — the only unit it was
  tested against — has an empty `/etc/exports`.
- **`insecure` vs `insecure_locks`.** DSM writes `insecure_locks` on every rule
  it generates. A substring test for `insecure` matches every export and the
  finding becomes noise. The option must be anchored on both sides.

Whenever you add a check here, run it against **Scooter** too. Kermit has no NFS
exports, so it cannot exercise the export path at all.

## Remediation

### Codified

`ansible/synology/remediate_share_access_exposure.yml` handles findings 1 and 2.
Both default to off; each needs its `*_apply` flag.

> **Prerequisite before enabling Auto Block — do not skip.** Auto Block blocks a
> *source IP*, and the playbook's defaults are 5 attempts within 5 minutes with
> `expire_day: 0`, which means **blocked addresses are never released
> automatically**. The Ansible control host authenticates over SSH with a
> password, so five failed runs — a rotated DSM password, a typo, a stale Vault
> read — permanently block the workstation from the NAS. Do this first, in DSM >
> Control Panel > Security > **Account** > *Allow/Block List* > Allow List, on
> **each unit**:
>
> 1. Add the Ansible control host address and `192.168.1.0/24`.
> 2. Add the Tailscale interface range.
> 3. Only then run with `synology_autoblock_apply=true`.
>
> `SYNO.Core.Security.AutoBlock.Rules method=list version=1` returns error 5100
> on this DSM build, so the allow list cannot be read or set from the playbook —
> it is a UI step and it is a hard prerequisite, not an optional hardening extra.
> The break-glass account works on both units, so a control-host block is
> recoverable from the DSM UI on the LAN, but it still costs you every automated
> path at once because both units are in the same inventory group and share the
> same credential. Populate the allow list on **both** units before applying to
> either. Both units also run Tailscale, which is a second way back in.

```bash
cd homelab_ansible
source .venv/bin/activate
export VAULT_ADDR=https://vault.myrobertson.net:8200
SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
SYNO_PASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"

# Dry report: prints before/after state, changes nothing.
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ANSIBLE_FORKS=1 \
  ansible-playbook -i inventory/environments/production.ini \
  ansible/synology/remediate_share_access_exposure.yml \
  -e "ansible_user=${SYNO_USER}" -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}"

# Apply. Confirm first that nobody depends on QuickConnect for remote access.
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES ANSIBLE_FORKS=1 \
  ansible-playbook -i inventory/environments/production.ini \
  ansible/synology/remediate_share_access_exposure.yml \
  -e "ansible_user=${SYNO_USER}" -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}" \
  -e synology_autoblock_apply=true \
  -e synology_quickconnect_disable_apply=true
```

The example above passes both flags in one invocation. That contradicts the
ordering below — run it twice, once per flag, so each change is verified alone.

Ordering, and why: **disable QuickConnect first.** It is the internet-facing
finding, and it removes the exposure outright. Auto Block does not stop an
attack, it only rate-limits one, and enabling it while the relay is still up
means the block list fills with relay-side addresses — under QuickConnect relay
the connection reaches DSM from Synology's relay, not from the attacker, so
Auto Block will happily block the relay (cutting off any legitimate remote user)
without inconveniencing the guesser. Auto Block's value here is as the standing
control *after* the internet path is closed.

The counter-argument for doing Auto Block first is that it is invisible to users
while turning QuickConnect off is not. That is a user-experience argument, not a
risk argument. Confirm first that nobody depends on QuickConnect: **both** units
run Tailscale (`synopkg status Tailscale` reports `running` on Kermit and
Scooter), so remote access has a supported replacement on each.

> **Disabling QuickConnect does not close the internet-facing DSM login page on
> Kermit.** Finding 0 — the `kermit.myrobertson.com` HTTPRoute — is a separate
> path through the Istio gateway and survives this change. Do finding 0 in the
> same window or the critical is only half closed. Order:
>
> 1. Close the Kermit gateway route (finding 0) — see below.
> 2. Disable QuickConnect on both units (finding 1).
> 3. Populate the DSM Allow List on both units, then enable Auto Block
>    (finding 2), which is now a standing control rather than the only control.

**Close the Kermit gateway route (finding 0).** This is codified and belongs in
`homelab_flux`, not here, so it is a cross-repo change and is called out rather
than performed by this runbook's playbooks. Two parts, and the order matters:

1. **First** add an internal redirect URI for the `synology_kermit_prod` OIDC
   client in `apps/prod/authelia/authelia-values.yaml`, alongside the existing
   `https://kermit.myrobertson.com/...` one — for example
   `https://kermit.myrobertson.net:5001/webman/ssoclient/token_relay.html`,
   matching how `synology_scooter_prod` is already configured. Without this,
   removing the public hostname breaks DSM SSO on Kermit **for LAN users as
   well**, because that public URL is currently the client's only registered
   redirect URI.
2. **Then** remove the route. Delete
   `infrastructure/gateway/externalServices/kermit.yaml`, drop the `kermit`
   listener from `infrastructure/gateway/myrobertson-com/myrobertson-com-gateway.yaml`,
   and remove this line from `infrastructure/gateway/kustomization.yaml`
   (line 8 at time of writing) — it is reported here rather than edited because
   that file is shared:

   ```yaml
     - externalServices/kermit.yaml
   ```

   If the route must stay, the minimum acceptable alternative is an Istio
   `AuthorizationPolicy` with `action: CUSTOM` and the Authelia `extensionProvider`,
   modelled on `auth-policy-radarr` — but it must exclude
   `/webman/ssoclient/token_relay.html`, or the OIDC callback is itself gated by
   the thing it is trying to complete and login loops.

Verify either way with `curl -s -o /dev/null -w '%{http_code}\n' https://kermit.myrobertson.com/`
and a DSM SSO login from the LAN.

**Do not paste the playbook's output into `ansible/synology/logs/` or any file
in this repo.** `homelab_ansible` is public, that directory is git-tracked, and
`synology_audit_service_summary` prints `quickconnect_alias` in clear. Note also
that the aliases follow the `<hostname>-<domain-label>` pattern and are
guessable in one attempt from the hostnames already in this repo — redaction
buys almost nothing here, and only disabling QuickConnect actually closes it.

### Not codified — DSM UI only

These have no API that is both present on this DSM build and safe to drive
unattended. The procedure is manual and that is stated rather than pretended
away.

**Disable the rsync daemon (finding 5).** `SYNO.Core.FileServ.Rsync` returns
`{"error": {"code": 102}}` on Kermit (RS1221+, DSM 7.2) — the endpoint does not
exist on this build, so there is nothing to script against.

1. DSM > Control Panel > File Services > **rsync**.
2. Clear **Enable rsync service**. Apply.
3. Verify from a workstation: `rsync rsync://<host>/` must fail to connect, and
   `nmap -p873 <host>` must report closed.

Check first whether Hyper Backup or Snapshot Replication uses rsync transport to
the peer. Snapshot Replication uses port 5566, not 873, so disabling the rsync
*service* does not affect it — but confirm on the unit before applying.

**Tighten NFS exports (findings 2a, 6, 7, 8).** `SYNO.Core.Share` can set
`nfs_priv`, but a malformed call replaces the whole privilege list for the share
and silently breaks every mount that depends on it, including live Nextcloud.
Not worth scripting for a ten-row change.

Sequence these by blast radius, not by list order. `/volume1/nextcloud-data`
(legacy, nothing mounts it) is free. `/volume1/plex`, `/volume1/janice`,
`/volume1/downloads` and `/volume1/radarr` cost a media outage at worst. The
`no_root_squash` change on `/volume1/nextcloud-data-prod` is the one that can
take Nextcloud down — do it last, after proving it on `-stage`.

For each share, DSM > Control Panel > **Shared Folder** > select > Edit > **NFS
Permissions**:

1. `/volume1/nextcloud-data` — verify nothing mounts it
   (`kubectl --context admin@prod get pv`, `kubectl --context admin@staging get pv`;
   both clusters bind `-prod` / `-stage`, not this path), then **delete** every
   rule and the export with it.
2. `/volume1/plex` — replace `k8s-stg-worker-*` with `10.21.0.2`, `10.21.1.2`,
   `10.21.2.2`; replace `k8s-prod-worker-*` with `10.31.0.2`, `10.31.1.2`,
   `10.31.2.2`; delete the `10.21.0.0/16` rule; **and delete the
   `10.31.0.1/24`, `10.31.1.1/24`, `10.31.2.1/24` rules.** Those three /24s
   already contain `10.31.{0,1,2}.2`, so replacing the `k8s-prod-worker-*`
   wildcard while leaving them in place narrows nothing — the export stays open
   to every address on all three prod node subnets. The same applies to
   `/volume1/janice`, `/volume1/downloads` and `/volume1/radarr`, which are
   scoped by those /24s and not by node IPs.
3. `/volume1/janice` — replace `pve*` with `192.168.1.241`, `192.168.1.242`,
   `192.168.1.243`.
4. `/volume1/plex` — replace `pve*` the same way.
5. `/volume1/docker`, `/volume1/NetBackup`, `/volume1/downloads`,
   `/volume1/unraid` — delete the rule for host `animal`. It does not resolve,
   so nothing can be using it, and it carries `rw,no_root_squash`.
6. **Squash is a separate, riskier change. Read the current value before you
   touch it.** In DSM this is the *Squash* dropdown; the mapping to what the
   audit reports is:

   | DSM Squash | `/etc/exports` | Effect |
   |---|---|---|
   | No mapping | `no_root_squash` | client root **is** root on the share |
   | Map root to admin | `root_squash` | only UID 0 is remapped to `anonuid` |
   | Map all users to admin | `all_squash` | **every** client UID is remapped |

   Today every Scooter export has at least one `no_root_squash` rule, so this
   is not hypothetical — see finding 2a for the per-export table.

   - `Map root to admin` (`root_squash`) is the correct target for a data
     share. It remaps only UID 0 and leaves every other client UID intact.
   - `Map all users to admin` (`all_squash`) is **not** an acceptable
     substitute. It rewrites the effective UID of *every* client to the rule's
     `anonuid`. On `/volume1/nextcloud-data-prod` that changes the owner of
     newly written files away from the UID Nextcloud runs as, so Nextcloud
     stops being able to read files it wrote before the change and files
     written after it become unreadable to any pod that reverts. Note
     `/volume1/NetBackup` already mixes `anonuid=1024` (its `all_squash` IP
     rules) with `anonuid=1025` elsewhere, so "the anon user" is not one
     identity across this NAS — check the anonuid on the specific rule.
     Treat an `all_squash` change to a live data export as a data-ownership
     migration, not a firewall tweak.
   - **`/volume1/nextcloud-data-prod` very likely depends on `no_root_squash`
     today.** The Nextcloud pods run with `fsGroup: 33` and no `runAsUser`, so
     the image entrypoint starts as root and chowns the data directory. Moving
     to `root_squash` squashes that root to `anonuid=1025` and the entrypoint
     can fail. Prove the change on `/volume1/nextcloud-data-stage` first — it
     has the same options and the same client shape — and only then touch prod.
     If prod genuinely needs remote root, say so in this runbook as an accepted
     exception rather than leaving it undocumented.
   - Also clear the `insecure` option on the `nextcloud-data*` and `plex`
     rules that carry it. Requiring a privileged source port is the cheap half
     of this finding and it does not change any UID.
7. Re-run the audit playbook against **both** units. Re-mount one PVC per
   affected share and confirm read/write **as the workload's own UID, not as
   root** before closing the change — a root-only test will pass against an
   export that has just been broken for the application. For Nextcloud
   specifically, restart a pod: the failure mode is at entrypoint chown time,
   not at read time, so an already-running pod will look healthy.

Do this in a maintenance window. An NFS export change takes effect immediately
and drops in-flight mounts.

**Remove the unused iSCSI target (finding 9).** On **each** unit: DSM > SAN
Manager > **Target** > select `Synology iSCSI Target` > confirm **Mapped LUN**
is empty > Remove. If it must stay, Edit > **Authentication** > enable CHAP and
store the secret in Vault under `secret/synology/<host>/iscsi-chap`. Deleting a
target is a storage mutation; confirm no initiator is connected first
(`Connected Sessions` must be 0). Scooter matters more than Kermit here: it is
the DSM the CSI driver is pointed at.

> **Blocking dependency — Vault Secrets Operator.** Anything that ends with a
> credential landing in Kubernetes is stalled right now. The Synology CSI
> driver is deployed and running in prod (`synology-csi`, targeting DSM
> `192.168.1.215`) and takes its DSM credential from the VaultStaticSecret
> `client-info-secret` at `secret/synology/prod/csi-client-info` (verify with
> `kubectl --context admin@prod get vaultstaticsecret -n synology-csi
> client-info-secret -o yaml`). That object currently reports
> `SecretSynced=False` — it is one of the 73/74 VaultStaticSecrets whose
> reconcile loop is dead. The Kubernetes Secret still holds the correct value,
> so the driver keeps working, but **any new value written to Vault will not
> reach the cluster.** Consequences for this runbook:
>
> - Do not rotate or disable the DSM account the CSI driver uses on Scooter
>   (including as part of the `ldap@myrobertson.net` retirement) until VSO is
>   fixed. Rotating it breaks volume provisioning with no way to push the new
>   credential.
> - If CHAP is enabled on a target the CSI driver is expected to use, the
>   driver's `client-info` has to change, which is the same blocked path. Prefer
>   removing the unused target over enabling CHAP on it.
> - **Finding 0 has the same dependency in a less obvious place.**
>   `synology-kermit-oidc-secret` and `synology-scooter-oidc-secret` (namespace
>   `default`, paths `synology/prod/sso/authelia/{kermit,scooter}`) are also
>   `SecretSynced=False`. Adding a redirect URI does not touch the client
>   secret, so the finding 0 fix is safe — but **rotating** either Synology OIDC
>   client secret is blocked until VSO is fixed, and a half-rotated OIDC client
>   locks every SSO user out of that NAS's DSM.

**Enable the DSM firewall (finding 10, both units).** Not scripted deliberately:
a firewall rule applied over SSH can terminate the session that applied it and
leave the unit unreachable. DSM > Control Panel > Security > **Firewall**. Build
the rule set with **Deny** as the default action *last*, and keep a DSM session
open on a second browser until the rules are verified.

Derive the allow list from the unit's own export and client inventory, not from
this page — it will drift. Read `/etc/exports` on the unit rather than
`showmount -e`, which omits rules whose hostname does not resolve. On Scooter
today the clients that must survive the change are:

| Source | Why it must stay |
|--------|------------------|
| `192.168.1.0/24` | management, Proxmox `.241-.243`, ABB agents, DSM admin |
| `10.31.0.0/24`, `10.31.1.0/24`, `10.31.2.0/24` | prod k8s nodes — Nextcloud data, plex, radarr, downloads |
| `10.21.0.0/24`, `10.21.1.0/24`, `10.21.2.0/24` | staging k8s nodes — `nextcloud-data-stage` |
| `192.168.88.0/24` | exported to `/volume1/plex`, `/volume1/radarr`, **`/volume1/NetBackup`** |
| `192.168.12.0/24` | exported to `/volume1/plex`, `/volume1/radarr` |
| `192.168.11.0/24` | exported to `/volume1/plex`, `/volume1/janice` |
| `192.168.10.0/24` | exported to `/volume1/janice` |
| Tailscale interface | the fallback path once QuickConnect is off |
| The Ansible control host | the audit and remediation playbooks run over SSH 5022 |

Omitting the four `192.168.{10,11,12,88}.0/24` ranges is the easy mistake here:
it silently cuts the NetBackup target and the Plex/Janice clients, and NFS
failures surface as hung mounts rather than errors. Also allow the DSM HTTPS API
port from the prod node subnets — the Synology CSI controller in
`synology-csi` talks to DSM `192.168.1.215` over it, and it is not an NFS
client, so an NFS-only allow list will break volume provisioning.

**Require SMB signing (finding 11).** DSM > Control Panel > File Services > SMB >
Advanced Settings > **Transport encryption mode** and server signing. Changing
signing forces existing SMB sessions to reconnect; do it in a window. Verify
with `nmap --script smb2-security-mode`: the expected result is
"Message signing enabled and required".

**Remove the Synology DDNS record (finding 4).** DSM > Control Panel > External
Access > **DDNS** on Scooter > remove the `*.synology.me` record. Confirm no
client config points at that name first.

**Tidy break-glass SSH on Scooter (finding 12).** The account already works — do
not recreate it. Two cosmetic-but-worth-doing items:

1. Give `svc-syno-ssh` a home directory on Scooter (DSM > Control Panel > User &
   Group > **Advanced** > User Home) so sessions stop emitting the
   `Could not chdir to home directory` banner on stderr. Any tool that parses
   `raw` output has to filter that banner otherwise; the audit playbook now
   does, but the next one written will not.
2. Consider closing the extra port 22.

Do **not** close port 22 on Scooter in the same change as anything else. Both
ports there present identical SSH host keys:

```
ssh-keyscan -p 22   192.168.1.215 | ssh-keygen -lf -
ssh-keyscan -p 5022 192.168.1.215 | ssh-keygen -lf -
```

Identical fingerprints mean one `sshd` bound to two ports, not a second daemon —
so this is an extra `Port` directive in `/etc/ssh/sshd_config`, not something
the Control Panel > Terminal & SNMP dialog can turn off (that dialog sets a
single port, it has no per-port toggle). Sequence it safely: confirm the audit
playbook completes against Scooter over 5022, keep an open session on 5022, and
only then remove the extra `Port 22` line.

## Re-audit cadence

Run the audit playbook after any DSM update, after any share is created, and
monthly as a standing control. A clean run is one where every host reports an
empty `synology_audit_findings` list with `synology_audit_fail_on_finding` left
at its default of `true`.

Baselines as of 2026-07-29, both authenticated:

- **Kermit — 6 findings.** iSCSI without CHAP, QuickConnect enabled, Auto Block
  disabled, rsync daemon listening, DSM firewall disabled, SMB signing not
  required. 20 shares, `/etc/exports` empty.
- **Scooter — 23 findings.** The same 6, plus 10 × `no_root_squash`,
  4 × `insecure`, 2 × wildcard hostname, and 1 × `all_squash` (informational).
  24 shares, 10 exports.

Neither of those counts includes finding 0 — the `kermit.myrobertson.com`
gateway route is a `homelab_flux` object and this playbook cannot see it. Check
it separately on every re-audit:

```bash
kubectl --context admin@prod get httproute -A -o json | \
  jq -r '.items[] | select([.spec.rules[].backendRefs[]?.name] |
         any(test("kermit|scooter|192\\.168\\.1\\.(141|215)")))
         | [.metadata.namespace, .metadata.name, (.spec.hostnames|join(","))] | @tsv'
```
