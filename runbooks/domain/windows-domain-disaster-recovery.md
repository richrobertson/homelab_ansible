# Windows domain, DNS, and DHCP disaster recovery

## Scope and topology

This runbook covers the `MYROBERTSON` Windows domain and its infrastructure
roles as declared in `inventory/environments/production.ini`:

- Domain controllers: `dc1.myrobertson.net`, `rhonda.myrobertson.net`
- DHCP primary: `rhonda.myrobertson.net`
- DHCP failover partner: `dns01.myrobertson.net`
- RDP/member server: `janice.myrobertson.net`

Synology ABB on Kermit provides machine recovery points. The enrollment
playbook is `ansible/synology/configure_windows_activebackup_agent.yml`.
Credentials come from `secret/windows/domain/ldap`; never place passwords in
this document or an incident record.

DSRM credentials must be escrowed separately from normal domain credentials at
`secret/windows/domain/dsrm/dc1` and `secret/windows/domain/dsrm/rhonda` (or an
approved successor path). Treat a missing path or an untested credential as a
recovery blocker; this runbook does not claim those secrets currently exist.

## Recovery rules

- If one healthy domain controller survives, prefer rebuilding or restoring a
  failed peer non-authoritatively so AD replication supplies current state.
- Do not reconnect a restored domain controller until its recovery mode and AD
  replication strategy are confirmed. This avoids duplicate identities and
  directory divergence.
- An authoritative forest recovery is required only when no trustworthy domain
  controller remains or when an authoritative object/SYSVOL recovery is
  explicitly selected.
- Restore DNS before services that depend on AD-integrated zones. Restore DHCP
  only after proving the surviving partner's lease state and failover role.

## 1. Capture and classify

From a healthy Windows management host:

```powershell
Get-ADDomainController -Filter * |
  Select-Object HostName,Site,IsGlobalCatalog,OperationMasterRoles
repadmin /replsummary
repadmin /showrepl * /csv
dcdiag /e /c /v
Get-DnsServerZone
Get-DhcpServerv4Failover
Get-DhcpServerv4Scope
```

Record FSMO role holders, Global Catalog/DNS roles, replication health, DHCP
failover state, last known good time, and the ABB recovery point for each host.

## 2. Recover one failed domain controller when another survives

Preferred path:

1. Seize FSMO roles only if the failed holder will not return.
2. Remove stale AD metadata and DNS records only after declaring the old DC
   permanently offline.
3. Rebuild Windows Server, join the domain, install AD DS/DNS, and promote it.
4. Allow SYSVOL and AD-integrated DNS to replicate from the healthy DC.
5. Verify replication, shares, time, and DNS before returning client traffic.

If bare-metal ABB restoration is required, restore the selected image while
isolated. Boot Directory Services Restore Mode (DSRM) and use Windows Server
Backup/AD recovery procedures appropriate to the restored system-state age.
Reconnect only after confirming the restore will be non-authoritative and the
surviving DC is the replication source.

Validation:

```powershell
repadmin /replsummary
dcdiag /test:Advertising /test:Services /test:DNS /e /v
net share | findstr /I "SYSVOL NETLOGON"
w32tm /query /status
```

## 3. Recover the forest when all domain controllers are lost

This is a controlled forest-recovery event.

1. Select the newest trusted system-state/bare-metal recovery point that
   predates the incident.
2. Isolate the recovery network and restore the first DC that held the most
   complete DNS/GC/FSMO role set.
3. Boot into DSRM and perform the supported non-authoritative system-state
   restore. Mark SYSVOL authoritative only for the first recovered DC when the
   forest-recovery procedure requires it.
4. Verify AD DS, SYSVOL, NETLOGON, DNS, time, and FSMO ownership before adding
   another DC.
5. Rebuild or restore remaining DCs one at a time as non-authoritative replicas.
6. Reset credentials for privileged/service accounts when compromise is in
   scope, then repair dependent services.

The DSRM credential for every DC must be escrowed outside AD in the paths above
and tested during a scheduled drill. If it is unavailable
while a DC is healthy, reset it with the supported `ntdsutil` procedure before
an incident. Never print or commit it.

## 4. Recover DNS

AD-integrated zones recover with AD DS. After the first DC is healthy:

```powershell
Get-DnsServerZone
Get-DnsServerResourceRecord -ZoneName myrobertson.net
Resolve-DnsName _ldap._tcp.dc._msdcs.myrobertson.net -Type SRV
dcdiag /test:DNS /e /v
```

Restore non-AD-integrated zones from their authoritative export/source of truth.
Do not overwrite a newer AD-integrated zone with a flat-file copy.

## 5. Recover DHCP and failover

If one DHCP partner survives, keep it authoritative while the failed partner is
rebuilt. Verify leases and failover status before changing mode:

```powershell
Get-DhcpServerv4Failover
Get-DhcpServerv4Scope
Get-DhcpServerv4Lease -ScopeId <scope>
```

Export a healthy configuration before repair:

```powershell
Export-DhcpServer -ComputerName <healthy-server> \
  -File C:\Windows\Temp\dhcp-export.xml -Leases -Force
```

On a clean/replacement DHCP server, install and authorize the role, import the
reviewed export, then recreate or repair failover deliberately:

```powershell
Import-DhcpServer -ComputerName <replacement-server> \
  -File C:\Windows\Temp\dhcp-export.xml -BackupPath C:\Windows\Temp\dhcp-backup \
  -Leases -Force
Get-DhcpServerInDC
Get-DhcpServerv4Failover
```

Do not place both partners in partner-down mode or import stale leases into a
healthy active partner.

## 6. Restore member servers

Use ABB file-level restore for narrow loss and bare-metal restore for system
loss. Restore into isolation when the original hostname/IP may still exist.
After boot, verify domain trust, time, DNS, application services, and the ABB
agent. Reset the machine account secure channel if required:

```powershell
Test-ComputerSecureChannel -Verbose
```

## 7. Final validation

- At least two domain controllers advertise and replicate successfully.
- SYSVOL and NETLOGON are shared on every DC.
- FSMO roles, Global Catalog, DNS, and time hierarchy are documented and healthy.
- AD-integrated DNS resolves domain SRV records internally.
- DHCP scopes, leases, options, authorization, and failover are healthy.
- Keycloak/LDAP federation and representative domain logins succeed.
- DC1, RHONDA, DNS01, and JANICE have fresh successful ABB recovery points.
- A post-recovery ABB backup completes and a file-level test restore is verified.
