"""nextcloud_admin rotation kind for rotate_due_k8s_secrets.py.

The Nextcloud image reads NEXTCLOUD_ADMIN_USER/PASSWORD only on FIRST INSTALL;
afterwards the account lives in Nextcloud's database. So the k8s_vss pattern
(patch Vault, restart) would change nothing: the real password is changed with
`occ user:resetpassword --password-from-env`, and Vault is kept in step with it.

Per run, strictly in this order:
  0. precheck - the CURRENT Vault password must log in (OCS 200) and every
     exporter must report nextcloud_up 1; otherwise refuse, nothing touched.
  1. vault kv patch <field> only (Vault first: if the runner dies mid-way, the
     new value is never lost - it is in Vault, the old one in Vault's history).
  2. occ user:resetpassword in the Nextcloud container (value via stdin -> env).
  3. force-sync every VaultStaticSecret, confirm each Secret holds the new value.
  4. restart the listed consumers (the metrics exporters - NOT Nextcloud itself:
     it uses Recreate, a restart is a ~3 min outage, and buys nothing).
  5. verify: new password logs in over OCS (200) and the Database backend
     accepts it, the Database backend REFUSES the old one (no failed HTTP
     login, so no brute-force throttle), every exporter nextcloud_up 1.
On ANY failure after step 1: occ reset back to the old password, vault kv
rollback, resync, restart, verify the OLD password logs in and the exporters
are up. A failure there is raised as RuntimeError (the caller's "ROLLBACK
FAILED, manual action" path).

Map entry (ansible/k8s/vars/k8s_secret_consumers.yml):
  nextcloud/<env>/app:
    kind: nextcloud_admin
    kube_context: admin@prod | admin@staging
    field: NEXTCLOUD_ADMIN_PASSWORD        user_field: NEXTCLOUD_ADMIN_USER
    instance: {namespace, deployment, container}   (where occ + the checks run)
    vss:      [{namespace, name}]
    restart:  [{namespace, deployment}]
    exporters: [<nextcloud-exporter /metrics URL reachable from the instance>]

Secrets never reach a command line, a log or an argv inside the pod: they go
over stdin to vault/kubectl exec, into occ through OC_PASS and into curl
through a `-K -` config read from stdin.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import string
import subprocess
import time
from collections.abc import Callable

_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.:/_-]+$")
_TRANSIENT = re.compile(r"container not found|unable to upgrade connection|pod does not exist|"
    r"no such container|is not running|ContainerCreating|PodInitializing|connection refused")

# Runs as root in the Nextcloud container; occ must run as the config owner.
_RESET = (
    'read -r U; read -r P; export U; OC_PASS="$P"; export OC_PASS; '
    "su -s /bin/sh www-data -c 'cd /var/www/html && php occ user:resetpassword --password-from-env \"$U\"' "
    ">/dev/null"
)
# One curl per line of stdin: "<user>\n<password>" -> HTTP status of an
# authenticated OCS call against the local Apache.
_AUTH = (
    'read -r U; read -r P; printf \'user = "%s:%s"\\n\' "$U" "$P" | '
    'curl -s -m 20 -o /dev/null -w "%{http_code}" -K - -H "OCS-APIRequest: true" '
    '"http://127.0.0.1/ocs/v2.php/cloud/user?format=json"'
)
# "<user>\n<password>" -> accepted|refused, asked of the Database user backend
# directly. Used for the NEGATIVE check: an OCS login with a wrong password
# falls through to user_ldap (500 on staging, whose LDAP bind is broken) and
# feeds the brute-force throttle; the backend check does neither and tests
# exactly what matters - the stored hash no longer matches the old password.
_DB_CHECK = (
    'read -r U; read -r P; export U; OC_PASS="$P"; export OC_PASS; '
    "su -s /bin/sh www-data -c 'cd /var/www/html && php -r '\"'\"'require \"lib/base.php\"; "
    "$r = (new \\OC\\User\\Database())->checkPassword(getenv(\"U\"), getenv(\"OC_PASS\")); "
    "echo $r === false ? \"refused\" : \"accepted\";'\"'\"' 2>/dev/null | tail -c 20'"
)


def _generate() -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(40))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def rotate_nextcloud_admin(name: str, spec: dict, ctx: str, run: Callable, kubectl: Callable,
                           vault_json: Callable, sync_and_confirm: Callable, restart: Callable,
                           log: Callable, step_error: type) -> None:
    inst, field = spec["instance"], spec["field"]
    exporters = spec.get("exporters") or []
    for u in exporters:
        if not _URL_RE.match(u):
            raise step_error(f"refusing odd exporter URL {u!r}")

    def exec_(script: str, stdin: str = "") -> str:
        # Every script here is idempotent, so a pod that is restarting (Nextcloud
        # uses Recreate: ~3 min with no pod at all) is waited out, not fatal -
        # least of all in the middle of a rollback.
        for attempt in range(24):
            try:
                return kubectl(ctx, "-n", inst["namespace"], "exec", "-i", f"deploy/{inst['deployment']}",
                               "-c", inst.get("container", "nextcloud"), "--", "sh", "-c", script,
                               stdin=stdin, timeout=180).strip()
            except step_error as exc:
                if attempt == 23 or not _TRANSIENT.search(str(exc)):
                    raise
                log(f"  instance pod not reachable ({str(exc)[-80:]}); retrying in 15s")
                time.sleep(15)
        raise step_error("unreachable")

    def auth(user: str, pw: str) -> str:
        return exec_(_AUTH, f"{user}\n{pw}\n")

    def exporters_up() -> list[str]:
        bad = []
        for u in exporters:
            try:
                body = exec_(f"curl -s -m 20 {u}")
            except step_error:
                body = ""
            if not re.search(r"^nextcloud_up 1$", body, re.MULTILINE):
                bad.append(u)
        return bad

    def wait_exporters() -> None:
        for _ in range(9):
            bad = exporters_up()
            if not bad:
                return
            time.sleep(10)
        raise step_error(f"exporter(s) not reporting nextcloud_up 1: {', '.join(bad)}")

    def verify(user: str, good: str, bad: str | None) -> None:
        code = ""
        for _ in range(6):
            code = auth(user, good)
            if code == "200":
                break
            time.sleep(10)
        else:
            raise step_error(f"expected password refused (OCS HTTP {code})")
        if bad is not None:
            if exec_(_DB_CHECK, f"{user}\n{good}\n") != "accepted":
                raise step_error("Database backend does not accept the expected password")
            if (res := exec_(_DB_CHECK, f"{user}\n{bad}\n")) != "refused":
                raise step_error(f"previous password NOT refused by the Database backend ({res!r})")
        wait_exporters()

    cur = vault_json("kv", "get", "-mount=secret", name)["data"]
    old_version, old, user = cur["metadata"]["version"], cur["data"][field], cur["data"][spec["user_field"]]
    if not _USER_RE.match(user):
        raise step_error("admin user name in Vault has unexpected characters; refusing")

    # 0. precheck - Vault must match reality before we touch anything.
    code = auth(user, old)
    if code != "200":
        raise step_error(f"current Vault password does not log in (OCS HTTP {code}); refusing, nothing changed")
    if bad := exporters_up():
        raise step_error(f"exporter(s) already unhealthy before rotation: {', '.join(bad)}; refusing")

    new = _generate()
    run(["vault", "kv", "patch", "-mount=secret", name, "-"], stdin=json.dumps({field: new}))
    try:
        exec_(_RESET, f"{user}\n{new}\n")
        log("  occ reset done; syncing Secrets")
        sync_and_confirm(ctx, spec, field, new)
        restart(ctx, spec)
        verify(user, new, old)
    except (step_error, subprocess.TimeoutExpired, KeyError, ValueError) as exc:
        log(f"  FAILED {name}: {exc}; rolling back to v{old_version}")
        try:
            exec_(_RESET, f"{user}\n{old}\n")
            run(["vault", "kv", "rollback", "-mount=secret", f"-version={old_version}", name])
            sync_and_confirm(ctx, spec, field, old)
            restart(ctx, spec)
            verify(user, old, None)
        except (step_error, subprocess.TimeoutExpired, KeyError, ValueError) as exc2:
            raise RuntimeError(f"ROLLBACK FAILED for {name}: {exc2} (original error: {exc})") from exc2
        raise step_error(f"{exc} (rolled back; old password verified working)") from exc

    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run(["vault", "kv", "metadata", "patch", "-mount=secret", f"-custom-metadata=rotated_at={stamp}", name])
