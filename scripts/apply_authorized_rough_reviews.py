#!/usr/bin/env python3
"""Close user-authorized rough-review jobs without claiming formal verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_ledger import open_ledger, stable_json, utc_now
from app.models.evidence_policy import is_conflicting_evidence_status


DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_OPERATIONS_DB = ROOT / "data" / "finance_radar_operations.sqlite3"
DEFAULT_REPORT = ROOT / "reports" / "authorized_rough_review_latest.json"
AUTHORIZATION_PHRASE = "user_explicit_bulk_rough_review"
COMPLETED_STATUS = "COMPLETED_AUTHORIZED_ROUGH_REVIEW"

OUTCOME_BY_DECISION = {
    "EVIDENCE_READY": "ROUGH_ACCEPTED",
    "INSUFFICIENT": "ROUGH_INSUFFICIENT",
    "CONFLICT": "ROUGH_CONFLICT",
}

ROUGH_REVIEW_CONTRACT_VERSION = "rough-review-v3"
ALLOWED_CLAIM_STATES = {
    "PRIMARY_SUPPORTED",
    "DISCOVERY_SUPPORTED",
    "INSUFFICIENT",
    "HUMAN_REVIEW",
}
ALLOWED_EDGE_RELATIONS = {"SUPPORTS", "CONTRADICTS"}
ALLOWED_AUTHORITY_TIERS = {"P0", "P1", "P2", "P3"}
REASON_BY_DECISION = {
    "EVIDENCE_READY": "当前版本存在可定位的一手证据；仅完成粗审，不改变正式结论。",
    "INSUFFICIENT": "当前版本的已引用证据不足以支撑正式结论；保留证据缺口。",
    "CONFLICT": "当前版本的已引用证据存在冲突；不得据此提升正式结论。",
}


def open_read_only(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without schema initialization or writes."""

    resolved = path.resolve(strict=True)
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _json_string_list(raw: Any) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp is missing a timezone")
    return parsed.astimezone(timezone.utc)


def _nonempty_text(value: Any, *, minimum: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= minimum


def _decision_semantic_reason(
    decision: dict[str, Any],
    receipt: dict[str, Any],
    *,
    decision_status: str,
    decision_evidence_ids: list[str],
) -> str | None:
    """Return a fail-closed reason when a decision is not rough-reviewable.

    A bulk rough review may never treat an opaque status flag as a meaningful
    assessment.  It needs a current, internally consistent claim/evidence
    graph, while still stopping short of formal verification.
    """

    try:
        output = json.loads(str(decision.get("output_json") or ""))
    except (TypeError, json.JSONDecodeError):
        return "INVALID_DECISION_OUTPUT"
    if not isinstance(output, dict):
        return "INVALID_DECISION_OUTPUT"
    if str(output.get("event_id") or "") != str(receipt["event_id"]):
        return "DECISION_OUTPUT_EVENT_MISMATCH"

    output_status = str(output.get("status") or "").upper()
    allowed_output_statuses = {decision_status}
    if decision_status == "CONFLICT":
        # Earlier evidence-agent records express a conflict as HUMAN_REVIEW.
        allowed_output_statuses.add("HUMAN_REVIEW")
    if output_status not in allowed_output_statuses:
        return "DECISION_OUTPUT_STATUS_MISMATCH"

    claims = output.get("claims")
    if not isinstance(claims, list) or not claims:
        return "MISSING_STRUCTURED_CLAIMS"
    claim_states: dict[str, str] = {}
    for claim in claims:
        if not isinstance(claim, dict):
            return "INVALID_STRUCTURED_CLAIM"
        claim_id = str(claim.get("claim_id") or "").strip()
        state = str(claim.get("verification_state") or "").upper()
        if not claim_id or claim_id in claim_states or not _nonempty_text(claim.get("text"), minimum=12):
            return "INVALID_STRUCTURED_CLAIM"
        if state not in ALLOWED_CLAIM_STATES:
            return "INVALID_CLAIM_VERIFICATION_STATE"
        claim_states[claim_id] = state

    edges = output.get("evidence_edges")
    if not isinstance(edges, list):
        return "INVALID_STRUCTURED_EVIDENCE_EDGES"
    current_evidence_ids = set(receipt["evidence_ids"])
    evidence_by_id = {str(item["evidence_id"]): item for item in receipt["evidence"]}
    receipt_has_conflict = any(
        is_conflicting_evidence_status(item.get("evidence_status"))
        for item in receipt["evidence"]
    )
    edge_ids: set[str] = set()
    edges_by_claim: dict[str, list[dict[str, str]]] = {claim_id: [] for claim_id in claim_states}
    for edge in edges:
        if not isinstance(edge, dict):
            return "INVALID_STRUCTURED_EVIDENCE_EDGE"
        claim_id = str(edge.get("claim_id") or "").strip()
        evidence_id = str(edge.get("evidence_id") or "").strip()
        relation = str(edge.get("relation") or "").upper()
        authority_tier = str(edge.get("authority_tier") or "").upper()
        source_url = str(edge.get("source_url") or "").strip()
        parsed_url = urlparse(source_url)
        if (
            claim_id not in claim_states
            or not evidence_id
            or evidence_id not in current_evidence_ids
            or relation not in ALLOWED_EDGE_RELATIONS
            or authority_tier not in ALLOWED_AUTHORITY_TIERS
            or not _nonempty_text(edge.get("exact_excerpt"), minimum=12)
            or parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
        ):
            return "INVALID_STRUCTURED_EVIDENCE_EDGE"
        if (
            is_conflicting_evidence_status(evidence_by_id[evidence_id].get("evidence_status"))
            and relation != "CONTRADICTS"
        ):
            return "CONFLICTING_EVIDENCE_MISCLASSIFIED"
        edge_ids.add(evidence_id)
        edges_by_claim[claim_id].append(
            {"relation": relation, "authority_tier": authority_tier}
        )

    if edge_ids != set(decision_evidence_ids):
        return "DECISION_EVIDENCE_GRAPH_MISMATCH"

    try:
        decision_time = _parse_time(str(decision["created_at"]))
        evidence_times = [_parse_time(str(item["updated_at"])) for item in receipt["evidence"]]
    except (KeyError, TypeError, ValueError):
        return "INVALID_DECISION_OR_EVIDENCE_TIMESTAMP"
    if evidence_times and decision_time < max(evidence_times):
        return "DECISION_PREDATES_CURRENT_EVIDENCE"

    all_edges = [edge for entries in edges_by_claim.values() for edge in entries]
    has_contradiction = any(edge["relation"] == "CONTRADICTS" for edge in all_edges)
    if decision_status == "EVIDENCE_READY":
        if receipt_has_conflict or has_contradiction:
            return "EVIDENCE_READY_CONFLICT_REQUIRES_HUMAN_REVIEW"
        for claim_id, state in claim_states.items():
            if state != "PRIMARY_SUPPORTED":
                return "EVIDENCE_READY_CLAIM_NOT_PRIMARY_SUPPORTED"
            if not any(
                edge["relation"] == "SUPPORTS" and edge["authority_tier"] == "P0"
                for edge in edges_by_claim[claim_id]
            ):
                return "EVIDENCE_READY_CLAIM_LACKS_PRIMARY_SUPPORT"
    elif decision_status == "INSUFFICIENT":
        if receipt_has_conflict or has_contradiction or "HUMAN_REVIEW" in claim_states.values():
            return "INSUFFICIENT_CONFLICT_REQUIRES_HUMAN_REVIEW"
        if not ({"INSUFFICIENT", "DISCOVERY_SUPPORTED"} & set(claim_states.values())):
            return "INSUFFICIENT_WITHOUT_EVIDENCE_GAP"
    elif decision_status == "CONFLICT" and not (receipt_has_conflict or has_contradiction):
        return "CONFLICT_WITHOUT_CONTRADICTION"
    return None


def load_latest_decisions(path: Path) -> dict[str, dict[str, Any]]:
    connection = open_read_only(path)
    try:
        latest: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            """SELECT decision_id,event_id,status,trace_id,created_at,evidence_ids_json,output_json
               FROM agent_decisions ORDER BY event_id,created_at DESC,decision_id DESC"""
        ):
            latest.setdefault(str(row["event_id"]), dict(row))
        return latest
    finally:
        connection.close()


def _evidence_receipt(
    connection: sqlite3.Connection,
    *,
    event_id: str,
) -> dict[str, Any]:
    event = connection.execute(
        """SELECT e.event_id,e.current_version,e.status,v.changed_at,v.facts_json
           FROM canonical_events e
           JOIN event_versions v
             ON v.event_id=e.event_id AND v.version=e.current_version
           WHERE e.event_id=?""",
        (event_id,),
    ).fetchone()
    if event is None:
        raise RuntimeError(f"event disappeared before rough review: {event_id}")
    evidence = [
        dict(row)
        for row in connection.execute(
            """SELECT evidence_id,observation_id,evidence_url,filing_date,form,items,
                      evidence_passage,matched_keywords,passage_score,evidence_status,updated_at
               FROM event_evidence WHERE event_id=? ORDER BY evidence_id""",
            (event_id,),
        )
    ]
    receipt = {
        "event_id": str(event["event_id"]),
        "event_version": int(event["current_version"]),
        "event_status": str(event["status"]),
        "event_version_changed_at": str(event["changed_at"]),
        "facts_json": str(event["facts_json"]),
        "evidence": evidence,
    }
    receipt["evidence_ids"] = [str(item["evidence_id"]) for item in evidence]
    receipt["evidence_fingerprint"] = hashlib.sha256(
        stable_json(receipt).encode("utf-8")
    ).hexdigest()
    return receipt


def _deferred(
    job: sqlite3.Row,
    *,
    reason: str,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "job_id": str(job["job_id"]),
        "event_id": str(job["event_id"]),
        "reason": reason,
        "decision_id": str(decision["decision_id"]) if decision else None,
    }


def build_rows(
    connection: sqlite3.Connection,
    decisions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    jobs = connection.execute(
        """SELECT j.job_id,j.event_id,j.status,j.payload_json,e.discovery_source,e.event_family
           FROM pipeline_jobs j
           JOIN canonical_events e ON e.event_id=j.event_id
           WHERE j.job_type='live_primary_evidence_review'
             AND j.status='PENDING_HUMAN_REVIEW'
           ORDER BY j.event_id"""
    ).fetchall()
    for job in jobs:
        decision = decisions.get(str(job["event_id"]))
        if decision is None:
            deferred.append(_deferred(job, reason="MISSING_AGENT_DECISION"))
            continue
        decision_status = str(decision["status"]).upper()
        outcome = OUTCOME_BY_DECISION.get(decision_status)
        if outcome is None:
            deferred.append(_deferred(job, reason="UNSUPPORTED_DECISION_STATUS", decision=decision))
            continue
        receipt = _evidence_receipt(connection, event_id=str(job["event_id"]))
        if receipt["event_status"] != "candidate":
            deferred.append(_deferred(job, reason="EVENT_NOT_CANDIDATE", decision=decision))
            continue
        decision_evidence_ids = _json_string_list(decision.get("evidence_ids_json"))
        current_evidence_ids = set(receipt["evidence_ids"])
        if not decision_evidence_ids and decision_status in {"EVIDENCE_READY", "CONFLICT"}:
            deferred.append(_deferred(job, reason="MISSING_DECISION_EVIDENCE", decision=decision))
            continue
        if not set(decision_evidence_ids).issubset(current_evidence_ids):
            deferred.append(_deferred(job, reason="DECISION_EVIDENCE_STALE", decision=decision))
            continue
        try:
            decision_time = _parse_time(str(decision["created_at"]))
            version_time = _parse_time(str(receipt["event_version_changed_at"]))
        except ValueError:
            deferred.append(_deferred(job, reason="INVALID_DECISION_TIMESTAMP", decision=decision))
            continue
        if decision_time < version_time:
            deferred.append(_deferred(job, reason="DECISION_PREDATES_CURRENT_VERSION", decision=decision))
            continue
        semantic_reason = _decision_semantic_reason(
            decision,
            receipt,
            decision_status=decision_status,
            decision_evidence_ids=decision_evidence_ids,
        )
        if semantic_reason is not None:
            deferred.append(_deferred(job, reason=semantic_reason, decision=decision))
            continue
        rows.append(
            {
                "job_id": str(job["job_id"]),
                "event_id": str(job["event_id"]),
                "before_status": str(job["status"]),
                "payload_json": str(job["payload_json"] or "{}"),
                "discovery_source": str(job["discovery_source"] or ""),
                "event_family": str(job["event_family"] or ""),
                "event_version": receipt["event_version"],
                "evidence_ids": receipt["evidence_ids"],
                "evidence_fingerprint": receipt["evidence_fingerprint"],
                "decision_id": str(decision["decision_id"]),
                "decision_trace_id": str(decision["trace_id"]),
                "decision_created_at": str(decision["created_at"]),
                "decision_evidence_ids": decision_evidence_ids,
                "decision_output_sha256": hashlib.sha256(
                    str(decision["output_json"]).encode("utf-8")
                ).hexdigest(),
                "decision_evidence_latest_at": max(
                    str(item["updated_at"]) for item in receipt["evidence"]
                ) if receipt["evidence"] else None,
                "decision_status": decision_status,
                "outcome": outcome,
                "reason": REASON_BY_DECISION[decision_status],
            }
        )
    return rows, deferred


def manifest_sha256(rows: list[dict[str, Any]]) -> str:
    manifest = [
        {
            "event_id": row["event_id"],
            "event_version": row["event_version"],
            "evidence_fingerprint": row["evidence_fingerprint"],
            "decision_id": row["decision_id"],
            "decision_output_sha256": row["decision_output_sha256"],
            "decision_evidence_latest_at": row["decision_evidence_latest_at"],
            "decision_status": row["decision_status"],
            "outcome": row["outcome"],
        }
        for row in rows
    ]
    return hashlib.sha256(stable_json(manifest).encode("utf-8")).hexdigest()


def apply_rows(
    connection: Any,
    rows: list[dict[str, Any]],
    *,
    batch_id: str,
    reviewed_at: str,
) -> int:
    connection.execute("BEGIN IMMEDIATE")
    updated = 0
    try:
        for row in rows:
            receipt = _evidence_receipt(connection, event_id=row["event_id"])
            if (
                receipt["event_version"] != row["event_version"]
                or receipt["evidence_fingerprint"] != row["evidence_fingerprint"]
                or receipt["event_status"] != "candidate"
            ):
                raise RuntimeError(
                    f"event version or evidence changed since rough-review snapshot: {row['event_id']}"
                )
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, TypeError):
                payload = {"previous_payload_raw": row["payload_json"]}
            if not isinstance(payload, dict):
                payload = {"previous_payload": payload}
            payload["rough_review"] = {
                "batch_id": batch_id,
                "contract_version": ROUGH_REVIEW_CONTRACT_VERSION,
                "reviewed_at": reviewed_at,
                "reviewer_mode": "codex_machine_rough_review",
                "authorization": AUTHORIZATION_PHRASE,
                "decision_id": row["decision_id"],
                "decision_trace_id": row["decision_trace_id"],
                "decision_created_at": row["decision_created_at"],
                "decision_evidence_ids": row["decision_evidence_ids"],
                "decision_output_sha256": row["decision_output_sha256"],
                "decision_evidence_latest_at": row["decision_evidence_latest_at"],
                "decision_status": row["decision_status"],
                "outcome": row["outcome"],
                "event_version": row["event_version"],
                "evidence_ids": row["evidence_ids"],
                "evidence_fingerprint": row["evidence_fingerprint"],
                "reason": row["reason"],
                "formal_verification": False,
                "canonical_event_label_changed": False,
            }
            cursor = connection.execute(
                """UPDATE pipeline_jobs
                   SET status=?,last_error=NULL,payload_json=?,updated_at=?
                    WHERE job_id=? AND job_type='live_primary_evidence_review'
                      AND status='PENDING_HUMAN_REVIEW' AND payload_json=?""",
                (
                    COMPLETED_STATUS,
                    stable_json(payload),
                    reviewed_at,
                    row["job_id"],
                    row["payload_json"],
                ),
            )
            updated += int(cursor.rowcount)
        if updated != len(rows):
            raise RuntimeError(f"rough-review update mismatch: expected {len(rows)}, updated {updated}")
        connection.commit()
        return updated
    except Exception:
        connection.rollback()
        raise


def run(
    db: Path,
    operations_db: Path,
    *,
    apply: bool,
    authorization: str | None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    connection = open_read_only(db)
    try:
        rows, deferred = build_rows(connection, load_latest_decisions(operations_db))
    finally:
        connection.close()
    reviewed_at = utc_now()
    resolved_batch_id = batch_id or f"rough-review-{uuid.uuid4().hex}"
    result: dict[str, Any] = {
        "batch_id": resolved_batch_id,
        "reviewed_at": reviewed_at,
        "mode": "APPLY" if apply else "DRY_RUN",
        "selected": len(rows),
        "deferred": len(deferred),
        "deferred_reasons": dict(Counter(row["reason"] for row in deferred)),
        "updated": 0,
        "outcomes": dict(Counter(row["outcome"] for row in rows)),
        "decision_statuses": dict(Counter(row["decision_status"] for row in rows)),
        "manifest_sha256": manifest_sha256(rows),
        "formal_verification": False,
        "canonical_event_labels_changed": 0,
        "no_trading": True,
    }
    if not apply:
        return result
    if authorization != AUTHORIZATION_PHRASE:
        raise ValueError(
            f"--authorization must equal {AUTHORIZATION_PHRASE!r} for --apply"
        )
    connection = open_ledger(db)
    try:
        result["updated"] = apply_rows(
            connection,
            rows,
            batch_id=resolved_batch_id,
            reviewed_at=reviewed_at,
        )
        return result
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--operations-db", type=Path, default=DEFAULT_OPERATIONS_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--batch-id")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--authorization")
    args = parser.parse_args()
    result = run(
        args.db,
        args.operations_db,
        apply=args.apply,
        authorization=args.authorization,
        batch_id=args.batch_id,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"REPORT={args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
