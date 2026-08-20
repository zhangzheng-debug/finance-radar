from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import event_ledger
from app.services.event_fact_review import (
    _EVENT_EVIDENCE_SQL,
    _event_evidence,
    apply_consensus,
    build_assignment,
    build_authorization_template,
    merge_submissions,
    select_reviewable_events,
    validate_submission,
)


def queue_row(candidate_id: str, ticker: str, family: str = "bankruptcy_or_distress") -> dict[str, str]:
    return {
        "queue_rank": "1",
        "event_candidate_id": candidate_id,
        "stable_id": f"permaticker:{candidate_id}",
        "ticker_at_event": ticker,
        "company_name": f"{ticker} Corporation",
        "event_date": "2026-01-01",
        "event_family": family,
        "event_type": "bankruptcy_liquidation" if "bankruptcy" in family else "delisted",
        "detection_rule": "fixture candidate",
        "detection_value": "fixture",
        "priority_score": "100",
        "provisional_grade_cap": "A++_candidate",
        "sec_filings_url": f"https://www.sec.gov/{candidate_id}",
    }


def passage(candidate_id: str, ticker: str) -> dict[str, str]:
    return {
        "event_candidate_id": candidate_id,
        "accession_number": f"0001-26-{candidate_id}",
        "filing_date": "2026-01-01",
        "form": "8-K",
        "items": "1.03",
        "filing_document_url": f"https://www.sec.gov/{candidate_id}/document",
        "text_sha256": (candidate_id.lower() * 64)[:64],
        "evidence_passage": (
            f"{ticker} Corporation filed a voluntary petition under Chapter 11 "
            "in the United States Bankruptcy Court on January 1, 2026."
        ),
        "matched_keywords": "chapter 11",
        "passage_score": "10",
        "passage_status": "candidate_passage",
    }


@pytest.fixture()
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.sqlite3"
    connection = event_ledger.open_ledger(path)
    try:
        queues = [
            queue_row("C1", "AAA"),
            queue_row("C2", "BBB", "delisting_or_suspension"),
        ]
        passages = [passage("C1", "AAA"), passage("C2", "BBB")]
        event_ledger.import_active_research(
            connection,
            queue_rows=queues,
            passage_rows=passages,
            adjudication_rows=[],
            market_rows=[],
        )
    finally:
        connection.close()
    return path


def submission(assignment: dict, reviewer_id: str, decision: str = "CONFIRM_EVENT") -> dict:
    results = []
    for event in assignment["events"]:
        row = {
            "event_id": event["event_id"],
            "event_version": event["current_version"],
            "evidence_fingerprint": event["evidence_fingerprint"],
            "checks": {
                "source_accessible": "YES",
                "subject_match": "YES",
                "event_claim_supported": "YES",
                "date_coherent": "YES",
                "primary_evidence": "YES",
                "conflict_found": "NO",
            },
            "modality": "REALIZED",
            "decision": decision,
            "reason_code": (
                "PRIMARY_EVIDENCE_DIRECTLY_SUPPORTS"
                if decision == "CONFIRM_EVENT"
                else "NO_EXACT_PASSAGE"
            ),
            "selected_evidence_id": event["evidence"][0]["evidence_id"],
            "severity": "",
            "rationale": f"{reviewer_id} independently checked the exact primary passage and event identity.",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": 90,
        }
        results.append(row)
    return {
        "schema_version": 1,
        "contract_version": "event-fact-review-v1",
        "batch_id": assignment["batch_id"],
        "reviewer_slot": assignment["reviewer_slot"],
        "assignment_sha256": assignment["assignment_sha256"],
        "reviewer_id": reviewer_id,
        "attestation": True,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "complete": True,
        "results": results,
        "no_model_output": True,
        "no_market_outcome": True,
        "no_trading": True,
    }


def assignments(ledger_path: Path) -> tuple[dict, dict]:
    events = select_reviewable_events(ledger_path, limit=2)
    expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    return (
        build_assignment(events, batch_id="EFR-TEST", reviewer_slot="A", expires_at=expiry),
        build_assignment(events, batch_id="EFR-TEST", reviewer_slot="B", expires_at=expiry),
    )


def test_selection_is_evidence_ready_and_balanced(ledger_path: Path) -> None:
    events = select_reviewable_events(ledger_path, limit=2)
    assert {event["event_family"] for event in events} == {
        "bankruptcy_or_distress",
        "delisting_or_suspension",
    }
    assert all(event["evidence_fingerprint"] for event in events)
    assert all(event["evidence"][0]["authority_tier"] == "P0" for event in events)


def test_event_evidence_uses_latest_revision_without_materializing_full_view(
    ledger_path: Path,
) -> None:
    connection = event_ledger.open_ledger(ledger_path)
    try:
        source = connection.execute(
            """SELECT ee.event_id,ro.source_id,ro.external_id,ro.source_published_at,
                      ro.local_received_at,ro.title,ro.summary,ro.canonical_url,ro.raw_json
               FROM event_evidence ee
               JOIN raw_observations ro ON ro.observation_id=ee.observation_id
               ORDER BY ee.event_id,ee.evidence_id LIMIT 1"""
        ).fetchone()
        event_ledger.record_source_observation(
            connection,
            source_id=source["source_id"],
            external_id=source["external_id"],
            source_published_at=source["source_published_at"],
            local_received_at=source["local_received_at"],
            title=source["title"],
            summary=source["summary"],
            canonical_url=source["canonical_url"],
            content_sha256="latest-revision-content-sha256",
            raw_json=source["raw_json"],
            revision_kind="edit",
        )
        connection.commit()

        expected_rows = connection.execute(
            """SELECT ee.evidence_id,ee.evidence_url,ee.filing_date,ee.form,ee.items,
                      ee.evidence_passage,ee.passage_score,ee.evidence_status,
                      ro.content_sha256,ro.source_id,s.name AS source_name,
                      s.authority_tier,s.source_type
               FROM event_evidence ee
               LEFT JOIN latest_source_content ro ON ro.observation_id=ee.observation_id
               LEFT JOIN sources s ON s.source_id=ro.source_id
               WHERE ee.event_id=?
               ORDER BY CASE WHEN s.authority_tier='P0' THEN 0
                             WHEN s.authority_tier='P1' THEN 1 ELSE 2 END,
                        ee.passage_score DESC,ee.updated_at DESC,ee.evidence_id""",
            (source["event_id"],),
        ).fetchall()
        actual_rows = _event_evidence(connection, source["event_id"])
        assert len(actual_rows) == len(expected_rows)
        for actual, expected_row in zip(actual_rows, expected_rows, strict=True):
            expected = dict(expected_row)
            assert actual["evidence_id"] == expected["evidence_id"]
            assert actual["content_sha256"] == expected["content_sha256"]
            assert actual["source_id"] == expected["source_id"]
            assert actual["source_name"] == expected["source_name"]
            assert actual["authority_tier"] == expected["authority_tier"]
            assert actual["source_type"] == expected["source_type"]
        assert actual_rows[0]["content_sha256"] == "latest-revision-content-sha256"

        plan = connection.execute(
            "EXPLAIN QUERY PLAN " + _EVENT_EVIDENCE_SQL,
            (source["event_id"],),
        ).fetchall()
        details = [str(row[3]) for row in plan]
        assert not any("MATERIALIZE" in detail for detail in details)
        assert not any(detail.startswith("SCAN ") for detail in details)
        assert any("idx_evidence_event" in detail for detail in details)
        assert any("idx_source_revisions_observation" in detail for detail in details)
    finally:
        connection.close()


def test_submission_validation_rejects_copied_reasons_and_bad_confirmation(ledger_path: Path) -> None:
    assignment_a, _ = assignments(ledger_path)
    valid = submission(assignment_a, "reviewer-a")
    assert validate_submission(assignment_a, valid)["valid"] is True

    invalid = json.loads(json.dumps(valid))
    invalid["results"][0]["checks"]["subject_match"] = "UNCLEAR"
    report = validate_submission(assignment_a, invalid)
    assert report["valid"] is False
    assert any("five affirmative checks" in issue for issue in report["issues"])

    invalid_severity = json.loads(json.dumps(valid))
    invalid_severity["results"][0]["severity"] = "S"
    report = validate_submission(assignment_a, invalid_severity)
    assert report["valid"] is False
    assert any("severity must remain blank" in issue for issue in report["issues"])

    invalid_extra = json.loads(json.dumps(valid))
    invalid_extra["results"][0]["market_return"] = -0.45
    report = validate_submission(assignment_a, invalid_extra)
    assert report["valid"] is False
    assert any("unsupported fields" in issue for issue in report["issues"])


def test_merge_requires_independent_reviewers_and_separates_conflict(ledger_path: Path) -> None:
    assignment_a, assignment_b = assignments(ledger_path)
    review_a = submission(assignment_a, "reviewer-a")
    review_b = submission(assignment_b, "reviewer-b")
    review_b["results"][0]["decision"] = "NEEDS_EVIDENCE"
    review_b["results"][0]["reason_code"] = "NO_EXACT_PASSAGE"
    merged = merge_submissions(assignment_a, review_a, assignment_b, review_b)
    assert merged["consensus_count"] == 1
    assert merged["conflict_count"] == 1
    assert merged["formal_application"] is False

    duplicate_identity = submission(assignment_b, "reviewer-a")
    with pytest.raises(ValueError, match="independent reviewer"):
        merge_submissions(assignment_a, review_a, assignment_b, duplicate_identity)


def test_apply_is_authorized_atomic_and_stale_safe(ledger_path: Path) -> None:
    assignment_a, assignment_b = assignments(ledger_path)
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a"),
        assignment_b,
        submission(assignment_b, "reviewer-b"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "AUTH-EFR-TEST",
            "actor": "owner",
            "purpose": "Apply independently reviewed fixture facts.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    result = apply_consensus(ledger_path, merged, authorization)
    assert result["applied"] == 2
    connection = event_ledger.open_ledger(ledger_path)
    try:
        rows = connection.execute(
            "SELECT status,current_version,no_trading FROM canonical_events ORDER BY event_id"
        ).fetchall()
        assert {(row["status"], row["current_version"], row["no_trading"]) for row in rows} == {
            ("verified", 2, 1)
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM event_versions WHERE change_reason='dual_human_fact_review_v1'"
        ).fetchone()[0] == 2
    finally:
        connection.close()

    with pytest.raises(ValueError, match="STALE_REVIEW|no longer reviewable"):
        apply_consensus(ledger_path, merged, authorization)


def test_needs_evidence_keeps_followup_route_open(ledger_path: Path) -> None:
    assignment_a, assignment_b = assignments(ledger_path)
    merged = merge_submissions(
        assignment_a,
        submission(assignment_a, "reviewer-a", decision="NEEDS_EVIDENCE"),
        assignment_b,
        submission(assignment_b, "reviewer-b", decision="NEEDS_EVIDENCE"),
    )
    authorization = build_authorization_template(merged)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "AUTH-EFR-WEAK",
            "actor": "owner",
            "purpose": "Record the evidence gap without hiding follow-up work.",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
    )
    apply_consensus(ledger_path, merged, authorization)
    connection = event_ledger.open_ledger(ledger_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM canonical_events WHERE status='weak'"
        ).fetchone()[0] == 2
        statuses = {
            row[0]
            for row in connection.execute(
                "SELECT status FROM pipeline_jobs WHERE event_id IS NOT NULL"
            ).fetchall()
        }
        assert statuses == {"PENDING_PRIMARY_EVIDENCE"}
    finally:
        connection.close()
