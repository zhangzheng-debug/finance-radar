#!/usr/bin/env python3
"""Build an owner-only T+1..T+N short-sale gross-return curve.

The entry is the adjusted close of the first trading day on or after the event
date.  A T+N position is covered at that security's Nth subsequent trading-day
adjusted close.  Gross short return is ``1 - cover / entry``; no benchmark,
borrow fee, locate fee, commission, spread, slippage, tax, or margin financing
is included.

The script intentionally reads the security-readiness export only.  It refuses
reviewer/gold-label columns and keeps every output owner-only and outside the
blind-review package.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from build_unlabeled_tn_audit import (
    Price,
    build_price_index,
    build_terminal_index,
    quantile,
    read_csv,
    trimmed_mean,
    validate_unlabeled_readiness,
)


SCHEMA_VERSION = "finance-radar-unlabeled-short-horizon-curve-v1"
DEFAULT_MIN_FIXED_COHORT_SIZE = 40
BEST_METRICS = (
    "mean_short_return",
    "trimmed_mean_5pct",
    "median_short_return",
    "win_rate",
    "ticker_equal_mean_short_return",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build_event_daily_returns(
    readiness_rows: Iterable[dict[str, str]],
    prices: dict[str, list[Price]],
    quality_statuses: dict[str, str],
    terminals: dict[tuple[str, str], dict[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return one row per event and matured post-event trading-day horizon."""
    output: list[dict[str, Any]] = []
    counts: defaultdict[str, int] = defaultdict(int)

    for event in readiness_rows:
        counts["events_total"] += 1
        if str(event.get("mapping_status") or "") != "MAPPED":
            counts["events_unmapped"] += 1
            continue

        ticker = str(event.get("ticker_at_event") or "").strip()
        event_date_text = str(event.get("event_date") or "").strip()
        if terminals.get((ticker, event_date_text)):
            counts["events_terminal_or_complex"] += 1
            continue

        series = prices.get(ticker, [])
        if not series:
            counts["events_without_price_series"] += 1
            continue

        event_day = date.fromisoformat(event_date_text)
        days = [price.day for price in series]
        anchor_index = bisect.bisect_left(days, event_day)
        if anchor_index >= len(series):
            counts["events_waiting_for_anchor"] += 1
            continue

        anchor = series[anchor_index]
        available_horizons = len(series) - anchor_index - 1
        if available_horizons <= 0:
            counts["events_without_post_event_session"] += 1
            continue

        counts["events_computable_t1"] += 1
        quality = quality_statuses.get(
            ticker, "PROVIDER_ADJUSTED_CLOSE_UNREPAIRED"
        )
        for horizon in range(1, available_horizons + 1):
            target = series[anchor_index + horizon]
            long_return = target.adjusted_close / anchor.adjusted_close - 1.0
            short_return = -long_return
            output.append(
                {
                    "sample_id": event["sample_id"],
                    "event_id": event["event_id"],
                    "event_date": event_date_text,
                    "event_family": event["event_family"],
                    "ticker": ticker,
                    "event_trade_date": anchor.day.isoformat(),
                    "horizon_trading_days": horizon,
                    "cover_trade_date": target.day.isoformat(),
                    "entry_adjusted_close": anchor.adjusted_close,
                    "cover_adjusted_close": target.adjusted_close,
                    "long_total_return": long_return,
                    "short_gross_return": short_return,
                    "short_profitable": str(short_return > 0).lower(),
                    "price_quality_status": quality,
                    "entry_rule": "FIRST_TRADE_DATE_ON_OR_AFTER_EVENT_CLOSE",
                    "cover_rule": "NTH_SUBSEQUENT_SECURITY_TRADE_DATE_CLOSE",
                    "owner_only": "true",
                    "reviewer_safe": "false",
                    "gold_labels_read": "false",
                    "allowed_as_model_feature": "false",
                }
            )

    output.sort(
        key=lambda row: (
            int(row["horizon_trading_days"]),
            str(row["sample_id"]),
        )
    )
    return output, dict(counts)


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def aggregate_return_rows(
    rows: list[dict[str, Any]],
    *,
    curve_type: str,
    cohort_maturity_horizon: int | str,
    horizon: int,
) -> dict[str, Any]:
    values = [float(row["short_gross_return"]) for row in rows]
    if not values:
        raise ValueError("cannot aggregate an empty return group")

    by_ticker: defaultdict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_ticker[str(row["ticker"])].append(float(row["short_gross_return"]))
    ticker_means = [statistics.fmean(group) for group in by_ticker.values()]
    mean = statistics.fmean(values)
    sample_stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    standard_error = sample_stdev / math.sqrt(len(values))

    return {
        "curve_type": curve_type,
        "cohort_maturity_horizon": cohort_maturity_horizon,
        "horizon_trading_days": horizon,
        "n_events": len(values),
        "n_distinct_tickers": len(by_ticker),
        "mean_short_return": mean,
        "trimmed_mean_5pct": trimmed_mean(values, 0.05),
        "median_short_return": statistics.median(values),
        "win_rate": sum(value > 0 for value in values) / len(values),
        "loss_rate": sum(value < 0 for value in values) / len(values),
        "flat_rate": sum(value == 0 for value in values) / len(values),
        "p10_short_return": quantile(values, 0.10),
        "p25_short_return": quantile(values, 0.25),
        "p75_short_return": quantile(values, 0.75),
        "p90_short_return": quantile(values, 0.90),
        "worst_short_return": min(values),
        "best_short_return": max(values),
        "sample_stdev": sample_stdev,
        "standard_error": standard_error,
        "approx_mean_ci95_low": mean - 1.96 * standard_error,
        "approx_mean_ci95_high": mean + 1.96 * standard_error,
        "ticker_equal_mean_short_return": statistics.fmean(ticker_means),
        "ticker_equal_median_short_return": statistics.median(ticker_means),
        "return_definition": "1_MINUS_COVER_ADJ_CLOSE_DIV_ENTRY_ADJ_CLOSE",
        "benchmark_used": "false",
        "costs_included": "false",
        "owner_only": "true",
        "gold_labels_read": "false",
    }


def build_curves(
    daily_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Build available-case and all fixed-maturity cohort curves."""
    by_horizon: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    by_sample: defaultdict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in daily_rows:
        horizon = int(row["horizon_trading_days"])
        by_horizon[horizon].append(row)
        by_sample[str(row["sample_id"])][horizon] = row

    if not by_horizon:
        raise ValueError("no matured short-return observations")

    curve: list[dict[str, Any]] = []
    for horizon in sorted(by_horizon):
        curve.append(
            aggregate_return_rows(
                by_horizon[horizon],
                curve_type="AVAILABLE_CASE",
                cohort_maturity_horizon="VARIES",
                horizon=horizon,
            )
        )

    cohort_sizes: dict[int, int] = {}
    max_horizon = max(by_horizon)
    for maturity_horizon in range(1, max_horizon + 1):
        eligible = {
            sample_id
            for sample_id, observations in by_sample.items()
            if maturity_horizon in observations
        }
        if not eligible:
            continue
        cohort_sizes[maturity_horizon] = len(eligible)
        for horizon in range(1, maturity_horizon + 1):
            rows = [by_sample[sample_id][horizon] for sample_id in eligible]
            curve.append(
                aggregate_return_rows(
                    rows,
                    curve_type="FIXED_MATURITY_COHORT",
                    cohort_maturity_horizon=maturity_horizon,
                    horizon=horizon,
                )
            )

    curve.sort(
        key=lambda row: (
            0 if row["curve_type"] == "AVAILABLE_CASE" else 1,
            0
            if row["cohort_maturity_horizon"] == "VARIES"
            else int(row["cohort_maturity_horizon"]),
            int(row["horizon_trading_days"]),
        )
    )
    return curve, cohort_sizes


def _best_row(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    return max(
        rows,
        key=lambda row: (
            float(row[metric]),
            -int(row["horizon_trading_days"]),
        ),
    )


def build_best_points(
    curve_rows: list[dict[str, Any]], primary_fixed_horizon: int
) -> list[dict[str, Any]]:
    groups: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in curve_rows:
        key = (str(row["curve_type"]), str(row["cohort_maturity_horizon"]))
        groups[key].append(row)

    output: list[dict[str, Any]] = []
    for key in sorted(
        groups,
        key=lambda item: (
            0 if item[0] == "AVAILABLE_CASE" else 1,
            0 if item[1] == "VARIES" else int(item[1]),
        ),
    ):
        rows = groups[key]
        for metric in BEST_METRICS:
            best = _best_row(rows, metric)
            output.append(
                {
                    "curve_type": key[0],
                    "cohort_maturity_horizon": key[1],
                    "selection_metric": metric,
                    "best_horizon_trading_days": best["horizon_trading_days"],
                    "best_metric_value": best[metric],
                    "n_events_at_best": best["n_events"],
                    "mean_short_return_at_best": best["mean_short_return"],
                    "median_short_return_at_best": best["median_short_return"],
                    "win_rate_at_best": best["win_rate"],
                    "p10_short_return_at_best": best["p10_short_return"],
                    "owner_only": "true",
                    "gold_labels_read": "false",
                }
            )

        # A descriptive consensus across five profit-oriented metrics.  Ranking
        # is within the curve, and ties prefer the shorter holding period.
        rank_sum = {int(row["horizon_trading_days"]): 0.0 for row in rows}
        for metric in BEST_METRICS:
            ordered = sorted(
                rows,
                key=lambda row: (
                    -float(row[metric]),
                    int(row["horizon_trading_days"]),
                ),
            )
            for rank, row in enumerate(ordered, start=1):
                rank_sum[int(row["horizon_trading_days"])] += rank
        consensus_horizon = min(rank_sum, key=lambda horizon: (rank_sum[horizon], horizon))
        consensus = next(
            row
            for row in rows
            if int(row["horizon_trading_days"]) == consensus_horizon
        )
        output.append(
            {
                "curve_type": key[0],
                "cohort_maturity_horizon": key[1],
                "selection_metric": "FIVE_METRIC_RANK_CONSENSUS",
                "best_horizon_trading_days": consensus_horizon,
                "best_metric_value": rank_sum[consensus_horizon],
                "n_events_at_best": consensus["n_events"],
                "mean_short_return_at_best": consensus["mean_short_return"],
                "median_short_return_at_best": consensus["median_short_return"],
                "win_rate_at_best": consensus["win_rate"],
                "p10_short_return_at_best": consensus["p10_short_return"],
                "owner_only": "true",
                "gold_labels_read": "false",
            }
        )
    return output


def stable_fixed_cohort_recommendation(
    curve_rows: list[dict[str, Any]], min_events: int = 100, min_horizon: int = 3
) -> tuple[int, dict[int, int], int]:
    """Choose the modal mean-best day across adequately sized fixed cohorts."""
    by_cohort: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in curve_rows:
        if row["curve_type"] != "FIXED_MATURITY_COHORT":
            continue
        cohort = int(row["cohort_maturity_horizon"])
        if cohort < min_horizon or int(row["n_events"]) < min_events:
            continue
        by_cohort[cohort].append(row)
    if not by_cohort:
        raise ValueError("no fixed maturity cohort meets the stability floor")

    votes: defaultdict[int, int] = defaultdict(int)
    for rows in by_cohort.values():
        best = _best_row(rows, "mean_short_return")
        votes[int(best["horizon_trading_days"])] += 1
    selected = min(votes, key=lambda horizon: (-votes[horizon], horizon))
    return selected, dict(sorted(votes.items())), len(by_cohort)


def _pct(value: Any) -> str:
    return f"{float(value) * 100:.3f}%"


def _markdown_curve(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 回补日 | 样本 | 股票数 | 做空均值 | 5%截尾均值 | 中位数 | 赚钱率 | P10 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| T+{h} | {n} | {t} | {mean} | {trim} | {median} | {win} | {p10} |".format(
                h=row["horizon_trading_days"],
                n=row["n_events"],
                t=row["n_distinct_tickers"],
                mean=_pct(row["mean_short_return"]),
                trim=_pct(row["trimmed_mean_5pct"]),
                median=_pct(row["median_short_return"]),
                win=_pct(row["win_rate"]),
                p10=_pct(row["p10_short_return"]),
            )
        )
    return lines


def render_report(
    curve_rows: list[dict[str, Any]],
    best_rows: list[dict[str, Any]],
    counts: dict[str, int],
    primary_fixed_horizon: int,
    data_cutoff: date,
) -> str:
    available = [
        row for row in curve_rows if row["curve_type"] == "AVAILABLE_CASE"
    ]
    fixed = [
        row
        for row in curve_rows
        if row["curve_type"] == "FIXED_MATURITY_COHORT"
        and int(row["cohort_maturity_horizon"]) == primary_fixed_horizon
    ]
    available_mean_best = next(
        row
        for row in best_rows
        if row["curve_type"] == "AVAILABLE_CASE"
        and row["selection_metric"] == "mean_short_return"
    )
    fixed_mean_best = next(
        row
        for row in best_rows
        if row["curve_type"] == "FIXED_MATURITY_COHORT"
        and int(row["cohort_maturity_horizon"]) == primary_fixed_horizon
        and row["selection_metric"] == "mean_short_return"
    )
    fixed_consensus = next(
        row
        for row in best_rows
        if row["curve_type"] == "FIXED_MATURITY_COHORT"
        and int(row["cohort_maturity_horizon"]) == primary_fixed_horizon
        and row["selection_metric"] == "FIVE_METRIC_RANK_CONSENSUS"
    )
    stable_horizon, stability_votes, stability_cohorts = (
        stable_fixed_cohort_recommendation(curve_rows)
    )
    stability_text = "、".join(
        f"T+{horizon}={votes}次" for horizon, votes in stability_votes.items()
    )
    checkpoints = [3, 5, 10, 15, 17, primary_fixed_horizon]
    checkpoint_rows: list[str] = [
        "| 要求至少成熟到 | 同一批事件数 | 该批均值最佳回补日 | 最佳做空均值 |",
        "|---:|---:|---:|---:|",
    ]
    for checkpoint in dict.fromkeys(checkpoints):
        candidates = [
            row
            for row in curve_rows
            if row["curve_type"] == "FIXED_MATURITY_COHORT"
            and int(row["cohort_maturity_horizon"]) == checkpoint
        ]
        if not candidates:
            continue
        best = _best_row(candidates, "mean_short_return")
        checkpoint_rows.append(
            f"| T+{checkpoint} | {best['n_events']} | T+{best['horizon_trading_days']} | {_pct(best['mean_short_return'])} |"
        )

    lines = [
        "# Finance Radar 720 条事件：逐交易日做空毛收益",
        "",
        f"价格数据截止：{data_cutoff.isoformat()}。本报告不读取真人双盲答案或金标，也不使用 SPY 或其他基准。",
        "",
        "## 口径",
        "",
        "- 建仓：事件日当日（若非交易日则下一交易日）复权收盘价做空。",
        "- 回补：该股票第 N 个后续交易日的复权收盘价。",
        "- 做空毛收益：`1 - 回补复权价 / 建仓复权价`。正数赚钱，负数亏钱。",
        "- 未计入：借券费、融券可得性、召回风险、点差、滑点、佣金、税费和保证金融资。",
        "- 终止上市或复杂公司行动事件不强行填收益；证券映射不明确的事件也排除。",
        "",
        "## 观察到的最佳点",
        "",
        f"逐期限使用全部可用事件时，均值最高在 T+{available_mean_best['best_horizon_trading_days']}，做空毛收益均值 {_pct(available_mean_best['mean_short_return_at_best'])}，样本 {available_mean_best['n_events_at_best']}。",
        f"更稳妥的单点是 **T+{stable_horizon} 回补**：在 {stability_cohorts} 个样本数不少于 100 的固定成熟样本比较中，均值最佳日投票为 {stability_text}。也就是说，T+3 是全可用样本的表面峰值，T+{stable_horizon} 是对齐同一批事件后更稳定的峰值。",
        f"最长仍有至少 40 个事件的固定样本是 T+{primary_fixed_horizon}（{fixed_mean_best['n_events_at_best']} 个事件）；这批较早且较小的事件均值最佳在 T+{fixed_mean_best['best_horizon_trading_days']}，五指标共识在 T+{fixed_consensus['best_horizon_trading_days']}，最佳均值 {_pct(fixed_mean_best['mean_short_return_at_best'])}。它是小样本敏感性结果，不应覆盖大样本的 T+{stable_horizon} 结论。",
        "",
        "### 固定样本敏感性",
        "",
        *checkpoint_rows,
        "",
        "## 全部可用事件：每个交易日",
        "",
        "该曲线回答‘在每个期限上，当前能观察到的所有事件表现如何’，但每行样本会递减。",
        "",
        *_markdown_curve(available),
        "",
        f"## 固定 T+{primary_fixed_horizon} 成熟样本：同一批事件逐日比较",
        "",
        "这张表每一行使用完全相同的事件，适合判断回补日；代价是样本较少。",
        "",
        *_markdown_curve(fixed),
        "",
        "## 数据覆盖",
        "",
        f"- 总事件：{counts.get('events_total', 0)}",
        f"- 可计算 T+1：{counts.get('events_computable_t1', 0)}",
        f"- 无明确证券映射：{counts.get('events_unmapped', 0)}",
        f"- 终止上市/复杂公司行动：{counts.get('events_terminal_or_complex', 0)}",
        "",
        "## 审计边界",
        "",
        "逐日扫描会产生多重比较和样本内挑选偏差。均值的普通近似 95% 置信区间在大样本 T+1 至 T+3 上仍跨过 0，且事件并非严格独立。最佳点必须在金标冻结后，按预先固定的事件方向、可借券条件、成本和独立留出样本重新验证；当前结果只回答历史上按该机械做空口径是否赚钱。",
        "",
    ]
    return "\n".join(lines)


def choose_primary_fixed_horizon(
    cohort_sizes: dict[int, int], min_size: int
) -> int:
    eligible = [horizon for horizon, size in cohort_sizes.items() if size >= min_size]
    if eligible:
        return max(eligible)
    return max(cohort_sizes, key=lambda horizon: (cohort_sizes[horizon], horizon))


def run(args: argparse.Namespace) -> dict[str, Any]:
    readiness_path = Path(args.readiness_csv)
    prices_path = Path(args.prices_csv)
    terminal_path = Path(args.terminal_csv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    readiness = read_csv(readiness_path)
    validate_unlabeled_readiness(readiness)
    price_rows = read_csv(prices_path)
    prices, _symbols, quality_statuses, latest_price_date = build_price_index(price_rows)
    terminals = build_terminal_index(read_csv(terminal_path))

    daily_rows, counts = build_event_daily_returns(
        readiness, prices, quality_statuses, terminals
    )
    curve_rows, cohort_sizes = build_curves(daily_rows)
    primary_fixed_horizon = choose_primary_fixed_horizon(
        cohort_sizes, args.min_fixed_cohort_size
    )
    best_rows = build_best_points(curve_rows, primary_fixed_horizon)

    daily_path = output_dir / "unlabeled_short_daily_event_returns.csv"
    curve_path = output_dir / "short_horizon_curve.csv"
    best_path = output_dir / "short_horizon_best_points.csv"
    report_path = output_dir / "short_horizon_report.md"
    manifest_path = output_dir / "short_horizon_manifest.json"

    write_csv(daily_path, daily_rows)
    write_csv(curve_path, curve_rows)
    write_csv(best_path, best_rows)
    report_path.write_text(
        render_report(
            curve_rows,
            best_rows,
            counts,
            primary_fixed_horizon,
            latest_price_date,
        ),
        encoding="utf-8",
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "owner_only": True,
        "reviewer_safe": False,
        "gold_labels_read": False,
        "benchmark_used": False,
        "costs_included": False,
        "entry_rule": "first trade date on or after event date adjusted close",
        "cover_rule": "Nth subsequent security trading-day adjusted close",
        "short_return_definition": "1 - cover_adjusted_close / entry_adjusted_close",
        "data_cutoff": latest_price_date.isoformat(),
        "max_matured_horizon": max(cohort_sizes),
        "primary_fixed_horizon": primary_fixed_horizon,
        "min_fixed_cohort_size": args.min_fixed_cohort_size,
        "counts": counts,
        "cohort_sizes": cohort_sizes,
        "inputs": {
            "readiness_csv": str(readiness_path.resolve()),
            "readiness_sha256": sha256_file(readiness_path),
            "prices_csv": str(prices_path.resolve()),
            "prices_sha256": sha256_file(prices_path),
            "terminal_csv": str(terminal_path.resolve()),
            "terminal_sha256": sha256_file(terminal_path),
        },
        "outputs": {
            path.name: sha256_file(path)
            for path in (daily_path, curve_path, best_path, report_path)
        },
        "limitations": [
            "descriptive in-sample horizon scan; multiple-comparison bias applies",
            "borrow availability and all trading costs are excluded",
            "date-only events use first on-or-after trade-date close",
            "terminal and complex corporate-action events are not imputed",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--readiness-csv",
        default=r"D:\train 720\post_event_audit_web\human_gold_720_sharadar_readiness.csv",
    )
    parser.add_argument(
        "--prices-csv",
        default=r"D:\train 720\web_market_data\quality_adjusted\daily_prices_split_repaired.csv",
    )
    parser.add_argument(
        "--terminal-csv",
        default=r"D:\train 720\web_market_data\terminal_security_events.csv",
    )
    parser.add_argument(
        "--output-dir",
        default=r"D:\train 720\web_market_data\short_horizon_curve",
    )
    parser.add_argument(
        "--min-fixed-cohort-size",
        type=int,
        default=DEFAULT_MIN_FIXED_COHORT_SIZE,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = run(args)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
