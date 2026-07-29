# Proxmox MCP server

Deployment and operation of the Model Context Protocol server that exposes the
`cl0` Proxmox cluster to MCP clients.

## Topology

```
MCP client  --HTTP-->  mcp0 (LXC 9010)                    -->  Proxmox API
                       supergateway :8096/mcp                  pve3/4/5 :8006
                         |
                         +-- stdio --> node /opt/proxmox-mcp/index.js
```

The container is created on a Proxmox node by the same playbook that configures
it. Authentication to Proxmox uses a dedicated `mcp@pve!mcpserver` API token
holding the custom `MCPAudit` role, whose privileges are audit-only.

## Deploy

```bash
export VAULT_ADDR=https://vault.myrobertson.net:8200
export VAULT_TOKEN=<token>

ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/proxmox_mcp.yml
```

The run is split into three tagged stages, each of which can be run alone:

| Tag | Effect |
|-----|--------|
| `proxmox_mcp_token` | Creates/reconciles the Proxmox role, user, and API token; writes the secret to Vault |
| `proxmox_mcp_lxc` | Creates and starts the container |
| `proxmox_mcp_service` | Installs the MCP server, the HTTP bridge, and the systemd unit |

## Connect a client

```bash
claude mcp add --transport http proxmox http://mcp0.myrobertson.net:8096/mcp
```

## Access control

The endpoint has no inbound authentication. `supergateway` provides none and
cannot be bound to a single interface, so the nftables rules written to
`/etc/nftables.d/proxmox-mcp.conf` are the only thing restricting access.
Anything inside `proxmox_mcp_allowed_client_cidrs` gets the full read-only tool
surface: cluster inventory, node and guest configuration, storage layout.

Treat widening that list, or setting `proxmox_mcp_allow_elevated: true`, as a
change that needs the same scrutiny as handing out a Proxmox login.

## Rotating the API token

Proxmox reveals a token secret exactly once, at creation. Rotation therefore
deletes and recreates the token, and any client holding the old value breaks
immediately.

```bash
ansible-playbook -i inventory/environments/production.ini \
  ansible/proxmox/proxmox_mcp.yml \
  -e proxmox_mcp_token_rotate=true
```

The `proxmox_mcp_api_token` role also recreates the token automatically if the
secret has gone missing from Vault, since there is no way to recover it.

## Upgrading the MCP server

Upstream publishes no releases, so upgrades are a deliberate commit bump.

1. Review the diff between the pinned commit and the target commit upstream.
2. Update `proxmox_mcp_repo_version` in `roles/proxmox_mcp/defaults/main.yml`.
3. Re-run with `--tags proxmox_mcp_service`.

The role reinstalls dependencies and restarts the unit only when the checkout
actually changes.

## Troubleshooting

**Service will not start.** `journalctl -u proxmox-mcp -n 100`. The most common
causes are a Vault secret that is missing keys and a Node version older than 20.

**Health endpoint returns nothing.** Confirm the unit is running, then check
whether nftables is dropping the connection:

```bash
nft list table inet proxmox_mcp
```

**Proxmox API authentication fails.** Verify the token still exists and that its
role has not drifted:

```bash
pveum user token list mcp@pve
pveum role list --output-format json | jq '.[] | select(.roleid=="MCPAudit")'
```

**TLS verification errors.** The cluster uses Vault-issued certificates; see
[Vault connectivity and Proxmox cert-agent recovery](./vault-connectivity-and-cert-agent-recovery.md).
Set `proxmox_mcp_verify_tls: false` only as a temporary diagnostic.

## Removal

```bash
# on the container host
pct stop 9010 && pct destroy 9010

# on a Proxmox node
pveum user token remove mcp@pve mcpserver
pveum user delete mcp@pve
pveum role delete MCPAudit
```

Then delete `secret/proxmox/cl0/mcp` from Vault and drop the `[mcp_servers]`
group from the inventory.
