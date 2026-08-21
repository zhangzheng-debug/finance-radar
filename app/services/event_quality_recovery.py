"""Plan and narrowly recover machine-reconstructable historical evidence links.

Planning is always read-only.  Applying is a separate, explicitly authorized
operation which may only *insert* a missing current-version evidence relation
and workflow row.  It never changes an event version, formal status or label.
Legacy formal conclusions therefore remain human-review debt instead of being
silently grandfathered through the current reader gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from app.evidence_policy import is_primary_authority_tier
from app.services.event_admission import (
    ADMISSION_CONTRACT_VERSION,
    EVENT_STAGES,
    FACT_SLOT_CONTRACT_VERSION,
    LEGACY_ADMISSION_CONTRACT_VERSION,
    READER_ALLOWED_EVIDENCE_STATUSES,
    READER_BLOCKED_EVIDENCE_STATUSES,
    SUPPORTED_RELATION_STATES,
    evidence_relation_fingerprint,
    extract_evidence_fact_slots,
    fact_slot_receipt_sha256,
    public_fact_summary,
)
from app.services.event_fact_review import event_receipt


CONTRACT_VERSION = "event-quality-recovery-plan-v4"
APPLY_CONTRACT_VERSION = "event-quality-recovery-apply-v3"
APPLY_ACTION = "apply_machine_reconstructable_event_relations"
SCHEMA_VERSION = 4
RECOVERY_AUDIT_CONTRACT_VERSION = "event-quality-recovery-db-audit-v1"
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
    "ENRICHABLE_PRIMARY": "REPARSE_FACT_SLOTS_THEN_REBUILD_SCOPED_RELATION",
    "NEEDS_HUMAN": "QUEUE_HUMAN_FACT_REVIEW",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _target_ledger_identity(
    ledger_path: Path,
    *,
    source_database_logical_sha256: str,
) -> dict[str, Any]:
    """Bind a plan and authorization to one resolved on-disk target."""

    resolved = ledger_path.resolve(strict=True)
    stat = resolved.stat()
    return {
        "resolved_path": str(resolved),
        "resolved_path_key": os.path.normcase(str(resolved)),
        "filesystem_device": int(stat.st_dev),
        "filesystem_inode": int(stat.st_ino),
        "source_database_logical_sha256": _text(source_database_logical_sha256).lower(),
    }


def _database_logical_sha256(connection: sqlite3.Connection) -> str:
    """Hash the complete SQLite logical dump visible to this transaction.

    Unlike hashing only the main ``.sqlite3`` file, this includes committed
    rows currently visible through WAL and therefore detects an incomplete
    raw-file copy used as a purported rollback backup.
    """

    digest = hashlib.sha256()
    for statement in connection.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


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


def _require_apply_schema(connection: sqlite3.Connection) -> None:
    try:
        version = int(
            connection.execute("SELECT MAX(version) FROM event_ledger_schema").fetchone()[0]
            or 0
        )
    except sqlite3.DatabaseError as exc:
        raise ValueError("target is not an event ledger") from exc
    missing = [
        table
        for table in ("event_evidence_relations", "event_fact_workflow")
        if not _table_exists(connection, table)
    ]
    if version < 14 or missing:
        raise ValueError(
            "target must be migrated to Schema 14 before apply"
            + (f"; missing tables: {','.join(missing)}" if missing else "")
        )


def _aware_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


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


def _replay_fact_slot_receipt(
    *,
    facts: dict[str, Any],
    evidence_passage: str,
    subject: str,
    action: str,
    stage: str,
) -> tuple[Any, str, str, list[str]]:
    """Re-run the current deterministic extractor and compare every stored byte."""

    reasons: list[str] = []
    stored_contract = _text(facts.get("admission_contract_version"))
    if stored_contract == LEGACY_ADMISSION_CONTRACT_VERSION:
        reasons.append("LEGACY_ADMISSION_V1_READ_ONLY_NOT_RECOVERABLE")
    elif stored_contract != ADMISSION_CONTRACT_VERSION:
        reasons.append("NO_CURRENT_ADMISSION_CONTRACT_BINDING")
    if _text(facts.get("fact_slot_contract_version")) != FACT_SLOT_CONTRACT_VERSION:
        reasons.append("FACT_SLOT_CONTRACT_VERSION_MISSING_OR_STALE")

    stored_slots = facts.get("claim_fact_slots")
    extraction = extract_evidence_fact_slots(
        evidence_passage=evidence_passage,
        event_type=action,
        expected_subject=subject,
    )
    if not isinstance(stored_slots, dict):
        reasons.append("MISSING_STORED_FACT_SLOTS")
    elif stored_slots != extraction.as_dict():
        reasons.append("STORED_FACT_SLOTS_DO_NOT_REPLAY")
    if extraction.contract_version != FACT_SLOT_CONTRACT_VERSION:
        reasons.append("REPLAYED_FACT_SLOT_CONTRACT_MISMATCH")
    if extraction.event_type != _text(action).casefold():
        reasons.append("REPLAYED_FACT_SLOT_EVENT_TYPE_MISMATCH")
    if not extraction.supports_specific_fact:
        reasons.append("REPLAYED_FACT_HAS_NO_ISSUER_BOUND_ACTION")

    replayed_summary = public_fact_summary(
        subject=subject,
        action_label=action,
        stage_label=stage,
        extraction=extraction,
    )
    stored_summary = str(facts.get("public_fact_summary") or "")
    if stored_summary != replayed_summary:
        reasons.append("PUBLIC_FACT_SUMMARY_DOES_NOT_REPLAY")
    replayed_receipt_sha256 = fact_slot_receipt_sha256(
        extraction=extraction,
        public_fact_summary_text=replayed_summary,
    )
    if _text(facts.get("fact_slot_receipt_sha256")).lower() != replayed_receipt_sha256:
        reasons.append("FACT_SLOT_RECEIPT_HASH_MISMATCH")
    return extraction, replayed_summary, replayed_receipt_sha256, reasons


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


def _current_relation_snapshot(
    connection: sqlite3.Connection, event_id: str, version: int
) -> list[dict[str, Any]]:
    if not _table_exists(connection, "event_evidence_relations"):
        return []
    rows = connection.execute(
        """SELECT event_id,evidence_id,event_version,relation_status,subject_match,
                  event_claim_supported,date_coherent,modality,evidence_fingerprint,
                  contract_version,assessed_by,created_at
           FROM event_evidence_relations
           WHERE event_id=? AND event_version=?
           ORDER BY evidence_id""",
        (event_id, version),
    ).fetchall()
    return [dict(row) for row in rows]


def _current_workflow_snapshot(
    connection: sqlite3.Connection, event_id: str, version: int
) -> dict[str, Any] | None:
    if not _table_exists(connection, "event_fact_workflow"):
        return None
    row = connection.execute(
        """SELECT event_id,event_version,workflow_state,reason_codes_json,
                  evidence_fingerprint,contract_version,updated_at
           FROM event_fact_workflow WHERE event_id=? AND event_version=?""",
        (event_id, version),
    ).fetchone()
    return dict(row) if row is not None else None


def _evidence_context(
    connection: sqlite3.Connection, event_id: str, evidence_id: str
) -> dict[str, Any]:
    row = connection.execute(
        """SELECT ee.evidence_id,ee.observation_id,ee.filing_date,
                  ro.source_published_at,ro.local_received_at,ro.title,ro.summary,
                  ro.raw_json
           FROM event_evidence ee
           JOIN raw_observations ro ON ro.observation_id=ee.observation_id
           WHERE ee.event_id=? AND ee.evidence_id=?""",
        (event_id, evidence_id),
    ).fetchone()
    return dict(row) if row is not None else {}


def _primary_citable_evidence(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in receipt.get("evidence") or []
        if is_primary_authority_tier(row.get("authority_tier"))
        and bool(_canonical_url(row.get("evidence_url")))
        and len(_text(row.get("evidence_passage"))) >= 40
    ]


def _source_revision_binding_issues(
    selected: dict[str, Any], facts: dict[str, Any]
) -> list[str]:
    """Require the latest source revision to preserve the frozen admission receipt."""

    observation_status = _text(selected.get("observation_status")).casefold()
    revision_kind = _text(selected.get("latest_revision_kind")).casefold()
    if observation_status == "deleted" or revision_kind == "delete":
        return ["SOURCE_REVISION_DELETED"]

    stored_content_sha256 = _text(facts.get("source_content_sha256")).lower()
    current_content_sha256 = _text(selected.get("content_sha256")).lower()
    stored_slots = facts.get("claim_fact_slots")
    stored_passage_sha256 = (
        _text(stored_slots.get("passage_sha256")).lower()
        if isinstance(stored_slots, dict)
        else ""
    )
    current_passage_sha256 = hashlib.sha256(
        str(selected.get("evidence_passage") or "").encode("utf-8")
    ).hexdigest()
    if revision_kind == "edit" and (
        current_content_sha256 != stored_content_sha256
        or current_passage_sha256 != stored_passage_sha256
    ):
        return ["SOURCE_REVISION_CHANGED"]
    return []


def _reader_ready(
    receipt: dict[str, Any], facts: dict[str, Any], relation_evidence_ids: set[str]
) -> bool:
    subject = _text(receipt.get("company_name") or receipt.get("ticker_at_event"))
    if not subject or not _structured_claim(facts):
        return False
    for row in _primary_citable_evidence(receipt):
        if _text(row.get("evidence_id")) not in relation_evidence_ids:
            continue
        if _text(row.get("evidence_status")) not in READER_ALLOWED_EVIDENCE_STATUSES:
            continue
        if _source_revision_binding_issues(row, facts):
            continue
        _, _, _, replay_reasons = _replay_fact_slot_receipt(
            facts=facts,
            evidence_passage=str(row.get("evidence_passage") or ""),
            subject=_text(facts.get("claim_subject")),
            action=_text(facts.get("claim_action")),
            stage=_text(facts.get("claim_stage")).upper(),
        )
        if not replay_reasons:
            return True
    return False


def _machine_relation_candidate(
    connection: sqlite3.Connection,
    receipt: dict[str, Any],
    facts: dict[str, Any],
    relation_snapshot: list[dict[str, Any]],
    workflow_snapshot: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Re-prove a relation from frozen facts; never infer a new event claim.

    The safe subset is intentionally small.  The current facts must already
    carry the exact admission evidence ID, source content hash and relation
    fingerprint.  This function only reconstructs rows lost at a schema
    boundary; it does not manufacture missing subject/action facts.
    """

    reasons: list[str] = []
    status = _text(receipt.get("status"))
    if status not in {"candidate", "weak"}:
        reasons.append("FORMAL_OR_TERMINAL_STATUS_REQUIRES_HUMAN")
    if relation_snapshot:
        reasons.append("CURRENT_VERSION_RELATION_ALREADY_EXISTS")
    if workflow_snapshot is not None:
        reasons.append("CURRENT_VERSION_WORKFLOW_ALREADY_EXISTS")
    if not _structured_claim(facts):
        reasons.append("MISSING_STRUCTURED_SUBJECT_ACTION_STAGE")

    evidence_id = _text(facts.get("evidence_id"))
    source_observation_id = _text(facts.get("source_observation_id"))
    source_content_sha256 = _text(facts.get("source_content_sha256")).lower()
    stored_fingerprint = _text(facts.get("evidence_fingerprint")).lower()
    if not evidence_id:
        reasons.append("NO_FACT_BOUND_EVIDENCE_ID")
    if not source_observation_id:
        reasons.append("NO_FACT_BOUND_OBSERVATION_ID")
    if len(source_content_sha256) != 64:
        reasons.append("NO_FACT_BOUND_SOURCE_HASH")
    if len(stored_fingerprint) != 64:
        reasons.append("NO_FACT_BOUND_RELATION_FINGERPRINT")

    selected = next(
        (
            row
            for row in receipt.get("evidence") or []
            if _text(row.get("evidence_id")) == evidence_id
        ),
        None,
    )
    if selected is None:
        reasons.append("FACT_BOUND_EVIDENCE_MISSING")
        return None, sorted(set(reasons))
    if selected not in _primary_citable_evidence(receipt):
        reasons.append("FACT_BOUND_EVIDENCE_NOT_CITABLE_P0_P1")
    if _text(selected.get("evidence_status")) not in READER_ALLOWED_EVIDENCE_STATUSES:
        reasons.append("FACT_BOUND_EVIDENCE_STATUS_NOT_SUPPORTIVE")
    reasons.extend(_source_revision_binding_issues(selected, facts))
    selected_content_sha256 = _text(selected.get("content_sha256")).lower()
    if selected_content_sha256 != source_content_sha256:
        reasons.append("FACT_BOUND_SOURCE_HASH_MISMATCH")

    context = _evidence_context(connection, str(receipt["event_id"]), evidence_id)
    if _text(context.get("observation_id")) != source_observation_id:
        reasons.append("FACT_BOUND_OBSERVATION_MISMATCH")
    subject = _text(facts.get("claim_subject"))
    canonical_subject = _text(
        receipt.get("company_name") or receipt.get("ticker_at_event")
    )
    if subject.casefold() != canonical_subject.casefold():
        reasons.append("CLAIM_SUBJECT_DIFFERS_FROM_CANONICAL_SUBJECT")
    subject_context = " ".join(
        _text(value)
        for value in (
            selected.get("evidence_passage"),
            context.get("title"),
            context.get("summary"),
            context.get("raw_json"),
        )
    ).casefold()
    if not subject or subject.casefold() not in subject_context:
        reasons.append("SUBJECT_NOT_OBSERVED_IN_BOUND_EVIDENCE")

    action = _text(facts.get("claim_action"))
    if action.casefold() != _text(receipt.get("event_type")).casefold():
        reasons.append("CLAIM_ACTION_DIFFERS_FROM_CURRENT_EVENT_TYPE")
    stage = _text(facts.get("claim_stage")).upper()
    extraction, replayed_summary, replayed_receipt_sha256, replay_reasons = (
        _replay_fact_slot_receipt(
            facts=facts,
            evidence_passage=str(selected.get("evidence_passage") or ""),
            subject=subject,
            action=action,
            stage=stage,
        )
    )
    reasons.extend(replay_reasons)
    known_at = _text(facts.get("known_at"))
    known_timestamp = _aware_timestamp(known_at)
    published_timestamp = _aware_timestamp(context.get("source_published_at"))
    received_timestamp = _aware_timestamp(context.get("local_received_at"))
    if known_timestamp is None or published_timestamp is None or received_timestamp is None:
        reasons.append("BOUND_TIMESTAMPS_NOT_EXACT")
    elif known_timestamp != max(published_timestamp, received_timestamp):
        reasons.append("KNOWN_AT_NOT_BOUND_TO_DISCOVERY_TIMESTAMPS")

    event_date = _text(receipt.get("event_date"))[:10]
    coherent_dates = {
        _text(context.get("filing_date"))[:10],
        _text(context.get("source_published_at"))[:10],
    } - {""}
    if not event_date or event_date not in coherent_dates:
        reasons.append("EVENT_DATE_NOT_COHERENT_WITH_BOUND_EVIDENCE")

    calculated_fingerprint = evidence_relation_fingerprint(
        event_id=str(receipt["event_id"]),
        event_version=int(receipt["current_version"]),
        evidence_id=evidence_id,
        content_sha256=selected_content_sha256,
        subject=subject,
        action=action,
        stage=stage,
        known_at=known_at,
        contract_version=ADMISSION_CONTRACT_VERSION,
        evidence_passage_sha256=extraction.passage_sha256,
        fact_slot_receipt_sha256=replayed_receipt_sha256,
        public_fact_summary_sha256=hashlib.sha256(
            replayed_summary.encode("utf-8")
        ).hexdigest(),
    )
    if calculated_fingerprint != stored_fingerprint:
        reasons.append("RELATION_FINGERPRINT_RECOMPUTE_MISMATCH")
    if reasons:
        return None, sorted(set(reasons))
    candidate = {
        "event_id": str(receipt["event_id"]),
        "event_version": int(receipt["current_version"]),
        "evidence_id": evidence_id,
        "relation_status": "SCOPED_MATCH",
        "subject_match": 1,
        "event_claim_supported": 1,
        "date_coherent": 1,
        "modality": stage,
        "evidence_fingerprint": calculated_fingerprint,
        "workflow_state": "EVIDENCE_READY",
        "contract_version": ADMISSION_CONTRACT_VERSION,
    }
    candidate["candidate_sha256"] = sha256_json(candidate)
    return candidate, []


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
        source_database_logical_sha256 = _database_logical_sha256(connection)
        target_ledger_identity = _target_ledger_identity(
            ledger_path,
            source_database_logical_sha256=source_database_logical_sha256,
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
            relation_snapshot = _current_relation_snapshot(connection, event_id, version)
            relation_ids = {
                _text(row.get("evidence_id"))
                for row in relation_snapshot
                if _text(row.get("relation_status")) in SUPPORTED_RELATION_STATES
                and int(row.get("subject_match") or 0) == 1
                and int(row.get("event_claim_supported") or 0) == 1
                and int(row.get("date_coherent") or 0) == 1
            }
            workflow_snapshot = _current_workflow_snapshot(connection, event_id, version)
            bucket, reasons = _bucket(
                receipt, facts, relation_ids, duplicate_of.get(event_id)
            )
            machine_candidate, machine_reasons = _machine_relation_candidate(
                connection,
                receipt,
                facts,
                relation_snapshot,
                workflow_snapshot,
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
                "relations_sha256": sha256_json(relation_snapshot),
                "workflow_sha256": sha256_json(workflow_snapshot),
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
                "machine_relation_backfill": machine_candidate,
                "machine_relation_backfill_eligible": machine_candidate is not None,
                "machine_relation_backfill_reason_codes": machine_reasons,
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
        "source_database_logical_sha256": source_database_logical_sha256,
        "event_count": len(records),
        "event_receipts": [
            {
                "event_id": record["event_id"],
                "event_version": record["event_version"],
                "evidence_fingerprint": record["before"]["evidence_fingerprint"],
                "facts_sha256": record["before"]["facts_sha256"],
                "relations_sha256": record["before"]["relations_sha256"],
                "workflow_sha256": record["before"]["workflow_sha256"],
            }
            for record in records
        ],
    }
    plan = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "generated_at": utc_now(),
        "source_ledger_name": ledger_path.name,
        "target_ledger_resolved_path": target_ledger_identity["resolved_path"],
        "target_ledger_identity": target_ledger_identity,
        "target_ledger_identity_sha256": sha256_json(target_ledger_identity),
        "source_schema_version": source_schema_version,
        "source_database_logical_sha256": source_database_logical_sha256,
        "source_event_count": len(records),
        "logical_snapshot_sha256": sha256_json(logical_snapshot),
        "machine_relation_backfill_eligible": sum(
            bool(record["machine_relation_backfill_eligible"]) for record in records
        ),
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
                "relations_sha256",
                "workflow_sha256",
            ],
            "before_snapshot_required": True,
            "append_only_audit_required": True,
            "automatic_rollback_authority": False,
        },
        "records": records,
    }
    plan["plan_sha256"] = sha256_json(plan)
    return plan


def jsonl_records(plan: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return plan.get("records") or []


def validate_recovery_plan(plan: dict[str, Any]) -> str:
    """Validate the complete plan and every immutable record hash."""

    claimed = _text(plan.get("plan_sha256"))
    payload = dict(plan)
    payload.pop("plan_sha256", None)
    calculated = sha256_json(payload)
    if claimed != calculated:
        raise ValueError("plan_sha256 does not match recovery plan content")
    if _text(plan.get("contract_version")) != CONTRACT_VERSION:
        raise ValueError("unsupported recovery plan contract")
    if int(plan.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError("unsupported recovery plan schema")
    records = list(plan.get("records") or [])
    if int(plan.get("source_event_count") or -1) != len(records):
        raise ValueError("recovery plan event count does not match records")
    seen_event_ids: set[str] = set()
    calculated_counts: Counter[str] = Counter()
    machine_eligible = 0
    for record in records:
        event_id = _text(record.get("event_id"))
        if not event_id or event_id in seen_event_ids:
            raise ValueError("recovery plan contains a missing or duplicate event_id")
        seen_event_ids.add(event_id)
        bucket = _text(record.get("bucket"))
        if bucket not in BUCKETS:
            raise ValueError(f"unsupported recovery bucket for {event_id}")
        calculated_counts[bucket] += 1
        claimed_record = _text(record.get("record_sha256"))
        record_payload = dict(record)
        record_payload.pop("record_sha256", None)
        if claimed_record != sha256_json(record_payload):
            raise ValueError(f"record_sha256 does not match {record.get('event_id')}")
        candidate = record.get("machine_relation_backfill")
        eligible = isinstance(candidate, dict)
        if bool(record.get("machine_relation_backfill_eligible")) != eligible:
            raise ValueError(f"machine eligibility flag is inconsistent for {event_id}")
        if eligible:
            machine_eligible += 1
            candidate_payload = dict(candidate)
            claimed_candidate = _text(candidate_payload.pop("candidate_sha256", ""))
            if claimed_candidate != sha256_json(candidate_payload):
                raise ValueError(f"candidate_sha256 does not match {event_id}")
    expected_counts = {
        bucket: calculated_counts.get(bucket, 0) for bucket in BUCKETS
    }
    if plan.get("bucket_counts") != expected_counts:
        raise ValueError("recovery plan bucket counts do not match records")
    if int(plan.get("partition_total") or -1) != len(records) or plan.get(
        "partition_complete"
    ) is not True:
        raise ValueError("recovery plan partition is incomplete")
    if int(plan.get("machine_relation_backfill_eligible") or 0) != machine_eligible:
        raise ValueError("machine recovery count does not match records")
    if len(_text(plan.get("source_database_logical_sha256"))) != 64:
        raise ValueError("source_database_logical_sha256 is missing or invalid")
    target_identity = plan.get("target_ledger_identity")
    if not isinstance(target_identity, dict):
        raise ValueError("target_ledger_identity is missing or invalid")
    if _text(plan.get("target_ledger_resolved_path")) != _text(
        target_identity.get("resolved_path")
    ):
        raise ValueError("target ledger resolved path does not match identity")
    if _text(target_identity.get("source_database_logical_sha256")).lower() != _text(
        plan.get("source_database_logical_sha256")
    ).lower():
        raise ValueError("target identity is not bound to the source snapshot")
    if _text(plan.get("target_ledger_identity_sha256")) != sha256_json(target_identity):
        raise ValueError("target_ledger_identity_sha256 is invalid")
    logical_snapshot = {
        "source_schema_version": int(plan.get("source_schema_version") or 0),
        "source_database_logical_sha256": plan["source_database_logical_sha256"],
        "event_count": len(records),
        "event_receipts": [
            {
                "event_id": record["event_id"],
                "event_version": record["event_version"],
                "evidence_fingerprint": record["before"]["evidence_fingerprint"],
                "facts_sha256": record["before"]["facts_sha256"],
                "relations_sha256": record["before"]["relations_sha256"],
                "workflow_sha256": record["before"]["workflow_sha256"],
            }
            for record in records
        ],
    }
    if _text(plan.get("logical_snapshot_sha256")) != sha256_json(logical_snapshot):
        raise ValueError("logical_snapshot_sha256 does not match records")
    return claimed


def recovery_apply_scope(plan: dict[str, Any]) -> list[dict[str, Any]]:
    validate_recovery_plan(plan)
    scope: list[dict[str, Any]] = []
    for record in plan.get("records") or []:
        candidate = record.get("machine_relation_backfill")
        if not isinstance(candidate, dict):
            continue
        scope.append(
            {
                "event_id": record["event_id"],
                "event_version": int(record["event_version"]),
                "evidence_id": candidate["evidence_id"],
                "candidate_sha256": candidate["candidate_sha256"],
                "record_sha256": record["record_sha256"],
                "receipt_evidence_fingerprint": record["before"]["evidence_fingerprint"],
                "facts_sha256": record["before"]["facts_sha256"],
                "relations_sha256": record["before"]["relations_sha256"],
                "workflow_sha256": record["before"]["workflow_sha256"],
            }
        )
    return sorted(scope, key=lambda row: (row["event_id"], row["evidence_id"]))


def build_recovery_authorization_template(plan: dict[str, Any]) -> dict[str, Any]:
    scope = recovery_apply_scope(plan)
    return {
        "schema_version": 1,
        "action": APPLY_ACTION,
        "approved": False,
        "authorization_id": "FILL_ME",
        "actor": "FILL_ME",
        "purpose": "FILL_ME",
        "expires_at": "FILL_ME_WITH_TIMEZONE",
        "plan_sha256": plan["plan_sha256"],
        "logical_snapshot_sha256": plan["logical_snapshot_sha256"],
        "source_database_logical_sha256": plan["source_database_logical_sha256"],
        "target_ledger_resolved_path": plan["target_ledger_resolved_path"],
        "target_ledger_identity_sha256": plan["target_ledger_identity_sha256"],
        "scope": scope,
        "scope_sha256": sha256_json(scope),
        "backup_path": "FILL_ME",
        "backup_sha256": "FILL_ME",
        "no_event_version_mutation": True,
        "no_status_or_label_mutation": True,
        "no_trading": True,
    }


def _validated_authorization(
    ledger_path: Path,
    plan: dict[str, Any],
    authorization: dict[str, Any],
) -> dict[str, Any]:
    validate_recovery_plan(plan)
    if authorization.get("approved") is not True:
        raise ValueError("authorization approved must be true")
    if _text(authorization.get("action")) != APPLY_ACTION:
        raise ValueError("authorization action is invalid")
    for field in ("authorization_id", "actor", "purpose"):
        value = _text(authorization.get(field))
        if not value or value == "FILL_ME":
            raise ValueError(f"authorization {field} is required")
    expiry = _aware_timestamp(authorization.get("expires_at"))
    if expiry is None:
        raise ValueError("authorization expires_at must be timezone-aware")
    if expiry <= datetime.now(timezone.utc):
        raise ValueError("authorization is expired")
    if _text(authorization.get("plan_sha256")) != plan["plan_sha256"]:
        raise ValueError("authorization plan_sha256 does not match plan")
    if _text(authorization.get("logical_snapshot_sha256")) != plan["logical_snapshot_sha256"]:
        raise ValueError("authorization logical snapshot does not match plan")
    if _text(authorization.get("source_database_logical_sha256")) != plan[
        "source_database_logical_sha256"
    ]:
        raise ValueError("authorization database logical snapshot does not match plan")
    current_target_identity = _target_ledger_identity(
        ledger_path,
        source_database_logical_sha256=plan["source_database_logical_sha256"],
    )
    if current_target_identity != plan.get("target_ledger_identity"):
        raise ValueError("target ledger identity does not match the recovery plan")
    if _text(authorization.get("target_ledger_resolved_path")) != _text(
        plan.get("target_ledger_resolved_path")
    ):
        raise ValueError("authorization target ledger path does not match plan")
    if _text(authorization.get("target_ledger_identity_sha256")) != _text(
        plan.get("target_ledger_identity_sha256")
    ):
        raise ValueError("authorization target ledger identity does not match plan")
    scope = recovery_apply_scope(plan)
    if authorization.get("scope") != scope:
        raise ValueError("authorization scope does not exactly match safe recovery scope")
    if _text(authorization.get("scope_sha256")) != sha256_json(scope):
        raise ValueError("authorization scope_sha256 is invalid")
    backup_path = Path(_text(authorization.get("backup_path")))
    if not backup_path.is_file():
        raise ValueError("authorization backup_path is not a file")
    claimed_backup_sha256 = _text(authorization.get("backup_sha256")).lower()
    if len(claimed_backup_sha256) != 64 or sha256_file(backup_path) != claimed_backup_sha256:
        raise ValueError("authorization backup_sha256 does not match backup file")
    for field in (
        "no_event_version_mutation",
        "no_status_or_label_mutation",
        "no_trading",
    ):
        if authorization.get(field) is not True:
            raise ValueError(f"authorization must preserve {field}")
    return authorization


def _validate_scope_against_connection(
    connection: sqlite3.Connection, plan: dict[str, Any]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[str]]:
    records = {
        str(record["event_id"]): record
        for record in plan.get("records") or []
        if isinstance(record.get("machine_relation_backfill"), dict)
    }
    prepared: list[tuple[dict[str, Any], dict[str, Any]]] = []
    issues: list[str] = []
    if _database_logical_sha256(connection) != plan["source_database_logical_sha256"]:
        issues.append("DATABASE_LOGICAL_SNAPSHOT_DRIFT")
    for scope_row in recovery_apply_scope(plan):
        event_id = str(scope_row["event_id"])
        record = records[event_id]
        try:
            receipt = event_receipt(connection, event_id)
        except (ValueError, sqlite3.DatabaseError) as exc:
            issues.append(f"{event_id}: EVENT_UNAVAILABLE: {exc}")
            continue
        version = int(receipt["current_version"])
        facts = _current_facts(connection, event_id, version)
        relation_snapshot = _current_relation_snapshot(connection, event_id, version)
        workflow_snapshot = _current_workflow_snapshot(connection, event_id, version)
        bound_evidence_id = _text(facts.get("evidence_id"))
        bound_evidence = next(
            (
                row
                for row in receipt.get("evidence") or []
                if _text(row.get("evidence_id")) == bound_evidence_id
            ),
            None,
        )
        source_revision_issues = (
            _source_revision_binding_issues(bound_evidence, facts)
            if isinstance(bound_evidence, dict)
            else []
        )
        checks = {
            "event_version": version,
            "evidence_fingerprint": receipt["evidence_fingerprint"],
            "facts_sha256": sha256_json(facts),
            "relations_sha256": sha256_json(relation_snapshot),
            "workflow_sha256": sha256_json(workflow_snapshot),
        }
        expected = {
            "event_version": int(record["event_version"]),
            "evidence_fingerprint": record["before"]["evidence_fingerprint"],
            "facts_sha256": record["before"]["facts_sha256"],
            "relations_sha256": record["before"]["relations_sha256"],
            "workflow_sha256": record["before"]["workflow_sha256"],
        }
        drift = [field for field in checks if checks[field] != expected[field]]
        if drift:
            issues.extend(f"{event_id}: {reason}" for reason in source_revision_issues)
            issues.append(f"{event_id}: STALE_CAS_{'_'.join(field.upper() for field in drift)}")
            continue
        recalculated, reasons = _machine_relation_candidate(
            connection,
            receipt,
            facts,
            relation_snapshot,
            workflow_snapshot,
        )
        if recalculated != record["machine_relation_backfill"]:
            detail = ",".join(reasons) if reasons else "CANDIDATE_HASH_CHANGED"
            issues.append(f"{event_id}: SAFE_SUBSET_REPROOF_FAILED: {detail}")
            continue
        prepared.append((record, recalculated))
    return prepared, issues


def _validate_backup(
    ledger_path: Path, plan: dict[str, Any], authorization: dict[str, Any]
) -> None:
    backup_path = Path(_text(authorization["backup_path"])).resolve()
    target_path = ledger_path.resolve()
    try:
        same_file_identity = backup_path.samefile(target_path)
    except OSError as exc:
        raise ValueError("cannot verify that backup_path is independent") from exc
    if backup_path == target_path or same_file_identity:
        raise ValueError(
            "backup_path must be an independent file, not the target ledger or a hard link"
        )
    with closing(_read_only_connection(backup_path)) as backup:
        _require_apply_schema(backup)
        quick_check = backup.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or _text(quick_check[0]).lower() != "ok":
            raise ValueError("backup quick_check failed")
        integrity_check = backup.execute("PRAGMA integrity_check").fetchone()
        if integrity_check is None or _text(integrity_check[0]).lower() != "ok":
            raise ValueError("backup integrity_check failed")
        if _database_logical_sha256(backup) != plan["source_database_logical_sha256"]:
            raise ValueError(
                "backup database logical snapshot differs; incomplete WAL copy is unsafe"
            )
        prepared, issues = _validate_scope_against_connection(backup, plan)
        if issues or len(prepared) != len(recovery_apply_scope(plan)):
            raise ValueError("backup does not contain the exact authorized pre-mutation scope")
    backup_plan = build_recovery_plan(backup_path)
    if backup_plan["source_event_count"] != plan["source_event_count"]:
        raise ValueError("backup source event count does not match the authorized plan")
    if backup_plan["logical_snapshot_sha256"] != plan["logical_snapshot_sha256"]:
        raise ValueError("backup logical snapshot does not match the authorized plan")
    if backup_plan["source_database_logical_sha256"] != plan[
        "source_database_logical_sha256"
    ]:
        raise ValueError(
            "backup database logical snapshot differs; incomplete WAL copy is unsafe"
        )


def _ensure_recovery_audit_schema(connection: sqlite3.Connection) -> None:
    """Create the minimal append-only DB receipt in the apply transaction."""

    # ``executescript`` implicitly commits a pending Python sqlite3
    # transaction.  Issue each DDL statement separately so table creation,
    # relation/workflow inserts and the durable receipt share BEGIN IMMEDIATE.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS event_quality_recovery_audit (
            audit_id TEXT PRIMARY KEY,
            contract_version TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state='DB_COMMITTED'),
            authorization_id TEXT NOT NULL,
            authorization_sha256 TEXT NOT NULL,
            target_ledger_identity_sha256 TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,
            scope_sha256 TEXT NOT NULL,
            result_sha256 TEXT NOT NULL,
            result_json TEXT NOT NULL,
            committed_at TEXT NOT NULL,
            no_status_or_version_mutation INTEGER NOT NULL CHECK (
                no_status_or_version_mutation=1
            ),
            no_trading INTEGER NOT NULL CHECK (no_trading=1)
        )
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS event_quality_recovery_audit_no_update
        BEFORE UPDATE ON event_quality_recovery_audit
        BEGIN
            SELECT RAISE(ABORT,'event_quality_recovery_audit is append-only');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS event_quality_recovery_audit_no_delete
        BEFORE DELETE ON event_quality_recovery_audit
        BEGIN
            SELECT RAISE(ABORT,'event_quality_recovery_audit is append-only');
        END
        """
    )


def apply_machine_relation_backfill(
    ledger_path: Path,
    plan: dict[str, Any],
    authorization: dict[str, Any] | None = None,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    """Dry-run or atomically insert the exact authorized safe subset.

    ``execute`` defaults to ``False``.  Even an approved authorization file is
    inert unless the caller explicitly opts into execution.
    """

    validate_recovery_plan(plan)
    scope = recovery_apply_scope(plan)
    if not execute:
        with closing(_read_only_connection(ledger_path)) as connection:
            connection.execute("BEGIN")
            prepared, issues = _validate_scope_against_connection(connection, plan)
            connection.rollback()
        result = {
            "contract_version": APPLY_CONTRACT_VERSION,
            "mode": "DRY_RUN",
            "plan_sha256": plan["plan_sha256"],
            "authorized_scope_count": len(scope),
            "ready_to_apply": len(prepared) if not issues else 0,
            "individually_revalidated": len(prepared),
            "stale_or_blocked": len(issues),
            "issues": issues,
            "applied": 0,
            "canonical_status_or_version_mutation": False,
            "no_trading": True,
        }
        result["result_sha256"] = sha256_json(result)
        return result

    if not scope:
        raise ValueError(
            "recovery plan contains no machine-safe rows; human/reparse work remains"
        )
    auth = _validated_authorization(ledger_path, plan, authorization or {})
    _validate_backup(ledger_path, plan, auth)
    connection = sqlite3.connect(ledger_path)
    connection.row_factory = sqlite3.Row
    changes: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        _require_apply_schema(connection)
        prepared, issues = _validate_scope_against_connection(connection, plan)
        if issues or len(prepared) != len(scope):
            raise ValueError("STALE_RECOVERY_SCOPE: " + "; ".join(issues))
        now = utc_now()
        audit_id = "EQR-AUDIT-" + sha256_json(
            {
                "authorization_id": auth["authorization_id"],
                "plan_sha256": plan["plan_sha256"],
                "scope_sha256": auth["scope_sha256"],
                "target_ledger_identity_sha256": plan[
                    "target_ledger_identity_sha256"
                ],
            }
        )[:32]
        _ensure_recovery_audit_schema(connection)
        assessed_by = (
            f"authorized_historical_recovery:{_text(auth['authorization_id'])}:"
            f"{_text(auth['actor'])}"
        )
        for record, candidate in prepared:
            connection.execute(
                """INSERT INTO event_evidence_relations(
                       event_id,evidence_id,event_version,relation_status,subject_match,
                       event_claim_supported,date_coherent,modality,evidence_fingerprint,
                       contract_version,assessed_by,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate["event_id"],
                    candidate["evidence_id"],
                    candidate["event_version"],
                    candidate["relation_status"],
                    candidate["subject_match"],
                    candidate["event_claim_supported"],
                    candidate["date_coherent"],
                    candidate["modality"],
                    candidate["evidence_fingerprint"],
                    candidate["contract_version"],
                    assessed_by,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO event_fact_workflow(
                       event_id,event_version,workflow_state,reason_codes_json,
                       evidence_fingerprint,contract_version,updated_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    candidate["event_id"],
                    candidate["event_version"],
                    candidate["workflow_state"],
                    stable_json(["MACHINE_RECONSTRUCTED_FROZEN_ADMISSION_BINDING"]),
                    candidate["evidence_fingerprint"],
                    candidate["contract_version"],
                    now,
                ),
            )
            changes.append(
                {
                    "event_id": candidate["event_id"],
                    "event_version_before": candidate["event_version"],
                    "event_version_after": candidate["event_version"],
                    "status_before": record["before"]["status"],
                    "status_after": record["before"]["status"],
                    "evidence_id": candidate["evidence_id"],
                    "candidate_sha256": candidate["candidate_sha256"],
                }
            )
        result = {
            "contract_version": APPLY_CONTRACT_VERSION,
            "mode": "APPLIED",
            "plan_sha256": plan["plan_sha256"],
            "authorization_id": auth["authorization_id"],
            "applied_at": now,
            "authorized_scope_count": len(scope),
            "applied": len(changes),
            "changes": changes,
            "target_ledger_identity_sha256": plan[
                "target_ledger_identity_sha256"
            ],
            "durable_audit_id": audit_id,
            "durable_audit_state": "DB_COMMITTED",
            "canonical_status_or_version_mutation": False,
            "no_trading": True,
        }
        result["result_sha256"] = sha256_json(result)
        connection.execute(
            """INSERT INTO event_quality_recovery_audit(
                   audit_id,contract_version,state,authorization_id,
                   authorization_sha256,target_ledger_identity_sha256,
                   plan_sha256,scope_sha256,result_sha256,result_json,
                   committed_at,no_status_or_version_mutation,no_trading
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                audit_id,
                RECOVERY_AUDIT_CONTRACT_VERSION,
                "DB_COMMITTED",
                auth["authorization_id"],
                sha256_json(auth),
                plan["target_ledger_identity_sha256"],
                plan["plan_sha256"],
                auth["scope_sha256"],
                result["result_sha256"],
                stable_json(result),
                now,
                1,
                1,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if result is None:  # pragma: no cover - defensive; transaction paths return or raise.
        raise RuntimeError("recovery apply completed without a durable result")
    return result
