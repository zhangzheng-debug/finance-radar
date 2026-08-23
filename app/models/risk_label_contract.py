"""Evidence-first label contract for a future risk-router blind-v2 dataset.

This module is deliberately independent from the deployed v1 model. It defines
how human-adjudicated content may become training/evaluation data without using
source identity as the target label.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from app.evidence_policy import is_primary_authority_tier


LABELS = {"RISK_REVIEW", "NON_TARGET", "ABSTAIN"}
MATERIALITY = {"MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "UNCLEAR"}
POLARITIES = {"ADVERSE", "POSITIVE", "NEUTRAL", "MIXED", "UNCLEAR"}
EVIDENCE_STATES = {
    "PRIMARY_SUPPORTED",
    "MULTI_SOURCE_SUPPORTED",
    "DISCOVERY_ONLY",
    "CONFLICTED",
    "INSUFFICIENT",
}
FINALIZABLE_EVIDENCE = {"PRIMARY_SUPPORTED", "MULTI_SOURCE_SUPPORTED"}
REQUIRED_FIELDS = {
    "sample_id",
    "text_sha256",
    "content_present",
    "source_id",
    "authority_tier",
    "source_lane",
    "entity_group",
    "event_chain_group",
    "label",
    "materiality",
    "polarity",
    "evidence_state",
    "rationale",
    "adjudicator_id",
    "reviewer_id",
    "adjudicated_at",
    "source_used_as_label",
    "split",
}
AXIS_FIELDS = ("materiality", "polarity", "evidence_state")


def deterministic_source_lane(authority_tier: str, evidence_state: str) -> str:
    """Route evidence acquisition deterministically without deciding polarity."""
    tier = str(authority_tier or "").upper()
    evidence = str(evidence_state or "").upper()
    if not is_primary_authority_tier(tier):
        return "DISCOVERY_ONLY"
    if tier.split("_", 1)[0] == "P0":
        return "P0_EVIDENCE_READY" if evidence in FINALIZABLE_EVIDENCE else "P0_EVIDENCE_REQUIRED"
    return "P1_ISSUER_CONTEXT"


def coherent_label(materiality: str, polarity: str, evidence_state: str) -> str:
    """Return the only label coherent with independently adjudicated axes."""
    materiality = str(materiality or "").upper()
    polarity = str(polarity or "").upper()
    evidence_state = str(evidence_state or "").upper()
    if evidence_state not in FINALIZABLE_EVIDENCE:
        return "ABSTAIN"
    if materiality == "MATERIAL_ADVERSE" and polarity in {"ADVERSE", "MIXED"}:
        return "RISK_REVIEW"
    if materiality == "NOT_MATERIAL_ADVERSE" and polarity in {"POSITIVE", "NEUTRAL", "MIXED"}:
        return "NON_TARGET"
    return "ABSTAIN"


def _valid_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_annotation(row: dict[str, Any]) -> list[str]:
    """Validate one pre-freeze human annotation under the v3 contract."""
    issues: list[str] = []
    missing = sorted(field for field in REQUIRED_FIELDS if field not in row)
    issues.extend(f"missing:{field}" for field in missing)
    if missing:
        return issues

    if row["label"] not in LABELS:
        issues.append("invalid:label")
    if row["materiality"] not in MATERIALITY:
        issues.append("invalid:materiality")
    if row["polarity"] not in POLARITIES:
        issues.append("invalid:polarity")
    if row["evidence_state"] not in EVIDENCE_STATES:
        issues.append("invalid:evidence_state")

    expected_lane = deterministic_source_lane(row["authority_tier"], row["evidence_state"])
    if row["source_lane"] != expected_lane:
        issues.append("mismatch:source_lane")
    expected_label = coherent_label(row["materiality"], row["polarity"], row["evidence_state"])
    if row["label"] in LABELS and row["label"] != expected_label:
        issues.append("mismatch:label_axes")

    if row["content_present"] is not True:
        issues.append("invalid:content_present")
    if not isinstance(row["text_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["text_sha256"]):
        issues.append("invalid:text_sha256")
    if not isinstance(row["rationale"], str) or len(row["rationale"].strip()) < 20:
        issues.append("invalid:rationale")
    if not str(row["entity_group"] or "").strip():
        issues.append("invalid:entity_group")
    if not str(row["event_chain_group"] or "").strip():
        issues.append("invalid:event_chain_group")
    if not str(row["adjudicator_id"] or "").strip():
        issues.append("invalid:adjudicator_id")
    if not str(row["reviewer_id"] or "").strip():
        issues.append("invalid:reviewer_id")
    if str(row["adjudicator_id"]).strip() == str(row["reviewer_id"]).strip():
        issues.append("invalid:independent_review")
    if not _valid_iso_datetime(row["adjudicated_at"]):
        issues.append("invalid:adjudicated_at")
    if row["source_used_as_label"] is not False:
        issues.append("invalid:source_used_as_label")
    if row["split"] != "UNASSIGNED":
        issues.append("invalid:pre_freeze_split")
    return issues


def reviews_agree(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Compare only independently assigned axes, never a derived target label."""
    return all(first.get(field) == second.get(field) for field in AXIS_FIELDS)


def build_dual_review_annotation(
    sample: dict[str, Any],
    reviews: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one pre-freeze row from two reviews and optional conflict arbitration.

    Reviewers never submit a target label. The label and source lane are derived
    only after independent review, which makes source identity unavailable as a
    shortcut for the human target decision.
    """
    reviewers = [row for row in reviews if row.get("review_role") == "REVIEWER"]
    arbiters = [row for row in reviews if row.get("review_role") == "ARBITER"]
    if len(reviewers) != 2:
        raise ValueError("exactly two independent reviewer rows are required")
    reviewer_ids = [str(row.get("reviewer_id") or "").strip() for row in reviewers]
    if not all(reviewer_ids) or reviewer_ids[0] == reviewer_ids[1]:
        raise ValueError("two distinct reviewer identities are required")

    if reviews_agree(reviewers[0], reviewers[1]):
        if arbiters:
            raise ValueError("matching reviews must not be arbitrated")
        decisive = reviewers[0]
        resolution = "CONSENSUS"
        adjudicator_id = reviewer_ids[0]
        reviewer_id = reviewer_ids[1]
    else:
        if len(arbiters) != 1:
            raise ValueError("conflicting reviews require exactly one arbiter")
        decisive = arbiters[0]
        arbiter_id = str(decisive.get("reviewer_id") or "").strip()
        if not arbiter_id or arbiter_id in reviewer_ids:
            raise ValueError("arbiter must be a third independent reviewer")
        resolution = "ARBITRATED"
        adjudicator_id = arbiter_id
        reviewer_id = reviewer_ids[0]

    rationale_parts = [
        f"{row['review_role']}:{row['reviewer_id']} — {str(row.get('rationale') or '').strip()}"
        for row in reviews
    ]
    annotation = {
        "sample_id": sample["sample_id"],
        "event_id": sample.get("event_id"),
        "text_sha256": sample["text_sha256"],
        "content_present": bool(sample.get("content")),
        "content": sample.get("content"),
        "source_id": sample["source_id"],
        "authority_tier": sample["authority_tier"],
        "source_lane": deterministic_source_lane(
            sample["authority_tier"], decisive["evidence_state"]
        ),
        "entity_group": sample["entity_group"],
        "event_chain_group": sample["event_chain_group"],
        "label": coherent_label(
            decisive["materiality"], decisive["polarity"], decisive["evidence_state"]
        ),
        "materiality": decisive["materiality"],
        "polarity": decisive["polarity"],
        "evidence_state": decisive["evidence_state"],
        "rationale": " | ".join(rationale_parts),
        "adjudicator_id": adjudicator_id,
        "reviewer_id": reviewer_id,
        "adjudicated_at": decisive["created_at"],
        "source_used_as_label": False,
        "split": "UNASSIGNED",
        "resolution": resolution,
        "review_ids": [row.get("review_id") for row in reviews],
    }
    issues = validate_annotation(annotation)
    if issues:
        raise ValueError("invalid dual-review annotation: " + ", ".join(issues))
    return annotation
