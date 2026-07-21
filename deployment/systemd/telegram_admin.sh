#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "telegram_admin.sh must run as root" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1091
source /etc/finance-radar.env
set +a

exec sudo --preserve-env=TELEGRAM_BOT_TOKEN,TELEGRAM_CHAT_ID,FINANCE_RADAR_WEB_URL \
  -u finance-radar \
  /opt/finance-radar/venv/bin/python \
  /opt/finance-radar/current/scripts/telegram_alert_outbox.py \
  --db /opt/finance-radar/shared/data/finance_radar.sqlite3 \
  "$@"
