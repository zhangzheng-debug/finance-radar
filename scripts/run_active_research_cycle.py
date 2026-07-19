#!/usr/bin/env python3
"""Advance one non-trading Sharadar/SEC historical evidence research batch."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from active_event_discovery import (
    build_active_queue,
    update_completed_registry,
    update_completed_thread_registry,
    write_outputs as write_discovery_outputs,
)
from active_sec_evidence import load_simple_env, run as run_sec_evidence, write_csv as write_evidence_csv
from build_active_review_triage import build_rows as build_triage_rows
from build_active_review_triage import read_csv, write_outputs as write_triage_outputs
from event_ledger import import_active_research, ledger_summary, open_ledger
from extract_sec_evidence_text import run as run_passage_extraction
from extract_sec_evidence_text import write_passages


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "active_event_research.json"
DEFAULT_STATE = ROOT / "data" / "research" / "active_research_cycle_state.json"
DEFAULT_REPORT = ROOT / "reports" / "active_research_cycle_latest.md"


def queue_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def merge_rows(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    key_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    merged = {tuple(str(row.get(key, "")) for key in key_fields): dict(row) for row in existing}
    for row in incoming:
        merged[tuple(str(row.get(key, "")) for key in key_fields)] = dict(row)
    return sorted(
        merged.values(),
        key=lambda row: (
            int(row.get("queue_rank") or 10**9),
            str(row.get("event_candidate_id") or ""),
            str(row.get("filing_date") or ""),
            str(row.get("accession_number") or ""),
        ),
    )


def infer_initial_offset(
    state: dict[str, Any] | None,
    *,
    current_hash: str,
    initial_batch_size: int,
    existing_evidence: bool,
) -> int:
    if state:
        if state.get("queue_sha256") == current_hash:
            return max(0, int(state.get("next_offset", 0)))
        return 0
    return initial_batch_size if existing_evidence else 0


def committed_next_offset(
    *,
    offset: int,
    batch_rows: int,
    queue_rows: int,
    evidence_errors: tuple[str, ...],
    passage_errors: tuple[str, ...],
) -> int:
    """Advance only after every source and extraction operation succeeded."""
    if evidence_errors or passage_errors:
        return offset
    return min(queue_rows, offset + batch_rows)


def ensure_queue(
    config: dict[str, Any], queue_path: Path, *, force_refill: bool = False
) -> tuple[list[dict[str, str]], bool]:
    existing = read_csv(queue_path) if queue_path.is_file() else []
    floor = int(config.get("queue_refill_floor", 50))
    if len(existing) >= floor and not force_refill:
        return existing, False
    short_root = Path(config["short_research_root"])
    completed_ids = update_completed_registry(
        ROOT / "data" / "research" / "active_event_completed_candidates.csv",
        queue_path=queue_path,
        adjudications_path=ROOT / "reports" / "active_event_adjudications.csv",
    )
    completed_threads = update_completed_thread_registry(
        ROOT / "data" / "research" / "active_event_completed_threads.csv",
        queue_path=queue_path,
        adjudications_path=ROOT / "reports" / "active_event_adjudications.csv",
    )
    queue = build_active_queue(
        short_root,
        start_date=str(config.get("start_date") or "1900-01-01"),
        end_date=config.get("end_date"),
        per_family=int(config.get("per_family", 30)),
        max_total=int(config.get("queue_target", config.get("max_total", 150))),
        common_equity_only=bool(config.get("common_equity_only", True)),
        exclude_reviewed=bool(config.get("exclude_reviewed", True)),
        additional_excluded_ids=completed_ids,
        additional_excluded_threads=completed_threads,
    )
    result = write_discovery_outputs(
        queue,
        short_root=short_root,
        output_dir=queue_path.parent,
        start_date=str(config.get("start_date") or "1900-01-01"),
        end_date=config.get("end_date"),
        per_family=int(config.get("per_family", 30)),
        max_total=int(config.get("queue_target", config.get("max_total", 150))),
        completed_registry_rows=len(completed_ids),
    )
    return read_csv(result.queue_path), True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    queue_path = ROOT / "data" / "research" / "active_event_research_queue.csv"
    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.is_file() else None
    triage_path = ROOT / "data" / "research" / "active_event_review_triage.csv"
    old_hash = queue_hash(queue_path) if queue_path.is_file() else ""
    force_refill = bool(
        state
        and state.get("queue_exhausted")
        and state.get("queue_sha256") == old_hash
        and not read_csv(triage_path)
    )
    queue_rows, regenerated = ensure_queue(
        config, queue_path, force_refill=force_refill
    )
    print(json.dumps({"stage": "queue_ready", "rows": len(queue_rows), "regenerated": regenerated}), flush=True)
    current_hash = queue_hash(queue_path)
    batch_size = int(config.get("active_cycle_batch_size", 25))
    main_candidates = ROOT / "data" / "research" / "active_event_sec_evidence_candidates.csv"
    main_passages = ROOT / "data" / "research" / "active_event_sec_evidence_passages.csv"
    offset = 0 if regenerated else infer_initial_offset(
        state,
        current_hash=current_hash,
        initial_batch_size=int(config.get("sec_evidence_top_n", batch_size)),
        existing_evidence=main_candidates.is_file(),
    )
    offset = min(offset, len(queue_rows))
    batch = queue_rows[offset : offset + batch_size]
    generated_at = datetime.now(timezone.utc).isoformat()

    new_candidates: list[dict[str, Any]] = []
    new_passages: list[dict[str, Any]] = []
    evidence_errors: tuple[str, ...] = ()
    passage_errors: tuple[str, ...] = ()
    if batch:
        batch_root = ROOT / "data" / "research" / "cycle_batch"
        batch_reports = ROOT / "reports" / "cycle_batch"
        batch_queue = batch_root / "active_event_research_queue.csv"
        write_rows(batch_queue, batch, list(batch[0]))
        env = {**load_simple_env(args.env), **os.environ}
        evidence_run = run_sec_evidence(
            queue_path=batch_queue,
            output_dir=batch_root,
            report_dir=batch_reports,
            cache_dir=ROOT / "data" / "cache" / "sec" / "submissions",
            user_agent=env.get("SEC_USER_AGENT", ""),
            top_n=len(batch),
            before_days=int(config.get("sec_window_before_days", 10)),
            after_days=int(config.get("sec_window_after_days", 45)),
            filings_per_event=int(config.get("sec_filings_per_event", 5)),
        )
        evidence_errors = evidence_run.errors
        new_candidates = read_csv(evidence_run.evidence_path)
        print(json.dumps({"stage": "sec_candidates_ready", "rows": len(new_candidates), "errors": len(evidence_errors)}), flush=True)
        extraction_run = run_passage_extraction(
            candidates_path=evidence_run.evidence_path,
            output_dir=batch_root,
            report_dir=batch_reports,
            cache_dir=ROOT / "data" / "cache" / "sec" / "documents",
            user_agent=env.get("SEC_USER_AGENT", ""),
            event_limit=len(batch),
            per_event=int(config.get("sec_extract_filings_per_event", 2)),
            max_chars=int(config.get("sec_evidence_passage_max_chars", 700)),
        )
        passage_errors = extraction_run.errors
        new_passages = read_csv(extraction_run.passages_path)
        print(json.dumps({"stage": "sec_passages_ready", "rows": len(new_passages), "errors": len(passage_errors)}), flush=True)

    merged_candidates = merge_rows(
        read_csv(main_candidates) if main_candidates.is_file() else [],
        new_candidates,
        ("event_candidate_id", "accession_number"),
    )
    merged_passages = merge_rows(
        read_csv(main_passages) if main_passages.is_file() else [],
        new_passages,
        ("event_candidate_id", "accession_number", "filing_document_url"),
    )
    if merged_candidates:
        write_evidence_csv(main_candidates, merged_candidates)
    if merged_passages:
        write_passages(main_passages, merged_passages)
    print(json.dumps({"stage": "evidence_merged", "candidates": len(merged_candidates), "passages": len(merged_passages)}), flush=True)

    adjudications_path = ROOT / "reports" / "active_event_adjudications.csv"
    adjudications = read_csv(adjudications_path)
    triage_rows = build_triage_rows(queue_rows, merged_passages, adjudications)
    write_triage_outputs(
        triage_rows,
        ROOT / "data" / "research" / "active_event_review_triage.csv",
        ROOT / "reports" / "active_event_review_triage_latest.md",
    )
    print(json.dumps({"stage": "triage_ready", "rows": len(triage_rows)}), flush=True)

    market_path = ROOT / "data" / "research" / "active_event_market_outcomes.csv"
    connection = open_ledger(ROOT / "data" / "finance_radar.sqlite3")
    try:
        counts = import_active_research(
            connection,
            queue_rows=queue_rows,
            passage_rows=merged_passages,
            adjudication_rows=adjudications,
            market_rows=read_csv(market_path) if market_path.is_file() else [],
        )
        ledger = ledger_summary(connection)
    finally:
        connection.close()
    print(json.dumps({"stage": "ledger_imported", "canonical_events": ledger["table_counts"]["canonical_events"]}), flush=True)

    next_offset = committed_next_offset(
        offset=offset,
        batch_rows=len(batch),
        queue_rows=len(queue_rows),
        evidence_errors=evidence_errors,
        passage_errors=passage_errors,
    )
    batch_committed = next_offset > offset or not batch
    state_payload = {
        "schema_version": "active-research-cycle-v1",
        "queue_sha256": current_hash,
        "queue_rows": len(queue_rows),
        "last_offset": offset,
        "last_batch_rows": len(batch),
        "next_offset": next_offset,
        "queue_exhausted": next_offset >= len(queue_rows),
        "cycles_completed": int((state or {}).get("cycles_completed", 0)) + 1,
        "updated_at": generated_at,
        "invariants": {
            "D_short_read_only": True,
            "post_event_outcomes_used_for_ranking": False,
            "automatic_label_mutation": False,
            "live_trading_allowed": False,
        },
    }
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    args.report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Active Research Cycle",
        "",
        f"Generated: `{generated_at}`",
        "",
        f"- Queue regenerated: `{regenerated}`",
        f"- Queue rows: `{len(queue_rows)}`",
        f"- Batch offset/rows: `{offset}` / `{len(batch)}`",
        f"- Next offset: `{next_offset}`",
        f"- Batch committed: `{batch_committed}`",
        f"- New SEC filing candidates: `{len(new_candidates)}`",
        f"- New SEC passage rows: `{len(new_passages)}`",
        f"- Aggregate SEC filing candidates: `{len(merged_candidates)}`",
        f"- Aggregate SEC passage rows: `{len(merged_passages)}`",
        f"- Unreviewed triage rows: `{len(triage_rows)}`",
        f"- Evidence/extraction errors: `{len(evidence_errors)}` / `{len(passage_errors)}`",
        f"- Ledger canonical events: `{ledger['table_counts']['canonical_events']}`",
        f"- Ledger evidence rows: `{ledger['table_counts']['event_evidence']}`",
        "- Safety: D:/short read-only; no outcome-based ranking; no automatic labels; no trading.",
        "",
    ]
    args.report.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "batch_offset": offset,
        "batch_rows": len(batch),
        "new_candidates": len(new_candidates),
        "new_passages": len(new_passages),
        "next_offset": next_offset,
        "queue_exhausted": state_payload["queue_exhausted"],
        "batch_committed": batch_committed,
        "ledger_queue_rows": counts.queue_rows,
        "errors": len(evidence_errors) + len(passage_errors),
    }, sort_keys=True))
    print(f"REPORT={args.report}")
    return 0 if not evidence_errors and not passage_errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
