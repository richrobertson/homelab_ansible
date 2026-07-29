# proxmox_mcp

Installs the Proxmox MCP server on a Debian host and exposes it over Streamable
HTTP so remote MCP clients can use it.

## What it deploys

| Component | Detail |
|-----------|--------|
| MCP server | [`gilby125/mcp-proxmox`](https://github.com/gilby125/mcp-proxmox), MIT, pinned by commit |
| HTTP bridge | [`supergateway`](https://www.npmjs.com/package/supergateway), pinned by version |
| Runtime | Node.js from the NodeSource apt repository |
| Service | `proxmox-mcp.service`, running as the unprivileged `proxmox-mcp` user |
| Endpoint | `http://<host>:8096/mcp`, health at `/healthz` |

The upstream MCP server only speaks stdio. `supergateway` wraps that process and
publishes it as a Streamable HTTP endpoint, which is what makes a remote
deployment usable at all. The commit pin exists because upstream publishes no
tags and is not on the npm registry, so `main` is the only moving alternative.

## Prerequisites

- `VAULT_ADDR` and `VAULT_TOKEN` exported on the controller.
- A Proxmox API token already stored in Vault at `secret/proxmox/cl0/mcp` with
  the keys `user`, `token_name`, and `token_secret`. The `proxmox_mcp_api_token`
  role creates it.

## Security model

Two things are worth being explicit about.

**The endpoint is unauthenticated.** `supergateway` offers no inbound
authentication, and it has no bind-address flag, so it always listens on every
interface. Reachability is therefore the entire access control story, and the
role installs nftables rules restricting the port to
`proxmox_mcp_allowed_client_cidrs`. Setting that to an empty list, or setting
`proxmox_mcp_manage_firewall: false`, leaves the tool surface open to the whole
L2 segment. If you need real authentication, put this behind a reverse proxy
with forward-auth rather than relaxing the CIDR list.

**Elevated mode is genuinely destructive.** `proxmox_mcp_allow_elevated: true`
turns on VM/LXC lifecycle and config-mutation tools, which means a model can
stop or delete guests. It also requires widening the Proxmox-side role, since
the default grant is audit-only privileges. The default here is read-only, and
`proxmox_mcp_node_allowlist` / `proxmox_mcp_vmid_allowlist` are available to cap
the blast radius if you do turn writes on.

## Key variables

See `defaults/main.yml` for the full set.

| Variable | Default | Purpose |
|----------|---------|---------|
| `proxmox_mcp_repo_version` | pinned commit | Upstream revision to deploy |
| `proxmox_mcp_port` | `8096` | Listener port |
| `proxmox_mcp_http_path` | `/mcp` | Streamable HTTP endpoint path |
| `proxmox_mcp_proxmox_host` | `pve3.myrobertson.net` | Proxmox API target |
| `proxmox_mcp_verify_tls` | `true` | Validate the Proxmox API certificate |
| `proxmox_mcp_allow_elevated` | `false` | Enable write/destructive tooling |
| `proxmox_mcp_allowed_client_cidrs` | `192.168.1.0/24` | Networks allowed to reach the port |

## Client configuration

```bash
claude mcp add --transport http proxmox http://mcp0.myrobertson.net:8096/mcp
```

## Verifying

```bash
systemctl status proxmox-mcp
curl -fsS http://mcp0.myrobertson.net:8096/healthz
journalctl -u proxmox-mcp -n 50
```
