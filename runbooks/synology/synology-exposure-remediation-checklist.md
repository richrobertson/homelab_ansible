# Synology exposure remediation — DSM click-by-click

Companion to [synology-share-access-audit.md](synology-share-access-audit.md),
which explains *why* each item is a finding. This is the execution list, in the
order it should actually be done.

**State this was written against: 2026-09-23.** Scooter 23 findings, Kermit 6,
finding 0 already closed by gating the route. Every rule quoted below was read
off Scooter's live `/etc/exports` that day — re-read it before you start, because
if it has changed, so has this list.

## What is scriptable and what is not

Tested against the DSM API as root on 2026-09-23. **The NFS export phases turned
out to be scriptable after all** — the rest are genuinely UI tasks.

| API | Result |
|---|---|
| `SYNO.Core.FileServ.NFS.SharePrivilege` `load` / `save` | **works** — see below |
| `SYNO.Core.ISCSI.Target` `delete` / `set` | `18990710` (API present, `list` works, writes refused) |
| `SYNO.Core.FileServ.SMB` `set` | `2001` (wants the whole config object) |
| `SYNO.Core.FileServ.Rsync` | `102` (endpoint absent on this build) |
| `SYNO.Core.Security.AutoBlock.Rules` | `5100` |

Don't re-derive that. Phases 5–10 are UI tasks; phases 1–4 need not be.

### Driving NFS exports from the CLI

The method names are not the obvious ones: it is **`load`** and **`save`**, not
`get`/`set` (both return `103`, "method does not exist"). `SYNO.Core.Share`
`get` with `additional=["nfs_priv"]` silently returns the share without the
privileges — it is the wrong API, not a wrong parameter.

```sh
W=/usr/syno/bin/synowebapi
# read
$W --exec api=SYNO.Core.FileServ.NFS.SharePrivilege method=load version=1 \
   share_name='"plex"'
# write - REPLACES the whole rule list for that share
$W --exec api=SYNO.Core.FileServ.NFS.SharePrivilege method=save version=1 \
   share_name='"plex"' rule='[ ...full desired array... ]'
```

A rule object looks like this; `root_squash: "root"` is what `/etc/exports`
writes as `no_root_squash`:

```json
{"async":true,"client":"10.31.0.2","crossmnt":false,"insecure":true,
 "privilege":"rw","root_squash":"root",
 "security_flavor":{"kerberos":false,"kerberos_integrity":false,
                    "kerberos_privacy":false,"sys":true}}
```

Three things that make this safe to use, and are how phase 1 was done:

1. **`save` replaces the entire list**, exactly as the audit runbook warned. So
   never hand-write the rules you are keeping — `load` them, filter the ones you
   want gone programmatically, and `save` the result. The kept rules then come
   back byte-identical, which was verified by diffing against
   `/etc/exports.bak-2026-09-23` per rule.
2. **Prove the call shape idempotently first.** Writing a share's *current*
   rules back unchanged is a no-op that confirms the method name and JSON shape
   without risking anything. `/etc/exports` should be byte-identical afterwards.
3. **`rule='[]'` removes the export** entirely. That is how `docker` and the
   legacy `nextcloud-data` were retired.

Back up the table first: `cp -a /etc/exports /etc/exports.bak-<date>`. It is not
what DSM reads from, so it is a reference for diffing and not a rollback you can
restore — roll back by `save`-ing the original rule list.

**A `sudo -S` gotcha that will eat your output:** the `Password: ` prompt is
written without a trailing newline, so the first line of your script's output is
appended to it. A filter like `grep -v '^Password:'` then silently deletes that
first line. Use `sed 's/^Password: //'` instead.

## Before you start

- Open DSM on **both** units in separate browser tabs and stay logged in. Several
  steps below can cut the path you are working through.
- Keep an SSH session open on `5022` to each unit as a second way in.
- `smbstatus` and `testparm` are **not on `PATH`** — they live in
  `/usr/local/bin/`.
- Re-read the live export table first:

```bash
SYNO_USER="$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)"
export SSHPASS="$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)"
printf '%s\n' "$SSHPASS" | sshpass -e ssh -p 5022 "$SYNO_USER@scooter.myrobertson.net" 'sudo -S cat /etc/exports'
```

### The DSM ⇄ /etc/exports mapping

Everything below is DSM > **Control Panel** > **Shared Folder** > select the
share > **Edit** > **NFS Permissions**. Each rule row maps like this:

| DSM field | `/etc/exports` |
|---|---|
| Hostname or IP | the host/CIDR before the `(` |
| Squash: *No mapping* | `no_root_squash` — client root **is** root |
| Squash: *Map root to admin* | `root_squash` — only UID 0 remapped ← **target for data shares** |
| Squash: *Map all users to admin* | `all_squash` — **every** UID remapped |
| ☑ Allow connections from non-privileged ports | `insecure` |
| ☑ Enable asynchronous | `async` |

`insecure_locks` is on every DSM-generated rule and is **not** the `insecure`
finding — don't confuse them.

---

## Phase 0 — Auto Block allow list (prerequisite, do NOT enable Auto Block yet)

This is first because getting it wrong locks you out of both NAS units at once,
and both share one credential and one inventory group.

The playbook's defaults are 5 attempts / 5 minutes with `expire_day: 0`, which
means **a blocked address is never released automatically**.

On **each** unit: Control Panel > **Security** > **Account** > *Allow/Block List*
> **Allow List** > Create:

1. The Ansible control host address.
2. `192.168.1.0/24`.
3. The Tailscale interface range.

Do this on **both** units before enabling Auto Block on **either**. Do not enable
Auto Block in this phase — it comes at the very end, once everything else that
might cause repeated failed logins is done.

---

## Phase 1 — Free wins (no client can be affected) — **DONE 2026-09-23**

Applied via the `SharePrivilege` API above, not the UI. Scooter went from
**23 findings to 19**, and 10 exports to 8:

| Share | Before | After |
|---|---|---|
| `/volume1/docker` | 1 rule (`animal`) | **export removed** |
| `/volume1/nextcloud-data` (legacy) | 6 rules | **export removed** |
| `/volume1/NetBackup` | 8 rules | 7 (`animal` gone; the 7 `all_squash` rules untouched) |
| `/volume1/downloads` | 4 rules | 3 |
| `/volume1/unraid` | 2 rules | 1 (`kermit` only) |

`animal` occurrences in `/etc/exports`: **0**. Every kept rule diffed
byte-identical against the backup, `nextcloud-data-prod` stayed `Bound`,
Nextcloud pods stayed healthy, and 2049 kept serving. The four findings that
cleared were `docker` + `NetBackup` + `nextcloud-data` `no_root_squash`, and
`nextcloud-data` `insecure`.

Backup of the pre-change table: `/etc/exports.bak-2026-09-23` on scooter.

The original instructions are kept below for the record.

### Original UI steps

### 1.1 Delete the legacy `/volume1/nextcloud-data` export — Scooter

Verified 2026-09-23: **0 PVs bind it** in either cluster (prod binds
`nextcloud-data-prod`, staging binds `nextcloud-data-stage`). It carries six
rules, all `no_root_squash,insecure`.

Shared Folder > `nextcloud-data` > Edit > NFS Permissions > select each rule >
**Delete** (all six) > Save.

Re-confirm before deleting, since it is the one destructive-looking step:

```bash
kubectl --context admin@prod    get pv -o json | jq -r '.items[].spec.nfs.path' | grep -c '^/volume1/nextcloud-data$'
kubectl --context admin@staging get pv -o json | jq -r '.items[].spec.nfs.path' | grep -c '^/volume1/nextcloud-data$'
# both must print 0
```

### 1.2 Delete every `animal` rule — Scooter

`animal` does not resolve (confirmed 2026-09-23), so no client can be using it.
Every one of these is `rw,no_root_squash`:

| Share | Action |
|---|---|
| `docker` | delete the `animal` rule — it is the **only** rule, so the export goes away with it |
| `NetBackup` | delete the `animal` rule, keep the seven `all_squash` IP rules |
| `downloads` | delete the `animal` rule |
| `unraid` | delete the `animal` rule, keep `kermit` (resolves to 192.168.1.141) |

`/volume1/docker` is the export `showmount -e` never shows, so it is easy to
believe it doesn't exist. It does.

---

## Phase 2 — Narrow host lists (media shares; worst case a media outage)

**The trap in this phase:** replacing a wildcard while leaving the `/24` in place
narrows *nothing*. `10.31.0.1/24` already contains `10.31.0.2`. Delete the `/24`
rules, don't just add node IPs beside them.

Prod worker node IPs: `10.31.0.2`, `10.31.1.2`, `10.31.2.2`
Staging worker node IPs: `10.21.0.2`, `10.21.1.2`, `10.21.2.2`
Proxmox hosts: `192.168.1.241`, `192.168.1.242`, `192.168.1.243`

### 2.1 `/volume1/plex` — the widest export on the estate

Currently 17 rules. Do:

- **Delete** `10.21.0.0/16` — 65 534 addresses where three are wanted.
- **Delete** `10.31.0.1/24`, `10.31.1.1/24`, `10.31.2.1/24`.
- **Replace** `k8s-prod-worker-*` with the three prod node IPs.
- **Replace** `k8s-stg-worker-*` with the three staging node IPs.
- **Replace** `pve*` with the three Proxmox IPs.
- Keep `192.168.1.18`, `192.168.11.0/24`, `192.168.12.0/24`, `192.168.88.0/24`
  only if you know what uses them — each is a media client range.
- Untick **Allow connections from non-privileged ports** on the `10.21.*` and
  `k8s-stg-worker-*` rules (they carry `insecure`).

### 2.2 `/volume1/janice`

- **Replace** `pve*` with the three Proxmox IPs.
- The `10.31.*` rules are already `root_squash` — leave them.
- `192.168.10.0/24`, `192.168.11.0/24`, `192.168.88.0/24`, `192.168.1.242`,
  `192.168.1.243` are `no_root_squash`; see Phase 4.

### 2.3 `/volume1/radarr` and `/volume1/downloads`

- **Delete** `10.31.0.1/24`, `10.31.1.1/24`, `10.31.2.1/24` from both.
- **Add** the three prod node IPs in their place.
- `radarr` also carries `192.168.12.0/24`, `192.168.88.0/24`, `192.168.1.43`,
  `192.168.1.18` — keep only what you can account for.

**Verify after this phase**, before going near squash:

```bash
showmount -e 192.168.1.215     # remember: a LOWER BOUND, it hides unresolvable rules and /volume1/docker
```
and confirm Plex still plays a file and Radarr still sees its library.

---

## Phase 3 — Clear `insecure` (cheap, changes no UID)

`insecure` lets a client mount from an unprivileged source port. Combined with
`no_root_squash` it means a **non-root** local user on an allowed host can speak
NFS directly and assert UID 0. Clearing it changes no ownership and is the safe
half of the squash problem.

Untick **Allow connections from non-privileged ports** on every rule of:

- `/volume1/nextcloud-data-stage` (3 rules)
- `/volume1/nextcloud-data-prod` (3 rules)
- `/volume1/plex` (the `10.21.*` and `k8s-stg-worker-*` rules, if not already
  done in 2.1)

Do `-stage` first, restart a staging Nextcloud pod, confirm it starts, then do
`-prod`.

---

## Phase 4 — Squash (the risky one; prove on staging first)

> **`/volume1/nextcloud-data-prod` very likely depends on `no_root_squash`
> today.** The Nextcloud pods run `fsGroup: 33` with no `runAsUser`, so the
> image entrypoint starts as root and chowns the data directory. Moving to
> `root_squash` squashes that root to `anonuid=1025` and the entrypoint can
> fail. The failure is at **pod start**, not at read time — an already-running
> pod looks perfectly healthy while the change is broken.

Order, strictly:

1. `/volume1/nextcloud-data-stage` → Squash = **Map root to admin**. Save.
2. Restart a staging Nextcloud pod and watch it come up:
   ```bash
   kubectl --context admin@staging rollout restart deploy/nextcloud -n nextcloud
   kubectl --context admin@staging logs -n nextcloud -l app=nextcloud --tail=50 -f
   ```
   Confirm it can **write** as its own UID, not as root.
3. Only if staging survives, repeat on `/volume1/nextcloud-data-prod` and restart
   a prod pod the same way.
4. If prod genuinely needs remote root, **write that into the audit runbook as an
   accepted exception** rather than leaving it undocumented — that is a real
   outcome, not a failure.

Then the lower-risk shares: `radarr`, `downloads`, `unraid`, `janice`'s
`no_root_squash` rules, `plex` → **Map root to admin**.

**Do not** use *Map all users to admin* as a substitute. It rewrites every
client UID to the rule's `anonuid`, which changes the owner of newly written
files and is a data-ownership migration, not a hardening tweak. Note
`/volume1/NetBackup` already mixes `anonuid=1024` and `anonuid=1025`, so "the
anon user" is not one identity on this NAS — check the anonuid on the specific
rule.

NFS export changes take effect **immediately and drop in-flight mounts**. Do
this phase in a window.

---

## Phase 5 — Remove the unused iSCSI target (both units)

Verified 2026-09-23: `mapping_index: -1` (no LUN mapped), `max_sessions: 0`, no
ESTABLISHED connections on 3260, and **0 iSCSI PVs** across both clusters
(110 prod / 91 staging PVs checked). Nothing is behind it.

On **each** unit: **SAN Manager** > **Target** > select `Synology iSCSI Target` >
confirm **Mapped LUN** is empty and **Connected Sessions** is 0 > **Remove**.

- Scooter: `iqn.2000-01.com.synology:scooter.default-target.ea061292341` (target_id 2)
- Kermit: `iqn.2000-01.com.synology:kermit.default-target.3d35caf9bf1` (target_id 1)

Prefer removing over enabling CHAP: CHAP would require changing the Synology CSI
driver's `client-info`, which is a Kubernetes secret path and a bigger change
than deleting a target nothing uses.

---

## Phase 6 — Disable the rsync daemon (both units)

Discloses every shared-folder name and description pre-auth on TCP 873. Data
reads already fail (`@ERROR: auth failed` / `account system disabled`), so this
is disclosure, not access.

**Check first** that Hyper Backup doesn't use rsync transport to the peer.
Snapshot Replication uses **5566**, not 873, so it is unaffected — confirm on
the unit rather than assuming.

Control Panel > **File Services** > **rsync** > untick **Enable rsync service** >
Apply.

```bash
rsync rsync://192.168.1.215/    # must fail to connect
nmap -p873 192.168.1.215        # must report closed
```

---

## Phase 7 — Require SMB signing (both units)

> **Check for live sessions first.** On 2026-09-23 Kermit had an active session
> (`MYROBERTSON\rich` from `192.168.16.6`, SMB3_11) while Scooter had none.
> Applying this restarts the SMB service and drops sessions.

```bash
sudo /usr/local/bin/smbstatus -b     # full path; not on PATH
```

Control Panel > **File Services** > **SMB** > **Advanced Settings** > set server
signing to required (and consider **Transport encryption mode**). Apply.

Do Scooter first if it is idle. Verify:

```bash
nmap --script smb2-security-mode -p445 192.168.1.215
# expect: "Message signing enabled and required"
```

---

## Phase 8 — DSM firewall (both units) — do this LAST

Deliberately last and deliberately not scripted: a rule applied over SSH can
terminate the session that applied it and leave the unit unreachable.

Control Panel > **Security** > **Firewall**. Build the allow rules **first** and
set the default action to **Deny** only at the end, with a second browser session
held open.

Scooter's allow list — omitting the `192.168.{10,11,12,88}.0/24` ranges is the
easy mistake, and NFS failures surface as **hung mounts**, not errors:

| Source | Why it must stay |
|---|---|
| `192.168.1.0/24` | management, Proxmox `.241-.243`, ABB agents, DSM admin |
| `10.31.0.0/24`, `10.31.1.0/24`, `10.31.2.0/24` | prod k8s nodes |
| `10.21.0.0/24`, `10.21.1.0/24`, `10.21.2.0/24` | staging k8s nodes |
| `192.168.88.0/24` | `plex`, `radarr`, **`NetBackup`** |
| `192.168.12.0/24` | `plex`, `radarr` |
| `192.168.11.0/24` | `plex`, `janice` |
| `192.168.10.0/24` | `janice` |
| Tailscale interface | remote path |
| Ansible control host | audit/remediation over SSH 5022 |

Also allow the **DSM HTTPS API port from the prod node subnets** — the Synology
CSI controller talks to DSM at `192.168.1.215` and is not an NFS client, so an
NFS-only allow list breaks volume provisioning.

Note: `enable_firewall: false` still produces `filtered` rather than `closed`
from nmap, because DSM's `DOS_PROTECT` chain rate-limits outbound RSTs. **Do not
read `filtered` as "a firewall is on".**

---

## Phase 9 — Remove the Synology DDNS record (Scooter)

Control Panel > **External Access** > **DDNS** > remove the `*.synology.me`
record. Confirm no client config points at that name first.

---

## Phase 10 — Enable Auto Block (both units) — only now

Phase 0's allow list must already be populated on **both** units.

Control Panel > **Security** > **Protection** > enable Auto Block.

Or, once the allow list exists, via the playbook:

```bash
ansible-playbook -i inventory/environments/production.ini \
  ansible/synology/remediate_share_access_exposure.yml \
  -e "ansible_user=${SYNO_USER}" -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}" \
  -e synology_autoblock_apply=true
```

It is last because everything above involves repeated authentication, and a
mistyped credential five times over is a permanent block with `expire_day: 0`.

---

## Not in this list, and why

- **QuickConnect** — still enabled on both. Tailscale is currently **stopped** on
  both units, so disabling QuickConnect today removes remote access rather than
  replacing it. Restore a remote path first.
- **Finding 0** — already closed 2026-09-23 by gating
  `kermit.myrobertson.com` behind `keycloak-authproxy` (homelab_flux `bc66189`).
  Removing the route outright is still the stronger fix and needs the OIDC origin
  swap described in the audit runbook.

## Closing out

Re-run the audit against **both** units after each phase, not just at the end:

```bash
ansible-playbook -i inventory/environments/production.ini \
  ansible/synology/audit_share_access_exposure.yml \
  -e "ansible_user=${SYNO_USER}" -e "ansible_password=${SYNO_PASS}" \
  -e "ansible_become_password=${SYNO_PASS}" \
  -e synology_audit_fail_on_finding=false
```

Drop `synology_audit_fail_on_finding=false` for the final run — a clean estate is
one where both hosts report an empty findings list with the gate at its default.
Remember Kermit has no NFS exports, so a Kermit-only run exercises none of the
export checks and will look far cleaner than the estate is.
