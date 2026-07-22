from __future__ import annotations

from app.models.risk_scope_gate import assess_risk_scope


def test_scope_gate_admits_explicit_material_downside() -> None:
    result = assess_risk_scope(
        "The issuer filed a voluntary Chapter 11 bankruptcy petition after a debt default."
    )
    assert result.decision == "ADMIT_RISK_SCOPE"
    assert "bankruptcy" in result.risk_cues


def test_scope_gate_rejects_ai_benchmark_hack_as_operational_noise() -> None:
    result = assess_risk_scope(
        "OpenAI models escaped a secure test environment and hacked a benchmark to cheat on an evaluation."
    )
    assert result.decision == "REJECT_NOISE"
    assert "ai_security_test_not_operational_breach" in result.reason_codes


def test_scope_gate_abstains_on_central_bank_name_collision() -> None:
    result = assess_risk_scope(
        "The Fed rang the alarm about an AI model but had to go months without it."
    )
    assert result.decision == "ADMIT_CONTEXT"


def test_scope_gate_keeps_positive_news_outside_downside_ontology() -> None:
    result = assess_risk_scope(
        "The company posted record revenue, beat estimates, raised guidance and increased its dividend."
    )
    assert result.decision == "REJECT_NON_TARGET"


def test_scope_gate_admits_concrete_enforcement_evidence() -> None:
    result = assess_risk_scope(
        "The SEC complaint seeks permanent injunctive relief, civil penalties and disgorgement."
    )
    assert result.decision == "ADMIT_RISK_SCOPE"
    assert "enforcement" in result.risk_cues


def test_scope_gate_supports_chinese_material_risk_cues() -> None:
    bankruptcy = assess_risk_scope("公司已申请破产清算，法院正式受理相关申请。")
    cyber = assess_risk_scope("交易平台遭遇黑客攻击，部分客户资金被盗。")
    assert bankruptcy.decision == "ADMIT_RISK_SCOPE"
    assert "bankruptcy" in bankruptcy.risk_cues
    assert cyber.decision == "ADMIT_RISK_SCOPE"
    assert "security_incident" in cyber.risk_cues
