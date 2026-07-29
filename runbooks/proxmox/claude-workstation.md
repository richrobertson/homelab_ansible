# Claude Code workstation

Deployment and operation of the domain-joined Linux workstation used to run
Claude Code, reached through Guacamole.

## Topology

```
browser --HTTPS--> rdp.myrobertson.com          --SSH 22--> cwork0 (LXC 9020)
                   Guacamole 1.6.0 (k8s)                    192.168.1.61
                   guacd :4822                              claude, node 22
                     |                                        |
                   Keycloak OIDC                            sssd/realmd
                   sso.myrobertson.com                        |
                     |                                      AD: myrobertson.net
                     +---- LDAPS federation ----------------> dc1 / rhonda
```

The same AD identity is used twice over: Keycloak federates AD over LDAPS to let
you into Guacamole, and sssd on the workstation authenticates the SSH session
against the same directory. There is no credential pass-through between the
two — you sign into Guacamole with SSO, then Guacamole prompts you for the SSH
password separately.

Access is an **SSH** connection, not RDP. Claude Code is a terminal application,
so a browser terminal is the right shape and avoids installing a desktop. The
XFCE/XRDP path exists behind `claude_workstation_desktop=true` for the rare case
that something genuinely needs a GUI.

## Deploy

```bash
export VAULT_ADDR=https://vault.myrobertson.net:8200
export VAULT_TOKEN=<token>

ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/claude_workstation.yml
```

Tagged stages, each runnable alone:

| Tag | Effect |
|-----|--------|
| `claude_workstation_lxc` | Creates the container and its separate `/home` volume |
| `claude_workstation_config` | Everything inside the container |
| `claude_workstation_domain` | Realm join and sssd only |
| `claude_workstation_ca` | Certificate trust only |
| `claude_workstation_claude` | Claude Code install and credentials only |
| `claude_workstation_guacamole` | Optional Guacamole REST registration (off by default) |

### Prerequisites

Create these before the first run or it fails, by design:

1. **Vault secrets**

   | Path | Keys |
   |------|------|
   | `secret/windows/domain/service-accounts/linux-realm-join` | `username`, `password` |
   | `secret/claude-code/workstation` | `api_key` |
   | `secret/windows/domain/ca/root` | `certificate` (PEM) |

2. **AD groups.** `claude-workstation-users` (login) and
   `claude-workstation-admins` (sudo). They are separate on purpose.

3. **Break-glass keys.** Set `claude_workstation_authorized_keys`. The role
   refuses to run with an empty list, because AD being down is exactly when you
   need a way in.

## How the Guacamole connection actually gets created

Read this before assuming the deploy is complete.

**The Guacamole connection is not managed by this Ansible code, and by default
is not created by it at all.**

The flux-managed Guacamole at `apps/base/guacamole/` runs Guacamole 1.6.0 with
`POSTGRESQL_ENABLED=true`, pointed at a CloudNativePG cluster
(`guacamole-cnpg-rw`). Connections therefore live as rows in that Postgres
database. There is no `user-mapping.xml`, no JSON-auth ConfigMap, and no
declarative connection definition anywhere in the flux repo — the only SQL in
the repo is a schema bootstrap job plus a block that normalises parameters on
connections that already exist. `docs/runbooks/GUACAMOLE_RUNBOOK.md` says the
same thing: "Add RDP/VNC/SSH connections inside the Guacamole admin UI."

So there are exactly three options, and only the first is fully supported:

### Option 1 — the admin UI (recommended)

Sign in at `https://rdp.myrobertson.com` as a member of `Domain Admins` (the
`guacamole-admin-groups` job grants that group `ADMINISTER`), then
**Settings → Connections → New Connection**:

| Field | Value |
|-------|-------|
| Name | `Claude Code workstation` |
| Protocol | `SSH` |
| Hostname | `192.168.1.61` |
| Port | `22` |
| Username / Password | **leave blank** |
| Enable SFTP | yes |
| SFTP root directory | `/home` |

Leaving the credentials blank is deliberate: Guacamole prompts at connect time,
so nobody's AD password ends up stored in the Guacamole database.

This is a manual, one-time, click-through step. It is not captured in Git, it
will not be recreated if the Guacamole database is restored from an older
backup, and nothing in this repository will detect that it has drifted or gone
missing.

### Option 2 — the REST API play (optional, off by default)

`ansible/proxmox/claude_workstation.yml` has a `claude_workstation_guacamole`
play that does the same thing over `/api/tokens` and
`/api/session/data/postgresql/connections`. Enable it explicitly:

```bash
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/claude_workstation.yml \
  --tags claude_workstation_guacamole \
  -e claude_workstation_guacamole_manage_connection=true
```

Be aware of its real limitations:

- **It needs a username/password Guacamole account.** This deployment's
  `EXTENSION_PRIORITY` is `openid,postgresql,ban` and the primary auth is
  Keycloak OIDC. `/api/tokens` with a username and password only works for a
  local database account — the default `guacadmin`, or one you create. If that
  account has been disabled in favour of SSO, this play cannot authenticate.
  It expects the credentials in Vault at `secret/guacamole/admin`.
- **It only reduces the click-through; it does not make the connection
  declarative.** The row still lives in Postgres and is still outside Git. Its
  real backup is the CNPG scheduled backup to Backblaze B2, not this repo.
- Idempotency is by connection *name*. Guacamole has no upsert and mints its own
  identifiers, so a POST of a name that already exists creates a duplicate; the
  play lists connections first and skips if the name is present. Renaming the
  connection in the UI will cause the play to create a second one.

### Option 3 — seed it in SQL

Possible via the postgres-init job in the flux repo, but it means writing
`guacamole_connection` INSERTs by hand against an internal schema. Not
recommended and not implemented here.

### Known blocker: guacd egress

`apps/base/guacamole/networkpolicy.yaml` defines `guacamole-allow-egress` over
`app.kubernetes.io/part-of: guacamole`, which includes guacd. It permits DNS,
4822, 5432, port 80 to Authelia, and 443 anywhere — **there is no egress rule
for port 22**. As written, guacd cannot open an SSH connection to
`192.168.1.61:22` regardless of how the connection is defined.

Before the first connection attempt, either confirm the CNI is not enforcing
that policy, or add an egress rule permitting TCP 22 to the LAN. That is a
change in `homelab_flux`, not here. Symptom if you skip it: the connection
appears in the UI and fails with a generic connection error.

## Connecting

1. Browse to `https://rdp.myrobertson.com`, sign in through Keycloak SSO.
2. Open the `Claude Code workstation` connection.
3. At the SSH prompt, enter your AD username (short form, e.g. `rich` — the box
   is configured with `use_fully_qualified_names = False`) and your AD password.
4. `claude` is on `PATH`. Start work in a `tmux` session so a dropped browser
   tab does not kill a long-running task:

```bash
tmux new -A -s claude
claude
```

Your home directory is on a separate volume that survives container rebuilds.

## Credential rotation

### Anthropic API key

```bash
vault kv put secret/claude-code/workstation api_key=<new key>

ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/claude_workstation.yml --tags claude_workstation_claude
```

Rewrites `/etc/claude-code/claude-code.env`. Existing shells keep the old value
until they re-source `/etc/profile.d/claude-code.sh` — tell people to open a new
session.

Per-user tokens created with `claude setup-token` are **not** touched by this;
each user rotates their own with `claude /logout` then `claude setup-token`.

### Domain join account

```bash
vault kv put secret/windows/domain/service-accounts/linux-realm-join \
  username=<user> password=<new password>
```

No re-run is needed. The account is used once at join time; the host thereafter
authenticates with its own computer account keytab, which sssd rotates on its
own schedule.

### Rejoining the domain

Only if the computer account has been deleted or the keytab is broken:

```bash
realm leave myrobertson.net
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/claude_workstation.yml --tags claude_workstation_domain
```

### CA certificates

```bash
vault kv put secret/windows/domain/ca/root certificate=@ad-root-ca.pem

ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/claude_workstation.yml --tags claude_workstation_ca
```

`update-ca-certificates` runs from a handler, so it fires only when a file
actually changed.

Verify the trust is real, not just installed:

```bash
openssl s_client -connect dc1.myrobertson.net:636 -showcerts </dev/null 2>&1 \
  | grep -E 'Verify return code|subject='

ldapsearch -H ldaps://dc1.myrobertson.net -Y GSSAPI -b 'DC=myrobertson,DC=net' \
  -s base '(objectclass=*)' defaultNamingContext
```

`Verify return code: 0 (ok)` is the answer you want. Anything else means the
chain is incomplete — usually a missing issuing CA, not a missing root.

## Upgrading Claude Code

The CLI is installed globally from npm and pinned by
`claude_workstation_claude_version` (default `latest`). The role resolves that
tag to a concrete version with `npm view` and only installs when the resolved
version differs from what is present, so a re-run is a no-op when already
current.

```bash
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/claude_workstation.yml --tags claude_workstation_claude
```

To pin a specific release, set `claude_workstation_claude_version: 2.1.211` in
inventory or on the command line.

The in-place updater is disabled (`DISABLE_AUTOUPDATER=1`) because the global
install is root-owned and the updater cannot write to it; leaving it on produces
a confusing permission error on every start. Upgrades are a deliberate re-run.

## Rebuilding the container without losing work

`/home` is `mp0`, a separate volume. **`pct destroy` deletes referenced mount
point volumes**, so the volume only survives if it is detached from the config
first. On the Proxmox node:

```bash
pct config 9020 | grep mp0            # note the volume id, e.g. local-lvm:vm-9020-disk-1
pct stop 9020
pct set 9020 --delete mp0             # volume becomes unreferenced, not deleted
pct destroy 9020                      # do NOT pass --destroy-unreferenced-disks
```

Then recreate, re-attaching the preserved volume:

```bash
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/claude_workstation.yml \
  -e claude_workstation_lxc_home_volume=local-lvm:vm-9020-disk-1
```

Verify before trusting it:

```bash
pct exec 9020 -- df -h /home          # must show the mp0 volume, not the rootfs
```

The role asserts `/home` is a separate filesystem and fails the run if it is
not, so a silent fallback to the root disk cannot go unnoticed.

Note the computer account: destroying and recreating the container produces a
new host that must rejoin. Run `realm leave` first, or clean up the stale
computer object in AD afterwards.

## Troubleshooting

**Login rejected with correct AD password.**
Check access control before suspecting the password:

```bash
getent group claude-workstation-users     # empty output means sssd cannot see it
id <aduser>
journalctl -u sssd -n 50
```

`Access denied` in the sssd log with a valid password means the account is not
in `claude_workstation_ad_allowed_groups`. That is the allowlist working.

**Nothing in AD resolves at all.**
Almost always DNS. The container must use a domain controller as its resolver,
not a forwarder, because it needs the `_ldap._tcp` SRV records:

```bash
cat /etc/resolv.conf                                   # expect 192.168.1.3
dig +short -t SRV _ldap._tcp.myrobertson.net
```

**Kerberos "clock skew too great".**

```bash
chronyc tracking      # System time offset must be well under 5 minutes
chronyc sources -v
```

If the container came back from a suspended host, `chronyc makestep` forces an
immediate correction.

**Domain login succeeds but files are unownable / `id` shows a huge uid.**
This is the other unprivileged-LXC failure mode. sssd's default SID-to-uid range
starts at 200000, but an unprivileged container maps only 65536 uids onto the
host, so a stock-range uid falls off the end of the map. The role pins the range
under that limit:

```bash
id <aduser>                                    # expect a uid in 10000-60000
grep idmap_range /etc/sssd/sssd.conf
```

If a uid outside that window appears, sssd cached it before the range was
applied. Clear the cache and restart:

```bash
sss_cache -E
systemctl restart sssd
```

The tradeoff: uids on this host will not match an AD-joined machine using the
default range. Do not share NFS or other uid-sensitive storage between them
without reconciling the ranges first.

**Realm join fails with a keyring or ccache error.**
This is the unprivileged-LXC failure mode. Confirm the container has the
required features:

```bash
pct config 9020 | grep features       # expect nesting=1,keyctl=1
```

`/etc/krb5.conf` should contain `default_ccache_name = FILE:/tmp/krb5cc_%{uid}`.
The kernel keyring is not namespaced into an unprivileged container, so a
`KEYRING` ccache fails there and the error frequently surfaces as an
authentication failure rather than a storage failure.

**sudo says the user is not in the sudoers file.**
Login and sudo are separate grants. Being in `claude-workstation-users` gets a
shell; sudo needs `claude-workstation-admins`.

```bash
sudo -l -U <aduser>
visudo -cf /etc/sudoers.d/claude-workstation
```

**Guacamole connection fails immediately.**
Check the egress NetworkPolicy blocker documented above, then confirm the
workstation is actually listening and reachable from the cluster:

```bash
ss -tlnp | grep :22
journalctl -u ssh -n 50
```

**TLS failures from Claude Code or npm.**

```bash
echo $NODE_EXTRA_CA_CERTS                      # /etc/ssl/certs/ca-certificates.crt
ls -l /usr/local/share/ca-certificates/
awk '/BEGIN CERT/{n++} END{print n}' /etc/ssl/certs/ca-certificates.crt
```

If a certificate was added but not merged, `update-ca-certificates` did not run;
re-run the `claude_workstation_ca` tag.
