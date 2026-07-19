from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from review_threads import (
    EVENT_FAMILY_BY_TYPE,
    expanded_completed_thread_keys,
    review_thread_assignments,
)


ROOT = Path(__file__).resolve().parents[1]


PRIORITY_BY_EVENT_TYPE: dict[str, int] = {
    "bankruptcy_liquidation": 100,
    "delisted": 90,
    "voluntarydelisting": 85,
    "negative_equity": 75,
    "cash_short_debt_stress": 72,
    "revenue_collapse_yoy": 68,
    "free_cash_flow_turn_negative": 60,
    "gross_margin_collapse": 58,
    "interest_coverage_below_1": 55,
    "reverse_split": 45,
    "volume_crash": 40,
    "one_day_crash": 38,
    "five_day_crash": 35,
    "twenty_one_day_crash": 32,
}
POST_EVENT_OUTCOME_COLUMNS = {
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
    "delist_within_1y",
    "recovery_to_pre_event_price_126d",
}

QUEUE_COLUMNS = [
    "queue_rank",
    "family_rank",
    "research_queue_id",
    "event_candidate_id",
    "stable_id",
    "ticker_at_event",
    "identity_review_flag",
    "identity_review_reason",
    "company_name",
    "exchange",
    "cik",
    "sec_filings_url",
    "sector",
    "industry",
    "event_date",
    "event_family",
    "event_type",
    "detection_rule",
    "detection_value",
    "severity_raw",
    "source_table",
    "priority_score",
    "selection_strategy",
    "provisional_grade_cap",
    "required_evidence",
    "evidence_search_query",
    "selection_status",
    "allowed_use",
]


@dataclass(frozen=True)
class DiscoveryResult:
    queue: pl.DataFrame
    queue_path: Path
    report_path: Path
    manifest_path: Path


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def completed_review_event_ids(
    queue_rows: list[dict[str, str]], adjudication_rows: list[dict[str, str]]
) -> set[str]:
    """Return all members of review threads that already have an adjudication."""
    assignments = review_thread_assignments(queue_rows)
    adjudicated = {
        str(row.get("event_candidate_id") or "")
        for row in adjudication_rows
        if row.get("event_candidate_id")
    }
    completed_threads = {
        assignments[event_id]
        for event_id in adjudicated
        if event_id in assignments
    }
    return {
        event_id
        for event_id, thread in assignments.items()
        if thread in completed_threads
    }


def completed_review_threads(
    queue_rows: list[dict[str, str]], adjudication_rows: list[dict[str, str]]
) -> set[tuple[str, str, str]]:
    assignments = review_thread_assignments(queue_rows)
    adjudicated = {
        str(row.get("event_candidate_id") or "")
        for row in adjudication_rows
        if row.get("event_candidate_id")
    }
    from_queue = {
        assignments[event_id]
        for event_id in adjudicated
        if event_id in assignments
    }
    from_adjudications = {
        (
            str(row.get("stable_id") or ""),
            str(row.get("event_date") or ""),
            EVENT_FAMILY_BY_TYPE.get(str(row.get("detected_event_type") or ""), ""),
        )
        for row in adjudication_rows
        if row.get("stable_id")
        and row.get("event_date")
        and EVENT_FAMILY_BY_TYPE.get(str(row.get("detected_event_type") or ""))
    }
    return from_queue | from_adjudications


def update_completed_registry(
    registry_path: Path,
    *,
    queue_path: Path,
    adjudications_path: Path,
) -> set[str]:
    existing = {
        str(row.get("event_candidate_id") or "")
        for row in read_csv_rows(registry_path)
        if row.get("event_candidate_id")
    }
    adjudication_rows = read_csv_rows(adjudications_path)
    adjudicated_ids = {
        str(row.get("event_candidate_id") or "")
        for row in adjudication_rows
        if row.get("event_candidate_id")
    }
    newly_completed = completed_review_event_ids(
        read_csv_rows(queue_path), adjudication_rows
    )
    combined = existing | adjudicated_ids | newly_completed
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with registry_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["event_candidate_id"])
        writer.writeheader()
        writer.writerows(
            {"event_candidate_id": event_id} for event_id in sorted(combined)
        )
    return combined


def update_completed_thread_registry(
    registry_path: Path,
    *,
    queue_path: Path,
    adjudications_path: Path,
) -> set[tuple[str, str, str]]:
    existing = {
        (
            str(row.get("stable_id") or ""),
            str(row.get("thread_date") or ""),
            str(row.get("event_family") or ""),
        )
        for row in read_csv_rows(registry_path)
        if row.get("stable_id") and row.get("event_family")
    }
    newly_completed = completed_review_threads(
        read_csv_rows(queue_path), read_csv_rows(adjudications_path)
    )
    combined = existing | newly_completed
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with registry_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = ["stable_id", "thread_date", "event_family"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(
            {
                "stable_id": stable_id,
                "thread_date": thread_date,
                "event_family": event_family,
            }
            for stable_id, thread_date, event_family in sorted(combined)
        )
    return combined


def _require_file(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Active event research config must be a JSON object")
    return payload


def _security_filter() -> pl.Expr:
    security_text = pl.concat_str(
        [
            pl.col("category").fill_null(""),
            pl.lit(" "),
            pl.col("security_type").fill_null(""),
        ]
    ).str.to_lowercase()
    common_or_adr = security_text.str.contains("common|adr|ads|depositary")
    excluded = security_text.str.contains("preferred|warrant|unit|fund|etf|note|bond")
    ticker = pl.col("ticker_at_event").fill_null("").str.to_uppercase()
    non_common_ticker_pattern = ticker.str.contains(
        r"(?:-P$|-PR[A-Z]?$|\.P[A-Z]?$|\.U$|-U$|/U$|^[A-Z]{4}U$|\.WS$|-WS$|/WS$|\.W$|-W$|/W$|\.R$|-R$|/R$|^[A-Z]{4}[WRT]$)"
    )
    return common_or_adr & ~excluded & ~non_common_ticker_pattern


def _possible_post_event_otc_alias() -> pl.Expr:
    """Flag common OTC alias patterns without deleting or rewriting the candidate.

    Five-letter U.S. OTC symbols ending in F, Y, or Q frequently represent a
    foreign ordinary share, ADR, or bankruptcy-stage quotation that appeared
    after the original exchange event.  The pattern is a routing hint only:
    event-time identity still requires primary-source confirmation.
    """

    return (
        pl.col("ticker_at_event")
        .fill_null("")
        .str.to_uppercase()
        .str.contains(r"^[A-Z]{4}[FQY]$")
    )


def _fundamental_semantic_filter() -> pl.Expr:
    """Reject known ratio artifacts before they consume primary-evidence review."""

    event_type = pl.col("event_type")
    metric = pl.col("detection_value").cast(pl.Float64, strict=False)
    invalid_revenue_yoy = (event_type == "revenue_collapse_yoy") & (metric < -1.0)
    unstable_margin = (event_type == "gross_margin_collapse") & (metric < -2.0)
    unseasonal_fcf = (event_type == "free_cash_flow_turn_negative") & pl.col(
        "detection_rule"
    ).fill_null("").str.contains("previous quarter")
    incompatible_cash_ratio = (event_type == "cash_short_debt_stress") & (
        (pl.col("industry").fill_null("") == "Shell Companies")
        | pl.col("sector").fill_null("").is_in(["Financial Services", "Utilities"])
    )
    return ~(invalid_revenue_yoy | unstable_margin | unseasonal_fcf | incompatible_cash_ratio)


def _selection_strategy() -> pl.Expr:
    return (
        pl.when(_possible_post_event_otc_alias())
        .then(pl.lit("event_time_identity_review"))
        .when(pl.col("event_family") == "price_crash")
        .then(pl.lit("price_dislocation_evidence_search"))
        .when(pl.col("event_family") == "fundamental_shock")
        .then(pl.lit("point_in_time_fundamental_review"))
        .otherwise(pl.lit("corporate_action_evidence_review"))
        .alias("selection_strategy")
    )


def _provisional_grade_cap() -> pl.Expr:
    return (
        pl.when(pl.col("event_type") == "bankruptcy_liquidation")
        .then(pl.lit("A++_candidate"))
        .when(pl.col("event_type").is_in(["delisted", "voluntarydelisting"]))
        .then(pl.lit("A_candidate"))
        .when(pl.col("event_family") == "fundamental_shock")
        .then(pl.lit("A_candidate"))
        .when(pl.col("event_type") == "reverse_split")
        .then(pl.lit("B_candidate"))
        .when(pl.col("event_family") == "price_crash")
        .then(pl.lit("C_price_only"))
        .otherwise(pl.lit("B_candidate"))
        .alias("provisional_grade_cap")
    )


def _required_evidence() -> pl.Expr:
    return (
        pl.when(pl.col("event_type") == "bankruptcy_liquidation")
        .then(pl.lit("SEC 8-K/10-K, court docket, restructuring or cancellation terms"))
        .when(pl.col("event_type").is_in(["delisted", "voluntarydelisting"]))
        .then(pl.lit("exchange notice plus SEC filing; determine cause and old-common treatment"))
        .when(pl.col("event_type") == "reverse_split")
        .then(pl.lit("SEC corporate-action filing; do not infer fraud or equity death"))
        .when(pl.col("event_family") == "fundamental_shock")
        .then(pl.lit("point-in-time SEC filing and management disclosure available on event date"))
        .when(pl.col("event_family") == "price_crash")
        .then(pl.lit("contemporaneous SEC/regulator/company evidence explaining the dislocation"))
        .otherwise(pl.lit("primary-source evidence plus independent corroboration"))
        .alias("required_evidence")
    )


def _priority_score() -> pl.Expr:
    event_base = pl.col("event_type").replace_strict(
        PRIORITY_BY_EVENT_TYPE,
        default=25,
        return_dtype=pl.Int64,
    )
    severity_bonus = (
        pl.col("severity_raw").cast(pl.Float64, strict=False).fill_null(0.0).clip(0.0, 3.0) * 5.0
    )
    source_bonus = (
        pl.when(pl.col("source_table") == "ACTIONS")
        .then(10)
        .when(pl.col("source_table") == "SF1")
        .then(5)
        .otherwise(0)
    )
    identity_bonus = pl.when(_possible_post_event_otc_alias()).then(12).otherwise(0)
    return (event_base + severity_bonus + source_bonus + identity_bonus).round(3).alias(
        "priority_score"
    )


def build_active_queue(
    short_root: Path,
    *,
    start_date: str,
    end_date: str | None,
    per_family: int,
    max_total: int,
    common_equity_only: bool = True,
    exclude_reviewed: bool = True,
    additional_excluded_ids: set[str] | None = None,
    additional_excluded_threads: set[tuple[str, str, str]] | None = None,
) -> pl.DataFrame:
    if per_family <= 0 or max_total <= 0:
        raise ValueError("per_family and max_total must be positive")

    curated = short_root / "data" / "curated"
    candidates_path = _require_file(curated / "event_candidates.parquet")
    security_path = _require_file(curated / "security_master.parquet")
    label_book_path = curated / "event_label_book_v0.parquet"

    candidate_columns = [
        "event_candidate_id",
        "stable_id_match_status",
        "stable_id",
        "security_master_id",
        "ticker_at_event",
        "category",
        "security_type",
        "event_date",
        "event_family",
        "event_type",
        "detection_rule",
        "detection_value",
        "severity_raw",
        "source_table",
        "label_status",
    ]
    candidates = pl.scan_parquet(candidates_path).select(candidate_columns).filter(
        (pl.col("stable_id_match_status") == "matched")
        & pl.col("stable_id").is_not_null()
        & (pl.col("label_status") == "candidate")
        & (pl.col("event_date") >= start_date)
    )
    if end_date:
        candidates = candidates.filter(pl.col("event_date") <= end_date)
    if common_equity_only:
        candidates = candidates.filter(_security_filter())

    if exclude_reviewed and label_book_path.is_file():
        reviewed_ids = (
            pl.scan_parquet(label_book_path)
            .select(pl.col("event_id").alias("event_candidate_id"))
            .filter(pl.col("event_candidate_id").is_not_null())
            .unique()
        )
        candidates = candidates.join(reviewed_ids, on="event_candidate_id", how="anti")
    if additional_excluded_ids:
        candidates = candidates.filter(
            ~pl.col("event_candidate_id").is_in(sorted(additional_excluded_ids))
        )
    if additional_excluded_threads:
        additional_excluded_threads = expanded_completed_thread_keys(
            additional_excluded_threads
        )
        completed_threads = pl.DataFrame(
            [
                {
                    "stable_id": stable_id,
                    "event_date": thread_date,
                    "event_family": event_family,
                }
                for stable_id, thread_date, event_family in sorted(additional_excluded_threads)
            ]
        ).lazy()
        candidates = candidates.join(
            completed_threads,
            on=["stable_id", "event_date", "event_family"],
            how="anti",
        )

    security = (
        pl.scan_parquet(security_path)
        .select(
            [
                "security_master_id",
                "company_name",
                "exchange",
                "sector",
                "industry",
                "secfilings",
            ]
        )
        .with_columns(
            pl.col("secfilings").str.extract(r"CIK=(\d+)", 1).alias("cik"),
            pl.col("secfilings").alias("sec_filings_url"),
        )
        .drop("secfilings")
        .unique(subset=["security_master_id"], keep="first")
    )

    enriched = (
        candidates.join(security, on="security_master_id", how="left")
        .filter(_fundamental_semantic_filter())
        .with_columns(
            _possible_post_event_otc_alias().alias("identity_review_flag"),
            pl.when(_possible_post_event_otc_alias())
            .then(pl.lit("possible_post_event_OTC_alias_suffix_F_Q_or_Y"))
            .otherwise(pl.lit(""))
            .alias("identity_review_reason"),
            _priority_score(),
            _selection_strategy(),
            _provisional_grade_cap(),
            _required_evidence(),
            pl.concat_str(
                [
                    pl.col("ticker_at_event"),
                    pl.lit(" "),
                    pl.col("company_name").fill_null(""),
                    pl.lit(" "),
                    pl.col("event_date"),
                    pl.lit(" "),
                    pl.col("event_type"),
                    pl.lit(" CIK "),
                    pl.col("cik").fill_null(""),
                    pl.lit(" SEC filing regulator court"),
                ]
            ).alias("evidence_search_query"),
            pl.when(_possible_post_event_otc_alias())
            .then(pl.lit("needs_event_time_identity_review"))
            .otherwise(pl.lit("needs_primary_evidence"))
            .alias("selection_status"),
            pl.lit("manual_research_priority_only").alias("allowed_use"),
        )
        .with_columns(
            pl.when(pl.col("identity_review_flag"))
            .then(
                pl.concat_str(
                    [
                        pl.lit(
                            "event-time exchange ticker, exact legal/effective date, post-event OTC venue and underlying-security continuity; do not backfill OTC alias; "
                        ),
                        pl.col("required_evidence"),
                    ]
                )
            )
            .otherwise(pl.col("required_evidence"))
            .alias("required_evidence")
        )
        .sort(
            ["priority_score", "event_date", "event_candidate_id"],
            descending=[True, True, False],
        )
        .unique(
            subset=["stable_id", "event_date", "event_family", "event_type"],
            keep="first",
            maintain_order=True,
        )
    )

    balanced = (
        enriched.sort(
            ["event_family", "event_type", "priority_score", "event_date", "event_candidate_id"],
            descending=[False, False, True, True, False],
        )
        .with_columns(
            pl.col("event_type").n_unique().over("event_family").alias("__family_type_count"),
            pl.col("event_candidate_id")
            .cum_count()
            .over(["event_family", "event_type"])
            .alias("__event_type_rank"),
        )
        .with_columns(
            (
                (pl.lit(per_family) + pl.col("__family_type_count") - 1)
                // pl.col("__family_type_count")
            ).alias("__event_type_quota")
        )
        .filter(pl.col("__event_type_rank") <= pl.col("__event_type_quota"))
        .sort(
            ["event_family", "priority_score", "event_date", "event_candidate_id"],
            descending=[False, True, True, False],
        )
        .with_columns(
            pl.col("event_candidate_id")
            .cum_count()
            .over("event_family")
            .alias("family_rank")
        )
        .filter(pl.col("family_rank") <= per_family)
        .sort(
            ["family_rank", "priority_score", "event_date", "event_candidate_id"],
            descending=[False, True, True, False],
        )
        .head(max_total)
        .collect()
        .with_row_index("queue_rank", offset=1)
        .with_columns(
            pl.concat_str(
                [pl.lit("RADAR-HIST-"), pl.col("event_candidate_id")]
            ).alias("research_queue_id")
        )
        .select(QUEUE_COLUMNS)
    )

    leaked = POST_EVENT_OUTCOME_COLUMNS.intersection(balanced.columns)
    if leaked:
        raise RuntimeError(f"Post-event outcome columns leaked into queue: {sorted(leaked)}")
    return balanced


def _markdown_table(frame: pl.DataFrame, columns: list[str], limit: int = 20) -> list[str]:
    view = frame.select(columns).head(limit)
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in view.iter_rows(named=True):
        values = [str(row[column] if row[column] is not None else "").replace("|", "\\|") for column in columns]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def write_outputs(
    queue: pl.DataFrame,
    *,
    short_root: Path,
    output_dir: Path,
    start_date: str,
    end_date: str | None,
    per_family: int,
    max_total: int,
    completed_registry_rows: int = 0,
) -> DiscoveryResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir = output_dir.parents[1] / "reports" if output_dir.name == "research" else output_dir
    report_dir.mkdir(parents=True, exist_ok=True)

    queue_path = output_dir / "active_event_research_queue.csv"
    manifest_path = output_dir / "active_event_research_manifest.json"
    report_path = report_dir / "active_event_research_latest.md"
    queue.write_csv(queue_path)

    generated_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": "active-event-research-v1",
        "generated_at": generated_at,
        "short_research_root": str(short_root.resolve()),
        "source_candidates": str((short_root / "data/curated/event_candidates.parquet").resolve()),
        "source_security_master": str((short_root / "data/curated/security_master.parquet").resolve()),
        "source_label_book": str((short_root / "data/curated/event_label_book_v0.parquet").resolve()),
        "selection": {
            "start_date": start_date,
            "end_date": end_date,
            "per_family": per_family,
            "max_total": max_total,
            "rows": queue.height,
            "completed_registry_rows": completed_registry_rows,
        },
        "invariants": {
            "label_status": "candidate_only",
            "stable_id": "matched_only",
            "price_only_can_assign_severe_grade": False,
            "post_event_outcomes_used_for_ranking": False,
            "reviewed_event_ids_excluded": True,
            "family_and_event_type_balanced": True,
            "fundamental_semantic_artifacts_excluded": True,
            "five_letter_otc_aliases_routed_to_identity_review_not_auto_rejected": True,
            "live_trading_allowed": False,
        },
        "ranking_inputs": [
            "event_type",
            "severity_raw",
            "source_table",
            "possible_post_event_otc_alias",
            "event_date_as_tiebreaker",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    by_family = queue.group_by("event_family").len().sort("len", descending=True)
    by_strategy = queue.group_by("selection_strategy").len().sort("len", descending=True)
    report_lines = [
        "# Active Event Research Queue",
        "",
        f"Generated: `{generated_at}`",
        "",
        f"- Queue rows: `{queue.height}`",
        f"- Historical window: `{start_date}` to `{end_date or 'latest available'}`",
        f"- Source research workspace: `{short_root.resolve()}`",
        "- Purpose: keep a research backlog even when the live-news stream is quiet.",
        "- Status: candidate discovery only; no label mutation, model training, trading, or recommendation.",
        "- Ranking uses only event metadata available at the candidate date; no post-event return or drawdown is used.",
        "",
        "## Queue Policy",
        "",
        "1. Corporate actions and point-in-time fundamentals create evidence-review candidates.",
        "2. Price crashes create evidence-search candidates only and remain capped at `C_price_only`.",
        "3. S/A++ requires primary evidence of truth death, legality death, or common-equity death.",
        "4. Reviewed event IDs, unmatched securities, and non-common-equity instruments are excluded.",
        "5. Family and within-family event-type quotas prevent one metadata category from dominating the queue.",
        "6. Known semantic artifacts are excluded: revenue YoY below -100% with positive current revenue, gross-margin deltas below -200pp, previous-quarter FCF turns, and generic cash/debt ratios for SPACs, financials, and utilities.",
        "7. Five-letter tickers ending in F, Q, or Y remain candidates but are routed to event-time identity review; the detector never rewrites the ticker or assigns a final label.",
        "",
        "## By Event Family",
        "",
        *_markdown_table(by_family, ["event_family", "len"], limit=50),
        "",
        "## By Selection Strategy",
        "",
        *_markdown_table(by_strategy, ["selection_strategy", "len"], limit=50),
        "",
        "## Highest-Priority Evidence Reviews",
        "",
        *_markdown_table(
            queue,
            [
                "queue_rank",
                "family_rank",
                "ticker_at_event",
                "identity_review_flag",
                "event_date",
                "event_family",
                "event_type",
                "priority_score",
                "provisional_grade_cap",
            ],
            limit=25,
        ),
        "",
        "## Required Next Action",
        "",
        "For each row, retrieve contemporaneous SEC, court, regulator, exchange, or company evidence. "
        "Only a separate evidence-review step may promote `candidate` to `verified`, `weak`, or `rejected`.",
        "",
    ]
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    return DiscoveryResult(queue, queue_path, report_path, manifest_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a family-balanced active historical event research queue from D:/short."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config" / "active_event_research.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "research",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = _load_config(args.config)
    short_root = Path(config["short_research_root"])
    start_date = str(config.get("start_date") or "1900-01-01")
    end_date = config.get("end_date")
    per_family = int(config.get("per_family", 30))
    max_total = int(config.get("max_total", 150))
    queue_path = args.output_dir / "active_event_research_queue.csv"
    completed_ids = update_completed_registry(
        args.output_dir / "active_event_completed_candidates.csv",
        queue_path=queue_path,
        adjudications_path=ROOT / "reports" / "active_event_adjudications.csv",
    )
    completed_threads = update_completed_thread_registry(
        args.output_dir / "active_event_completed_threads.csv",
        queue_path=queue_path,
        adjudications_path=ROOT / "reports" / "active_event_adjudications.csv",
    )

    queue = build_active_queue(
        short_root,
        start_date=start_date,
        end_date=end_date,
        per_family=per_family,
        max_total=max_total,
        common_equity_only=bool(config.get("common_equity_only", True)),
        exclude_reviewed=bool(config.get("exclude_reviewed", True)),
        additional_excluded_ids=completed_ids,
        additional_excluded_threads=completed_threads,
    )
    result = write_outputs(
        queue,
        short_root=short_root,
        output_dir=args.output_dir,
        start_date=start_date,
        end_date=end_date,
        per_family=per_family,
        max_total=max_total,
        completed_registry_rows=len(completed_ids),
    )
    print(result.queue_path)
    print(result.report_path)
    print(result.manifest_path)
    print(f"rows={result.queue.height}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
