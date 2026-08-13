from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import timedelta
from unittest.mock import patch
from pathlib import Path

import pytest


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
            """INSERT INTO raw_observations VALUES (
               ?, 'src', ?, '2026-08-01', ?, 'Primary filing',
               'A current primary-source passage.', 'https://example.test/evidence', ?, '{}', 'captured')""",
            (f"obs-{index}", f"source-{index}", now, f"sha-{index}"),
        )
        ledger.execute(
            """INSERT INTO event_evidence VALUES (
               ?,?,?,'https://example.test/evidence', '2026-08-01','8-K','1.01',
               'A current primary-source passage.', 'primary',10,'confirmed',0,?,?)""",
            (f"evidence-{index}", event_id, f"obs-{index}", now, now),
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
            event_id = f"evt-{index}"
            evidence_id = f"evidence-{index}"
            claim_id = f"claim-{index}"
            output = {
                "event_id": event_id,
                "status": decision_status,
                "claims": [
                    {
                        "claim_id": claim_id,
                        "text": "The primary filing describes the current event.",
                        "verification_state": (
                            "PRIMARY_SUPPORTED"
                            if decision_status == "EVIDENCE_READY"
                            else "INSUFFICIENT"
                        ),
                    }
                ],
                "evidence_edges": (
                    [
                        {
                            "claim_id": claim_id,
                            "evidence_id": evidence_id,
                            "relation": "SUPPORTS",
                            "authority_tier": "P0",
                            "exact_excerpt": "A current primary-source passage.",
                            "source_url": "https://example.test/evidence",
                        }
                    ]
                    if decision_status == "EVIDENCE_READY"
                    else []
                ),
            }
            operations.execute(
                """INSERT INTO agent_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"decision-{index}",
                    event_id,
                    f"trace-{index}",
                    decision_status,
                    "prompt-v1",
                    "deterministic",
                    "snapshot",
                    json.dumps(output),
                    "{}",
                    "[]",
                    json.dumps([evidence_id] if decision_status == "EVIDENCE_READY" else []),
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
        with patch.object(rough, "open_ledger", side_effect=AssertionError("dry-run wrote ledger")):
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
        assert all(row["contract_version"] == rough.ROUGH_REVIEW_CONTRACT_VERSION for row in payloads)
        assert all(row["event_version"] == 1 for row in payloads)
        assert all(row["evidence_ids"] for row in payloads)
        assert all(len(row["evidence_fingerprint"]) == 64 for row in payloads)
        assert all(row["reason"] for row in payloads)
        assert ledger.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE status='candidate' AND label_status='candidate'"
        ).fetchone()[0] == 2
        ledger.close()


def test_apply_refuses_a_changed_event_version_or_evidence_snapshot() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger_path, operations_path = _fixture(Path(directory))
        read_only = rough.open_read_only(ledger_path)
        try:
            rows, deferred = rough.build_rows(read_only, rough.load_latest_decisions(operations_path))
        finally:
            read_only.close()
        assert not deferred
        ledger = open_ledger(ledger_path)
        ledger.execute(
            "UPDATE event_evidence SET evidence_passage='changed after dry snapshot' WHERE event_id='evt-1'"
        )
        ledger.commit()
        with pytest.raises(RuntimeError, match="event version or evidence changed"):
            rough.apply_rows(
                ledger,
                rows,
                batch_id="batch-evidence-changed",
                reviewed_at=utc_now(),
            )
        ledger.close()


def test_build_defers_decision_that_predates_current_evidence_update() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger_path, operations_path = _fixture(Path(directory))
        operations = sqlite3.connect(operations_path)
        try:
            created_at = operations.execute(
                "SELECT created_at FROM agent_decisions WHERE event_id='evt-1'"
            ).fetchone()[0]
        finally:
            operations.close()
        later_evidence_time = (
            rough._parse_time(created_at) + timedelta(seconds=1)
        ).isoformat()
        ledger = open_ledger(ledger_path)
        ledger.execute(
            "UPDATE event_evidence SET updated_at=? WHERE event_id='evt-1'",
            (later_evidence_time,),
        )
        ledger.commit()
        ledger.close()

        dry = rough.run(
            ledger_path,
            operations_path,
            apply=False,
            authorization=None,
            batch_id="batch-current-evidence-gate",
        )
        assert dry["selected"] == 1
        assert dry["deferred_reasons"] == {"DECISION_PREDATES_CURRENT_EVIDENCE": 1}


def test_build_requires_meaningful_primary_claim_graph_for_evidence_ready() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger_path, operations_path = _fixture(Path(directory))
        operations = sqlite3.connect(operations_path)
        try:
            output = json.loads(
                operations.execute(
                    "SELECT output_json FROM agent_decisions WHERE event_id='evt-1'"
                ).fetchone()[0]
            )
            output["evidence_edges"][0]["authority_tier"] = "P2"
            operations.execute(
                "UPDATE agent_decisions SET output_json=? WHERE event_id='evt-1'",
                (json.dumps(output),),
            )
            operations.commit()
        finally:
            operations.close()

        dry = rough.run(
            ledger_path,
            operations_path,
            apply=False,
            authorization=None,
            batch_id="batch-primary-claim-gate",
        )
        assert dry["selected"] == 1
        assert dry["deferred_reasons"] == {"EVIDENCE_READY_CLAIM_LACKS_PRIMARY_SUPPORT": 1}


def test_build_never_rough_accepts_a_conflicted_evidence_row_as_support() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger_path, operations_path = _fixture(Path(directory))
        ledger = open_ledger(ledger_path)
        ledger.execute(
            "UPDATE event_evidence SET evidence_status='conflicted' WHERE event_id='evt-1'"
        )
        ledger.commit()
        ledger.close()

        dry = rough.run(
            ledger_path,
            operations_path,
            apply=False,
            authorization=None,
            batch_id="batch-conflict-cannot-rough-accept",
        )
        assert dry["selected"] == 1
        assert dry["outcomes"] == {"ROUGH_INSUFFICIENT": 1}
        assert dry["deferred_reasons"] == {"CONFLICTING_EVIDENCE_MISCLASSIFIED": 1}


def test_build_rejects_opaque_decision_output() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger_path, operations_path = _fixture(Path(directory))
        operations = sqlite3.connect(operations_path)
        try:
            operations.execute(
                "UPDATE agent_decisions SET output_json='{}' WHERE event_id='evt-1'"
            )
            operations.commit()
        finally:
            operations.close()

        dry = rough.run(
            ledger_path,
            operations_path,
            apply=False,
            authorization=None,
            batch_id="batch-opaque-decision-gate",
        )
        assert dry["selected"] == 1
        assert dry["deferred_reasons"] == {"DECISION_OUTPUT_EVENT_MISMATCH": 1}


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


def test_light_followup_jobs_cannot_be_closed_by_bulk_rough_review() -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger_path, operations_path = _fixture(Path(directory))
        ledger = open_ledger(ledger_path)
        now = utc_now()
        ledger.execute(
            """INSERT INTO pipeline_jobs VALUES (
               'light-followup-1','evt-1','light_verification_followup','PENDING_HUMAN_REVIEW',
               75,0,?,NULL,'{}',?,?)""",
            (now, now, now),
        )
        ledger.commit()
        ledger.close()

        dry = rough.run(
            ledger_path,
            operations_path,
            apply=False,
            authorization=None,
            batch_id="batch-followup-isolation",
        )
        assert dry["selected"] == 2

        applied = rough.run(
            ledger_path,
            operations_path,
            apply=True,
            authorization=rough.AUTHORIZATION_PHRASE,
            batch_id="batch-followup-isolation",
        )
        assert applied["updated"] == 2
        ledger = open_ledger(ledger_path)
        followup = ledger.execute(
            "SELECT status,payload_json FROM pipeline_jobs WHERE job_id='light-followup-1'"
        ).fetchone()
        assert followup["status"] == "PENDING_HUMAN_REVIEW"
        assert "rough_review" not in json.loads(followup["payload_json"])
        ledger.close()
