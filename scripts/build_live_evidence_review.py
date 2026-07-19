#!/usr/bin/env python3
"""Build official-source evidence routes for pending live event candidates."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from event_ledger import open_ledger


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_CONFIG = ROOT / "config" / "live_evidence_routes.json"
DEFAULT_CSV = ROOT / "data" / "research" / "live_evidence_review_queue.csv"
DEFAULT_REPORT = ROOT / "reports" / "live_evidence_review_latest.md"


FIELDS = [
    "event_id",
    "event_date",
    "event_family",
    "event_type",
    "priority",
    "discovery_title",
    "evidence_goal",
    "source_name",
    "source_home",
    "suggested_query",
    "known_evidence_url",
    "known_evidence_date",
    "support_level",
    "evidence_note",
    "review_decision",
]


def build_rows(connection: Any, config: dict[str, Any]) -> list[dict[str, str]]:
    candidates = connection.execute(
        """
        SELECT e.*,j.priority,
               (SELECT r.title FROM event_observations eo
                JOIN latest_source_content r ON r.observation_id=eo.observation_id
                WHERE eo.event_id=e.event_id ORDER BY r.local_received_at LIMIT 1) AS discovery_title
              ,(SELECT r.canonical_url FROM event_observations eo
                JOIN latest_source_content r ON r.observation_id=eo.observation_id
                WHERE eo.event_id=e.event_id ORDER BY r.local_received_at LIMIT 1) AS discovery_url
              ,(SELECT r.source_id FROM event_observations eo
                JOIN latest_source_content r ON r.observation_id=eo.observation_id
                WHERE eo.event_id=e.event_id ORDER BY r.local_received_at LIMIT 1) AS discovery_source_id
              ,(SELECT s.authority_tier FROM event_observations eo
                JOIN latest_source_content r ON r.observation_id=eo.observation_id
                JOIN sources s ON s.source_id=r.source_id
                WHERE eo.event_id=e.event_id ORDER BY r.local_received_at LIMIT 1) AS discovery_authority
        FROM canonical_events e
        JOIN pipeline_jobs j ON j.event_id=e.event_id
        WHERE j.job_type='live_primary_evidence_review'
          AND j.status='PENDING_PRIMARY_EVIDENCE'
        ORDER BY j.priority DESC,e.event_date DESC,e.event_id
        """
    ).fetchall()
    routes = config["routes"]
    known = config.get("known_context", {})
    output: list[dict[str, str]] = []
    for event in candidates:
        route = routes.get(event["event_type"], routes["default"])
        evidence_rows = known.get(event["event_id"]) or [{}]
        if str(event["discovery_authority"] or "").startswith("P0") and event["discovery_url"]:
            output.append(
                {
                    "event_id": event["event_id"],
                    "event_date": event["event_date"],
                    "event_family": event["event_family"],
                    "event_type": event["event_type"],
                    "priority": str(event["priority"]),
                    "discovery_title": event["discovery_title"] or "",
                    "evidence_goal": route["goal"],
                    "source_name": f"Captured P0 source: {event['discovery_source_id']}",
                    "source_home": event["discovery_url"],
                    "suggested_query": "",
                    "known_evidence_url": event["discovery_url"],
                    "known_evidence_date": event["event_date"],
                    "support_level": "official_discovery_unreviewed",
                    "evidence_note": (
                        "Official source was captured directly; a reviewer must still confirm "
                        "the exact event claim, materiality, and severity."
                    ),
                    "review_decision": "pending_manual_review",
                }
            )
        enrichment = connection.execute(
            """SELECT primary_document_url,evidence_excerpt,matched_event_type,confidence
               FROM sec_filing_enrichments
               WHERE event_id=? AND status='PARSED'""",
            (event["event_id"],),
        ).fetchone()
        if enrichment is not None and enrichment["primary_document_url"]:
            matched = enrichment["matched_event_type"] or "no_specific_type_match"
            output.append(
                {
                    "event_id": event["event_id"],
                    "event_date": event["event_date"],
                    "event_family": event["event_family"],
                    "event_type": event["event_type"],
                    "priority": str(event["priority"]),
                    "discovery_title": event["discovery_title"] or "",
                    "evidence_goal": route["goal"],
                    "source_name": "SEC primary document extraction",
                    "source_home": enrichment["primary_document_url"],
                    "suggested_query": "",
                    "known_evidence_url": enrichment["primary_document_url"],
                    "known_evidence_date": event["event_date"],
                    "support_level": "machine_extracted_unreviewed",
                    "evidence_note": (
                        f"Machine match: {matched}; confidence={enrichment['confidence']}. "
                        f"Excerpt: {enrichment['evidence_excerpt']}"
                    ),
                    "review_decision": "pending_manual_review",
                }
            )
        for source_name, source_home in route["official_sources"]:
            matching = [
                item for item in evidence_rows if item.get("source_name") == source_name
            ] or ([evidence_rows[0]] if len(evidence_rows) == 1 and not evidence_rows[0] else [{}])
            for evidence in matching:
                company = event["company_name"] or ""
                query = " ".join(
                    value
                    for value in [
                        company,
                        str(event["event_date"]),
                        str(event["event_type"]),
                        *route["query_terms"],
                    ]
                    if value
                )
                output.append(
                    {
                        "event_id": event["event_id"],
                        "event_date": event["event_date"],
                        "event_family": event["event_family"],
                        "event_type": event["event_type"],
                        "priority": str(event["priority"]),
                        "discovery_title": event["discovery_title"] or "",
                        "evidence_goal": route["goal"],
                        "source_name": source_name,
                        "source_home": source_home,
                        "suggested_query": query,
                        "known_evidence_url": evidence.get("evidence_url", ""),
                        "known_evidence_date": evidence.get("evidence_date", ""),
                        "support_level": evidence.get("support_level", "search_required"),
                        "evidence_note": evidence.get("note", ""),
                        "review_decision": "pending_manual_review",
                    }
                )
    return output


def write_outputs(rows: list[dict[str, str]], csv_path: Path, report_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    event_ids = sorted({row["event_id"] for row in rows})
    partial = sum(row["support_level"] == "partial_context_only" for row in rows)
    lines = [
        "# Live Evidence Review Queue",
        "",
        f"- Pending events: `{len(event_ids)}`",
        f"- Official-source routes: `{len(rows)}`",
        f"- Partial context links: `{partial}`",
        "- Policy: search results and context links do not change event status.",
        "- Promotion requires a reviewed row with exact-claim primary evidence and R/L/E/C/P/X scores.",
        "",
    ]
    for event_id in event_ids:
        event_rows = [row for row in rows if row["event_id"] == event_id]
        first = event_rows[0]
        lines.extend(
            [
                f"## {event_id}",
                "",
                f"- Type/date: `{first['event_type']}` / `{first['event_date']}`",
                f"- Discovery: {first['discovery_title']}",
                f"- Evidence goal: {first['evidence_goal']}",
            ]
        )
        for row in event_rows:
            suffix = (
                f" — {row['support_level']}: {row['known_evidence_url']}"
                if row["known_evidence_url"]
                else ""
            )
            lines.append(f"- {row['source_name']}: {row['source_home']}{suffix}")
        lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    connection = open_ledger(args.db)
    try:
        rows = build_rows(connection, config)
    finally:
        connection.close()
    write_outputs(rows, args.csv, args.report)
    print(f"pending_events={len({row['event_id'] for row in rows})} routes={len(rows)}")
    print(f"CSV={args.csv}")
    print(f"REPORT={args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
