#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Identify and optionally delete Synology iSCSI IQNs with no mapped LUNs."
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=5022)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password")
    parser.add_argument(
        "--password-env",
        help="Environment variable name containing the SSH/sudo password.",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read SSH/sudo password from stdin.",
    )
    parser.add_argument(
        "--k8s-prefix",
        default="k8s-csi-pvc-",
        help="Only targets with this name prefix are deleted unless --include-non-k8s is set.",
    )
    parser.add_argument(
        "--report",
        default="ansible/synology/logs/empty_iqn_targets_report.csv",
    )
    parser.add_argument(
        "--log",
        default="ansible/synology/logs/empty_iqn_target_cleanup.log",
    )
    parser.add_argument(
        "--include-non-k8s",
        action="store_true",
        help="Include non-k8s targets with no mapped LUNs.",
    )
    parser.add_argument(
        "--include-default-target",
        action="store_true",
        help="Include Synology's default target if it has no mapped LUNs.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the identified targets. Without this flag, only a report is written.",
    )
    return parser.parse_args()


def remote_exec(host: str, port: int, user: str, password: str, remote_cmd: str) -> str:
    remote_shell = f"sudo -S -p '' {remote_cmd}"
    env = os.environ.copy()
    env["SSHPASS"] = password
    result = subprocess.run(
        [
            "sshpass",
            "-e",
            "ssh",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "StrictHostKeyChecking=no",
            "-p",
            str(port),
            f"{user}@{host}",
            remote_shell,
        ],
        input=f"{password}\n",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "remote command failed")
    return result.stdout


def extract_json(raw_text: str) -> dict:
    match = re.search(r"(?ms)^\{.*\}\s*$", raw_text)
    if not match:
        raise RuntimeError("Could not locate JSON payload in Synology API output")
    return json.loads(match.group(0))


def fetch_targets(host: str, port: int, user: str, password: str) -> list[dict]:
    raw = remote_exec(
        host,
        port,
        user,
        password,
        "/usr/syno/bin/synowebapi --exec api=SYNO.Core.ISCSI.Target method=list version=1",
    )
    payload = extract_json(raw)
    return payload.get("data", {}).get("targets", [])


def fetch_mapped_target_ids(host: str, port: int, user: str, password: str) -> set[str]:
    raw = remote_exec(host, port, user, password, "cat /usr/syno/etc/iscsi_mapping.conf")
    return {tid for tid, _ in re.findall(r"(?m)^\[iSCSI_MAP_T(\d+)_L([0-9a-fA-F-]{36})\]$", raw)}


def identify_empty_targets(
    targets: list[dict],
    mapped_target_ids: set[str],
    k8s_prefix: str,
    include_non_k8s: bool,
    include_default_target: bool,
) -> list[dict]:
    empty_targets = []
    for target in sorted(targets, key=lambda item: int(item.get("target_id", 0))):
        target_id = str(target.get("target_id", ""))
        if not target_id or target_id in mapped_target_ids:
            continue

        name = str(target.get("name", ""))
        is_default_target = bool(target.get("is_default_target", False))
        is_k8s = name.startswith(k8s_prefix)

        if is_default_target and not include_default_target:
            continue
        if not is_k8s and not include_non_k8s:
            continue

        empty_targets.append(
            {
                "target_id": target_id,
                "name": name,
                "iqn": str(target.get("iqn", "")),
                "max_sessions": str(int(target.get("max_sessions", 0) or 0)),
                "is_default_target": str(is_default_target).lower(),
                "is_k8s": str(is_k8s).lower(),
            }
        )
    return empty_targets


def write_report(report_path: Path, targets: list[dict]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "target_id",
                "name",
                "iqn",
                "max_sessions",
                "is_default_target",
                "is_k8s",
            ],
        )
        writer.writeheader()
        writer.writerows(targets)


def delete_target(host: str, port: int, user: str, password: str, target_id: str) -> tuple[bool, str, str]:
    commands = [
        f"/usr/syno/bin/synowebapi --exec api=SYNO.Core.ISCSI.Target method=delete version=1 target_id_list='[{target_id}]'",
        f"/usr/syno/bin/synowebapi --exec api=SYNO.Core.ISCSI.Target method=delete version=1 target_id_list=[{target_id}]",
        f"/usr/syno/bin/synowebapi --exec api=SYNO.Core.ISCSI.Target method=delete version=1 target_ids='[{target_id}]'",
        f"/usr/syno/bin/synowebapi --exec api=SYNO.Core.ISCSI.Target method=delete version=1 target_ids=[{target_id}]",
        f"/usr/syno/bin/synowebapi --exec api=SYNO.Core.ISCSI.Target method=delete version=1 target_id={target_id}",
    ]

    last_output = ""
    for command in commands:
        output = remote_exec(host, port, user, password, command)
        last_output = output.strip()
        try:
            payload = extract_json(output)
        except RuntimeError:
            payload = {}
        if payload.get("success") is True:
            return True, command, last_output

    return False, commands[-1], last_output


def append_log(log_path: Path, lines: list[str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(f"{line}\n")


def main() -> int:
    args = parse_args()

    password = args.password or ""
    if not password and args.password_env:
        password = os.environ.get(args.password_env, "")
    if not password and args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    if not password:
        raise RuntimeError("Provide password via --password, --password-env, or --password-stdin")

    targets = fetch_targets(args.host, args.port, args.user, password)
    mapped_target_ids = fetch_mapped_target_ids(args.host, args.port, args.user, password)
    empty_targets = identify_empty_targets(
        targets,
        mapped_target_ids,
        args.k8s_prefix,
        args.include_non_k8s,
        args.include_default_target,
    )

    report_path = Path(args.report)
    write_report(report_path, empty_targets)
    print(f"empty_targets={len(empty_targets)}")
    print(f"report={report_path}")

    if not args.delete:
        return 0

    log_lines = []
    deleted_ids = []
    for target in empty_targets:
        target_id = target["target_id"]
        success, command, output = delete_target(args.host, args.port, args.user, password, target_id)
        status = "deleted" if success else "failed"
        log_lines.extend(
            [
                f"TARGET_ID={target_id} IQN={target['iqn']} NAME={target['name']} STATUS={status}",
                f"  COMMAND={command}",
                f"  OUTPUT={output}",
            ]
        )
        if success:
            deleted_ids.append(target_id)

    append_log(Path(args.log), log_lines)

    remaining_targets = fetch_targets(args.host, args.port, args.user, password)
    remaining_target_ids = {str(target.get('target_id', '')) for target in remaining_targets}
    not_removed = [target_id for target_id in deleted_ids if target_id in remaining_target_ids]

    print(f"deleted_targets={len(deleted_ids)}")
    print(f"still_present_after_delete={len(not_removed)}")
    if not_removed:
        print("remaining_target_ids=" + ",".join(sorted(not_removed)))
        return 1

    failed_count = len(empty_targets) - len(deleted_ids)
    print(f"failed_targets={failed_count}")
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())