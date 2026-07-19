#!/usr/bin/env bash
set -euo pipefail
umask 027

ARCHIVE=${1:-/tmp/finance-radar-deploy.tgz}
RELEASE_ID=${2:?release id required}
EXPECTED_SHA256=${3:?archive sha256 required}
SOURCE_ENV=${4:-/tmp/finance-radar-source.env}
BASE=/opt/finance-radar
RELEASE="$BASE/releases/$RELEASE_ID"
SHARED="$BASE/shared"

printf '%s  %s\n' "$EXPECTED_SHA256" "$ARCHIVE" | sha256sum -c -
[ -f "$SOURCE_ENV" ] || { printf 'source env not found: %s\n' "$SOURCE_ENV" >&2; exit 2; }

if ! getent passwd finance-radar >/dev/null; then
    useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin finance-radar
fi

if [ -e "$RELEASE" ]; then
    printf 'release already exists: %s\n' "$RELEASE" >&2
    exit 2
fi
install -d -o finance-radar -g finance-radar "$BASE" "$BASE/releases" "$SHARED"
# Preserve the previous release intact so a failed upgrade can be rolled back.
# The first shared-data migration is a copy, never a move.
if [ ! -f "$SHARED/data/finance_radar.sqlite3" ] && [ -f "$BASE/current/data/finance_radar.sqlite3" ]; then
    install -d -o finance-radar -g finance-radar "$SHARED/data"
    cp -a "$BASE/current/data/." "$SHARED/data/"
fi
if [ ! -e "$SHARED/reports" ] && [ -d "$BASE/current/reports" ]; then
    install -d -o finance-radar -g finance-radar "$SHARED/reports"
    cp -a "$BASE/current/reports/." "$SHARED/reports/"
fi
install -d -o finance-radar -g finance-radar "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"
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
if [ -f "$BASE/current/.env" ]; then
    install -m 0640 -o finance-radar -g finance-radar "$BASE/current/.env" "$RELEASE/.env"
else
    install -m 0640 -o finance-radar -g finance-radar "$SOURCE_ENV" "$RELEASE/.env"
fi
chown -R finance-radar:finance-radar "$RELEASE" "$SHARED"

if [ ! -x "$BASE/venv/bin/python" ]; then
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
        'FINANCE_RADAR_WEB_URL=https://radar.167-172-69-16.sslip.io:8443/radar' \
        'FINANCE_RADAR_DEMO_MODE=RECENT_CAPTURE' \
        "FINANCE_RADAR_ADMIN_TOKEN=$ADMIN_TOKEN" \
        > /etc/finance-radar.env
else
    sed -i 's#/opt/finance-radar/current/data#/opt/finance-radar/shared/data#g' /etc/finance-radar.env
    if grep -q '^FINANCE_RADAR_WEB_URL=' /etc/finance-radar.env; then
        sed -i 's#^FINANCE_RADAR_WEB_URL=.*#FINANCE_RADAR_WEB_URL=https://radar.167-172-69-16.sslip.io:8443/radar#' /etc/finance-radar.env
    else
        printf '%s\n' 'FINANCE_RADAR_WEB_URL=https://radar.167-172-69-16.sslip.io:8443/radar' >> /etc/finance-radar.env
    fi
    if ! grep -q '^FINANCE_RADAR_EVIDENCE_OBJECT_DIR=' /etc/finance-radar.env; then
        printf '%s\n' 'FINANCE_RADAR_EVIDENCE_OBJECT_DIR=/opt/finance-radar/shared/data/evidence_objects' >> /etc/finance-radar.env
    fi
fi

ln -sfn "$RELEASE" "$BASE/current"
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-api.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-web.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-worker.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-backup.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-backup.timer" /etc/systemd/system/
if [ -f "$RELEASE/deployment/systemd/finance-radar-evidence-llm.service" ]; then
    install -m 0644 "$RELEASE/deployment/systemd/finance-radar-evidence-llm.service" \
        /etc/systemd/system/
fi
systemctl daemon-reload

printf 'release=%s\n' "$RELEASE"
printf 'python=%s\n' "$BASE/venv/bin/python"
printf 'services=installed_not_started\n'
