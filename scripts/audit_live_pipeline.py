#!/usr/bin/env python3
"""Audit Finance Radar live-ledger safety, source health, and candidate integrity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from event_ledger import ledger_summary, open_ledger, utc_now


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_REPORT = ROOT / "reports" / "live_pipeline_audit_latest.md"
OFFICIAL_SOURCES = (
    "federal_reserve_press",
    "sec_current_filings",
    "bls_key_indicators",
)


def scalar(connection: Any, query: str, params: tuple[Any, ...] = ()) -> int:
    return int(connection.execute(query, params).fetchone()[0])


def audit(connection: Any) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in OFFICIAL_SOURCES)
    checks = {
        "canonical_no_trading_violations": scalar(
            connection, "SELECT COUNT(*) FROM canonical_events WHERE no_trading!=1"
        ),
        "impact_no_trading_violations": scalar(
            connection, "SELECT COUNT(*) FROM event_asset_impacts WHERE no_trading!=1"
        ),
        "non_abstain_asset_impacts": scalar(
            connection, "SELECT COUNT(*) FROM event_asset_impacts WHERE direction!='ABSTAIN'"
        ),
        "candidate_market_observation_violations": scalar(
            connection,
            """SELECT COUNT(*) FROM event_asset_impacts i
               JOIN canonical_events e ON e.event_id=i.event_id
               WHERE e.status!='verified' AND i.market_observation_allowed!=0
                 AND i.assessment_source NOT LIKE 'automatic_asset_mapping_v1:%'""",
        ),
        "automatic_asset_mapping_contract_violations": scalar(
            connection,
            """SELECT COUNT(*) FROM event_asset_impacts
               WHERE assessment_source LIKE 'automatic_asset_mapping_v1:%'
                 AND (direction!='ABSTAIN' OR impact_score!=0 OR no_trading!=1)""",
        ),
        "automatic_asset_mapping_cap_violations": scalar(
            connection,
            """SELECT COUNT(*) FROM (
                   SELECT event_id FROM event_asset_impacts
                    WHERE assessment_source LIKE 'automatic_asset_mapping_v1:%'
                      AND market_observation_allowed=1
                    GROUP BY event_id HAVING COUNT(*)>3
               )""",
        ),
        "mapping_receipt_boundary_violations": scalar(
            connection,
            """SELECT COUNT(*) FROM event_asset_mapping_receipts
               WHERE no_trading!=1 OR mapping_rank NOT BETWEEN 1 AND 3
                  OR LENGTH(policy_sha256)!=64
                  OR LOWER(policy_sha256) GLOB '*[^0-9a-f]*'""",
        ),
        "mapping_decision_boundary_violations": scalar(
            connection,
            """SELECT COUNT(*) FROM event_asset_mapping_decisions
               WHERE no_trading!=1 OR asset_count NOT BETWEEN 0 AND 3
                  OR LENGTH(policy_sha256)!=64
                  OR LOWER(policy_sha256) GLOB '*[^0-9a-f]*'
                  OR LENGTH(source_content_sha256)!=64
                  OR LOWER(source_content_sha256) GLOB '*[^0-9a-f]*'""",
        ),
        "automatic_asset_mapping_projection_violations": scalar(
            connection,
            """SELECT
                 (SELECT COUNT(*)
                    FROM event_asset_impacts impact
                    JOIN canonical_events event ON event.event_id=impact.event_id
                    LEFT JOIN event_asset_mapping_decisions decision
                      ON decision.decision_id=impact.mapping_decision_id
                     AND decision.event_id=impact.event_id
                     AND decision.event_version=event.current_version
                     AND decision.decision='MAPPED'
                    LEFT JOIN event_asset_mapping_receipts receipt
                      ON receipt.mapping_decision_id=impact.mapping_decision_id
                     AND receipt.event_id=impact.event_id
                     AND receipt.event_version=event.current_version
                     AND receipt.asset_id=impact.asset_id
                     AND receipt.relation_type=impact.relation_type
                     AND receipt.decision='SELECTED'
                   WHERE impact.assessment_source LIKE 'automatic_asset_mapping_v1:%'
                     AND impact.market_observation_allowed=1
                     AND (decision.decision_id IS NULL OR receipt.receipt_id IS NULL))
                 +
                 (SELECT COUNT(*)
                    FROM event_asset_mapping_receipts receipt
                    JOIN event_asset_mapping_decisions decision
                      ON decision.decision_id=receipt.mapping_decision_id
                    JOIN canonical_events event
                      ON event.event_id=decision.event_id
                     AND event.current_version=decision.event_version
                    LEFT JOIN event_asset_impacts impact
                      ON impact.mapping_decision_id=receipt.mapping_decision_id
                     AND impact.event_id=receipt.event_id
                     AND impact.asset_id=receipt.asset_id
                     AND impact.relation_type=receipt.relation_type
                     AND impact.assessment_source LIKE 'automatic_asset_mapping_v1:%'
                     AND impact.market_observation_allowed=1
                   WHERE decision.decision='MAPPED'
                     AND receipt.decision='SELECTED'
                     AND impact.impact_id IS NULL)""",
        ),
        "market_bar_boundary_violations": scalar(
            connection,
            "SELECT COUNT(*) FROM market_bars WHERE read_only!=1 OR no_trading!=1",
        ),
        "market_job_boundary_violations": scalar(
            connection,
            "SELECT COUNT(*) FROM market_jobs WHERE no_trading!=1",
        ),
        "market_job_anchor_violations": scalar(
            connection,
            """SELECT COUNT(*) FROM market_jobs job
               LEFT JOIN market_job_anchor_links link
                 ON link.market_job_id=job.market_job_id
               LEFT JOIN market_event_anchors anchor
                 ON anchor.anchor_id=link.anchor_id
               WHERE job.status NOT LIKE 'CANCELLED_%'
                 AND (link.market_job_id IS NULL OR anchor.anchor_id IS NULL
                      OR anchor.reaction_anchor_at IS NULL
                      OR link.offset_seconds IS NULL
                      OR anchor.event_id!=job.event_id
                      OR anchor.event_version!=job.event_version
                      OR anchor.asset_id!=job.asset_id
                      OR anchor.provider!=job.provider
                      OR ABS(
                           (JULIANDAY(job.scheduled_at)
                            - JULIANDAY(anchor.reaction_anchor_at)) * 86400.0
                           - link.offset_seconds
                         ) > 1.0)""",
        ),
        "market_metric_current_version_violations": scalar(
            connection,
            """SELECT COUNT(*) FROM event_market_metrics metric
               LEFT JOIN canonical_events event
                 ON event.event_id=metric.event_id
                AND event.current_version=metric.event_version
               LEFT JOIN assets asset ON asset.asset_id=metric.stable_id
               WHERE event.event_id IS NULL
                  OR (metric.metric_name LIKE 'reaction_return_%'
                      AND asset.asset_id IS NULL)
                  OR metric.metric_scope!='post_event_audit_only'
                  OR metric.allowed_for_discovery_rank!=0
                  OR metric.allowed_as_model_feature!=0""",
        ),
        "candidate_outbox_violations": scalar(
            connection,
            """SELECT COUNT(*) FROM alert_outbox o
               JOIN canonical_events e ON e.event_id=o.event_id
               WHERE e.status!='verified'""",
        ),
        "auto_verification_violations": scalar(
            connection, "SELECT COUNT(*) FROM event_evidence WHERE auto_verification_allowed!=0"
        ),
        "official_auto_promotion_violations": scalar(
            connection,
            f"""SELECT COUNT(*) FROM canonical_events e
                WHERE e.discovery_source IN ({placeholders}) AND e.status!='candidate'
                  AND NOT EXISTS (
                    SELECT 1 FROM event_versions v
                    WHERE v.event_id=e.event_id
                      AND v.change_reason='manual_primary_evidence_review'
                  )""",
            OFFICIAL_SOURCES,
        ),
        "official_multi_event_cluster_violations": scalar(
            connection,
            f"""SELECT COUNT(*) FROM (
                   SELECT e.event_id FROM canonical_events e
                   JOIN event_observations eo ON eo.event_id=e.event_id
                   JOIN raw_observations r ON r.observation_id=eo.observation_id
                   WHERE e.discovery_source IN ({placeholders})
                     AND eo.relation_type!='confirming_primary_evidence'
                   GROUP BY e.event_id HAVING COUNT(DISTINCT r.observation_id)>1
                )""",
            OFFICIAL_SOURCES,
        ),
        "event_chain_primary_count_violations": scalar(
            connection,
            """SELECT COUNT(*) FROM (
                   SELECT c.chain_id
                   FROM event_chains c
                   LEFT JOIN event_chain_members m ON m.chain_id=c.chain_id
                   GROUP BY c.chain_id
                   HAVING SUM(CASE WHEN m.chain_role='primary_event' THEN 1 ELSE 0 END)!=1
                      OR SUM(m.counts_as_primary_event)!=1
                )""",
        ),
        "event_chain_primary_pointer_violations": scalar(
            connection,
            """SELECT COUNT(*) FROM event_chains c
               LEFT JOIN event_chain_members m
                 ON m.chain_id=c.chain_id
                AND m.event_id=c.primary_event_id
                AND m.chain_role='primary_event'
                AND m.counts_as_primary_event=1
               WHERE c.primary_event_id IS NULL OR m.event_id IS NULL""",
        ),
        "event_chain_no_trading_violations": scalar(
            connection, "SELECT COUNT(*) FROM event_chains WHERE no_trading!=1"
        ),
        "source_cursor_errors": scalar(
            connection, "SELECT COUNT(*) FROM source_cursors WHERE status='ERROR'"
        ),
        "sec_enrichment_errors": scalar(
            connection, "SELECT COUNT(*) FROM sec_filing_enrichments WHERE status='ERROR'"
        ),
        "sec_enrichment_read_only_violations": scalar(
            connection,
            "SELECT COUNT(*) FROM sec_filing_enrichments WHERE read_only!=1 OR no_trading!=1",
        ),
        "review_triage_no_trading_violations": scalar(
            connection, "SELECT COUNT(*) FROM event_review_triage WHERE no_trading!=1"
        ),
        "review_triage_auto_s_violations": scalar(
            connection, "SELECT COUNT(*) FROM event_review_triage WHERE severity_ceiling='S'"
        ),
        "pending_review_without_triage": scalar(
            connection,
            """SELECT COUNT(*) FROM pipeline_jobs j
               LEFT JOIN event_review_triage t ON t.event_id=j.event_id
               WHERE j.job_type='live_primary_evidence_review'
                 AND j.status='PENDING_PRIMARY_EVIDENCE'
                 AND t.event_id IS NULL""",
        ),
        "runtime_leases": scalar(connection, "SELECT COUNT(*) FROM runtime_leases"),
        "alert_delivery_leases": scalar(
            connection, "SELECT COUNT(*) FROM alert_delivery_leases"
        ),
    }
    cursors = [
        dict(row)
        for row in connection.execute(
            """SELECT source_id,cursor_type,cursor_value,last_polled_at,last_success_at,status,last_error
               FROM source_cursors ORDER BY source_id"""
        )
    ]
    official_types = {
        row[0]: row[1]
        for row in connection.execute(
            f"""SELECT event_type,COUNT(*) FROM canonical_events
                WHERE discovery_source IN ({placeholders})
                GROUP BY event_type ORDER BY event_type""",
            OFFICIAL_SOURCES,
        )
    }
    event_chains = []
    for chain in connection.execute(
        """SELECT chain_id,chain_type,canonical_key,primary_event_id
           FROM event_chains ORDER BY chain_id"""
    ):
        item = dict(chain)
        item["members"] = [
            dict(member)
            for member in connection.execute(
                """SELECT event_id,chain_role,counts_as_primary_event,rationale
                   FROM event_chain_members
                   WHERE chain_id=?
                   ORDER BY counts_as_primary_event DESC,chain_role,event_id""",
                (chain["chain_id"],),
            )
        ]
        event_chains.append(item)
    summary = ledger_summary(connection)
    return {
        "audited_at": utc_now(),
        "passed": all(value == 0 for value in checks.values()),
        "checks": checks,
        "source_cursors": cursors,
        "official_event_types": official_types,
        "event_chains": event_chains,
        "ledger": summary,
    }


def write_report(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Live Pipeline Audit",
        "",
        f"- Audited at: `{result['audited_at']}`",
        f"- Result: `{'PASS' if result['passed'] else 'FAIL'}`",
        f"- Schema version: `{result['ledger']['schema_version']}`",
        f"- Canonical events: `{result['ledger']['table_counts']['canonical_events']}`",
        f"- Raw observations: `{result['ledger']['table_counts']['raw_observations']}`",
        "",
        "## Safety checks",
        "",
    ]
    lines.extend(f"- {name}: `{value}`" for name, value in result["checks"].items())
    lines.extend(["", "## Official source cursors", ""])
    for cursor in result["source_cursors"]:
        lines.append(
            f"- {cursor['source_id']}: `{cursor['status']}`; "
            f"last success `{cursor['last_success_at']}`"
        )
    lines.extend(["", "## Official candidate types", ""])
    lines.extend(
        f"- {event_type}: `{count}`"
        for event_type, count in result["official_event_types"].items()
    )
    lines.extend(["", "## Event chains", ""])
    if not result["event_chains"]:
        lines.append("- none")
    for chain in result["event_chains"]:
        lines.append(
            f"- `{chain['chain_id']}` / `{chain['chain_type']}` / "
            f"primary `{chain['primary_event_id']}`"
        )
        for member in chain["members"]:
            lines.append(
                f"  - `{member['event_id']}`: `{member['chain_role']}`; "
                f"primary_count `{member['counts_as_primary_event']}`"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    connection = open_ledger(args.db)
    try:
        result = audit(connection)
    finally:
        connection.close()
    write_report(args.report, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"REPORT={args.report}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
