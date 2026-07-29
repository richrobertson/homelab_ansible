# Ceph admin gateway

Publishes the Ceph mgr dashboard at a stable, LAN-only HTTPS name that survives
mgr failover.

## The problem this solves

The Ceph dashboard runs on the **active mgr only**. Standbys answer with a 303
redirect. The active mgr moves between `pve3`, `pve4`, and `pve5` on its own —
on daemon restart, on node reboot, on `ceph mgr fail`, and during upgrades.

Before this gateway, reaching the console meant knowing which node was currently
active and browsing to that node's IP on port 8089. Every failover silently
invalidated whatever bookmark or URL an operator had. Worse, the certificate
reload logic in `ceph_admin_portal.yml` deliberately calls `ceph mgr fail` when
it rotates the dashboard certificate, so a routine certificate run relocates the
console as a side effect.

## Topology

```
browser --HTTPS--> VIP 192.168.1.246:443
                     |  (keepalived VRRP across pve3/4/5)
                     v
                   HAProxy on whichever node holds the VIP
                     |  health check: GET / expecting 200
                     |  active mgr returns 200, standbys return 303
                     v
                   active mgr dashboard :8089
```

Two independent failovers are covered. keepalived moves the VIP if a node dies
or its HAProxy stops. HAProxy's health check re-points at the new active mgr
when mgr leadership moves, without the browser ever seeing a redirect.

## Deploy

```bash
export VAULT_ADDR=https://vault.myrobertson.net:8200
export VAULT_TOKEN=<token>

ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/ceph_admin_gateway.yml
```

`ansible/proxmox/provision_certificates.yml` must have run at least once first.
The gateway reuses the Vault AppRole and `pki_int/issue/proxmox-api` PKI role
that playbook creates rather than bootstrapping a second credential path.

## Prerequisite: DNS

DNS records are not managed by this repository, so the A record is a manual
step. Create it wherever `cl0.myrobertson.net` is currently served:

```
ceph-admin.cl0.myrobertson.net.  A  192.168.1.246
ceph.cl0.myrobertson.net.        A  192.168.1.246
```

Both names are on the certificate.

## About the hostname

The originally requested name was `ceph_admin.cl0.myrobertson.net`, with an
underscore. That will not work as deployed, for a concrete reason: the Vault PKI
role `proxmox-api` is created with `enforce_hostnames=true`, and Vault rejects
certificate requests whose names are not valid RFC 1123 hostnames. Underscores
are not. The request fails at issuance, before anything reaches HAProxy. The
CA/Browser Forum baseline requirements prohibit the same thing for public CAs.

The role therefore defaults to `ceph-admin.cl0.myrobertson.net` and asserts the
name is valid before doing any work.

If the underscore form is genuinely required, it needs a dedicated Vault PKI
role with `enforce_hostnames=false`:

```bash
vault write pki_int/roles/ceph-admin-lax \
  allowed_domains=myrobertson.net \
  allow_subdomains=true \
  enforce_hostnames=false \
  server_flag=true client_flag=false \
  key_type=rsa key_bits=2048 max_ttl=720h
```

Then set `ceph_admin_gateway_pki_role: ceph-admin-lax` and
`ceph_admin_gateway_fqdn: ceph_admin.cl0.myrobertson.net`, and relax the
hostname assertion in the role. Be aware that client behaviour with underscore
hostnames over TLS is inconsistent across browsers and HTTP libraries.

## Access control

LAN-only, enforced in two places:

- nftables (`/etc/nftables.d/ceph-admin-gateway.conf`) drops non-LAN sources
  before they reach HAProxy.
- HAProxy returns 403 to any source outside `ceph_admin_gateway_allowed_cidrs`.

The duplication is intentional. The firewall is the real control; the HAProxy
ACL keeps the restriction visible in the proxy config and holds if someone
disables the firewall while debugging.

This gateway does **not** add authentication. The Ceph dashboard's own login
still applies. Do not expose the VIP beyond the LAN.

## Verifying failover

```bash
# which node currently holds the VIP
ip -4 addr show vmbr0 | grep 192.168.1.246

# which mgr is active
ceph mgr stat --format json

# backend health as HAProxy sees it: exactly one server should be UP
echo "show stat" | socat /run/haproxy/admin.sock stdio | grep ceph_dashboard
```

Force an mgr failover and confirm the endpoint stays up:

```bash
active="$(ceph mgr stat --format json | python3 -c 'import json,sys;print(json.load(sys.stdin)["active_name"])')"
ceph mgr fail "$active"
curl -sk -o /dev/null -w '%{http_code}\n' https://ceph-admin.cl0.myrobertson.net/
```

The curl should return 200 within a few seconds of the health check interval.

## Troubleshooting

**HAProxy will not start.** It refuses to start without its certificate. Check
that the Vault Agent produced one:

```bash
systemctl status vault-agent-ceph-admin
openssl x509 -in /etc/haproxy/certs/ceph-admin.pem -noout -subject -dates
```

**All backends DOWN.** The health check requires a literal 200. If the dashboard
module is not loaded on any mgr, every backend fails. Confirm with
`ceph mgr module ls` and see [Ceph dashboard bind fix](./ceph-dashboard-bind-fix.md).

**VIP on two nodes at once.** A VRRP split brain, almost always caused by
multicast being blocked on `vmbr0` or by a `virtual_router_id` collision with
another keepalived cluster on the same segment. Check
`ceph_admin_gateway_vrrp_id` is unique on the LAN.

**Certificate does not renew.** The Vault Agent re-issues as expiry approaches
and reloads HAProxy via its template `command`. Check
`journalctl -u vault-agent-ceph-admin` for AppRole authentication failures; the
AppRole secret_id is shared with the Proxmox certificate agent and may have been
rotated out from under it.
