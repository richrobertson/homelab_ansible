# claude_workstation

Configures a Debian host as a shared Claude Code workstation: domain joined to
Active Directory, reachable over SSH through Guacamole, with the CLI installed
system-wide.

## What it deploys

| Component | Detail |
|-----------|--------|
| CLI | [`@anthropic-ai/claude-code`](https://www.npmjs.com/package/@anthropic-ai/claude-code) installed globally with npm |
| Runtime | Node.js from the NodeSource apt repository (22.x; the CLI needs 22+) |
| Identity | `realmd` + `sssd` + `adcli` joined to `myrobertson.net` |
| Access control | sssd `simple` access provider, fail-closed on an AD group allowlist |
| Sudo | `/etc/sudoers.d/claude-workstation`, granted to an AD group |
| Trust | AD Certificate Services root and Vault `pki_int` installed into the system store |
| Time | chrony following the domain controllers |
| Desktop | none by default; opt-in XFCE + XRDP |

## Prerequisites

- `VAULT_ADDR` and `VAULT_TOKEN` exported on the controller.
- Vault secrets:

  | Path | Keys | Purpose |
  |------|------|---------|
  | `secret/windows/domain/service-accounts/linux-realm-join` | `username`, `password` | Account that may create computer objects |
  | `secret/claude-code/workstation` | `api_key` | Anthropic API key |
  | `secret/windows/domain/ca/root` | `certificate` | AD CS root CA, PEM |

- An AD group named by `claude_workstation_ad_allowed_groups` must exist, or
  nobody can log in. That is deliberate; see below.
- `/home` mounted from a separate volume. The `claude_workstation_lxc` role
  attaches it; the role asserts it rather than trusting it.

## Access control

**Joining a domain is not an access control.** realmd's generated sssd.conf uses
`access_provider = ad`, which admits every enabled account in the directory.
This role overwrites that file after the join with `access_provider = simple`
and an explicit `simple_allow_groups`, and asserts the allowlist is non-empty
before it starts. An empty list fails the run instead of meaning "everyone".

Sudo is separate from login: `claude_workstation_ad_allowed_groups` decides who
gets a shell, `claude_workstation_sudo_ad_groups` decides who gets root. Neither
list implies the other.

## SSH authentication modes

`claude_workstation_ssh_auth_mode` reconciles two goals that genuinely conflict:
key-only SSH is the stronger posture, but a Guacamole SSH connection that
prompts for AD credentials needs a password path.

| Mode | Behaviour |
|------|-----------|
| `key` | Public key only. AD password logins do not work; the Guacamole connection must carry a private key. |
| `domain_password` (default) | Keys, plus keyboard-interactive so PAM hands the password to sssd. |
| `password` | Additionally enables plain `PasswordAuthentication`. |

The default is `domain_password` because it is the only mode in which the
intended access path works at all. It is narrower than it first looks:

- `PasswordAuthentication` stays **off**, so authentication cannot bypass PAM.
- sssd rejects every account outside the allowlist before a password is even
  evaluated.
- The local break-glass account has a locked password, so it stays key-only
  regardless of this setting.
- `PermitRootLogin no` in every mode.

The local account exists precisely because AD, DNS, or Kerberos being down is
when you most need a shell here, and that is exactly when domain auth fails.
`claude_workstation_authorized_keys` is asserted non-empty for that reason.

## Anthropic authentication

The role writes the Vault-sourced API key to a root-owned `0640` file at
`/etc/claude-code/claude-code.env`, read by `/etc/profile.d/claude-code.sh`.

**An API key is probably not what you want on this box.** It bills every user's
work to one identity and gives no per-person attribution or revocation. The
interactive alternative is better for a host humans log into:

```bash
claude setup-token          # once per user, stores ~/.claude/.credentials.json
```

`/home` is on a persistent volume, so those per-user credentials survive a
container rebuild. To use only that path, set
`claude_workstation_manage_api_key: false` and the role removes the shared
credentials file.

If you do want the shared key, note that `claude_workstation_env_group` controls
who can read it. It defaults to the local `workstation` group, so AD users
cannot read it; set it to an AD group name to share the key with them.

## Certificate trust

Two separate chains exist in this homelab and neither validates the other:

- **AD Certificate Services root** — signs domain controller LDAPS
  certificates. Sourced from Vault KV.
- **Vault `pki_int` intermediate** — signs Proxmox and other service
  certificates. Fetched from the mount's own `/v1/pki_int/ca/pem` endpoint, the
  same source `ansible/proxmox/provision_certificates.yml` uses.

Both land in `/usr/local/share/ca-certificates/` and `update-ca-certificates`
runs from a handler, so re-runs do not churn. Add more via
`claude_workstation_ca_certificates` rather than editing tasks.

`NODE_EXTRA_CA_CERTS` is exported to `/etc/ssl/certs/ca-certificates.crt`
because Node only reads the OS trust store on 22.15+, and only on some code
paths.

On LDAPS: `ldap_tls_reqcert` is `demand` and `ldap_tls_cacert` points at the
merged bundle. Be aware these only take effect once
`claude_workstation_sssd_ldap_uri` points at `ldaps://` — sssd's AD provider
seals LDAP with GSSAPI over port 389 by default and does not use TLS at all.
The values are set correctly anyway so switching is one variable, not a
security decision made under time pressure.

## Container-specific notes

Three things are configured because this runs in an unprivileged LXC:

- **sssd's uid range is pinned under 65536.** This is the one that actually
  bites. sssd derives uids from the object SID over a default range starting at
  200000, but an unprivileged container maps only 65536 uids onto the host, so
  a stock-range AD uid lands outside the map and logins fail with unownable
  files rather than an identity error. `ldap_idmap_range_min/max/range_size`
  force it into 10000-60000. The cost is that uids here do not match an
  AD-joined host using the default range — do not share uid-sensitive storage
  between them without reconciling.

- `default_ccache_name = FILE:...` in `krb5.conf`. The kernel keyring is not
  namespaced into an unprivileged container, so the default `KEYRING` ccache
  fails in ways that look like a rejected password.
- `keyctl=1,nesting=1` container features, set by the `claude_workstation_lxc`
  role, for sssd's own credential cache and for dbus/oddjob under systemd.

## Key variables

See `defaults/main.yml` for the full set.

| Variable | Default | Purpose |
|----------|---------|---------|
| `claude_workstation_ad_allowed_groups` | `[claude-workstation-users]` | Who may log in at all |
| `claude_workstation_sudo_ad_groups` | `[claude-workstation-admins]` | Who may sudo |
| `claude_workstation_ssh_auth_mode` | `domain_password` | SSH authentication posture |
| `claude_workstation_claude_version` | `latest` | Pin a specific CLI version here |
| `claude_workstation_manage_api_key` | `true` | Write the shared Vault-sourced API key |
| `claude_workstation_sssd_use_fully_qualified_names` | `false` | `rich` vs `rich@myrobertson.net` |
| `claude_workstation_desktop` | `false` | Install XFCE + XRDP |
| `claude_workstation_authorized_keys` | `[]` | Break-glass keys; required |

## Verifying

```bash
realm list
getent group claude-workstation-users
sudo -l -U <aduser>
claude --version
openssl s_client -connect dc1.myrobertson.net:636 -showcerts </dev/null 2>&1 | grep -i verify
```
