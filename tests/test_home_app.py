from __future__ import annotations

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
        "timing": {"latest_event_age_seconds": 30, "worker_cycle_duration_seconds": 2.5},
        "recent_events": [
            {
                "event_id": "event-a",
                "status": "candidate",
                "event_family": "enforcement",
                "event_type": "sec_litigation_release",
                "company_name": "Example Holdings",
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


def test_situation_room_prioritizes_event_feed_and_human_queue(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "api_request", lambda *_args, **_kwargs: _overview())
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in page.markdown)
    assert not page.exception
    assert "实时事件流" in rendered
    assert "Example Holdings" in rendered
    assert "等待证据或规则复核" in rendered
    assert "硬边界审计 0 违规" in rendered
    assert "UTC" in rendered
    assert any(item.label == "全终端检索" for item in page.text_input)
    assert any(item.label == "检索 /" for item in page.button)
    assert "快捷命令" in rendered
