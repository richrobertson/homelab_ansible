#!/usr/bin/env python3
"""Unattended rotation of k8s_secret-class Vault secrets that are DUE.

Companion of rotate_due_service_accounts.py, run by the same CronJob in each
cluster. The secrets it may rotate, and everything about how, live in
ansible/k8s/vars/k8s_secret_consumers.yml - read its header first.

  due  - custom_metadata.rotated_at older than --due-fraction (default 0.8) of
         custom_metadata.rotation_interval_days (unknown age = due).
  env  - only entries whose kube_context matches ROTATION_ENV are considered;
         each cluster's runner can reach only its own API server.

Per secret, strictly one at a time:
  generate -> vault kv patch <field> -> force-sync every VaultStaticSecret ->
  confirm every k8s Secret holds the new value -> restart -> verify (new value
  accepted AND old value refused).
On ANY failure after the Vault write: vault kv rollback to the previous
version, resync, restart, and verify the OLD value works again. A failure in
that rollback is reported as such, loudly.

Exit codes: 0 nothing failed (incl. nothing due), 1 a rotation failed (rolled
back), 3 a rollback failed (manual action needed), 2 could not plan.
Secret values never appear on a command line or in output: they travel over
stdin (vault, kubectl exec) and are compared, never printed.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import secrets
import subprocess
import sys
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rotate_due_service_accounts import log, notify, rotated_at

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = os.path.join(REPO, "ansible/k8s/vars/k8s_secret_consumers.yml")
CONTEXT = {"prod": "admin@prod", "staging": "admin@staging"}
GENERATORS = {"urlsafe32": lambda: secrets.token_urlsafe(32)}


class StepError(RuntimeError):
    pass


def run(cmd: list[str], stdin: str | None = None, timeout: int = 120) -> str:
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    if r.returncode != 0:
        # stderr never contains the secret: values only ever go in over stdin.
        raise StepError(f"{' '.join(cmd[:4])}...: {r.stderr.strip()[:300]}")
    return r.stdout


def vault_json(*args: str) -> dict:
    # The vault CLI rejects flags after the positional path, so -format goes
    # before the last argument (the path).
    return json.loads(run(["vault", *args[:-1], "-format=json", args[-1]]))


def kubectl(ctx: str, *args: str, stdin: str | None = None, timeout: int = 120) -> str:
    return run(["kubectl", "--context", ctx, "--request-timeout=30s", *args], stdin=stdin, timeout=timeout)


def interval_days(path: str) -> int | None:
    meta = vault_json("kv", "metadata", "get", "-mount=secret", path)["data"].get("custom_metadata") or {}
    v = meta.get("rotation_interval_days")
    return int(v) if v else None


def sync_and_confirm(ctx: str, spec: dict, field: str, want: str) -> None:
    stamp = str(int(time.time()))
    for v in spec["vss"]:
        kubectl(ctx, "-n", v["namespace"], "annotate", "vaultstaticsecret", v["name"],
                f"ad-rotation/force-sync={stamp}", "--overwrite")
    for v in spec["vss"]:
        for _ in range(24):
            raw = kubectl(ctx, "-n", v["namespace"], "get", "secret", v["name"], "-o", "json")
            data = json.loads(raw).get("data") or {}
            if base64.b64decode(data.get(field, "")).decode() == want:
                break
            time.sleep(5)
        else:
            raise StepError(f"Secret {v['namespace']}/{v['name']} did not sync within 120s")


def restart(ctx: str, spec: dict) -> None:
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    patch = json.dumps({"spec": {"template": {"metadata": {"annotations": {
        "kubectl.kubernetes.io/restartedAt": now}}}}})
    for r in spec["restart"]:
        kubectl(ctx, "-n", r["namespace"], "patch", "deployment", r["deployment"], "-p", patch)
    for r in spec["restart"]:
        # Poll with plain `get` (rollout status needs list/watch, which RBAC cannot
        # narrow to one name).
        for _ in range(60):
            d = json.loads(kubectl(ctx, "-n", r["namespace"], "get", "deployment", r["deployment"], "-o", "json"))
            st, want = d.get("status") or {}, d["spec"].get("replicas", 1)
            if (st.get("observedGeneration", 0) >= d["metadata"]["generation"]
                    and st.get("updatedReplicas", 0) == want
                    and st.get("availableReplicas", 0) == want
                    and st.get("replicas", 0) == want):
                break
            time.sleep(10)
        else:
            raise StepError(f"deployment {r['namespace']}/{r['deployment']} did not roll out within 10m")


def verify(ctx: str, spec: dict, user: str, good: str, bad: str | None) -> None:
    v = spec["verify"]
    if v["kind"] != "collabora_admin":
        raise StepError(f"unknown verify kind {v['kind']}")
    script = (
        'read -r U; read -r G; read -r B; sleep 5; '
        'base=http://localhost:9980; '
        '[ "$(curl -sk -o /dev/null -w %{http_code} https://localhost:9980/hosting/discovery)" = 200 ] '
        '&& base=https://localhost:9980; '
        'd=$(curl -sk -o /dev/null -w %{http_code} $base/hosting/discovery); '
        'g=$(curl -sk -o /dev/null -w %{http_code} -u "$U:$G" $base/browser/dist/admin/admin.html); '
        'b=-; [ -n "$B" ] && b=$(curl -sk -o /dev/null -w %{http_code} -u "$U:$B" $base/browser/dist/admin/admin.html); '
        'echo "$d $g $b"'
    )
    out = ""
    for _ in range(6):  # the new pod may still be warming up
        out = kubectl(ctx, "-n", v["namespace"], "exec", "-i", f"deploy/{v['deployment']}", "--",
                      "sh", "-c", script, stdin=f"{user}\n{good}\n{bad or ''}\n").strip()
        d, g, b = (out.split() + ["", "", ""])[:3]
        if d == "200" and g == "200" and (bad is None or b == "401"):
            return
        time.sleep(10)
    raise StepError(f"verify failed (discovery/expected-login/refused-login = {out})")


def rotate(name: str, spec: dict, ctx: str) -> None:
    field = spec["field"]
    cur = vault_json("kv", "get", "-mount=secret", name)["data"]
    old_version, old, user = cur["metadata"]["version"], cur["data"][field], cur["data"][spec["verify"]["user_field"]]
    new = GENERATORS[spec["generator"]]()
    run(["vault", "kv", "patch", "-mount=secret", name, "-"], stdin=json.dumps({field: new}))
    try:
        sync_and_confirm(ctx, spec, field, new)
        restart(ctx, spec)
        verify(ctx, spec, user, new, old)
    except (StepError, subprocess.TimeoutExpired, KeyError, ValueError) as exc:
        log(f"FAILED {name}: {exc}; rolling back to v{old_version}")
        try:
            run(["vault", "kv", "rollback", "-mount=secret", f"-version={old_version}", name])
            sync_and_confirm(ctx, spec, field, old)
            restart(ctx, spec)
            verify(ctx, spec, user, old, None)
        except (StepError, subprocess.TimeoutExpired, KeyError, ValueError) as exc2:
            raise RuntimeError(f"ROLLBACK FAILED for {name}: {exc2} (original error: {exc})") from exc2
        raise StepError(f"{exc} (rolled back; old value verified working)") from exc
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run(["vault", "kv", "metadata", "patch", "-mount=secret", f"-custom-metadata=rotated_at={stamp}", name])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", choices=sorted(CONTEXT), default=os.environ.get("ROTATION_ENV", "prod"))
    ap.add_argument("--due-fraction", type=float, default=float(os.environ.get("K8S_ROTATION_DUE_FRACTION", "0.8")))
    ap.add_argument("--dry-run", action="store_true",
                    default=os.environ.get("ROTATION_DRY_RUN", "").lower() in ("1", "true", "yes"))
    ap.add_argument("--only", help="restrict to one Vault path (testing)")
    args = ap.parse_args()
    ctx = CONTEXT[args.env]

    try:
        with open(MAP, encoding="utf-8") as fh:
            entries = yaml.safe_load(fh)["k8s_secret_rotations"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        log(f"cannot load {MAP}: {exc}")
        notify("k8s secret rotation: planning failed", str(exc)[:500], 8)
        return 2

    now, plan = dt.datetime.now(dt.UTC), []
    log(f"k8s secrets, environment: {args.env}")
    for name, spec in entries.items():
        if spec.get("kube_context") != ctx or (args.only and name != args.only):
            continue
        if spec.get("generator") not in GENERATORS:
            log(f"skip   {name:<34} unknown generator {spec.get('generator')}")
            continue
        try:
            ts, iv = rotated_at(name), interval_days(name)
        except (RuntimeError, StepError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            log(str(exc))
            notify("k8s secret rotation: planning failed", str(exc)[:500], 8)
            return 2
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        age = (now - ts).days if ts else None
        if age is not None and iv and age < args.due_fraction * iv:
            log(f"skip   {name:<34} not due ({age}d < {args.due_fraction:.0%} of {iv}d)")
            continue
        log(f"DUE    {name:<34} age={'unknown' if age is None else f'{age}d'} interval={iv or '?'}d")
        plan.append((name, spec))

    if not plan:
        log("nothing due")
        return 0
    if args.dry_run:
        log("dry run: not rotating")
        return 0

    ok, failed, broken = [], [], []
    for name, spec in plan:
        log(f"rotating {name}")
        try:
            rotate(name, spec, ctx)
            ok.append(name)
            log(f"rotated {name}")
        except StepError as exc:
            failed.append(name)
            log(f"FAILED {name}: {exc}")
        except RuntimeError as exc:
            broken.append(name)
            log(str(exc))

    summary = (f"rotated: {', '.join(ok) or 'none'}; failed+rolled back: {', '.join(failed) or 'none'}; "
               f"ROLLBACK FAILED: {', '.join(broken) or 'none'}")
    log(summary)
    if broken:
        notify("k8s secret rotation: ROLLBACK FAILED", summary + "\nManual action needed - see the CronJob log.", 10)
        return 3
    if failed:
        notify("k8s secret rotation FAILED (rolled back)", summary, 8)
        return 1
    notify("k8s secret rotation done", summary, 3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
