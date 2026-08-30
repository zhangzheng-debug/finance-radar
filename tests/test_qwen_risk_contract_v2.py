from __future__ import annotations

from app.models.qwen_risk_contract_v2 import (
    semantic_priority_v2,
    validate_semantic_v2_payload,
)


def _payload(**overrides):
    value = {
        "materiality": "MATERIAL_ADVERSE",
        "polarity": "ADVERSE",
        "impact_strength": "MODERATE",
        "event_realization": "FORMALLY_DECIDED_OR_COMMITTED",
        "subject_relation": "PRIMARY_SUBJECT",
        "risk_status": "ACTIVE",
        "novelty": "NEW_EVENT_OR_STATUS_CHANGE",
        "reason_codes": [
            "FORMAL_DECISION_OR_BINDING_COMMITMENT",
            "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
            "MATERIAL_DOWNSIDE_MECHANISM",
            "ADVERSE_CONDITION_ACTIVE",
            "ADVERSE_COMPONENT_PRESENT",
            "MODERATE_SOURCE_SUPPORTED_IMPACT",
            "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
            "FORMAL_DELISTING_SUSPENSION_OR_TERMINATION",
        ],
        "brief_reason": "The exchange made a final suspension decision affecting the issuer.",
    }
    value.update(overrides)
    return value


def test_formal_delisting_can_be_material_adverse() -> None:
    assert validate_semantic_v2_payload(_payload()) == []
    assert semantic_priority_v2("MATERIAL_ADVERSE", "ADVERSE") == "PRIORITY_REVIEW"


def test_open_cure_period_is_not_formal_delisting() -> None:
    value = _payload(
        materiality="NOT_MATERIAL_ADVERSE",
        polarity="ADVERSE",
        impact_strength="MINOR",
        event_realization="REALIZED_OR_EFFECTIVE",
        risk_status="CURE_OR_REMEDIATION_PERIOD_OPEN",
        reason_codes=[
            "ACTUAL_EVENT_COMPLETED_OR_EFFECTIVE",
            "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
            "NO_MATERIAL_DOWNSIDE_MECHANISM",
            "CURE_OR_REMEDIATION_PERIOD_STILL_OPEN",
            "ADVERSE_COMPONENT_PRESENT",
            "MINOR_SOURCE_SUPPORTED_IMPACT",
            "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
        ],
        brief_reason="A deficiency notice opened a remediation window but did not delist the issuer.",
    )
    assert validate_semantic_v2_payload(value) == []
    invalid = {**value, "reason_codes": [*value["reason_codes"], "FORMAL_DELISTING_SUSPENSION_OR_TERMINATION"]}
    assert "incoherent:open_cure_period_is_not_final_action" in validate_semantic_v2_payload(invalid)


def test_hypothetical_definition_and_third_party_cannot_be_material_event() -> None:
    value = _payload(
        event_realization="HYPOTHETICAL_OR_CONTRACT_DEFINITION",
        subject_relation="THIRD_PARTY",
        reason_codes=[
            "HYPOTHETICAL_SCENARIO_OR_CONTRACT_DEFINITION",
            "THIRD_PARTY_ONLY_OR_EXTERNAL_TARGET",
            "MATERIAL_DOWNSIDE_MECHANISM",
            "ADVERSE_COMPONENT_PRESENT",
            "MODERATE_SOURCE_SUPPORTED_IMPACT",
            "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
        ],
    )
    issues = validate_semantic_v2_payload(value)
    assert "incoherent:material_adverse_requires_primary_subject" in issues
    assert "incoherent:hypothetical_definition_not_material_event" in issues


def test_duplicate_without_new_status_cannot_be_material_event() -> None:
    value = _payload(
        novelty="DUPLICATE_OR_RESTATEMENT",
        reason_codes=[
            "FORMAL_DECISION_OR_BINDING_COMMITMENT",
            "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
            "MATERIAL_DOWNSIDE_MECHANISM",
            "ADVERSE_CONDITION_ACTIVE",
            "ADVERSE_COMPONENT_PRESENT",
            "MODERATE_SOURCE_SUPPORTED_IMPACT",
            "DUPLICATE_RESTATEMENT_WITHOUT_NEW_FACT",
        ],
        brief_reason="The item repeats the prior decision and adds no new fact or status change.",
    )
    assert (
        "incoherent:duplicate_without_new_fact_not_material_event"
        in validate_semantic_v2_payload(value)
    )


def test_cured_issue_is_positive_or_mixed_not_adverse_only() -> None:
    value = _payload(
        materiality="NOT_MATERIAL_ADVERSE",
        polarity="POSITIVE",
        impact_strength="MODERATE",
        event_realization="REALIZED_OR_EFFECTIVE",
        risk_status="ADVERSE_CONDITION_CURED_OR_REMOVED",
        reason_codes=[
            "ACTUAL_EVENT_COMPLETED_OR_EFFECTIVE",
            "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
            "NO_MATERIAL_DOWNSIDE_MECHANISM",
            "ADVERSE_CONDITION_CURED_OR_REMOVED",
            "POSITIVE_COMPONENT_PRESENT",
            "MODERATE_SOURCE_SUPPORTED_IMPACT",
            "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
        ],
        brief_reason="The source states that the previously reported deficiency was cured.",
    )
    assert validate_semantic_v2_payload(value) == []
    invalid = {**value, "polarity": "ADVERSE"}
    assert "incoherent:cured_condition_not_adverse_only" in validate_semantic_v2_payload(invalid)


def test_adverse_legal_disposition_remains_active_despite_word_resolved() -> None:
    value = _payload(
        risk_status="ACTIVE",
        reason_codes=[
            "ACTUAL_EVENT_COMPLETED_OR_EFFECTIVE",
            "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
            "MATERIAL_DOWNSIDE_MECHANISM",
            "ADVERSE_CONDITION_ACTIVE",
            "ADVERSE_COMPONENT_PRESENT",
            "MODERATE_SOURCE_SUPPORTED_IMPACT",
            "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
        ],
        brief_reason="A consent order resolved the case by imposing permanent bans and remains adverse.",
    )
    assert validate_semantic_v2_payload(value) == []


def test_mixed_requires_real_positive_and_adverse_components() -> None:
    value = _payload(
        polarity="MIXED",
        reason_codes=[
            "FORMAL_DECISION_OR_BINDING_COMMITMENT",
            "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
            "MATERIAL_DOWNSIDE_MECHANISM",
            "ADVERSE_CONDITION_ACTIVE",
            "POSITIVE_AND_ADVERSE_COMPONENTS",
            "MODERATE_SOURCE_SUPPORTED_IMPACT",
            "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
        ],
        brief_reason="A financing extends runway but imposes independently material dilution.",
    )
    assert validate_semantic_v2_payload(value) == []
    invalid = {**value, "reason_codes": [code for code in value["reason_codes"] if code != "POSITIVE_AND_ADVERSE_COMPONENTS"]}
    assert "incoherent:mixed_requires_two_directional_components" in validate_semantic_v2_payload(invalid)


def test_unclear_is_reserved_for_insufficient_text() -> None:
    value = _payload(
        materiality="UNCLEAR",
        polarity="UNCLEAR",
        impact_strength="UNCLEAR",
        event_realization="UNCLEAR",
        subject_relation="UNCLEAR",
        risk_status="UNCLEAR",
        novelty="UNCLEAR",
        reason_codes=[
            "INSUFFICIENT_TEXT_FOR_AXIS",
            "SUBJECT_RELATION_NOT_RESOLVABLE",
            "IMPACT_STRENGTH_UNCLEAR",
        ],
        brief_reason="The excerpt omits the affected subject and whether the action occurred.",
    )
    assert validate_semantic_v2_payload(value) == []
    assert semantic_priority_v2("UNCLEAR", "UNCLEAR") == "UNDECIDABLE"


def test_open_cure_period_can_still_be_material_when_downside_is_moderate() -> None:
    value = _payload(
        event_realization="REALIZED_OR_EFFECTIVE",
        risk_status="CURE_OR_REMEDIATION_PERIOD_OPEN",
        reason_codes=[
            "ACTUAL_EVENT_COMPLETED_OR_EFFECTIVE",
            "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
            "MATERIAL_DOWNSIDE_MECHANISM",
            "CURE_OR_REMEDIATION_PERIOD_STILL_OPEN",
            "ADVERSE_COMPONENT_PRESENT",
            "MODERATE_SOURCE_SUPPORTED_IMPACT",
            "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
        ],
        brief_reason="The current deficiency restricts financing access during an open cure period.",
    )
    assert validate_semantic_v2_payload(value) == []


def test_positive_major_event_is_not_material_adverse_for_core_v1_compatibility() -> None:
    value = _payload(
        materiality="NOT_MATERIAL_ADVERSE",
        polarity="POSITIVE",
        impact_strength="MAJOR",
        risk_status="NO_ADVERSE_CONDITION",
        reason_codes=[
            "ACTUAL_EVENT_COMPLETED_OR_EFFECTIVE",
            "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
            "NO_MATERIAL_DOWNSIDE_MECHANISM",
            "POSITIVE_COMPONENT_PRESENT",
            "MAJOR_SOURCE_SUPPORTED_IMPACT",
            "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
        ],
        brief_reason="A completed cash-premium acquisition delivers a major positive outcome.",
    )
    assert validate_semantic_v2_payload(value) == []


def test_paid_merger_and_non_core_security_cannot_be_material_without_other_trigger() -> None:
    paid = _payload(
        reason_codes=[*_payload()["reason_codes"], "PAID_MERGER_OR_CASH_PREMIUM_EXIT"],
        brief_reason="The compensated merger is adverse only because the listing will end.",
    )
    assert (
        "incoherent:paid_merger_not_adverse_for_loss_of_listing_alone"
        in validate_semantic_v2_payload(paid)
    )
    warrant = _payload(
        reason_codes=[*_payload()["reason_codes"], "NON_CORE_SECURITY_ONLY"],
        brief_reason="Only the issuer warrants are being removed from the exchange.",
    )
    assert (
        "incoherent:non_core_security_not_issuer_material_by_itself"
        in validate_semantic_v2_payload(warrant)
    )


def test_missing_history_context_forces_unclear_novelty() -> None:
    invalid = _payload(
        reason_codes=[*_payload()["reason_codes"], "NOVELTY_CONTEXT_MISSING"]
    )
    assert (
        "incoherent:missing_history_requires_unclear_novelty"
        in validate_semantic_v2_payload(invalid)
    )


def test_brief_reason_cannot_explicitly_contradict_axes() -> None:
    invalid = _payload(
        brief_reason="There is no material adverse event or material downside mechanism."
    )
    assert "incoherent:brief_reason_denies_materiality" in validate_semantic_v2_payload(invalid)


def test_legacy_payload_without_impact_axis_has_explicit_compatibility_path() -> None:
    legacy = _payload()
    legacy.pop("impact_strength")
    legacy["reason_codes"] = [
        code
        for code in legacy["reason_codes"]
        if code != "MODERATE_SOURCE_SUPPORTED_IMPACT"
    ]
    assert "missing:impact_strength" in validate_semantic_v2_payload(legacy)
    assert validate_semantic_v2_payload(
        legacy, allow_legacy_missing_impact_strength=True
    ) == []
