#!/usr/bin/env python3
"""Cross-check cached adjusted closes against Twelve Data ``adjust=all`` bars."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ENDPOINT = "https://api.twelvedata.com/time_series"
SCHEMA_VERSION = "human-gold-price-crosscheck-v1"


def sanitize_error(value: Any) -> str:
    return re.sub(r"([?&]apikey=)[^&\s)]+", r"\1REDACTED", str(value), flags=re.IGNORECASE)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_api_key(env_file: Path) -> str:
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("TWELVE_DATA_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    raise ValueError("TWELVE_DATA_API_KEY is missing")


def read_web_prices(path: Path) -> dict[str, dict[str, float]]:
    values: dict[str, dict[str, float]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            source_symbol = str(row.get("source_symbol") or "").strip()
            day = str(row.get("date") or "").strip()
            adjusted = str(row.get("adj_close") or "").strip()
            if source_symbol and day and adjusted:
                values.setdefault(source_symbol, {})[day] = float(adjusted)
    return values


def compare_symbol(
    symbol: str,
    response: dict[str, Any],
    web_by_symbol: dict[str, dict[str, float]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if response.get("status") != "ok":
        return (
            {
                "symbol": symbol,
                "status": "PROVIDER_ERROR",
                "overlap_days": 0,
                "max_abs_relative_difference": None,
                "mean_abs_relative_difference": None,
                "within_1bp_rate": None,
                "message": response.get("message"),
            },
            [],
        )
    web = web_by_symbol.get(symbol, {})
    comparisons: list[dict[str, Any]] = []
    for row in response.get("values") or []:
        day = str(row.get("datetime") or "")[:10]
        twelve_raw = row.get("close")
        if day not in web or twelve_raw in (None, ""):
            continue
        twelve = float(twelve_raw)
        yahoo = float(web[day])
        difference = yahoo - twelve
        relative = abs(difference) / abs(twelve) if twelve else None
        comparisons.append(
            {
                "symbol": symbol,
                "date": day,
                "yahoo_adjusted_close": yahoo,
                "twelve_adjust_all_close": twelve,
                "difference": difference,
                "abs_relative_difference": relative,
            }
        )
    relatives = [
        float(row["abs_relative_difference"])
        for row in comparisons
        if row["abs_relative_difference"] is not None
    ]
    return (
        {
            "symbol": symbol,
            "status": "OK" if comparisons else "NO_OVERLAP",
            "overlap_days": len(comparisons),
            "max_abs_relative_difference": max(relatives) if relatives else None,
            "mean_abs_relative_difference": sum(relatives) / len(relatives) if relatives else None,
            "within_1bp_rate": (
                sum(value <= 0.0001 for value in relatives) / len(relatives) if relatives else None
            ),
            "message": "",
        },
        comparisons,
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run(args: argparse.Namespace) -> dict[str, Any]:
    prices_path = Path(args.web_prices).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    symbols = [symbol.strip() for symbol in str(args.symbols).split(",") if symbol.strip()]
    if not symbols:
        raise ValueError("at least one symbol is required")
    api_key = read_api_key(Path(args.env_file).resolve())
    response = None
    request_error: requests.RequestException | None = None
    for attempt in range(4):
        try:
            response = requests.get(
                ENDPOINT,
                params={
                    "symbol": ",".join(symbols),
                    "interval": "1day",
                    "start_date": args.start_date,
                    "end_date": args.end_date,
                    "adjust": "all",
                    "order": "ASC",
                    "outputsize": 100,
                    "apikey": api_key,
                },
                timeout=float(args.timeout),
            )
            request_error = None
            break
        except requests.RequestException as exc:
            request_error = exc
            if attempt < 3:
                time.sleep(2**attempt)
    if response is None:
        raise ValueError(f"Twelve Data request failed: {sanitize_error(request_error)}")
    response.raise_for_status()
    payload = response.json()
    if len(symbols) == 1 and payload.get("status"):
        payload = {symbols[0]: payload}
    raw_path = output_dir / "twelvedata_crosscheck_raw.json"
    raw_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    web = read_web_prices(prices_path)
    summaries: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for symbol in symbols:
        summary, comparison = compare_symbol(symbol, payload.get(symbol) or {}, web)
        summaries.append(summary)
        details.extend(comparison)
    summary_path = output_dir / "price_crosscheck_summary.csv"
    detail_path = output_dir / "price_crosscheck_daily.csv"
    write_csv(summary_path, summaries, list(summaries[0]))
    write_csv(
        detail_path,
        details,
        [
            "symbol",
            "date",
            "yahoo_adjusted_close",
            "twelve_adjust_all_close",
            "difference",
            "abs_relative_difference",
        ],
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "twelve_data_parameters": {
            "interval": "1day",
            "start_date": args.start_date,
            "end_date": args.end_date,
            "adjust": "all",
            "order": "ASC",
        },
        "api_credits_used": response.headers.get("api-credits-used"),
        "api_credits_left": response.headers.get("api-credits-left"),
        "source": {
            "web_prices": str(prices_path),
            "web_prices_sha256": sha256_file(prices_path),
        },
        "outputs": {
            "raw": str(raw_path),
            "raw_sha256": sha256_file(raw_path),
            "summary": str(summary_path),
            "summary_sha256": sha256_file(summary_path),
            "daily": str(detail_path),
            "daily_sha256": sha256_file(detail_path),
        },
        "invariants": {
            "api_key_stored_in_output": False,
            "adjusted_prices_compared": True,
            "missing_values_imputed": False,
            "owner_only": True,
            "allowed_as_model_feature": False,
        },
    }
    manifest_path = output_dir / "price_crosscheck_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-prices", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--start-date", default="2026-07-01")
    parser.add_argument("--end-date", default="2026-08-21")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        manifest = run(parse_args(argv))
    except (OSError, ValueError, requests.RequestException, json.JSONDecodeError) as exc:
        print(f"ERROR: {sanitize_error(exc)}")
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
