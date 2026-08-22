#!/usr/bin/env python3
"""Repair Yahoo daily-bar scale toggles around stock-split metadata.

Some Yahoo chart responses expose otherwise continuous bars in alternating
pre- and post-split units.  The raw response still contains the split ratio.
For tickers with split metadata, this script chooses among equivalent scale
states with a continuity-minimizing dynamic program, anchors the last observed
bar to the provider's current units, and retains row-level repair provenance.
Tickers without split metadata are never altered.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "yahoo-split-scale-repair-v1"
PRICE_COLUMNS = ("open", "high", "low", "close", "adj_close")


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


def split_metadata(
    status_rows: Iterable[dict[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for status in status_rows:
        if status.get("status") != "OK":
            continue
        ticker = str(status.get("ticker") or "").strip()
        raw_path = Path(str(status.get("raw_path") or ""))
        if not ticker or not raw_path.is_file():
            continue
        payload = json.loads(raw_path.read_text(encoding="utf-8-sig"))
        results = ((payload.get("chart") or {}).get("result") or [])
        if len(results) != 1:
            continue
        splits = (((results[0].get("events") or {}).get("splits") or {}))
        for split in splits.values():
            numerator = float(split.get("numerator") or 0)
            denominator = float(split.get("denominator") or 0)
            if numerator <= 0 or denominator <= 0:
                continue
            event_day = datetime.fromtimestamp(
                int(split["date"]), tz=timezone.utc
            ).date().isoformat()
            output[ticker].append(
                {
                    "date": event_day,
                    "numerator": numerator,
                    "denominator": denominator,
                    "provider_ratio": str(split.get("splitRatio") or ""),
                    "scale_factor": denominator / numerator,
                    "raw_path": str(raw_path.resolve()),
                }
            )
    return dict(output)


def candidate_multipliers(events: list[dict[str, Any]]) -> list[float]:
    states = {1.0}
    for event in events:
        factor = float(event["scale_factor"])
        expanded = set(states)
        for state in states:
            expanded.add(state * factor)
            expanded.add(state / factor)
        states = {round(value, 12) for value in expanded if 1e-9 <= value <= 1e9}
        if len(states) > 25:
            raise ValueError("too many candidate split-scale states")
    return sorted(states)


def choose_scale_path(
    adjusted_closes: list[float],
    multipliers: list[float],
    *,
    switch_penalty: float = 0.05,
) -> list[float]:
    """Choose a smooth equivalent-price path and anchor the final state at 1."""
    if not adjusted_closes:
        return []
    if 1.0 not in multipliers:
        raise ValueError("candidate multipliers must contain 1")
    count = len(adjusted_closes)
    state_count = len(multipliers)
    costs = [[math.inf] * state_count for _ in range(count)]
    previous_state = [[-1] * state_count for _ in range(count)]
    for state in range(state_count):
        costs[0][state] = 0.0
    for index in range(1, count):
        for current_state, current_multiplier in enumerate(multipliers):
            current = adjusted_closes[index] * current_multiplier
            for prior_state, prior_multiplier in enumerate(multipliers):
                prior = adjusted_closes[index - 1] * prior_multiplier
                transition = abs(math.log(current / prior))
                if current_state != prior_state:
                    transition += switch_penalty
                candidate = costs[index - 1][prior_state] + transition
                if candidate < costs[index][current_state]:
                    costs[index][current_state] = candidate
                    previous_state[index][current_state] = prior_state
    final_state = multipliers.index(1.0)
    states = [final_state]
    for index in range(count - 1, 0, -1):
        final_state = previous_state[index][final_state]
        if final_state < 0:
            raise AssertionError("split-scale path backtracking failed")
        states.append(final_state)
    states.reverse()
    return [multipliers[state] for state in states]


def max_step_ratio(rows: list[dict[str, Any]], field: str) -> float:
    maximum = 1.0
    for previous, current in zip(rows, rows[1:]):
        first = float(previous[field])
        second = float(current[field])
        if first > 0 and second > 0:
            maximum = max(maximum, second / first, first / second)
    return maximum


def repair_rows(
    rows: list[dict[str, str]],
    splits: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("ticker") or "")].append(row)
    repaired: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for ticker in sorted(grouped):
        ticker_rows = sorted(grouped[ticker], key=lambda row: str(row.get("date") or ""))
        events = splits.get(ticker, [])
        raw_max_step = max_step_ratio(ticker_rows, "adj_close")
        if events:
            multipliers = candidate_multipliers(events)
            closes = [float(row["adj_close"]) for row in ticker_rows]
            chosen = choose_scale_path(closes, multipliers)
        else:
            multipliers = [1.0]
            chosen = [1.0] * len(ticker_rows)
        changed = sum(not math.isclose(value, 1.0, rel_tol=0, abs_tol=1e-12) for value in chosen)
        split_ratios = ";".join(str(event["provider_ratio"]) for event in events)
        split_dates = ";".join(str(event["date"]) for event in events)
        status = (
            "SPLIT_SCALE_REPAIRED"
            if changed
            else "SPLIT_METADATA_NO_REPAIR_NEEDED"
            if events
            else "UNCHANGED_NO_SPLIT_METADATA"
        )
        output_rows: list[dict[str, Any]] = []
        for source, multiplier in zip(ticker_rows, chosen):
            output: dict[str, Any] = dict(source)
            output["original_adj_close"] = source.get("adj_close")
            for field in PRICE_COLUMNS:
                raw_value = str(source.get(field) or "").strip()
                if raw_value:
                    output[field] = float(raw_value) * multiplier
            output["repair_multiplier"] = multiplier
            output["price_quality_status"] = status
            output["split_event_dates"] = split_dates
            output["split_ratios"] = split_ratios
            output_rows.append(output)
        repaired.extend(output_rows)
        repaired_max_step = max_step_ratio(output_rows, "adj_close")
        summary.append(
            {
                "ticker": ticker,
                "source_symbol": ticker_rows[0].get("source_symbol"),
                "price_rows": len(ticker_rows),
                "split_event_count": len(events),
                "split_event_dates": split_dates,
                "split_ratios": split_ratios,
                "candidate_multipliers": ";".join(f"{value:.12g}" for value in multipliers),
                "repaired_rows": changed,
                "repair_status": status,
                "raw_max_consecutive_scale_ratio": raw_max_step,
                "repaired_max_consecutive_scale_ratio": repaired_max_step,
                "residual_ge_4x_step": repaired_max_step >= 4.0,
            }
        )
    repaired.sort(key=lambda row: (str(row.get("ticker") or ""), str(row.get("date") or "")))
    return repaired, summary


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run(args: argparse.Namespace) -> dict[str, Any]:
    prices_path = Path(args.web_prices).resolve()
    status_path = Path(args.fetch_status).resolve()
    output_dir = Path(args.output_dir).resolve()
    events = split_metadata(read_csv(status_path))
    repaired, summary = repair_rows(read_csv(prices_path), events)
    prices_output = output_dir / "daily_prices_split_repaired.csv"
    summary_output = output_dir / "split_scale_repair_summary.csv"
    write_csv(prices_output, repaired)
    write_csv(summary_output, summary)
    status_counts: dict[str, int] = {}
    for row in summary:
        status = str(row["repair_status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    residual = [row["ticker"] for row in summary if row["residual_ge_4x_step"]]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "split_metadata_ticker_count": len(events),
        "repair_status_counts": dict(sorted(status_counts.items())),
        "residual_ge_4x_step_tickers": residual,
        "source": {
            "web_prices": str(prices_path),
            "web_prices_sha256": sha256_file(prices_path),
            "fetch_status": str(status_path),
            "fetch_status_sha256": sha256_file(status_path),
            "split_metadata_origin": "Yahoo chart raw response events.splits",
        },
        "outputs": {
            "repaired_prices_csv": str(prices_output),
            "repaired_prices_sha256": sha256_file(prices_output),
            "repair_summary_csv": str(summary_output),
            "repair_summary_sha256": sha256_file(summary_output),
        },
        "invariants": {
            "tickers_without_split_metadata_changed": False,
            "final_price_scale_anchored_to_provider": True,
            "rows_dropped": False,
            "missing_prices_imputed": False,
            "raw_provider_responses_preserved": True,
            "owner_only": True,
        },
    }
    manifest_path = output_dir / "split_scale_repair_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-prices", type=Path, required=True)
    parser.add_argument("--fetch-status", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        manifest = run(parse_args(argv))
    except (OSError, ValueError, AssertionError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
