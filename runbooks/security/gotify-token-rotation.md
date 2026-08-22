# Rotating Gotify application tokens

All Gotify application tokens live under `secret/monitoring/gotify/<app>`.
As of 2026-08-22 there are six, all rotated that day:

| Vault path | Gotify app | Consumer |
|---|---|---|
| `monitoring/gotify/prod` | prod | gotify-bridge, gotify-bridge-flux (prod cluster) **and** the PBS notification target |
| `monitoring/gotify/staging` | staging | gotify-bridge, gotify-bridge-flux (staging cluster) |
| `monitoring/gotify/proxmox` | proxmox | PVE, via `ansible/proxmox/pve_notification_targets.yml` |
| `monitoring/gotify/prowlarr` | prowlarr | Prowlarr notification connection |
| `monitoring/gotify/tautulli` | tautulli | Tautulli notification agent |
| `monitoring/gotify/seerr` | seerr | Seerr (formerly Overseerr) notification agent |

## The shape of a rotation

Gotify 2.7.3 has **no in-place regeneration**. `/application/{id}/token`,
`/application/{id}/regenerate` and even `GET /application/{id}` are 404; only the
collection `GET /application` exists. So rotating is:

1. Create a replacement application (`<name>-v2`).
2. Copy its token into Vault — `scripts/clip-to-vault.sh secret/monitoring/gotify/<app> token A`.
   The UI shows tokens persistently, so use its copy button; nothing needs to be
   read into a terminal or a transcript.
3. Update the consumer (below) and **prove delivery**.
4. Delete the old application — which destroys that application's message
   history. There is no way to keep it.
5. Rename `<name>-v2` back to `<name>` via `PUT /application/{id}`.

Never delete the old application before the replacement has actually delivered.
`lastUsed` is a poor check here (see below); look for a real message.

## Judging whether a token is still in use

**Do not trust `lastUsed`.** The cheap validity probe —
`POST /message` with a deliberately invalid body, 400 = good token, 401 = revoked
— creates no message but Gotify authenticates *before* validating the payload,
so it stamps `lastUsed`. On 2026-08-22 that set five applications to the same
second and destroyed the idle-time evidence being relied on.

Use the **newest message date** instead: `GET /application/{id}/message?limit=1`.

## Per-consumer update

- **prod / staging** — write Vault, force the VSO sync, then
  `rollout restart` BOTH `gotify-bridge` and `gotify-bridge-flux`. They take the
  token as an env var, so updating the Secret does not reach a running pod. The
  bridge containers are distroless: `kubectl exec` fails and returns an empty
  string that looks exactly like a token mismatch. Verify by POSTing an alert to
  `http://gotify-bridge/gotify_webhook` and watching the app's message list.
  Then re-run `ansible/proxmox/pbs_notification_targets.yml` for PBS.
- **proxmox** — write Vault, re-run `ansible/proxmox/pve_notification_targets.yml`.
  It reads the token from Vault and sends its own test notification.
- **prowlarr** — `PUT /api/v1/notification/{id}` with the full object, API key
  from `/config/config.xml`. **Prowlarr masks secret fields on read**: the GET
  returns `appToken="********"`, so hashing what you read back proves nothing.
  Verify with `POST /api/v1/notification/test` and check Gotify.
- **tautulli** — `cmd=set_notifier_config`, API key from `/config/config.ini`.
  Two traps, both of which bit on 2026-08-22:
  - Config options take the **agent name** as prefix: `gotify_app_token`,
    `gotify_host`. Not `config_*`, not the bare key.
  - `agent_id` is applied as truth. Passing the wrong id silently **converts the
    notifier to that agent** and clears its config, while still returning
    `{"result": "success"}`. Gotify is **29**; 27 is LunaSea. Trigger flags and
    message templates live in their own columns and survive, but `host`,
    `app_token` and the `incl_*` options must be restored — the defaults are in
    the `GOTIFY._DEFAULT_CONFIG` dict in `plexpy/notifiers.py`.
  Send a test with `cmd=notify&notifier_id=<id>`.
- **seerr** — `POST /api/v1/settings/notifications/gotify` with
  `{enabled, types, options}`, API key from `main.apiKey` in
  `/app/config/settings.json`. Use the API rather than editing the file so seerr
  reloads its own config. Test endpoint is the same path with `/test`.

## Adopting rather than rotating

If a token exists in Gotify but not in Vault, do NOT stamp `rotated_at` as
today — that resets its age to zero and hides it. Use the oldest surviving
message for that application as a lower bound, and say so in `custom_metadata`:

    ROTATED_AT=2025-08-09 scripts/clip-to-vault.sh secret/monitoring/gotify/<app> token A
