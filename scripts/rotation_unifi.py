"""kind unifi_console_ssh - the UniFi OS console's root SSH password.

Endpoint found in the UniFi OS web UI bundle (SshPasswordModal):
    POST /api/system/ssh/setpassword  {"password": "..."}
authenticated as a local UniFi OS admin (/api/auth/login + X-Csrf-Token).

Order: prove Vault's value logs in over SSH -> set via API -> prove new logs in
and old is refused -> Vault -> VSS sync + consumer restart. Any failure sets
the old password back through the same API and proves it logs in.
Login attempts are kept minimal: UniFi OS locks SSH for 3 minutes after 5
failed passwords, and the only intended failure here is the single old-password
check.
"""
from __future__ import annotations

import http.cookiejar
import json
import secrets
import ssl
import string
import time
import urllib.error
import urllib.request
from collections.abc import Callable


class _Console:
    def __init__(self, host: str, step_error: type):
        self.host, self.base, self.err, self.csrf = host, f"https://{host}", step_error, None
        ctx = ssl.create_default_context()
        # UniFi OS serves a self-signed certificate on the LAN address; the
        # session carries only this console's own admin login.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                              urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body: dict | None = None) -> int:
        h = {"Content-Type": "application/json"}
        if self.csrf:
            h["X-Csrf-Token"] = self.csrf
        r = urllib.request.Request(self.base + path, method=method, headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.op.open(r, timeout=30) as resp:  # nosec B310 - fixed https URL
                self.csrf = resp.headers.get("X-Updated-Csrf-Token") or resp.headers.get("x-csrf-token") or self.csrf
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code
        except OSError as e:
            raise self.err(f"UniFi OS API {path}: {type(e).__name__}: {str(e)[:120]}") from None

    def login(self, user: str, pw: str) -> None:
        st = self.call("POST", "/api/auth/login", {"username": user, "password": pw, "rememberMe": False})
        if st != 200:
            raise self.err(f"UniFi OS admin login HTTP {st}")

    def set_ssh_password(self, pw: str) -> None:
        st = self.call("POST", "/api/system/ssh/setpassword", {"password": pw})
        if st not in (200, 204):
            raise self.err(f"setpassword HTTP {st}")

    def ssh_ok(self, user: str, pw: str) -> bool:
        import paramiko
        c = paramiko.SSHClient()
        # The console regenerates host keys on firmware reset; the password, not
        # the host key, is what this check proves.
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # nosec B507
        try:
            c.connect(self.host, username=user, password=pw, timeout=15, look_for_keys=False,
                      allow_agent=False, banner_timeout=15, auth_timeout=15)
            _, o, _ = c.exec_command("echo ok")  # nosec B601 - fixed command
            ok = o.read().decode().strip() == "ok"
            c.close()
            return ok
        except paramiko.AuthenticationException:
            return False
        except (paramiko.SSHException, OSError) as exc:
            raise self.err(f"SSH {self.host}: {type(exc).__name__}: {str(exc)[:120]}") from None


def _gen() -> str:
    alpha = string.ascii_letters + string.digits
    return "".join(secrets.choice(alpha) for _ in range(28))


def rotate_unifi_console_ssh(name: str, spec: dict, ctx: str, run: Callable, vault_json: Callable,
                             sync_and_confirm: Callable, restart: Callable, log: Callable,
                             step_error: type) -> None:
    import os
    field, user_field = spec.get("field", "password"), spec.get("user_field", "username")
    cur = vault_json("kv", "get", "-mount=secret", name)["data"]
    version, user, old = cur["metadata"]["version"], cur["data"][user_field], cur["data"][field]
    admin = vault_json("kv", "get", "-mount=secret", spec["admin_credentials"])["data"]["data"]
    con = _Console(spec["host"], step_error)
    con.login(admin["username"], admin["password"])
    if not con.ssh_ok(user, old):
        raise step_error("pre-flight: the console refuses the CURRENT Vault password over SSH; refusing")
    log(f"  pre-flight ok: UniFi OS admin login; SSH {user}@{spec['host']} accepts the current password")
    if os.environ.get("ROTATION_PREFLIGHT_ONLY") == name:
        raise step_error("pre-flight only (ROTATION_PREFLIGHT_ONLY): nothing changed")

    new = _gen()
    changed = vault_changed = False
    try:
        changed = True
        con.set_ssh_password(new)
        for _ in range(6):
            time.sleep(5)
            if con.ssh_ok(user, new):
                break
        else:
            raise step_error("NEW password refused over SSH after setpassword")
        if con.ssh_ok(user, old):
            raise step_error("OLD password still accepted after setpassword")
        if os.environ.get("ROTATION_TEST_FAIL") == name:
            raise step_error("forced failure (ROTATION_TEST_FAIL) to exercise rollback")
        vault_changed = True
        run(["vault", "kv", "patch", "-mount=secret", name, "-"], stdin=json.dumps({field: new}))
        if spec.get("vss"):
            sync_and_confirm(ctx, spec, field, new)
        if spec.get("restart"):
            restart(ctx, spec)
        log("  console SSH password changed; new accepted, old refused; Vault + consumers updated")
    except Exception as exc:
        log(f"  FAILED {name}: {exc}; restoring the old password")
        problems = []
        if changed:
            try:
                con.set_ssh_password(old)
                time.sleep(5)
                if not con.ssh_ok(user, old):
                    problems.append("console still refuses the OLD password after restore")
            except Exception as e1:  # noqa: BLE001
                problems.append(f"console restore: {e1}")
        if vault_changed:
            try:
                if vault_json("kv", "get", "-mount=secret", name)["data"]["metadata"]["version"] != version:
                    run(["vault", "kv", "rollback", "-mount=secret", f"-version={version}", name])
                if spec.get("vss"):
                    sync_and_confirm(ctx, spec, field, old)
                if spec.get("restart"):
                    restart(ctx, spec)
            except Exception as e2:  # noqa: BLE001
                problems.append(f"Vault/consumers: {e2}")
        if problems:
            raise RuntimeError(f"ROLLBACK FAILED for {name}: {'; '.join(problems)} (original error: {exc})") from exc
        raise step_error(f"{exc} (rolled back; old password logs in again)") from exc

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run(["vault", "kv", "metadata", "patch", "-mount=secret", f"-custom-metadata=rotated_at={stamp}", name])
