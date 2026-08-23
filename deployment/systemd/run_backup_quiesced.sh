#!/usr/bin/env bash
# Run the daily recovery drill without competing with the continuous worker on
# the 1-GiB production host.  This script is installed at a stable root-owned
# path before the systemd unit is enabled, so an in-place upgrade can use it
# while /opt/finance-radar/current still points at the prior release.
set -euo pipefail
umask 077

BASE="${FINANCE_RADAR_BASE:-/opt/finance-radar}"
WORKER_UNIT="${FINANCE_RADAR_WORKER_UNIT:-finance-radar-worker.service}"
RESUME_INHIBIT="${FINANCE_RADAR_WORKER_RESUME_INHIBIT:-/run/finance-radar/worker-resume.inhibit}"
BACKUP_SOURCE_ROOT="${FINANCE_RADAR_BACKUP_SOURCE_ROOT:-$BASE/current}"
PREDEPLOY_BRIDGE="${FINANCE_RADAR_PREDEPLOY_BRIDGE:-0}"
BACKUP_START_INHIBIT="${FINANCE_RADAR_BACKUP_START_INHIBIT:-/run/finance-radar/backup-start.inhibit}"
PYTHON_BIN="$BASE/venv/bin/python"
OPERATIONS_DB="${FINANCE_RADAR_OPS_DB:-$BASE/shared/data/finance_radar_operations.sqlite3}"

[ "$(id -u)" -eq 0 ] || {
    printf 'finance-radar backup quiesce wrapper must run as root\n' >&2
    exit 2
}
[ -x "$PYTHON_BIN" ] || {
    printf 'finance-radar backup Python is unavailable: %s\n' "$PYTHON_BIN" >&2
    exit 2
}
[[ "$OPERATIONS_DB" == /* && "$OPERATIONS_DB" != *$'\n'* ]] || {
    printf 'finance-radar operations database path is invalid\n' >&2
    exit 2
}

case "$PREDEPLOY_BRIDGE" in
    0|1)
        ;;
    *)
        printf 'finance-radar backup bridge flag is invalid: %s\n' "$PREDEPLOY_BRIDGE" >&2
        exit 2
        ;;
esac
if [ -e "$BACKUP_START_INHIBIT" ] || [ -L "$BACKUP_START_INHIBIT" ]; then
    [ -f "$BACKUP_START_INHIBIT" ] && [ ! -L "$BACKUP_START_INHIBIT" ] || {
        printf 'finance-radar backup start inhibit marker is unsafe\n' >&2
        exit 3
    }
    if [ "$PREDEPLOY_BRIDGE" = 0 ]; then
        printf 'scheduled backup start is inhibited during deployment stabilization\n' >&2
        exit 3
    fi
fi
[ -d "$BASE/releases" ] && [ ! -L "$BASE/releases" ] || {
    printf 'finance-radar release root is unavailable: %s\n' "$BASE/releases" >&2
    exit 2
}
case "$BACKUP_SOURCE_ROOT" in
    "$BASE/current"|"$BASE/releases/"*)
        ;;
    *)
        printf 'finance-radar backup source is outside the managed release roots: %s\n' \
            "$BACKUP_SOURCE_ROOT" >&2
        exit 2
        ;;
esac
SOURCE_ROOT="$(readlink -f -- "$BACKUP_SOURCE_ROOT")" || {
    printf 'finance-radar backup source cannot be resolved: %s\n' "$BACKUP_SOURCE_ROOT" >&2
    exit 2
}
RELEASES_ROOT="$(readlink -f -- "$BASE/releases")" || {
    printf 'finance-radar release root cannot be resolved: %s\n' "$BASE/releases" >&2
    exit 2
}
CURRENT_ROOT="$(readlink -f -- "$BASE/current")" || {
    printf 'finance-radar current release cannot be resolved: %s\n' "$BASE/current" >&2
    exit 2
}
case "$SOURCE_ROOT" in
    "$RELEASES_ROOT"/*)
        ;;
    *)
        printf 'finance-radar backup source did not resolve below releases: %s\n' "$SOURCE_ROOT" >&2
        exit 2
        ;;
esac
SOURCE_LEAF="${SOURCE_ROOT#"$RELEASES_ROOT/"}"
[[ "$SOURCE_LEAF" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]] || {
    printf 'finance-radar backup source is not a direct release directory: %s\n' "$SOURCE_ROOT" >&2
    exit 2
}
[ -d "$SOURCE_ROOT" ] && [ ! -L "$SOURCE_ROOT" ] || {
    printf 'finance-radar backup source is not a regular release directory: %s\n' "$SOURCE_ROOT" >&2
    exit 2
}
[ -f "$SOURCE_ROOT/app/ops/backup.py" ] && [ ! -L "$SOURCE_ROOT/app/ops/backup.py" ] || {
    printf 'finance-radar backup source is missing app.ops.backup: %s\n' "$SOURCE_ROOT" >&2
    exit 2
}
runuser -u finance-radar -- test -r "$SOURCE_ROOT/app/ops/backup.py" || {
    printf 'finance-radar backup source is not readable by the runtime account: %s\n' "$SOURCE_ROOT" >&2
    exit 2
}
if [ "$PREDEPLOY_BRIDGE" = 1 ]; then
    [ "$SOURCE_ROOT" != "$CURRENT_ROOT" ] || {
        printf 'predeploy backup bridge must use a candidate release, not current\n' >&2
        exit 2
    }
else
    [ "$SOURCE_ROOT" = "$CURRENT_ROOT" ] || {
        printf 'normal backup must use the active current release\n' >&2
        exit 2
    }
fi

worker_was_active=0

resume_owned_worker() {
    local original_status="$1"
    if [ "$worker_was_active" -ne 1 ]; then
        return "$original_status"
    fi
    # The installer writes this transient root-owned marker *before* it stops
    # the worker for its protected pre/post-cutover bridge.  A wrapper that
    # actually stopped a worker just before the marker appeared must not race
    # the deployment by bringing it back during that bridge.
    if [ -e "$RESUME_INHIBIT" ] || [ -L "$RESUME_INHIBIT" ]; then
        printf 'backup_worker_resume=INHIBITED marker=%s\n' "$RESUME_INHIBIT" >&2
        return "$original_status"
    fi
    if ! systemctl start "$WORKER_UNIT" || ! systemctl is-active --quiet "$WORKER_UNIT"; then
        printf 'backup_worker_resume=FAILED unit=%s\n' "$WORKER_UNIT" >&2
        # Do not disguise a successful recovery receipt as a successful daily
        # run when the workload it intentionally paused failed to return.
        if [ "$original_status" -eq 0 ]; then
            return 70
        fi
        return "$original_status"
    fi
    printf 'backup_worker_resume=PASS unit=%s\n' "$WORKER_UNIT"
    return "$original_status"
}

finish() {
    local status=$? final_status
    trap - EXIT
    set +e
    resume_owned_worker "$status"
    final_status=$?
    exit "$final_status"
}
trap finish EXIT

worker_state="$(systemctl show "$WORKER_UNIT" --property=ActiveState --value)"
case "$worker_state" in
    active)
        systemctl stop "$WORKER_UNIT"
        if systemctl is-active --quiet "$WORKER_UNIT"; then
            printf 'finance-radar worker remains active after backup quiesce request\n' >&2
            exit 3
        fi
        worker_was_active=1
        printf 'backup_worker_quiesce=PASS unit=%s\n' "$WORKER_UNIT"
        ;;
    inactive|failed)
        # Do not revive a deliberately stopped worker.  In particular, the
        # protected deployment bridge starts this unit with the worker already
        # inactive and will perform its own explicit restart after validation.
        printf 'backup_worker_quiesce=NOT_OWNED state=%s unit=%s\n' "$worker_state" "$WORKER_UNIT"
        ;;
    *)
        printf 'refusing backup while worker has a transitional/unknown state: %s\n' "$worker_state" >&2
        exit 3
        ;;
esac

cd -- "$SOURCE_ROOT"
backup_args=(backup --retention 1 --weekly-retention 0)
if [ "$PREDEPLOY_BRIDGE" = 1 ]; then
    backup_args+=(--predeploy-bridge)
fi
printf 'backup_source=READY release=%s predeploy_bridge=%s\n' "$SOURCE_ROOT" "$PREDEPLOY_BRIDGE"
runuser -u finance-radar -- env \
    "PYTHONPATH=$SOURCE_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -B -m app.ops.backup "${backup_args[@]}"

# A normal daily/full backup has already completed its expensive isolated
# restore at this point. Record a small root-owned attestation so later
# code-only releases can prove that recovery work happened recently without
# repeating multi-gigabyte copies and restores. Candidate bridge backups do not
# create durable backup_runs rows and must never replace the daily attestation.
if [ "$PREDEPLOY_BRIDGE" = 0 ] && \
   [ "${FINANCE_RADAR_SKIP_ROOT_BACKUP_ATTESTATION:-0}" != 1 ]; then
    ATTESTATION_DIR=/var/lib/finance-radar
    ATTESTATION_PATH="$ATTESTATION_DIR/latest-verified-backup.json"
    install -d -m 0700 -o root -g root "$ATTESTATION_DIR"
    python3 - "$OPERATIONS_DB" \
        "$ATTESTATION_PATH" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import sys
import tempfile

operations_path = Path(sys.argv[1])
attestation_path = Path(sys.argv[2])
uri = f"file:{operations_path.as_posix()}?mode=ro"
with sqlite3.connect(uri, uri=True) as connection:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    row = connection.execute(
        """SELECT backup_id,backup_path,source_bytes,backup_bytes,quick_check,
                  restored_count_json,status,created_at,verified_at,manifest_path,
                  components_json,snapshot_kind
           FROM backup_runs WHERE status='VERIFIED'
           ORDER BY verified_at DESC,created_at DESC LIMIT 1"""
    ).fetchone()
if row is None:
    raise SystemExit("verified backup completed without a durable run record")
record = dict(row)
manifest_path = Path(str(record.get("manifest_path") or ""))
if Path(str(record.get("backup_path") or "")) != manifest_path:
    raise SystemExit("verified backup record paths disagree")
if manifest_path.is_symlink() or not manifest_path.is_file():
    raise SystemExit("verified backup manifest is unavailable")
bundle = manifest_path.parent
manifest_bytes = manifest_path.read_bytes()
manifest = json.loads(manifest_bytes)
recorded_components = json.loads(record.get("components_json") or "")
if manifest.get("format") != "finance-radar-recovery-bundle-v1":
    raise SystemExit("verified backup manifest format is invalid")
if manifest.get("snapshot_id") != bundle.name:
    raise SystemExit("verified backup snapshot id is invalid")
if recorded_components != manifest.get("components"):
    raise SystemExit("verified backup component receipt differs from manifest")
payload_stats = []
manifest_files = manifest.get("files")
if not isinstance(manifest_files, list) or not manifest_files:
    raise SystemExit("verified backup file inventory is incomplete")
manifest_paths = []
for item in manifest_files:
    relative = item.get("path") if isinstance(item, dict) else None
    if not isinstance(relative, str) or not relative:
        raise SystemExit("verified backup file inventory is invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise SystemExit("verified backup payload path is unsafe")
    candidate = bundle.joinpath(*pure.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise SystemExit("verified backup payload is missing or unsafe")
    resolved_candidate = candidate.resolve()
    if bundle.resolve() not in resolved_candidate.parents:
        raise SystemExit("verified backup payload escaped its bundle")
    stat = candidate.stat()
    expected_bytes = item.get("bytes")
    if (
        not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 0
    ):
        raise SystemExit("verified backup payload byte count is invalid")
    expected_sha = item.get("sha256")
    if (
        not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha)
    ):
        raise SystemExit("verified backup payload hash is invalid")
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_sha = digest.hexdigest()
    if stat.st_size != expected_bytes or actual_sha != expected_sha:
        raise SystemExit("verified backup payload size differs from manifest")
    manifest_paths.append(relative)
    payload_stats.append(
        {
            "path": relative,
            "bytes": stat.st_size,
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": actual_sha,
        }
    )
if len(set(manifest_paths)) != len(manifest_paths):
    raise SystemExit("verified backup manifest contains duplicate payload paths")
actual_bundle_files = set()
for candidate in bundle.rglob("*"):
    relative = candidate.relative_to(bundle).as_posix()
    if candidate.is_symlink():
        raise SystemExit("verified backup bundle contains a symlink")
    if candidate.is_file():
        actual_bundle_files.add(relative)
    elif not candidate.is_dir():
        raise SystemExit("verified backup bundle contains a special object")
expected_bundle_files = set(manifest_paths) | {"manifest.json"}
if actual_bundle_files != expected_bundle_files:
    raise SystemExit("verified backup bundle file set differs from manifest")
attestation = {
    "format": "finance-radar-root-backup-attestation-v1",
    "status": record["status"],
    "backup_id": record["backup_id"],
    "snapshot_id": bundle.name,
    "manifest_path": str(manifest_path),
    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "verified_at": record["verified_at"],
    "quick_check": record["quick_check"],
    "snapshot_kind": record["snapshot_kind"],
    "source_bytes": record["source_bytes"],
    "backup_bytes": record["backup_bytes"],
    "restored_count_json": record["restored_count_json"],
    "components": recorded_components,
    "payload_stats": payload_stats,
}
attestation_path.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(
    prefix=".latest-verified-backup.", dir=attestation_path.parent
)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(attestation, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, attestation_path)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY
    chown root:root "$ATTESTATION_PATH"
    chmod 0600 "$ATTESTATION_PATH"
    printf 'backup_root_attestation=PASS path=%s\n' "$ATTESTATION_PATH"
fi
