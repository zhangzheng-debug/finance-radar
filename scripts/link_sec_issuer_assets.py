#!/usr/bin/env python3
"""Link SEC live events to official CIK/ticker associations and read-only assets."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from event_ledger import open_ledger, stable_id, stable_json, utc_now
from telegram_mtproto_listener import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_ENV = ROOT / ".env"
DEFAULT_CACHE = ROOT / "data" / "cache" / "sec_company_tickers"
DEFAULT_REPORT = ROOT / "reports" / "sec_issuer_assets_latest.md"
SEC_TICKER_INDEX_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
CIK_IN_URL = re.compile(r"/Archives/edgar/data/0*([0-9]{1,10})/", re.IGNORECASE)
CIK_IN_TITLE = re.compile(r"\(([0-9]{10})\)(?:\s+\(Filer\))?", re.IGNORECASE)


Fetcher = Callable[[str, str, float], bytes]


def fetch_index(url: str, user_agent: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(8 * 1024 * 1024)


def load_index(
    cache_dir: Path,
    *,
    user_agent: str,
    timeout: float,
    fetcher: Fetcher = fetch_index,
) -> tuple[dict[int, list[dict[str, str]]], str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "company_tickers_exchange.json"
    has_cache = cache_path.is_file() and cache_path.stat().st_size > 0
    refresh = not has_cache or time.time() - cache_path.stat().st_mtime >= 24 * 3600
    source = "cache"
    if refresh:
        try:
            payload = fetcher(SEC_TICKER_INDEX_URL, user_agent, timeout)
            if not payload:
                raise ValueError("SEC ticker index is empty")
            temporary = cache_path.with_suffix(".json.tmp")
            temporary.write_bytes(payload)
            temporary.replace(cache_path)
            source = "network"
        except (OSError, ValueError, urllib.error.URLError):
            if not has_cache:
                raise
            source = "stale_cache"
    document = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    fields = document.get("fields")
    data = document.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        raise ValueError("SEC ticker index has an unexpected structure")
    field_index = {str(name): index for index, name in enumerate(fields)}
    required = {"cik", "name", "ticker", "exchange"}
    if not required.issubset(field_index):
        raise ValueError("SEC ticker index is missing required fields")
    result: dict[int, list[dict[str, str]]] = {}
    for row in data:
        if not isinstance(row, list) or len(row) < len(fields):
            continue
        try:
            cik = int(row[field_index["cik"]])
        except (TypeError, ValueError):
            continue
        ticker = str(row[field_index["ticker"]] or "").strip().upper()
        if not ticker:
            continue
        result.setdefault(cik, []).append(
            {
                "name": str(row[field_index["name"]] or "").strip(),
                "ticker": ticker,
                "exchange": str(row[field_index["exchange"]] or "").strip(),
            }
        )
    return result, source


def cik_from_facts(facts: dict[str, Any]) -> int | None:
    for pattern, value in (
        (CIK_IN_URL, str(facts.get("canonical_url") or "")),
        (CIK_IN_TITLE, str(facts.get("source_title") or "")),
    ):
        match = pattern.search(value)
        if match:
            return int(match.group(1))
    return None


def link_sec_issuer_assets(
    connection: Any,
    *,
    cache_dir: Path,
    user_agent: str,
    timeout: float = 20.0,
    limit: int = 500,
    fetcher: Fetcher = fetch_index,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "selected": 0,
        "mapped": 0,
        "ticker_updates": 0,
        "assets": 0,
        "market_enabled": 0,
        "unmapped_cik": 0,
        "no_cik": 0,
        "index_source": None,
        "errors": [],
        "policy": {
            "mapping_source": SEC_TICKER_INDEX_URL,
            "candidate_ticker_mapping_allowed": True,
            "market_only_after_verified": True,
            "direction": "ABSTAIN",
            "no_trading": True,
        },
    }
    try:
        index, source = load_index(
            cache_dir,
            user_agent=user_agent,
            timeout=timeout,
            fetcher=fetcher,
        )
        result["index_source"] = source
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        result["errors"].append(f"{type(exc).__name__}: {str(exc)[:300]}")
        return result

    rows = connection.execute(
        """
        SELECT e.event_id,e.status,e.label_status,e.company_name,e.ticker_at_event,
               v.facts_json
        FROM canonical_events e
        JOIN event_versions v ON v.event_id=e.event_id AND v.version=e.current_version
        WHERE e.event_id LIKE 'FR-LIVE-%' AND e.discovery_source='sec_current_filings'
        ORDER BY e.last_updated_at DESC,e.event_id
        LIMIT ?
        """,
        (max(1, min(int(limit), 5000)),),
    ).fetchall()
    result["selected"] = len(rows)
    now = utc_now()
    for row in rows:
        try:
            facts = json.loads(row["facts_json"] or "{}")
        except json.JSONDecodeError:
            result["errors"].append(f"{row['event_id']}: invalid facts_json")
            continue
        cik = cik_from_facts(facts)
        if cik is None:
            result["no_cik"] += 1
            continue
        associations = index.get(cik, [])
        if not associations:
            result["unmapped_cik"] += 1
            continue
        primary = associations[0]
        ticker = primary["ticker"]
        result["mapped"] += 1
        if row["ticker_at_event"] != ticker:
            connection.execute(
                "UPDATE canonical_events SET ticker_at_event=? WHERE event_id=?",
                (ticker, row["event_id"]),
            )
            result["ticker_updates"] += 1
        asset_id = stable_id("ASSET", "equity", ticker, primary["exchange"])
        before = connection.total_changes
        connection.execute(
            """INSERT INTO assets(
               asset_id,asset_type,symbol,provider_symbol,venue,currency,metadata_json,
               created_at,updated_at
               ) VALUES (?,'equity',?,?,?,?,?,?,?)
               ON CONFLICT(asset_id) DO UPDATE SET
                 symbol=excluded.symbol,provider_symbol=excluded.provider_symbol,
                 venue=excluded.venue,currency=excluded.currency,
                 metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (
                asset_id,
                ticker,
                ticker,
                primary["exchange"],
                "USD",
                stable_json(
                    {
                        "mapping_source": SEC_TICKER_INDEX_URL,
                        "cik": cik,
                        "issuer_name": primary["name"],
                        "associations": associations,
                    }
                ),
                now,
                now,
            ),
        )
        result["assets"] += int(connection.total_changes > before)
        if row["status"] != "verified" or row["label_status"] != "verified":
            continue
        before = connection.total_changes
        connection.execute(
            """INSERT INTO event_asset_impacts(
               impact_id,event_id,asset_id,relation_type,direction,impact_score,confidence,
               reason_codes_json,assessment_source,market_observation_allowed,no_trading,
               created_at,updated_at
               ) VALUES (?,?,?,'PRIMARY','ABSTAIN',0,0.95,?, 'sec_official_cik_ticker_mapping',1,1,?,?)
               ON CONFLICT(event_id,asset_id,relation_type) DO UPDATE SET
                 direction='ABSTAIN',impact_score=0,confidence=0.95,
                 reason_codes_json=excluded.reason_codes_json,
                 assessment_source=excluded.assessment_source,
                 market_observation_allowed=1,no_trading=1,updated_at=excluded.updated_at""",
            (
                stable_id("IMPACT", row["event_id"], asset_id, "PRIMARY"),
                row["event_id"],
                asset_id,
                stable_json(["SEC_CIK_TICKER_ASSOCIATION", "POST_EVENT_AUDIT_ONLY"]),
                now,
                now,
            ),
        )
        result["market_enabled"] += int(connection.total_changes > before)
    connection.commit()
    return result


def write_report(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# SEC issuer asset mapping",
        "",
        f"- Events scanned: `{result['selected']}`",
        f"- Official CIK/ticker mappings: `{result['mapped']}`",
        f"- Ticker updates: `{result['ticker_updates']}`",
        f"- Verified events enabled for read-only market observation: `{result['market_enabled']}`",
        f"- Index source: `{result['index_source']}`",
        "- Boundary: official SEC association only; candidates may display a ticker, but only verified events can schedule post-event market observations.",
        "- Direction remains `ABSTAIN`; no order, account, position or balance capability is introduced.",
    ]
    if result["errors"]:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in result["errors"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    load_dotenv(args.env_file)
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent or "@" not in user_agent:
        raise SystemExit("SEC_USER_AGENT with a contact email is required")
    connection = open_ledger(args.db)
    try:
        result = link_sec_issuer_assets(
            connection,
            cache_dir=args.cache_dir,
            user_agent=user_agent,
            timeout=args.timeout,
        )
    finally:
        connection.close()
    write_report(args.report, result)
    print(stable_json(result))
    print(f"REPORT={args.report}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
