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
PYTHON_BIN="$BASE/venv/bin/python"

[ "$(id -u)" -eq 0 ] || {
    printf 'finance-radar backup quiesce wrapper must run as root\n' >&2
    exit 2
}
[ -x "$PYTHON_BIN" ] || {
    printf 'finance-radar backup Python is unavailable: %s\n' "$PYTHON_BIN" >&2
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
