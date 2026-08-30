#!/usr/bin/env bash
set -euo pipefail
umask 027

ARCHIVE=${1:-/tmp/finance-radar-deploy.tgz}
RELEASE_ID=${2:?release id required}
EXPECTED_SHA256=${3:?archive sha256 required}
SOURCE_ENV=${4:-/tmp/finance-radar-source.env}
RELEASE_MANIFEST=${5:-}
PUBLIC_WEB_URL=${6:-${FINANCE_RADAR_PUBLIC_WEB_URL:-}}
DEPLOY_MODE=${FINANCE_RADAR_DEPLOY_MODE:-full}
BASE=/opt/finance-radar
RELEASE="$BASE/releases/$RELEASE_ID"
SHARED="$BASE/shared"
RELEASE_RECORDS="$RELEASE/release-records"
PUBLIC_STATUS_DIR=/var/www/finance-radar-terminal
PUBLIC_RELEASE_MARKER="$PUBLIC_STATUS_DIR/release.json"
LEGACY_STATIC_NGINX=/etc/nginx/conf.d/finance-radar-aws.conf
LEGACY_STATIC_INDEX="$PUBLIC_STATUS_DIR/index.html"
LEGACY_STATIC_RETIRE_DIR=/etc/nginx/finance-radar-retired
INSTALLER_SOURCE="$(readlink -f -- "${BASH_SOURCE[0]}")"
DEEPSEEK_CREDENTIAL_READY=0
MANIFEST_SIDECAR=""

[ "$(id -u)" -eq 0 ] || { printf 'run as root\n' >&2; exit 2; }
case "$DEPLOY_MODE" in
    full|code-only) ;;
    *)
        printf 'invalid FINANCE_RADAR_DEPLOY_MODE: %s (expected full or code-only)\n' \
            "$DEPLOY_MODE" >&2
        exit 2
        ;;
esac
if [ "$DEPLOY_MODE" = code-only ]; then
    ACTIVE_INSTALLER="$(readlink -f -- "$BASE/current/deployment/systemd/install_remote.sh" 2>/dev/null || true)"
    [ -n "$ACTIVE_INSTALLER" ] && [ "$INSTALLER_SOURCE" = "$ACTIVE_INSTALLER" ] || {
        printf 'code-only deployment must be launched with the active release installer\n' >&2
        exit 2
    }
fi
[[ "$RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]] || {
    printf 'invalid release id\n' >&2
    exit 2
}
[[ "$EXPECTED_SHA256" =~ ^[0-9A-Fa-f]{64}$ ]] || {
    printf 'invalid archive sha256\n' >&2
    exit 2
}
[ -n "$PUBLIC_WEB_URL" ] || {
    printf 'public Web URL is required as argument 6 or FINANCE_RADAR_PUBLIC_WEB_URL\n' >&2
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
for required_command in \
    awk basename curl dirname find getent nginx openssl python3 readlink runuser sha256sum stat systemctl systemd-run tar; do
    command -v "$required_command" >/dev/null || {
        printf 'missing prerequisite: %s\n' "$required_command" >&2
        exit 2
    }
done
if [ -n "$RELEASE_MANIFEST" ]; then
    EXPECTED_MANIFEST_NAME="$RELEASE_ID.release-manifest.json"
    [ "$(basename -- "$RELEASE_MANIFEST")" = "$EXPECTED_MANIFEST_NAME" ] || {
        printf 'release manifest must keep the exact basename: %s\n' \
            "$EXPECTED_MANIFEST_NAME" >&2
        exit 2
    }
    [ -f "$RELEASE_MANIFEST" ] && [ ! -L "$RELEASE_MANIFEST" ] || {
        printf 'release manifest must be a regular non-symlink file: %s\n' \
            "$RELEASE_MANIFEST" >&2
        exit 2
    }
    MANIFEST_SIDECAR="$(dirname -- "$RELEASE_MANIFEST")/$RELEASE_ID.release-records.SHA256"
    [ -f "$MANIFEST_SIDECAR" ] && [ ! -L "$MANIFEST_SIDECAR" ] || {
        printf 'release manifest sidecar must be staged beside the manifest as a regular non-symlink file: %s\n' \
            "$MANIFEST_SIDECAR" >&2
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
                "credentials.json", "secrets.json", "secrets.toml", "credentials.toml", "id_rsa", "id_ed25519"
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

ensure_public_web_principal() {
    if ! getent group finance-radar-web >/dev/null; then
        groupadd --system finance-radar-web
    fi
    if ! getent passwd finance-radar-web >/dev/null; then
        useradd --system --gid finance-radar-web --home-dir /nonexistent \
            --shell /usr/sbin/nologin finance-radar-web
    fi
}

validate_optional_deepseek_credential() {
    local credential=/etc/finance-radar/deepseek-api-key
    local resolved metadata owner_id group_id mode size

    # The interpretation units remain installed but disabled when no provider
    # credential has been provisioned.  Once the path exists, fail closed on
    # anything that systemd LoadCredential should not consume.
    if [ ! -e "$credential" ] && [ ! -L "$credential" ]; then
        DEEPSEEK_CREDENTIAL_READY=0
        printf 'deepseek_credential=not_provisioned\n'
        return 0
    fi
    [ -f "$credential" ] && [ ! -L "$credential" ] || {
        printf 'DeepSeek credential must be a regular non-symlink file: %s\n' \
            "$credential" >&2
        return 1
    }
    resolved="$(readlink -f -- "$credential")" || return 1
    [ "$resolved" = "$credential" ] || {
        printf 'DeepSeek credential path must not traverse a symlink: %s\n' \
            "$credential" >&2
        return 1
    }
    metadata="$(stat -c '%u:%g:%a:%s' -- "$credential")" || return 1
    IFS=: read -r owner_id group_id mode size <<< "$metadata"
    [ "$owner_id" = 0 ] && [ "$group_id" = 0 ] || {
        printf 'DeepSeek credential must be owned by root:root\n' >&2
        return 1
    }
    [ "$mode" = 600 ] || {
        printf 'DeepSeek credential mode must be exactly 0600\n' >&2
        return 1
    }
    [ "$size" -gt 0 ] && [ "$size" -le 512 ] && [ -r "$credential" ] || {
        printf 'DeepSeek credential must be non-empty, root-readable and at most 512 bytes\n' >&2
        return 1
    }
    DEEPSEEK_CREDENTIAL_READY=1
    printf 'deepseek_credential=validated_loadcredential_source\n'
}

grant_public_web_runtime_access() {
    local path streamlit_dir streamlit_unexpected
    for path in "$BASE" "$BASE/releases" "$RELEASE"; do
        [ -d "$path" ] && [ ! -L "$path" ] || {
            printf 'public Web runtime parent is not a regular directory: %s\n' "$path" >&2
            return 1
        }
    done
    [ -d "$RELEASE/app" ] && [ ! -L "$RELEASE/app" ] || {
        printf 'public Web application tree is unavailable\n' >&2
        return 1
    }
    [ -d "$BASE/venv" ] && [ ! -L "$BASE/venv" ] || {
        printf 'public Web virtual environment is unavailable\n' >&2
        return 1
    }
    # Permit the distinct public UID to traverse only the known code and venv
    # paths.  Do not relax /etc/finance-radar.env, release .env, shared data or
    # reports: those remain owned by the private runtime account.
    # Python's FileFinder must be able to enumerate the candidate release root
    # to discover the top-level app package.  Keep the shared releases parent
    # group-private, but make this immutable candidate root listable.  Files
    # outside the explicitly public trees remain 0640 root:finance-radar, so
    # the public UID can see their names but cannot read their contents.
    chmod 0711 "$BASE"
    chmod 0751 "$BASE/releases"
    chmod 0755 "$RELEASE"
    find "$RELEASE/app" -type d -exec chmod 0755 {} +
    find "$RELEASE/app" -type f -exec chmod 0644 {} +
    # app/__init__.py reads VERSION during import.  Treat that one marker as
    # public runtime metadata; all other root-level release files stay 0640.
    chmod 0644 "$RELEASE/VERSION" "$RELEASE/requirements.txt" "$RELEASE/requirements.lock"
    runuser -u finance-radar-web -- test -r "$RELEASE/VERSION" || {
        printf 'public release version marker is not readable by its isolated runtime account\n' >&2
        return 1
    }
    # Streamlit probes $PWD/.streamlit/secrets.toml even when it is absent.
    # The isolated public UID must traverse the project config directory for
    # that probe and for the public config.toml, but a release must never carry
    # a Streamlit secret file that would become readable through this path.
    streamlit_dir="$RELEASE/.streamlit"
    if [ -e "$streamlit_dir" ] || [ -L "$streamlit_dir" ]; then
        [ -d "$streamlit_dir" ] && [ ! -L "$streamlit_dir" ] || {
            printf 'public Streamlit config path is not a regular directory: %s\n' "$streamlit_dir" >&2
            return 1
        }
        [ ! -e "$streamlit_dir/secrets.toml" ] && [ ! -L "$streamlit_dir/secrets.toml" ] || {
            printf 'refusing a release that contains Streamlit secrets: %s\n' "$streamlit_dir/secrets.toml" >&2
            return 1
        }
        [ -f "$streamlit_dir/config.toml" ] && [ ! -L "$streamlit_dir/config.toml" ] || {
            printf 'public Streamlit config is missing or unsafe: %s\n' "$streamlit_dir/config.toml" >&2
            return 1
        }
        streamlit_unexpected="$(find "$streamlit_dir" -mindepth 1 -maxdepth 1 ! -name config.toml -print -quit)"
        [ -z "$streamlit_unexpected" ] || {
            printf 'refusing an unexpected Streamlit runtime file in release\n' >&2
            return 1
        }
        # Execute/search is enough for the public process; do not grant it a
        # directory listing or make arbitrary Streamlit files readable.
        chmod 0711 "$streamlit_dir"
        chmod 0644 "$streamlit_dir/config.toml"
        runuser -u finance-radar-web -- test -r "$streamlit_dir/config.toml" || {
            printf 'public Streamlit config is not readable by its isolated runtime account\n' >&2
            return 1
        }
    fi
    # The full installer may have created or changed the environment. A
    # code-only release proves its lock is byte-identical to the active one and
    # deliberately leaves the venv untouched, avoiding a recursive chmod over
    # thousands of files on every UI/API edit.
    if [ "$DEPLOY_MODE" = full ]; then
        find "$BASE/venv" -type d -exec chmod 0755 {} +
        find "$BASE/venv" -type f -exec chmod a+r {} +
        find "$BASE/venv" -type f -perm /111 -exec chmod a+rx {} +
    fi
}

assert_private_runtime_import_boundary() {
    # Mirror the API/worker WorkingDirectory import path without the explicit
    # PYTHONPATH used by the predeploy backup bridge.  This catches a release
    # root whose permissions permit traversal but prevent package discovery.
    runuser -u finance-radar -- bash -c '
        set -euo pipefail
        cd -- "$1"
        unset PYTHONPATH
        exec "$2" -B -c "import app; assert app.__file__"
    ' _ "$RELEASE" "$BASE/venv/bin/python"
}

assert_public_runtime_import_boundary() {
    # Exercise the same cwd-based import that the isolated Streamlit unit uses.
    # A process-only health probe does not catch an unreadable release root.
    runuser -u finance-radar-web -- bash -c '
        set -euo pipefail
        cd -- "$1"
        unset PYTHONPATH
        exec "$2" -B -c "import app; assert app.__file__"
    ' _ "$RELEASE" "$BASE/venv/bin/python"
}

verify_code_only_candidate_before_candidate_execution() {
    local trusted_validator
    [ -n "$PREVIOUS_RELEASE" ] && [ -d "$PREVIOUS_RELEASE" ] || {
        printf 'code-only deployment requires an existing active release\n' >&2
        return 1
    }
    trusted_validator="$PREVIOUS_RELEASE/deployment/systemd/verify_code_only_release.py"
    [ -f "$trusted_validator" ] && [ ! -L "$trusted_validator" ] || {
        printf 'active release has no trusted code-only verifier; bootstrap with a full deployment\n' >&2
        return 1
    }
    # This is deliberately the previous release's root-owned verifier.  It
    # proves the candidate's scripts and deployment tree are unchanged before
    # any Python file extracted from the candidate is executed as root.
    python3 "$trusted_validator" contract \
        --previous "$PREVIOUS_RELEASE" \
        --candidate "$RELEASE" || return 1
    [ -x "$BASE/venv/bin/python" ] || {
        printf 'code-only deployment requires the active virtual environment\n' >&2
        return 1
    }
}

if ! getent passwd finance-radar >/dev/null; then
    useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin finance-radar
fi
ensure_public_web_principal

if [ -e "$RELEASE" ]; then
    printf 'release already exists: %s\n' "$RELEASE" >&2
    exit 2
fi
# Candidate source is executed by a root-owned bridge wrapper before cutover.
# Keep the release tree outside the runtime account's write authority from the
# instant it is created; the runtime account receives only group read/search
# access for its unprivileged Python child.
install -d -m 0751 -o root -g root "$BASE"
install -d -m 0751 -o root -g finance-radar "$BASE/releases"
install -d -m 0750 -o finance-radar -g finance-radar "$SHARED"
install -d -m 0750 -o root -g finance-radar "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"
chown -R root:finance-radar "$RELEASE"
find "$RELEASE" -type d -exec chmod 0750 {} +
find "$RELEASE" -type f -exec chmod 0640 {} +
[ ! -e "$RELEASE_RECORDS" ] && [ ! -L "$RELEASE_RECORDS" ] || {
    printf 'candidate archive must not contain release-records\n' >&2
    exit 2
}

# Resolve and validate the active release before invoking any helper shipped by
# the candidate.  In full mode the candidate is allowed to change those
# helpers; in code-only mode it must first prove they are byte-identical to the
# already installed, trusted release.
PREVIOUS_RELEASE=""
PREVIOUS_ACCEPTED_RELEASE_ID=""
if [ -e "$BASE/current" ] || [ -L "$BASE/current" ]; then
    PREVIOUS_RELEASE="$(readlink -f -- "$BASE/current")"
    [[ "$PREVIOUS_RELEASE" == "$BASE/releases/"* ]] && [ -d "$PREVIOUS_RELEASE" ] || {
        printf 'current release is not a complete release directory: %s\n' "$PREVIOUS_RELEASE" >&2
        exit 3
    }
    PREVIOUS_ACCEPTED_RELEASE_ID="${PREVIOUS_RELEASE##*/}"
    [[ "$PREVIOUS_ACCEPTED_RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$ ]] || {
        printf 'current release has an invalid release id: %s\n' \
            "$PREVIOUS_ACCEPTED_RELEASE_ID" >&2
        exit 3
    }
fi
if [ "$DEPLOY_MODE" = code-only ]; then
    verify_code_only_candidate_before_candidate_execution || {
        printf 'code-only candidate is not eligible; rerun with FINANCE_RADAR_DEPLOY_MODE=full\n' >&2
        exit 4
    }
fi

# Optional, backward-compatible release gate. It verifies the explicit release
# id, manifest sidecar, archive hash/member safety and every critical file
# before shared data, the current symlink or any service unit is changed.
if [ -n "$RELEASE_MANIFEST" ]; then
    install -d -m 0750 -o root -g finance-radar "$RELEASE_RECORDS"
    python3 "$RELEASE/scripts/release_audit.py" verify \
        --manifest "$RELEASE_MANIFEST" \
        --root "$RELEASE" \
        --artifact "$ARCHIVE" \
        --expected-release-id "$RELEASE_ID" \
        --require-ready \
        --require-sidecar \
        --require-artifact \
        --report-dir "$RELEASE_RECORDS"
    install -m 0640 -o root -g finance-radar \
        "$RELEASE_MANIFEST" "$RELEASE_RECORDS/RELEASE_MANIFEST.json"
    install -m 0640 -o root -g finance-radar \
        "$MANIFEST_SIDECAR" "$RELEASE_RECORDS/RELEASE_RECORDS.SHA256"
    printf 'release_manifest=verified\n'
else
    printf 'release_manifest=not_supplied\n'
fi

# Verify the dependency input/lock binding from the extracted candidate itself.
# This catches platform line-ending conversions and stale metadata before any
# backup, shared-data migration, package installation or service cutover.
python3 "$RELEASE/scripts/verify_dependency_locks.py" || {
    printf 'candidate dependency lock verification failed\n' >&2
    exit 4
}
printf 'dependency_lock=verified\n'

# Validate an already provisioned provider secret before any recovery backup,
# package mutation or service cutover.  The secret is not copied into an
# environment file and its contents are never printed.
validate_optional_deepseek_credential || {
    printf 'DeepSeek LoadCredential source validation failed\n' >&2
    exit 4
}

# Mandatory recovery gates. A code/config rollback without a fresh recovery
# point is not a safe cutover.  The first release on an older host is allowed
# to bridge a legacy, verified ledger-only SQLite snapshot, but it must finish
# by producing and independently validating the new complete recovery bundle
# before activation is recorded as a success.
PREDEPLOY_BACKUP_ID=""
PREDEPLOY_BACKUP_RUN_ID=""
PREDEPLOY_BACKUP_KIND=""
PREDEPLOY_BACKUP_RECEIPT_SHA256=""
PREDEPLOY_BACKUP_PATH=""
POSTDEPLOY_BACKUP_ID=""
POSTDEPLOY_BACKUP_RUN_ID=""
POSTDEPLOY_BACKUP_MANIFEST_SHA256=""
POSTDEPLOY_BACKUP_PATH=""
RECOVERY_BASIS=""
POSTDEPLOY_FULL_BUNDLE_STATUS=""
CODE_ONLY_SCHEMA_RECEIPT_BEFORE=""
PREDEPLOY_HOLD_ROOT=""
PREDEPLOY_HOLD_PATH=""
RECOVERY_HOLD_PARENT=/var/lib/finance-radar
RECOVERY_HOLD_ROOT="$RECOVERY_HOLD_PARENT/recovery-holds"
WORKER_RESUME_INHIBIT_CREATED=0

# The receipt verifier is versioned as a standalone, standard-library helper so
# its legacy bridge and full-bundle checks remain independently testable.
BACKUP_RECEIPT_VERIFIER="$RELEASE/deployment/systemd/verify_backup_receipt.py"
[ -f "$BACKUP_RECEIPT_VERIFIER" ] || {
    printf 'backup receipt verifier is missing from candidate release\n' >&2
    exit 3
}
BACKUP_HOLD_TRANSFER="$RELEASE/deployment/systemd/transfer_verified_backup_hold.py"
[ -f "$BACKUP_HOLD_TRANSFER" ] && [ ! -L "$BACKUP_HOLD_TRANSFER" ] || {
    printf 'atomic backup custody helper is missing from candidate release\n' >&2
    exit 3
}
BACKUP_QUIESCE_WRAPPER_SOURCE="$RELEASE/deployment/systemd/run_backup_quiesced.sh"
BACKUP_QUIESCE_WRAPPER_TARGET=/usr/local/libexec/finance-radar/run_backup_quiesced.sh
WORKER_RESUME_INHIBIT=/run/finance-radar/worker-resume.inhibit
BACKUP_START_INHIBIT=/run/finance-radar/backup-start.inhibit
BACKUP_RESTORE_TMPDIR="$SHARED/data/.backup-restore-tmp"
BACKUP_START_INHIBIT_CREATED=0
BACKUP_SERVICE_OWNED=0

write_backup_inventory() {
    local backup_root="$1"
    local inventory_path="$2"
    python3 "$BACKUP_RECEIPT_VERIFIER" inventory \
        --backup-root "$backup_root" \
        --output "$inventory_path"
}

operations_database_path() {
    local configured=""
    if [ -r /etc/finance-radar.env ]; then
        configured="$(awk -F= '
            $1 == "FINANCE_RADAR_OPS_DB" {
                value = substr($0, index($0, "=") + 1)
                print value
                exit
            }
        ' /etc/finance-radar.env)"
    fi
    if [ -z "$configured" ]; then
        configured="$SHARED/data/finance_radar_operations.sqlite3"
    fi
    [[ "$configured" == /* && "$configured" != *$'\n'* ]] || {
        printf 'Finance Radar operations database path is invalid\n' >&2
        return 1
    }
    printf '%s\n' "$configured"
}

ledger_database_path() {
    local configured=""
    if [ -r /etc/finance-radar.env ]; then
        configured="$(awk -F= '
            $1 == "FINANCE_RADAR_DB" {
                value = substr($0, index($0, "=") + 1)
                print value
                exit
            }
        ' /etc/finance-radar.env)"
    fi
    if [ -z "$configured" ]; then
        configured="$SHARED/data/finance_radar.sqlite3"
    fi
    [[ "$configured" == /* && "$configured" != *$'\n'* ]] || {
        printf 'Finance Radar ledger database path is invalid\n' >&2
        return 1
    }
    printf '%s\n' "$configured"
}

capture_fresh_verified_backup_receipt() {
    local backup_root="$1"
    local started_at="$2"
    local inventory_path="$3"
    local required_kind="$4"
    local operations_db="$5"
    local ledger_source="$6"
    local python_bin receipt_tmpdir
    python_bin="$(command -v python3)" || return 1
    receipt_tmpdir="$(dirname "$inventory_path")"
    [ -d "$receipt_tmpdir" ] && [ ! -L "$receipt_tmpdir" ] || {
        printf 'receipt verifier temporary directory is unsafe: %s\n' "$receipt_tmpdir" >&2
        return 1
    }
    # The Python restore drill is a root-operated deployment guard, not a
    # long-lived Radar service.  It still belongs to the aggregate Radar slice
    # during the bridge so an old host cannot escape the host-wide memory
    # budget merely because this verifier is transient.  Its limit must not be
    # lower than the bounded backup unit that just produced the SQLite snapshot:
    # the independent drill makes another isolated SQLite restore and can have
    # the same working-set peak.  A smaller 160M/220M transient ceiling caused
    # sustained cgroup throttling on the production legacy snapshot instead of
    # a bounded, promptly verifiable recovery gate.
    systemd-run --quiet --wait --collect --pipe \
        --slice=finance-radar.slice \
        --setenv=TMPDIR="$receipt_tmpdir" \
        --property=MemoryAccounting=yes \
        --property=MemoryHigh=340M \
        --property=MemoryMax=460M \
        --property=MemorySwapMax=128M \
        --property=TasksMax=32 \
        "$python_bin" "$BACKUP_RECEIPT_VERIFIER" receipt \
        --backup-root "$backup_root" \
        --inventory "$inventory_path" \
        --required-kind "$required_kind" \
        --started-at "$started_at" \
        --operations-db "$operations_db" \
        --ledger-source "$ledger_source"
}

assert_bounded_backup_unit() {
    local properties
    properties="$(systemctl show finance-radar-backup.service \
        -p User -p Slice -p MemoryHigh -p MemoryMax -p MemorySwapMax -p TasksMax \
        -p ExecStart -p ReadWritePaths)" || return 1
    python3 - "$properties" "$BACKUP_QUIESCE_WRAPPER_TARGET" <<'PY'
from __future__ import annotations

import re
import sys

properties, wrapper_path = sys.argv[1:]
values = {}
for line in properties.splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        values[key] = value

def bytes_value(raw: str) -> int:
    if raw.isdigit():
        return int(raw)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMG])", raw)
    if not match:
        raise SystemExit(f"unexpected systemd memory value: {raw!r}")
    number, suffix = match.groups()
    return int(float(number) * {"K": 1024, "M": 1024**2, "G": 1024**3}[suffix])

expected = {
    "MemoryHigh": 340 * 1024**2,
    "MemoryMax": 460 * 1024**2,
    "MemorySwapMax": 128 * 1024**2,
    "TasksMax": 128,
}
if values.get("User") != "root":
    raise SystemExit(f"bridge backup must use the root quiesce wrapper: {values.get('User')!r}")
if values.get("Slice") != "finance-radar.slice":
    raise SystemExit(f"bridge backup is outside finance-radar.slice: {values.get('Slice')!r}")
exec_start = values.get("ExecStart", "")
if f"path={wrapper_path}" not in exec_start or f"argv[]={wrapper_path}" not in exec_start:
    raise SystemExit(f"bridge backup ExecStart does not use the root wrapper: {exec_start!r}")
if "/var/lib/finance-radar-health" not in values.get("ReadWritePaths", "").split():
    raise SystemExit("bridge backup cannot publish the root-owned health receipt")
for key, minimum in expected.items():
    raw = values.get(key, "")
    actual = int(raw) if key == "TasksMax" and raw.isdigit() else bytes_value(raw)
    if actual != minimum:
        raise SystemExit(f"bridge backup {key} is not the candidate bound: {raw!r}")
PY
}

install_backup_quiesce_wrapper() {
    [ -f "$BACKUP_QUIESCE_WRAPPER_SOURCE" ] || {
        printf 'candidate backup quiesce wrapper is missing\n' >&2
        return 1
    }
    install -D -m 0750 -o root -g root \
        "$BACKUP_QUIESCE_WRAPPER_SOURCE" "$BACKUP_QUIESCE_WRAPPER_TARGET" || return 1
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

prepare_candidate_predeploy_backup() {
    [ -d "$RELEASE" ] && [ ! -L "$RELEASE" ] || {
        printf 'candidate backup release is not a regular directory: %s\n' "$RELEASE" >&2
        return 1
    }
    [ -f "$BACKUP_QUIESCE_WRAPPER_SOURCE" ] && [ ! -L "$BACKUP_QUIESCE_WRAPPER_SOURCE" ] || {
        printf 'candidate backup quiesce wrapper is missing or unsafe\n' >&2
        return 1
    }
    [ -f "$RELEASE/app/ops/backup.py" ] && [ ! -L "$RELEASE/app/ops/backup.py" ] || {
        printf 'candidate backup module is missing or unsafe\n' >&2
        return 1
    }
    # The candidate is passed through a root-owned wrapper before cutover, so
    # it must stay immutable to the runtime account.  Group read/search is
    # enough for the unprivileged Python child; writable candidate code would
    # otherwise turn the bridge into a root-executed source boundary.
    if find "$RELEASE/app" "$RELEASE/deployment" -xdev -perm /022 -print -quit | grep -q .; then
        printf 'candidate backup source is writable by group or others\n' >&2
        return 1
    fi
    runuser -u finance-radar -- test -r "$RELEASE/app/ops/backup.py" || {
        printf 'candidate backup module is unreadable by finance-radar\n' >&2
        return 1
    }
    [ -x "$BASE/venv/bin/python" ] || {
        printf 'current Finance Radar Python is unavailable for candidate backup bridge\n' >&2
        return 1
    }
    # Import only: this proves the candidate backup path can run with the
    # active venv without instantiating OperationsRepository or migrating live
    # SQLite state before a verified recovery point exists.
    runuser -u finance-radar -- env "PYTHONPATH=$RELEASE" "$BASE/venv/bin/python" -c \
        'import app.ops.backup, app.storage.operations' || {
        printf 'candidate backup imports are incompatible with the active Python environment\n' >&2
        return 1
    }
}

run_predeploy_candidate_backup() {
    local transient_unit runtime_log
    prepare_candidate_predeploy_backup || return 1
    prepare_backup_restore_tmpdir || return 1
    transient_unit="finance-radar-predeploy-backup-${RELEASE_ID}-$$"
    runtime_log="$RELEASE_RECORDS/PREDEPLOY_BACKUP_RUNTIME.log"
    install -d -m 0750 -o root -g finance-radar "$RELEASE_RECORDS" || return 1
    # A transient service gives the candidate the exact same privilege,
    # sandbox and memory envelope as the normal backup job, but does not
    # replace the old service unit, wrapper, timer or current symlink.  It is
    # collected automatically whether it succeeds or fails, so no candidate
    # source-selection state can leak into rollback.
    if ! systemd-run --quiet --wait --collect --pipe \
        --unit="$transient_unit" \
        --slice=finance-radar.slice \
        --property=User=root \
        --property=Group=root \
        --property="WorkingDirectory=$RELEASE" \
        --property=EnvironmentFile=/etc/finance-radar.env \
        --property=MemoryAccounting=yes \
        --property=MemoryHigh=340M \
        --property=MemoryMax=460M \
        --property=MemorySwapMax=128M \
        --property=TasksMax=128 \
        --property=OOMPolicy=stop \
        --property=OOMScoreAdjust=700 \
        --property=TimeoutStartSec=90min \
        --property=TimeoutStopSec=2min \
        --property=UMask=0077 \
        --property=NoNewPrivileges=true \
        --property=PrivateTmp=true \
        --property=ProtectSystem=strict \
        --property=ProtectHome=true \
        --property="ReadWritePaths=$SHARED/data" \
        --setenv="TMPDIR=$BACKUP_RESTORE_TMPDIR" \
        --setenv="FINANCE_RADAR_BASE=$BASE" \
        --setenv="FINANCE_RADAR_WORKER_RESUME_INHIBIT=$WORKER_RESUME_INHIBIT" \
        --setenv="FINANCE_RADAR_BACKUP_SOURCE_ROOT=$RELEASE" \
        --setenv=FINANCE_RADAR_PREDEPLOY_BRIDGE=1 \
        bash "$BACKUP_QUIESCE_WRAPPER_SOURCE" >"$runtime_log" 2>&1; then
        chown root:finance-radar "$runtime_log" 2>/dev/null || true
        chmod 0640 "$runtime_log" 2>/dev/null || true
        printf 'candidate predeploy backup bridge failed; runtime_log=%s\n' "$runtime_log" >&2
        return 1
    fi
    chown root:finance-radar "$runtime_log" || return 1
    chmod 0640 "$runtime_log" || return 1
    printf 'predeploy_candidate_backup_runtime=PASS unit=%s source=%s high=340M max=460M\n' \
        "$transient_unit" "$RELEASE" >&2
}

require_predeploy_memory_headroom() {
    local available_kb minimum_kb=300000
    available_kb="$(awk '/^MemAvailable:/ { print $2; exit }' /proc/meminfo)"
    [[ "$available_kb" =~ ^[0-9]+$ ]] || {
        printf 'unable to determine MemAvailable before bridge backup\n' >&2
        return 1
    }
    if [ "$available_kb" -lt "$minimum_kb" ]; then
        printf 'insufficient MemAvailable for protected bridge backup: available_kb=%s required_kb=%s\n' \
            "$available_kb" "$minimum_kb" >&2
        return 1
    fi
    printf 'predeploy_memory_headroom=PASS available_kb=%s required_kb=%s\n' \
        "$available_kb" "$minimum_kb"
}

run_and_capture_fresh_backup() {
    local required_kind="$1"
    local runner="${2:-installed_service}"
    local backup_root="$SHARED/data/operational_backups"
    # SQLite's backup API needs a complete isolated copy while validating a
    # receipt.  /tmp is a small tmpfs on the production host, so keep this
    # root-owned scratch space on the verified root volume instead.
    local receipt_tmpdir="${FINANCE_RADAR_BACKUP_RECEIPT_TMPDIR:-/var/tmp/finance-radar-receipt}"
    local inventory_path started_at receipt operations_db ledger_source
    if systemctl is-active --quiet finance-radar-backup.service; then
        printf 'finance-radar-backup.service is active; wait for the existing verified backup to finish\n' >&2
        return 1
    fi
    operations_db="$(operations_database_path)" || return 1
    ledger_source="$(ledger_database_path)" || return 1
    if [ -e "$receipt_tmpdir" ] || [ -L "$receipt_tmpdir" ]; then
        [ -d "$receipt_tmpdir" ] && [ ! -L "$receipt_tmpdir" ] || {
            printf 'receipt verifier temporary directory is unsafe: %s\n' "$receipt_tmpdir" >&2
            return 1
        }
    else
        install -d -m 0700 -o root -g root "$receipt_tmpdir" || return 1
    fi
    inventory_path="$(mktemp "$receipt_tmpdir/finance-radar-predeploy-backup-inventory.XXXXXX")" || return 1
    if ! write_backup_inventory "$backup_root" "$inventory_path"; then
        rm -f -- "$inventory_path"
        printf 'unable to capture the pre-backup inventory\n' >&2
        return 1
    fi
    started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    case "$runner" in
        candidate_bridge)
            if ! run_predeploy_candidate_backup; then
                rm -f -- "$inventory_path"
                printf 'candidate predeploy backup bridge failed\n' >&2
                return 1
            fi
            ;;
        installed_service)
            BACKUP_SERVICE_OWNED=1
            if ! systemctl start finance-radar-backup.service; then
                BACKUP_SERVICE_OWNED=0
                rm -f -- "$inventory_path"
                printf 'backup service failed\n' >&2
                return 1
            fi
            if [ "$(systemctl show finance-radar-backup.service --property=Result --value)" != "success" ]; then
                BACKUP_SERVICE_OWNED=0
                rm -f -- "$inventory_path"
                printf 'backup service did not report success\n' >&2
                return 1
            fi
            if systemctl is-active --quiet finance-radar-backup.service; then
                BACKUP_SERVICE_OWNED=0
                rm -f -- "$inventory_path"
                printf 'backup service is still active after start returned\n' >&2
                return 1
            fi
            BACKUP_SERVICE_OWNED=0
            ;;
        *)
            rm -f -- "$inventory_path"
            printf 'unknown backup runner: %s\n' "$runner" >&2
            return 1
            ;;
    esac
    receipt="$(capture_fresh_verified_backup_receipt \
        "$backup_root" "$started_at" "$inventory_path" "$required_kind" "$operations_db" "$ledger_source")" || {
        rm -f -- "$inventory_path"
        printf 'backup receipt validation failed\n' >&2
        return 1
    }
    rm -f -- "$inventory_path"
    printf '%s\n' "$receipt"
}

require_predeploy_verified_backup() {
    if systemctl is-active --quiet finance-radar-admin; then
        printf 'finance-radar-admin is active; stop the manual loopback session before backup/cutover\n' >&2
        return 1
    fi
    local admin_unit_state
    admin_unit_state="$(systemctl show finance-radar-admin --property=UnitFileState --value 2>/dev/null || true)"
    case "$admin_unit_state" in
        enabled|enabled-runtime|linked|linked-runtime|alias|indirect|generated)
            printf 'finance-radar-admin is boot-enabled (%s); keep the privileged UI disabled before backup/cutover\n' \
                "$admin_unit_state" >&2
            return 1
            ;;
        disabled|static|masked|masked-runtime|not-found|"")
            ;;
        *)
            printf 'finance-radar-admin has an unrecognized unit-file state: %s\n' "$admin_unit_state" >&2
            return 1
            ;;
    esac
    local internal_unit internal_unit_state
    for internal_unit in finance-radar-reviewer finance-radar-operator; do
        if systemctl is-active --quiet "$internal_unit"; then
            printf '%s is active; stop the manual loopback session before backup/cutover\n' "$internal_unit" >&2
            return 1
        fi
        internal_unit_state="$(systemctl show "$internal_unit" --property=UnitFileState --value 2>/dev/null || true)"
        case "$internal_unit_state" in
            enabled|enabled-runtime|linked|linked-runtime|alias|indirect|generated)
                printf '%s is boot-enabled (%s); keep internal UIs disabled before backup/cutover\n' \
                    "$internal_unit" "$internal_unit_state" >&2
                return 1
                ;;
            disabled|static|masked|masked-runtime|not-found|"")
                ;;
            *)
                printf '%s has an unrecognized unit-file state: %s\n' \
                    "$internal_unit" "$internal_unit_state" >&2
                return 1
                ;;
        esac
    done
    local receipt
    # The candidate bridge protects a coordinated ledger + operations recovery
    # point.  A legacy ledger-only SQLite file is useful only for explicit
    # historical recovery, never as authorization to switch releases.
    receipt="$(run_and_capture_fresh_backup recovery_bundle candidate_bridge)" || {
        printf 'predeploy backup service or receipt validation failed\n' >&2
        return 1
    }
    IFS=$'\t' read -r \
        PREDEPLOY_BACKUP_ID PREDEPLOY_BACKUP_KIND PREDEPLOY_BACKUP_RECEIPT_SHA256 PREDEPLOY_BACKUP_PATH \
        <<< "$receipt"
    [[ "$PREDEPLOY_BACKUP_ID" =~ ^finance_radar_[A-Za-z0-9_]+$ ]] || {
        printf 'predeploy backup receipt has an invalid snapshot id\n' >&2
        return 1
    }
    [ "$PREDEPLOY_BACKUP_KIND" = recovery_bundle ] || {
        printf 'predeploy backup did not produce a complete recovery bundle\n' >&2
        return 1
    }
    [[ "$PREDEPLOY_BACKUP_RECEIPT_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
        printf 'predeploy backup receipt has an invalid hash\n' >&2
        return 1
    }
    PREDEPLOY_BACKUP_PATH="$SHARED/data/operational_backups/$PREDEPLOY_BACKUP_PATH"
    [ -f "$PREDEPLOY_BACKUP_PATH/manifest.json" ] || {
        printf 'predeploy recovery bundle is missing after validation\n' >&2
        return 1
    }
    printf 'predeploy_backup=VERIFIED format=%s snapshot_id=%s receipt_sha256=%s\n' \
        "$PREDEPLOY_BACKUP_KIND" "$PREDEPLOY_BACKUP_ID" "$PREDEPLOY_BACKUP_RECEIPT_SHA256"
}

require_postcutover_verified_backup() {
    local receipt postdeploy_kind
    receipt="$(run_and_capture_fresh_backup recovery_bundle)" || {
        printf 'postcutover full recovery backup service or receipt validation failed\n' >&2
        return 1
    }
    IFS=$'\t' read -r \
        POSTDEPLOY_BACKUP_ID postdeploy_kind POSTDEPLOY_BACKUP_MANIFEST_SHA256 POSTDEPLOY_BACKUP_PATH \
        <<< "$receipt"
    [ "$postdeploy_kind" = recovery_bundle ] || {
        printf 'postcutover backup did not produce a complete recovery bundle\n' >&2
        return 1
    }
    [[ "$POSTDEPLOY_BACKUP_ID" =~ ^finance_radar_[A-Za-z0-9_]+$ ]] || {
        printf 'postcutover backup receipt has an invalid snapshot id\n' >&2
        return 1
    }
    [[ "$POSTDEPLOY_BACKUP_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
        printf 'postcutover backup receipt has an invalid manifest hash\n' >&2
        return 1
    }
    POSTDEPLOY_BACKUP_PATH="$SHARED/data/operational_backups/$POSTDEPLOY_BACKUP_PATH"
    [ -f "$POSTDEPLOY_BACKUP_PATH/manifest.json" ] || {
        printf 'postcutover recovery bundle is missing after validation\n' >&2
        return 1
    }
    printf 'postcutover_backup=VERIFIED snapshot_id=%s manifest_sha256=%s\n' \
        "$POSTDEPLOY_BACKUP_ID" "$POSTDEPLOY_BACKUP_MANIFEST_SHA256"
}

# Code-only releases reuse the already verified daily recovery point instead of
# manufacturing two more multi-gigabyte bundles.  Candidate trust was already
# established above, before any candidate helper could execute as root.
require_recent_verified_backup_record() {
    local max_age_seconds receipt operations_db trusted_validator
    max_age_seconds=${FINANCE_RADAR_CODE_ONLY_BACKUP_MAX_AGE_SECONDS:-93600}
    [[ "$max_age_seconds" =~ ^[0-9]+$ ]] && \
        (( max_age_seconds >= 3600 && max_age_seconds <= 93600 )) || {
        printf 'invalid FINANCE_RADAR_CODE_ONLY_BACKUP_MAX_AGE_SECONDS\n' >&2
        return 1
    }
    trusted_validator="$PREVIOUS_RELEASE/deployment/systemd/verify_code_only_release.py"
    [ -f "$trusted_validator" ] || return 1
    operations_db="$(operations_database_path)" || return 1
    receipt="$(python3 "$trusted_validator" backup \
        --operations "$operations_db" \
        --backup-root "$SHARED/data/operational_backups" \
        --attestation "$RECOVERY_HOLD_PARENT/latest-verified-backup.json" \
        --max-age-seconds "$max_age_seconds")" || return 1
    IFS=$'\t' read -r PREDEPLOY_BACKUP_RUN_ID PREDEPLOY_BACKUP_ID \
        PREDEPLOY_BACKUP_RECEIPT_SHA256 CODE_ONLY_BACKUP_AGE_SECONDS \
        <<< "$(python3 - "$receipt" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
print(
    "\t".join(
        str(payload[key])
        for key in ("backup_id", "snapshot_id", "manifest_sha256", "age_seconds")
    )
)
PY
)" || return 1
    [[ "$PREDEPLOY_BACKUP_RUN_ID" =~ ^backup-[A-Za-z0-9_-]+$ ]] || return 1
    [[ "$PREDEPLOY_BACKUP_ID" =~ ^finance_radar_[A-Za-z0-9_-]+$ ]] || return 1
    [[ "$PREDEPLOY_BACKUP_RECEIPT_SHA256" =~ ^[0-9a-f]{64}$ ]] || return 1
    PREDEPLOY_BACKUP_KIND=recovery_bundle
    PREDEPLOY_BACKUP_PATH="$SHARED/data/operational_backups/$PREDEPLOY_BACKUP_ID"
    POSTDEPLOY_BACKUP_RUN_ID="$PREDEPLOY_BACKUP_RUN_ID"
    POSTDEPLOY_BACKUP_ID="$PREDEPLOY_BACKUP_ID"
    POSTDEPLOY_BACKUP_MANIFEST_SHA256="$PREDEPLOY_BACKUP_RECEIPT_SHA256"
    printf 'code_only_recovery_basis=PASS backup_id=%s snapshot_id=%s age_seconds=%s manifest_sha256=%s\n' \
        "$PREDEPLOY_BACKUP_RUN_ID" "$PREDEPLOY_BACKUP_ID" \
        "$CODE_ONLY_BACKUP_AGE_SECONDS" "$PREDEPLOY_BACKUP_RECEIPT_SHA256"
}

require_code_only_shared_state() {
    local ledger_db operations_db
    [ "$DEPLOY_MODE" = code-only ] || return 0
    [ -d "$SHARED/data" ] && [ ! -L "$SHARED/data" ] || {
        printf 'code-only deployment requires the existing shared data directory\n' >&2
        return 1
    }
    [ -d "$SHARED/reports" ] && [ ! -L "$SHARED/reports" ] || {
        printf 'code-only deployment requires the existing shared reports directory\n' >&2
        return 1
    }
    [ -L "$PREVIOUS_RELEASE/data" ] && \
        [ "$(readlink -f -- "$PREVIOUS_RELEASE/data")" = "$(readlink -f -- "$SHARED/data")" ] || {
        printf 'code-only deployment requires the active release shared-data link\n' >&2
        return 1
    }
    [ -L "$PREVIOUS_RELEASE/reports" ] && \
        [ "$(readlink -f -- "$PREVIOUS_RELEASE/reports")" = "$(readlink -f -- "$SHARED/reports")" ] || {
        printf 'code-only deployment requires the active release shared-reports link\n' >&2
        return 1
    }
    ledger_db="$(ledger_database_path)" || return 1
    operations_db="$(operations_database_path)" || return 1
    [ -s "$ledger_db" ] && [ ! -L "$ledger_db" ] || {
        printf 'code-only deployment requires the existing ledger database\n' >&2
        return 1
    }
    [ -s "$operations_db" ] && [ ! -L "$operations_db" ] || {
        printf 'code-only deployment requires the existing operations database\n' >&2
        return 1
    }
    [ ! -e "$RELEASE/data" ] && [ ! -L "$RELEASE/data" ] || {
        printf 'code-only candidate must not contain a data path\n' >&2
        return 1
    }
    # Tracked reports are normal archive input but never persistence. Discard
    # them before the common linking code attaches the verified shared root.
    if [ -e "$RELEASE/reports" ] || [ -L "$RELEASE/reports" ]; then
        [ -d "$RELEASE/reports" ] && [ ! -L "$RELEASE/reports" ] || {
            printf 'code-only candidate reports path is unsafe\n' >&2
            return 1
        }
        rm -rf -- "$RELEASE/reports" || return 1
    fi
    printf 'code_only_shared_state=PASS ledger=%s operations=%s\n' "$ledger_db" "$operations_db"
}

capture_code_only_schema_receipt() {
    local ledger_db operations_db trusted_validator receipt digest
    [ "$DEPLOY_MODE" = code-only ] || return 0
    trusted_validator="$PREVIOUS_RELEASE/deployment/systemd/verify_code_only_release.py"
    [ -f "$trusted_validator" ] && [ ! -L "$trusted_validator" ] || return 1
    ledger_db="$(ledger_database_path)" || return 1
    operations_db="$(operations_database_path)" || return 1
    receipt="$(python3 "$trusted_validator" schema \
        --ledger "$ledger_db" --operations "$operations_db")" || return 1
    digest="$(python3 - "$receipt" <<'PY'
import json
import re
import sys

payload = json.loads(sys.argv[1])
digest = str(payload.get("sha256") or "")
if payload.get("format") != "finance-radar-live-schema-receipt-v1" or not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit("invalid live schema receipt")
print(digest)
PY
)" || return 1
    if [ -z "$CODE_ONLY_SCHEMA_RECEIPT_BEFORE" ]; then
        CODE_ONLY_SCHEMA_RECEIPT_BEFORE="$digest"
        printf 'code_only_schema_before=PASS sha256=%s\n' "$digest"
    elif [ "$digest" != "$CODE_ONLY_SCHEMA_RECEIPT_BEFORE" ]; then
        printf 'code-only activation changed live database schema: before=%s after=%s\n' \
            "$CODE_ONLY_SCHEMA_RECEIPT_BEFORE" "$digest" >&2
        return 1
    else
        printf 'code_only_schema_after=PASS sha256=%s\n' "$digest"
    fi
}

# Preserve the previous release intact so a failed upgrade can be rolled back.
# The release was resolved before candidate helpers ran; shared-data migration
# below remains a copy/move into the managed shared roots, never an in-place
# rewrite of the active release.
# Preserve the previously resolved release intact so a failed upgrade can be
# rolled back.  The first shared-data migration is a copy, never a move.
require_code_only_shared_state || {
    printf 'code-only shared-state precondition failed; use full deployment\n' >&2
    exit 4
}
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
# A candidate archive carries a reports/ directory.  Leaving that directory in
# place would make `ln -s target existing-dir` create reports/reports instead
# of replacing it, silently serving release-local stale reports.  The archive
# preflight forbids links and this is a newly extracted candidate path, but
# retain an explicit shape check before removing it under root.
if [ -e "$RELEASE/reports" ] || [ -L "$RELEASE/reports" ]; then
    [ -d "$RELEASE/reports" ] && [ ! -L "$RELEASE/reports" ] || {
        printf 'candidate reports path is not a regular directory: %s\n' "$RELEASE/reports" >&2
        exit 3
    }
    rm -rf -- "$RELEASE/reports"
fi
ln -s "$SHARED/data" "$RELEASE/data"
ln -s -- "$SHARED/reports" "$RELEASE/reports"
[ -L "$RELEASE/reports" ] && [ "$(readlink -f -- "$RELEASE/reports")" = "$SHARED/reports" ] || {
    printf 'candidate reports path is not the shared reports link: %s\n' "$RELEASE/reports" >&2
    exit 3
}
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
ACTIVATION_RECORD_COMMITTED=0
ACTIVATION_PENDING=""
LEGACY_MANAGED_PROPERTY_DROPINS=(
    /etc/systemd/system.control/finance-radar-api.service.d/50-MemoryHigh.conf
    /etc/systemd/system.control/finance-radar-api.service.d/50-MemoryMax.conf
    /etc/systemd/system.control/finance-radar-api.service.d/50-MemorySwapMax.conf
    /etc/systemd/system.control/finance-radar-api.service.d/50-TasksMax.conf
    /etc/systemd/system.control/finance-radar-web.service.d/50-MemoryHigh.conf
    /etc/systemd/system.control/finance-radar-web.service.d/50-MemoryMax.conf
    /etc/systemd/system.control/finance-radar-web.service.d/50-MemorySwapMax.conf
    /etc/systemd/system.control/finance-radar-web.service.d/50-TasksMax.conf
    /etc/systemd/system.control/finance-radar-worker.service.d/50-MemoryHigh.conf
    /etc/systemd/system.control/finance-radar-worker.service.d/50-MemoryMax.conf
    /etc/systemd/system.control/finance-radar-worker.service.d/50-MemorySwapMax.conf
    /etc/systemd/system.control/finance-radar-worker.service.d/50-TasksMax.conf
    /etc/systemd/system.control/finance-radar-backup.service.d/50-MemoryHigh.conf
    /etc/systemd/system.control/finance-radar-backup.service.d/50-MemoryMax.conf
    /etc/systemd/system.control/finance-radar-backup.service.d/50-MemorySwapMax.conf
    /etc/systemd/system.control/finance-radar-backup.service.d/50-TasksMax.conf
    /etc/systemd/system.control/finance-radar-admin.service.d/50-MemoryHigh.conf
    /etc/systemd/system.control/finance-radar-admin.service.d/50-MemoryMax.conf
    /etc/systemd/system.control/finance-radar-admin.service.d/50-MemorySwapMax.conf
    /etc/systemd/system.control/finance-radar-admin.service.d/50-TasksMax.conf
)
LEGACY_BACKUP_RETENTION_DROPIN=/etc/systemd/system/finance-radar-backup.service.d/retention.conf
ROLLBACK_SERVICE_UNITS=(
    finance-radar-api
    finance-radar-overview-snapshot.service
    finance-radar-overview-snapshot.timer
    finance-radar-market.timer
    finance-radar-web
    finance-radar-worker
    # The backup oneshot is externally scheduled and may run for 90 minutes.
    # Its unit file is snapshotted in ROLLBACK_PATHS, but its active state is
    # never owned/restored by deployment; doing so could kill or duplicate an
    # unrelated daily recovery drill.
    finance-radar-backup.timer
    finance-radar-evidence-llm.service
    finance-radar-qwen-risk-model.service
    finance-radar-qwen-risk-worker.timer
    # The interpretation service is a bounded oneshot, not a persistent
    # session.  Deployment waits for an in-flight invocation to finish but
    # never kills or restarts it merely because it was active at snapshot time.
    # The timer below is the durable state that must be restored.
    finance-radar-capture-interpretation.timer
)
ROLLBACK_PATHS=(
    /etc/finance-radar.env
    /etc/finance-radar-public.env
    /etc/finance-radar-reviewer-principals.json
    /etc/systemd/system/finance-radar-api.service
    /etc/systemd/system/finance-radar-overview-snapshot.service
    /etc/systemd/system/finance-radar-overview-snapshot.timer
    /etc/systemd/system/finance-radar-market.service
    /etc/systemd/system/finance-radar-market.timer
    /etc/systemd/system/finance-radar-web.service
    /etc/systemd/system/finance-radar-admin.service
    /etc/systemd/system/finance-radar-reviewer.service
    /etc/systemd/system/finance-radar-operator.service
    /etc/systemd/system/finance-radar-worker.service
    /etc/systemd/system/finance-radar-backup.service
    /etc/systemd/system/finance-radar-backup.timer
    /etc/systemd/system/finance-radar-evidence-llm.service
    /etc/systemd/system/radarqwen.slice
    /etc/systemd/system/finance-radar-qwen-risk-model.service
    /etc/systemd/system/finance-radar-qwen-risk-worker.service
    /etc/systemd/system/finance-radar-qwen-risk-worker.timer
    /etc/systemd/system/finance-radar-capture-interpretation.service
    /etc/systemd/system/finance-radar-capture-interpretation.timer
    /etc/systemd/system/finance-radar.slice
    "$BACKUP_QUIESCE_WRAPPER_TARGET"
    /etc/systemd/system/finance-radar-worker.service.d/telegram-send.conf
    /etc/nginx/conf.d/finance-radar-direct.conf
    "$LEGACY_STATIC_NGINX"
    /etc/letsencrypt/renewal-hooks/deploy/finance-radar-reload-nginx.sh
    "$LEGACY_STATIC_INDEX"
    "$PUBLIC_RELEASE_MARKER"
    "$LEGACY_BACKUP_RETENTION_DROPIN"
    "${LEGACY_MANAGED_PROPERTY_DROPINS[@]}"
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
    if [ "$DEPLOY_MODE" = full ] && \
       { [ -e "$BASE/venv" ] || [ -L "$BASE/venv" ]; }; then
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
        if [ "$unit" = finance-radar-backup.service ] && \
           [ "$BACKUP_SERVICE_OWNED" -eq 0 ] && \
           systemctl is-active --quiet finance-radar-backup.service; then
            printf 'rollback_preserved_unowned_backup_service=1\n' >&2
            continue
        fi
        if grep -Fqx -- "$unit" "$ROLLBACK_ACTIVE_UNITS"; then
            systemctl start "$unit" || printf 'rollback_warning=active_restore_failed unit=%s\n' "$unit" >&2
        else
            systemctl stop "$unit" || printf 'rollback_warning=inactive_restore_failed unit=%s\n' "$unit" >&2
        fi
    done
}

RETIRABLE_VHOST_KIND=

classify_retirable_finance_radar_vhost() {
    RETIRABLE_VHOST_KIND=
    [ -f "$LEGACY_STATIC_NGINX" ] && [ ! -L "$LEGACY_STATIC_NGINX" ] || {
        printf 'previous Finance Radar Nginx path is not a regular file: %s\n' "$LEGACY_STATIC_NGINX" >&2
        return 1
    }
    grep -Eq "^[[:space:]]*server_name[[:space:]]+$PUBLIC_EDGE_HOST;" "$LEGACY_STATIC_NGINX" || {
        printf 'previous Finance Radar vhost has an unexpected server name: %s\n' "$LEGACY_STATIC_NGINX" >&2
        return 1
    }

    # There are two audited historic static shapes.  The earliest deployment
    # used a root-based static site; the July deployment used a hash-routed
    # alias plus a public API proxy.  Recognize only those exact signatures,
    # never a generic Nginx server block.
    if grep -Eq "^[[:space:]]*root[[:space:]]+$PUBLIC_STATUS_DIR;" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "location[[:space:]]+/?radar" "$LEGACY_STATIC_NGINX" && \
       ! grep -Fq 'proxy_pass http://127.0.0.1:18501' "$LEGACY_STATIC_NGINX"; then
        RETIRABLE_VHOST_KIND=static
        return 0
    fi
    if grep -Eq "^[[:space:]]*location[[:space:]]*=[[:space:]]*/radar/index[.]html[[:space:]]*\\{" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "^[[:space:]]*alias[[:space:]]+$PUBLIC_STATUS_DIR/index[.]html;" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "rewrite[[:space:]]+\\^[[:space:]]+/radar/index[.]html[[:space:]]+last;" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "location[[:space:]]+/finance-radar-api/" "$LEGACY_STATIC_NGINX" && \
       grep -Fq 'proxy_pass http://127.0.0.1:18000/' "$LEGACY_STATIC_NGINX"; then
        RETIRABLE_VHOST_KIND=static
        return 0
    fi

    # The current production predecessor is already a guarded Streamlit
    # proxy, but it lives at the retired static filename.  It must be moved
    # out of the active Nginx directory before the candidate writes
    # finance-radar-direct.conf, otherwise Nginx can retain two server blocks
    # for the same TLS listener/host and serve the wrong one.  Require its
    # public-page redirects, loopback-only API guard and all internal-page
    # guards so a hand-edited or unrelated proxy is never silently replaced.
    if grep -Eq "^[[:space:]]*listen[[:space:]]+$PUBLIC_EDGE_PORT[[:space:]]+ssl;" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "^[[:space:]]*location[[:space:]]*=[[:space:]]*/[[:space:]]*\\{" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "return[[:space:]]+302[[:space:]]+/radar/;" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "^[[:space:]]*location[[:space:]]*=[[:space:]]*/radar[[:space:]]*\\{" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "return[[:space:]]+301[[:space:]]+/radar/;" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "^[[:space:]]*location[[:space:]]+/radar/[[:space:]]*\\{" "$LEGACY_STATIC_NGINX" && \
       grep -Fq 'proxy_pass http://127.0.0.1:18501;' "$LEGACY_STATIC_NGINX" && \
       grep -Eq "location[[:space:]]*=[[:space:]]*/finance-radar-api[[:space:]]*\\{" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "location[[:space:]]+\\^~[[:space:]]+/finance-radar-api/[[:space:]]*\\{" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "location[[:space:]]*=[[:space:]]*/radar-admin[[:space:]]*\\{" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "location[[:space:]]+\\^~[[:space:]]+/radar-admin/[[:space:]]*\\{" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "Event_Intelligence\\|Operations_and_Model\\|Adjudication_Studio" "$LEGACY_STATIC_NGINX" && \
       grep -Eq "return[[:space:]]+404;" "$LEGACY_STATIC_NGINX" && \
       ! grep -Eq "(^|[[:space:]])root[[:space:]]+$PUBLIC_STATUS_DIR;|$PUBLIC_STATUS_DIR/index[.]html" "$LEGACY_STATIC_NGINX"; then
        RETIRABLE_VHOST_KIND=direct-streamlit
        return 0
    fi
    printf 'refusing to retire an unrecognized Finance Radar Nginx vhost: %s\n' \
        "$LEGACY_STATIC_NGINX" >&2
    return 1
}

retire_known_predecessor_vhost() {
    local stamp retired_config retired_index
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    install -d -m 0700 "$LEGACY_STATIC_RETIRE_DIR" || return 1
    if [ -e "$LEGACY_STATIC_NGINX" ] || [ -L "$LEGACY_STATIC_NGINX" ]; then
        classify_retirable_finance_radar_vhost || return 1
        retired_config="$LEGACY_STATIC_RETIRE_DIR/finance-radar-aws.conf.${stamp}.disabled"
        [ ! -e "$retired_config" ] || return 1
        mv -- "$LEGACY_STATIC_NGINX" "$retired_config" || return 1
        printf 'previous_vhost=RETIRED kind=%s archive=%s\n' \
            "$RETIRABLE_VHOST_KIND" "$retired_config"
    fi
    # The current direct endpoint legitimately exposes only release.json below
    # this directory.  Any remaining static-root reference
    # means an unknown vhost would still be able to serve the retired shell.
    if nginx -T 2>&1 | grep -Eq \
        "(^|[[:space:]])root[[:space:]]+$PUBLIC_STATUS_DIR;|$PUBLIC_STATUS_DIR/index[.]html"; then
        printf 'an unrecognized Nginx static-terminal reference remains active\n' >&2
        return 1
    fi
    if [ -e "$LEGACY_STATIC_INDEX" ] || [ -L "$LEGACY_STATIC_INDEX" ]; then
        [ -f "$LEGACY_STATIC_INDEX" ] && [ ! -L "$LEGACY_STATIC_INDEX" ] || {
            printf 'legacy static index is not a regular file: %s\n' "$LEGACY_STATIC_INDEX" >&2
            return 1
        }
        retired_index="$LEGACY_STATIC_RETIRE_DIR/index.html.${stamp}.retired"
        [ ! -e "$retired_index" ] || return 1
        mv -- "$LEGACY_STATIC_INDEX" "$retired_index" || return 1
        printf 'legacy_static_index=RETIRED archive=%s\n' "$retired_index"
    fi
}

assert_candidate_vhost_owns_public_edge() {
    local rendered matches
    [ ! -e "$LEGACY_STATIC_NGINX" ] && [ ! -L "$LEGACY_STATIC_NGINX" ] || {
        printf 'previous Finance Radar Nginx vhost remains active: %s\n' "$LEGACY_STATIC_NGINX" >&2
        return 1
    }
    rendered="$(nginx -T 2>&1)" || {
        printf 'unable to render active Nginx configuration after candidate install\n' >&2
        return 1
    }
    matches="$(printf '%s\n' "$rendered" | grep -Ec \
        "^[[:space:]]*server_name[[:space:]]+$PUBLIC_EDGE_HOST;[[:space:]]*$" || true)"
    [ "$matches" -eq 1 ] || {
        printf 'expected exactly one active Nginx vhost for %s, found %s\n' \
            "$PUBLIC_EDGE_HOST" "$matches" >&2
        return 1
    }
}

write_public_release_marker() {
    local activation_state=${1:?activation state required}
    local accepted_release_id
    case "$activation_state" in
        ACTIVATING)
            # Keep the legacy release_id pinned to the last accepted release.
            # Status-aware clients can inspect candidate_release_id, while old
            # clients cannot mistake a candidate for a completed deployment.
            accepted_release_id="$PREVIOUS_ACCEPTED_RELEASE_ID"
            ;;
        ACCEPTED)
            accepted_release_id="$RELEASE_ID"
            ;;
        *)
            printf 'invalid public activation state: %s\n' "$activation_state" >&2
            return 2
            ;;
    esac
    install -d -m 0755 -o root -g root "$PUBLIC_STATUS_DIR" || return 1
    python3 - "$PUBLIC_RELEASE_MARKER" "$activation_state" "$RELEASE_ID" \
        "$accepted_release_id" <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1])
activation_state, candidate_release_id, accepted_release_id = sys.argv[2:]
if activation_state not in {"ACTIVATING", "ACCEPTED"}:
    raise SystemExit("unsupported activation state")
if activation_state == "ACCEPTED" and accepted_release_id != candidate_release_id:
    raise SystemExit("accepted marker must expose the candidate as the accepted release")
if path.is_symlink() or (path.exists() and not path.is_file()):
    raise SystemExit("public release marker target is unsafe")
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
payload = {
    "activation_state": activation_state,
    "candidate_release_id": candidate_release_id,
    # Existing clients continue to read release_id/public_ui. During activation
    # release_id is the prior accepted release, or null on a first deployment.
    "release_id": accepted_release_id or None,
    "public_ui": "streamlit",
}
temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(temporary, 0o644)
os.replace(temporary, path)
PY
}

assert_public_release_marker() {
    local expected_state=${1:?expected activation state required}
    local expected_accepted_release_id
    case "$expected_state" in
        ACTIVATING)
            expected_accepted_release_id="$PREVIOUS_ACCEPTED_RELEASE_ID"
            ;;
        ACCEPTED)
            expected_accepted_release_id="$RELEASE_ID"
            ;;
        *)
            printf 'invalid expected public activation state: %s\n' "$expected_state" >&2
            return 2
            ;;
    esac
    local attempt marker
    for attempt in $(seq 1 15); do
        marker=""
        if marker="$(curl --noproxy '*' --fail --silent --show-error --max-time 5 \
            --resolve "$PUBLIC_EDGE_HOST:$PUBLIC_EDGE_PORT:127.0.0.1" \
            "$PUBLIC_WEB_BASE/release.json")" && \
            python3 - "$expected_state" "$RELEASE_ID" \
                "$expected_accepted_release_id" "$marker" <<'PY'
from __future__ import annotations

import json
import sys

expected_state, candidate_release_id, accepted_release_id, raw = sys.argv[1:]
try:
    value = json.loads(raw)
except json.JSONDecodeError:
    raise SystemExit(1)
expected = {
    "activation_state": expected_state,
    "candidate_release_id": candidate_release_id,
    "release_id": accepted_release_id or None,
    "public_ui": "streamlit",
}
if value != expected:
    raise SystemExit(1)
PY
        then
            printf 'public_release_marker=PASS attempt=%s activation_state=%s release_id=%s candidate_release_id=%s\n' \
                "$attempt" "$expected_state" \
                "${expected_accepted_release_id:-none}" "$RELEASE_ID"
            return 0
        fi
        sleep 1
    done
    printf 'public release marker did not converge to %s after %s attempts: %s\n' \
        "$expected_state" 15 "$PUBLIC_WEB_BASE/release.json" >&2
    return 1
}

remove_legacy_managed_property_dropins() {
    local path property
    for path in "${LEGACY_MANAGED_PROPERTY_DROPINS[@]}"; do
        [ -e "$path" ] || continue
        [ -f "$path" ] && [ ! -L "$path" ] || {
            printf 'refusing unexpected managed systemd override: %s\n' "$path" >&2
            return 1
        }
        case "$(basename "$path")" in
            50-MemoryHigh.conf)
                property=MemoryHigh
                ;;
            50-MemoryMax.conf)
                property=MemoryMax
                ;;
            50-MemorySwapMax.conf)
                property=MemorySwapMax
                ;;
            50-TasksMax.conf)
                property=TasksMax
                ;;
            *)
                printf 'refusing unknown managed systemd override: %s\n' "$path" >&2
                return 1
                ;;
        esac
        if ! awk -v property="$property" '
            /^[[:space:]]*$/ || /^[[:space:]]*#/ || /^[[:space:]]*\[Service\][[:space:]]*$/ { next }
            $0 ~ ("^" property "=[^[:space:]]+$") { next }
            { exit 1 }
        ' "$path"; then
            printf 'refusing to remove non-generated systemd override: %s\n' "$path" >&2
            return 1
        fi
        rm -f -- "$path" || return
    done
}

remove_legacy_backup_retention_dropin() {
    local path="$LEGACY_BACKUP_RETENTION_DROPIN"
    [ -e "$path" ] || return 0
    [ -f "$path" ] && [ ! -L "$path" ] || {
        printf 'refusing unexpected legacy backup retention override: %s\n' "$path" >&2
        return 1
    }
    if ! awk '
        /^[[:space:]]*$/ || /^[[:space:]]*#/ || /^[[:space:]]*\[Service\][[:space:]]*$/ { next }
        /^[[:space:]]*ExecStart=[[:space:]]*$/ { next }
        /^[[:space:]]*ExecStart=\/opt\/finance-radar\/venv\/bin\/python -m app[.]ops[.]backup backup --retention 1 --weekly-retention 0[[:space:]]*$/ { next }
        { exit 1 }
    ' "$path"; then
        printf 'refusing to remove unrecognized backup retention override: %s\n' "$path" >&2
        return 1
    fi
    rm -f -- "$path"
    printf 'legacy_backup_retention_override=RETIRED path=%s\n' "$path"
}

create_predeploy_backup_hold_physical_copy() {
    local hold_root failed_holds receipt_tmpdir
    [ "$PREDEPLOY_BACKUP_KIND" = recovery_bundle ] || {
        printf 'cannot hold a predeploy backup that is not a complete recovery bundle\n' >&2
        return 1
    }
    [ -n "$PREDEPLOY_BACKUP_PATH" ] || {
        printf 'cannot hold a missing predeploy backup\n' >&2
        return 1
    }
    install -d -m 0700 -o root -g root "$RECOVERY_HOLD_PARENT" "$RECOVERY_HOLD_ROOT" || return 1
    failed_holds="$(find "$RECOVERY_HOLD_ROOT" -mindepth 1 -maxdepth 1 -type d \
        \( -name 'failed-precutover-*' -o -name 'failed-cutover-*' \) -print | wc -l)" || return 1
    [[ "$failed_holds" =~ ^[0-9]+$ ]] || return 1
    if [ "$failed_holds" -ge 2 ]; then
        printf 'two retained failed recovery holds require explicit operator review before another cutover\n' >&2
        return 1
    fi
    # The held bundle is verified with isolated SQLite restores.  Keep that
    # scratch on the same private, disk-backed location used by the backup
    # receipt gate rather than falling back to a small /tmp tmpfs.
    receipt_tmpdir="${FINANCE_RADAR_BACKUP_RECEIPT_TMPDIR:-/var/tmp/finance-radar-receipt}"
    [ -d "$receipt_tmpdir" ] && [ ! -L "$receipt_tmpdir" ] || {
        printf 'receipt verifier temporary directory is unsafe: %s\n' "$receipt_tmpdir" >&2
        return 1
    }
    hold_root="$RECOVERY_HOLD_ROOT/.inflight-${RELEASE_ID}-$$"
    [[ "$hold_root" == "$RECOVERY_HOLD_ROOT/.inflight-${RELEASE_ID}-"* ]] || {
        printf 'refusing unexpected predeploy recovery hold path: %s\n' "$hold_root" >&2
        return 1
    }
    [ ! -e "$hold_root" ] || {
        printf 'predeploy recovery hold path already exists: %s\n' "$hold_root" >&2
        return 1
    }
    if ! TMPDIR="$receipt_tmpdir" python3 - "$PREDEPLOY_BACKUP_PATH" "$SHARED/data/operational_backups" \
        "$hold_root" "$PREDEPLOY_BACKUP_RECEIPT_SHA256" "$BACKUP_RECEIPT_VERIFIER" \
        "$receipt_tmpdir" <<'PY'
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys


source = Path(sys.argv[1])
backup_root = Path(sys.argv[2])
hold_root = Path(sys.argv[3])
receipt_sha256 = sys.argv[4]
verifier_path = Path(sys.argv[5])
receipt_tmpdir = Path(sys.argv[6])


def fail(message: str) -> None:
    raise SystemExit(message)


def lstat_directory(path: Path, label: str, *, private_root_only: bool = False) -> os.stat_result:
    try:
        result = os.lstat(path)
    except OSError as exc:
        fail(f"{label} is unavailable: {exc}")
    if stat.S_ISLNK(result.st_mode) or not stat.S_ISDIR(result.st_mode):
        fail(f"{label} is not a real directory")
    if private_root_only and (
        result.st_uid != 0
        or result.st_gid != 0
        or stat.S_IMODE(result.st_mode) & 0o077
    ):
        fail(f"{label} must be a root-owned private directory")
    return result


def same_inode(expected: os.stat_result, actual: os.stat_result) -> bool:
    return (
        stat.S_IFMT(expected.st_mode) == stat.S_IFMT(actual.st_mode)
        and expected.st_dev == actual.st_dev
        and expected.st_ino == actual.st_ino
    )


def same_source_snapshot(expected: os.stat_result, actual: os.stat_result) -> bool:
    return same_inode(expected, actual) and expected.st_size == actual.st_size and expected.st_mtime_ns == actual.st_mtime_ns


def close_quietly(fd: int | None) -> None:
    if fd is not None:
        os.close(fd)


def scandir_from_duplicate(directory_fd: int) -> os.ScandirIterator[str]:
    """Let scandir own a duplicate, never the caller's verified directory FD."""
    duplicate_fd = os.dup(directory_fd)
    try:
        return os.scandir(duplicate_fd)
    except BaseException:
        close_quietly(duplicate_fd)
        raise


if not source.is_absolute() or not backup_root.is_absolute() or not hold_root.is_absolute():
    fail("predeploy hold paths must be absolute")
# This is deliberately lexical: resolving first would turn a symlink or an
# embedded parent traversal into an apparently safe backup-root child.
if source.parent != backup_root or source.name in {"", ".", ".."}:
    fail("predeploy source is not a direct operational-backups child")
if not receipt_sha256 or len(receipt_sha256) != 64:
    fail("invalid predeploy receipt hash")
if any(character not in "0123456789abcdef" for character in receipt_sha256):
    fail("invalid predeploy receipt hash")
if not verifier_path.is_file() or verifier_path.is_symlink():
    fail("backup receipt verifier is unavailable")
if not receipt_tmpdir.is_absolute():
    fail("receipt verifier temporary directory must be absolute")
if os.path.lexists(hold_root):
    fail("predeploy hold already exists")

# Linux production must expose descriptor-relative, no-follow operations. A
# deployment guard that cannot pin source and root-only hold directories must
# fail closed rather than race a path lookup during cutover.
descriptor_functions = (os.open, os.stat, os.mkdir)
if (
    not hasattr(os, "O_NOFOLLOW")
    or not hasattr(os, "O_DIRECTORY")
    or any(function not in os.supports_dir_fd for function in descriptor_functions)
):
    fail("platform lacks safe descriptor-relative predeploy hold support")
no_follow = os.O_NOFOLLOW
directory_flags = os.O_RDONLY | os.O_DIRECTORY | no_follow

lstat_directory(backup_root, "operational backup root")
lstat_directory(hold_root.parent, "predeploy hold parent", private_root_only=True)
lstat_directory(hold_root.parent.parent, "predeploy hold grandparent", private_root_only=True)
lstat_directory(receipt_tmpdir, "receipt verifier temporary directory", private_root_only=True)
backup_root_fd: int | None = None
hold_fd: int | None = None
source_fd: int | None = None
destination_fd: int | None = None
destination: Path | None = None
try:
    backup_root_fd = os.open(backup_root, directory_flags)
    source_stat = os.stat(source.name, dir_fd=backup_root_fd, follow_symlinks=False)
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISDIR(source_stat.st_mode):
        fail("predeploy source must be a real recovery-bundle directory")

    source_fd = os.open(source.name, directory_flags, dir_fd=backup_root_fd)
    if not same_inode(source_stat, os.fstat(source_fd)):
        fail("bundle source changed while opening the verified child")

    def inspect_bundle_size(source_directory_fd: int, expected: os.stat_result) -> tuple[int, int]:
        logical_bytes = 0
        largest_sqlite_bytes = 0
        with scandir_from_duplicate(source_directory_fd) as entries:
            for entry in entries:
                member = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(member.st_mode):
                    logical_bytes += member.st_size
                    if entry.name.endswith(".sqlite3"):
                        largest_sqlite_bytes = max(largest_sqlite_bytes, member.st_size)
                    continue
                if not stat.S_ISDIR(member.st_mode):
                    fail(f"bundle contains an unsafe member: {entry.name}")
                child_fd: int | None = None
                try:
                    child_fd = os.open(entry.name, directory_flags, dir_fd=source_directory_fd)
                    if not same_inode(member, os.fstat(child_fd)):
                        fail(f"bundle source changed while sizing directory: {entry.name}")
                    child_logical, child_largest_sqlite = inspect_bundle_size(child_fd, member)
                    logical_bytes += child_logical
                    largest_sqlite_bytes = max(largest_sqlite_bytes, child_largest_sqlite)
                    if not same_source_snapshot(member, os.fstat(child_fd)):
                        fail(f"bundle source changed while sizing directory: {entry.name}")
                finally:
                    close_quietly(child_fd)
        if not same_source_snapshot(expected, os.fstat(source_directory_fd)):
            fail("bundle source changed while sizing the verified child")
        return logical_bytes, largest_sqlite_bytes

    # The hold survives until the post-cutover bundle and its restore receipt
    # have both passed.  Budget that future bundle and SQLite restore *before*
    # beginning any physical copy, so a small volume fails closed rather than
    # running out of space partway through a protected cutover.
    source_logical_bytes, largest_sqlite_bytes = inspect_bundle_size(source_fd, source_stat)
    if source_logical_bytes <= 0 or largest_sqlite_bytes <= 0:
        fail("predeploy recovery bundle has no measurable SQLite recovery payload")
    safety_bytes = 512 * 1024 * 1024
    planned_by_device: dict[int, dict[str, int]] = {}

    def reserve(path: Path, label: str, amount: int) -> None:
        path_stat = os.stat(path)
        filesystem = os.statvfs(path)
        available = filesystem.f_bavail * filesystem.f_frsize
        plan = planned_by_device.setdefault(path_stat.st_dev, {"available": available})
        if plan["available"] != available:
            plan["available"] = min(plan["available"], available)
        plan[label] = plan.get(label, 0) + amount

    reserve(hold_root.parent, "physical_hold", source_logical_bytes)
    reserve(backup_root, "projected_postcutover_bundle", source_logical_bytes)
    reserve(receipt_tmpdir, "projected_sqlite_receipt_scratch", largest_sqlite_bytes)
    for device, plan in planned_by_device.items():
        planned = sum(amount for label, amount in plan.items() if label != "available")
        required = planned + safety_bytes
        if plan["available"] < required:
            fail(
                "predeploy recovery hold storage headroom insufficient: "
                f"device={device} available_bytes={plan['available']} required_bytes={required} "
                f"physical_hold_bytes={plan.get('physical_hold', 0)} "
                f"projected_postcutover_bundle_bytes={plan.get('projected_postcutover_bundle', 0)} "
                f"projected_sqlite_receipt_scratch_bytes={plan.get('projected_sqlite_receipt_scratch', 0)} "
                f"safety_bytes={safety_bytes}"
            )

    os.mkdir(hold_root, mode=0o700)
    hold_fd = os.open(hold_root, directory_flags)
    destination = hold_root / source.name

    def copy_regular(
        name: str,
        *,
        source_directory_fd: int,
        destination_directory_fd: int,
        expected: os.stat_result,
    ) -> None:
        source_file_fd: int | None = None
        destination_file_fd: int | None = None
        try:
            source_file_fd = os.open(name, os.O_RDONLY | no_follow, dir_fd=source_directory_fd)
            opened_source = os.fstat(source_file_fd)
            if not stat.S_ISREG(opened_source.st_mode) or not same_inode(expected, opened_source):
                fail(f"predeploy source changed while opening file: {name}")
            destination_file_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
                0o400,
                dir_fd=destination_directory_fd,
            )
            while True:
                chunk = os.read(source_file_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_file_fd, view)
                    if written <= 0:
                        fail(f"unable to copy predeploy hold file: {name}")
                    view = view[written:]
            os.fsync(destination_file_fd)
            if not same_source_snapshot(expected, os.fstat(source_file_fd)):
                fail(f"predeploy source changed while copying file: {name}")
            held = os.fstat(destination_file_fd)
            if (
                not stat.S_ISREG(held.st_mode)
                or held.st_dev == opened_source.st_dev and held.st_ino == opened_source.st_ino
                or held.st_nlink != 1
            ):
                fail(f"predeploy hold is not an independent regular copy: {name}")
            os.fchmod(destination_file_fd, 0o400)
        finally:
            close_quietly(destination_file_fd)
            close_quietly(source_file_fd)

    def copy_tree(source_directory_fd: int, destination_directory_fd: int) -> None:
        with scandir_from_duplicate(source_directory_fd) as entries:
            for entry in entries:
                member = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(member.st_mode):
                    copy_regular(
                        entry.name,
                        source_directory_fd=source_directory_fd,
                        destination_directory_fd=destination_directory_fd,
                        expected=member,
                    )
                    continue
                if not stat.S_ISDIR(member.st_mode):
                    fail(f"bundle contains an unsafe member: {entry.name}")
                os.mkdir(entry.name, mode=0o500, dir_fd=destination_directory_fd)
                child_source_fd: int | None = None
                child_destination_fd: int | None = None
                try:
                    child_source_fd = os.open(
                        entry.name,
                        directory_flags,
                        dir_fd=source_directory_fd,
                    )
                    if not same_inode(member, os.fstat(child_source_fd)):
                        fail(f"bundle source changed while opening directory: {entry.name}")
                    child_destination_fd = os.open(
                        entry.name,
                        directory_flags,
                        dir_fd=destination_directory_fd,
                    )
                    copy_tree(child_source_fd, child_destination_fd)
                    os.fsync(child_destination_fd)
                    if not same_source_snapshot(member, os.fstat(child_source_fd)):
                        fail(f"bundle source changed while copying directory: {entry.name}")
                    os.fchmod(child_destination_fd, 0o500)
                finally:
                    close_quietly(child_destination_fd)
                    close_quietly(child_source_fd)

    os.mkdir(source.name, mode=0o500, dir_fd=hold_fd)
    destination_fd = os.open(source.name, directory_flags, dir_fd=hold_fd)
    copy_tree(source_fd, destination_fd)
    os.fsync(destination_fd)
    if not same_source_snapshot(source_stat, os.fstat(source_fd)):
        fail("bundle source changed while copying the verified child")
    os.fchmod(destination_fd, 0o500)
    os.fsync(hold_fd)
finally:
    close_quietly(destination_fd)
    close_quietly(source_fd)
    close_quietly(hold_fd)
    close_quietly(backup_root_fd)

# The original receipt validated the source before the hold was made. Validate
# the independent root-owned physical copy again, then bind its manifest digest
# to the one just accepted before any cutover may start.
if destination is None:
    fail("predeploy hold destination was not created")
module_spec = importlib.util.spec_from_file_location("finance_radar_backup_receipt", verifier_path)
if module_spec is None or module_spec.loader is None:
    fail("unable to load backup receipt verifier")
verifier = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = verifier
module_spec.loader.exec_module(verifier)
baseline = datetime(1970, 1, 1, tzinfo=timezone.utc)
try:
    held_receipt = verifier.verify_full_bundle(destination, started_at=baseline)
except verifier.ReceiptError as exc:
    fail(f"predeploy hold receipt validation failed: {exc}")

held_receipt_sha256 = str(held_receipt.get("receipt_sha256") or "")
if held_receipt_sha256 != receipt_sha256:
    fail("predeploy hold manifest hash does not match the verified receipt")
metadata = {
    "kind": "recovery_bundle",
    "original_path": str(source),
    "hold_path": str(destination),
    "receipt_sha256": receipt_sha256,
    "hold_digest_sha256": held_receipt_sha256,
    "held_receipt_sha256": held_receipt_sha256,
    "protection": "root-owned independent physical copy; preserve on any subsequent deployment failure",
}
metadata_path = hold_root / "HOLD_RECEIPT.json"
with metadata_path.open("w", encoding="utf-8") as handle:
    handle.write(json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(metadata_path, 0o400)
metadata_dir_fd = os.open(hold_root, directory_flags)
try:
    os.fsync(metadata_dir_fd)
finally:
    close_quietly(metadata_dir_fd)
print(destination)
PY
    then
        if [ -d "$hold_root" ] && [[ "$hold_root" == "$RECOVERY_HOLD_ROOT/.inflight-${RELEASE_ID}-"* ]]; then
            rm -rf -- "$hold_root" || true
        fi
        return 1
    fi
    PREDEPLOY_HOLD_ROOT="$hold_root"
    PREDEPLOY_HOLD_PATH="$hold_root/$(basename "$PREDEPLOY_BACKUP_PATH")"
    [ -n "$PREDEPLOY_HOLD_PATH" ] && [ -e "$PREDEPLOY_HOLD_PATH" ] || {
        printf 'predeploy recovery hold did not contain a protected backup\n' >&2
        return 1
    }
    printf 'predeploy_backup_hold=READY path=%s format=%s mode=physical_copy\n' \
        "$PREDEPLOY_HOLD_PATH" "$PREDEPLOY_BACKUP_KIND"
}

create_predeploy_backup_hold() {
    local failed_holds hold_root hold_summary receipt_tmpdir
    case "${FINANCE_RADAR_DEPLOY_HOLD_MODE:-atomic_custody}" in
        atomic_custody)
            ;;
        physical_copy)
            create_predeploy_backup_hold_physical_copy
            return
            ;;
        *)
            printf 'unknown predeploy hold mode: %s\n' \
                "${FINANCE_RADAR_DEPLOY_HOLD_MODE}" >&2
            return 1
            ;;
    esac
    [ "$PREDEPLOY_BACKUP_KIND" = recovery_bundle ] || {
        printf 'cannot hold a predeploy backup that is not a complete recovery bundle\n' >&2
        return 1
    }
    [ -n "$PREDEPLOY_BACKUP_PATH" ] || {
        printf 'cannot hold a missing predeploy backup\n' >&2
        return 1
    }
    install -d -m 0700 -o root -g root "$RECOVERY_HOLD_PARENT" "$RECOVERY_HOLD_ROOT" || return 1
    failed_holds="$(find "$RECOVERY_HOLD_ROOT" -mindepth 1 -maxdepth 1 -type d \
        \( -name 'failed-precutover-*' -o -name 'failed-cutover-*' \) -print | wc -l)" || return 1
    [[ "$failed_holds" =~ ^[0-9]+$ ]] || return 1
    if [ "$failed_holds" -ge 2 ]; then
        printf 'two retained failed recovery holds require explicit operator review before another cutover\n' >&2
        return 1
    fi
    receipt_tmpdir="${FINANCE_RADAR_BACKUP_RECEIPT_TMPDIR:-/var/tmp/finance-radar-receipt}"
    [ -d "$receipt_tmpdir" ] && [ ! -L "$receipt_tmpdir" ] || {
        printf 'receipt verifier temporary directory is unsafe: %s\n' "$receipt_tmpdir" >&2
        return 1
    }
    hold_root="$RECOVERY_HOLD_ROOT/.inflight-${RELEASE_ID}-$$"
    [[ "$hold_root" == "$RECOVERY_HOLD_ROOT/.inflight-${RELEASE_ID}-"* ]] || {
        printf 'refusing unexpected predeploy recovery hold path: %s\n' "$hold_root" >&2
        return 1
    }
    [ ! -e "$hold_root" ] && [ ! -L "$hold_root" ] || {
        printf 'predeploy recovery hold path already exists: %s\n' "$hold_root" >&2
        return 1
    }
    hold_summary="$(TMPDIR="$receipt_tmpdir" python3 "$BACKUP_HOLD_TRANSFER" \
        --source "$PREDEPLOY_BACKUP_PATH" \
        --backup-root "$SHARED/data/operational_backups" \
        --hold-root "$hold_root" \
        --receipt-sha256 "$PREDEPLOY_BACKUP_RECEIPT_SHA256" \
        --verifier "$BACKUP_RECEIPT_VERIFIER" \
        --receipt-tmpdir "$receipt_tmpdir")" || return 1
    PREDEPLOY_HOLD_ROOT="$hold_root"
    PREDEPLOY_HOLD_PATH="$hold_root/$(basename "$PREDEPLOY_BACKUP_PATH")"
    [ -f "$PREDEPLOY_HOLD_ROOT/HOLD_RECEIPT.json" ] && \
        [ -f "$PREDEPLOY_HOLD_PATH/manifest.json" ] && \
        [ ! -e "$PREDEPLOY_BACKUP_PATH" ] || {
            printf 'atomic predeploy custody transfer is incomplete\n' >&2
            return 1
        }
    printf 'predeploy_backup_hold=READY path=%s format=%s mode=atomic_custody\n' \
        "$PREDEPLOY_HOLD_PATH" "$PREDEPLOY_BACKUP_KIND"
    printf 'predeploy_backup_hold_summary=%s\n' "$hold_summary"
}

clear_predeploy_backup_hold() {
    [ -n "$PREDEPLOY_HOLD_ROOT" ] || return 0
    [[ "$PREDEPLOY_HOLD_ROOT" == "$RECOVERY_HOLD_ROOT/.inflight-${RELEASE_ID}-"* ]] || {
        printf 'refusing unexpected predeploy recovery hold cleanup path: %s\n' \
            "$PREDEPLOY_HOLD_ROOT" >&2
        return 1
    }
    rm -rf -- "$PREDEPLOY_HOLD_ROOT" || return 1
    PREDEPLOY_HOLD_ROOT=""
    PREDEPLOY_HOLD_PATH=""
}

inhibit_worker_resume() {
    local runtime_dir
    runtime_dir="$(dirname "$WORKER_RESUME_INHIBIT")"
    [ ! -e "$WORKER_RESUME_INHIBIT" ] && [ ! -L "$WORKER_RESUME_INHIBIT" ] || {
        printf 'worker resume inhibit marker already exists: %s\n' "$WORKER_RESUME_INHIBIT" >&2
        return 1
    }
    install -d -m 0750 -o root -g root "$runtime_dir" || return 1
    install -m 0600 -o root -g root /dev/null "$WORKER_RESUME_INHIBIT" || return 1
    printf 'release=%s\npid=%s\npurpose=protected-cutover-bridge\n' "$RELEASE_ID" "$$" \
        > "$WORKER_RESUME_INHIBIT" || return 1
    WORKER_RESUME_INHIBIT_CREATED=1
    printf 'worker_resume_inhibit=READY path=%s\n' "$WORKER_RESUME_INHIBIT"
}

clear_worker_resume_inhibit() {
    [ "$WORKER_RESUME_INHIBIT_CREATED" -eq 1 ] || return 0
    [ ! -L "$WORKER_RESUME_INHIBIT" ] && [ -f "$WORKER_RESUME_INHIBIT" ] || {
        printf 'worker resume inhibit marker is no longer a regular file: %s\n' \
            "$WORKER_RESUME_INHIBIT" >&2
        return 1
    }
    rm -f -- "$WORKER_RESUME_INHIBIT" || return 1
    WORKER_RESUME_INHIBIT_CREATED=0
    printf 'worker_resume_inhibit=CLEARED path=%s\n' "$WORKER_RESUME_INHIBIT"
}

inhibit_scheduled_backup_start() {
    local runtime_dir
    runtime_dir="$(dirname "$BACKUP_START_INHIBIT")"
    [ ! -e "$BACKUP_START_INHIBIT" ] && [ ! -L "$BACKUP_START_INHIBIT" ] || {
        printf 'backup start inhibit marker already exists: %s\n' "$BACKUP_START_INHIBIT" >&2
        return 1
    }
    install -d -m 0750 -o root -g root "$runtime_dir" || return 1
    install -m 0600 -o root -g root /dev/null "$BACKUP_START_INHIBIT" || return 1
    printf 'release=%s\npid=%s\npurpose=timer-stop-stabilization\n' "$RELEASE_ID" "$$" \
        > "$BACKUP_START_INHIBIT" || return 1
    BACKUP_START_INHIBIT_CREATED=1
    printf 'backup_start_inhibit=READY path=%s\n' "$BACKUP_START_INHIBIT"
}

clear_scheduled_backup_start_inhibit() {
    [ "$BACKUP_START_INHIBIT_CREATED" -eq 1 ] || return 0
    [ -f "$BACKUP_START_INHIBIT" ] && [ ! -L "$BACKUP_START_INHIBIT" ] || {
        printf 'backup start inhibit marker is missing or unsafe\n' >&2
        return 1
    }
    rm -f -- "$BACKUP_START_INHIBIT" || return 1
    BACKUP_START_INHIBIT_CREATED=0
    printf 'backup_start_inhibit=CLEARED path=%s\n' "$BACKUP_START_INHIBIT"
}

assert_backup_service_quiescent() {
    local attempt state jobs
    for attempt in 1 2; do
        state="$(systemctl show finance-radar-backup.service --property=ActiveState --value)" || return 1
        jobs="$(systemctl list-jobs --no-legend --plain 2>/dev/null | \
            awk '$2 == "finance-radar-backup.service" { print $0 }')" || return 1
        case "$state" in
            inactive|failed) ;;
            *)
                printf 'backup service is not quiescent: state=%s\n' "$state" >&2
                return 1
                ;;
        esac
        [ -z "$jobs" ] || {
            printf 'backup service still has a queued systemd job: %s\n' "$jobs" >&2
            return 1
        }
        [ "$attempt" -eq 2 ] || sleep 1
    done
    systemctl reset-failed finance-radar-backup.service 2>/dev/null || true
    state="$(systemctl show finance-radar-backup.service --property=ActiveState --value)" || return 1
    [ "$state" = inactive ] || {
        printf 'backup service did not settle inactive: state=%s\n' "$state" >&2
        return 1
    }
    printf 'backup_service_quiescent=PASS\n'
}

assert_capture_interpretation_quiescent() {
    local attempt state jobs
    # An already running provider call owns its lease and may already be
    # billable.  Stop only the timer, then wait up to one bounded request
    # window for the oneshot to finish naturally; never SIGTERM it mid-call.
    # One batch may contain 20 requests, run with three workers and a 45-second
    # provider timeout.  Eight minutes covers that bounded worst case plus
    # scheduler/database overhead without turning a healthy in-flight batch
    # into a repeatable deployment failure.
    for attempt in $(seq 1 480); do
        state="$(systemctl show finance-radar-capture-interpretation.service \
            --property=ActiveState --value 2>/dev/null || printf 'not-found')"
        jobs="$(systemctl list-jobs --no-legend --plain 2>/dev/null | \
            awk '$2 == "finance-radar-capture-interpretation.service" { print $0 }')" || return 1
        case "$state" in
            inactive|failed|not-found)
                if [ -z "$jobs" ]; then
                    systemctl reset-failed finance-radar-capture-interpretation.service \
                        2>/dev/null || true
                    printf 'capture_interpretation_quiescent=PASS wait_seconds=%s\n' \
                        "$((attempt - 1))"
                    return 0
                fi
                ;;
        esac
        sleep 1
    done
    printf 'capture interpretation did not become quiescent within 480 seconds: state=%s jobs=%s\n' \
        "$state" "${jobs:-none}" >&2
    return 1
}

restore_capture_interpretation_runtime() {
    local timer=finance-radar-capture-interpretation.timer
    local was_enabled=0 was_active=0
    if grep -Fqx -- "$timer" "$ROLLBACK_ENABLED_UNITS"; then
        was_enabled=1
    fi
    if grep -Fqx -- "$timer" "$ROLLBACK_ACTIVE_UNITS"; then
        was_active=1
    fi

    if [ "$was_enabled" -eq 1 ]; then
        systemctl enable "$timer" || return 1
    else
        systemctl disable "$timer" || return 1
    fi
    if [ "$was_active" -eq 1 ]; then
        [ "$DEEPSEEK_CREDENTIAL_READY" -eq 1 ] || {
            printf 'cannot resume capture interpretation timer without a validated credential\n' >&2
            return 1
        }
        systemctl start "$timer" || return 1
        systemctl is-active --quiet "$timer" || return 1
        printf 'capture_interpretation_timer=RESTORED_ACTIVE enabled=%s\n' "$was_enabled"
    else
        systemctl stop "$timer" 2>/dev/null || true
        systemctl is-active --quiet "$timer" && return 1
        printf 'capture_interpretation_timer=RESTORED_INACTIVE enabled=%s\n' "$was_enabled"
    fi
}

assert_qwen_risk_worker_quiescent() {
    local attempt state jobs
    for attempt in $(seq 1 650); do
        state="$(systemctl show finance-radar-qwen-risk-worker.service \
            --property=ActiveState --value 2>/dev/null || printf 'not-found')"
        jobs="$(systemctl list-jobs --no-legend --plain 2>/dev/null | \
            awk '$2 == "finance-radar-qwen-risk-worker.service" { print $0 }')" || return 1
        case "$state" in
            inactive|failed|not-found)
                if [ -z "$jobs" ]; then
                    systemctl reset-failed finance-radar-qwen-risk-worker.service \
                        2>/dev/null || true
                    printf 'qwen_risk_worker_quiescent=PASS wait_seconds=%s\n' \
                        "$((attempt - 1))"
                    return 0
                fi
                ;;
        esac
        sleep 1
    done
    printf 'Qwen risk worker did not become quiescent: state=%s jobs=%s\n' \
        "$state" "${jobs:-none}" >&2
    return 1
}

restore_qwen_risk_runtime() {
    local timer=finance-radar-qwen-risk-worker.timer
    local was_enabled=0 was_active=0
    grep -Fqx -- "$timer" "$ROLLBACK_ENABLED_UNITS" && was_enabled=1
    grep -Fqx -- "$timer" "$ROLLBACK_ACTIVE_UNITS" && was_active=1
    if [ "$was_enabled" -eq 1 ]; then
        systemctl enable "$timer" || return 1
    else
        systemctl disable "$timer" || return 1
    fi
    if [ "$was_active" -eq 1 ]; then
        [ -s /etc/finance-radar-qwen-risk.env ] && \
        [ -s /opt/finance-radar/qwen-risk/model-manifest.json ] && \
        [ -s /opt/finance-radar/qwen-risk/qwen2.5-1.5b-instruct-q4_k_m.gguf ] && \
        [ -s /opt/finance-radar/qwen-risk/finance-radar-qwen-risk-v3-lora-f16.gguf ] || {
            printf 'cannot resume Qwen risk timer without its accepted model bundle\n' >&2
            return 1
        }
        systemctl start finance-radar-qwen-risk-model.service || return 1
        systemctl start "$timer" || return 1
        systemctl is-active --quiet finance-radar-qwen-risk-model.service "$timer" || return 1
        printf 'qwen_risk_timer=RESTORED_ACTIVE enabled=%s\n' "$was_enabled"
    else
        systemctl stop "$timer" 2>/dev/null || true
        systemctl is-active --quiet "$timer" && return 1
        printf 'qwen_risk_timer=RESTORED_INACTIVE enabled=%s\n' "$was_enabled"
    fi
}

preserve_failed_predeploy_backup_hold() {
    local failed_root failure_phase
    [ -n "$PREDEPLOY_HOLD_ROOT" ] && [ -d "$PREDEPLOY_HOLD_ROOT" ] || return 0
    [[ "$PREDEPLOY_HOLD_ROOT" == "$RECOVERY_HOLD_ROOT/.inflight-${RELEASE_ID}-"* ]] || {
        printf 'refusing unexpected predeploy recovery hold preservation path: %s\n' \
            "$PREDEPLOY_HOLD_ROOT" >&2
        return 1
    }
    if [ "$CUTOVER_STARTED" -eq 1 ]; then
        failure_phase=failed-cutover
    else
        failure_phase=failed-precutover
    fi
    failed_root="$RECOVERY_HOLD_ROOT/${failure_phase}-${RELEASE_ID}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    [[ "$failed_root" == "$RECOVERY_HOLD_ROOT/${failure_phase}-${RELEASE_ID}-"* ]] || return 1
    mv -- "$PREDEPLOY_HOLD_ROOT" "$failed_root" || return 1
    PREDEPLOY_HOLD_ROOT="$failed_root"
    PREDEPLOY_HOLD_PATH="$failed_root/$(basename "$PREDEPLOY_BACKUP_PATH")"
    printf 'rollback_recovery_hold=PRESERVED phase=%s path=%s\n' \
        "$failure_phase" "$PREDEPLOY_HOLD_PATH" >&2
}

rollback() {
    local status=${1:-1}
    local path
    trap - ERR
    set +e
    printf 'activation_state=ROLLBACK candidate_release_id=%s; restoring previous release and configuration\n' \
        "$RELEASE_ID" >&2
    # A code-only failure before the current symlink changes has touched only
    # the backup timer and byte-identical unit/config files.  Do not turn an
    # eligibility or preparation failure into an avoidable API/Web/collector
    # outage.  Once cutover starts (or for every full deployment), stop the
    # runtime before restoring its release/configuration snapshot as before.
    if [ "$SERVICES_TOUCHED" -eq 1 ] && \
       { [ "$DEPLOY_MODE" = full ] || [ "$CUTOVER_STARTED" -eq 1 ]; }; then
        systemctl stop finance-radar-evidence-llm \
            finance-radar-worker finance-radar-api finance-radar-web finance-radar-backup.timer \
            2>/dev/null || true
        if [ "$BACKUP_SERVICE_OWNED" -eq 1 ]; then
            systemctl stop finance-radar-backup.service 2>/dev/null || true
        fi
    fi
    clear_scheduled_backup_start_inhibit || \
        printf 'rollback_warning=backup_start_inhibit_cleanup_failed\n' >&2
    clear_worker_resume_inhibit || \
        printf 'rollback_warning=worker_resume_inhibit_cleanup_failed\n' >&2
    if [ "$ACTIVATION_RECORD_COMMITTED" -eq 1 ]; then
        # ACTIVATION.txt belongs to the candidate tree, which the archive was
        # required not to contain. Never retain an accepted receipt for a
        # release whose symlink, services or public marker are being rolled back.
        rm -f -- "$RELEASE_RECORDS/ACTIVATION.txt" || \
            printf 'rollback_warning=activation_receipt_cleanup_failed\n' >&2
        ACTIVATION_RECORD_COMMITTED=0
    fi
    if [ -n "$ACTIVATION_PENDING" ] && \
       [[ "$ACTIVATION_PENDING" == "$RELEASE_RECORDS/.ACTIVATION.pending."* ]]; then
        rm -f -- "$ACTIVATION_PENDING" || \
            printf 'rollback_warning=activation_pending_cleanup_failed\n' >&2
    fi
    if [ "$CUTOVER_STARTED" -eq 1 ]; then
        if [ -n "$PREVIOUS_RELEASE" ]; then
            ln -sfn "$PREVIOUS_RELEASE" "$BASE/current" || true
        else
            rm -f -- "$BASE/current" || true
        fi
    fi
    preserve_failed_predeploy_backup_hold || \
        printf 'rollback_warning=predeploy_recovery_hold_preservation_failed\n' >&2
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

if systemctl is-active --quiet finance-radar-backup.service; then
    printf 'daily backup is already active; retry deployment after it finishes\n' >&2
    exit 4
fi
snapshot_rollback_state || {
    snapshot_status=$?
    if [[ "$ROLLBACK_DIR" == /var/tmp/finance-radar-install-* ]]; then
        rm -rf -- "$ROLLBACK_DIR"
    fi
    exit "$snapshot_status"
}
trap 'rollback "$?"' ERR

SERVICES_TOUCHED=1
# The bridge is a one-shot candidate service, never an in-place replacement of
# the active backup unit. Stop the only continuous mutable workload and the
# timer before it can race the inventory, then retain that stopped state until
# the new complete bundle has passed its outer receipt. The rollback snapshot
# above restores the exact previous active/enabled state on failure.
inhibit_scheduled_backup_start || \
    abort_cutover 'unable to inhibit scheduled backup start during timer stop' 4
systemctl stop finance-radar-backup.timer || \
    abort_cutover 'backup timer failed to stop before protected bridge backup' 4
if ! assert_backup_service_quiescent; then
    # The timer raced our stop. Do not terminate a valid 90-minute restore
    # drill: no workload or release state has changed yet, so restore the timer
    # and leave the candidate staged for a later retry.
    clear_scheduled_backup_start_inhibit || \
        abort_cutover 'daily backup raced cutover and its start inhibit could not be cleared' 4
    systemctl start finance-radar-backup.timer || \
        abort_cutover 'daily backup raced cutover and its timer could not be restored' 4
    systemctl is-active --quiet finance-radar-backup.timer || \
        abort_cutover 'daily backup raced cutover and its timer remains inactive' 4
    [[ "$ROLLBACK_DIR" == /var/tmp/finance-radar-install-* ]] && rm -rf -- "$ROLLBACK_DIR"
    printf 'daily backup raced the timer stop; retry deployment after it finishes\n' >&2
    exit 4
fi
clear_scheduled_backup_start_inhibit || \
    abort_cutover 'backup timer stopped but its start inhibit could not be cleared' 4
# A full deployment may change persistence, collection or schema code.  It must
# quiesce every writer before the candidate backup and keep the collector down
# through the post-cutover restore drill. The code-only contract proves schema
# owners, dependencies and service contracts byte-identical, so the active
# worker can keep running through candidate preparation. It stops only for the
# short activation and the live schema is receipted before and after it.
# Preserve the credential gate in both modes: an enabled interpretation timer
# without its root-only credential is already an unsafe state to carry forward.
if { grep -Fqx -- finance-radar-capture-interpretation.timer "$ROLLBACK_ENABLED_UNITS" || \
     grep -Fqx -- finance-radar-capture-interpretation.timer "$ROLLBACK_ACTIVE_UNITS"; } && \
   [ "$DEEPSEEK_CREDENTIAL_READY" -ne 1 ]; then
    abort_cutover 'capture interpretation was enabled or active but its new root-only credential is unavailable' 4
fi
if [ "$DEPLOY_MODE" = full ]; then
    if systemctl cat finance-radar-capture-interpretation.timer >/dev/null 2>&1; then
        systemctl stop finance-radar-capture-interpretation.timer || \
            abort_cutover 'capture interpretation timer failed to stop before protected bridge backup' 4
    fi
    assert_capture_interpretation_quiescent || \
        abort_cutover 'capture interpretation remained active before protected bridge backup' 4
    if systemctl cat finance-radar-qwen-risk-worker.timer >/dev/null 2>&1; then
        systemctl stop finance-radar-qwen-risk-worker.timer || \
            abort_cutover 'Qwen risk timer failed to stop before protected bridge backup' 4
    fi
    assert_qwen_risk_worker_quiescent || \
        abort_cutover 'Qwen risk worker remained active before protected bridge backup' 4
    if systemctl cat finance-radar-market.timer >/dev/null 2>&1; then
        systemctl stop finance-radar-market.timer || \
            abort_cutover 'market timer failed to stop before protected bridge backup' 4
    fi
    if systemctl is-active --quiet finance-radar-market.service; then
        for _ in $(seq 1 60); do
            systemctl is-active --quiet finance-radar-market.service || break
            sleep 1
        done
    fi
    systemctl is-active --quiet finance-radar-market.service && \
        abort_cutover 'market observation cycle remained active before protected bridge backup' 4
    inhibit_worker_resume || \
        abort_cutover 'unable to inhibit worker resume during protected bridge backup' 4
    systemctl stop finance-radar-worker || \
        abort_cutover 'worker failed to stop before protected bridge backup' 4
    systemctl is-active --quiet finance-radar-worker && \
        abort_cutover 'worker remains active before protected bridge backup' 4
    systemctl stop finance-radar-evidence-llm.service 2>/dev/null || true
    require_predeploy_memory_headroom || \
        abort_cutover 'insufficient host memory for protected bridge backup' 4
    require_predeploy_verified_backup || \
        abort_cutover 'predeploy backup service or receipt validation failed' 4
    create_predeploy_backup_hold || \
        abort_cutover 'unable to create an independent predeploy recovery hold before cutover' 4
else
    # Validate only after both the timer and any in-flight backup service are
    # stopped.  Retention=1 can otherwise replace the attested bundle between
    # the early preflight and the actual cutover.
    require_recent_verified_backup_record || \
        abort_cutover 'code-only recovery precondition failed; rerun the verified daily backup or use full deployment' 4
fi

if [ -f "$BASE/current/.env" ]; then
    install -m 0640 -o root -g finance-radar "$BASE/current/.env" "$RELEASE/.env"
else
    install -m 0640 -o root -g finance-radar "$SOURCE_ENV" "$RELEASE/.env"
fi
if [ "$DEPLOY_MODE" = full ]; then
    chown -R finance-radar:finance-radar "$SHARED/data" "$SHARED/reports"
fi

DIRECT_ENDPOINT_TEMPLATE="$RELEASE/deployment/systemd/nginx-radar-direct.conf"
DIRECT_ENDPOINT_CANDIDATE="/tmp/finance-radar-nginx-$RELEASE_ID.conf"
DIRECT_ENDPOINT_INSTALLER="$RELEASE/deployment/systemd/install_direct_endpoint.sh"
DIRECT_ENDPOINT_HOOK="$RELEASE/deployment/systemd/certbot-reload-nginx.sh"
for required_file in "$DIRECT_ENDPOINT_TEMPLATE" "$DIRECT_ENDPOINT_INSTALLER" "$DIRECT_ENDPOINT_HOOK"; do
    [ -f "$required_file" ] || abort_cutover "required edge deployment file missing: $required_file" 4
done
sed \
    -e "s/__FINANCE_RADAR_DOMAIN__/$PUBLIC_EDGE_HOST/g" \
    -e "s/__FINANCE_RADAR_PORT__/$PUBLIC_EDGE_PORT/g" \
    "$DIRECT_ENDPOINT_TEMPLATE" > "$DIRECT_ENDPOINT_CANDIDATE"
chmod 0600 "$DIRECT_ENDPOINT_CANDIDATE"
if grep -Eq '__FINANCE_RADAR_(DOMAIN|PORT)__' "$DIRECT_ENDPOINT_CANDIDATE"; then
    abort_cutover "versioned Nginx candidate still contains an unresolved placeholder" 4
fi
CANDIDATE_SERVER_NAME="$(awk '$1 == "server_name" { sub(/;$/, "", $2); print $2; exit }' "$DIRECT_ENDPOINT_CANDIDATE")"
[ "$CANDIDATE_SERVER_NAME" = "$PUBLIC_EDGE_HOST" ] || \
    abort_cutover "public Web host does not match the versioned Nginx candidate" 4
if ! grep -Eq "^[[:space:]]*listen[[:space:]]+$PUBLIC_EDGE_PORT[[:space:]]+ssl;" "$DIRECT_ENDPOINT_CANDIDATE"; then
    abort_cutover "public Web port does not match the versioned Nginx candidate" 4
fi

if [ "$DEPLOY_MODE" = full ]; then
    if [ ! -x "$BASE/venv/bin/python" ]; then
        VENV_CREATED=1
        python3 -m venv "$BASE/venv"
    fi
    "$BASE/venv/bin/python" -m pip install --upgrade pip
    "$BASE/venv/bin/python" -m pip install --require-hashes -r "$RELEASE/requirements.lock"
    # pip runs as root during installation. With the deployment umask, newly
    # installed packages would otherwise be unreadable to the unprivileged
    # service account and could silently force the model into its fallback.
    chown -R finance-radar:finance-radar "$BASE/venv"
else
    "$BASE/venv/bin/python" -m pip check
fi
runuser -u finance-radar -- "$BASE/venv/bin/python" -c \
    'import sklearn, sklearn.pipeline; assert sklearn.__version__ == "1.8.0"'
grant_public_web_runtime_access || \
    abort_cutover 'public Web runtime access boundary could not be prepared' 4
assert_private_runtime_import_boundary || \
    abort_cutover 'private runtime cannot import candidate application from its service working directory' 4
assert_public_runtime_import_boundary || \
    abort_cutover 'public Web runtime cannot import candidate application from its service working directory' 4

if [ ! -f /etc/finance-radar.env ]; then
    ADMIN_TOKEN=$(openssl rand -hex 32)
    REVIEWER_TOKEN=$(openssl rand -hex 32)
    OPERATOR_TOKEN=$(openssl rand -hex 32)
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
        "FINANCE_RADAR_REVIEWER_TOKEN=$REVIEWER_TOKEN" \
        "FINANCE_RADAR_OPERATOR_TOKEN=$OPERATOR_TOKEN" \
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
if ! grep -q '^FINANCE_RADAR_REVIEWER_TOKEN=' /etc/finance-radar.env; then
    printf 'FINANCE_RADAR_REVIEWER_TOKEN=%s\n' "$(openssl rand -hex 32)" >> /etc/finance-radar.env
fi
if ! grep -q '^FINANCE_RADAR_OPERATOR_TOKEN=' /etc/finance-radar.env; then
    printf 'FINANCE_RADAR_OPERATOR_TOKEN=%s\n' "$(openssl rand -hex 32)" >> /etc/finance-radar.env
fi
# Human-review principals are deliberately separate from the shared Reviewer
# UI access token.  An empty array keeps label submission fail-closed until an
# operator installs an owner-approved, per-person credential file.
if [ ! -e /etc/finance-radar-reviewer-principals.json ] && \
   [ ! -L /etc/finance-radar-reviewer-principals.json ]; then
    install -m 0600 -o root -g root /dev/null \
        /etc/finance-radar-reviewer-principals.json
    printf '%s\n' '[]' > /etc/finance-radar-reviewer-principals.json
fi
[ -f /etc/finance-radar-reviewer-principals.json ] && \
    [ ! -L /etc/finance-radar-reviewer-principals.json ] || \
    abort_cutover 'reviewer principals credential must be a regular file' 4
chown root:root /etc/finance-radar-reviewer-principals.json
chmod 0600 /etc/finance-radar-reviewer-principals.json
python3 - /etc/finance-radar-reviewer-principals.json <<'PY'
import hashlib
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, list):
    raise SystemExit("reviewer principals credential must contain a JSON array")
principal_ids = set()
token_hashes = set()
for item in payload:
    if not isinstance(item, dict) or set(item) != {"principal_id", "role", "token"}:
        raise SystemExit("each reviewer principal must contain exactly principal_id, role and token")
    principal_id = str(item["principal_id"]).strip()
    role = str(item["role"]).strip().upper()
    token = str(item["token"]).strip()
    principal_key = principal_id.casefold()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if len(principal_id) < 3 or role not in {"REVIEWER", "ARBITER"} or len(token) < 24:
        raise SystemExit("reviewer principal values violate the runtime contract")
    if principal_key in principal_ids or token_hash in token_hashes:
        raise SystemExit("reviewer principal IDs and tokens must be unique")
    principal_ids.add(principal_key)
    token_hashes.add(token_hash)
PY
if grep -q '^FINANCE_RADAR_RELEASE_ID=' /etc/finance-radar.env; then
    sed -i "s#^FINANCE_RADAR_RELEASE_ID=.*#FINANCE_RADAR_RELEASE_ID=$RELEASE_ID#" \
        /etc/finance-radar.env
else
    printf 'FINANCE_RADAR_RELEASE_ID=%s\n' "$RELEASE_ID" >> /etc/finance-radar.env
fi

# The public Streamlit process receives a deliberately minimal environment.
# Never derive this file by copying or filtering /etc/finance-radar.env: that
# file contains the administrator token and may also contain provider secrets.
install -m 0600 -o finance-radar-web -g finance-radar-web /dev/null /etc/finance-radar-public.env
printf '%s\n' \
    'FINANCE_RADAR_API_URL=http://127.0.0.1:18000' \
    'FINANCE_RADAR_UI_ROLE=public' \
    'FINANCE_RADAR_SHOW_DEBUG=0' \
    > /etc/finance-radar-public.env

install -m 0644 "$RELEASE/deployment/systemd/finance-radar-api.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-overview-snapshot.service" \
    /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-overview-snapshot.timer" \
    /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-market.service" \
    /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-market.timer" \
    /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-web.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-admin.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-reviewer.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-operator.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-worker.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar.slice" /etc/systemd/system/
# Keep an operator-installed Telegram sender override, but refresh it from the
# release so it cannot re-enable autonomous formal light verification after the
# base worker was safely changed to --no-light-verify.
if [ -f /etc/systemd/system/finance-radar-worker.service.d/telegram-send.conf ]; then
    install -d -m 0755 /etc/systemd/system/finance-radar-worker.service.d
    install -m 0644 "$RELEASE/deployment/systemd/finance-radar-worker-send.conf" \
        /etc/systemd/system/finance-radar-worker.service.d/telegram-send.conf
fi
install_backup_quiesce_wrapper || \
    abort_cutover 'candidate backup quiesce wrapper could not be installed' 4
install -d -m 0700 -o root -g root "$RECOVERY_HOLD_PARENT" || \
    abort_cutover 'root backup attestation directory could not be prepared' 4
install -d -m 0755 -o root -g root /var/lib/finance-radar-health || \
    abort_cutover 'public backup health receipt directory could not be prepared' 4
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-backup.service" /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-backup.timer" /etc/systemd/system/
if [ -f "$RELEASE/deployment/systemd/finance-radar-evidence-llm.service" ]; then
    install -m 0644 "$RELEASE/deployment/systemd/finance-radar-evidence-llm.service" \
        /etc/systemd/system/
fi
# Install the bounded external interpretation units without enabling them.
# Activation requires a separately provisioned root-only credential and an
# explicit operator decision after shadow validation.
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-capture-interpretation.service" \
    /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-capture-interpretation.timer" \
    /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/radarqwen.slice" \
    /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-qwen-risk-model.service" \
    /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-qwen-risk-worker.service" \
    /etc/systemd/system/
install -m 0644 "$RELEASE/deployment/systemd/finance-radar-qwen-risk-worker.timer" \
    /etc/systemd/system/
remove_legacy_managed_property_dropins || \
    abort_cutover 'refusing to replace an unrecognized Finance Radar memory override' 4
remove_legacy_backup_retention_dropin || \
    abort_cutover 'refusing to replace an unrecognized Finance Radar backup override' 4
systemctl daemon-reload
assert_bounded_backup_unit || \
    abort_cutover 'candidate backup service memory budget is not effective' 4

if systemctl is-active --quiet finance-radar-admin; then
    abort_cutover 'finance-radar-admin is active; stop the manual loopback session before cutover' 5
fi
if systemctl is-active --quiet finance-radar-reviewer finance-radar-operator; then
    abort_cutover 'a scoped internal UI is active; stop reviewer/operator sessions before cutover' 5
fi
if systemctl is-active --quiet finance-radar-backup.service; then
    abort_cutover 'finance-radar-backup.service is active; wait for the verified backup to finish before cutover' 5
fi

# The only point at which the running release changes. Everything before this
# line was validated against the candidate and snapshotted for automatic
# rollback. Keep the failed release on disk for forensic inspection.
# A full deployment keeps the mutable collector quiesced until the candidate's
# recovery receipt succeeds. A code-only deployment proves database schema
# owners and dependencies unchanged and deliberately keeps collection live until this
# short activation window.  Stop it only now so the candidate overview can be
# built immediately and no process remains attached to the predecessor release.
# API/Web stay available until their controlled restart below in either mode.
if [ "$DEPLOY_MODE" = full ]; then
    systemctl is-active --quiet finance-radar-worker && \
        abort_cutover 'worker unexpectedly restarted during protected cutover' 5
else
    systemctl stop finance-radar-worker || \
        abort_cutover 'worker failed to stop for the short code-only activation window' 5
    systemctl is-active --quiet finance-radar-worker && \
        abort_cutover 'worker remains active during the short code-only activation window' 5
    capture_code_only_schema_receipt || \
        abort_cutover 'unable to capture the pre-activation live schema receipt' 5
fi
systemctl stop finance-radar-evidence-llm.service 2>/dev/null || true
systemctl disable finance-radar-evidence-llm.service
CUTOVER_STARTED=1
ln -sfn "$RELEASE" "$BASE/current"
# Publish the candidate's complete overview while the previous API/Web are
# still serving.  Only after the atomic data file exists do we restart the
# request process, so a multi-minute ledger aggregation cannot become public
# downtime.
systemctl start finance-radar-overview-snapshot.service || \
    abort_cutover 'external overview snapshot publication failed before API restart' 6
systemctl enable finance-radar-api finance-radar-overview-snapshot.service \
    finance-radar-overview-snapshot.timer finance-radar-market.timer \
    finance-radar-web finance-radar-worker \
    finance-radar-backup.timer
systemctl restart finance-radar-api finance-radar-web
systemctl start finance-radar-overview-snapshot.timer

wait_for_url() {
    local url="$1"
    # The overview projection is intentionally precomputed before FastAPI
    # declares startup complete. Production ledgers can take around 45
    # seconds to build after a cold restart, so a 30-second activation probe
    # creates a false rollback even though Uvicorn becomes healthy moments
    # later. Keep the cutover fail-closed, but allow the measured cold-start
    # envelope plus headroom.
    local attempts=${2:-90}
    local _
    for _ in $(seq 1 "$attempts"); do
        if curl -fsS --max-time 5 "$url" >/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

assert_effective_slice_budget() {
    local properties
    properties="$(systemctl show finance-radar.slice \
        -p MemoryHigh -p MemoryMax -p MemorySwapMax -p MemoryCurrent -p TasksMax)" || return 1
    printf '%s\n' "$properties"
    python3 - "$properties" <<'PY'
from __future__ import annotations

import re
import sys

expected = {
    "MemoryHigh": 600 * 1024 * 1024,
    "MemoryMax": 700 * 1024 * 1024,
    "MemorySwapMax": 384 * 1024 * 1024,
    "TasksMax": 256,
}
values = {}
for line in sys.argv[1].splitlines():
    key, separator, value = line.partition("=")
    if separator:
        values[key] = value.strip()

def numeric(value: str) -> int:
    match = re.fullmatch(r"(\d+)([KMG])?", value)
    if not match:
        raise SystemExit(f"systemd property is not numeric: {value!r}")
    number = int(match.group(1))
    suffix = match.group(2)
    return number * {None: 1, "K": 1024, "M": 1024**2, "G": 1024**3}[suffix]

for key, required in expected.items():
    if numeric(values.get(key, "")) != required:
        raise SystemExit(f"unexpected {key}: {values.get(key)!r}")
if numeric(values.get("MemoryCurrent", "")) < 0:
    raise SystemExit("MemoryCurrent is invalid")
PY
}

assert_active_service_cgroup() {
    local unit="$1" slice group events
    slice="$(systemctl show "$unit" -p Slice --value)" || return 1
    [ "$slice" = finance-radar.slice ] || {
        printf 'service is outside the aggregate Radar slice: %s slice=%s\n' "$unit" "$slice" >&2
        return 1
    }
    group="$(systemctl show "$unit" -p ControlGroup --value)" || return 1
    # A dashed slice name is hierarchical in systemd: finance-radar.slice is
    # a child of finance.slice, so its cgroup lives below
    # /finance.slice/finance-radar.slice rather than /system.slice.  Keep the
    # explicit Slice property check above as the ownership boundary, then
    # validate the canonical cgroup-v2 hierarchy here.
    [[ "$group" == /finance.slice/finance-radar.slice/* ]] || {
        printf 'service has an unexpected control group: %s group=%s\n' "$unit" "$group" >&2
        return 1
    }
    events="/sys/fs/cgroup$group/memory.events"
    [ -r "$events" ] || {
        printf 'service cgroup memory events are unavailable: %s\n' "$events" >&2
        return 1
    }
    if awk '$1 == "oom" || $1 == "oom_kill" { if ($2 != 0) exit 1 }' "$events"; then
        printf 'service_cgroup=PASS unit=%s group=%s\n' "$unit" "$group"
    else
        printf 'service cgroup has OOM events: unit=%s events=%s\n' "$unit" "$events" >&2
        return 1
    fi
}

assert_public_web_identity_and_boundary() {
    local user group protect_proc proc_subset
    user="$(systemctl show finance-radar-web -p User --value)" || return 1
    group="$(systemctl show finance-radar-web -p Group --value)" || return 1
    protect_proc="$(systemctl show finance-radar-web -p ProtectProc --value)" || return 1
    proc_subset="$(systemctl show finance-radar-web -p ProcSubset --value)" || return 1
    if [ "$user" != finance-radar-web ] || [ "$group" != finance-radar-web ] || \
       [ "$protect_proc" != invisible ] || [ "$proc_subset" != pid ]; then
        printf 'public Web service identity/isolation is not effective: user=%s group=%s ProtectProc=%s ProcSubset=%s\n' \
            "$user" "$group" "$protect_proc" "$proc_subset" >&2
        return 1
    fi
    runuser -u finance-radar-web -- test -r /etc/finance-radar-public.env || return 1
    if runuser -u finance-radar-web -- test -r /etc/finance-radar.env || \
       runuser -u finance-radar-web -- test -r "$RELEASE/.env" || \
       runuser -u finance-radar-web -- test -r "$SHARED/data/finance_radar.sqlite3" || \
       runuser -u finance-radar-web -- test -r "$SHARED/reports"; then
        printf 'public Web Unix identity can read a private Radar path\n' >&2
        return 1
    fi
    runuser -u finance-radar-web -- test -r "$RELEASE/app/web/Home.py" || return 1
    runuser -u finance-radar-web -- test -r "$RELEASE/.streamlit/config.toml" || return 1
    runuser -u finance-radar-web -- test -x "$BASE/venv/bin/python" || return 1
    assert_public_runtime_import_boundary || return 1
    printf 'public_web_identity=PASS user=%s ProtectProc=%s ProcSubset=%s\n' \
        "$user" "$protect_proc" "$proc_subset"
}

assert_edge_status() {
    local path="$1"
    local expected="$2"
    local status
    if ! status=$(curl --noproxy '*' --silent --show-error --output /dev/null --write-out '%{http_code}' \
        --max-time 20 --resolve "$PUBLIC_EDGE_HOST:$PUBLIC_EDGE_PORT:127.0.0.1" \
        "$PUBLIC_EDGE_ORIGIN$path"); then
        return 1
    fi
    [ "$status" = "$expected" ]
}

wait_for_url http://127.0.0.1:18000/api/v1/live || \
    abort_cutover 'API health check failed after activation' 6
wait_for_url http://127.0.0.1:18000/api/v1/overview || \
    abort_cutover 'published overview is unavailable after activation' 6
wait_for_url http://127.0.0.1:18501/radar/_stcore/health || \
    abort_cutover 'public Web loopback health check failed after activation' 6
capture_code_only_schema_receipt || \
    abort_cutover 'code-only activation changed the live database schema' 6
systemctl is-active --quiet finance-radar-api finance-radar-web
systemctl is-active --quiet finance-radar-overview-snapshot.timer
test "$(systemctl show finance-radar-overview-snapshot.service -p Result --value)" = success || \
    abort_cutover 'overview snapshot service did not finish successfully' 6
assert_effective_slice_budget || \
    abort_cutover 'aggregate Finance Radar memory budget is not effective after activation' 6
assert_active_service_cgroup finance-radar-api || \
    abort_cutover 'API cgroup is not protected by the aggregate budget' 6
assert_active_service_cgroup finance-radar-web || \
    abort_cutover 'Web cgroup is not protected by the aggregate budget' 6
assert_public_web_identity_and_boundary || \
    abort_cutover 'public Web identity or private-path isolation is not effective' 6
if systemctl is-active --quiet finance-radar-admin; then
    abort_cutover 'finance-radar-admin became active during cutover' 6
fi
if systemctl is-active --quiet finance-radar-reviewer finance-radar-operator; then
    abort_cutover 'a scoped internal UI became active during cutover' 6
fi

# Treat the public edge as part of the release rather than a follow-up manual
# step. The candidate installer validates Nginx, reloads it atomically and
# restores its own immediate backup on failure; our outer transaction also
# restores the previous Nginx file and renewal hook with the application.
# From this point, the previous vhost may be moved before the candidate is
# installed.  Mark the edge as touched first so any later failure reloads the
# outer transaction's restored Nginx snapshot.
EDGE_TOUCHED=1
retire_known_predecessor_vhost || \
    abort_cutover 'unable to retire the previous Finance Radar Nginx vhost safely' 6
write_public_release_marker ACTIVATING || \
    abort_cutover 'unable to publish the activating release state' 6
bash "$DIRECT_ENDPOINT_INSTALLER" "$DIRECT_ENDPOINT_CANDIDATE" "$DIRECT_ENDPOINT_HOOK"
assert_candidate_vhost_owns_public_edge || \
    abort_cutover 'candidate Nginx vhost does not exclusively own the public edge' 6
assert_public_release_marker ACTIVATING || \
    abort_cutover 'public edge does not serve the activating release state' 6
for denied_path in \
    /finance-radar-api/ \
    /radar/offhost-status.json \
    /radar-admin/ \
    /radar-review/ \
    /radar-ops/ \
    /radar/Event_Intelligence \
    '/radar/?_page=Operations_and_Model'; do
    assert_edge_status "$denied_path" 404 || \
        abort_cutover "public edge deny check failed for $denied_path" 6
done

# Do not mark this release active until the newly installed backup service has
# emitted a complete, two-database recovery bundle and that bundle has passed
# its independent manifest, restore and audit-consistency checks.
if [ "$DEPLOY_MODE" = full ]; then
    require_postcutover_verified_backup || \
        abort_cutover 'postcutover full recovery backup validation failed after activation' 6
    RECOVERY_BASIS=new_verified_full
    POSTDEPLOY_FULL_BUNDLE_STATUS=VERIFIED
else
    RECOVERY_BASIS=reused_verified_daily
    POSTDEPLOY_FULL_BUNDLE_STATUS=REUSED_VERIFIED_DAILY
fi
if [ "$DEPLOY_MODE" = full ]; then
    systemctl start finance-radar-worker || \
        abort_cutover 'worker failed to start after the protected postcutover backup' 6
else
    # The fast path kept the active worker alive while validating and preparing
    # the schema-neutral candidate, then stopped it only for atomic activation.
    systemctl start finance-radar-worker || \
        abort_cutover 'worker failed to start after the short code-only activation window' 6
fi
systemctl start finance-radar-market.timer || \
    abort_cutover 'market observation timer failed to start after the protected postcutover backup' 6
systemctl is-active --quiet finance-radar-api finance-radar-web finance-radar-worker || \
    abort_cutover 'required services are not active after protected postcutover backup' 6
systemctl is-active --quiet finance-radar-overview-snapshot.timer || \
    abort_cutover 'overview snapshot timer is not active after protected postcutover backup' 6
systemctl is-active --quiet finance-radar-market.timer || \
    abort_cutover 'market observation timer is not active after protected postcutover backup' 6
assert_active_service_cgroup finance-radar-worker || \
    abort_cutover 'worker cgroup is not protected by the aggregate budget' 6
if systemctl is-active --quiet finance-radar-evidence-llm.service || \
   systemctl is-enabled --quiet finance-radar-evidence-llm.service; then
    abort_cutover 'advisory evidence LLM must remain stopped and disabled after deployment' 6
fi
clear_worker_resume_inhibit || \
    abort_cutover 'worker resumed but protected-cutover inhibit marker could not be cleared' 6

# Keep the persistent daily timer stopped throughout validation and the
# explicit post-cutover backup.  Starting it only after the recovery point,
# public edge and worker are settled prevents retention=1 from racing the
# activation transaction.  If a scheduled run became due while stopped,
# systemd may launch it asynchronously now as the normal daily job.
systemctl start finance-radar-backup.timer || \
    abort_cutover 'daily backup timer failed to resume after activation checks' 6
systemctl is-active --quiet finance-radar-backup.timer || \
    abort_cutover 'daily backup timer is not active after activation checks' 6

install -d -m 0750 -o root -g finance-radar "$RELEASE_RECORDS"
ACTIVATION_PENDING="$RELEASE_RECORDS/.ACTIVATION.pending.$$"
[ ! -e "$RELEASE_RECORDS/ACTIVATION.txt" ] && \
    [ ! -L "$RELEASE_RECORDS/ACTIVATION.txt" ] || \
    abort_cutover 'activation record target already exists or is unsafe' 6
[ ! -e "$ACTIVATION_PENDING" ] && [ ! -L "$ACTIVATION_PENDING" ] || \
    abort_cutover 'activation pending record path is unsafe' 6
install -m 0640 -o root -g finance-radar /dev/null "$ACTIVATION_PENDING"
printf 'activation_state=ACCEPTED\nactivation=PASS\nrelease=%s\nprevious_release=%s\npublic_web=%s\ndeploy_mode=%s\nrecovery_basis=%s\npredeploy_backup_run_id=%s\npredeploy_backup_snapshot_id=%s\npredeploy_backup_format=%s\npredeploy_backup_receipt_sha256=%s\npostdeploy_backup_run_id=%s\npostdeploy_backup_snapshot_id=%s\npostdeploy_backup_manifest_sha256=%s\npostdeploy_full_bundle=%s\nservices=active\noverview_snapshot_timer=active\nbackup_timer=active\nnginx_edge=PASS\n' \
    "$RELEASE_ID" "${PREVIOUS_RELEASE:-none}" "$PUBLIC_WEB_URL" \
    "$DEPLOY_MODE" "$RECOVERY_BASIS" \
    "${PREDEPLOY_BACKUP_RUN_ID:-not-recorded}" \
    "$PREDEPLOY_BACKUP_ID" "$PREDEPLOY_BACKUP_KIND" "$PREDEPLOY_BACKUP_RECEIPT_SHA256" \
    "${POSTDEPLOY_BACKUP_RUN_ID:-not-recorded}" \
    "$POSTDEPLOY_BACKUP_ID" "$POSTDEPLOY_BACKUP_MANIFEST_SHA256" "$POSTDEPLOY_FULL_BUNDLE_STATUS" \
    > "$ACTIVATION_PENDING"
# The atomic rename is the deployment commit point. Before it, every failure
# rolls back and the protected hold remains. Afterwards, failure to delete the
# now-redundant hold is safe housekeeping: retain the extra recovery copy and
# report it, but never leave a rolled-back release carrying activation=PASS.
mv -f -- "$ACTIVATION_PENDING" "$RELEASE_RECORDS/ACTIVATION.txt" || \
    abort_cutover 'atomic activation record commit failed' 6
ACTIVATION_RECORD_COMMITTED=1
[ -f "$RELEASE_RECORDS/ACTIVATION.txt" ] && \
    [ ! -L "$RELEASE_RECORDS/ACTIVATION.txt" ] && \
    grep -Fx 'activation_state=ACCEPTED' "$RELEASE_RECORDS/ACTIVATION.txt" >/dev/null && \
    grep -Fx 'activation=PASS' "$RELEASE_RECORDS/ACTIVATION.txt" >/dev/null && \
    grep -Fx "release=$RELEASE_ID" "$RELEASE_RECORDS/ACTIVATION.txt" >/dev/null && \
    grep -Fx 'services=active' "$RELEASE_RECORDS/ACTIVATION.txt" >/dev/null || {
        abort_cutover 'committed activation record failed validation' 6
    }
# Resume provider interpretation last.  A Persistent timer can immediately
# launch a missed run, so keep it quiescent until the activation record itself
# has been atomically committed and validated.  If restoration fails, remove
# that record and execute the normal rollback transaction.
if ! restore_capture_interpretation_runtime; then
    abort_cutover 'capture interpretation timer state could not be restored after activation checks' 6
fi
if ! restore_qwen_risk_runtime; then
    abort_cutover 'Qwen risk timer state could not be restored after activation checks' 6
fi
# Recheck every public runtime and persistent timer after the activation receipt
# and optional interpretation timer have settled. The accepted public marker is
# the last deployment mutation: no candidate release_id is advertised as final
# before these checks and the private receipt all succeed.
systemctl is-active --quiet finance-radar-api finance-radar-web finance-radar-worker || \
    abort_cutover 'required services are not active at final acceptance' 6
systemctl is-active --quiet finance-radar-overview-snapshot.timer \
    finance-radar-market.timer finance-radar-backup.timer || \
    abort_cutover 'required timers are not active at final acceptance' 6
systemctl is-enabled --quiet finance-radar-api finance-radar-web finance-radar-worker \
    finance-radar-overview-snapshot.timer finance-radar-market.timer \
    finance-radar-backup.timer || \
    abort_cutover 'required services or timers are not enabled at final acceptance' 6
write_public_release_marker ACCEPTED || \
    abort_cutover 'unable to publish the accepted release state' 6
assert_public_release_marker ACCEPTED || \
    abort_cutover 'public edge does not serve the accepted release state' 6
trap - ERR
if [ "$DEPLOY_MODE" = full ]; then
    if ! clear_predeploy_backup_hold; then
        printf 'activation_warning=predeploy_hold_cleanup_failed retained=%s\n' \
            "${PREDEPLOY_HOLD_ROOT:-unknown}" >&2
    fi
fi
[[ "$ROLLBACK_DIR" == /var/tmp/finance-radar-install-* ]] || exit 70
rm -rf -- "$ROLLBACK_DIR"

printf 'activation_state=ACCEPTED\nactivation=PASS\nrelease=%s\nprevious_release=%s\npublic_web=%s\ndeploy_mode=%s\nrecovery_basis=%s\npredeploy_backup_run_id=%s\npredeploy_backup_snapshot_id=%s\npredeploy_backup_format=%s\npredeploy_backup_receipt_sha256=%s\npostdeploy_backup_run_id=%s\npostdeploy_backup_snapshot_id=%s\npostdeploy_backup_manifest_sha256=%s\npostdeploy_full_bundle=%s\nnginx_edge=PASS\n' \
    "$RELEASE" "${PREVIOUS_RELEASE:-none}" "$PUBLIC_WEB_URL" \
    "$DEPLOY_MODE" "$RECOVERY_BASIS" \
    "${PREDEPLOY_BACKUP_RUN_ID:-not-recorded}" \
    "$PREDEPLOY_BACKUP_ID" "$PREDEPLOY_BACKUP_KIND" "$PREDEPLOY_BACKUP_RECEIPT_SHA256" \
    "${POSTDEPLOY_BACKUP_RUN_ID:-not-recorded}" \
    "$POSTDEPLOY_BACKUP_ID" "$POSTDEPLOY_BACKUP_MANIFEST_SHA256" "$POSTDEPLOY_FULL_BUNDLE_STATUS"
