"""Offline, human-only dual-blind review contract for risk-router gold labels.

The public reviewer assignments contain only frozen, source-masked content and
slot-specific anonymous tokens.  Reviewers submit the three independent axes;
the target label is derived by :mod:`app.models.risk_label_contract` only after
two submissions agree or a distinct third human resolves the conflict.

This module never mutates the event ledger, adjudication store, model, or any
market/trading state.  Its outputs remain ``UNASSIGNED`` until the existing
freeze workflow explicitly accepts them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from app.models.risk_label_contract import (
    EVIDENCE_STATES,
    MATERIALITY,
    POLARITIES,
    build_dual_review_annotation,
)
from app.services.adjudication import HUMAN_BLIND_CONTRACT_VERSION


OFFLINE_GOLD_CONTRACT_VERSION = "human-gold-offline-v1"
REVIEW_ROLES = {"REVIEWER", "ARBITER"}
REQUIRED_ATTESTATIONS = {
    "human_only",
    "independent_judgment",
    "no_ai_assistance",
    "no_model_output",
    "no_market_outcome",
    "no_old_label",
}
RESULT_FIELDS = {
    "sample_token",
    "materiality",
    "polarity",
    "evidence_state",
    "rationale",
    "reviewed_at",
    "duration_seconds",
}
SUBMISSION_FIELDS = {
    "schema_version",
    "contract_version",
    "batch_id",
    "reviewer_slot",
    "review_role",
    "reviewer_token",
    "assignment_sha256",
    "attestations",
    "peer_answers_hidden",
    "exported_at",
    "complete",
    "results",
    "target_label_submitted",
    "canonical_state_changed",
    "no_trading",
}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _valid_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _assignment_digest(assignment: dict[str, Any]) -> str:
    payload = {key: value for key, value in assignment.items() if key != "assignment_sha256"}
    return sha256_text(stable_json(payload))


def reviewer_principal(reviewer_token: str) -> str:
    """Bind an anonymous offline credential to the existing SHA-256 identity contract."""

    return sha256_text(
        f"finance-radar-human-gold-offline-v1:{reviewer_token}".encode("utf-8").hex()
    )


def _sample_token(secret: str, slot: str, sample_id: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{slot}:{sample_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"HG-{digest[:20]}"


def _order_key(secret: str, slot: str, sample_id: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"order:{slot}:{sample_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _masked_content(sample: dict[str, Any], secret: str, slot: str) -> dict[str, Any]:
    content = sample.get("content")
    if not isinstance(content, dict):
        raise ValueError(f"sample {sample.get('sample_id')} has no content object")
    if content.get("contract_version") != HUMAN_BLIND_CONTRACT_VERSION:
        raise ValueError(
            f"sample {sample.get('sample_id')} is not {HUMAN_BLIND_CONTRACT_VERSION} content"
        )
    required_flags = {
        "source_identity_hidden": True,
        "target_label_hidden": True,
        "post_event_market_data_included": False,
        "model_output_included": False,
    }
    for field, expected in required_flags.items():
        if content.get(field) is not expected:
            raise ValueError(f"sample {sample.get('sample_id')} violates {field}={expected}")

    passages = []
    for index, row in enumerate(content.get("passages") or [], 1):
        if not isinstance(row, dict) or not _clean_text(row.get("passage")):
            continue
        passage_token = _sample_token(
            secret,
            f"{slot}:P{index}",
            str(sample.get("sample_id") or ""),
        )
        passages.append(
            {
                "passage_token": passage_token,
                "authority_context": _clean_text(row.get("authority_class")),
                "document_type": _clean_text(row.get("document_type")),
                "item_section": _clean_text(row.get("item_section")),
                "published_at": row.get("published_at"),
                "received_at": row.get("received_at"),
                "passage": _clean_text(row.get("passage")),
            }
        )
    if not passages:
        raise ValueError(f"sample {sample.get('sample_id')} has no reviewable passage")
    return {
        "as_of": content.get("as_of"),
        "event_date": content.get("event_date"),
        "headline": _clean_text(content.get("headline")),
        "summary": _clean_text(content.get("summary")),
        "context_facts": [
            _clean_text(item)
            for item in (content.get("confirmed_facts") or [])
            if _clean_text(item)
        ][:8],
        "passages": passages,
        "source_identity_hidden": True,
        "target_label_hidden": True,
        "post_event_market_data_included": False,
        "model_output_included": False,
    }


def _validate_owner_sample(sample: dict[str, Any]) -> None:
    required = {
        "sample_id",
        "text_sha256",
        "content",
        "source_id",
        "authority_tier",
        "entity_group",
        "event_chain_group",
    }
    missing = sorted(required - sample.keys())
    if missing:
        raise ValueError(f"sample missing fields: {', '.join(missing)}")
    if not _clean_text(sample["sample_id"]):
        raise ValueError("sample_id must not be blank")
    if not _is_sha256(sample["text_sha256"]):
        raise ValueError(f"sample {sample['sample_id']} has invalid text_sha256")
    if "label" in sample or "model_prediction" in sample or "market_outcome" in sample:
        raise ValueError(f"sample {sample['sample_id']} contains prohibited pre-label/output data")
    if not _clean_text(sample["entity_group"]) or not _clean_text(sample["event_chain_group"]):
        raise ValueError(f"sample {sample['sample_id']} lacks grouping metadata")


def build_offline_batch(
    samples: Iterable[dict[str, Any]],
    *,
    batch_id: str,
    expires_at: str,
    batch_secret: str | None = None,
    reviewer_tokens: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build two independently ordered, anonymously tokenized reviewer assignments."""

    normalized = [dict(sample) for sample in samples]
    if not normalized:
        raise ValueError("at least one sample is required")
    if not _clean_text(batch_id):
        raise ValueError("batch_id must not be blank")
    if not _valid_iso_datetime(expires_at):
        raise ValueError("expires_at must be a timezone-aware ISO datetime")
    ids = [str(sample.get("sample_id") or "") for sample in normalized]
    if len(set(ids)) != len(ids):
        raise ValueError("sample_id values must be unique")
    for sample in normalized:
        _validate_owner_sample(sample)

    secret = batch_secret or secrets.token_urlsafe(32)
    tokens = dict(reviewer_tokens or {})
    for slot in ("A", "B"):
        tokens.setdefault(slot, secrets.token_urlsafe(32))
    if not all(_clean_text(tokens.get(slot)) for slot in ("A", "B")):
        raise ValueError("both reviewer tokens are required")
    if tokens["A"] == tokens["B"]:
        raise ValueError("reviewer tokens must be distinct")

    assignments: dict[str, dict[str, Any]] = {}
    token_maps: dict[str, dict[str, str]] = {}
    orders: dict[str, list[str]] = {}
    for slot in ("A", "B"):
        ordered = sorted(normalized, key=lambda row: _order_key(secret, slot, row["sample_id"]))
        orders[slot] = [row["sample_id"] for row in ordered]
        token_map = {
            _sample_token(secret, slot, row["sample_id"]): row["sample_id"] for row in ordered
        }
        token_maps[slot] = token_map
        events = []
        for row in ordered:
            sample_token = next(token for token, sample_id in token_map.items() if sample_id == row["sample_id"])
            masked = _masked_content(row, secret, slot)
            events.append(
                {
                    "sample_token": sample_token,
                    "content_sha256": sha256_text(stable_json(masked)),
                    "content": masked,
                    "peer_answers_hidden": True,
                    "target_label_hidden": True,
                    "no_model_prediction_shown": True,
                    "no_market_outcome_shown": True,
                    "old_labels_hidden": True,
                }
            )
        assignment = {
            "schema_version": 1,
            "contract_version": OFFLINE_GOLD_CONTRACT_VERSION,
            "content_contract_version": HUMAN_BLIND_CONTRACT_VERSION,
            "batch_id": batch_id,
            "reviewer_slot": slot,
            "review_role": "REVIEWER",
            "reviewer_token": tokens[slot],
            "expires_at": expires_at,
            "sample_count": len(events),
            "events": events,
            "axes": {
                "materiality": sorted(MATERIALITY),
                "polarity": sorted(POLARITIES),
                "evidence_state": sorted(EVIDENCE_STATES),
            },
            "target_label_submitted": False,
            "peer_answers_hidden": True,
            "human_only": True,
            "ai_assistance_allowed": False,
            "model_output_included": False,
            "post_event_market_data_included": False,
            "old_labels_included": False,
            "canonical_state_changed": False,
            "no_trading": True,
        }
        assignment["assignment_sha256"] = _assignment_digest(assignment)
        assignments[slot] = assignment

    # Independent randomization must be observable even for an unlucky two-item draw.
    if len(normalized) > 1 and orders["A"] == orders["B"]:
        assignments["B"]["events"] = assignments["B"]["events"][1:] + assignments["B"]["events"][:1]
        orders["B"] = orders["B"][1:] + orders["B"][:1]
        assignments["B"]["assignment_sha256"] = _assignment_digest(assignments["B"])

    owner_manifest = {
        "schema_version": 1,
        "contract_version": OFFLINE_GOLD_CONTRACT_VERSION,
        "content_contract_version": HUMAN_BLIND_CONTRACT_VERSION,
        "batch_id": batch_id,
        "expires_at": expires_at,
        "samples": normalized,
        "assignments": assignments,
        "token_maps": token_maps,
        "reviewer_principals": {
            slot: reviewer_principal(tokens[slot]) for slot in ("A", "B")
        },
        "reviewer_order_sample_ids": orders,
        "human_only": True,
        "target_labels_preassigned": False,
        "canonical_state_changed": False,
        "model_changed": False,
        "no_trading": True,
    }
    owner_manifest["manifest_sha256"] = sha256_text(stable_json(owner_manifest))
    return {"assignments": assignments, "owner_manifest": owner_manifest}


def validate_submission(assignment: dict[str, Any], submission: dict[str, Any]) -> dict[str, Any]:
    """Validate a reviewer or arbiter export without deriving a target label."""

    issues: list[str] = []
    extra = sorted(set(submission) - SUBMISSION_FIELDS)
    if extra:
        issues.append("unsupported submission fields: " + ", ".join(extra))
    for field in sorted(SUBMISSION_FIELDS - submission.keys()):
        issues.append(f"missing submission field: {field}")
    if issues:
        return {"valid": False, "issues": issues}

    expected_hash = _assignment_digest(assignment)
    if assignment.get("assignment_sha256") != expected_hash:
        issues.append("assignment file hash is invalid")
    for field in ("contract_version", "batch_id", "reviewer_slot", "review_role", "reviewer_token"):
        if submission.get(field) != assignment.get(field):
            issues.append(f"submission {field} does not match assignment")
    if submission.get("assignment_sha256") != assignment.get("assignment_sha256"):
        issues.append("submission is not bound to this assignment")
    if submission.get("schema_version") != 1:
        issues.append("schema_version must be 1")
    if submission.get("complete") is not True:
        issues.append("final submission must be complete")
    if submission.get("target_label_submitted") is not False:
        issues.append("reviewers must not submit a target label")
    if submission.get("canonical_state_changed") is not False:
        issues.append("offline review must not claim a canonical state change")
    if submission.get("no_trading") is not True:
        issues.append("no_trading must be true")
    if not _valid_iso_datetime(submission.get("exported_at")):
        issues.append("exported_at must be a timezone-aware ISO datetime")

    attestations = submission.get("attestations")
    if not isinstance(attestations, dict):
        issues.append("attestations must be an object")
    else:
        for field in sorted(REQUIRED_ATTESTATIONS):
            if attestations.get(field) is not True:
                issues.append(f"attestation {field} must be true")
        extra_attestations = sorted(set(attestations) - REQUIRED_ATTESTATIONS)
        if extra_attestations:
            issues.append("unsupported attestations: " + ", ".join(extra_attestations))

    expected_peer_hidden = assignment.get("review_role") == "REVIEWER"
    if submission.get("peer_answers_hidden") is not expected_peer_hidden:
        issues.append(f"peer_answers_hidden must be {str(expected_peer_hidden).lower()}")

    assignment_tokens = [row.get("sample_token") for row in assignment.get("events", [])]
    rows = submission.get("results")
    if not isinstance(rows, list):
        issues.append("results must be an array")
        rows = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        prefix = f"results[{index}]"
        if not isinstance(row, dict):
            issues.append(f"{prefix} must be an object")
            continue
        extra_fields = sorted(set(row) - RESULT_FIELDS)
        missing_fields = sorted(RESULT_FIELDS - row.keys())
        if extra_fields:
            issues.append(f"{prefix} unsupported fields: {', '.join(extra_fields)}")
        if missing_fields:
            issues.append(f"{prefix} missing fields: {', '.join(missing_fields)}")
            continue
        token = str(row.get("sample_token") or "")
        if token not in assignment_tokens:
            issues.append(f"{prefix} unknown sample_token")
        if token in seen:
            issues.append(f"{prefix} duplicate sample_token")
        seen.add(token)
        if row.get("materiality") not in MATERIALITY:
            issues.append(f"{prefix} invalid materiality")
        if row.get("polarity") not in POLARITIES:
            issues.append(f"{prefix} invalid polarity")
        if row.get("evidence_state") not in EVIDENCE_STATES:
            issues.append(f"{prefix} invalid evidence_state")
        if len(_clean_text(row.get("rationale"))) < 20:
            issues.append(f"{prefix} rationale must contain at least 20 characters")
        if not _valid_iso_datetime(row.get("reviewed_at")):
            issues.append(f"{prefix} reviewed_at must be a timezone-aware ISO datetime")
        duration = row.get("duration_seconds")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
            issues.append(f"{prefix} duration_seconds must be non-negative")
    if set(assignment_tokens) != seen:
        issues.append("submission must contain exactly one result for every assigned sample")
    return {
        "valid": not issues,
        "issues": issues,
        "batch_id": assignment.get("batch_id"),
        "reviewer_slot": assignment.get("reviewer_slot"),
        "review_role": assignment.get("review_role"),
        "result_count": len(rows),
        "target_label_derived": False,
        "canonical_state_changed": False,
    }


def _draft_row_is_blank(row: dict[str, Any]) -> bool:
    """Return whether an exported reviewer row still contains no decision.

    The offline exporter stamps every row with the export time, including
    untouched rows.  ``reviewed_at`` therefore is not a completion signal; the
    three axes and rationale are authoritative.
    """

    return not any(
        _clean_text(row.get(field))
        for field in ("materiality", "polarity", "evidence_state", "rationale")
    )


def validate_progress_submission(
    assignment: dict[str, Any], submission: dict[str, Any]
) -> dict[str, Any]:
    """Validate an in-progress export without treating it as final gold.

    Partial snapshots are useful for durable progress and early integration,
    but they remain ineligible for training or blind evaluation until the
    existing strict validator accepts a complete, fully attested export.
    """

    issues: list[str] = []
    extra = sorted(set(submission) - SUBMISSION_FIELDS)
    if extra:
        issues.append("unsupported submission fields: " + ", ".join(extra))
    for field in sorted(SUBMISSION_FIELDS - submission.keys()):
        issues.append(f"missing submission field: {field}")
    if issues:
        return {"valid": False, "issues": issues, "gold_eligible": False}

    expected_hash = _assignment_digest(assignment)
    if assignment.get("assignment_sha256") != expected_hash:
        issues.append("assignment file hash is invalid")
    for field in ("contract_version", "batch_id", "reviewer_slot", "review_role", "reviewer_token"):
        if submission.get(field) != assignment.get(field):
            issues.append(f"submission {field} does not match assignment")
    if submission.get("assignment_sha256") != assignment.get("assignment_sha256"):
        issues.append("submission is not bound to this assignment")
    if submission.get("schema_version") != 1:
        issues.append("schema_version must be 1")
    if submission.get("complete") not in {True, False}:
        issues.append("complete must be boolean")
    if submission.get("target_label_submitted") is not False:
        issues.append("reviewers must not submit a target label")
    if submission.get("canonical_state_changed") is not False:
        issues.append("offline review must not claim a canonical state change")
    if submission.get("no_trading") is not True:
        issues.append("no_trading must be true")
    if not _valid_iso_datetime(submission.get("exported_at")):
        issues.append("exported_at must be a timezone-aware ISO datetime")

    attestations = submission.get("attestations")
    attested = False
    if not isinstance(attestations, dict):
        issues.append("attestations must be an object")
    else:
        extra_attestations = sorted(set(attestations) - REQUIRED_ATTESTATIONS)
        missing_attestations = sorted(REQUIRED_ATTESTATIONS - set(attestations))
        if extra_attestations:
            issues.append("unsupported attestations: " + ", ".join(extra_attestations))
        if missing_attestations:
            issues.append("missing attestations: " + ", ".join(missing_attestations))
        for field in sorted(REQUIRED_ATTESTATIONS & set(attestations)):
            if attestations.get(field) not in {True, False}:
                issues.append(f"attestation {field} must be boolean")
        attested = all(attestations.get(field) is True for field in REQUIRED_ATTESTATIONS)

    expected_peer_hidden = assignment.get("review_role") == "REVIEWER"
    if submission.get("peer_answers_hidden") is not expected_peer_hidden:
        issues.append(f"peer_answers_hidden must be {str(expected_peer_hidden).lower()}")

    assignment_tokens = [row.get("sample_token") for row in assignment.get("events", [])]
    assignment_token_set = set(assignment_tokens)
    rows = submission.get("results")
    if not isinstance(rows, list):
        issues.append("results must be an array")
        rows = []
    seen: set[str] = set()
    completed_tokens: list[str] = []
    for index, row in enumerate(rows):
        prefix = f"results[{index}]"
        if not isinstance(row, dict):
            issues.append(f"{prefix} must be an object")
            continue
        extra_fields = sorted(set(row) - RESULT_FIELDS)
        missing_fields = sorted(RESULT_FIELDS - row.keys())
        if extra_fields:
            issues.append(f"{prefix} unsupported fields: {', '.join(extra_fields)}")
        if missing_fields:
            issues.append(f"{prefix} missing fields: {', '.join(missing_fields)}")
            continue
        token = str(row.get("sample_token") or "")
        if token not in assignment_token_set:
            issues.append(f"{prefix} unknown sample_token")
        if token in seen:
            issues.append(f"{prefix} duplicate sample_token")
        seen.add(token)

        duration = row.get("duration_seconds")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
            issues.append(f"{prefix} duration_seconds must be non-negative")

        if _draft_row_is_blank(row):
            continue
        populated_axes = [
            bool(_clean_text(row.get(field)))
            for field in ("materiality", "polarity", "evidence_state")
        ]
        if not all(populated_axes) or not _clean_text(row.get("rationale")):
            issues.append(f"{prefix} partially completed decision")
            continue
        if row.get("materiality") not in MATERIALITY:
            issues.append(f"{prefix} invalid materiality")
        if row.get("polarity") not in POLARITIES:
            issues.append(f"{prefix} invalid polarity")
        if row.get("evidence_state") not in EVIDENCE_STATES:
            issues.append(f"{prefix} invalid evidence_state")
        if len(_clean_text(row.get("rationale"))) < 20:
            issues.append(f"{prefix} rationale must contain at least 20 characters")
        if not _valid_iso_datetime(row.get("reviewed_at")):
            issues.append(f"{prefix} reviewed_at must be a timezone-aware ISO datetime")
        completed_tokens.append(token)

    if not seen.issubset(assignment_token_set):
        issues.append("submission contains tokens outside the assignment")
    if submission.get("complete") is True:
        if seen != assignment_token_set or set(completed_tokens) != assignment_token_set:
            issues.append("complete submission must contain every assigned decision")
        if not attested:
            issues.append("complete submission requires every human attestation")

    strict_report = validate_submission(assignment, submission) if submission.get("complete") is True else None
    gold_eligible = bool(strict_report and strict_report.get("valid"))
    return {
        "valid": not issues,
        "issues": issues,
        "batch_id": assignment.get("batch_id"),
        "reviewer_slot": assignment.get("reviewer_slot"),
        "review_role": assignment.get("review_role"),
        "assigned_count": len(assignment_tokens),
        "row_count": len(rows),
        "completed_count": len(set(completed_tokens)),
        "remaining_count": max(0, len(assignment_tokens) - len(set(completed_tokens))),
        "attested": attested,
        "complete": submission.get("complete") is True,
        "gold_eligible": gold_eligible,
        "target_label_derived": False,
        "canonical_state_changed": False,
        "model_changed": False,
        "no_trading": True,
    }


def summarize_partial_progress(
    owner_manifest: dict[str, Any],
    submissions_by_slot: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Align partial A/B snapshots through the private owner token map.

    This emits progress and provisional disagreements only.  It never derives
    a target label or a freeze-ready annotation.
    """

    if owner_manifest.get("contract_version") != OFFLINE_GOLD_CONTRACT_VERSION:
        raise ValueError("unsupported owner manifest contract")
    expected_manifest_hash = sha256_text(
        stable_json({key: value for key, value in owner_manifest.items() if key != "manifest_sha256"})
    )
    if owner_manifest.get("manifest_sha256") != expected_manifest_hash:
        raise ValueError("owner manifest hash is invalid")

    samples_by_id = {row["sample_id"]: row for row in owner_manifest["samples"]}
    selected: dict[str, dict[str, dict[str, Any]]] = {"A": {}, "B": {}}
    validation: dict[str, list[dict[str, Any]]] = {"A": [], "B": []}
    revisions: list[dict[str, Any]] = []

    for slot in ("A", "B"):
        assignment = owner_manifest["assignments"][slot]
        token_map = owner_manifest["token_maps"][slot]
        snapshots = list(submissions_by_slot.get(slot) or [])
        snapshots.sort(key=lambda row: str(row.get("exported_at") or ""))
        for snapshot_index, submission in enumerate(snapshots, 1):
            report = validate_progress_submission(assignment, submission)
            validation[slot].append(report)
            if not report["valid"]:
                raise ValueError(
                    f"invalid {slot} progress submission #{snapshot_index}: "
                    + ", ".join(report["issues"])
                )
            for row in submission["results"]:
                if _draft_row_is_blank(row):
                    continue
                sample_id = token_map[row["sample_token"]]
                prior = selected[slot].get(sample_id)
                if prior is not None:
                    prior_axes = tuple(prior[field] for field in ("materiality", "polarity", "evidence_state"))
                    current_axes = tuple(row[field] for field in ("materiality", "polarity", "evidence_state"))
                    if prior_axes != current_axes or _clean_text(prior["rationale"]) != _clean_text(row["rationale"]):
                        revisions.append(
                            {
                                "slot": slot,
                                "sample_id": sample_id,
                                "previous_axes": list(prior_axes),
                                "current_axes": list(current_axes),
                                "previous_reviewed_at": prior.get("reviewed_at"),
                                "current_reviewed_at": row.get("reviewed_at"),
                            }
                        )
                selected[slot][sample_id] = dict(row)

    agreements: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    one_sided: list[dict[str, Any]] = []
    for sample_id in sorted(samples_by_id):
        first = selected["A"].get(sample_id)
        second = selected["B"].get(sample_id)
        if first is None and second is None:
            continue
        if first is None or second is None:
            one_sided.append({"sample_id": sample_id, "completed_by": "A" if first else "B"})
            continue
        axis_conflicts = [
            field
            for field in ("materiality", "polarity", "evidence_state")
            if first[field] != second[field]
        ]
        if axis_conflicts:
            conflicts.append(
                {
                    "sample_id": sample_id,
                    "axis_conflicts": axis_conflicts,
                    "review_a": {field: first[field] for field in ("materiality", "polarity", "evidence_state", "rationale")},
                    "review_b": {field: second[field] for field in ("materiality", "polarity", "evidence_state", "rationale")},
                }
            )
        else:
            agreements.append(
                {
                    "sample_id": sample_id,
                    "axes": {field: first[field] for field in ("materiality", "polarity", "evidence_state")},
                    "status": "PROVISIONAL_EXACT_AGREEMENT",
                }
            )

    completed_a = len(selected["A"])
    completed_b = len(selected["B"])
    covered = len(set(selected["A"]) | set(selected["B"]))
    both = len(set(selected["A"]) & set(selected["B"]))
    total = len(samples_by_id)
    blockers = []
    for slot in ("A", "B"):
        reports = validation[slot]
        if not reports:
            blockers.append(f"missing_reviewer_{slot}_snapshot")
        elif not any(report.get("gold_eligible") for report in reports):
            blockers.append(f"reviewer_{slot}_not_complete_or_attested")
    if conflicts:
        blockers.append("third_human_arbitration_required_for_current_conflicts")
    if both < total:
        blockers.append("dual_review_incomplete")

    return {
        "schema_version": 1,
        "contract_version": "human-gold-progress-v1",
        "batch_id": owner_manifest["batch_id"],
        "owner_manifest_sha256": owner_manifest["manifest_sha256"],
        "sample_count": total,
        "progress": {
            "A_completed": completed_a,
            "B_completed": completed_b,
            "A_remaining": total - completed_a,
            "B_remaining": total - completed_b,
            "covered_by_at_least_one": covered,
            "covered_by_both": both,
            "dual_reviews_remaining": total - both,
            "provisional_exact_agreements": len(agreements),
            "provisional_conflicts": len(conflicts),
            "current_arbitrations_required": len(conflicts),
            "untouched": total - covered,
        },
        "completion_requirements": {
            "each_reviewer_must_complete": total,
            "reviewer_A_required": total,
            "reviewer_B_required": total,
            "combined_one_sided_coverage_is_not_gold": True,
            "every_sample_requires_two_independent_reviews": True,
            "every_axis_conflict_requires_third_human_arbitration": True,
            "current_dual_review_candidates": both,
            "current_exact_consensus_candidates": len(agreements),
        },
        "validation": validation,
        "provisional_agreements": agreements,
        "provisional_conflicts": conflicts,
        "one_sided_reviews": one_sided,
        "revision_audit": revisions,
        "finalization_blockers": blockers,
        "gold_eligible": not blockers,
        "provisional_only": True,
        "target_label_derived": False,
        "split": "UNASSIGNED",
        "freeze_required_before_training_or_blind_evaluation": True,
        "canonical_state_changed": False,
        "model_changed": False,
        "no_trading": True,
    }


def _review_row(
    *,
    sample_id: str,
    result: dict[str, Any],
    reviewer_token: str,
    review_role: str,
    batch_id: str,
) -> dict[str, Any]:
    principal = reviewer_principal(reviewer_token)
    axes = ":".join(str(result[field]) for field in ("materiality", "polarity", "evidence_state"))
    return {
        "review_id": "hgr-" + sha256_text(f"{batch_id}:{sample_id}:{principal}:{review_role}:{axes}")[:24],
        "review_role": review_role,
        "reviewer_id": principal,
        "materiality": result["materiality"],
        "polarity": result["polarity"],
        "evidence_state": result["evidence_state"],
        "rationale": _clean_text(result["rationale"]),
        "created_at": result["reviewed_at"],
    }


def _result_by_sample_id(
    owner_manifest: dict[str, Any], slot: str, submission: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    token_map = owner_manifest["token_maps"][slot]
    return {token_map[row["sample_token"]]: row for row in submission["results"]}


def _build_arbiter_assignment(
    owner_manifest: dict[str, Any],
    conflicts: list[dict[str, Any]],
    *,
    arbiter_token: str,
    secret: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    samples_by_id = {row["sample_id"]: row for row in owner_manifest["samples"]}
    conflict_by_id = {row["sample_id"]: row for row in conflicts}
    ordered_ids = sorted(conflict_by_id, key=lambda sample_id: _order_key(secret, "ARBITER", sample_id))
    token_map = {
        _sample_token(secret, "ARBITER", sample_id): sample_id for sample_id in ordered_ids
    }
    events = []
    for sample_id in ordered_ids:
        sample = samples_by_id[sample_id]
        sample_token = next(token for token, mapped in token_map.items() if mapped == sample_id)
        content = _masked_content(sample, secret, "ARBITER")
        conflict = conflict_by_id[sample_id]
        events.append(
            {
                "sample_token": sample_token,
                "content_sha256": sha256_text(stable_json(content)),
                "content": content,
                "conflict_options": [
                    {
                        "option": "甲",
                        "materiality": conflict["review_a"]["materiality"],
                        "polarity": conflict["review_a"]["polarity"],
                        "evidence_state": conflict["review_a"]["evidence_state"],
                        "rationale": conflict["review_a"]["rationale"],
                    },
                    {
                        "option": "乙",
                        "materiality": conflict["review_b"]["materiality"],
                        "polarity": conflict["review_b"]["polarity"],
                        "evidence_state": conflict["review_b"]["evidence_state"],
                        "rationale": conflict["review_b"]["rationale"],
                    },
                ],
                "peer_answers_hidden": False,
                "target_label_hidden": True,
                "no_model_prediction_shown": True,
                "no_market_outcome_shown": True,
                "old_labels_hidden": True,
            }
        )
    assignment = {
        "schema_version": 1,
        "contract_version": OFFLINE_GOLD_CONTRACT_VERSION,
        "content_contract_version": HUMAN_BLIND_CONTRACT_VERSION,
        "batch_id": owner_manifest["batch_id"],
        "reviewer_slot": "ARBITER",
        "review_role": "ARBITER",
        "reviewer_token": arbiter_token,
        "expires_at": owner_manifest["expires_at"],
        "sample_count": len(events),
        "events": events,
        "axes": {
            "materiality": sorted(MATERIALITY),
            "polarity": sorted(POLARITIES),
            "evidence_state": sorted(EVIDENCE_STATES),
        },
        "target_label_submitted": False,
        "peer_answers_hidden": False,
        "human_only": True,
        "ai_assistance_allowed": False,
        "model_output_included": False,
        "post_event_market_data_included": False,
        "old_labels_included": False,
        "canonical_state_changed": False,
        "no_trading": True,
    }
    assignment["assignment_sha256"] = _assignment_digest(assignment)
    return assignment, token_map


def merge_dual_submissions(
    owner_manifest: dict[str, Any],
    submission_a: dict[str, Any],
    submission_b: dict[str, Any],
    *,
    arbiter_token: str | None = None,
    arbitration_secret: str | None = None,
) -> dict[str, Any]:
    """Derive exact agreements and create a third-human package for conflicts."""

    if owner_manifest.get("contract_version") != OFFLINE_GOLD_CONTRACT_VERSION:
        raise ValueError("unsupported owner manifest contract")
    expected_manifest_hash = sha256_text(
        stable_json({key: value for key, value in owner_manifest.items() if key != "manifest_sha256"})
    )
    if owner_manifest.get("manifest_sha256") != expected_manifest_hash:
        raise ValueError("owner manifest hash is invalid")
    assignments = owner_manifest["assignments"]
    reports = {
        "A": validate_submission(assignments["A"], submission_a),
        "B": validate_submission(assignments["B"], submission_b),
    }
    invalid = [slot for slot, report in reports.items() if not report["valid"]]
    if invalid:
        details = "; ".join(f"{slot}: {', '.join(reports[slot]['issues'])}" for slot in invalid)
        raise ValueError("invalid reviewer submission: " + details)
    if reviewer_principal(submission_a["reviewer_token"]) == reviewer_principal(
        submission_b["reviewer_token"]
    ):
        raise ValueError("two distinct anonymous reviewer principals are required")

    a_by_id = _result_by_sample_id(owner_manifest, "A", submission_a)
    b_by_id = _result_by_sample_id(owner_manifest, "B", submission_b)
    samples_by_id = {row["sample_id"]: row for row in owner_manifest["samples"]}
    consensus: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    review_rows: dict[str, list[dict[str, Any]]] = {}
    for sample_id in sorted(samples_by_id):
        first = _review_row(
            sample_id=sample_id,
            result=a_by_id[sample_id],
            reviewer_token=submission_a["reviewer_token"],
            review_role="REVIEWER",
            batch_id=owner_manifest["batch_id"],
        )
        second = _review_row(
            sample_id=sample_id,
            result=b_by_id[sample_id],
            reviewer_token=submission_b["reviewer_token"],
            review_role="REVIEWER",
            batch_id=owner_manifest["batch_id"],
        )
        review_rows[sample_id] = [first, second]
        agree = all(
            first[field] == second[field]
            for field in ("materiality", "polarity", "evidence_state")
        )
        if agree:
            consensus.append(build_dual_review_annotation(samples_by_id[sample_id], [first, second]))
        else:
            conflicts.append(
                {
                    "sample_id": sample_id,
                    "axis_conflicts": [
                        field
                        for field in ("materiality", "polarity", "evidence_state")
                        if first[field] != second[field]
                    ],
                    "review_a": first,
                    "review_b": second,
                }
            )

    arbitration_assignment = None
    arbitration_token_map: dict[str, str] = {}
    if conflicts:
        token = arbiter_token or secrets.token_urlsafe(32)
        if reviewer_principal(token) in set(owner_manifest["reviewer_principals"].values()):
            raise ValueError("arbiter must use a third distinct anonymous token")
        arbitration_assignment, arbitration_token_map = _build_arbiter_assignment(
            owner_manifest,
            conflicts,
            arbiter_token=token,
            secret=arbitration_secret or secrets.token_urlsafe(32),
        )
    return {
        "schema_version": 1,
        "contract_version": OFFLINE_GOLD_CONTRACT_VERSION,
        "batch_id": owner_manifest["batch_id"],
        "owner_manifest_sha256": owner_manifest["manifest_sha256"],
        "samples": owner_manifest["samples"],
        "review_rows": review_rows,
        "consensus_annotations": consensus,
        "conflicts": conflicts,
        "consensus_count": len(consensus),
        "conflict_count": len(conflicts),
        "axis_conflict_counts": dict(
            sorted(Counter(axis for row in conflicts for axis in row["axis_conflicts"]).items())
        ),
        "arbitration_assignment": arbitration_assignment,
        "arbitration_token_map": arbitration_token_map,
        "all_conflicts_resolved": not conflicts,
        "target_labels_were_submitted": False,
        "split": "UNASSIGNED",
        "freeze_required_before_blind_use": True,
        "canonical_state_changed": False,
        "model_changed": False,
        "no_trading": True,
    }


def finalize_with_arbitration(
    merge_manifest: dict[str, Any],
    arbiter_submission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine consensus and third-human resolutions into freeze-ready annotations."""

    annotations = list(merge_manifest.get("consensus_annotations") or [])
    conflicts = list(merge_manifest.get("conflicts") or [])
    if conflicts:
        assignment = merge_manifest.get("arbitration_assignment")
        if not isinstance(assignment, dict) or arbiter_submission is None:
            raise ValueError("conflicts require a completed third-human arbiter submission")
        report = validate_submission(assignment, arbiter_submission)
        if not report["valid"]:
            raise ValueError("invalid arbiter submission: " + ", ".join(report["issues"]))
        arbiter_principal = reviewer_principal(arbiter_submission["reviewer_token"])
        reviewer_principals = {
            row["reviewer_id"]
            for reviews in merge_manifest["review_rows"].values()
            for row in reviews
        }
        if arbiter_principal in reviewer_principals:
            raise ValueError("arbiter must be a third independent reviewer")
        result_by_token = {
            row["sample_token"]: row for row in arbiter_submission["results"]
        }
        samples_by_id = {row["sample_id"]: row for row in merge_manifest["samples"]}
        for token, sample_id in merge_manifest["arbitration_token_map"].items():
            arbiter = _review_row(
                sample_id=sample_id,
                result=result_by_token[token],
                reviewer_token=arbiter_submission["reviewer_token"],
                review_role="ARBITER",
                batch_id=merge_manifest["batch_id"],
            )
            annotations.append(
                build_dual_review_annotation(
                    samples_by_id[sample_id],
                    [*merge_manifest["review_rows"][sample_id], arbiter],
                )
            )
    annotations.sort(key=lambda row: str(row["sample_id"]))
    label_counts = dict(sorted(Counter(row["label"] for row in annotations).items()))
    return {
        "schema_version": 1,
        "contract_version": OFFLINE_GOLD_CONTRACT_VERSION,
        "batch_id": merge_manifest["batch_id"],
        "annotations": annotations,
        "annotation_count": len(annotations),
        "label_counts": label_counts,
        "resolution_counts": dict(
            sorted(Counter(row["resolution"] for row in annotations).items())
        ),
        "all_conflicts_resolved": True,
        "target_labels_were_submitted": False,
        "target_labels_derived_from_axes": True,
        "split": "UNASSIGNED",
        "freeze_required_before_training_or_blind_evaluation": True,
        "canonical_state_changed": False,
        "model_changed": False,
        "no_trading": True,
    }
