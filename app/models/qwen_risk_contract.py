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
QWEN_RISK_PROMPT_VERSION = "qwen-risk-dual-review-consensus-v2"
QWEN_RISK_SYSTEM_PROMPT = (
    "你是金融雷达的语义风险分类器。只判断所给文本表达的极性与做空风险重大性，"
    "不判断证据真假，不补充外部事实，不给投资建议。"
    "已发生或正式披露的破产重组、Form 25或确定退市、现金不足或无法融资将缩减业务、"
    "已发生违约、正式监管处罚、重大内控审计失败、关键临床失败，通常属于"
    "MATERIAL_ADVERSE与ADVERSE；单纯风险因素、合同定义、假设性清算、已解决问题或"
    "有偿并购退市不得仅凭关键词判为重大负面。明确业务改善或成功结果可判POSITIVE，"
    "普通信息披露判NEUTRAL。仅输出指定 JSON。"
)
ADVERSE_STRENGTHS = frozenset({"HIGH", "LOW", "NONE", "UNCLEAR"})
ASSESSMENT_SCOPES = frozenset({"EVIDENCE_SUPPORTED", "SOURCE_CONDITIONAL"})
SEMANTIC_PRIORITIES = frozenset({"PRIORITY_REVIEW", "ROUTINE", "UNDECIDABLE"})


def normalize_qwen_risk_content(content: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize the exact semantic input used for SFT and inference."""

    passages: list[dict[str, Any]] = []
    for item in content.get("passages") or []:
        if not isinstance(item, dict):
            continue
        passage = " ".join(str(item.get("passage") or "").split())
        if not passage:
            continue
        passages.append(
            {
                "document_type": str(item.get("document_type") or "")[:80],
                "item_section": str(item.get("item_section") or "")[:120],
                "published_at": item.get("published_at"),
                "passage": passage[:6000],
            }
        )
    return {
        "as_of": content.get("as_of"),
        "event_date": content.get("event_date"),
        "headline": " ".join(str(content.get("headline") or "").split())[:500],
        "summary": " ".join(str(content.get("summary") or "").split())[:2000],
        "passages": passages[:5],
    }


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
