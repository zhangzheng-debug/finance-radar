from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.models.event_asset_mapping import (
    MAPPING_PATH,
    load_asset_mapping_policy,
    resolve_event_assets,
)


def _symbols(items: list[dict[str, object]]) -> list[str]:
    return [str(item["symbol"]) for item in items]


def _default_document() -> dict[str, object]:
    return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))


def _write_policy(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_default_policy_is_strict_versioned_and_content_addressed() -> None:
    load_asset_mapping_policy.cache_clear()
    policy = load_asset_mapping_policy()

    assert policy.policy_version == "event-asset-mapping-v1.0.0"
    assert policy.policy_sha256 == hashlib.sha256(MAPPING_PATH.read_bytes()).hexdigest()
    assert policy.max_assets_per_event == 3
    assert policy.direction == "ABSTAIN"
    assert policy.impact_score == 0
    assert policy.no_trading == 1
    assert [rule.priority for rule in policy.rules] == [10, 20, 30, 40, 50]


def test_company_maps_to_direct_ticker_and_spy_only() -> None:
    items = resolve_event_assets(
        {
            "event_family": "governance",
            "event_type": "chief_financial_officer_appointment",
            "company_name": "NVIDIA Corporation",
            "ticker_at_event": "NVDA",
            "exchange": "Nasdaq",
            "title": "NVIDIA appointed a new chief financial officer",
        }
    )

    assert _symbols(items) == ["NVDA", "SPY"]
    assert items[0]["relation_type"] == "PRIMARY"
    assert items[0]["role"] == "DIRECT_SECURITY"
    assert items[0]["venue"] == "Nasdaq"
    assert items[1]["role"] == "MARKET_BENCHMARK"
    assert {item["rule_id"] for item in items} == {"company-direct-market-v1"}


def test_company_mapping_deduplicates_spy_when_spy_is_the_direct_security() -> None:
    items = resolve_event_assets(
        {
            "event_family": "fund",
            "event_type": "fund_report",
            "company_name": "SPDR S&P 500 ETF Trust",
            "ticker_at_event": "SPY",
        }
    )

    assert _symbols(items) == ["SPY"]
    assert items[0]["role"] == "DIRECT_SECURITY"
    assert items[0]["rank"] == 1


def test_high_confidence_geopolitical_energy_maps_to_oil_and_gold_proxies() -> None:
    items = resolve_event_assets(
        {
            "event_family": "geopolitical",
            "event_type": "active_iranian_attacks_and_threats_to_commercial_shipping",
            "title": "Iranian missile attacks threaten commercial shipping near Hormuz",
        }
    )

    assert _symbols(items) == ["USO", "BNO", "GLD"]
    assert {item["role"] for item in items} == {"THEMATIC_PROXY"}
    assert {item["rule_id"] for item in items} == {
        "geopolitical-energy-transmission-v1"
    }
    assert [item["proxy_label"] for item in items] == [
        "WTI原油ETF代理",
        "Brent原油ETF代理",
        "黄金ETF代理",
    ]


def test_general_armed_conflict_without_energy_terms_maps_to_gold_and_spy() -> None:
    items = resolve_event_assets(
        {
            "event_family": "geopolitical",
            "event_type": "armed_conflict",
            "title": "Military strikes intensified near the border overnight",
        }
    )

    assert _symbols(items) == ["GLD", "SPY"]
    assert "USO" not in _symbols(items)
    assert "BNO" not in _symbols(items)
    assert {item["rule_id"] for item in items} == {
        "armed-conflict-broad-market-v1"
    }


def test_chinese_geopolitical_energy_text_uses_the_same_high_precision_rule() -> None:
    items = resolve_event_assets(
        {
            "event_family": "geopolitical",
            "event_type": "shipping_disruption",
            "title": "伊朗导弹袭击威胁霍尔木兹海峡商业航运与原油供应",
        }
    )

    assert _symbols(items) == ["USO", "BNO", "GLD"]


@pytest.mark.parametrize(
    ("event", "expected_rule"),
    [
        (
            {
                "event_family": "macro_policy",
                "event_type": "monetary_policy",
                "title": "FOMC announces its federal funds rate decision",
            },
            "monetary-policy-cross-asset-v1",
        ),
        (
            {
                "event_family": "macro_data",
                "event_type": "inflation_release",
                "title": "Consumer Price Index inflation report released",
            },
            "inflation-release-cross-asset-v1",
        ),
    ],
)
def test_monetary_policy_and_inflation_use_tlt_spy_gld(
    event: dict[str, str], expected_rule: str
) -> None:
    items = resolve_event_assets(event)

    assert _symbols(items) == ["TLT", "SPY", "GLD"]
    assert {item["rule_id"] for item in items} == {expected_rule}


def test_unrelated_event_does_not_invent_an_asset_mapping() -> None:
    assert (
        resolve_event_assets(
            {
                "event_family": "administrative",
                "event_type": "routine_calendar_notice",
                "title": "The annual calendar has been published",
            }
        )
        == []
    )


def test_company_earnings_text_that_mentions_inflation_is_not_a_macro_release() -> None:
    assert (
        resolve_event_assets(
            {
                "event_family": "earnings",
                "event_type": "quarterly_results",
                "title": "Management discussed inflation pressure during the earnings call",
            }
        )
        == []
    )


@pytest.mark.parametrize(
    "event",
    [
        {
            "event_family": "governance",
            "event_type": "management_change",
            "company_name": "Example Inc.",
            "ticker_at_event": "EXM",
        },
        {
            "event_family": "geopolitical",
            "event_type": "conflict_or_blockade",
            "title": "A blockade disrupted a crude oil shipping route",
        },
        {
            "event_family": "macro_data",
            "event_type": "inflation_release",
            "title": "CPI inflation data released",
        },
    ],
)
def test_all_mappings_obey_read_only_directionless_contract(event: dict[str, str]) -> None:
    items = resolve_event_assets(event)

    assert 1 <= len(items) <= 3
    assert len({(item["provider_symbol"], item["venue"]) for item in items}) == len(items)
    assert [item["rank"] for item in items] == list(range(1, len(items) + 1))
    for item in items:
        assert item["direction"] == "ABSTAIN"
        assert item["impact_score"] == 0
        assert item["no_trading"] == 1
        assert item["policy_version"] == "event-asset-mapping-v1.0.0"
        assert len(str(item["policy_sha256"])) == 64
        assert item["rule_id"]
        assert item["role"]
        assert item["proxy_label"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update({"direction": "LONG"}), "direction must be ABSTAIN"),
        (lambda payload: payload.update({"impact_score": 1}), "impact_score must be zero"),
        (lambda payload: payload.update({"no_trading": 0}), "no_trading must equal one"),
        (lambda payload: payload.update({"unexpected": True}), "unknown fields"),
        (
            lambda payload: payload["rules"][1]["assets"].append("SPY"),
            "exceeds max_assets_per_event",
        ),
    ],
)
def test_invalid_or_expansive_policy_fails_closed(
    tmp_path: Path, mutation, message: str
) -> None:
    payload = copy.deepcopy(_default_document())
    mutation(payload)
    path = _write_policy(tmp_path, payload)

    with pytest.raises(ValueError, match=message):
        load_asset_mapping_policy(str(path))
