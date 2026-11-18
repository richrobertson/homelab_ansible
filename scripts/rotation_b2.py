"""b2_application_key rotation kind for rotate_due_k8s_secrets.py (VolSync/CNPG).

Rotates the Backblaze B2 S3 credential (keyID/applicationKey/keyName) in a Vault
path that VaultStaticSecrets fan out to restic and CNPG Secrets.

NEVER touches RESTIC_PASSWORD. That field unlocks each restic repository's
master key; changing it without `restic key add/remove` makes every snapshot
unrecoverable (secret_rotation_classes.yml, restic_key). The B2 key is only the
S3 login and can be swapped freely. Every write is followed by a hash check that
RESTIC_PASSWORD is byte-identical.

Two phases, state in Vault METADATA (never synced into consumer Secrets):

  phase 1 (due by age) - mint with the stored scoped rotator key
     (ops/backblaze/key-rotator; only the PROD runner may read it, so staging can
     never mint keys for prod's data), same bucket + namePrefix, capabilities =
     current key's caps that the rotator holds; prove (authorize + list under the
     prefix); patch the three key fields; read back; stamp rotated_at and
     b2_pending_delete=<old keyID>, b2_swapped_at=<iso>. VSO delivers the new
     key to every consumer within refreshAfter (60s); the old key keeps working.
  phase 2 (later runs) - delete the old key only when grace_hours have passed AND
     the consumer cluster's health report (written to health_path by THAT
     cluster's runner, from read-only status) shows every restic backup fed by
     this path succeeded after the swap, none failed, and every CNPG cluster is
     still archiving. Otherwise hold; a failure since the swap fails loudly.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable

AUTH_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
REQUIRED_CAPS = {"listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"}
ROTATOR = ("ops", "backblaze/key-rotator")
SETTLE = dt.timedelta(minutes=15)   # VSO refresh + the first backups to pick it up


class B2:
    def __init__(self, key_id: str, app_key: str):
        basic = base64.b64encode(f"{key_id}:{app_key}".encode()).decode()
        req = urllib.request.Request(AUTH_URL, headers={"Authorization": "Basic " + basic})
        with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 - fixed https URL
            a = json.load(r)
        st = a["apiInfo"]["storageApi"]
        self.account_id, self.api, self.token = a["accountId"], st["apiUrl"], a["authorizationToken"]
        self.caps = set(st.get("capabilities") or [])
        self.bucket_id, self.prefix = st.get("bucketId"), st.get("namePrefix")

    def call(self, op: str, body: dict) -> dict:
        req = urllib.request.Request(f"{self.api}/b2api/v3/{op}", data=json.dumps(body).encode(),
                                     headers={"Authorization": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310 - B2 apiUrl
                return json.load(r)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"B2 {op}: http {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}") from exc

    def list_keys(self) -> list[dict]:
        return self.call("b2_list_keys", {"accountId": self.account_id, "maxKeyCount": 1000})["keys"]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pw_hash(d: dict) -> str:
    return hashlib.sha256(str(d.get("RESTIC_PASSWORD", "")).encode()).hexdigest()


def _meta(vault_json: Callable, path: str) -> dict:
    return vault_json("kv", "metadata", "get", "-mount=secret", path)["data"].get("custom_metadata") or {}


def pending(vault_json: Callable, path: str) -> bool:
    return bool(_meta(vault_json, path).get("b2_pending_delete"))


def rotate_b2_key(name: str, spec: dict, run: Callable, vault_json: Callable, log: Callable,
                  step_error: type) -> None:
    meta = _meta(vault_json, name)
    if meta.get("b2_pending_delete"):
        _finalize(name, spec, meta, run, vault_json, log, step_error)
        return

    cur = vault_json("kv", "get", "-mount=secret", name)["data"]
    data, version = cur["data"], cur["metadata"]["version"]
    old_id, pw0 = data["keyID"], _pw_hash(data)
    r = vault_json("kv", "get", f"-mount={ROTATOR[0]}", ROTATOR[1])["data"]["data"]
    rot = B2(r["keyID"], r["applicationKey"])
    live = {k["applicationKeyId"]: k for k in rot.list_keys()}
    if old_id not in live:
        raise step_error(f"Vault's key ...{old_id[-4:]} is not a live B2 key; refusing")
    old = live[old_id]
    caps = sorted(set(old["capabilities"]) & rot.caps)
    if not REQUIRED_CAPS <= set(caps):
        raise step_error(f"rotator cannot mint the caps restic/CNPG need: missing {sorted(REQUIRED_CAPS - set(caps))}")
    bucket = old.get("bucketId") or (old.get("bucketIds") or [None])[0]
    prefix = old.get("namePrefix")
    if not bucket:
        raise step_error("current key is not bucket-scoped; refusing to mint an account-wide key")
    base = re.sub(r"-\d{8}$", "", old["keyName"])
    new_name = f"{base}-{_now():%Y%m%d}"
    body = {"accountId": rot.account_id, "capabilities": caps, "keyName": new_name, "bucketId": bucket}
    if prefix:
        body["namePrefix"] = prefix
    new = rot.call("b2_create_key", body)
    log(f"  minted {new_name} ...{new['applicationKeyId'][-4:]} ({len(caps)} caps, prefix={prefix}); old ...{old_id[-4:]} still live")
    patched = False
    try:
        for attempt in range(24):
            try:
                nk = B2(new["applicationKeyId"], new["applicationKey"])
                if nk.bucket_id != bucket or nk.prefix != prefix:
                    raise step_error("new key's scope differs from the old key's")
                nk.call("b2_list_file_names", {"bucketId": bucket, "prefix": prefix or "", "maxFileCount": 1})
                break
            except (urllib.error.URLError, RuntimeError):
                if attempt == 23:
                    raise
                time.sleep(5)
        log("  new key authorizes and lists under its prefix")
        run(["vault", "kv", "patch", "-mount=secret", name, "-"], stdin=json.dumps(
            {"keyID": new["applicationKeyId"], "applicationKey": new["applicationKey"], "keyName": new_name}))
        patched = True
        back = vault_json("kv", "get", "-mount=secret", name)["data"]["data"]
        if back.get("keyID") != new["applicationKeyId"]:
            raise step_error("Vault did not take the new key")
        if _pw_hash(back) != pw0:
            raise step_error("RESTIC_PASSWORD changed during the patch - this must never happen")
    except Exception as exc:
        log(f"  FAILED ({exc}); rolling back")
        if patched:
            run(["vault", "kv", "rollback", "-mount=secret", f"-version={version}", name])
            if _pw_hash(vault_json("kv", "get", "-mount=secret", name)["data"]["data"]) != pw0:
                raise RuntimeError(f"ROLLBACK FAILED for {name}: RESTIC_PASSWORD does not match the original") from exc
        rot.call("b2_delete_key", {"applicationKeyId": new["applicationKeyId"]})
        raise step_error(f"{exc} (rolled back: Vault restored, new key deleted, old key untouched)") from exc
    run(["vault", "kv", "metadata", "patch", "-mount=secret", f"-custom-metadata=rotated_at={_iso(_now())}",
         f"-custom-metadata=b2_pending_delete={old_id}", f"-custom-metadata=b2_swapped_at={_iso(_now())}", name])
    log(f"  recorded; old key ...{old_id[-4:]} held until {spec.get('grace_hours', 24)}h + healthy backups")


def _finalize(name: str, spec: dict, meta: dict, run: Callable, vault_json: Callable, log: Callable,
              step_error: type) -> None:
    old_id = meta["b2_pending_delete"]
    swapped = dt.datetime.fromisoformat(meta["b2_swapped_at"])
    grace = dt.timedelta(hours=int(spec.get("grace_hours", 24)))
    if _now() < swapped + grace:
        log(f"  holding old key ...{old_id[-4:]}: grace ends {_iso(swapped + grace)}")
        return
    try:
        rep = vault_json("kv", "get", "-mount=secret", spec["health_path"])["data"]["data"]
    except Exception as exc:
        raise step_error(f"no health report at {spec['health_path']} ({exc}); old key held") from exc
    checked = dt.datetime.fromisoformat(rep["checked_at"])
    if checked < swapped + grace:
        log(f"  holding old key ...{old_id[-4:]}: health report ({rep['checked_at']}) predates the grace end")
        return
    report = json.loads(rep["report"])
    after = swapped + SETTLE
    stale = [r["name"] for r in report["restic"] if not r.get("last_success")
             or dt.datetime.fromisoformat(r["last_success"]) < after]
    failed = [r["name"] for r in report["restic"] if r.get("failed_since")
              and dt.datetime.fromisoformat(r["failed_since"]) >= swapped]
    cnpg_bad = [c["name"] for c in report["cnpg"] if not c.get("archiving")]
    if failed or cnpg_bad:
        raise step_error(f"consumers unhealthy since the swap (restic failed: {failed}; CNPG not archiving: "
                         f"{cnpg_bad}); old key ...{old_id[-4:]} KEPT. Roll back with "
                         f"vault kv rollback on {name}.")
    if stale:
        log(f"  holding old key ...{old_id[-4:]}: {len(stale)} restic backup(s) not yet run since the swap "
            f"({', '.join(stale[:4])})")
        return
    r = vault_json("kv", "get", f"-mount={ROTATOR[0]}", ROTATOR[1])["data"]["data"]
    rot = B2(r["keyID"], r["applicationKey"])
    if any(k["applicationKeyId"] == old_id for k in rot.list_keys()):
        rot.call("b2_delete_key", {"applicationKeyId": old_id})
    if any(k["applicationKeyId"] == old_id for k in rot.list_keys()):
        raise step_error(f"old key ...{old_id[-4:]} still present after delete")
    run(["vault", "kv", "metadata", "patch", "-mount=secret", "-custom-metadata=b2_pending_delete=",
         "-custom-metadata=b2_swapped_at=", name])
    log(f"  deleted old key ...{old_id[-4:]}: {len(report['restic'])} restic backup(s) ok on the new key, "
        f"{len(report['cnpg'])} CNPG cluster(s) archiving")


def health_report(path: str, kubectl: Callable) -> dict:
    """Read-only status of every consumer fed from Vault `path` in THIS cluster:
    restic ReplicationSources and CNPG clusters whose Secret comes from a
    VaultStaticSecret on that path. Reads statuses only, never Secrets."""
    vss = json.loads(kubectl("get", "vaultstaticsecrets.secrets.hashicorp.com", "-A", "-o", "json"))["items"]
    fed = {(v["metadata"]["namespace"], v["spec"]["destination"]["name"]) for v in vss if v["spec"].get("path") == path}
    restic = []
    for r in json.loads(kubectl("get", "replicationsources.volsync.backube", "-A", "-o", "json"))["items"]:
        repo = (r["spec"].get("restic") or {}).get("repository")
        if (r["metadata"]["namespace"], repo) not in fed:
            continue
        st = r.get("status") or {}
        res = (st.get("latestMoverStatus") or {}).get("result")
        restic.append({"name": f'{r["metadata"]["namespace"]}/{r["metadata"]["name"]}',
                       "last_success": st.get("lastSyncTime") if res == "Successful" else None,
                       "failed_since": st.get("lastSyncStartTime") if res == "Failed" else None})
    cnpg = []
    for c in json.loads(kubectl("get", "clusters.postgresql.cnpg.io", "-A", "-o", "json"))["items"]:
        b = ((c["spec"].get("backup") or {}).get("barmanObjectStore") or {})
        cred = ((b.get("s3Credentials") or {}).get("accessKeyId") or {}).get("name")
        if (c["metadata"]["namespace"], cred) not in fed:
            continue
        cond = [x for x in (c.get("status", {}).get("conditions") or []) if x["type"] == "ContinuousArchiving"]
        cnpg.append({"name": f'{c["metadata"]["namespace"]}/{c["metadata"]["name"]}',
                     "archiving": bool(cond) and cond[0]["status"] == "True"})
    return {"secrets": len(fed), "restic": restic, "cnpg": cnpg}
