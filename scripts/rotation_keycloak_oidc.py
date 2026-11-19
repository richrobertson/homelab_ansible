"""keycloak_oidc_client rotation kind for rotate_due_k8s_secrets.py.

Rotates the secret of a CONFIDENTIAL Keycloak OIDC client together with every
place that holds it, so all copies always agree:

  Keycloak   the client itself (kcadm inside the Keycloak pod, merge-update of
             `secret` only - read from stdin, never argv).
  Vault      the map key (and optional `copies`). The Keycloak client-bootstrap
             Job (homelab_flux apps/base/keycloak/grafana-clients-job.yaml) sets
             client secrets from the VSS-synced Secrets on every new Job version,
             so the Vault value it reads MUST change in the same rotation, or the
             next Job version silently reinstates the old secret.
  k8s        every VaultStaticSecret (force-synced, then confirmed) and every
             plain, hand-made Secret in `secrets` (patched over stdin).
  app        the relying party's own stored copy (see APP KINDS), optionally
             followed by a restart that must reproduce the same value.

Order: pre-flight (old value accepted by Keycloak, every copy holds it - else
refuse without changing anything) -> Vault -> k8s Secrets -> Keycloak -> app
-> [restart] -> verify. Keycloak and app are changed back-to-back, so the
window in which a NEW interactive login fails is a few seconds; existing
sessions are unaffected.

Verify: the realm token endpoint is called with grant_type=authorization_code
and a bogus code. `invalid_grant` means client authentication PASSED (the code
was the only thing wrong); `invalid_client`/`unauthorized_client` means the
secret was refused. New value must pass, old value must be refused - from the
runner AND, for the app, with the secret the app itself has stored, over the
app's own network path.

On ANY failure after the first write: Keycloak and app set back to the old
value, Vault rolled back, Secrets restored, [restart], and the OLD value is
verified working again. A failure in that rollback raises RuntimeError.

APP KINDS
  nextcloud_user_oidc  user_oidc providers (`providers`) updated in place with
                       `occ user_oidc:provider <id> --clientsecret-env` (the
                       secret goes in over stdin); verified by decrypting the
                       stored secret in-process and using it against the token
                       endpoint from inside the pod. The chart's before-starting
                       hook re-applies the provider secret from the env Secret on
                       every start, so `restart: true` proves the Secret and the
                       DB agree.

  synology_dsm_sso     the DSM OIDC SSO client of one NAS (scripts/rotation_synology.py
                       DsmSso): the live profile is re-set with only the secret
                       changed, then read back on the NAS and used against the
                       token endpoint DSM itself has configured. app spec:
                       {kind, nas: {name, host, port}, ssh_credentials: <vault path>}

Map entry (ansible/k8s/vars/k8s_secret_consumers.yml):
  <vault path>:
    kind: keycloak_oidc_client
    kube_context: admin@prod | admin@staging
    field: OIDC_CLIENT_SECRET          client_id_field: OIDC_CLIENT_ID
    keycloak: {namespace, deployment, realm, token_endpoint}
    copies:  [{path, field}]                        (optional, other Vault paths)
    vss:     [{namespace, name}]                    (VaultStaticSecret = Secret name)
    secrets: [{namespace, name, field}]             (optional, hand-made Secrets)
    app: {kind: nextcloud_user_oidc, namespace, deployment, container,
          providers: [..], restart: true|false}
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

ACCEPTED, REFUSED = "accepted", "refused"

# kcadm in the Keycloak pod. Positional args: realm, mode (set|check).
# stdin: client id, then the secret. Prints only status words.
_KC_SCRIPT = r"""
set -eu
read -r CID; read -r S
cfg=$(mktemp); trap 'rm -f "$cfg"' EXIT
cd /opt/keycloak
bin/kcadm.sh config credentials --config "$cfg" --server http://localhost:8080 --realm master \
  --user admin --password "${KC_BOOTSTRAP_ADMIN_PASSWORD:-$KEYCLOAK_ADMIN_PASSWORD}" >/dev/null 2>&1 \
  || { echo kc-login-failed; exit 1; }
# (the image has no awk/grep: parse with the shell)
list=$(bin/kcadm.sh get clients --config "$cfg" -r "$1" -q clientId="$CID" --fields id,clientId --format csv --noquotes)
ids=; n=0
while IFS=, read -r i c; do
  if [ "$c" = "$CID" ]; then ids=$i; n=$((n + 1)); fi
done <<EOF
$list
EOF
[ "$n" = 1 ] || { echo kc-client-not-unique; exit 1; }
if [ "$2" = set ]; then
  printf '{"secret":"%s"}' "$S" | bin/kcadm.sh update "clients/$ids" --config "$cfg" -r "$1" --merge -f - >/dev/null
fi
cur=$(bin/kcadm.sh get "clients/$ids/client-secret" --config "$cfg" -r "$1" --fields value --format csv --noquotes)
[ "$cur" = "$S" ] && echo kc-match || echo kc-mismatch
"""

# user_oidc, inside the Nextcloud container. stdin: the secret, then the client
# id. Positional args: provider identifiers. --clientid is REQUIRED: user_oidc
# 8.x does not count --clientsecret-env as an update option, so with it alone
# the command just prints the provider and silently changes nothing.
_NC_SET = r"""
set -eu
read -r S; read -r CID
for p in "$@"; do
  NC_ROTATION_SECRET="$S" php occ user_oidc:provider "$p" --clientid="$CID" \
    --clientsecret-env=NC_ROTATION_SECRET >/dev/null
done
echo nc-set
"""

# Decrypts each provider's stored secret, compares it with stdin and uses it
# against the provider's token endpoint through Nextcloud's own HTTP client.
# Prints one line per provider: "<id> match|mismatch <token-endpoint error>".
_NC_CHECK_PHP = r"""
require_once '/var/www/html/lib/base.php';
\OC_App::loadApp('user_oidc');
$want = rtrim(fgets(STDIN), "\r\n");
$crypto = \OC::$server->get(\OCP\Security\ICrypto::class);
$mapper = \OC::$server->get(\OCA\UserOIDC\Db\ProviderMapper::class);
$http = \OC::$server->get(\OCP\Http\Client\IClientService::class)->newClient();
foreach (array_slice($argv, 1) as $id) {
    try {
        $p = $mapper->findProviderByIdentifier($id);
    } catch (\Throwable $e) {
        echo "$id missing -\n";
        continue;
    }
    try {
        $have = $crypto->decrypt($p->getClientSecret());
    } catch (\Throwable $e) {
        echo "$id undecryptable -\n";
        continue;
    }
    $disc = json_decode($http->get($p->getDiscoveryEndpoint())->getBody(), true);
    $r = $http->post($disc['token_endpoint'], ['http_errors' => false, 'form_params' => [
        'grant_type' => 'authorization_code', 'code' => 'rotation-probe-invalid-code',
        'redirect_uri' => 'https://invalid.example/', 'client_id' => $p->getClientId(),
        'client_secret' => $have]]);
    $err = json_decode((string)$r->getBody(), true)['error'] ?? ('http-' . $r->getStatusCode());
    echo $id, ' ', (hash_equals($want, $have) ? 'match' : 'mismatch'), ' ', $err, "\n";
}
"""


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # Staging's SSO certificate is issued by the homelab PKI; the runner mounts
    # that chain for Vault already.
    for var in ("ROTATION_EXTRA_CA", "VAULT_CACERT"):
        path = os.environ.get(var)
        if path and os.path.isfile(path):
            ctx.load_verify_locations(cafile=path)
    return ctx


def client_auth(token_endpoint: str, client_id: str, secret: str, step_error: type) -> str:
    """ACCEPTED if Keycloak authenticated the client, REFUSED if it rejected the
    secret. Anything else is an error: we must never mistake an outage for a
    refusal."""
    if not token_endpoint.startswith("https://"):
        raise step_error("token_endpoint must be https")
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": "rotation-probe-invalid-code",
        "redirect_uri": "https://invalid.example/", "client_id": client_id, "client_secret": secret,
    }).encode()
    req = urllib.request.Request(token_endpoint, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          # Cloudflare (prod SSO) answers 403 to urllib's default UA.
                                          "User-Agent": "homelab-secret-rotation/1"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as r:  # nosec B310 - https checked above
            raise step_error(f"token endpoint accepted a bogus code (HTTP {r.status})")
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read() or b"{}").get("error")
        except ValueError:
            err = None
        if err == "invalid_grant":
            return ACCEPTED
        if err in ("invalid_client", "unauthorized_client"):
            return REFUSED
        raise step_error(f"token endpoint: unexpected HTTP {exc.code} error={err}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise step_error(f"token endpoint unreachable: {exc}") from None


def rotate_keycloak_oidc_client(name: str, spec: dict, ctx: str, run: Callable, vault_json: Callable,
                                kubectl: Callable, sync_and_confirm: Callable, restart: Callable,
                                log: Callable, step_error: type) -> None:
    field, kc, app = spec["field"], spec["keycloak"], spec.get("app")
    copies, plain = spec.get("copies") or [], spec.get("secrets") or []
    token_ep = kc["token_endpoint"]

    cur = vault_json("kv", "get", "-mount=secret", name)["data"]
    client_id, old = cur["data"][spec["client_id_field"]], cur["data"][field]
    versions = {name: cur["metadata"]["version"]}
    for c in copies:
        versions[c["path"]] = vault_json("kv", "get", "-mount=secret", c["path"])["data"]["metadata"]["version"]

    def wait_auth(secret: str, want: str) -> None:
        # Keycloak replicas share the DB; an update can take a moment to reach
        # every node's cache.
        got = None
        for _ in range(12):
            got = client_auth(token_ep, client_id, secret, step_error)
            if got == want:
                return
            time.sleep(5)
        raise step_error(f"Keycloak {client_id}: expected {want}, got {got}")

    def keycloak(mode: str, secret: str) -> None:
        out = kubectl(ctx, "-n", kc["namespace"], "exec", "-i", f"deploy/{kc['deployment']}", "-c", "keycloak",
                      "--", "sh", "-c", _KC_SCRIPT, "kc", kc["realm"], mode,
                      stdin=f"{client_id}\n{secret}\n").strip()
        if out != "kc-match":
            raise step_error(f"Keycloak {client_id} ({mode}): {out or 'no output'}")

    def plain_secrets(secret: str, check_only: bool = False) -> None:
        for s in plain:
            if not check_only:
                patch = {"metadata": {"annotations": {
                    # kubectl apply stored the hand-made Secret in plaintext here;
                    # never leave a stale copy behind.
                    "kubectl.kubernetes.io/last-applied-configuration": None}},
                    "data": {s["field"]: base64.b64encode(secret.encode()).decode()}}
                kubectl(ctx, "-n", s["namespace"], "patch", "secret", s["name"], "--type=merge",
                        "--patch-file=/dev/stdin", stdin=json.dumps(patch))
            d = json.loads(kubectl(ctx, "-n", s["namespace"], "get", "secret", s["name"], "-o", "json"))
            if base64.b64decode((d.get("data") or {}).get(s["field"], "")).decode() != secret:
                raise step_error(f"Secret {s['namespace']}/{s['name']} does not hold the expected value")

    def app_exec(*args: str, stdin: str) -> str:
        return kubectl(ctx, "-n", app["namespace"], "exec", "-i", f"deploy/{app['deployment']}",
                       "-c", app.get("container", "nextcloud"), "--", *args, stdin=stdin, timeout=180)

    dsm = None
    if app and app.get("kind") == "synology_dsm_sso":
        from rotation_synology import DsmSso  # paramiko only needed for this app kind
        dsm = DsmSso(app, vault_json, step_error)
    elif app and app.get("kind", "nextcloud_user_oidc") != "nextcloud_user_oidc":
        raise step_error(f"unknown app kind {app.get('kind')}")

    def app_set(secret: str) -> None:
        if dsm:
            dsm.set(secret, client_id)
        elif app:
            app_exec("sh", "-c", _NC_SET, "nc", *app["providers"], stdin=f"{secret}\n{client_id}\n")

    def app_check(secret: str) -> None:
        if dsm:
            dsm.check(secret, client_id)
            return
        if not app:
            return
        out = ""
        for _ in range(6):  # a freshly restarted pod may still be warming up
            try:
                out = app_exec("php", "-r", _NC_CHECK_PHP, "--", *app["providers"], stdin=secret + "\n").strip()
            except step_error as exc:
                out = str(exc)
            lines = out.splitlines()
            if len(lines) == len(app["providers"]) and all(ln.split()[1:] == ["match", "invalid_grant"]
                                                           for ln in lines):
                return
            time.sleep(10)
        raise step_error(f"app {app['namespace']}/{app['deployment']} check failed: {out[-300:]}")

    def app_restart() -> None:
        if app and not dsm and app.get("restart"):
            restart(ctx, {"restart": [{"namespace": app["namespace"], "deployment": app["deployment"]}]})

    def vss_check(secret: str) -> None:
        # Read-only form of sync_and_confirm: every synced Secret holds `secret`.
        for v in spec.get("vss") or []:
            d = json.loads(kubectl(ctx, "-n", v["namespace"], "get", "secret", v["name"], "-o", "json"))
            if base64.b64decode((d.get("data") or {}).get(field, "")).decode() != secret:
                raise step_error(f"Secret {v['namespace']}/{v['name']} does not hold Vault's current value")

    # Pre-flight: everything must agree on the OLD value before we touch anything,
    # otherwise "rollback" would have no consistent state to return to.
    for c in copies:
        if vault_json("kv", "get", "-mount=secret", c["path"])["data"]["data"].get(c["field"]) != old:
            raise step_error(f"pre-flight: Vault copy {c['path']} differs from {name}; refusing")
    if client_auth(token_ep, client_id, old, step_error) != ACCEPTED:
        raise step_error(f"pre-flight: Keycloak refuses the CURRENT Vault secret for {client_id}; refusing")
    keycloak("check", old)
    vss_check(old)
    plain_secrets(old, check_only=True)
    app_check(old)
    log(f"  pre-flight ok: Keycloak, Vault, Secrets and app agree on the current {client_id} secret")

    if os.environ.get("ROTATION_PREFLIGHT_ONLY") == name:
        raise step_error("pre-flight only (ROTATION_PREFLIGHT_ONLY): nothing changed")
    new = secrets.token_urlsafe(32)
    try:
        run(["vault", "kv", "patch", "-mount=secret", name, "-"], stdin=json.dumps({field: new}))
        for c in copies:
            run(["vault", "kv", "patch", "-mount=secret", c["path"], "-"], stdin=json.dumps({c["field"]: new}))
        if spec.get("vss"):
            sync_and_confirm(ctx, spec, field, new)
        plain_secrets(new)
        keycloak("set", new)
        app_set(new)
        log("  Keycloak, Vault, Secrets and app updated; verifying")
        if os.environ.get("ROTATION_TEST_FAIL") == name:
            raise step_error("forced failure (ROTATION_TEST_FAIL) to exercise rollback")
        app_restart()
        wait_auth(new, ACCEPTED)
        wait_auth(old, REFUSED)
        app_check(new)
    except (step_error, KeyError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        log(f"  FAILED {name}: {exc}; restoring the old secret everywhere")
        try:
            keycloak("set", old)
            app_set(old)
            for p, v in versions.items():
                if vault_json("kv", "get", "-mount=secret", p)["data"]["metadata"]["version"] != v:
                    run(["vault", "kv", "rollback", "-mount=secret", f"-version={v}", p])
            if spec.get("vss"):
                sync_and_confirm(ctx, spec, field, old)
            plain_secrets(old)
            app_restart()
            wait_auth(old, ACCEPTED)
            app_check(old)
        except Exception as exc2:  # any rollback failure must surface as RuntimeError
            raise RuntimeError(f"ROLLBACK FAILED for {name}: {exc2} (original error: {exc})") from exc2
        raise step_error(f"{exc} (rolled back; old secret verified working)") from exc

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for p in versions:
        run(["vault", "kv", "metadata", "patch", "-mount=secret", f"-custom-metadata=rotated_at={stamp}", p])
