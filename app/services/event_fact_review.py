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
import sqlite3
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CONTRACT_VERSION = "event-fact-review-v1"
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


_EVENT_EVIDENCE_SQL = """SELECT ee.evidence_id,ee.evidence_url,ee.filing_date,ee.form,ee.items,
                                 ee.evidence_passage,ee.passage_score,ee.evidence_status,
                                 COALESCE(sr.content_sha256,ro.content_sha256) AS content_sha256,
                                 ro.source_id,s.name AS source_name,
                                 s.authority_tier,s.source_type
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
                          ORDER BY CASE WHEN s.authority_tier='P0' THEN 0
                                        WHEN s.authority_tier='P1' THEN 1 ELSE 2 END,
                                   ee.passage_score DESC,ee.updated_at DESC,ee.evidence_id"""


def _event_evidence(connection: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(_EVENT_EVIDENCE_SQL, (event_id,)).fetchall()
    evidence: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["passage_score"] = int(item.get("passage_score") or 0)
        item["evidence_url"] = _text(item.get("evidence_url"))
        item["evidence_passage"] = _text(item.get("evidence_passage"))
        item["authority_tier"] = _text(item.get("authority_tier")) or "UNKNOWN"
        item["source_name"] = _text(item.get("source_name")) or _text(item.get("source_id"))
        evidence.append(item)
    return evidence


def event_receipt(connection: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    row = connection.execute(
        """SELECT event_id,current_version,status,label_status,event_family,event_type,
                  event_date,stable_id,ticker_at_event,company_name,manual_grade,
                  discovery_source,last_updated_at,no_trading
           FROM canonical_events WHERE event_id=?""",
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
                    "source_id",
                    "authority_tier",
                )
            }
            for item in evidence
        ],
    }
    return {
        **event,
        "claim": claim,
        "evidence": evidence,
        "evidence_fingerprint": sha256_json(fingerprint_payload),
    }


def _review_priority(item: dict[str, Any]) -> tuple[Any, ...]:
    evidence = item.get("evidence") or []
    p0 = sum(1 for row in evidence if _text(row.get("authority_tier")) == "P0")
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
                _text(row.get("authority_tier")) in {"P0", "P1"}
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
    if submission.get("schema_version") != SCHEMA_VERSION:
        issues.append("schema_version must be 1")
    if _text(submission.get("contract_version")) != CONTRACT_VERSION:
        issues.append(f"contract_version must be {CONTRACT_VERSION}")
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
        elif decision == "REJECT_CANDIDATE":
            if reason_code not in REJECTION_REASONS:
                issues.append(f"{prefix} rejection reason is invalid")
        elif decision == "NEEDS_EVIDENCE":
            if reason_code not in EVIDENCE_GAPS:
                issues.append(f"{prefix} evidence-gap reason is invalid")
        elif decision == "ESCALATE":
            if reason_code not in ESCALATION_REASONS:
                issues.append(f"{prefix} escalation reason is invalid")

        normalized_results.append(dict(raw))
    missing = sorted(set(event_by_id) - seen)
    if missing:
        issues.append(f"submission is missing {len(missing)} assigned events")
    repeated_long_reasons = {
        reason for reason in rationales if len(reason) >= 20 and rationales.count(reason) > 2
    }
    if repeated_long_reasons:
        issues.append("the same rationale was copied to more than two events")
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "valid": not issues,
        "issues": issues,
        "batch_id": assignment.get("batch_id"),
        "reviewer_slot": assignment.get("reviewer_slot"),
        "reviewer_id": reviewer_id,
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
        if reason:
            conflicts.append(
                {
                    "event_id": event_id,
                    "event_version": events_a[event_id]["current_version"],
                    "evidence_fingerprint": events_a[event_id]["evidence_fingerprint"],
                    "reason": reason,
                    "reviewer_a_decision": left["decision"],
                    "reviewer_b_decision": right["decision"],
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
            }
        )
    output: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
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


def apply_consensus(
    ledger_path: Path,
    consensus: dict[str, Any],
    authorization: dict[str, Any],
) -> dict[str, Any]:
    """Atomically apply an exact, independently reviewed and authorized batch.

    Callers must create and verify a ledger backup before invoking this function.
    Any event-version or evidence drift aborts the entire transaction.
    """

    auth = _validated_authorization(consensus, authorization)
    rows = list(consensus.get("consensus") or [])
    if not rows:
        raise ValueError("consensus contains no directly applicable rows")
    connection = sqlite3.connect(ledger_path)
    connection.row_factory = sqlite3.Row
    changed: list[dict[str, Any]] = []
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for reviewed in rows:
            event_id = _text(reviewed.get("event_id"))
            current = event_receipt(connection, event_id)
            before_version = int(reviewed["event_version"])
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
                if _text(selected.get("authority_tier")) not in {"P0", "P1"}:
                    raise ValueError(f"verified consensus lacks P0/P1 evidence for {event_id}")
            new_version = before_version + 1
            now = utc_now()
            facts = _facts_for_current_version(connection, event_id, before_version)
            facts["dual_human_fact_review"] = {
                "contract_version": CONTRACT_VERSION,
                "batch_id": consensus["batch_id"],
                "consensus_sha256": consensus["consensus_sha256"],
                "decision": reviewed["decision"],
                "target_status": target_status,
                "reviewers": consensus["reviewers"],
                "submission_sha256": consensus["submission_sha256"],
                "selected_evidence_id": selected_evidence_id or None,
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
                    "dual_human_fact_review_v1",
                ),
            )
            if selected_evidence_id and target_status == "verified":
                connection.execute(
                    """UPDATE event_evidence
                       SET evidence_status='accepted_dual_human_primary_evidence',
                           auto_verification_allowed=0,updated_at=?
                       WHERE event_id=? AND evidence_id=?""",
                    (now, event_id, selected_evidence_id),
                )
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
        "contract_version": CONTRACT_VERSION,
        "batch_id": consensus["batch_id"],
        "consensus_sha256": consensus["consensus_sha256"],
        "authorization_id": auth["authorization_id"],
        "applied_at": utc_now(),
        "applied": len(changed),
        "changes": changed,
        "no_trading": True,
    }
