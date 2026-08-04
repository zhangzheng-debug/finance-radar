#!/usr/bin/env bash
set -euo pipefail
umask 027

ARCHIVE=${1:-/tmp/finance-radar-deploy.tgz}
RELEASE_ID=${2:?release id required}
EXPECTED_SHA256=${3:?archive sha256 required}
SOURCE_ENV=${4:-/tmp/finance-radar-source.env}
RELEASE_MANIFEST=${5:-}
PUBLIC_WEB_URL=${6:-https://radar.18-208-34-152.sslip.io:8443/radar}
BASE=/opt/finance-radar
RELEASE="$BASE/releases/$RELEASE_ID"
SHARED="$BASE/shared"
RELEASE_RECORDS="$RELEASE/release-records"

[ "$(id -u)" -eq 0 ] || { printf 'run as root\n' >&2; exit 2; }
[[ "$RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]] || {
    printf 'invalid release id\n' >&2
    exit 2
}
[[ "$EXPECTED_SHA256" =~ ^[0-9A-Fa-f]{64}$ ]] || {
    printf 'invalid archive sha256\n' >&2
    exit 2
}
[[ "$PUBLIC_WEB_URL" =~ ^https://([A-Za-z0-9.-]+)(:([0-9]{1,5}))?/radar/?$ ]] || {
    printf 'public Web URL must be a simple HTTPS URL ending in /radar\n' >&2
    exit 2
}
PUBLIC_EDGE_HOST="${BASH_REMATCH[1]}"
PUBLIC_EDGE_PORT="${BASH_REMATCH[3]:-443}"
(( 10#$PUBLIC_EDGE_PORT >= 1 && 10#$PUBLIC_EDGE_PORT <= 65535 )) || {
    printf 'public Web URL port is invalid\n' >&2
    exit 2
}
PUBLIC_EDGE_ORIGIN="https://$PUBLIC_EDGE_HOST"
if [ "$PUBLIC_EDGE_PORT" != 443 ]; then
    PUBLIC_EDGE_ORIGIN+=":$PUBLIC_EDGE_PORT"
fi
PUBLIC_WEB_BASE="${PUBLIC_WEB_URL%/}"
printf '%s  %s\n' "$EXPECTED_SHA256" "$ARCHIVE" | sha256sum -c -
[ -f "$SOURCE_ENV" ] || { printf 'source env not found: %s\n' "$SOURCE_ENV" >&2; exit 2; }
for required_command in curl nginx systemctl; do
    command -v "$required_command" >/dev/null || {
        printf 'missing prerequisite: %s\n' "$required_command" >&2
        exit 2
    }
done
if [ -n "$RELEASE_MANIFEST" ]; then
    [ -f "$RELEASE_MANIFEST" ] || {
        printf 'release manifest not found\n' >&2
        exit 2
    }
fi

# Reject traversal, links, devices, sensitive filenames and archive bombs
# before GNU tar is allowed to write anything. This preflight is independent of
# the optional release manifest and therefore protects first-time installs too.
python3 - "$ARCHIVE" <<'PY'
from pathlib import PurePosixPath
import sys
import tarfile

archive_path = sys.argv[1]
seen = set()
members = 0
unpacked_bytes = 0
with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive:
        members += 1
        if members > 100_000:
            raise SystemExit("archive preflight failed: too many members")
        raw_name = member.name.replace("\\", "/")
        if member.isdir() and raw_name in {".", "./"}:
            continue
        path = PurePosixPath(raw_name)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
        ):
            raise SystemExit("archive preflight failed: unsafe member path")
        normalized = path.as_posix()
        if normalized in seen:
            raise SystemExit("archive preflight failed: duplicate member path")
        seen.add(normalized)
        lowered = tuple(part.lower() for part in path.parts)
        base = lowered[-1]
        sensitive_parent = any(
            part in {".git", "server_migration_backup", "secrets", "credentials"}
            for part in lowered
        )
        sensitive_base = (
            (
                (base == ".env" or base.startswith(".env."))
                and base != ".env.example"
            )
            or base in {
                "credentials.json", "secrets.json", "id_rsa", "id_ed25519"
            }
            or any(marker in base for marker in ("passphrase", "private_key", "privkey"))
            or base.endswith(
                (".key", ".pem", ".p12", ".pfx", ".session", ".session-journal")
            )
        )
        if sensitive_parent or sensitive_base:
            raise SystemExit("archive preflight failed: sensitive member path")
        if not (member.isfile() or member.isdir()):
            raise SystemExit("archive preflight failed: links and special members are forbidden")
        if member.mode & 0o6000:
            raise SystemExit("archive preflight failed: setuid/setgid member")
        if member.isfile():
            if member.size > 1024 * 1024 * 1024:
                raise SystemExit("archive preflight failed: member too large")
            unpacked_bytes += member.size
            if unpacked_bytes > 4 * 1024 * 1024 * 1024:
                raise SystemExit("archive preflight failed: expanded archive too large")
print(f"archive_preflight=PASS members={members} unpacked_bytes={unpacked_bytes}")
PY

if ! getent passwd finance-radar >/dev/null; then
    useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin finance-radar
fi

if [ -e "$RELEASE" ]; then
    printf 'release already exists: %s\n' "$RELEASE" >&2
    exit 2
fi
install -d -o finance-radar -g finance-radar "$BASE" "$BASE/releases" "$SHARED"
install -d -o finance-radar -g finance-radar "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"

# Optional, backward-compatible release gate. It verifies the explicit release
# id, manifest sidecar, archive hash/member safety and every critical file
# before shared data, the current symlink or any service unit is changed.
if [ -n "$RELEASE_MANIFEST" ]; then
    install -d -m 0750 -o finance-radar -g finance-radar "$RELEASE_RECORDS"
    python3 "$RELEASE/scripts/release_audit.py" verify \
        --manifest "$RELEASE_MANIFEST" \
        --root "$RELEASE" \
        --artifact "$ARCHIVE" \
        --expected-release-id "$RELEASE_ID" \
        --require-ready \
        --require-sidecar \
        --require-artifact \
        --report-dir "$RELEASE_RECORDS"
    install -m 0644 -o finance-radar -g finance-radar \
        "$RELEASE_MANIFEST" "$RELEASE_RECORDS/RELEASE_MANIFEST.json"
    MANIFEST_SIDECAR="$(dirname "$RELEASE_MANIFEST")/$RELEASE_ID.release-records.SHA256"
    install -m 0644 -o finance-radar -g finance-radar \
        "$MANIFEST_SIDECAR" "$RELEASE_RECORDS/RELEASE_RECORDS.SHA256"
    printf 'release_manifest=verified\n'
else
    printf 'release_manifest=not_supplied\n'
fi

# Preserve the previous release intact so a failed upgrade can be rolled back.
# The first shared-data migration is a copy, never a move, and runs only after
# the optional candidate release gate has succeeded.
PREVIOUS_RELEASE=""
if [ -e "$BASE/current" ] || [ -L "$BASE/current" ]; then
    PREVIOUS_RELEASE="$(readlink -f -- "$BASE/current")"
    [[ "$PREVIOUS_RELEASE" == "$BASE/releases/"* ]] && [ -d "$PREVIOUS_RELEASE" ] || {
        printf 'current release is not a complete release directory: %s\n' "$PREVIOUS_RELEASE" >&2
        exit 3
    }
fi
if [ ! -f "$SHARED/data/finance_radar.sqlite3" ] && [ -f "$BASE/current/data/finance_radar.sqlite3" ]; then
    install -d -o finance-radar -g finance-radar "$SHARED/data"
    cp -a "$BASE/current/data/." "$SHARED/data/"
fi
if [ ! -e "$SHARED/reports" ] && [ -d "$BASE/current/reports" ]; then
    install -d -o finance-radar -g finance-radar "$SHARED/reports"
    cp -a "$BASE/current/reports/." "$SHARED/reports/"
fi
if [ ! -e "$SHARED/data" ]; then
    mv "$RELEASE/data" "$SHARED/data"
else
    rm -rf "$RELEASE/data"
fi
if [ ! -e "$SHARED/reports" ]; then
    install -d -o finance-radar -g finance-radar "$SHARED/reports"
fi
ln -s "$SHARED/data" "$RELEASE/data"
ln -s "$SHARED/reports" "$RELEASE/reports"
[ -s "$SHARED/data/finance_radar.sqlite3" ] || {
    printf 'shared ledger missing after migration: %s\n' "$SHARED/data/finance_radar.sqlite3" >&2
    exit 3
}
# Source credentials (SEC identity, read-only market relays, Telegram dry-run)
# live in the release .env, while service topology/admin settings live in /etc.
# An upgrade must inherit the prior source credentials instead of replacing
# them with the narrower systemd environment file.
ROLLBACK_DIR=""
ROLLBACK_PRESENT=""
ROLLBACK_ABSENT=""
ROLLBACK_ENABLED_UNITS=""
ROLLBACK_ACTIVE_UNITS=""
VENV_SNAPSHOT=0
VENV_CREATED=0
CUTOVER_STARTED=0
SERVICES_TOUCHED=0
EDGE_TOUCHED=0
ROLLBACK_SERVICE_UNITS=(
    finance-radar-api
    finance-radar-web
    finance-radar-worker
    finance-radar-backup.timer
)
ROLLBACK_PATHS=(
    /etc/finance-radar.env
    /etc/finance-radar-public.env
    /etc/systemd/system/finance-radar-api.service
    /etc/systemd/system/finance-radar-web.service
    /etc/systemd/system/finance-radar-admin.service
    /etc/systemd/system/finance-radar-worker.service
    /etc/systemd/system/finance-radar-backup.service
    /etc/systemd/system/finance-radar-backup.timer
    /etc/systemd/system/finance-radar-evidence-llm.service
    /etc/systemd/system/finance-radar-worker.service.d/telegram-send.conf
    /etc/nginx/conf.d/finance-radar-direct.conf
    /etc/letsencrypt/renewal-hooks/deploy/finance-radar-reload-nginx.sh
)

backup_path() {
    local path="$1"
    local target="$ROLLBACK_DIR/files${path}"
    if [ -e "$path" ] || [ -L "$path" ]; then
        install -d -m 0700 "$(dirname "$target")" || return
        cp -a -- "$path" "$target" || return
        printf '%s\n' "$path" >> "$ROLLBACK_PRESENT" || return
    else
        printf '%s\n' "$path" >> "$ROLLBACK_ABSENT" || return
    fi
}

restore_path() {
    local path="$1"
    local source="$ROLLBACK_DIR/files${path}"
    if grep -Fqx -- "$path" "$ROLLBACK_PRESENT"; then
        install -d -m 0755 "$(dirname "$path")" || return
        rm -f -- "$path" || return
        cp -a -- "$source" "$path" || return
    elif grep -Fqx -- "$path" "$ROLLBACK_ABSENT"; then
        rm -f -- "$path" || return
    else
        printf 'rollback snapshot is incomplete for %s\n' "$path" >&2
        return 70
    fi
}

snapshot_rollback_state() {
    ROLLBACK_DIR="/var/tmp/finance-radar-install-${RELEASE_ID}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    [[ "$ROLLBACK_DIR" == /var/tmp/finance-radar-install-* ]] || return 70
    install -d -m 0700 "$ROLLBACK_DIR" || return
    ROLLBACK_PRESENT="$ROLLBACK_DIR/present.paths"
    ROLLBACK_ABSENT="$ROLLBACK_DIR/absent.paths"
    ROLLBACK_ENABLED_UNITS="$ROLLBACK_DIR/enabled.units"
    ROLLBACK_ACTIVE_UNITS="$ROLLBACK_DIR/active.units"
    install -m 0600 /dev/null "$ROLLBACK_PRESENT" || return
    install -m 0600 /dev/null "$ROLLBACK_ABSENT" || return
    install -m 0600 /dev/null "$ROLLBACK_ENABLED_UNITS" || return
    install -m 0600 /dev/null "$ROLLBACK_ACTIVE_UNITS" || return
    local path
    for path in "${ROLLBACK_PATHS[@]}"; do
        backup_path "$path" || return
    done
    if [ -e "$BASE/venv" ] || [ -L "$BASE/venv" ]; then
        cp -a -- "$BASE/venv" "$ROLLBACK_DIR/venv" || return
        VENV_SNAPSHOT=1
    fi
    local unit
    for unit in "${ROLLBACK_SERVICE_UNITS[@]}"; do
        if systemctl is-enabled --quiet "$unit"; then
            printf '%s\n' "$unit" >> "$ROLLBACK_ENABLED_UNITS" || return
        fi
        if systemctl is-active --quiet "$unit"; then
            printf '%s\n' "$unit" >> "$ROLLBACK_ACTIVE_UNITS" || return
        fi
    done
}

restore_service_runtime() {
    local unit
    for unit in "${ROLLBACK_SERVICE_UNITS[@]}"; do
        if grep -Fqx -- "$unit" "$ROLLBACK_ENABLED_UNITS"; then
            systemctl enable "$unit" || printf 'rollback_warning=enable_restore_failed unit=%s\n' "$unit" >&2
        else
            systemctl disable "$unit" || printf 'rollback_warning=disable_restore_failed unit=%s\n' "$unit" >&2
        fi
    done
    for unit in "${ROLLBACK_SERVICE_UNITS[@]}"; do
        if grep -Fqx -- "$unit" "$ROLLBACK_ACTIVE_UNITS"; then
            systemctl start "$unit" || printf 'rollback_warning=active_restore_failed unit=%s\n' "$unit" >&2
        else
            systemctl stop "$unit" || printf 'rollback_warning=inactive_restore_failed unit=%s\n' "$unit" >&2
        fi
    done
}

rollback() {
    local status=${1:-1}
    local path
    trap - ERR
    set +e
    printf 'cutover_failed=1; restoring previous release and configuration\n' >&2
    if [ "$SERVICES_TOUCHED" -eq 1 ]; then
        systemctl stop finance-radar-worker finance-radar-api finance-radar-web finance-radar-backup.timer 2>/dev/null || true
    fi
    if [ "$CUTOVER_STARTED" -eq 1 ]; then
        if [ -n "$PREVIOUS_RELEASE" ]; then
            ln -sfn "$PREVIOUS_RELEASE" "$BASE/current" || true
        else
            rm -f -- "$BASE/current" || true
        fi
    fi
    if [ "$VENV_SNAPSHOT" -eq 1 ]; then
        if [ "$BASE" = /opt/finance-radar ]; then
            rm -rf -- "$BASE/venv" || true
            mv -- "$ROLLBACK_DIR/venv" "$BASE/venv" || true
        else
            printf 'refusing unexpected venv rollback base: %s\n' "$BASE" >&2
        fi
    elif [ "$VENV_CREATED" -eq 1 ] && [ "$BASE" = /opt/finance-radar ]; then
        rm -rf -- "$BASE/venv" || true
    fi
    for path in "${ROLLBACK_PATHS[@]}"; do
        restore_path "$path" || printf 'rollback_warning=configuration_restore_failed path=%s\n' "$path" >&2
    done
    systemctl daemon-reload || true
    if [ "$EDGE_TOUCHED" -eq 1 ]; then
        if nginx -t; then
            systemctl reload nginx || true
        else
            printf 'rollback_warning=nginx_config_validation_failed\n' >&2
        fi
    fi
    if [ "$SERVICES_TOUCHED" -eq 1 ]; then
        restore_service_runtime
    fi
    printf 'rollback=COMPLETE previous_release=%s retained_failed_release=%s\n' \
        "${PREVIOUS_RELEASE:-none}" "$RELEASE" >&2
    exit "$status"
}

abort_cutover() {
    local message="$1"
    local status=${2:-1}
    printf '%s\n' "$message" >&2
    rollback "$status"
}

snapshot_rollback_state || {
    snapshot_status=$?
    if [[ "$ROLLBACK_DIR" == /var/tmp/finance-radar-install-* ]]; then
        rm -rf -- "$ROLLBACK_DIR"
    fi
    exit "$snapshot_status"
}
trap 'rollback "$?"' ERR

if [ -f "$BASE/current/.env" ]; then
    install -m 0640 -o finance-radar -g finance-radar "$BASE/current/.env" "$RELEASE/.env"
else
    install -m 0640 -o finance-radar -g finance-radar "$SOURCE_ENV" "$RELEASE/.env"
fi
chown -R finance-radar:finance-radar "$RELEASE" "$SHARED"

DIRECT_ENDPOINT_CANDIDATE="$RELEASE/deployment/systemd/nginx-radar-direct.conf"
DIRECT_ENDPOINT_INSTALLER="$RELEASE/deployment/systemd/install_direct_endpoint.sh"
DIRECT_ENDPOINT_HOOK="$RELEASE/deployment/systemd/certbot-reload-nginx.sh"
for required_file in "$DIRECT_ENDPOINT_CANDIDATE" "$DIRECT_ENDPOINT_INSTALLER" "$DIRECT_ENDPOINT_HOOK"; do
    [ -f "$required_file" ] || abort_cutover "required edge deployment file missing: $required_file" 4
done
CANDIDATE_SERVER_NAME="$(awk '$1 == "server_name" { sub(/;$/, "", $2); print $2; exit }' "$DIRECT_ENDPOINT_CANDIDATE")"
[ "$CANDIDATE_SERVER_NAME" = "$PUBLIC_EDGE_HOST" ] || \
    abort_cutover "public Web host does not match the versioned Nginx candidate" 4
if ! grep -Eq "^[[:space:]]*listen[[:space:]]+$PUBLIC_EDGE_PORT[[:space:]]+ssl;" "$DIRECT_ENDPOINT_CANDIDATE"; then
    abort_cutover "public Web port does not match the versioned Nginx candidate" 4
fi

if [ ! -x "$BASE/venv/bin/python" ]; then
    VENV_CREATED=1
    python3 -m venv "$BASE/venv"
fi
"$BASE/venv/bin/python" -m pip install --upgrade pip
"$BASE/venv/bin/python" -m pip install -r "$RELEASE/requirements.txt"
# pip runs as root during installation. With the deployment umask, newly
# installed packages would otherwise be unreadable to the unprivileged service
# account and could silently force the model into its fallback path.
chown -R finance-radar:finance-radar "$BASE/venv"
runuser -u finance-radar -- "$BASE/venv/bin/python" -c \
    'import sklearn, sklearn.pipeline; assert sklearn.__version__ == "1.8.0"'

if [ ! -f /etc/finance-radar.env ]; then
    ADMIN_TOKEN=$(openssl rand -hex 32)
    install -m 0640 -o root -g finance-radar /dev/null /etc/finance-radar.env
    printf '%s\n' \
        'PYTHONUNBUFFERED=1' \
        'OMP_NUM_THREADS=1' \
        'FINANCE_RADAR_DB=/opt/finance-radar/shared/data/finance_radar.sqlite3' \
        'FINANCE_RADAR_OPS_DB=/opt/finance-radar/shared/data/finance_radar_operations.sqlite3' \
        'FINANCE_RADAR_EVIDENCE_OBJECT_DIR=/opt/finance-radar/shared/data/evidence_objects' \
        'FINANCE_RADAR_ARTIFACT_DIR=/opt/finance-radar/current/artifacts' \
        'FINANCE_RADAR_REPLAY_DIR=/opt/finance-radar/current/replay/cases' \
        'FINANCE_RADAR_API_URL=http://127.0.0.1:18000' \
        "FINANCE_RADAR_WEB_URL=$PUBLIC_WEB_URL" \
        'FINANCE_RADAR_DEMO_MODE=RECENT_CAPTURE' \
        "FINANCE_RADAR_ADMIN_TOKEN=$ADMIN_TOKEN" \
        > /etc/finance-radar.env
else
    sed -i 's#/opt/finance-radar/current/data#/opt/finance-radar/shared/data#g' /etc/finance-radar.env
    if grep -q '^FINANCE_RADAR_WEB_URL=' /etc/finance-radar.env; then
        sed -i "s#^FINANCE_RADAR_WEB_URL=.*#FINANCE_RADAR_WEB_URL=$PUBLIC_WEB_URL#" /etc/finance-radar.env
    else
        printf 'FINANCE_RADAR_WEB_URL=%s\n' "$PUBLIC_WEB_URL" >> /etc/finance-radar.env
    fi
    if ! grep -q '^FINANCE_RADAR_EVIDENCE_OBJECT_DIR=' /etc/finance-radar.env; then
        printf '%s\n' 'FINANCE_RADAR_EVIDENCE_OBJECT_DIR=/opt/finance-radar/shared/data/evidence_objects' >> /etc/finance-radar.env
    fi
fi
if grep -q '^FINANCE_RADAR_RELEASE_ID=' /etc/finance-radar.env; then
    sed -i "s#^FINANCE_RADAR_RELEASE_ID=.*#FINANCE_RADAR_RELEASE_ID=$RELEASE_ID#" \
        /etc/finance-radar.env
else
    printf 'FINANCE_RADAR_RELEASE_ID=%s\n' "$RELEASE_ID" >> /etc/finance-radar.env
fi

# The public Streamlit process receives a deliberately minimal environment.
# Never derive this file by copying or filtering /etc/finance-radar.env: that
# file contains the administrator token and may also contain provider secrets.
install -m 0640 -o root -g finance-radar /dev/null /etc/finance-radar-public.env
printf '%s\n' \
    'FINANCE_RADAR_API_URL=http://127.0.0.1:18000' \
    'FINANCE_RADAR_UI_ROLE=public' \
    'FINANCE_RADAR_SHOW_DEBUG=0' \
    > /etc/finance-radar-public.env

install -m 0644 "$RELEASE/deployment/systemd/finance-radar-api.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-web.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-admin.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-worker.service" /etc/systemd/system/
# Keep an operator-installed Telegram sender override, but refresh it from the
# release so it cannot re-enable autonomous formal light verification after the
# base worker was safely changed to --no-light-verify.
if [ -f /etc/systemd/system/finance-radar-worker.service.d/telegram-send.conf ]; then
    install -d -m 0755 /etc/systemd/system/finance-radar-worker.service.d
    install -m 0644 "$RELEASE/deployment/systemd/finance-radar-worker-send.conf" \
        /etc/systemd/system/finance-radar-worker.service.d/telegram-send.conf
fi
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-backup.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-backup.timer" /etc/systemd/system/
if [ -f "$RELEASE/deployment/systemd/finance-radar-evidence-llm.service" ]; then
    install -m 0644 "$RELEASE/deployment/systemd/finance-radar-evidence-llm.service" \
        /etc/systemd/system/
fi
systemctl daemon-reload

if systemctl is-active --quiet finance-radar-admin; then
    abort_cutover 'finance-radar-admin is active; stop the manual loopback session before cutover' 5
fi
if systemctl is-active --quiet finance-radar-backup.service; then
    abort_cutover 'finance-radar-backup.service is active; wait for the verified backup to finish before cutover' 5
fi

# The only point at which the running release changes. Everything before this
# line was validated against the candidate and snapshotted for automatic
# rollback. Keep the failed release on disk for forensic inspection.
# Stop the mutable collector before resolving `current` to a different release;
# API/Web remain available until their controlled restart below.
SERVICES_TOUCHED=1
systemctl stop finance-radar-worker
CUTOVER_STARTED=1
ln -sfn "$RELEASE" "$BASE/current"
systemctl enable finance-radar-api finance-radar-web finance-radar-worker finance-radar-backup.timer
systemctl restart finance-radar-api finance-radar-web finance-radar-worker
systemctl start finance-radar-backup.timer

wait_for_url() {
    local url="$1"
    local attempts=${2:-30}
    local _
    for _ in $(seq 1 "$attempts"); do
        if curl -fsS --max-time 5 "$url" >/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

assert_edge_status() {
    local path="$1"
    local expected="$2"
    local status
    if ! status=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        --max-time 20 --resolve "$PUBLIC_EDGE_HOST:$PUBLIC_EDGE_PORT:127.0.0.1" \
        "$PUBLIC_EDGE_ORIGIN$path"); then
        return 1
    fi
    [ "$status" = "$expected" ]
}

wait_for_url http://127.0.0.1:18000/api/v1/health || \
    abort_cutover 'API health check failed after activation' 6
wait_for_url http://127.0.0.1:18501/radar/_stcore/health || \
    abort_cutover 'public Web loopback health check failed after activation' 6
systemctl is-active --quiet finance-radar-api finance-radar-web finance-radar-worker finance-radar-backup.timer
if systemctl is-active --quiet finance-radar-admin; then
    abort_cutover 'finance-radar-admin became active during cutover' 6
fi

# Treat the public edge as part of the release rather than a follow-up manual
# step. The candidate installer validates Nginx, reloads it atomically and
# restores its own immediate backup on failure; our outer transaction also
# restores the previous Nginx file and renewal hook with the application.
EDGE_TOUCHED=1
bash "$DIRECT_ENDPOINT_INSTALLER" "$DIRECT_ENDPOINT_CANDIDATE" "$DIRECT_ENDPOINT_HOOK"
curl --fail --silent --show-error --location --max-time 20 \
    --resolve "$PUBLIC_EDGE_HOST:$PUBLIC_EDGE_PORT:127.0.0.1" \
    "$PUBLIC_WEB_BASE/" >/dev/null
for denied_path in \
    /finance-radar-api/ \
    /radar-admin/ \
    /radar/Event_Intelligence \
    '/radar/?_page=Operations_and_Model'; do
    assert_edge_status "$denied_path" 404 || \
        abort_cutover "public edge deny check failed for $denied_path" 6
done

install -d -m 0750 -o finance-radar -g finance-radar "$RELEASE_RECORDS"
install -m 0640 -o root -g finance-radar /dev/null "$RELEASE_RECORDS/ACTIVATION.txt"
printf 'release=%s\nprevious_release=%s\npublic_web=%s\nservices=active\nnginx_edge=PASS\n' \
    "$RELEASE_ID" "${PREVIOUS_RELEASE:-none}" "$PUBLIC_WEB_URL" \
    > "$RELEASE_RECORDS/ACTIVATION.txt"
trap - ERR
[[ "$ROLLBACK_DIR" == /var/tmp/finance-radar-install-* ]] || exit 70
rm -rf -- "$ROLLBACK_DIR"

printf 'activation=PASS\nrelease=%s\nprevious_release=%s\npublic_web=%s\nnginx_edge=PASS\n' \
    "$RELEASE" "${PREVIOUS_RELEASE:-none}" "$PUBLIC_WEB_URL"
