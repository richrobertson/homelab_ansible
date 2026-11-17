"""aws_iam_access_key rotation kind for rotate_due_k8s_secrets.py.

Same design as ansible/proxmox/roles/pbs_s3_key_rotation (read its header):

  * The key being rotated never mints its own successor. Vault's AWS secrets
    engine issues a THROWAWAY IAM user per run (role `rotator_role`), allowed
    only to manage `iam_user`'s keys and console password; its lease is revoked
    when the run ends, success or not.
  * AWS allows two live access keys per user, and that overlap is the safety
    mechanism: the old key keeps working until the new one is proven with STS
    and every Vault copy holds it. Only then is the old key deleted.
  * Any failure before that deletion: Vault copies rolled back to their previous
    versions and the NEW key deleted - the old key was never touched.

Map entry (ansible/k8s/vars/k8s_secret_consumers.yml):
  aws/user/<user>:
    kind: aws_iam_access_key
    kube_context: admin@prod            (selects which runner owns it)
    iam_user: <IAM user name>
    rotator_role: <aws/roles/<name>>
    fields: {id: access_key, secret: secret_access_key}
    console_password_field: console_password   (optional)
    copies: [{path, id: <field>, secret: <field>}]
Secret values never reach a command line or a log: Vault gets them on stdin.
"""
from __future__ import annotations

import json
import secrets
import string
import time
from collections.abc import Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError


def _gen_console_password() -> str:
    # IAM password policies commonly require all four classes; 32 chars of a
    # mixed alphabet with one of each guaranteed.
    alpha = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    pw = [secrets.choice(string.ascii_lowercase), secrets.choice(string.ascii_uppercase),
          secrets.choice(string.digits), secrets.choice("!@#$%^&*()-_=+")]
    pw += [secrets.choice(alpha) for _ in range(28)]
    secrets.SystemRandom().shuffle(pw)
    return "".join(pw)


def rotate_aws_iam_key(name: str, spec: dict, run: Callable, vault_json: Callable,
                       log: Callable, step_error: type) -> None:
    user, f = spec["iam_user"], spec["fields"]
    copies = spec.get("copies") or []
    paths = [name] + [c["path"] for c in copies]
    versions = {p: vault_json("kv", "get", "-mount=secret", p)["data"]["metadata"]["version"] for p in paths}
    truth = vault_json("kv", "get", "-mount=secret", name)["data"]["data"]
    old_id = truth[f["id"]]

    lease = vault_json("read", f"aws/creds/{spec['rotator_role']}")
    lease_id, rc = lease["lease_id"], lease["data"]
    new = iam = None
    try:
        sess = {"aws_access_key_id": rc["access_key"], "aws_secret_access_key": rc["secret_key"]}
        iam = boto3.client("iam", **sess)
        for attempt in range(12):  # a freshly minted IAM user takes a few seconds to propagate
            try:
                existing = iam.list_access_keys(UserName=user)["AccessKeyMetadata"]
                break
            except ClientError as exc:
                if attempt == 11:
                    raise step_error(f"rotator identity never became usable: {exc}") from exc
                time.sleep(5)
        ids = [k["AccessKeyId"] for k in existing]
        if old_id not in ids:
            raise step_error(f"Vault's key for {user} is not one of its live IAM keys; refusing")
        if len(ids) >= 2:
            raise step_error(f"{user} already has 2 access keys; refusing to guess which to drop")

        new = iam.create_access_key(UserName=user)["AccessKey"]
        log(f"  created new access key for {user}; proving it with STS")
        for attempt in range(18):
            try:
                arn = boto3.client("sts", aws_access_key_id=new["AccessKeyId"],
                                   aws_secret_access_key=new["SecretAccessKey"]).get_caller_identity()["Arn"]
                break
            except ClientError:
                if attempt == 17:
                    raise step_error("new key never authenticated with STS") from None
                time.sleep(5)
        if not arn.endswith(f":user/{user}"):
            raise step_error("new key authenticates as an unexpected principal")

        run(["vault", "kv", "patch", "-mount=secret", name, "-"],
            stdin=json.dumps({f["id"]: new["AccessKeyId"], f["secret"]: new["SecretAccessKey"]}))
        for c in copies:
            run(["vault", "kv", "patch", "-mount=secret", c["path"], "-"],
                stdin=json.dumps({c["id"]: new["AccessKeyId"], c["secret"]: new["SecretAccessKey"]}))
        for p, idf in [(name, f["id"])] + [(c["path"], c["id"]) for c in copies]:
            if vault_json("kv", "get", "-mount=secret", p)["data"]["data"].get(idf) != new["AccessKeyId"]:
                raise step_error(f"Vault {p} did not take the new key")
    except (step_error, ClientError, BotoCoreError, KeyError) as exc:
        log(f"  FAILED before revoking the old key ({exc}); restoring Vault, deleting the new key")
        for p, v in versions.items():
            try:
                if vault_json("kv", "get", "-mount=secret", p)["data"]["metadata"]["version"] != v:
                    run(["vault", "kv", "rollback", "-mount=secret", f"-version={v}", p])
            except step_error as exc2:
                raise RuntimeError(f"ROLLBACK FAILED restoring {p}: {exc2}") from exc2
        if new and iam:
            try:
                iam.delete_access_key(UserName=user, AccessKeyId=new["AccessKeyId"])
            except (ClientError, BotoCoreError) as exc2:
                raise RuntimeError(f"Vault restored, but the NEW key for {user} could not be deleted: {exc2}") from exc2
        _revoke(run, lease_id, log)
        raise step_error(f"{exc} (rolled back: old key untouched, Vault restored)") from exc

    # Point of no return: every copy holds the proven new key.
    try:
        iam.delete_access_key(UserName=user, AccessKeyId=old_id)
        log(f"  deleted the old access key for {user}")
        pwf = spec.get("console_password_field")
        if pwf:
            try:
                iam.get_login_profile(UserName=user)
                pw = _gen_console_password()
                iam.update_login_profile(UserName=user, Password=pw, PasswordResetRequired=False)
                try:
                    run(["vault", "kv", "patch", "-mount=secret", name, "-"], stdin=json.dumps({pwf: pw}))
                except step_error as exc:
                    raise RuntimeError(f"console password for {user} CHANGED in AWS but the Vault write "
                                       f"failed ({exc}); reset it from the AWS root account") from exc
                log(f"  rotated the console password for {user}")
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                    raise
    except (ClientError, BotoCoreError) as exc:
        # The new key is live everywhere; only cleanup failed. Loud, not fatal.
        raise step_error(f"new key is LIVE and in Vault, but cleanup failed: {exc}") from exc
    finally:
        _revoke(run, lease_id, log)

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for pth in paths:
        run(["vault", "kv", "metadata", "patch", "-mount=secret", f"-custom-metadata=rotated_at={stamp}", pth])


def _revoke(run: Callable, lease_id: str, log: Callable) -> None:
    try:
        # Path form, so the runner policy can be scoped to this role's own leases
        # (sys/leases/revoke/aws/creds/<role>/*) instead of revoking any lease.
        run(["vault", "write", "-f", f"sys/leases/revoke/{lease_id}"])
    except Exception as exc:  # noqa: BLE001 - never mask the rotation result
        log(f"  warning: could not revoke rotator lease: {exc}")
