from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl


NUMERIC_METRICS = [
    "event_day_close_to_close",
    "ret_1d",
    "ret_5d",
    "ret_21d",
    "ret_63d",
    "ret_126d",
    "ret_252d",
    "market_adj_ret_1d",
    "market_adj_ret_5d",
    "market_adj_ret_21d",
    "market_adj_ret_63d",
    "market_adj_ret_126d",
    "market_adj_ret_252d",
    "max_drawdown_21d",
    "max_drawdown_63d",
    "max_drawdown_252d",
    "max_favorable_excursion_21d",
    "max_favorable_excursion_63d",
    "max_favorable_excursion_252d",
]

OUTPUT_COLUMNS = [
    "event_candidate_id",
    "stable_id",
    "ticker_at_event",
    "event_date",
    "event_trade_date",
    "provider",
    "benchmark_ticker",
    "metric_name",
    "metric_value",
    "metric_value_type",
    "metric_scope",
    "allowed_for_discovery_rank",
    "allowed_as_model_feature",
]


def queue_event_ids(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row["event_candidate_id"] for row in csv.DictReader(handle)]


def build_market_outcomes(queue_path: Path, event_returns_path: Path) -> tuple[pl.DataFrame, int]:
    event_ids = queue_event_ids(queue_path)
    base_columns = [
        "event_id",
        "stable_id",
        "ticker_at_event",
        "event_date",
        "event_trade_date",
        "benchmark_ticker",
        "delist_within_1y",
        *NUMERIC_METRICS,
    ]
    source = (
        pl.scan_parquet(event_returns_path)
        .filter(pl.col("event_id").is_in(event_ids))
        .select(base_columns)
        .unique(subset=["event_id"], keep="first")
        .collect()
    )
    matched_events = source["event_id"].n_unique() if source.height else 0
    numeric = (
        source.unpivot(
            index=[
                "event_id",
                "stable_id",
                "ticker_at_event",
                "event_date",
                "event_trade_date",
                "benchmark_ticker",
            ],
            on=NUMERIC_METRICS,
            variable_name="metric_name",
            value_name="metric_value",
        )
        .filter(pl.col("metric_value").is_not_null())
        .with_columns(
            pl.lit("float").alias("metric_value_type"),
            pl.col("metric_value").cast(pl.Utf8),
        )
    )
    boolean = (
        source.select(
            [
                "event_id",
                "stable_id",
                "ticker_at_event",
                "event_date",
                "event_trade_date",
                "benchmark_ticker",
                pl.lit("delist_within_1y").alias("metric_name"),
                pl.col("delist_within_1y").cast(pl.Utf8).alias("metric_value"),
                pl.lit("boolean").alias("metric_value_type"),
            ]
        )
        .filter(pl.col("metric_value").is_not_null())
    )
    output = (
        pl.concat([numeric, boolean], how="diagonal_relaxed")
        .rename({"event_id": "event_candidate_id"})
        .with_columns(
            pl.lit("sharadar").alias("provider"),
            pl.lit("post_event_audit_only").alias("metric_scope"),
            pl.lit("false").alias("allowed_for_discovery_rank"),
            pl.lit("false").alias("allowed_as_model_feature"),
        )
        .select(OUTPUT_COLUMNS)
        .sort(["event_date", "event_candidate_id", "metric_name"], descending=[True, False, False])
    )
    return output, matched_events


def write_outputs(
    frame: pl.DataFrame,
    *,
    matched_events: int,
    queue_events: int,
    output_dir: Path,
    report_dir: Path,
    source_path: Path,
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "active_event_market_outcomes.csv"
    manifest_path = output_dir / "active_event_market_outcomes_manifest.json"
    report_path = report_dir / "active_event_market_outcomes_latest.md"
    frame.write_csv(csv_path)
    generated_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": "active-event-market-outcomes-v1",
        "generated_at": generated_at,
        "source": str(source_path.resolve()),
        "queue_events": queue_events,
        "matched_events": matched_events,
        "metric_rows": frame.height,
        "invariants": {
            "metric_scope": "post_event_audit_only",
            "allowed_for_discovery_rank": False,
            "allowed_as_model_feature": False,
            "provider_read_only": True,
            "live_trading_allowed": False,
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = frame.group_by("metric_name").len().sort("metric_name")
    lines = [
        "# Active Event Market Outcomes",
        "",
        f"Generated: `{generated_at}`",
        "",
        f"- Queue events: `{queue_events}`",
        f"- Matched Sharadar event-return rows: `{matched_events}`",
        f"- Long-form metric rows: `{frame.height}`",
        "- Scope: post-event audit only.",
        "- These values are forbidden in event discovery ranking and current-event model inputs.",
        "",
        "## Metric Coverage",
        "",
        "| metric | rows |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {row[0]} | {row[1]} |" for row in counts.iter_rows())
    lines.extend(
        [
            "",
            "The metrics may be used for historical outcome analysis after an event is frozen, "
            "not to relabel the event cause or prove causal impact.",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, report_path, manifest_path


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Export Sharadar post-event metrics for active queue rows.")
    parser.add_argument(
        "--queue", type=Path, default=root / "data/research/active_event_research_queue.csv"
    )
    parser.add_argument(
        "--event-returns", type=Path, default=Path("D:/short/data/curated/event_returns.parquet")
    )
    parser.add_argument("--output-dir", type=Path, default=root / "data/research")
    parser.add_argument("--report-dir", type=Path, default=root / "reports")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    event_ids = queue_event_ids(args.queue)
    frame, matched_events = build_market_outcomes(args.queue, args.event_returns)
    paths = write_outputs(
        frame,
        matched_events=matched_events,
        queue_events=len(event_ids),
        output_dir=args.output_dir,
        report_dir=args.report_dir,
        source_path=args.event_returns,
    )
    for path in paths:
        print(path)
    print(f"matched_events={matched_events} metric_rows={frame.height}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
