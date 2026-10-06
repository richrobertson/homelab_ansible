# Domain controllers as cattle: rearm and rebuild on evaluation media

There are no Windows Server licences. Every domain controller runs **Windows
Server Evaluation**, which gives about 180 days per window and a fixed number of
rearms. An expired evaluation **shuts Windows down every 60 minutes** and HA
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
verifies SYSVOL/NETLOGON, site and RODC flag -> destroys the old disks.

Afterwards, run the playbooks that configure a DC once it exists. The final
message lists them:

```sh
ansible-playbook -i $INV ansible/domain/configure_windows_dhcp_ha.yml      # DHCP partner only
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

## dc1 and the CA (not codified yet)

dc1 hosts the Enterprise CA `myrobertson-DC1-CA-1` and its IIS enrolment
services, and the playbook refuses it. Its config string is
`dc1.myrobertson.net\myrobertson-DC1-CA-1`, so the plan is to rebuild **under
the same hostname** and restore the CA there. Every consumer, including the
Terraform AD CS provider, keeps working unchanged. Steps to codify:

1. `certutil -backupDB` + `certutil -backupKey` + export
   `HKLM\SYSTEM\CurrentControlSet\Services\CertSvc\Configuration`, with the key
   backup password and files escrowed off-box.
2. Remove the CA role, then rebuild through this playbook.
3. Install AD CS with the existing key (`Install-AdcsCertificationAuthority
   -CertFile ... -KeyContainerName`), restore the database and registry, and
   reinstall the web enrolment roles.
4. Set `spec_verified: true` on dc1 only after checking its NICs against the
   live guest.

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
