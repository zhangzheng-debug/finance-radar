#!/usr/bin/env python3
"""Build a unified, non-trading quality snapshot for live and historical research."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from review_threads import EVENT_FAMILY_BY_TYPE, review_thread_assignments


ROOT = Path(__file__).resolve().parents[1]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def pct(numerator: int, denominator: int) -> float:
    return round(100 * numerator / denominator, 1) if denominator else 0.0


def build_snapshot(
    live_triage: list[dict[str, str]],
    historical_queue: list[dict[str, str]],
    historical_passages: list[dict[str, str]],
    historical_triage: list[dict[str, str]],
    adjudications: list[dict[str, str]],
) -> dict[str, Any]:
    live_primary = sum(row.get("evidence_readiness") == "primary_text_ready" for row in live_triage)
    live_high = sum(int(row.get("review_score") or 0) >= 80 for row in live_triage)

    passage_events = {row.get("event_candidate_id", "") for row in historical_passages}
    passage_events.discard("")
    matched_events = {
        row.get("event_candidate_id", "")
        for row in historical_passages
        if row.get("passage_status") == "candidate_passage"
    }
    matched_events.discard("")
    reviewed_ids = {row.get("event_candidate_id", "") for row in adjudications}
    reviewed_ids.discard("")
    queue_by_id = {row.get("event_candidate_id", ""): row for row in historical_queue}
    thread_input = list(historical_queue)
    for row in adjudications:
        event_id = row.get("event_candidate_id", "")
        family = EVENT_FAMILY_BY_TYPE.get(row.get("detected_event_type", ""), "")
        if (
            event_id
            and event_id not in queue_by_id
            and row.get("stable_id")
            and row.get("event_date")
            and family
        ):
            thread_input.append(
                {
                    "event_candidate_id": event_id,
                    "stable_id": row["stable_id"],
                    "event_date": row["event_date"],
                    "event_family": family,
                    "queue_rank": "0",
                }
            )
    thread_by_id = review_thread_assignments(thread_input)
    review_groups = {
        thread_by_id[event_id]
        for event_id in queue_by_id
        if event_id in thread_by_id
    }
    matched_groups = {
        thread_by_id[event_id]
        for event_id in matched_events
        if event_id in queue_by_id
    }
    reviewed_groups = {
        thread_by_id[event_id]
        for event_id in reviewed_ids
        if event_id in thread_by_id and thread_by_id[event_id] in review_groups
    }
    queue_passage_events = passage_events & set(queue_by_id)
    queue_matched_events = matched_events & set(queue_by_id)
    adjudicated_queue_rows = sum(
        1
        for event_id in queue_by_id
        if event_id in thread_by_id and thread_by_id[event_id] in reviewed_groups
    )
    verified = [row for row in adjudications if row.get("label_status") == "verified"]
    rejected = [row for row in adjudications if row.get("label_status") == "rejected"]
    hard_labels = [
        row
        for row in verified
        if row.get("manual_grade") in {"S", "A++"}
        and "linked_consequence" not in row.get("training_role", "")
    ]

    reviewed_by_type: dict[str, dict[str, int]] = {}
    for row in adjudications:
        event_type = row.get("detected_event_type") or "unknown"
        bucket = reviewed_by_type.setdefault(event_type, {"reviewed": 0, "verified": 0, "rejected": 0})
        bucket["reviewed"] += 1
        if row.get("label_status") in {"verified", "rejected"}:
            bucket[row["label_status"]] += 1

    historical_complete = bool(review_groups) and reviewed_groups == review_groups
    terminal_live_types = {"offering_or_dilution", "senior_unsecured_debt_financing"}
    live_waiting_terminal_fact = bool(live_triage) and all(
        row.get("event_type") in terminal_live_types
        and str(row.get("next_action") or "").startswith("confirm_")
        for row in live_triage
    )
    if historical_complete and live_waiting_terminal_fact:
        priority = [
            {
                "rank": 1,
                "work": "monitor_terminal_primary_evidence",
                "why": "The remaining live candidates require future closing evidence, not another interpretation of existing text.",
                "measure": f"live_waiting_for_terminal_fact={len(live_triage)}",
            },
            {
                "rank": 2,
                "work": "expand_auditable_candidate_generation",
                "why": "The current historical review universe is fully adjudicated, so the next batch must add candidates without future-outcome ranking.",
                "measure": f"historical_review_threads_adjudicated={len(reviewed_groups)}/{len(review_groups)}",
            },
            {
                "rank": 3,
                "work": "measure_false_positive_controls_by_family",
                "why": "New discovery should be evaluated against existing rejected controls before model training.",
                "measure": f"reviewed_rejected={len(rejected)}/{len(adjudications)}",
            },
        ]
        interpretation = (
            "Basic connectivity and the current review backlog are no longer the bottleneck. "
            "Three live candidates are blocked on future closing facts, while the historical review universe is exhausted. "
            "The next bottleneck is adding new auditable candidates and measuring source-family precision."
        )
    else:
        priority = [
            {
                "rank": 1,
                "work": "adjudicate_primary_evidence",
                "why": "Primary evidence exists, but review throughput is below discovery throughput.",
                "measure": f"historical_review_threads_adjudicated={len(reviewed_groups)}/{len(review_groups)}; live_pending={len(live_triage)}",
            },
            {
                "rank": 2,
                "work": "expand_primary_evidence_coverage",
                "why": "Candidates without a relevant passage cannot be promoted or rejected safely.",
                "measure": f"historical_keyword_passage_threads={len(matched_groups)}/{len(review_groups)}",
            },
            {
                "rank": 3,
                "work": "measure_false_positive_controls_by_family",
                "why": "Mergers, redemptions and stale price-cause matches can look severe but are not equity-death labels.",
                "measure": f"reviewed_rejected={len(rejected)}/{len(adjudications)}",
            },
        ]
        interpretation = (
            "The bottleneck is no longer basic source connectivity. It is converting primary evidence into reviewed, "
            "auditable labels while preserving false-positive controls. Discovery should continue in the background, "
            "but reviewer throughput and evidence coverage are the gating metrics."
        )

    return {
        "schema_version": "research-quality-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safety": {
            "historical_outcomes_used_for_ranking": False,
            "automatic_label_promotion": False,
            "trading_allowed": False,
        },
        "live": {
            "pending_review": len(live_triage),
            "primary_text_ready": live_primary,
            "primary_text_ready_pct": pct(live_primary, len(live_triage)),
            "score_80_plus": live_high,
            "event_types": dict(Counter(row.get("event_type") or "unknown" for row in live_triage)),
        },
        "historical": {
            "queue_rows": len(historical_queue),
            "review_threads": len(review_groups),
            "events_with_any_sec_passage_row": len(queue_passage_events),
            "events_with_keyword_passage": len(queue_matched_events),
            "keyword_passage_coverage_pct": pct(len(queue_matched_events), len(historical_queue)),
            "review_threads_with_keyword_passage": len(matched_groups),
            "review_thread_keyword_coverage_pct": pct(len(matched_groups), len(review_groups)),
            "unreviewed_triage_rows": len(historical_triage),
            "adjudicated": len(reviewed_ids),
            "adjudicated_queue_rows": adjudicated_queue_rows,
            "adjudicated_pct": pct(adjudicated_queue_rows, len(historical_queue)),
            "adjudicated_review_threads": len(reviewed_groups),
            "adjudicated_review_thread_pct": pct(len(reviewed_groups), len(review_groups)),
            "verified": len(verified),
            "rejected": len(rejected),
            "verified_share_of_reviewed_pct": pct(len(verified), len(adjudications)),
            "hard_labels_s_or_a_plus_plus": len(hard_labels),
            "reviewed_by_detected_type": reviewed_by_type,
        },
        "priority": priority,
        "interpretation": interpretation,
    }


def write_report(snapshot: dict[str, Any], output_json: Path, output_md: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    live = snapshot["live"]
    hist = snapshot["historical"]
    lines = [
        "# Unified Research Quality Snapshot",
        "",
        f"Generated: `{snapshot['generated_at']}`",
        "",
        "## What matters now",
        "",
    ]
    for item in snapshot["priority"]:
        lines.append(f"{item['rank']}. **{item['work']}** - {item['why']} `{item['measure']}`")
    lines.extend(
        [
            "",
            "## Live event stream",
            "",
            f"- Pending manual review: `{live['pending_review']}`",
            f"- Primary text ready: `{live['primary_text_ready']}` / `{live['pending_review']}` ({live['primary_text_ready_pct']}%)",
            f"- Review score 80+: `{live['score_80_plus']}`",
            "",
            "## Historical Sharadar + SEC research",
            "",
            f"- Queue: `{hist['queue_rows']}`",
            f"- Unique review threads after sibling-detector collapse: `{hist['review_threads']}`",
            f"- Review threads with keyword evidence passage: `{hist['review_threads_with_keyword_passage']}` / `{hist['review_threads']}` ({hist['review_thread_keyword_coverage_pct']}%)",
            f"- Adjudicated review threads: `{hist['adjudicated_review_threads']}` / `{hist['review_threads']}` ({hist['adjudicated_review_thread_pct']}%)",
            f"- Verified / rejected: `{hist['verified']}` / `{hist['rejected']}`",
            f"- S or A++ labels after review: `{hist['hard_labels_s_or_a_plus_plus']}`",
            "",
            "## Interpretation",
            "",
            snapshot["interpretation"],
            "",
            "## Safety invariants",
            "",
            "- Post-event market outcomes are audit-only and are not ranking inputs.",
            "- No automatic label promotion.",
            "- No trading or order path.",
        ]
    )
    output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-json", type=Path, default=ROOT / "data/research/research_quality_snapshot.json")
    parser.add_argument("--output-md", type=Path, default=ROOT / "reports/research_quality_latest.md")
    args = parser.parse_args()
    snapshot = build_snapshot(
        read_csv(ROOT / "data/research/live_review_triage.csv"),
        read_csv(ROOT / "data/research/active_event_research_queue.csv"),
        read_csv(ROOT / "data/research/active_event_sec_evidence_passages.csv"),
        read_csv(ROOT / "data/research/active_event_review_triage.csv"),
        read_csv(ROOT / "reports/active_event_adjudications.csv"),
    )
    write_report(snapshot, args.output_json, args.output_md)
    print(json.dumps({"live_pending": snapshot["live"]["pending_review"], "historical_adjudicated": snapshot["historical"]["adjudicated"], "historical_keyword_passages": snapshot["historical"]["events_with_keyword_passage"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
