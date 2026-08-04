from __future__ import annotations

import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.ops import backup as backup_module
from app.ops.backup import create_and_verify, verify_bundle_restore
from app.storage.operations import OperationsRepository
from scripts.event_ledger import open_ledger, stable_json, utc_now


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


def test_verified_bundle_captures_evidence_reports_and_failed_followup_keeps_previous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger_path = tmp_path / "data" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    _seed_minimal_ledger(ledger_path)
    operations = OperationsRepository(tmp_path / "data" / "operations.sqlite3")
    evidence_dir = tmp_path / "data" / "evidence_objects"
    reports_dir = tmp_path / "reports"
    evidence_dir.mkdir(parents=True)
    reports_dir.mkdir()
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
    assert verify_bundle_restore(first_bundle)["files_verified"] >= 4

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
