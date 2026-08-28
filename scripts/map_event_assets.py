#!/usr/bin/env python3
"""Apply deterministic, versioned, read-only event-to-asset mappings."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.event_asset_mapping import (  # noqa: E402
    MAPPING_PATH,
    AssetMappingPolicy,
    load_asset_mapping_policy,
    resolve_event_assets,
)
from app.models.issuer_directory import IssuerDirectory, load_issuer_directory  # noqa: E402
from event_ledger import open_ledger, stable_id, stable_json, utc_now  # noqa: E402


DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_REPORT = ROOT / "reports" / "event_asset_mapping_shadow_latest.md"
AUTO_SOURCE_PREFIX = "automatic_asset_mapping_v1:"


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _selected_events(
    connection: Any,
    *,
    freshness_days: int,
    today: dt.date,
    event_ids: Iterable[str] | None = None,
    policy_sha256: str | None = None,
    only_unmapped: bool = False,
) -> list[dict[str, Any]]:
    cutoff = today - dt.timedelta(days=freshness_days)
    requested = [str(value) for value in (event_ids or []) if str(value).strip()]
    date_filter = ""
    parameters: list[Any] = []
    if not requested:
        date_filter = " AND date(e.event_date) BETWEEN date(?) AND date(?)"
        parameters.extend((cutoff.isoformat(), today.isoformat()))
    event_filter = ""
    if requested:
        event_filter = f" AND e.event_id IN ({','.join('?' for _ in requested)})"
        parameters.extend(requested)
    policy_filter = ""
    if only_unmapped and policy_sha256:
        policy_filter = """
           AND NOT EXISTS (
             SELECT 1 FROM event_asset_mapping_decisions decision
             WHERE decision.event_id=e.event_id
                AND decision.event_version=e.current_version
                AND decision.policy_sha256=?
                AND decision.observation_id=COALESCE(capture.observation_id,'')
                AND decision.source_content_sha256=COALESCE(capture.content_sha256,'')
                AND (
                  (
                    decision.decision='NO_MATCH'
                    AND NOT EXISTS (
                      SELECT 1 FROM event_asset_impacts old_impact
                       WHERE old_impact.event_id=e.event_id
                         AND old_impact.assessment_source LIKE 'automatic_asset_mapping_v1:%'
                         AND old_impact.market_observation_allowed=1
                    )
                  )
                  OR
                  (
                    decision.decision='MAPPED'
                    AND decision.asset_count=(
                      SELECT COUNT(*) FROM event_asset_mapping_receipts receipt
                       WHERE receipt.mapping_decision_id=decision.decision_id
                         AND receipt.decision='SELECTED'
                    )
                    AND decision.asset_count=(
                      SELECT COUNT(*) FROM event_asset_impacts current_impact
                       WHERE current_impact.mapping_decision_id=decision.decision_id
                         AND current_impact.market_observation_allowed=1
                    )
                    AND decision.asset_count=(
                      SELECT COUNT(*) FROM event_asset_impacts all_impact
                       WHERE all_impact.event_id=e.event_id
                         AND all_impact.assessment_source LIKE 'automatic_asset_mapping_v1:%'
                         AND all_impact.market_observation_allowed=1
                    )
                  )
                )
           )
        """
        parameters.append(policy_sha256)
    rows = connection.execute(
        f"""
        WITH ranked_capture AS (
            SELECT eo.event_id,source.observation_id,source.title,source.summary,
                   source.content_sha256,
                   source.source_published_at,source.local_received_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY eo.event_id
                       ORDER BY COALESCE(source.source_published_at,source.local_received_at) DESC,
                                source.observation_id
                   ) AS rank_no
              FROM event_observations eo
              JOIN latest_source_content source
                ON source.observation_id=eo.observation_id
             WHERE source.observation_status!='deleted'
               AND eo.relation_type!='filtered_aggregated_noise'
        )
        SELECT e.event_id,e.current_version,e.status,e.event_family,e.event_type,
               e.event_date,e.ticker_at_event,e.company_name,ev.facts_json,
               e.discovery_source,
               capture.observation_id,capture.title AS source_title,
               capture.summary AS source_summary,capture.content_sha256,
               capture.source_published_at,
               capture.local_received_at
          FROM canonical_events e
          JOIN event_versions ev
            ON ev.event_id=e.event_id AND ev.version=e.current_version
          LEFT JOIN ranked_capture capture
            ON capture.event_id=e.event_id AND capture.rank_no=1
         WHERE e.status IN ('candidate','weak','verified') AND e.no_trading=1
           {date_filter}
           {event_filter}
           {policy_filter}
         ORDER BY e.event_date DESC,e.event_id
        """,
        parameters,
    ).fetchall()
    selected: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["facts"] = _json_object(item.get("facts_json"))
        selected.append(item)
    return selected


def _existing_asset_id(connection: Any, mapping: dict[str, Any]) -> str | None:
    row = connection.execute(
        """
        SELECT asset_id FROM assets
         WHERE LOWER(asset_type)=LOWER(?)
           AND UPPER(provider_symbol)=UPPER(?)
           AND venue=?
         ORDER BY updated_at DESC,asset_id
         LIMIT 1
        """,
        (mapping["asset_type"], mapping["provider_symbol"], mapping["venue"]),
    ).fetchone()
    return str(row["asset_id"]) if row is not None else None


def _upsert_asset(
    connection: Any, mapping: dict[str, Any], *, now: str
) -> str:
    asset_id = _existing_asset_id(connection, mapping) or stable_id(
        "ASSET",
        str(mapping["asset_type"]),
        str(mapping["provider_symbol"]),
        str(mapping["venue"]),
    )
    existing = connection.execute(
        "SELECT metadata_json FROM assets WHERE asset_id=?", (asset_id,)
    ).fetchone()
    metadata = _json_object(existing["metadata_json"] if existing is not None else "{}")
    if (
        str(mapping["asset_type"]).lower() in {"equity", "etf"}
        and str(mapping["venue"]).strip().upper()
        in {"", "NYSE", "NYSE AMERICAN", "NASDAQ", "TWELVEDATA"}
    ):
        metadata.setdefault("session_timezone", "America/New_York")
        metadata.setdefault("regular_open_local", "09:30")
        metadata.setdefault("regular_close_local", "16:00")
        metadata.setdefault("trading_weekdays", [0, 1, 2, 3, 4])
        metadata.setdefault("holidays", [])
    connection.execute(
        """
        INSERT INTO assets(
            asset_id,asset_type,symbol,provider_symbol,venue,currency,metadata_json,
            created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(asset_id) DO UPDATE SET
            symbol=excluded.symbol,provider_symbol=excluded.provider_symbol,
            currency=COALESCE(excluded.currency,assets.currency),
            metadata_json=excluded.metadata_json,updated_at=excluded.updated_at
        """,
        (
            asset_id,
            mapping["asset_type"],
            mapping["symbol"],
            mapping["provider_symbol"],
            mapping["venue"],
            mapping["currency"],
            stable_json(metadata),
            now,
            now,
        ),
    )
    return asset_id


def _decision_binding(
    event: dict[str, Any], policy: AssetMappingPolicy
) -> tuple[str, str, str]:
    observation_id = str(event.get("observation_id") or "")
    source_content_sha256 = str(event.get("content_sha256") or "")
    decision_id = stable_id(
        "AMAPDEC",
        str(event["event_id"]),
        str(event["current_version"]),
        policy.policy_sha256,
        observation_id,
        source_content_sha256,
    )
    return decision_id, observation_id, source_content_sha256


def _insert_decision(
    connection: Any,
    *,
    decision_id: str,
    event: dict[str, Any],
    policy: AssetMappingPolicy,
    decision: str,
    rule_id: str | None,
    asset_count: int,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO event_asset_mapping_decisions(
            decision_id,event_id,event_version,policy_version,policy_sha256,
            observation_id,source_content_sha256,source_published_at,
            local_received_at,decision,rule_id,asset_count,created_at,no_trading
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
        """,
        (
            decision_id,
            event["event_id"],
            event["current_version"],
            policy.policy_version,
            policy.policy_sha256,
            str(event.get("observation_id") or ""),
            str(event.get("content_sha256") or ""),
            event.get("source_published_at"),
            event.get("local_received_at"),
            decision,
            rule_id,
            asset_count,
            now,
        ),
    )


def _deactivate_old_automatic_impacts(
    connection: Any,
    *,
    event_id: str,
    keep_projection_keys: set[tuple[str, str]],
    now: str,
) -> int:
    rows = connection.execute(
        """
        SELECT asset_id,relation_type FROM event_asset_impacts
         WHERE event_id=? AND assessment_source LIKE ?
           AND market_observation_allowed=1
        """,
        (event_id, f"{AUTO_SOURCE_PREFIX}%"),
    ).fetchall()
    superseded = [
        row
        for row in rows
        if (str(row["asset_id"]), str(row["relation_type"]))
        not in keep_projection_keys
    ]
    keep_asset_ids = {asset_id for asset_id, _relation_type in keep_projection_keys}
    removed_asset_ids = sorted(
        {
            str(row["asset_id"])
            for row in superseded
            if str(row["asset_id"]) not in keep_asset_ids
        }
    )
    for row in superseded:
        connection.execute(
            """
            UPDATE event_asset_impacts
               SET market_observation_allowed=0,updated_at=?,no_trading=1
             WHERE event_id=? AND asset_id=? AND relation_type=?
            """,
            (now, event_id, row["asset_id"], row["relation_type"]),
        )
    if removed_asset_ids:
        placeholders = ",".join("?" for _ in removed_asset_ids)
        connection.execute(
            f"""
            UPDATE market_jobs
               SET status='CANCELLED_MAPPING_SUPERSEDED',completed_at=?,
                   last_error='mapping_superseded'
             WHERE event_id=?
               AND asset_id IN ({placeholders})
               AND status IN ('PENDING','RETRY','UNAVAILABLE')
            """,
            (now, event_id, *removed_asset_ids),
        )
    return len(superseded)


def map_event_assets(
    connection: Any,
    *,
    freshness_days: int = 14,
    today: dt.date | None = None,
    event_ids: Iterable[str] | None = None,
    policy: AssetMappingPolicy | None = None,
    config_path: str | None = None,
    issuer_directory: IssuerDirectory | None = None,
    apply: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    selected_policy = policy or load_asset_mapping_policy(config_path)
    events = _selected_events(
        connection,
        freshness_days=freshness_days,
        today=today,
        event_ids=event_ids,
        policy_sha256=selected_policy.policy_sha256,
        only_unmapped=apply and not force,
    )
    now = utc_now()
    result: dict[str, Any] = {
        "mode": "APPLY" if apply else "SHADOW",
        "forced_reconciliation": bool(force),
        "policy_version": selected_policy.policy_version,
        "policy_sha256": selected_policy.policy_sha256,
        "selected_events": len(events),
        "mapped_events": 0,
        "unmapped_events": 0,
        "mapped_assets": 0,
        "receipts_inserted": 0,
        "impacts_upserted": 0,
        "superseded_impacts": 0,
        "manual_conflicts": 0,
        "exact_source_timestamps": 0,
        "date_only_or_missing_timestamps": 0,
        "rule_hits": {},
        "issuer_directory_records": (
            issuer_directory.record_count if issuer_directory is not None else 0
        ),
        "issuer_resolved_events": 0,
        "sample": [],
    }
    for event in events:
        mappings = resolve_event_assets(
            event,
            policy=selected_policy,
            issuer_directory=issuer_directory,
        )
        decision_id, _observation_id, _source_hash = _decision_binding(
            event, selected_policy
        )
        rule_id = str(mappings[0]["rule_id"]) if mappings else None

        if not apply:
            if not mappings:
                result["unmapped_events"] += 1
                continue
            effective = [(None, mapping) for mapping in mappings]
        else:
            effective: list[tuple[str | None, dict[str, Any]]] = []
            for mapping in mappings:
                asset_id = _upsert_asset(connection, mapping, now=now)
                existing = connection.execute(
                    """
                    SELECT assessment_source FROM event_asset_impacts
                     WHERE event_id=? AND asset_id=? AND relation_type=?
                    """,
                    (event["event_id"], asset_id, mapping["relation_type"]),
                ).fetchone()
                if existing is not None and not str(
                    existing["assessment_source"]
                ).startswith(AUTO_SOURCE_PREFIX):
                    result["manual_conflicts"] += 1
                    continue
                effective.append((asset_id, mapping))

            decision_kind = "MAPPED" if effective else "NO_MATCH"
            _insert_decision(
                connection,
                decision_id=decision_id,
                event=event,
                policy=selected_policy,
                decision=decision_kind,
                rule_id=rule_id,
                asset_count=len(effective),
                now=now,
            )
            result["superseded_impacts"] += _deactivate_old_automatic_impacts(
                connection,
                event_id=str(event["event_id"]),
                keep_projection_keys={
                    (str(asset_id), str(mapping["relation_type"]))
                    for asset_id, mapping in effective
                    if asset_id is not None
                },
                now=now,
            )
            if not effective:
                result["unmapped_events"] += 1
                continue

        result["mapped_events"] += 1
        result["mapped_assets"] += len(effective)
        if any(
            any(
                str(code).startswith(
                    ("SOURCE_VALIDATED_CASHTAG", "SOURCE_LEADING_ISSUER")
                )
                for code in mapping.get("reason_codes", [])
            )
            for _asset_id, mapping in effective
        ):
            result["issuer_resolved_events"] += 1
        source_time = str(event.get("source_published_at") or "")
        try:
            parsed_time = dt.datetime.fromisoformat(source_time.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            parsed_time = None
        if parsed_time is not None and "T" in source_time:
            result["exact_source_timestamps"] += 1
        else:
            result["date_only_or_missing_timestamps"] += 1

        assert rule_id is not None
        result["rule_hits"][rule_id] = int(result["rule_hits"].get(rule_id, 0)) + 1
        if len(result["sample"]) < 25:
            result["sample"].append(
                {
                    "event_id": event["event_id"],
                    "event_version": event["current_version"],
                    "rule_id": rule_id,
                    "symbols": [mapping["symbol"] for _asset_id, mapping in effective],
                }
            )
        if not apply:
            continue

        for asset_id, mapping in effective:
            assert asset_id is not None
            receipt_id = stable_id(
                "AMAP",
                decision_id,
                asset_id,
                str(mapping["relation_type"]),
                "SELECTED",
            )
            before = connection.total_changes
            connection.execute(
                """
                INSERT OR IGNORE INTO event_asset_mapping_receipts(
                    receipt_id,event_id,event_version,mapping_decision_id,asset_id,
                    relation_type,display_role,proxy_label,rule_id,policy_version,
                    policy_sha256,mapping_rank,confidence,decision,reason_codes_json,
                    created_at,no_trading
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,1)
                """,
                (
                    receipt_id,
                    event["event_id"],
                    event["current_version"],
                    decision_id,
                    asset_id,
                    mapping["relation_type"],
                    mapping["role"],
                    mapping["proxy_label"],
                    mapping["rule_id"],
                    mapping["policy_version"],
                    mapping["policy_sha256"],
                    mapping["rank"],
                    mapping["confidence"],
                    "SELECTED",
                    stable_json(mapping["reason_codes"]),
                    now,
                ),
            )
            result["receipts_inserted"] += connection.total_changes - before
            before = connection.total_changes
            connection.execute(
                """
                INSERT INTO event_asset_impacts(
                    impact_id,event_id,asset_id,relation_type,direction,impact_score,
                    confidence,reason_codes_json,assessment_source,mapping_decision_id,
                    market_observation_allowed,no_trading,created_at,updated_at
                ) VALUES (?,?,?,?, 'ABSTAIN',0,?,?,?,?,1,1,?,?)
                ON CONFLICT(event_id,asset_id,relation_type) DO UPDATE SET
                    direction='ABSTAIN',impact_score=0,confidence=excluded.confidence,
                    reason_codes_json=excluded.reason_codes_json,
                    assessment_source=excluded.assessment_source,
                    mapping_decision_id=excluded.mapping_decision_id,
                    market_observation_allowed=1,no_trading=1,
                    updated_at=excluded.updated_at
                """,
                (
                    stable_id(
                        "IMPACT",
                        str(event["event_id"]),
                        asset_id,
                        str(mapping["relation_type"]),
                    ),
                    event["event_id"],
                    asset_id,
                    mapping["relation_type"],
                    mapping["confidence"],
                    stable_json(mapping["reason_codes"]),
                    f"{AUTO_SOURCE_PREFIX}{mapping['rule_id']}",
                    decision_id,
                    now,
                    now,
                ),
            )
            result["impacts_upserted"] += int(connection.total_changes > before)
    if apply:
        connection.commit()
    return result


def write_report(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Event asset mapping V1",
        "",
        f"- Mode: `{result['mode']}`",
        f"- Policy: `{result['policy_version']}`",
        f"- Policy SHA256: `{result['policy_sha256']}`",
        f"- Selected events: `{result['selected_events']}`",
        f"- Mapped / unmapped: `{result['mapped_events']}` / `{result['unmapped_events']}`",
        f"- Assets selected: `{result['mapped_assets']}`",
        f"- Issuer directory records: `{result['issuer_directory_records']}`",
        f"- Events resolved through issuer directory: `{result['issuer_resolved_events']}`",
        f"- Exact source timestamps: `{result['exact_source_timestamps']}`",
        f"- Date-only or missing timestamps: `{result['date_only_or_missing_timestamps']}`",
        "- Boundary: mappings select read-only observation instruments only; direction is ABSTAIN, impact is zero, and no trading path is created.",
        "",
        "## Rule hits",
        "",
    ]
    for rule_id, count in sorted(result["rule_hits"].items()):
        lines.append(f"- `{rule_id}`: `{count}`")
    lines.extend(["", "## Sample", "", "| Event | Version | Rule | Assets |", "|---|---:|---|---|"])
    for item in result["sample"]:
        lines.append(
            f"| `{item['event_id']}` | {item['event_version']} | `{item['rule_id']}` | "
            f"{', '.join(item['symbols'])} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=MAPPING_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--freshness-days", type=int, default=14)
    parser.add_argument("--event-id", action="append", default=[])
    parser.add_argument("--issuer-index", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    connection = open_ledger(args.db)
    try:
        issuer_index = args.issuer_index or (
            args.db.parent
            / "cache"
            / "sec_company_tickers"
            / "company_tickers_exchange.json"
        )
        result = map_event_assets(
            connection,
            freshness_days=max(0, args.freshness_days),
            event_ids=args.event_id,
            config_path=str(args.config),
            issuer_directory=load_issuer_directory(issuer_index),
            apply=not args.dry_run,
        )
        write_report(args.report, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
