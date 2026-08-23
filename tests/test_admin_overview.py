from __future__ import annotations

from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

import app.web.common as web_common
from app.web.admin_overview import (
    READ_ENDPOINTS,
    fetch_admin_read_snapshot,
    summarize_admin_read_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "web" / "Admin.py"


def _payloads() -> dict[str, dict[str, Any]]:
    return {
        "/api/v1/health": {
            "status": "ok",
            "service_version": "2026.08.23.4",
            "ledger": {
                "backup_snapshot": {
                    "status": "FRESH",
                    "fresh": True,
                    "age_seconds": 3600,
                    "quick_check": "ok",
                    "verified_at": "2026-08-23T01:02:03+00:00",
                    "path_available": True,
                    "artifact_visibility": "visible",
                },
                "audit": {
                    "trading_boundary_violations": 0,
                    "auto_verification_violations": 0,
                    "market_feature_leakage_violations": 0,
                }
            },
            "operations": {
                "counts": {"capture_interpretation_runs": 321},
                "latest_worker_cycle": {"status": "SUCCESS"},
                "latest_verified_backup": {
                    "status": "VERIFIED",
                    "quick_check": "ok",
                    "verified_at": "2026-08-23T01:02:03+00:00",
                },
                "audit_reconciliation": {
                    "status": "ok",
                    "pending_reconciliation": 0,
                    "recovery_conflicts": 0,
                },
            },
        },
        "/api/v1/overview": {
            "timing": {
                "latest_worker_success_age_seconds": 180,
                "latest_worker_success_at": "2026-08-23T03:00:00+00:00",
            },
            "latest_worker_cycle": {"status": "SUCCESS"},
            "reader_quality": {"total": 1200, "citation_ready": 240},
        },
        "/api/v1/sources/health": {
            "items": [
                {"source_id": "sec", "name": "SEC", "cursor_status": "SUCCESS"},
                {
                    "source_id": "macro",
                    "name": "Macro Feed",
                    "cursor_status": "ERROR",
                    "last_error": "bounded failure",
                },
            ]
        },
        "/api/v1/evidence/archive": {
            "coverage": {"coverage_pct": 76.5, "missing_links": 42}
        },
        "/api/v1/model/status": {
            "status": "ready",
            "external_blind": {
                "evaluation_type": "frozen_label_first_external_blind",
                "rows": 80,
                "gate_pass": True,
                "promotion_decision": "QUALIFIED_SHADOW",
            },
            "recent_runs": [{"run_id": "r1"}, {"run_id": "r2"}],
        },
    }


def _fake_api(path: str, **_kwargs: Any) -> dict[str, Any]:
    return _payloads()[path]


def test_owner_summary_uses_independent_read_only_endpoints() -> None:
    requested: list[str] = []

    def request(path: str, **_kwargs: Any) -> dict[str, Any]:
        requested.append(path)
        return _payloads()[path]

    snapshot = fetch_admin_read_snapshot(request)
    summary = summarize_admin_read_snapshot(snapshot)

    assert requested == [path for _, path in READ_ENDPOINTS]
    assert all(path.startswith("/api/v1/") for path in requested)
    assert summary["release"] == {
        "service_version": "2026.08.23.4",
        "release_id": None,
    }
    assert summary["worker"]["last_success_age_seconds"] == 180.0
    assert summary["sources"]["failures"] == 1
    assert summary["sources"]["failure_names"] == ["Macro Feed"]
    assert summary["interpretation"] == {
        "recorded_runs": 321,
        "pending_backlog": None,
        "limitation": "当前只读 API 未提供解读队列的状态分组",
    }
    assert summary["evidence"]["citation_ready"] == 240
    assert summary["evidence"]["archive_coverage_pct"] == 76.5
    assert summary["backup"] == {
        "status": "FRESH",
        "fresh": True,
        "age_seconds": 3600.0,
        "verified_at": "2026-08-23T01:02:03+00:00",
        "quick_check": "ok",
        "path_available": True,
        "artifact_visibility": "visible",
        "last_verified_record_status": "VERIFIED",
        "last_verified_record_at": "2026-08-23T01:02:03+00:00",
    }
    assert summary["model"] == {
        "status": "ready",
        "recent_runs": 2,
        "external_blind_rows": 80,
        "external_blind_gate_pass": True,
        "promotion_decision": "QUALIFIED_SHADOW",
        "evaluation_type": "frozen_label_first_external_blind",
    }
    assert summary["audit"]["boundary_violations"] == 0
    assert summary["audit"]["latest_at"] is None


def test_partial_endpoint_failure_stays_explicit_instead_of_becoming_zero() -> None:
    def request(path: str, **_kwargs: Any) -> dict[str, Any]:
        if path == "/api/v1/health":
            return _payloads()[path]
        raise RuntimeError("temporarily unavailable")

    summary = summarize_admin_read_snapshot(fetch_admin_read_snapshot(request))
    assert summary["unavailable"] == ["overview", "sources", "evidence", "model"]
    assert summary["sources"]["failures"] is None
    assert summary["sources"]["total"] is None
    assert summary["evidence"]["citation_ready"] is None
    assert summary["model"]["external_blind_gate_pass"] is None
    assert summary["interpretation"]["pending_backlog"] is None


def test_stale_current_backup_is_not_overridden_by_verified_history() -> None:
    payloads = _payloads()
    payloads["/api/v1/health"]["ledger"]["backup_snapshot"] = {
        "status": "STALE",
        "fresh": False,
        "age_seconds": 172800,
        "quick_check": "ok",
        "verified_at": "2026-08-21T01:02:03+00:00",
        "path_available": True,
        "artifact_visibility": "visible",
    }

    summary = summarize_admin_read_snapshot(
        fetch_admin_read_snapshot(lambda path, **_kwargs: payloads[path])
    )
    assert summary["backup"]["status"] == "STALE"
    assert summary["backup"]["fresh"] is False
    assert summary["backup"]["last_verified_record_status"] == "VERIFIED"


def test_admin_home_renders_owner_summary_without_dangerous_actions(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "admin")
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in page.markdown)
    rendered += "\n" + "\n".join(str(item.value) for item in page.caption)

    assert not page.exception
    assert any(item.value == "老板总览" for item in page.subheader)
    assert "数据与 API 解读" in rendered
    assert "当前待处理 backlog" in rendered
    assert "当前只读 API 未提供解读队列的状态分组" in rendered
    assert "最近审计" in rendered
    assert "不会根据版本号猜测发布提交" in rendered
    assert any(metric.label == "最近成功采集" and metric.value == "3.0 分钟" for metric in page.metric)
    assert any(metric.label == "正式可引用" and metric.value == "240 / 1,200" for metric in page.metric)
    assert any(metric.label == "盲测门禁" and metric.value == "通过" for metric in page.metric)
    assert "模型覆盖率" not in rendered
    forbidden = ("部署", "重启", "删除", "下单", "交易", "切换运行模式")
    assert not any(any(term in button.label for term in forbidden) for button in page.button)


def test_admin_home_degrades_per_card_when_read_models_are_missing(monkeypatch) -> None:
    def partial_api(path: str, **_kwargs: Any) -> dict[str, Any]:
        if path == "/api/v1/health":
            return _payloads()[path]
        raise RuntimeError("temporarily unavailable")

    monkeypatch.setattr(web_common, "UI_ROLE", "admin")
    monkeypatch.setattr(web_common, "api_request", partial_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in page.markdown)
    rendered += "\n" + "\n".join(str(item.value) for item in page.warning)

    assert not page.exception
    assert any(item.value == "老板总览" for item in page.subheader)
    assert "部分只读数据暂不可用" in rendered
    assert "不可用" in rendered
