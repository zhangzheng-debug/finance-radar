#!/usr/bin/env python3
"""Audit historical fact decisions against the current integrity contracts.

The audit is deliberately read-only.  It may classify an old decision as stale
or in need of review, but it never changes canonical event state, evidence, or
operations history.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.services.evidence_agent import (  # noqa: E402
    PROMPT_VERSION,
    _claim_evidence_relevance,
)
from app.services.light_verification import (  # noqa: E402
    LIGHT_VERIFICATION_VERSION,
    evaluate_event,
    evidence_receipt_rows,
)
from scripts.apply_live_primary_adjudications import (  # noqa: E402
    LEGACY_STATUS,
    audit_rows as audit_legacy_config_rows,
)
from scripts.event_ledger import stable_json, utc_now  # noqa: E402


DEFAULT_LEDGER = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_OPERATIONS = ROOT / "data" / "finance_radar_operations.sqlite3"
DEFAULT_LEGACY_CONFIG = ROOT / "config" / "live_primary_adjudications.json"
DEFAULT_JSON_REPORT = ROOT / "reports" / "fact_integrity_history_audit_latest.json"
DEFAULT_MARKDOWN_REPORT = ROOT / "reports" / "fact_integrity_history_audit_latest.md"
AGENT_CURRENT = "CURRENT_CONTRACT"
AGENT_STALE = "STALE_CONTRACT_REQUIRES_RERUN"
AGENT_REJECTED = "EDGE_REJECTED_BY_CURRENT_RELEVANCE_GATE"
LIGHT_CURRENT = "PASSES_CURRENT_GATE_DRY_RUN"
LIGHT_REVIEW = "CURRENT_GATE_REQUIRES_REVIEW"
LIGHT_EVOLVED = "EVENT_EVOLVED_AFTER_FORMALIZATION"


def open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _safe_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, json.JSONDecodeError):
        return default


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _event_row(
    ledger: sqlite3.Connection,
    event_id: str,
) -> dict[str, Any] | None:
    row = ledger.execute(
        "SELECT * FROM canonical_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def audit_agent_decisions(
    ledger: sqlite3.Connection,
    operations: sqlite3.Connection,
) -> dict[str, Any]:
    """Re-score stored edges without writing evidence objects or decisions."""

    if not _table_exists(operations, "agent_decisions"):
        return {
            "status": "NOT_AVAILABLE",
            "current_contract": PROMPT_VERSION,
            "total": 0,
            "counts": {},
            "decisions": [],
        }

    rows = operations.execute(
        """SELECT decision_id,event_id,status,prompt_version,output_json,created_at
           FROM agent_decisions ORDER BY created_at,decision_id"""
    ).fetchall()
    decisions: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for row in rows:
        event_id = str(row["event_id"])
        event = _event_row(ledger, event_id)
        output = _safe_json(row["output_json"], {})
        claims = {
            str(item.get("claim_id") or ""): item
            for item in output.get("claims", [])
            if isinstance(item, dict) and item.get("claim_id")
        }
        rejected_edges: list[dict[str, Any]] = []
        stored_edges = [
            item
            for item in output.get("evidence_edges", [])
            if isinstance(item, dict)
        ]
        if event is not None:
            for edge in stored_edges:
                claim = claims.get(str(edge.get("claim_id") or ""))
                excerpt = str(edge.get("exact_excerpt") or "").strip()
                if claim is None or not excerpt:
                    rejected_edges.append(
                        {
                            "evidence_id": str(edge.get("evidence_id") or ""),
                            "reason": "MISSING_STORED_CLAIM_OR_EXCERPT",
                        }
                    )
                    continue
                eligible, relevance = _claim_evidence_relevance(event, claim, excerpt)
                if not eligible:
                    rejected_edges.append(
                        {
                            "evidence_id": str(edge.get("evidence_id") or ""),
                            "reason": str(relevance.get("reason") or "NOT_RELEVANT"),
                        }
                    )

        prompt_current = str(row["prompt_version"]) == PROMPT_VERSION
        if event is None:
            classification = "EVENT_NOT_FOUND"
        elif rejected_edges:
            classification = AGENT_REJECTED
        elif not prompt_current:
            classification = AGENT_STALE
        else:
            classification = AGENT_CURRENT
        counts[classification] += 1
        decisions.append(
            {
                "decision_id": str(row["decision_id"]),
                "event_id": event_id,
                "created_at": str(row["created_at"]),
                "stored_status": str(row["status"]),
                "stored_prompt_version": str(row["prompt_version"]),
                "classification": classification,
                "stored_edge_count": len(stored_edges),
                "rejected_edge_count": len(rejected_edges),
                "rejected_edges": rejected_edges,
                "canonical_version_now": (
                    int(event["current_version"]) if event is not None else None
                ),
                "canonical_mutation_attempted": False,
            }
        )
    return {
        "status": "OK",
        "current_contract": PROMPT_VERSION,
        "total": len(decisions),
        "counts": dict(sorted(counts.items())),
        "decisions": decisions,
    }


def _sanitized_checks(result: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for item in result.get("checks", []):
        if not isinstance(item, dict):
            continue
        checks.append(
            {
                "evidence_id": str(item.get("evidence_id") or ""),
                "identity_match": bool(item.get("identity_match")),
                "event_signal": bool(item.get("event_signal")),
                "subject_event_bound": bool(item.get("subject_event_bound")),
                "modality_safe": bool(item.get("modality_safe")),
                "date_coherent": bool(item.get("date_coherent")),
                "automatic_formal_eligible": bool(
                    item.get("automatic_formal_eligible")
                ),
            }
        )
    return checks


def audit_light_verifications(ledger: sqlite3.Connection) -> dict[str, Any]:
    """Dry-run old formalizations against the current subject-binding gate."""

    if not _table_exists(ledger, "event_versions"):
        return {
            "status": "NOT_AVAILABLE",
            "current_contract": LIGHT_VERIFICATION_VERSION,
            "total": 0,
            "counts": {},
            "records": [],
        }
    rows = ledger.execute(
        """SELECT event_id,version,changed_at,status,event_family,event_type,
                  facts_json,change_reason
           FROM event_versions
           WHERE change_reason LIKE 'light_evidence_verification_v%'
           ORDER BY changed_at,event_id,version"""
    ).fetchall()
    records: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for row in rows:
        event_id = str(row["event_id"])
        event = _event_row(ledger, event_id)
        facts = _safe_json(row["facts_json"], {})
        light = facts.get("light_verification") if isinstance(facts, dict) else {}
        light = light if isinstance(light, dict) else {}
        stored_contract = str(light.get("version") or row["change_reason"])
        if event is None:
            classification = "EVENT_NOT_FOUND"
            decision = "NOT_EVALUATED"
            checks: list[dict[str, Any]] = []
            current_version = None
        else:
            historical_input = {
                **event,
                "current_version": max(0, int(row["version"]) - 1),
                "status": "candidate",
                "label_status": "candidate",
                "event_family": str(row["event_family"]),
                "event_type": str(row["event_type"]),
                "facts": facts,
            }
            evidence = evidence_receipt_rows(ledger, event_id)
            evaluation = evaluate_event(historical_input, evidence)
            decision = str(evaluation.get("decision") or "UNKNOWN")
            checks = _sanitized_checks(evaluation)
            current_version = int(event["current_version"])
            if current_version != int(row["version"]):
                classification = LIGHT_EVOLVED
            elif decision == "SUPPORTED" and stored_contract == LIGHT_VERIFICATION_VERSION:
                classification = LIGHT_CURRENT
            else:
                classification = LIGHT_REVIEW
        counts[classification] += 1
        records.append(
            {
                "event_id": event_id,
                "formalized_version": int(row["version"]),
                "canonical_version_now": current_version,
                "formalized_at": str(row["changed_at"]),
                "stored_contract": stored_contract,
                "current_contract": LIGHT_VERIFICATION_VERSION,
                "current_dry_run_decision": decision,
                "classification": classification,
                "checks": checks,
                "canonical_mutation_attempted": False,
            }
        )
    return {
        "status": "OK",
        "current_contract": LIGHT_VERIFICATION_VERSION,
        "total": len(records),
        "counts": dict(sorted(counts.items())),
        "records": records,
    }


def audit_legacy_config(
    ledger: sqlite3.Connection,
    config_path: Path,
) -> dict[str, Any]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    rows = payload.get("adjudications")
    if not isinstance(rows, list):
        raise ValueError("legacy config requires an adjudications list")
    result = audit_legacy_config_rows(ledger, rows)
    return {
        "status": LEGACY_STATUS,
        "total": int(result["audited"]),
        "unproven": int(result["unproven"]),
        "canonical_mutation_allowed": False,
        "formal_mutation_attempted": False,
        "rows": result["rows"],
    }


def build_report(
    ledger: sqlite3.Connection,
    operations: sqlite3.Connection,
    legacy_config: Path,
) -> dict[str, Any]:
    report = {
        "contract": "fact-integrity-history-audit-v1",
        "generated_at": utc_now(),
        "read_only": True,
        "canonical_mutation_attempted": False,
        "agent_decisions": audit_agent_decisions(ledger, operations),
        "light_verifications": audit_light_verifications(ledger),
        "legacy_review_config": audit_legacy_config(ledger, legacy_config),
    }
    report["report_sha256"] = hashlib.sha256(
        stable_json(report).encode("utf-8")
    ).hexdigest()
    return report


def _count(section: dict[str, Any], key: str) -> int:
    return int((section.get("counts") or {}).get(key, 0))


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    agent = report["agent_decisions"]
    light = report["light_verifications"]
    legacy = report["legacy_review_config"]
    affected_agent_ids = [
        row["event_id"]
        for row in agent.get("decisions", [])
        if row.get("classification") != AGENT_CURRENT
    ]
    affected_light_ids = [
        row["event_id"]
        for row in light.get("records", [])
        if row.get("classification") != LIGHT_CURRENT
    ]
    lines = [
        "# Fact Integrity History Audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Contract: `{report['contract']}`",
        "- Read only: `true`",
        "- Canonical mutation attempted: `false`",
        f"- Report SHA-256: `{report['report_sha256']}`",
        "",
        "## Evidence Agent decisions",
        "",
        f"- Total stored decisions: `{agent['total']}`",
        f"- Current contract: `{_count(agent, AGENT_CURRENT)}`",
        f"- Old contract requires rerun: `{_count(agent, AGENT_STALE)}`",
        f"- Stored edge rejected by current relevance gate: `{_count(agent, AGENT_REJECTED)}`",
        "",
        "All non-current decisions are advisory history only. The live cycle must rerun them",
        "under the current receipt and prompt contract before displaying a current conclusion.",
        "",
        "## Formal light-verification history",
        "",
        f"- Total formalized versions: `{light['total']}`",
        f"- Pass current gate in dry run: `{_count(light, LIGHT_CURRENT)}`",
        f"- Current gate requires review: `{_count(light, LIGHT_REVIEW)}`",
        f"- Event evolved after formalization: `{_count(light, LIGHT_EVOLVED)}`",
        "",
        "This section does not roll back a historical version. A scoped human/operator",
        "decision is required before any canonical correction.",
        "",
        "## Retired legacy review config",
        "",
        f"- Rows audited: `{legacy['total']}`",
        f"- Rows with unproven provenance: `{legacy['unproven']}`",
        "- Allowed to mutate canonical truth: `false`",
        "- Eligible as authentic-human labels or training truth: `false`",
        "",
        "## Affected event manifests",
        "",
        f"- Agent decisions requiring rerun/review: `{len(affected_agent_ids)}`",
        f"- Light formalizations requiring review/evolution check: `{len(affected_light_ids)}`",
        "",
        "The adjacent JSON report contains every decision/event identifier and sanitized gate",
        "result. It intentionally excludes evidence passage text and credentials.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--operations", type=Path, default=DEFAULT_OPERATIONS)
    parser.add_argument("--legacy-config", type=Path, default=DEFAULT_LEGACY_CONFIG)
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument("--markdown-report", type=Path, default=DEFAULT_MARKDOWN_REPORT)
    args = parser.parse_args()

    with closing(open_read_only(args.ledger)) as ledger, closing(
        open_read_only(args.operations)
    ) as operations:
        report = build_report(ledger, operations, args.legacy_config)
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(stable_json(report) + "\n", encoding="utf-8")
    write_markdown(args.markdown_report, report)
    print(
        stable_json(
            {
                "report_sha256": report["report_sha256"],
                "agent_counts": report["agent_decisions"]["counts"],
                "light_counts": report["light_verifications"]["counts"],
                "legacy_unproven": report["legacy_review_config"]["unproven"],
                "canonical_mutation_attempted": False,
                "json_report": str(args.json_report),
                "markdown_report": str(args.markdown_report),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
