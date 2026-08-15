from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


INSTALLER = Path(__file__).parents[1] / "deployment" / "systemd" / "install_remote.sh"
VALIDATOR = Path(__file__).parents[1] / "deployment" / "systemd" / "verify_backup_receipt.py"
HOLD_TRANSFER = (
    Path(__file__).parents[1]
    / "deployment"
    / "systemd"
    / "transfer_verified_backup_hold.py"
)


def _bash() -> str:
    candidates = [
        # Avoid the Microsoft Store WindowsApps launcher when WSL is absent.
        "C:/Program Files/Git/bin/bash.exe",
        shutil.which("bash"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("bash is required to exercise the systemd installer gate")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _predeploy_gate_source() -> str:
    source = INSTALLER.read_text(encoding="utf-8")
    start = source.index('PREDEPLOY_BACKUP_ID=""')
    end = source.index("# Preserve the previous release intact", start)
    return source[start:end]


def _predeploy_hold_source() -> str:
    source = INSTALLER.read_text(encoding="utf-8")
    start = source.index("create_predeploy_backup_hold() {")
    end = source.index("clear_predeploy_backup_hold() {", start)
    return source[start:end]


def _supports_descriptor_relative_hold() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and bool(os.supports_dir_fd)
        and getattr(os, "geteuid", lambda: -1)() == 0
    )


def _make_gate_driver(tmp_path: Path, *, entrypoint: str = "predeploy") -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    python_executable = Path(sys.executable).as_posix()
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -euo pipefail

create_legacy_sqlite() {
    local target="$1"
    mkdir -p "$(dirname "$target")"
    "$FAKE_PYTHON" - "$target" "$OPS_DB" <<'PY'
import json
import shutil
import sqlite3
import sys

target, operations = map(__import__("pathlib").Path, sys.argv[1:])
ledger_tables = (
    "sources", "raw_observations", "canonical_events", "event_versions",
    "event_evidence", "event_market_metrics",
)
ledger_application_tables = (
    "alert_delivery_attempts", "alert_delivery_cleanup", "alert_delivery_leases", "alert_outbox",
    "assets", "canonical_events", "entities", "event_assessments", "event_asset_impacts",
    "event_chain_members", "event_chains", "event_entities", "event_evidence",
    "event_ledger_schema", "event_market_metrics", "event_observations", "event_review_triage",
    "event_versions", "market_jobs", "market_snapshots", "observation_jobs", "pipeline_jobs",
    "raw_observations", "runtime_leases", "sec_filing_enrichments", "source_cursors",
    "source_revisions", "sources", "telegram_source_channels", "telegram_source_messages",
)
with sqlite3.connect(target) as connection:
    connection.execute("CREATE TABLE event_ledger_schema(version INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO event_ledger_schema VALUES (1)")
    for table in ledger_application_tables:
        if table == "event_ledger_schema":
            continue
        if table == "event_versions":
            connection.execute("CREATE TABLE event_versions(event_id TEXT, version INTEGER, change_reason TEXT)")
            connection.execute("INSERT INTO event_versions VALUES ('evt-1', 1, 'initial_capture')")
        else:
            connection.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
            connection.execute(f"INSERT INTO {table} VALUES ('{table}-1')")
with sqlite3.connect(target) as connection:
    counts = {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ledger_tables}
shutil.copyfile(target, target.parent.parent / "finance_radar.sqlite3")
operations.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(operations) as connection:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS backup_runs("
        "backup_id TEXT, backup_path TEXT, backup_bytes INTEGER, quick_check TEXT,"
        "restored_count_json TEXT, status TEXT, verified_at TEXT)"
    )
    connection.execute(
        "INSERT INTO backup_runs VALUES (?,?,?,?,?,?,?)",
        (target.name, str(target), target.stat().st_size, "ok", json.dumps(counts), "VERIFIED", "2999-01-01T00:00:00+00:00"),
    )
PY
}

create_recovery_bundle() {
    local bundle="$1"
    "$FAKE_PYTHON" - "$bundle" <<'PY'
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

bundle = Path(sys.argv[1])
bundle.mkdir(parents=True, exist_ok=False)
ledger = bundle / "ledger.sqlite3"
operations = bundle / "operations.sqlite3"
evidence = bundle / "evidence"
reports = bundle / "reports"
ledger_tables = (
    "sources", "raw_observations", "canonical_events", "event_versions",
    "event_evidence", "event_market_metrics",
)
ledger_application_tables = (
    "alert_delivery_attempts", "alert_delivery_cleanup", "alert_delivery_leases", "alert_outbox",
    "assets", "canonical_events", "entities", "event_assessments", "event_asset_impacts",
    "event_chain_members", "event_chains", "event_entities", "event_evidence",
    "event_ledger_schema", "event_market_metrics", "event_observations", "event_review_triage",
    "event_versions", "market_jobs", "market_snapshots", "observation_jobs", "pipeline_jobs",
    "raw_observations", "runtime_leases", "sec_filing_enrichments", "source_cursors",
    "source_revisions", "sources", "telegram_source_channels", "telegram_source_messages",
)
operations_tables = (
    "replay_runs", "model_runs", "worker_cycles", "backup_runs", "agent_decisions",
    "light_verification_runs", "formal_mutation_audits", "evidence_objects",
    "human_overrides", "adjudication_samples", "adjudication_reviews",
)
operations_application_tables = (
    "adjudication_reviews", "adjudication_samples", "agent_decisions", "backup_runs",
    "evidence_object_links", "evidence_objects", "formal_mutation_audits", "human_overrides",
    "light_verification_runs", "model_runs", "operations_schema", "replay_runs", "runtime_state",
    "worker_cycles",
)
with sqlite3.connect(ledger) as connection:
    connection.execute("CREATE TABLE event_ledger_schema(version INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO event_ledger_schema VALUES (1)")
    for table in ledger_application_tables:
        if table == "event_ledger_schema":
            continue
        if table == "event_versions":
            connection.execute("CREATE TABLE event_versions(event_id TEXT, version INTEGER, change_reason TEXT)")
            connection.execute("INSERT INTO event_versions VALUES ('evt-1', 1, 'initial_capture')")
        else:
            connection.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
            connection.execute(f"INSERT INTO {table} VALUES ('{table}-1')")
with sqlite3.connect(operations) as connection:
    connection.execute("CREATE TABLE operations_schema(version INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO operations_schema VALUES (1)")
    for table in operations_application_tables:
        if table == "operations_schema":
            continue
        if table == "formal_mutation_audits":
            connection.execute("CREATE TABLE formal_mutation_audits(event_id TEXT, after_version INTEGER, mutation_kind TEXT, state TEXT)")
        elif table == "backup_runs":
            connection.execute("CREATE TABLE backup_runs(id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO backup_runs VALUES ('bundle-backup')")
        else:
            connection.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
            connection.execute(f"INSERT INTO {table} VALUES ('{table}-1')")

evidence.mkdir()
reports.mkdir()
evidence_file = evidence / "proof.txt"
report_file = reports / "cycle.json"
evidence_file.write_text("immutable evidence", encoding="utf-8")
report_file.write_text('{"status":"ok"}', encoding="utf-8")

def entry(path: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(bundle).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }

with sqlite3.connect(ledger) as connection:
    ledger_counts = {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ledger_tables}
with sqlite3.connect(operations) as connection:
    operations_counts = {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in operations_tables}
with sqlite3.connect(ledger) as connection:
    ledger_table_counts = {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ledger_application_tables}
with sqlite3.connect(operations) as connection:
    operations_table_counts = {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in operations_application_tables}

manifest = {
    "format": "finance-radar-recovery-bundle-v1",
    "snapshot_id": bundle.name,
    "created_at": "2999-01-01T00:00:00+00:00",
    "files": [entry(ledger), entry(operations), entry(evidence_file), entry(report_file)],
    "components": {
        "ledger": {"path": "ledger.sqlite3", "source_counts": ledger_counts, "table_counts": ledger_table_counts},
        "operations": {"path": "operations.sqlite3", "bundle_counts": operations_counts, "table_counts": operations_table_counts},
        "evidence": {
            "present": True,
            "path": "evidence",
            "files": 1,
            "bytes": evidence_file.stat().st_size,
            "file_inventory": [entry(evidence_file)],
            "directories": ["."],
            "skipped_symlinks": [],
        },
        "reports": {
            "present": True,
            "path": "reports",
            "files": 1,
            "bytes": report_file.stat().st_size,
            "file_inventory": [entry(report_file)],
            "directories": ["."],
            "skipped_symlinks": [],
        },
    },
}
(bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
PY
}

if [ "$1" = "is-active" ]; then
    exit 1
fi
if [ "$1" = "start" ] && [ "${2:-}" = "finance-radar-backup.service" ]; then
    if [ "${FAKE_BACKUP_START_FAIL:-0}" = "1" ]; then
        exit 70
    fi
    case "${FAKE_BACKUP_FORMAT:-bundle}" in
        bundle)
            create_recovery_bundle "$BACKUP_ROOT/finance_radar_29990101T000000Z_abcdef12"
            ;;
        legacy)
            create_legacy_sqlite "$BACKUP_ROOT/finance_radar_29990101T000000Z.sqlite3"
            ;;
        corrupt_legacy)
            printf 'not a sqlite database\n' \
                > "$BACKUP_ROOT/finance_radar_29990101T000000Z.sqlite3"
            ;;
        ambiguous_legacy)
            create_legacy_sqlite "$BACKUP_ROOT/finance_radar_29990101T000000Z.sqlite3"
            create_legacy_sqlite "$BACKUP_ROOT/finance_radar_29990101T000001Z.sqlite3"
            ;;
        none)
            ;;
        *)
            printf 'unknown fake backup format: %s\n' "${FAKE_BACKUP_FORMAT}" >&2
            exit 71
            ;;
    esac
    exit 0
fi
if [ "$1" = "show" ]; then
    if [ "${2:-}" = "finance-radar-admin" ]; then
        if [ "${FAKE_ADMIN_ENABLED:-0}" = "1" ]; then
            printf 'enabled\\n'
        else
            printf 'static\\n'
        fi
        exit 0
    fi
    if [ "${2:-}" = "finance-radar-backup.service" ]; then
        printf 'success\\n'
        exit 0
    fi
fi
printf 'unexpected systemctl call: %s\\n' "$*" >&2
exit 99
""",
    )
    _write_executable(
        fake_bin / "python3",
        f"#!/usr/bin/env bash\nexec '{python_executable}' \"$@\"\n",
    )
    _write_executable(
        fake_bin / "systemd-run",
        """#!/usr/bin/env bash
set -euo pipefail
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --setenv=*) export "${1#--setenv=}" ;;
    esac
    shift
done
if [ "${1:-}" = bash ] && [[ "${2:-}" == */run_backup_quiesced.sh ]]; then
    "$FAKE_SYSTEMCTL_TOOL" start finance-radar-backup.service
    exit $?
fi
exec "$@"
""",
    )
    _write_executable(
        fake_bin / "install",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"${1:-}\" == -d ]]; then\n"
        "    mode=0755\n"
        "    shift\n"
        "    targets=()\n"
        "    while [[ $# -gt 0 ]]; do\n"
        "        case \"$1\" in\n"
        "            -m) mode=\"$2\"; shift 2 ;;\n"
        "            -o|-g) shift 2 ;;\n"
        "            *) targets+=(\"$1\"); shift ;;\n"
        "        esac\n"
        "    done\n"
        "    for target in \"${targets[@]}\"; do mkdir -p \"$target\"; chmod \"$mode\" \"$target\"; done\n"
        "    exit 0\n"
        "fi\n"
        "printf 'unexpected install call: %s\\n' \"$*\" >&2\n"
        "exit 99\n",
    )
    _write_executable(
        fake_bin / "find",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \" $* \" == *\" -user finance-radar \"* ]]; then\n"
        "    printf '%s\\n' \"$1\"\n"
        "    exit 0\n"
        "fi\n"
        "exec /usr/bin/find \"$@\"\n",
    )
    _write_executable(
        fake_bin / "chown",
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
    )
    _write_executable(
        fake_bin / "runuser",
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
    )
    base = tmp_path / "base"
    release = base / "releases" / "test-hold-release"
    release_verifier = release / "deployment" / "systemd" / "verify_backup_receipt.py"
    release_verifier.parent.mkdir(parents=True)
    shutil.copy2(VALIDATOR, release_verifier)
    shutil.copy2(HOLD_TRANSFER, release_verifier.with_name(HOLD_TRANSFER.name))
    wrapper = release / "deployment" / "systemd" / "run_backup_quiesced.sh"
    _write_executable(wrapper, "#!/usr/bin/env bash\nexit 0\n")
    (release / "app" / "ops").mkdir(parents=True)
    (release / "app" / "ops" / "backup.py").write_text("# synthetic candidate\n", encoding="utf-8")
    python_bin = base / "venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    _write_executable(python_bin, f"#!/usr/bin/env bash\nexec '{python_executable}' \"$@\"\n")
    if entrypoint == "predeploy":
        gate_invocation = (
            "require_predeploy_verified_backup\n"
            'printf "receipt=%s:%s:%s\\n" "$PREDEPLOY_BACKUP_KIND" "$PREDEPLOY_BACKUP_ID" "$PREDEPLOY_BACKUP_RECEIPT_SHA256"\n'
        )
    elif entrypoint == "postdeploy":
        gate_invocation = (
            "require_postcutover_verified_backup\n"
            'printf "receipt=%s:%s\\n" "$POSTDEPLOY_BACKUP_ID" "$POSTDEPLOY_BACKUP_MANIFEST_SHA256"\n'
        )
    elif entrypoint == "hold":
        gate_invocation = (
            "require_predeploy_verified_backup\n"
            "case \"${HOLD_MUTATION:-}\" in\n"
            "    '') ;;\n"
            "    symlink)\n"
            "        mv -- \"$PREDEPLOY_BACKUP_PATH\" \"$PREDEPLOY_BACKUP_PATH.source\"\n"
            "        ln -s \"$(basename \"$PREDEPLOY_BACKUP_PATH.source\")\" \"$PREDEPLOY_BACKUP_PATH\"\n"
            "        ;;\n"
            "    replacement)\n"
            "        mv -- \"$PREDEPLOY_BACKUP_PATH\" \"$PREDEPLOY_BACKUP_PATH.source\"\n"
            "        mkdir \"$PREDEPLOY_BACKUP_PATH\"\n"
            "        ;;\n"
            "    outside)\n"
            "        cp -a -- \"$PREDEPLOY_BACKUP_PATH\" \"$ROOT/outside-backup\"\n"
            "        PREDEPLOY_BACKUP_PATH=\"$ROOT/outside-backup\"\n"
            "        ;;\n"
            "    *) printf 'unknown hold mutation: %s\\n' \"$HOLD_MUTATION\" >&2; exit 97 ;;\n"
            "esac\n"
            + _predeploy_hold_source()
            + 'RECOVERY_HOLD_PARENT="$ROOT/root-hold-parent"\n'
            + 'RECOVERY_HOLD_ROOT="$RECOVERY_HOLD_PARENT/recovery-holds"\n'
            + "create_predeploy_backup_hold\n"
            + 'if [ "${HOLD_AFTER_CREATE_MUTATION:-}" = source-content ]; then printf "tampered\\n" > "$PREDEPLOY_BACKUP_PATH/ledger.sqlite3"; fi\n'
            + 'printf "hold=%s\\n" "$PREDEPLOY_HOLD_PATH"\n'
            + 'cat "$PREDEPLOY_HOLD_ROOT/HOLD_RECEIPT.json"\n'
        )
    else:
        raise ValueError(f"unknown gate entrypoint: {entrypoint}")

    driver = tmp_path / "run-gate.sh"
    driver.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'ROOT="$(cd "$(dirname "$0")" && pwd)"\n'
        'PATH="$ROOT/fake-bin:$PATH"\n'
        'RELEASE_ID="test-hold-release"\n'
        'BASE="$ROOT/base"\n'
        'SHARED="$ROOT/shared"\n'
        'RELEASE="$BASE/releases/$RELEASE_ID"\n'
        'RELEASE_RECORDS="$RELEASE/release-records"\n'
        'BACKUP_ROOT="$SHARED/data/operational_backups"\n'
        'OPS_DB="$SHARED/data/finance_radar_operations.sqlite3"\n'
        'FINANCE_RADAR_BACKUP_RECEIPT_TMPDIR="$ROOT/receipt-tmp"\n'
        'mkdir -p "$FINANCE_RADAR_BACKUP_RECEIPT_TMPDIR"\n'
        'chmod 0700 "$FINANCE_RADAR_BACKUP_RECEIPT_TMPDIR"\n'
        'FAKE_PYTHON="$ROOT/fake-bin/python3"\n'
        'FAKE_SYSTEMCTL_TOOL="$ROOT/fake-bin/systemctl"\n'
        "export BACKUP_ROOT OPS_DB FAKE_PYTHON FINANCE_RADAR_BACKUP_RECEIPT_TMPDIR FAKE_SYSTEMCTL_TOOL\n"
        + _predeploy_gate_source()
        + gate_invocation,
        encoding="utf-8",
        newline="\n",
    )
    driver.chmod(0o755)
    return driver


def test_predeploy_backup_gate_requires_new_bundle_and_records_receipt(tmp_path: Path) -> None:
    driver = _make_gate_driver(tmp_path)

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "predeploy_backup=VERIFIED format=recovery_bundle snapshot_id=finance_radar_29990101T000000Z_abcdef12" in result.stdout
    assert "receipt=recovery_bundle:finance_radar_29990101T000000Z_abcdef12:" in result.stdout
    assert len(result.stdout.rsplit(":", 1)[1].strip()) == 64


def test_predeploy_backup_gate_rejects_a_legacy_only_candidate_bridge(
    tmp_path: Path,
) -> None:
    driver = _make_gate_driver(tmp_path)

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "FAKE_BACKUP_FORMAT": "legacy"},
    )

    assert result.returncode != 0
    assert "predeploy backup service or receipt validation failed" in result.stderr


@pytest.mark.skipif(
    not _supports_descriptor_relative_hold(),
    reason="the deployment hold deliberately requires root Linux descriptor-relative APIs",
)
def test_predeploy_recovery_hold_revalidates_the_held_bundle_before_accepting_it(
    tmp_path: Path,
) -> None:
    driver = _make_gate_driver(tmp_path, entrypoint="hold")

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "predeploy_backup_hold=READY" in result.stdout
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt["kind"] == "recovery_bundle"
    assert receipt["receipt_sha256"] == receipt["held_receipt_sha256"]
    assert Path(receipt["hold_path"]).is_dir()


@pytest.mark.skipif(
    not _supports_descriptor_relative_hold(),
    reason="the deployment hold deliberately requires root Linux descriptor-relative APIs",
)
def test_predeploy_recovery_hold_leaves_normal_retention_and_enters_root_custody(
    tmp_path: Path,
) -> None:
    driver = _make_gate_driver(tmp_path, entrypoint="hold")

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt["kind"] == "recovery_bundle"
    assert receipt["receipt_sha256"] == receipt["held_receipt_sha256"]
    assert receipt["protection"].startswith("root-owned atomic custody transfer")
    assert Path(receipt["hold_path"]).is_dir()
    assert not Path(receipt["original_path"]).exists()


@pytest.mark.skipif(
    not _supports_descriptor_relative_hold(),
    reason="the deployment hold deliberately requires root Linux descriptor-relative APIs",
)
@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        ("symlink", "fresh verified recovery bundle is not a real directory"),
        ("replacement", "predeploy hold receipt validation failed"),
        ("outside", "predeploy source is not a direct operational-backups child"),
    ),
)
def test_predeploy_recovery_hold_rejects_symlink_or_replaced_source(
    tmp_path: Path,
    mutation: str,
    expected_error: str,
) -> None:
    driver = _make_gate_driver(tmp_path, entrypoint="hold")

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "HOLD_MUTATION": mutation},
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


@pytest.mark.parametrize("backup_format", ["corrupt_legacy", "ambiguous_legacy", "none"])
def test_predeploy_backup_gate_rejects_invalid_or_ambiguous_legacy_receipts(
    tmp_path: Path,
    backup_format: str,
) -> None:
    driver = _make_gate_driver(tmp_path)

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "FAKE_BACKUP_FORMAT": backup_format},
    )

    assert result.returncode != 0
    assert "predeploy backup service or receipt validation failed" in result.stderr


def test_postcutover_backup_gate_requires_the_new_complete_recovery_bundle(tmp_path: Path) -> None:
    driver = _make_gate_driver(tmp_path, entrypoint="postdeploy")

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "postcutover_backup=VERIFIED snapshot_id=finance_radar_29990101T000000Z_abcdef12" in result.stdout
    assert "receipt=finance_radar_29990101T000000Z_abcdef12:" in result.stdout


def test_postcutover_backup_gate_rejects_a_legacy_only_receipt(tmp_path: Path) -> None:
    driver = _make_gate_driver(tmp_path, entrypoint="postdeploy")

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "FAKE_BACKUP_FORMAT": "legacy"},
    )

    assert result.returncode != 0
    assert "postcutover full recovery backup service or receipt validation failed" in result.stderr


def test_predeploy_backup_gate_refuses_an_enabled_admin_ui(tmp_path: Path) -> None:
    driver = _make_gate_driver(tmp_path)

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "FAKE_ADMIN_ENABLED": "1"},
    )

    assert result.returncode != 0
    assert "finance-radar-admin is boot-enabled (enabled)" in result.stderr
    backup_root = tmp_path / "shared" / "data" / "operational_backups"
    assert not backup_root.exists()


def test_predeploy_backup_gate_stops_when_the_backup_service_fails(tmp_path: Path) -> None:
    driver = _make_gate_driver(tmp_path)

    result = subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={**os.environ, "FAKE_BACKUP_START_FAIL": "1"},
    )

    assert result.returncode != 0
    assert "predeploy backup service or receipt validation failed" in result.stderr
    backup_root = tmp_path / "shared" / "data" / "operational_backups"
    assert not backup_root.exists()
