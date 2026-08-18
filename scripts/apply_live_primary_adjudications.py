#!/usr/bin/env python3
"""Audit retired legacy review config without mutating canonical event truth."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from event_ledger import stable_json


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_CONFIG = ROOT / "config" / "live_primary_adjudications.json"
DEFAULT_REPORT = ROOT / "reports" / "live_primary_adjudications_latest.md"


def heuristic_grade(total: int) -> str:
    if total >= 11:
        return "S"
    if total >= 8:
        return "A++"
    if total >= 5:
        return "A"
    if total >= 2:
        return "B"
    return "C"


def validate_scores(row: dict[str, Any]) -> tuple[dict[str, int], int]:
    scores = row.get("scores")
    if not isinstance(scores, dict) or set(scores) != {"R", "L", "E", "C", "P", "X"}:
        raise ValueError(f"{row['event_id']} requires exact R/L/E/C/P/X scores")
    normalized = {key: int(value) for key, value in scores.items()}
    for key in ("R", "L", "E", "C", "P"):
        if not 0 <= normalized[key] <= 3:
            raise ValueError(f"{row['event_id']} {key} score is out of range")
    if not -3 <= normalized["X"] <= 0:
        raise ValueError(f"{row['event_id']} X score is out of range")
    total = sum(normalized.values())
    if row.get("manual_grade") not in {"S", "A++", "A", "B", "C"}:
        raise ValueError(
            f"{row['event_id']} requires a valid manual grade"
        )
    # The score-derived grade is review priority only.  D:/short explicitly keeps
    # manual/rule conflicts for boundary review and never lets the rule overwrite
    # a reviewed grade.
    return normalized, total


REQUIRED_PROVENANCE_FIELDS = {
    "authorization_id",
    "evidence_fingerprint",
    "event_version",
    "reviewed_at",
    "reviewer_id",
    "source_sha256",
}
LEGACY_STATUS = "LEGACY_REVIEW_CONFIG_UNPROVEN_PROVENANCE"


def open_audit_ledger(path: Path) -> sqlite3.Connection:
    """Open the retired-config audit input without schema or WAL side effects."""

    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _row_provenance_issues(row: dict[str, Any], event: Any) -> list[str]:
    issues = [
        f"missing_{field}"
        for field in sorted(REQUIRED_PROVENANCE_FIELDS)
        if not row.get(field)
    ]
    if event is None:
        return [*issues, "unknown_event_id"]
    if row.get("event_version") is not None:
        try:
            if int(row["event_version"]) != int(event["current_version"]):
                issues.append("event_version_mismatch")
        except (TypeError, ValueError):
            issues.append("event_version_invalid")
    claimed_sha256 = str(row.get("source_sha256") or "").lower()
    if claimed_sha256:
        payload = dict(row)
        payload.pop("source_sha256", None)
        computed = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
        if claimed_sha256 != computed:
            issues.append("source_sha256_mismatch")
    return issues


def audit_rows(connection: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify legacy rows; never update ledger, evidence, jobs or assessments."""

    result: dict[str, Any] = {
        "requested": len(rows),
        "audited": 0,
        "unproven": 0,
        "status": LEGACY_STATUS,
        "canonical_mutation_allowed": False,
        "formal_mutation_attempted": False,
        "rows": [],
    }
    for row in rows:
        scores, score_total = validate_scores(row)
        event = connection.execute(
            "SELECT * FROM canonical_events WHERE event_id=?", (row["event_id"],)
        ).fetchone()
        issues = _row_provenance_issues(row, event)
        if str(row.get("status") or "") != "verified":
            issues.append("legacy_status_not_verified")
        result["audited"] += 1
        if issues:
            result["unproven"] += 1
        result["rows"].append(
            {
                "event_id": row.get("event_id"),
                "config_sha256": hashlib.sha256(
                    stable_json(row).encode("utf-8")
                ).hexdigest(),
                "configured_score_total": score_total,
                "configured_scores": scores,
                "canonical_status": event["status"] if event is not None else None,
                "canonical_version": int(event["current_version"]) if event is not None else None,
                "provenance_status": "PROVEN" if not issues else LEGACY_STATUS,
                "issues": issues,
            }
        )
    return result


def apply_rows(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise PermissionError(
        "legacy config-to-canonical mutation is retired; use scoped light formalization "
        "or authenticated independent human adjudication"
    )


def write_report(path: Path, rows: list[dict[str, Any]], result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Legacy Live Primary Adjudication Audit",
        "",
        f"- Requested: `{result['requested']}`",
        f"- Audited: `{result['audited']}`",
        f"- Unproven provenance: `{result['unproven']}`",
        "- Canonical mutation allowed: `false`",
        "- Formal mutation attempted: `false`",
        "- Status: `LEGACY_REVIEW_CONFIG_UNPROVEN_PROVENANCE`",
        "",
        "These rows are historical review hints only. They are not authentic-human labels,",
        "training truth, or authority to change canonical event state.",
        "",
    ]
    by_event = {str(item["event_id"]): item for item in result["rows"]}
    for row in rows:
        audit = by_event[str(row["event_id"])]
        lines.extend(
            [
                f"### {row['company_name']} — {row['event_type']}",
                "",
                f"- Event: `{row['event_id']}`",
                f"- Canonical status/version: `{audit['canonical_status']} / {audit['canonical_version']}`",
                f"- Config SHA-256: `{audit['config_sha256']}`",
                f"- Provenance status: `{audit['provenance_status']}`",
                f"- Issues: `{', '.join(audit['issues']) or 'none'}`",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    payload = json.loads(args.config.read_text(encoding="utf-8"))
    rows = payload.get("adjudications")
    if not isinstance(rows, list):
        raise ValueError("Config requires an adjudications list")
    connection = open_audit_ledger(args.db)
    try:
        result = audit_rows(connection, rows)
    finally:
        connection.close()
    write_report(args.report, rows, result)
    print(stable_json(result))
    print(f"REPORT={args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
