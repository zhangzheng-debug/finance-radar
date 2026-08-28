from __future__ import annotations

import copy

import pytest

from app.evidence_policy import (
    build_dual_human_selected_evidence_receipt,
    dual_human_selected_evidence_receipt_matches,
    is_http_evidence_url,
    is_primary_authority_tier,
)


def _selected_evidence() -> dict[str, object]:
    return {
        "evidence_id": "evidence-1",
        "evidence_status": "candidate_passage",
        "evidence_url": "https://www.sec.gov/Archives/example",
        "evidence_passage": (
            "Example Corp disclosed in this exact primary-source passage that "
            "its chief financial officer resigned effective immediately."
        ),
        "authority_tier": "P0",
        "source_id": "sec-edgar",
        "content_sha256": "a" * 64,
        "observation_status": "captured",
        "latest_revision_no": 2,
        "latest_revision_kind": "edit",
        "passage_currently_proven": 1,
    }


_CLAIM_RECEIPT_ARGS = {
    "canonical_claim_sha256": "c" * 64,
    "public_fact_summary_sha256": "d" * 64,
}


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("evidence_url", "ftp://sec.example/filing", "MISSING_HTTP_EVIDENCE_URL"),
        ("evidence_url", "http://127.0.0.1/filing", "MISSING_HTTP_EVIDENCE_URL"),
        (
            "evidence_url",
            "https://user:secret@sec.example/filing",
            "MISSING_HTTP_EVIDENCE_URL",
        ),
        ("evidence_passage", "short", "MISSING_EXACT_PASSAGE"),
        ("authority_tier", "P2", "SOURCE_NOT_EXACT_P0_P1"),
        ("authority_tier", "P00", "SOURCE_NOT_EXACT_P0_P1"),
        ("authority_tier", "P01_official", "SOURCE_NOT_EXACT_P0_P1"),
        ("authority_tier", "P10", "SOURCE_NOT_EXACT_P0_P1"),
        ("authority_tier", "P0OFFICIAL", "SOURCE_NOT_EXACT_P0_P1"),
        ("passage_currently_proven", 0, "SOURCE_REVISION_PASSAGE_NOT_PROVEN"),
    ),
)
def test_dual_receipt_rejects_non_citable_selected_evidence(
    field: str,
    value: object,
    reason: str,
) -> None:
    evidence = _selected_evidence()
    evidence[field] = value
    with pytest.raises(ValueError, match=reason):
        build_dual_human_selected_evidence_receipt(
            evidence,
            event_id="event-1",
            event_version=2,
            evidence_fingerprint_before="fingerprint-v1",
            **_CLAIM_RECEIPT_ARGS,
        )


@pytest.mark.parametrize(
    "tier",
    ("P0", "P1", "P0_official", "P1_issuer_official", "p0_official"),
)
def test_primary_authority_tier_accepts_canonical_qualified_values(tier: str) -> None:
    assert is_primary_authority_tier(tier)
    evidence = _selected_evidence()
    evidence["authority_tier"] = tier
    receipt = build_dual_human_selected_evidence_receipt(
        evidence,
        event_id="event-1",
        event_version=2,
        evidence_fingerprint_before="fingerprint-v1",
        **_CLAIM_RECEIPT_ARGS,
    )
    assert receipt["source_authority_tier"] == tier.upper()


@pytest.mark.parametrize("tier", ("", "P2", "P00", "P01", "P10", "P0OFFICIAL"))
def test_primary_authority_tier_rejects_discovery_and_lookalike_prefixes(tier: str) -> None:
    assert not is_primary_authority_tier(tier)


@pytest.mark.parametrize(
    "url",
    (
        "http://localhost/filing",
        "http://10.0.0.8/filing",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:secret@example.com/filing",
        "file:///etc/passwd",
    ),
)
def test_evidence_url_must_be_safe_for_public_readers(url: str) -> None:
    assert is_http_evidence_url(url) is False


def test_dual_receipt_binds_current_source_revision_and_checksum() -> None:
    evidence = _selected_evidence()
    receipt = build_dual_human_selected_evidence_receipt(
        evidence,
        event_id="event-1",
        event_version=2,
        evidence_fingerprint_before="fingerprint-v1",
        **_CLAIM_RECEIPT_ARGS,
    )
    accepted = {**evidence, "evidence_status": "accepted_dual_human_primary_evidence"}
    assert dual_human_selected_evidence_receipt_matches(
        receipt,
        accepted,
        event_id="event-1",
        event_version=2,
        evidence_fingerprint_before="fingerprint-v1",
        **_CLAIM_RECEIPT_ARGS,
    )

    revised = {**accepted, "latest_revision_no": 3, "content_sha256": "b" * 64}
    assert not dual_human_selected_evidence_receipt_matches(
        receipt,
        revised,
        event_id="event-1",
        event_version=2,
        evidence_fingerprint_before="fingerprint-v1",
        **_CLAIM_RECEIPT_ARGS,
    )
    tampered = copy.deepcopy(receipt)
    tampered["source_authority_tier"] = "P1"
    assert not dual_human_selected_evidence_receipt_matches(
        tampered,
        accepted,
        event_id="event-1",
        event_version=2,
        evidence_fingerprint_before="fingerprint-v1",
        **_CLAIM_RECEIPT_ARGS,
    )
