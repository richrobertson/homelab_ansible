# LDAPS certificate renewal failed on all three domain controllers (resolved)

**Found and FIXED 2026-09-17.** All three DCs now serve 365-day certificates
from the current CA. Kept because the failure was silent for three weeks, the
cause is non-obvious, and two steps in the fix are easy to get wrong.

**The fix was one command on the CA:**

```
certutil -SetCATemplates +DomainController
```

Everything else below is why that was the command, and what verifying it
properly required.

## The deadline that was

All three DCs presented a `DomainController`-template certificate expiring on the
**same day**:

| DC | expires | days left at discovery |
|---|---|---|
| rhonda.myrobertson.net | 2026-10-08 09:51 | 20.9 |
| dc1.myrobertson.net | 2026-10-08 15:00 | 21.1 |
| dns01.myrobertson.net | 2026-10-08 16:37 | 21.2 |

When they lapse, every LDAPS bind in the estate fails at once - Vault's LDAP
auth method, Nextcloud's directory bind, Keycloak, Synology DSM on scooter and
kermit, and the Proxmox VE realm. There is no staggering to soften it.

## It was already failing, not merely pending

The DCs were attempting renewal and being refused, every eight hours:

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

## Why it failed

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

The DCs were asking for renewal of a template their CA did not offer.

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

**Added alongside the fix:** `ProbeLDAPSCertificateRenewalFailing` in homelab_flux
`infrastructure/configs/certificate-expiry-alerts.yaml`, firing at T-35d at
`severity: critical`. Derived from `probe_ssl_earliest_cert_expiry` rather than
from Event 64, because there is no windows_exporter or event-log exporter in
this estate and that would have meant a new agent on three DCs. It would have
caught this on 2026-08-27 rather than 2026-09-17.

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

## What the fix was

Done 2026-09-17. The template ACL needed no changes - `Domain Controllers` and
`NT AUTHORITY\ENTERPRISE DOMAIN CONTROLLERS` already held **Enroll**, and the
template carries `msPKI-Enrollment-Flag = 41`, which includes `0x20
AUTO_ENROLLMENT`. `DomainController` is a V1 template, where Enroll plus that
flag is how DC autoenrollment works; there is no separate AutoEnroll ACE to
grant and none was needed.

The only thing missing was publication. Result:

```
dc1.myrobertson.net:636      365.0 days
dns01.myrobertson.net:636    365.0 days
rhonda.myrobertson.net:636   365.0 days
```

### Two things that cost time and will again

**1. Autoenrollment is slower than you will be patient for.** After publishing,
`certutil -pulse` plus a 45-second wait renewed *nothing* on rhonda or dns01,
while dc1 - the CA host itself - renewed immediately. That asymmetry reads like
an AD replication problem and is not: both DCs were confirmed seeing the
template published (`certificateTemplates` contains `DomainController`) while
still not enrolling. Forcing it works and reports the real outcome:

```
certreq -enroll -machine -q DomainController
  -> RequestId = 173
     The requested certificate has been issued.
```

Use that rather than waiting on the 8-hour autoenrollment cycle, and rather than
concluding something is still broken.

**2. The certificate store having it is NOT port 636 serving it.** All three DCs
held the new leaf in `LocalMachine\My` while dns01 was still presenting the old
one on 636 for a further two minutes. Checking the store alone would have
declared success while a third of the estate was still on the old certificate.
dns01 picked it up on its own - Schannel caches, no NTDS restart was needed -
but it must be confirmed from outside:

```bash
openssl s_client -connect <dc>:636 -showcerts   # or the blackbox-tls-ldaps probe
```

### Still open, deliberately not done

- **Template family.** This republished the legacy `DomainController` template,
  which is the minimal change: it renews exactly what the DCs already held.
  `KerberosAuthentication` supersedes both `DomainController` and
  `DomainControllerAuthentication` and is the modern choice, but moving family
  on live DCs is a migration rather than a renewal and was not appropriate as
  part of an alert triage. All three templates exist in AD at `validity=365d`,
  `renewal_overlap=42d`.
- **dc1 still has no autoenrollment GPO** (see below). It renewed here because
  the enrollment was forced. Whether it renews unattended in September 2027 is
  untested.
- **The expired certificates in `LocalMachine\My`** were not cleaned up.

## What a fix had to do

Recorded as written before the work, for the reasoning.

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

## Whether this recurs in 2027

The template is published and the ACL is correct, so ordinary autoenrollment
should now renew these at T-42d without intervention. That is untested here -
every one of the three renewals on 2026-09-17 was forced with `certreq`. The
alert added alongside this fix
(`ProbeLDAPSCertificateRenewalFailing`, homelab_flux
`infrastructure/configs/certificate-expiry-alerts.yaml`) fires at T-35d, which
is the check: if renewal happens on its own it never fires, and if it does not,
it pages at `severity: critical` with a month of runway rather than the 21-day
warning that goes to a null receiver.

## Note on this file's name

The filename says "failing" and is kept that way deliberately: the alert
annotation in homelab_flux points at this path, and renaming it would break that
link at exactly the moment someone is following it.

## Related

`svc-tf-adcs` needed a `Read` ACE on this same CA on 2026-09-16 to unblock
terraform - see `ansible/domain/configure_adcs_reader_permissions.yml`. That was
a permissions gap on a CA whose published-template set is also minimal. Both are
consistent with a CA that was rebuilt and configured only as far as the thing
being worked on at the time required.
