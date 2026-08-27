#!/usr/bin/env python3
"""Run one bounded event-asset mapping and market observation cycle."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from event_ledger import open_ledger, stable_json, utc_now
from map_event_assets import map_event_assets
from observe_live_event_markets import run_pending, schedule_followup_jobs, schedule_jobs
from telegram_mtproto_listener import load_dotenv


DEFAULT_ENV = ROOT / ".env"


def run_market_cycle(
    connection: Any,
    *,
    mapping_mode: str,
    api_key: str,
    timeout: float,
    request_limit: int,
    freshness_days: int = 0,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Map high-confidence assets and drain a bounded set of due price jobs."""

    started_at = utc_now()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    mode = str(mapping_mode or "").strip().lower()
    if mode not in {"shadow", "apply"}:
        mode = "disabled"
    if mode in {"shadow", "apply"}:
        mapping = map_event_assets(
            connection,
            freshness_days=max(0, int(freshness_days)),
            today=today,
            apply=mode == "apply",
        )
    else:
        mapping = {
            "mode": "DISABLED",
            "reason": "FINANCE_RADAR_ASSET_MAPPING_MODE_not_shadow_or_apply",
        }
    scheduled = schedule_jobs(
        connection,
        freshness_days=max(0, int(freshness_days)),
        today=today,
    )
    followups_before = schedule_followup_jobs(connection)
    market = run_pending(
        connection,
        api_key=api_key,
        timeout=max(1.0, float(timeout)),
        max_exact_requests_per_provider=max(1, int(request_limit)),
    )
    followups_after = schedule_followup_jobs(connection)
    market["scheduled"] = scheduled
    market["followups_scheduled"] = followups_before + followups_after
    return {
        "started_at": started_at,
        "finished_at": utc_now(),
        "mapping": mapping,
        "market": market,
        "configuration": {
            "mapping_mode": mode,
            "freshness_days": max(0, int(freshness_days)),
            "request_limit": max(1, int(request_limit)),
        },
        "no_trading": True,
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    """Publish the latest bounded cycle receipt atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--freshness-days", type=int)
    args = parser.parse_args()
    load_dotenv(args.env_file)
    settings = Settings.from_env()
    db_path = (args.db or settings.ledger_db).resolve()
    report_path = (
        args.report or db_path.parent / "market_cycle_latest.json"
    ).resolve()
    try:
        request_limit = max(
            1,
            int(os.environ.get("MARKET_EXACT_BAR_REQUEST_LIMIT", "6") or "6"),
        )
    except ValueError:
        request_limit = 6
    mapping_mode = os.environ.get(
        "FINANCE_RADAR_ASSET_MAPPING_MODE", "shadow"
    ).strip().lower()
    if args.freshness_days is None:
        try:
            freshness_days = max(
                0,
                int(os.environ.get("MARKET_MAPPING_FRESHNESS_DAYS", "0") or "0"),
            )
        except ValueError:
            freshness_days = 0
    else:
        freshness_days = max(0, int(args.freshness_days))
    connection = open_ledger(db_path)
    try:
        result = run_market_cycle(
            connection,
            mapping_mode=mapping_mode,
            api_key=os.environ.get("TWELVE_DATA_API_KEY", "").strip(),
            timeout=args.timeout,
            request_limit=request_limit,
            freshness_days=freshness_days,
        )
    finally:
        connection.close()
    write_report(report_path, result)
    print(stable_json(result))
    print(f"REPORT={report_path}")
    return 1 if result["market"].get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
