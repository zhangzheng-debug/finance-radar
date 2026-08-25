"""Semantic contract for the human-gold-trained Qwen risk assessor.

Qwen estimates polarity and downside materiality from captured text.  Evidence
posture remains deterministic and DeepSeek remains a separate summarization
lane.  Keeping these concerns separate prevents a fluent model response from
being mistaken for proof that an event happened.
"""

from __future__ import annotations

from typing import Any

from app.models.risk_label_contract import MATERIALITY, POLARITIES


QWEN_RISK_CONTRACT_VERSION = "qwen-risk-semantics-v1"
QWEN_RISK_PROMPT_VERSION = "qwen-risk-human-gold-sft-v1"
QWEN_RISK_SYSTEM_PROMPT = (
    "你是金融雷达的语义风险分类器。只判断所给文本表达的极性与做空风险重大性，"
    "不判断证据真假，不补充外部事实，不给投资建议。仅输出指定 JSON。"
)
ADVERSE_STRENGTHS = frozenset({"HIGH", "LOW", "NONE", "UNCLEAR"})
ASSESSMENT_SCOPES = frozenset({"EVIDENCE_SUPPORTED", "SOURCE_CONDITIONAL"})
SEMANTIC_PRIORITIES = frozenset({"PRIORITY_REVIEW", "ROUTINE", "UNDECIDABLE"})


def derive_adverse_strength(materiality: str, polarity: str) -> str:
    """Derive the only strength granularity supported by the 720-label kit.

    The reviewers label binary downside materiality, not an invented five-point
    severity scale.  Consequently the honest model target is HIGH/LOW/NONE/
    UNCLEAR; adding MEDIUM or CRITICAL would fabricate supervision the humans
    never supplied.
    """

    normalized_materiality = str(materiality or "").strip().upper()
    normalized_polarity = str(polarity or "").strip().upper()
    if normalized_materiality not in MATERIALITY or normalized_polarity not in POLARITIES:
        raise ValueError("invalid materiality or polarity")
    if "UNCLEAR" in {normalized_materiality, normalized_polarity}:
        return "UNCLEAR"
    if normalized_polarity in {"POSITIVE", "NEUTRAL"}:
        return "NONE" if normalized_materiality == "NOT_MATERIAL_ADVERSE" else "UNCLEAR"
    if normalized_polarity in {"ADVERSE", "MIXED"}:
        return "HIGH" if normalized_materiality == "MATERIAL_ADVERSE" else "LOW"
    return "UNCLEAR"


def semantic_priority(materiality: str, polarity: str) -> str:
    strength = derive_adverse_strength(materiality, polarity)
    if strength == "HIGH":
        return "PRIORITY_REVIEW"
    if strength in {"LOW", "NONE"}:
        return "ROUTINE"
    return "UNDECIDABLE"


def assessment_scope(evidence_state: str) -> str:
    return (
        "EVIDENCE_SUPPORTED"
        if str(evidence_state or "").strip().upper()
        in {"PRIMARY_SUPPORTED", "MULTI_SOURCE_SUPPORTED"}
        else "SOURCE_CONDITIONAL"
    )


def expected_semantic_payload(materiality: str, polarity: str) -> dict[str, str]:
    normalized_materiality = str(materiality or "").strip().upper()
    normalized_polarity = str(polarity or "").strip().upper()
    strength = derive_adverse_strength(normalized_materiality, normalized_polarity)
    return {
        "materiality": normalized_materiality,
        "polarity": normalized_polarity,
        "adverse_strength": strength,
        "semantic_priority": semantic_priority(normalized_materiality, normalized_polarity),
    }


def validate_semantic_payload(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["payload_not_object"]
    allowed = {"materiality", "polarity", "adverse_strength", "semantic_priority"}
    issues: list[str] = []
    extra = sorted(set(value) - allowed)
    missing = sorted(allowed - set(value))
    if extra:
        issues.append("unsupported_fields:" + ",".join(extra))
    if missing:
        issues.append("missing_fields:" + ",".join(missing))
        return issues
    materiality = str(value.get("materiality") or "").upper()
    polarity = str(value.get("polarity") or "").upper()
    if materiality not in MATERIALITY:
        issues.append("invalid_materiality")
    if polarity not in POLARITIES:
        issues.append("invalid_polarity")
    if str(value.get("adverse_strength") or "").upper() not in ADVERSE_STRENGTHS:
        issues.append("invalid_adverse_strength")
    if str(value.get("semantic_priority") or "").upper() not in SEMANTIC_PRIORITIES:
        issues.append("invalid_semantic_priority")
    if not issues:
        expected = expected_semantic_payload(materiality, polarity)
        for field in ("adverse_strength", "semantic_priority"):
            if str(value.get(field) or "").upper() != expected[field]:
                issues.append(f"incoherent_{field}")
    return issues
