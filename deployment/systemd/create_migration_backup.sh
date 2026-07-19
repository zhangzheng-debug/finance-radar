#!/usr/bin/env bash
set -euo pipefail
umask 077

STAMP=${1:-$(date -u +%Y%m%dT%H%M%SZ)}
STAGE="/tmp/finance-radar-migration-$STAMP"
ARCHIVE="/tmp/finance-radar-migration-$STAMP.tgz"
BASE=/opt/finance-radar

[ ! -e "$STAGE" ] || { printf 'stage already exists: %s\n' "$STAGE" >&2; exit 2; }
[ ! -e "$ARCHIVE" ] || { printf 'archive already exists: %s\n' "$ARCHIVE" >&2; exit 2; }
install -d -m 0700 \
    "$STAGE/shared/data" \
    "$STAGE/shared/reports" \
    "$STAGE/config/etc/systemd/system" \
    "$STAGE/config/etc/nginx"

# First create the normal verified ledger backup and restore drill.
systemctl start finance-radar-backup.service

# Preserve every application release for rollback, but omit the reproducible venv.
cp -a "$BASE/releases" "$STAGE/"
readlink -f "$BASE/current" > "$STAGE/CURRENT_RELEASE.txt"
if [ -d "$BASE/evidence-llm" ]; then
    # Dereference symlinks and deliberately do not preserve hard links.  The
    # audit hashes every manifest path as a regular file, while tar otherwise
    # serializes duplicate llama.cpp inodes as hard-link members.
    cp -RL --preserve=mode,timestamps,ownership,xattr \
        "$BASE/evidence-llm" "$STAGE/"
    if find "$STAGE/evidence-llm" -type f -links +1 -print -quit | grep -q .; then
        printf 'evidence model copy still contains hard links\n' >&2
        exit 3
    fi
fi
# Copy non-live data first.  The root SQLite WAL/SHM files may appear or vanish
# between directory enumeration and copy, so the two live databases are
# deliberately excluded here and replaced with online snapshots below.
tar -C "$BASE/shared/data" \
    --exclude='./finance_radar.sqlite3' \
    --exclude='./finance_radar.sqlite3-*' \
    --exclude='./finance_radar_operations.sqlite3' \
    --exclude='./finance_radar_operations.sqlite3-*' \
    -cf - . \
    | tar -C "$STAGE/shared/data" -xf -
cp -a "$BASE/shared/reports/." "$STAGE/shared/reports/"

# Replace copied live databases with transactionally consistent SQLite snapshots.
"$BASE/venv/bin/python" - "$STAGE" <<'PY'
from pathlib import Path
import os
import sqlite3
import sys

stage = Path(sys.argv[1])
for name in ("finance_radar.sqlite3", "finance_radar_operations.sqlite3"):
    source = Path("/opt/finance-radar/shared/data") / name
    target = stage / "shared" / "data" / name
    temporary = target.with_suffix(target.suffix + ".snapshot")
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as src:
        with sqlite3.connect(temporary, timeout=30) as dst:
            src.backup(dst, pages=256)
    with sqlite3.connect(f"file:{temporary.as_posix()}?mode=ro", uri=True) as check:
        result = check.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"{name} quick_check={result}")
    os.replace(temporary, target)

for path in (stage / "shared" / "data").glob("*.sqlite3-*"):
    path.unlink()
for path in (stage / "shared" / "data").glob("*.snapshot-*"):
    path.unlink()
PY

install -m 0600 /etc/finance-radar.env "$STAGE/config/etc/finance-radar.env"
cp -a /etc/systemd/system/finance-radar-*.service \
    /etc/systemd/system/finance-radar-*.timer \
    "$STAGE/config/etc/systemd/system/" 2>/dev/null || true

while IFS= read -r file; do
    cp --parents "$file" "$STAGE/config"
done < <(
    find /etc/nginx -maxdepth 3 -type f -print0 2>/dev/null \
        | xargs -0 grep -IlE \
            'radar\.167-172-69-16\.sslip\.io|finance-radar-api|/radar/' \
        || true
)

certbot certificates > "$STAGE/config/CERTIFICATE_STATUS.txt" 2>&1 || true
systemctl status finance-radar-api finance-radar-web finance-radar-worker \
    finance-radar-backup.timer --no-pager \
    > "$STAGE/config/SERVICE_STATUS.txt" 2>&1 || true
"$BASE/venv/bin/python" --version > "$STAGE/config/PYTHON_VERSION.txt" 2>&1
"$BASE/venv/bin/pip" freeze > "$STAGE/config/PIP_FREEZE.txt"
du -ah "$BASE/shared" "$BASE/releases" ${BASE}/evidence-llm 2>/dev/null \
    > "$STAGE/config/SOURCE_INVENTORY.txt" || true

(
    cd "$STAGE"
    find . -type f ! -name MANIFEST.sha256 -print0 \
        | sort -z \
        | xargs -0 sha256sum \
        > MANIFEST.sha256
)

tar -czf "$ARCHIVE" -C /tmp "finance-radar-migration-$STAMP"
chmod 0600 "$ARCHIVE"
sha256sum "$ARCHIVE"
stat -c 'bytes=%s' "$ARCHIVE"
printf 'stage=%s\narchive=%s\n' "$STAGE" "$ARCHIVE"
