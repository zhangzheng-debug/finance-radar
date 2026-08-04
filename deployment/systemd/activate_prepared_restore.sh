#!/usr/bin/env bash
set -euo pipefail
umask 027

PREPARED=${1:?prepared restore directory required}
EXPECTED_RELEASE=${2:?expected release required}
PUBLIC_WEB_URL=${3:?public Web URL required}
CONFIRM=${4:-}
BASE=/opt/finance-radar
FAILED_BASE="${BASE}.failed-$(date -u +%Y%m%dT%H%M%SZ)"

[ "$(id -u)" -eq 0 ] || { printf 'run as root\n' >&2; exit 2; }
[[ "$EXPECTED_RELEASE" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || {
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
[ ! -e "$BASE" ] || {
    printf 'refusing to overwrite existing %s; use a clean replacement VPS\n' "$BASE" >&2; exit 4;
}
compgen -G '/etc/systemd/system/finance-radar-*.service' >/dev/null && {
    printf 'refusing to overwrite existing Finance Radar service units\n' >&2; exit 4;
}
for command in python3 tar sha256sum systemctl curl; do
    command -v "$command" >/dev/null || { printf 'missing prerequisite: %s\n' "$command" >&2; exit 5; }
done

python3 - "$PREPARED" "$EXPECTED_RELEASE" <<'PY'
import json
import pathlib
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
    root / "shared" / "data" / "finance_radar.sqlite3",
    root / "shared" / "data" / "finance_radar_operations.sqlite3",
    root / "config" / "etc" / "finance-radar.env",
    root / "SYMLINK_PLAN.json",
]
missing = [str(path) for path in required if not path.is_file()]
if missing:
    raise SystemExit(f"prepared restore is incomplete: {missing}")
PY

rollback() {
    systemctl stop finance-radar-api finance-radar-web finance-radar-admin finance-radar-worker 2>/dev/null || true
    if [ -d "$BASE" ]; then
        mv "$BASE" "$FAILED_BASE" || true
    fi
    printf 'activation failed; staged files retained at %s\n' "$FAILED_BASE" >&2
}
trap rollback ERR

if ! getent passwd finance-radar >/dev/null; then
    useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin finance-radar
fi
mv "$PREPARED" "$BASE"

python3 - "$BASE" <<'PY'
import json
import os
import pathlib
import sys

base = pathlib.Path(sys.argv[1])
plan = json.loads((base / "SYMLINK_PLAN.json").read_text(encoding="utf-8"))
for item in plan:
    relative = pathlib.PurePosixPath(item["path"])
    target = pathlib.PurePosixPath(item["target"])
    if relative.parts[:1] != ("releases",) or relative.name not in {"data", "reports"}:
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
if grep -q '^FINANCE_RADAR_WEB_URL=' /etc/finance-radar.env; then
    sed -i "s#^FINANCE_RADAR_WEB_URL=.*#FINANCE_RADAR_WEB_URL=$PUBLIC_WEB_URL#" /etc/finance-radar.env
else
    printf 'FINANCE_RADAR_WEB_URL=%s\n' "$PUBLIC_WEB_URL" >> /etc/finance-radar.env
fi
# Recreate rather than copy/filter the minimal public environment. This keeps
# every administrator, Telegram and provider secret out of the public process.
install -m 0640 -o root -g finance-radar /dev/null /etc/finance-radar-public.env
printf '%s\n' \
    'FINANCE_RADAR_API_URL=http://127.0.0.1:18000' \
    'FINANCE_RADAR_UI_ROLE=public' \
    'FINANCE_RADAR_SHOW_DEBUG=0' \
    > /etc/finance-radar-public.env

python3 -m venv "$BASE/venv"
"$BASE/venv/bin/python" -m pip install --upgrade pip
"$BASE/venv/bin/python" -m pip install -r "$BASE/current/requirements.txt"
chown -R finance-radar:finance-radar "$BASE/releases" "$BASE/shared" "$BASE/config"
if [ -d "$BASE/evidence-llm" ]; then
    chown -R finance-radar:finance-radar "$BASE/evidence-llm"
fi
if [ -f "$BASE/var/www/finance-radar-terminal/index.html" ]; then
    install -d -m 0755 -o root -g root /var/www/finance-radar-terminal
    install -m 0644 -o root -g root \
        "$BASE/var/www/finance-radar-terminal/index.html" \
        /var/www/finance-radar-terminal/index.html
fi
install -m 0644 "$BASE/config/etc/systemd/system/finance-radar-api.service" /etc/systemd/system/
install -m 0644 "$BASE/config/etc/systemd/system/finance-radar-web.service" /etc/systemd/system/
if [ -f "$BASE/config/etc/systemd/system/finance-radar-admin.service" ]; then
    install -m 0644 "$BASE/config/etc/systemd/system/finance-radar-admin.service" \
        /etc/systemd/system/
elif [ -f "$BASE/current/deployment/systemd/finance-radar-admin.service" ]; then
    install -m 0644 "$BASE/current/deployment/systemd/finance-radar-admin.service" \
        /etc/systemd/system/
fi
install -m 0644 "$BASE/config/etc/systemd/system/finance-radar-worker.service" /etc/systemd/system/
# A recovered host can retain its optional Telegram override.  Refresh that
# override from the prepared release so it preserves delivery without reviving
# automatic formal verification.
if [ -f /etc/systemd/system/finance-radar-worker.service.d/telegram-send.conf ] && \
   [ -f "$BASE/current/deployment/systemd/finance-radar-worker-send.conf" ]; then
    install -d -m 0755 /etc/systemd/system/finance-radar-worker.service.d
    install -m 0644 "$BASE/current/deployment/systemd/finance-radar-worker-send.conf" \
        /etc/systemd/system/finance-radar-worker.service.d/telegram-send.conf
fi
install -m 0644 "$BASE/config/etc/systemd/system/finance-radar-backup.service" /etc/systemd/system/
install -m 0644 "$BASE/config/etc/systemd/system/finance-radar-backup.timer" /etc/systemd/system/
if [ -f "$BASE/config/etc/systemd/system/finance-radar-evidence-llm.service" ]; then
    install -m 0644 "$BASE/config/etc/systemd/system/finance-radar-evidence-llm.service" \
        /etc/systemd/system/
fi
systemctl daemon-reload
if [ -x "$BASE/evidence-llm/current/llama-server" ] && \
   [ -s "$BASE/evidence-llm/models/qwen2.5-0.5b-instruct-q4_k_m.gguf" ]; then
    systemctl enable --now finance-radar-evidence-llm.service
    for _ in $(seq 1 90); do
        curl -fsS http://127.0.0.1:18601/health >/dev/null 2>&1 && break
        sleep 1
    done
    curl -fsS http://127.0.0.1:18601/health >/dev/null
fi
systemctl enable --now finance-radar-api finance-radar-web finance-radar-worker finance-radar-backup.timer

for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:18000/api/v1/health >/dev/null && break
    sleep 1
done
curl -fsS http://127.0.0.1:18000/api/v1/health >/dev/null
curl -fsS http://127.0.0.1:18501/radar/_stcore/health >/dev/null
systemctl is-active --quiet finance-radar-api finance-radar-web finance-radar-worker finance-radar-backup.timer

trap - ERR
printf 'activation=PASS\nrelease=%s\npublic_web=%s\nnginx_tls=pending\n' \
    "$EXPECTED_RELEASE" "$PUBLIC_WEB_URL"
