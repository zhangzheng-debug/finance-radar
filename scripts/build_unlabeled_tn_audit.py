#!/usr/bin/env python3
"""Build an owner-only, unlabeled T+N return audit for the 720-event sample.

This pre-freeze audit deliberately reads only the security-readiness table and
market-price inputs.  It never reads reviewer submissions or human-gold labels.
The outputs must stay outside reviewer delivery packages until both blind
reviews and adjudication are frozen.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


HORIZONS = (1, 5, 21, 63, 126, 252)
BENCHMARK_TICKER = "SPY"
SCHEMA_VERSION = "finance-radar-unlabeled-tn-audit-v1"
FORBIDDEN_READINESS_COLUMNS = {
    "answer",
    "gold_label",
    "human_label",
    "label",
    "reviewer_answer",
    "split",
}


@dataclass(frozen=True)
class Price:
    day: date
    adjusted_close: float


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader)


def validate_unlabeled_readiness(rows: list[dict[str, str]]) -> None:
    if not rows:
        raise ValueError("readiness CSV is empty")
    forbidden = FORBIDDEN_READINESS_COLUMNS.intersection(rows[0])
    if forbidden:
        raise ValueError(
            "refusing to read readiness data containing gold/reviewer fields: "
            + ", ".join(sorted(forbidden))
        )
    required = {
        "sample_id",
        "event_id",
        "event_date",
        "event_family",
        "headline",
        "mapping_status",
        "ticker_at_event",
    }
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"readiness CSV is missing columns: {sorted(missing)}")
    sample_ids = [row["sample_id"] for row in rows]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("readiness CSV contains duplicate sample_id values")


def build_price_index(
    rows: Iterable[dict[str, str]],
) -> tuple[dict[str, list[Price]], dict[str, str], dict[str, str], date]:
    by_ticker_day: dict[str, dict[date, float]] = defaultdict(dict)
    source_symbols: dict[str, str] = {}
    quality_statuses: dict[str, str] = {}
    latest: date | None = None
    for row in rows:
        ticker = str(row.get("ticker") or "").strip()
        raw_day = str(row.get("date") or "").strip()
        raw_close = str(row.get("adj_close") or "").strip()
        if not ticker or not raw_day or not raw_close:
            continue
        day = date.fromisoformat(raw_day)
        close = float(raw_close)
        if not math.isfinite(close) or close <= 0:
            continue
        previous = by_ticker_day[ticker].get(day)
        if previous is not None and not math.isclose(previous, close, rel_tol=0, abs_tol=1e-12):
            raise ValueError(f"conflicting adjusted closes for {ticker} on {day}")
        by_ticker_day[ticker][day] = close
        source_symbols[ticker] = str(row.get("source_symbol") or ticker).strip()
        quality_statuses[ticker] = str(
            row.get("price_quality_status") or "PROVIDER_ADJUSTED_CLOSE_UNREPAIRED"
        ).strip()
        latest = day if latest is None or day > latest else latest
    if latest is None:
        raise ValueError("web-price CSV contains no usable adjusted closes")
    prices = {
        ticker: [Price(day=day, adjusted_close=values[day]) for day in sorted(values)]
        for ticker, values in by_ticker_day.items()
    }
    return prices, source_symbols, quality_statuses, latest


def build_terminal_index(
    rows: Iterable[dict[str, str]],
) -> dict[tuple[str, str], dict[str, str]]:
    return {
        (str(row.get("ticker") or "").strip(), str(row.get("event_date") or "").strip()): row
        for row in rows
        if row.get("ticker") and row.get("event_date")
    }


def simple_return(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start <= 0 or end <= 0:
        return None
    return end / start - 1.0


def benchmark_return(
    benchmark: dict[date, float], start_day: date, end_day: date
) -> float | None:
    return simple_return(benchmark.get(start_day), benchmark.get(end_day))


def _base_output(
    readiness: dict[str, str], source_symbol: str, price_quality_status: str
) -> dict[str, Any]:
    output: dict[str, Any] = dict(readiness)
    output.update(
        {
            "web_source_symbol": source_symbol,
            "price_quality_status": price_quality_status,
            "benchmark_ticker": BENCHMARK_TICKER,
            "anchor_precision": "DATE_ONLY",
            "event_trade_date": "",
            "pre_event_trade_date": "",
            "event_day_close_to_close": None,
            "spy_event_day_close_to_close": None,
            "market_adj_event_day_close_to_close": None,
            "pre_event_anchor_status": "NOT_AVAILABLE",
            "outcome_status": "NOT_COMPUTABLE",
            "terminal_evidence_url": "",
            "metric_scope": "owner_only_pre_freeze_unlabeled_audit",
            "owner_only": "true",
            "reviewer_safe": "false",
            "gold_labels_read": "false",
            "allowed_as_model_feature": "false",
        }
    )
    for horizon in HORIZONS:
        output[f"target_date_{horizon}d"] = ""
        output[f"ret_{horizon}d"] = None
        output[f"spy_ret_{horizon}d"] = None
        output[f"market_adj_ret_{horizon}d"] = None
        output[f"reaction_ret_{horizon}d"] = None
        output[f"spy_reaction_ret_{horizon}d"] = None
        output[f"market_adj_reaction_ret_{horizon}d"] = None
        output[f"maturity_{horizon}d"] = "NOT_COMPUTABLE"
    return output


def compute_event_outcome(
    readiness: dict[str, str],
    prices: dict[str, list[Price]],
    source_symbols: dict[str, str],
    terminals: dict[tuple[str, str], dict[str, str]],
    quality_statuses: dict[str, str] | None = None,
) -> dict[str, Any]:
    ticker = str(readiness.get("ticker_at_event") or "").strip()
    event_date_text = str(readiness.get("event_date") or "").strip()
    output = _base_output(
        readiness,
        source_symbols.get(ticker, ""),
        (quality_statuses or {}).get(ticker, "PROVIDER_ADJUSTED_CLOSE_UNREPAIRED"),
    )
    if readiness.get("mapping_status") != "MAPPED":
        output["outcome_status"] = "NO_UNAMBIGUOUS_SECURITY"
        return output

    terminal = terminals.get((ticker, event_date_text))
    if terminal:
        status = str(terminal.get("status") or "TERMINAL_SECURITY_EVENT")
        output["outcome_status"] = status
        output["terminal_evidence_url"] = str(terminal.get("evidence_url") or "")
        maturity_status = (
            "MANUAL_CORPORATE_ACTION_REVIEW"
            if status == "COMPLEX_CORPORATE_ACTION"
            else "TERMINAL_SECURITY_EVENT"
        )
        for horizon in HORIZONS:
            output[f"maturity_{horizon}d"] = maturity_status
        return output

    series = prices.get(ticker, [])
    if not series:
        output["outcome_status"] = "NO_WEB_PRICE_SERIES"
        return output
    event_day = date.fromisoformat(event_date_text)
    days = [price.day for price in series]
    anchor_index = bisect.bisect_left(days, event_day)
    if anchor_index >= len(series):
        output["outcome_status"] = "WAITING_FOR_EVENT_TRADE_DATE"
        for horizon in HORIZONS:
            output[f"maturity_{horizon}d"] = "RIGHT_CENSORED"
        return output

    anchor = series[anchor_index]
    benchmark = {price.day: price.adjusted_close for price in prices.get(BENCHMARK_TICKER, [])}
    output["event_trade_date"] = anchor.day.isoformat()
    previous = series[anchor_index - 1] if anchor_index > 0 else None
    if previous is not None:
        output["pre_event_trade_date"] = previous.day.isoformat()
        output["pre_event_anchor_status"] = "AVAILABLE"
        event_return = simple_return(previous.adjusted_close, anchor.adjusted_close)
        spy_event_return = benchmark_return(benchmark, previous.day, anchor.day)
        output["event_day_close_to_close"] = event_return
        output["spy_event_day_close_to_close"] = spy_event_return
        output["market_adj_event_day_close_to_close"] = (
            event_return - spy_event_return
            if event_return is not None and spy_event_return is not None
            else None
        )
    else:
        output["pre_event_anchor_status"] = "MISSING_PRE_EVENT_PRICE"

    matured = 0
    for horizon in HORIZONS:
        target_index = anchor_index + horizon
        if target_index >= len(series):
            output[f"maturity_{horizon}d"] = "RIGHT_CENSORED"
            continue
        target = series[target_index]
        output[f"target_date_{horizon}d"] = target.day.isoformat()
        stock_return = simple_return(anchor.adjusted_close, target.adjusted_close)
        spy_return = benchmark_return(benchmark, anchor.day, target.day)
        output[f"ret_{horizon}d"] = stock_return
        output[f"spy_ret_{horizon}d"] = spy_return
        output[f"market_adj_ret_{horizon}d"] = (
            stock_return - spy_return
            if stock_return is not None and spy_return is not None
            else None
        )
        if previous is not None:
            reaction_return = simple_return(previous.adjusted_close, target.adjusted_close)
            spy_reaction_return = benchmark_return(benchmark, previous.day, target.day)
            output[f"reaction_ret_{horizon}d"] = reaction_return
            output[f"spy_reaction_ret_{horizon}d"] = spy_reaction_return
            output[f"market_adj_reaction_ret_{horizon}d"] = (
                reaction_return - spy_reaction_return
                if reaction_return is not None and spy_reaction_return is not None
                else None
            )
        output[f"maturity_{horizon}d"] = "MATURED"
        matured += 1
    output["outcome_status"] = "HAS_MATURED_HORIZON" if matured else "RIGHT_CENSORED"
    return output


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def trimmed_mean(values: list[float], fraction: float = 0.05) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    trim = math.floor(len(ordered) * fraction)
    selected = ordered[trim : len(ordered) - trim] if trim else ordered
    return statistics.fmean(selected)


def metric_specs() -> list[tuple[int, str]]:
    specs = [
        (0, "event_day_close_to_close"),
        (0, "market_adj_event_day_close_to_close"),
    ]
    for horizon in HORIZONS:
        specs.extend(
            [
                (horizon, f"ret_{horizon}d"),
                (horizon, f"market_adj_ret_{horizon}d"),
                (horizon, f"reaction_ret_{horizon}d"),
                (horizon, f"market_adj_reaction_ret_{horizon}d"),
            ]
        )
    return specs


def aggregate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[tuple[str, str, list[dict[str, Any]]]] = [("ALL", "ALL", rows)]
    families = sorted({str(row.get("event_family") or "UNSPECIFIED") for row in rows})
    groups.extend(
        (
            "EVENT_FAMILY",
            family,
            [row for row in rows if str(row.get("event_family") or "UNSPECIFIED") == family],
        )
        for family in families
    )
    aggregates: list[dict[str, Any]] = []

    def append_summary(
        group_type: str,
        group_name: str,
        horizon: int,
        metric: str,
        values: list[float],
        distinct_tickers: int,
    ) -> None:
        aggregates.append(
            {
                "group_type": group_type,
                "group_name": group_name,
                "horizon_trading_days": horizon,
                "metric": metric,
                "n": len(values),
                "distinct_tickers": distinct_tickers,
                "mean": statistics.fmean(values) if values else None,
                "trimmed_mean_5pct": trimmed_mean(values),
                "median": statistics.median(values) if values else None,
                "positive_rate": (
                    sum(value > 0 for value in values) / len(values) if values else None
                ),
                "p10": quantile(values, 0.10),
                "p25": quantile(values, 0.25),
                "p75": quantile(values, 0.75),
                "p90": quantile(values, 0.90),
                "minimum": min(values) if values else None,
                "maximum": max(values) if values else None,
            }
        )

    for group_type, group_name, selected in groups:
        for horizon, metric in metric_specs():
            usable = [row for row in selected if row.get(metric) is not None]
            values = [float(row[metric]) for row in usable]
            append_summary(
                group_type,
                group_name,
                horizon,
                metric,
                values,
                len({str(row.get("ticker_at_event") or "") for row in usable}),
            )
    for horizon, metric in metric_specs():
        by_ticker: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            ticker = str(row.get("ticker_at_event") or "")
            if ticker and row.get(metric) is not None:
                by_ticker[ticker].append(float(row[metric]))
        ticker_means = [statistics.fmean(values) for values in by_ticker.values()]
        append_summary(
            "ALL_TICKER_EQUAL",
            "ALL",
            horizon,
            metric,
            ticker_means,
            len(by_ticker),
        )
    return aggregates


def extreme_rows(rows: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for horizon in HORIZONS:
        for metric in (
            f"market_adj_ret_{horizon}d",
            f"market_adj_reaction_ret_{horizon}d",
        ):
            selected = [row for row in rows if row.get(metric) is not None]
            for side, ordered in (
                ("BOTTOM", sorted(selected, key=lambda row: float(row[metric]))),
                ("TOP", sorted(selected, key=lambda row: float(row[metric]), reverse=True)),
            ):
                for rank, row in enumerate(ordered[:limit], start=1):
                    output.append(
                        {
                            "horizon_trading_days": horizon,
                            "metric": metric,
                            "side": side,
                            "rank": rank,
                            "sample_id": row.get("sample_id"),
                            "event_id": row.get("event_id"),
                            "event_date": row.get("event_date"),
                            "event_trade_date": row.get("event_trade_date"),
                            "target_date": row.get(f"target_date_{horizon}d"),
                            "event_family": row.get("event_family"),
                            "ticker_at_event": row.get("ticker_at_event"),
                            "web_source_symbol": row.get("web_source_symbol"),
                            "headline": row.get("headline"),
                            "value": row.get(metric),
                            "raw_post_event_return": row.get(f"ret_{horizon}d"),
                            "raw_reaction_return": row.get(f"reaction_ret_{horizon}d"),
                        }
                    )
    return output


def coverage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[tuple[str, list[dict[str, Any]]]] = [("ALL", rows)]
    groups.extend(
        (
            family,
            [row for row in rows if str(row.get("event_family") or "UNSPECIFIED") == family],
        )
        for family in sorted(
            {str(row.get("event_family") or "UNSPECIFIED") for row in rows}
        )
    )
    output: list[dict[str, Any]] = []
    for family, selected in groups:
        mapped = [row for row in selected if row.get("mapping_status") == "MAPPED"]
        output.append(
            {
                "event_family": family,
                "total_events": len(selected),
                "mapped_events": len(mapped),
                "mapping_coverage_rate": len(mapped) / len(selected) if selected else None,
                "unique_mapped_tickers": len(
                    {str(row.get("ticker_at_event") or "") for row in mapped}
                ),
                **{
                    f"matured_{horizon}d": sum(
                        row[f"maturity_{horizon}d"] == "MATURED" for row in selected
                    )
                    for horizon in HORIZONS
                },
            }
        )
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def percentage(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return f"{float(value):.2%}"


def write_report(
    path: Path,
    *,
    generated_at: str,
    latest_price_date: date,
    outcomes: list[dict[str, Any]],
    aggregates: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
) -> None:
    status_counts = Counter(row["outcome_status"] for row in outcomes)
    mapped = [row for row in outcomes if row.get("mapping_status") == "MAPPED"]
    ticker_counts = Counter(str(row.get("ticker_at_event") or "") for row in mapped)
    repeated_events = sum(count - 1 for ticker, count in ticker_counts.items() if ticker)
    quality_repaired = [
        row for row in mapped if row.get("price_quality_status") == "SPLIT_SCALE_REPAIRED"
    ]
    aggregate_index = {
        (row["horizon_trading_days"], row["metric"]): row
        for row in aggregates
        if row["group_type"] == "ALL"
    }
    lines = [
        "# Finance Radar 720 条事件：无标签 T+N 收益审计",
        "",
        f"- 生成时间（UTC）：`{generated_at}`",
        f"- 价格数据截止：`{latest_price_date.isoformat()}`",
        f"- 样本总数：`{len(outcomes)}`；可唯一映射证券：`{len(mapped)}`；唯一证券：`{len(ticker_counts)}`。",
        f"- 证券映射覆盖率：`{len(mapped) / len(outcomes):.1%}`；当前 T+1 可计算：`{sum(row['maturity_1d'] == 'MATURED' for row in outcomes)}`。",
        f"- 重复证券对应的额外事件数：`{repeated_events}`；因此事件观测并不完全独立。",
        f"- 反向拆股尺度修复涉及：`{len(quality_repaired)}` 个事件、`{len({row.get('ticker_at_event') for row in quality_repaired})}` 个证券。",
        "- 本报告没有读取 A/B 组员答案、裁决结果或金标标签，也没有按标签分组。",
        "- 这是负责人专用的事后收益审计；双盲审核冻结前不得放入组员包。",
        "",
        "## 两种时间锚点",
        "",
        "1. **事件后窗口（主口径）**：事件日或其后首个交易日收盘买入，持有 N 个该证券交易日。",
        "2. **全反应窗口（敏感性口径）**：事件前一交易日收盘起算，到上述 T+N 收盘；它包含事件日反应。",
        "",
        "两种口径都使用复权收盘价；超额收益为证券收益减去 SPY 在完全相同日期区间的收益。事件只有日期、没有精确发布时间，所以两种口径必须同时保留，不能挑选更好看的一个。",
        "",
        "## 当前整体结果",
        "",
        "| 窗口 | 口径 | n | 均值 | 5%截尾均值 | 中位数 | 正收益率 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for horizon in (1, 5, 21, 63, 126, 252):
        for label, metric in (
            ("事件日收盘后 / SPY超额", f"market_adj_ret_{horizon}d"),
            ("含事件日反应 / SPY超额", f"market_adj_reaction_ret_{horizon}d"),
        ):
            row = aggregate_index[(horizon, metric)]
            lines.append(
                f"| T+{horizon} | {label} | {row['n']} | {percentage(row['mean'])} | "
                f"{percentage(row['trimmed_mean_5pct'])} | {percentage(row['median'])} | "
                f"{percentage(row['positive_rate'])} |"
            )
    family_rows = [
        row
        for row in aggregates
        if row["group_type"] == "EVENT_FAMILY"
        and row["metric"] == "market_adj_ret_5d"
        and int(row["n"]) >= 15
    ]
    family_rows.sort(key=lambda row: float(row["median"]), reverse=True)
    lines.extend(
        [
            "",
            "## 重复证券等权敏感性",
            "",
            "先在每个证券内平均重复事件，再让每个证券等权；用于检查 24 个重复证券事件是否扭曲整体结果。",
            "",
            "| 窗口 | 事件等权均值 | 证券等权均值 | 事件等权中位数 | 证券等权中位数 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    ticker_equal_index = {
        (row["horizon_trading_days"], row["metric"]): row
        for row in aggregates
        if row["group_type"] == "ALL_TICKER_EQUAL"
    }
    for horizon in (1, 5, 21):
        metric = f"market_adj_ret_{horizon}d"
        event_row = aggregate_index[(horizon, metric)]
        ticker_row = ticker_equal_index[(horizon, metric)]
        lines.append(
            f"| T+{horizon} | {percentage(event_row['mean'])} | {percentage(ticker_row['mean'])} | "
            f"{percentage(event_row['median'])} | {percentage(ticker_row['median'])} |"
        )
    lines.extend(
        [
            "",
            "## T+5 事件家族探索性排序",
            "",
            "仅展示当前可计算样本数不少于 15 的家族，按 SPY 超额收益中位数完整排序；这不是金标分组，也不是显著性检验。",
            "",
            "| 事件家族 | n | 均值 | 5%截尾均值 | 中位数 | 正收益率 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend(
        f"| {row['group_name']} | {row['n']} | {percentage(row['mean'])} | "
        f"{percentage(row['trimmed_mean_5pct'])} | {percentage(row['median'])} | "
        f"{percentage(row['positive_rate'])} |"
        for row in family_rows
    )
    lines.extend(
        [
            "",
            "## 可计算状态",
            "",
            "| 状态 | 事件数 |",
            "| --- | ---: |",
        ]
    )
    lines.extend(f"| {status} | {count} |" for status, count in sorted(status_counts.items()))
    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            "- 缺失与右删失保持空白，绝不当作 0。",
            "- 现金并购完成、证券注销和复杂公司行动单列，不把终止状态伪装成普通市场收益。",
            "- 均值容易被小盘股和极端值主导，判断时优先同时查看中位数、5%截尾均值和分位数。",
            "- 同一证券可能出现多条事件，事件窗口也会重叠；当前报告只做描述统计，不给出独立同分布假设下的显著性结论。",
            "- T+21 当前只有 42 条，且来自最早一批事件，时间队列选择偏差很强；目前只能观察，不能形成稳定结论。",
            "- 195 条没有唯一证券映射；宏观、监管执法等不可直接落到单一股票的家族基本不在收益样本内，因此不能把 521 条可计算结果外推为全部 720 条的市场表现。",
            "- Yahoo 原始条目中部分反向拆股证券存在价格单位来回切换。已依据原始 split 元数据统一尺度，并以网页历史价格抽查；修复行在逐事件表的 `price_quality_status` 中可追溯。",
            "- 这是事件后关联，不证明新闻导致了收益，更不能直接当成交易策略回测。",
            "",
            "## 价格质量抽查来源",
            "",
            "- Nasdaq Corporate Actions: https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2026-568",
            "- Nasdaq Corporate Actions: https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2026-579",
            "- Nasdaq Corporate Actions: https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2026-561",
            "- Nasdaq Corporate Actions: https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2026-553",
            "- Historical-price spot checks: https://stockanalysis.com/stocks/bynd/history/ ; https://stockanalysis.com/stocks/msgy/history/ ; https://stockanalysis.com/stocks/thh/history/",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def run(args: argparse.Namespace) -> dict[str, Any]:
    readiness_path = Path(args.readiness_csv).resolve()
    prices_path = Path(args.web_prices).resolve()
    terminal_path = Path(args.terminal_events).resolve()
    output_dir = Path(args.output_dir).resolve()

    readiness = read_csv(readiness_path)
    validate_unlabeled_readiness(readiness)
    prices, source_symbols, quality_statuses, latest_price_date = build_price_index(
        read_csv(prices_path)
    )
    if BENCHMARK_TICKER not in prices:
        raise ValueError(f"benchmark price series is missing: {BENCHMARK_TICKER}")
    terminals = build_terminal_index(read_csv(terminal_path))
    outcomes = [
        compute_event_outcome(
            row, prices, source_symbols, terminals, quality_statuses
        )
        for row in readiness
    ]
    if len(outcomes) != len(readiness):
        raise AssertionError("output row count does not match readiness row count")

    aggregates = aggregate_rows(outcomes)
    extremes = extreme_rows(outcomes)
    coverage = coverage_rows(outcomes)
    outcomes_path = output_dir / "unlabeled_tn_event_outcomes.csv"
    summary_path = output_dir / "unlabeled_tn_summary.csv"
    extremes_path = output_dir / "unlabeled_tn_extremes.csv"
    coverage_path = output_dir / "unlabeled_tn_coverage.csv"
    report_path = output_dir / "unlabeled_tn_report.md"
    write_csv(outcomes_path, outcomes)
    write_csv(summary_path, aggregates)
    write_csv(extremes_path, extremes)
    write_csv(coverage_path, coverage)
    generated_at = datetime.now(timezone.utc).isoformat()
    write_report(
        report_path,
        generated_at=generated_at,
        latest_price_date=latest_price_date,
        outcomes=outcomes,
        aggregates=aggregates,
        coverage=coverage,
    )

    status_counts = dict(sorted(Counter(row["outcome_status"] for row in outcomes).items()))
    maturity_counts = {
        f"T+{horizon}": sum(row[f"maturity_{horizon}d"] == "MATURED" for row in outcomes)
        for horizon in HORIZONS
    }
    mapped_tickers = [
        str(row.get("ticker_at_event") or "")
        for row in outcomes
        if row.get("mapping_status") == "MAPPED"
    ]
    duplicate_sample_ids = len(outcomes) - len({row["sample_id"] for row in outcomes})
    nonfinite_values = 0
    for row in outcomes:
        for _, metric in metric_specs():
            value = row.get(metric)
            if value is not None and not math.isfinite(float(value)):
                nonfinite_values += 1
    if duplicate_sample_ids or nonfinite_values:
        raise AssertionError(
            f"audit invariants failed: duplicate_sample_ids={duplicate_sample_ids}, "
            f"nonfinite_values={nonfinite_values}"
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "price_data_cutoff": latest_price_date.isoformat(),
        "sample_count": len(outcomes),
        "mapped_event_count": len(mapped_tickers),
        "unique_mapped_ticker_count": len(set(mapped_tickers)),
        "repeated_security_extra_event_count": len(mapped_tickers) - len(set(mapped_tickers)),
        "outcome_status_counts": status_counts,
        "matured_horizon_counts": maturity_counts,
        "source": {
            "readiness_csv": str(readiness_path),
            "readiness_sha256": sha256_file(readiness_path),
            "web_prices_csv": str(prices_path),
            "web_prices_sha256": sha256_file(prices_path),
            "terminal_events_csv": str(terminal_path),
            "terminal_events_sha256": sha256_file(terminal_path),
            "benchmark_ticker": BENCHMARK_TICKER,
            "price_field": "adj_close",
        },
        "outputs": {
            "event_outcomes_csv": str(outcomes_path),
            "event_outcomes_sha256": sha256_file(outcomes_path),
            "summary_csv": str(summary_path),
            "summary_sha256": sha256_file(summary_path),
            "extremes_csv": str(extremes_path),
            "extremes_sha256": sha256_file(extremes_path),
            "coverage_csv": str(coverage_path),
            "coverage_sha256": sha256_file(coverage_path),
            "report": str(report_path),
            "report_sha256": sha256_file(report_path),
        },
        "invariants": {
            "owner_only": True,
            "reviewer_safe": False,
            "reviewer_answers_read": False,
            "gold_labels_read": False,
            "label_stratification_included": False,
            "missing_values_imputed": False,
            "terminal_events_imputed_as_zero": False,
            "adjusted_close_used": True,
            "benchmark_dates_exactly_matched": True,
            "allowed_as_model_feature": False,
            "causal_claim_allowed": False,
            "live_trading_allowed": False,
            "duplicate_sample_ids": duplicate_sample_ids,
            "nonfinite_metric_values": nonfinite_values,
        },
    }
    manifest_path = output_dir / "unlabeled_tn_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readiness-csv", type=Path, required=True)
    parser.add_argument("--web-prices", type=Path, required=True)
    parser.add_argument("--terminal-events", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        manifest = run(parse_args(argv))
    except (OSError, ValueError, AssertionError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
