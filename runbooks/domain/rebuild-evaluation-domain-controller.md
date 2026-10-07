# Domain controllers as cattle: rearm and rebuild on evaluation media

There are no Windows Server licences. Every domain controller runs **Windows
Server Evaluation**. A fresh Server 2025 evaluation install gives **180 days and
1 rearm**, so roughly a year per build (measured on the dns01 rebuild,
2026-10-06). An expired evaluation **shuts Windows down every 60 minutes** and HA
starts it again, indefinitely. dc1 and dns01 did that from 2026-09-29.

So no DC is precious. Each one is declared in code, its evaluation is rearmed
while rearms last, and it is rebuilt on fresh media when they run out. AD
replication repopulates a rebuilt DC from the survivors, so nothing on the old
disk needs to be kept.

| Piece | File |
|---|---|
| What each DC is (VM, NICs, RODC or writable, DHCP/CA role) | `ansible/domain/vars/domain_controller_specs.yml` |
| Rearm before expiry, and say when a rebuild is due | `ansible/domain/rearm_domain_controller_eval.yml` |
| Rebuild one DC in place | `ansible/domain/rebuild_domain_controller.yml` |
| Unattended install | `ansible/domain/templates/autounattend.xml.j2`, `dc-bootstrap.ps1.j2` |

## Environment

```sh
cd ~/homelab/homelab_ansible
source ~/.config/vault/token.env
export VAULT_ADDR=https://vault.myrobertson.net:8200 OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
INV=inventory/environments/production.ini
```

## Routine: rearm

Report on every DC (read-only):

```sh
ansible-playbook -i $INV ansible/domain/rearm_domain_controller_eval.yml
```

Rearm any DC within 21 days of expiry, one at a time with a reboot each:

```sh
ansible-playbook -i $INV ansible/domain/rearm_domain_controller_eval.yml -e dc_rearm_confirm=true
```

If a DC is due and has **no rearms left**, the play fails and names the rebuild
command. That failure is the trigger for a rebuild, not a fault.

Never reboot a DC with `qm shutdown` or `qm reboot`. They are HA resources, so
that becomes an HA stop. The playbook reboots from inside the guest.

## Rebuild one DC

Run the preflight first. It is read-only and checks the spec, the VM's MACs, the
ISOs, FSMO roles and the surviving DCs:

```sh
ansible-playbook -i $INV ansible/domain/rebuild_domain_controller.yml -e dc_rebuild_target=dns01.myrobertson.net
```

Then run it for real. Allow about 1-2 hours; Windows Setup alone takes 15-40
minutes.

```sh
ansible-playbook -i $INV ansible/domain/rebuild_domain_controller.yml \
  -e dc_rebuild_target=dns01.myrobertson.net -e dc_rebuild_confirm=true
```

What it does: escrows new DSRM and bootstrap passwords in Vault
(`secret/windows/domain/dsrm/<host>`, `.../bootstrap-admin/<host>`) ->
removes the DHCP failover relationship if the target is the partner -> demotes
the DC -> deletes its computer object from a survivor -> HA-stops the VM,
detaches its disks and creates blank ones -> boots the evaluation ISO with a
rendered autounattend -> waits for WinRM -> installs roles and promotes ->
verifies SYSVOL/NETLOGON, site and RODC flag -> for the DHCP partner,
re-authorises it under its current IP and re-creates the failover relationship
(hot standby, every scope, NORMAL checked) -> destroys the old disks.

The first dns01 rebuild took about 1h45 end to end: Setup ~45 minutes, plus
~12 minutes of demotion pre-checks.

### Resuming a run that stopped

If a run stops after the reimage (laptop closed, a failure during promotion),
do NOT re-run it plainly. The preflight refuses because the old disks are still
`unusedN`, and new passwords would not match the install. Resume instead:

```sh
ansible-playbook -i $INV ansible/domain/rebuild_domain_controller.yml \
  -e dc_rebuild_target=dns01.myrobertson.net -e dc_rebuild_confirm=true -e dc_rebuild_resume=true
```

This reads the passwords back from Vault, skips everything up to and including
the reimage, and skips promotion if the domain already lists the DC. Re-running
it when nothing is left to do changes nothing on the servers.

Afterwards, run the playbooks that configure a DC once it exists. The final
message lists them:

```sh
ansible-playbook -i $INV ansible/domain/fix_dc_time_drift_windows.yml
ansible-playbook -i $INV ansible/domain/configure_windows_winrm.yml
ansible-playbook -i $INV ansible/proxmox/guest_fstrim.yml
ansible-playbook -i $INV ansible/synology/configure_windows_activebackup_agent.yml
```

### A DC that is dead, not just expired

Add `-e dc_rebuild_skip_demote=true`. The cleanup step then also removes the
server object under Sites and, for an RODC, its `krbtgt_NNNNN` account. That is
metadata cleanup, so only use it when the old DC will never return.

### Refusals and what they mean

- **spec not verified**: the spec has not been checked against the live guest.
  Compare `nics` with `Get-NetIPConfiguration` and `qm config`, then set
  `spec_verified: true`.
- **hosts_ca**: see dc1 below.
- **DHCP primary**: the primary owns the scope definitions. Move the primary
  role first.
- **FSMO roles / last writable DC**: transfer the roles first, and never rebuild
  the last writable DC.
- **MACs do not match / unusedN disks present**: the spec points at the wrong
  VM, or an earlier run stopped halfway. See rollback below.

## Rollback of a half-finished run

Until the final play, the old disks sit on the VM as `unused0..2`. If the new
install fails:

```sh
qm config <vmid>        # find unusedN = old scsi0, efidisk0, tpmstate0
qm set <vmid> --delete scsi0,efidisk0,tpmstate0     # new blank disks become unusedN too
qm set <vmid> --scsi0 <old scsi volume>,discard=on,iothread=1 \
              --efidisk0 <old efi volume>,efitype=4m,ms-cert=2023w,pre-enrolled-keys=1 \
              --tpmstate0 <old tpm volume>,version=v2.0 --boot order=scsi0
```

If the demotion already ran, the old disk boots as a demoted member server.
That is still useful for its data, but it is not a DC. A finished re-run is
usually the better way forward.

## dc1 and the CA

dc1 hosts **myrobertson-DC1-CA-1**, an Enterprise **Root** CA that signs Vault's
`pki_int`, `pki_int_prod` and `pki_int_staging` intermediates. It is the root of
the internal PKI. The rebuild handles it, but only with a fresh backup and its
own confirmation flag:

```sh
ansible-playbook -i $INV ansible/domain/backup_adcs_ca.yml          # minutes before, every time
ansible-playbook -i $INV ansible/domain/rebuild_domain_controller.yml \
  -e dc_rebuild_target=dc1.myrobertson.net -e dc_rebuild_confirm=true -e dc_rebuild_ca_confirm=true
```

The preflight requires a `complete` Vault backup under 12 hours old, with its
recorded CA thumbprint and its local archive present. On a resume it accepts an
older backup, since that is the one taken before the CA was removed.

**Backup** (`backup_adcs_ca.yml`, non-destructive): it publishes a fresh CRL, then
backs up the database and key of the active CA. It also exports the OLD
`myrobertson-DC1-CA` keys. That CA is still trusted in AD and NTAuth, and
PowerShell cannot see its certificates; only certutil can. Every PFX is
re-opened to prove its key is inside. The key material, registry config and
template list go to Vault at `secret/windows/domain/adcs/<CA>/backup`. The full
archive, database included, goes to `~/homelab-backups/adcs/` and to
`kermit:/volume1/NetBackup/adcs-ca` (owner-only, newest 30 kept).

**Removal** (`tasks/adcs_ca_remove.yml`, before demotion): unconfigures CES, CEP,
Web Enrollment and the CA, then removes the role features, which demotion
requires. AD objects are deliberately left in place.

**The computer account is kept for a CA host.** The CDP container and Cert
Publishers grant rights to DC1$'s SID, and rejoining under the same name reuses
the account.

**Restore** (`tasks/adcs_ca_restore.yml`, after the new DC verifies):
- Re-imports the old CA keys.
- Installs the Enterprise Root CA from the backed-up key with
  `-OverwriteExistingCAinDS`.
- Restores the database and registry config (once only, guarded by a marker
  file), sets the exact template list, restores CertEnroll and publishes a CRL.
- Reinstalls Web Enrollment, CES and CEP on the CA certificate.
- Verifies that the thumbprint matches the backup, the CA answers `certutil
  -ping`, the templates match, the database has rows and the CRL is served over
  http.

**Identities:** the AD CS steps run as **Administrator**, the only Enterprise
Admin, from `secret/windows/domain/domain_admin` via runas. svc-ansible-win is
only a Domain Admin.

**Outage window:** from removal until the restore finishes (~2h), the CA issues
nothing and `http://dc1/CertEnroll` is down. The LDAP CDP keeps serving the CRL
the backup published, which is valid for 1 week.

**If the restore fails:**
- **Before the old disks are destroyed:** the final play only runs after
  everything verifies, so the old disk is still on the VM as `unusedN`. Roll
  back as above. The old disk has the CA role removed and is demoted, so re-run
  the restore steps there by hand from the same archive.
- **Otherwise:** fix the cause and resume with `-e dc_rebuild_resume=true`. The
  restore steps are idempotent.

## Install details worth knowing

- **ISO:** Windows Server 2025 Evaluation, build 26100.32230, at
  `local:iso/WinServer2025-Eval-26100.32230.iso` on pve4. `install.wim` index 4 =
  Datacenter Evaluation (Desktop Experience), matching the previous builds.
  Check the index again for any new ISO.
- **Disk drivers:** the disk is virtio-scsi, so Setup loads `vioscsi\2k25` from
  `virtio-win-stable.iso`. If Setup cannot see a disk, the driver folder name is
  wrong for the build.
- **"Press any key to boot from CD":** the playbook sends Enter for 40 s after
  the VM starts. If the console sits in the UEFI shell, start the VM and press a
  key yourself.
- **NICs:** autounattend matches NICs by MAC, never by name.
  `register_dns: false` keeps dns01's 192.168.88.x management address out of
  the AD zone, as before.
- **dns01's second gateway:** the old build had a default gateway on
  192.168.88.1 as well. The spec drops it, because a DC with two default routes
  picks its reply path by metric.

## Lessons from the first real run (dns01, 2026-10-06)

Each of these is fixed in the code. They are written down so nobody "simplifies"
the fix away.

- **Demotion vs the hourly shutdown:** the demotion's pre-checks took ~12 minutes
  (an ~11 s DNS timeout per reverse zone) and finished one minute before the
  expired evaluation shut down. The play now waits for the hourly restart if
  the guest has been up more than 30 minutes.
- **Never reboot through the bootstrap identity after promotion:** promotion
  deletes local accounts, so `reboot: true` on the promote step can never log
  back in. The playbook fires `shutdown /r` instead and reconnects as the domain
  identity.
- **Guest agent:** `qemu-ga` alone never starts without the virtio-serial
  driver. bootstrap.ps1 installs `virtio-win-gt-x64.msi` first.
- **DHCP failover needs runas and `-Force`:** a WinRM session cannot reach the
  partner's DHCP RPC (double hop: "Failed to get version of the DHCP server"),
  and the cmdlet prompts for confirmation.
- **Stale DHCP authorisation:** dns01 was still authorised in AD as .244,
  months after moving to .101. The play removes stale entries for the name.

## Lessons from the dc1 + CA run (2026-10-06)

The CA came back as the same CA: thumbprint `5C98DAA0...3F18`, 121 issued rows,
the five original templates, and http CRL/AIA. All three Vault intermediates
still verify against it. Getting there exposed these, and each is fixed in code:

- **Evaluation window:** the wait-for-a-fresh-hour loop polled over WinRM, and a
  refused connection during the hourly shutdown is a hard failure, not a retry.
  It now asks the Proxmox guest agent (`tasks/wait_eval_window.yml`). It also
  runs before the CA removal, which took ~15 minutes of feature removal.
- **Boot order:** set in the same `qm set` as the new CD drives, it did not
  stick; the VM booted virtio ISO -> PXE -> ... and the CD-prompt keypresses
  missed. It is now its own call, and the playbook checks it.
- **SAN policy:** WinPE brought the virtio-scsi (SAS) disk up "Offline
  (Policy)", read-only, so DiskConfiguration failed with 0x80070013 and Setup
  dropped to the manual disk page. autounattend now sets `SanPolicy=1` and runs
  diskpart `online disk` in RunSynchronous before disk configuration. (This
  time it was fixed by hand in Shift+F10.)
- **Promotion result:** dcpromo exit 4 (success with non-critical failures,
  e.g. no DNS delegation) is reported as failed by the module. Success is now
  judged by the domain listing the DC. A resume restarts a promotion that is
  still pending.
- **`-OverwriteExistingCAinDS`** is not valid with `-CertFile`; installing from
  the backed-up certificate re-attaches to the existing AD objects anyway.
- **reg.exe writes success to STDERR,** which PowerShell turned into a failure.
  It now runs as a process and the exit code is checked.
- **`CertUtil` is not a template.** certutil's "CertUtil: -CATemplates command
  completed successfully." footer was parsed into the backed-up template list.
  Excluded on backup, and ignored on restore for older backups.
- **Re-imported old-CA keys get GUID container names**
  (`myrobertson-DC1-CA-<guid>`). Backups after the restore still find all five.
