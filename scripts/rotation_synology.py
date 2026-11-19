"""Synology DSM rotation support for rotate_due_k8s_secrets.py.

Two things live here, both talking to the NAS over SSH (paramiko, host keys
pinned in ansible/k8s/vars/synology_known_hosts, password auth, `sudo -S`):

1. kind `synology_local_account` - rotates the password of a LOCAL DSM account
   that exists on several NAS with the same password (svc-syno-ssh, Vault
   synology/dsm-admin/local-ssh-account).

   Consumers of that password, and how each is kept in step:
     DSM itself       `synouser --setpw` on EVERY NAS listed in `nas`.
     Snapshot         each NAS's mainsite DR plans authenticate to the partner
     Replication      as this account. DSM stores a session (node_cred.session),
                      not the password; whether DSM revokes it on a password
                      change is undocumented, so every mainsite plan is re-pointed
                      with `synodr plan edit_conn` (the operation
                      ansible/synology/repoint_snapshot_replication_credentials.yml
                      proved does NOT re-baseline: history and counters are kept)
                      after `plan test_conn` proved the partner accepts it.
     k8s              VaultStaticSecret(s) in `vss` (force-synced + confirmed),
                      then the `restart` deployments; `exporters` must report
                      synology_replication_up 1 for every NAS on a FRESH scrape.
                      The staging cluster's copy of the exporter is restarted by
                      VSO itself (rolloutRestartTargets on the shared VSS), as
                      this runner cannot reach the staging API.
     Ansible          every playbook reads Vault at run time - nothing to do.

   Order: pre-flight (old password logs in on every NAS, every plan ready and
   idle, test_conn ok, exporter healthy - else refuse, nothing changed) ->
   Vault (new value never lost) -> setpw on every NAS -> fresh SSH login with the
   new password works AND the old one is refused -> test_conn + edit_conn every
   plan -> every plan ready -> VSS sync -> restart -> exporter fresh-scrape ok.

   Rollback (any failure after the Vault write): setpw back to the old password
   on every NAS that took the new one, old login verified, EVERY plan
   edit_conn'd back (DSM may revoke sessions on any password change), and ONLY
   THEN Vault rolled back + resync + restart. If a NAS refuses the old password
   (e.g. scooter's 2-deep password history) the others are set FORWARD to the
   new one instead, Vault keeps the new value (both versions stay in its
   history) and RuntimeError says the plans still need re-pointing.

2. app kind `synology_dsm_sso` for rotation_keycloak_oidc.py - the DSM OIDC SSO
   client (SYNO.Core.Directory.OIDC.SSO) that holds the Keycloak client secret.
   `set` reads DSM's CURRENT profile and re-sets it with only the secret changed
   (the live profile, not configure_keycloak_sso.yml's, is the truth: kermit's
   single redirect URI is deliberate). `check` reads the stored secret back ON
   the NAS, compares it to the expected one and uses it against the token
   endpoint DSM itself has configured, from the NAS (its own network path).

Secrets never reach a log or the runner's argv: they travel inside the SSH
channel on stdin. On the NAS, `synouser --setpw`, `synodr` and `synowebapi` only
take them as arguments (root processes, milliseconds) - the same exposure the
existing playbooks accept; NAS shell access is limited to administrators.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import string
import time
from collections.abc import Callable

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWN_HOSTS = os.path.join(REPO, "ansible/k8s/vars/synology_known_hosts")
SYNODR = "/var/packages/SnapshotReplication/target/sbin/synodr"
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_PLAN_RE = re.compile(
    r"planId: \[(?P<id>[0-9a-f-]{36})\][\s\S]*?target: \[(?P<target>[^\]]*)\][\s\S]*?"
    r"status: \[(?P<status>[^\]]*)\][\s\S]*?opstatus: \[(?P<op>[^\]]*)\]"
    r"(?P<rest>[\s\S]*?)(?=planId: \[|\Z)")
_CONN_RE = re.compile(r"remote conn\[(\d+)\]: conn\[([0-9A-Za-z.-]+)/(\d+)/(https?)\]/ credId\[[^\]]*\]/ "
                      r"replicaConn\[[^/\]]+/(\d+)/(\d+)\]")


def generate_password() -> str:
    """32 chars meeting DSM's strong-password policy (mixed case, digit, special),
    shell- and URL-safe."""
    alphabet = string.ascii_letters + string.digits + "-_"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(32))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)
                and any(c in "-_" for c in pw) and pw[0] not in "-_"):
            return pw


class Nas:
    """One NAS, reached over SSH as `user`. Every call opens a fresh connection:
    a login IS the check, and there is never a stale session to mislead us."""

    def __init__(self, name: str, host: str, port: int, user: str, step_error: type,
                 known_hosts: str = KNOWN_HOSTS):
        if not _HOST_RE.match(host) or not _USER_RE.match(user):
            raise step_error(f"refusing odd NAS host/user {host!r}/{user!r}")
        self.name, self.host, self.port, self.user = name, host, int(port), user
        self.known_hosts, self.err = known_hosts, step_error

    def _client(self, password: str):
        import paramiko  # only this kind needs it
        c = paramiko.SSHClient()
        c.load_host_keys(self.known_hosts)
        c.set_missing_host_key_policy(paramiko.RejectPolicy())
        c.connect(self.host, port=self.port, username=self.user, password=password, timeout=20,
                  banner_timeout=20, auth_timeout=20, look_for_keys=False, allow_agent=False)
        return c

    def login_ok(self, password: str) -> bool:
        """True if `password` logs in, False if it is REFUSED. Anything else
        (unreachable, host key mismatch) raises: an outage is never a refusal."""
        import paramiko
        try:
            self._client(password).close()
            return True
        except paramiko.AuthenticationException:
            return False
        except (paramiko.SSHException, OSError) as exc:
            raise self.err(f"{self.name}: SSH failed: {type(exc).__name__}: {str(exc)[:120]}") from None

    def sudo(self, password: str, script: str, *args: str, stdin: str = "", timeout: int = 300) -> str:
        """Run `script` as root via sudo -S. The password is the first stdin line
        (eaten by sudo), `stdin` follows for the script's own `read`s."""
        import paramiko
        # -k: always prompt, so the first stdin line is ALWAYS sudo's (a cached
        # timestamp would otherwise hand the password to the script's `read`).
        cmd = "sudo -k -S -p '' sh -c " + shlex.quote(script) + " sh " + " ".join(shlex.quote(a) for a in args)
        try:
            c = self._client(password)
        except paramiko.AuthenticationException:
            raise self.err(f"{self.name}: SSH login refused") from None
        except (paramiko.SSHException, OSError) as exc:
            raise self.err(f"{self.name}: SSH failed: {type(exc).__name__}: {str(exc)[:120]}") from None
        try:
            i, o, e = c.exec_command(cmd, timeout=timeout)  # nosec B601 - fixed script, every arg shlex-quoted
            i.write(password + "\n" + stdin)
            i.flush()
            i.channel.shutdown_write()
            out = o.read().decode(errors="replace")
            rc = o.channel.recv_exit_status()
            err = e.read().decode(errors="replace")
        finally:
            c.close()
        if rc != 0:
            raise self.err(f"{self.name}: remote script exit {rc}: {(out + err).strip()[-200:]}")
        return out


# --------------------------------------------------------------------------
# kind synology_local_account
# --------------------------------------------------------------------------

_SETPW = 'read -r U; read -r P; /usr/syno/sbin/synouser --setpw "$U" "$P" >/dev/null 2>&1 && echo setpw-ok'

# $1 local_user/account, then per connection: addr port protocol replica_port.
# Prints one status word per connection; synodr's output is reduced to the
# success flag / error code so nothing it might echo can leak.
_TEST_CONN = r"""
read -r PW; U=$1; shift
while [ $# -ge 4 ]; do
  out=$(""" + SYNODR + r""" plan test_conn "$U" "$1" "$2" "$3" "$U" "$PW" "" btrfs "$4" 2>&1)
  case "$out" in *'"success" : true'*|*'"success":true'*) echo "$1 ok";;
    *) echo "$1 fail $(printf '%s' "$out" | grep -o '"code" *: *[0-9]*' | head -1 | tr -d ' "')";; esac
  shift 4
done
"""
# $1 local_user/account, then per plan: plan_id addr port protocol replica_port.
_EDIT_CONN = r"""
read -r PW; U=$1; shift
while [ $# -ge 5 ]; do
  out=$(""" + SYNODR + r""" plan edit_conn "$1" "$U" "$2" "$3" "$4" "$U" "$PW" "" btrfs "$5" 2>&1)
  case "$out" in *'"success" : true'*|*'"success":true'*) echo "$1 ok";;
    *) echo "$1 fail $(printf '%s' "$out" | grep -o '"code" *: *[0-9]*' | head -1 | tr -d ' "')";; esac
  shift 5
done
"""
_PLAN_LIST = SYNODR + " plan list all mainsite"


def parse_plans(text: str) -> list[dict]:
    plans = []
    for m in _PLAN_RE.finditer(text):
        conns = _CONN_RE.findall(m.group("rest"))
        plans.append({"id": m.group("id"), "target": m.group("target"), "status": m.group("status"),
                      "op": m.group("op"),
                      "conns": [{"addr": c[1], "port": c[2], "protocol": c[3], "replica_port": c[4]}
                                for c in conns]})
    return plans


def rotate_synology_local_account(name: str, spec: dict, ctx: str, run: Callable, vault_json: Callable,
                                  kubectl: Callable, sync_and_confirm: Callable, restart: Callable,
                                  log: Callable, step_error: type) -> None:
    field, user_field = spec["field"], spec["user_field"]
    cur = vault_json("kv", "get", "-mount=secret", name)["data"]
    version, old, user = cur["metadata"]["version"], cur["data"][field], cur["data"][user_field]
    nases = [Nas(n["name"], n["host"], n.get("port", 22), user, step_error) for n in spec["nas"]]
    exporters = spec.get("exporters") or []

    def plans_of(nas: Nas, pw: str) -> list[dict]:
        plans = parse_plans(nas.sudo(pw, _PLAN_LIST))
        for p in plans:
            if len(p["conns"]) != 1:
                raise step_error(f"{nas.name}: plan {p['target']} has {len(p['conns'])} remote conns; refusing")
        return plans

    def require_ready(nas: Nas, pw: str, idle: bool) -> list[dict]:
        plans = plans_of(nas, pw)
        if not plans and spec.get("require_plans", True):
            raise step_error(f"{nas.name}: no mainsite replication plans parsed; refusing")
        bad = [p["target"] for p in plans if p["status"] != "ready" or (idle and p["op"] != "ready")]
        if bad:
            raise step_error(f"{nas.name}: plan(s) not ready/idle: {', '.join(bad)}")
        return plans

    def test_conns(nas: Nas, pw: str, plans: list[dict]) -> None:
        seen = {(c["addr"], c["port"], c["protocol"], c["replica_port"]) for p in plans for c in p["conns"]}
        if not seen:
            return
        out = nas.sudo(pw, _TEST_CONN, user, *[x for s in sorted(seen) for x in s], stdin=pw + "\n")
        bad = [ln for ln in out.split("\n") if ln.strip() and not ln.endswith(" ok")]
        if bad or len(out.split()) < 2 * len(seen):
            raise step_error(f"{nas.name}: replication test_conn failed: {'; '.join(bad) or out.strip()[-120:]}")

    def edit_conns(nas: Nas, pw: str, plans: list[dict]) -> None:
        args = [x for p in plans for c in p["conns"]
                for x in (p["id"], c["addr"], c["port"], c["protocol"], c["replica_port"])]
        if not args:
            return
        out = nas.sudo(pw, _EDIT_CONN, user, *args, stdin=pw + "\n", timeout=900)
        ok = {ln.split()[0] for ln in out.splitlines() if ln.endswith(" ok")}
        missing = [p["target"] for p in plans if p["id"] not in ok]
        if missing:
            raise step_error(f"{nas.name}: edit_conn failed for {', '.join(missing)}: {out.strip()[-160:]}")

    def exporters_ok(since: float) -> None:
        names = {n.name for n in nases}
        last = ""
        for _ in range(18):
            bad = []
            for e in exporters:
                body = kubectl(ctx, "get", "--raw",
                               f"/api/v1/namespaces/{e['namespace']}/services/{e['service']}:{e['port']}/proxy/metrics")
                up = dict(re.findall(r'^synology_replication_up\{nas="([^"]+)"\} ([0-9.e+]+)$', body, re.MULTILINE))
                ts = dict(re.findall(r'^synology_replication_last_scrape_timestamp_seconds\{nas="([^"]+)"\} '
                                     r'([0-9.e+]+)$', body, re.MULTILINE))
                bad += [f"{e['service']}/{n}" for n in names
                        if float(up.get(n, 0)) != 1.0 or float(ts.get(n, 0)) < since]
            if not bad:
                return
            last = ", ".join(bad)
            time.sleep(20)
        raise step_error(f"exporter not healthy on a fresh scrape: {last}")

    # ---- pre-flight: reality must match Vault before anything changes --------
    plans: dict[str, list[dict]] = {}
    for nas in nases:
        if not nas.login_ok(old):
            raise step_error(f"pre-flight: {nas.name} refuses the CURRENT Vault password; refusing")
        plans[nas.name] = require_ready(nas, old, idle=True)
        test_conns(nas, old, plans[nas.name])
    if exporters:
        exporters_ok(0)
    log(f"  pre-flight ok: {len(nases)} NAS accept the current password; "
        f"{sum(len(p) for p in plans.values())} plans ready; test_conn ok")

    if os.environ.get("ROTATION_PREFLIGHT_ONLY") == name:
        raise step_error("pre-flight only (ROTATION_PREFLIGHT_ONLY): nothing changed")
    new = generate_password()
    on_new: list[Nas] = []
    t0 = time.time()
    try:
        run(["vault", "kv", "patch", "-mount=secret", name, "-"], stdin=json.dumps({field: new}))
        for nas in nases:
            on_new.append(nas)  # before the call: even a lost connection may have changed it
            out = nas.sudo(old, _SETPW, stdin=f"{user}\n{new}\n")
            if out.strip() != "setpw-ok":
                raise step_error(f"{nas.name}: setpw failed")
        for nas in nases:
            if not nas.login_ok(new):
                raise step_error(f"{nas.name}: NEW password refused after setpw")
            if nas.login_ok(old):
                raise step_error(f"{nas.name}: OLD password still accepted after setpw")
        log("  DSM password changed on every NAS; new accepted, old refused; re-pointing replication")
        if os.environ.get("ROTATION_TEST_FAIL") == name:
            raise step_error("forced failure (ROTATION_TEST_FAIL) to exercise rollback")
        for nas in nases:
            test_conns(nas, new, plans[nas.name])
        for nas in nases:
            edit_conns(nas, new, plans[nas.name])
        for nas in nases:
            require_ready(nas, new, idle=False)
        if spec.get("vss"):
            sync_and_confirm(ctx, spec, field, new)
        if spec.get("restart"):
            restart(ctx, spec)
        if exporters:
            exporters_ok(t0)
    except Exception as exc:
        log(f"  FAILED {name}: {exc}; restoring the old password everywhere")
        stuck = []
        for nas in on_new:
            try:
                nas.sudo(new, _SETPW, stdin=f"{user}\n{old}\n")
            except Exception as e1:  # noqa: BLE001 - maybe it never took the new one
                log(f"  {nas.name}: setpw back to old: {e1} (checking which password it has)")
            try:
                if not nas.login_ok(old):
                    stuck.append(nas.name)
            except Exception as e2:  # noqa: BLE001
                stuck.append(f"{nas.name} ({e2})")
        if stuck:
            # Converge FORWARD instead: one password on every NAS, the one Vault holds.
            fwd = []
            for nas in on_new:
                if nas.name in stuck:
                    continue
                try:
                    nas.sudo(old, _SETPW, stdin=f"{user}\n{new}\n")
                    fwd.append(nas.name if nas.login_ok(new) else f"{nas.name}(FAILED)")
                except Exception as e2:  # noqa: BLE001
                    fwd.append(f"{nas.name}(FAILED: {e2})")
            raise RuntimeError(
                f"ROLLBACK FAILED for {name}: {', '.join(stuck)} refused the OLD password back (password "
                f"history?), so {', '.join(fwd) or 'no other NAS'} were set FORWARD to the new one. Vault holds "
                f"the NEW value (v{version + 1}; old = v{version}). Replication plans were NOT re-pointed: run "
                f"ansible/synology/repoint_snapshot_replication_credentials.yml or synodr plan edit_conn. "
                f"(original error: {exc})") from exc
        try:
            # Every plan, not just the edited ones: DSM may have revoked the
            # replication sessions when the password changed at all.
            for nas in nases:
                edit_conns(nas, old, plans[nas.name])
            for nas in nases:
                require_ready(nas, old, idle=False)
            if vault_json("kv", "get", "-mount=secret", name)["data"]["metadata"]["version"] != version:
                run(["vault", "kv", "rollback", "-mount=secret", f"-version={version}", name])
            if spec.get("vss"):
                sync_and_confirm(ctx, spec, field, old)
            if spec.get("restart"):
                restart(ctx, spec)
            if exporters:
                exporters_ok(time.time())
        except Exception as exc2:
            raise RuntimeError(f"ROLLBACK FAILED for {name}: every NAS is back on the old password, but: "
                               f"{exc2} (original error: {exc})") from exc2
        raise step_error(f"{exc} (rolled back; old password verified on every NAS)") from exc

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run(["vault", "kv", "metadata", "patch", "-mount=secret", f"-custom-metadata=rotated_at={stamp}", name])


# --------------------------------------------------------------------------
# app kind synology_dsm_sso (used by rotation_keycloak_oidc.py)
# --------------------------------------------------------------------------

# Runs as root on the NAS with DSM's python3. stdin: expected client id, secret.
# argv[1]: set|check. Prints status words only, never a secret.
_SSO_PY = r'''
import hmac, json, subprocess, sys, urllib.error, urllib.parse, urllib.request
API = "/usr/syno/bin/synowebapi"
cid, sec = sys.stdin.readline().rstrip("\n"), sys.stdin.readline().rstrip("\n")
def get():
    r = json.loads(subprocess.run([API, "--exec", "api=SYNO.Core.Directory.OIDC.SSO", "method=get", "version=1"],
                                  capture_output=True, text=True, check=True).stdout)
    if not r.get("success"):
        print("dsm-get-failed"); sys.exit(1)
    return r["data"]
d = get()
if d.get("oidc_client_id") != cid:
    print("dsm-client-id-mismatch"); sys.exit(1)
if sys.argv[1] == "set":
    keep = ["oidc_name", "oidc_wellknown", "oidc_client_id", "oidc_redirect_uri", "oidc_scope", "oidc_user_claim"]
    args = [API, "--exec", "api=SYNO.Core.Directory.OIDC.SSO", "method=set", "version=1", "profile=oidc"]
    args += ["%s=%s" % (k, d[k]) for k in keep]
    args += ["oidc_allow_local_user=" + ("true" if d.get("oidc_allow_local_user") else "false"),
             "oidc_client_secret=" + sec]
    r = subprocess.run(args, capture_output=True, text=True)
    try:
        ok = json.loads(r.stdout).get("success") is True
    except ValueError:
        ok = False
    if not ok:
        print("dsm-set-failed"); sys.exit(1)
    after = get()
    if any(after.get(k) != d.get(k) for k in keep + ["oidc_allow_local_user"]):
        print("dsm-profile-drifted"); sys.exit(1)
    d = after
have = d.get("oidc_client_secret") or ""
match = "match" if hmac.compare_digest(have.encode(), sec.encode()) else "mismatch"
body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": "rotation-probe-invalid-code",
                               "redirect_uri": "https://invalid.example/", "client_id": cid,
                               "client_secret": have}).encode()
req = urllib.request.Request(d["oidc_token_endpoint"], data=body, method="POST", headers={
    "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "homelab-secret-rotation/1"})
try:
    urllib.request.urlopen(req, timeout=20)
    err = "accepted-bogus-code"
except urllib.error.HTTPError as e:
    try:
        err = json.loads(e.read() or b"{}").get("error") or ("http-%d" % e.code)
    except ValueError:
        err = "http-%d" % e.code
except Exception as e:
    err = "unreachable"
print(match, err)
'''


class DsmSso:
    """The DSM side of a keycloak_oidc_client rotation. app spec:
         {kind: synology_dsm_sso, nas: {name, host, port}, ssh_credentials: <vault path>}"""

    def __init__(self, app: dict, vault_json: Callable, step_error: type):
        cred = vault_json("kv", "get", "-mount=secret", app["ssh_credentials"])["data"]["data"]
        n = app["nas"]
        self.nas = Nas(n["name"], n["host"], n.get("port", 22), cred["username"], step_error)
        self._pw, self.err = cred["password"], step_error

    def _run(self, mode: str, client_id: str, secret: str) -> str:
        return self.nas.sudo(self._pw, 'exec python3 -c "$1" "$2"', _SSO_PY, mode,
                             stdin=f"{client_id}\n{secret}\n").strip()

    def set(self, secret: str, client_id: str) -> None:
        # Only the stored value is required here; whether Keycloak already
        # agrees is check()'s job (it retries while Keycloak's cache catches up).
        out = self._run("set", client_id, secret)
        if out.split()[:1] != ["match"]:
            raise self.err(f"DSM {self.nas.name} SSO set: {out[-120:]}")

    def check(self, secret: str, client_id: str) -> None:
        out = ""
        for _ in range(6):  # Keycloak replicas may lag a few seconds
            try:
                out = self._run("check", client_id, secret)
            except self.err as exc:
                out = str(exc)
            if out == "match invalid_grant":
                return
            time.sleep(5)
        raise self.err(f"DSM {self.nas.name} SSO check failed: {out[-120:]}")


# --------------------------------------------------------------------------
# kind mailu_mailbox_password
# --------------------------------------------------------------------------
# A Mailu mailbox whose password is ALSO the SMTP login in DSM's notification
# settings on several NAS (secret/mailu/prod/accounts/nas-notifications).
# Order: prove reality matches Vault (SMTP AUTH with the Vault value; DSM on
# every NAS names this account) -> set in Mailu -> prove new AUTHs / old is
# refused -> Vault -> each NAS's DSM SMTP auth -> read back. Any failure puts
# the old password back in Mailu, Vault and every NAS already touched.

# Runs inside the Mailu admin pod. stdin: the password. Prints a status word only.
_MAILU_SETPW = r'''
import sys, warnings
warnings.filterwarnings("ignore")
from mailu import create_app, models
pw = sys.stdin.read()
app = create_app()
with app.app_context():
    u = models.db.session.get(models.User, sys.argv[1])
    if u is None:
        print("no-such-user"); sys.exit(2)
    u.set_password(pw)
    models.db.session.commit()
    print("setpw-ok")
'''

# Runs as root on the NAS with DSM's python3. stdin: user, password.
# argv[1]: set|get. Prints status words only, never the password. synowebapi
# takes smtp_auth as an argument (as the existing playbook does); it is a
# short-lived root process on the NAS.
_DSM_MAIL_AUTH_PY = r'''
import json, subprocess, sys
API = "/usr/syno/bin/synowebapi"
def call(*a):
    out = subprocess.run([API, "--exec", *a], capture_output=True, text=True, timeout=60).stdout
    i = out.find("{")
    return json.loads(out[i:]) if i >= 0 else {}
if sys.argv[1] == "set":
    user = sys.stdin.readline().rstrip("\n"); pw = sys.stdin.readline().rstrip("\n")
    r = call("api=SYNO.Core.Notification.Mail.Auth", "method=set", "version=1",
             "smtp_auth=" + json.dumps({"enable": True, "user": user, "pass": pw}))
    print("set-ok" if r.get("success") else "set-failed")
r = call("api=SYNO.Core.Notification.Mail.Auth", "method=get", "version=1")
d = r.get("data") or {}
auth = d.get("smtp_auth", d)
print("user=" + str(auth.get("user", "")) + " enable=" + str(auth.get("enable", "")).lower())
'''


def smtp_auth_ok(host: str, port: int, user: str, password: str, step_error: type) -> bool:
    """True if SMTP AUTH succeeds, False if REFUSED (535). Anything else raises."""
    import smtplib
    import ssl
    try:
        s = smtplib.SMTP(host, port, timeout=30)
        s.starttls(context=ssl.create_default_context())
    except (OSError, smtplib.SMTPException) as exc:
        raise step_error(f"SMTP {host}:{port} unreachable: {type(exc).__name__}: {str(exc)[:120]}") from None
    try:
        s.login(user, password)
        return True
    except smtplib.SMTPAuthenticationError:
        return False
    except smtplib.SMTPException as exc:
        raise step_error(f"SMTP AUTH error: {type(exc).__name__}: {str(exc)[:120]}") from None
    finally:
        try:
            s.quit()
        except Exception:  # noqa: BLE001,S110 - best-effort close
            pass


def rotate_mailu_mailbox_password(name: str, spec: dict, ctx: str, run: Callable, vault_json: Callable,
                                  kubectl: Callable, log: Callable, step_error: type) -> None:
    field = spec.get("field", "password")
    cur = vault_json("kv", "get", "-mount=secret", name)["data"]
    version, data = cur["metadata"]["version"], cur["data"]
    old, user = data[field], data[spec.get("user_field", "username")]
    smtp_host, smtp_port = data.get("smtp_server", spec.get("smtp_host")), int(data.get("smtp_port", 587))
    cred = vault_json("kv", "get", "-mount=secret", spec["ssh_credentials"])["data"]["data"]
    ssh_pw = cred["password"]
    nases = [Nas(n["name"], n["host"], n.get("port", 22), cred["username"], step_error) for n in spec["nas"]]
    ns, sel = spec["mailu"]["namespace"], spec["mailu"]["admin_selector"]

    def admin_pod() -> str:
        out = kubectl(ctx, "-n", ns, "get", "pods", "-l", sel, "--field-selector=status.phase=Running",
                      "-o", "jsonpath={.items[0].metadata.name}").strip()
        if not out:
            raise step_error(f"no running Mailu admin pod ({ns}, {sel})")
        return out

    def mailu_set(pw: str) -> None:
        out = kubectl(ctx, "-n", ns, "exec", "-i", admin_pod(), "--", "sh", "-c",
                      'cd /app 2>/dev/null; exec python3 -c "$1" "$2"', "sh", _MAILU_SETPW, user,
                      stdin=pw).strip()
        if out.splitlines()[-1:] != ["setpw-ok"]:
            raise step_error(f"Mailu set_password: {out[-120:]}")

    def dsm(nas: Nas, mode: str, pw: str = "") -> str:
        return nas.sudo(ssh_pw, 'exec python3 -c "$1" "$2"', _DSM_MAIL_AUTH_PY, mode,
                        stdin=f"{user}\n{pw}\n" if mode == "set" else "").strip()

    def dsm_set(nas: Nas, pw: str) -> None:
        out = dsm(nas, "set", pw)
        if not out.startswith("set-ok") or f"user={user} enable=true" not in out:
            raise step_error(f"DSM {nas.name} mail auth set: {out[-120:]}")

    # ---- pre-flight -----------------------------------------------------------
    if not smtp_auth_ok(smtp_host, smtp_port, user, old, step_error):
        raise step_error("pre-flight: SMTP refuses the CURRENT Vault password; refusing")
    for nas in nases:
        out = dsm(nas, "get")
        if f"user={user} enable=true" not in out:
            raise step_error(f"pre-flight: DSM {nas.name} mail auth is not {user} (got {out[-80:]}); refusing")
    admin_pod()
    log(f"  pre-flight ok: SMTP accepts the current password; DSM on {len(nases)} NAS uses {user}")
    if os.environ.get("ROTATION_PREFLIGHT_ONLY") == name:
        raise step_error("pre-flight only (ROTATION_PREFLIGHT_ONLY): nothing changed")

    new = generate_password()
    touched: list[Nas] = []
    mailu_changed = vault_changed = False
    try:
        mailu_changed = True  # before the call: a lost exec may still have applied it
        mailu_set(new)
        for _ in range(6):
            if smtp_auth_ok(smtp_host, smtp_port, user, new, step_error):
                break
            time.sleep(5)
        else:
            raise step_error("SMTP refuses the NEW password after the Mailu change")
        if smtp_auth_ok(smtp_host, smtp_port, user, old, step_error):
            raise step_error("SMTP still accepts the OLD password after the Mailu change")
        vault_changed = True
        run(["vault", "kv", "patch", "-mount=secret", name, "-"], stdin=json.dumps({field: new}))
        if os.environ.get("ROTATION_TEST_FAIL") == name:
            raise step_error("forced failure (ROTATION_TEST_FAIL) to exercise rollback")
        for nas in nases:
            touched.append(nas)
            dsm_set(nas, new)
        log(f"  Mailu + Vault + DSM on {len(nases)} NAS updated; new password AUTHs, old refused")
    except Exception as exc:
        log(f"  FAILED {name}: {exc}; restoring the old password everywhere")
        problems = []
        for nas in touched:
            try:
                dsm_set(nas, old)
            except Exception as e1:  # noqa: BLE001
                problems.append(f"DSM {nas.name}: {e1}")
        if mailu_changed:
            try:
                mailu_set(old)
                if not smtp_auth_ok(smtp_host, smtp_port, user, old, step_error):
                    problems.append("Mailu: old password still refused after restore")
            except Exception as e2:  # noqa: BLE001
                problems.append(f"Mailu: {e2}")
        if vault_changed:
            try:
                if vault_json("kv", "get", "-mount=secret", name)["data"]["metadata"]["version"] != version:
                    run(["vault", "kv", "rollback", "-mount=secret", f"-version={version}", name])
            except Exception as e3:  # noqa: BLE001
                problems.append(f"Vault rollback to v{version}: {e3}")
        if problems:
            raise RuntimeError(f"ROLLBACK FAILED for {name}: {'; '.join(problems)} (original error: {exc})") from exc
        raise step_error(f"{exc} (rolled back; old password AUTHs again, Vault v{version})") from exc

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run(["vault", "kv", "metadata", "patch", "-mount=secret", f"-custom-metadata=rotated_at={stamp}", name])
