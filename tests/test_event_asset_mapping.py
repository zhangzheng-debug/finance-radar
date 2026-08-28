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
from app.models.issuer_directory import IssuerDirectory


def _symbols(items: list[dict[str, object]]) -> list[str]:
    return [str(item["symbol"]) for item in items]


def _default_document() -> dict[str, object]:
    return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))


def _write_policy(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _issuer_directory() -> IssuerDirectory:
    return IssuerDirectory.from_document(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [1045810, "NVIDIA CORP", "NVDA", "Nasdaq"],
                [1652044, "Alphabet Inc.", "GOOG", "Nasdaq"],
                [1652044, "Alphabet Inc.", "GOOGL", "Nasdaq"],
            ],
        },
        source_sha256="a" * 64,
    )


def test_default_policy_is_strict_versioned_and_content_addressed() -> None:
    load_asset_mapping_policy.cache_clear()
    policy = load_asset_mapping_policy()

    assert policy.policy_version == "event-asset-mapping-v1.5.0"
    assert policy.policy_sha256 == hashlib.sha256(MAPPING_PATH.read_bytes()).hexdigest()
    assert policy.max_assets_per_event == 3
    assert policy.direction == "ABSTAIN"
    assert policy.impact_score == 0
    assert policy.no_trading == 1
    assert len(policy.asset_registry) == 160
    priorities = [rule.priority for rule in policy.rules]
    assert priorities == sorted(priorities)
    assert len(priorities) == len(set(priorities)) == 41


def test_claim_bound_bitcoin_event_maps_spot_then_us_listed_proxy() -> None:
    items = resolve_event_assets(
        {
            "event_family": "security_incident",
            "event_type": "bitcoin_network_security_alert",
            "ticker_at_event": "BTC",
            "discovery_source": "opennews_free",
            "facts": {
                "source_shape": "SINGLE_EVENT",
                "event_claim_text": "Bitcoin Lightning Network security alert affects BTC",
                "affected_assets": ["BTC"],
            },
        },
        issuer_directory=_issuer_directory(),
    )

    assert _symbols(items) == ["BTC", "IBIT"]
    assert items[0]["asset_type"] == "crypto"
    assert items[0]["provider_symbol"] == "BTCUSDT"
    assert items[0]["currency"] == "USDT"
    assert items[0]["role"] == "DIRECT_ASSET"
    assert items[0]["proxy_label"] == "BTC现货参考（BTC/USDT，24×7）"
    assert items[1]["asset_type"] == "etf"
    assert items[1]["role"] == "US_LISTED_PROXY"
    assert items[1]["proxy_label"] == "美国现货比特币ETP代理（NASDAQ时段）"
    assert {item["rule_id"] for item in items} == {"bitcoin-direct-us-proxy-v1"}


@pytest.mark.parametrize(
    ("ticker", "claim", "expected"),
    [
        ("ETH", "Ethereum network upgrade affects ETH", ["ETH", "ETHA"]),
        ("SOL", "Solana validator incident affects SOL", ["SOL"]),
        ("XRP", "XRP ledger disruption affects XRP", ["XRP"]),
    ],
)
def test_claim_bound_crypto_assets_use_direct_market_before_any_listed_proxy(
    ticker: str, claim: str, expected: list[str]
) -> None:
    items = resolve_event_assets(
        {
            "event_family": "security_incident",
            "event_type": "crypto_network_incident",
            "ticker_at_event": ticker,
            "discovery_source": "opennews_free",
            "facts": {
                "source_shape": "SINGLE_EVENT",
                "event_claim_text": claim,
                "affected_assets": [ticker],
            },
        }
    )

    assert _symbols(items) == expected
    assert items[0]["role"] == "DIRECT_ASSET"
    assert items[0]["provider_symbol"] == f"{ticker}USDT"
    assert all(item["no_trading"] == 1 for item in items)


def test_asset_universe_covers_major_regions_sectors_rates_fx_commodities_and_crypto() -> None:
    registry = load_asset_mapping_policy().asset_registry

    expected = {
        # Direct crypto and listed proxies.
        "BTC", "IBIT", "ETH", "ETHA", "SOL", "XRP", "BNB", "ADA",
        "DOGE", "AVAX", "LINK", "DOT", "LTC", "BCH",
        # Broad markets and countries.
        "SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "VTI", "VT", "ACWI",
        "RSP", "MDY", "EWJ", "EWY",
        "MCHI", "EWH", "EWT", "INDA", "EWA", "EWC", "EWZ", "EWW",
        "EWU", "FEZ", "EWG", "EWQ", "EZA", "EIS", "TUR", "KSA", "UAE", "QAT",
        "EWS", "EIDO", "EWM", "THD", "VNM", "EPHE", "ENZL", "EWN",
        "EWO", "EWK", "EWD", "ENOR", "EDEN", "EFNL", "EPOL", "GREK",
        "EIRL", "PGAL", "ECH", "EPU", "ARGT", "GXG", "EGPT", "PAK", "KWT", "NGE",
        # Sectors and themes.
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
        "XLRE", "XLC", "SMH", "KRE", "XBI", "ITA", "CIBR", "ICLN",
        "SOXX", "IGV", "SKYY", "BOTZ", "PAVE", "JETS", "IYT", "XRT",
        "ITB", "VNQ", "TAN", "KWEB", "XME", "SLX", "SIL", "GDXJ",
        "REMX", "PHO", "MOO", "XOP", "OIH", "KIE", "IHI", "NLR", "DRIV", "FINX",
        # Commodities, rates, credit and FX.
        "GLD", "SLV", "USO", "BNO", "UNG", "DBA", "GDX", "URA", "COPX", "LIT",
        "DBC", "CPER", "PPLT", "PALL", "WEAT", "CORN", "SOYB",
        "SHY", "IEF", "TLT", "TIP", "HYG", "LQD", "UUP", "FXY", "FXE", "FXB",
        "SGOV", "BIL", "BND", "GOVT", "MUB", "EMB", "JNK", "BKLN",
        "FXA", "FXC", "FXF", "CYB", "CEW", "VIXY",
    }

    assert expected <= set(registry)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bank of Japan raises its policy rate", ["EWJ", "FXY"]),
        ("South Korea raises interest rates after an inflation surprise", ["EWY"]),
        ("Hong Kong Monetary Authority tightens policy", ["EWH"]),
        ("China's central bank announces a new reserve policy", ["MCHI"]),
        ("Taiwan growth forecast is revised lower", ["EWT"]),
        ("Reserve Bank of India changes its policy stance", ["INDA"]),
        ("Bank of England cuts rates", ["EWU", "FXB"]),
        ("Germany reports a sharp drop in industrial output", ["EWG"]),
        ("France revises its economic growth forecast", ["EWQ"]),
        ("European Central Bank changes its policy rate", ["FEZ", "FXE"]),
        ("Reserve Bank of Australia changes its policy rate", ["EWA"]),
        ("Bank of Canada cuts its policy rate", ["EWC"]),
        ("Central Bank of Brazil raises its policy rate", ["EWZ"]),
        ("Bank of Mexico cuts its policy rate", ["EWW"]),
        ("South African Reserve Bank changes its policy rate", ["EZA"]),
        ("Central Bank of Turkey raises its policy rate", ["TUR"]),
        ("Bank of Israel changes its policy rate", ["EIS"]),
        ("Saudi Arabia revises its economic growth forecast", ["KSA"]),
        ("UAE revises its economic growth forecast", ["UAE"]),
        ("Qatar central bank changes its policy stance", ["QAT"]),
    ],
)
def test_atomic_country_market_events_map_to_country_proxies(
    title: str, expected: list[str]
) -> None:
    items = resolve_event_assets(
        {
            "event_family": "macro_policy",
            "event_type": "country_policy_action",
            "discovery_source": "opennews_free",
            "facts": {
                "source_shape": "SINGLE_EVENT",
                "event_claim_text": title,
            },
        }
    )

    assert _symbols(items) == expected
    assert all(item["role"] == "THEMATIC_PROXY" for item in items)


def test_country_name_in_company_earnings_does_not_create_a_country_proxy() -> None:
    assert resolve_event_assets(
        {
            "event_family": "earnings",
            "event_type": "quarterly_results",
            "discovery_source": "opennews_free",
            "facts": {
                "source_shape": "SINGLE_EVENT",
                "event_claim_text": "A U.S. retailer said sales in Japan improved this quarter",
            },
        }
    ) == []


def test_multi_country_market_digest_cannot_reach_country_mapping() -> None:
    assert resolve_event_assets(
        {
            "event_family": "regional_market",
            "event_type": "market_roundup",
            "discovery_source": "opennews_free",
            "facts": {
                "source_shape": "MULTI_TOPIC_DIGEST",
                "event_claim_text": "Japan rises while Korea falls and China awaits policy news",
            },
        }
    ) == []


def test_north_korea_reference_cannot_be_misclassified_as_south_korea_market() -> None:
    assert resolve_event_assets(
        {
            "event_family": "country_risk",
            "event_type": "sovereign_risk",
            "discovery_source": "opennews_free",
            "facts": {
                "source_shape": "SINGLE_EVENT",
                "event_claim_text": "North Korea reports a new missile test",
            },
        }
    ) == []


def test_turkey_animal_reference_cannot_reach_country_market_mapping() -> None:
    assert resolve_event_assets(
        {
            "event_family": "market_move",
            "event_type": "food_price_move",
            "discovery_source": "opennews_free",
            "facts": {
                "source_shape": "SINGLE_EVENT",
                "event_claim_text": "Thanksgiving turkey prices fall as poultry supply recovers",
            },
        }
    ) == []


def test_multi_topic_capture_cannot_reach_asset_mapping() -> None:
    items = resolve_event_assets(
        {
            "event_family": "earnings",
            "event_type": "earnings_or_guidance",
            "ticker_at_event": "BTC",
            "discovery_source": "opennews_free",
            "source_title": (
                "Nvidia shares jump after earnings, while Bitcoin tests $80,000"
            ),
            "facts": {
                "source_shape": "MULTI_TOPIC_DIGEST",
                "affected_assets": ["BTC"],
            },
        },
        issuer_directory=_issuer_directory(),
    )

    assert items == []


def test_public_earnings_leading_issuer_resolves_to_direct_security() -> None:
    items = resolve_event_assets(
        {
            "event_family": "earnings",
            "event_type": "earnings_or_guidance",
            "discovery_source": "opennews_free",
            "source_title": "NVIDIA Q2 EARNINGS — REVENUE BEATS ESTIMATES",
            "source_summary": "Data-center revenue and guidance were reported.",
        },
        issuer_directory=_issuer_directory(),
    )

    assert _symbols(items) == ["NVDA", "SPY"]
    assert items[0]["rule_id"] == "resolved-public-company-v1"
    assert "SOURCE_LEADING_ISSUER_EXACT" in items[0]["reason_codes"]
    assert "ISSUER_DIRECTORY_SHA256:" + "a" * 64 in items[0]["reason_codes"]


def test_validated_public_cashtag_resolves_without_guessing_bare_uppercase() -> None:
    items = resolve_event_assets(
        {
            "event_family": "earnings",
            "event_type": "earnings_or_guidance",
            "discovery_source": "opennews_free",
            "source_title": "$NVDA reports quarterly results",
        },
        issuer_directory=_issuer_directory(),
    )

    assert _symbols(items) == ["NVDA", "SPY"]
    assert "SOURCE_VALIDATED_CASHTAG" in items[0]["reason_codes"]


def test_ambiguous_issuer_name_does_not_choose_a_share_class() -> None:
    items = resolve_event_assets(
        {
            "event_family": "earnings",
            "event_type": "earnings_or_guidance",
            "discovery_source": "opennews_free",
            "source_title": "Alphabet reports quarterly results",
        },
        issuer_directory=_issuer_directory(),
    )

    assert items == []


def test_public_issuer_resolution_does_not_override_macro_subject() -> None:
    items = resolve_event_assets(
        {
            "event_family": "macro_policy",
            "event_type": "central_bank_commentary",
            "discovery_source": "opennews_free",
            "source_title": "NVIDIA earnings arrive before a central-bank speech",
        },
        issuer_directory=_issuer_directory(),
    )

    assert items == []


def test_company_maps_to_direct_ticker_and_spy_only() -> None:
    items = resolve_event_assets(
        {
            "event_family": "governance",
            "event_type": "chief_financial_officer_appointment",
            "company_name": "NVIDIA Corporation",
            "ticker_at_event": "NVDA",
            "discovery_source": "sec_current_filings",
            "exchange": "Nasdaq",
            "title": "NVIDIA appointed a new chief financial officer",
            "source_title": "NVIDIA appointed a new chief financial officer",
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
            "discovery_source": "sharadar_active_research",
            "source_title": "SPDR S&P 500 ETF Trust files its annual report",
        }
    )

    assert _symbols(items) == ["SPY"]
    assert items[0]["role"] == "DIRECT_SECURITY"
    assert items[0]["rank"] == 1


def test_exchange_qualified_source_ticker_maps_company_without_canonical_ticker() -> None:
    items = resolve_event_assets(
        {
            "event_family": "regulatory",
            "event_type": "product_recall",
            "company_name": "Baxter International Inc.",
            "discovery_source": "fda_medwatch",
            "source_title": "Baxter International Inc. (NYSE:BAX) announces a recall",
            "source_summary": "The company published the affected product lots.",
        }
    )

    assert _symbols(items) == ["BAX", "SPY"]
    assert items[0]["venue"] == "NYSE"
    assert "SOURCE_EXCHANGE_TICKER" in items[0]["reason_codes"]


def test_exchange_ticker_for_another_company_does_not_map_current_subject() -> None:
    items = resolve_event_assets(
        {
            "event_family": "regulatory",
            "event_type": "product_recall",
            "company_name": "Baxter International Inc.",
            "source_title": "Unrelated Medical Corp. (NYSE:UMC) announces a recall",
        }
    )

    assert items == []


def test_bare_source_ticker_does_not_create_a_direct_asset() -> None:
    items = resolve_event_assets(
        {
            "event_family": "regulatory",
            "event_type": "product_recall",
            "company_name": "Baxter International Inc.",
            "source_title": "BAX announces a recall",
        }
    )

    assert items == []


@pytest.mark.parametrize(
    "event",
    [
        {
            "event_family": "governance",
            "event_type": "management_change",
            "company_name": "OpenAI",
            "ticker_at_event": "NVDA",
            "discovery_source": "sec_current_filings",
            "source_title": "NVIDIA reports quarterly earnings",
        },
        {
            "event_family": "macro_policy",
            "event_type": "central_bank_policy",
            "company_name": "Federal Reserve",
            "ticker_at_event": "NEAR",
            "discovery_source": "sec_current_filings",
            "source_title": "NVIDIA earnings arrive ahead of the Fed decision",
        },
    ],
)
def test_canonical_ticker_without_same_source_company_match_fails_closed(
    event: dict[str, str],
) -> None:
    assert resolve_event_assets(event) == []


def test_canonical_ticker_with_same_source_company_match_is_admitted() -> None:
    items = resolve_event_assets(
        {
            "event_family": "regulatory",
            "event_type": "sec_filing",
            "company_name": "HSBC Holdings plc",
            "ticker_at_event": "HSBC",
            "discovery_source": "sec_current_filings",
            "source_title": "HSBC Holdings plc files Form 6-K",
        }
    )

    assert _symbols(items) == ["HSBC", "SPY"]
    assert "SOURCE_SUBJECT_COHERENT" in items[0]["reason_codes"]


def test_public_news_company_and_ticker_do_not_bypass_trusted_source_gate() -> None:
    items = resolve_event_assets(
        {
            "event_family": "earnings",
            "event_type": "earnings_or_guidance",
            "company_name": "OpenAI",
            "ticker_at_event": "NVDA",
            "discovery_source": "opennews_free",
            "source_title": "OpenAI investment discussed in an NVDA earnings preview",
        }
    )

    assert items == []


def test_exchange_ticker_requires_an_exact_company_token_not_a_substring() -> None:
    items = resolve_event_assets(
        {
            "event_family": "governance",
            "event_type": "management_change",
            "company_name": "Power Solutions International Inc.",
            "source_title": "Empowered board update (NASDAQ:PSIX)",
        }
    )

    assert items == []


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
            "discovery_source": "sec_current_filings",
            "source_title": "Example Inc. announced a management change",
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
        assert item["policy_version"] == "event-asset-mapping-v1.5.0"
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
            lambda payload: payload["rules"][1]["assets"].extend(["SPY", "GLD"]),
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
