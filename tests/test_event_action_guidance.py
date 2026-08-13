from __future__ import annotations

from app.web.components import next_action_guidance, next_action_markup


def _evidence(status: str = "supported", tier: str = "P0") -> dict[str, str]:
    return {"evidence_status": status, "authority_tier": tier}


def test_next_action_prioritizes_conflict_over_model_output() -> None:
    guidance = next_action_guidance(
        {"status": "verified"},
        [_evidence("supported"), _evidence("contradicted", "P1")],
        {"label": "NO_MATERIAL_RISK", "confidence": 0.99},
    )
    assert guidance["code"] == "EVIDENCE_CONFLICT"
    assert guidance["tone"] == "risk"
    assert "冲突" in guidance["title"]
    assert "自动升级" in guidance["reason"]


def test_next_action_keeps_missing_evidence_in_abstention_path() -> None:
    guidance = next_action_guidance(
        {"status": "candidate"},
        [],
        {"label": "RISK_REVIEW", "confidence": 1.0},
    )
    assert guidance["code"] == "MISSING_EVIDENCE"
    assert guidance["priority"] == "先补证据"
    assert any("P0/P1" in step for step in guidance["steps"])
    assert any("会写入审计和关联证据记录" in step for step in guidance["steps"])


def test_next_action_explains_shadow_risk_is_only_review_routing() -> None:
    guidance = next_action_guidance(
        {"status": "candidate"},
        [_evidence()],
        {"label": "RISK_REVIEW", "confidence": 0.96},
        trace={"agent_decisions": [{"status": "complete"}]},
    )
    assert guidance["code"] == "HUMAN_RISK_REVIEW"
    assert "不是交易方向" in guidance["reason"]


def test_next_action_gives_weak_evidence_a_specific_completion_path() -> None:
    guidance = next_action_guidance(
        {"status": "weak"},
        [_evidence("uncertain", "P2")],
        {"label": "ABSTAIN", "confidence": 0.99},
    )
    assert guidance["code"] == "WEAK_EVIDENCE"
    assert guidance["priority"] == "补强证据"
    assert "人工标记证据不足或拒绝" in guidance["steps"][-1]


def test_next_action_markup_is_escaped_and_keeps_hard_boundary() -> None:
    markup = next_action_markup(
        {
            "tone": "invalid",
            "priority": "<script>priority</script>",
            "title": "<img src=x onerror=alert(1)>",
            "reason": "A&B",
            "steps": ("<b>one</b>", "two"),
            "review_recorded": True,
        }
    )
    assert "next-action-watch" in markup
    assert "&lt;script&gt;priority&lt;/script&gt;" in markup
    assert "&lt;img src=x onerror=alert(1)&gt;" in markup
    assert "A&amp;B" in markup
    assert "<script>" not in markup
    assert "不构成交易建议" in markup
    assert "已存在人工复核记录" in markup
