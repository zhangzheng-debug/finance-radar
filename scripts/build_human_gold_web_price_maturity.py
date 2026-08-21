#!/usr/bin/env python3
"""Report T+N price-window maturity without exposing post-event returns."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


HORIZONS = (1, 5, 21, 63, 126, 252)
SCHEMA_VERSION = "human-gold-web-price-maturity-v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def price_index(rows: list[dict[str, str]]) -> tuple[dict[str, list[date]], dict[str, str]]:
    days: dict[str, set[date]] = defaultdict(set)
    source_symbols: dict[str, str] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").strip()
        raw_day = str(row.get("date") or "").strip()
        if not ticker or not raw_day:
            continue
        days[ticker].add(date.fromisoformat(raw_day))
        source_symbols[ticker] = str(row.get("source_symbol") or ticker).strip()
    return {ticker: sorted(values) for ticker, values in days.items()}, source_symbols


def terminal_index(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    return {
        (str(row.get("ticker") or "").strip(), str(row.get("event_date") or "").strip()): row
        for row in rows
        if row.get("ticker") and row.get("event_date")
    }


def assess_row(
    readiness: dict[str, str],
    prices: dict[str, list[date]],
    source_symbols: dict[str, str],
    terminals: dict[tuple[str, str], dict[str, str]],
) -> dict[str, Any]:
    output: dict[str, Any] = dict(readiness)
    ticker = str(readiness.get("ticker_at_event") or "").strip()
    event_date = str(readiness.get("event_date") or "").strip()
    output.update(
        {
            "web_source_symbol": source_symbols.get(ticker, ""),
            "event_trade_date": "",
            "last_price_date": "",
            "available_post_event_sessions": 0,
            "window_status": "NOT_MAPPABLE",
            "post_event_returns_included": "false",
            "reviewer_safe": "true",
        }
    )
    for horizon in HORIZONS:
        output[f"maturity_{horizon}d"] = "NOT_APPLICABLE"
    if readiness.get("mapping_status") != "MAPPED":
        return output
    terminal = terminals.get((ticker, event_date))
    if terminal:
        output["window_status"] = str(terminal.get("status") or "TERMINAL_SECURITY_EVENT")
        maturity_status = (
            "MANUAL_CORPORATE_ACTION_REVIEW"
            if output["window_status"] == "COMPLEX_CORPORATE_ACTION"
            else "TERMINAL_SECURITY_EVENT"
        )
        for horizon in HORIZONS:
            output[f"maturity_{horizon}d"] = maturity_status
        return output
    series = prices.get(ticker, [])
    if not series:
        output["window_status"] = "NO_WEB_PRICE_SERIES"
        for horizon in HORIZONS:
            output[f"maturity_{horizon}d"] = "NO_WEB_PRICE_SERIES"
        return output
    event_day = date.fromisoformat(event_date)
    anchor_index = bisect.bisect_left(series, event_day)
    output["last_price_date"] = series[-1].isoformat()
    if anchor_index >= len(series):
        output["window_status"] = "WAITING_FOR_EVENT_TRADE_DATE"
        for horizon in HORIZONS:
            output[f"maturity_{horizon}d"] = "RIGHT_CENSORED"
        return output
    available = len(series) - anchor_index - 1
    output["event_trade_date"] = series[anchor_index].isoformat()
    output["available_post_event_sessions"] = available
    output["window_status"] = "HAS_MATURED_HORIZON" if available >= 1 else "RIGHT_CENSORED"
    for horizon in HORIZONS:
        output[f"maturity_{horizon}d"] = "MATURED" if available >= horizon else "RIGHT_CENSORED"
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError("maturity output cannot be empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run(args: argparse.Namespace) -> dict[str, Any]:
    readiness_path = Path(args.readiness_csv).resolve()
    prices_path = Path(args.web_prices).resolve()
    terminal_path = Path(args.terminal_events).resolve()
    output_dir = Path(args.output_dir).resolve()
    readiness = read_csv(readiness_path)
    prices, symbols = price_index(read_csv(prices_path))
    terminals = terminal_index(read_csv(terminal_path))
    rows = [assess_row(row, prices, symbols, terminals) for row in readiness]
    maturity_path = output_dir / "human_gold_720_web_price_maturity.csv"
    write_csv(maturity_path, rows)
    status_counts = dict(sorted(Counter(row["window_status"] for row in rows).items()))
    horizon_counts = {
        f"T+{horizon}": sum(row[f"maturity_{horizon}d"] == "MATURED" for row in rows)
        for horizon in HORIZONS
    }
    generated_at = datetime.now(timezone.utc).isoformat()
    report_path = output_dir / "human_gold_720_web_price_maturity_report.md"
    lines = [
        "# Human Gold 720 - Web Price Maturity",
        "",
        f"- Generated: `{generated_at}`",
        f"- Samples: `{len(rows)}`",
        f"- Price series: `{len(prices)}`",
        f"- Latest observed trading date: `{max((day for series in prices.values() for day in series), default=None)}`",
        "- This artifact contains window availability only. It contains no prices, returns, reviewer answers or gold labels.",
        "",
        "## Matured windows",
        "",
        "| horizon | events |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {horizon} | {count} |" for horizon, count in horizon_counts.items())
    lines.extend(["", "## Event status", "", "| status | events |", "| --- | ---: |"])
    lines.extend(f"| {status} | {count} |" for status, count in status_counts.items())
    lines.extend(
        [
            "",
            "A matured window means only that enough trading sessions exist. It does not reveal or imply return direction.",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "sample_count": len(rows),
        "price_series": len(prices),
        "status_counts": status_counts,
        "matured_horizon_counts": horizon_counts,
        "source": {
            "readiness_csv": str(readiness_path),
            "readiness_sha256": sha256_file(readiness_path),
            "web_prices_csv": str(prices_path),
            "web_prices_sha256": sha256_file(prices_path),
            "terminal_events_csv": str(terminal_path),
            "terminal_events_sha256": sha256_file(terminal_path),
        },
        "outputs": {
            "maturity_csv": str(maturity_path),
            "maturity_sha256": sha256_file(maturity_path),
            "report": str(report_path),
            "report_sha256": sha256_file(report_path),
        },
        "invariants": {
            "post_event_returns_included": False,
            "prices_included": False,
            "reviewer_answers_read": False,
            "gold_labels_read": False,
            "missing_values_imputed": False,
            "allowed_as_model_feature": False,
            "live_trading_allowed": False,
        },
    }
    manifest_path = output_dir / "human_gold_720_web_price_maturity_manifest.json"
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
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
