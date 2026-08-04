#!/usr/bin/env python3
"""Close user-authorized rough-review jobs without claiming formal verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from event_ledger import open_ledger, stable_json, utc_now


ROOT = Path(__file__).resolve().parents[1]
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


def load_latest_decisions(path: Path) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        latest: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            """SELECT decision_id,event_id,status,trace_id,created_at
               FROM agent_decisions ORDER BY event_id,created_at DESC,decision_id DESC"""
        ):
            latest.setdefault(str(row["event_id"]), dict(row))
        return latest
    finally:
        connection.close()


def build_rows(connection: Any, decisions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job in connection.execute(
        """SELECT j.job_id,j.event_id,j.status,j.payload_json,e.discovery_source,e.event_family
           FROM pipeline_jobs j
           JOIN canonical_events e ON e.event_id=j.event_id
           WHERE j.job_type='live_primary_evidence_review'
             AND j.status='PENDING_HUMAN_REVIEW'
           ORDER BY j.event_id"""
    ):
        decision = decisions.get(str(job["event_id"]))
        if decision is None:
            raise RuntimeError(f"missing agent decision for {job['event_id']}")
        decision_status = str(decision["status"])
        rows.append(
            {
                "job_id": str(job["job_id"]),
                "event_id": str(job["event_id"]),
                "before_status": str(job["status"]),
                "payload_json": str(job["payload_json"] or "{}"),
                "discovery_source": str(job["discovery_source"] or ""),
                "event_family": str(job["event_family"] or ""),
                "decision_id": str(decision["decision_id"]),
                "decision_trace_id": str(decision["trace_id"]),
                "decision_status": decision_status,
                "outcome": OUTCOME_BY_DECISION.get(decision_status, "ROUGH_UNRESOLVED"),
            }
        )
    return rows


def manifest_sha256(rows: list[dict[str, Any]]) -> str:
    manifest = [
        {
            "event_id": row["event_id"],
            "decision_id": row["decision_id"],
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
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, TypeError):
                payload = {"previous_payload_raw": row["payload_json"]}
            if not isinstance(payload, dict):
                payload = {"previous_payload": payload}
            payload["rough_review"] = {
                "batch_id": batch_id,
                "reviewed_at": reviewed_at,
                "reviewer_mode": "codex_machine_rough_review",
                "authorization": AUTHORIZATION_PHRASE,
                "decision_id": row["decision_id"],
                "decision_trace_id": row["decision_trace_id"],
                "decision_status": row["decision_status"],
                "outcome": row["outcome"],
                "formal_verification": False,
                "canonical_event_label_changed": False,
            }
            cursor = connection.execute(
                """UPDATE pipeline_jobs
                   SET status=?,last_error=NULL,payload_json=?,updated_at=?
                   WHERE job_id=? AND job_type='live_primary_evidence_review'
                     AND status='PENDING_HUMAN_REVIEW'""",
                (COMPLETED_STATUS, stable_json(payload), reviewed_at, row["job_id"]),
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
    connection = open_ledger(db)
    try:
        rows = build_rows(connection, load_latest_decisions(operations_db))
        reviewed_at = utc_now()
        resolved_batch_id = batch_id or f"rough-review-{uuid.uuid4().hex}"
        result: dict[str, Any] = {
            "batch_id": resolved_batch_id,
            "reviewed_at": reviewed_at,
            "mode": "APPLY" if apply else "DRY_RUN",
            "selected": len(rows),
            "updated": 0,
            "outcomes": dict(Counter(row["outcome"] for row in rows)),
            "decision_statuses": dict(Counter(row["decision_status"] for row in rows)),
            "manifest_sha256": manifest_sha256(rows),
            "formal_verification": False,
            "canonical_event_labels_changed": 0,
            "no_trading": True,
        }
        if apply:
            if authorization != AUTHORIZATION_PHRASE:
                raise ValueError(
                    f"--authorization must equal {AUTHORIZATION_PHRASE!r} for --apply"
                )
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
