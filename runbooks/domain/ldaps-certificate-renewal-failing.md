# LDAPS certificate renewal is failing on all three domain controllers

**Found 2026-09-17. Deadline 2026-10-08.** Nothing has been changed. This
records the diagnosis and what a fix has to do.

## The deadline

All three DCs present a `DomainController`-template certificate expiring on the
**same day**:

| DC | expires | days left at discovery |
|---|---|---|
| rhonda.myrobertson.net | 2026-10-08 09:51 | 20.9 |
| dc1.myrobertson.net | 2026-10-08 15:00 | 21.1 |
| dns01.myrobertson.net | 2026-10-08 16:37 | 21.2 |

When they lapse, every LDAPS bind in the estate fails at once - Vault's LDAP
auth method, Nextcloud's directory bind, Keycloak, Synology DSM on scooter and
kermit, and the Proxmox VE realm. There is no staggering to soften it.

## It is already failing, not merely pending

The DCs are attempting renewal and being refused, every eight hours:

```
Event 64, Microsoft-Windows-CertificateServicesClient-AutoEnrollment, dc1
  9/17/2026 8:30:11 AM   Certificate for local system with Thumbprint 88 2b c7 b2 ...
                         is about to expire or already expired.
  9/17/2026 12:30:11 AM  (same)
  9/16/2026 4:30:12 PM   (same)
  ... three times a day, at least since 2026-09-15
```

The `DomainController` template has `validity=365d` and
`renewal_overlap=42d`, so autoenrollment entered its renewal window around
**2026-08-27** and has been failing ever since. Roughly three weeks of retries
have already been logged and discarded.

## Why it fails

`myrobertson-DC1-CA-1` publishes four templates, and no DC template is among
them:

```
certutil -CATemplates
  WebServer                               : Web Server
  SubordinateCertificationAuthority-Vault : Subordinate Certification Authority-Vault
  SubCA                                   : Subordinate Certification Authority
  Machine                                 : Computer
```

Confirmed against AD directly - the `certificateTemplates` attribute on
`CN=myrobertson-DC1-CA-1,CN=Enrollment Services,CN=Public Key Services,CN=Services,CN=Configuration,DC=myrobertson,DC=net`
lists exactly `Machine`, `SubCA`, `SubordinateCertificationAuthority-Vault`,
`WebServer`.

The DCs are asking for renewal of a template their CA does not offer.

**The CA was reinstalled.** Compare the issuer on the live certificates with the
CA that is running now:

```
live DC certs issued by : CN=myrobertson-DC1-CA          <- no suffix
CA currently running    : myrobertson-DC1-CA-1           <- "-1"
```

AD CS appends `-1` when a CA is installed under a name that already exists in
AD. The certificates in use were issued by the PREVIOUS instance; the
replacement was never configured to publish the DC templates. Everything else it
issues - `WebServer`, `Machine`, the Vault subordinate - works, which is why
nothing else has complained and why this went unnoticed for three weeks.

## What the monitoring did and did not do

`ProbeTLSCertificateExpiringSoon` fired for `rhonda:636` at 20.9 days on
2026-09-17, and `dc1`/`dns01` crossed the same 21-day threshold within hours.
That is working as designed.

But the threshold sits **inside** the 42-day renewal window. By the time it
fires, renewal has already been failing for three weeks. For certificates whose
renewal is driven by a window wider than the alert threshold, the alert is a
deadline warning, not a renewal-failure signal. The Vault Agent tier in
`homelab_flux/infrastructure/configs/certificate-expiry-alerts.yaml` gets this
right - its thresholds are deliberately set *below* the measured renewal point
so that firing means renewal genuinely did not happen. The AD CS tier has no
equivalent.

**Worth adding alongside the fix:** an alert on autoenrollment Event 64, or on
`certificate age > (validity - renewal_overlap)`, which would have caught this on
2026-08-27 instead of 2026-09-17.

## Other findings from the same investigation

- **A second CA object exists**: `myrobertson-RHONDA-CA`, publishing no
  templates at all. Establish what it is before changing CA configuration.
- **DC1 has no autoenrollment GPO.** `HKLM:\SOFTWARE\Policies\Microsoft\Cryptography\AutoEnrollment`
  is absent on dc1, while rhonda and dns01 both have `AEPolicy=7` (enabled +
  renew-expired + update-templates). dc1 still logs Event 64, so it is
  attempting renewal, but it is not configured the same way as its peers and
  should not be assumed to behave the same once the template is published.
- **Expired certificates are accumulating in `LocalMachine\My`** on every DC,
  from two retired issuers - `CN=myrobertson.net Intermediate Authority` and
  `CN=Vault MyRobertson.net Intermediate CA`, both expired 2026-05-24/25
  (-116 days). Not currently harmful, since Windows selects a valid leaf, but
  they are cruft and they make `Get-ChildItem Cert:\LocalMachine\My` harder to
  read during exactly this kind of incident.

## What a fix has to do

Not yet done - this is a change to the enterprise root CA and wants a deliberate
window.

1. **Decide the template family.** `KerberosAuthentication` supersedes both
   `DomainController` and `DomainControllerAuthentication` and is the modern
   choice. All three already exist in AD with `validity=365d`,
   `renewal_overlap=42d`. Switching family on live DCs is not a like-for-like
   swap - the DCs currently hold `DomainController` certs, so decide whether to
   republish the legacy template (minimal change, renews what is already there)
   or move to `KerberosAuthentication` (better, but a migration).
2. **Publish it on `myrobertson-DC1-CA-1`**, e.g.
   `certutil -SetCATemplates +KerberosAuthentication` or via the Certification
   Authority MMC. Verify with `certutil -CATemplates`.
3. **Grant `Domain Controllers` Enroll and Autoenroll** on the template ACL.
   Without Autoenroll the DCs will still not renew unattended.
4. **Force a renewal** rather than waiting 8 hours:
   `certutil -pulse` on each DC, then confirm a new leaf:
   `Get-ChildItem Cert:\LocalMachine\My | ? { $_.Subject -match $env:COMPUTERNAME }`
5. **Verify LDAPS actually serves the new certificate** - the store having it is
   not the same as 636 presenting it. From outside:
   `openssl s_client -connect <dc>:636 -showcerts` and check `notAfter` moved.
   Restart of the DC is normally not required; Schannel picks up the new cert,
   but confirm rather than assume.
6. **Re-check all three.** They fail together and must be confirmed together.

## Related

`svc-tf-adcs` needed a `Read` ACE on this same CA on 2026-09-16 to unblock
terraform - see `ansible/domain/configure_adcs_reader_permissions.yml`. That was
a permissions gap on a CA whose published-template set is also minimal. Both are
consistent with a CA that was rebuilt and configured only as far as the thing
being worked on at the time required.
