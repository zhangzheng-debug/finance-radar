"""Backward-compatible imports for the canonical evidence policy."""

from app.evidence_policy import (  # noqa: F401
    CONFLICTING_EVIDENCE_STATUSES,
    DUAL_HUMAN_EVIDENCE_STATUS,
    DUAL_HUMAN_SELECTED_EVIDENCE_RECEIPT_V1,
    DUAL_HUMAN_SELECTED_EVIDENCE_RECEIPT_VERSION,
    HUMAN_FACT_CLAIM_CONTRACT_VERSION,
    STANDARD_READER_EVIDENCE_STATUSES,
    build_dual_human_selected_evidence_receipt,
    canonicalize_human_fact_claim,
    dual_human_selected_evidence_receipt_matches,
    is_conflicting_evidence_status,
    is_http_evidence_url,
    is_primary_authority_tier,
    is_reader_supporting_evidence,
    is_strict_dual_human_evidence,
    normalize_evidence_status,
    strict_selected_evidence_issues,
)
