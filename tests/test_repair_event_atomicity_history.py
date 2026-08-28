from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, stable_id, stable_json, upsert_source, utc_now
import repair_event_atomicity_history as repair


def _observation(
    connection,
    *,
    external_id: str,
    title: str,
    coins: list[str] | None = None,
) -> str:
    now = utc_now()
    observation_id = stable_id("OBS", "opennews_free", external_id)
    raw_json = stable_json({"item": {"title": title, "coins": coins or []}})
    connection.execute(
        """INSERT INTO raw_observations(
           observation_id,source_id,external_id,source_published_at,local_received_at,
           title,summary,canonical_url,content_sha256,raw_json,observation_status)
           VALUES(?,?,?,'2026-08-28T00:00:00+00:00',?,?,?,'https://example.test/item',?,?,
                  'captured')""",
        (
            observation_id,
            "opennews_free",
            external_id,
            now,
            title,
            title,
            stable_id("SHA", title),
            raw_json,
        ),
    )
    return observation_id


def _event(
    connection,
    *,
    event_id: str,
    observation_id: str,
    family: str,
    event_type: str,
    facts: dict | None = None,
) -> None:
    now = utc_now()
    connection.execute(
        """INSERT INTO canonical_events(
           event_id,current_version,status,label_status,event_family,event_type,
           event_date,first_seen_at,last_updated_at,company_name,ticker_at_event,
           stable_id,manual_grade,provisional_grade_cap,discovery_source,no_trading)
           VALUES(?,1,'candidate','candidate',?,?,'2026-08-28',?,?,NULL,NULL,NULL,NULL,
                  'B_P2_discovery_only','opennews_free',1)""",
        (event_id, family, event_type, now, now),
    )
    connection.execute(
        """INSERT INTO event_versions(
           event_id,version,changed_at,status,label_status,event_family,event_type,
           manual_grade,facts_json,change_reason)
           VALUES(?,1,?,'candidate','candidate',?,?,NULL,?,'legacy_import')""",
        (event_id, now, family, event_type, stable_json(facts or {})),
    )
    connection.execute(
        "INSERT INTO event_observations VALUES(?,?,?,?)",
        (event_id, observation_id, "aggregated_discovery_candidate", now),
    )
    connection.execute(
        """INSERT INTO pipeline_jobs(
           job_id,event_id,job_type,status,priority,attempts,available_at,last_error,
           payload_json,created_at,updated_at)
           VALUES(?,?,'live_primary_evidence_review','PENDING_PRIMARY_EVIDENCE',50,0,
                  ?,NULL,'{}',?,?)""",
        (stable_id("JOB", event_id), event_id, now, now, now),
    )


def _stale_market_projection(connection, event_id: str) -> None:
    now = utc_now()
    asset_id = stable_id("ASSET", event_id)
    connection.execute(
        """INSERT INTO assets(
           asset_id,asset_type,symbol,provider_symbol,venue,currency,metadata_json,
           created_at,updated_at)
           VALUES(?,'equity','NVDA','NVDA','NASDAQ','USD','{}',?,?)""",
        (asset_id, now, now),
    )
    connection.execute(
        """INSERT INTO event_asset_impacts(
           impact_id,event_id,asset_id,relation_type,direction,impact_score,confidence,
           reason_codes_json,assessment_source,mapping_decision_id,
           market_observation_allowed,no_trading,created_at,updated_at)
           VALUES(?,?,?,'PRIMARY','ABSTAIN',0,1.0,'[]',
                  'automatic_asset_mapping_v1:legacy',NULL,1,1,?,?)""",
        (stable_id("IMPACT", event_id), event_id, asset_id, now, now),
    )
    connection.execute(
        """INSERT INTO market_jobs(
           market_job_id,event_id,event_version,asset_id,provider,observation_window,
           status,scheduled_at,completed_at,attempts,last_error,no_trading)
           VALUES(?,?,1,?,'twelve_data','T+30m','PENDING',?,NULL,0,NULL,1)""",
        (stable_id("MJOB", event_id), event_id, asset_id, now),
    )


@pytest.fixture()
def ledger(tmp_path: Path):
    connection = open_ledger(tmp_path / "ledger.sqlite3")
    upsert_source(
        connection,
        source_id="opennews_free",
        name="OpenNews",
        source_type="aggregated_discovery",
        authority_tier="P2_experimental",
    )
    try:
        yield connection
    finally:
        connection.close()


def test_plan_and_apply_filter_multi_topic_event_and_retire_stale_market_state(ledger) -> None:
    title = (
        "Nvidia shares jump after another earnings beat, bitcoin runs into a major "
        "wall of supply around $80,000, and AI security reports put Lightning on alert"
    )
    observation_id = _observation(
        ledger,
        external_id="mixed-digest",
        title=title,
        coins=["BTC", "NVDA"],
    )
    _event(
        ledger,
        event_id="mixed-event",
        observation_id=observation_id,
        family="earnings",
        event_type="earnings_or_guidance",
    )
    _stale_market_projection(ledger, "mixed-event")
    ledger.commit()

    plan = repair.build_plan(ledger)
    record = next(item for item in plan["records"] if item["event_id"] == "mixed-event")
    assert record["action"] == "FILTER_EVENT"
    assert record["source_shapes"][observation_id] == "MULTI_TOPIC_DIGEST"

    result = repair.apply_plan(
        ledger,
        plan,
        expected_plan_sha256=plan["plan_sha256"],
    )

    event = ledger.execute(
        "SELECT status,current_version FROM canonical_events WHERE event_id='mixed-event'"
    ).fetchone()
    impact = ledger.execute(
        "SELECT market_observation_allowed FROM event_asset_impacts WHERE event_id='mixed-event'"
    ).fetchone()
    job = ledger.execute(
        "SELECT status FROM market_jobs WHERE event_id='mixed-event'"
    ).fetchone()
    relation = ledger.execute(
        "SELECT relation_type FROM event_observations WHERE event_id='mixed-event'"
    ).fetchone()
    assert event["status"] == "rejected"
    assert event["current_version"] == 2
    assert impact["market_observation_allowed"] == 0
    assert job["status"] == "CANCELLED_EVENT_REJECTED"
    assert relation["relation_type"] == "filtered_aggregated_noise"
    assert ledger.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0] == 1
    assert result["filtered_events"] == 1
    assert result["automatic_impacts_deactivated"] == 1


def test_apply_rejects_changed_event_binding_before_writing(ledger) -> None:
    observation_id = _observation(
        ledger,
        external_id="mixed-stale",
        title="Nvidia reports earnings, while Bitcoin faces an $80,000 supply wall",
        coins=["BTC", "NVDA"],
    )
    _event(
        ledger,
        event_id="stale-event",
        observation_id=observation_id,
        family="earnings",
        event_type="earnings_or_guidance",
    )
    ledger.commit()
    plan = repair.build_plan(ledger)
    ledger.execute(
        """UPDATE event_observations SET relation_type='manual_context'
           WHERE event_id='stale-event'"""
    )
    ledger.commit()

    with pytest.raises(ValueError, match="stale repair plan bindings"):
        repair.apply_plan(
            ledger,
            plan,
            expected_plan_sha256=plan["plan_sha256"],
        )
    assert ledger.execute(
        "SELECT status FROM canonical_events WHERE event_id='stale-event'"
    ).fetchone()[0] == "candidate"


def test_retained_bitcoin_event_is_remapped_to_btc_and_ibit(ledger) -> None:
    title = "Federal Reserve cuts rates as Bitcoin liquidity conditions improve"
    observation_id = _observation(
        ledger,
        external_id="btc-atomic",
        title=title,
        coins=["BTC"],
    )
    _event(
        ledger,
        event_id="btc-event",
        observation_id=observation_id,
        family="macro_policy",
        event_type="monetary_policy",
        facts={
            "source_shape": "SINGLE_EVENT",
            "source_shape_contract": "opennews-source-shape-v1",
            "event_claim_text": title,
            "affected_assets": ["BTC"],
        },
    )
    ledger.commit()

    plan = repair.build_plan(ledger)
    record = next(item for item in plan["records"] if item["event_id"] == "btc-event")
    assert record["action"] == "REMAP_CURRENT"
    result = repair.apply_plan(
        ledger,
        plan,
        expected_plan_sha256=plan["plan_sha256"],
    )

    symbols = [
        row[0]
        for row in ledger.execute(
            """SELECT a.symbol FROM event_asset_impacts impact
               JOIN assets a ON a.asset_id=impact.asset_id
               WHERE impact.event_id='btc-event'
                 AND impact.market_observation_allowed=1
               ORDER BY a.symbol"""
        ).fetchall()
    ]
    assert symbols == ["BTC", "IBIT"]
    assert result["asset_mapping"]["mapped_events"] == 1
    assert result["asset_mapping"]["forced_reconciliation"] is True


def test_plan_hash_must_match_exactly(ledger) -> None:
    plan = repair.build_plan(ledger)
    with pytest.raises(ValueError, match="expect-plan-sha256"):
        repair.apply_plan(ledger, plan, expected_plan_sha256="0" * 64)
