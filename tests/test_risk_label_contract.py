from __future__ import annotations

import hashlib

from app.models.risk_label_contract import (
    coherent_label,
    deterministic_source_lane,
    validate_annotation,
)
from scripts.audit_risk_label_contract_v3 import audit_rows


def valid_row(**updates):
    row = {
        "sample_id": "sample-1",
        "text_sha256": hashlib.sha256(b"content").hexdigest(),
        "content_present": True,
        "source_id": "sec_current_filings",
        "authority_tier": "P0_official",
        "source_lane": "P0_EVIDENCE_READY",
        "entity_group": "entity-1",
        "event_chain_group": "chain-1",
        "label": "RISK_REVIEW",
        "materiality": "MATERIAL_ADVERSE",
        "polarity": "ADVERSE",
        "evidence_state": "PRIMARY_SUPPORTED",
        "rationale": "Primary filing states a material adverse bankruptcy event.",
        "adjudicator_id": "student-a",
        "reviewer_id": "student-b",
        "adjudicated_at": "2026-07-19T00:00:00Z",
        "source_used_as_label": False,
        "split": "UNASSIGNED",
    }
    row.update(updates)
    return row


def test_source_lane_never_returns_a_target_label() -> None:
    lanes = {
        deterministic_source_lane("P0_official", "PRIMARY_SUPPORTED"),
        deterministic_source_lane("P0_official", "DISCOVERY_ONLY"),
        deterministic_source_lane("P1_issuer_official", "PRIMARY_SUPPORTED"),
        deterministic_source_lane("P2_experimental", "DISCOVERY_ONLY"),
    }
    assert lanes == {
        "P0_EVIDENCE_READY",
        "P0_EVIDENCE_REQUIRED",
        "P1_ISSUER_CONTEXT",
        "DISCOVERY_ONLY",
    }
    assert not lanes & {"RISK_REVIEW", "NON_TARGET", "ABSTAIN"}


def test_coherent_label_requires_content_axes_and_finalizable_evidence() -> None:
    assert coherent_label("MATERIAL_ADVERSE", "ADVERSE", "PRIMARY_SUPPORTED") == "RISK_REVIEW"
    assert coherent_label("NOT_MATERIAL_ADVERSE", "POSITIVE", "PRIMARY_SUPPORTED") == "NON_TARGET"
    assert coherent_label("MATERIAL_ADVERSE", "ADVERSE", "DISCOVERY_ONLY") == "ABSTAIN"
    assert coherent_label("UNCLEAR", "UNCLEAR", "CONFLICTED") == "ABSTAIN"


def test_valid_dual_adjudication_passes() -> None:
    assert validate_annotation(valid_row()) == []


def test_source_derived_or_pre_split_row_is_rejected() -> None:
    issues = validate_annotation(valid_row(source_used_as_label=True, split="train"))
    assert "invalid:source_used_as_label" in issues
    assert "invalid:pre_freeze_split" in issues


def test_label_must_match_independent_axes() -> None:
    issues = validate_annotation(
        valid_row(label="RISK_REVIEW", materiality="NOT_MATERIAL_ADVERSE", polarity="POSITIVE")
    )
    assert "mismatch:label_axes" in issues


def test_old_candidate_manifest_shape_is_not_blind_v2_ready() -> None:
    report = audit_rows(
        [
            {
                "event_id": "legacy-candidate",
                "label": "RISK_REVIEW",
                "label_basis": "verified official source",
                "split": "test",
                "text_sha256": hashlib.sha256(b"title only").hexdigest(),
            }
        ],
        source="unit-test",
    )
    assert report["status"] == "NOT_READY_FOR_BLIND_V2"
    assert report["valid_rows"] == 0
    assert report["issue_counts"]["legacy:preassigned_split"] == 1
    assert report["issue_counts"]["legacy:source_or_corpus_label_basis"] == 1
    assert report["production_changed"] is False
    assert report["no_blind_v2_claim"] is True
