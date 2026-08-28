from __future__ import annotations

from scripts.audit_asset_provider_coverage import (
    BINANCE_EXCHANGE_INFO_URL,
    assess_coverage,
    render_markdown,
    run,
)


def config_fixture() -> dict:
    return {
        "policy_version": "test-v1",
        "asset_registry": {
            "BTC": {
                "asset_type": "crypto",
                "symbol": "BTC",
                "provider_symbol": "BTCUSDT",
                "role": "DIRECT_ASSET",
                "proxy_label": "BTC",
            },
            "IBIT": {
                "asset_type": "etf",
                "symbol": "IBIT",
                "provider_symbol": "IBIT",
                "role": "US_LISTED_PROXY",
                "proxy_label": "IBIT",
            },
            "EWJ": {
                "asset_type": "etf",
                "symbol": "EWJ",
                "provider_symbol": "EWJ",
                "role": "THEMATIC_PROXY",
                "proxy_label": "Japan",
            },
        },
        "rules": [{"id": "btc", "assets": ["BTC", "IBIT"]}],
    }


def test_active_assets_gate_release_but_dormant_assets_only_warn() -> None:
    report = assess_coverage(
        config_fixture(),
        binance_symbols={"BTCUSDT"},
        twelve_data_symbols={"IBIT"},
    )
    assert report["passed"] is True
    assert report["active_assets"] == 2
    assert [row["symbol"] for row in report["dormant_failures"]] == ["EWJ"]
    assert "All active assets supported" in render_markdown(report)


def test_missing_active_symbol_or_provider_error_fails() -> None:
    report = assess_coverage(
        config_fixture(),
        binance_symbols=set(),
        twelve_data_symbols={"IBIT", "EWJ"},
        provider_errors={"binance_public": "timeout"},
    )
    assert report["passed"] is False
    assert report["active_failures"][0]["symbol"] == "BTC"
    assert report["provider_errors"] == {"binance_public": "timeout"}


def test_run_uses_injected_catalog_fetchers(tmp_path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(__import__("json").dumps(config_fixture()), encoding="utf-8")
    report = run(
        path,
        api_key="not-a-real-key",
        timeout=3.0,
        binance_fetcher=lambda timeout, symbols: {"BTCUSDT"},
        twelve_data_fetcher=lambda key, timeout, symbols: {"IBIT", "EWJ"},
    )
    assert report["passed"] is True
    assert report["registry_assets"] == 3


def test_symbol_scoped_provider_error_does_not_hide_supported_assets() -> None:
    report = assess_coverage(
        config_fixture(),
        binance_symbols={"BTCUSDT"},
        twelve_data_symbols={"IBIT"},
        provider_errors={"twelve_data": "one filtered lookup failed"},
        provider_symbol_errors={"twelve_data": {"EWJ": "timeout"}},
    )
    assert report["passed"] is True
    assert [row["status"] for row in report["assets"] if row["symbol"] == "IBIT"] == [
        "SUPPORTED"
    ]
    assert [row["status"] for row in report["assets"] if row["symbol"] == "EWJ"] == [
        "UNKNOWN_PROVIDER_ERROR"
    ]


def test_binance_audit_uses_public_market_data_only_host() -> None:
    assert BINANCE_EXCHANGE_INFO_URL.startswith("https://data-api.binance.vision/")
