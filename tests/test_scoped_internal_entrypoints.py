from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import app.web.common as web_common


ROOT = Path(__file__).resolve().parents[1]


def test_reviewer_entrypoint_uses_review_only_data_and_navigation(monkeypatch) -> None:
    calls: list[str] = []

    def fake_api(path: str, **_kwargs):
        calls.append(path)
        if path == "/api/v1/overview":
            return {"review_queue": 3, "counts": {"canonical_events": 12}}
        if path == "/api/v1/adjudication/status":
            return {"status_counts": {"OPEN": 2}}
        raise AssertionError(path)

    monkeypatch.setattr(web_common, "UI_ROLE", "reviewer")
    monkeypatch.setattr(web_common, "api_request", fake_api)
    page = AppTest.from_file(str(ROOT / "app/web/Reviewer.py"), default_timeout=10).run()
    assert not page.exception
    assert calls == ["/api/v1/overview", "/api/v1/adjudication/status"]
    button_labels = {str(item.label) for item in page.button}
    assert button_labels == {"进入事件复核", "进入双人盲审", "查看方法与边界"}


def test_operator_entrypoint_cannot_load_reviewer_workflow(monkeypatch) -> None:
    calls: list[str] = []

    def fake_api(path: str, **_kwargs):
        calls.append(path)
        if path == "/api/v1/health":
            return {
                "status": "ok",
                "operations": {"latest_worker_cycle": {}, "latest_backup": {}},
            }
        if path == "/api/v1/model/status":
            return {"status": "ready"}
        raise AssertionError(path)

    monkeypatch.setattr(web_common, "UI_ROLE", "operator")
    monkeypatch.setattr(web_common, "api_request", fake_api)
    page = AppTest.from_file(str(ROOT / "app/web/Operator.py"), default_timeout=10).run()
    assert not page.exception
    assert calls == ["/api/v1/health", "/api/v1/model/status"]
    button_labels = {str(item.label) for item in page.button}
    assert button_labels == {"进入运行与模型"}


def test_scoped_entrypoints_stop_before_api_for_wrong_role(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", lambda path, **kwargs: calls.append(path))
    for entrypoint in ("Reviewer.py", "Operator.py"):
        page = AppTest.from_file(str(ROOT / "app/web" / entrypoint), default_timeout=10).run()
        assert not page.exception
    assert calls == []
