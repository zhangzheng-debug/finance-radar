#!/usr/bin/env python3
"""Schedule and run read-only multi-provider prices for verified events.

Provider policy is deliberately narrow: crypto uses Binance's public spot
market-data-only endpoint; non-crypto assets use Twelve Data.  No API in this
module accepts account credentials or exposes order, position, or balance
operations.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from event_ledger import open_ledger, stable_id, stable_json, utc_now
from telegram_mtproto_listener import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_ENV = ROOT / ".env"
DEFAULT_REPORT = ROOT / "reports" / "live_market_observation_latest.md"
BINANCE_MARKET_DATA_URL = "https://data-api.binance.vision/api/v3/ticker/price"
BINANCE_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,24}$")
HORIZON_WINDOWS: dict[str, tuple[dt.timedelta, dt.timedelta]] = {
    "t_plus_5m": (dt.timedelta(minutes=5), dt.timedelta(minutes=2)),
    "t_plus_30m": (dt.timedelta(minutes=30), dt.timedelta(minutes=5)),
    "t_plus_1d": (dt.timedelta(days=1), dt.timedelta(minutes=30)),
}


def _as_utc(value: str | dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def provider_for_asset(asset_type: str) -> str:
    """Return the reviewed observation provider for an asset class."""
    return "binance_public" if str(asset_type).lower() == "crypto" else "twelve_data"


def binance_symbol(symbol: str) -> str:
    """Map a reviewed crypto base symbol to the public USDT spot pair."""
    base = re.sub(r"[^A-Z0-9]", "", str(symbol).upper())
    candidate = f"{base}USDT"
    if not base or not BINANCE_SYMBOL_PATTERN.fullmatch(candidate):
        raise ValueError(f"Unsupported Binance base symbol: {symbol!r}")
    return candidate


def schedule_jobs(
    connection: Any,
    *,
    freshness_days: int,
    today: dt.date,
    now: dt.datetime | None = None,
) -> int:
    cutoff = today - dt.timedelta(days=freshness_days)
    rows = connection.execute(
        """
        SELECT e.event_id,a.asset_id,a.asset_type
        FROM canonical_events e
        JOIN event_asset_impacts i ON i.event_id=e.event_id
        JOIN assets a ON a.asset_id=i.asset_id
        WHERE e.status='verified' AND e.label_status='verified' AND e.no_trading=1
          AND i.market_observation_allowed=1 AND i.no_trading=1
          AND date(e.event_date) BETWEEN date(?) AND date(?)
        ORDER BY e.event_id,a.asset_id
        """,
        (cutoff.isoformat(), today.isoformat()),
    ).fetchall()
    inserted = 0
    scheduled_at = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    for row in rows:
        provider = provider_for_asset(row["asset_type"])
        before = connection.total_changes
        connection.execute(
            """INSERT OR IGNORE INTO market_jobs(
               market_job_id,event_id,asset_id,provider,observation_window,status,
               scheduled_at,completed_at,attempts,last_error,no_trading
               ) VALUES (?,?,?,?,'initial','PENDING',?,NULL,0,NULL,1)""",
            (
                stable_id("MJOB", row["event_id"], row["asset_id"], provider, "initial"),
                row["event_id"],
                row["asset_id"],
                provider,
                scheduled_at,
            ),
        )
        inserted += connection.total_changes - before
    connection.commit()
    return inserted


def schedule_followup_jobs(connection: Any) -> int:
    """Schedule observer-relative horizons from the first real baseline capture."""
    baselines = connection.execute(
        """
        SELECT j.event_id,j.asset_id,j.provider,MIN(s.captured_at) AS baseline_at
        FROM market_jobs j
        JOIN market_snapshots s ON s.market_job_id=j.market_job_id
        JOIN canonical_events e ON e.event_id=j.event_id
        JOIN event_asset_impacts i ON i.event_id=j.event_id AND i.asset_id=j.asset_id
        WHERE j.observation_window='initial' AND j.status='COMPLETED'
          AND e.status='verified' AND e.label_status='verified' AND e.no_trading=1
          AND i.market_observation_allowed=1 AND i.no_trading=1
        GROUP BY j.event_id,j.asset_id,j.provider
        """
    ).fetchall()
    inserted = 0
    for row in baselines:
        baseline = _as_utc(row["baseline_at"])
        for window, (offset, _grace) in HORIZON_WINDOWS.items():
            before = connection.total_changes
            connection.execute(
                """INSERT OR IGNORE INTO market_jobs(
                   market_job_id,event_id,asset_id,provider,observation_window,status,
                   scheduled_at,completed_at,attempts,last_error,no_trading
                   ) VALUES (?,?,?,?,?,'PENDING',?,NULL,0,NULL,1)""",
                (
                    stable_id("MJOB", row["event_id"], row["asset_id"], row["provider"], window),
                    row["event_id"],
                    row["asset_id"],
                    row["provider"],
                    window,
                    (baseline + offset).isoformat(),
                ),
            )
            inserted += connection.total_changes - before
    connection.commit()
    return inserted


def expire_missed_windows(connection: Any, *, now: dt.datetime) -> int:
    """Close overdue horizons honestly instead of substituting a late/latest quote."""
    now = _as_utc(now)
    rows = connection.execute(
        """SELECT market_job_id,observation_window,scheduled_at
           FROM market_jobs
           WHERE status IN ('PENDING','RETRY') AND observation_window!='initial'
             AND no_trading=1 AND datetime(scheduled_at)<=datetime(?)""",
        (now.isoformat(),),
    ).fetchall()
    missed = 0
    for row in rows:
        window = str(row["observation_window"])
        definition = HORIZON_WINDOWS.get(window)
        if definition is None:
            continue
        scheduled_at = _as_utc(row["scheduled_at"])
        grace = definition[1]
        if now <= scheduled_at + grace:
            continue
        lateness = int((now - scheduled_at).total_seconds())
        connection.execute(
            """UPDATE market_jobs
               SET status='MISSED_WINDOW',completed_at=?,
                   last_error=?,no_trading=1
               WHERE market_job_id=?""",
            (
                now.isoformat(),
                f"capture_window_missed_by_{lateness}s; no historical quote substituted",
                row["market_job_id"],
            ),
        )
        missed += 1
    connection.commit()
    return missed


def fetch_twelve_prices(
    symbols: list[str], api_key: str, timeout: float = 20.0
) -> dict[str, Any]:
    params = urllib.parse.urlencode({"symbol": ",".join(symbols), "apikey": api_key})
    url = f"https://api.twelvedata.com/price?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "FinanceRadar/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Twelve Data HTTP {exc.code}: {detail[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Twelve Data request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Twelve Data returned a non-object response")
    if payload.get("status") == "error":
        raise RuntimeError(f"Twelve Data error: {payload.get('message', 'unknown')}")
    if len(symbols) == 1 and payload.get("price") is not None:
        return {symbols[0]: payload}
    return payload


def fetch_binance_prices(symbols: list[str], timeout: float = 20.0) -> dict[str, Any]:
    """Fetch public spot price tickers without an API key or signed request."""
    invalid = [symbol for symbol in symbols if not BINANCE_SYMBOL_PATTERN.fullmatch(symbol)]
    if invalid:
        raise ValueError(f"Invalid Binance symbols: {', '.join(invalid)}")
    params = urllib.parse.urlencode(
        {"symbols": json.dumps(symbols, ensure_ascii=True, separators=(",", ":"))}
    )
    request = urllib.request.Request(
        f"{BINANCE_MARKET_DATA_URL}?{params}",
        headers={"User-Agent": "FinanceRadar/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Binance public market data HTTP {exc.code}: {detail[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Binance public market data request failed: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Binance public market data returned a non-list response")
    quotes = {
        str(item.get("symbol")): item
        for item in payload
        if isinstance(item, dict) and item.get("symbol") and item.get("price") is not None
    }
    missing = [symbol for symbol in symbols if symbol not in quotes]
    if missing:
        raise RuntimeError(f"Binance public market data missing: {', '.join(missing)}")
    return quotes


def _mark_retry(connection: Any, rows: list[Any], error: str) -> None:
    for row in rows:
        connection.execute(
            """UPDATE market_jobs SET status='RETRY',attempts=attempts+1,last_error=?
               WHERE market_job_id=?""",
            (error[:1000], row["market_job_id"]),
        )
    connection.commit()


def _persist_quotes(
    connection: Any,
    rows: list[Any],
    payload: dict[str, Any],
    *,
    provider: str,
    symbol_for_row: Callable[[Any], str],
    captured_at: str,
) -> tuple[int, int]:
    completed = 0
    errors = 0
    for row in rows:
        provider_symbol = symbol_for_row(row)
        quote = payload.get(provider_symbol)
        if not isinstance(quote, dict) or quote.get("price") is None:
            error_payload = quote if isinstance(quote, dict) else {}
            error = str(error_payload.get("message") or "price missing")[:1000]
            connection.execute(
                """UPDATE market_jobs SET status='RETRY',attempts=attempts+1,last_error=?
                   WHERE market_job_id=?""",
                (error, row["market_job_id"]),
            )
            errors += 1
            continue
        currency = "USDT" if provider == "binance_public" else row["currency"]
        raw_json = stable_json(
            {
                "provider": provider,
                "provider_symbol": provider_symbol,
                "response": quote,
                "authentication_used": False if provider == "binance_public" else "api_key_not_stored",
                "account_endpoint_called": False,
                "order_endpoint_called": False,
                "observation_window": row["observation_window"],
                "scheduled_for": row["scheduled_at"],
                "capture_lag_seconds": max(
                    0,
                    int(
                        (
                            _as_utc(captured_at) - _as_utc(row["scheduled_at"])
                        ).total_seconds()
                    ),
                ),
            }
        )
        snapshot_id = stable_id("SNAP", row["market_job_id"], captured_at)
        if row["observation_window"] == "initial":
            data_scope = (
                "latest_public_spot_price"
                if provider == "binance_public"
                else "latest_provider_price"
            )
        else:
            data_scope = f"observer_relative_{row['observation_window']}"
        lag_seconds = max(
            0,
            int((_as_utc(captured_at) - _as_utc(row["scheduled_at"])).total_seconds()),
        )
        freshness_status = (
            "provider_timestamp_unavailable"
            if row["observation_window"] == "initial"
            else f"window_capture_lag_{lag_seconds}s"
        )
        connection.execute(
            """INSERT INTO market_snapshots(
               snapshot_id,market_job_id,event_id,asset_id,provider,provider_symbol,
               data_scope,price,currency,provider_as_of,captured_at,freshness_status,
               raw_json,read_only,no_trading
               ) VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,?,1,1)""",
            (
                snapshot_id,
                row["market_job_id"],
                row["event_id"],
                row["asset_id"],
                provider,
                provider_symbol,
                data_scope,
                str(quote["price"]),
                currency,
                captured_at,
                freshness_status,
                raw_json,
            ),
        )
        connection.execute(
            """UPDATE market_jobs SET status='COMPLETED',completed_at=?,attempts=attempts+1,
               last_error=NULL,no_trading=1 WHERE market_job_id=?""",
            (captured_at, row["market_job_id"]),
        )
        completed += 1
    connection.commit()
    return completed, errors


def upsert_horizon_metrics(connection: Any, *, updated_at: str | None = None) -> int:
    """Materialize returns only when both baseline and a real horizon snapshot exist."""
    updated_at = updated_at or utc_now()
    rows = connection.execute(
        """
        SELECT h.event_id,h.asset_id,h.provider,h.observation_window,
               hs.provider_symbol,hs.price AS horizon_price,hs.captured_at AS horizon_at,
               bs.price AS baseline_price,bs.captured_at AS baseline_at,
               e.event_date,a.symbol
        FROM market_jobs h
        JOIN market_snapshots hs ON hs.market_job_id=h.market_job_id
        JOIN market_jobs b ON b.event_id=h.event_id AND b.asset_id=h.asset_id
                          AND b.provider=h.provider AND b.observation_window='initial'
        JOIN market_snapshots bs ON bs.market_job_id=b.market_job_id
        JOIN canonical_events e ON e.event_id=h.event_id
        JOIN assets a ON a.asset_id=h.asset_id
        WHERE h.status='COMPLETED' AND h.observation_window IN ('t_plus_5m','t_plus_30m','t_plus_1d')
          AND h.no_trading=1 AND hs.read_only=1 AND hs.no_trading=1
          AND bs.read_only=1 AND bs.no_trading=1
        """
    ).fetchall()
    upserted = 0
    for row in rows:
        try:
            baseline = Decimal(str(row["baseline_price"]))
            horizon = Decimal(str(row["horizon_price"]))
            if baseline == 0:
                continue
            value = ((horizon / baseline) - Decimal("1")) * Decimal("100")
        except (InvalidOperation, ValueError, ZeroDivisionError):
            continue
        metric_name = (
            f"observer_return_{row['observation_window']}_pct__"
            f"{str(row['provider_symbol']).upper()}"
        )
        metric_value = format(value.quantize(Decimal("0.000001")), "f")
        before = connection.total_changes
        connection.execute(
            """
            INSERT INTO event_market_metrics(
                metric_id,event_id,provider,stable_id,ticker_at_event,event_date,event_trade_date,
                benchmark_ticker,metric_name,metric_value,metric_value_type,metric_scope,
                allowed_for_discovery_rank,allowed_as_model_feature,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'post_event_audit_only',0,0,?,?)
            ON CONFLICT(event_id,provider,metric_name) DO UPDATE SET
                metric_value=excluded.metric_value,
                metric_value_type='decimal_percent',
                metric_scope='post_event_audit_only',
                allowed_for_discovery_rank=0,
                allowed_as_model_feature=0,
                updated_at=excluded.updated_at
            """,
            (
                stable_id("MKT", row["event_id"], row["provider"], metric_name),
                row["event_id"],
                row["provider"],
                row["asset_id"],
                row["provider_symbol"],
                row["event_date"],
                str(row["horizon_at"])[:10],
                None,
                metric_name,
                metric_value,
                "decimal_percent",
                updated_at,
                updated_at,
            ),
        )
        upserted += int(connection.total_changes > before)
    connection.commit()
    return upserted


def run_pending(
    connection: Any,
    *,
    api_key: str = "",
    requester: Callable[[list[str], str, float], dict[str, Any]] = fetch_twelve_prices,
    binance_requester: Callable[[list[str], float], dict[str, Any]] = fetch_binance_prices,
    timeout: float = 20.0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = _as_utc(now or dt.datetime.now(dt.timezone.utc))
    missed_windows = expire_missed_windows(connection, now=now)
    rows = connection.execute(
        """
        SELECT j.*,a.provider_symbol,a.currency,a.asset_type,a.symbol
        FROM market_jobs j JOIN assets a ON a.asset_id=j.asset_id
        WHERE j.status IN ('PENDING','RETRY')
          AND j.provider IN ('twelve_data','binance_public')
          AND j.no_trading=1
          AND datetime(j.scheduled_at)<=datetime(?)
        ORDER BY j.scheduled_at,j.market_job_id
        """,
        (now.isoformat(),),
    ).fetchall()
    result: dict[str, Any] = {
        "requested": len(rows),
        "completed": 0,
        "errors": 0,
        "skipped_missing_key": 0,
        "providers": {},
        "missed_windows": missed_windows,
        "metrics_upserted": 0,
    }
    if not rows:
        result["metrics_upserted"] = upsert_horizon_metrics(
            connection, updated_at=now.isoformat()
        )
        return result

    for provider in ("binance_public", "twelve_data"):
        provider_rows = [row for row in rows if row["provider"] == provider]
        if not provider_rows:
            continue
        provider_result = {
            "requested": len(provider_rows),
            "completed": 0,
            "errors": 0,
            "status": "PENDING",
        }
        result["providers"][provider] = provider_result
        if provider == "twelve_data" and not api_key.strip():
            provider_result["status"] = "SKIPPED_MISSING_KEY"
            result["skipped_missing_key"] += len(provider_rows)
            continue
        try:
            if provider == "binance_public":
                symbols = sorted({binance_symbol(row["symbol"]) for row in provider_rows})
                payload = binance_requester(symbols, timeout)
                completed, errors = _persist_quotes(
                    connection,
                    provider_rows,
                    payload,
                    provider=provider,
                    symbol_for_row=lambda row: binance_symbol(row["symbol"]),
                    captured_at=now.isoformat(),
                )
            else:
                symbols = sorted({row["provider_symbol"] for row in provider_rows})
                payload = requester(symbols, api_key, timeout)
                completed, errors = _persist_quotes(
                    connection,
                    provider_rows,
                    payload,
                    provider=provider,
                    symbol_for_row=lambda row: row["provider_symbol"],
                    captured_at=now.isoformat(),
                )
        except (RuntimeError, ValueError) as exc:
            _mark_retry(connection, provider_rows, str(exc))
            completed, errors = 0, len(provider_rows)
        provider_result.update(
            {
                "completed": completed,
                "errors": errors,
                "status": "COMPLETED" if not errors else "DEGRADED",
            }
        )
        result["completed"] += completed
        result["errors"] += errors
    result["metrics_upserted"] = upsert_horizon_metrics(
        connection, updated_at=now.isoformat()
    )
    return result


def write_report(path: Path, connection: Any, scheduled: int, result: dict[str, Any]) -> None:
    rows = connection.execute(
        """SELECT e.company_name,e.event_type,j.observation_window,s.provider,s.provider_symbol,s.price,s.currency,
                  s.captured_at,s.provider_as_of,s.freshness_status
           FROM market_snapshots s
           JOIN market_jobs j ON j.market_job_id=s.market_job_id
           JOIN canonical_events e ON e.event_id=s.event_id
           JOIN assets a ON a.asset_id=s.asset_id
           ORDER BY s.captured_at DESC,s.provider_symbol"""
    ).fetchall()
    jobs = connection.execute(
        """SELECT observation_window,status,COUNT(*) AS count
           FROM market_jobs GROUP BY observation_window,status
           ORDER BY observation_window,status"""
    ).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Live Read-Only Market Observation",
        "",
        f"- Newly scheduled: `{scheduled}`",
        f"- Requested this run: `{result['requested']}`",
        f"- Completed: `{result['completed']}`",
        f"- Errors: `{result['errors']}`",
        f"- Missed windows: `{result.get('missed_windows', 0)}` (never backfilled with a latest quote)",
        f"- Horizon metrics written: `{result.get('metrics_upserted', 0)}`",
        "- Scope: latest provider price only; no order, position, balance, or account endpoint exists.",
        "- Provider policy: crypto -> Binance public spot market data; other assets -> Twelve Data.",
        "- Neither selected price endpoint provides a source timestamp, so snapshots are explicitly marked `provider_timestamp_unavailable`.",
        "",
        "",
        "## Job windows",
        "",
        "| Window | Status | Count |",
        "|---|---|---:|",
    ]
    for job in jobs:
        lines.append(f"| {job['observation_window']} | {job['status']} | {job['count']} |")
    lines.extend(
        [
            "",
            "## Captures",
            "",
            "| Event | Type | Window | Provider | Asset | Price | Captured UTC | Freshness |",
            "|---|---|---|---|---:|---:|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['company_name'] or 'N/A'} | {row['event_type']} | {row['observation_window']} | "
            f"{row['provider']} | {row['provider_symbol']} | {row['price']} {row['currency'] or ''} | "
            f"{row['captured_at']} | {row['freshness_status']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--freshness-days", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--schedule-only", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env_file)
    connection = open_ledger(args.db)
    try:
        scheduled = schedule_jobs(
            connection,
            freshness_days=args.freshness_days,
            today=dt.datetime.now(dt.timezone.utc).date(),
        )
        followups_before = schedule_followup_jobs(connection)
        if args.schedule_only:
            result = {
                "requested": 0,
                "completed": 0,
                "errors": 0,
                "missed_windows": 0,
                "metrics_upserted": 0,
            }
        else:
            api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
            result = run_pending(
                connection, api_key=api_key, timeout=args.timeout
            )
        followups_after = schedule_followup_jobs(connection)
        result["followups_scheduled"] = followups_before + followups_after
        write_report(args.report, connection, scheduled, result)
        print(f"scheduled={scheduled} {stable_json(result)}")
        print(f"REPORT={args.report}")
        return 1 if result["errors"] else 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
