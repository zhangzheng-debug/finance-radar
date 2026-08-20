"""Build an immutable, read-only recovery plan for historical event quality debt.

The planner never applies a canonical mutation.  It freezes the exact event
version, evidence receipt and current fact payload that a later, separately
authorized recovery action would have to compare-and-swap against.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from app.services.event_admission import (
    EVENT_STAGES,
    READER_ALLOWED_EVIDENCE_STATUSES,
    READER_BLOCKED_EVIDENCE_STATUSES,
    SUPPORTED_RELATION_STATES,
)
from app.services.event_fact_review import event_receipt


CONTRACT_VERSION = "event-quality-recovery-plan-v1"
SCHEMA_VERSION = 1
BUCKETS = (
    "READER_READY_CURRENT",
    "LEGACY_FORMAL_REVIEW_REQUIRED",
    "STRICT_DUPLICATE_CANDIDATE",
    "GENERIC_SEC_DISCOVERY",
    "NON_DECISION_EVIDENCE_ONLY",
    "MISSING_PRIMARY_EVIDENCE",
    "ENRICHABLE_PRIMARY",
    "NEEDS_HUMAN",
)
PROPOSED_ACTIONS = {
    "READER_READY_CURRENT": "NO_ACTION",
    "LEGACY_FORMAL_REVIEW_REQUIRED": "PRESERVE_AND_REVIEW_BEFORE_ANY_STATUS_CHANGE",
    "STRICT_DUPLICATE_CANDIDATE": "REVIEW_DUPLICATE_LINKAGE",
    "GENERIC_SEC_DISCOVERY": "REPARSE_AS_DISCOVERY_LEAD",
    "NON_DECISION_EVIDENCE_ONLY": "KEEP_AS_DISCOVERY_LEAD",
    "MISSING_PRIMARY_EVIDENCE": "QUEUE_PRIMARY_SOURCE_ENRICHMENT",
    "ENRICHABLE_PRIMARY": "REBUILD_SCOPED_EVIDENCE_RELATION",
    "NEEDS_HUMAN": "QUEUE_HUMAN_FACT_REVIEW",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _read_only_connection(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _current_facts(connection: sqlite3.Connection, event_id: str, version: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT facts_json FROM event_versions WHERE event_id=? AND version=?",
        (event_id, version),
    ).fetchone()
    if row is None:
        return {}
    try:
        parsed = json.loads(row["facts_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _canonical_url(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.casefold()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), host, path, "", ""))


def _structured_claim(facts: dict[str, Any]) -> bool:
    summary = next(
        (
            _text(facts.get(key))
            for key in ("public_fact_summary", "fact_summary", "evidence_summary")
            if _text(facts.get(key))
        ),
        "",
    )
    return (
        len(summary) >= 20
        and len(_text(facts.get("claim_subject"))) >= 2
        and len(_text(facts.get("claim_action"))) >= 3
        and _text(facts.get("claim_stage")).upper() in EVENT_STAGES
        and len(_text(facts.get("known_at"))) >= 20
    )


def _current_supported_relations(
    connection: sqlite3.Connection, event_id: str, version: int
) -> set[str]:
    if not _table_exists(connection, "event_evidence_relations"):
        return set()
    rows = connection.execute(
        """SELECT evidence_id FROM event_evidence_relations
           WHERE event_id=? AND event_version=?
             AND relation_status IN ('SCOPED_MATCH','HUMAN_CONFIRMED')
             AND subject_match=1 AND event_claim_supported=1 AND date_coherent=1""",
        (event_id, version),
    ).fetchall()
    return {str(row["evidence_id"]) for row in rows}


def _primary_citable_evidence(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in receipt.get("evidence") or []
        if _text(row.get("authority_tier")).upper().startswith(("P0", "P1"))
        and bool(_canonical_url(row.get("evidence_url")))
        and len(_text(row.get("evidence_passage"))) >= 40
    ]


def _reader_ready(
    receipt: dict[str, Any], facts: dict[str, Any], relation_evidence_ids: set[str]
) -> bool:
    subject = _text(receipt.get("company_name") or receipt.get("ticker_at_event"))
    if not subject or not _structured_claim(facts):
        return False
    return any(
        _text(row.get("evidence_id")) in relation_evidence_ids
        and _text(row.get("evidence_status")) in READER_ALLOWED_EVIDENCE_STATUSES
        for row in _primary_citable_evidence(receipt)
    )


def _duplicate_keys(receipt: dict[str, Any]) -> set[str]:
    event_type = _text(receipt.get("event_type"))
    keys: set[str] = set()
    claim = receipt.get("claim") or {}
    claim_hash = _text(claim.get("content_sha256"))
    if len(claim_hash) == 64:
        keys.add(f"content:{event_type}:{claim_hash.lower()}")
    claim_url = _canonical_url(claim.get("canonical_url"))
    if claim_url:
        keys.add(f"url:{event_type}:{claim_url}")
    return keys


def _bucket(
    receipt: dict[str, Any],
    facts: dict[str, Any],
    relation_evidence_ids: set[str],
    duplicate_of_event_id: str | None,
) -> tuple[str, list[str]]:
    if _reader_ready(receipt, facts, relation_evidence_ids):
        return "READER_READY_CURRENT", []
    reasons: list[str] = []
    if _text(receipt.get("status")) in {"verified", "rejected"}:
        reasons.append("FORMAL_STATUS_PREDATES_CURRENT_SEMANTIC_GATE")
        if not _structured_claim(facts):
            reasons.append("MISSING_STRUCTURED_SUBJECT_ACTION_STAGE")
        if not relation_evidence_ids:
            reasons.append("MISSING_CURRENT_EVIDENCE_RELATION")
        return "LEGACY_FORMAL_REVIEW_REQUIRED", reasons
    if duplicate_of_event_id:
        return "STRICT_DUPLICATE_CANDIDATE", ["STRICT_URL_OR_CONTENT_DUPLICATE"]
    event_type = _text(receipt.get("event_type"))
    source = _text(receipt.get("discovery_source"))
    if source == "sec_current_filings" and event_type == "sec_material_filing":
        return "GENERIC_SEC_DISCOVERY", ["SEC_FORM_IS_NOT_AN_EVENT_PREDICATE"]
    evidence = receipt.get("evidence") or []
    evidence_statuses = {_text(row.get("evidence_status")) for row in evidence}
    if evidence and evidence_statuses and evidence_statuses <= READER_BLOCKED_EVIDENCE_STATUSES:
        return "NON_DECISION_EVIDENCE_ONLY", ["ONLY_BLOCKED_NON_DECISION_EVIDENCE"]
    primary = _primary_citable_evidence(receipt)
    if not primary:
        return "MISSING_PRIMARY_EVIDENCE", ["NO_CITABLE_P0_P1_PASSAGE"]
    if not _structured_claim(facts) or not relation_evidence_ids:
        if not _structured_claim(facts):
            reasons.append("MISSING_STRUCTURED_SUBJECT_ACTION_STAGE")
        if not relation_evidence_ids:
            reasons.append("MISSING_CURRENT_EVIDENCE_RELATION")
        return "ENRICHABLE_PRIMARY", reasons
    return "NEEDS_HUMAN", ["NONTERMINAL_EVENT_REQUIRES_HUMAN_FACT_REVIEW"]


def build_recovery_plan(ledger_path: Path) -> dict[str, Any]:
    """Return a deterministic plan from one consistent read transaction."""

    with closing(_read_only_connection(ledger_path)) as connection:
        connection.execute("BEGIN")
        source_schema_version = int(
            connection.execute("SELECT MAX(version) FROM event_ledger_schema").fetchone()[0]
            or 0
        )
        event_ids = [
            str(row["event_id"])
            for row in connection.execute(
                "SELECT event_id FROM canonical_events ORDER BY event_id"
            ).fetchall()
        ]
        receipts = [event_receipt(connection, event_id) for event_id in event_ids]
        duplicate_groups: dict[str, list[str]] = defaultdict(list)
        for receipt in receipts:
            for key in _duplicate_keys(receipt):
                duplicate_groups[key].append(str(receipt["event_id"]))
        duplicate_of: dict[str, str] = {}
        for members in duplicate_groups.values():
            ordered = sorted(set(members))
            if len(ordered) > 1:
                for event_id in ordered[1:]:
                    duplicate_of.setdefault(event_id, ordered[0])

        records: list[dict[str, Any]] = []
        for receipt in receipts:
            event_id = str(receipt["event_id"])
            version = int(receipt["current_version"])
            facts = _current_facts(connection, event_id, version)
            relation_ids = _current_supported_relations(connection, event_id, version)
            bucket, reasons = _bucket(
                receipt, facts, relation_ids, duplicate_of.get(event_id)
            )
            before = {
                "event_id": event_id,
                "event_version": version,
                "status": receipt.get("status"),
                "label_status": receipt.get("label_status"),
                "event_family": receipt.get("event_family"),
                "event_type": receipt.get("event_type"),
                "evidence_fingerprint": receipt["evidence_fingerprint"],
                "facts_sha256": sha256_json(facts),
            }
            record = {
                "event_id": event_id,
                "event_version": version,
                "bucket": bucket,
                "reason_codes": reasons,
                "proposed_action": PROPOSED_ACTIONS[bucket],
                "duplicate_of_event_id": duplicate_of.get(event_id),
                "before": before,
                "rollback_identity_sha256": sha256_json(before),
                "requires_separate_mutation_authorization": bucket != "READER_READY_CURRENT",
                "dry_run_only": True,
                "canonical_mutation_attempted": False,
            }
            record["record_sha256"] = sha256_json(record)
            records.append(record)
        connection.rollback()

    counts = Counter(record["bucket"] for record in records)
    logical_snapshot = {
        "source_schema_version": source_schema_version,
        "event_count": len(records),
        "event_receipts": [
            {
                "event_id": record["event_id"],
                "event_version": record["event_version"],
                "evidence_fingerprint": record["before"]["evidence_fingerprint"],
                "facts_sha256": record["before"]["facts_sha256"],
            }
            for record in records
        ],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
        "source_ledger_name": ledger_path.name,
        "source_schema_version": source_schema_version,
        "source_event_count": len(records),
        "logical_snapshot_sha256": sha256_json(logical_snapshot),
        "bucket_counts": {bucket: counts.get(bucket, 0) for bucket in BUCKETS},
        "partition_total": sum(counts.values()),
        "partition_complete": sum(counts.values()) == len(records),
        "read_only": True,
        "canonical_mutation_attempted": False,
        "requires_separate_mutation_authorization": True,
        "rollback_contract": {
            "compare_and_swap_fields": [
                "event_id",
                "event_version",
                "evidence_fingerprint",
                "facts_sha256",
            ],
            "before_snapshot_required": True,
            "append_only_audit_required": True,
            "automatic_rollback_authority": False,
        },
        "records": records,
    }


def jsonl_records(plan: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return plan.get("records") or []
