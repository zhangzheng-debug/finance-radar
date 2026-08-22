"""Read-only, provider-neutral AI census for every canonical event.

The census is deliberately an advisory lane.  It freezes event packets, splits
them between two operators with a deterministic five-percent overlap, validates
line-oriented AI output, and merges the returned records.  It never writes to
the ledger and exposes no method that can change canonical truth.

This contract is separate from the human fact-review and human blind-label
contracts.  AI census output is useful for queue routing and integrity audits;
it is not a human label, a formal verification, a model target, or a trading
instruction.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.services.event_fact_review import event_receipt


SCHEMA_VERSION = 1
CONTRACT_VERSION = "ai-census-v1"
PROMPT_VERSION = "ai-census-prompt-v1"
PROMPT_SHA256 = "883687c79bc2bf826a64d55470487ca0e2e8c2430ccc36767b10ac42f1f7bde5"
OVERLAP_RATE = 0.05
REVIEWER_SLOTS = {"A", "B"}

CHECK_VALUES = {"YES", "NO", "UNCLEAR"}
EVENT_STAGES = {"REALIZED", "PROPOSED_OR_CONDITIONAL", "UNCLEAR"}
MATERIALITY_VALUES = {"MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "UNCLEAR"}
POLARITY_VALUES = {"ADVERSE", "POSITIVE", "NEUTRAL", "MIXED", "UNCLEAR"}
EVIDENCE_STATES = {
    "PRIMARY_SUPPORTED",
    "MULTI_SOURCE_SUPPORTED",
    "DISCOVERY_ONLY",
    "CONFLICTED",
    "INSUFFICIENT",
}
DISPOSITIONS = {
    "AI_CONFIRM_CANDIDATE",
    "AI_REJECT_CANDIDATE",
    "AI_NEEDS_EVIDENCE",
    "AI_ESCALATE",
    "AI_DUPLICATE_CANDIDATE",
}
REASON_CODES = {
    "SUPPORTED_BY_PRIMARY",
    "SUPPORTED_BY_MULTIPLE",
    "WRONG_SUBJECT",
    "CLAIM_NOT_SUPPORTED",
    "DATE_STAGE_MISMATCH",
    "NEGATED_OR_WITHDRAWN",
    "ONLY_DISCOVERY_SOURCE",
    "NO_EXACT_PASSAGE",
    "SOURCE_UNAVAILABLE",
    "CONFLICTING_EVIDENCE",
    "POSSIBLE_DUPLICATE",
    "COMPLEX_EVENT_CHAIN",
    "LEGAL_OR_EQUITY_OUTCOME_UNCLEAR",
    "CLASSIFICATION_UNCLEAR",
    "OTHER",
}
TOOL_MODES = {"MANUAL_UPLOAD", "LOCAL_API", "OTHER"}

CHECK_FIELDS = {
    "source_accessible",
    "subject_match",
    "event_claim_supported",
    "date_stage_coherent",
    "evidence_sufficient",
    "conflict_found",
}

SUBMISSION_HEADER_FIELDS = {
    "record_type",
    "schema_version",
    "contract_version",
    "batch_id",
    "reviewer_slot",
    "shard_id",
    "assignment_sha256",
    "ai_system",
    "complete",
    "ai_assisted",
    "human_reviewed",
    "formal_verification",
    "canonical_mutation_allowed",
    "no_market_outcome",
    "no_trading",
    "exported_at",
}

RESULT_FIELDS = {
    "record_type",
    "schema_version",
    "contract_version",
    "batch_id",
    "reviewer_slot",
    "shard_id",
    "assignment_sha256",
    "event_id",
    "event_version",
    "event_fingerprint",
    "packet_sha256",
    "checks",
    "event_stage",
    "materiality",
    "polarity",
    "evidence_state",
    "disposition",
    "reason_codes",
    "selected_evidence_ids",
    "possible_duplicate_event_ids",
    "summary",
    "rationale",
    "reviewed_at",
    "ai_assisted",
    "human_reviewed",
    "formal_verification",
    "canonical_mutation_allowed",
    "no_market_outcome",
    "no_trading",
}

AGREEMENT_FIELDS = (
    "checks",
    "event_stage",
    "materiality",
    "polarity",
    "evidence_state",
    "disposition",
    "reason_codes",
    "selected_evidence_ids",
    "possible_duplicate_event_ids",
)

BOUNDARY_VALUES = {
    "ai_assisted": True,
    "human_reviewed": False,
    "formal_verification": False,
    "canonical_mutation_allowed": False,
    "no_market_outcome": True,
    "no_trading": True,
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


def _require_timestamp(value: Any, field: str) -> str:
    text = _text(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{line_number}: every JSONL record must be an object")
        records.append(record)
    if not records:
        raise ValueError(f"{path}: JSONL file is empty")
    return records


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(stable_json(record) + "\n")


def _chain_index(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_chain_members'"
    ).fetchone()
    if table is None:
        return {}
    rows = connection.execute(
        """SELECT m.event_id,m.chain_id,m.chain_role,m.counts_as_primary_event,
                  c.chain_type,c.primary_event_id
           FROM event_chain_members m
           JOIN event_chains c ON c.chain_id=m.chain_id"""
    ).fetchall()
    return {
        str(row["event_id"]): {
            "chain_id": row["chain_id"],
            "chain_type": row["chain_type"],
            "chain_role": row["chain_role"],
            "primary_event_id": row["primary_event_id"],
            "counts_as_primary_event": bool(row["counts_as_primary_event"]),
        }
        for row in rows
    }


def _public_event_packet(receipt: dict[str, Any], chain: dict[str, Any] | None) -> dict[str, Any]:
    claim = receipt.get("claim") or {}
    evidence: list[dict[str, Any]] = []
    for row in receipt.get("evidence") or []:
        evidence.append(
            {
                "evidence_id": row.get("evidence_id"),
                "evidence_url": row.get("evidence_url"),
                "filing_date": row.get("filing_date"),
                "form": row.get("form"),
                "items": row.get("items"),
                "evidence_passage": row.get("evidence_passage"),
                "evidence_status": row.get("evidence_status"),
                "content_sha256": row.get("content_sha256"),
                "source_id": row.get("source_id"),
                "source_name": row.get("source_name"),
                "authority_tier": row.get("authority_tier"),
                "source_type": row.get("source_type"),
            }
        )
    packet: dict[str, Any] = {
        "record_type": "event_packet",
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "event_id": receipt["event_id"],
        "event_version": int(receipt["current_version"]),
        "event_fingerprint": receipt["evidence_fingerprint"],
        "event_date": receipt.get("event_date"),
        "stable_id": receipt.get("stable_id"),
        "ticker_at_event": receipt.get("ticker_at_event"),
        "company_name": receipt.get("company_name"),
        "proposed_event_family": receipt.get("event_family"),
        "proposed_event_type": receipt.get("event_type"),
        "discovery_source": receipt.get("discovery_source"),
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
        "evidence_count": len(evidence),
        "evidence": evidence,
        "event_chain": chain,
        **BOUNDARY_VALUES,
    }
    packet["packet_sha256"] = sha256_json(packet)
    return packet


def extract_all_event_packets(ledger_path: Path) -> dict[str, Any]:
    """Freeze every canonical event from a SQLite ledger opened read-only.

    Existing canonical status and manual/model conclusions are retained only in
    the owner index.  They are intentionally absent from operator packets to
    reduce anchoring during the AI census.
    """

    with closing(_read_only_connection(ledger_path)) as connection:
        # Hold one SQLite read snapshot across the N event/evidence queries. In
        # WAL mode this does not stop collectors from writing, but it prevents
        # a single export from mixing event versions committed at different
        # moments.
        connection.execute("BEGIN")
        rows = connection.execute(
            """SELECT event_id,current_version,status,label_status,manual_grade,
                      last_updated_at
               FROM canonical_events ORDER BY event_id"""
        ).fetchall()
        chains = _chain_index(connection)
        packets: list[dict[str, Any]] = []
        owner_events: list[dict[str, Any]] = []
        for row in rows:
            event_id = str(row["event_id"])
            receipt = event_receipt(connection, event_id)
            packet = _public_event_packet(receipt, chains.get(event_id))
            packets.append(packet)
            owner_events.append(
                {
                    "event_id": event_id,
                    "event_version": int(row["current_version"]),
                    "event_fingerprint": receipt["evidence_fingerprint"],
                    "packet_sha256": packet["packet_sha256"],
                    "canonical_status_at_freeze": row["status"],
                    "label_status_at_freeze": row["label_status"],
                    "manual_grade_at_freeze": row["manual_grade"],
                    "last_updated_at_freeze": row["last_updated_at"],
                }
            )
    if not packets:
        raise ValueError("ledger contains no canonical events")
    logical_snapshot_sha256 = sha256_json(
        {
            "contract_version": CONTRACT_VERSION,
            "events": owner_events,
            "packet_sha256": [packet["packet_sha256"] for packet in packets],
        }
    )
    return {
        "packets": packets,
        "owner_events": owner_events,
        "logical_snapshot_sha256": logical_snapshot_sha256,
        "status_counts": dict(Counter(row["canonical_status_at_freeze"] for row in owner_events)),
        "label_status_counts": dict(
            Counter(row["label_status_at_freeze"] for row in owner_events)
        ),
    }


def allocate_packets(
    packets: Sequence[dict[str, Any]],
    *,
    batch_id: str,
    overlap_rate: float = OVERLAP_RATE,
) -> dict[str, Any]:
    """Cover all events once and deterministically duplicate five percent.

    Apart from the overlap, each event belongs to exactly one slot.  Ranking is
    based only on ``batch_id`` and ``event_id`` so the assignment is reproducible
    and independent of canonical status or the event text.
    """

    if not _text(batch_id):
        raise ValueError("batch_id is required")
    if not 0 <= float(overlap_rate) <= 1:
        raise ValueError("overlap_rate must be between 0 and 1")
    ids = [_text(packet.get("event_id")) for packet in packets]
    if any(not event_id for event_id in ids):
        raise ValueError("every packet must have an event_id")
    if len(ids) != len(set(ids)):
        raise ValueError("event_id values must be unique")

    ranked = sorted(
        packets,
        key=lambda packet: hashlib.sha256(
            f"{batch_id}|{packet['event_id']}".encode("utf-8")
        ).hexdigest(),
    )
    overlap_count = min(len(ranked), math.ceil(len(ranked) * float(overlap_rate)))
    overlap_packets = ranked[:overlap_count]
    remainder = ranked[overlap_count:]
    slot_a = [*overlap_packets, *remainder[::2]]
    slot_b = [*overlap_packets, *remainder[1::2]]
    overlap_ids = [str(packet["event_id"]) for packet in overlap_packets]
    return {
        "batch_id": batch_id,
        "overlap_rate": float(overlap_rate),
        "overlap_count": overlap_count,
        "overlap_event_ids": overlap_ids,
        "slots": {"A": slot_a, "B": slot_b},
    }


def build_assignment_shards(
    allocation: dict[str, Any],
    *,
    generated_at: str,
    shard_size: int,
) -> list[list[dict[str, Any]]]:
    """Build self-verifying JSONL assignment records for both slots."""

    _require_timestamp(generated_at, "generated_at")
    if not 1 <= int(shard_size) <= 500:
        raise ValueError("shard_size must be between 1 and 500")
    batch_id = _text(allocation.get("batch_id"))
    overlap_ids = {str(value) for value in allocation.get("overlap_event_ids") or []}
    shards: list[list[dict[str, Any]]] = []
    for slot in ("A", "B"):
        packets = list((allocation.get("slots") or {}).get(slot) or [])
        for offset in range(0, len(packets), int(shard_size)):
            number = offset // int(shard_size) + 1
            chunk = packets[offset : offset + int(shard_size)]
            shard_id = f"{batch_id}-{slot}-{number:04d}"
            header: dict[str, Any] = {
                "record_type": "assignment_header",
                "schema_version": SCHEMA_VERSION,
                "contract_version": CONTRACT_VERSION,
                "prompt_version": PROMPT_VERSION,
                "batch_id": batch_id,
                "reviewer_slot": slot,
                "shard_id": shard_id,
                "generated_at": generated_at,
                "event_count": len(chunk),
                "overlap_event_count": sum(
                    1 for packet in chunk if str(packet["event_id"]) in overlap_ids
                ),
                "review_mode": "ai_assisted_advisory_census",
                **BOUNDARY_VALUES,
            }
            header["assignment_sha256"] = sha256_json(
                {"header": header, "events": chunk}
            )
            shards.append([header, *chunk])
    return shards


def parse_assignment_records(
    records: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not records or records[0].get("record_type") != "assignment_header":
        raise ValueError("first assignment record must be assignment_header")
    header = dict(records[0])
    packets = [dict(record) for record in records[1:]]
    issues: list[str] = []
    if header.get("schema_version") != SCHEMA_VERSION:
        issues.append(f"schema_version must be {SCHEMA_VERSION}")
    if _text(header.get("contract_version")) != CONTRACT_VERSION:
        issues.append(f"contract_version must be {CONTRACT_VERSION}")
    if _text(header.get("prompt_version")) != PROMPT_VERSION:
        issues.append(f"prompt_version must be {PROMPT_VERSION}")
    if _text(header.get("reviewer_slot")) not in REVIEWER_SLOTS:
        issues.append("reviewer_slot must be A or B")
    try:
        _require_timestamp(header.get("generated_at"), "generated_at")
    except ValueError as exc:
        issues.append(str(exc))
    for field, expected in BOUNDARY_VALUES.items():
        if header.get(field) is not expected:
            issues.append(f"assignment {field} must be {str(expected).lower()}")
    if header.get("event_count") != len(packets):
        issues.append("assignment event_count does not match JSONL packets")
    event_ids: set[str] = set()
    for index, packet in enumerate(packets, 1):
        prefix = f"event packet {index}"
        if packet.get("record_type") != "event_packet":
            issues.append(f"{prefix}: record_type must be event_packet")
        if packet.get("schema_version") != SCHEMA_VERSION:
            issues.append(f"{prefix}: schema_version must be {SCHEMA_VERSION}")
        if _text(packet.get("contract_version")) != CONTRACT_VERSION:
            issues.append(f"{prefix}: contract_version must be {CONTRACT_VERSION}")
        event_id = _text(packet.get("event_id"))
        if not event_id:
            issues.append(f"{prefix}: event_id is required")
        elif event_id in event_ids:
            issues.append(f"{prefix}: duplicate event_id {event_id}")
        event_ids.add(event_id)
        claimed_packet_hash = _text(packet.get("packet_sha256"))
        packet_without_hash = dict(packet)
        packet_without_hash.pop("packet_sha256", None)
        if claimed_packet_hash != sha256_json(packet_without_hash):
            issues.append(f"{prefix}: packet_sha256 does not match packet content")
        for field, expected in BOUNDARY_VALUES.items():
            if packet.get(field) is not expected:
                issues.append(f"{prefix}: {field} must be {str(expected).lower()}")
    claimed = _text(header.pop("assignment_sha256", ""))
    calculated = sha256_json({"header": header, "events": packets})
    header["assignment_sha256"] = claimed
    if claimed != calculated:
        issues.append("assignment_sha256 does not match assignment content")
    if issues:
        raise ValueError("; ".join(issues))
    return header, packets


def _check_boundary_fields(record: dict[str, Any], prefix: str, issues: list[str]) -> None:
    for field, expected in BOUNDARY_VALUES.items():
        if record.get(field) is not expected:
            issues.append(f"{prefix}: {field} must be {str(expected).lower()}")


def _normalize_string_list(
    value: Any,
    *,
    field: str,
    allowed: set[str] | None,
    issues: list[str],
) -> list[str]:
    if not isinstance(value, list):
        issues.append(f"{field} must be a list")
        return []
    normalized = [_text(item) for item in value]
    if any(not item for item in normalized):
        issues.append(f"{field} cannot contain blank values")
    if len(normalized) != len(set(normalized)):
        issues.append(f"{field} cannot contain duplicate values")
    if allowed is not None:
        unsupported = sorted(set(normalized) - allowed)
        if unsupported:
            issues.append(f"{field} contains unsupported values: {unsupported}")
    return normalized


def validate_submission_records(
    assignment_records: Sequence[dict[str, Any]],
    submission_records: Sequence[dict[str, Any]],
    *,
    batch_event_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate one complete AI shard without changing any external state."""

    try:
        assignment_header, packets = parse_assignment_records(assignment_records)
    except ValueError as exc:
        return {"valid": False, "issues": [f"invalid assignment: {exc}"], "results": []}
    issues: list[str] = []
    if not submission_records or submission_records[0].get("record_type") != "submission_header":
        return {
            "valid": False,
            "issues": ["first submission record must be submission_header"],
            "results": [],
        }
    header = dict(submission_records[0])
    extra_header_fields = sorted(set(header) - SUBMISSION_HEADER_FIELDS)
    missing_header_fields = sorted(SUBMISSION_HEADER_FIELDS - set(header))
    if extra_header_fields:
        issues.append(f"submission header has unsupported fields: {extra_header_fields}")
    if missing_header_fields:
        issues.append(f"submission header is missing fields: {missing_header_fields}")
    for field in ("schema_version", "contract_version", "batch_id", "reviewer_slot", "shard_id"):
        if header.get(field) != assignment_header.get(field):
            issues.append(f"submission header {field} does not match assignment")
    if _text(header.get("assignment_sha256")) != _text(
        assignment_header.get("assignment_sha256")
    ):
        issues.append("submission header assignment_sha256 does not match assignment")
    if header.get("complete") is not True:
        issues.append("submission header complete must be true")
    _check_boundary_fields(header, "submission header", issues)
    try:
        _require_timestamp(header.get("exported_at"), "exported_at")
    except ValueError as exc:
        issues.append(str(exc))
    ai_system = header.get("ai_system")
    if not isinstance(ai_system, dict):
        issues.append("submission header ai_system must be an object")
        ai_system = {}
    expected_ai_fields = {
        "provider",
        "model",
        "prompt_version",
        "prompt_sha256",
        "tool_mode",
    }
    if set(ai_system) != expected_ai_fields:
        issues.append(
            "ai_system must contain exactly provider, model, prompt_version, "
            "prompt_sha256 and tool_mode"
        )
    if len(_text(ai_system.get("provider"))) < 2:
        issues.append("ai_system.provider is required")
    if len(_text(ai_system.get("model"))) < 2:
        issues.append("ai_system.model is required")
    if _text(ai_system.get("prompt_version")) != PROMPT_VERSION:
        issues.append(f"ai_system.prompt_version must be {PROMPT_VERSION}")
    if _text(ai_system.get("prompt_sha256")) != PROMPT_SHA256:
        issues.append("ai_system.prompt_sha256 does not match the fixed prompt")
    if _text(ai_system.get("tool_mode")) not in TOOL_MODES:
        issues.append(f"ai_system.tool_mode must be one of {sorted(TOOL_MODES)}")

    packet_by_id = {str(packet["event_id"]): packet for packet in packets}
    allowed_batch_ids = (
        {str(value) for value in batch_event_ids}
        if batch_event_ids is not None
        else set(packet_by_id)
    )
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, source in enumerate(submission_records[1:], 2):
        row = dict(source)
        prefix = f"submission line {line_number}"
        extra_fields = sorted(set(row) - RESULT_FIELDS)
        missing_fields = sorted(RESULT_FIELDS - set(row))
        if extra_fields:
            issues.append(f"{prefix}: unsupported fields: {extra_fields}")
        if missing_fields:
            issues.append(f"{prefix}: missing fields: {missing_fields}")
        if row.get("record_type") != "ai_census_result":
            issues.append(f"{prefix}: record_type must be ai_census_result")
        for field in (
            "schema_version",
            "contract_version",
            "batch_id",
            "reviewer_slot",
            "shard_id",
            "assignment_sha256",
        ):
            if row.get(field) != assignment_header.get(field):
                issues.append(f"{prefix}: {field} does not match assignment")
        _check_boundary_fields(row, prefix, issues)
        event_id = _text(row.get("event_id"))
        packet = packet_by_id.get(event_id)
        if packet is None:
            issues.append(f"{prefix}: event_id is not assigned to this shard")
            continue
        if event_id in seen:
            issues.append(f"{prefix}: duplicate event_id {event_id}")
            continue
        seen.add(event_id)
        if row.get("event_version") != packet.get("event_version"):
            issues.append(f"{prefix}: event_version does not match packet")
        if _text(row.get("event_fingerprint")) != _text(packet.get("event_fingerprint")):
            issues.append(f"{prefix}: event_fingerprint does not match packet")
        if _text(row.get("packet_sha256")) != _text(packet.get("packet_sha256")):
            issues.append(f"{prefix}: packet_sha256 does not match packet")
        checks = row.get("checks")
        if not isinstance(checks, dict):
            issues.append(f"{prefix}: checks must be an object")
            checks = {}
        if set(checks) != CHECK_FIELDS:
            issues.append(f"{prefix}: checks must contain exactly {sorted(CHECK_FIELDS)}")
        for field in CHECK_FIELDS:
            if _text(checks.get(field)) not in CHECK_VALUES:
                issues.append(f"{prefix}: checks.{field} must be YES, NO or UNCLEAR")
        event_stage = _text(row.get("event_stage"))
        materiality = _text(row.get("materiality"))
        polarity = _text(row.get("polarity"))
        evidence_state = _text(row.get("evidence_state"))
        disposition = _text(row.get("disposition"))
        if event_stage not in EVENT_STAGES:
            issues.append(f"{prefix}: invalid event_stage")
        if materiality not in MATERIALITY_VALUES:
            issues.append(f"{prefix}: invalid materiality")
        if polarity not in POLARITY_VALUES:
            issues.append(f"{prefix}: invalid polarity")
        if evidence_state not in EVIDENCE_STATES:
            issues.append(f"{prefix}: invalid evidence_state")
        if disposition not in DISPOSITIONS:
            issues.append(f"{prefix}: invalid disposition")
        reason_codes = _normalize_string_list(
            row.get("reason_codes"),
            field=f"{prefix}.reason_codes",
            allowed=REASON_CODES,
            issues=issues,
        )
        if not reason_codes:
            issues.append(f"{prefix}: at least one reason_code is required")
        evidence_ids = {
            _text(item.get("evidence_id")) for item in packet.get("evidence") or []
        }
        selected_ids = _normalize_string_list(
            row.get("selected_evidence_ids"),
            field=f"{prefix}.selected_evidence_ids",
            allowed=evidence_ids,
            issues=issues,
        )
        duplicate_ids = _normalize_string_list(
            row.get("possible_duplicate_event_ids"),
            field=f"{prefix}.possible_duplicate_event_ids",
            allowed=allowed_batch_ids,
            issues=issues,
        )
        if event_id in duplicate_ids:
            issues.append(f"{prefix}: possible duplicate cannot be the event itself")
        if len(_text(row.get("summary"))) < 10:
            issues.append(f"{prefix}: summary must contain at least 10 characters")
        if len(_text(row.get("rationale"))) < 20:
            issues.append(f"{prefix}: rationale must contain at least 20 characters")
        try:
            _require_timestamp(row.get("reviewed_at"), f"{prefix}.reviewed_at")
        except ValueError as exc:
            issues.append(str(exc))

        if disposition == "AI_CONFIRM_CANDIDATE":
            required_yes = {
                "subject_match",
                "event_claim_supported",
                "date_stage_coherent",
                "evidence_sufficient",
            }
            if any(_text(checks.get(field)) != "YES" for field in required_yes):
                issues.append(f"{prefix}: AI_CONFIRM_CANDIDATE requires affirmative checks")
            if _text(checks.get("conflict_found")) != "NO":
                issues.append(f"{prefix}: AI_CONFIRM_CANDIDATE requires conflict_found=NO")
            if evidence_state not in {"PRIMARY_SUPPORTED", "MULTI_SOURCE_SUPPORTED"}:
                issues.append(f"{prefix}: AI_CONFIRM_CANDIDATE requires supported evidence_state")
            if not selected_ids:
                issues.append(f"{prefix}: AI_CONFIRM_CANDIDATE requires selected evidence")
        elif disposition == "AI_REJECT_CANDIDATE":
            rejected = any(
                _text(checks.get(field)) == "NO"
                for field in ("subject_match", "event_claim_supported", "date_stage_coherent")
            )
            if not rejected:
                issues.append(f"{prefix}: AI_REJECT_CANDIDATE requires a failed identity/claim/date check")
        elif disposition == "AI_NEEDS_EVIDENCE":
            if evidence_state not in {"DISCOVERY_ONLY", "INSUFFICIENT"}:
                issues.append(f"{prefix}: AI_NEEDS_EVIDENCE requires discovery-only or insufficient evidence")
        elif disposition == "AI_ESCALATE":
            escalation = (
                _text(checks.get("conflict_found")) == "YES"
                or evidence_state == "CONFLICTED"
                or bool(
                    set(reason_codes)
                    & {
                        "CONFLICTING_EVIDENCE",
                        "COMPLEX_EVENT_CHAIN",
                        "LEGAL_OR_EQUITY_OUTCOME_UNCLEAR",
                        "CLASSIFICATION_UNCLEAR",
                    }
                )
            )
            if not escalation:
                issues.append(f"{prefix}: AI_ESCALATE requires a conflict or escalation reason")
        elif disposition == "AI_DUPLICATE_CANDIDATE":
            if not duplicate_ids or "POSSIBLE_DUPLICATE" not in reason_codes:
                issues.append(f"{prefix}: AI_DUPLICATE_CANDIDATE requires duplicate IDs and reason")
        results.append(row)

    missing = sorted(set(packet_by_id) - seen)
    if missing:
        issues.append(f"submission is incomplete; missing {len(missing)} assigned events")
    if len(results) != len(packets):
        issues.append(
            f"submission result count {len(results)} does not match assignment count {len(packets)}"
        )
    return {
        "valid": not issues,
        "issues": issues,
        "batch_id": assignment_header["batch_id"],
        "reviewer_slot": assignment_header["reviewer_slot"],
        "shard_id": assignment_header["shard_id"],
        "assignment_sha256": assignment_header["assignment_sha256"],
        "event_count": len(packets),
        "results": results if not issues else [],
        "canonical_state_changed": False,
        "formal_verification": False,
        "no_trading": True,
    }


def _review_projection(review: dict[str, Any]) -> dict[str, Any]:
    return {field: review.get(field) for field in AGREEMENT_FIELDS}


def merge_census_submissions(
    assignment_records: Sequence[Sequence[dict[str, Any]]],
    submission_records: Sequence[Sequence[dict[str, Any]]],
    *,
    expected_event_ids: Iterable[str],
    overlap_event_ids: Iterable[str],
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Merge validated shards into advisory per-event records.

    Complete mode enforces every expected shard and the exact one-review/two-
    review coverage contract.  No output field can be interpreted as a
    canonical mutation authorization.
    """

    assignments: dict[str, Sequence[dict[str, Any]]] = {}
    packet_by_id: dict[str, dict[str, Any]] = {}
    expected_slots_by_event: dict[str, set[str]] = defaultdict(set)
    batch_ids: set[str] = set()
    for records in assignment_records:
        header, packets = parse_assignment_records(records)
        assignment_hash = str(header["assignment_sha256"])
        if assignment_hash in assignments:
            raise ValueError(f"duplicate assignment_sha256: {assignment_hash}")
        assignments[assignment_hash] = records
        batch_ids.add(str(header["batch_id"]))
        slot = str(header["reviewer_slot"])
        for packet in packets:
            event_id = str(packet["event_id"])
            existing = packet_by_id.get(event_id)
            if existing is not None and (
                existing.get("packet_sha256") != packet.get("packet_sha256")
            ):
                raise ValueError(f"overlap packet differs between assignments: {event_id}")
            packet_by_id[event_id] = packet
            expected_slots_by_event[event_id].add(slot)
    if len(batch_ids) != 1:
        raise ValueError("all assignments must belong to one batch")

    all_batch_ids = {str(value) for value in expected_event_ids}
    overlap_ids = {str(value) for value in overlap_event_ids}
    if set(packet_by_id) != all_batch_ids:
        raise ValueError("assignment coverage does not match expected event IDs")
    actual_overlap = {
        event_id for event_id, slots in expected_slots_by_event.items() if slots == {"A", "B"}
    }
    if actual_overlap != overlap_ids:
        raise ValueError("assignment overlap does not match owner index")

    submitted_assignments: set[str] = set()
    reviews_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for records in submission_records:
        if not records or records[0].get("record_type") != "submission_header":
            raise ValueError("submission is missing submission_header")
        assignment_hash = _text(records[0].get("assignment_sha256"))
        assignment = assignments.get(assignment_hash)
        if assignment is None:
            raise ValueError(f"submission references unknown assignment: {assignment_hash}")
        if assignment_hash in submitted_assignments:
            raise ValueError(f"duplicate submission for assignment: {assignment_hash}")
        report = validate_submission_records(
            assignment,
            records,
            batch_event_ids=all_batch_ids,
        )
        if not report["valid"]:
            raise ValueError(
                f"invalid submission {report.get('shard_id')}: " + "; ".join(report["issues"])
            )
        submitted_assignments.add(assignment_hash)
        for review in report["results"]:
            reviews_by_event[str(review["event_id"])].append(review)

    missing_assignments = sorted(set(assignments) - submitted_assignments)
    if missing_assignments and not allow_partial:
        raise ValueError(f"missing {len(missing_assignments)} complete shard submissions")

    merged_rows: list[dict[str, Any]] = []
    coverage_issues: list[str] = []
    for event_id in sorted(all_batch_ids):
        reviews = sorted(reviews_by_event.get(event_id, []), key=lambda row: row["reviewer_slot"])
        expected_slots = expected_slots_by_event[event_id]
        actual_slots = {str(row["reviewer_slot"]) for row in reviews}
        if len(actual_slots) != len(reviews):
            coverage_issues.append(f"{event_id}: duplicate reviewer-slot results")
        if not allow_partial and actual_slots != expected_slots:
            coverage_issues.append(
                f"{event_id}: expected slots {sorted(expected_slots)}, got {sorted(actual_slots)}"
            )
        if not reviews:
            if allow_partial:
                continue
            coverage_issues.append(f"{event_id}: no result")
            continue
        packet = packet_by_id[event_id]
        agreement = len(reviews) == 1 or all(
            _review_projection(review) == _review_projection(reviews[0])
            for review in reviews[1:]
        )
        if len(reviews) == 1:
            coverage_status = "SINGLE_REVIEW"
            advisory = _review_projection(reviews[0])
        elif agreement:
            coverage_status = "OVERLAP_AGREEMENT"
            advisory = _review_projection(reviews[0])
        else:
            coverage_status = "OVERLAP_DISAGREEMENT"
            advisory = None
        dispositions = {str(row["disposition"]) for row in reviews}
        requires_followup = (
            not agreement
            or bool(
                dispositions
                & {
                    "AI_NEEDS_EVIDENCE",
                    "AI_ESCALATE",
                    "AI_DUPLICATE_CANDIDATE",
                }
            )
        )
        merged_rows.append(
            {
                "record_type": "merged_census_event",
                "schema_version": SCHEMA_VERSION,
                "contract_version": CONTRACT_VERSION,
                "batch_id": next(iter(batch_ids)),
                "event_id": event_id,
                "event_version": packet["event_version"],
                "event_fingerprint": packet["event_fingerprint"],
                "packet_sha256": packet["packet_sha256"],
                "review_count": len(reviews),
                "reviewer_slots": sorted(actual_slots),
                "coverage_status": coverage_status,
                "advisory_consensus": advisory,
                "requires_human_followup": requires_followup,
                "reviews": reviews,
                **BOUNDARY_VALUES,
            }
        )
    if coverage_issues and not allow_partial:
        raise ValueError("; ".join(coverage_issues))
    coverage_counts = Counter(row["coverage_status"] for row in merged_rows)
    disposition_counts = Counter(
        str((row.get("advisory_consensus") or {}).get("disposition") or "DISAGREEMENT")
        for row in merged_rows
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "batch_id": next(iter(batch_ids)),
        "expected_event_count": len(all_batch_ids),
        "merged_event_count": len(merged_rows),
        "expected_assignment_count": len(assignments),
        "received_assignment_count": len(submitted_assignments),
        "missing_assignment_count": len(missing_assignments),
        "overlap_event_count": len(overlap_ids),
        "coverage_counts": dict(coverage_counts),
        "advisory_disposition_counts": dict(disposition_counts),
        "human_followup_count": sum(
            1 for row in merged_rows if row["requires_human_followup"]
        ),
        "allow_partial": bool(allow_partial),
        "canonical_state_changed": False,
        "formal_verification": False,
        "human_reviewed": False,
        "no_trading": True,
    }
    header = {
        "record_type": "merged_census_header",
        "generated_at": utc_now(),
        **summary,
    }
    return {
        "summary": summary,
        "records": [header, *merged_rows],
        "coverage_issues": coverage_issues,
        "canonical_state_changed": False,
    }
