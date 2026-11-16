#!/usr/bin/env python3
"""Unattended AD service-account rotation: rotate every account that is DUE and SAFE.

This is the scheduler half of ansible/domain/rotate_service_account_passwords.yml.
It does not change a password itself. For each account it decides two things and,
only if both are true, runs the playbook for that ONE account:

  due   - Vault custom_metadata.rotated_at on the account's secret is older than
          --due-days (default 25, well inside the domain's 42-day maxPwdAge), or
          missing entirely (an unknown age is treated as due).
  safe  - every consumer in ansible/domain/vars/service_account_consumers.yml has
          an update kind the playbook can complete unattended. `occ` (which
          restarts Nextcloud) only counts as safe inside the maintenance window.

Accounts are rotated strictly one at a time; the playbook's own verify-and-roll-
back runs per account, and a failure stops nothing else but is reported. The
playbook already refuses the account it connects as (svc-ansible-win), so this
wrapper skips it rather than let it fail.

Exit codes: 0 = nothing failed (including "nothing due"), 1 = at least one
rotation failed, 2 = could not even plan (Vault/config unreadable).

Everything secret stays inside ansible-playbook (no_log) and Vault; this script
only ever reads metadata and prints account names.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYBOOK = os.path.join(REPO, "ansible/domain/rotate_service_account_passwords.yml")
CONSUMERS = os.path.join(REPO, "ansible/domain/vars/service_account_consumers.yml")
IDENTITIES = os.path.join(REPO, "ansible/domain/vars/service_account_identities.yml")

# Must mirror sac_automatable_updates in the playbook. `occ` is added only inside
# the maintenance window, exactly as the playbook adds it only when
# sac_accept_app_restart=true.
AUTOMATABLE = {"vault_only", "vault_ldap_auth", "keycloak_component"}
APP_RESTART_KINDS = {"occ"}
# The account the play connects to the DC as; the playbook refuses it.
CONNECTION_ACCOUNT = "svc-ansible-win"


def log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.UTC):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def vault_paths() -> dict[str, str]:
    """account name -> KV path, assembled exactly as the playbook's sac_all_accounts:
    sai_bind_accounts + sai_automation_accounts + [sai_winrm_account] + sai_existing_accounts."""
    with open(IDENTITIES, encoding="utf-8") as fh:
        ids = yaml.safe_load(fh)
    accts = list(ids.get("sai_bind_accounts") or []) + list(ids.get("sai_automation_accounts") or [])
    if isinstance(ids.get("sai_winrm_account"), dict):
        accts.append(ids["sai_winrm_account"])
    accts += list(ids.get("sai_existing_accounts") or [])
    return {a["name"]: a["vault_path"] for a in accts if a.get("name") and a.get("vault_path")}


def rotated_at(path: str) -> dt.datetime | None:
    r = subprocess.run(
        ["vault", "kv", "metadata", "get", "-format=json", "-mount=secret", path],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"vault metadata read failed for {path}: {r.stderr.strip()[:200]}")
    meta = (json.loads(r.stdout).get("data") or {}).get("custom_metadata") or {}
    val = meta.get("rotated_at")
    if not val:
        return None
    return dt.datetime.fromisoformat(val)


def in_window(now_local: dt.datetime, start: int, end: int) -> bool:
    return start <= now_local.hour < end


def notify(title: str, message: str, priority: int) -> None:
    url, token = os.environ.get("GOTIFY_URL"), os.environ.get("GOTIFY_TOKEN")
    if not url or not token:
        return
    # GOTIFY_URL comes from the environment; refuse anything but http(s) so a
    # file:// or custom-scheme value can never turn this into a local file read.
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        log("gotify notify skipped: GOTIFY_URL scheme must be http or https")
        return
    body = json.dumps({"title": title, "message": message, "priority": priority}).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/message", data=body,
        headers={"Content-Type": "application/json", "X-Gotify-Key": token},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()  # nosec B310 - scheme checked above
    except (urllib.error.URLError, OSError, ValueError) as exc:  # never mask the real result
        log(f"gotify notify failed: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", default=os.environ.get("ROTATION_INVENTORY",
                    os.path.join(REPO, "inventory/runner/rotation.ini")))
    ap.add_argument("--due-days", type=int, default=int(os.environ.get("ROTATION_DUE_DAYS", "25")))
    ap.add_argument("--window-tz", default=os.environ.get("ROTATION_WINDOW_TZ", "America/Los_Angeles"))
    ap.add_argument("--window-start", type=int, default=int(os.environ.get("ROTATION_WINDOW_START", "3")))
    ap.add_argument("--window-end", type=int, default=int(os.environ.get("ROTATION_WINDOW_END", "4")))
    ap.add_argument("--dry-run", action="store_true",
                    default=os.environ.get("ROTATION_DRY_RUN", "") in ("1", "true"),
                    help="plan only; never invoke the playbook with confirmation")
    ap.add_argument("--only", help="restrict to one account (testing)")
    args = ap.parse_args()

    if args.due_days >= 42:
        log("refusing: --due-days must stay under the 42-day domain maxPwdAge")
        return 2

    now = dt.datetime.now(dt.UTC)
    window = in_window(now.astimezone(ZoneInfo(args.window_tz)), args.window_start, args.window_end)
    allowed = AUTOMATABLE | (APP_RESTART_KINDS if window else set())

    try:
        with open(CONSUMERS, encoding="utf-8") as fh:
            consumers = yaml.safe_load(fh)["sac_consumers"]
        paths = vault_paths()
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        log(f"cannot load account config: {exc}")
        notify("AD rotation: planning failed", str(exc)[:500], 8)
        return 2

    plan, skipped = [], []
    for name, spec in consumers.items():
        if args.only and name != args.only:
            continue
        kinds = {c.get("update") for c in spec.get("consumers") or []}
        if name == CONNECTION_ACCOUNT:
            skipped.append((name, "is the WinRM connection account (needs a second identity)"))
            continue
        blocked = sorted(k for k in kinds if k not in allowed)
        if blocked:
            why = ("app restart outside the maintenance window"
                   if set(blocked) <= APP_RESTART_KINDS else f"consumer not automatable: {', '.join(blocked)}")
            skipped.append((name, why))
            continue
        if name not in paths:
            skipped.append((name, "no Vault path in the identity list"))
            continue
        try:
            ts = rotated_at(paths[name])
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            log(str(exc))
            notify("AD rotation: planning failed", str(exc)[:500], 8)
            return 2
        age = (now - ts).days if ts else None
        if age is not None and age < args.due_days:
            skipped.append((name, f"not due ({age}d < {args.due_days}d)"))
            continue
        plan.append((name, age, bool(kinds & APP_RESTART_KINDS)))

    for name, why in skipped:
        log(f"skip   {name:<20} {why}")
    for name, age, restarts in plan:
        log(f"DUE    {name:<20} age={'unknown' if age is None else f'{age}d'}{' (restarts app)' if restarts else ''}")
    if not plan:
        log("nothing due")
        return 0
    if args.dry_run:
        log("dry run: not rotating")
        return 0

    ok, failed = [], []
    for name, _age, restarts in plan:
        cmd = ["ansible-playbook", "-i", args.inventory, PLAYBOOK,
               "-e", f"sac_rotate_only={name}", "-e", "sac_rotate_confirm=true"]
        if restarts:
            cmd += ["-e", "sac_accept_app_restart=true"]
        log(f"rotating {name}")
        r = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True, check=False)
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:])
        if r.returncode == 0:
            ok.append(name)
            log(f"rotated {name}")
        else:
            failed.append(name)
            log(f"FAILED {name} (rc={r.returncode})\n{tail}")

    summary = f"rotated: {', '.join(ok) or 'none'}; failed: {', '.join(failed) or 'none'}"
    log(summary)
    if failed:
        notify("AD rotation FAILED", summary + "\nThe playbook rolls AD back on a failed step; check the CronJob log.", 8)
        return 1
    notify("AD rotation done", summary, 3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
