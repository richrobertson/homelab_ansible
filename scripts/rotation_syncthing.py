"""syncthing_api_key rotation kind for rotate_due_k8s_secrets.py.

The Syncthing REST/GUI API key lives in Syncthing's own config.xml on the
config PVC; that file is the source of truth. The Vault copy (secret/syncthing/
prod, api_key) is the reference for humans and tools, and nothing in either
cluster reads it through VSO (consumer audit 2026-11-18 below). So rotation is
done AT Syncthing, over its own REST API, and Vault is kept in step:

  0. precheck - the CURRENT Vault key must be accepted by Syncthing
     (/rest/system/ping 200); otherwise refuse, nothing touched.
  1. vault kv patch <field> (Vault first: if the runner dies mid-way, the new
     key is never lost - it is in Vault, the old one in Vault's history).
  2. PATCH /rest/config/gui {"apiKey": new}, authenticated with the old key.
     Syncthing restarts only its API listener; syncing is not interrupted.
  3. verify: new key 200 on ping + /rest/system/connections, OLD key 403,
     config.xml on disk holds the new key (hash compare - survives restarts and
     is what the syncthing-settings Job reads), /metrics still 200 WITHOUT auth
     (the Prometheus PodMonitor scrape path, metricsWithoutAuth=true).
  4. optional: force-sync any VaultStaticSecrets listed under `vss`.
On ANY failure after step 1: set the old key back at Syncthing (authenticating
with whichever key it currently accepts), vault kv rollback, verify the OLD key
works. A failure there is raised as RuntimeError (the caller's "ROLLBACK FAILED,
manual action" path).

Map entry (ansible/k8s/vars/k8s_secret_consumers.yml):
  syncthing/prod:
    kind: syncthing_api_key
    kube_context: admin@prod
    field: api_key
    instance: {namespace, deployment, container}   (where curl runs, against 127.0.0.1:8384)
    vss: []                                        (optional)

Keys never reach a command line, a log or an argv inside the pod: they go over
stdin to vault/kubectl exec and into curl through a `-K -` config.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import string
import subprocess
import time
from collections.abc import Callable

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_TRANSIENT = re.compile(r"container not found|unable to upgrade connection|pod does not exist|"
                        r"no such container|is not running|ContainerCreating|PodInitializing|connection refused")
_BASE = "http://127.0.0.1:8384"

# "<key>" on stdin -> HTTP status of an authenticated GET <path>.
_STATUS = (
    'read -r K; printf \'header = "X-API-Key: %s"\\n\' "$K" | '
    'curl -s -m 20 -o /dev/null -w "%{http_code}" -K - "' + _BASE + '$P"'
)
# "<auth key>\n<new key>" on stdin -> HTTP status of PATCH /rest/config/gui.
# Both keys are validated against _KEY_RE first, so they need no JSON escaping.
_SET = (
    'read -r A; read -r N; '
    'printf \'header = "X-API-Key: %s"\\nheader = "Content-Type: application/json"\\n'
    'request = "PATCH"\\ndata = "{\\\\"apiKey\\\\":\\\\"%s\\\\"}"\\n\' "$A" "$N" | '
    'curl -s -m 60 -o /dev/null -w "%{http_code}" -K - "' + _BASE + '/rest/config/gui"'
)
# -> sha256 of the <apikey> persisted in config.xml (never the key itself).
_DISK = ("f=${STHOMEDIR:-/var/syncthing/config}/config.xml; "
         "sed -n 's|.*<apikey>\\(.*\\)</apikey>.*|\\1|p' \"$f\" | tr -d '\\n' | sha256sum | cut -c1-64")
_METRICS = 'curl -s -m 20 -o /dev/null -w "%{http_code}" "' + _BASE + '/metrics"'
_CONNECTED = (
    'read -r K; printf \'header = "X-API-Key: %s"\\n\' "$K" | curl -s -m 20 -K - "' + _BASE
    + '/rest/system/connections" | tr -d " \\n" | grep -o \'"connected":true\' | wc -l'
)


def _generate() -> str:
    # Same shape Syncthing itself generates: 32 alphanumerics.
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def rotate_syncthing_api_key(name: str, spec: dict, ctx: str, run: Callable, kubectl: Callable,
                             vault_json: Callable, sync_and_confirm: Callable, log: Callable,
                             step_error: type) -> None:
    inst, field = spec["instance"], spec["field"]

    def exec_(script: str, stdin: str = "", path: str = "") -> str:
        # Idempotent scripts only, so a pod that is (re)starting is waited out.
        for attempt in range(12):
            try:
                return kubectl(ctx, "-n", inst["namespace"], "exec", "-i", f"deploy/{inst['deployment']}",
                               "-c", inst.get("container", "syncthing"), "--", "env", f"P={path}",
                               "sh", "-c", script, stdin=stdin, timeout=120).strip()
            except step_error as exc:
                if attempt == 11 or not _TRANSIENT.search(str(exc)):
                    raise
                log(f"  syncthing pod not reachable ({str(exc)[-80:]}); retrying in 10s")
                time.sleep(10)
        raise step_error("unreachable")

    def status(key: str, path: str = "/rest/system/ping") -> str:
        return exec_(_STATUS, f"{key}\n", path)

    def set_key(auth: str, new: str) -> None:
        code = exec_(_SET, f"{auth}\n{new}\n")
        if code != "200":
            raise step_error(f"PATCH /rest/config/gui returned HTTP {code}")

    def verify(good: str, bad: str | None) -> None:
        code = ""
        for _ in range(12):  # the API listener restarts after a GUI config change
            code = status(good)
            if code == "200":
                break
            time.sleep(5)
        else:
            raise step_error(f"expected key refused (ping HTTP {code})")
        if (c := status(good, "/rest/system/connections")) != "200":
            raise step_error(f"/rest/system/connections HTTP {c} with the expected key")
        if bad is not None and (c := status(bad)) != "403":
            raise step_error(f"previous key NOT refused (ping HTTP {c})")
        if exec_(_DISK) != _sha(good):
            raise step_error("config.xml on disk does not hold the expected key")
        if (c := exec_(_METRICS)) != "200":
            raise step_error(f"/metrics without auth returned HTTP {c} (Prometheus scrape path)")

    cur = vault_json("kv", "get", "-mount=secret", name)["data"]
    old_version, old = cur["metadata"]["version"], cur["data"][field]
    if not _KEY_RE.match(old):
        raise step_error("current key in Vault has unexpected characters; refusing")

    # 0. precheck - Vault must match reality before we touch anything.
    if (code := status(old)) != "200":
        raise step_error(f"current Vault key not accepted by Syncthing (HTTP {code}); refusing, nothing changed")
    before = exec_(_CONNECTED, f"{old}\n")
    log(f"  precheck ok; {before} device(s) connected")

    new = _generate()
    run(["vault", "kv", "patch", "-mount=secret", name, "-"], stdin=json.dumps({field: new}))
    try:
        set_key(old, new)
        verify(new, old)
        if spec.get("vss"):
            sync_and_confirm(ctx, spec, field, new)
        after = exec_(_CONNECTED, new + "\n")
        log(f"  new key verified; {after} device(s) connected")
    except (step_error, subprocess.TimeoutExpired, KeyError, ValueError) as exc:
        log(f"  FAILED: {exc}; restoring the old key at Syncthing and Vault v{old_version}")
        try:
            # Authenticate with whichever key Syncthing accepts right now.
            if status(old) != "200":
                if status(new) != "200":
                    raise step_error("Syncthing accepts neither the old nor the new key")
                set_key(new, old)
            run(["vault", "kv", "rollback", "-mount=secret", f"-version={old_version}", name])
            verify(old, None)
            if spec.get("vss"):
                sync_and_confirm(ctx, spec, field, old)
        except (step_error, subprocess.TimeoutExpired, KeyError, ValueError) as exc2:
            raise RuntimeError(f"ROLLBACK FAILED for {name}: {exc2} (original error: {exc})") from exc2
        raise step_error(f"{exc} (rolled back; old key verified working)") from exc

    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run(["vault", "kv", "metadata", "patch", "-mount=secret", f"-custom-metadata=rotated_at={stamp}", name])
