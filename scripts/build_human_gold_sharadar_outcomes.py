#!/usr/bin/env python3
"""Build a sealed Sharadar T+N outcome audit for frozen human-gold events.

The default ``readiness`` mode only checks issuer mapping and market-data
maturity.  ``outcomes`` mode additionally requires the finalized human-gold
JSONL plus its SHA-256 sidecar, so post-event prices cannot leak into the A/B
review workflow before the human labels are frozen.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import io
import json
import re
import statistics
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "human-gold-sharadar-outcomes-v1"
HORIZONS = (1, 5, 21, 63, 126, 252)
BENCHMARK_TICKER = "SPY"
FILER_CIK_RE = re.compile(r"\((\d{1,10})\)\s*\(Filer\)", re.IGNORECASE)
SEC_URL_CIK_RE = re.compile(r"[?&]CIK=(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class Security:
    cik: str
    permaticker: str
    ticker: str
    name: str
    category: str
    exchange: str
    isdelisted: bool
    firstpricedate: date | None
    lastpricedate: date | None


@dataclass(frozen=True)
class Price:
    day: date
    closeadj: float


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _zip_entry(archive: zipfile.ZipFile, *, suffix: str | None = None) -> zipfile.ZipInfo:
    entries = [entry for entry in archive.infolist() if not entry.is_dir()]
    if suffix is not None:
        entries = [entry for entry in entries if entry.filename.endswith(suffix)]
    if len(entries) != 1:
        label = suffix or "the sole file"
        raise ValueError(f"expected exactly one zip entry matching {label!r}, got {len(entries)}")
    return entries[0]


def load_owner_manifest(package_zip: Path) -> dict[str, Any]:
    with zipfile.ZipFile(package_zip) as archive:
        entry = _zip_entry(archive, suffix="owner_manifest.json")
        with archive.open(entry) as raw:
            manifest = json.load(io.TextIOWrapper(raw, encoding="utf-8-sig"))
    samples = manifest.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("owner manifest contains no samples")
    sample_ids = [str(row.get("sample_id") or "") for row in samples]
    event_ids = [str(row.get("event_id") or "") for row in samples]
    if "" in sample_ids or len(set(sample_ids)) != len(sample_ids):
        raise ValueError("owner manifest sample_id values must be non-empty and unique")
    if "" in event_ids or len(set(event_ids)) != len(event_ids):
        raise ValueError("owner manifest event_id values must be non-empty and unique")
    return manifest


def extract_filer_cik(headline: Any) -> str | None:
    match = FILER_CIK_RE.search(str(headline or ""))
    return match.group(1).zfill(10) if match else None


def load_ticker_metadata(tickers_zip: Path) -> tuple[dict[str, list[Security]], date]:
    by_cik: dict[str, list[Security]] = defaultdict(list)
    cutoff: date | None = None
    with zipfile.ZipFile(tickers_zip) as archive:
        entry = _zip_entry(archive)
        with archive.open(entry) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            required = {
                "table",
                "permaticker",
                "ticker",
                "name",
                "category",
                "exchange",
                "isdelisted",
                "firstpricedate",
                "lastpricedate",
                "secfilings",
            }
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                missing = sorted(required - set(reader.fieldnames or []))
                raise ValueError(f"Sharadar TICKERS is missing columns: {missing}")
            for row in reader:
                lastpricedate = _parse_date(row.get("lastpricedate"))
                if lastpricedate is not None and (cutoff is None or lastpricedate > cutoff):
                    cutoff = lastpricedate
                if str(row.get("table") or "").upper() != "SEP":
                    continue
                match = SEC_URL_CIK_RE.search(str(row.get("secfilings") or ""))
                if not match:
                    continue
                cik = match.group(1).zfill(10)
                by_cik[cik].append(
                    Security(
                        cik=cik,
                        permaticker=str(row.get("permaticker") or "").strip(),
                        ticker=str(row.get("ticker") or "").strip(),
                        name=str(row.get("name") or "").strip(),
                        category=str(row.get("category") or "").strip(),
                        exchange=str(row.get("exchange") or "").strip(),
                        isdelisted=str(row.get("isdelisted") or "").strip().upper() == "Y",
                        firstpricedate=_parse_date(row.get("firstpricedate")),
                        lastpricedate=lastpricedate,
                    )
                )
    if cutoff is None:
        raise ValueError("Sharadar TICKERS contains no lastpricedate values")
    return dict(by_cik), cutoff


def _security_is_eligible(security: Security, event_day: date, cutoff: date) -> bool:
    if not security.ticker or not security.permaticker or security.firstpricedate is None:
        return False
    if security.firstpricedate > event_day:
        return False
    if security.lastpricedate is not None and security.lastpricedate >= event_day:
        return True
    return event_day > cutoff and not security.isdelisted and security.lastpricedate == cutoff


def _dedupe_securities(securities: Iterable[Security]) -> list[Security]:
    by_permaticker: dict[str, Security] = {}
    for security in securities:
        previous = by_permaticker.get(security.permaticker)
        previous_date = previous.lastpricedate if previous else None
        if previous is None or (security.lastpricedate or date.min) > (previous_date or date.min):
            by_permaticker[security.permaticker] = security
    return sorted(by_permaticker.values(), key=lambda row: (row.ticker, row.permaticker))


def choose_security(
    cik: str | None,
    event_day: date,
    by_cik: dict[str, list[Security]],
    cutoff: date,
) -> tuple[str, Security | None, list[Security], str]:
    if cik is None:
        return "NO_FILER_CIK", None, [], "headline_has_no_sec_filer_cik"
    candidates = _dedupe_securities(
        security
        for security in by_cik.get(cik, [])
        if _security_is_eligible(security, event_day, cutoff)
    )
    if not candidates:
        return "NO_SHARADAR_SECURITY", None, [], "no_point_in_time_sep_security"
    if len(candidates) > 1:
        primary = [row for row in candidates if "primary class" in row.category.casefold()]
        if len(primary) == 1:
            candidates = primary
    if len(candidates) != 1:
        return "AMBIGUOUS_SHARADAR_SECURITY", None, candidates, "multiple_point_in_time_securities"
    chosen = candidates[0]
    if event_day > cutoff:
        basis = "active_at_dataset_cutoff_carried_forward"
    else:
        basis = "point_in_time_price_range"
    return "MAPPED", chosen, candidates, basis


def build_readiness_rows(
    samples: list[dict[str, Any]],
    by_cik: dict[str, list[Security]],
    metadata_cutoff: date,
    market_data_cutoff: date | None = None,
) -> list[dict[str, Any]]:
    price_cutoff = market_data_cutoff or metadata_cutoff
    rows: list[dict[str, Any]] = []
    for sample in samples:
        content = sample.get("content") or {}
        event_day = _parse_date(content.get("event_date"))
        if event_day is None:
            raise ValueError(f"sample {sample.get('sample_id')} has no event_date")
        cik = extract_filer_cik(content.get("headline"))
        status, chosen, candidates, basis = choose_security(
            cik, event_day, by_cik, metadata_cutoff
        )
        if status != "MAPPED":
            maturity = "NOT_MAPPABLE"
        elif event_day > price_cutoff:
            maturity = "WAITING_FOR_EVENT_DAY_PRICE"
        else:
            maturity = "EVENT_DAY_WITHIN_DATASET"
        rows.append(
            {
                "sample_id": sample["sample_id"],
                "event_id": sample["event_id"],
                "event_date": event_day.isoformat(),
                "event_family": str(sample.get("event_family") or ""),
                "source_id": str(sample.get("source_id") or ""),
                "headline": str(content.get("headline") or ""),
                "filer_cik": cik or "",
                "mapping_status": status,
                "mapping_basis": basis,
                "candidate_tickers": "|".join(row.ticker for row in candidates),
                "permaticker": chosen.permaticker if chosen else "",
                "ticker_at_event": chosen.ticker if chosen else "",
                "issuer_name": chosen.name if chosen else "",
                "security_category": chosen.category if chosen else "",
                "exchange": chosen.exchange if chosen else "",
                "security_metadata_cutoff": metadata_cutoff.isoformat(),
                "market_data_cutoff": price_cutoff.isoformat(),
                "data_maturity_status": maturity,
                "post_event_market_data_included_in_review": "false",
                "allowed_as_model_feature": "false",
            }
        )
    return rows


def load_and_verify_frozen_gold(path: Path, samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        raise ValueError(f"frozen-gold SHA-256 sidecar is required: {sidecar}")
    expected = sidecar.read_text(encoding="utf-8-sig").strip().split()[0].lower()
    actual = _sha256_file(path)
    if expected != actual:
        raise ValueError("frozen-gold SHA-256 sidecar does not match the dataset")
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    by_sample = {str(row.get("sample_id") or ""): row for row in rows}
    expected_ids = {str(row["sample_id"]) for row in samples}
    if len(by_sample) != len(rows) or set(by_sample) != expected_ids:
        raise ValueError("frozen-gold sample IDs must exactly match the owner manifest")
    expected_events = {str(row["sample_id"]): str(row["event_id"]) for row in samples}
    for sample_id, row in by_sample.items():
        if str(row.get("event_id") or "") != expected_events[sample_id]:
            raise ValueError(f"frozen-gold event_id mismatch for {sample_id}")
        content = row.get("content") or {}
        if content.get("post_event_market_data_included") is not False:
            raise ValueError(f"frozen-gold review leaked post-event market data for {sample_id}")
        if content.get("model_output_included") is not False:
            raise ValueError(f"frozen-gold review included model output for {sample_id}")
        if not str(row.get("label") or "").strip():
            raise ValueError(f"frozen-gold label is missing for {sample_id}")
    return by_sample


def load_selected_prices(sep_zip: Path, tickers: set[str]) -> dict[str, list[Price]]:
    selected = set(tickers) | {BENCHMARK_TICKER}
    prices: dict[str, list[Price]] = defaultdict(list)
    if not tickers:
        return {}
    with zipfile.ZipFile(sep_zip) as archive:
        entry = _zip_entry(archive)
        with archive.open(entry) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            required = {"ticker", "date", "closeadj"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                missing = sorted(required - set(reader.fieldnames or []))
                raise ValueError(f"Sharadar SEP is missing columns: {missing}")
            for row in reader:
                ticker = str(row.get("ticker") or "").strip()
                if ticker not in selected:
                    continue
                day = _parse_date(row.get("date"))
                raw_close = str(row.get("closeadj") or "").strip()
                if day is None or not raw_close:
                    continue
                close = float(raw_close)
                if close > 0:
                    prices[ticker].append(Price(day=day, closeadj=close))
    for ticker in prices:
        prices[ticker].sort(key=lambda row: row.day)
    return dict(prices)


def normalized_web_price_cutoff(path: Path) -> date:
    cutoff: date | None = None
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"ticker", "date", "adj_close"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            missing = sorted(required - set(reader.fieldnames or []))
            raise ValueError(f"normalized web prices are missing columns: {missing}")
        for row in reader:
            day = _parse_date(row.get("date"))
            if day is not None and (cutoff is None or day > cutoff):
                cutoff = day
    if cutoff is None:
        raise ValueError("normalized web prices contain no dates")
    return cutoff


def load_normalized_web_prices(path: Path, tickers: set[str]) -> dict[str, list[Price]]:
    selected = set(tickers) | {BENCHMARK_TICKER}
    prices: dict[str, list[Price]] = defaultdict(list)
    if not tickers:
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"ticker", "date", "adj_close"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            missing = sorted(required - set(reader.fieldnames or []))
            raise ValueError(f"normalized web prices are missing columns: {missing}")
        for row in reader:
            ticker = str(row.get("ticker") or "").strip()
            if ticker not in selected:
                continue
            day = _parse_date(row.get("date"))
            raw_close = str(row.get("adj_close") or "").strip()
            if day is None or not raw_close:
                continue
            close = float(raw_close)
            if close > 0:
                prices[ticker].append(Price(day=day, closeadj=close))
    for ticker in prices:
        prices[ticker].sort(key=lambda row: row.day)
    return dict(prices)


def load_terminal_events(path: Path | None) -> dict[tuple[str, str], str]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            (
                str(row.get("ticker") or "").strip(),
                str(row.get("event_date") or "").strip(),
            ): str(row.get("status") or "TERMINAL_SECURITY_EVENT").strip()
            for row in csv.DictReader(handle)
            if row.get("ticker") and row.get("event_date")
        }


def _return(start: float, end: float) -> float | None:
    return end / start - 1.0 if start > 0 and end > 0 else None


def _benchmark_return(
    benchmark_by_day: dict[date, float], start_day: date, end_day: date
) -> float | None:
    start = benchmark_by_day.get(start_day)
    end = benchmark_by_day.get(end_day)
    if start is None or end is None:
        return None
    return _return(start, end)


def compute_outcome_row(
    readiness: dict[str, Any],
    frozen: dict[str, Any],
    prices: dict[str, list[Price]],
    terminal_status: str | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        **readiness,
        "label": frozen.get("label"),
        "split": frozen.get("split"),
        "benchmark_ticker": BENCHMARK_TICKER,
        "anchor_precision": "DATE_ONLY",
        "event_trade_date": "",
        "event_day_close_to_close": None,
        "outcome_status": "NOT_COMPUTABLE",
        "metric_scope": "post_event_audit_only",
        "reviewer_safe": "false",
        "allowed_for_discovery_rank": "false",
        "allowed_as_model_feature": "false",
    }
    for horizon in HORIZONS:
        output[f"ret_{horizon}d"] = None
        output[f"market_adj_ret_{horizon}d"] = None
        output[f"maturity_{horizon}d"] = "NOT_COMPUTABLE"
    if readiness["mapping_status"] != "MAPPED":
        output["outcome_status"] = "NO_UNAMBIGUOUS_SECURITY"
        return output
    if terminal_status:
        output["outcome_status"] = terminal_status
        terminal_maturity = (
            "MANUAL_CORPORATE_ACTION_REVIEW"
            if terminal_status == "COMPLEX_CORPORATE_ACTION"
            else "TERMINAL_SECURITY_EVENT"
        )
        for horizon in HORIZONS:
            output[f"maturity_{horizon}d"] = terminal_maturity
        return output
    if readiness["data_maturity_status"] == "WAITING_FOR_EVENT_DAY_PRICE":
        output["outcome_status"] = "WAITING_FOR_EVENT_DAY_PRICE"
        for horizon in HORIZONS:
            output[f"maturity_{horizon}d"] = "WAITING_FOR_EVENT_DAY_PRICE"
        return output
    series = prices.get(str(readiness["ticker_at_event"]), [])
    if not series:
        output["outcome_status"] = "NO_PRICE_SERIES"
        return output
    event_day = date.fromisoformat(str(readiness["event_date"]))
    days = [row.day for row in series]
    anchor_index = bisect.bisect_left(days, event_day)
    if anchor_index >= len(series):
        output["outcome_status"] = "WAITING_FOR_EVENT_DAY_PRICE"
        return output
    anchor = series[anchor_index]
    output["event_trade_date"] = anchor.day.isoformat()
    if anchor_index > 0:
        output["event_day_close_to_close"] = _return(
            series[anchor_index - 1].closeadj, anchor.closeadj
        )
    benchmark = {row.day: row.closeadj for row in prices.get(BENCHMARK_TICKER, [])}
    matured = 0
    for horizon in HORIZONS:
        target_index = anchor_index + horizon
        if target_index >= len(series):
            output[f"maturity_{horizon}d"] = "RIGHT_CENSORED"
            continue
        target = series[target_index]
        stock_return = _return(anchor.closeadj, target.closeadj)
        benchmark_return = _benchmark_return(benchmark, anchor.day, target.day)
        output[f"ret_{horizon}d"] = stock_return
        output[f"market_adj_ret_{horizon}d"] = (
            stock_return - benchmark_return
            if stock_return is not None and benchmark_return is not None
            else None
        )
        output[f"maturity_{horizon}d"] = "MATURED"
        matured += 1
    output["outcome_status"] = "HAS_MATURED_HORIZON" if matured else "RIGHT_CENSORED"
    return output


def aggregate_outcomes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregates: list[dict[str, Any]] = []
    group_keys = [("ALL", rows)]
    labels = sorted({str(row.get("label") or "") for row in rows if row.get("label")})
    group_keys.extend((f"LABEL:{label}", [row for row in rows if row.get("label") == label]) for label in labels)
    for group, selected in group_keys:
        for horizon in HORIZONS:
            for metric in (f"ret_{horizon}d", f"market_adj_ret_{horizon}d"):
                values = [float(row[metric]) for row in selected if row.get(metric) is not None]
                aggregates.append(
                    {
                        "group": group,
                        "horizon_trading_days": horizon,
                        "metric": metric,
                        "n": len(values),
                        "mean": statistics.fmean(values) if values else None,
                        "median": statistics.median(values) if values else None,
                        "positive_rate": (
                            sum(value > 0 for value in values) / len(values) if values else None
                        ),
                    }
                )
    return aggregates


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write headerless empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_report(
    path: Path,
    *,
    mode: str,
    sample_count: int,
    cutoff: date,
    readiness_rows: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
) -> None:
    mapping = Counter(row["mapping_status"] for row in readiness_rows)
    maturity = Counter(row["data_maturity_status"] for row in readiness_rows)
    lines = [
        "# Human Gold 720 - Sharadar T+N Audit",
        "",
        f"- Mode: `{mode}`",
        f"- Human-gold samples: `{sample_count}`",
        f"- Sharadar market-data cutoff (from TICKERS): `{cutoff.isoformat()}`",
        f"- Unambiguous security mappings: `{mapping.get('MAPPED', 0)}`",
        f"- Mapped events whose event day is inside the dataset: `{maturity.get('EVENT_DAY_WITHIN_DATASET', 0)}`",
        "- Horizons are trading days: `1, 5, 21, 63, 126, 252`.",
        "- Adjusted close is used; SPY-adjusted return is stock return minus SPY return over identical dates.",
        "- Event timestamps are date-only, so the anchor is the first trading close on or after the event date.",
        "- Results are post-event audit only and must never be shown to A/B reviewers or used as model features.",
        "",
        "## Mapping status",
        "",
        "| status | rows |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {status} | {count} |" for status, count in sorted(mapping.items()))
    lines.extend(["", "## Data maturity", "", "| status | rows |", "| --- | ---: |"])
    lines.extend(f"| {status} | {count} |" for status, count in sorted(maturity.items()))
    if mode == "outcomes":
        statuses = Counter(row["outcome_status"] for row in outcome_rows)
        lines.extend(["", "## Outcome status", "", "| status | rows |", "| --- | ---: |"])
        lines.extend(f"| {status} | {count} |" for status, count in sorted(statuses.items()))
    lines.extend(
        [
            "",
            "Missing or right-censored returns remain blank. They are never converted to zero. "
            "The audit measures association after a frozen event; it does not prove that the event caused the return.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def run(args: argparse.Namespace) -> dict[str, Any]:
    package = Path(args.package).resolve()
    tickers_zip = Path(args.tickers_zip).resolve()
    sep_zip = Path(args.sep_zip).resolve() if getattr(args, "sep_zip", None) else None
    web_prices_path = (
        Path(args.web_prices).resolve() if getattr(args, "web_prices", None) else None
    )
    if (sep_zip is None) == (web_prices_path is None):
        raise ValueError("provide exactly one of --sep-zip or --web-prices")
    terminal_path = (
        Path(args.terminal_events).resolve()
        if getattr(args, "terminal_events", None)
        else None
    )
    output_dir = Path(args.output_dir).resolve()
    manifest = load_owner_manifest(package)
    samples = list(manifest["samples"])
    by_cik, metadata_cutoff = load_ticker_metadata(tickers_zip)
    market_cutoff = (
        normalized_web_price_cutoff(web_prices_path)
        if web_prices_path is not None
        else metadata_cutoff
    )
    readiness_rows = build_readiness_rows(
        samples, by_cik, metadata_cutoff, market_cutoff
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    readiness_path = output_dir / "human_gold_720_sharadar_readiness.csv"
    _write_csv(readiness_path, readiness_rows)

    outcome_rows: list[dict[str, Any]] = []
    aggregate_rows: list[dict[str, Any]] = []
    frozen_path: Path | None = None
    if args.mode == "outcomes":
        if not args.frozen_gold:
            raise ValueError("--frozen-gold is required in outcomes mode")
        frozen_path = Path(args.frozen_gold).resolve()
        frozen = load_and_verify_frozen_gold(frozen_path, samples)
        computable_tickers = {
            str(row["ticker_at_event"])
            for row in readiness_rows
            if row["mapping_status"] == "MAPPED"
            and date.fromisoformat(str(row["event_date"])) <= market_cutoff
        }
        prices = (
            load_normalized_web_prices(web_prices_path, computable_tickers)
            if web_prices_path is not None
            else load_selected_prices(sep_zip, computable_tickers)  # type: ignore[arg-type]
        )
        terminals = load_terminal_events(terminal_path)
        outcome_rows = [
            compute_outcome_row(
                row,
                frozen[str(row["sample_id"])],
                prices,
                terminals.get((str(row["ticker_at_event"]), str(row["event_date"]))),
            )
            for row in readiness_rows
        ]
        _write_csv(output_dir / "human_gold_720_sharadar_outcomes.csv", outcome_rows)
        aggregate_rows = aggregate_outcomes(outcome_rows)
        _write_csv(output_dir / "human_gold_720_sharadar_summary.csv", aggregate_rows)

    report_path = output_dir / "human_gold_720_sharadar_report.md"
    write_report(
        report_path,
        mode=args.mode,
        sample_count=len(samples),
        cutoff=market_cutoff,
        readiness_rows=readiness_rows,
        outcome_rows=outcome_rows,
    )
    mapping_counts = dict(sorted(Counter(row["mapping_status"] for row in readiness_rows).items()))
    maturity_counts = dict(
        sorted(Counter(row["data_maturity_status"] for row in readiness_rows).items())
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    output_manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "mode": args.mode,
        "batch_id": manifest.get("batch_id"),
        "sample_count": len(samples),
        "security_metadata_cutoff": metadata_cutoff.isoformat(),
        "market_data_cutoff": market_cutoff.isoformat(),
        "horizons_trading_days": list(HORIZONS),
        "mapping_counts": mapping_counts,
        "data_maturity_counts": maturity_counts,
        "outcome_status_counts": dict(
            sorted(Counter(row["outcome_status"] for row in outcome_rows).items())
        ),
        "source_files": {
            "owner_package": {"path": str(package), "sha256": _sha256_file(package)},
            "sharadar_tickers": {"path": str(tickers_zip), "sha256": _sha256_file(tickers_zip)},
            "sharadar_sep": (
                {
                    "path": str(sep_zip),
                    "sha256": _sha256_file(sep_zip),
                    "scanned": args.mode == "outcomes" and any(
                    row["mapping_status"] == "MAPPED"
                    and date.fromisoformat(str(row["event_date"])) <= market_cutoff
                    for row in readiness_rows
                    ),
                }
                if sep_zip is not None
                else None
            ),
            "web_market_prices": (
                {"path": str(web_prices_path), "sha256": _sha256_file(web_prices_path)}
                if web_prices_path is not None
                else None
            ),
            "terminal_events": (
                {"path": str(terminal_path), "sha256": _sha256_file(terminal_path)}
                if terminal_path is not None
                else None
            ),
            "frozen_gold": (
                {"path": str(frozen_path), "sha256": _sha256_file(frozen_path)}
                if frozen_path
                else None
            ),
        },
        "outputs": {
            "readiness_csv": str(readiness_path),
            "outcomes_csv": (
                str(output_dir / "human_gold_720_sharadar_outcomes.csv") if outcome_rows else None
            ),
            "summary_csv": (
                str(output_dir / "human_gold_720_sharadar_summary.csv") if aggregate_rows else None
            ),
            "report": str(report_path),
        },
        "invariants": {
            "human_labels_must_be_frozen_before_outcomes": True,
            "post_event_market_data_included_in_review": False,
            "reviewer_safe": False,
            "metric_scope": "post_event_audit_only",
            "allowed_for_discovery_rank": False,
            "allowed_as_model_feature": False,
            "missing_returns_imputed_as_zero": False,
            "causal_claim_allowed": False,
            "provider_read_only": True,
            "live_trading_allowed": False,
        },
    }
    manifest_path = output_dir / "human_gold_720_sharadar_manifest.json"
    manifest_path.write_text(
        json.dumps(output_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return output_manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True, help="Team delivery ZIP")
    parser.add_argument("--tickers-zip", type=Path, required=True, help="Sharadar TICKERS ZIP")
    price_group = parser.add_mutually_exclusive_group(required=True)
    price_group.add_argument("--sep-zip", type=Path, help="Sharadar SEP ZIP")
    price_group.add_argument("--web-prices", type=Path, help="Normalized web daily_prices.csv")
    parser.add_argument("--terminal-events", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("readiness", "outcomes"), default="readiness")
    parser.add_argument("--frozen-gold", type=Path, help="Final frozen human-gold JSONL")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(parse_args(argv))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
