#!/usr/bin/env python3
"""Safely reset untouched official-source candidates after a clustering-key change."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from event_ledger import open_ledger, utc_now


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_SOURCES = (
    "federal_reserve_press",
    "sec_current_filings",
    "bls_key_indicators",
)


def reset_candidates(
    connection: Any,
    *,
    source_ids: tuple[str, ...],
    apply: bool,
) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in source_ids)
    event_rows = connection.execute(
        f"""SELECT event_id FROM canonical_events
            WHERE discovery_source IN ({placeholders}) ORDER BY event_id""",
        source_ids,
    ).fetchall()
    event_ids = [row["event_id"] for row in event_rows]
    result: dict[str, Any] = {
        "apply": apply,
        "source_ids": list(source_ids),
        "candidate_events": len(event_ids),
        "reset_observation_jobs": 0,
        "deleted_events": 0,
    }
    if not event_ids:
        return result

    event_placeholders = ",".join("?" for _ in event_ids)
    unsafe: dict[str, int] = {}
    unsafe["reviewed_or_promoted"] = connection.execute(
        f"""SELECT COUNT(*) FROM canonical_events
            WHERE event_id IN ({event_placeholders})
              AND (status!='candidate' OR label_status!='candidate' OR manual_grade IS NOT NULL
                   OR current_version!=1)""",
        event_ids,
    ).fetchone()[0]
    for table in (
        "event_evidence",
        "event_assessments",
        "event_entities",
        "event_asset_impacts",
        "market_jobs",
        "event_market_metrics",
        "alert_outbox",
    ):
        unsafe[table] = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE event_id IN ({event_placeholders})",
            event_ids,
        ).fetchone()[0]
    unsafe["non_target_observations"] = connection.execute(
        f"""SELECT COUNT(*) FROM event_observations eo
            JOIN raw_observations r ON r.observation_id=eo.observation_id
            WHERE eo.event_id IN ({event_placeholders})
              AND r.source_id NOT IN ({placeholders})""",
        (*event_ids, *source_ids),
    ).fetchone()[0]
    result["unsafe_counts"] = unsafe
    if any(unsafe.values()):
        raise RuntimeError(f"refusing to reset reviewed or downstream-linked events: {unsafe}")
    if not apply:
        return result

    now = utc_now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        before = connection.total_changes
        connection.execute(
            f"""UPDATE observation_jobs SET status='PENDING',attempts=0,last_error=NULL,updated_at=?
                WHERE job_type='extract_live_event_candidate'
                  AND observation_id IN (
                      SELECT observation_id FROM raw_observations
                      WHERE source_id IN ({placeholders})
                  )""",
            (now, *source_ids),
        )
        result["reset_observation_jobs"] = connection.total_changes - before
        connection.execute(
            f"DELETE FROM pipeline_jobs WHERE event_id IN ({event_placeholders})", event_ids
        )
        connection.execute(
            f"DELETE FROM event_observations WHERE event_id IN ({event_placeholders})", event_ids
        )
        connection.execute(
            f"DELETE FROM event_versions WHERE event_id IN ({event_placeholders})", event_ids
        )
        before = connection.total_changes
        connection.execute(
            f"DELETE FROM canonical_events WHERE event_id IN ({event_placeholders})", event_ids
        )
        result["deleted_events"] = connection.total_changes - before
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--source-id", action="append", dest="source_ids")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    connection = open_ledger(args.db)
    try:
        result = reset_candidates(
            connection,
            source_ids=tuple(args.source_ids or DEFAULT_SOURCES),
            apply=args.apply,
        )
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
