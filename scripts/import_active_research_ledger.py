from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from manual_historical_findings import load_manual_findings

from event_ledger import (
    backup_database,
    canonical_event_id,
    has_event_ledger_schema,
    import_active_research,
    ledger_summary,
    open_ledger,
    read_csv,
)


def include_durable_adjudication_context(
    connection,
    queue_rows: list[dict[str, str]],
    manual_rows: list[dict[str, str]],
    adjudication_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Replay reviewed rows that have rotated out of the active discovery queue.

    The adjudication CSV is the durable human-decision ledger.  A queue rotation
    must not prevent a later correction from reaching SQLite, and a fresh ledger
    rebuild must not silently omit older reviewed events.
    """
    combined = [*queue_rows, *manual_rows]
    represented = {row["event_candidate_id"] for row in combined}
    for index, adjudication in enumerate(adjudication_rows, start=1):
        candidate_id = adjudication["event_candidate_id"]
        if candidate_id in represented:
            continue
        existing = connection.execute(
            """
            SELECT company_name,event_family,provisional_grade_cap
            FROM canonical_events
            WHERE event_id=?
            """,
            (canonical_event_id(candidate_id),),
        ).fetchone()
        ticker = adjudication.get("ticker_at_event", "")
        combined.append(
            {
                "queue_rank": f"durable-{index}",
                "event_candidate_id": candidate_id,
                "stable_id": adjudication.get("stable_id", ""),
                "ticker_at_event": ticker,
                "company_name": (
                    existing["company_name"] if existing and existing["company_name"] else ticker
                ),
                "event_date": adjudication.get("event_date", ""),
                "event_family": (
                    existing["event_family"]
                    if existing and existing["event_family"]
                    else "historical_adjudication_archive"
                ),
                "event_type": adjudication.get("detected_event_type", ""),
                "detection_rule": "durable historical adjudication replay",
                "detection_value": adjudication.get("detected_event_type", ""),
                "priority_score": "0",
                "provisional_grade_cap": (
                    existing["provisional_grade_cap"]
                    if existing and existing["provisional_grade_cap"]
                    else ""
                ),
                "sec_filings_url": "",
            }
        )
        represented.add(candidate_id)
    return combined


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Import active research artifacts into the event ledger.")
    parser.add_argument("--db", type=Path, default=root / "data/finance_radar.sqlite3")
    parser.add_argument(
        "--queue", type=Path, default=root / "data/research/active_event_research_queue.csv"
    )
    parser.add_argument(
        "--passages",
        type=Path,
        default=root / "data/research/active_event_sec_evidence_passages.csv",
    )
    parser.add_argument(
        "--adjudications",
        type=Path,
        default=root / "reports/active_event_adjudications.csv",
    )
    parser.add_argument(
        "--market-outcomes",
        type=Path,
        default=root / "data/research/active_event_market_outcomes.csv",
    )
    parser.add_argument(
        "--manual-findings",
        type=Path,
        default=root / "config/active_event_manual_findings.json",
    )
    parser.add_argument("--report-dir", type=Path, default=root / "reports")
    parser.add_argument("--backup-dir", type=Path, default=root / "data/backups")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    backup_path = None
    if args.db.is_file() and not has_event_ledger_schema(args.db):
        backup_path = backup_database(args.db, args.backup_dir)
    if backup_path is None and args.backup_dir.is_dir():
        existing_backups = sorted(
            args.backup_dir.glob("finance_radar_before_event_ledger_*.sqlite3"),
            key=lambda path: path.stat().st_mtime,
        )
        if existing_backups:
            backup_path = existing_backups[-1]
    connection = open_ledger(args.db)
    try:
        queue_rows = read_csv(args.queue)
        manual_rows = load_manual_findings(args.manual_findings)
        queue_ids = {row["event_candidate_id"] for row in queue_rows}
        overlap = queue_ids.intersection(row["event_candidate_id"] for row in manual_rows)
        if overlap:
            raise ValueError(f"Manual finding duplicates generated queue candidate: {sorted(overlap)}")
        adjudication_rows = read_csv(args.adjudications)
        import_rows = include_durable_adjudication_context(
            connection,
            queue_rows,
            manual_rows,
            adjudication_rows,
        )
        counts = import_active_research(
            connection,
            queue_rows=import_rows,
            passage_rows=read_csv(args.passages),
            adjudication_rows=adjudication_rows,
            market_rows=read_csv(args.market_outcomes),
        )
        summary = ledger_summary(connection)
    finally:
        connection.close()

    generated_at = datetime.now(timezone.utc).isoformat()
    report = {
        "generated_at": generated_at,
        "database": str(args.db.resolve()),
        "backup_before_migration": str(backup_path.resolve()) if backup_path else None,
        "import_counts": counts.__dict__,
        **summary,
    }
    args.report_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.report_dir / "event_ledger_import_latest.json"
    md_path = args.report_dir / "event_ledger_import_latest.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Event Ledger Import",
        "",
        f"Generated: `{generated_at}`",
        "",
        f"- Database: `{args.db.resolve()}`",
        f"- Backup before first migration: `{backup_path.resolve() if backup_path else 'database was new'}`",
        f"- Imported queue rows: `{counts.queue_rows}`",
        f"- Canonical events: `{summary['table_counts']['canonical_events']}`",
        f"- Raw observations: `{summary['table_counts']['raw_observations']}`",
        f"- Event versions: `{summary['table_counts']['event_versions']}`",
        f"- Evidence rows: `{summary['table_counts']['event_evidence']}`",
        f"- Post-event market metrics: `{summary['table_counts']['event_market_metrics']}`",
        f"- Pipeline jobs: `{summary['table_counts']['pipeline_jobs']}`",
        f"- No-trading violations: `{summary['no_trading_violations']}`",
        f"- Auto-verification violations: `{summary['auto_verification_violations']}`",
        f"- Market-metric scope violations: `{summary['market_metric_scope_violations']}`",
        "",
        "## Event Status",
        "",
    ]
    lines.extend(f"- `{status}`: `{count}`" for status, count in summary["event_status"].items())
    lines.extend(["", "## Job Status", ""])
    lines.extend(f"- `{status}`: `{count}`" for status, count in summary["job_status"].items())
    lines.extend(
        [
            "",
            "The import is idempotent. It does not enqueue Telegram messages, mutate D:/short, "
            "or create any trading/order capability.",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(args.db)
    print(md_path)
    print(json_path)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
