#!/usr/bin/env python3
"""Fetch and cache daily web prices for the owner-only human-gold audit.

The downloader reads only the already prepared security-mapping CSV.  It does
not read reviewer answers or write into any reviewer package.  Raw provider
responses, normalized rows, failures, hashes and retrieval timestamps are kept
so the downstream outcome audit is reproducible.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import re
import time
from collections import Counter
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


SCHEMA_VERSION = "human-gold-web-prices-v1"
PROVIDER = "yahoo_chart"
ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "FinanceRadar-PostEventAudit/1.0"
PRICE_FIELDS = [
    "provider",
    "ticker",
    "source_symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "currency",
    "exchange",
    "instrument_type",
    "fetched_at",
    "raw_sha256",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def unix_seconds(day: date) -> int:
    return int(datetime.combine(day, datetime_time.min, tzinfo=timezone.utc).timestamp())


def source_symbol_variants(ticker: str, override: str | None = None) -> list[str]:
    """Return conservative Yahoo symbol variants without silently changing issuers."""
    if override:
        return [override]
    variants = [ticker]
    if ticker.endswith(".U"):
        variants.extend([ticker[:-2] + "-UN", ticker[:-2] + "-U"])
    elif ticker.endswith(".WS"):
        variants.extend([ticker[:-3] + "-WT", ticker[:-3] + "-WS"])
    elif "." in ticker:
        variants.append(ticker.replace(".", "-"))
    return list(dict.fromkeys(variants))


def load_symbol_overrides(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    rows = read_csv(path)
    overrides: dict[str, str] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").strip()
        source_symbol = str(row.get("source_symbol") or "").strip()
        if ticker and source_symbol:
            overrides[ticker] = source_symbol
    return overrides


def load_target_tickers(readiness_csv: Path) -> list[str]:
    with readiness_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    tickers = {
        str(row.get("ticker_at_event") or "").strip()
        for row in rows
        if row.get("mapping_status") == "MAPPED"
    }
    tickers.discard("")
    tickers.add("SPY")
    return sorted(tickers)


def _series_value(series: list[Any] | None, index: int) -> Any:
    if not series or index >= len(series):
        return None
    return series[index]


def normalize_chart(
    payload: dict[str, Any],
    *,
    ticker: str,
    source_symbol: str,
    fetched_at: str,
    raw_sha256: str,
) -> list[dict[str, Any]]:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise ValueError(str(chart["error"]))
    results = chart.get("result") or []
    if len(results) != 1:
        raise ValueError("chart response contains no unique result")
    result = results[0]
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adjusted = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get(
        "adjclose"
    ) or []
    rows: list[dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        close = _series_value(quotes.get("close"), index)
        adj_close = _series_value(adjusted, index)
        if close is None or adj_close is None:
            continue
        rows.append(
            {
                "provider": PROVIDER,
                "ticker": ticker,
                "source_symbol": source_symbol,
                "date": datetime.fromtimestamp(int(timestamp), timezone.utc).date().isoformat(),
                "open": _series_value(quotes.get("open"), index),
                "high": _series_value(quotes.get("high"), index),
                "low": _series_value(quotes.get("low"), index),
                "close": close,
                "adj_close": adj_close,
                "volume": _series_value(quotes.get("volume"), index),
                "currency": meta.get("currency"),
                "exchange": meta.get("exchangeName"),
                "instrument_type": meta.get("instrumentType"),
                "fetched_at": fetched_at,
                "raw_sha256": raw_sha256,
            }
        )
    if not rows:
        raise ValueError("chart response contains no usable adjusted daily rows")
    return rows


def _safe_filename(ticker: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", ticker)


def fetch_ticker(
    ticker: str,
    *,
    start_day: date,
    end_day: date,
    raw_dir: Path,
    timeout: float,
    retries: int,
    source_symbol_override: str | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    variants = source_symbol_variants(ticker, source_symbol_override)
    for source_symbol in variants:
        url = ENDPOINT.format(symbol=quote(source_symbol, safe=""))
        params = {
            "period1": unix_seconds(start_day),
            "period2": unix_seconds(end_day + timedelta(days=1)),
            "interval": "1d",
            "events": "div,splits",
            "includeAdjustedClose": "true",
        }
        for attempt in range(retries + 1):
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    timeout=timeout,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise requests.HTTPError(f"HTTP {response.status_code}")
                response.raise_for_status()
                payload = response.json()
                fetched_at = datetime.now(timezone.utc).isoformat()
                raw_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                raw_bytes = (raw_text + "\n").encode("utf-8")
                raw_digest = hashlib.sha256(raw_bytes).hexdigest()
                rows = normalize_chart(
                    payload,
                    ticker=ticker,
                    source_symbol=source_symbol,
                    fetched_at=fetched_at,
                    raw_sha256=raw_digest,
                )
                raw_path = raw_dir / f"{_safe_filename(ticker)}.json"
                raw_path.write_bytes(raw_bytes)
                return {
                    "ticker": ticker,
                    "status": "OK",
                    "source_symbol": source_symbol,
                    "rows": rows,
                    "first_date": rows[0]["date"],
                    "last_date": rows[-1]["date"],
                    "raw_path": str(raw_path),
                    "raw_sha256": raw_digest,
                    "attempts": attempt + 1,
                    "error": "",
                }
            except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
                failures.append(f"{source_symbol}:attempt={attempt + 1}:{exc}")
                if attempt < retries:
                    time.sleep(min(2**attempt, 4))
        # Only try a symbol variant after every retry for the previous spelling failed.
    return {
        "ticker": ticker,
        "status": "FAILED",
        "source_symbol": "",
        "rows": [],
        "first_date": "",
        "last_date": "",
        "raw_path": "",
        "raw_sha256": "",
        "attempts": (retries + 1) * len(variants),
        "error": " | ".join(failures),
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def run(args: argparse.Namespace) -> dict[str, Any]:
    readiness_csv = Path(args.readiness_csv).resolve()
    output_dir = Path(args.output_dir).resolve()
    raw_dir = output_dir / "raw" / PROVIDER
    raw_dir.mkdir(parents=True, exist_ok=True)
    start_day = date.fromisoformat(args.start_date)
    end_day = date.fromisoformat(args.end_date)
    if start_day > end_day:
        raise ValueError("start-date must not be after end-date")
    tickers = load_target_tickers(readiness_csv)
    override_path = Path(args.symbol_overrides).resolve() if args.symbol_overrides else None
    overrides = load_symbol_overrides(override_path)
    price_path = output_dir / "daily_prices.csv"
    status_path = output_dir / "fetch_status.csv"
    prior_prices = read_csv(price_path) if args.resume else []
    prior_statuses = read_csv(status_path) if args.resume else []
    prior_ok = {
        str(row.get("ticker") or "")
        for row in prior_statuses
        if row.get("status") == "OK"
    }
    forced = {
        ticker.strip()
        for ticker in str(args.force_tickers or "").split(",")
        if ticker.strip()
    }
    attempted_tickers = [
        ticker for ticker in tickers if ticker not in prior_ok or ticker in forced
    ]
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=int(args.workers)) as executor:
        futures = {
            executor.submit(
                fetch_ticker,
                ticker,
                start_day=start_day,
                end_day=end_day,
                raw_dir=raw_dir,
                timeout=float(args.timeout),
                retries=int(args.retries),
                source_symbol_override=overrides.get(ticker),
            ): ticker
            for ticker in attempted_tickers
        }
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda row: row["ticker"])
    replaced = {str(row["ticker"]) for row in results}
    prices = sorted(
        [row for row in prior_prices if str(row.get("ticker") or "") not in replaced]
        + [price for result in results for price in result["rows"]],
        key=lambda row: (row["ticker"], row["date"]),
    )
    write_csv(price_path, prices, PRICE_FIELDS)
    status_fields = [
        "ticker",
        "status",
        "source_symbol",
        "first_date",
        "last_date",
        "raw_path",
        "raw_sha256",
        "attempts",
        "error",
    ]
    combined_statuses = sorted(
        [row for row in prior_statuses if str(row.get("ticker") or "") not in replaced]
        + [{key: value for key, value in result.items() if key != "rows"} for result in results],
        key=lambda row: str(row["ticker"]),
    )
    write_csv(
        status_path,
        combined_statuses,
        status_fields,
    )
    statuses = Counter(row["status"] for row in combined_statuses)
    generated_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "provider": PROVIDER,
        "endpoint": ENDPOINT,
        "requested_start_date": start_day.isoformat(),
        "requested_end_date": end_day.isoformat(),
        "requested_tickers": len(tickers),
        "attempted_tickers_this_run": len(attempted_tickers),
        "forced_tickers": sorted(forced),
        "resume": bool(args.resume),
        "status_counts": dict(sorted(statuses.items())),
        "normalized_price_rows": len(prices),
        "observed_min_date": min((row["date"] for row in prices), default=None),
        "observed_max_date": max((row["date"] for row in prices), default=None),
        "source": {
            "readiness_csv": str(readiness_csv),
            "readiness_sha256": sha256_file(readiness_csv),
            "symbol_overrides_csv": str(override_path) if override_path else None,
            "symbol_overrides_sha256": sha256_file(override_path) if override_path else None,
        },
        "outputs": {
            "daily_prices_csv": str(price_path),
            "daily_prices_sha256": sha256_file(price_path),
            "fetch_status_csv": str(status_path),
            "fetch_status_sha256": sha256_file(status_path),
            "raw_directory": str(raw_dir),
        },
        "invariants": {
            "owner_only": True,
            "reviewer_safe": False,
            "reviewer_answers_read": False,
            "reviewer_package_changed": False,
            "adjusted_close_required": True,
            "missing_prices_imputed": False,
            "allowed_as_model_feature": False,
            "live_trading_allowed": False,
        },
    }
    manifest_path = output_dir / "fetch_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readiness-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-date", default="2026-07-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--resume", action="store_true", help="Retry only non-OK prior statuses")
    parser.add_argument("--symbol-overrides", type=Path)
    parser.add_argument("--force-tickers", default="", help="Comma-separated tickers to refetch")
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
