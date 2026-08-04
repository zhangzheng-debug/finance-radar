from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from app.storage import OperationsRepository
from event_ledger import open_ledger, utc_now
import apply_authorized_rough_reviews as rough


def _fixture(root: Path) -> tuple[Path, Path]:
    ledger_path = root / "ledger.sqlite3"
    operations_path = root / "operations.sqlite3"
    ledger = open_ledger(ledger_path)
    now = utc_now()
    ledger.execute(
        "INSERT INTO sources VALUES ('src','Source','official_primary','P0',1,1,?,?)",
        (now, now),
    )
    for index, decision_status in enumerate(("EVIDENCE_READY", "INSUFFICIENT"), start=1):
        event_id = f"evt-{index}"
        ledger.execute(
            """INSERT INTO canonical_events VALUES (
               ?,1,'candidate','candidate','regulatory','filing','2026-08-01',
               ?,?,NULL,NULL,'Example',NULL,NULL,'src',1)""",
            (event_id, now, now),
        )
        ledger.execute(
            """INSERT INTO event_versions VALUES (
               ?,1,?,'candidate','candidate','regulatory','filing',NULL,'{}','fixture')""",
            (event_id, now),
        )
        ledger.execute(
            """INSERT INTO pipeline_jobs VALUES (
               ?,?,'live_primary_evidence_review','PENDING_HUMAN_REVIEW',50,0,?,NULL,'{}',?,?)""",
            (f"job-{index}", event_id, now, now, now),
        )
    ledger.commit()
    ledger.close()
    OperationsRepository(operations_path)
    operations = sqlite3.connect(operations_path)
    try:
        for index, decision_status in enumerate(("EVIDENCE_READY", "INSUFFICIENT"), start=1):
            operations.execute(
                """INSERT INTO agent_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"decision-{index}",
                    f"evt-{index}",
                    f"trace-{index}",
                    decision_status,
                    "prompt-v1",
                    "deterministic",
                    "snapshot",
                    "{}",
                    "{}",
                    "[]",
                    "[]",
                    1.0,
                    now,
                ),
            )
        operations.commit()
    finally:
        operations.close()
    return ledger_path, operations_path


def test_dry_run_and_apply_preserve_canonical_labels() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger_path, operations_path = _fixture(Path(directory))
        dry = rough.run(
            ledger_path,
            operations_path,
            apply=False,
            authorization=None,
            batch_id="batch-test",
        )
        assert dry["selected"] == 2
        assert dry["updated"] == 0
        assert dry["outcomes"] == {"ROUGH_ACCEPTED": 1, "ROUGH_INSUFFICIENT": 1}

        applied = rough.run(
            ledger_path,
            operations_path,
            apply=True,
            authorization=rough.AUTHORIZATION_PHRASE,
            batch_id="batch-test",
        )
        assert applied["selected"] == applied["updated"] == 2
        ledger = open_ledger(ledger_path)
        rows = ledger.execute(
            "SELECT event_id,status,payload_json FROM pipeline_jobs ORDER BY event_id"
        ).fetchall()
        assert {row["status"] for row in rows} == {rough.COMPLETED_STATUS}
        payloads = [json.loads(row["payload_json"])["rough_review"] for row in rows]
        assert {row["outcome"] for row in payloads} == {
            "ROUGH_ACCEPTED",
            "ROUGH_INSUFFICIENT",
        }
        assert all(row["formal_verification"] is False for row in payloads)
        assert ledger.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE status='candidate' AND label_status='candidate'"
        ).fetchone()[0] == 2
        ledger.close()


def test_apply_requires_exact_authorization() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger_path, operations_path = _fixture(Path(directory))
        try:
            rough.run(
                ledger_path,
                operations_path,
                apply=True,
                authorization="wrong",
            )
        except ValueError as exc:
            assert rough.AUTHORIZATION_PHRASE in str(exc)
        else:
            raise AssertionError("apply must require the authorization phrase")
