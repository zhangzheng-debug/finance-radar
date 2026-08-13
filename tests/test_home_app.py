from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

import app.web.common as web_common


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "web" / "Home.py"


def _overview() -> dict[str, Any]:
    return {
        "demo_mode": "RECENT_CAPTURE",
        "counts": {"canonical_events": 12, "event_evidence": 9},
        "event_status": {"verified": 5, "candidate": 4, "weak": 2, "rejected": 1},
        "review_queue": 6,
        "rough_reviewed": 3,
        "public_funnel": {
            "total": 12,
            "verified": 5,
            "excluded": 1,
            "insufficient": 2,
            "rough_reviewed": 3,
            "pending_verification": 1,
        },
        "job_status": {"COMPLETED_AUTHORIZED_ROUGH_REVIEW": 3},
        "timing": {
            "latest_event_age_seconds": 30,
            "latest_new_event_age_seconds": 30,
            "latest_worker_success_age_seconds": 12,
            "worker_cycle_duration_seconds": 2.5,
        },
        "latest_worker_cycle": {"status": "SUCCESS"},
        "recent_events": [
            {
                "event_id": "event-a",
                "status": "candidate",
                "public_state": "rough_reviewed",
                "reviewed_at": "2026-08-03T23:00:00+00:00",
                "event_family": "enforcement",
                "event_type": "sec_litigation_release",
                "company_name": "Example Holdings",
                "event_date": "2026-08-02",
                "first_seen_at": "2026-08-03T20:00:00+00:00",
                "last_updated_at": "2026-07-18T12:34:00+00:00",
                "credibility_tier": "P0",
                "discovery_source": "sec_current_filings",
                "evidence_excerpt": "Primary-source exact passage.",
            }
        ],
        "source_health": [
            {
                "authority_tier": "P0",
                "cursor_status": "SUCCESS",
                "last_success_at": "2026-07-18T12:34:00+00:00",
                "last_error": None,
            },
            {
                "authority_tier": "P2",
                "cursor_status": "UNOBSERVED",
                "last_success_at": None,
                "last_error": None,
            },
        ],
        "audit": {"no_trading": 0, "no_auto_verify": 0, "no_leakage": 0},
        "schema_version": 12,
        "quick_check": "ok",
    }


def _fake_api(path: str, **_kwargs: Any) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(path)
    if parsed.path == "/api/v1/overview":
        return _overview()
    if parsed.path == "/api/v1/events/facets":
        return {"families": [], "sources": []}
    if parsed.path == "/api/v1/events":
        return {"items": _overview()["recent_events"], "total": 1}
    if parsed.path == "/api/v1/events/event-a/evidence":
        return {
            "items": [
                {
                    "authority_tier": "P0",
                    "source_name": "Official source",
                    "evidence_status": "confirmed",
                    "evidence_passage": "Exact primary-source passage.",
                    "evidence_url": "https://example.test/source",
                }
            ]
        }
    if parsed.path == "/api/v1/events/event-a":
        event = _overview()["recent_events"][0]
        return {
            "event": event,
            "current_version": {"facts": {"evidence_summary": event["evidence_excerpt"]}},
            "preferred_source": {"source_published_at": "2026-08-02T08:30:00+00:00"},
            "verification_method": {
                "reviewed_at": "2026-08-03T23:00:00+00:00",
                "evidence_ids": ["ev-primary-1"],
                "score": 74,
            },
            "model_shadow_output": {"label": "ABSTAIN", "confidence": 0.5},
        }
    raise AssertionError(f"unexpected API request: {path}")


def test_situation_room_prioritizes_event_feed_and_human_queue(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in page.markdown)
    assert not page.exception
    assert "事件浏览" in rendered
    assert "Example Holdings" in rendered
    assert "优先核验队列" in rendered
    assert "先看证据是否足够" in rendered
    assert "证据路径" not in rendered
    assert "UTC" in rendered
    assert any(item.label == "搜索事件" for item in page.text_input)
    assert any(item.label == "应用筛选" for item in page.button)
    assert "系统与来源健康" not in rendered
    assert "Worker" not in rendered
    assert "已粗审" in rendered
    assert "最近成功采集" in rendered
    assert "最近发现新事件" in rendered
    assert "正式结论" in rendered
    assert "核验 5 · 排除 1" in rendered
    assert "待补证 / 复核" in rendered
    assert "实时事件、原始证据与核验进度" not in rendered
    assert "正式处置状态" not in rendered
    assert "Schema" not in rendered
    assert "quick_check" not in rendered
    assert "快捷命令" not in rendered
    assert "运行状态" not in rendered
    assert 'target="_self"' in rendered
    assert 'target="_blank"' not in rendered


def test_home_event_link_opens_inline_preview_before_full_workbench(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10)
    page.query_params["preview_flow"] = "全部事件"
    page.query_params["preview_event_id"] = "event-a"
    page.run()
    rendered = "\n".join(str(item.value) for item in page.markdown)
    assert not page.exception
    assert "当前页事件预览" in rendered
    assert "Exact primary-source passage." in rendered
    assert "阅读提示" in rendered
    assert "粗审已完成，继续核对正式证据" in rendered
    assert "监管执法" in rendered
    assert "SEC 官方文件" in rendered
    assert "原始证据 · 请结合完整文件阅读" in rendered
    assert "发生了什么" in rendered
    assert "为什么关注" in rendered
    assert "粗审已完成，尚未正式核验" in rendered
    assert "时间口径" in rendered
    assert "来源发布" in rendered
    assert "系统发现" in rendered
    assert "核验记录" in rendered
    assert "本轮引用证据 ID" in rendered
    assert "ev-primary-1" in rendered
    assert "已按一级来源证据门槛完成" not in rendered
    assert "sec_litigation_release" not in rendered
    assert "sec_current_filings" not in rendered
    assert ">P0<" not in rendered
    assert any(
        link.label == "打开原始来源（外部网站）" for link in page.get("link_button")
    )
    assert not any("工作台" in button.label or "人工复核" in button.label for button in page.button)
    assert any(button.label == "收起当前页预览" for button in page.button)


def test_public_verified_event_without_receipt_is_labeled_as_historical(monkeypatch) -> None:
    def historical_verified_api(path: str, **kwargs: Any) -> dict[str, Any]:
        data = _fake_api(path, **kwargs)
        if urllib.parse.urlsplit(path).path == "/api/v1/events/event-a":
            event = {
                **data["event"],
                "status": "verified",
                "public_state": "verified",
                "reviewed_at": None,
            }
            return {**data, "event": event, "verification_method": {}}
        return data

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", historical_verified_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10)
    page.query_params["preview_event_id"] = "event-a"
    page.run()
    rendered = "\n".join(str(item.value) for item in page.markdown)

    assert not page.exception
    assert "历史已核验记录 · 核验时间与核验留痕未存档" in rendered
    assert "核验留痕" in rendered
    assert "历史记录未存档" in rendered
    assert "正式核验已完成" not in rendered


def test_public_verified_event_with_receipt_keeps_formal_label(monkeypatch) -> None:
    def recorded_verified_api(path: str, **kwargs: Any) -> dict[str, Any]:
        data = _fake_api(path, **kwargs)
        if urllib.parse.urlsplit(path).path == "/api/v1/events/event-a":
            event = {
                **data["event"],
                "status": "verified",
                "public_state": "verified",
                "reviewed_at": None,
            }
            return {
                **data,
                "event": event,
                "verification_method": {
                    "kind": "light_verification",
                    "reviewed_at": "2026-08-03T23:00:00+00:00",
                    "evidence_ids": ["ev-primary-1"],
                    "score": 74,
                },
            }
        return data

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", recorded_verified_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10)
    page.query_params["preview_event_id"] = "event-a"
    page.run()
    rendered = "\n".join(str(item.value) for item in page.markdown)

    assert not page.exception
    assert "正式核验已完成" in rendered
    assert "历史已核验记录" not in rendered
    assert "核验留痕" not in rendered


def test_public_inline_preview_bounds_long_source_text(monkeypatch) -> None:
    def long_text_api(path: str, **kwargs: Any) -> dict[str, Any]:
        data = _fake_api(path, **kwargs)
        parsed = urllib.parse.urlsplit(path)
        if parsed.path == "/api/v1/events/event-a/evidence":
            data["items"][0]["evidence_passage"] = "Z" * 1500
        elif parsed.path == "/api/v1/events/event-a":
            data["current_version"]["facts"]["evidence_summary"] = "Y" * 800
        return data

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", long_text_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10)
    page.query_params["preview_flow"] = "全部事件"
    page.query_params["preview_event_id"] = "event-a"
    page.run()
    rendered = "\n".join(str(item.value) for item in page.markdown)

    assert not page.exception
    assert "Z" * 901 not in rendered
    assert "Y" * 361 not in rendered
    assert "…" in rendered


def test_public_event_feed_failure_never_substitutes_overview_events(monkeypatch) -> None:
    def failing_feed_api(path: str, **kwargs: Any) -> dict[str, Any]:
        if urllib.parse.urlsplit(path).path == "/api/v1/events":
            raise web_common.ApiError("API unavailable")
        return _fake_api(path, **kwargs)

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", failing_feed_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in [*page.markdown, *page.caption])

    assert not page.exception
    assert "当前筛选的数据暂时不可用" in rendered
    assert "未显示任何替代事件" in rendered
    assert "数据服务暂时不可用" in rendered
    assert "Example Holdings" not in rendered
    assert "当前筛选没有匹配事件" not in rendered
    assert "事件分页" not in rendered


def test_public_collector_marks_missing_success_timestamp_unknown(monkeypatch) -> None:
    def missing_worker_time_api(path: str, **kwargs: Any) -> dict[str, Any]:
        if urllib.parse.urlsplit(path).path == "/api/v1/overview":
            overview = _overview()
            overview["timing"] = {**overview["timing"], "latest_worker_success_age_seconds": None}
            return overview
        return _fake_api(path, **kwargs)

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", missing_worker_time_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(
        str(item.value) for item in [*page.markdown, *page.warning]
    )

    assert not page.exception
    assert "采集状态" in rendered
    assert "更新状态未知" in rendered
    assert "数据更新状态无法确认" in rendered
    assert "不能视为实时信息" in rendered


def test_public_collector_marks_overdue_worker_as_stale_not_realtime(monkeypatch) -> None:
    def stale_worker_api(path: str, **kwargs: Any) -> dict[str, Any]:
        if urllib.parse.urlsplit(path).path == "/api/v1/overview":
            overview = _overview()
            overview["timing"] = {
                **overview["timing"],
                "latest_worker_success_age_seconds": 30 * 60 + 1,
            }
            return overview
        return _fake_api(path, **kwargs)

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", stale_worker_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(
        str(item.value) for item in [*page.markdown, *page.error]
    )

    assert not page.exception
    assert "数据更新已中断" in rendered
    assert "不是实时信息" in rendered
    assert "更新已中断" in rendered
