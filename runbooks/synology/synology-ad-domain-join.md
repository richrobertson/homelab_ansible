# Synology AD domain join — scooter and kermit

Written 2026-09-22 while sweeping for clients broken by the AD CS trust-anchor
change (see [ldaps-certificate-renewal-failing.md](../domain/ldaps-certificate-renewal-failing.md)).
Both NAS units turned out to be **unaffected**, but the investigation found a
single-DC dependency on scooter that has now been fixed.

## They are domain-joined, NOT LDAP directory clients

This is the fact that matters, and it is easy to get wrong because DSM presents
"Domain/LDAP" as one settings page.

```
/etc/samba/smb.conf   security=ads   realm=MYROBERTSON.NET   workgroup=MYROBERTSON
/usr/syno/etc/ldapclient/            EMPTY on both (dirs dated 2021/2022, never populated)
/usr/syno/etc/private/adsinfo        LDAP_port="389"
```

Domain join authenticates over **Kerberos and SMB**, and the directory lookups
run over plain LDAP on 389 with Kerberos sign/seal. Nothing pins the AD CS root,
so replacing the CA could not break them — and did not. Confirmed live on
2026-09-22, with nested group membership resolving from a real DC query:

```
id 'MYROBERTSON\svc-syno-admin'
  uid=2088767497(MYROBERTSON\svc-syno-admin)
  groups=...(MYROBERTSON\Domain Users),...(MYROBERTSON\GG-Synology-DSM-Administrators)
```

**Do not go looking for an LDAPS CA bundle on these units.** There isn't one.

## scooter was pinned to a single DC (fixed 2026-09-22)

scooter had rhonda's IP hardcoded in three places; kermit used the realm name
and therefore DNS SRV discovery. AD DNS advertises two live DCs:

```
_ldap._tcp.dc._msdcs.myrobertson.net
  0 100 389 dc1.myrobertson.net.      192.168.1.3
  0 100 389 rhonda.myrobertson.net.   192.168.1.245
```

Changed on scooter to match kermit:

| file | before | after |
|---|---|---|
| `/etc/samba/smb.conf` | `password server=192.168.1.245` | `password server=MYROBERTSON.NET` |
| `/etc/krb5.conf` | `kdc = 192.168.1.245` | `kdc = MYROBERTSON.NET` |
| `/etc/krb5.conf` | `kpasswd_server = 192.168.1.245:464` | `kpasswd_server = MYROBERTSON.NET:464` |

Backups: `*.bak-2026-09-22` next to each file. Applied with
`smbcontrol all reload-config` (no restart, no dropped sessions); domain
lookups verified working immediately after.

`kpasswd_server` is the one most likely to be forgotten — it is the endpoint for
**machine account password changes**, which happen every 30 days. Left pinned,
scooter's machine account renewal would depend on rhonda alone even after the
other two were fixed.

### Verifying failover capability without an outage

You cannot easily prove failover without taking a DC down, but you can prove
scooter has everything it needs:

```bash
nslookup -type=SRV _ldap._tcp.dc._msdcs.myrobertson.net   # expect BOTH DCs
for p in 389 88 445; do (echo > /dev/tcp/192.168.1.3/$p) && echo "dc1:$p ok"; done
```

Note that `netstat` will still show an established connection to
`192.168.1.245:389` after the change. That is correct — winbind keeps a healthy
DC connection and only re-selects when it fails. The config change governs what
happens *then*.

## Two stale artifacts found, deliberately left alone

- **kermit caches a dead DC.** `/usr/syno/etc/private/kdc_ip` lists
  `192.168.1.245:389` and `192.168.1.244:389`. **`.244` does not exist** — no
  ping, nothing listening on 88/389/636, no reverse DNS. scooter has no
  `kdc_ip` file at all. Harmless while `dns_lookup_kdc = true`, but do not read
  that file as a list of real DCs.
- **`testparm` reports `ERROR: Invalid idmap range for domain *!`** on both
  units, and on scooter's pre-change backup. It is pre-existing DSM behaviour
  (DSM manages idmap outside `smb.conf`), **not** a symptom of a config edit.
  Check it against the backup before chasing it.

## This may revert on a DSM update

`/etc/samba/smb.conf` was last rewritten wholesale on **2026-07-07** on both
units, which matches a DSM update rather than any domain change — note that
kermit's `adsinfo` changed on 2026-08-12 and its `smb.conf` was *not*
regenerated, so `smb.conf` is not derived from `adsinfo`. The source DSM
regenerates from was not identified.

**So re-check after every DSM upgrade:**

```bash
grep "password server" /etc/samba/smb.conf     # expect MYROBERTSON.NET
grep -E "kdc|kpasswd_server" /etc/krb5.conf    # expect MYROBERTSON.NET
```

## Access

SSH is on port **5022**, with the Vault-backed local account:

```bash
U=$(vault kv get -field=username -mount=secret synology/dsm-admin/local-ssh-account)
export SSHPASS=$(vault kv get -field=password -mount=secret synology/dsm-admin/local-ssh-account)
sshpass -e ssh -p 5022 "$U@scooter.myrobertson.net"
```

`sudo` requires the same password and there is no TTY, so feed it on stdin —
never in argv:

```bash
printf '%s\n' "$SSHPASS" | sshpass -e ssh -p 5022 "$U@host" "sudo -S sh /tmp/script.sh"
```

Gotchas: `net`, `wbinfo` and `testparm` are not on `PATH`; `testparm` lives at
**`/usr/local/bin/testparm`** and `smbcontrol` at `/usr/local/bin/smbcontrol`.
`ps w` does not show samba processes — enumerate `/proc/*/comm` instead.
