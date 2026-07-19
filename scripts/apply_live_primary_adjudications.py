#!/usr/bin/env python3
"""Apply reviewed P0/P1 evidence to live candidates without automatic verification."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from event_ledger import (
    link_event_chain_member,
    open_ledger,
    record_source_observation,
    stable_id,
    stable_json,
    upsert_event_chain,
    upsert_source,
    utc_now,
)


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


def apply_rows(connection: Any, rows: list[dict[str, Any]]) -> dict[str, int]:
    result = {"requested": len(rows), "applied": 0, "already_applied": 0}
    for row in rows:
        scores, score_total = validate_scores(row)
        event = connection.execute(
            "SELECT * FROM canonical_events WHERE event_id=?", (row["event_id"],)
        ).fetchone()
        if event is None:
            raise ValueError(f"Unknown event_id: {row['event_id']}")
        if row["status"] != "verified":
            raise ValueError("This importer only accepts manually reviewed verified rows")
        chain = row.get("event_chain")
        if chain is not None:
            required = {
                "chain_id",
                "chain_type",
                "canonical_key",
                "chain_role",
                "counts_as_primary_event",
                "rationale",
            }
            if not isinstance(chain, dict) or not required.issubset(chain):
                raise ValueError(f"{row['event_id']} has an invalid event_chain object")
            upsert_event_chain(
                connection,
                chain_id=str(chain["chain_id"]),
                chain_type=str(chain["chain_type"]),
                canonical_key=str(chain["canonical_key"]),
            )
            link_event_chain_member(
                connection,
                chain_id=str(chain["chain_id"]),
                event_id=row["event_id"],
                chain_role=str(chain["chain_role"]),
                counts_as_primary_event=bool(chain["counts_as_primary_event"]),
                rationale=str(chain["rationale"]),
            )
        upsert_source(
            connection,
            source_id=row["source_id"],
            name=row["source_name"],
            source_type=row["source_type"],
            authority_tier=row["authority_tier"],
        )
        payload = stable_json(row)
        observation_id, _ = record_source_observation(
            connection,
            source_id=row["source_id"],
            external_id=row["external_id"],
            source_published_at=row["event_date"],
            local_received_at=utc_now(),
            title=row["evidence_title"],
            summary=row["evidence_passage"],
            canonical_url=row["evidence_url"],
            content_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            raw_json=payload,
            revision_kind="edit",
        )
        evidence_id = stable_id("EVID", row["event_id"], observation_id)
        confirmed_evidence = connection.execute(
            """SELECT 1 FROM event_evidence
               WHERE evidence_id=? AND evidence_status='confirmed_primary'""",
            (evidence_id,),
        ).fetchone()
        already = bool(
            confirmed_evidence
            and event["status"] == "verified"
            and event["label_status"] == "verified"
            and event["event_family"] == row["event_family"]
            and event["event_type"] == row["event_type"]
            and event["manual_grade"] == row["manual_grade"]
        )
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO event_observations(
               event_id,observation_id,relation_type,linked_at
               ) VALUES (?,?,'confirming_primary_evidence',?)""",
            (row["event_id"], observation_id, now),
        )
        connection.execute(
            """INSERT INTO event_evidence(
               evidence_id,event_id,observation_id,evidence_url,filing_date,form,items,
               evidence_passage,matched_keywords,passage_score,evidence_status,
               auto_verification_allowed,created_at,updated_at
               ) VALUES (?,?,?,?,?,NULL,NULL,?,NULL,NULL,'confirmed_primary',0,?,?)
               ON CONFLICT(evidence_id) DO UPDATE SET
                 evidence_passage=excluded.evidence_passage,
                 evidence_status='confirmed_primary',auto_verification_allowed=0,
                 updated_at=excluded.updated_at""",
            (
                evidence_id,
                row["event_id"],
                observation_id,
                row["evidence_url"],
                row["event_date"],
                row["evidence_passage"],
                now,
                now,
            ),
        )
        assessment_version = int(event["current_version"]) if already else int(event["current_version"]) + 1
        assessment_id = stable_id("ASSESS", row["event_id"], str(assessment_version))
        connection.execute(
            """INSERT INTO event_assessments(
               assessment_id,event_id,event_version,severity_grade,credibility_tier,
               r_score,l_score,e_score,c_score,p_score,x_score,score_total,
               assessed_by,rationale,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id,event_version) DO UPDATE SET
                 severity_grade=excluded.severity_grade,
                 credibility_tier=excluded.credibility_tier,
                 r_score=excluded.r_score,l_score=excluded.l_score,e_score=excluded.e_score,
                 c_score=excluded.c_score,p_score=excluded.p_score,x_score=excluded.x_score,
                 score_total=excluded.score_total,rationale=excluded.rationale""",
            (
                assessment_id,
                row["event_id"],
                assessment_version,
                row["manual_grade"],
                row["credibility_tier"],
                scores["R"],
                scores["L"],
                scores["E"],
                scores["C"],
                scores["P"],
                scores["X"],
                score_total,
                "manual_review_config",
                row["score_rationale"],
                now,
            ),
        )
        if already:
            result["already_applied"] += 1
            continue
        version = int(event["current_version"]) + 1
        facts = {
            "confirmed_facts": row["confirmed_facts"],
            "unconfirmed_facts": row["unconfirmed_facts"],
            "primary_evidence_url": row["evidence_url"],
            "manual_reviewed": True,
            "auto_verification_allowed": False,
            "no_trading": True,
        }
        connection.execute(
            """UPDATE canonical_events SET
               current_version=?,status='verified',label_status='verified',event_family=?,
               event_type=?,event_date=?,last_updated_at=?,ticker_at_event=?,company_name=?,
               manual_grade=?,provisional_grade_cap=?,no_trading=1
               WHERE event_id=?""",
            (
                version,
                row["event_family"],
                row["event_type"],
                row["event_date"],
                now,
                row.get("ticker_at_event"),
                row.get("company_name"),
                row["manual_grade"],
                row["manual_grade"],
                row["event_id"],
            ),
        )
        connection.execute(
            """INSERT INTO event_versions(
               event_id,version,changed_at,status,label_status,event_family,event_type,
               manual_grade,facts_json,change_reason
               ) VALUES (?,? ,?,'verified','verified',?,?,?,?,'manual_primary_evidence_review')""",
            (
                row["event_id"],
                version,
                now,
                row["event_family"],
                row["event_type"],
                row["manual_grade"],
                stable_json(facts),
            ),
        )
        connection.execute(
            """UPDATE pipeline_jobs SET status='COMPLETED_MANUAL_ADJUDICATION',
               attempts=attempts+1,last_error=NULL,updated_at=?
               WHERE event_id=? AND job_type='live_primary_evidence_review'""",
            (now, row["event_id"]),
        )
        result["applied"] += 1
    connection.commit()
    return result


def write_report(path: Path, rows: list[dict[str, Any]], result: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Live Primary Adjudications",
        "",
        f"- Requested: `{result['requested']}`",
        f"- Newly applied: `{result['applied']}`",
        f"- Already applied: `{result['already_applied']}`",
        "- All status changes require reviewed config rows; no model/API may auto-verify.",
        "",
        "## Decisions",
        "",
    ]
    for row in rows:
        chain_line = []
        if row.get("event_chain"):
            chain = row["event_chain"]
            chain_line = [
                f"- Event chain: `{chain['chain_id']}` / `{chain['chain_role']}` / "
                f"primary_count=`{int(bool(chain['counts_as_primary_event']))}`"
            ]
        lines.extend(
            [
                f"### {row['company_name']} — {row['event_type']}",
                "",
                f"- Event: `{row['event_id']}`",
                f"- Status/grade: `verified / {row['manual_grade']}`",
                f"- Credibility: `{row['credibility_tier']}`",
                f"- R/L/E/C/P/X: `{row['scores']['R']}/{row['scores']['L']}/{row['scores']['E']}/{row['scores']['C']}/{row['scores']['P']}/{row['scores']['X']}`",
                f"- Primary evidence: {row['evidence_url']}",
                f"- Confirmed: {'; '.join(row['confirmed_facts'])}",
                f"- Still unconfirmed: {'; '.join(row['unconfirmed_facts']) or 'none'}",
                *chain_line,
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
    connection = open_ledger(args.db)
    try:
        result = apply_rows(connection, rows)
    finally:
        connection.close()
    write_report(args.report, rows, result)
    print(stable_json(result))
    print(f"REPORT={args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
