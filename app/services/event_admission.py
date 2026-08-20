from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


ADMISSION_CONTRACT_VERSION = "event-admission-v1"
DISCOVERY_LEAD_STATES = {
    "PENDING_ENRICHMENT",
    "NEEDS_EVIDENCE",
    "LEAD_NO_SCOPED_EVENT",
    "READY_FOR_CANONICAL",
    "PROMOTED",
    "DUPLICATE",
    "EXCLUDED",
}
EVENT_FACT_STATES = {
    "NEEDS_EVIDENCE",
    "EVIDENCE_READY",
    "NEEDS_HUMAN",
    "DUPLICATE",
    "EXCLUDED",
}
SUPPORTED_RELATION_STATES = {"SCOPED_MATCH", "HUMAN_CONFIRMED"}
READER_ALLOWED_EVIDENCE_STATUSES = {
    "machine_extracted_unreviewed",
    "candidate_passage",
    "confirmed_primary",
    "accepted_manual_primary_evidence",
}
READER_BLOCKED_EVIDENCE_STATUSES = {
    "machine_extracted_non_decision",
    "attachment_incomplete",
    "link_only_no_relevant_passage",
    "no_keyword_passage",
}
EVENT_STAGES = {
    "PROPOSED",
    "FILED",
    "DISCLOSED",
    "EFFECTIVE",
    "ONGOING",
    "COMPLETED",
}


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    workflow_state: str
    reasons: tuple[str, ...]
    evidence_fingerprint: str


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _is_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _aware_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def evidence_relation_fingerprint(
    *,
    event_id: str,
    event_version: int,
    evidence_id: str,
    content_sha256: str,
    subject: str,
    action: str,
    stage: str,
    known_at: str,
) -> str:
    payload = {
        "action": _clean(action),
        "content_sha256": _clean(content_sha256).lower(),
        "event_id": _clean(event_id),
        "event_version": int(event_version),
        "evidence_id": _clean(evidence_id),
        "known_at": _clean(known_at),
        "stage": _clean(stage).upper(),
        "subject": _clean(subject),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evaluate_event_admission(
    *,
    event_id: str,
    event_version: int,
    evidence_id: str,
    subject: str,
    action: str,
    stage: str,
    known_at: str,
    source_authority_tier: str,
    evidence_url: str,
    evidence_passage: str,
    evidence_status: str,
    content_sha256: str,
    subject_match: bool,
    event_claim_supported: bool,
    date_coherent: bool,
) -> AdmissionDecision:
    """Fail closed before a discovery lead may become a canonical event claim.

    Passing this contract creates an evidence-supported *candidate*.  It does
    not verify the fact, assign materiality, create a trading signal, or grant
    an automated canonical-conclusion write.
    """

    normalized_stage = _clean(stage).upper()
    normalized_status = _clean(evidence_status)
    reasons: list[str] = []
    if len(_clean(subject)) < 2:
        reasons.append("MISSING_NAMED_SUBJECT")
    if len(_clean(action)) < 3:
        reasons.append("MISSING_EVENT_ACTION")
    if normalized_stage not in EVENT_STAGES:
        reasons.append("MISSING_OR_INVALID_STAGE")
    if not _aware_timestamp(_clean(known_at)):
        reasons.append("MISSING_OR_NAIVE_KNOWN_AT")
    if not _clean(source_authority_tier).upper().startswith(("P0", "P1")):
        reasons.append("SOURCE_NOT_P0_P1")
    if not _is_http_url(_clean(evidence_url)):
        reasons.append("MISSING_CITABLE_URL")
    if len(_clean(evidence_passage)) < 40:
        reasons.append("MISSING_EXACT_PASSAGE")
    if normalized_status not in READER_ALLOWED_EVIDENCE_STATUSES:
        reasons.append("EVIDENCE_STATUS_NOT_SUPPORTIVE")
    if normalized_status in READER_BLOCKED_EVIDENCE_STATUSES:
        reasons.append("EVIDENCE_STATUS_EXPLICITLY_BLOCKED")
    if not subject_match:
        reasons.append("SUBJECT_NOT_BOUND_TO_EVIDENCE")
    if not event_claim_supported:
        reasons.append("EVENT_PREDICATE_NOT_SUPPORTED")
    if not date_coherent:
        reasons.append("EVENT_DATE_NOT_COHERENT")
    if len(_clean(content_sha256)) != 64:
        reasons.append("MISSING_SOURCE_CONTENT_HASH")

    fingerprint = evidence_relation_fingerprint(
        event_id=event_id,
        event_version=event_version,
        evidence_id=evidence_id,
        content_sha256=content_sha256,
        subject=subject,
        action=action,
        stage=normalized_stage,
        known_at=known_at,
    )
    return AdmissionDecision(
        admitted=not reasons,
        workflow_state="EVIDENCE_READY" if not reasons else "NEEDS_EVIDENCE",
        reasons=tuple(reasons),
        evidence_fingerprint=fingerprint,
    )


def public_fact_summary(*, subject: str, action_label: str, stage_label: str) -> str:
    return (
        f"{_clean(subject)} 的权威原始文件中出现了与“{_clean(action_label)}”有关的"
        f"明确段落；当前记录阶段为“{_clean(stage_label)}”，尚待人工核验。"
    )
