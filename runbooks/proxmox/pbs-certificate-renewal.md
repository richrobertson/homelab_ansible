# PBS API certificate renewal

Renewing and then auto-managing the TLS certificate on the Proxmox Backup
Server API (`pbs.myrobertson.net:8007`).

## Why this exists

The PBS API certificate **expired on 2026-05-15** and served an expired leaf for
75+ days before anyone noticed. That is not an accident of neglect — it is a
structural gap: **PBS has no certificate management anywhere in this repository.**

`ansible/proxmox/provision_certificates.yml` issues and auto-renews certificates
for the Proxmox VE nodes via a Vault Agent, but it targets `pveproxy` on the
`proxmox_cert_nodes` group only. PBS (`pbs_servers`) was never included, so its
certificate is whatever was placed on it by hand, and nothing renews it.

This is the same class of silent failure the incident kept surfacing: a control
that appears to exist (TLS on the API) quietly stops working, and nothing
watches it. The certificate-expiry alerts added in homelab_flux PR #65
(`certificate-expiry-alerts.yaml`, backed by the blackbox exporter) close the
*detection* half. This runbook closes the *renewal* half.

## Immediate renewal

Run on the PBS host. Two options.

### Option A — Vault-issued certificate (preferred, matches the estate)

Consistent with how the Proxmox VE nodes get their certs. Requires the Vault CLI
and a token or AppRole on PBS with access to the `pki_int` mount.

```bash
export VAULT_ADDR=https://vault.myrobertson.net:8200
# authenticate (token, or AppRole as the VE nodes do)

vault write -format=json pki_int/issue/proxmox-api \
  common_name=pbs.myrobertson.net \
  alt_names=pbs.myrobertson.net \
  ip_sans=192.168.1.217 \
  ttl=720h key_type=rsa key_bits=2048 \
  > /tmp/pbs-cert.json

python3 - <<'PY'
import json
d=json.load(open('/tmp/pbs-cert.json'))['data']
open('/etc/proxmox-backup/proxy.pem','w').write(d['certificate']+'\n'+d['issuing_ca']+'\n')
open('/etc/proxmox-backup/proxy.key','w').write(d['private_key']+'\n')
PY
chmod 640 /etc/proxmox-backup/proxy.pem /etc/proxmox-backup/proxy.key
chown root:backup /etc/proxmox-backup/proxy.pem /etc/proxmox-backup/proxy.key
rm -f /tmp/pbs-cert.json

systemctl reload proxmox-backup-proxy
```

Note the PKI role `proxmox-api` runs with `enforce_hostnames=true`; the
`pbs.myrobertson.net` name is a valid hostname so issuance succeeds.

### Option B — regenerate self-signed (fastest, untrusted)

Only if Vault is unreachable and the API must come back now. Clients then have to
trust the self-signed cert or skip verification.

```bash
proxmox-backup-manager cert update --force
systemctl reload proxmox-backup-proxy
```

### Verify

```bash
proxmox-backup-manager cert info
echo | openssl s_client -connect pbs.myrobertson.net:8007 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates
```

The `notAfter` date should be ~30 days out (self-signed default) or 720h (Vault).

## Durable fix — auto-renewing Vault Agent

Renewing by hand recreates the original problem: the next expiry is silent too.
The Proxmox VE nodes solved this with a Vault Agent that re-issues from
`pki_int` as expiry approaches and reloads the service. PBS should have the same.

**Implemented** as `ansible/proxmox/pbs_provision_certificate.yml`, which applies
the reusable `roles/vault_cert_agent` role to `pbs_servers`, reusing the same
Vault AppRole (`proxmox-api-cert`) and PKI role (`pki_int/issue/proxmox-api`) as
the VE nodes, with these PBS-specific differences from the VE version:

| Aspect | Proxmox VE | PBS |
|--------|-----------|-----|
| Cert path | `/etc/pve/local/pveproxy-ssl.pem` | `/etc/proxmox-backup/proxy.pem` |
| Key path | `/etc/pve/local/pveproxy-ssl.key` | `/etc/proxmox-backup/proxy.key` |
| Filesystem | pmxcfs (FUSE — needs `cp`, not `install`) | regular ext4 (`install` is fine) |
| Reload | `systemctl restart pveproxy` | `systemctl reload proxmox-backup-proxy` |
| Ownership | `root:root` | `root:backup` |
| API port to verify | 8006 | 8007 |

The Vault Agent config, apply-script bundle parsing, and systemd unit transfer
almost unchanged — only the destination paths, ownership, and reload command
differ. The apply script is actually simpler on PBS because the target is a
normal filesystem.

The role and playbook are written and validated offline (syntax-check, template
render, apply-script bash + marker parsing). Two things gate the first run, both
by design:

1. **One-time Vault material (operator, once).** The AppRole `secret-id` write is
   gated from automation, so a human seeds it once:
   ```bash
   export VAULT_ADDR=https://vault.myrobertson.net:8200
   RID=$(vault read  -field=role_id   auth/approle/role/proxmox-api-cert/role-id)
   SID=$(vault write -f -field=secret_id auth/approle/role/proxmox-api-cert/secret-id)
   vault kv put secret/proxmox/pbs/cert-approle role_id="$RID" secret_id="$SID"
   ```
2. **Canary run (operator).** The playbook only touches the PBS host when run:
   ```bash
   VAULT_ADDR=… VAULT_TOKEN=… \
     ansible-playbook ansible/proxmox/pbs_provision_certificate.yml \
       --limit pbs.myrobertson.net
   ```
   It installs `proxy.pem`/`proxy.key`, reloads `proxmox-backup-proxy`, and
   verifies port 8007 serves a Vault-issued leaf before reporting success.

Note: PBS's SSH host key changed since this estate was last touched (RSA →
ED25519); confirm the box was intentionally rebuilt/re-keyed before running.

## Related

- [Vault connectivity and Proxmox cert-agent recovery](./vault-connectivity-and-cert-agent-recovery.md)
- `ansible/proxmox/provision_certificates.yml` — the pattern to extend
- homelab_flux `infrastructure/configs/certificate-expiry-alerts.yaml` — the
  detection that would have caught this 30 days before expiry
