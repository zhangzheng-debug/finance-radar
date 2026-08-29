"""Versioned semantic supervision contract for the next Qwen risk model.

The v2 contract separates *what the source says* from evidence posture and from
post-event market outcomes.  It is suitable for AI-assisted review and SFT
targets, but it does not certify that the described event is true.

Materiality and polarity are the model targets.  Four mechanism axes and stable
reason codes make those targets auditable and prevent common keyword shortcuts:

* realised event versus proposal, hypothetical or contract definition;
* primary affected subject versus a third party or the general market;
* active downside versus an open cure period or a condition actually cured;
* a new fact/status change versus a duplicate or restatement.
"""

from __future__ import annotations

import re
from typing import Any


QWEN_RISK_CONTRACT_V2_VERSION = "qwen-risk-semantics-v2"

MATERIALITIES_V2 = frozenset(
    {"MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "UNCLEAR"}
)
POLARITIES_V2 = frozenset({"ADVERSE", "POSITIVE", "NEUTRAL", "MIXED", "UNCLEAR"})
IMPACT_STRENGTHS_V2 = frozenset(
    {"MAJOR", "MODERATE", "MINOR", "ROUTINE_OR_NONE", "UNCLEAR"}
)
EVENT_REALIZATIONS_V2 = frozenset(
    {
        "REALIZED_OR_EFFECTIVE",
        "FORMALLY_DECIDED_OR_COMMITTED",
        "PROPOSED_OR_CONDITIONAL",
        "HYPOTHETICAL_OR_CONTRACT_DEFINITION",
        "UNCLEAR",
    }
)
SUBJECT_RELATIONS_V2 = frozenset(
    {"PRIMARY_SUBJECT", "THIRD_PARTY", "GENERAL_MARKET", "UNCLEAR"}
)
RISK_STATUSES_V2 = frozenset(
    {
        "ACTIVE",
        "CURE_OR_REMEDIATION_PERIOD_OPEN",
        "ADVERSE_CONDITION_CURED_OR_REMOVED",
        "NO_ADVERSE_CONDITION",
        "UNCLEAR",
    }
)
NOVELTY_STATES_V2 = frozenset(
    {"NEW_EVENT_OR_STATUS_CHANGE", "DUPLICATE_OR_RESTATEMENT", "UNCLEAR"}
)

# Codes describe only source-text semantics.  Source tier, reviewer identity,
# model output, price response and evidence sufficiency deliberately have no
# representation in this vocabulary.
REASON_CODES_V2 = frozenset(
    {
        "ACTUAL_EVENT_COMPLETED_OR_EFFECTIVE",
        "FORMAL_DECISION_OR_BINDING_COMMITMENT",
        "PROPOSAL_OR_CONDITION_NOT_YET_EFFECTIVE",
        "HYPOTHETICAL_SCENARIO_OR_CONTRACT_DEFINITION",
        "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
        "THIRD_PARTY_ONLY_OR_EXTERNAL_TARGET",
        "GENERAL_MARKET_COMMENTARY_ONLY",
        "SUBJECT_RELATION_NOT_RESOLVABLE",
        "MATERIAL_DOWNSIDE_MECHANISM",
        "NO_MATERIAL_DOWNSIDE_MECHANISM",
        "ADVERSE_CONDITION_ACTIVE",
        "CURE_OR_REMEDIATION_PERIOD_STILL_OPEN",
        "ADVERSE_CONDITION_CURED_OR_REMOVED",
        "FORMAL_DELISTING_SUSPENSION_OR_TERMINATION",
        "ISSUER_COMMON_EQUITY_DIRECTLY_AFFECTED",
        "NON_CORE_SECURITY_ONLY",
        "ADS_OR_CROSS_LISTING_MIGRATION",
        "PAID_MERGER_OR_CASH_PREMIUM_EXIT",
        "SPAC_STRUCTURAL_LIFECYCLE_NOT_TRIGGERED",
        "BANKRUPTCY_LIQUIDATION_OR_DISSOLUTION",
        "GOING_CONCERN_CURRENT_SUBSTANTIAL_DOUBT",
        "PAYMENT_DEFAULT_OR_COVENANT_BREACH",
        "ADVERSE_REGULATORY_OR_LEGAL_DISPOSITION",
        "OPERATING_CESSATION_OR_WIND_DOWN",
        "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
        "DUPLICATE_RESTATEMENT_WITHOUT_NEW_FACT",
        "NOVELTY_CONTEXT_MISSING",
        "FOCAL_SUBJECT_CONTEXT_MISSING",
        "SOURCE_TEXT_TRUNCATED_OR_INCOMPLETE",
        "POSITIVE_COMPONENT_PRESENT",
        "ADVERSE_COMPONENT_PRESENT",
        "POSITIVE_AND_ADVERSE_COMPONENTS",
        "MAJOR_SOURCE_SUPPORTED_IMPACT",
        "MODERATE_SOURCE_SUPPORTED_IMPACT",
        "MINOR_SOURCE_SUPPORTED_IMPACT",
        "ROUTINE_OR_NO_SOURCE_SUPPORTED_IMPACT",
        "IMPACT_STRENGTH_UNCLEAR",
        "INSUFFICIENT_TEXT_FOR_AXIS",
    }
)

V2_REQUIRED_FIELDS = frozenset(
    {
        "materiality",
        "polarity",
        "impact_strength",
        "event_realization",
        "subject_relation",
        "risk_status",
        "novelty",
        "reason_codes",
        "brief_reason",
    }
)


def semantic_priority_v2(materiality: str, polarity: str) -> str:
    """Derive routing priority without treating uncertainty as negative proof."""

    materiality = str(materiality or "").strip().upper()
    polarity = str(polarity or "").strip().upper()
    if materiality == "MATERIAL_ADVERSE" and polarity in {"ADVERSE", "MIXED"}:
        return "PRIORITY_REVIEW"
    if materiality == "UNCLEAR" or polarity == "UNCLEAR":
        return "UNDECIDABLE"
    return "ROUTINE"


def validate_semantic_v2_payload(
    value: Any, *, allow_legacy_missing_impact_strength: bool = False
) -> list[str]:
    """Validate one v2 review result and its boundary-code coherence."""

    if not isinstance(value, dict):
        return ["payload_not_object"]
    issues: list[str] = []
    fields = set(value)
    missing = V2_REQUIRED_FIELDS - fields
    if allow_legacy_missing_impact_strength:
        missing = missing - {"impact_strength"}
    for field in sorted(missing):
        issues.append(f"missing:{field}")
    for field in sorted(fields - V2_REQUIRED_FIELDS):
        issues.append(f"unsupported:{field}")
    if missing:
        return issues

    materiality = str(value.get("materiality") or "").strip().upper()
    polarity = str(value.get("polarity") or "").strip().upper()
    impact_strength = str(value.get("impact_strength") or "").strip().upper()
    realization = str(value.get("event_realization") or "").strip().upper()
    subject = str(value.get("subject_relation") or "").strip().upper()
    risk_status = str(value.get("risk_status") or "").strip().upper()
    novelty = str(value.get("novelty") or "").strip().upper()
    if materiality not in MATERIALITIES_V2:
        issues.append("invalid:materiality")
    if polarity not in POLARITIES_V2:
        issues.append("invalid:polarity")
    if not (allow_legacy_missing_impact_strength and "impact_strength" not in fields):
        if impact_strength not in IMPACT_STRENGTHS_V2:
            issues.append("invalid:impact_strength")
    if realization not in EVENT_REALIZATIONS_V2:
        issues.append("invalid:event_realization")
    if subject not in SUBJECT_RELATIONS_V2:
        issues.append("invalid:subject_relation")
    if risk_status not in RISK_STATUSES_V2:
        issues.append("invalid:risk_status")
    if novelty not in NOVELTY_STATES_V2:
        issues.append("invalid:novelty")

    codes = value.get("reason_codes")
    if not isinstance(codes, list) or not 1 <= len(codes) <= 16:
        issues.append("invalid:reason_codes")
        code_set: set[str] = set()
    else:
        normalized_codes = [str(code or "").strip().upper() for code in codes]
        code_set = set(normalized_codes)
        if len(code_set) != len(normalized_codes):
            issues.append("invalid:duplicate_reason_codes")
        unknown = sorted(code_set - REASON_CODES_V2)
        if unknown:
            issues.append("invalid:reason_code:" + ",".join(unknown))

    reason = value.get("brief_reason")
    if not isinstance(reason, str) or not 12 <= len(reason.strip()) <= 500:
        issues.append("invalid:brief_reason")
        normalized_reason = ""
    else:
        normalized_reason = " ".join(reason.strip().lower().split())

    # The following invariants encode the hard boundaries that failed in v1.
    if materiality == "MATERIAL_ADVERSE":
        if polarity not in {"ADVERSE", "MIXED"}:
            issues.append("incoherent:material_adverse_requires_adverse_or_mixed_polarity")
        if subject != "PRIMARY_SUBJECT":
            issues.append("incoherent:material_adverse_requires_primary_subject")
        if "MATERIAL_DOWNSIDE_MECHANISM" not in code_set:
            issues.append("incoherent:material_adverse_requires_downside_mechanism")
        if realization == "HYPOTHETICAL_OR_CONTRACT_DEFINITION":
            issues.append("incoherent:hypothetical_definition_not_material_event")
        if novelty == "DUPLICATE_OR_RESTATEMENT":
            issues.append("incoherent:duplicate_without_new_fact_not_material_event")
        if impact_strength and impact_strength not in {"MAJOR", "MODERATE"}:
            issues.append("incoherent:material_adverse_requires_major_or_moderate_impact")
    if subject in {"THIRD_PARTY", "GENERAL_MARKET"} and materiality == "MATERIAL_ADVERSE":
        issues.append("incoherent:non_primary_subject_not_material_event")
    if risk_status == "CURE_OR_REMEDIATION_PERIOD_OPEN":
        if "CURE_OR_REMEDIATION_PERIOD_STILL_OPEN" not in code_set:
            issues.append("incoherent:open_cure_period_reason_missing")
        if "FORMAL_DELISTING_SUSPENSION_OR_TERMINATION" in code_set:
            issues.append("incoherent:open_cure_period_is_not_final_action")
        # An open cure period describes status, not severity.  It may be material
        # when the source supplies a concrete downside mechanism, and must never
        # be used as an automatic NOT_MATERIAL_ADVERSE shortcut.
    if risk_status == "ADVERSE_CONDITION_CURED_OR_REMOVED":
        if "ADVERSE_CONDITION_CURED_OR_REMOVED" not in code_set:
            issues.append("incoherent:cured_reason_missing")
        if polarity == "ADVERSE":
            issues.append("incoherent:cured_condition_not_adverse_only")
    if novelty == "DUPLICATE_OR_RESTATEMENT":
        if "DUPLICATE_RESTATEMENT_WITHOUT_NEW_FACT" not in code_set:
            issues.append("incoherent:duplicate_reason_missing")
        if "NEW_MATERIAL_FACT_OR_STATUS_CHANGE" in code_set:
            issues.append("incoherent:duplicate_and_new_status_conflict")
    if novelty == "NEW_EVENT_OR_STATUS_CHANGE" and (
        "NEW_MATERIAL_FACT_OR_STATUS_CHANGE" not in code_set
    ):
        issues.append("incoherent:new_status_reason_missing")
    if polarity == "MIXED" and "POSITIVE_AND_ADVERSE_COMPONENTS" not in code_set:
        issues.append("incoherent:mixed_requires_two_directional_components")
    if polarity == "POSITIVE" and "POSITIVE_COMPONENT_PRESENT" not in code_set:
        issues.append("incoherent:positive_component_reason_missing")
    if polarity == "ADVERSE" and "ADVERSE_COMPONENT_PRESENT" not in code_set:
        issues.append("incoherent:adverse_component_reason_missing")
    strength_code = {
        "MAJOR": "MAJOR_SOURCE_SUPPORTED_IMPACT",
        "MODERATE": "MODERATE_SOURCE_SUPPORTED_IMPACT",
        "MINOR": "MINOR_SOURCE_SUPPORTED_IMPACT",
        "ROUTINE_OR_NONE": "ROUTINE_OR_NO_SOURCE_SUPPORTED_IMPACT",
        "UNCLEAR": "IMPACT_STRENGTH_UNCLEAR",
    }.get(impact_strength)
    if impact_strength and strength_code and strength_code not in code_set:
        issues.append("incoherent:impact_strength_reason_missing")
    if impact_strength == "ROUTINE_OR_NONE" and materiality == "MATERIAL_ADVERSE":
        issues.append("incoherent:routine_impact_not_material_adverse")
    if impact_strength == "UNCLEAR" and "INSUFFICIENT_TEXT_FOR_AXIS" not in code_set:
        issues.append("incoherent:unclear_impact_requires_insufficient_text")
    if (
        materiality == "UNCLEAR"
        or polarity == "UNCLEAR"
        or impact_strength == "UNCLEAR"
    ) and (
        "INSUFFICIENT_TEXT_FOR_AXIS" not in code_set
    ):
        issues.append("incoherent:unclear_requires_insufficient_text")
    if "HYPOTHETICAL_SCENARIO_OR_CONTRACT_DEFINITION" in code_set and (
        realization != "HYPOTHETICAL_OR_CONTRACT_DEFINITION"
    ):
        issues.append("incoherent:hypothetical_reason_realization_mismatch")
    if "FORMAL_DELISTING_SUSPENSION_OR_TERMINATION" in code_set and (
        realization
        not in {"REALIZED_OR_EFFECTIVE", "FORMALLY_DECIDED_OR_COMMITTED"}
    ):
        issues.append("incoherent:formal_action_requires_realized_or_decided")
    if "NON_CORE_SECURITY_ONLY" in code_set and materiality == "MATERIAL_ADVERSE":
        issues.append("incoherent:non_core_security_not_issuer_material_by_itself")
    if "ADS_OR_CROSS_LISTING_MIGRATION" in code_set and materiality == "MATERIAL_ADVERSE":
        hard_downside = code_set & {
            "BANKRUPTCY_LIQUIDATION_OR_DISSOLUTION",
            "GOING_CONCERN_CURRENT_SUBSTANTIAL_DOUBT",
            "PAYMENT_DEFAULT_OR_COVENANT_BREACH",
            "ADVERSE_REGULATORY_OR_LEGAL_DISPOSITION",
            "OPERATING_CESSATION_OR_WIND_DOWN",
        }
        if not hard_downside:
            issues.append("incoherent:cross_listing_exit_not_issuer_material_by_itself")
    if "PAID_MERGER_OR_CASH_PREMIUM_EXIT" in code_set and materiality == "MATERIAL_ADVERSE":
        independent_downside = code_set & {
            "BANKRUPTCY_LIQUIDATION_OR_DISSOLUTION",
            "GOING_CONCERN_CURRENT_SUBSTANTIAL_DOUBT",
            "PAYMENT_DEFAULT_OR_COVENANT_BREACH",
            "ADVERSE_REGULATORY_OR_LEGAL_DISPOSITION",
            "OPERATING_CESSATION_OR_WIND_DOWN",
        }
        if not independent_downside:
            issues.append("incoherent:paid_merger_not_adverse_for_loss_of_listing_alone")
    if "SPAC_STRUCTURAL_LIFECYCLE_NOT_TRIGGERED" in code_set:
        if materiality == "MATERIAL_ADVERSE":
            issues.append("incoherent:untriggered_spac_lifecycle_not_material_event")
        if realization == "REALIZED_OR_EFFECTIVE":
            issues.append("incoherent:untriggered_spac_lifecycle_not_realized")
    if "NOVELTY_CONTEXT_MISSING" in code_set and novelty != "UNCLEAR":
        issues.append("incoherent:missing_history_requires_unclear_novelty")
    if "SOURCE_TEXT_TRUNCATED_OR_INCOMPLETE" in code_set and not (
        "INSUFFICIENT_TEXT_FOR_AXIS" in code_set
        or "UNCLEAR" in {materiality, polarity, realization, risk_status, novelty}
    ):
        issues.append("incoherent:truncated_source_requires_unclear_axis")

    # Keep the human-readable rationale from contradicting the machine axes.
    # These are deliberately narrow phrase checks: they reject explicit semantic
    # contradictions without trying to re-classify free prose.
    if materiality == "MATERIAL_ADVERSE" and re.search(
        r"\b(?:no|not|without) (?:a )?material (?:adverse|downside)",
        normalized_reason,
    ):
        issues.append("incoherent:brief_reason_denies_materiality")
    if materiality == "NOT_MATERIAL_ADVERSE" and re.search(
        r"\b(?:is|constitutes|represents) (?:a )?material adverse (?:event|condition|impact)",
        normalized_reason,
    ):
        issues.append("incoherent:brief_reason_claims_materiality")
    if polarity == "POSITIVE" and re.search(
        r"\bno (?:explicit |independent )?positive component",
        normalized_reason,
    ):
        issues.append("incoherent:brief_reason_denies_positive_polarity")
    if polarity == "ADVERSE" and re.search(
        r"\bno (?:adverse|downside) (?:event|condition|component|mechanism|impact)",
        normalized_reason,
    ):
        issues.append("incoherent:brief_reason_denies_adverse_polarity")
    if polarity == "NEUTRAL" and re.search(
        r"\b(?:is|are|indicating|constitutes?) (?:a )?(?:clearly |strongly )?positive "
        r"(?:development|event|outcome|status change)",
        normalized_reason,
    ):
        issues.append("incoherent:brief_reason_claims_positive_but_axis_neutral")
    return issues
