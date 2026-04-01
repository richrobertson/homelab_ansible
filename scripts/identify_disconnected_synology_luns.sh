#!/usr/bin/env bash
set -euo pipefail

INVENTORY="inventory/environments/synology.ini"
HOST_GROUP="synology_nas"
PORT="5022"
USER=""
PASSWORD=""
PASSWORD_ENV=""
PASSWORD_STDIN="false"
OUTPUT=""
K8S_PREFIX="k8s-csi-pvc-"

usage() {
  cat <<'EOF'
Usage:
  scripts/identify_disconnected_synology_luns.sh --user USER [--password PASS|--password-env VAR|--password-stdin] [options]

Options:
  --inventory PATH     Ansible inventory file (default: inventory/environments/synology.ini)
  --host-group NAME    Inventory host/group to query (default: synology_nas)
  --port PORT          SSH port (default: 5022)
  --user USER          SSH username (required)
  --password PASS      SSH/sudo password
  --password-env VAR   Read SSH/sudo password from environment variable VAR
  --password-stdin     Read SSH/sudo password from stdin
  --output PATH        Output report path (default: /tmp/orphan_luns_dry_run_report_<timestamp>.csv)
  --k8s-prefix PREFIX  Target name prefix to treat as k8s (default: k8s-csi-pvc-)
  -h, --help           Show this help
EOF
}

is_valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && (( "$1" >= 1 && "$1" <= 65535 ))
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory)
      INVENTORY="$2"
      shift 2
      ;;
    --host-group)
      HOST_GROUP="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --user)
      USER="$2"
      shift 2
      ;;
    --password)
      PASSWORD="$2"
      shift 2
      ;;
    --password-env)
      PASSWORD_ENV="$2"
      shift 2
      ;;
    --password-stdin)
      PASSWORD_STDIN="true"
      shift
      ;;
    --output)
      OUTPUT="$2"
      shift 2
      ;;
    --k8s-prefix)
      K8S_PREFIX="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$USER" ]]; then
  echo "--user is required" >&2
  usage
  exit 1
fi

if [[ -n "$PASSWORD_ENV" ]]; then
  PASSWORD="${!PASSWORD_ENV:-}"
fi

if [[ "$PASSWORD_STDIN" == "true" ]]; then
  IFS= read -r PASSWORD
fi

if [[ -z "$PASSWORD" ]]; then
  echo "one of --password, --password-env, or --password-stdin is required" >&2
  usage
  exit 1
fi

if [[ -z "$OUTPUT" ]]; then
  OUTPUT="/tmp/orphan_luns_dry_run_report_$(date -u +%Y%m%dT%H%M%SZ).csv"
fi

if ! is_valid_port "$PORT"; then
  echo "--port must be an integer between 1 and 65535" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT")"

tmp_luns="$(mktemp)"
tmp_targets="$(mktemp)"
tmp_mapping="$(mktemp)"
tmp_ansible_vars="$(mktemp)"
tmp_hosts="$(mktemp)"
cleanup() {
  rm -f "$tmp_luns" "$tmp_targets" "$tmp_mapping" "$tmp_ansible_vars" "$tmp_hosts"
}
trap cleanup EXIT

chmod 600 "$tmp_ansible_vars"
python3 - "$tmp_ansible_vars" "$USER" "$PORT" <<'PY' <<<"$PASSWORD"
import json
import pathlib
import sys

output_path = pathlib.Path(sys.argv[1])
ansible_user = sys.argv[2]
ansible_port = int(sys.argv[3])
password = sys.stdin.read().rstrip("\n")

payload = {
    "ansible_user": ansible_user,
    "ansible_port": ansible_port,
    "ansible_password": password,
    "ansible_become_password": password,
}

output_path.write_text(json.dumps(payload), encoding="utf-8")
PY

ansible "$HOST_GROUP" \
  -i "$INVENTORY" \
  --list-hosts \
  -e "@$tmp_ansible_vars" > "$tmp_hosts"

host_count="$(awk '/^  / {count++} END {print count+0}' "$tmp_hosts")"
if [[ "$host_count" != "1" ]]; then
  echo "expected exactly one Synology host from --host-group; got $host_count" >&2
  echo "ansible --list-hosts output:" >&2
  cat "$tmp_hosts" >&2
  exit 1
fi

run_raw() {
  local cmd="$1"
  ansible "$HOST_GROUP" \
    -i "$INVENTORY" \
    -m raw \
    -a "$cmd" \
    -e "@$tmp_ansible_vars" \
    -b --become-method=sudo
}

run_raw "/usr/syno/bin/synowebapi --exec api=SYNO.Core.ISCSI.LUN method=list version=1" > "$tmp_luns"
run_raw "/usr/syno/bin/synowebapi --exec api=SYNO.Core.ISCSI.Target method=list version=1 additional='[\"status\"]'" > "$tmp_targets"
run_raw "cat /usr/syno/etc/iscsi_mapping.conf" > "$tmp_mapping"

python3 - "$tmp_luns" "$tmp_targets" "$tmp_mapping" "$OUTPUT" "$K8S_PREFIX" <<'PY'
import csv
import json
import re
import sys

luns_raw_path, targets_raw_path, mapping_raw_path, output_path, k8s_prefix = sys.argv[1:]


def extract_json(raw_text: str):
  decoder = json.JSONDecoder()
  candidates = []
  for index, char in enumerate(raw_text):
    if char != "{":
      continue
    try:
      payload, _ = decoder.raw_decode(raw_text[index:])
      if isinstance(payload, dict):
        candidates.append(payload)
    except json.JSONDecodeError:
      continue
  for payload in reversed(candidates):
    if "data" in payload or "success" in payload:
      return payload
  if candidates:
    return candidates[-1]
  raise RuntimeError("Could not locate JSON payload in command output")


with open(luns_raw_path, "r", encoding="utf-8", errors="ignore") as handle:
  luns_payload = extract_json(handle.read())

with open(targets_raw_path, "r", encoding="utf-8", errors="ignore") as handle:
  targets_payload = extract_json(handle.read())

with open(mapping_raw_path, "r", encoding="utf-8", errors="ignore") as handle:
  mapping_raw = handle.read()

uuid_to_tid = {}
for tid, uuid in re.findall(r"(?m)^\[iSCSI_MAP_T(\d+)_L([0-9a-fA-F-]{36})\]$", mapping_raw):
  uuid_to_tid[uuid.lower()] = tid

targets_by_id = {}
for target in targets_payload.get("data", {}).get("targets", []):
  target_id = str(target.get("target_id", ""))
  if not target_id:
    continue
  status = str(target.get("status", "unknown"))
  targets_by_id[target_id] = {
    "name": str(target.get("name", "")),
    "status": status,
    "connected": status == "connected",
  }

rows = []
for lun in luns_payload.get("data", {}).get("luns", []):
  uuid = str(lun.get("uuid") or lun.get("lun_uuid") or "").lower()
  if not uuid:
    continue
  tid = uuid_to_tid.get(uuid, "")
  target_meta = targets_by_id.get(tid, {})
  target_name = target_meta.get("name", "")
  target_status = str(target_meta.get("status", "unknown"))
  is_k8s = bool(tid and target_name.startswith(k8s_prefix))
  if not is_k8s or target_status == "connected":
    continue
  rows.append(
    {
      "uuid": uuid,
      "name": str(lun.get("name") or lun.get("display_name") or "unnamed-lun"),
      "description": str(lun.get("description") or ""),
      "mapped": "true",
      "target_connected": "false",
      "mapping": f"tid={tid};target={target_name};status={target_status}",
    }
  )

rows.sort(key=lambda item: item["uuid"])

with open(output_path, "w", newline="", encoding="utf-8") as handle:
  writer = csv.DictWriter(
    handle,
    fieldnames=["uuid", "name", "description", "mapped", "target_connected", "mapping"],
  )
  writer.writeheader()
  writer.writerows(rows)

print(f"disconnected_k8s_luns={len(rows)}")
print(f"report={output_path}")
PY
