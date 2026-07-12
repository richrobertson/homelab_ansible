#!/bin/sh
# Managed by Ansible: configure_grafana_log_forwarding.yml
set -eu

nas="$(hostname -s)"
collected_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
snapshot_bin=/var/packages/SnapshotReplication/target/sbin

package_status() {
    /usr/syno/bin/synopkg status "$1" 2>/dev/null |
        jq -r '.status // "unknown"' 2>/dev/null || printf 'not_installed\n'
}

if [ -x "${snapshot_bin}/synosharereplica" ]; then
    replica_count="$("${snapshot_bin}/synosharereplica" --list 2>/dev/null | grep -c '"replica_id"' || true)"
    schedules="$("${snapshot_bin}/synodrsnapschedtool" list_sched_task 2>/dev/null || printf '{}')"
    replication_schedule_count="$(printf '%s' "${schedules}" | jq -r '.replication_sched_tasks | length' 2>/dev/null || printf '0')"
    snapshot_schedule_count="$(printf '%s' "${schedules}" | jq -r '.share_sched_tasks | length' 2>/dev/null || printf '0')"
    retention_policy_count="$(printf '%s' "${schedules}" | jq -r '.share_retention_tasks | length' 2>/dev/null || printf '0')"

    payload="$(jq -nc \
        --arg component snapshot_replication \
        --arg nas "${nas}" \
        --arg collected_at "${collected_at}" \
        --arg package_status "$(package_status SnapshotReplication)" \
        --argjson replica_count "${replica_count:-0}" \
        --argjson replication_schedule_count "${replication_schedule_count:-0}" \
        --argjson snapshot_schedule_count "${snapshot_schedule_count:-0}" \
        --argjson retention_policy_count "${retention_policy_count:-0}" \
        '{component:$component,event:"posture",nas:$nas,collected_at:$collected_at,package_status:$package_status,replica_count:$replica_count,replication_schedule_count:$replication_schedule_count,snapshot_schedule_count:$snapshot_schedule_count,retention_policy_count:$retention_policy_count}')"
    logger -t synology-observability -- "${payload}"
fi

active_backup_db=/volume1/@ActiveBackup/activity.db
active_backup_status="$(package_status ActiveBackup)"
if [ "${active_backup_status}" = running ] && [ -r "${active_backup_db}" ]; then
    sqlite3 -json "${active_backup_db}" '
        SELECT r.task_id, r.task_name, r.status, r.time_start, r.time_end,
               r.success_count, r.warning_count, r.error_count, r.task_config
          FROM result_table r
          JOIN (
                SELECT task_id, MAX(result_id) AS result_id
                  FROM result_table
                 WHERE task_id IS NOT NULL
                 GROUP BY task_id
               ) latest ON latest.result_id = r.result_id
         ORDER BY r.task_id;
    ' | jq -c --arg nas "${nas}" --arg collected_at "${collected_at}" --arg package_status "${active_backup_status}" '
        .[] |
        .status_text = (if .status == 2 then "success" elif .status == 4 then "failed" else "unknown" end) |
        .device = (try (.task_config | fromjson | .device_list[0].host_name) catch "") |
        del(.task_config) |
        . + {component:"active_backup",event:"latest_task_result",nas:$nas,collected_at:$collected_at,package_status:$package_status}
    ' | while IFS= read -r payload; do
        logger -t synology-observability -- "${payload}"
    done
fi
