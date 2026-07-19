#!/usr/bin/env python3
"""Measure reviewed historical detector yield without changing any label."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from review_threads import EVENT_FAMILY_BY_TYPE, review_thread_assignments


ROOT = Path(__file__).resolve().parents[1]
FIELDS = [
    "group_scope",
    "group_name",
    "reviewed_rows",
    "review_threads",
    "verified_rows",
    "rejected_rows",
    "acceptance_rate_pct",
    "training_eligible_verified_rows",
    "a_or_higher_rows",
    "s_or_a_plus_plus_rows",
    "training_eligible_a_or_higher_rows",
    "training_eligible_s_or_a_plus_plus_rows",
    "linked_consequence_excluded",
    "false_positive_controls",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def precision_rows(adjudications: Iterable[dict[str, str]]) -> list[dict[str, str | int | float]]:
    rows = list(adjudications)
    thread_input = []
    for row in rows:
        family = EVENT_FAMILY_BY_TYPE.get(row.get("detected_event_type", ""), "unknown")
        thread_input.append(
            {
                "event_candidate_id": row.get("event_candidate_id", ""),
                "stable_id": row.get("stable_id", "") or row.get("event_candidate_id", ""),
                "event_date": row.get("event_date", ""),
                "event_family": family,
                "queue_rank": "0",
            }
        )
    thread_by_id = review_thread_assignments(thread_input)
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        detected_type = row.get("detected_event_type", "unknown") or "unknown"
        family = EVENT_FAMILY_BY_TYPE.get(detected_type, "unknown")
        groups[("event_family", family)].append(row)
        groups[("detected_event_type", detected_type)].append(row)

    output: list[dict[str, str | int | float]] = []
    for (scope, name), group in groups.items():
        verified = [row for row in group if row.get("label_status") == "verified"]
        rejected = [row for row in group if row.get("label_status") == "rejected"]
        training_eligible_verified = [
            row for row in verified if "linked_consequence" not in row.get("training_role", "")
        ]
        threads = {
            thread_by_id.get(row.get("event_candidate_id", ""))
            for row in group
            if thread_by_id.get(row.get("event_candidate_id", "")) is not None
        }
        output.append(
            {
                "group_scope": scope,
                "group_name": name,
                "reviewed_rows": len(group),
                "review_threads": len(threads),
                "verified_rows": len(verified),
                "rejected_rows": len(rejected),
                "acceptance_rate_pct": round(100 * len(verified) / len(group), 1) if group else 0.0,
                "training_eligible_verified_rows": len(training_eligible_verified),
                "a_or_higher_rows": sum(row.get("manual_grade") in {"S", "A++", "A"} for row in verified),
                "s_or_a_plus_plus_rows": sum(row.get("manual_grade") in {"S", "A++"} for row in verified),
                "training_eligible_a_or_higher_rows": sum(
                    row.get("manual_grade") in {"S", "A++", "A"}
                    for row in training_eligible_verified
                ),
                "training_eligible_s_or_a_plus_plus_rows": sum(
                    row.get("manual_grade") in {"S", "A++"}
                    for row in training_eligible_verified
                ),
                "linked_consequence_excluded": sum("linked_consequence" in row.get("training_role", "") for row in group),
                "false_positive_controls": sum(
                    row.get("label_status") == "rejected"
                    and any(
                        token in row.get("training_role", "")
                        for token in ("control", "mismatch", "price_only", "metric_only", "boilerplate")
                    )
                    for row in group
                ),
            }
        )
    return sorted(
        output,
        key=lambda row: (
            0 if row["group_scope"] == "event_family" else 1,
            -int(row["reviewed_rows"]),
            str(row["group_name"]),
        ),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adjudications",
        type=Path,
        default=ROOT / "reports" / "active_event_adjudications.csv",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=ROOT / "reports" / "detector_precision_by_type.csv",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "reports" / "detector_precision_latest.md",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    adjudications = read_csv(args.adjudications)
    rows = precision_rows(adjudications)
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Historical Detector Precision",
        "",
        f"Generated: `{generated_at}`",
        "",
        f"- Reviewed candidate rows: `{len(adjudications)}`",
        "- Acceptance means that manual review found a real event, not that the detector's original family or severity was exact.",
        "- Raw severity counts are descriptive. Training-eligible counts exclude linked price/consequence proxies so they cannot inflate hard labels.",
        "- This report is diagnostic only and cannot mutate labels, ranking features, alerts or trading state.",
        "",
        "| scope | group | reviewed | threads | verified | rejected | accept % | eligible verified | eligible A+ | eligible S/A++ | chain-excluded | controls |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['group_scope']} | {row['group_name']} | {row['reviewed_rows']} | "
            f"{row['review_threads']} | {row['verified_rows']} | {row['rejected_rows']} | "
            f"{row['acceptance_rate_pct']} | {row['training_eligible_verified_rows']} | "
            f"{row['training_eligible_a_or_higher_rows']} | "
            f"{row['training_eligible_s_or_a_plus_plus_rows']} | {row['linked_consequence_excluded']} | "
            f"{row['false_positive_controls']} |"
        )
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"rows={len(rows)} reviewed={len(adjudications)}")
    print(f"CSV={args.csv}")
    print(f"REPORT={args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
