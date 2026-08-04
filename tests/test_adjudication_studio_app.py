from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

import app.web.common as web_common


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "web" / "pages" / "4_Adjudication_Studio.py"


@pytest.fixture(autouse=True)
def _run_in_admin_ui(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "admin")


def _fake_api(path: str, *, method: str = "GET", json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(path)
    if parsed.path == "/api/v1/adjudication/status":
        return {
            "status": "NOT_READY_FOR_FREEZE",
            "samples": 4,
            "status_counts": {"OPEN": 2, "IN_REVIEW": 1, "CONFLICT": 1},
            "valid_annotations": 0,
            "label_counts": {},
            "label_deficits": {"RISK_REVIEW": 30, "NON_TARGET": 30, "ABSTAIN": 20},
            "source_groups": 0,
            "split": "UNASSIGNED",
            "production_changed": False,
        }
    if parsed.path == "/api/v1/adjudication/queue":
        query = urllib.parse.parse_qs(parsed.query)
        assert query["reviewer_id"] == ["reviewer-a"]
        return {
            "reviewer_id": "reviewer-a",
            "role": "REVIEWER",
            "peer_answers_hidden": True,
            "items": [
                {
                    "sample_id": "v3-test",
                    "event_id": "evt-a",
                    "text_sha256": "a" * 64,
                    "status": "OPEN",
                    "source_token": "src-1234567890",
                    "authority_context": "PRIMARY_OFFICIAL",
                    "review_count": 0,
                    "arbitration_count": 0,
                    "peer_answers_hidden": True,
                    "no_model_prediction_shown": True,
                    "no_market_outcome_shown": True,
                    "own_submission": None,
                    "content": {
                        "headline": "Material issuer disclosure",
                        "summary": "A source-masked summary for independent review.",
                        "confirmed_facts": ["An exact disclosure passage is available."],
                        "passages": [
                            {
                                "authority_class": "PRIMARY_OFFICIAL",
                                "document_type": "8-K",
                                "passage": "The issuer disclosed a material event in the filing.",
                            }
                        ],
                    },
                }
            ],
        }
    raise AssertionError(f"unexpected API request: {method} {path} {json_body}")


def test_adjudication_page_hides_peer_model_and_market_outputs(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    monkeypatch.setenv("FINANCE_RADAR_REVIEW_UI_ENABLED", "1")
    monkeypatch.setenv("FINANCE_RADAR_REVIEW_ACCESS_CODE", "review-secret")
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    assert not page.exception
    access = next(item for item in page.text_input if item.label == "内部审核访问码")
    access.set_value("review-secret")
    page.run()
    reviewer = next(item for item in page.text_input if item.label == "审核者 ID")
    reviewer.set_value("reviewer-a")
    page.run()
    assert not page.exception
    rendered = "\n".join(str(item.value) for item in [*page.markdown, *page.caption, *page.code])
    assert "Material issuer disclosure" in rendered
    assert "同伴答案: HIDDEN" in rendered
    assert "模型 / 行情: HIDDEN" in rendered
    assert "RISK_REVIEW" not in rendered
    assert "NON_TARGET" not in rendered


def test_public_adjudication_page_is_read_only_by_default(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "api_request", _fake_api)
    monkeypatch.delenv("FINANCE_RADAR_REVIEW_UI_ENABLED", raising=False)
    monkeypatch.delenv("FINANCE_RADAR_REVIEW_ACCESS_CODE", raising=False)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in [*page.markdown, *page.info])
    assert not page.exception
    assert "公网部署当前为只读观察模式" in rendered
    assert not any(item.label == "审核者 ID" for item in page.text_input)
    assert not any(item.label == "事件 ID" for item in page.text_input)


def test_adjudication_page_failure_state_hides_internal_details(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise web_common.ApiError("API unavailable at http://private:8000: internal trace")

    monkeypatch.setattr(web_common, "api_request", fail)
    monkeypatch.setattr(web_common, "SHOW_DEBUG", False)
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in [*page.markdown, *page.info, *page.error])
    assert not page.exception
    assert "数据服务暂时不可用" in rendered
    assert "private:8000" not in rendered
