#!/usr/bin/env python3
"""Close SEC evidence gaps for unresolved historical review threads.

The first research pass intentionally extracts only a small number of filings per
event.  This follow-up pass targets unresolved triage rows, refreshes the SEC
filing candidates with event-specific lookback windows, expands the selected
filings, merges candidate passages, and rebuilds review priority.  It never
verifies, rejects, grades, alerts, or trades.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from active_sec_evidence import load_simple_env
from active_sec_evidence import run as run_candidate_refresh
from active_sec_evidence import write_csv as write_candidate_rows
from build_active_review_triage import build_rows as build_triage_rows
from build_active_review_triage import write_outputs as write_triage_outputs
from extract_sec_evidence_text import run as run_extraction
from extract_sec_evidence_text import write_passages
from run_active_research_cycle import merge_rows


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRIAGE = ROOT / "data" / "research" / "active_event_review_triage.csv"
DEFAULT_CANDIDATES = ROOT / "data" / "research" / "active_event_sec_evidence_candidates.csv"
DEFAULT_PASSAGES = ROOT / "data" / "research" / "active_event_sec_evidence_passages.csv"
DEFAULT_QUEUE = ROOT / "data" / "research" / "active_event_research_queue.csv"
DEFAULT_ADJUDICATIONS = ROOT / "reports" / "active_event_adjudications.csv"
DEFAULT_REPORT = ROOT / "reports" / "active_evidence_gap_closure_latest.md"


BUCKET_PRIORITY = {
    "delisting_cause_review": 100,
    "source_mismatch_review": 95,
    "low_evidence_fundamental": 85,
    "price_only_control": 75,
    "ordinary_corporate_action": 65,
    "single_quarter_interest_coverage_boundary": 60,
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def target_priority(row: dict[str, str]) -> tuple[int, int, int]:
    priority = BUCKET_PRIORITY.get(row.get("review_bucket", ""), 0)
    if row.get("proposed_disposition") == "cause_unresolved":
        priority += 20
    if row.get("evidence_readiness") in {"filing_link_only", "no_sec_candidate_yet"}:
        priority += 10
    return (
        priority,
        int(row.get("review_score") or 0),
        -int(row.get("review_rank") or 10**9),
    )


def select_targets(rows: Iterable[dict[str, str]], top_n: int) -> list[dict[str, str]]:
    eligible = [row for row in rows if row.get("review_bucket") in BUCKET_PRIORITY]
    eligible.sort(key=target_priority, reverse=True)
    return eligible[: max(0, top_n)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triage", type=Path, default=DEFAULT_TRIAGE)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--passages", type=Path, default=DEFAULT_PASSAGES)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--adjudications", type=Path, default=DEFAULT_ADJUDICATIONS)
    parser.add_argument("--top-n", type=int, default=25)
    parser.add_argument("--per-event", type=int, default=5)
    parser.add_argument("--candidate-per-event", type=int, default=10)
    parser.add_argument("--candidate-before-days", type=int, default=10)
    parser.add_argument("--candidate-after-days", type=int, default=45)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument(
        "--event-id",
        action="append",
        default=[],
        help="Deepen these exact triage event IDs instead of automatic gap selection; repeatable.",
    )
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    before_triage = read_csv(args.triage)
    requested_ids = set(args.event_id)
    targets = (
        [row for row in before_triage if row.get("event_candidate_id") in requested_ids]
        if requested_ids
        else select_targets(before_triage, args.top_n)
    )
    event_ids = {row["event_candidate_id"] for row in targets}
    if not event_ids:
        print(json.dumps({"targeted": 0, "reason": "no_eligible_triage_rows"}))
        return 0

    env = {**load_simple_env(args.env), **os.environ}
    deep_data = ROOT / "data" / "research" / "deep_evidence"
    deep_reports = ROOT / "reports" / "deep_evidence"
    candidate_refresh = run_candidate_refresh(
        queue_path=args.queue,
        output_dir=deep_data / "candidate_refresh",
        report_dir=deep_reports / "candidate_refresh",
        cache_dir=ROOT / "data" / "cache" / "sec" / "submissions",
        user_agent=env.get("SEC_USER_AGENT", ""),
        top_n=len(event_ids),
        before_days=max(0, args.candidate_before_days),
        after_days=max(0, args.candidate_after_days),
        filings_per_event=max(1, args.candidate_per_event),
        event_ids=event_ids,
    )
    existing_candidates = read_csv(args.candidates)
    refreshed_candidates = read_csv(candidate_refresh.evidence_path)
    merged_candidates = merge_rows(
        existing_candidates,
        refreshed_candidates,
        ("event_candidate_id", "accession_number", "filing_document_url"),
    )
    write_candidate_rows(args.candidates, merged_candidates)
    extraction = run_extraction(
        candidates_path=args.candidates,
        output_dir=deep_data,
        report_dir=deep_reports,
        cache_dir=ROOT / "data" / "cache" / "sec" / "documents",
        user_agent=env.get("SEC_USER_AGENT", ""),
        event_limit=len(event_ids),
        per_event=max(1, args.per_event),
        max_chars=max(200, args.max_chars),
        event_ids=event_ids,
    )
    incoming = read_csv(extraction.passages_path)
    existing = read_csv(args.passages)
    merged = merge_rows(
        existing,
        incoming,
        ("event_candidate_id", "accession_number", "filing_document_url"),
    )
    write_passages(args.passages, merged)

    after_triage = build_triage_rows(
        read_csv(args.queue), merged, read_csv(args.adjudications)
    )
    write_triage_outputs(
        after_triage,
        args.triage,
        ROOT / "reports" / "active_event_review_triage_latest.md",
    )

    generated_at = datetime.now(timezone.utc).isoformat()
    before_by_id = {row["event_candidate_id"]: row for row in before_triage}
    after_by_id = {row["event_candidate_id"]: row for row in after_triage}
    changed = []
    for event_id in sorted(event_ids):
        before = before_by_id.get(event_id, {})
        after = after_by_id.get(event_id, {})
        if (
            before.get("review_bucket") != after.get("review_bucket")
            or before.get("filing_document_url") != after.get("filing_document_url")
            or before.get("review_score") != str(after.get("review_score", ""))
        ):
            changed.append(
                {
                    "event_id": event_id,
                    "ticker": before.get("ticker_at_event", after.get("ticker_at_event", "")),
                    "before": before.get("review_bucket", ""),
                    "after": after.get("review_bucket", "removed_or_reviewed"),
                }
            )

    all_errors = list(candidate_refresh.errors) + list(extraction.errors)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Active Evidence Gap Closure",
        "",
        f"Generated: `{generated_at}`",
        "",
        f"- Targeted unresolved threads: `{len(event_ids)}`",
        f"- Refreshed SEC filing candidates: `{len(refreshed_candidates)}`",
        f"- New merged filing candidates: `{max(0, len(merged_candidates) - len(existing_candidates))}`",
        f"- Expanded filing rows: `{len(incoming)}`",
        f"- New merged passage rows: `{max(0, len(merged) - len(existing))}`",
        f"- Candidate passages in expanded rows: `{sum(row.get('passage_status') == 'candidate_passage' for row in incoming)}`",
        f"- Fetch/extraction errors: `{len(all_errors)}`",
        f"- Triage rows before/after: `{len(before_triage)}` / `{len(after_triage)}`",
        f"- Target rows whose route or selected evidence changed: `{len(changed)}`",
        "- Boundary: this pass changes evidence coverage and review priority only; labels, alerts and trading remain untouched.",
        "",
        "## Target buckets",
        "",
    ]
    counts = Counter(row.get("review_bucket", "unknown") for row in targets)
    lines.extend(f"- `{bucket}`: `{count}`" for bucket, count in sorted(counts.items()))
    if changed:
        lines.extend(
            [
                "",
                "## Changed routes",
                "",
                "| ticker | before | after |",
                "|---|---|---|",
            ]
        )
        lines.extend(
            f"| {row['ticker']} | {row['before']} | {row['after']} |" for row in changed[:30]
        )
    if all_errors:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- `{error}`" for error in all_errors)
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result: dict[str, Any] = {
        "targeted": len(event_ids),
        "refreshed_candidate_rows": len(refreshed_candidates),
        "new_candidate_rows": max(0, len(merged_candidates) - len(existing_candidates)),
        "expanded_rows": len(incoming),
        "new_passage_rows": max(0, len(merged) - len(existing)),
        "changed_routes": len(changed),
        "triage_after": len(after_triage),
        "errors": len(all_errors),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"REPORT={args.report}")
    return 0 if not all_errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
