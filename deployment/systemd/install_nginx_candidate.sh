#!/usr/bin/env bash
set -euo pipefail

CANDIDATE=${1:-/tmp/alone.conf.finance-radar.candidate}
SOURCE=/etc/nginx/conf.d/alone.conf
BACKUP_DIR=/etc/nginx/finance-radar-backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="$BACKUP_DIR/alone.conf.$STAMP"

[ -f "$CANDIDATE" ] || { printf 'candidate not found: %s\n' "$CANDIDATE" >&2; exit 2; }
[ -f "$SOURCE" ] || { printf 'source not found: %s\n' "$SOURCE" >&2; exit 2; }
install -d -m 0700 "$BACKUP_DIR"
cp -a "$SOURCE" "$BACKUP"
install -m 0644 "$CANDIDATE" "$SOURCE"

if ! nginx -t; then
    cp -a "$BACKUP" "$SOURCE"
    nginx -t
    printf 'candidate validation failed; original restored from %s\n' "$BACKUP" >&2
    exit 3
fi

if ! systemctl reload nginx; then
    cp -a "$BACKUP" "$SOURCE"
    nginx -t
    systemctl reload nginx || true
    printf 'nginx reload failed; original restored from %s\n' "$BACKUP" >&2
    exit 4
fi

printf 'nginx candidate installed; backup=%s\n' "$BACKUP"
