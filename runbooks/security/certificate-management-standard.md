# Certificate Management Standard

Status: **adopted 2026-07-29**. Owner: platform. Review: quarterly.

## The rule

Every TLS/SSL certificate in the estate — anything another system connects
to — MUST satisfy all three of:

1. **Issued by a managed authority** — HashiCorp Vault PKI (`pki_int`),
   Microsoft AD CS (`myrobertson-DC1-CA`), or a public ACME CA (Let's Encrypt).
   Never hand-generated, never a long-lived self-signed leaf.
2. **Renewed by automation** — a Vault Agent, AD CS autoenrollment, or an ACME
   client (cert-manager / certbot). Never renewed by a human remembering to.
3. **Monitored for expiry** — the endpoint is probed and an alert fires **≥30
   days** before `notAfter` (blackbox exporter, homelab_flux
   `infrastructure/configs/certificate-expiry-alerts.yaml`).

A certificate missing any one of the three is a **defect**, tracked and
remediated like any other — not a permanent state of affairs.

The load-bearing requirement is **#2 (a renewer)**. The authority in #1 is a
detail chosen by trust domain; a cert with the "right" CA and no renewer still
fails silently. #3 exists to catch the exceptions where #2 is impossible.

**The only standing exceptions are the AD root CA and the Vault intermediate
CA** — the trust anchors themselves. They rotate rarely and are bootstrapped
out-of-band. *Everything they sign* — every leaf, internal or external — is
issued and auto-rotated by a consistent process. "Consistent" is deliberate:
one mechanism per lane (one Vault-Agent role, cert-manager, AD CS
autoenrollment), not a bespoke script per service.

## Why this exists

On **2026-05-15** the PBS API certificate (`pbs.myrobertson.net:8007`) expired
and served an expired leaf for **75 days** before anyone noticed — because PBS
was never brought under any of the three. The cost of a missing renewer is a
silent outage that surfaces at the worst time. This standard makes "a human has
to remember" an unacceptable design.

## Choosing the authority (by trust domain)

| Who connects | Authority | Renewer | Why |
|---|---|---|---|
| Internal Linux services (Proxmox, PBS, Vault, app backends, service-to-service) | **Vault PKI** `pki_int` | **Vault Agent** | short-lived, API-driven, already the estate pattern |
| Windows / domain-joined / LDAPS / anything that already trusts the AD root | **AD CS** `myrobertson-DC1-CA` | **GPO autoenrollment** | domain machines trust the AD root; autoenrollment renews without touching the host |
| Public-facing / reached by clients that do **not** have our CA installed | **ACME / Let's Encrypt** | **cert-manager** (k8s) or certbot | publicly trusted; our internal CAs are not |

Rule of thumb: *only our machines connect* → Vault or AD CS. *A browser or third
party connects without our root installed* → ACME.

## Renewal mechanisms

### Vault Agent — the reference pattern (internal Linux)

The canonical, reusable implementation is the **`roles/vault_cert_agent`** role
(`ansible/proxmox/roles/vault_cert_agent`): give it a service's cert paths,
owner, reload command and API port, and it runs a Vault Agent that keeps that
one certificate auto-rotated. `ansible/proxmox/pbs_provision_certificate.yml` is
a worked example. Per host the agent:

- authenticates a **Vault Agent** via AppRole (`proxmox-api-cert`),
- runs a `pkiCert` **template** that re-issues from
  `pki_int/issue/proxmox-api` as the lease approaches expiry,
- writes the cert/key via an **apply script** and reloads the service,
- is supervised by a **systemd unit** so it renews forever.

To bring a **new Linux service** under management, apply `roles/vault_cert_agent`
with four service-specific values: cert path, key path + ownership, reload
command, and the API port to verify. PBS is the role's first user.

**Consolidation status (for full consistency):** two older copies of this exact
pattern predate the role — `provision_certificates.yml` (pveproxy, inline) and
`roles/ceph_admin_gateway` (its own agent template). Converging both onto
`roles/vault_cert_agent` is the tracked follow-up that completes "one mechanism
per lane." It is **canary-gated**: those renew live production certificates, so
migrate one host at a time and verify issuance before the next.

### AD CS autoenrollment (Windows / LDAPS)

Domain machines renew certificates via **GPO certificate autoenrollment** against
`myrobertson-DC1-CA`. Requirement: the autoenrollment GPO is applied to the DCs
and relevant member servers, and a suitable certificate template
(auto-enroll permission for the machine) exists.
> **Verify:** confirm the autoenrollment GPO + template actually renew the DC /
> LDAPS certs, rather than these having been enrolled once by hand. (Inventory
> pending — see Current coverage.)

### ACME / cert-manager (public / Kubernetes)

Public-facing and cluster-terminated certs use **cert-manager** with an ACME
`ClusterIssuer`, or certbot on standalone hosts.
> **Verify:** confirm cert-manager + a working `ClusterIssuer` exist in
> homelab_flux, and that each public Ingress/Gateway consumes a cert-manager
> `Certificate` (not a statically committed TLS secret). (Inventory pending.)

## Exceptions

A platform that genuinely cannot accept an externally-managed cert or expose a
renewal hook is an **exception, not an exemption**. It MUST still be:

1. **documented** in the Current coverage table below with the reason, and
2. **covered by an expiry alert**, so its manual renewal is never silent.

## Current coverage (live probe, 2026-07-29)

| Service | Endpoint | Authority | Expires | Renewer | Status |
|---|---|---|---|---|---|
| pve3 / pve4 / pve5 | :8006 | Vault `pki_int` | 2026-08-24 | Vault Agent | ✅ managed, actively renewing (30-day certs) |
| **PBS** | :8007 | Vault `pki_int/issue/proxmox-api` | 2026-05-15 (stale) | **Vault Agent** (`roles/vault_cert_agent`) | 🛠 automation written + validated offline; pending one-time KV `secret_id` + canary run |
| Vault | :8200 | Vault intermediate (self) | 2026-10-11 | verify | ⚠ confirm renewer for the listener cert |
| dc1 LDAPS | :636 | AD CS `myrobertson-DC1-CA` | 2026-10-08 | AD CS autoenroll (verify) | ⚠ confirm autoenrollment renews |
| rhonda LDAPS | :636 | AD CS `myrobertson-DC1-CA` | 2026-10-08 | AD CS autoenroll (verify) | ⚠ confirm autoenrollment renews |
| Ceph admin gateway | 192.168.1.246:8443 | Vault (role authored) | not deployed | Vault Agent (planned) | ⏳ pending deployment |

> Repo-wide inventory (ansible / bootstrap / flux) for client certs, committed
> `*.pem`/`*.crt`, k8s TLS secrets, and other endpoints is in progress; this
> table is completed from it.

## Enforcement

- **Detection:** `homelab_flux/infrastructure/configs/certificate-expiry-alerts.yaml`
  (blackbox exporter) probes endpoints and alerts <30 days to expiry. This is
  what would have caught PBS 30 days early.
- **Audit (to build):** a periodic job that enumerates every TLS endpoint and
  flags any (a) not issued by a managed authority, (b) with no renewer, or
  (c) not covered by an expiry alert. Until then, re-run the live probe in this
  runbook by hand each quarter.

## Onboarding a new certificate (operator checklist)

1. **Authority** — pick by trust domain (table above).
2. **Renewer** — wire it: instantiate the Vault Agent pattern, add an AD CS
   template + autoenroll, or add a cert-manager `Certificate`.
3. **Monitor** — add the endpoint to the blackbox expiry probe.
4. **Record** — add a row to *Current coverage* and, if it is an exception,
   state why.

No certificate ships without steps 1–3.

## Related

- `ansible/proxmox/provision_certificates.yml` — Vault Agent reference implementation
- `runbooks/proxmox/pbs-certificate-renewal.md` — closing the PBS gap
- `homelab_flux/infrastructure/configs/certificate-expiry-alerts.yaml` — detection half
- `runbooks/security/credential-lifecycle-roadmap.md` — the sibling standard for credentials
