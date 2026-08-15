from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.ops import backup as backup_module
from app.ops.backup import create_and_verify, verify_bundle_restore
from app.storage.operations import OperationsRepository
from scripts.event_ledger import open_ledger, stable_json, utc_now


_BACKUP_LOCK_CONTENDER = r"""
import json
import os
import sys
import time
from pathlib import Path
from app.ops import backup as backup_module

backup_dir, start_path, release_path, ready_path, result_path = map(Path, sys.argv[1:])
ready_path.write_text(str(os.getpid()), encoding="utf-8")
deadline = time.monotonic() + 20
while not start_path.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("parent did not release the contenders")
    time.sleep(0.005)
try:
    with backup_module._backup_lock(backup_dir) as state:
        result_path.write_text(
            json.dumps(
                {
                    "status": "acquired",
                    "pid": os.getpid(),
                    "recovered_stale_lock": state["recovered_stale_lock"],
                }
            ),
            encoding="utf-8",
        )
        while not release_path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("parent did not release the winner")
            time.sleep(0.005)
except RuntimeError as exc:
    result_path.write_text(
        json.dumps({"status": "blocked", "pid": os.getpid(), "error": str(exc)}),
        encoding="utf-8",
    )
"""


def _light_result(*, rationale: str = "primary source supports the formal event") -> dict:
    return {
        "batch_id": "batch-recovery",
        "event_id": "evt-recovery",
        "decision": "SUPPORTED",
        "before_version": 1,
        "after_version": 2,
        "evidence_ids": ["ev-recovery"],
        "budget": {"max_primary_documents": 2},
        "rationale": rationale,
        "checks": ["entity", "event", "date"],
        "before_model": {"input_sha256": "before", "label": "ABSTAIN"},
        "after_model": {"input_sha256": "after", "label": "RISK_REVIEW"},
        "applied": True,
        "no_trading": True,
    }


def _seed_committed_light_version(
    path: Path,
    result: dict,
    *,
    change_reason: str = "light_evidence_verification_v1",
) -> None:
    (path.parent / "evidence_objects").mkdir(parents=True, exist_ok=True)
    (path.parent.parent / "reports").mkdir(parents=True, exist_ok=True)
    connection = open_ledger(path)
    now = utc_now()
    connection.execute(
        """INSERT INTO canonical_events(
               event_id,current_version,status,label_status,event_family,event_type,event_date,
               first_seen_at,last_updated_at,stable_id,ticker_at_event,company_name,manual_grade,
               provisional_grade_cap,discovery_source,no_trading
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (
            "evt-recovery", 2, "verified", "verified", "delisting_or_suspension", "delisted",
            "2025-01-02", now, now, "stable-recovery", "ACME", "ACME HOLDINGS", None,
            None, "test",
        ),
    )
    connection.execute(
        "INSERT INTO event_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "evt-recovery", 1, now, "candidate", "candidate", "delisting_or_suspension",
            "delisted", None, stable_json({"evidence_summary": "seed"}), "seed",
        ),
    )
    light = {
        "version": "light-evidence-v2",
        "formal_conclusion": "verified",
        "reviewed_at": now,
        "batch_id": result["batch_id"],
        "evidence_ids": result["evidence_ids"],
        "budget": result["budget"],
        "rationale": result["rationale"],
        "checks": result["checks"],
        "model_reassessment": {
            "before": result["before_model"],
            "after": result["after_model"],
        },
        "no_trading": True,
    }
    connection.execute(
        "INSERT INTO event_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "evt-recovery", 2, now, "verified", "verified", "delisting_or_suspension",
            "delisted", None, stable_json({"light_verification": light}),
            change_reason,
        ),
    )
    connection.commit()
    connection.close()


def test_prepared_mutation_recovers_after_ledger_commit_without_duplicate_audit(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    result = _light_result()
    mutation_id = operations.prepare_light_verification_mutation(result)

    # Simulate a crash after the ledger transaction commits but before the
    # process can call confirm_light_verification_mutation().
    _seed_committed_light_version(ledger_path, result)
    first = operations.reconcile_light_verification_mutations(ledger_path)
    second = operations.reconcile_light_verification_mutations(ledger_path)

    assert first["recovered"] == 1
    assert second["recovered"] == 0
    audits = operations.formal_mutation_audits("evt-recovery")
    assert audits[0]["mutation_id"] == mutation_id
    assert audits[0]["state"] == "RECOVERED"
    assert len(operations.light_verification_runs("evt-recovery")) == 1
    assert operations.audit_reconciliation_status()["status"] == "ok"


def test_formal_mutation_identity_rejects_a_different_retry_payload(tmp_path: Path) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    operations.prepare_light_verification_mutation(_light_result())
    with pytest.raises(ValueError, match="identity collision"):
        operations.prepare_light_verification_mutation(_light_result(rationale="different conclusion rationale"))


def test_model_run_once_is_atomic_under_concurrent_retries(tmp_path: Path) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    result = {
        "input_sha256": "input" * 16,
        "model_version": "model-v1",
        "label": "ABSTAIN",
        "confidence": 0.5,
        "latency_ms": 1.0,
        "event_version": 3,
        "call_kind": "DETERMINISTIC_EVIDENCE_GATE",
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: operations.record_model_run_once("evt-model", result), range(2)))

    assert sum(int(created) for _run_id, created in outcomes) == 1
    assert len({run_id for run_id, _created in outcomes}) == 1
    assert len(operations.model_runs("evt-model")) == 1


def _seed_minimal_ledger(path: Path) -> None:
    # A complete recovery point carries both mutable evidence objects and
    # reports.  They may be empty, but their roots must exist so an absent
    # component cannot be mistaken for an empty one.
    (path.parent / "evidence_objects").mkdir(parents=True, exist_ok=True)
    (path.parent.parent / "reports").mkdir(parents=True, exist_ok=True)
    connection = open_ledger(path)
    now = utc_now()
    connection.execute(
        """INSERT INTO canonical_events(
               event_id,current_version,status,label_status,event_family,event_type,event_date,
               first_seen_at,last_updated_at,stable_id,ticker_at_event,company_name,manual_grade,
               provisional_grade_cap,discovery_source,no_trading
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        ("evt-backup", 1, "candidate", "candidate", "test", "test", "2025-01-02", now, now,
         "backup-stable", None, "Backup Co", None, None, "test"),
    )
    connection.execute(
        "INSERT INTO event_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("evt-backup", 1, now, "candidate", "candidate", "test", "test", None, "{}", "seed"),
    )
    connection.commit()
    connection.close()


def test_predeploy_bridge_backup_is_verified_without_mutating_live_operations_state(tmp_path: Path) -> None:
    ledger_path = tmp_path / "data" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    _seed_minimal_ledger(ledger_path)
    operations_path = tmp_path / "data" / "operations.sqlite3"
    operations = OperationsRepository(operations_path)
    operations.create_backup_run(
        tmp_path / "previous-manifest.json",
        source_bytes=123,
        manifest_path=tmp_path / "previous-manifest.json",
        snapshot_kind="recovery_bundle",
    )

    # Mirror the production mismatch that prompted the bridge: the active
    # release has the legacy ten-column receipt table while candidate code
    # knows how to add three newer columns.  A pre-cutover recovery bundle must
    # retain the active-release format, not silently migrate its copied SQLite.
    with sqlite3.connect(operations_path) as connection:
        connection.execute("ALTER TABLE backup_runs RENAME TO backup_runs_new")
        connection.execute(
            """CREATE TABLE backup_runs(
                   backup_id TEXT PRIMARY KEY, backup_path TEXT NOT NULL, source_bytes INTEGER NOT NULL,
                   backup_bytes INTEGER, quick_check TEXT, restored_count_json TEXT,
                   status TEXT NOT NULL, created_at TEXT NOT NULL, verified_at TEXT, error TEXT
               )"""
        )
        connection.execute(
            """INSERT INTO backup_runs(
                   backup_id,backup_path,source_bytes,backup_bytes,quick_check,restored_count_json,
                   status,created_at,verified_at,error
               )
               SELECT backup_id,backup_path,source_bytes,backup_bytes,quick_check,restored_count_json,
                      status,created_at,verified_at,error
               FROM backup_runs_new"""
        )
        connection.execute("DROP TABLE backup_runs_new")
        connection.commit()

    with sqlite3.connect(operations_path) as connection:
        before_columns = connection.execute("PRAGMA table_info(backup_runs)").fetchall()
        before_rows = connection.execute("SELECT * FROM backup_runs ORDER BY backup_id").fetchall()
        before_schema = connection.execute("SELECT * FROM operations_schema ORDER BY version").fetchall()
        before_sql = connection.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
        ).fetchall()
        before_operations_dump = list(connection.iterdump())
    with sqlite3.connect(ledger_path) as connection:
        before_ledger_dump = list(connection.iterdump())

    # This is the deployment-only construction: no initialize(), reconcile, or
    # backup_runs receipt write may touch the shared database before cutover.
    bridge_operations = OperationsRepository(operations_path, initialize=False)
    result = create_and_verify(
        ledger_path,
        tmp_path / "backups",
        bridge_operations,
        predeploy_bridge=True,
    )

    assert result["status"] == "VERIFIED"
    assert result["backup_id"] is None
    assert result["predeploy_bridge"] is True
    verification = verify_bundle_restore(Path(result["backup_path"]))
    assert verification["manifest_verified"] is True
    assert verification["formal_audit_consistency"]["status"] == "PASS"
    manifest = json.loads((Path(result["backup_path"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot_audit_reconciliation"]["status"] == "PRESERVED_PREDEPLOY_BRIDGE"
    bundle_path = Path(result["backup_path"])
    with sqlite3.connect(bundle_path / "operations.sqlite3") as connection:
        assert connection.execute("PRAGMA table_info(backup_runs)").fetchall() == before_columns
        assert connection.execute("SELECT * FROM backup_runs ORDER BY backup_id").fetchall() == before_rows
        assert connection.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
        ).fetchall() == before_sql
        assert list(connection.iterdump()) == before_operations_dump
        # A recovered old release still writes ten positional backup values.
        # This would fail if candidate bridge code had added the three newer
        # columns to the retained SQLite copy.
        connection.execute(
            "INSERT INTO backup_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "old-release-write",
                "old-release-path",
                1,
                None,
                None,
                "{}",
                "VERIFIED",
                "2026-08-05T00:00:00+00:00",
                None,
                None,
            ),
        )
        connection.commit()
    with sqlite3.connect(bundle_path / "ledger.sqlite3") as connection:
        assert list(connection.iterdump()) == before_ledger_dump
    with sqlite3.connect(operations_path) as connection:
        assert connection.execute("PRAGMA table_info(backup_runs)").fetchall() == before_columns
        assert connection.execute("SELECT * FROM backup_runs ORDER BY backup_id").fetchall() == before_rows
        assert connection.execute("SELECT * FROM operations_schema ORDER BY version").fetchall() == before_schema


def test_predeploy_bridge_rejects_unresolved_audit_without_mutating_it(tmp_path: Path) -> None:
    ledger_path = tmp_path / "data" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    _seed_minimal_ledger(ledger_path)
    operations_path = tmp_path / "data" / "operations.sqlite3"
    operations = OperationsRepository(operations_path)
    mutation_id = operations.prepare_light_verification_mutation(_light_result())

    with pytest.raises(RuntimeError, match="formal-audit consistency failed"):
        create_and_verify(
            ledger_path,
            tmp_path / "backups",
            OperationsRepository(operations_path, initialize=False),
            predeploy_bridge=True,
        )

    with sqlite3.connect(operations_path) as connection:
        assert connection.execute(
            "SELECT state FROM formal_mutation_audits WHERE mutation_id=?", (mutation_id,)
        ).fetchone() == ("PREPARED",)


def test_verified_bundle_captures_evidence_reports_and_failed_followup_keeps_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger_path = tmp_path / "data" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    _seed_minimal_ledger(ledger_path)
    operations = OperationsRepository(tmp_path / "data" / "operations.sqlite3")
    evidence_dir = tmp_path / "data" / "evidence_objects"
    reports_dir = tmp_path / "reports"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(exist_ok=True)
    (evidence_dir / "proof.txt").write_text("immutable evidence", encoding="utf-8")
    (reports_dir / "cycle.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    backup_dir = tmp_path / "backups"

    first = create_and_verify(
        ledger_path,
        backup_dir,
        operations,
        evidence_dir=evidence_dir,
        report_dir=reports_dir,
    )
    first_bundle = Path(first["backup_path"])
    assert (first_bundle / "evidence" / "proof.txt").is_file()
    assert (first_bundle / "reports" / "cycle.json").is_file()
    verified_first = verify_bundle_restore(first_bundle)
    assert verified_first["files_verified"] >= 4
    manifest = json.loads((first_bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["components"]["evidence"]["file_inventory"][0]["path"] == "evidence/proof.txt"
    assert manifest["components"]["reports"]["file_inventory"][0]["path"] == "reports/cycle.json"
    assert verified_first["evidence"]["files"] == 1
    assert verified_first["reports"]["files"] == 1
    assert "pipeline_jobs" in verified_first["ledger"]["table_counts"]
    assert "runtime_state" in verified_first["operations"]["table_counts"]

    original_snapshot_tree = backup_module._snapshot_tree

    def fail_reports(source: Path, bundle: Path, component: str):
        if component == "reports":
            raise RuntimeError("injected report snapshot failure")
        return original_snapshot_tree(source, bundle, component)

    monkeypatch.setattr(backup_module, "_snapshot_tree", fail_reports)
    with pytest.raises(RuntimeError, match="injected report snapshot failure"):
        create_and_verify(
            ledger_path,
            backup_dir,
            operations,
            evidence_dir=evidence_dir,
            report_dir=reports_dir,
        )
    assert first_bundle.is_dir()
    assert verify_bundle_restore(first_bundle)["manifest_verified"] is True
    assert Path(str(operations.latest_verified_backup()["backup_path"])).parent == first_bundle
    assert operations.latest_backup()["status"] == "FAILED"


def test_recovery_bundle_requires_existing_component_roots_and_rejects_unmanifested_payload(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "data" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    _seed_minimal_ledger(ledger_path)
    operations = OperationsRepository(tmp_path / "data" / "operations.sqlite3")
    evidence_dir = ledger_path.parent / "evidence_objects"
    reports_dir = ledger_path.parent.parent / "reports"

    result = create_and_verify(
        ledger_path,
        tmp_path / "backups",
        operations,
        evidence_dir=evidence_dir,
        report_dir=reports_dir,
    )
    bundle = Path(result["backup_path"])
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["components"]["evidence"]["directories"] == ["."]
    assert manifest["components"]["reports"]["directories"] == ["."]
    assert manifest["components"]["evidence"]["file_inventory"] == []
    assert manifest["components"]["reports"]["file_inventory"] == []
    assert verify_bundle_restore(bundle)["evidence"]["files"] == 0

    (bundle / "evidence" / "unmanifested.txt").write_text("not in receipt", encoding="utf-8")
    with pytest.raises(RuntimeError, match="file inventory does not exactly match bundle payload"):
        verify_bundle_restore(bundle)

    missing_evidence = tmp_path / "missing-evidence"
    with pytest.raises(FileNotFoundError, match="required snapshot component is missing"):
        create_and_verify(
            ledger_path,
            tmp_path / "other-backups",
            operations,
            evidence_dir=missing_evidence,
            report_dir=reports_dir,
        )


def test_recovery_bundle_excludes_its_transient_run_and_recovers_only_dead_stale_lock(tmp_path: Path) -> None:
    ledger_path = tmp_path / "data" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    _seed_minimal_ledger(ledger_path)
    operations = OperationsRepository(tmp_path / "data" / "operations.sqlite3")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    lock = backup_dir / ".finance-radar-backup.lock"
    lock.write_text(json.dumps({"token": "dead-owner", "pid": 999_999_999}), encoding="utf-8")
    stale_time = time.time() - backup_module.LOCK_STALE_AFTER_SECONDS - 1
    os.utime(lock, (stale_time, stale_time))

    result = create_and_verify(ledger_path, backup_dir, operations)
    bundle = Path(result["backup_path"])
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["lock"]["recovered_stale_lock"] is True
    with sqlite3.connect(bundle / "operations.sqlite3") as connection:
        assert connection.execute(
            "SELECT 1 FROM backup_runs WHERE backup_id=?", (result["backup_id"],)
        ).fetchone() is None
    assert operations.latest_backup()["status"] == "VERIFIED"

    lock.write_text(json.dumps({"token": "live-owner", "pid": os.getpid()}), encoding="utf-8")
    try:
        with pytest.raises(RuntimeError, match="another backup is already running"):
            create_and_verify(ledger_path, backup_dir, operations)
    finally:
        lock.unlink(missing_ok=True)


def test_two_processes_cannot_both_reclaim_the_same_stale_backup_lock(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    lock = backup_dir / ".finance-radar-backup.lock"
    lock.write_text(json.dumps({"token": "dead-owner", "pid": 999_999_999}), encoding="utf-8")
    stale_time = time.time() - backup_module.LOCK_STALE_AFTER_SECONDS - 1
    os.utime(lock, (stale_time, stale_time))

    start_path = tmp_path / "start"
    release_path = tmp_path / "release"
    ready_paths = [tmp_path / f"ready-{index}" for index in range(2)]
    result_paths = [tmp_path / f"result-{index}.json" for index in range(2)]
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(root), str(root / "scripts"), environment.get("PYTHONPATH", "")]
    )
    contenders = [
        subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                _BACKUP_LOCK_CONTENDER,
                str(backup_dir),
                str(start_path),
                str(release_path),
                str(ready_paths[index]),
                str(result_paths[index]),
            ],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    completed: list[tuple[str, str]] = []
    try:
        deadline = time.monotonic() + 15
        while not all(path.exists() for path in ready_paths) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert all(path.exists() for path in ready_paths)

        start_path.write_text("start", encoding="utf-8")
        deadline = time.monotonic() + 15
        while not all(path.exists() for path in result_paths) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert all(path.exists() for path in result_paths)
        results = [json.loads(path.read_text(encoding="utf-8")) for path in result_paths]
        release_path.write_text("release", encoding="utf-8")
        completed = [contender.communicate(timeout=15) for contender in contenders]
    finally:
        release_path.touch(exist_ok=True)
        for contender in contenders:
            if contender.poll() is None:
                contender.terminate()
                contender.wait(timeout=5)

    assert [contender.returncode for contender in contenders] == [0, 0], completed
    assert sorted(item["status"] for item in results) == ["acquired", "blocked"]
    acquired = next(item for item in results if item["status"] == "acquired")
    blocked = next(item for item in results if item["status"] == "blocked")
    assert acquired["recovered_stale_lock"] is True
    assert "another backup is already running" in blocked["error"]
    assert not lock.exists()
    assert (backup_dir / ".finance-radar-backup.guard").is_file()


def test_new_exclusive_backup_workflow_reconciles_orphaned_running_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "data" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    _seed_minimal_ledger(ledger_path)
    operations = OperationsRepository(tmp_path / "data" / "operations.sqlite3")
    orphan_id = operations.create_backup_run(
        tmp_path / "data" / "operational_backups" / "finance_radar_20260730T195415Z.sqlite3",
        ledger_path.stat().st_size,
    )
    backup_dir = tmp_path / "backups"
    original_reconcile = operations.reconcile_abandoned_backup_runs

    def under_exclusive_lock(*, exclusive_owner: str) -> dict:
        assert (backup_dir / ".finance-radar-backup.lock").is_file()
        return original_reconcile(exclusive_owner=exclusive_owner)

    monkeypatch.setattr(operations, "reconcile_abandoned_backup_runs", under_exclusive_lock)
    result = create_and_verify(ledger_path, backup_dir, operations)

    with operations.connect() as connection:
        row = connection.execute(
            "SELECT status,verified_at,error FROM backup_runs WHERE backup_id=?", (orphan_id,)
        ).fetchone()
    assert row is not None
    assert row["status"] == "FAILED"
    assert row["verified_at"]
    assert "ABANDONED_RUNNING_BACKUP_RECONCILED" in row["error"]
    reconciliation = result["backup_run_reconciliation"]
    assert reconciliation["reconciled"] == 1
    assert reconciliation["backup_ids"] == [orphan_id]
    assert reconciliation["exclusive_owner"].startswith("backup-root-lock pid=")


def test_formal_audit_consistency_detects_a_split_recovery_pair(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    result = _light_result()
    _seed_committed_light_version(
        ledger_path,
        result,
        change_reason="light_evidence_verification_v2",
    )

    missing = backup_module._formal_audit_consistency(ledger_path, operations.path)
    assert missing["status"] == "FAIL"
    assert missing["missing_audits"] == ["evt-recovery@2"]

    mutation_id = operations.prepare_light_verification_mutation(result)
    operations.confirm_light_verification_mutation(mutation_id, result)
    consistent = backup_module._formal_audit_consistency(ledger_path, operations.path)
    assert consistent["status"] == "PASS"


def test_sparse_v1_history_is_disclosed_without_blocking_a_verified_recovery_bundle(tmp_path: Path) -> None:
    ledger_path = tmp_path / "data" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    _seed_minimal_ledger(ledger_path)
    with sqlite3.connect(ledger_path) as connection:
        connection.execute(
            """UPDATE event_versions
               SET change_reason='light_evidence_verification_v1',
                   facts_json=?
               WHERE event_id='evt-backup' AND version=1""",
            (stable_json({"light_verification": {"version": "light-evidence-gate-v1"}}),),
        )
        connection.commit()
    operations = OperationsRepository(tmp_path / "data" / "operations.sqlite3")

    consistency = backup_module._formal_audit_consistency(ledger_path, operations.path)
    assert consistency["status"] == "PASS"
    assert consistency["legacy_v1_without_audit"] == ["evt-backup@1"]

    result = create_and_verify(ledger_path, tmp_path / "backups", operations)
    assert result["verification"]["formal_audit_consistency"]["status"] == "PASS"
    assert result["verification"]["formal_audit_consistency"]["legacy_v1_without_audit"] == ["evt-backup@1"]


def test_parseable_v1_history_is_not_retroactively_audited_by_backup(tmp_path: Path) -> None:
    ledger_path = tmp_path / "data" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    result = _light_result()
    _seed_committed_light_version(ledger_path, result)
    operations = OperationsRepository(tmp_path / "data" / "operations.sqlite3")

    backup = create_and_verify(ledger_path, tmp_path / "backups", operations)
    assert operations.formal_mutation_audits("evt-recovery") == []
    assert backup["verification"]["formal_audit_consistency"]["legacy_v1_without_audit"] == [
        "evt-recovery@2"
    ]


def test_posix_permission_denied_lock_probe_is_treated_as_a_live_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_module.os, "name", "posix")

    def deny_probe(_pid: int, _signal: int) -> None:
        raise PermissionError("not allowed")

    monkeypatch.setattr(backup_module.os, "kill", deny_probe)
    assert backup_module._pid_is_alive(424242) is True
