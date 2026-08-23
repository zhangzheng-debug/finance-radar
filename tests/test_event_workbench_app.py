from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

import app.web.common as web_common


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "web" / "pages" / "1_Event_Intelligence.py"


def _event(event_id: str, company: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "status": "candidate",
        "event_date": "2026-07-18",
        "event_type": "bankruptcy_or_distress",
        "event_family": "bankruptcy_or_distress",
        "company_name": company,
        "ticker_at_event": "TST",
        "last_updated_at": "2026-07-18T12:34:00+00:00",
        "current_version": 1,
        "manual_grade": "A",
        "discovery_source": "sec_current_filings",
        "evidence_excerpt": f"Evidence summary for {company}",
    }


EVENTS = [_event("event-a", "Alpha Test"), _event("event-b", "Beta Test")]


@pytest.fixture(autouse=True)
def _run_in_admin_ui(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "admin")


def _fake_api(path: str, *, method: str = "GET", json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(path)
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.path == "/api/v1/events/facets":
        return {
            "families": [{"value": "bankruptcy_or_distress", "count": 2}],
            "sources": [{"value": "sec_current_filings", "count": 2}],
            "read_only": True,
            "no_trading": True,
        }
    if parsed.path == "/api/v1/events":
        items = [] if query.get("q") == ["__empty__"] else EVENTS
        return {"items": items, "total": len(items)}
    event_id = parsed.path.split("/")[4] if parsed.path.startswith("/api/v1/events/") else "event-a"
    if parsed.path.endswith("/evidence"):
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
    if parsed.path.endswith("/timeline"):
        return {"items": []}
    if parsed.path.endswith("/trace"):
        return {
            "agent_decisions": [],
            "pipeline_jobs": [],
            "alerts": [],
            "evidence_objects": [],
            "human_overrides": [],
        }
    if parsed.path.startswith("/api/v1/events/"):
        event = next(item for item in EVENTS if item["event_id"] == event_id)
        return {
            "event": event,
            "current_version": {"facts": {"evidence_summary": event["evidence_excerpt"]}},
            "model_shadow_output": {
                "label": "ABSTAIN",
                "confidence": 0.61,
                "model_version": "test-shadow",
                "runtime": "rules",
                "latency_ms": 0.4,
            },
            "market_snapshots": [],
            "market_metrics": [],
        }
    raise AssertionError(f"unexpected API request: {method} {path} {json_body}")


def test_event_workbench_next_button_changes_selected_event(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    assert not page.exception
    assert page.session_state["selected_event_id"] == "event-a"
    next(button for button in page.button if button.label == "J / ↓ 下一条").click()
    page.run()
    assert not page.exception
    assert page.session_state["selected_event_id"] == "event-b"
    assert page.query_params["event_id"] == ["event-b"]
    rendered = "\n".join(str(item.value) for item in page.markdown)
    assert "只读行情上下文" in rendered
    assert "NO REVIEWED ASSET" in rendered
    assert "精确证据段落已在本页显示" in rendered
    assert "确需核对时打开外部原文 E01" in rendered
    assert 'target="_blank"' in rendered
    assert all("打开原始来源" not in button.label for button in page.button)


def test_event_workbench_empty_view_can_reset_without_stale_state(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10)
    page.query_params["flow"] = "全部事件"
    page.query_params["q"] = "__empty__"
    page.run()
    reset = next(button for button in page.button if button.label == "重置为待复核视图")
    reset.click()
    page.run()
    assert not page.exception
    assert "q" not in page.query_params
    assert page.query_params["flow"] == ["待复核"]
    assert page.session_state["selected_event_id"] == "event-a"


def test_event_workbench_failure_state_hides_internal_diagnostics(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise web_common.ApiError("API unavailable at http://internal:8000: private diagnostic")

    monkeypatch.setattr(web_common, "api_request", fail)
    monkeypatch.setattr(web_common, "SHOW_DEBUG", False)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in [*page.markdown, *page.info, *page.error])
    assert not page.exception
    assert "数据服务暂时不可用" in rendered
    assert "internal:8000" not in rendered
    assert "uvicorn" not in rendered


def test_event_workbench_keeps_core_event_and_evidence_visible_when_trace_is_down(monkeypatch) -> None:
    def trace_outage_api(path: str, **kwargs: Any) -> dict[str, Any]:
        if urllib.parse.urlsplit(path).path.endswith("/trace"):
            raise web_common.ApiError("trace endpoint unavailable")
        return _fake_api(path, **kwargs)

    monkeypatch.setattr(web_common, "api_request", trace_outage_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(
        str(item.value) for item in [*page.markdown, *page.info, *page.warning]
    )

    assert not page.exception
    assert "Alpha Test" in rendered
    assert "Exact primary-source passage." in rendered
    assert "附加审计追踪暂不可用" in rendered


def test_event_workbench_external_query_replaces_stale_widget_state(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    flow_widget = next(item for item in page.selectbox if item.key == "event_flow")
    flow_widget.set_value("已核验")
    page.run()
    assert page.query_params["flow"] == ["已核验"]

    page.query_params["flow"] = "全部事件"
    page.query_params["q"] = "Beta"
    page.query_params["source"] = "sec_current_filings"
    page.query_params["limit"] = "50"
    page.query_params["event_id"] = "event-b"
    page.run()
    assert not page.exception
    assert next(item for item in page.selectbox if item.key == "event_flow").value == "全部事件"
    assert next(item for item in page.text_input if item.key == "event_global_query").value == "Beta"
    assert next(item for item in page.selectbox if item.key == "event_source_filter").value == (
        "sec_current_filings"
    )
    assert next(item for item in page.selectbox if item.key == "event_limit").value == 50
    assert page.session_state["selected_event_id"] == "event-b"


def test_event_workbench_labels_its_controlled_audit_and_review_writes() -> None:
    source = PAGE.read_text(encoding="utf-8")

    assert '"READ ONLY"' not in source
    assert "内部 · 受控写入" in source
    assert "确认运行证据代理（受控写入）" in source
    assert "记录人工复核（会写入）" in source
    assert "会写入不可变的人工复核记录" in source
    assert "audit_write_confirmed" in source
    assert "reviewer_attestation" in source
    assert "个人审核凭据" in source
    assert "reviewer_credential=reviewer_credential" in source
    assert '"actor": actor' not in source
    assert "Admin 可查看审计历史，但不能代替个人 Reviewer" in source
    assert "由本人填写，将作为不可变审计记录保存" in source
    assert "我确认已有新增或修订证据，需要创建新的审计记录" in source
    assert "确认重新运行证据代理（受控写入）" in source


def test_event_workbench_keeps_developer_trace_out_of_the_default_reviewer_surface() -> None:
    source = PAGE.read_text(encoding="utf-8")
    audit_marker = 'with st.expander("审计追踪（开发/取证）", expanded=False):'
    debug_gate = "if SHOW_DEBUG:\n        " + audit_marker
    gate_start = source.index(debug_gate)
    audit_start = source.index(audit_marker)
    context_start = source.index("with context_col:")
    audit_surface = source[audit_start:context_start]
    default_context = source[context_start:]

    assert "SHOW_DEBUG," in source
    assert gate_start < audit_start
    assert "仅供开发排障、证据取证和审计复盘" in audit_surface
    assert "provider=" in audit_surface
    assert "流水线追踪与原始事件" in audit_surface
    assert "内部标识、模型与运行时" in audit_surface
    assert "行情审计原始记录" in audit_surface
    assert "render_score_rail" in audit_surface
    assert "运行时=" not in default_context
    assert "render_score_rail" not in default_context
    assert "事件身份" not in default_context
    assert "当前复核状态" in default_context


def test_event_workbench_hides_developer_trace_when_debug_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    monkeypatch.setattr(web_common, "SHOW_DEBUG", False)

    page = AppTest.from_file(str(PAGE), default_timeout=10).run()

    assert not page.exception
    assert not any(expander.label == "审计追踪（开发/取证）" for expander in page.expander)


def test_event_workbench_shows_developer_trace_only_when_debug_is_enabled(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    monkeypatch.setattr(web_common, "SHOW_DEBUG", True)

    page = AppTest.from_file(str(PAGE), default_timeout=10).run()

    assert not page.exception
    assert any(expander.label == "审计追踪（开发/取证）" for expander in page.expander)


def test_event_workbench_requires_new_evidence_confirmation_before_reopening_closed_event(monkeypatch) -> None:
    writes: list[str] = []

    def closed_event_api(
        path: str, *, method: str = "GET", json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        parsed = urllib.parse.urlsplit(path)
        if parsed.path.endswith("/agent/run"):
            assert method == "POST"
            assert json_body == {
                "audit_write_confirmed": True,
                "evidence_change_confirmed": True,
            }
            writes.append(parsed.path)
            return {"status": "EVIDENCE_READY", "trace_id": "trace-1"}
        response = _fake_api(path, method=method, json_body=json_body)
        if parsed.path == "/api/v1/events/event-a":
            response = {**response, "event": {**response["event"], "status": "verified"}}
        return response

    monkeypatch.setattr(web_common, "api_request", closed_event_api)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    submit = next(
        button
        for button in page.button
        if button.label == "确认重新运行证据代理（受控写入）"
    )
    submit.click()
    page.run()

    assert writes == []
    assert any("请先确认确有新增或修订证据" in str(item.value) for item in page.warning)

    confirmation = next(
        checkbox
        for checkbox in page.checkbox
        if checkbox.label == "我确认已有新增或修订证据，需要创建新的审计记录"
    )
    confirmation.set_value(True)
    page.run()
    submit = next(
        button
        for button in page.button
        if button.label == "确认重新运行证据代理（受控写入）"
    )
    submit.click()
    page.run()

    assert writes == ["/api/v1/events/event-a/agent/run"]
