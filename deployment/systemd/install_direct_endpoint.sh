#!/usr/bin/env bash
set -euo pipefail

CANDIDATE=${1:-/tmp/finance-radar-direct.candidate}
SOURCE=/etc/nginx/conf.d/finance-radar-direct.conf
HOOK_SOURCE=${2:-/tmp/certbot-reload-nginx.sh}
HOOK=/etc/letsencrypt/renewal-hooks/deploy/finance-radar-reload-nginx.sh
BACKUP_DIR=/etc/nginx/finance-radar-backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$BACKUP_DIR/finance-radar-direct.conf.$STAMP"
HAD_SOURCE=0

[ -f "$CANDIDATE" ] || { printf 'candidate not found: %s\n' "$CANDIDATE" >&2; exit 2; }
[ -f "$HOOK_SOURCE" ] || { printf 'renewal hook not found: %s\n' "$HOOK_SOURCE" >&2; exit 2; }
install -d -m 0700 "$BACKUP_DIR"
if [ -f "$SOURCE" ]; then
    HAD_SOURCE=1
    cp -a "$SOURCE" "$BACKUP"
fi
install -m 0644 "$CANDIDATE" "$SOURCE"

restore_source() {
    if [ "$HAD_SOURCE" -eq 1 ]; then
        cp -a "$BACKUP" "$SOURCE"
    else
        rm -f "$SOURCE"
    fi
}

if ! nginx -t; then
    restore_source
    nginx -t
    printf 'direct endpoint validation failed; prior state restored\n' >&2
    exit 3
fi

if ! systemctl reload nginx; then
    restore_source
    nginx -t
    systemctl reload nginx || true
    printf 'direct endpoint reload failed; prior state restored\n' >&2
    exit 4
fi

install -d -m 0755 "$(dirname "$HOOK")"
install -m 0755 "$HOOK_SOURCE" "$HOOK"
printf 'direct endpoint installed; backup=%s\n' "$BACKUP"
