from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

import app.web.common as web_common


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "web" / "pages" / "3_Operations_and_Model.py"


@pytest.fixture(autouse=True)
def _run_in_admin_ui(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "admin")


def _fake_api(
    path: str, *, method: str = "GET", json_body: dict[str, Any] | None = None
) -> dict[str, Any]:
    if path == "/api/v1/health":
        return {
            "status": "ok",
            "demo_mode": "RECENT_CAPTURE",
            "ledger": {
                "quick_check": "ok",
                "audit": {
                    "trading_boundary_violations": 0,
                    "auto_verification_violations": 0,
                    "market_feature_leakage_violations": 0,
                },
            },
            "operations": {
                "latest_worker_cycle": None,
                "latest_backup": None,
                "worker_window_24h": {"status": "NO DATA", "observed_hours": 0, "complete": False},
            },
        }
    if path == "/api/v1/sources/health":
        return {"items": []}
    if path == "/api/v1/model/status":
        return {
            "status": "ready",
            "model_card": None,
            "robustness": None,
            "external_blind": None,
        }
    if path == "/api/v1/market/capabilities":
        return {
            "providers": [
                {
                    "provider_id": "binance_public",
                    "name": "Binance Public Spot",
                    "role": "PERSISTED_EVENT_OBSERVATION",
                    "asset_classes": ["crypto"],
                    "access": "PUBLIC_NONE_AUTH",
                    "deployment": "SERVER_DIRECT",
                    "status": "OBSERVED",
                    "completed_jobs": 2,
                    "pending_jobs": 0,
                    "snapshots": 2,
                    "last_snapshot_at": "2026-07-18T22:56:23Z",
                    "last_error": None,
                    "observation_windows": {
                        "t_plus_5m": {"MISSED_WINDOW": 1},
                        "t_plus_1d": {"PENDING": 1},
                    },
                }
            ],
            "provider_policy": {
                "crypto": "binance_public",
                "non_crypto": "twelve_data",
                "ibkr": "local_capability_probe_only",
            },
            "horizon_policy": {
                "baseline": "version_bound_exact_event_anchor",
                "missed_window_behavior": "record_MISSED_WINDOW_without_latest_quote_substitution",
            },
            "boundary": {"read_only": True, "no_trading": True},
        }
    if path == "/api/v1/evidence/archive":
        return {
            "objects": 8,
            "archived_bytes": 20480,
            "source_snapshots": 3,
            "exact_excerpts": 5,
            "by_mime": {"text/html": {"objects": 3, "bytes": 18000}},
            "recent_objects": [
                {
                    "object_sha256": "a" * 64,
                    "relative_path": "aa/example.html",
                    "mime_type": "text/html",
                    "byte_length": 18000,
                    "source_url": "https://www.sec.gov/example.htm",
                    "event_id": "evt-1",
                    "evidence_id": "evid-1",
                    "object_kind": "SOURCE_SNAPSHOT",
                    "integrity_verified": True,
                }
            ],
            "integrity_failures_in_recent_sample": 0,
            "policy": {"immutable": True, "content_address": "sha256"},
        }
    raise AssertionError(f"unexpected API request: {method} {path} {json_body}")


def test_operations_page_separates_event_sources_and_market_capabilities(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    assert not page.exception
    assert [tab.label for tab in page.tabs][:2] == ["事件源", "行情能力"]
    rendered = "\n".join(str(item.value) for item in [*page.markdown, *page.caption, *page.success])
    tables = "\n".join(str(item.value) for item in page.dataframe)
    market_table = next(
        item.value for item in page.dataframe if "provider" in getattr(item.value, "columns", [])
    )
    assert market_table.iloc[0]["provider"] == "Binance Public Spot"
    assert "不使用账户数据" in rendered
    assert "crypto=binance_public" in rendered
    assert "MISSED_WINDOW=1" in tables
    assert "version_bound_exact_event_anchor" in rendered
    assert "最近证据对象均已重新计算" in rendered
    assert "3 raw" in rendered


def test_operations_page_degrades_when_model_card_metrics_are_missing(monkeypatch) -> None:
    def partial_model_api(
        path: str, *, method: str = "GET", json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if path == "/api/v1/model/status":
            return {
                "status": "ready",
                "model_card": {
                    "dataset": {"rows": 12},
                    "polarity_policy": "shadow only",
                    "limitations": ["human review required"],
                },
                "robustness": None,
                "external_blind": None,
            }
        return _fake_api(path, method=method, json_body=json_body)

    monkeypatch.setattr(web_common, "api_request", partial_model_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in [*page.markdown, *page.warning])

    assert not page.exception
    assert "评估指标尚未生成" in rendered


def test_operations_mode_change_requires_explicit_confirmation(monkeypatch) -> None:
    writes: list[str] = []

    def tracked_api(
        path: str, *, method: str = "GET", json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "POST":
            writes.append(path)
            return {"mode": path.rsplit("/", 1)[-1]}
        return _fake_api(path, method=method, json_body=json_body)

    monkeypatch.setattr(web_common, "api_request", tracked_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    assert not page.exception
    assert any(
        checkbox.label == "我确认要切换运行模式（会写入系统状态）"
        for checkbox in page.checkbox
    )

    page.radio[0].set_value("LIVE")
    page.run()
    submit = next(
        button
        for button in page.button
        if button.label == "确认切换运行模式（受控写入）"
    )
    submit.click()
    page.run()

    assert writes == []
    assert any("请先确认本次运行模式切换会写入系统状态" in str(item.value) for item in page.warning)

    confirmation = next(
        checkbox
        for checkbox in page.checkbox
        if checkbox.label == "我确认要切换运行模式（会写入系统状态）"
    )
    confirmation.set_value(True)
    page.run()
    submit = next(
        button
        for button in page.button
        if button.label == "确认切换运行模式（受控写入）"
    )
    submit.click()
    page.run()

    assert writes == ["/api/v1/demo/mode/LIVE"]
