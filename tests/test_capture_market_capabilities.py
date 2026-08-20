from __future__ import annotations

from scripts.capture_market_capabilities import assess, render_markdown


def fixture_payload() -> dict:
    return {
        "providers": [
            {
                "provider_id": "binance_public",
                "role": "PERSISTED_EVENT_OBSERVATION",
                "deployment": "SERVER_DIRECT",
                "status": "OBSERVED",
                "completed_jobs": 2,
                "snapshots": 2,
                "last_snapshot_at": "2026-07-18T23:04:08+00:00",
                "last_error": None,
                "read_only": True,
                "order_endpoints_present": False,
            },
            {
                "provider_id": "twelve_data",
                "role": "PERSISTED_EVENT_OBSERVATION",
                "deployment": "SERVER_DIRECT",
                "status": "OBSERVED",
                "completed_jobs": 3,
                "snapshots": 3,
                "last_snapshot_at": "2026-07-18T12:00:00+00:00",
                "last_error": None,
                "read_only": True,
                "order_endpoints_present": False,
            },
            {
                "provider_id": "ibkr_tws_readonly",
                "role": "CAPABILITY_PROBE_ONLY",
                "deployment": "OPERATOR_DESKTOP",
                "status": "LOCAL_PROBE_ONLY",
                "completed_jobs": 0,
                "snapshots": 0,
                "last_snapshot_at": None,
                "last_error": None,
                "read_only": True,
                "order_endpoints_present": False,
            },
        ],
        "provider_policy": {
            "crypto": "binance_public",
            "non_crypto": "twelve_data",
            "ibkr": "local_capability_probe_only",
        },
        "horizon_policy": {
            "baseline": "version_bound_exact_event_anchor",
            "anchor_contract": "market-anchor-v1",
            "known_at_rule": "max_source_published_at_local_received_at",
            "windows": [
                "t_plus_5m",
                "t_plus_30m",
                "t_plus_2h",
                "next_close",
                "t_plus_1d",
                "t_plus_5d",
            ],
            "missed_window_behavior": "record_MISSED_WINDOW_without_latest_quote_substitution",
            "return_metric_scope": "post_event_audit_only",
        },
        "boundary": {
            "read_only": True,
            "no_trading": True,
            "account_data_used": False,
            "post_event_audit_only": True,
            "allowed_as_model_feature": False,
        },
    }


def test_assess_accepts_observed_providers_and_safe_boundary() -> None:
    report = assess(fixture_payload(), "https://example.test/api/v1/market/capabilities")
    assert report["passed"] is True
    assert all(report["checks"].values())
    assert "| binance_public |" in render_markdown(report)
    assert "post-event context only" in render_markdown(report)


def test_assess_rejects_order_endpoint_or_unobserved_binance() -> None:
    payload = fixture_payload()
    payload["providers"][0]["status"] = "UNOBSERVED"
    payload["providers"][1]["order_endpoints_present"] = True
    report = assess(payload, "https://example.test/api/v1/market/capabilities")
    assert report["passed"] is False
    assert report["checks"]["binance_server_observed"] is False
    assert report["checks"]["no_order_endpoints"] is False
