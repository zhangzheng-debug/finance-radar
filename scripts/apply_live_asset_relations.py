#!/usr/bin/env python3
"""Apply reviewed event-entity and event-asset relations for live events."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from event_ledger import open_ledger, stable_id, stable_json, utc_now


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_CONFIG = ROOT / "config" / "live_asset_relations.json"
DEFAULT_REPORT = ROOT / "reports" / "live_asset_relations_latest.md"


def apply_relations(connection: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "events": 0,
        "entities": 0,
        "asset_impacts": 0,
        "market_enabled": 0,
        "stale_event_definitions": 0,
        "stale_event_ids": [],
    }
    now = utc_now()
    for definition in events:
        event = connection.execute(
            "SELECT * FROM canonical_events WHERE event_id=?", (definition["event_id"],)
        ).fetchone()
        if event is None:
            # Canonical quality recovery can intentionally delete an event
            # after a reviewed relation config was published.  The relation
            # is then stale input, not a reason to fail an otherwise healthy
            # collection cycle.  Keep the omission explicit in the report so
            # operators can remove the obsolete config entry.
            result["stale_event_definitions"] += 1
            result["stale_event_ids"].append(str(definition["event_id"]))
            continue
        result["events"] += 1
        for entity in definition.get("entities", []):
            entity_id = stable_id(
                "ENTITY", entity["type"], entity["name"].strip().casefold()
            )
            connection.execute(
                """INSERT INTO entities(
                   entity_id,entity_type,canonical_name,aliases_json,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(entity_id) DO UPDATE SET
                     canonical_name=excluded.canonical_name,updated_at=excluded.updated_at""",
                (
                    entity_id,
                    entity["type"],
                    entity["name"],
                    stable_json(entity.get("aliases", [])),
                    now,
                    now,
                ),
            )
            before = connection.total_changes
            connection.execute(
                """INSERT OR IGNORE INTO event_entities(
                   event_id,entity_id,role,confidence,linked_at) VALUES (?,?,?,?,?)""",
                (
                    definition["event_id"],
                    entity_id,
                    entity["role"],
                    float(entity["confidence"]),
                    now,
                ),
            )
            result["entities"] += connection.total_changes - before
        for asset in definition.get("assets", []):
            allowed = bool(asset["market_observation_allowed"])
            if allowed and event["status"] != "verified":
                raise ValueError(
                    f"Candidate event {definition['event_id']} cannot enable market observation"
                )
            if asset["direction"] != "ABSTAIN":
                raise ValueError("M1 asset relations must use ABSTAIN until separately reviewed")
            asset_id = stable_id(
                "ASSET",
                asset["asset_type"],
                asset["provider_symbol"],
                asset.get("venue", ""),
            )
            connection.execute(
                """INSERT INTO assets(
                   asset_id,asset_type,symbol,provider_symbol,venue,currency,metadata_json,
                   created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(asset_id) DO UPDATE SET
                     symbol=excluded.symbol,provider_symbol=excluded.provider_symbol,
                     currency=excluded.currency,metadata_json=excluded.metadata_json,
                     updated_at=excluded.updated_at""",
                (
                    asset_id,
                    asset["asset_type"],
                    asset["symbol"],
                    asset["provider_symbol"],
                    asset.get("venue", ""),
                    asset.get("currency"),
                    stable_json({"reviewed_config": True}),
                    now,
                    now,
                ),
            )
            impact_id = stable_id(
                "IMPACT", definition["event_id"], asset_id, asset["relation_type"]
            )
            before = connection.total_changes
            connection.execute(
                """INSERT INTO event_asset_impacts(
                   impact_id,event_id,asset_id,relation_type,direction,impact_score,confidence,
                   reason_codes_json,assessment_source,market_observation_allowed,no_trading,
                   created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)
                   ON CONFLICT(event_id,asset_id,relation_type) DO UPDATE SET
                     direction=excluded.direction,impact_score=excluded.impact_score,
                     confidence=excluded.confidence,reason_codes_json=excluded.reason_codes_json,
                     assessment_source=excluded.assessment_source,
                     market_observation_allowed=excluded.market_observation_allowed,
                     no_trading=1,updated_at=excluded.updated_at""",
                (
                    impact_id,
                    definition["event_id"],
                    asset_id,
                    asset["relation_type"],
                    asset["direction"],
                    int(asset["impact_score"]),
                    float(asset["confidence"]),
                    stable_json(asset["reason_codes"]),
                    "manual_review_config",
                    int(allowed),
                    now,
                    now,
                ),
            )
            result["asset_impacts"] += int(connection.total_changes > before)
            result["market_enabled"] += int(allowed)
            if not allowed:
                connection.execute(
                    """UPDATE market_jobs SET status='CANCELLED_RELATION_DISABLED',
                       last_error='market observation relation disabled after review'
                       WHERE event_id=? AND asset_id=? AND status IN ('PENDING','RETRY')""",
                    (definition["event_id"], asset_id),
                )
    connection.commit()
    return result


def write_report(path: Path, events: list[dict[str, Any]], result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Live Entity and Asset Relations",
        "",
        f"- Events: `{result['events']}`",
        f"- New entity links: `{result['entities']}`",
        f"- Asset relations processed: `{result['asset_impacts']}`",
        f"- Market-observation relations: `{result['market_enabled']}`",
        f"- Stale event definitions skipped: `{result['stale_event_definitions']}`",
        "- Direction policy: all M1 relations are `ABSTAIN`; observations are descriptive, not recommendations.",
        "- Candidate events cannot start market observation.",
        "",
    ]
    for event in events:
        lines.extend([f"## {event['event_id']}", ""])
        for asset in event.get("assets", []):
            lines.append(
                f"- `{asset['provider_symbol']}` / `{asset['relation_type']}` / "
                f"impact `{asset['impact_score']}` / confidence `{asset['confidence']}` / "
                f"market `{asset['market_observation_allowed']}`"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    events = json.loads(args.config.read_text(encoding="utf-8"))["events"]
    connection = open_ledger(args.db)
    try:
        result = apply_relations(connection, events)
    finally:
        connection.close()
    write_report(args.report, events, result)
    print(stable_json(result))
    print(f"REPORT={args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
