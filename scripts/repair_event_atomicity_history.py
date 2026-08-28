#!/usr/bin/env python3
"""Plan or apply a version-bound repair of historical OpenNews event atomicity.

The default mode is read-only and writes a content-addressed plan.  Apply mode
requires that exact plan hash, revalidates every event/source binding, preserves
raw captures and immutable market history, then retires only invalid current
projections and unfinished work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from app.models.event_asset_mapping import load_asset_mapping_policy  # noqa: E402
from app.models.issuer_directory import load_issuer_directory  # noqa: E402
from event_ledger import open_ledger, stable_json, utc_now  # noqa: E402
from live_candidate_extractor import (  # noqa: E402
    _filter_candidate_event,
    assess_opennews_source_shape,
    opennews_admission,
    repair_opennews_asset_tags,
)
from map_event_assets import map_event_assets  # noqa: E402
from observe_live_event_markets import cancel_superseded_version_jobs  # noqa: E402


CONTRACT_VERSION = "event-atomicity-history-repair-v1"
AUTO_SOURCE_PREFIX = "automatic_asset_mapping_v1:"
UNFINISHED_MARKET_STATUSES = ("PENDING", "RETRY", "UNAVAILABLE")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _source_rows(connection: Any, event_id: str) -> list[Any]:
    return connection.execute(
        """SELECT r.observation_id,r.source_id,r.external_id,r.title,r.summary,
                  r.content_sha256,r.raw_json,r.source_published_at,
                  r.local_received_at,r.observation_status,
                  s.authority_tier,eo.relation_type
           FROM event_observations eo
           JOIN latest_source_content r ON r.observation_id=eo.observation_id
           JOIN sources s ON s.source_id=r.source_id
           WHERE eo.event_id=?
           ORDER BY r.observation_id""",
        (event_id,),
    ).fetchall()


def _event_record(connection: Any, event: Any) -> dict[str, Any] | None:
    event_id = str(event["event_id"])
    observations = _source_rows(connection, event_id)
    source_bindings: list[dict[str, Any]] = []
    admitted_ids: list[str] = []
    filtered_ids: list[str] = []
    source_shapes: dict[str, str] = {}
    has_primary_source = False
    for row in observations:
        observation_id = str(row["observation_id"])
        source_id = str(row["source_id"])
        authority_tier = str(row["authority_tier"])
        if authority_tier.startswith(("P0", "P1")):
            has_primary_source = True
        if source_id == "opennews_free" and str(row["observation_status"]) != "deleted":
            assessment = assess_opennews_source_shape(row)
            source_shapes[observation_id] = assessment.shape
            admitted = bool(
                assessment.shape == "SINGLE_EVENT"
                and assessment.matched_rule is not None
                and opennews_admission(row, assessment.matched_rule).admitted
            )
            (admitted_ids if admitted else filtered_ids).append(observation_id)
        source_bindings.append(
            {
                "observation_id": observation_id,
                "source_id": source_id,
                "authority_tier": authority_tier,
                "content_sha256": str(row["content_sha256"] or ""),
                "observation_status": str(row["observation_status"]),
                "relation_type": str(row["relation_type"]),
            }
        )

    active_impacts = [
        {
            "asset_id": str(row["asset_id"]),
            "relation_type": str(row["relation_type"]),
            "mapping_decision_id": str(row["mapping_decision_id"] or ""),
        }
        for row in connection.execute(
            """SELECT asset_id,relation_type,mapping_decision_id
               FROM event_asset_impacts
               WHERE event_id=? AND assessment_source LIKE ?
                 AND market_observation_allowed=1
               ORDER BY asset_id,relation_type""",
            (event_id, f"{AUTO_SOURCE_PREFIX}%"),
        ).fetchall()
    ]
    unfinished_jobs = [
        {
            "market_job_id": str(row["market_job_id"]),
            "event_version": int(row["event_version"]),
            "asset_id": str(row["asset_id"]),
            "status": str(row["status"]),
        }
        for row in connection.execute(
            """SELECT market_job_id,event_version,asset_id,status
               FROM market_jobs
               WHERE event_id=? AND status IN ('PENDING','RETRY','UNAVAILABLE')
               ORDER BY market_job_id""",
            (event_id,),
        ).fetchall()
    ]

    status = str(event["status"])
    if status == "rejected":
        if not active_impacts and not unfinished_jobs:
            return None
        action = "DEACTIVATE_REJECTED_PROJECTION"
    elif status in {"verified", "weak"} and filtered_ids and not (
        has_primary_source or admitted_ids
    ):
        action = "MANUAL_HOLD"
    elif status == "candidate" and not (has_primary_source or admitted_ids):
        action = "FILTER_EVENT"
    elif filtered_ids:
        action = "FILTER_CAPTURES_AND_REMAP"
    else:
        action = "REMAP_CURRENT"

    binding = {
        "event_id": event_id,
        "current_version": int(event["current_version"]),
        "status": status,
        "label_status": str(event["label_status"]),
        "source_bindings": source_bindings,
        "active_automatic_impacts": active_impacts,
        "unfinished_market_jobs": unfinished_jobs,
    }
    return {
        "event_id": event_id,
        "event_version": int(event["current_version"]),
        "status": status,
        "action": action,
        "admitted_observation_ids": admitted_ids,
        "filtered_observation_ids": filtered_ids,
        "source_shapes": source_shapes,
        "active_automatic_impact_count": len(active_impacts),
        "unfinished_market_job_count": len(unfinished_jobs),
        "binding_sha256": _sha256_json(binding),
        "raw_observations_preserved": True,
        "no_trading": True,
    }


def _records(connection: Any) -> list[dict[str, Any]]:
    events = connection.execute(
        """SELECT event_id,current_version,status,label_status
           FROM canonical_events
           WHERE discovery_source='opennews_free'
             AND status IN ('candidate','weak','verified','rejected')
           ORDER BY event_id"""
    ).fetchall()
    records = [record for event in events if (record := _event_record(connection, event))]
    return sorted(records, key=lambda item: str(item["event_id"]))


def build_plan(connection: Any) -> dict[str, Any]:
    policy = load_asset_mapping_policy()
    records = _records(connection)
    counts = Counter(str(record["action"]) for record in records)
    plan: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "created_at": utc_now(),
        "mapping_policy_version": policy.policy_version,
        "mapping_policy_sha256": policy.policy_sha256,
        "record_count": len(records),
        "action_counts": dict(sorted(counts.items())),
        "records": records,
        "raw_observations_preserved": True,
        "completed_market_history_preserved": True,
        "no_trading": True,
    }
    plan["plan_sha256"] = _sha256_json(plan)
    return plan


def validate_plan(plan: dict[str, Any]) -> str:
    if str(plan.get("contract_version")) != CONTRACT_VERSION:
        raise ValueError("unsupported event atomicity repair contract")
    expected = str(plan.get("plan_sha256") or "")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    actual = _sha256_json(unsigned)
    if len(expected) != 64 or expected != actual:
        raise ValueError("plan_sha256 mismatch")
    if not isinstance(plan.get("records"), list):
        raise ValueError("repair records must be a list")
    return actual


def _current_records_by_id(connection: Any) -> dict[str, dict[str, Any]]:
    return {str(record["event_id"]): record for record in _records(connection)}


def _deactivate_rejected_projection(connection: Any, event_id: str, now: str) -> tuple[int, int]:
    before = connection.total_changes
    connection.execute(
        """UPDATE event_asset_impacts
           SET market_observation_allowed=0,no_trading=1,updated_at=?
           WHERE event_id=? AND assessment_source LIKE ?
             AND market_observation_allowed=1""",
        (now, event_id, f"{AUTO_SOURCE_PREFIX}%"),
    )
    impacts = connection.total_changes - before
    before = connection.total_changes
    connection.execute(
        """UPDATE market_jobs
           SET status='CANCELLED_EVENT_REJECTED',completed_at=?,
               last_error='historical_event_atomicity_repair',no_trading=1
           WHERE event_id=? AND status IN ('PENDING','RETRY','UNAVAILABLE')""",
        (now, event_id),
    )
    return impacts, connection.total_changes - before


def apply_plan(
    connection: Any,
    plan: dict[str, Any],
    *,
    expected_plan_sha256: str,
    issuer_index_path: Path | None = None,
) -> dict[str, Any]:
    plan_sha256 = validate_plan(plan)
    if expected_plan_sha256 != plan_sha256:
        raise ValueError("--expect-plan-sha256 does not match the plan")
    policy = load_asset_mapping_policy()
    if (
        str(plan.get("mapping_policy_sha256")) != policy.policy_sha256
        or str(plan.get("mapping_policy_version")) != policy.policy_version
    ):
        raise ValueError("asset mapping policy changed after the plan was built")

    current = _current_records_by_id(connection)
    stale: list[str] = []
    for record in plan["records"]:
        event_id = str(record.get("event_id") or "")
        live = current.get(event_id)
        if (
            live is None
            or str(live.get("binding_sha256")) != str(record.get("binding_sha256"))
            or str(live.get("action")) != str(record.get("action"))
        ):
            stale.append(event_id)
    if stale:
        raise ValueError(f"stale repair plan bindings: {','.join(stale[:20])}")

    now = utc_now()
    result: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "plan_sha256": plan_sha256,
        "started_at": now,
        "filtered_events": 0,
        "filtered_capture_edges": 0,
        "automatic_impacts_deactivated": 0,
        "unfinished_market_jobs_cancelled": 0,
        "manual_holds": 0,
        "asset_tag_repairs": 0,
        "superseded_version_jobs_cancelled": 0,
        "raw_observations_preserved": True,
        "completed_market_history_preserved": True,
        "no_trading": True,
    }
    remap_ids: list[str] = []
    for record in plan["records"]:
        event_id = str(record["event_id"])
        action = str(record["action"])
        if action == "MANUAL_HOLD":
            result["manual_holds"] += 1
            continue
        if action == "DEACTIVATE_REJECTED_PROJECTION":
            impacts, jobs = _deactivate_rejected_projection(connection, event_id, now)
            result["automatic_impacts_deactivated"] += impacts
            result["unfinished_market_jobs_cancelled"] += jobs
            continue

        filtered_ids = [str(value) for value in record["filtered_observation_ids"]]
        admitted_ids = [str(value) for value in record["admitted_observation_ids"]]
        if filtered_ids:
            placeholders = ",".join("?" for _ in filtered_ids)
            before = connection.total_changes
            connection.execute(
                f"""UPDATE event_observations
                    SET relation_type='filtered_aggregated_noise',linked_at=?
                    WHERE event_id=? AND observation_id IN ({placeholders})
                      AND relation_type!='filtered_aggregated_noise'""",
                (now, event_id, *filtered_ids),
            )
            result["filtered_capture_edges"] += connection.total_changes - before
        if admitted_ids:
            placeholders = ",".join("?" for _ in admitted_ids)
            connection.execute(
                f"""UPDATE event_observations
                    SET relation_type='aggregated_discovery_candidate',linked_at=?
                    WHERE event_id=? AND observation_id IN ({placeholders})
                      AND relation_type='filtered_aggregated_noise'""",
                (now, event_id, *admitted_ids),
            )

        if action == "FILTER_EVENT":
            connection.execute(
                """UPDATE pipeline_jobs
                   SET status='COMPLETED_DISCOVERY_FILTERED',
                       last_error='historical_multi_topic_or_non_atomic_source',updated_at=?
                   WHERE event_id=? AND job_type='live_primary_evidence_review'
                     AND status!='COMPLETED_DUPLICATE_CLUSTER'""",
                (now, event_id),
            )
            before_impacts = int(record["active_automatic_impact_count"])
            before_jobs = int(record["unfinished_market_job_count"])
            if _filter_candidate_event(
                connection,
                event_id,
                reason="historical_multi_topic_or_non_atomic_source",
                now=now,
            ):
                result["filtered_events"] += 1
                result["automatic_impacts_deactivated"] += before_impacts
                result["unfinished_market_jobs_cancelled"] += before_jobs
        else:
            remap_ids.append(event_id)
    connection.commit()

    candidate_remap_ids = [
        event_id
        for event_id in remap_ids
        if connection.execute(
            "SELECT status FROM canonical_events WHERE event_id=?", (event_id,)
        ).fetchone()[0]
        == "candidate"
    ]
    result["asset_tag_repairs"] = repair_opennews_asset_tags(
        connection, event_ids=candidate_remap_ids
    )
    result["superseded_version_jobs_cancelled"] = cancel_superseded_version_jobs(
        connection, completed_at=utc_now()
    )
    issuer_directory = load_issuer_directory(issuer_index_path)
    result["asset_mapping"] = map_event_assets(
        connection,
        event_ids=remap_ids,
        policy=policy,
        issuer_directory=issuer_directory,
        apply=True,
        force=True,
    )
    result["finished_at"] = utc_now()
    result["result_sha256"] = _sha256_json(result)
    return result


def _write_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    with path.open(mode, encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-plan-sha256")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--issuer-index", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.apply:
        with open_ledger(args.db) as connection:
            plan = build_plan(connection)
        _write_json(args.plan, plan)
        print(stable_json({key: value for key, value in plan.items() if key != "records"}))
        return 0
    if not args.expect_plan_sha256:
        parser.error("--apply requires --expect-plan-sha256")
    if args.receipt is None:
        parser.error("--apply requires --receipt")
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise ValueError("repair plan must be a JSON object")
    with open_ledger(args.db) as connection:
        result = apply_plan(
            connection,
            plan,
            expected_plan_sha256=args.expect_plan_sha256,
            issuer_index_path=args.issuer_index,
        )
    _write_json(args.receipt, result, exclusive=True)
    print(stable_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
