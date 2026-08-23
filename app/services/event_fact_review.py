"""Offline human fact-review batches with deterministic validation and merge.

The reviewer-facing package is deliberately disconnected from production.  A
batch freezes event identity, version and evidence fingerprints; returned files
are useful only when those receipts still match the current ledger.

This module does not collect market data, expose model output or perform any
trading action.  Canonical mutation remains a separate, explicitly authorized
step implemented by :mod:`scripts.event_fact_review_kit`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.evidence_policy import (
    DUAL_HUMAN_EVIDENCE_STATUS,
    allowed_human_fact_predicates,
    build_dual_human_selected_evidence_receipt,
    canonicalize_human_fact_claim,
    dual_human_selected_evidence_receipt_matches,
    is_primary_authority_tier,
)


LEGACY_CONTRACT_VERSION = "event-fact-review-v1"
CONTRACT_VERSION = "event-fact-review-v2"
SUPPORTED_CONTRACT_VERSIONS = frozenset({LEGACY_CONTRACT_VERSION, CONTRACT_VERSION})
SCHEMA_VERSION = 1
DECISIONS = {
    "CONFIRM_EVENT",
    "REJECT_CANDIDATE",
    "NEEDS_EVIDENCE",
    "ESCALATE",
}
CHECK_VALUES = {"YES", "NO", "UNCLEAR"}
MODALITIES = {"REALIZED", "PROPOSED_OR_CONDITIONAL", "UNCLEAR"}
REJECTION_REASONS = {
    "WRONG_SUBJECT",
    "WRONG_EVENT",
    "WRONG_EVENT_STAGE",
    "DATE_MISMATCH",
    "NEGATED_OR_WITHDRAWN",
    "DATA_ARTIFACT",
    "OTHER",
}
EVIDENCE_GAPS = {
    "SOURCE_UNAVAILABLE",
    "NO_EXACT_PASSAGE",
    "ONLY_DISCOVERY_SOURCE",
    "SUBJECT_UNCLEAR",
    "DATE_OR_STAGE_UNCLEAR",
    "CONFLICTING_EVIDENCE",
    "OTHER",
}
ESCALATION_REASONS = {
    "CONFLICTING_EVIDENCE",
    "POSSIBLE_DUPLICATE",
    "COMPLEX_EVENT_CHAIN",
    "LEGAL_OR_EQUITY_OUTCOME_UNCLEAR",
    "CANNOT_UNDERSTAND",
    "OTHER",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _exact_text(value: Any) -> str:
    return str(value or "")


_HUMAN_FACT_INPUT_FIELDS = (
    "contract_version",
    "subject",
    "subject_basis",
    "predicate",
    "fact_predicate",
    "action_quote",
    "object_quote",
    "stage",
    "modality",
    "fact_sentence_quote",
    "fact_sentence_start",
    "fact_sentence_end",
    "evidence_passage_sha256",
    "event_date_or_effective_date",
)


def _human_fact_input(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {field: value.get(field) for field in _HUMAN_FACT_INPUT_FIELDS}


def _selected_evidence(event: dict[str, Any], evidence_id: Any) -> dict[str, Any] | None:
    selected_id = _text(evidence_id)
    return next(
        (
            row
            for row in event.get("evidence") or []
            if _text(row.get("evidence_id")) == selected_id
        ),
        None,
    )


def _read_only_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _event_claim(connection: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    row = connection.execute(
        """SELECT r.title,r.summary,r.source_id,r.source_published_at,
                  r.local_received_at,r.content_sha256,r.canonical_url
           FROM event_observations eo
           JOIN latest_source_content r ON r.observation_id=eo.observation_id
           WHERE eo.event_id=? AND eo.relation_type!='filtered_aggregated_noise'
           ORDER BY r.local_received_at DESC,r.observation_id DESC LIMIT 1""",
        (event_id,),
    ).fetchone()
    return dict(row) if row is not None else {}


_EVENT_EVIDENCE_SQL = """SELECT ee.evidence_id,ee.observation_id,ee.evidence_url,
                                 ee.filing_date,ee.form,ee.items,
                                 ee.evidence_passage,ee.passage_score,ee.evidence_status,
                                 COALESCE(sr.content_sha256,ro.content_sha256) AS content_sha256,
                                 ro.source_published_at,ro.local_received_at,
                                 ro.source_id,s.name AS source_name,
                                 s.authority_tier,s.source_type,
                                 CASE WHEN sr.revision_kind='delete'
                                      THEN 'deleted' ELSE ro.observation_status END
                                   AS observation_status,
                                 COALESCE(sr.revision_no,0) AS latest_revision_no,
                                 COALESCE(sr.revision_kind,'new') AS latest_revision_kind,
                                 CASE
                                   WHEN COALESCE(sr.revision_kind,'new') NOT IN ('edit','delete')
                                     THEN 1
                                   WHEN INSTR(
                                     COALESCE(sr.title,ro.title,'') || CHAR(10) ||
                                     COALESCE(sr.summary,ro.summary,'') || CHAR(10) ||
                                     COALESCE(sr.raw_json,ro.raw_json,''),
                                     TRIM(ee.evidence_passage)
                                   )>0 THEN 1 ELSE 0
                                 END AS passage_currently_proven
                          FROM event_evidence ee
                          LEFT JOIN raw_observations ro
                            ON ro.observation_id=ee.observation_id
                          LEFT JOIN source_revisions sr
                            ON sr.observation_id=ro.observation_id
                           AND sr.revision_no=(
                               SELECT MAX(sr2.revision_no)
                               FROM source_revisions sr2
                               WHERE sr2.observation_id=ro.observation_id
                           )
                          LEFT JOIN sources s ON s.source_id=ro.source_id
                          WHERE ee.event_id=?
                          ORDER BY CASE
                                     WHEN UPPER(s.authority_tier)='P0'
                                       OR UPPER(s.authority_tier) GLOB 'P0_*' THEN 0
                                     WHEN UPPER(s.authority_tier)='P1'
                                       OR UPPER(s.authority_tier) GLOB 'P1_*' THEN 1
                                     ELSE 2
                                   END,
                                   ee.passage_score DESC,ee.updated_at DESC,ee.evidence_id"""


def _event_evidence(connection: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(_EVENT_EVIDENCE_SQL, (event_id,)).fetchall()
    evidence: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["passage_score"] = int(item.get("passage_score") or 0)
        item["evidence_url"] = _text(item.get("evidence_url"))
        # Quotes and selected-evidence receipts bind the stored passage
        # byte-for-byte; even trimming here could make an applied review fail
        # the stricter public reader check against the raw ledger value.
        item["evidence_passage"] = _exact_text(item.get("evidence_passage"))
        item["evidence_passage_sha256"] = hashlib.sha256(
            item["evidence_passage"].encode("utf-8")
        ).hexdigest()
        item["authority_tier"] = _text(item.get("authority_tier")) or "UNKNOWN"
        item["source_name"] = _text(item.get("source_name")) or _text(item.get("source_id"))
        cik_match = re.search(
            r"/Archives/edgar/data/0*([0-9]+)/",
            _text(item.get("evidence_url")),
            re.I,
        )
        item["document_issuer_identity_type"] = "CIK" if cik_match else ""
        item["document_issuer_identity_value"] = cik_match.group(1) if cik_match else ""
        evidence.append(item)
    return evidence


def event_receipt(connection: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    row = connection.execute(
        """SELECT ce.event_id,ce.current_version,ce.status,ce.label_status,
                  ce.event_family,ce.event_type,ce.event_date,ce.stable_id,
                  ce.ticker_at_event,ce.company_name,ce.manual_grade,
                  ce.discovery_source,ce.last_updated_at,ce.no_trading,
                  CASE
                    WHEN json_valid(ev.facts_json)
                     AND LENGTH(TRIM(COALESCE(json_extract(ev.facts_json,'$.cik'),'')))>0
                    THEN 'CIK' ELSE ''
                  END AS canonical_issuer_identity_type,
                  CASE
                    WHEN json_valid(ev.facts_json)
                    THEN LTRIM(TRIM(COALESCE(json_extract(ev.facts_json,'$.cik'),'')),'0')
                    ELSE ''
                  END AS canonical_issuer_identity_value
           FROM canonical_events ce
           LEFT JOIN event_versions ev
             ON ev.event_id=ce.event_id AND ev.version=ce.current_version
           WHERE ce.event_id=?""",
        (event_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown event: {event_id}")
    event = dict(row)
    event["current_version"] = int(event["current_version"])
    event["no_trading"] = True
    claim = _event_claim(connection, event_id)
    evidence = _event_evidence(connection, event_id)
    fingerprint_payload = {
        "event": {
            key: event.get(key)
            for key in (
                "event_id",
                "current_version",
                "status",
                "label_status",
                "event_family",
                "event_type",
                "event_date",
                "stable_id",
                "ticker_at_event",
                "company_name",
                "discovery_source",
                "canonical_issuer_identity_type",
                "canonical_issuer_identity_value",
            )
        },
        "claim": {
            key: claim.get(key)
            for key in (
                "title",
                "summary",
                "source_id",
                "source_published_at",
                "local_received_at",
                "content_sha256",
                "canonical_url",
            )
        },
        "evidence": [
            {
                key: item.get(key)
                for key in (
                    "evidence_id",
                    "evidence_url",
                    "filing_date",
                    "form",
                    "items",
                    "evidence_passage",
                    "evidence_status",
                    "content_sha256",
                    "source_published_at",
                    "local_received_at",
                    "source_id",
                    "authority_tier",
                    "observation_status",
                    "latest_revision_no",
                    "latest_revision_kind",
                    "passage_currently_proven",
                    "document_issuer_identity_type",
                    "document_issuer_identity_value",
                )
            }
            for item in evidence
        ],
    }
    return {
        **event,
        "allowed_human_fact_predicates": list(
            allowed_human_fact_predicates(
                event.get("event_type"), event.get("event_family")
            )
        ),
        "claim": claim,
        "evidence": evidence,
        "evidence_fingerprint": sha256_json(fingerprint_payload),
    }


def _review_priority(item: dict[str, Any]) -> tuple[Any, ...]:
    evidence = item.get("evidence") or []
    p0 = sum(
        1
        for row in evidence
        if is_primary_authority_tier(row.get("authority_tier"))
        and _text(row.get("authority_tier")).upper().split("_", 1)[0] == "P0"
    )
    accepted = sum(
        1
        for row in evidence
        if _text(row.get("evidence_status"))
        in {"accepted_manual_primary_evidence", "confirmed_primary"}
    )
    exact = sum(1 for row in evidence if len(_text(row.get("evidence_passage"))) >= 40)
    return (
        accepted,
        p0,
        exact,
        _text(item.get("event_date")),
        _text(item.get("last_updated_at")),
        _text(item.get("event_id")),
    )


def select_reviewable_events(
    ledger_path: Path,
    *,
    limit: int,
    excluded_event_ids: Iterable[str] = (),
    families: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Return a balanced, evidence-ready snapshot for offline review.

    Price-only candidates are excluded by default because a market move cannot
    be used as evidence that a corporate fact occurred.
    """

    limit = max(1, min(int(limit), 500))
    excluded = {str(value) for value in excluded_event_ids}
    allowed_families = {str(value) for value in families if str(value).strip()}
    with closing(_read_only_connection(ledger_path)) as connection:
        family_clause = ""
        query_parameters: list[Any] = []
        if allowed_families:
            ordered_families = sorted(allowed_families)
            family_clause = (
                " AND e.event_family IN ("
                + ",".join("?" for _ in ordered_families)
                + ")"
            )
            query_parameters.extend(ordered_families)
            candidate_limit = max(200, limit * 20)
        else:
            candidate_limit = max(1000, limit * 30)
        query_parameters.append(candidate_limit)
        candidate_ids = [
            str(row["event_id"])
            for row in connection.execute(
                f"""SELECT e.event_id
                   FROM canonical_events e
                   WHERE e.status IN ('candidate','weak')
                     AND e.event_family!='price_crash'
                     {family_clause}
                     AND EXISTS (
                         SELECT 1 FROM event_evidence ee
                         WHERE ee.event_id=e.event_id
                           AND LENGTH(TRIM(COALESCE(ee.evidence_passage,'')))>=40
                           AND LENGTH(TRIM(COALESCE(ee.evidence_url,'')))>0
                     )
                   ORDER BY e.event_date DESC,e.last_updated_at DESC,e.event_id
                   LIMIT ?""",
                query_parameters,
            )
        ]
        items: list[dict[str, Any]] = []
        for event_id in candidate_ids:
            if event_id in excluded:
                continue
            receipt = event_receipt(connection, event_id)
            if allowed_families and _text(receipt.get("event_family")) not in allowed_families:
                continue
            if not any(
                is_primary_authority_tier(row.get("authority_tier"))
                and len(_text(row.get("evidence_passage"))) >= 40
                for row in receipt["evidence"]
            ):
                continue
            items.append(receipt)

    by_family: dict[str, deque[dict[str, Any]]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[_text(item.get("event_family")) or "other"].append(item)
    for family, rows in grouped.items():
        rows.sort(key=_review_priority, reverse=True)
        by_family[family] = deque(rows)
    family_order = sorted(by_family, key=lambda key: (-len(by_family[key]), key))
    selected: list[dict[str, Any]] = []
    while family_order and len(selected) < limit:
        next_round: list[str] = []
        for family in family_order:
            queue = by_family[family]
            if queue and len(selected) < limit:
                selected.append(queue.popleft())
            if queue:
                next_round.append(family)
        family_order = next_round
    if len(selected) < limit:
        raise ValueError(
            f"only {len(selected)} evidence-ready events are available; requested {limit}"
        )
    return selected


def build_assignment(
    events: list[dict[str, Any]],
    *,
    batch_id: str,
    reviewer_slot: str,
    expires_at: str,
) -> dict[str, Any]:
    if reviewer_slot not in {"A", "B"}:
        raise ValueError("reviewer_slot must be A or B")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "batch_id": batch_id,
        "reviewer_slot": reviewer_slot,
        "generated_at": utc_now(),
        "expires_at": expires_at,
        "review_mode": "offline_independent_fact_review",
        "event_count": len(events),
        "events": events,
        "no_model_output": True,
        "no_market_outcome": True,
        "no_trading": True,
    }
    payload["assignment_sha256"] = sha256_json(payload)
    return payload


def assignment_hash(assignment: dict[str, Any]) -> str:
    payload = dict(assignment)
    claimed = _text(payload.pop("assignment_sha256", ""))
    calculated = sha256_json(payload)
    if claimed != calculated:
        raise ValueError("assignment_sha256 does not match assignment content")
    return claimed


def _require_timestamp(value: Any, field: str) -> str:
    text = _text(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def validate_submission(
    assignment: dict[str, Any], submission: dict[str, Any]
) -> dict[str, Any]:
    expected_hash = assignment_hash(assignment)
    issues: list[str] = []
    assignment_contract = _text(assignment.get("contract_version"))
    submission_contract = _text(submission.get("contract_version"))
    if submission.get("schema_version") != SCHEMA_VERSION:
        issues.append("schema_version must be 1")
    if assignment_contract not in SUPPORTED_CONTRACT_VERSIONS:
        issues.append("assignment contract_version is unsupported")
    if submission_contract != assignment_contract:
        issues.append("contract_version must match assignment")
    for field in ("batch_id", "reviewer_slot"):
        if _text(submission.get(field)) != _text(assignment.get(field)):
            issues.append(f"{field} does not match assignment")
    if _text(submission.get("assignment_sha256")) != expected_hash:
        issues.append("assignment_sha256 does not match assignment")
    reviewer_id = _text(submission.get("reviewer_id"))
    if len(reviewer_id) < 2:
        issues.append("reviewer_id is required")
    if submission.get("attestation") is not True:
        issues.append("reviewer attestation must be accepted")
    if submission.get("complete") is not True:
        issues.append("submission must be exported as a complete final review")
    for field in ("no_model_output", "no_market_outcome", "no_trading"):
        if submission.get(field) is not True:
            issues.append(f"{field} must remain true")
    try:
        _require_timestamp(submission.get("exported_at"), "exported_at")
    except ValueError as exc:
        issues.append(str(exc))

    event_by_id = {str(row["event_id"]): row for row in assignment.get("events") or []}
    results = submission.get("results")
    if not isinstance(results, list):
        issues.append("results must be a list")
        results = []
    seen: set[str] = set()
    normalized_results: list[dict[str, Any]] = []
    rationales: list[str] = []
    allowed_result_fields = {
        "event_id",
        "event_version",
        "evidence_fingerprint",
        "checks",
        "modality",
        "decision",
        "reason_code",
        "selected_evidence_id",
        "severity",
        "rationale",
        "reviewed_at",
        "duration_seconds",
        "started_at",
        "human_fact_claim",
    }
    for index, raw in enumerate(results, start=1):
        prefix = f"result {index}"
        if not isinstance(raw, dict):
            issues.append(f"{prefix} must be an object")
            continue
        event_id = _text(raw.get("event_id"))
        event = event_by_id.get(event_id)
        if event is None:
            issues.append(f"{prefix} has an unknown event_id")
            continue
        if event_id in seen:
            issues.append(f"{prefix} duplicates event_id {event_id}")
            continue
        seen.add(event_id)
        unexpected_fields = sorted(set(raw) - allowed_result_fields)
        if unexpected_fields:
            issues.append(
                f"{prefix} contains unsupported fields: {', '.join(unexpected_fields)}"
            )
        if int(raw.get("event_version") or -1) != int(event["current_version"]):
            issues.append(f"{prefix} event_version does not match assignment")
        if _text(raw.get("evidence_fingerprint")) != _text(event["evidence_fingerprint"]):
            issues.append(f"{prefix} evidence_fingerprint does not match assignment")
        decision = _text(raw.get("decision"))
        if decision not in DECISIONS:
            issues.append(f"{prefix} has an unsupported decision")
        checks = raw.get("checks")
        required_checks = {
            "source_accessible",
            "subject_match",
            "event_claim_supported",
            "date_coherent",
            "primary_evidence",
            "conflict_found",
        }
        if not isinstance(checks, dict) or set(checks) != required_checks:
            issues.append(f"{prefix} must contain the exact six checks")
            checks = {}
        elif any(_text(value) not in CHECK_VALUES for value in checks.values()):
            issues.append(f"{prefix} checks must be YES, NO or UNCLEAR")
        modality = _text(raw.get("modality"))
        if modality not in MODALITIES:
            issues.append(f"{prefix} modality is invalid")
        if _text(raw.get("severity")):
            issues.append(f"{prefix} severity must remain blank")
        rationale = _text(raw.get("rationale"))
        rationales.append(rationale)
        if len(rationale) < 20:
            issues.append(f"{prefix} rationale must contain at least 20 characters")
        try:
            _require_timestamp(raw.get("reviewed_at"), f"{prefix}.reviewed_at")
        except ValueError as exc:
            issues.append(str(exc))
        evidence_ids = {
            _text(row.get("evidence_id")) for row in event.get("evidence") or []
        }
        selected_evidence_id = _text(raw.get("selected_evidence_id"))
        if selected_evidence_id and selected_evidence_id not in evidence_ids:
            issues.append(f"{prefix} selected_evidence_id is not part of the assignment")

        reason_code = _text(raw.get("reason_code"))
        if decision == "CONFIRM_EVENT":
            if reason_code != "PRIMARY_EVIDENCE_DIRECTLY_SUPPORTS":
                issues.append(f"{prefix} confirmation reason is invalid")
            if not selected_evidence_id:
                issues.append(f"{prefix} confirmation requires selected_evidence_id")
            required_yes = {
                "source_accessible",
                "subject_match",
                "event_claim_supported",
                "date_coherent",
                "primary_evidence",
            }
            if any(_text(checks.get(key)) != "YES" for key in required_yes):
                issues.append(f"{prefix} confirmation requires five affirmative checks")
            if _text(checks.get("conflict_found")) != "NO":
                issues.append(f"{prefix} confirmation requires no unresolved conflict")
            if modality != "REALIZED":
                issues.append(f"{prefix} confirmation requires REALIZED modality")
            if assignment_contract == LEGACY_CONTRACT_VERSION:
                issues.append(
                    f"{prefix} V1_CONFIRM_REQUIRES_FACT_CLAIM_ADDENDUM"
                )
            elif assignment_contract == CONTRACT_VERSION:
                selected = _selected_evidence(event, selected_evidence_id)
                if selected is None:
                    issues.append(f"{prefix} selected evidence is unavailable for fact claim")
                else:
                    try:
                        canonicalize_human_fact_claim(
                            raw.get("human_fact_claim"),
                            event=event,
                            evidence=selected,
                        )
                    except ValueError as exc:
                        issues.append(f"{prefix} {exc}")
        elif decision == "REJECT_CANDIDATE":
            if reason_code not in REJECTION_REASONS:
                issues.append(f"{prefix} rejection reason is invalid")
        elif decision == "NEEDS_EVIDENCE":
            if reason_code not in EVIDENCE_GAPS:
                issues.append(f"{prefix} evidence-gap reason is invalid")
        elif decision == "ESCALATE":
            if reason_code not in ESCALATION_REASONS:
                issues.append(f"{prefix} escalation reason is invalid")

        if decision != "CONFIRM_EVENT" and raw.get("human_fact_claim") not in (None, {}):
            issues.append(f"{prefix} human_fact_claim is only allowed for confirmation")

        normalized_results.append(dict(raw))
    missing = sorted(set(event_by_id) - seen)
    if missing:
        issues.append(f"submission is missing {len(missing)} assigned events")
    repeated_long_reasons = {
        reason for reason in rationales if len(reason) >= 20 and rationales.count(reason) > 2
    }
    if repeated_long_reasons:
        issues.append("the same rationale was copied to more than two events")
    legacy_addendum_count = sum(
        1
        for row in results
        if isinstance(row, dict)
        and _text(row.get("decision")) == "CONFIRM_EVENT"
        and assignment_contract == LEGACY_CONTRACT_VERSION
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": assignment_contract,
        "valid": not issues,
        "issues": issues,
        "batch_id": assignment.get("batch_id"),
        "reviewer_slot": assignment.get("reviewer_slot"),
        "reviewer_id": reviewer_id,
        "legacy_v1_confirm_requires_fact_claim_addendum": legacy_addendum_count,
        "required_action": (
            "REISSUE_EVENT_IN_EVENT_FACT_REVIEW_V2_FOR_INDEPENDENT_FACT_CLAIM_ADDENDUM"
            if legacy_addendum_count
            else None
        ),
        "expected": len(event_by_id),
        "received": len(normalized_results),
        "submission_sha256": sha256_json(submission),
    }


def merge_submissions(
    assignment_a: dict[str, Any],
    submission_a: dict[str, Any],
    assignment_b: dict[str, Any],
    submission_b: dict[str, Any],
) -> dict[str, Any]:
    validation_a = validate_submission(assignment_a, submission_a)
    validation_b = validate_submission(assignment_b, submission_b)
    if not validation_a["valid"]:
        raise ValueError("reviewer A submission is invalid: " + "; ".join(validation_a["issues"]))
    if not validation_b["valid"]:
        raise ValueError("reviewer B submission is invalid: " + "; ".join(validation_b["issues"]))
    if _text(assignment_a.get("batch_id")) != _text(assignment_b.get("batch_id")):
        raise ValueError("assignments do not belong to the same batch")
    review_contract = _text(assignment_a.get("contract_version"))
    if review_contract != _text(assignment_b.get("contract_version")):
        raise ValueError("dual-review assignments use different contract versions")
    if validation_a["reviewer_slot"] == validation_b["reviewer_slot"]:
        raise ValueError("two different reviewer slots are required")
    if validation_a["reviewer_id"].casefold() == validation_b["reviewer_id"].casefold():
        raise ValueError("two independent reviewer identities are required")

    events_a = {str(row["event_id"]): row for row in assignment_a["events"]}
    events_b = {str(row["event_id"]): row for row in assignment_b["events"]}
    if set(events_a) != set(events_b):
        raise ValueError("dual-review assignments must contain the same event IDs")
    for event_id in events_a:
        if events_a[event_id]["evidence_fingerprint"] != events_b[event_id]["evidence_fingerprint"]:
            raise ValueError(f"assignment evidence differs for {event_id}")
    results_a = {str(row["event_id"]): row for row in submission_a["results"]}
    results_b = {str(row["event_id"]): row for row in submission_b["results"]}
    consensus: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for event_id in sorted(events_a):
        left = results_a[event_id]
        right = results_b[event_id]
        same_decision = left["decision"] == right["decision"]
        reason = ""
        if not same_decision:
            reason = "DECISION_DISAGREEMENT"
        elif left["decision"] == "ESCALATE":
            reason = "BOTH_ESCALATED"
        elif left["decision"] == "CONFIRM_EVENT" and (
            left.get("selected_evidence_id") != right.get("selected_evidence_id")
        ):
            reason = "CONFIRMATION_DETAILS_DISAGREE"
        canonical_human_fact: dict[str, Any] | None = None
        left_claim_sha256 = ""
        right_claim_sha256 = ""
        if not reason and left["decision"] == "CONFIRM_EVENT":
            selected_id = _text(left.get("selected_evidence_id"))
            selected_left = _selected_evidence(events_a[event_id], selected_id)
            selected_right = _selected_evidence(events_b[event_id], selected_id)
            if selected_left is None or selected_right is None:
                reason = "CONFIRMATION_EVIDENCE_UNAVAILABLE"
            else:
                left_claim = canonicalize_human_fact_claim(
                    left.get("human_fact_claim"),
                    event=events_a[event_id],
                    evidence=selected_left,
                )
                right_claim = canonicalize_human_fact_claim(
                    right.get("human_fact_claim"),
                    event=events_b[event_id],
                    evidence=selected_right,
                )
                left_claim_sha256 = _text(left_claim.get("canonical_claim_sha256"))
                right_claim_sha256 = _text(right_claim.get("canonical_claim_sha256"))
                if left_claim_sha256 != right_claim_sha256:
                    reason = "HUMAN_FACT_CLAIM_DISAGREEMENT"
                elif (
                    left_claim.get("public_fact_summary_sha256")
                    != right_claim.get("public_fact_summary_sha256")
                ):
                    reason = "HUMAN_FACT_SUMMARY_DERIVATION_DISAGREEMENT"
                else:
                    canonical_human_fact = left_claim
        if reason:
            conflicts.append(
                {
                    "event_id": event_id,
                    "event_version": events_a[event_id]["current_version"],
                    "evidence_fingerprint": events_a[event_id]["evidence_fingerprint"],
                    "reason": reason,
                    "reviewer_a_decision": left["decision"],
                    "reviewer_b_decision": right["decision"],
                    "reviewer_a_claim_sha256": left_claim_sha256 or None,
                    "reviewer_b_claim_sha256": right_claim_sha256 or None,
                }
            )
            continue
        decision = str(left["decision"])
        target_status = {
            "CONFIRM_EVENT": "verified",
            "REJECT_CANDIDATE": "rejected",
            "NEEDS_EVIDENCE": "weak",
        }[decision]
        consensus.append(
            {
                "event_id": event_id,
                "event_version": events_a[event_id]["current_version"],
                "evidence_fingerprint": events_a[event_id]["evidence_fingerprint"],
                "decision": decision,
                "target_status": target_status,
                "selected_evidence_id": _text(left.get("selected_evidence_id")) or None,
                "reason_codes": sorted({_text(left.get("reason_code")), _text(right.get("reason_code"))} - {""}),
                "reviewer_rationales": {
                    "A": _text(left.get("rationale")),
                    "B": _text(right.get("rationale")),
                },
                "reviewed_at": {
                    "A": _text(left.get("reviewed_at")),
                    "B": _text(right.get("reviewed_at")),
                },
                "human_fact_claim": canonical_human_fact,
                "canonical_claim_sha256": (
                    canonical_human_fact.get("canonical_claim_sha256")
                    if canonical_human_fact
                    else None
                ),
                "public_fact_summary_sha256": (
                    canonical_human_fact.get("public_fact_summary_sha256")
                    if canonical_human_fact
                    else None
                ),
            }
        )
    output: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": review_contract,
        "batch_id": assignment_a["batch_id"],
        "generated_at": utc_now(),
        "reviewers": {
            "A": validation_a["reviewer_id"],
            "B": validation_b["reviewer_id"],
        },
        "submission_sha256": {
            "A": validation_a["submission_sha256"],
            "B": validation_b["submission_sha256"],
        },
        "consensus": consensus,
        "conflicts": conflicts,
        "consensus_count": len(consensus),
        "conflict_count": len(conflicts),
        "formal_application": False,
        "no_trading": True,
    }
    output["consensus_sha256"] = sha256_json(output)
    return output


def consensus_hash(consensus: dict[str, Any]) -> str:
    payload = dict(consensus)
    claimed = _text(payload.pop("consensus_sha256", ""))
    calculated = sha256_json(payload)
    if claimed != calculated:
        raise ValueError("consensus_sha256 does not match consensus content")
    return claimed


def build_authorization_template(consensus: dict[str, Any]) -> dict[str, Any]:
    digest = consensus_hash(consensus)
    scope = [
        {
            "event_id": row["event_id"],
            "event_version": int(row["event_version"]),
            "evidence_fingerprint": row["evidence_fingerprint"],
            "target_status": row["target_status"],
            "canonical_claim_sha256": row.get("canonical_claim_sha256"),
            "public_fact_summary_sha256": row.get("public_fact_summary_sha256"),
        }
        for row in consensus.get("consensus") or []
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "apply_dual_human_fact_review_consensus",
        "approved": False,
        "authorization_id": "FILL_ME",
        "actor": "FILL_ME",
        "purpose": "FILL_ME",
        "expires_at": "FILL_ME_WITH_TIMEZONE",
        "batch_id": consensus.get("batch_id"),
        "consensus_sha256": digest,
        "scope": scope,
        "scope_sha256": sha256_json(scope),
        "no_trading": True,
    }


def _validated_authorization(
    consensus: dict[str, Any], authorization: dict[str, Any]
) -> dict[str, Any]:
    digest = consensus_hash(consensus)
    if authorization.get("approved") is not True:
        raise ValueError("authorization approved must be true")
    if _text(authorization.get("action")) != "apply_dual_human_fact_review_consensus":
        raise ValueError("authorization action is invalid")
    for field in ("authorization_id", "actor", "purpose"):
        if not _text(authorization.get(field)) or _text(authorization.get(field)) == "FILL_ME":
            raise ValueError(f"authorization {field} is required")
    expiry = _require_timestamp(authorization.get("expires_at"), "authorization.expires_at")
    if datetime.fromisoformat(expiry) <= datetime.now(timezone.utc):
        raise ValueError("authorization is expired")
    if _text(authorization.get("batch_id")) != _text(consensus.get("batch_id")):
        raise ValueError("authorization batch_id does not match consensus")
    if _text(authorization.get("consensus_sha256")) != digest:
        raise ValueError("authorization consensus_sha256 does not match consensus")
    expected_scope = [
        {
            "event_id": row["event_id"],
            "event_version": int(row["event_version"]),
            "evidence_fingerprint": row["evidence_fingerprint"],
            "target_status": row["target_status"],
            "canonical_claim_sha256": row.get("canonical_claim_sha256"),
            "public_fact_summary_sha256": row.get("public_fact_summary_sha256"),
        }
        for row in consensus.get("consensus") or []
    ]
    if authorization.get("scope") != expected_scope:
        raise ValueError("authorization scope does not exactly match consensus")
    if _text(authorization.get("scope_sha256")) != sha256_json(expected_scope):
        raise ValueError("authorization scope_sha256 is invalid")
    if authorization.get("no_trading") is not True:
        raise ValueError("authorization must preserve no_trading")
    return authorization


def _facts_for_current_version(
    connection: sqlite3.Connection, event_id: str, version: int
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT facts_json FROM event_versions WHERE event_id=? AND version=?",
        (event_id, version),
    ).fetchone()
    if row is None:
        return {}
    try:
        payload = json.loads(row["facts_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {"legacy_facts_raw": row["facts_json"]}
    return payload if isinstance(payload, dict) else {"legacy_facts": payload}


def _workflow_outcome(reviewed: dict[str, Any]) -> tuple[str, list[str]]:
    target_status = _text(reviewed.get("target_status"))
    reviewed_reasons = sorted(
        {_text(value) for value in reviewed.get("reason_codes") or []} - {""}
    )
    if target_status == "verified":
        return "EVIDENCE_READY", ["DUAL_HUMAN_PRIMARY_EVIDENCE_CONFIRMED"]
    if target_status == "weak":
        return "NEEDS_EVIDENCE", reviewed_reasons or ["DUAL_HUMAN_NEEDS_EVIDENCE"]
    if target_status == "rejected":
        return "EXCLUDED", reviewed_reasons or ["DUAL_HUMAN_REJECTED_CANDIDATE"]
    raise ValueError(f"unsupported target status for {reviewed.get('event_id')}")


def _dual_human_assessed_by(consensus: dict[str, Any]) -> str:
    return "dual_human:" + "+".join(
        _text(consensus.get("reviewers", {}).get(slot)) for slot in ("A", "B")
    )


def _is_exact_applied_consensus(
    connection: sqlite3.Connection,
    *,
    current: dict[str, Any],
    reviewed: dict[str, Any],
    consensus: dict[str, Any],
) -> bool:
    """Return true only for a complete retry of this exact immutable receipt.

    A version increment alone is deliberately insufficient: evidence, relation,
    workflow and facts receipts must all prove that the same consensus finished.
    An incomplete or foreign version remains stale and is never repaired through
    the idempotency path.
    """

    before_version = int(reviewed["event_version"])
    new_version = before_version + 1
    target_status = _text(reviewed.get("target_status"))
    review_contract = _text(consensus.get("contract_version"))
    change_reason = (
        "dual_human_fact_review_v2"
        if review_contract == CONTRACT_VERSION
        else "dual_human_fact_review_v1"
    )
    if (
        int(current["current_version"]) != new_version
        or _text(current.get("status")) != target_status
        or _text(current.get("label_status")) != target_status
    ):
        return False
    event_id = _text(reviewed.get("event_id"))
    version_receipt = connection.execute(
        """SELECT status,label_status,change_reason FROM event_versions
           WHERE event_id=? AND version=?""",
        (event_id, new_version),
    ).fetchone()
    if version_receipt is None or (
        version_receipt["status"],
        version_receipt["label_status"],
        version_receipt["change_reason"],
    ) != (target_status, target_status, change_reason):
        return False
    facts = _facts_for_current_version(connection, event_id, new_version)
    review_receipt = facts.get("dual_human_fact_review")
    if not isinstance(review_receipt, dict):
        return False
    if any(
        _text(review_receipt.get(key)) != expected
        for key, expected in (
            ("contract_version", review_contract),
            ("batch_id", _text(consensus.get("batch_id"))),
            ("consensus_sha256", _text(consensus.get("consensus_sha256"))),
            ("target_status", target_status),
        )
    ):
        return False
    selected_evidence_id = _text(reviewed.get("selected_evidence_id"))
    if _text(review_receipt.get("selected_evidence_id")) != selected_evidence_id:
        return False
    current_selected = next(
        (
            item
            for item in current.get("evidence") or []
            if _text(item.get("evidence_id")) == selected_evidence_id
        ),
        None,
    )
    selected_receipt = review_receipt.get("selected_evidence_receipt")
    canonical_claim_sha256 = _text(reviewed.get("canonical_claim_sha256"))
    public_fact_summary_sha256 = _text(reviewed.get("public_fact_summary_sha256"))
    if target_status == "verified" and (
        review_contract != CONTRACT_VERSION
        or current_selected is None
        or not dual_human_selected_evidence_receipt_matches(
            selected_receipt,
            current_selected,
            event_id=event_id,
            event_version=new_version,
            evidence_fingerprint_before=_text(reviewed.get("evidence_fingerprint")),
            canonical_claim_sha256=canonical_claim_sha256,
            public_fact_summary_sha256=public_fact_summary_sha256,
        )
    ):
        return False
    if target_status == "verified":
        stored_claim = facts.get("human_fact_claim")
        if not isinstance(stored_claim, dict):
            return False
        if (
            _text(stored_claim.get("canonical_claim_sha256")) != canonical_claim_sha256
            or _text(stored_claim.get("public_fact_summary_sha256"))
            != public_fact_summary_sha256
            or _text(facts.get("public_fact_summary"))
            != _text(stored_claim.get("public_fact_summary"))
        ):
            return False
    workflow_state, expected_reasons = _workflow_outcome(reviewed)
    workflow = connection.execute(
        """SELECT workflow_state,reason_codes_json,evidence_fingerprint,contract_version
           FROM event_fact_workflow WHERE event_id=? AND event_version=?""",
        (event_id, new_version),
    ).fetchone()
    if workflow is None:
        return False
    try:
        stored_reasons = json.loads(workflow["reason_codes_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    if (
        workflow["workflow_state"] != workflow_state
        or stored_reasons != expected_reasons
        or _text(workflow["evidence_fingerprint"])
        != _text(reviewed.get("evidence_fingerprint"))
        or workflow["contract_version"] != review_contract
    ):
        return False
    if target_status != "verified":
        relation_count = connection.execute(
            """SELECT COUNT(*) FROM event_evidence_relations
               WHERE event_id=? AND event_version=?""",
            (event_id, new_version),
        ).fetchone()[0]
        return int(relation_count or 0) == 0
    if not selected_evidence_id:
        return False
    relation_count = connection.execute(
        """SELECT COUNT(*) FROM event_evidence_relations
           WHERE event_id=? AND event_version=?""",
        (event_id, new_version),
    ).fetchone()[0]
    if int(relation_count or 0) != 1:
        return False
    closure = connection.execute(
        """SELECT rel.relation_status,rel.subject_match,rel.event_claim_supported,
                  rel.date_coherent,rel.modality,rel.evidence_fingerprint,
                  rel.contract_version,rel.assessed_by,ev.evidence_status
           FROM event_evidence_relations rel
           JOIN event_evidence ev
             ON ev.event_id=rel.event_id AND ev.evidence_id=rel.evidence_id
           WHERE rel.event_id=? AND rel.evidence_id=? AND rel.event_version=?""",
        (event_id, selected_evidence_id, new_version),
    ).fetchone()
    return closure is not None and (
        closure["relation_status"],
        int(closure["subject_match"]),
        int(closure["event_claim_supported"]),
        int(closure["date_coherent"]),
        closure["modality"],
        _text(closure["evidence_fingerprint"]),
        closure["contract_version"],
        closure["assessed_by"],
        closure["evidence_status"],
    ) == (
        "HUMAN_CONFIRMED",
        1,
        1,
        1,
        "REALIZED",
        _text(reviewed.get("evidence_fingerprint")),
        review_contract,
        _dual_human_assessed_by(consensus),
        DUAL_HUMAN_EVIDENCE_STATUS,
    )


def apply_consensus(
    ledger_path: Path,
    consensus: dict[str, Any],
    authorization: dict[str, Any],
) -> dict[str, Any]:
    """Atomically apply an exact, independently reviewed and authorized batch.

    Callers must create and verify a ledger backup before invoking this function.
    Any event-version or evidence drift aborts the entire transaction.
    """

    review_contract = _text(consensus.get("contract_version"))
    if review_contract not in SUPPORTED_CONTRACT_VERSIONS:
        raise ValueError("consensus contract_version is unsupported")
    auth = _validated_authorization(consensus, authorization)
    rows = list(consensus.get("consensus") or [])
    if not rows:
        raise ValueError("consensus contains no directly applicable rows")
    connection = sqlite3.connect(ledger_path)
    connection.row_factory = sqlite3.Row
    changed: list[dict[str, Any]] = []
    already_applied: list[dict[str, Any]] = []
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for reviewed in rows:
            event_id = _text(reviewed.get("event_id"))
            current = event_receipt(connection, event_id)
            before_version = int(reviewed["event_version"])
            if _is_exact_applied_consensus(
                connection,
                current=current,
                reviewed=reviewed,
                consensus=consensus,
            ):
                already_applied.append(
                    {
                        "event_id": event_id,
                        "event_version": before_version + 1,
                        "status": _text(reviewed.get("target_status")),
                    }
                )
                continue
            if int(current["current_version"]) != before_version:
                raise ValueError(f"STALE_REVIEW: event version changed for {event_id}")
            if _text(current["evidence_fingerprint"]) != _text(reviewed["evidence_fingerprint"]):
                raise ValueError(f"STALE_REVIEW: evidence changed for {event_id}")
            if _text(current["status"]) not in {"candidate", "weak"}:
                raise ValueError(f"event is no longer reviewable: {event_id}")
            target_status = _text(reviewed.get("target_status"))
            if target_status not in {"verified", "rejected", "weak"}:
                raise ValueError(f"unsupported target status for {event_id}")
            selected_evidence_id = _text(reviewed.get("selected_evidence_id"))
            if target_status == "verified":
                if review_contract != CONTRACT_VERSION:
                    raise ValueError(
                        f"V1_CONFIRM_REQUIRES_FACT_CLAIM_ADDENDUM: {event_id}"
                    )
                selected = next(
                    (
                        item
                        for item in current["evidence"]
                        if _text(item.get("evidence_id")) == selected_evidence_id
                    ),
                    None,
                )
                if selected is None:
                    raise ValueError(f"selected evidence disappeared for {event_id}")
                if not is_primary_authority_tier(selected.get("authority_tier")):
                    raise ValueError(f"verified consensus lacks P0/P1 evidence for {event_id}")
            new_version = before_version + 1
            now = utc_now()
            human_fact_claim: dict[str, Any] | None = None
            if target_status == "verified":
                human_fact_claim = canonicalize_human_fact_claim(
                    _human_fact_input(reviewed.get("human_fact_claim")),
                    event=current,
                    evidence=selected,
                )
                if (
                    _text(reviewed.get("canonical_claim_sha256"))
                    != _text(human_fact_claim.get("canonical_claim_sha256"))
                    or _text(reviewed.get("public_fact_summary_sha256"))
                    != _text(human_fact_claim.get("public_fact_summary_sha256"))
                ):
                    raise ValueError(f"HUMAN_FACT_CLAIM_RECEIPT_MISMATCH: {event_id}")
            selected_evidence_receipt: dict[str, Any] | None = None
            if target_status == "verified":
                selected_evidence_receipt = build_dual_human_selected_evidence_receipt(
                    selected,
                    event_id=event_id,
                    event_version=new_version,
                    evidence_fingerprint_before=_text(reviewed["evidence_fingerprint"]),
                    canonical_claim_sha256=human_fact_claim["canonical_claim_sha256"],
                    public_fact_summary_sha256=human_fact_claim[
                        "public_fact_summary_sha256"
                    ],
                )
            if connection.execute(
                "SELECT 1 FROM event_versions WHERE event_id=? AND version=?",
                (event_id, new_version),
            ).fetchone() is not None:
                raise ValueError(f"EVENT_VERSION_CONFLICT: version exists for {event_id}")
            if connection.execute(
                "SELECT 1 FROM event_fact_workflow WHERE event_id=? AND event_version=?",
                (event_id, new_version),
            ).fetchone() is not None:
                raise ValueError(f"WORKFLOW_CONFLICT: version exists for {event_id}")
            if connection.execute(
                """SELECT 1 FROM event_evidence_relations
                   WHERE event_id=? AND event_version=?""",
                (event_id, new_version),
            ).fetchone() is not None:
                raise ValueError(f"EVIDENCE_RELATION_CONFLICT: version exists for {event_id}")
            facts = _facts_for_current_version(connection, event_id, before_version)
            workflow_state, workflow_reasons = _workflow_outcome(reviewed)
            if human_fact_claim is not None:
                facts.update(
                    {
                        "human_fact_claim": human_fact_claim,
                        "claim_subject": human_fact_claim["subject"],
                        "claim_action": human_fact_claim["action_quote"],
                        "claim_stage": human_fact_claim["stage"],
                        "claim_modality": human_fact_claim["modality"],
                        "public_fact_summary": human_fact_claim["public_fact_summary"],
                        # Human publication cannot be backdated before the formal
                        # two-reviewer application completed.
                        "known_at": now,
                    }
                )
            facts["dual_human_fact_review"] = {
                "contract_version": review_contract,
                "batch_id": consensus["batch_id"],
                "consensus_sha256": consensus["consensus_sha256"],
                "decision": reviewed["decision"],
                "target_status": target_status,
                "reviewers": consensus["reviewers"],
                "submission_sha256": consensus["submission_sha256"],
                "selected_evidence_id": selected_evidence_id or None,
                "selected_evidence_receipt": selected_evidence_receipt,
                "canonical_claim_sha256": (
                    human_fact_claim.get("canonical_claim_sha256")
                    if human_fact_claim
                    else None
                ),
                "public_fact_summary_sha256": (
                    human_fact_claim.get("public_fact_summary_sha256")
                    if human_fact_claim
                    else None
                ),
                "evidence_relation_status": (
                    "HUMAN_CONFIRMED" if target_status == "verified" else None
                ),
                "evidence_status": (
                    DUAL_HUMAN_EVIDENCE_STATUS
                    if target_status == "verified"
                    else None
                ),
                "workflow_state": workflow_state,
                "reason_codes": reviewed.get("reason_codes") or [],
                "reviewer_rationales": reviewed.get("reviewer_rationales") or {},
                "reviewed_at": reviewed.get("reviewed_at") or {},
                "applied_at": now,
                "authorization": {
                    "authorization_id": auth["authorization_id"],
                    "actor": auth["actor"],
                    "purpose": auth["purpose"],
                    "expires_at": auth["expires_at"],
                },
                "no_model_output": True,
                "no_market_outcome": True,
                "no_trading": True,
            }
            manual_grade: Any
            if target_status == "rejected":
                manual_grade = "rejected"
            else:
                manual_grade = current.get("manual_grade")
            cursor = connection.execute(
                """UPDATE canonical_events
                   SET current_version=?,status=?,label_status=?,manual_grade=?,
                       last_updated_at=?,no_trading=1
                   WHERE event_id=? AND current_version=? AND status=?""",
                (
                    new_version,
                    target_status,
                    target_status,
                    manual_grade,
                    now,
                    event_id,
                    before_version,
                    current["status"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"event update lost atomic comparison for {event_id}")
            connection.execute(
                """INSERT INTO event_versions(
                   event_id,version,changed_at,status,label_status,event_family,event_type,
                   manual_grade,facts_json,change_reason
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    new_version,
                    now,
                    target_status,
                    target_status,
                    current["event_family"],
                    current["event_type"],
                    manual_grade,
                    stable_json(facts),
                    (
                        "dual_human_fact_review_v2"
                        if review_contract == CONTRACT_VERSION
                        else "dual_human_fact_review_v1"
                    ),
                ),
            )
            if selected_evidence_id and target_status == "verified":
                selected_status = _text(selected.get("evidence_status"))
                evidence_cursor = connection.execute(
                    """UPDATE event_evidence
                       SET evidence_status=?,
                           auto_verification_allowed=0,updated_at=?
                       WHERE event_id=? AND evidence_id=? AND evidence_status=?""",
                    (
                        DUAL_HUMAN_EVIDENCE_STATUS,
                        now,
                        event_id,
                        selected_evidence_id,
                        selected_status,
                    ),
                )
                if evidence_cursor.rowcount != 1:
                    raise ValueError(f"EVIDENCE_CAS_FAILED: evidence changed for {event_id}")
                assessed_by = _dual_human_assessed_by(consensus)
                try:
                    connection.execute(
                        """INSERT INTO event_evidence_relations(
                               event_id,evidence_id,event_version,relation_status,
                               subject_match,event_claim_supported,date_coherent,modality,
                               evidence_fingerprint,contract_version,assessed_by,created_at
                           ) VALUES (?,?,?,'HUMAN_CONFIRMED',1,1,1,'REALIZED',?,?,?,?)""",
                        (
                            event_id,
                            selected_evidence_id,
                            new_version,
                            reviewed["evidence_fingerprint"],
                            review_contract,
                            assessed_by,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(
                        f"EVIDENCE_RELATION_CONFLICT: version exists for {event_id}"
                    ) from exc
            try:
                connection.execute(
                    """INSERT INTO event_fact_workflow(
                           event_id,event_version,workflow_state,reason_codes_json,
                           evidence_fingerprint,contract_version,updated_at
                       ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        event_id,
                        new_version,
                        workflow_state,
                        stable_json(workflow_reasons),
                        reviewed["evidence_fingerprint"],
                        review_contract,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"WORKFLOW_CONFLICT: version exists for {event_id}") from exc
            open_jobs = connection.execute(
                """SELECT job_id,payload_json FROM pipeline_jobs
                   WHERE event_id=? AND status IN (
                       'PENDING_PRIMARY_EVIDENCE','PENDING_EVIDENCE_REVIEW','PENDING_HUMAN_REVIEW'
                   )""",
                (event_id,),
            ).fetchall()
            for job in open_jobs:
                try:
                    job_payload = json.loads(job["payload_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    job_payload = {"previous_payload_raw": job["payload_json"]}
                if not isinstance(job_payload, dict):
                    job_payload = {"previous_payload": job_payload}
                job_payload["dual_human_fact_review"] = {
                    "batch_id": consensus["batch_id"],
                    "consensus_sha256": consensus["consensus_sha256"],
                    "target_status": target_status,
                    "formal_application": True,
                    "no_trading": True,
                }
                if target_status == "weak":
                    # The review established that evidence is still missing;
                    # keep the collection route open instead of hiding the gap.
                    next_job_status = "PENDING_PRIMARY_EVIDENCE"
                    last_error = "DUAL_HUMAN_REVIEW_REQUIRES_MORE_EVIDENCE"
                else:
                    next_job_status = "COMPLETED_DUAL_HUMAN_FACT_REVIEW"
                    last_error = None
                connection.execute(
                    """UPDATE pipeline_jobs
                       SET status=?,last_error=?,payload_json=?,updated_at=?
                       WHERE job_id=?""",
                    (
                        next_job_status,
                        last_error,
                        stable_json(job_payload),
                        now,
                        job["job_id"],
                    ),
                )
            changed.append(
                {
                    "event_id": event_id,
                    "before_version": before_version,
                    "after_version": new_version,
                    "before_status": current["status"],
                    "after_status": target_status,
                }
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": review_contract,
        "batch_id": consensus["batch_id"],
        "consensus_sha256": consensus["consensus_sha256"],
        "authorization_id": auth["authorization_id"],
        "applied_at": utc_now(),
        "applied": len(changed),
        "already_applied": len(already_applied),
        "changes": changed,
        "unchanged": already_applied,
        "no_trading": True,
    }
