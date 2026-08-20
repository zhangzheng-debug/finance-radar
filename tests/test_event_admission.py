from __future__ import annotations

from app.services.event_admission import evaluate_event_admission


BASE = {
    "event_id": "event-1",
    "event_version": 1,
    "evidence_id": "evidence-1",
    "subject": "Example Corporation",
    "action": "management_change",
    "stage": "DISCLOSED",
    "known_at": "2026-08-20T01:02:03+00:00",
    "source_authority_tier": "P0_official",
    "evidence_url": "https://www.sec.gov/Archives/example.htm",
    "evidence_passage": (
        "Example Corporation disclosed that its chief financial officer resigned "
        "effective immediately on August 20, 2026."
    ),
    "evidence_status": "machine_extracted_unreviewed",
    "content_sha256": "a" * 64,
    "subject_match": True,
    "event_claim_supported": True,
    "date_coherent": True,
}


def decision(**overrides):
    payload = {**BASE, **overrides}
    return evaluate_event_admission(**payload)


def test_admission_accepts_only_a_scoped_primary_evidence_claim() -> None:
    result = decision()

    assert result.admitted is True
    assert result.workflow_state == "EVIDENCE_READY"
    assert result.reasons == ()
    assert len(result.evidence_fingerprint) == 64


def test_admission_rejects_non_decision_and_subject_mismatch() -> None:
    result = decision(
        evidence_status="machine_extracted_non_decision",
        subject_match=False,
    )

    assert result.admitted is False
    assert result.workflow_state == "NEEDS_EVIDENCE"
    assert "EVIDENCE_STATUS_NOT_SUPPORTIVE" in result.reasons
    assert "EVIDENCE_STATUS_EXPLICITLY_BLOCKED" in result.reasons
    assert "SUBJECT_NOT_BOUND_TO_EVIDENCE" in result.reasons


def test_admission_rejects_missing_time_hash_and_non_primary_source() -> None:
    result = decision(
        known_at="2026-08-20",
        content_sha256="not-a-hash",
        source_authority_tier="P2_discovery",
    )

    assert result.admitted is False
    assert "MISSING_OR_NAIVE_KNOWN_AT" in result.reasons
    assert "MISSING_SOURCE_CONTENT_HASH" in result.reasons
    assert "SOURCE_NOT_P0_P1" in result.reasons


def test_admission_rejects_a_link_without_an_exact_supporting_passage() -> None:
    result = decision(evidence_passage="See filing.", event_claim_supported=False)

    assert result.admitted is False
    assert "MISSING_EXACT_PASSAGE" in result.reasons
    assert "EVENT_PREDICATE_NOT_SUPPORTED" in result.reasons
