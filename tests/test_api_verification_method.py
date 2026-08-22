from __future__ import annotations

import json

from app.api.main import public_verification_method


def _dual_review_facts() -> dict[str, object]:
    return {
        "human_fact_claim": {
            "contract_version": "human-fact-claim-v1",
            "canonical_claim_sha256": "c" * 64,
            "public_fact_summary_sha256": "d" * 64,
        },
        "dual_human_fact_review": {
            "contract_version": "event-fact-review-v2",
            "canonical_claim_sha256": "c" * 64,
            "public_fact_summary_sha256": "d" * 64,
            "target_status": "verified",
            "selected_evidence_id": "evidence-1",
            "reviewers": {"A": "alice-internal", "B": "bob-internal"},
            "reviewer_rationales": {"alice-internal": "secret rationale"},
            "submission_sha256": {"alice-internal": "a" * 64},
            "authorization": {"actor": "owner-secret"},
            "applied_at": "2026-08-20T08:00:00+00:00",
        }
    }


def _current_selected_evidence() -> dict[str, object]:
    return {
        "evidence_id": "evidence-1",
        "evidence_status": "accepted_dual_human_primary_evidence",
        "relation_status": "HUMAN_CONFIRMED",
        "subject_match": 1,
        "event_claim_supported": 1,
        "date_coherent": 1,
        "dual_human_receipt_consistent": 1,
        "reader_eligible": 1,
    }


def test_dual_human_verification_method_is_current_and_deidentified() -> None:
    result = public_verification_method(
        _dual_review_facts(),
        [_current_selected_evidence()],
    )

    assert result == {
        "kind": "dual_human_fact_review",
        "version": "event-fact-review-v2",
        "reviewed_at": "2026-08-20T08:00:00+00:00",
        "evidence_ids": ["evidence-1"],
        "independent_reviews": 2,
        "no_trading": True,
    }
    encoded = json.dumps(result, sort_keys=True)
    assert "alice-internal" not in encoded
    assert "bob-internal" not in encoded
    assert "owner-secret" not in encoded
    assert "secret rationale" not in encoded
    assert "a" * 64 not in encoded


def test_dual_human_verification_method_fails_closed_for_stale_receipt() -> None:
    evidence = _current_selected_evidence()
    evidence["dual_human_receipt_consistent"] = 0
    assert public_verification_method(_dual_review_facts(), [evidence]) is None


def test_legacy_v1_confirmation_never_exposes_a_public_verification_method() -> None:
    facts = _dual_review_facts()
    facts["dual_human_fact_review"]["contract_version"] = "event-fact-review-v1"
    assert public_verification_method(facts, [_current_selected_evidence()]) is None


def test_light_verification_normalizes_null_evidence_ids() -> None:
    result = public_verification_method(
        {"light_verification": {"version": "light-v1", "evidence_ids": None}},
        [],
    )
    assert result is not None
    assert result["evidence_ids"] == []
