from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def target_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        return next(reader)


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def build_import_rows(
    adjudications: Iterable[dict[str, str]],
    queue_by_event: dict[str, dict[str, str]],
    header: list[str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for adjudication in adjudications:
        event_id = adjudication["event_candidate_id"]
        queue = queue_by_event.get(event_id)
        if queue is None:
            # Adjudications are durable across queue rotations.  Identity fields in the
            # adjudication ledger are sufficient for a review-only row; richer company
            # metadata may remain blank until D:/short performs its own stable-ID join.
            queue = {
                "company_name": "",
                "exchange": "",
                "ticker_at_event": adjudication.get("ticker_at_event", ""),
                "industry": "",
            }
        verified_severe = (
            adjudication["label_status"] == "verified"
            and adjudication["manual_grade"] in {"S", "A++"}
        )
        rejected = adjudication["label_status"] == "rejected"
        linked_consequence = "linked_consequence" in adjudication.get("training_role", "")
        values = {column: "" for column in header}
        updates = {
                "event_source": "finance_radar_active_research",
                "event_id": event_id,
                "company_name": queue.get("company_name", ""),
                "listing": ":".join(
                    part for part in [queue.get("exchange", ""), queue.get("ticker_at_event", "")] if part
                ),
                "country_industry": queue.get("industry", ""),
                "event_type_raw": adjudication["detected_event_type"],
                "event_date": adjudication["event_date"],
                "milestones": adjudication["evidence_date"],
                "description": adjudication["evidence_summary"],
                "evidence_codes": adjudication["evidence_url"],
                "initial_grade": adjudication["manual_grade"],
                "final_outcome": adjudication["adjudication_note"],
                "notes": (
                    "Finance Radar evidence adjudication. Pending D:/short manual import approval; "
                    "no automatic training membership."
                ),
                "label_status": adjudication["label_status"],
                "match_status": "matched",
                "match_method": "finance_radar_stable_id",
                "security_master_id": "",
                "stable_id": adjudication["stable_id"],
                "ticker_at_event": adjudication["ticker_at_event"],
                "matched_company_name": queue.get("company_name", ""),
                "canonical_event_family": adjudication["canonical_event_family"],
                "canonical_event_type": adjudication["canonical_event_type"],
                "R": adjudication["R"],
                "L": adjudication["L"],
                "E": adjudication["E"],
                "C": adjudication["C"],
                "P": adjudication["P"],
                "X": adjudication["X"],
                "score_total_manual": adjudication["score_total"],
                "hard_training_label_raw": "false",
                "semi_supervised_label": "false",
                "event_chain_id": (
                    f"FR-{adjudication['stable_id']}-{adjudication['event_date']}"
                ),
                "event_chain_family": adjudication["canonical_event_family"],
                "event_chain_role": "consequence" if linked_consequence else "primary",
                "hard_training_dedup_excluded": "true",
                "hard_training_dedup_reason": (
                    "linked_consequence_same_event_chain"
                    if linked_consequence
                    else "pending_D_short_import_review"
                ),
                "hard_training_label": "false",
                "training_bucket": (
                    "pending_verified_severe_import_review"
                    if verified_severe
                    else "pending_rejected_control_import_review"
                    if rejected
                    else "pending_import_review"
                ),
                "recommended_use": adjudication["training_role"],
            }
        for key, value in updates.items():
            if key in values:
                values[key] = value
        rows.append(values)
    return rows


def write_exact_header(path: Path, rows: list[dict[str, str]], header: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run(
    *,
    adjudication_path: Path,
    queue_path: Path,
    short_label_book_path: Path,
    output_dir: Path,
    report_dir: Path,
) -> tuple[Path, Path, Path, int]:
    adjudications = read_csv(adjudication_path)
    queue_by_event = {row["event_candidate_id"]: row for row in read_csv(queue_path)}
    queue_metadata_rows = sum(
        1 for row in adjudications if row["event_candidate_id"] in queue_by_event
    )
    adjudication_fallback_rows = len(adjudications) - queue_metadata_rows
    header = target_header(short_label_book_path)
    rows = build_import_rows(adjudications, queue_by_event, header)

    packet_path = output_dir / "d_short_label_import_review_packet.csv"
    manifest_path = output_dir / "d_short_label_import_review_manifest.json"
    report_path = report_dir / "d_short_label_import_review_latest.md"
    write_exact_header(packet_path, rows, header)

    generated_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": "d-short-label-import-review-v1",
        "generated_at": generated_at,
        "source_adjudications": str(adjudication_path.resolve()),
        "source_queue": str(queue_path.resolve()),
        "target_schema_source": str(short_label_book_path.resolve()),
        "rows": len(rows),
        "queue_metadata_rows": queue_metadata_rows,
        "adjudication_identity_fallback_rows": adjudication_fallback_rows,
        "header_columns": len(header),
        "invariants": {
            "target_header_exact_match": True,
            "writes_to_D_short": False,
            "hard_training_label": False,
            "automatic_import_allowed": False,
            "manual_review_required": True,
            "live_trading_allowed": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# D:/short Label Import Review Packet",
        "",
        f"Generated: `{generated_at}`",
        "",
        f"- Rows: `{len(rows)}`",
        f"- Current-queue metadata rows: `{queue_metadata_rows}`",
        f"- Durable adjudication-identity fallback rows: `{adjudication_fallback_rows}`",
        f"- Target schema columns: `{len(header)}`",
        "- The CSV header exactly matches the current D:/short event label book.",
        "- No file under D:/short was written or changed.",
        "- Every row has hard training disabled and remains excluded pending manual import review.",
        "",
        "## Rows",
        "",
        "| ticker | event date | detected | label status | manual grade | import bucket | evidence |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('ticker_at_event', '')} | {row.get('event_date', '')} | "
            f"{row.get('event_type_raw', '')} | {row.get('label_status', '')} | "
            f"{row.get('initial_grade', '')} | {row.get('training_bucket', '')} | "
            f"[SEC]({row.get('evidence_codes', '')}) |"
        )
    lines.extend(
        [
            "",
            "## Import Gate",
            "",
            "Before importing, D:/short must review evidence, event-chain duplication, stable-ID mapping, "
            "canonical taxonomy, and whether the row belongs in a verified or rejected bucket. Only its "
            "existing label-book generator may decide final training membership.",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return packet_path, report_path, manifest_path, len(rows)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build a review-only import packet for D:/short labels.")
    parser.add_argument(
        "--adjudications",
        type=Path,
        default=root / "reports/active_event_adjudications.csv",
    )
    parser.add_argument(
        "--queue", type=Path, default=root / "data/research/active_event_research_queue.csv"
    )
    parser.add_argument(
        "--short-label-book",
        type=Path,
        default=Path("D:/short/data/curated/event_label_book_v0.csv"),
    )
    parser.add_argument("--output-dir", type=Path, default=root / "data/research")
    parser.add_argument("--report-dir", type=Path, default=root / "reports")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    packet, report, manifest, rows = run(
        adjudication_path=args.adjudications,
        queue_path=args.queue,
        short_label_book_path=args.short_label_book,
        output_dir=args.output_dir,
        report_dir=args.report_dir,
    )
    print(packet)
    print(report)
    print(manifest)
    print(f"rows={rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
