"""Re-admit legacy candidate events from exact current primary-source passages.

Planning is read-only.  Applying creates a new immutable event version while
preserving candidate/weak status, label, no-trading and every formal human
boundary.  A row is eligible only when the current deterministic admission
contract can reproduce an issuer-bound fact from a current P0/P1 passage.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.evidence_policy import is_primary_authority_tier
from app.services.event_admission import (
    ADMISSION_CONTRACT_VERSION,
    FACT_SLOT_CONTRACT_VERSION,
    READER_ALLOWED_EVIDENCE_STATUSES,
    evaluate_event_admission,
    extract_evidence_fact_slots,
    public_fact_summary,
)
from app.services.event_fact_review import event_receipt
from app.services.event_quality_recovery import (
    _aware_timestamp,
    _canonical_url,
    _current_facts,
    _current_relation_snapshot,
    _current_workflow_snapshot,
    _database_logical_sha256,
    _read_only_connection,
    _require_apply_schema,
    _target_ledger_identity,
    _text,
    build_recovery_plan,
    sha256_file,
    sha256_json,
    stable_json,
    utc_now,
)


PLAN_CONTRACT_VERSION = "historical-primary-readmission-plan-v1"
APPLY_CONTRACT_VERSION = "historical-primary-readmission-apply-v1"
AUDIT_CONTRACT_VERSION = "historical-primary-readmission-db-audit-v1"
APPLY_ACTION = "apply_historical_primary_readmission"
CHANGE_REASON = "authorized_historical_primary_readmission_v1"


def _known_at(evidence: dict[str, Any]) -> str:
    published = _aware_timestamp(evidence.get("source_published_at"))
    received = _aware_timestamp(evidence.get("local_received_at"))
    if published is None or received is None:
        return ""
    return max(published, received).isoformat()


def _date_coherent(receipt: dict[str, Any], evidence: dict[str, Any]) -> bool:
    event_date = _text(receipt.get("event_date"))[:10]
    evidence_dates = {
        _text(evidence.get("filing_date"))[:10],
        _text(evidence.get("source_published_at"))[:10],
    } - {""}
    return bool(event_date and event_date in evidence_dates)


def _evidence_precheck(evidence: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not is_primary_authority_tier(evidence.get("authority_tier")):
        reasons.append("SOURCE_NOT_P0_P1")
    if not _canonical_url(evidence.get("evidence_url")):
        reasons.append("MISSING_CITABLE_URL")
    if len(str(evidence.get("evidence_passage") or "").strip()) < 40:
        reasons.append("MISSING_EXACT_PASSAGE")
    if _text(evidence.get("evidence_status")) not in READER_ALLOWED_EVIDENCE_STATUSES:
        reasons.append("EVIDENCE_STATUS_NOT_SUPPORTIVE")
    if _text(evidence.get("observation_status")).casefold() == "deleted":
        reasons.append("SOURCE_REVISION_DELETED")
    if (
        _text(evidence.get("latest_revision_kind")).casefold() == "edit"
        and int(evidence.get("passage_currently_proven") or 0) != 1
    ):
        reasons.append("SOURCE_REVISION_CHANGED")
    if len(_text(evidence.get("content_sha256"))) != 64:
        reasons.append("MISSING_SOURCE_CONTENT_HASH")
    if not _known_at(evidence):
        reasons.append("BOUND_TIMESTAMPS_NOT_EXACT")
    return reasons


def _candidate(
    connection: sqlite3.Connection,
    receipt: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    event_id = _text(receipt.get("event_id"))
    current_version = int(receipt.get("current_version") or 0)
    status = _text(receipt.get("status"))
    label_status = _text(receipt.get("label_status"))
    if status not in {"candidate", "weak"} or label_status not in {"candidate", "weak"}:
        return None, ["FORMAL_OR_TERMINAL_STATUS_EXCLUDED"]
    if status != label_status:
        return None, ["STATUS_LABEL_MISMATCH"]

    subject = _text(receipt.get("company_name") or receipt.get("ticker_at_event"))
    action = _text(receipt.get("event_type"))
    if len(subject) < 2 or len(action) < 3:
        return None, ["MISSING_CANONICAL_SUBJECT_OR_EVENT_TYPE"]

    old_facts = _current_facts(connection, event_id, current_version)
    before = {
        "event_id": event_id,
        "event_version": current_version,
        "status": status,
        "label_status": label_status,
        "event_family": _text(receipt.get("event_family")),
        "event_type": action,
        "manual_grade": receipt.get("manual_grade"),
        "receipt_evidence_fingerprint": receipt.get("evidence_fingerprint"),
        "facts_sha256": sha256_json(old_facts),
        "relations_sha256": sha256_json(
            _current_relation_snapshot(connection, event_id, current_version)
        ),
        "workflow_sha256": sha256_json(
            _current_workflow_snapshot(connection, event_id, current_version)
        ),
    }
    blocked = Counter()
    new_version = current_version + 1
    for evidence in receipt.get("evidence") or []:
        precheck = _evidence_precheck(evidence)
        if precheck:
            blocked.update(precheck)
            continue
        passage = str(evidence.get("evidence_passage") or "")
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type=action,
            expected_subject=subject,
        )
        summary = public_fact_summary(
            subject=subject,
            action_label=action,
            stage_label="DISCLOSED",
            extraction=extraction,
        )
        decision = evaluate_event_admission(
            event_id=event_id,
            event_version=new_version,
            evidence_id=_text(evidence.get("evidence_id")),
            subject=subject,
            action=action,
            stage="DISCLOSED",
            known_at=_known_at(evidence),
            source_authority_tier=_text(evidence.get("authority_tier")),
            evidence_url=_text(evidence.get("evidence_url")),
            evidence_passage=passage,
            evidence_status=_text(evidence.get("evidence_status")),
            content_sha256=_text(evidence.get("content_sha256")),
            subject_match=extraction.supports_specific_fact,
            event_claim_supported=extraction.supports_specific_fact,
            date_coherent=_date_coherent(receipt, evidence),
            fact_extraction=extraction,
            public_fact_summary_text=summary,
        )
        if not decision.admitted:
            blocked.update(decision.reasons)
            continue

        facts = dict(old_facts)
        facts.update(
            {
                "candidate_only": True,
                "public_fact_summary": summary,
                "claim_subject": subject,
                "claim_action": action,
                "claim_stage": "DISCLOSED",
                "claim_fact_slots": extraction.as_dict(),
                "fact_slot_contract_version": FACT_SLOT_CONTRACT_VERSION,
                "fact_slot_receipt_sha256": decision.fact_slot_receipt_sha256,
                "known_at": _known_at(evidence),
                "source_observation_id": _text(evidence.get("observation_id")),
                "source_content_sha256": _text(evidence.get("content_sha256")),
                "evidence_id": _text(evidence.get("evidence_id")),
                "evidence_fingerprint": decision.evidence_fingerprint,
                "admission_contract_version": ADMISSION_CONTRACT_VERSION,
                "formal_verification": False,
                "auto_verification_allowed": False,
                "no_trading": True,
                "historical_primary_readmission": {
                    "contract_version": PLAN_CONTRACT_VERSION,
                    "from_event_version": current_version,
                    "source_facts_sha256": before["facts_sha256"],
                    "status_preserved": True,
                    "human_verification_claimed": False,
                },
            }
        )
        relation = {
            "event_id": event_id,
            "evidence_id": _text(evidence.get("evidence_id")),
            "event_version": new_version,
            "relation_status": "SCOPED_MATCH",
            "subject_match": 1,
            "event_claim_supported": 1,
            "date_coherent": 1,
            "modality": "DISCLOSED",
            "evidence_fingerprint": decision.evidence_fingerprint,
            "contract_version": ADMISSION_CONTRACT_VERSION,
        }
        candidate = {
            "event_id": event_id,
            "before": before,
            "new_event_version": new_version,
            "evidence_id": relation["evidence_id"],
            "facts": facts,
            "facts_sha256": sha256_json(facts),
            "relation": relation,
            "workflow_state": "EVIDENCE_READY",
            "public_fact_summary": summary,
            "no_status_or_label_change": True,
            "no_human_verification_claim": True,
            "no_trading": True,
        }
        candidate["candidate_sha256"] = sha256_json(candidate)
        return candidate, []
    reasons = sorted(blocked) or ["NO_PRIMARY_EVIDENCE"]
    return None, reasons


def build_readmission_plan(ledger_path: Path) -> dict[str, Any]:
    """Build an immutable safe subset from one unchanged logical snapshot."""

    quality_plan = build_recovery_plan(ledger_path)
    eligible_ids = {
        _text(record.get("event_id"))
        for record in quality_plan.get("records") or []
        if record.get("bucket") == "ENRICHABLE_PRIMARY"
    }
    records: list[dict[str, Any]] = []
    blocked = Counter()
    scanned_types = Counter()
    with closing(_read_only_connection(ledger_path)) as connection:
        connection.execute("BEGIN")
        if _database_logical_sha256(connection) != quality_plan[
            "source_database_logical_sha256"
        ]:
            connection.rollback()
            raise ValueError("ledger changed while readmission plan was being built; retry")
        for event_id in sorted(eligible_ids):
            receipt = event_receipt(connection, event_id)
            scanned_types.update([_text(receipt.get("event_type"))])
            candidate, reasons = _candidate(connection, receipt)
            if candidate is not None:
                records.append(candidate)
            else:
                blocked.update(reasons)
        connection.rollback()

    by_type = Counter(record["before"]["event_type"] for record in records)
    plan = {
        "schema_version": 1,
        "contract_version": PLAN_CONTRACT_VERSION,
        "generated_at": utc_now(),
        "source_database_logical_sha256": quality_plan[
            "source_database_logical_sha256"
        ],
        "source_quality_plan_sha256": quality_plan["plan_sha256"],
        "target_ledger_resolved_path": quality_plan["target_ledger_resolved_path"],
        "target_ledger_identity": quality_plan["target_ledger_identity"],
        "target_ledger_identity_sha256": quality_plan[
            "target_ledger_identity_sha256"
        ],
        "enrichable_primary_scanned": len(eligible_ids),
        "candidate_count": len(records),
        "candidate_counts_by_event_type": dict(sorted(by_type.items())),
        "scanned_counts_by_event_type": dict(sorted(scanned_types.items())),
        "blocked_reason_counts": dict(sorted(blocked.items())),
        "read_only": True,
        "preserves_status_and_label": True,
        "claims_human_verification": False,
        "no_trading": True,
        "records": records,
    }
    plan["plan_sha256"] = sha256_json(plan)
    return plan


def validate_readmission_plan(plan: dict[str, Any]) -> str:
    claimed = _text(plan.get("plan_sha256"))
    payload = dict(plan)
    payload.pop("plan_sha256", None)
    if claimed != sha256_json(payload):
        raise ValueError("plan_sha256 does not match readmission plan")
    if plan.get("contract_version") != PLAN_CONTRACT_VERSION:
        raise ValueError("unsupported readmission plan contract")
    if int(plan.get("schema_version") or 0) != 1:
        raise ValueError("unsupported readmission plan schema")
    records = list(plan.get("records") or [])
    if int(plan.get("candidate_count", -1)) != len(records):
        raise ValueError("candidate_count does not match records")
    seen: set[str] = set()
    for record in records:
        event_id = _text(record.get("event_id"))
        if not event_id or event_id in seen:
            raise ValueError("missing or duplicate event_id in readmission plan")
        seen.add(event_id)
        candidate_payload = dict(record)
        candidate_sha = _text(candidate_payload.pop("candidate_sha256", ""))
        if candidate_sha != sha256_json(candidate_payload):
            raise ValueError(f"candidate_sha256 mismatch for {event_id}")
        if not all(
            record.get(field) is True
            for field in (
                "no_status_or_label_change",
                "no_human_verification_claim",
                "no_trading",
            )
        ):
            raise ValueError(f"readmission safety flags missing for {event_id}")
    expected_by_type = Counter(record["before"]["event_type"] for record in records)
    if plan.get("candidate_counts_by_event_type") != dict(sorted(expected_by_type.items())):
        raise ValueError("candidate event type counts do not match records")
    if len(_text(plan.get("source_database_logical_sha256"))) != 64:
        raise ValueError("source database logical hash is invalid")
    identity = plan.get("target_ledger_identity")
    if not isinstance(identity, dict) or sha256_json(identity) != _text(
        plan.get("target_ledger_identity_sha256")
    ):
        raise ValueError("target ledger identity is invalid")
    return claimed


def readmission_scope(plan: dict[str, Any]) -> list[dict[str, Any]]:
    validate_readmission_plan(plan)
    return [
        {
            "event_id": record["event_id"],
            "event_version_before": record["before"]["event_version"],
            "event_version_after": record["new_event_version"],
            "status": record["before"]["status"],
            "label_status": record["before"]["label_status"],
            "evidence_id": record["evidence_id"],
            "candidate_sha256": record["candidate_sha256"],
        }
        for record in plan.get("records") or []
    ]


def build_readmission_authorization_template(plan: dict[str, Any]) -> dict[str, Any]:
    scope = readmission_scope(plan)
    return {
        "schema_version": 1,
        "action": APPLY_ACTION,
        "approved": False,
        "authorization_id": "FILL_ME",
        "actor": "FILL_ME",
        "purpose": "FILL_ME",
        "expires_at": "FILL_ME_WITH_TIMEZONE",
        "plan_sha256": plan["plan_sha256"],
        "source_database_logical_sha256": plan["source_database_logical_sha256"],
        "target_ledger_resolved_path": plan["target_ledger_resolved_path"],
        "target_ledger_identity_sha256": plan["target_ledger_identity_sha256"],
        "max_event_count": len(scope),
        "scope": scope,
        "scope_sha256": sha256_json(scope),
        "backup_path": "FILL_ME",
        "backup_sha256": "FILL_ME",
        "allow_new_event_version": True,
        "preserve_status_and_label": True,
        "claim_human_verification": False,
        "no_trading": True,
    }


def _validate_authorization(
    ledger_path: Path,
    plan: dict[str, Any],
    authorization: dict[str, Any],
) -> dict[str, Any]:
    validate_readmission_plan(plan)
    if authorization.get("approved") is not True:
        raise ValueError("authorization approved must be true")
    if _text(authorization.get("action")) != APPLY_ACTION:
        raise ValueError("authorization action is invalid")
    for field in ("authorization_id", "actor", "purpose"):
        value = _text(authorization.get(field))
        if not value or value == "FILL_ME":
            raise ValueError(f"authorization {field} is required")
    expiry = _aware_timestamp(authorization.get("expires_at"))
    if expiry is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("authorization expires_at must be a future aware timestamp")
    for field in (
        "plan_sha256",
        "source_database_logical_sha256",
        "target_ledger_resolved_path",
        "target_ledger_identity_sha256",
    ):
        if _text(authorization.get(field)) != _text(plan.get(field)):
            raise ValueError(f"authorization {field} does not match plan")
    scope = readmission_scope(plan)
    if authorization.get("scope") != scope:
        raise ValueError("authorization scope does not exactly match plan")
    if _text(authorization.get("scope_sha256")) != sha256_json(scope):
        raise ValueError("authorization scope_sha256 is invalid")
    if int(authorization.get("max_event_count") or -1) != len(scope):
        raise ValueError("authorization max_event_count is invalid")
    for field in ("allow_new_event_version", "preserve_status_and_label", "no_trading"):
        if authorization.get(field) is not True:
            raise ValueError(f"authorization must explicitly preserve {field}")
    if authorization.get("claim_human_verification") is not False:
        raise ValueError("authorization may not claim human verification")
    current_identity = _target_ledger_identity(
        ledger_path,
        source_database_logical_sha256=plan["source_database_logical_sha256"],
    )
    if current_identity != plan["target_ledger_identity"]:
        raise ValueError("target ledger identity does not match plan")
    backup_path = Path(_text(authorization.get("backup_path")))
    if not backup_path.is_file():
        raise ValueError("authorization backup_path is not a file")
    claimed_backup_sha = _text(authorization.get("backup_sha256")).lower()
    if len(claimed_backup_sha) != 64 or sha256_file(backup_path) != claimed_backup_sha:
        raise ValueError("authorization backup_sha256 does not match backup")
    if backup_path.resolve() == ledger_path.resolve() or backup_path.samefile(ledger_path):
        raise ValueError("backup must be an independent file")
    with closing(_read_only_connection(backup_path)) as backup:
        _require_apply_schema(backup)
        if _text(backup.execute("PRAGMA quick_check").fetchone()[0]).casefold() != "ok":
            raise ValueError("backup quick_check failed")
        if _text(backup.execute("PRAGMA integrity_check").fetchone()[0]).casefold() != "ok":
            raise ValueError("backup integrity_check failed")
        if _database_logical_sha256(backup) != plan["source_database_logical_sha256"]:
            raise ValueError("backup logical snapshot does not match plan")
    return authorization


def _ensure_audit_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS historical_primary_readmission_audit (
            audit_id TEXT PRIMARY KEY,
            contract_version TEXT NOT NULL,
            authorization_id TEXT NOT NULL,
            authorization_sha256 TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,
            scope_sha256 TEXT NOT NULL,
            result_sha256 TEXT NOT NULL,
            result_json TEXT NOT NULL,
            committed_at TEXT NOT NULL,
            event_version_mutation INTEGER NOT NULL CHECK(event_version_mutation=1),
            status_or_label_mutation INTEGER NOT NULL CHECK(status_or_label_mutation=0),
            human_verification_claimed INTEGER NOT NULL CHECK(human_verification_claimed=0),
            no_trading INTEGER NOT NULL CHECK(no_trading=1)
        )
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS historical_primary_readmission_audit_no_update
        BEFORE UPDATE ON historical_primary_readmission_audit
        BEGIN
            SELECT RAISE(ABORT,'historical_primary_readmission_audit is append-only');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS historical_primary_readmission_audit_no_delete
        BEFORE DELETE ON historical_primary_readmission_audit
        BEGIN
            SELECT RAISE(ABORT,'historical_primary_readmission_audit is append-only');
        END
        """
    )


def apply_readmission_plan(
    ledger_path: Path,
    plan: dict[str, Any],
    authorization: dict[str, Any] | None = None,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    """Dry-run or atomically apply the exact authorized candidate set."""

    validate_readmission_plan(plan)
    records = list(plan.get("records") or [])
    if not execute:
        result = {
            "contract_version": APPLY_CONTRACT_VERSION,
            "mode": "DRY_RUN",
            "plan_sha256": plan["plan_sha256"],
            "ready_to_apply": len(records),
            "applied": 0,
            "event_version_mutation": False,
            "status_or_label_mutation": False,
            "human_verification_claimed": False,
            "no_trading": True,
        }
        result["result_sha256"] = sha256_json(result)
        return result
    if not records:
        raise ValueError("readmission plan contains no eligible events")
    auth = _validate_authorization(ledger_path, plan, authorization or {})

    connection = sqlite3.connect(ledger_path)
    connection.row_factory = sqlite3.Row
    result: dict[str, Any] | None = None
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        _require_apply_schema(connection)
        if _database_logical_sha256(connection) != plan["source_database_logical_sha256"]:
            raise ValueError("STALE_READMISSION_PLAN: database logical snapshot changed")
        prepared: list[dict[str, Any]] = []
        for expected in records:
            receipt = event_receipt(connection, expected["event_id"])
            candidate, reasons = _candidate(connection, receipt)
            if candidate != expected:
                raise ValueError(
                    f"STALE_READMISSION_PLAN:{expected['event_id']}:"
                    + ",".join(reasons or ["CANDIDATE_CHANGED"])
                )
            prepared.append(candidate)

        now = utc_now()
        assessed_by = (
            f"authorized_historical_primary_readmission:"
            f"{_text(auth['authorization_id'])}:{_text(auth['actor'])}"
        )
        changes: list[dict[str, Any]] = []
        _ensure_audit_schema(connection)
        for candidate in prepared:
            before = candidate["before"]
            cursor = connection.execute(
                """UPDATE canonical_events
                   SET current_version=?,last_updated_at=?
                   WHERE event_id=? AND current_version=? AND status=? AND label_status=?""",
                (
                    candidate["new_event_version"],
                    now,
                    candidate["event_id"],
                    before["event_version"],
                    before["status"],
                    before["label_status"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"STALE_CAS:{candidate['event_id']}")
            connection.execute(
                """INSERT INTO event_versions(
                       event_id,version,changed_at,status,label_status,event_family,event_type,
                       manual_grade,facts_json,change_reason
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    candidate["event_id"],
                    candidate["new_event_version"],
                    now,
                    before["status"],
                    before["label_status"],
                    before["event_family"],
                    before["event_type"],
                    before["manual_grade"],
                    stable_json(candidate["facts"]),
                    CHANGE_REASON,
                ),
            )
            relation = candidate["relation"]
            connection.execute(
                """INSERT INTO event_evidence_relations(
                       event_id,evidence_id,event_version,relation_status,subject_match,
                       event_claim_supported,date_coherent,modality,evidence_fingerprint,
                       contract_version,assessed_by,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    relation["event_id"],
                    relation["evidence_id"],
                    relation["event_version"],
                    relation["relation_status"],
                    relation["subject_match"],
                    relation["event_claim_supported"],
                    relation["date_coherent"],
                    relation["modality"],
                    relation["evidence_fingerprint"],
                    relation["contract_version"],
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
                    candidate["new_event_version"],
                    candidate["workflow_state"],
                    stable_json(["AUTHORIZED_PRIMARY_SOURCE_FACT_REPLAY"]),
                    relation["evidence_fingerprint"],
                    relation["contract_version"],
                    now,
                ),
            )
            changes.append(
                {
                    "event_id": candidate["event_id"],
                    "event_version_before": before["event_version"],
                    "event_version_after": candidate["new_event_version"],
                    "status": before["status"],
                    "label_status": before["label_status"],
                    "evidence_id": candidate["evidence_id"],
                    "candidate_sha256": candidate["candidate_sha256"],
                }
            )

        audit_id = "HPR-AUDIT-" + hashlib.sha256(
            f"{auth['authorization_id']}:{plan['plan_sha256']}".encode("utf-8")
        ).hexdigest()[:32]
        result = {
            "contract_version": APPLY_CONTRACT_VERSION,
            "mode": "APPLIED",
            "plan_sha256": plan["plan_sha256"],
            "authorization_id": auth["authorization_id"],
            "applied_at": now,
            "applied": len(changes),
            "changes": changes,
            "durable_audit_id": audit_id,
            "event_version_mutation": True,
            "status_or_label_mutation": False,
            "human_verification_claimed": False,
            "no_trading": True,
        }
        result["result_sha256"] = sha256_json(result)
        connection.execute(
            """INSERT INTO historical_primary_readmission_audit(
                   audit_id,contract_version,authorization_id,authorization_sha256,
                   plan_sha256,scope_sha256,result_sha256,result_json,committed_at,
                   event_version_mutation,status_or_label_mutation,
                   human_verification_claimed,no_trading
               ) VALUES (?,?,?,?,?,?,?,?,?,1,0,0,1)""",
            (
                audit_id,
                AUDIT_CONTRACT_VERSION,
                auth["authorization_id"],
                sha256_json(auth),
                plan["plan_sha256"],
                auth["scope_sha256"],
                result["result_sha256"],
                stable_json(result),
                now,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if result is None:  # pragma: no cover
        raise RuntimeError("readmission apply completed without a result")
    return result
