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
        "reader_review_queue": 1,
        "reader_hidden_inventory": 5,
        "discovery_backlog": 5,
        "rough_reviewed": 3,
        "public_funnel": {
            "total": 12,
            "verified": 5,
            "excluded": 1,
            "insufficient": 2,
            "rough_reviewed": 3,
            "pending_verification": 1,
        },
        "reader_funnel": {
            "total": 7,
            "verified": 5,
            "excluded": 1,
            "insufficient": 0,
            "rough_reviewed": 1,
            "pending_verification": 0,
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
        "schema_version": 14,
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
                    "evidence_status": "confirmed_primary",
                    "relation_status": "HUMAN_CONFIRMED",
                    "subject_match": 1,
                    "event_claim_supported": 1,
                    "date_coherent": 1,
                    "reader_eligible": 1,
                    "evidence_passage": (
                        "Exact primary-source passage naming the issuer, action, and event stage."
                    ),
                    "evidence_url": "https://example.test/source",
                }
            ]
        }
    if parsed.path == "/api/v1/events/event-a":
        event = _overview()["recent_events"][0]
        return {
            "event": event,
            "current_version": {
                "facts": {
                    "evidence_summary": event["evidence_excerpt"],
                    "claim_subject": "Example Holdings",
                    "claim_action": "sec_litigation_release",
                    "claim_stage": "DISCLOSED",
                    "known_at": "2026-08-03T20:00:00+00:00",
                }
            },
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
    assert "可核验事件队列" in rendered
    assert "另有 5 条历史或发现记录未达到公开可读标准" in rendered
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
    assert "Exact primary-source passage naming the issuer, action, and event stage." in rendered
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
        link.label == "直达本条原始来源（外部网站）" for link in page.get("link_button")
    )
    assert not any("工作台" in button.label or "人工复核" in button.label for button in page.button)
    assert not any(button.label == "收起当前页预览" for button in page.button)
    assert "返回原筛选位置" in rendered
    assert "本次浏览会话首次查看" in "\n".join(str(item.value) for item in page.caption)


def test_public_preview_reports_changes_since_last_view(monkeypatch) -> None:
    revision = {"version": 1}

    def changing_api(path: str, **kwargs: Any) -> dict[str, Any]:
        data = _fake_api(path, **kwargs)
        parsed = urllib.parse.urlsplit(path)
        if parsed.path == "/api/v1/events/event-a":
            data["event"] = {
                **data["event"],
                "current_version": revision["version"],
                "last_updated_at": f"2026-07-1{7 + revision['version']}T12:34:00+00:00",
            }
            data["current_version"] = {
                **data["current_version"],
                "version": revision["version"],
            }
        elif parsed.path == "/api/v1/events/event-a/evidence" and revision["version"] == 2:
            data["items"].append(
                {
                    "evidence_id": "ev-primary-2",
                    "authority_tier": "P0",
                    "source_name": "Official source",
                        "evidence_status": "confirmed",
                        "relation_event_version": 2,
                        "relation_status": "HUMAN_CONFIRMED",
                        "subject_match": 1,
                        "event_claim_supported": 1,
                        "date_coherent": 1,
                        "reader_eligible": 1,
                    "evidence_passage": "A newly published exact passage.",
                    "evidence_url": "https://example.test/source-2",
                }
            )
        return data

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", changing_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10)
    page.query_params["preview_event_id"] = "event-a"
    page.run()
    revision["version"] = 2
    page.run()
    rendered = "\n".join(str(item.value) for item in [*page.markdown, *page.caption])

    assert not page.exception
    assert "自上次查看后的变化" in rendered
    assert "事件版本：1 → 2" in rendered
    assert "关联证据：新增 1 条，移除 0 条" in rendered


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


def test_excluded_preview_distinguishes_capture_from_citable_evidence(monkeypatch) -> None:
    def excluded_capture_api(path: str, **kwargs: Any) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(path)
        if parsed.path == "/api/v1/events/event-a/evidence":
            return {"items": []}
        if parsed.path == "/api/v1/events/event-a/sources":
            return {
                "items": [
                    {
                        "source_name": "OpenNews",
                        "source_type": "aggregated_discovery",
                        "authority_tier": "P2_experimental",
                        "source_title": "Markets await central-bank minutes while gold rises",
                        "source_excerpt": "A provider discovery summary, not a verified policy action.",
                        "source_url": "https://example.test/discovery",
                        "capture_receipt_sha256": "capture-a",
                        "capture_status": "FILTERED_DISCOVERY",
                        "is_citable_evidence": False,
                    }
                ]
            }
        if parsed.path == "/api/v1/events/event-a/source-interpretations":
            return {
                "items": [
                    {
                        "contract_version": "api-capture-interpretation-v1",
                        "event_id": "event-a",
                        "bound_event_version": 1,
                        "capture_receipt_sha256": "capture-a",
                        "source_revision_no": 1,
                        "bound_content_sha256": "a" * 64,
                        "status": "READY",
                        "mode": "DETERMINISTIC",
                        "generated_at": "2026-08-21T00:00:00+00:00",
                        "source_language": "en",
                        "coverage": "TITLE_ONLY",
                        "one_line_zh": "这是一条市场评论，不是一项已经发生的政策行动。",
                        "what_source_says": [
                            {
                                "text_zh": "来源称市场正在等待央行会议纪要。",
                                "quote": "Markets await central-bank minutes",
                            }
                        ],
                        "what_source_does_not_prove_zh": [
                            "没有证明央行已经发布会议纪要。"
                        ],
                        "actors": [],
                        "affected_assets": ["GOLD"],
                        "modality": "COMMENTARY",
                        "why_current_state_zh": "当前线索已按账本规则排除。",
                        "missing_to_change_state_zh": ["需要央行官方网站原文。"],
                        "prompt_injection_suspected": False,
                        "persisted": False,
                        "external_generation_state": "NOT_CONFIGURED",
                        "safety": {
                            "formal_status_mutated": False,
                            "used_as_event_truth": False,
                            "used_as_model_feature": False,
                            "price_used_as_truth": False,
                            "no_trading": True,
                        },
                    }
                ]
            }
        data = _fake_api(path, **kwargs)
        if parsed.path == "/api/v1/events/event-a":
            data["event"] = {
                **data["event"],
                "status": "rejected",
                "public_state": "excluded",
                "reader_ready": 0,
                "citable_evidence_count": 0,
                "captured_source_count": 1,
                "ticker_at_event": "GOLD",
                "company_name": None,
            }
            data["current_version"] = {"facts": {}}
            data["verification_method"] = None
        return data

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", excluded_capture_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10)
    page.query_params["preview_flow"] = "已排除"
    page.query_params["preview_event_id"] = "event-a"
    page.run()
    rendered = "\n".join(str(item.value) for item in page.markdown)

    assert not page.exception
    assert "0 条可引用支持证据" in rendered
    assert "1 条采集来源记录" in rendered
    assert any(
        item.label == "查看采集到的原始线索与内容解读（未核验、非证据）"
        for item in page.expander
    )
    assert "Markets await central-bank minutes while gold rises" in rendered
    assert "API 发现载荷 · 不参与正式结论" in rendered
    assert "这是一条市场评论，不是一项已经发生的政策行动" in rendered
    assert "来源称市场正在等待央行会议纪要" in rendered
    assert "没有证明央行已经发布会议纪要" in rendered
    assert "确定性预览 · 外部模型待接入" in rendered
    assert "保留排除结果，等待真正的新证据" in rendered
    assert any(
        link.label == "查看这条发现来源（非核验证据）"
        for link in page.get("link_button")
    )


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
