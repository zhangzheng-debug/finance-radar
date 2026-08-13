from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).parents[1]
VALIDATOR = ROOT / "deployment" / "systemd" / "verify_backup_receipt.py"
STARTED_AT = "2026-08-05T00:00:00+00:00"
VERIFIED_AT = "2026-08-05T00:01:00+00:00"
LEDGER_TABLES = (
    "sources",
    "raw_observations",
    "canonical_events",
    "event_versions",
    "event_evidence",
    "event_market_metrics",
)
LEDGER_APPLICATION_TABLES = (
    "alert_delivery_attempts",
    "alert_delivery_cleanup",
    "alert_delivery_leases",
    "alert_outbox",
    "assets",
    "canonical_events",
    "entities",
    "event_assessments",
    "event_asset_impacts",
    "event_chain_members",
    "event_chains",
    "event_entities",
    "event_evidence",
    "event_ledger_schema",
    "event_market_metrics",
    "event_observations",
    "event_review_triage",
    "event_versions",
    "market_jobs",
    "market_snapshots",
    "observation_jobs",
    "pipeline_jobs",
    "raw_observations",
    "runtime_leases",
    "sec_filing_enrichments",
    "source_cursors",
    "source_revisions",
    "sources",
    "telegram_source_channels",
    "telegram_source_messages",
)
OPERATIONS_TABLES = (
    "replay_runs",
    "model_runs",
    "worker_cycles",
    "backup_runs",
    "agent_decisions",
    "light_verification_runs",
    "formal_mutation_audits",
    "evidence_objects",
    "human_overrides",
    "adjudication_samples",
    "adjudication_reviews",
)
OPERATIONS_APPLICATION_TABLES = (
    "adjudication_reviews",
    "adjudication_samples",
    "agent_decisions",
    "backup_runs",
    "evidence_object_links",
    "evidence_objects",
    "formal_mutation_audits",
    "human_overrides",
    "light_verification_runs",
    "model_runs",
    "operations_schema",
    "replay_runs",
    "runtime_state",
    "worker_cycles",
)


def _counts(path: Path, tables: tuple[str, ...]) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def _ledger(path: Path, *, populated: bool = True) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE event_ledger_schema(version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO event_ledger_schema VALUES (1)")
        for table in LEDGER_APPLICATION_TABLES:
            if table == "event_ledger_schema":
                continue
            if table == "event_versions":
                connection.execute(
                    "CREATE TABLE event_versions(event_id TEXT, version INTEGER, change_reason TEXT)"
                )
                if populated:
                    connection.execute(
                        "INSERT INTO event_versions VALUES ('evt-1', 1, 'initial_capture')"
                    )
            else:
                connection.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
                if populated:
                    connection.execute(f"INSERT INTO {table} VALUES ('{table}-1')")


def _operations(path: Path, *, populated: bool = True) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE operations_schema(version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO operations_schema VALUES (1)")
        for table in OPERATIONS_APPLICATION_TABLES:
            if table == "operations_schema":
                continue
            if table == "formal_mutation_audits":
                connection.execute(
                    "CREATE TABLE formal_mutation_audits("
                    "event_id TEXT, after_version INTEGER, mutation_kind TEXT, state TEXT)"
                )
            elif table == "backup_runs":
                connection.execute("CREATE TABLE backup_runs(id TEXT PRIMARY KEY)")
                if populated:
                    connection.execute("INSERT INTO backup_runs VALUES ('bundle-backup')")
            else:
                connection.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
                if populated:
                    connection.execute(f"INSERT INTO {table} VALUES ('{table}-1')")


def _entry(bundle: Path, path: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(bundle).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _bundle(root: Path, *, manifest_count_delta: int = 0) -> Path:
    bundle = root / "finance_radar_20260805T000100Z_abc12345"
    bundle.mkdir()
    ledger = bundle / "ledger.sqlite3"
    operations = bundle / "operations.sqlite3"
    evidence = bundle / "evidence"
    reports = bundle / "reports"
    _ledger(ledger)
    _operations(operations)
    evidence.mkdir()
    reports.mkdir()
    evidence_file = evidence / "proof.txt"
    report_file = reports / "cycle.json"
    evidence_file.write_text("immutable evidence", encoding="utf-8")
    report_file.write_text('{"status":"ok"}', encoding="utf-8")
    ledger_counts = _counts(ledger, LEDGER_TABLES)
    ledger_counts["canonical_events"] += manifest_count_delta
    manifest = {
        "format": "finance-radar-recovery-bundle-v1",
        "snapshot_id": bundle.name,
        "created_at": VERIFIED_AT,
        "files": [
            _entry(bundle, ledger),
            _entry(bundle, operations),
            _entry(bundle, evidence_file),
            _entry(bundle, report_file),
        ],
        "components": {
            "ledger": {
                "path": "ledger.sqlite3",
                "source_counts": ledger_counts,
                "table_counts": _counts(ledger, LEDGER_APPLICATION_TABLES),
            },
            "operations": {
                "path": "operations.sqlite3",
                "bundle_counts": _counts(operations, OPERATIONS_TABLES),
                "table_counts": _counts(operations, OPERATIONS_APPLICATION_TABLES),
            },
            "evidence": {
                "present": True,
                "path": "evidence",
                "files": 1,
                "bytes": evidence_file.stat().st_size,
                "file_inventory": [_entry(bundle, evidence_file)],
                "directories": ["."],
                "skipped_symlinks": [],
            },
            "reports": {
                "present": True,
                "path": "reports",
                "files": 1,
                "bytes": report_file.stat().st_size,
                "file_inventory": [_entry(bundle, report_file)],
                "directories": ["."],
                "skipped_symlinks": [],
            },
        },
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def _legacy_operations(path: Path, backup_path: Path, counts: dict[str, int], *, verified_at: str = VERIFIED_AT, bytes_delta: int = 0) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE backup_runs(
                backup_id TEXT, backup_path TEXT, backup_bytes INTEGER, quick_check TEXT,
                restored_count_json TEXT, status TEXT, verified_at TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO backup_runs VALUES (?,?,?,?,?,?,?)",
            (
                "legacy-backup-1",
                str(backup_path),
                backup_path.stat().st_size + bytes_delta,
                "ok",
                json.dumps(counts, sort_keys=True),
                "VERIFIED",
                verified_at,
            ),
        )


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _inventory(root: Path) -> Path:
    inventory = root / "before.json"
    result = _run("inventory", "--backup-root", str(root), "--output", str(inventory))
    assert result.returncode == 0, result.stderr
    return inventory


def test_receipt_accepts_a_fresh_full_bundle_with_exact_manifest_counts(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    bundle = _bundle(tmp_path)

    result = _run(
        "receipt",
        "--backup-root",
        str(tmp_path),
        "--inventory",
        str(inventory),
        "--required-kind",
        "recovery_bundle",
        "--started-at",
        STARTED_AT,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\t")[:2] == [bundle.name, "recovery_bundle"]


def test_receipt_rejects_bundle_when_manifest_counts_do_not_match_restore(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    _bundle(tmp_path, manifest_count_delta=1)

    result = _run(
        "receipt",
        "--backup-root",
        str(tmp_path),
        "--inventory",
        str(inventory),
        "--required-kind",
        "recovery_bundle",
        "--started-at",
        STARTED_AT,
    )

    assert result.returncode != 0
    assert "source_counts does not match isolated restored counts" in result.stderr


def test_receipt_rejects_bundle_when_operations_counts_or_component_hash_coverage_are_incomplete(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    bundle = _bundle(tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["components"]["operations"]["bundle_counts"]["backup_runs"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    base = (
        "receipt",
        "--backup-root",
        str(tmp_path),
        "--inventory",
        str(inventory),
        "--required-kind",
        "recovery_bundle",
        "--started-at",
        STARTED_AT,
    )
    operations_result = _run(*base)
    assert operations_result.returncode != 0
    assert "bundle_counts does not match isolated restored counts" in operations_result.stderr

    manifest["components"]["operations"]["bundle_counts"]["backup_runs"] -= 1
    manifest["files"] = [entry for entry in manifest["files"] if entry["path"] != "ledger.sqlite3"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    coverage_result = _run(*base)
    assert coverage_result.returncode != 0
    assert "file inventory does not exactly match bundle payload" in coverage_result.stderr


def test_receipt_rejects_a_missing_or_unlisted_application_table(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    bundle = _bundle(tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # This is not one of the historical six headline counts.  The receipt must
    # nevertheless reject a manifest that silently omits it.
    del manifest["components"]["ledger"]["table_counts"]["pipeline_jobs"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _run(
        "receipt",
        "--backup-root",
        str(tmp_path),
        "--inventory",
        str(inventory),
        "--required-kind",
        "recovery_bundle",
        "--started-at",
        STARTED_AT,
    )

    assert result.returncode != 0
    assert "table set does not match isolated restore" in result.stderr


def test_receipt_rejects_missing_or_unmanifested_evidence_and_reports_payload(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    bundle = _bundle(tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = (
        "receipt",
        "--backup-root",
        str(tmp_path),
        "--inventory",
        str(inventory),
        "--required-kind",
        "recovery_bundle",
        "--started-at",
        STARTED_AT,
    )

    # A payload not listed by the global receipt is never an acceptable
    # recovery bundle, even if the two SQLite files are healthy.
    (bundle / "evidence" / "unmanifested.txt").write_text("not receipted", encoding="utf-8")
    unlisted = _run(*base)
    assert unlisted.returncode != 0
    assert "file inventory does not exactly match bundle payload" in unlisted.stderr

    (bundle / "evidence" / "unmanifested.txt").unlink()
    manifest["components"]["reports"]["file_inventory"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    missing_component_inventory = _run(*base)
    assert missing_component_inventory.returncode != 0
    assert "reports file inventory does not cover component data" in missing_component_inventory.stderr


def test_receipt_binds_a_legacy_snapshot_to_its_verified_operations_receipt(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    legacy = tmp_path / "finance_radar_20260805T000100Z.sqlite3"
    _ledger(legacy)
    operations = tmp_path / "operations.sqlite3"
    _legacy_operations(operations, legacy, _counts(legacy, LEDGER_TABLES))

    result = _run(
        "receipt",
        "--backup-root",
        str(tmp_path),
        "--inventory",
        str(inventory),
        "--required-kind",
        "any",
        "--started-at",
        STARTED_AT,
        "--operations-db",
        str(operations),
        "--ledger-source",
        str(legacy),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\t")[:2] == [legacy.stem, "legacy_sqlite"]


def test_receipt_rejects_structurally_valid_but_empty_legacy_snapshot(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    legacy = tmp_path / "finance_radar_20260805T000100Z.sqlite3"
    _ledger(legacy, populated=False)
    operations = tmp_path / "operations.sqlite3"
    _legacy_operations(operations, legacy, _counts(legacy, LEDGER_TABLES))

    result = _run(
        "receipt",
        "--backup-root",
        str(tmp_path),
        "--inventory",
        str(inventory),
        "--required-kind",
        "any",
        "--started-at",
        STARTED_AT,
        "--operations-db",
        str(operations),
        "--ledger-source",
        str(legacy),
    )

    assert result.returncode != 0
    assert "structurally valid but contains no data" in result.stderr


def test_legacy_receipt_rejects_a_backup_that_loses_an_untracked_application_table(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    legacy = tmp_path / "finance_radar_20260805T000100Z.sqlite3"
    _ledger(legacy)
    live_ledger = tmp_path / "live-ledger.sqlite3"
    shutil.copyfile(legacy, live_ledger)
    with sqlite3.connect(live_ledger) as connection:
        connection.execute("INSERT INTO pipeline_jobs VALUES ('pipeline-live-only')")
    operations = tmp_path / "operations.sqlite3"
    _legacy_operations(operations, legacy, _counts(legacy, LEDGER_TABLES))

    result = _run(
        "receipt",
        "--backup-root",
        str(tmp_path),
        "--inventory",
        str(inventory),
        "--required-kind",
        "any",
        "--started-at",
        STARTED_AT,
        "--operations-db",
        str(operations),
        "--ledger-source",
        str(live_ledger),
    )

    assert result.returncode != 0
    assert "full application-table inventory does not match its live ledger source" in result.stderr


def test_receipt_rejects_legacy_record_with_mismatched_path_counts_bytes_or_age(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    legacy = tmp_path / "finance_radar_20260805T000100Z.sqlite3"
    _ledger(legacy)
    counts = _counts(legacy, LEDGER_TABLES)
    operations = tmp_path / "operations.sqlite3"
    other = tmp_path / "other.sqlite3"
    shutil.copyfile(legacy, other)
    _legacy_operations(operations, other, counts)

    base = (
        "receipt",
        "--backup-root",
        str(tmp_path),
        "--inventory",
        str(inventory),
        "--required-kind",
        "any",
        "--started-at",
        STARTED_AT,
        "--operations-db",
        str(operations),
        "--ledger-source",
        str(legacy),
    )
    path_result = _run(*base)
    assert path_result.returncode != 0
    assert "expected exactly one verified legacy backup_runs receipt" in path_result.stderr

    count_operations = tmp_path / "operations-count.sqlite3"
    mismatched_counts = dict(counts)
    mismatched_counts["canonical_events"] += 1
    _legacy_operations(count_operations, legacy, mismatched_counts)
    count_result = _run(
        *base[:-4], "--operations-db", str(count_operations), "--ledger-source", str(legacy)
    )
    assert count_result.returncode != 0
    assert "restored counts do not match isolated restore" in count_result.stderr

    bytes_operations = tmp_path / "operations-bytes.sqlite3"
    _legacy_operations(bytes_operations, legacy, counts, bytes_delta=1)
    bytes_result = _run(
        *base[:-4], "--operations-db", str(bytes_operations), "--ledger-source", str(legacy)
    )
    assert bytes_result.returncode != 0
    assert "does not match snapshot bytes" in bytes_result.stderr

    stale_operations = tmp_path / "operations-stale.sqlite3"
    _legacy_operations(stale_operations, legacy, counts, verified_at="2026-08-04T23:59:59+00:00")
    stale_result = _run(
        *base[:-4], "--operations-db", str(stale_operations), "--ledger-source", str(legacy)
    )
    assert stale_result.returncode != 0
    assert "predates the requested backup run" in stale_result.stderr
