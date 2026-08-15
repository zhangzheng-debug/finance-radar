#!/usr/bin/env bash
set -euo pipefail
umask 077

STAMP=${1:-$(date -u +%Y%m%dT%H%M%SZ)}
STAGE_ROOT=${FINANCE_RADAR_BACKUP_STAGE_ROOT:-/var/tmp}
STAGE="$STAGE_ROOT/finance-radar-migration-$STAMP"
ARCHIVE="$STAGE_ROOT/finance-radar-migration-$STAMP.tgz"
BASE=/opt/finance-radar

[ "$(id -u)" -eq 0 ] || { printf 'run as root\n' >&2; exit 2; }
[ -d "$STAGE_ROOT" ] || { printf 'stage root missing: %s\n' "$STAGE_ROOT" >&2; exit 2; }

[ ! -e "$STAGE" ] || { printf 'stage already exists: %s\n' "$STAGE" >&2; exit 2; }
[ ! -e "$ARCHIVE" ] || { printf 'archive already exists: %s\n' "$ARCHIVE" >&2; exit 2; }
install -d -m 0700 \
    "$STAGE/shared/data" \
    "$STAGE/shared/reports" \
    "$STAGE/config/etc/systemd/system" \
    "$STAGE/config/etc/nginx" \
    "$STAGE/var/www"

# A migration must be made from one already verified recovery bundle, rather
# than by taking a second, unbound mix of live database and filesystem copies.
# The daily backup service pauses the worker, captures both databases behind
# its cross-store barrier, inventories evidence/reports, and performs an
# isolated restore before publishing this bundle.  Re-verify the exact fresh
# child independently before using it as the migration source.
BACKUP_ROOT="$BASE/shared/data/operational_backups"
BACKUP_RECEIPT_VERIFIER="$BASE/current/deployment/systemd/verify_backup_receipt.py"
BACKUP_INVENTORY="$STAGE/config/PRE_MIGRATION_BACKUP_INVENTORY.json"
install -d -m 0750 -o finance-radar -g finance-radar "$BACKUP_ROOT"
[ -f "$BACKUP_RECEIPT_VERIFIER" ] && [ ! -L "$BACKUP_RECEIPT_VERIFIER" ] || {
    printf 'backup receipt verifier is unavailable: %s\n' "$BACKUP_RECEIPT_VERIFIER" >&2
    exit 3
}
"$BASE/venv/bin/python" "$BACKUP_RECEIPT_VERIFIER" inventory \
    --backup-root "$BACKUP_ROOT" \
    --output "$BACKUP_INVENTORY"
BACKUP_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
systemctl start finance-radar-backup.service
BACKUP_RECEIPT="$("$BASE/venv/bin/python" "$BACKUP_RECEIPT_VERIFIER" receipt \
    --backup-root "$BACKUP_ROOT" \
    --inventory "$BACKUP_INVENTORY" \
    --required-kind recovery_bundle \
    --started-at "$BACKUP_STARTED_AT" \
    --operations-db "$BASE/shared/data/finance_radar_operations.sqlite3" \
    --ledger-source "$BASE/shared/data/finance_radar.sqlite3")"
IFS=$'\t' read -r \
    MIGRATION_BUNDLE_ID MIGRATION_BUNDLE_KIND MIGRATION_BUNDLE_MANIFEST_SHA256 MIGRATION_BUNDLE_RELATIVE \
    <<< "$BACKUP_RECEIPT"
[ "$MIGRATION_BUNDLE_KIND" = recovery_bundle ] || {
    printf 'migration requires a full recovery bundle, got: %s\n' "$MIGRATION_BUNDLE_KIND" >&2
    exit 3
}
[[ "$MIGRATION_BUNDLE_ID" =~ ^finance_radar_[A-Za-z0-9_]+$ ]] && \
    [[ "$MIGRATION_BUNDLE_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]] && \
    [ "$MIGRATION_BUNDLE_RELATIVE" = "$MIGRATION_BUNDLE_ID" ] || {
    printf 'migration recovery bundle receipt is malformed\n' >&2
    exit 3
}
MIGRATION_SOURCE_BUNDLE="$BACKUP_ROOT/$MIGRATION_BUNDLE_RELATIVE"
[ -d "$MIGRATION_SOURCE_BUNDLE" ] && [ ! -L "$MIGRATION_SOURCE_BUNDLE" ] || {
    printf 'migration recovery bundle disappeared after verification\n' >&2
    exit 3
}

# Preserve every application release for rollback, but omit the reproducible venv.
cp -a "$BASE/releases" "$STAGE/"
readlink -f "$BASE/current" > "$STAGE/CURRENT_RELEASE.txt"
MODEL_DIR="$BASE/evidence-llm"
MODEL_FILE="$MODEL_DIR/models/qwen2.5-0.5b-instruct-q4_k_m.gguf"
MODEL_SERVER="$MODEL_DIR/current/llama-server"
MODEL_INSTALLED=false
MODEL_ARCHIVED=false
if [ -x "$MODEL_SERVER" ] && [ -s "$MODEL_FILE" ]; then
    MODEL_INSTALLED=true
    # Dereference symlinks and deliberately do not preserve hard links.  The
    # audit hashes every manifest path as a regular file, while tar otherwise
    # serializes duplicate llama.cpp inodes as hard-link members.
    cp -RL --preserve=mode,timestamps,ownership,xattr \
        "$MODEL_DIR" "$STAGE/"
    if find "$STAGE/evidence-llm" -type f -links +1 -print -quit | grep -q .; then
        printf 'evidence model copy still contains hard links\n' >&2
        exit 3
    fi
    MODEL_ARCHIVED=true
elif [ -e "$MODEL_DIR" ]; then
    # The evidence LLM is advisory-only.  Do not archive a partial runtime and
    # later mistake it for a deployable model; record its absence explicitly.
    printf 'optional local evidence model is absent or incomplete; omitting it from this archive\n' >&2
fi
# A service unit does not make the advisory GGUF a restore prerequisite.  The
# manifest protects this small capability declaration alongside all payload
# files, while audit/restore retain the historic unit-coupled rule only for
# archives created before this declaration existed.
python3 - "$STAGE/config/LOCAL_EVIDENCE_MODEL_CAPABILITY.json" \
    "$MODEL_INSTALLED" "$MODEL_ARCHIVED" <<'PY'
import json
from pathlib import Path
import sys

destination = Path(sys.argv[1])
installed = sys.argv[2] == "true"
archive_includes_model = sys.argv[3] == "true"
destination.write_text(
    json.dumps(
        {
            "schema_version": 1,
            "kind": "local_evidence_model",
            "installed": installed,
            "archive_includes_model": archive_includes_model,
            "restore_policy": "DISABLED_AFTER_RESTORE",
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
# Materialize the migration state only from the independently verified bundle.
# Its saved manifest is carried beside an exact source-to-migration mapping;
# the isolated migration audit rechecks every mapped file before accepting a
# restore.  This closes the post-backup-write window for both SQLite stores,
# evidence objects and runtime reports.
"$BASE/venv/bin/python" - \
    "$MIGRATION_SOURCE_BUNDLE" "$STAGE" "$MIGRATION_BUNDLE_ID" "$MIGRATION_BUNDLE_MANIFEST_SHA256" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys


bundle = Path(sys.argv[1])
stage = Path(sys.argv[2])
snapshot_id = sys.argv[3]
receipt_sha256 = sys.argv[4]
manifest_path = bundle / "manifest.json"
if bundle.is_symlink() or not bundle.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
    raise SystemExit("migration source recovery bundle is unavailable")
manifest_bytes = manifest_path.read_bytes()
if hashlib.sha256(manifest_bytes).hexdigest() != receipt_sha256:
    raise SystemExit("migration source manifest no longer matches its verified receipt")
try:
    manifest = json.loads(manifest_bytes)
except json.JSONDecodeError as exc:
    raise SystemExit("migration source manifest is invalid JSON") from exc
if not isinstance(manifest, dict) or manifest.get("format") != "finance-radar-recovery-bundle-v1":
    raise SystemExit("migration source manifest has an unexpected format")
if manifest.get("snapshot_id") != snapshot_id:
    raise SystemExit("migration source manifest snapshot identity changed")
files = manifest.get("files")
if not isinstance(files, list) or not files:
    raise SystemExit("migration source manifest has no payload files")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def mapped_path(relative: str) -> Path:
    raw = PurePosixPath(relative)
    if raw.is_absolute() or not raw.parts or any(part in ("", ".", "..") for part in raw.parts):
        raise SystemExit(f"unsafe recovery-bundle member: {relative!r}")
    if relative == "ledger.sqlite3":
        return stage / "shared" / "data" / "finance_radar.sqlite3"
    if relative == "operations.sqlite3":
        return stage / "shared" / "data" / "finance_radar_operations.sqlite3"
    if raw.parts[0] == "evidence" and len(raw.parts) > 1:
        return stage / "shared" / "data" / "evidence_objects" / Path(*raw.parts[1:])
    if raw.parts[0] == "reports" and len(raw.parts) > 1:
        return stage / "shared" / "reports" / Path(*raw.parts[1:])
    raise SystemExit(f"unmapped recovery-bundle member: {relative!r}")


mapping: list[dict[str, object]] = []
seen_sources: set[str] = set()
seen_targets: set[str] = set()
for entry in files:
    if not isinstance(entry, dict):
        raise SystemExit("migration source manifest has an invalid file record")
    relative = entry.get("path")
    expected_sha = entry.get("sha256")
    expected_bytes = entry.get("bytes")
    if (
        not isinstance(relative, str)
        or relative in seen_sources
        or not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or isinstance(expected_bytes, bool)
    ):
        raise SystemExit("migration source manifest has an invalid payload record")
    try:
        expected_size = int(expected_bytes)
    except (TypeError, ValueError) as exc:
        raise SystemExit("migration source manifest has an invalid payload size") from exc
    source = bundle / relative
    destination = mapped_path(relative)
    if source.is_symlink() or not source.is_file() or source.stat().st_size != expected_size:
        raise SystemExit(f"migration source payload is unavailable: {relative}")
    if digest(source) != expected_sha:
        raise SystemExit(f"migration source payload hash changed: {relative}")
    target_relative = destination.relative_to(stage).as_posix()
    if target_relative in seen_targets:
        raise SystemExit(f"migration source maps multiple files to {target_relative}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)
    if destination.is_symlink() or not destination.is_file() or destination.stat().st_size != expected_size:
        raise SystemExit(f"migration payload copy failed: {relative}")
    if digest(destination) != expected_sha:
        raise SystemExit(f"migration payload copy hash mismatch: {relative}")
    mapping.append(
        {
            "source_path": relative,
            "target_path": target_relative,
            "sha256": expected_sha,
            "bytes": expected_size,
        }
    )
    seen_sources.add(relative)
    seen_targets.add(target_relative)

required_sources = {"ledger.sqlite3", "operations.sqlite3"}
if not required_sources.issubset(seen_sources):
    raise SystemExit("migration source bundle is missing a database component")
source_manifest_copy = stage / "config" / "MIGRATION_RECOVERY_BUNDLE.manifest.json"
shutil.copy2(manifest_path, source_manifest_copy, follow_symlinks=False)
if digest(source_manifest_copy) != receipt_sha256:
    raise SystemExit("migration source manifest copy hash mismatch")
receipt = {
    "schema_version": 1,
    "snapshot_id": snapshot_id,
    "source_manifest_sha256": receipt_sha256,
    "source_manifest_path": "config/MIGRATION_RECOVERY_BUNDLE.manifest.json",
    "mapping": mapping,
    "consistency": "verified_full_recovery_bundle",
}
receipt_path = stage / "config" / "MIGRATION_RECOVERY_BUNDLE.json"
receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(receipt_path, stat.S_IRUSR | stat.S_IWUSR)
PY
# The former static terminal is retired.  The public edge only needs this
# generated status document; archiving the directory could revive index.html
# as an accidental second UI on a restored host.
PUBLIC_STATUS_SOURCE=/var/www/finance-radar-terminal/offhost-status.json
PUBLIC_STATUS_STAGE="$STAGE/var/www/finance-radar-terminal/offhost-status.json"
if [ -f "$PUBLIC_STATUS_SOURCE" ]; then
    install -d -m 0755 "$STAGE/var/www/finance-radar-terminal"
    install -m 0644 "$PUBLIC_STATUS_SOURCE" "$PUBLIC_STATUS_STAGE"
fi

install -m 0600 /etc/finance-radar.env "$STAGE/config/etc/finance-radar.env"
if [ -f /etc/finance-radar-public.env ]; then
    install -m 0640 /etc/finance-radar-public.env \
        "$STAGE/config/etc/finance-radar-public.env"
fi
cp -a /etc/systemd/system/finance-radar-*.service \
    /etc/systemd/system/finance-radar-*.timer \
    /etc/systemd/system/finance-radar.slice \
    "$STAGE/config/etc/systemd/system/" 2>/dev/null || true

while IFS= read -r file; do
    cp --parents "$file" "$STAGE/config"
done < <(
    find /etc/nginx -maxdepth 3 -type f -print0 2>/dev/null \
        | xargs -0 grep -IlE \
            'finance-radar-api|/radar/' \
        || true
)

certbot certificates > "$STAGE/config/CERTIFICATE_STATUS.txt" 2>&1 || true
systemctl status finance-radar.slice finance-radar-api finance-radar-web finance-radar-admin \
    finance-radar-reviewer finance-radar-operator \
    finance-radar-worker finance-radar-backup.timer finance-radar-evidence-llm.service --no-pager \
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

tar -czf "$ARCHIVE" -C "$STAGE_ROOT" "finance-radar-migration-$STAMP"
chmod 0600 "$ARCHIVE"
sha256sum "$ARCHIVE"
stat -c 'bytes=%s' "$ARCHIVE"
printf 'stage=%s\narchive=%s\n' "$STAGE" "$ARCHIVE"
