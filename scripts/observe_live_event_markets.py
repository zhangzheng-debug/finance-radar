#!/usr/bin/env python3
"""Schedule and run read-only multi-provider post-publication prices.

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
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.event_playbook import time_anchor_for_family
from event_ledger import open_ledger, stable_id, stable_json, utc_now
from telegram_mtproto_listener import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_ENV = ROOT / ".env"
DEFAULT_REPORT = ROOT / "reports" / "live_market_observation_latest.md"
BINANCE_MARKET_DATA_URL = "https://data-api.binance.vision/api/v3/klines"
BINANCE_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,24}$")
ANCHOR_CONTRACT_VERSION = "market-anchor-v1"
WINDOW_CONTRACT_VERSION = "market-windows-v2"
INITIAL_GRACE = dt.timedelta(minutes=2)
MAX_MARKET_ATTEMPTS = 3
EXACT_BAR_INGESTION_GRACE = dt.timedelta(seconds=30)
EXACT_BAR_EMPTY_RETRY_WINDOW = dt.timedelta(minutes=15)
HORIZON_WINDOWS: dict[str, tuple[dt.timedelta, dt.timedelta]] = {
    "t_plus_5m": (dt.timedelta(minutes=5), dt.timedelta(minutes=2)),
    "t_plus_30m": (dt.timedelta(minutes=30), dt.timedelta(minutes=5)),
    "t_plus_2h": (dt.timedelta(hours=2), dt.timedelta(minutes=15)),
    "t_plus_1d": (dt.timedelta(days=1), dt.timedelta(minutes=30)),
    "t_plus_5d": (dt.timedelta(days=5), dt.timedelta(hours=2)),
}


class PermanentMarketDataError(RuntimeError):
    """The provider cannot supply the requested exact minute."""


@dataclass(frozen=True)
class MarketAnchorDecision:
    event_id: str
    event_version: int
    asset_id: str
    provider: str
    declared_anchor_kind: str | None
    reaction_anchor_at: str | None
    source_published_at: str | None
    local_received_at: str | None
    known_at: str | None
    timestamp_precision: str
    anchor_status: str
    anchor_lag_seconds: int | None
    unsupported_windows: tuple[str, ...]
    reason_code: str | None


def _as_utc(value: str | dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _precise_timestamp(value: Any) -> tuple[dt.datetime | None, str]:
    text = str(value or "").strip()
    if not text:
        return None, "MISSING"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None, "DATE_ONLY"
    try:
        return _as_utc(text), "EXACT_TIMESTAMP"
    except (TypeError, ValueError):
        return None, "INVALID"


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _latest_known_at(*values: dt.datetime | None) -> dt.datetime | None:
    exact = [value for value in values if value is not None]
    return max(exact) if exact else None


def _next_regular_close(
    anchor: dt.datetime, *, asset_type: str, metadata: dict[str, Any]
) -> dt.datetime | None:
    if asset_type.lower() == "crypto":
        return None
    timezone_name = str(metadata.get("session_timezone") or "").strip()
    close_text = str(metadata.get("regular_close_local") or "").strip()
    if not timezone_name or not re.fullmatch(r"\d{2}:\d{2}", close_text):
        return None
    try:
        zone = ZoneInfo(timezone_name)
        close_hour, close_minute = (int(part) for part in close_text.split(":"))
    except (ZoneInfoNotFoundError, ValueError):
        return None
    weekdays = metadata.get("trading_weekdays", [0, 1, 2, 3, 4])
    holidays = {str(value) for value in metadata.get("holidays", [])}
    if not isinstance(weekdays, list) or not all(isinstance(value, int) for value in weekdays):
        return None
    local_anchor = anchor.astimezone(zone)
    for day_offset in range(0, 15):
        date = local_anchor.date() + dt.timedelta(days=day_offset)
        if date.weekday() not in weekdays or date.isoformat() in holidays:
            continue
        close_local = dt.datetime.combine(
            date, dt.time(close_hour, close_minute), tzinfo=zone
        )
        if close_local > local_anchor:
            return close_local.astimezone(dt.timezone.utc)
    return None


def _anchor_from_row(row: Any) -> MarketAnchorDecision:
    assessment_source = str(row.get("assessment_source") or "")
    declared = (
        "source_published"
        if assessment_source.startswith("automatic_asset_mapping_v1:")
        else time_anchor_for_family(row["event_family"], row["event_type"])
    )
    facts = _json_object(row["facts_json"])
    published, published_precision = _precise_timestamp(row["source_published_at"])
    received, received_precision = _precise_timestamp(row["local_received_at"])
    known = _latest_known_at(published, received)

    raw_anchor: Any = None
    precision = "MISSING"
    reason: str | None = None
    if declared == "source_published":
        raw_anchor = row["source_published_at"]
        precision = published_precision
    elif declared == "filing_effective":
        raw_anchor = facts.get("filing_effective_at") or facts.get("effective_at")
        _unused, precision = _precise_timestamp(raw_anchor)
    elif declared == "event_occurred":
        raw_anchor = facts.get("event_occurred_at") or facts.get("occurred_at")
        _unused, precision = _precise_timestamp(raw_anchor)
    else:
        reason = "ANCHOR_KIND_UNDECLARED"

    anchor, parsed_precision = _precise_timestamp(raw_anchor)
    if precision == "MISSING":
        precision = parsed_precision
    if reason is None and anchor is None:
        reason = f"{declared or 'anchor'}_{precision.lower()}"
    if reason is None and received is None:
        reason = f"local_received_{received_precision.lower()}"
    if reason is None and known is None:
        reason = "known_at_missing"

    metadata = _json_object(row["metadata_json"])
    unsupported: list[str] = []
    if str(row["asset_type"]).lower() != "crypto" and anchor is not None:
        if _next_regular_close(
            anchor, asset_type=str(row["asset_type"]), metadata=metadata
        ) is None:
            unsupported.append("next_close")

    lag = None
    if anchor is not None and known is not None:
        lag = int((known - anchor).total_seconds())
    return MarketAnchorDecision(
        event_id=str(row["event_id"]),
        event_version=int(row["event_version"]),
        asset_id=str(row["asset_id"]),
        provider=str(row["provider"]),
        declared_anchor_kind=declared,
        reaction_anchor_at=anchor.isoformat() if anchor else None,
        source_published_at=published.isoformat() if published else None,
        local_received_at=received.isoformat() if received else None,
        known_at=known.isoformat() if known else None,
        timestamp_precision=precision,
        anchor_status="EXACT" if reason is None else "UNAVAILABLE",
        anchor_lag_seconds=lag,
        unsupported_windows=tuple(unsupported),
        reason_code=reason,
    )


def _upsert_anchor(connection: Any, decision: MarketAnchorDecision, *, now: str) -> str:
    anchor_id = stable_id(
        "MKTANCHOR",
        decision.event_id,
        str(decision.event_version),
        decision.asset_id,
        decision.provider,
    )
    connection.execute(
        """INSERT INTO market_event_anchors(
               anchor_id,event_id,event_version,asset_id,provider,declared_anchor_kind,
               reaction_anchor_at,source_published_at,local_received_at,known_at,
               timestamp_precision,anchor_status,anchor_lag_seconds,
               unsupported_windows_json,reason_code,contract_version,
               created_at,updated_at,no_trading
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
           ON CONFLICT(event_id,event_version,asset_id,provider) DO UPDATE SET
               declared_anchor_kind=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.declared_anchor_kind ELSE excluded.declared_anchor_kind END,
               reaction_anchor_at=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.reaction_anchor_at ELSE excluded.reaction_anchor_at END,
               source_published_at=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.source_published_at ELSE excluded.source_published_at END,
               local_received_at=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.local_received_at ELSE excluded.local_received_at END,
               known_at=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.known_at ELSE excluded.known_at END,
               timestamp_precision=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.timestamp_precision ELSE excluded.timestamp_precision END,
               anchor_status=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.anchor_status ELSE excluded.anchor_status END,
               anchor_lag_seconds=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.anchor_lag_seconds ELSE excluded.anchor_lag_seconds END,
               unsupported_windows_json=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.unsupported_windows_json ELSE excluded.unsupported_windows_json END,
               reason_code=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.reason_code ELSE excluded.reason_code END,
               contract_version=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.contract_version ELSE excluded.contract_version END,
               updated_at=CASE WHEN market_event_anchors.anchor_status='EXACT'
                   THEN market_event_anchors.updated_at ELSE excluded.updated_at END,
               no_trading=1""",
        (
            anchor_id,
            decision.event_id,
            decision.event_version,
            decision.asset_id,
            decision.provider,
            decision.declared_anchor_kind,
            decision.reaction_anchor_at,
            decision.source_published_at,
            decision.local_received_at,
            decision.known_at,
            decision.timestamp_precision,
            decision.anchor_status,
            decision.anchor_lag_seconds,
            stable_json(list(decision.unsupported_windows)),
            decision.reason_code,
            ANCHOR_CONTRACT_VERSION,
            now,
            now,
        ),
    )
    return anchor_id


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


def cancel_superseded_version_jobs(connection: Any, *, completed_at: str) -> int:
    """Stop unfinished price work that belongs to a non-current event version."""

    before = connection.total_changes
    connection.execute(
        """
        UPDATE market_jobs
           SET status='CANCELLED_EVENT_VERSION_SUPERSEDED',completed_at=?,
               last_error='event_version_superseded',no_trading=1
         WHERE status IN ('PENDING','RETRY')
           AND EXISTS (
             SELECT 1 FROM canonical_events event
              WHERE event.event_id=market_jobs.event_id
                AND event.current_version!=market_jobs.event_version
           )
        """,
        (completed_at,),
    )
    cancelled = connection.total_changes - before
    connection.commit()
    return cancelled


def schedule_jobs(
    connection: Any,
    *,
    freshness_days: int,
    today: dt.date,
    now: dt.datetime | None = None,
) -> int:
    cutoff = today - dt.timedelta(days=freshness_days)
    now_utc = _as_utc(now or dt.datetime.now(dt.timezone.utc))
    persisted_at = now_utc.isoformat()
    cancel_superseded_version_jobs(connection, completed_at=persisted_at)
    rows = connection.execute(
        """
        WITH ranked_evidence AS (
            SELECT er.event_id,er.event_version,ee.evidence_id,
                   r.source_published_at,r.local_received_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY er.event_id,er.event_version
                       ORDER BY CASE er.relation_status
                                  WHEN 'HUMAN_CONFIRMED' THEN 0 ELSE 1 END,
                                COALESCE(r.source_published_at,r.local_received_at) DESC,
                                ee.evidence_id
                   ) AS rank_no
            FROM event_evidence_relations er
            JOIN event_evidence ee ON ee.evidence_id=er.evidence_id
            JOIN raw_observations r ON r.observation_id=ee.observation_id
            WHERE er.relation_status IN ('SCOPED_MATCH','HUMAN_CONFIRMED')
              AND er.subject_match=1 AND er.event_claim_supported=1
              AND er.date_coherent=1
        ),
        current_mapping AS (
            SELECT decision.decision_id,decision.event_id,decision.event_version,
                   receipt.asset_id,receipt.relation_type,
                   decision.source_published_at,decision.local_received_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY decision.event_id,decision.event_version,
                                    receipt.asset_id,receipt.relation_type
                       ORDER BY decision.created_at DESC,decision.decision_id DESC
                   ) AS rank_no
              FROM event_asset_mapping_decisions decision
              JOIN event_asset_mapping_receipts receipt
                ON receipt.mapping_decision_id=decision.decision_id
               AND receipt.event_id=decision.event_id
               AND receipt.event_version=decision.event_version
             WHERE decision.decision='MAPPED'
               AND decision.no_trading=1
               AND receipt.decision='SELECTED'
               AND receipt.no_trading=1
        )
        SELECT e.event_id,e.current_version AS event_version,
               e.event_family,e.event_type,ev.facts_json,
               a.asset_id,a.asset_type,a.metadata_json,
               COALESCE(ranked.source_published_at,mapping.source_published_at)
                   AS source_published_at,
               COALESCE(ranked.local_received_at,mapping.local_received_at)
                   AS local_received_at,
               i.assessment_source,i.mapping_decision_id
         FROM canonical_events e
         JOIN event_versions ev
           ON ev.event_id=e.event_id AND ev.version=e.current_version
         JOIN event_asset_impacts i ON i.event_id=e.event_id
         JOIN assets a ON a.asset_id=i.asset_id
         LEFT JOIN event_fact_workflow workflow
          ON workflow.event_id=e.event_id
         AND workflow.event_version=e.current_version
        LEFT JOIN ranked_evidence ranked
          ON ranked.event_id=e.event_id
         AND ranked.event_version=e.current_version AND ranked.rank_no=1
        LEFT JOIN current_mapping mapping
          ON mapping.decision_id=i.mapping_decision_id
         AND mapping.event_id=e.event_id
         AND mapping.event_version=e.current_version
         AND mapping.asset_id=i.asset_id
         AND mapping.relation_type=i.relation_type
         AND mapping.rank_no=1
        WHERE e.status IN ('candidate','weak','verified') AND e.no_trading=1
          AND i.market_observation_allowed=1 AND i.no_trading=1
          AND (
            (
              i.assessment_source LIKE 'automatic_asset_mapping_v1:%'
              AND i.mapping_decision_id IS NOT NULL
              AND mapping.decision_id IS NOT NULL
              AND i.direction='ABSTAIN' AND i.impact_score=0
            )
            OR (
              workflow.workflow_state='EVIDENCE_READY'
              AND ranked.evidence_id IS NOT NULL
            )
          )
          AND date(e.event_date) BETWEEN date(?) AND date(?)
        ORDER BY e.event_id,a.asset_id
        """,
        (cutoff.isoformat(), today.isoformat()),
    ).fetchall()
    inserted = 0
    for row in rows:
        provider = provider_for_asset(row["asset_type"])
        row_payload = dict(row)
        row_payload["provider"] = provider
        decision = _anchor_from_row(row_payload)
        anchor_id = _upsert_anchor(connection, decision, now=persisted_at)
        persisted_anchor = connection.execute(
            """SELECT anchor_status,reaction_anchor_at FROM market_event_anchors
                 WHERE anchor_id=?""",
            (anchor_id,),
        ).fetchone()
        if (
            persisted_anchor is None
            or persisted_anchor["anchor_status"] != "EXACT"
            or not persisted_anchor["reaction_anchor_at"]
        ):
            continue
        anchor = _as_utc(str(persisted_anchor["reaction_anchor_at"]))
        windows: list[tuple[str, dt.datetime]] = [
            ("initial", anchor),
            *[
                (window, anchor + offset)
                for window, (offset, _grace) in HORIZON_WINDOWS.items()
            ],
        ]
        close = _next_regular_close(
            anchor,
            asset_type=str(row["asset_type"]),
            metadata=_json_object(row["metadata_json"]),
        )
        if close is not None:
            windows.append(("next_close", close))

        for window, scheduled in windows:
            status = "PENDING"
            completed_at = None
            last_error = None
            market_job_id = stable_id(
                "MJOB",
                row["event_id"],
                str(row["event_version"]),
                row["asset_id"],
                provider,
                window,
            )
            before = connection.total_changes
            connection.execute(
                """INSERT OR IGNORE INTO market_jobs(
                   market_job_id,event_id,event_version,asset_id,provider,observation_window,status,
                   scheduled_at,completed_at,attempts,last_error,no_trading
                   ) VALUES (?,?,?,?,?,?,?,?,?,0,?,1)""",
                (
                    market_job_id,
                    row["event_id"],
                    row["event_version"],
                    row["asset_id"],
                    provider,
                    window,
                    status,
                    scheduled.isoformat(),
                    completed_at,
                    last_error,
                ),
            )
            was_inserted = connection.total_changes > before
            if not was_inserted:
                continue
            connection.execute(
                """INSERT INTO market_job_anchor_links(
                       market_job_id,anchor_id,offset_seconds,
                       window_contract_version,created_at
                   ) VALUES (?,?,?,?,?)""",
                (
                    market_job_id,
                    anchor_id,
                    int((scheduled - anchor).total_seconds()),
                    WINDOW_CONTRACT_VERSION,
                    persisted_at,
                ),
            )
            inserted += 1
    connection.commit()
    return inserted


def schedule_followup_jobs(connection: Any) -> int:
    """Compatibility no-op: v2 schedules every window from the frozen event anchor."""
    del connection
    return 0


def expire_missed_windows(
    connection: Any, *, now: dt.datetime, providers: tuple[str, ...] | None = None
) -> int:
    """Close overdue horizons honestly instead of substituting a late/latest quote."""
    now = _as_utc(now)
    if providers == ():
        return 0
    provider_sql = ""
    parameters: list[Any] = [now.isoformat()]
    if providers:
        provider_sql = f" AND provider IN ({','.join('?' for _ in providers)})"
        parameters.extend(providers)
    rows = connection.execute(
        f"""SELECT market_job_id,observation_window,scheduled_at
            FROM market_jobs
            WHERE status IN ('PENDING','RETRY')
              AND no_trading=1 AND datetime(scheduled_at)<=datetime(?)
              {provider_sql}""",
        parameters,
    ).fetchall()
    missed = 0
    for row in rows:
        window = str(row["observation_window"])
        if window == "initial":
            grace = INITIAL_GRACE
        elif window == "next_close":
            grace = dt.timedelta(minutes=30)
        elif window in HORIZON_WINDOWS:
            grace = HORIZON_WINDOWS[window][1]
        else:
            continue
        scheduled_at = _as_utc(row["scheduled_at"])
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


def reopen_exact_historical_windows(
    connection: Any,
    *,
    now: dt.datetime,
    lookback_days: int = 14,
    providers: tuple[str, ...] = ("twelve_data", "binance_public"),
) -> int:
    """Reopen recent jobs when the provider can return the exact target minute.

    This never authorizes a current/latest quote to stand in for an old window.
    It only changes jobs for providers whose production path requests a
    timestamp-bound one-minute bar.
    """

    cutoff = _as_utc(now) - dt.timedelta(days=lookback_days)
    before = connection.total_changes
    if not providers:
        return 0
    provider_sql = ",".join("?" for _ in providers)
    connection.execute(
        f"""
        UPDATE market_jobs
           SET status='PENDING',completed_at=NULL,last_error=NULL,no_trading=1
         WHERE status='MISSED_WINDOW'
           AND provider IN ({provider_sql})
           AND datetime(scheduled_at)>=datetime(?)
           AND datetime(scheduled_at)<=datetime(?)
        """,
        (*providers, cutoff.isoformat(), _as_utc(now).isoformat()),
    )
    reopened = connection.total_changes - before
    connection.commit()
    return reopened


def _bar_cache_key(
    provider: str, asset_id: str, scheduled_at: str
) -> tuple[str, str, str, str]:
    minute, _end = _minute_bounds(scheduled_at)
    return provider, asset_id, "1min", minute.isoformat()


def _normalized_provider_symbol(value: Any) -> str:
    """Normalize only representation-level symbol differences.

    Punctuation is deliberately preserved: ``ETH/USD`` and ``ETHUSDT`` are
    different provider instruments even though a more aggressive ticker
    sanitizer could make them look related.
    """

    return str(value or "").strip().upper()


def _cached_market_bar(
    connection: Any,
    *,
    provider: str,
    asset_id: str,
    provider_symbol: str,
    scheduled_at: str,
) -> dict[str, Any] | None:
    key = _bar_cache_key(provider, asset_id, scheduled_at)
    row = connection.execute(
        """
        SELECT * FROM market_bars
         WHERE provider=? AND asset_id=? AND interval=? AND bar_time=?
           AND read_only=1 AND no_trading=1
        """,
        key,
    ).fetchone()
    if row is None:
        return None
    stored_symbol = str(row["provider_symbol"] or "")
    if (
        not _normalized_provider_symbol(stored_symbol)
        or _normalized_provider_symbol(stored_symbol)
        != _normalized_provider_symbol(provider_symbol)
    ):
        # asset_id is stable, but an asset's provider instrument can be
        # corrected later.  A bar captured under the previous instrument must
        # never be relabelled with the caller's current symbol.
        return None
    return {
        "symbol": stored_symbol.strip(),
        "price": str(row["price"]),
        "provider_as_of": str(row["bar_time"]),
        "interval": str(row["interval"]),
        "price_kind": "bar_close",
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
        "cache_hit": True,
    }


def _store_market_bar(
    connection: Any,
    *,
    provider: str,
    asset_id: str,
    provider_symbol: str,
    scheduled_at: str,
    currency: str | None,
    quote: dict[str, Any],
    fetched_at: str,
) -> None:
    key = _bar_cache_key(provider, asset_id, scheduled_at)
    connection.execute(
        """
        INSERT INTO market_bars(
            provider,asset_id,provider_symbol,interval,bar_time,price,open,high,low,close,
            volume,currency,raw_json,fetched_at,read_only,no_trading
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1)
        ON CONFLICT(provider,asset_id,interval,bar_time) DO UPDATE SET
            provider_symbol=excluded.provider_symbol,price=excluded.price,
            open=excluded.open,high=excluded.high,
            low=excluded.low,close=excluded.close,volume=excluded.volume,
            currency=excluded.currency,raw_json=excluded.raw_json,
            fetched_at=excluded.fetched_at,read_only=1,no_trading=1
        """,
        (
            key[0],
            key[1],
            provider_symbol,
            key[2],
            key[3],
            str(quote["price"]),
            quote.get("open"),
            quote.get("high"),
            quote.get("low"),
            quote.get("close") or quote.get("price"),
            quote.get("volume"),
            currency,
            stable_json(quote),
            fetched_at,
        ),
    )


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


def _minute_bounds(value: str | dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    minute = _as_utc(value).replace(second=0, microsecond=0)
    return minute, minute + dt.timedelta(minutes=1)


def _exact_bar_request_at(value: str | dt.datetime) -> dt.datetime:
    _start, end = _minute_bounds(value)
    return end + EXACT_BAR_INGESTION_GRACE


def _empty_bar_is_retryable(
    scheduled_at: str | dt.datetime, observed_at: str | dt.datetime | None
) -> bool:
    if observed_at is None:
        return False
    _start, end = _minute_bounds(scheduled_at)
    return _as_utc(observed_at) <= end + EXACT_BAR_EMPTY_RETRY_WINDOW


def normalize_twelve_minute_bar(
    payload: dict[str, Any],
    *,
    symbol: str,
    scheduled_at: str,
    observed_at: str | dt.datetime | None = None,
) -> dict[str, Any]:
    if payload.get("status") == "error":
        raise RuntimeError(f"Twelve Data error: {payload.get('message', 'unknown')}")
    values = payload.get("values")
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        if _empty_bar_is_retryable(scheduled_at, observed_at):
            raise RuntimeError("BAR_NOT_AVAILABLE_YET")
        raise PermanentMarketDataError("NO_BAR_PROVIDER_UNAVAILABLE")
    bar = values[0]
    start, end = _minute_bounds(scheduled_at)
    try:
        provider_at = _as_utc(str(bar["datetime"]) + ("+00:00" if "+" not in str(bar["datetime"]) and not str(bar["datetime"]).endswith("Z") else ""))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Twelve Data minute bar timestamp missing") from exc
    if not start <= provider_at < end:
        raise PermanentMarketDataError("BAR_TIMESTAMP_OUTSIDE_REQUESTED_WINDOW")
    close = bar.get("close")
    if close in (None, ""):
        raise PermanentMarketDataError("BAR_CLOSE_MISSING")
    return {
        "symbol": symbol,
        "price": str(close),
        "provider_as_of": provider_at.isoformat(),
        "interval": "1min",
        "price_kind": "bar_close",
        "open": bar.get("open"),
        "high": bar.get("high"),
        "low": bar.get("low"),
        "close": close,
        "volume": bar.get("volume"),
    }


def fetch_twelve_minute_bar(
    symbol: str, scheduled_at: str, api_key: str, timeout: float = 20.0
) -> dict[str, Any]:
    start, end = _minute_bounds(scheduled_at)
    params = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "interval": "1min",
            "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "UTC",
            "order": "ASC",
            "outputsize": 1,
            "apikey": api_key,
        }
    )
    request = urllib.request.Request(
        f"https://api.twelvedata.com/time_series?{params}",
        headers={"User-Agent": "FinanceRadar/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Twelve Data minute bars HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Twelve Data minute bars request failed") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Twelve Data minute bars returned a non-object response")
    return normalize_twelve_minute_bar(
        payload,
        symbol=symbol,
        scheduled_at=scheduled_at,
        observed_at=dt.datetime.now(dt.timezone.utc),
    )


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


def normalize_binance_minute_bar(
    payload: Any,
    *,
    symbol: str,
    scheduled_at: str,
    observed_at: str | dt.datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        if _empty_bar_is_retryable(scheduled_at, observed_at):
            raise RuntimeError("BAR_NOT_AVAILABLE_YET")
        raise PermanentMarketDataError("NO_BAR_PROVIDER_UNAVAILABLE")
    bar = payload[0]
    if len(bar) < 7:
        raise RuntimeError("Binance public minute bar shape invalid")
    start, end = _minute_bounds(scheduled_at)
    try:
        provider_at = dt.datetime.fromtimestamp(int(bar[0]) / 1000, tz=dt.timezone.utc)
        close_at = dt.datetime.fromtimestamp(int(bar[6]) / 1000, tz=dt.timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("Binance public minute timestamp invalid") from exc
    if not start <= provider_at < end:
        raise PermanentMarketDataError("BAR_TIMESTAMP_OUTSIDE_REQUESTED_WINDOW")
    if not start <= close_at < end:
        raise PermanentMarketDataError("BAR_CLOSE_TIMESTAMP_OUTSIDE_REQUESTED_WINDOW")
    if observed_at is not None and close_at >= _as_utc(observed_at):
        raise RuntimeError("BAR_NOT_CLOSED_YET")
    return {
        "symbol": symbol,
        "price": str(bar[4]),
        "provider_as_of": provider_at.isoformat(),
        "interval": "1min",
        "price_kind": "bar_close",
        "open": str(bar[1]),
        "high": str(bar[2]),
        "low": str(bar[3]),
        "close": str(bar[4]),
        "volume": str(bar[5]),
        "close_time_ms": int(bar[6]),
    }


def fetch_binance_minute_bar(
    symbol: str, scheduled_at: str, timeout: float = 20.0
) -> dict[str, Any]:
    if not BINANCE_SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError(f"Invalid Binance symbol: {symbol}")
    start, end = _minute_bounds(scheduled_at)
    params = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "interval": "1m",
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000) - 1,
            "limit": 1,
        }
    )
    request = urllib.request.Request(
        f"{BINANCE_MARKET_DATA_URL}?{params}",
        headers={"User-Agent": "FinanceRadar/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Binance public minute bars HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Binance public minute bars request failed") from exc
    return normalize_binance_minute_bar(
        payload,
        symbol=symbol,
        scheduled_at=scheduled_at,
        observed_at=dt.datetime.now(dt.timezone.utc),
    )


def _mark_retry(connection: Any, rows: list[Any], error: str) -> None:
    for row in rows:
        connection.execute(
            """UPDATE market_jobs
               SET status=CASE WHEN attempts+1>=? THEN 'UNAVAILABLE' ELSE 'RETRY' END,
                   completed_at=CASE WHEN attempts+1>=? THEN ? ELSE NULL END,
                   attempts=MIN(attempts+1,?),last_error=?
               WHERE market_job_id=? AND status IN ('PENDING','RETRY')""",
            (
                MAX_MARKET_ATTEMPTS,
                MAX_MARKET_ATTEMPTS,
                utc_now(),
                MAX_MARKET_ATTEMPTS,
                error[:1000],
                row["market_job_id"],
            ),
        )
    connection.commit()


def _mark_unavailable(connection: Any, rows: list[Any], error: str) -> None:
    completed_at = utc_now()
    for row in rows:
        connection.execute(
            """UPDATE market_jobs
               SET status='UNAVAILABLE',completed_at=?,attempts=attempts+1,last_error=?
               WHERE market_job_id=?""",
            (completed_at, error[:1000], row["market_job_id"]),
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
        quote = payload.get(str(row["market_job_id"])) or payload.get(provider_symbol)
        if not isinstance(quote, dict) or quote.get("price") is None:
            error_payload = quote if isinstance(quote, dict) else {}
            error = str(error_payload.get("message") or "price missing")[:1000]
            _mark_retry(connection, [row], error)
            errors += 1
            continue
        currency = "USDT" if provider == "binance_public" else row["currency"]
        provider_as_of = str(quote.get("provider_as_of") or captured_at)
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
                "reaction_anchor_at": row["reaction_anchor_at"],
                "declared_anchor_kind": row["declared_anchor_kind"],
                "known_at": row["known_at"],
                "anchor_contract_version": row["anchor_contract_version"],
                "window_contract_version": row["window_contract_version"],
                "capture_lag_seconds": max(
                    0,
                    int(
                        (
                            _as_utc(captured_at) - _as_utc(row["scheduled_at"])
                        ).total_seconds()
                    ),
                ),
                "provider_as_of": provider_as_of,
                "interval": quote.get("interval") or "legacy_point",
                "price_kind": quote.get("price_kind") or "point_in_time",
                "retrieval_mode": (
                    "HISTORICAL_EXACT_BAR"
                    if quote.get("interval") == "1min"
                    else "LEGACY_POINT_CAPTURE"
                ),
                "cache_hit": bool(quote.get("cache_hit")),
            }
        )
        snapshot_id = stable_id("SNAP", row["market_job_id"], captured_at)
        data_scope = f"reaction_anchor_relative_{row['observation_window']}"
        lag_seconds = max(
            0,
            int((_as_utc(captured_at) - _as_utc(row["scheduled_at"])).total_seconds()),
        )
        freshness_status = (
            "HISTORICAL_EXACT_BAR"
            if quote.get("interval") == "1min"
            else f"window_capture_lag_{lag_seconds}s"
        )
        connection.execute(
            """INSERT INTO market_snapshots(
               snapshot_id,market_job_id,event_id,asset_id,provider,provider_symbol,
               data_scope,price,currency,provider_as_of,captured_at,freshness_status,
               raw_json,read_only,no_trading
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,1)""",
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
                provider_as_of,
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


def _exact_historical_payload(
    connection: Any,
    rows: list[Any],
    *,
    provider: str,
    symbol_for_row: Callable[[Any], str],
    fetcher: Callable[[str, str], dict[str, Any]] | None,
    fetched_at: str,
    max_provider_requests: int,
) -> tuple[dict[str, Any], list[Any], int, int, int, int]:
    """Fetch each provider/symbol/minute once and fan it out to event jobs."""

    grouped: dict[tuple[str, str, str, str, str], list[Any]] = {}
    for row in rows:
        symbol = symbol_for_row(row)
        cache_key = _bar_cache_key(
            provider, str(row["asset_id"]), str(row["scheduled_at"])
        )
        grouped.setdefault((*cache_key, symbol), []).append(row)

    payload: dict[str, Any] = {}
    ready_rows: list[Any] = []
    errors = 0
    provider_requests = 0
    cache_hits = 0
    deferred = 0
    for (_provider, asset_id, _interval, _bar_time, symbol), grouped_rows in grouped.items():
        scheduled_at = str(grouped_rows[0]["scheduled_at"])
        quote = _cached_market_bar(
            connection,
            provider=provider,
            asset_id=asset_id,
            provider_symbol=symbol,
            scheduled_at=scheduled_at,
        )
        if quote is not None:
            cache_hits += 1
        else:
            if fetcher is None:
                deferred += len(grouped_rows)
                continue
            if provider_requests >= max_provider_requests:
                deferred += len(grouped_rows)
                continue
            try:
                provider_requests += 1
                quote = fetcher(symbol, scheduled_at)
                _store_market_bar(
                    connection,
                    provider=provider,
                    asset_id=asset_id,
                    provider_symbol=symbol,
                    scheduled_at=scheduled_at,
                    currency=(
                        "USDT" if provider == "binance_public" else grouped_rows[0]["currency"]
                    ),
                    quote=quote,
                    fetched_at=fetched_at,
                )
            except PermanentMarketDataError as exc:
                _mark_unavailable(connection, grouped_rows, str(exc))
                errors += len(grouped_rows)
                continue
            except (RuntimeError, ValueError) as exc:
                _mark_retry(connection, grouped_rows, str(exc))
                errors += len(grouped_rows)
                continue
        for row in grouped_rows:
            payload[str(row["market_job_id"])] = quote
            ready_rows.append(row)
    connection.commit()
    return payload, ready_rows, errors, provider_requests, cache_hits, deferred


def upsert_horizon_metrics(connection: Any, *, updated_at: str | None = None) -> int:
    """Materialize returns only when both baseline and a real horizon snapshot exist."""
    updated_at = updated_at or utc_now()
    rows = connection.execute(
        """
        SELECT h.event_id,h.event_version,h.asset_id,h.provider,h.observation_window,
               hs.provider_symbol,hs.price AS horizon_price,
               COALESCE(hs.provider_as_of,hs.captured_at) AS horizon_at,
               bs.price AS baseline_price,
               COALESCE(bs.provider_as_of,bs.captured_at) AS baseline_at,
               e.event_date,a.symbol
        FROM market_jobs h
        JOIN market_snapshots hs ON hs.market_job_id=h.market_job_id
        JOIN market_job_anchor_links horizon_link
          ON horizon_link.market_job_id=h.market_job_id
        JOIN market_jobs b ON b.event_id=h.event_id AND b.asset_id=h.asset_id
                          AND b.event_version=h.event_version
                          AND b.provider=h.provider AND b.observation_window='initial'
        JOIN market_snapshots bs ON bs.market_job_id=b.market_job_id
        JOIN market_job_anchor_links baseline_link
          ON baseline_link.market_job_id=b.market_job_id
         AND baseline_link.anchor_id=horizon_link.anchor_id
        JOIN market_event_anchors anchor
          ON anchor.anchor_id=horizon_link.anchor_id
         AND anchor.anchor_status='EXACT'
        JOIN canonical_events e ON e.event_id=h.event_id
                               AND e.current_version=h.event_version
        JOIN assets a ON a.asset_id=h.asset_id
        WHERE h.status='COMPLETED' AND h.observation_window IN (
              't_plus_5m','t_plus_30m','t_plus_2h','t_plus_1d','t_plus_5d','next_close'
        )
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
            f"reaction_return_{row['observation_window']}_pct__"
            f"{str(row['provider_symbol']).upper()}"
        )
        metric_value = format(value.quantize(Decimal("0.000001")), "f")
        before = connection.total_changes
        connection.execute(
            """
            INSERT INTO event_market_metrics(
                metric_id,event_id,event_version,provider,stable_id,ticker_at_event,event_date,event_trade_date,
                benchmark_ticker,metric_name,metric_value,metric_value_type,metric_scope,
                allowed_for_discovery_rank,allowed_as_model_feature,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'post_event_audit_only',0,0,?,?)
            ON CONFLICT(event_id,event_version,provider,metric_name) DO UPDATE SET
                metric_value=excluded.metric_value,
                metric_value_type='decimal_percent',
                metric_scope='post_event_audit_only',
                allowed_for_discovery_rank=0,
                allowed_as_model_feature=0,
                updated_at=excluded.updated_at
            """,
            (
                stable_id(
                    "MKT",
                    row["event_id"],
                    str(row["event_version"]),
                    row["provider"],
                    metric_name,
                ),
                row["event_id"],
                row["event_version"],
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
    twelve_bar_requester: Callable[[str, str, str, float], dict[str, Any]] = fetch_twelve_minute_bar,
    binance_bar_requester: Callable[[str, str, float], dict[str, Any]] = fetch_binance_minute_bar,
    timeout: float = 20.0,
    now: dt.datetime | None = None,
    max_exact_requests_per_provider: int = 6,
) -> dict[str, Any]:
    now = _as_utc(now or dt.datetime.now(dt.timezone.utc))
    superseded_version_jobs = cancel_superseded_version_jobs(
        connection, completed_at=now.isoformat()
    )
    exact_providers = tuple(
        provider
        for provider, exact
        in (
            ("binance_public", binance_requester is fetch_binance_prices),
            ("twelve_data", requester is fetch_twelve_prices),
        )
        if exact
    )
    legacy_providers = tuple(
        provider
        for provider in ("binance_public", "twelve_data")
        if provider not in exact_providers
    )
    reopened_windows = reopen_exact_historical_windows(
        connection, now=now, providers=exact_providers
    )
    missed_windows = expire_missed_windows(
        connection, now=now, providers=legacy_providers
    )
    rows = connection.execute(
        """
        SELECT j.*,a.provider_symbol,a.currency,a.asset_type,a.symbol,
               anchor.reaction_anchor_at,anchor.declared_anchor_kind,
               anchor.known_at,anchor.contract_version AS anchor_contract_version,
               link.window_contract_version
        FROM market_jobs j
        JOIN canonical_events event
          ON event.event_id=j.event_id AND event.current_version=j.event_version
        JOIN assets a ON a.asset_id=j.asset_id
        JOIN market_job_anchor_links link ON link.market_job_id=j.market_job_id
        JOIN market_event_anchors anchor ON anchor.anchor_id=link.anchor_id
        WHERE j.status IN ('PENDING','RETRY')
          AND j.provider IN ('twelve_data','binance_public')
          AND j.no_trading=1
          AND datetime(j.scheduled_at)<=datetime(?)
          AND datetime(anchor.known_at)<=datetime(?)
        ORDER BY j.attempts ASC,j.scheduled_at DESC,j.market_job_id
        """,
        (now.isoformat(), now.isoformat()),
    ).fetchall()
    rows = [
        row
        for row in rows
        if row["provider"] not in exact_providers
        or now >= _exact_bar_request_at(str(row["scheduled_at"]))
    ]
    result: dict[str, Any] = {
        "requested": len(rows),
        "completed": 0,
        "errors": 0,
        "skipped_missing_key": 0,
        "providers": {},
        "missed_windows": missed_windows,
        "reopened_exact_windows": reopened_windows,
        "superseded_version_jobs": superseded_version_jobs,
        "deferred": 0,
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
            "provider_requests": 0,
            "cache_hits": 0,
            "deferred": 0,
            "status": "PENDING",
        }
        result["providers"][provider] = provider_result
        if (
            provider == "twelve_data"
            and not api_key.strip()
            and provider not in exact_providers
        ):
            provider_result["status"] = "SKIPPED_MISSING_KEY"
            result["skipped_missing_key"] += len(provider_rows)
            continue
        try:
            if provider in exact_providers:
                if provider == "binance_public":
                    symbol_for_row = lambda row: binance_symbol(row["symbol"])
                    fetcher = lambda symbol, scheduled_at: binance_bar_requester(
                        symbol, scheduled_at, timeout
                    )
                else:
                    symbol_for_row = lambda row: str(row["provider_symbol"])
                    fetcher = (
                        None
                        if not api_key.strip()
                        else lambda symbol, scheduled_at: twelve_bar_requester(
                            symbol, scheduled_at, api_key, timeout
                        )
                    )
                (
                    payload,
                    ready_rows,
                    fetch_errors,
                    provider_requests,
                    cache_hits,
                    deferred,
                ) = _exact_historical_payload(
                    connection,
                    provider_rows,
                    provider=provider,
                    symbol_for_row=symbol_for_row,
                    fetcher=fetcher,
                    fetched_at=now.isoformat(),
                    max_provider_requests=max(1, max_exact_requests_per_provider),
                )
                completed, persist_errors = _persist_quotes(
                    connection,
                    ready_rows,
                    payload,
                    provider=provider,
                    symbol_for_row=symbol_for_row,
                    captured_at=now.isoformat(),
                )
                errors = fetch_errors + persist_errors
                provider_result["provider_requests"] = provider_requests
                provider_result["cache_hits"] = cache_hits
                provider_result["deferred"] = deferred
                result["deferred"] += deferred
                if provider == "twelve_data" and not api_key.strip():
                    result["skipped_missing_key"] += deferred
            elif provider == "binance_public":
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
                "status": (
                    "DEGRADED"
                    if errors
                    else "SKIPPED_MISSING_KEY"
                    if (
                        provider == "twelve_data"
                        and not api_key.strip()
                        and provider_result.get("deferred")
                    )
                    else "DEFERRED"
                    if provider_result.get("deferred")
                    else "COMPLETED"
                ),
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
        f"- Exact historical windows reopened: `{result.get('reopened_exact_windows', 0)}`",
        f"- Deferred by provider budget: `{result.get('deferred', 0)}`",
        f"- Horizon metrics written: `{result.get('metrics_upserted', 0)}`",
        "- Scope: event-anchor-relative audit windows; no order, position, balance, or account endpoint exists.",
        "- Provider policy: crypto -> Binance public spot market data; other assets -> Twelve Data.",
        "- A job is scheduled only from an exact, version-bound timestamp. Automatic mappings use the captured source publication time; formal mappings retain the evidence gate.",
        "- Late recovery accepts only the exact target-minute bar; a latest/current quote can never replace an old window.",
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
    parser.add_argument("--freshness-days", type=int, default=14)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-exact-requests", type=int, default=6)
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
                connection,
                api_key=api_key,
                timeout=args.timeout,
                max_exact_requests_per_provider=max(1, args.max_exact_requests),
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
