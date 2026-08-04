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

[[ "$RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]] || {
    printf 'invalid release id\n' >&2
    exit 2
}
[[ "$EXPECTED_SHA256" =~ ^[0-9A-Fa-f]{64}$ ]] || {
    printf 'invalid archive sha256\n' >&2
    exit 2
}
printf '%s  %s\n' "$EXPECTED_SHA256" "$ARCHIVE" | sha256sum -c -
[ -f "$SOURCE_ENV" ] || { printf 'source env not found: %s\n' "$SOURCE_ENV" >&2; exit 2; }
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
    RELEASE_RECORDS="$RELEASE/release-records"
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

ln -sfn "$RELEASE" "$BASE/current"
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

printf 'release=%s\n' "$RELEASE"
printf 'python=%s\n' "$BASE/venv/bin/python"
printf 'services=installed_not_started admin=manual_loopback_only\n'
