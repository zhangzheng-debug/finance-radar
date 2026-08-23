#!/usr/bin/env bash
set -euo pipefail
umask 027

PREPARED=${1:?prepared restore directory required}
EXPECTED_RELEASE=${2:?expected release required}
PUBLIC_WEB_URL=${3:?public Web URL required}
CONFIRM=${4:-}
BASE=/opt/finance-radar
RELEASE="$BASE/releases/$EXPECTED_RELEASE"
FAILED_BASE="${BASE}.failed-$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_RESTORE_TMPDIR="$BASE/shared/data/.backup-restore-tmp"
MANAGED_UNIT_PATHS=(
    /etc/systemd/system/finance-radar.slice
    /etc/systemd/system/finance-radar-api.service
    /etc/systemd/system/finance-radar-overview-snapshot.service
    /etc/systemd/system/finance-radar-overview-snapshot.timer
    /etc/systemd/system/finance-radar-web.service
    /etc/systemd/system/finance-radar-admin.service
    /etc/systemd/system/finance-radar-reviewer.service
    /etc/systemd/system/finance-radar-operator.service
    /etc/systemd/system/finance-radar-worker.service
    /etc/systemd/system/finance-radar-backup.service
    /etc/systemd/system/finance-radar-backup.timer
    /etc/systemd/system/finance-radar-evidence-llm.service
    /usr/local/libexec/finance-radar/run_backup_quiesced.sh
)
MANAGED_CONFIG_PATHS=(
    /etc/finance-radar.env
    /etc/finance-radar-public.env
    /etc/finance-radar-reviewer-principals.json
)
MANAGED_RUNTIME_UNITS=(
    finance-radar-backup.timer
    finance-radar-backup.service
    finance-radar-overview-snapshot.timer
    finance-radar-overview-snapshot.service
    finance-radar-evidence-llm.service
    finance-radar-worker.service
    finance-radar-admin.service
    finance-radar-reviewer.service
    finance-radar-operator.service
    finance-radar-web.service
    finance-radar-api.service
)
MANAGED_ENABLEMENT_UNITS=(
    finance-radar-api.service
    finance-radar-overview-snapshot.service
    finance-radar-overview-snapshot.timer
    finance-radar-web.service
    finance-radar-worker.service
    finance-radar-backup.timer
    finance-radar-evidence-llm.service
)
BASE_MOVED=0

[ "$(id -u)" -eq 0 ] || { printf 'run as root\n' >&2; exit 2; }
[[ "$EXPECTED_RELEASE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]] || {
    printf 'invalid release id\n' >&2; exit 2;
}
[[ "$PUBLIC_WEB_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/radar/?$ ]] || {
    printf 'public Web URL must be a simple HTTPS URL ending in /radar\n' >&2; exit 2;
}
[ "$CONFIRM" = "--activate" ] || {
    printf 'preflight only: append --activate to install the prepared restore\n'
    exit 3
}
PREPARED=$(realpath -e "$PREPARED")
[[ "$PREPARED" == /tmp/finance-radar-restore-*.prepared ]] || {
    printf 'prepared directory must use /tmp/finance-radar-restore-*.prepared\n' >&2; exit 2;
}
[ ! -e "$BASE" ] && [ ! -L "$BASE" ] || {
    printf 'refusing to overwrite existing %s; use a clean replacement VPS\n' "$BASE" >&2; exit 4;
}
for path in "${MANAGED_UNIT_PATHS[@]}" "${MANAGED_CONFIG_PATHS[@]}"; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        printf 'refusing to overwrite existing Finance Radar managed path: %s\n' "$path" >&2
        exit 4
    fi
done
for command in find getent python3 runuser tar sha256sum systemctl curl; do
    command -v "$command" >/dev/null || { printf 'missing prerequisite: %s\n' "$command" >&2; exit 5; }
done

python3 - "$PREPARED" "$EXPECTED_RELEASE" <<'PY'
import json
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
release = sys.argv[2]
report = json.loads((root / "PREPARED_RESTORE.json").read_text(encoding="utf-8"))
if report.get("status") != "PREPARED_NOT_ACTIVATED":
    raise SystemExit("prepared report status is invalid")
if report.get("expected_release") != release:
    raise SystemExit("prepared report release mismatch")
expected_current = f"/opt/finance-radar/releases/{release}"
if (root / "CURRENT_RELEASE.txt").read_text(encoding="utf-8").strip() != expected_current:
    raise SystemExit("CURRENT_RELEASE mismatch")
required = [
    root / "releases" / release / "requirements.txt",
    root / "releases" / release / "requirements.lock",
    root / "shared" / "data" / "finance_radar.sqlite3",
    root / "shared" / "data" / "finance_radar_operations.sqlite3",
    root / "config" / "etc" / "finance-radar.env",
    root / "SYMLINK_PLAN.json",
]
missing = [str(path) for path in required if not path.is_file()]
if missing:
    raise SystemExit(f"prepared restore is incomplete: {missing}")
local_model = report.get("local_evidence_model") or {}
if local_model:
    policy = local_model.get("restore_policy")
    if policy not in (None, "DISABLED_AFTER_RESTORE"):
        raise SystemExit("prepared restore local evidence model policy is unsafe")

# New migration archives are bound to a verified recovery bundle during
# preparation.  Permit a historical archive only when both markers are absent;
# never silently treat a partially marked or newly marked archive as legacy.
receipt_path = root / "config" / "MIGRATION_RECOVERY_BUNDLE.json"
manifest_path = root / "config" / "MIGRATION_RECOVERY_BUNDLE.manifest.json"
receipt_present = receipt_path.exists() or receipt_path.is_symlink()
manifest_present = manifest_path.exists() or manifest_path.is_symlink()
if receipt_present != manifest_present:
    raise SystemExit("prepared recovery-bundle markers are incomplete")
bundle = report.get("migration_recovery_bundle")
if receipt_present:
    if (
        receipt_path.is_symlink()
        or manifest_path.is_symlink()
        or not receipt_path.is_file()
        or not manifest_path.is_file()
        or not isinstance(bundle, dict)
        or bundle.get("bound_to_verified_recovery_bundle") is not True
        or bundle.get("legacy_archive_contract") is not False
        or bundle.get("consistency") != "verified_full_recovery_bundle"
        or not isinstance(bundle.get("snapshot_id"), str)
        or not re.fullmatch(r"finance_radar_[A-Za-z0-9_]+", bundle["snapshot_id"])
        or not isinstance(bundle.get("source_manifest_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", bundle["source_manifest_sha256"])
        or isinstance(bundle.get("mapped_files"), bool)
        or not isinstance(bundle.get("mapped_files"), int)
        or bundle["mapped_files"] < 2
    ):
        raise SystemExit("prepared recovery-bundle verification is invalid")
elif bundle is not None and (
    not isinstance(bundle, dict)
    or bundle.get("bound_to_verified_recovery_bundle") is not False
    or bundle.get("legacy_archive_contract") is not True
    or bundle.get("consistency") != "legacy_unbound_live_copy"
):
    raise SystemExit("prepared legacy recovery-bundle report is invalid")
PY

rollback() {
    # The replacement preflight above proves none of these paths existed before
    # activation.  Remove every potentially installed unit/configuration on a
    # failure so a retry starts from a genuinely clean host, including the
    # backup timer and advisory model that may otherwise survive a failed API
    # health gate.
    systemctl stop "${MANAGED_RUNTIME_UNITS[@]}" 2>/dev/null || true
    systemctl disable "${MANAGED_ENABLEMENT_UNITS[@]}" 2>/dev/null || true
    if [ "$BASE_MOVED" -eq 1 ]; then
        rm -f -- "${MANAGED_UNIT_PATHS[@]}" "${MANAGED_CONFIG_PATHS[@]}"
        systemctl daemon-reload || true
    fi
    if [ -d "$BASE" ] || [ -L "$BASE" ]; then
        mv "$BASE" "$FAILED_BASE" || true
    fi
    printf 'activation failed; staged files retained at %s\n' "$FAILED_BASE" >&2
}
trap rollback ERR

ensure_public_web_principal() {
    if ! getent group finance-radar-web >/dev/null; then
        groupadd --system finance-radar-web
    fi
    if ! getent passwd finance-radar-web >/dev/null; then
        useradd --system --gid finance-radar-web --home-dir /nonexistent \
            --shell /usr/sbin/nologin finance-radar-web
    fi
}

prepare_backup_restore_tmpdir() {
    if [ -e "$BACKUP_RESTORE_TMPDIR" ] || [ -L "$BACKUP_RESTORE_TMPDIR" ]; then
        [ -d "$BACKUP_RESTORE_TMPDIR" ] && [ ! -L "$BACKUP_RESTORE_TMPDIR" ] || {
            printf 'backup restore temporary path is not a regular directory: %s\n' \
                "$BACKUP_RESTORE_TMPDIR" >&2
            return 1
        }
    else
        install -d -m 0700 -o finance-radar -g finance-radar \
            "$BACKUP_RESTORE_TMPDIR" || return 1
    fi
    find "$BACKUP_RESTORE_TMPDIR" -maxdepth 0 -type d -user finance-radar \
        -group finance-radar -perm 0700 -print -quit \
        | grep -Fx "$BACKUP_RESTORE_TMPDIR" >/dev/null || {
            printf 'backup restore temporary directory has unsafe ownership or mode: %s\n' \
                "$BACKUP_RESTORE_TMPDIR" >&2
            return 1
        }
    runuser -u finance-radar -- test -w "$BACKUP_RESTORE_TMPDIR" && \
        runuser -u finance-radar -- test -x "$BACKUP_RESTORE_TMPDIR" || {
            printf 'backup restore temporary directory is unusable by finance-radar: %s\n' \
                "$BACKUP_RESTORE_TMPDIR" >&2
            return 1
        }
}

grant_public_web_runtime_access() {
    local path streamlit_dir streamlit_unexpected
    for path in "$BASE" "$BASE/releases" "$RELEASE"; do
        [ -d "$path" ] && [ ! -L "$path" ] || {
            printf 'public Web runtime parent is not a regular directory: %s\n' "$path" >&2
            return 1
        }
    done
    [ -d "$RELEASE/app" ] && [ ! -L "$RELEASE/app" ] || return 1
    [ -d "$BASE/venv" ] && [ ! -L "$BASE/venv" ] || return 1
    # Python's FileFinder must enumerate the candidate root to discover app.
    # The root may expose names, but non-public files remain 0640 and unreadable
    # to the isolated public UID.
    chmod 0711 "$BASE"
    chmod 0751 "$BASE/releases"
    chmod 0755 "$RELEASE"
    find "$RELEASE/app" -type d -exec chmod 0755 {} +
    find "$RELEASE/app" -type f -exec chmod 0644 {} +
    # app/__init__.py reads VERSION during import.  Expose only that one
    # root-level runtime marker in addition to the dependency manifests.
    chmod 0644 "$RELEASE/VERSION" "$RELEASE/requirements.txt" "$RELEASE/requirements.lock"
    runuser -u finance-radar-web -- test -r "$RELEASE/VERSION" || return 1
    # Streamlit probes $PWD/.streamlit/secrets.toml even when it is absent.
    # Permit its isolated public account to traverse the public configuration,
    # but fail closed if a prepared archive contains a Streamlit secret file.
    streamlit_dir="$RELEASE/.streamlit"
    if [ -e "$streamlit_dir" ] || [ -L "$streamlit_dir" ]; then
        [ -d "$streamlit_dir" ] && [ ! -L "$streamlit_dir" ] || return 1
        [ ! -e "$streamlit_dir/secrets.toml" ] && [ ! -L "$streamlit_dir/secrets.toml" ] || {
            printf 'refusing a prepared restore that contains Streamlit secrets: %s\n' "$streamlit_dir/secrets.toml" >&2
            return 1
        }
        [ -f "$streamlit_dir/config.toml" ] && [ ! -L "$streamlit_dir/config.toml" ] || return 1
        streamlit_unexpected="$(find "$streamlit_dir" -mindepth 1 -maxdepth 1 ! -name config.toml -print -quit)"
        [ -z "$streamlit_unexpected" ] || {
            printf 'refusing an unexpected Streamlit runtime file in prepared restore\n' >&2
            return 1
        }
        # Search plus one known public config file is the whole public surface.
        chmod 0711 "$streamlit_dir"
        chmod 0644 "$streamlit_dir/config.toml"
        runuser -u finance-radar-web -- test -r "$streamlit_dir/config.toml" || return 1
    fi
    find "$BASE/venv" -type d -exec chmod 0755 {} +
    find "$BASE/venv" -type f -exec chmod a+r {} +
    find "$BASE/venv" -type f -perm /111 -exec chmod a+rx {} +
}

assert_private_runtime_import_boundary() {
    runuser -u finance-radar -- bash -c '
        set -euo pipefail
        cd -- "$1"
        unset PYTHONPATH
        exec "$2" -B -c "import app; assert app.__file__"
    ' _ "$RELEASE" "$BASE/venv/bin/python"
}

assert_public_runtime_import_boundary() {
    runuser -u finance-radar-web -- bash -c '
        set -euo pipefail
        cd -- "$1"
        unset PYTHONPATH
        exec "$2" -B -c "import app; assert app.__file__"
    ' _ "$RELEASE" "$BASE/venv/bin/python"
}

if ! getent passwd finance-radar >/dev/null; then
    useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin finance-radar
fi
ensure_public_web_principal
mv "$PREPARED" "$BASE"
BASE_MOVED=1

python3 - "$BASE" <<'PY'
import json
import os
import pathlib
import re
import sys

base = pathlib.Path(sys.argv[1])
release_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
plan = json.loads((base / "SYMLINK_PLAN.json").read_text(encoding="utf-8"))
for item in plan:
    if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("target"), str):
        raise SystemExit("unsafe symlink plan entry")
    relative = pathlib.PurePosixPath(item["path"])
    target = pathlib.PurePosixPath(item["target"])
    if (
        len(relative.parts) != 3
        or relative.parts[0] != "releases"
        or not release_pattern.fullmatch(relative.parts[1])
        or relative.name not in {"data", "reports"}
    ):
        raise SystemExit(f"unsafe link path: {relative}")
    if target not in {
        pathlib.PurePosixPath("/opt/finance-radar/shared/data"),
        pathlib.PurePosixPath("/opt/finance-radar/shared/reports"),
    }:
        raise SystemExit(f"unsafe link target: {target}")
    path = base.joinpath(*relative.parts)
    if path.exists() or path.is_symlink():
        raise SystemExit(f"link path already exists: {path}")
    os.symlink(target.as_posix(), path)
PY

ln -s "$BASE/releases/$EXPECTED_RELEASE" "$BASE/current"
install -m 0640 -o root -g finance-radar \
    "$BASE/config/etc/finance-radar.env" /etc/finance-radar.env
if ! grep -q '^FINANCE_RADAR_REVIEWER_TOKEN=' /etc/finance-radar.env; then
    printf 'FINANCE_RADAR_REVIEWER_TOKEN=%s\n' "$(openssl rand -hex 32)" >> /etc/finance-radar.env
fi
if ! grep -q '^FINANCE_RADAR_OPERATOR_TOKEN=' /etc/finance-radar.env; then
    printf 'FINANCE_RADAR_OPERATOR_TOKEN=%s\n' "$(openssl rand -hex 32)" >> /etc/finance-radar.env
fi
# Reviewer identities are not carried in migration archives.  Restore leaves
# human-label submission fail-closed until distinct principals are provisioned
# again through the documented owner-authorized process.
install -m 0600 -o root -g root /dev/null \
    /etc/finance-radar-reviewer-principals.json
printf '%s\n' '[]' > /etc/finance-radar-reviewer-principals.json
if grep -q '^FINANCE_RADAR_WEB_URL=' /etc/finance-radar.env; then
    sed -i "s#^FINANCE_RADAR_WEB_URL=.*#FINANCE_RADAR_WEB_URL=$PUBLIC_WEB_URL#" /etc/finance-radar.env
else
    printf 'FINANCE_RADAR_WEB_URL=%s\n' "$PUBLIC_WEB_URL" >> /etc/finance-radar.env
fi
# Recreate rather than copy/filter the minimal public environment. This keeps
# every administrator, Telegram and provider secret out of the public process.
install -m 0600 -o finance-radar-web -g finance-radar-web /dev/null /etc/finance-radar-public.env
printf '%s\n' \
    'FINANCE_RADAR_API_URL=http://127.0.0.1:18000' \
    'FINANCE_RADAR_UI_ROLE=public' \
    'FINANCE_RADAR_SHOW_DEBUG=0' \
    > /etc/finance-radar-public.env

python3 -m venv "$BASE/venv"
"$BASE/venv/bin/python" -m pip install --upgrade pip
"$BASE/venv/bin/python" -m pip install --require-hashes -r "$BASE/current/requirements.lock"
chown -R finance-radar:finance-radar \
    "$BASE/releases" "$BASE/shared/data" "$BASE/shared/reports" "$BASE/config" "$BASE/venv"
prepare_backup_restore_tmpdir || {
    printf 'unable to prepare disk-backed backup restore scratch directory\n' >&2
    exit 6
}
grant_public_web_runtime_access || {
    printf 'unable to establish public Web runtime access boundary\n' >&2
    exit 6
}
assert_private_runtime_import_boundary || {
    printf 'private runtime cannot import restored application from its service working directory\n' >&2
    exit 6
}
assert_public_runtime_import_boundary || {
    printf 'public Web runtime cannot import restored application from its service working directory\n' >&2
    exit 6
}
if [ -d "$BASE/evidence-llm" ]; then
    chown -R finance-radar:finance-radar "$BASE/evidence-llm"
fi
# A restored host publishes only the off-host backup status.  Remove the
# retired static shell explicitly so an older prepared archive cannot leave a
# misleading second UI behind.  If this archive has no status document, remove
# a possibly stale one rather than presenting it as the restored host's state.
PUBLIC_STATUS_SOURCE="$BASE/var/www/finance-radar-terminal/offhost-status.json"
PUBLIC_STATUS_TARGET=/var/www/finance-radar-terminal/offhost-status.json
install -d -m 0755 -o root -g root /var/www/finance-radar-terminal
rm -f -- /var/www/finance-radar-terminal/index.html
if [ -f "$PUBLIC_STATUS_SOURCE" ]; then
    install -m 0644 -o root -g root "$PUBLIC_STATUS_SOURCE" "$PUBLIC_STATUS_TARGET"
else
    rm -f -- "$PUBLIC_STATUS_TARGET"
fi
install_versioned_unit() {
    local unit="$1"
    local versioned="$BASE/current/deployment/systemd/$unit"
    local archived="$BASE/config/etc/systemd/system/$unit"
    if [ -f "$versioned" ]; then
        install -m 0644 "$versioned" /etc/systemd/system/
    elif [ -f "$archived" ]; then
        # Compatibility fallback for a historic prepared archive.  A current
        # release always wins so a restored host receives current limits,
        # isolation and UI policy rather than stale copied unit files.
        install -m 0644 "$archived" /etc/systemd/system/
    elif [ "$unit" = "finance-radar.slice" ]; then
        write_legacy_slice_fallback
    else
        printf 'prepared restore is missing required systemd unit: %s\n' "$unit" >&2
        return 1
    fi
}

write_legacy_slice_fallback() {
    # Archives made before the slice was introduced contain no candidate file.
    # Keep their recovery path safe with the same aggregate guardrail as the
    # current unit; a versioned or archived candidate above always wins.
    cat > /etc/systemd/system/finance-radar.slice <<'EOF'
[Unit]
Description=Finance Radar aggregate resource boundary

[Slice]
MemoryAccounting=true
MemoryHigh=600M
MemoryMax=700M
MemorySwapMax=384M
TasksMax=256
EOF
    printf 'using safe legacy fallback for finance-radar.slice\n' >&2
}

for unit in \
    finance-radar.slice \
    finance-radar-api.service \
    finance-radar-overview-snapshot.service \
    finance-radar-overview-snapshot.timer \
    finance-radar-web.service \
    finance-radar-admin.service \
    finance-radar-reviewer.service \
    finance-radar-operator.service \
    finance-radar-worker.service \
    finance-radar-backup.service \
    finance-radar-backup.timer; do
    install_versioned_unit "$unit"
done
if [ -f "$BASE/current/deployment/systemd/finance-radar-evidence-llm.service" ] || \
   [ -f "$BASE/config/etc/systemd/system/finance-radar-evidence-llm.service" ]; then
    install_versioned_unit finance-radar-evidence-llm.service
else
    printf 'optional evidence LLM unit is absent from this prepared archive\n' >&2
fi
BACKUP_QUIESCE_WRAPPER="$BASE/current/deployment/systemd/run_backup_quiesced.sh"
[ -f "$BACKUP_QUIESCE_WRAPPER" ] || {
    printf 'prepared restore is missing the backup quiesce wrapper\n' >&2
    exit 6
}
install -D -m 0750 -o root -g root \
    "$BACKUP_QUIESCE_WRAPPER" /usr/local/libexec/finance-radar/run_backup_quiesced.sh

assert_public_web_identity_and_boundary() {
    local user group protect_proc proc_subset
    user="$(systemctl show finance-radar-web -p User --value)" || return 1
    group="$(systemctl show finance-radar-web -p Group --value)" || return 1
    protect_proc="$(systemctl show finance-radar-web -p ProtectProc --value)" || return 1
    proc_subset="$(systemctl show finance-radar-web -p ProcSubset --value)" || return 1
    [ "$user" = finance-radar-web ] && [ "$group" = finance-radar-web ] && \
        [ "$protect_proc" = invisible ] && [ "$proc_subset" = pid ] || return 1
    runuser -u finance-radar-web -- test -r /etc/finance-radar-public.env || return 1
    if runuser -u finance-radar-web -- test -r /etc/finance-radar.env || \
       runuser -u finance-radar-web -- test -r "$RELEASE/.env" || \
       runuser -u finance-radar-web -- test -r "$BASE/shared/data/finance_radar.sqlite3" || \
       runuser -u finance-radar-web -- test -r "$BASE/shared/reports"; then
        return 1
    fi
    runuser -u finance-radar-web -- test -r "$RELEASE/app/web/Home.py" || return 1
    runuser -u finance-radar-web -- test -r "$RELEASE/.streamlit/config.toml" || return 1
    runuser -u finance-radar-web -- test -x "$BASE/venv/bin/python" || return 1
    assert_public_runtime_import_boundary
}

# A recovered host can retain its optional Telegram override.  Refresh that
# override from the prepared release so it preserves delivery without reviving
# automatic formal verification.
if [ -f /etc/systemd/system/finance-radar-worker.service.d/telegram-send.conf ] && \
   [ -f "$BASE/current/deployment/systemd/finance-radar-worker-send.conf" ]; then
    install -d -m 0755 /etc/systemd/system/finance-radar-worker.service.d
    install -m 0644 "$BASE/current/deployment/systemd/finance-radar-worker-send.conf" \
        /etc/systemd/system/finance-radar-worker.service.d/telegram-send.conf
fi
systemctl daemon-reload
# This model is advisory-only.  A disaster restore must never silently start a
# 560-MiB workload beside the public UI and collector.  A deliberate operator
# can later use install_local_evidence_model.sh --activate after its resource
# gate, with the worker and backup stopped.
systemctl disable --now finance-radar-evidence-llm.service || true
if systemctl is-active --quiet finance-radar-evidence-llm.service || \
   systemctl is-enabled --quiet finance-radar-evidence-llm.service; then
    printf 'evidence LLM must remain stopped and disabled after recovery\n' >&2
    exit 6
fi
systemctl start finance-radar-overview-snapshot.service
systemctl enable finance-radar-overview-snapshot.service
systemctl enable --now finance-radar-api finance-radar-web finance-radar-worker finance-radar-backup.timer
systemctl enable --now finance-radar-overview-snapshot.timer

# A restored production ledger performs the same synchronous overview
# precomputation as an ordinary deployment. Match the installer's measured
# cold-start allowance so restore activation does not reject a healthy API.
for _ in $(seq 1 90); do
    curl -fsS http://127.0.0.1:18000/api/v1/health >/dev/null && break
    sleep 1
done
curl -fsS http://127.0.0.1:18000/api/v1/health >/dev/null
curl -fsS http://127.0.0.1:18000/api/v1/overview >/dev/null
curl -fsS http://127.0.0.1:18501/radar/_stcore/health >/dev/null
systemctl is-active --quiet finance-radar-api finance-radar-web finance-radar-worker finance-radar-backup.timer
systemctl is-active --quiet finance-radar-overview-snapshot.timer
test "$(systemctl show finance-radar-overview-snapshot.service -p Result --value)" = success
assert_public_web_identity_and_boundary || {
    printf 'public Web identity or private-path isolation is not effective after recovery\n' >&2
    exit 6
}

trap - ERR
printf 'activation=PASS\nrelease=%s\npublic_web=%s\nlocal_evidence_model=disabled_after_restore\nnginx_tls=pending\n' \
    "$EXPECTED_RELEASE" "$PUBLIC_WEB_URL"
