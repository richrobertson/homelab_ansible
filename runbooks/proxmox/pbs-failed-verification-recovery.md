# PBS: snapshot stuck at verify_state=failed

Covers `PBSSnapshotVerifyFailed` on the S3-backed datastores (`pbs-b2`, `pbs-s3`).

## First: is it corruption or a transport blip?

This is the only question that matters, and the verify **task log does not answer
it**. The task log says only `chunks could not be verified` with an error count.

Run both checks on the PBS host:

```bash
# 1. Quarantined chunks -- the decisive test
find /mnt/datastore/pbs-b2-cache/.chunks -name '*.bad*' | wc -l

# 2. The actual failure reason
journalctl --since '<date of the failed verify>' | grep -i "can't verify chunk"
```

| Result | Meaning |
| --- | --- |
| `0` bad chunks, journal shows `load failed - error reading a body from connection` | **Transport blip.** The chunk never arrived intact from the object store. Backup data is fine. |
| Any `*.bad` files, journal shows `corrupt chunk` | **Real corruption.** PBS fetched the chunk and it failed its checksum. Do not just re-verify. |

`proxmox-s3-client` already retries transient errors with exponential back-off
(3 tries, max 30 min per request, since PBS 4.0.15). There is **no configuration
knob** to extend that — not in `s3.cfg`, not in the datastore `tuning` string. A
long enough blip will still land as a failed verification.

## Why it does not clear on its own

The daily verify jobs run `ignore-verified true`, which skips any snapshot that
already carries a verification record — **including a failed one**. The record
only ages out via `outdated-after` (30 days) or at the monthly
`ignore-verified false` full pass. So an untended blip can keep a *critical*
alert firing for weeks.

## Automatic remedy

`pbs-verify-failed-sweep.timer` runs daily at 18:00 and re-verifies exactly the
failed snapshots. It is bounded on purpose:

- gives up on a snapshot after 3 attempts, leaving it failed for a human
- re-verifies at most 3 snapshots per run (object-store egress costs money)
- refuses to touch a datastore holding `.bad` chunks
- defers entirely while any verification task is active

```bash
systemctl start pbs-verify-failed-sweep.service   # safe any time
journalctl -u pbs-verify-failed-sweep -f
cat /var/lib/pbs-verify-sweep/attempts.json       # attempt history
```

Installed by `ansible/proxmox/pbs_verify_failed_sweep.yml`.

## Manual remedy

`proxmox-backup-manager` has **no per-snapshot verify subcommand**. Use the debug
API tool, and target one snapshot — a datastore-wide verify re-reads everything
back out of the object store.

```bash
# find the failed snapshots (walks manifests, reads .unprotected.verify_state)
proxmox-backup-debug inspect file \
  /mnt/datastore/pbs-b2-cache/vm/<id>/<timestamp>/index.json.blob --decode -

# re-verify just that one; --ignore-verified false is what defeats the skip
proxmox-backup-debug api create /admin/datastore/pbs-b2/verify \
  --backup-type vm --backup-id <id> --backup-time <unix-epoch> \
  --ignore-verified false
```

This call **blocks until the verify finishes** (~76 min for a ~36 GiB snapshot),
so run it detached and watch `proxmox-backup-manager task list`.

Snapshots reporting state `none` are simply today's backups awaiting their first
verification — normal, not a fault.

## If it is real corruption

Do not re-verify in a loop. Confirm the chunk is unrecoverable, then prune the
affected snapshot and let the next backup re-upload the data. Check whether other
snapshots share the damaged chunk before pruning.
